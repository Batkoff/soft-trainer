from datetime import timedelta
from unittest.mock import Mock, patch
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import Attempt, Assignment, Contest, Evaluation, EvaluationTrace, Exercise, SKILLS, demo_rubric
from .rechecks import request_recheck, evaluate_recheck
from .services import review_attempt, finalize_contest, close_contest_early, auto_finalize_expired_contests


@override_settings(ALLOW_TEST_EVALUATOR=True, EVALUATOR_BACKEND="demo", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RecheckTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", password="pass")
        self.employee = User.objects.create_user("employee", password="pass")
        self.outside = User.objects.create_user("outside", password="pass")
        self.exercise = Exercise.objects.create(title="Перепроверка", hard_answer="Факт", required_facts="Факт")
        self.contest = Contest.objects.create(title="Конкурс", status="active", starts_at=timezone.now()-timedelta(days=2), ends_at=timezone.now()+timedelta(days=2), rubric=demo_rubric())
        self.contest.participants.add(self.employee)
        assignment = Assignment.objects.create(user=self.employee, contest=self.contest, exercise=self.exercise, day=timezone.localdate(), slot=1)
        self.attempt = Attempt.objects.create(user=self.employee, contest=self.contest, assignment=assignment, exercise=self.exercise,
            snapshot=self.exercise.snapshot(), rubric=demo_rubric(), answer="Ответ", expires_at=timezone.now(),
            status="graded", score=60, hard_verdict="passed", graded_at=timezone.now(), reviewed_skills={key: 60 for key in SKILLS})
        self.payload = {"skills": {key: 90 for key in SKILLS}, "skill_scale": 100, "hard_verdict": "passed", "provider": "demo", "model": "test"}
        Evaluation.objects.create(attempt=self.attempt, payload={**self.payload, "skills": {key: 60 for key in SKILLS}}, duration_ms=1, backend="demo")

    def request(self):
        with patch("trainer.rechecks.evaluate_recheck.defer") as defer:
            item = request_recheck(self.admin, self.attempt.pk)
            defer.assert_called_once()
            return item

    def test_success_keeps_old_result_until_completion_and_is_idempotent(self):
        item = self.request()
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.score, 60)
        self.client.force_login(self.admin)
        self.assertTrue(self.client.get(f"/attempts/{self.attempt.pk}/status/").json()["recheck_pending"])
        with self.assertRaises(ValidationError):
            self.request()
        with self.assertRaises(ValidationError):
            review_attempt(self.admin, self.attempt.pk, 100, "passed", "Причина")
        evaluator = Mock()
        evaluator.last_trace = {}
        evaluator.evaluate.return_value = self.payload
        with patch("trainer.rechecks.get_evaluator", return_value=evaluator):
            evaluate_recheck(item.pk)
            evaluate_recheck(item.pk)
        self.assertEqual(evaluator.evaluate.call_count, 1)
        self.attempt.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(self.attempt.score, 90)
        self.assertEqual(self.attempt.reviewed_skills, {})
        self.assertEqual(item.previous["score"], 60)
        self.assertEqual(item.status, "completed")
        self.assertFalse(self.client.get(f"/attempts/{self.attempt.pk}/status/").json()["recheck_pending"])
        self.assertEqual(EvaluationTrace.objects.filter(attempt=self.attempt).count(), 1)
        self.client.force_login(self.admin)
        page = self.client.get(f"/attempts/{self.attempt.pk}/")
        self.assertContains(page, "Пересмотреть вручную")
        self.assertContains(page, "Перепроверить нейросетью")

    def test_failure_keeps_score_and_previous_analysis(self):
        item = self.request()
        evaluator = Mock()
        evaluator.last_trace = {}
        evaluator.evaluate.side_effect = RuntimeError("secret upstream text")
        with patch("trainer.rechecks.get_evaluator", return_value=evaluator):
            evaluate_recheck(item.pk)
        item.refresh_from_db()
        self.attempt.refresh_from_db()
        self.assertEqual(item.status, "failed")
        self.assertNotIn("secret", item.error)
        self.assertEqual(self.attempt.score, 60)
        self.assertEqual(self.attempt.evaluation.payload["skills"]["tone"], 60)

    def test_retry_and_finalize_wait_for_recheck(self):
        from .evaluation import TemporaryEvaluationError
        item = self.request()
        close_contest_early(self.admin, self.contest.pk)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.status, "active")
        self.assertEqual(auto_finalize_expired_contests(), [])
        with self.assertRaises(ValidationError):
            finalize_contest(self.admin, self.contest.pk)
        evaluator = Mock()
        evaluator.last_trace = {}
        evaluator.evaluate.side_effect = TemporaryEvaluationError("Временная ошибка")
        with patch("trainer.rechecks.get_evaluator", return_value=evaluator):
            for _ in range(2):
                with self.assertRaises(TemporaryEvaluationError):
                    evaluate_recheck(item.pk)
            evaluate_recheck(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.status, "failed")
        finalize_contest(self.admin, self.contest.pk)
        with self.assertRaises(ValidationError):
            self.request()

    def test_access_and_dashboard_scope(self):
        for user in [self.employee, self.outside]:
            with self.assertRaises(PermissionDenied):
                request_recheck(user, self.attempt.pk)
        self.client.force_login(self.employee)
        response = self.client.get("/analytics/")
        data = response.context["dashboard"]
        self.assertEqual(data["recent_count"], 1)
        self.assertEqual(sum(b["count"] for b in data["bands"]), 1)
        self.assertEqual(data["skills"][0]["average"], 60)
        self.client.force_login(self.outside)
        data = self.client.get("/analytics/").context["dashboard"]
        self.assertEqual(data["recent_count"], 0)
        self.assertIsNone(data["skills"][0]["average"])
