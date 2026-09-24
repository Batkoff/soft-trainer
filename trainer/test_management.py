"""Права ролей, ручная оценка и редактирование с сохранением истории."""
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import (Attempt, AuditEvent, CalibrationCase, CalibrationRun, Contest, EvaluationTrace,
                     Exercise, SKILLS, UserProfile, demo_rubric)
from .people import apply_role
from .releases import VERSION
from .services import review_attempt, start_sandbox
from .tasks import evaluate_attempt

@override_settings(ALLOW_TEST_EVALUATOR=True, EVALUATOR_BACKEND="demo", EVALUATOR_MODEL="demo-v1", DEMO_EVALUATION_DELAY=0,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ManagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser("manager", password="test-password")
        cls.leader = User.objects.create_user("leader", password="test-password")
        apply_role(cls.leader, "group_leader")
        cls.user = User.objects.create_user("employee", password="test-password")
        UserProfile.objects.create(user=cls.user, manager=cls.leader)
        cls.exercise = Exercise.objects.create(title="Возврат", customer_message="Где возврат?",
            hard_answer="До 3 рабочих дней.", required_facts="До 3 рабочих дней.", status="published")

    def test_leader_cannot_administer_users_exercises_or_contests(self):
        self.client.force_login(self.leader)
        for path in ["/admin/auth/user/", "/admin/auth/user/add/", "/admin/trainer/exercise/add/", "/admin/trainer/contest/add/", "/admin/", "/admin/trainer/exercise/", "/admin/trainer/attempt/"]:
            self.assertEqual(self.client.get(path).status_code, 403, path)
        for path in ["/analytics/", "/sandbox/", "/guide/"]:
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.client.post(f"/admin/trainer/exercise/{self.exercise.pk}/new-version/").status_code, 403)

    def test_team_forms_show_expected_people_and_save_manager(self):
        User = get_user_model()
        sector = User.objects.create_user("sector", password="test-password")
        apply_role(sector, "sector_leader")
        legacy = User.objects.create_user("legacy", first_name="Без", last_name="Профиля", password="test-password")
        UserProfile.objects.filter(user=legacy).delete()

        self.client.force_login(self.admin)

        # РС получает штатный двухколоночный список доступных/выбранных РГ.
        sector_page = self.client.get(f"/admin/auth/user/{sector.pk}/change/")
        self.assertContains(sector_page, "Руководители групп")
        self.assertContains(sector_page, 'name="reports"')
        self.assertContains(sector_page, "selectfilter")
        self.assertContains(sector_page, self.leader.username)

        # Старый сотрудник без UserProfile всё равно доступен РГ для назначения.
        leader_page = self.client.get(f"/admin/auth/user/{self.leader.pk}/change/")
        self.assertContains(leader_page, legacy.username)

        # Сотруднику без руководителя можно назначить РГ и сохранить карточку.
        response = self.client.post(f"/admin/auth/user/{legacy.pk}/change/", {
            "username": legacy.username,
            "first_name": legacy.first_name,
            "last_name": legacy.last_name,
            "email": "",
            "role": "employee",
            "is_active": "on",
            "manager": self.leader.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(UserProfile.objects.get(user=legacy).manager_id, self.leader.pk)

        # РГ может выбрать РС; поле не исчезает.
        leader_page = self.client.get(f"/admin/auth/user/{self.leader.pk}/change/")
        self.assertContains(leader_page, 'name="manager"')
        self.assertContains(leader_page, sector.username)

    def test_create_leader_with_one_role_field(self):
        self.client.force_login(self.admin)
        response = self.client.post("/admin/auth/user/add/", {"username": "new-leader", "first_name": "Новый",
            "last_name": "Руководитель", "email": "", "role": "group_leader",
            "password1": "secure-test-leader-123", "password2": "secure-test-leader-123", "_save": "1"})
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(username="new-leader")
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.has_perm("trainer.review_attempt"))
        self.assertFalse(user.has_perm("auth.change_user"))
        self.assertTrue(user.check_password("secure-test-leader-123"))

    def test_last_login_is_readonly_and_last_admin_is_protected(self):
        self.client.force_login(self.admin)
        page = self.client.get(f"/admin/auth/user/{self.admin.pk}/change/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Последний вход")
        self.assertNotContains(page, 'name="last_login_0"')
        response = self.client.post(f"/admin/auth/user/{self.admin.pk}/change/", {
            "username": self.admin.username, "first_name": "", "last_name": "", "email": "", "role": "employee", "is_active": "on"})
        self.assertContains(response, "Нельзя отключить последнего администратора")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_superuser)

    def test_demoting_leader_removes_review_permission(self):
        UserProfile.objects.filter(manager=self.leader).update(manager=None)
        apply_role(self.leader, "employee")
        self.assertFalse(self.leader.is_staff)
        self.assertFalse(self.leader.has_perm("trainer.review_attempt"))
        attempt = start_sandbox(self.admin, self.exercise, "normal", "Ответ")
        evaluate_attempt(str(attempt.pk))
        with self.assertRaises(PermissionDenied):
            review_attempt(self.leader, attempt.pk, 100, "passed", "Изменить")

    def test_soft_review_updates_display_score_and_audit_preserving_original(self):
        attempt = start_sandbox(self.admin, self.exercise, "normal", "Ответ")
        Attempt.objects.filter(pk=attempt.pk).update(user=self.user)
        attempt.user = self.user
        evaluate_attempt(str(attempt.pk))
        original = attempt.evaluation.payload
        self.client.force_login(self.leader)
        self.assertContains(self.client.get(f"/attempts/{attempt.pk}/review/"), "Эмпатия · 0–100")
        response = self.client.post(f"/attempts/{attempt.pk}/review/", {
            "hard_verdict": "passed", "reason": "Проверено по критериям.", **{f"soft_{key}": 90 for key in SKILLS}})
        self.assertEqual(response.status_code, 302)
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 90)
        self.assertEqual(attempt.evaluation.payload, original)
        self.assertEqual(attempt.reviewed_skills, {key: 90 for key in SKILLS})
        self.assertContains(self.client.get(f"/attempts/{attempt.pk}/"), "SOFT SKILLS · ПОСЛЕ ПЕРЕСМОТРА")
        event = AuditEvent.objects.filter(action="manual_review", object_id=str(attempt.pk)).first()
        self.assertEqual(event.details["reviewed_skills"]["empathy"], 90)
        review_attempt(self.leader, attempt.pk, hard_verdict="violated", reason="Срок изменён.", soft_scores={key: 95 for key in SKILLS})
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 0)
        self.assertEqual(attempt.reviewed_skills["tone"], 95)
        self.assertEqual(attempt.evaluation.payload, original)

    def test_profile_edits_only_own_nickname_and_avatar(self):
        self.client.force_login(self.user)
        response = self.client.post("/profile/", {"display_name": "Новый ник", "avatar": "🦊",
            "username": "hacked", "is_superuser": "on", "user": self.admin.pk})
        self.assertRedirects(response, "/profile/")
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "employee")
        self.assertFalse(self.user.is_superuser)
        self.assertEqual(self.user.profile.display_name, "Новый ник")
        self.assertEqual(bytes(self.user.profile.avatar_data), b"")
        self.assertFalse(UserProfile.objects.filter(user=self.admin, display_name="Новый ник").exists())
        self.assertContains(self.client.get("/"), "Новый ник")

    def test_edit_published_exercise_creates_editable_copy(self):
        self.client.force_login(self.admin)
        page = self.client.get(f"/admin/trainer/exercise/{self.exercise.pk}/change/")
        self.assertContains(page, "Редактировать новую версию")
        self.assertEqual(self.client.get(f"/admin/trainer/exercise/{self.exercise.pk}/new-version/").status_code, 405)
        response = self.client.post(f"/admin/trainer/exercise/{self.exercise.pk}/new-version/")
        draft = Exercise.objects.exclude(pk=self.exercise.pk).get()
        self.assertRedirects(response, f"/admin/trainer/exercise/{draft.pk}/change/")
        self.assertEqual((draft.status, draft.version), ("draft", 2))
        draft.hard_answer = "Новая версия условий"
        draft.full_clean()
        draft.save()
        self.exercise.refresh_from_db()
        self.assertEqual(self.exercise.hard_answer, "До 3 рабочих дней.")

    def test_admin_home_links_training_and_pages_show_version(self):
        contest = Contest.objects.create(title="Демо", starts_at=timezone.now()-timedelta(days=1),
            ends_at=timezone.now()+timedelta(days=1), status="active", rubric=demo_rubric())
        contest.participants.add(self.user)
        self.client.force_login(self.admin)
        page = self.client.get("/")
        self.assertContains(page, "Начать тренировку в песочнице")
        self.assertNotContains(page, "На сегодня всё")
        for path in ["/", "/admin/", "/guide/", "/updates/", "/profile/", "/admin/auth/user/add/"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertContains(response, VERSION)

    def test_evaluation_trace_is_available_as_downloadable_admin_diagnostic(self):
        attempt = start_sandbox(self.admin, self.exercise, "normal", "Ответ")
        evaluate_attempt(str(attempt.pk))
        trace = EvaluationTrace.objects.get(attempt=attempt)
        self.client.force_login(self.admin)
        page = self.client.get("/admin/trainer/evaluationtrace/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Диагностика оценщика")
        response = self.client.get(f"/admin/trainer/evaluationtrace/{trace.pk}/download/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("evaluation-trace-", response["Content-Disposition"])
        self.assertEqual(response.json()["trace_id"], trace.pk)
        self.assertEqual(response.json()["normalized_payload"]["is_demo"], True)

    def test_new_contests_use_small_demo_prize_fund(self):
        contest = Contest(starts_at=timezone.now(), ends_at=timezone.now()+timedelta(days=1))
        self.assertEqual((contest.first_prize, contest.second_prize, contest.third_prize), (100, 70, 50))

    @override_settings(ALLOW_TEST_EVALUATOR=True, EVALUATOR_BACKEND="openai", OPENAI_API_KEY="")
    def test_calibrator_reports_missing_key_without_creating_partial_run(self):
        case = CalibrationCase.objects.create(title="Реальный пример", exercise=self.exercise,
            answer="До 3 рабочих дней.", scenario="real", expected_hard="passed")
        self.client.force_login(self.admin)
        response = self.client.post("/admin/trainer/calibrationcase/", {
            "action": "run_cases", "_selected_action": [case.pk]}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Для проверки откройте")
        self.assertFalse(CalibrationRun.objects.exists())
        self.assertFalse(Attempt.objects.exists())
