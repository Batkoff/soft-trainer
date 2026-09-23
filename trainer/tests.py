"""Проверяем правила конкурса и сбои. Запуск: python manage.py test trainer."""
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob
from procrastinate.contrib.django import app
from .evaluation import TemporaryEvaluationError
from .models import Assignment, Attempt, AuditEvent, Contest, Evaluation, Exercise
from .reports import standings
from .services import (activate_contest, auto_finalize_expired_contests, finalize_contest, finish_attempt,
    review_attempt, save_draft, start_next, start_sandbox)
from .tasks import evaluate_attempt, recover_jobs

@override_settings(ALLOW_TEST_EVALUATOR=True, EVALUATOR_BACKEND="demo", DEMO_EVALUATION_DELAY=0, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class TrainingRulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user("employee", password="test-password-123")
        cls.other = User.objects.create_user("other", password="test-password-123")
        cls.admin = User.objects.create_superuser("manager", password="test-password-123")
        cls.exercise = Exercise.objects.create(title="Возврат", customer_message="Где деньги?",
            hard_answer="Возврат обрабатывается до 3 рабочих дней.", required_facts="До 3 рабочих дней.",
            forbidden_promises="Не обещать сегодня.", status=Exercise.Status.PUBLISHED)
        cls.second = Exercise.objects.create(title="Перевод", customer_message="Почему отменён?",
            hard_answer="Перевод отменён.", required_facts="Отменён.", status=Exercise.Status.PUBLISHED)
        cls.contest = Contest.objects.create(title="Тест", starts_at=timezone.now()-timedelta(days=1),
            ends_at=timezone.now()+timedelta(days=1), daily_limit=2)
        cls.contest.participants.set([cls.user, cls.other])
        cls.contest.exercises.set([cls.exercise, cls.second])
        activate_contest(cls.admin, cls.contest.pk)

    def start(self):
        return start_next(self.user, self.contest.pk)

    def submit(self):
        attempt = self.start()
        return finish_attempt(attempt.pk, self.user, "Проверил: возврат обрабатывается до 3 рабочих дней.")

    def test_double_start_reuses_attempt(self):
        first = self.start()
        self.assertEqual(self.start().pk, first.pk)
        self.assertEqual(Attempt.objects.count(), 1)

    def test_two_submissions_do_not_duplicate_jobs(self):
        attempt = self.submit()
        job_count = ProcrastinateJob.objects.count()
        repeated = finish_attempt(attempt.pk, self.user, "Другой ответ")
        self.assertEqual(repeated.answer, attempt.answer)
        self.assertEqual(ProcrastinateJob.objects.count(), job_count)

    def test_daily_limit_enforced(self):
        self.submit()
        self.submit()
        with self.assertRaises(ValidationError):
            self.start()
        self.assertEqual(Assignment.objects.filter(user=self.user).count(), 2)

    def test_same_daily_exercises_for_everyone(self):
        first = self.start()
        other = start_next(self.other, self.contest.pk)
        self.assertEqual(first.exercise_id, other.exercise_id)

    def test_stale_draft_does_not_overwrite_newer(self):
        attempt = self.start()
        save_draft(attempt.pk, self.user, "Новая версия", 2)
        saved = save_draft(attempt.pk, self.user, "Старая версия", 1)
        self.assertEqual(saved.answer, "Новая версия")

    def test_http_reports_rejected_stale_draft(self):
        attempt = self.start()
        save_draft(attempt.pk, self.user, "Другая вкладка", 2)
        self.client.force_login(self.user)
        response = self.client.post(f"/attempts/{attempt.pk}/draft/", data={"answer": "Старый текст", "revision": 1}, content_type="application/json")
        self.assertFalse(response.json()["draft_saved"])
        self.assertEqual(response.json()["revision"], 2)

    def test_maintenance_awaits_recovery_and_submits_expired_draft(self):
        attempt = self.start()
        save_draft(attempt.pk, self.user, "Сохранённый ответ", 1)
        Attempt.objects.filter(pk=attempt.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        job = SimpleNamespace(id=123)
        with patch.object(app.job_manager, "get_stalled_jobs", new_callable=AsyncMock, return_value=[job]) as stalled, \
             patch.object(app.job_manager, "retry_job", new_callable=AsyncMock) as retry:
            async_to_sync(recover_jobs.func)(timestamp=0)
            stalled.assert_awaited_once()
            retry.assert_awaited_once_with(job)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "queued")
        self.assertTrue(attempt.timed_out)
        self.assertEqual(attempt.answer, "Сохранённый ответ")

    def test_deadline_accepts_only_previously_saved_text(self):
        attempt = self.start()
        save_draft(attempt.pk, self.user, "Сохранённый черновик", 1)
        deadline = timezone.now()-timedelta(seconds=1)
        Attempt.objects.filter(pk=attempt.pk).update(expires_at=deadline)
        result = finish_attempt(attempt.pk, self.user, "Этот ответ пришёл поздно")
        self.assertEqual(result.answer, "Сохранённый черновик")
        self.assertEqual(result.submitted_at, deadline)
        self.assertTrue(result.timed_out)

    def test_empty_timeout_scores_zero(self):
        attempt = self.start()
        Attempt.objects.filter(pk=attempt.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        finish_attempt(attempt.pk, expired=True)
        evaluate_attempt(str(attempt.pk))
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 0)

    def test_empty_manual_submit_is_rejected(self):
        attempt = self.start()
        with self.assertRaises(ValidationError):
            finish_attempt(attempt.pk, self.user, "   ")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "writing")

    def test_attempt_and_queue_commit_together(self):
        attempt = self.start()
        jobs_before = ProcrastinateJob.objects.count()
        try:
            with transaction.atomic():
                finish_attempt(attempt.pk, self.user, "Ответ")
                raise RuntimeError("Имитация сбоя транзакции")
        except RuntimeError:
            pass
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "writing")
        self.assertEqual(ProcrastinateJob.objects.count(), jobs_before)

    def test_worker_redelivery_keeps_single_score(self):
        attempt = self.submit()
        evaluate_attempt(str(attempt.pk))
        evaluate_attempt(str(attempt.pk))
        self.assertEqual(Evaluation.objects.filter(attempt=attempt).count(), 1)
        row = next(row for row in standings(self.contest) if row["user_id"] == self.user.pk)
        self.assertEqual(row["points"], 80)

    def test_hard_error_scores_zero_but_keeps_soft_feedback(self):
        attempt = start_sandbox(self.admin, self.exercise, "hard_error", "Ответ")
        evaluate_attempt(str(attempt.pk))
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 0)
        self.assertEqual(attempt.hard_verdict, "violated")
        self.assertEqual(len(attempt.evaluation.payload["skills"]), 5)

    def test_transient_error_can_be_retried(self):
        attempt = start_sandbox(self.admin, self.exercise, "transient", "Ответ")
        with self.assertRaises(TemporaryEvaluationError):
            evaluate_attempt(str(attempt.pk))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "retry")
        self.assertIsNone(attempt.score)
        evaluate_attempt(str(attempt.pk))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "graded")

    def test_permanent_failure_does_not_penalize_employee(self):
        attempt = start_sandbox(self.admin, self.exercise, "failure", "Ответ")
        evaluate_attempt(str(attempt.pk))
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "review")
        self.assertIsNone(attempt.score)

    def test_sandbox_excluded_from_ranking(self):
        attempt = start_sandbox(self.admin, self.exercise, "normal", "Ответ")
        evaluate_attempt(str(attempt.pk))
        self.assertEqual(sum(row["points"] for row in standings(self.contest)), 0)
        self.assertEqual(Assignment.objects.count(), 0)

    def test_manual_review_preserves_original(self):
        attempt = self.submit()
        evaluate_attempt(str(attempt.pk))
        original = Evaluation.objects.get(attempt=attempt).payload
        review_attempt(self.admin, attempt.pk, 90, "violated", "Искажён срок.")
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 0)
        self.assertEqual(attempt.evaluation.payload, original)
        self.assertTrue(AuditEvent.objects.filter(action="manual_review", object_id=str(attempt.pk)).exists())

    def test_employee_cannot_review_or_use_sandbox(self):
        attempt = self.submit()
        with self.assertRaises(PermissionDenied):
            review_attempt(self.user, attempt.pk, 100, "passed", "Хочу 100")
        with self.assertRaises(PermissionDenied):
            start_sandbox(self.user, self.exercise, "normal")

    def test_finalization_waits_for_all_answers(self):
        attempt = self.submit()
        Contest.objects.filter(pk=self.contest.pk).update(ends_at=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            finalize_contest(self.admin, self.contest.pk)
        evaluate_attempt(str(attempt.pk))
        finalize_contest(self.admin, self.contest.pk)
        with self.assertRaises(ValidationError):
            review_attempt(self.admin, attempt.pk, 90, "passed", "Поздний пересмотр")

    def test_expired_contest_is_finalized_by_maintenance_when_all_answers_graded(self):
        attempt = self.submit()
        evaluate_attempt(str(attempt.pk))
        Contest.objects.filter(pk=self.contest.pk).update(ends_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(auto_finalize_expired_contests(), [self.contest.pk])
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.status, Contest.Status.FINISHED)
        self.assertTrue(self.contest.finalized_at)
        self.assertEqual(self.contest.final_standings[0]["points"], 80)
        with self.assertRaises(ValidationError):
            start_next(self.user, self.contest.pk)

    def test_published_exercise_cannot_be_edited(self):
        self.exercise.hard_answer = "Возврат сегодня"
        with self.assertRaises(ValidationError):
            self.exercise.full_clean()

    def test_http_access_and_csrf(self):
        attempt = self.start()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"/attempts/{attempt.pk}/").status_code, 404)
        self.assertEqual(self.client.post(f"/attempts/{attempt.pk}/draft/", data='{"answer":"hack","revision":1}', content_type="application/json").status_code, 404)
        self.assertEqual(self.client.get("/analytics/").status_code, 200)
        self.assertEqual(self.client.get("/analytics/").context["stats"]["graded"], 0)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(f"/attempts/{attempt.pk}/submit/", data='{"answer":"answer"}', content_type="application/json").status_code, 403)

    def test_pages_render_and_hidden_rubric_is_not_exposed(self):
        attempt = self.start()
        self.client.force_login(self.user)
        for path in ["/", "/leaderboard/", f"/attempts/{attempt.pk}/"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotContains(response, "Не обещать сегодня.")
        self.client.force_login(self.admin)
        response = self.client.get(f"/attempts/{attempt.pk}/")
        self.assertContains(response, "ПРОСМОТР РАБОТЫ СОТРУДНИКА")
        self.assertContains(response, "@employee")
        self.assertNotContains(response, "ВАШ ОТВЕТ")
        self.assertNotContains(response, 'id="submit-answer"')
        for path in ["/analytics/", "/sandbox/", "/admin/", "/admin/trainer/contest/",
                     "/admin/trainer/contest/add/", f"/admin/trainer/contest/{self.contest.pk}/change/",
                     "/admin/trainer/exercise/add/", f"/admin/trainer/exercise/{self.exercise.pk}/change/",
                     "/admin/trainer/attempt/"]:
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertContains(self.client.get("/admin/"), "admin-theme.css")
