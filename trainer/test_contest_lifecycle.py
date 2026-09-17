"""Короткий конкурс, очередь и архив: сценарии досрочного завершения."""
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from .admin_forms import ContestDateTimeField, ContestDateTimeWidget
from .models import Attempt, AuditEvent, Contest, Exercise, demo_rubric
from .services import (auto_finalize_expired_contests, close_contest_early, finalize_contest,
                       finish_attempt, review_attempt, save_draft, start_next)
from .tasks import evaluate_attempt


@override_settings(EVALUATOR_BACKEND="demo", DEMO_EVALUATION_DELAY=0,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ContestLifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser("admin")
        cls.user = get_user_model().objects.create_user("employee")
        cls.exercise = Exercise.objects.create(title="Возврат", customer_message="Где возврат?",
            hard_answer="До 3 рабочих дней.", required_facts="До 3 рабочих дней.", status="published")
        cls.contest = Contest.objects.create(title="Пять минут", starts_at=timezone.now()-timedelta(minutes=1),
            ends_at=timezone.now()+timedelta(minutes=5), daily_limit=1, status="active", rubric=demo_rubric())
        cls.contest.participants.set([cls.admin, cls.user])
        cls.contest.exercises.add(cls.exercise)

    def test_early_close_saves_draft_and_waits_for_queue_then_freezes_once(self):
        attempt = start_next(self.user, self.contest.pk)
        save_draft(attempt.pk, self.user, "Сохранённый ответ", 1)
        result = close_contest_early(self.admin, self.contest.pk)
        attempt.refresh_from_db()
        self.assertEqual((result.status, attempt.status), ("active", "queued"))
        self.assertEqual(result.ends_at, self.contest.ends_at)  # Плановая дата не потеряна.
        self.assertEqual(attempt.answer, "Сохранённый ответ")
        self.assertLessEqual(attempt.expires_at, result.closed_at)
        self.assertFalse(result.accepts_answers)
        self.assertEqual(auto_finalize_expired_contests(), [])
        with self.assertRaises(ValidationError):
            start_next(self.admin, result.pk)
        with self.assertRaises(ValidationError):
            finalize_contest(self.admin, result.pk)
        # Поздний ответ не заменяет черновик, принятый при закрытии.
        self.assertEqual(finish_attempt(attempt.pk, self.user, "Опоздавший текст").answer, "Сохранённый ответ")
        evaluate_attempt(str(attempt.pk))
        self.assertEqual(auto_finalize_expired_contests(), [result.pk])
        result.refresh_from_db()
        original = result.final_standings
        closed_again = close_contest_early(self.admin, result.pk)
        self.assertEqual(closed_again.final_standings, original)
        self.assertEqual(AuditEvent.objects.filter(action="contest_closed_early").count(), 1)
        with self.assertRaises(ValidationError):
            review_attempt(self.admin, attempt.pk, 100, "passed", "Поздний пересмотр")

    def test_review_blocks_finalization_until_resolved(self):
        attempt = start_next(self.user, self.contest.pk)
        attempt = finish_attempt(attempt.pk, self.user, "Ответ")
        Attempt.objects.filter(pk=attempt.pk).update(status="review", last_error="Сбой API")
        close_contest_early(self.admin, self.contest.pk)
        self.assertEqual(auto_finalize_expired_contests(), [])
        review_attempt(self.admin, attempt.pk, 80, "passed", "Проверено вручную")
        self.assertEqual(auto_finalize_expired_contests(), [self.contest.pk])

    def test_empty_or_fully_graded_contest_finishes_immediately(self):
        result = close_contest_early(self.admin, self.contest.pk)
        self.assertEqual(result.status, "finished")
        self.assertIsNotNone(result.finalized_at)
        self.assertEqual(len(result.final_standings), 2)

    @override_settings(DEMO_PASSWORD="test-demo-password")
    def test_seed_on_update_does_not_change_archived_demo(self):
        self.contest.title = "Демо · Поддержка бизнеса"
        self.contest.first_prize = 125
        self.contest.save()
        self.contest.participants.remove(self.admin)
        original = close_contest_early(self.admin, self.contest.pk).final_standings
        call_command("seed_demo", verbosity=0)
        self.contest.refresh_from_db()
        self.assertEqual((self.contest.status, self.contest.first_prize), ("finished", 125))
        self.assertFalse(self.contest.participants.filter(pk=self.admin.pk).exists())
        self.assertEqual(self.contest.final_standings, original)

    def test_admin_confirmation_is_readonly_and_post_requires_permission_and_csrf(self):
        url = f"/admin/trainer/contest/{self.contest.pk}/close/"
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(url), "Завершить конкурс сейчас")
        self.contest.refresh_from_db()
        self.assertIsNone(self.contest.closed_at)
        secure_client = Client(enforce_csrf_checks=True)
        secure_client.force_login(self.admin)
        self.assertEqual(secure_client.post(url).status_code, 403)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.status, "finished")
        with self.assertRaises(PermissionDenied):
            close_contest_early(self.user, self.contest.pk)

    def test_default_selection_skips_archive_and_future_when_running_exists(self):
        future = Contest.objects.create(title="Будущий", starts_at=timezone.now()+timedelta(days=1),
            ends_at=timezone.now()+timedelta(days=2), status="active", rubric=demo_rubric())
        future.participants.add(self.user)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/").context["contest"].pk, self.contest.pk)
        self.assertContains(self.client.get(f"/?contest={future.pk}"), "Задания откроются автоматически")
        self.assertEqual(self.client.get("/leaderboard/").context["contest"].pk, future.pk)
        self.assertNotContains(self.client.get(f"/?contest={future.pk}"), "Таймер запускается")
        self.client.get(f"/?contest={self.contest.pk}")  # Запоминаем текущий конкурс.
        close_contest_early(self.admin, self.contest.pk)
        self.assertEqual(self.client.get("/").context["contest"].pk, future.pk)
        archived = self.client.get(f"/leaderboard/?contest={self.contest.pk}")
        self.assertContains(archived, "эта таблица больше не меняется")
        self.assertTrue(archived.context["archive_mode"])
        self.assertEqual(self.client.get("/leaderboard/?archive=1").context["contest"].pk, self.contest.pk)

    def test_only_archived_contest_does_not_look_current(self):
        close_contest_early(self.admin, self.contest.pk)
        self.client.force_login(self.user)
        self.assertIsNone(self.client.get("/").context["contest"])
        self.assertContains(self.client.get("/"), "Завершённые конкурсы")
        self.assertIsNone(self.client.get("/leaderboard/").context["contest"])
        self.assertRedirects(self.client.get(f"/?contest={self.contest.pk}"),
                             f"/leaderboard/?contest={self.contest.pk}")

    def test_contest_selection_does_not_expose_other_users_contests(self):
        self.contest.participants.remove(self.user)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(f"/?contest={self.contest.pk}").status_code, 404)
        self.assertEqual(self.client.get("/?contest=wrong").status_code, 404)

    def test_time_fields_default_to_midnight_and_remove_microseconds(self):
        self.client.force_login(self.admin)
        page = self.client.get("/admin/trainer/contest/add/")
        self.assertContains(page, 'value="00:00:00"', count=2)
        midnight = ContestDateTimeField().clean(["2026-09-20", ""])
        self.assertEqual(timezone.localtime(midnight).strftime("%H:%M:%S"), "00:00:00")
        html = ContestDateTimeWidget().render("moment", timezone.now().replace(microsecond=123456))
        self.assertNotIn("123456", html)
        with self.assertRaises(ValidationError):
            ContestDateTimeField().clean(["2026-09-20", "01:02:03.123456"])
