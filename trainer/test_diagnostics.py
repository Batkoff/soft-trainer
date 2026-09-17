"""Логи для диагностики: экспорт, фильтры, доступ и история повторов."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from .models import AuditEvent, EvaluationTrace, Exercise
from .people import apply_role
from .services import start_sandbox
from .tasks import evaluate_attempt


@override_settings(EVALUATOR_BACKEND="demo", DEMO_EVALUATION_DELAY=0,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DiagnosticTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser("admin")
        cls.leader = get_user_model().objects.create_user("leader")
        apply_role(cls.leader, "leader")
        exercise = Exercise.objects.create(title="Возврат", customer_message="Где деньги?",
            hard_answer="До 3 дней.", required_facts="До 3 дней.", status="published")
        cls.attempt = start_sandbox(cls.admin, exercise, "normal", "До 3 дней.")
        evaluate_attempt(str(cls.attempt.pk))
        cls.trace = EvaluationTrace.objects.get(attempt=cls.attempt)
        cls.trace.request_payload = {"messages": [{"role": "system", "content": "ПРОМПТ <script>alert(1)</script>"}]}
        cls.trace.save()
        cls.event = AuditEvent.objects.get(action="evaluation_completed")

    def body(self, response):
        self.assertEqual(response.status_code, 200)
        return b"".join(response.streaming_content).decode("utf-8-sig")

    def test_txt_download_includes_prompt_response_and_current_state(self):
        self.client.force_login(self.admin)
        response = self.client.get(f"/admin/trainer/evaluationtrace/{self.trace.pk}/text/")
        self.assertIn(".txt", response["Content-Disposition"])
        body = self.body(response)
        self.assertIn("ПРОМПТ", body)
        self.assertIn("response_payload", body)
        self.assertIn("normalized_payload", body)
        self.assertIn("Автоматических повторов больше нет", body)
        event_body = self.body(self.client.get(f"/admin/trainer/auditevent/{self.event.pk}/text/"))
        self.assertIn("Система (таймер / очередь)", event_body)
        self.assertIn("ПРОМПТ", event_body)

    def test_export_respects_filters_and_selected_records(self):
        self.client.force_login(self.admin)
        body = self.body(self.client.get("/admin/trainer/auditevent/export/?action__exact=evaluation_completed"))
        self.assertIn("[evaluation_completed]", body)
        self.assertNotIn("[attempt_submitted]", body)
        body = self.body(self.client.post("/admin/trainer/auditevent/", {
            "action": "export_txt", "_selected_action": [self.event.pk]}))
        self.assertIn("[evaluation_completed]", body)
        self.assertNotIn("[attempt_submitted]", body)

    def test_admin_cards_are_readable_escape_prompt_and_explain_old_retry(self):
        self.trace.status = "retry"
        self.trace.error_message = "Ошибка цитаты. Проверка повторится."
        self.trace.save()
        self.client.force_login(self.admin)
        page = self.client.get(f"/admin/trainer/evaluationtrace/{self.trace.pk}/change/")
        self.assertContains(page, "Сбой · назначен повтор")
        self.assertContains(page, "Автоматических повторов больше нет")
        self.assertNotContains(page, "Проверка повторится.")
        self.assertNotContains(page, "<script>alert(1)</script>")
        page = self.client.get(f"/admin/trainer/auditevent/{self.event.pk}/change/")
        self.assertContains(page, "Оценка получена")
        self.assertContains(page, "admin-status-success")
        self.assertContains(page, "Открыть запрос")
        self.assertNotContains(page, "AuditEvent object")
        self.assertContains(self.client.get("/admin/trainer/evaluationtrace/"), "Скачать этот список .txt")

    def test_staff_without_diagnostic_permission_cannot_download_any_format(self):
        self.client.force_login(self.leader)
        for url in [f"/admin/trainer/evaluationtrace/{self.trace.pk}/download/",
                    f"/admin/trainer/evaluationtrace/{self.trace.pk}/text/",
                    "/admin/trainer/evaluationtrace/export/", "/admin/trainer/auditevent/export/",
                    f"/admin/trainer/auditevent/{self.event.pk}/text/"]:
            self.assertEqual(self.client.get(url).status_code, 403, url)
