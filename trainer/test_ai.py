"""Контракт API и ошибки проверяем без платных запросов и без настоящих ключей."""
import io
import json
from urllib.error import HTTPError, URLError
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase, override_settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from .evaluation import EvaluationInput, DemoEvaluator, PermanentEvaluationError, TemporaryEvaluationError, calculate_score, get_evaluator
from .evaluation_http import LiveEvaluator, checked_quote, fact_items, normalize_result, post_json
from .evaluation_profiles import current_profile, FREE_OPENROUTER_MODEL
from .models import Attempt, Evaluation, EvaluationTrace, Exercise, SKILLS, default_rubric, demo_rubric
from .services import retry_attempt, start_sandbox
from .tasks import evaluate_attempt

AI_SETTINGS = {"EVALUATOR_BACKEND": "openai", "EVALUATOR_MODEL": "gpt-4.1-mini-2025-04-14",
               "OPENAI_API_KEY": "unit-test-key-no-network", "OPENROUTER_API_KEY": "unit-test-router-key", "DEMO_EVALUATION_DELAY": 0}
ANSWER = "Возврат обрабатывается. Деньги поступят в течение 3 рабочих дней."
TASK = {"customer_message": "Где возврат?", "hard_answer": "Возврат обрабатывается до 3 рабочих дней.",
        "required_facts": "Возврат обрабатывается.\nДо 3 рабочих дней.", "allowed_actions": "", "forbidden_promises": "Не обещать сегодня."}

def model_result():
    return {"hard_checks": [
        {"fact_id": "fact_1", "state": "preserved", "quote": "Возврат обрабатывается.", "explanation": "Статус сохранён."},
        {"fact_id": "fact_2", "state": "preserved", "quote": "в течение 3 рабочих дней", "explanation": "Срок сохранён."}],
        "unsupported_claims": [], "hard_uncertain": False, "uncertainty_note": "",
        "skills": {key: 80 for key in SKILLS}, "skill_notes": {key: "Краткое обоснование." for key in SKILLS},
        "strengths": ["Понятно изложен срок."], "improvements": ["Можно признать ожидание клиента."], "improved_answer": ANSWER}

def api_response(raw=None):
    return {"model": "gpt-4.1-mini-2025-04-14", "usage": {"prompt_tokens": 1400, "completion_tokens": 600},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(raw or model_result())}}]}

@override_settings(ALLOW_TEST_EVALUATOR=True, **AI_SETTINGS)
class APIContractTests(SimpleTestCase):
    def setUp(self):
        self.data = EvaluationInput(TASK, ANSWER, default_rubric(), attempt_id="test-attempt")
        self.evaluator = LiveEvaluator(current_profile())

    @patch("trainer.evaluation_http.post_json")
    def test_one_structured_request_and_server_score(self, post):
        post.return_value = api_response()
        result = self.evaluator.evaluate(self.data)
        self.assertEqual(calculate_score(result, ANSWER, self.data.rubric), 80)
        post.assert_called_once()
        provider, key, body = post.call_args.args
        self.assertEqual(provider, "openai")
        self.assertFalse(body["store"])
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        task = json.loads(body["messages"][1]["content"])
        self.assertEqual(set(task), {"customer_message", "hard_answer", "allowed_actions", "forbidden_promises", "required_facts", "employee_answer"})
        self.assertNotIn(key, json.dumps(body))
        self.assertEqual(result["usage"]["input_tokens"], 1400)
        trace = result["_trace"]
        self.assertEqual(trace["request_payload"], body)
        self.assertEqual(trace["response_payload"], api_response())
        self.assertIn("Ты — оценщик учебных ответов", body["messages"][0]["content"])
        self.assertEqual(trace["input_tokens"], 1400)

    def test_hard_violation_gives_zero_and_preserves_soft(self):
        raw = model_result()
        raw["hard_checks"][1].update(state="contradicted", explanation="Срок искажён.")
        result = normalize_result(raw, self.data, fact_items(TASK))
        self.assertEqual(calculate_score(result, ANSWER, self.data.rubric), 0)
        self.assertEqual(result["skills"]["empathy"], 80)

    def test_unsupported_promise_gives_zero(self):
        raw = model_result()
        raw["unsupported_claims"] = [{"quote": "Деньги поступят", "explanation": "Контроль неподтверждённого обещания."}]
        result = normalize_result(raw, self.data, fact_items(TASK))
        self.assertEqual(calculate_score(result, ANSWER, self.data.rubric), 0)

    def test_uncertainty_has_no_score_and_no_suggested_answer(self):
        raw = model_result()
        raw.update(hard_uncertain=True, uncertainty_note="Условия противоречат друг другу.")
        result = normalize_result(raw, self.data, fact_items(TASK))
        self.assertIsNone(calculate_score(result, ANSWER, self.data.rubric))
        self.assertEqual(result["improved_answer"], "")

    def test_malformed_feedback_never_becomes_an_employee_score(self):
        invalid = []
        raw = model_result(); raw["hard_checks"].pop(); invalid.append(raw)
        raw = model_result(); raw["hard_checks"][1]["fact_id"] = "fact_1"; invalid.append(raw)
        raw = model_result(); raw["hard_checks"][0]["quote"] = "Выдуманная цитата"; invalid.append(raw)
        raw = model_result(); raw["skills"]["tone"] = True; invalid.append(raw)
        raw = model_result(); raw["skills"]["tone"] = 101; invalid.append(raw)
        raw = model_result(); raw["skills"].pop("tone"); invalid.append(raw)
        raw = model_result(); raw["extra"] = 100; invalid.append(raw)
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(TemporaryEvaluationError):
                normalize_result(raw, self.data, fact_items(TASK))

    def test_quote_with_model_added_final_dot_uses_actual_employee_text(self):
        answer = "Обращение зарегистрировано. Рассмотрение занимает до 5 рабочих дней. Дополнительные документы сейчас не требуются"
        quote = "Дополнительные документы сейчас не требуются."
        self.assertEqual(checked_quote(quote, answer, required=True), quote[:-1])
        self.assertEqual(checked_quote("до 5 рабочих дней.", "до 5\nрабочих дней", True), "до 5\nрабочих дней")

    def test_quote_tolerance_does_not_accept_changed_words_numbers_or_decimal_prefix(self):
        for quote, answer in [("до 5 дней", "до 15 дней"), ("5.", "15."), ("5.", "5.5 дня"),
                              ("Документы требуются.", "Документы не требуются"),
                              ("Вернём сегодня.", "Вернём завтра"), ("До 5 дней.", "До 5 дней или дольше")]:
            with self.subTest(quote=quote, answer=answer), self.assertRaises(TemporaryEvaluationError):
                checked_quote(quote, answer, True)

    @patch("trainer.evaluation_http.post_json")
    def test_invalid_quote_trace_identifies_field_and_fact(self, post):
        raw = model_result()
        raw["hard_checks"][1]["quote"] = "Вернём сегодня"
        post.return_value = api_response(raw)
        with self.assertRaises(TemporaryEvaluationError):
            self.evaluator.evaluate(self.data)
        error = self.evaluator.last_trace["error"]
        self.assertEqual((error["stage"], error["fact_id"], error["field"]),
                         ("quote_validation", "fact_2", "hard_checks.quote"))

    @patch("trainer.evaluation_http.post_json")
    def test_refusal_incomplete_json_and_wrong_message(self, post):
        for message, finish, error in [({"refusal": "No"}, "stop", PermanentEvaluationError),
                ({"content": "{}"}, "length", TemporaryEvaluationError),
                ({"content": "not-json"}, "stop", TemporaryEvaluationError),
                (None, "stop", TemporaryEvaluationError)]:
            post.return_value = {"choices": [{"message": message, "finish_reason": finish}]}
            with self.subTest(message=message), self.assertRaises(error):
                self.evaluator.evaluate(self.data)

    @patch("trainer.evaluation_http.post_json")
    def test_empty_answer_needs_no_api(self, post):
        data = EvaluationInput(TASK, "  ", default_rubric())
        result = self.evaluator.evaluate(data)
        self.assertEqual(calculate_score(result, data.answer, data.rubric), 0)
        post.assert_not_called()

    @override_settings(ALLOW_TEST_EVALUATOR=True, OPENAI_API_KEY="")
    @patch("trainer.evaluation_http.post_json")
    def test_missing_key_is_not_demo_fallback(self, post):
        with self.assertRaises(PermanentEvaluationError):
            self.evaluator.evaluate(self.data)
        post.assert_not_called()

    def test_legacy_rubric_stays_demo_after_environment_switch(self):
        self.assertIsInstance(get_evaluator(demo_rubric()), DemoEvaluator)
        frozen = default_rubric()
        with override_settings(EVALUATOR_MODEL="another-model"):
            self.assertEqual(get_evaluator(frozen).profile["model"], AI_SETTINGS["EVALUATOR_MODEL"])

    @override_settings(ALLOW_TEST_EVALUATOR=True, EVALUATOR_BACKEND="openrouter", EVALUATOR_MODEL="openai/gpt-4.1-mini")
    @patch("trainer.evaluation_http.post_json", return_value=api_response())
    def test_openrouter_requires_schema_without_provider_fallback(self, post):
        LiveEvaluator(current_profile()).evaluate(self.data)
        body = post.call_args.args[2]
        self.assertTrue(body["provider"]["require_parameters"])
        self.assertFalse(body["provider"]["allow_fallbacks"])
        self.assertIn("max_tokens", body)
        self.assertNotIn("reasoning", body)  # GPT-4.1 не получает лишний параметр.

    @override_settings(ALLOW_TEST_EVALUATOR=True, EVALUATOR_BACKEND="openrouter", EVALUATOR_MODEL=FREE_OPENROUTER_MODEL)
    @patch("trainer.evaluation_http.post_json", return_value=api_response())
    def test_free_model_pins_reasoning_setting_and_does_not_fall_back_to_paid(self, post):
        rubric = default_rubric()
        self.assertIs(rubric["evaluator"]["reasoning_enabled"], False)
        with override_settings(EVALUATOR_MODEL="openai/gpt-4.1-mini"):
            get_evaluator(rubric).evaluate(self.data)
        body = post.call_args.args[2]
        self.assertEqual(body["model"], FREE_OPENROUTER_MODEL)
        self.assertEqual(body["reasoning"], {"enabled": False})
        self.assertFalse(body["provider"]["allow_fallbacks"])
        self.assertNotIn("models", body)

    @patch("trainer.evaluation_http.request.build_opener")
    def test_transport_classifies_errors_and_hides_response_secrets(self, opener):
        for status, code, expected in [(429, "rate_limit_exceeded", TemporaryEvaluationError),
                (429, "insufficient_quota", PermanentEvaluationError), (503, "", TemporaryEvaluationError),
                (401, "", PermanentEvaluationError), (402, "", PermanentEvaluationError), (400, "", PermanentEvaluationError)]:
            body = io.BytesIO(json.dumps({"error": {"code": code, "message": "PRIVATE"}}).encode())
            opener.return_value.open.side_effect = HTTPError("https://api.openai.com/", status, "PRIVATE", {}, body)
            with self.subTest(status=status, code=code), self.assertRaises(expected) as caught:
                trace = {}
                post_json("openai", "SECRET-KEY", {}, trace=trace)
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertNotIn("SECRET-KEY", str(caught.exception))
            self.assertEqual(trace["error"]["http_status"], status)
            self.assertEqual(trace["error"]["provider_code"], code)
        opener.return_value.open.side_effect = URLError("PRIVATE")
        with self.assertRaises(TemporaryEvaluationError):
            post_json("openai", "SECRET-KEY", {})

@override_settings(ALLOW_TEST_EVALUATOR=True, **AI_SETTINGS)
class AIQueueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser("api-admin")
        cls.exercise = Exercise.objects.create(title="Возврат", **TASK, status="published")

    def test_uncertain_result_saved_for_review_and_cannot_be_replaced_by_retry(self):
        attempt = start_sandbox(self.admin, self.exercise, "real", ANSWER)
        raw = model_result()
        raw.update(hard_uncertain=True, uncertainty_note="Требуется уточнение.")
        with patch("trainer.evaluation_http.post_json", return_value=api_response(raw)) as post:
            evaluate_attempt(str(attempt.pk))
            evaluate_attempt(str(attempt.pk))
        post.assert_called_once()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "review")
        self.assertIsNone(attempt.score)
        self.assertEqual(attempt.evaluation.payload["skills"]["clarity"], 80)
        trace = EvaluationTrace.objects.get(attempt=attempt)
        self.assertEqual(trace.status, "review")
        self.assertIn("Ты — оценщик учебных ответов", trace.request_payload["messages"][0]["content"])
        self.assertEqual(trace.response_payload["usage"]["completion_tokens"], 600)
        with self.assertRaises(ValidationError):
            retry_attempt(self.admin, attempt.pk)

    def test_rate_limit_retries_then_preserves_answer_without_score(self):
        attempt = start_sandbox(self.admin, self.exercise, "real", ANSWER)
        with patch("trainer.evaluation_http.post_json", side_effect=TemporaryEvaluationError("Временный сбой")):
            for _ in range(2):
                with self.assertRaises(TemporaryEvaluationError):
                    evaluate_attempt(str(attempt.pk))
            evaluate_attempt(str(attempt.pk))
        attempt.refresh_from_db()
        self.assertEqual((attempt.status, attempt.score, attempt.answer), ("review", None, ANSWER))
        self.assertFalse(Evaluation.objects.filter(attempt=attempt).exists())
        self.assertEqual(EvaluationTrace.objects.filter(attempt=attempt).count(), 3)
        self.assertEqual(EvaluationTrace.objects.filter(attempt=attempt, status="retry").count(), 2)
        self.assertEqual(EvaluationTrace.objects.filter(attempt=attempt, status="failed").count(), 1)
        retry_attempt(self.admin, attempt.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.evaluation_tries, 0)

    def test_simulation_remains_free_even_when_real_api_is_configured(self):
        attempt = start_sandbox(self.admin, self.exercise, "hard_error", ANSWER)
        with patch("trainer.evaluation_http.post_json") as post:
            evaluate_attempt(str(attempt.pk))
        post.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 0)
        self.assertEqual(attempt.evaluation.backend, "demo")
