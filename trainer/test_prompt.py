"""Редактирование правил не меняет уже выданные задания и старые конкурсы."""
from hashlib import sha256
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from .evaluation_http import LiveEvaluator
from .evaluation_profiles import current_profile
from .evaluation_prompt import LEGACY_SYSTEM_PROMPT, SYSTEM_PROMPT
from .models import AuditEvent, EvaluationPrompt


@override_settings(ALLOW_TEST_EVALUATOR=False, EVALUATOR_BACKEND="openrouter",
                   EVALUATOR_MODEL="test-model")
class PromptTests(TestCase):
    def test_snapshot_survives_edit_and_old_version_is_available(self):
        EvaluationPrompt.objects.update_or_create(pk=1, defaults={"text": "Первый промпт", "version": 3})
        before = current_profile()
        EvaluationPrompt.objects.filter(pk=1).update(text="Второй промпт")
        after = current_profile()
        self.assertEqual(LiveEvaluator(before).system_prompt, "Первый промпт")
        self.assertEqual(LiveEvaluator(after).system_prompt, "Второй промпт")
        self.assertEqual(before["prompt_hash"], sha256("Первый промпт".encode()).hexdigest())
        self.assertNotEqual(before["prompt_hash"], after["prompt_hash"])
        self.assertEqual(before["prompt_version"], "soft-v3")
        self.assertEqual(after["prompt_version"], "soft-v3")
        legacy = {**before, "prompt_version": "soft-v1"}
        self.assertEqual(LiveEvaluator(legacy).system_prompt, LEGACY_SYSTEM_PROMPT)


    def test_existing_snapshot_keeps_older_prompt_version(self):
        EvaluationPrompt.objects.update_or_create(pk=1, defaults={"text": "Текущие правила", "version": 7})
        profile = current_profile()
        self.assertEqual(profile["prompt_version"], "soft-v7")
        self.assertEqual(LiveEvaluator(profile).system_prompt, "Текущие правила")

    def test_default_without_saved_configuration(self):
        EvaluationPrompt.objects.all().delete()
        self.assertEqual(current_profile()["prompt_text"], SYSTEM_PROMPT)

    def test_admin_edit_permissions_validation_and_audit(self):
        EvaluationPrompt.objects.update_or_create(pk=1, defaults={"text": SYSTEM_PROMPT, "version": 3})
        staff = get_user_model().objects.create_user("leader", is_staff=True)
        admin = get_user_model().objects.create_superuser("owner", password="test-only")
        url = "/admin/trainer/evaluationprompt/1/change/"
        self.client.force_login(staff)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"text": "Changed"}).status_code, 403)
        self.client.force_login(admin)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, {"text": "   "}).status_code, 200)
        self.assertEqual(EvaluationPrompt.objects.get(pk=1).text, SYSTEM_PROMPT)
        self.assertEqual(self.client.post(url, {"text": "Новые правила", "_save": "1"}).status_code, 302)
        saved = EvaluationPrompt.objects.get(pk=1)
        self.assertEqual(saved.text, "Новые правила")
        self.assertEqual(saved.version, 4)
        self.assertEqual(current_profile()["prompt_version"], "soft-v4")
        self.assertTrue(AuditEvent.objects.filter(actor=admin, action="admin_updated").exists())
        self.assertEqual(self.client.get("/admin/trainer/evaluationprompt/add/").status_code, 403)
