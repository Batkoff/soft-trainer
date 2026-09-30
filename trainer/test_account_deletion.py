"""Отделяем снятие с команды от удаления учётной записи."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (Assignment, Attempt, AuditEvent, CalibrationCase, CalibrationRun, Contest,
                     Evaluation, EvaluationRecheck, EvaluationTrace, Exercise, UserProfile, demo_rubric)
from .people import apply_role


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AccountRemovalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "admin@example.test", "test-admin-password")
        self.leader = User.objects.create_user("leader", password="test-leader-password")
        apply_role(self.leader, "group_leader")
        self.person = User.objects.create_user("person", email="person@example.test", password="test-person-password")
        apply_role(self.person, "employee")
        UserProfile.objects.filter(user=self.person).update(manager=self.leader)
        self.exercise = Exercise.objects.create(title="Проверка", customer_message="Вопрос",
            hard_answer="Факт", required_facts="Факт")
        self.contest = Contest.objects.create(title="Текущий", starts_at=timezone.now()-timedelta(days=1),
            ends_at=timezone.now()+timedelta(days=1), status=Contest.Status.ACTIVE, rubric=demo_rubric())
        self.contest.participants.add(self.person)

    def make_attempt(self, status=Attempt.Status.GRADED):
        assignment = Assignment.objects.create(contest=self.contest, user=self.person, exercise=self.exercise,
            day=timezone.localdate(), slot=1)
        return Attempt.objects.create(user=self.person, contest=self.contest, assignment=assignment,
            exercise=self.exercise, snapshot=self.exercise.snapshot(), rubric=demo_rubric(),
            status=status, score=82 if status == Attempt.Status.GRADED else None,
            hard_verdict="passed", answer="Ответ клиента", expires_at=timezone.now()+timedelta(minutes=3))

    def test_remove_from_team_keeps_active_account_and_history(self):
        attempt = self.make_attempt()
        self.client.force_login(self.leader)
        confirm = self.client.get(f"/team/{self.person.pk}/remove/")
        self.assertEqual(confirm.status_code, 200)
        self.assertContains(confirm, "аккаунт останется активным")
        response = self.client.post(f"/team/{self.person.pk}/remove/", {"confirm_remove": "1"})
        self.assertRedirects(response, "/team/")
        self.person.refresh_from_db()
        attempt.refresh_from_db()
        self.assertTrue(self.person.is_active)
        self.assertIsNone(self.person.profile.manager_id)
        self.assertEqual(attempt.answer, "Ответ клиента")
        self.assertTrue(Attempt.objects.filter(pk=attempt.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(actor=self.leader, action="team_released",
            object_id=str(self.person.pk)).exists())

    def test_admin_can_delete_account_and_work_data_separately(self):
        attempt = self.make_attempt()
        evaluation = Evaluation.objects.create(attempt=attempt, payload={"score": 82})
        trace = EvaluationTrace.objects.create(attempt=attempt, status=EvaluationTrace.Status.SUCCESS)
        recheck = EvaluationRecheck.objects.create(attempt=attempt, requested_by=self.person, status="completed")
        case = CalibrationCase.objects.create(title="Эталон", exercise=self.exercise, answer="Ответ")
        run = CalibrationRun.objects.create(case=case, attempt=attempt, expected_min=80,
            expected_max=90, expected_hard="passed")
        self.contest.status = Contest.Status.FINISHED
        self.contest.final_standings = [{"user_id": self.person.pk, "name": "Person", "points": 82,
            "place": 1, "completed": 1, "average": 82.0}]
        self.contest.save(update_fields=["status", "final_standings"])
        old_user_event = AuditEvent.objects.create(actor=self.person, action="user_updated",
            object_id=str(self.person.pk), details={"username": self.person.username, "email": self.person.email})
        old_attempt_event = AuditEvent.objects.create(actor=self.person, action="manual_review",
            object_id=str(attempt.pk), details={"reason": "Ответ person@example.test проверен"})

        self.client.force_login(self.admin)
        page = self.client.get(f"/team/{self.person.pk}/delete/")
        self.assertContains(page, "Аккаунт, профиль, ответы, оценки и попытки пользователя будут удалены")
        self.client.post(f"/team/{self.person.pk}/delete/", {})
        self.assertTrue(get_user_model().objects.filter(pk=self.person.pk).exists())
        response = self.client.post(f"/team/{self.person.pk}/delete/", {"confirm_delete": "1"})
        self.assertRedirects(response, "/team/")
        self.assertFalse(get_user_model().objects.filter(pk=self.person.pk).exists())
        self.assertFalse(Attempt.objects.filter(pk=attempt.pk).exists())
        self.assertFalse(Assignment.objects.filter(user_id=self.person.pk).exists())
        self.assertFalse(Evaluation.objects.filter(pk=evaluation.pk).exists())
        self.assertFalse(EvaluationTrace.objects.filter(pk=trace.pk).exists())
        self.assertFalse(EvaluationRecheck.objects.filter(pk=recheck.pk).exists())
        self.assertFalse(CalibrationRun.objects.filter(pk=run.pk).exists())
        self.assertFalse(self.contest.participants.filter(pk=self.person.pk).exists())
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.final_standings[0]["user_id"], None)
        self.assertEqual(self.contest.final_standings[0]["name"], "Удалённый участник")
        old_user_event.refresh_from_db()
        self.assertNotIn("email", old_user_event.details)
        self.assertIsNone(old_attempt_event.__class__.objects.get(pk=old_attempt_event.pk).actor_id)
        self.assertEqual(old_attempt_event.__class__.objects.get(pk=old_attempt_event.pk).details,
            {"record_deleted": True})
        deletion = AuditEvent.objects.get(action="account_deleted", object_id=str(self.person.pk))
        self.assertEqual(deletion.actor, self.admin)

    def test_account_deletion_waits_for_incomplete_work_and_is_admin_only(self):
        self.make_attempt(status=Attempt.Status.WRITING)
        self.client.force_login(self.leader)
        self.assertEqual(self.client.get(f"/team/{self.person.pk}/delete/").status_code, 403)
        self.client.force_login(self.admin)
        self.client.post(f"/team/{self.person.pk}/delete/", {"confirm_delete": "1"})
        self.assertTrue(get_user_model().objects.filter(pk=self.person.pk).exists())
        response = self.client.get("/team/")
        self.assertContains(response, "Сначала дождитесь завершения открытых ответов")

    def test_team_list_is_paginated_by_ten(self):
        User = get_user_model()
        for index in range(11):
            person = User.objects.create_user(f"extra-{index}", password="test-extra-password")
            apply_role(person, "employee")
            profile, _ = UserProfile.objects.get_or_create(user=person)
            profile.manager = self.leader
            profile.save(update_fields=["manager"])
        self.client.force_login(self.leader)
        first = self.client.get("/team/")
        second = self.client.get("/team/?page=2")
        self.assertEqual(first.context["page"].paginator.per_page, 10)
        self.assertEqual(len(first.context["rows"]), 10)
        self.assertEqual(len(second.context["rows"]), 2)
