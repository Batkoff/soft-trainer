"""Один HTTP-запрос на оценку. Без SDK, скрытых повторов и подмены модели заглушкой."""
import json
import logging
import re
import socket
import time
from http.client import HTTPException
from urllib import error, request
from django.conf import settings
from .evaluation import PermanentEvaluationError, TemporaryEvaluationError
from .evaluation_profiles import PROMPT_VERSION
from .evaluation_prompt import SYSTEM_PROMPT
from .models import SKILLS

logger = logging.getLogger("trainer.api")
MAX_RESPONSE_BYTES = 256 * 1024
ENDPOINTS = {"openai": "https://api.openai.com/v1/chat/completions",
             "openrouter": "https://openrouter.ai/api/v1/chat/completions"}
FACT_STATES = ("preserved", "omitted", "contradicted", "uncertain")

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Ключ разрешено отправлять только выбранному фиксированному API-хосту.
        return None

def fact_items(assignment):
    fields = ("customer_message", "hard_answer", "required_facts", "allowed_actions", "forbidden_promises")
    if any(not isinstance(assignment.get(key, ""), str) for key in fields):
        raise PermanentEvaluationError("Некорректный текст задания.")
    facts = [line.strip() for line in assignment.get("required_facts", "").splitlines() if line.strip()]
    if not assignment.get("hard_answer", "").strip() or not 1 <= len(facts) <= 30:
        raise PermanentEvaluationError("Заполните hard-ответ и от 1 до 30 обязательных фактов, по одному на строку.")
    if sum(len(assignment.get(key, "")) for key in fields) > 24000:
        raise PermanentEvaluationError("Задание слишком большое для проверки. Сократите его до 24 000 символов.")
    return [{"fact_id": f"fact_{index}", "text": text} for index, text in enumerate(facts, 1)]

def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

def response_schema(facts):
    text = {"type": "string"}
    return object_schema({
        "hard_checks": {"type": "array", "items": object_schema({
            "fact_id": {"type": "string", "enum": [fact["fact_id"] for fact in facts]},
            "state": {"type": "string", "enum": list(FACT_STATES)}, "quote": text, "explanation": text})},
        "unsupported_claims": {"type": "array", "items": object_schema({"quote": text, "explanation": text})},
        "hard_uncertain": {"type": "boolean"}, "uncertainty_note": text,
        "skills": object_schema({key: {"type": "integer", "minimum": 0, "maximum": 100} for key in SKILLS}),
        "skill_notes": object_schema({key: text for key in SKILLS}),
        "strengths": {"type": "array", "items": text}, "improvements": {"type": "array", "items": text},
        "improved_answer": text,
    })

def checked_text(value, limit=1200, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value.strip()):
        raise TemporaryEvaluationError("Модель вернула неполный или некорректный разбор. Проверка повторится.")
    return value.strip()

def checked_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise TemporaryEvaluationError("Формат разбора не соответствует схеме. Проверка повторится.")

def checked_quote(value, answer, required):
    quote = checked_text(value, limit=6000, allow_empty=not required)
    if not quote:
        return ""
    # Модель иногда дописывает точку в самом конце ответа. Это не новая
    # информация: допускаем конечный знак, возвращая именно текст сотрудника.
    # Слова, числа и пунктуацию внутри цитаты не исправляем и не угадываем.
    def find(text, end_of_sentence=False):
        pattern = r"\s+".join(re.escape(word) for word in text.split())
        suffix = r"(?=\s*(?:[.!?…](?!\d)|$))" if end_of_sentence else r"(?!\w|[.,]\d)"
        return re.search(r"(?<!\w)" + pattern + suffix, answer)

    match = find(quote)
    if not match:
        without_ending = quote.rstrip(".!?…")
        if without_ending and without_ending != quote:
            match = find(without_ending, end_of_sentence=True)
    if not match:
        raise TemporaryEvaluationError("Цитата в разборе не найдена в ответе сотрудника.",
                                       metadata={"stage": "quote_validation", "quote": quote})
    return match.group(0)

def normalize_result(raw, data, facts):
    """Валидируем ответ локально даже при strict JSON Schema на стороне провайдера."""
    checked_keys(raw, response_schema(facts)["properties"])
    checked_keys(raw["skills"], SKILLS)
    checked_keys(raw["skill_notes"], SKILLS)
    if any(type(v) is not int or not 0 <= v <= 100 for v in raw["skills"].values()):
        raise TemporaryEvaluationError("Модель вернула оценку вне шкалы 0–100.")
    notes = {key: checked_text(raw["skill_notes"][key]) for key in SKILLS}
    expected = {fact["fact_id"]: fact["text"] for fact in facts}
    if not isinstance(raw["hard_checks"], list) or len(raw["hard_checks"]) != len(facts):
        raise TemporaryEvaluationError("Модель проверила не все обязательные факты.")
    checks, seen = [], set()
    for item in raw["hard_checks"]:
        checked_keys(item, ("fact_id", "state", "quote", "explanation"))
        fact_id, state = item["fact_id"], item["state"]
        if not isinstance(fact_id, str) or fact_id not in expected or fact_id in seen or state not in FACT_STATES:
            raise TemporaryEvaluationError("Модель вернула некорректный список проверенных фактов.")
        seen.add(fact_id)
        try:
            quote = checked_quote(item["quote"], data.answer, required=state in ("preserved", "contradicted"))
        except TemporaryEvaluationError as exc:
            exc.metadata.update(field="hard_checks.quote", fact_id=fact_id)
            raise
        if state == "omitted" and quote:
            raise TemporaryEvaluationError("Противоречивое объяснение пропущенного факта.")
        checks.append({"fact_id": fact_id, "fact": expected[fact_id], "state": state, "quote": quote,
                       "explanation": checked_text(item["explanation"])})
    unsupported = raw["unsupported_claims"]
    if not isinstance(unsupported, list) or len(unsupported) > 20:
        raise TemporaryEvaluationError("Некорректный список неподтверждённых обещаний.")
    claims = []
    for item in unsupported:
        checked_keys(item, ("quote", "explanation"))
        claims.append({"quote": checked_quote(item["quote"], data.answer, required=True),
                       "explanation": checked_text(item["explanation"])})
    if type(raw["hard_uncertain"]) is not bool:
        raise TemporaryEvaluationError("Некорректный результат проверки hard-части.")
    uncertainty = checked_text(raw["uncertainty_note"], allow_empty=not raw["hard_uncertain"])
    if raw["hard_uncertain"] or any(c["state"] == "uncertain" for c in checks):
        verdict = "uncertain"
    elif claims or any(c["state"] in ("omitted", "contradicted") for c in checks):
        verdict = "violated"
    else:
        verdict = "passed"
    feedback = {}
    for key in ("strengths", "improvements"):
        if not isinstance(raw[key], list) or len(raw[key]) > 5:
            raise TemporaryEvaluationError("Некорректный список рекомендаций.")
        feedback[key] = [checked_text(value) for value in raw[key]]
    improved = checked_text(raw["improved_answer"], limit=6000, allow_empty=True)
    return {"schema_version": 2, "is_demo": False, "skill_scale": 100,
            "hard_verdict": verdict, "hard_checks": checks, "unsupported_claims": claims,
            "hard_notes": [item["explanation"] for item in checks+claims] + ([uncertainty] if uncertainty else []),
            "uncertainty_note": uncertainty, "skills": raw["skills"], "skill_notes": notes,
            **feedback, "improved_answer": "" if verdict == "uncertain" else improved}

def usage_from_response(response):
    usage = response.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    return {target: value if type(value := usage.get(source)) is int and value >= 0 else None
            for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"))}

def post_json(provider, key, body, trace=None):
    """Отправить один запрос.

    ``trace`` — необязательный внутренний контейнер для диагностики. В него
    записываем только безопасные данные: JSON-запрос/ответ и код ошибки, но не
    заголовки с API-ключом. Повторами по-прежнему управляет очередь.
    """
    req = request.Request(ENDPOINTS[provider], data=json.dumps(body, ensure_ascii=False).encode(),
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    try:
        with request.build_opener(NoRedirect).open(req, timeout=settings.EVALUATOR_TIMEOUT) as response:
            content = response.read(MAX_RESPONSE_BYTES+1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise TemporaryEvaluationError("Ответ API слишком большой.")
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise TemporaryEvaluationError("API вернул некорректный ответ.")
        if trace is not None:
            trace["response_payload"] = parsed
        return parsed
    except error.HTTPError as exc:
        code = ""
        try:
            details = json.loads(exc.read(MAX_RESPONSE_BYTES))
            if isinstance(details.get("error"), dict):
                code = details["error"].get("code", "")
        except (ValueError, AttributeError, OSError):
            pass
        finally:
            exc.close()
        if trace is not None:
            trace["error"] = {"http_status": exc.code, "provider_code": code}
        if exc.code == 402 or code in ("insufficient_quota", "billing_hard_limit_reached"):
            raise PermanentEvaluationError("У API недостаточно средств или исчерпан бюджет. Проверьте баланс провайдера.") from None
        if exc.code == 429 or exc.code in (408, 409) or exc.code >= 500:
            raise TemporaryEvaluationError("API временно недоступен или ограничил частоту запросов. Проверка повторится.") from None
        if exc.code in (401, 403):
            raise PermanentEvaluationError("API отклонил доступ. Проверьте ключ и разрешения выбранной модели.") from None
        raise PermanentEvaluationError("API отклонил запрос. Проверьте имя модели и поддержку Structured Outputs.") from None
    except (error.URLError, TimeoutError, socket.timeout, HTTPException, ConnectionError):
        if trace is not None:
            trace["error"] = {"error_type": "transport"}
        raise TemporaryEvaluationError("Не удалось дождаться ответа API. Проверка повторится.") from None
    except (ValueError, UnicodeDecodeError):
        if trace is not None:
            trace["error"] = {"error_type": "invalid_json"}
        raise TemporaryEvaluationError("API вернул некорректный JSON. Проверка повторится.") from None

class LiveEvaluator:
    def __init__(self, profile):
        self.profile = profile
        # Задача очереди забирает этот контейнер при ошибке и сохраняет trace.
        # Экземпляр оценщика создаётся на одну задачу, поэтому состояние не смешивается между людьми.
        self.last_trace = {"provider": profile.get("provider", ""), "model": profile.get("model", ""),
                           "prompt_version": profile.get("prompt_version", ""), "request_payload": {},
                           "response_payload": {}}
        if profile.get("prompt_version") != PROMPT_VERSION:
            raise PermanentEvaluationError("Версия промпта этого конкурса недоступна. Нужен администратор.")
        if not isinstance(profile.get("model"), str) or not profile["model"].strip():
            raise PermanentEvaluationError("В профиле не указана модель.")
        if "reasoning_enabled" in profile and type(profile["reasoning_enabled"]) is not bool:
            raise PermanentEvaluationError("Некорректная настройка reasoning в профиле оценки.")

    def evaluate(self, data):
        facts = fact_items(data.assignment)
        if not data.answer.strip():
            # Пустой ответ имеет однозначный результат и не требует платного запроса.
            result = {"schema_version": 2, "is_demo": False, "is_empty": True, "skill_scale": 100,
                    "model": "empty-answer-rule", "provider": "local", "usage": {"input_tokens": 0, "output_tokens": 0},
                    "hard_verdict": "violated", "skills": {key: 0 for key in SKILLS}, "strengths": [],
                    "improvements": ["Ответ не заполнен. В следующем задании начните с сохранения всех обязательных фактов."],
                    "hard_notes": ["Обязательная информация не сообщена: ответ пустой."], "improved_answer": ""}
            self.last_trace = {"provider": "local", "model": "empty-answer-rule", "prompt_version": self.profile["prompt_version"],
                               "request_payload": {"rule": "empty_answer"}, "response_payload": {}, "normalized_payload": result}
            result["_trace"] = self.last_trace
            return result
        provider, model = self.profile["provider"], self.profile["model"]
        from .ai_configuration import api_key
        key = api_key(provider)
        if not key:
            raise PermanentEvaluationError("API-ключ не настроен. Откройте Управление → Нейросеть и сохраните ключ провайдера.")
        task = {name: data.assignment.get(name, "") for name in
                ("customer_message", "hard_answer", "allowed_actions", "forbidden_promises")}
        task.update(required_facts=facts, employee_answer=data.answer)
        body = {"model": model, "temperature": 0, "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(task, ensure_ascii=False)}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "support_evaluation", "strict": True, "schema": response_schema(facts)}}}
        if provider == "openai":
            body.update(max_completion_tokens=settings.EVALUATOR_MAX_TOKENS, store=False)
        else:
            body.update(max_tokens=settings.EVALUATOR_MAX_TOKENS,
                        provider={"require_parameters": True, "allow_fallbacks": False})
            if "reasoning_enabled" in self.profile:
                body["reasoning"] = {"enabled": self.profile["reasoning_enabled"]}
        trace = {"provider": provider, "model": model, "prompt_version": self.profile["prompt_version"],
                 "request_payload": body, "response_payload": {}}
        self.last_trace = trace
        start = time.monotonic()
        response = post_json(provider, key, body, trace=trace)
        # В unit-тестах transport может быть замокан, поэтому заполняем ответ здесь тоже.
        if not trace.get("response_payload"):
            trace["response_payload"] = response
        usage = usage_from_response(response)
        trace.update(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"])
        logger.info("evaluation_api_response", extra={"context": {"attempt_id": data.attempt_id,
            "provider": provider, "model": model, "duration_ms": round((time.monotonic()-start)*1000), **usage}})
        try:
            choice = response["choices"][0]
            message = choice["message"]
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise PermanentEvaluationError("Модель отказалась оценивать ответ. Нужен ручной пересмотр.")
            if choice.get("finish_reason") != "stop":
                raise TemporaryEvaluationError("Разбор модели не завершён. Проверка повторится.")
            raw = json.loads(message["content"])
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise TemporaryEvaluationError("Модель вернула некорректный JSON-разбор. Проверка повторится.") from None
        try:
            result = normalize_result(raw, data, facts)
        except TemporaryEvaluationError as exc:
            trace["error"] = {"stage": "result_validation", **exc.metadata}
            raise
        actual_model = response.get("model")
        result.update(provider=provider, model=model, actual_model=actual_model if isinstance(actual_model, str) else model,
                      prompt_version=self.profile["prompt_version"], usage=usage)
        trace["normalized_payload"] = dict(result)
        result["_trace"] = trace
        return result
