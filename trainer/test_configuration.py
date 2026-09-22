"""Настройки подключения, отсутствие подставных баллов и восстановление доступа."""
import io
import json
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from .ai_configuration import api_key
from .evaluation import get_evaluator, PermanentEvaluationError
from .evaluation_profiles import current_profile
from .models import AIConfiguration, AuditEvent, demo_rubric


@override_settings(ALLOW_TEST_EVALUATOR=False, OPENROUTER_API_KEY="", OPENAI_API_KEY="",
                   EVALUATOR_BACKEND="demo", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ConfigurationTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("admin", password="initial-test-password")
        self.staff = get_user_model().objects.create_user("leader", is_staff=True)

    def save_settings(self, **values):
        self.client.force_login(self.admin)
        return self.client.post("/settings/ai/", {"provider": "openrouter", "model_choice": "openai/gpt-4.1-mini", **values})

    def test_only_superuser_can_read_or_change_settings(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/settings/ai/").status_code, 403)
        self.assertEqual(self.client.post("/settings/ai/", {}).status_code, 403)
        self.assertFalse(AIConfiguration.objects.exists())

    def test_secret_is_encrypted_and_not_rendered_or_audited(self):
        secret = "unit-test-secret-not-a-real-key"
        self.assertEqual(self.save_settings(api_key=secret).status_code, 302)
        config = AIConfiguration.objects.get()
        self.assertNotIn(secret, config.openrouter_secret)
        self.assertEqual(api_key("openrouter"), secret)
        self.assertNotContains(self.client.get("/settings/ai/"), secret)
        self.assertNotIn(secret, json.dumps(list(AuditEvent.objects.values_list("details", flat=True))))
        self.save_settings(api_key="")
        self.assertEqual(api_key("openrouter"), secret)
        self.assertEqual(current_profile()["model"], "openai/gpt-4.1-mini")

    def test_provider_model_mismatch_rejected(self):
        response = self.save_settings(provider="openai", api_key="not-real")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AIConfiguration.objects.exists())

    def test_test_evaluator_cannot_grade_real_installation(self):
        self.assertEqual(current_profile()["provider"], "openrouter")
        with self.assertRaises(PermanentEvaluationError):
            get_evaluator(demo_rubric())

    def test_access_check_does_not_reset_existing_password(self):
        output = io.StringIO()
        with patch.dict("os.environ", {"DEMO_PASSWORD": "different-test-password"}):
            call_command("check_admin_access", stdout=output)
        self.assertFalse(json.loads(output.getvalue())["matches"])
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password("initial-test-password"))

    def test_explicit_recovery_sets_password_without_printing_it(self):
        output = io.StringIO()
        password = "recovered-test-password"
        with patch("sys.stdin", io.StringIO(password)):
            call_command("check_admin_access", reset_admin=True, stdout=output)
        self.assertTrue(json.loads(output.getvalue())["matches"])
        self.assertNotIn(password, output.getvalue())
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password(password))

    @patch("trainer.evaluation_http.post_json")
    def test_saved_connection_is_used_by_real_evaluator(self, post):
        from .evaluation import EvaluationInput
        from .models import default_rubric
        from .test_ai import TASK, ANSWER, api_response
        self.save_settings(api_key="integration-test-key")
        post.return_value = api_response()
        rubric = default_rubric()
        result = get_evaluator(rubric).evaluate(EvaluationInput(TASK, ANSWER, rubric))
        self.assertFalse(result["is_demo"])
        self.assertEqual(post.call_args.args[0:2], ("openrouter", "integration-test-key"))
        self.assertEqual(result["hard_verdict"], "passed")

    def test_startup_keeps_existing_contest_settings(self):
        from .models import Contest
        with patch.dict("os.environ", {"DEMO_PASSWORD": "initial-test-password"}):
            call_command("seed_demo", stdout=io.StringIO())
            contest = Contest.objects.get(title="Поддержка бизнеса · стартовый конкурс")
            self.assertEqual(contest.status, "draft")
            contest.first_prize = 123
            contest.save()
            call_command("seed_demo", stdout=io.StringIO())
        contest.refresh_from_db()
        self.assertEqual(contest.first_prize, 123)
        self.assertEqual(Contest.objects.count(), 1)

    def test_activation_freezes_current_model_and_later_changes_do_not_rewrite_it(self):
        from .models import Contest
        from .services import activate_contest
        with patch.dict("os.environ", {"DEMO_PASSWORD": "initial-test-password"}):
            call_command("seed_demo", stdout=io.StringIO())
        contest = Contest.objects.get(title="Поддержка бизнеса · стартовый конкурс")
        self.save_settings(api_key="activation-test-key", model_choice="custom", custom_model="provider/model-one")
        activate_contest(self.admin, contest.pk)
        contest.refresh_from_db()
        self.assertEqual(contest.rubric["evaluator"]["model"], "provider/model-one")
        self.save_settings(model_choice="custom", custom_model="provider/model-two")
        contest.refresh_from_db()
        self.assertEqual(contest.rubric["evaluator"]["model"], "provider/model-one")

    def test_old_demo_contest_cannot_issue_new_attempts(self):
        from .models import Contest, Attempt
        from .services import start_next
        from django.core.exceptions import ValidationError
        with patch.dict("os.environ", {"DEMO_PASSWORD": "initial-test-password"}):
            call_command("seed_demo", stdout=io.StringIO())
        contest = Contest.objects.get(title="Поддержка бизнеса · стартовый конкурс")
        Contest.objects.filter(pk=contest.pk).update(status="active", rubric=demo_rubric())
        with self.assertRaises(ValidationError):
            start_next(self.admin, contest.pk)
        self.assertFalse(Attempt.objects.exists())
