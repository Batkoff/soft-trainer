"""Проверяем доступ по подчинённости через URL и прямые вызовы сервисов."""
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import UserProfile, Attempt, Exercise, Contest, Assignment, demo_rubric
from .people import apply_role, can_review
from .services import review_attempt, retry_attempt


@override_settings(ALLOW_TEST_EVALUATOR=True, EVALUATOR_BACKEND="demo", DEMO_EVALUATION_DELAY=0,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class HierarchyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("owner", password="test-pass")
        def person(name, role, manager=None):
            user = User.objects.create_user(name, password="test-pass")
            apply_role(user, role)
            UserProfile.objects.filter(user=user).update(manager=manager)
            return user
        self.sector = person("sector-one", "sector_leader")
        self.other_sector = person("sector-two", "sector_leader")
        self.leader = person("group-one", "group_leader", self.sector)
        self.peer = person("group-two", "group_leader", self.sector)
        self.outside = person("group-outside", "group_leader", self.other_sector)
        self.employee = person("employee-one", "employee", self.leader)
        self.peer_employee = person("employee-two", "employee", self.peer)
        self.stranger = person("employee-outside", "employee", self.outside)
        self.unassigned = person("unassigned", "employee")
        self.exercise = Exercise.objects.create(title="Тест", hard_answer="Факт", customer_message="Вопрос", required_facts="Факт")
        self.contest = Contest.objects.create(title="Общий конкурс", starts_at=timezone.now()-timedelta(days=1),
            ends_at=timezone.now()+timedelta(days=1), status="active", rubric=demo_rubric())
        self.contest.participants.set([self.employee, self.peer_employee, self.stranger])
        self.attempts = {}
        for user in [self.employee, self.peer_employee, self.stranger]:
            assignment = Assignment.objects.create(contest=self.contest, user=user, exercise=self.exercise,
                day=timezone.localdate(), slot=1)
            self.attempts[user.pk] = Attempt.objects.create(user=user, contest=self.contest, assignment=assignment,
                exercise=self.exercise, snapshot=self.exercise.snapshot(), rubric=demo_rubric(),
                answer="Факт", expires_at=timezone.now(), status="graded", score=70, hard_verdict="passed")

    def test_view_and_review_scope(self):
        for viewer, allowed in [(self.employee, [self.employee]), (self.leader, [self.employee]),
                                (self.sector, [self.employee, self.peer_employee]),
                                (self.admin, [self.employee, self.peer_employee, self.stranger])]:
            self.client.force_login(viewer)
            for owner in [self.employee, self.peer_employee, self.stranger]:
                attempt = self.attempts[owner.pk]
                for suffix in ["", "status/"]:
                    response = self.client.get(f"/attempts/{attempt.pk}/{suffix}")
                    self.assertEqual(response.status_code, 200 if owner in allowed else 404)
                permitted = owner in allowed and viewer != self.employee
                self.assertEqual(can_review(viewer, attempt), permitted)
                if permitted:
                    review_attempt(viewer, attempt.pk, 80, "passed", "По критериям")
                else:
                    with self.assertRaises(PermissionDenied):
                        review_attempt(viewer, attempt.pk, 100, "passed", "Чужая работа")
                    with self.assertRaises(PermissionDenied):
                        retry_attempt(viewer, attempt.pk)
                    self.assertIn(self.client.post(f"/attempts/{attempt.pk}/review/", {}).status_code, [403,404])

    def test_analytics_scope_and_drill_down(self):
        for viewer, count in [(self.employee, 1), (self.leader, 1), (self.sector, 2), (self.admin, 3)]:
            self.client.force_login(viewer)
            response = self.client.get("/analytics/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["stats"]["graded"], count)
            if viewer != self.admin:
                self.assertEqual(self.client.get(f"/analytics/?person={self.stranger.pk}").status_code,404)
                self.assertEqual(self.client.get("/admin/").status_code,403)
        self.client.force_login(self.sector)
        response = self.client.get(f"/analytics/?person={self.peer.pk}")
        self.assertEqual(response.context["stats"]["graded"],1)
        self.assertEqual(self.client.get(f"/analytics/?person={self.employee.pk}").context["stats"]["graded"],1)

    def test_transfer_changes_access_immediately(self):
        attempt = self.attempts[self.employee.pk]
        self.assertTrue(can_review(self.leader, attempt))
        UserProfile.objects.filter(user=self.employee).update(manager=self.outside)
        self.assertFalse(can_review(self.leader, attempt))
        self.assertFalse(can_review(self.sector, attempt))
        self.assertTrue(can_review(self.other_sector, attempt))
        self.assertTrue(can_review(self.outside, attempt))

    def test_profile_lists_superiors_without_exposing_other_profiles(self):
        for viewer, expected in [(self.employee, [self.leader.username, self.sector.username]),
                                  (self.leader, [self.sector.username, self.admin.username]),
                                  (self.sector, [self.admin.username])]:
            self.client.force_login(viewer)
            page = self.client.get("/profile/")
            for name in expected:
                self.assertContains(page,name)
            self.assertNotContains(page,self.other_sector.username)
        self.client.force_login(self.unassigned)
        self.assertContains(self.client.get("/profile/"),"Не назначен")

    def test_admin_can_assign_reports_and_invalid_hierarchy_is_rejected(self):
        self.client.force_login(self.admin)
        url = f"/admin/auth/user/{self.leader.pk}/change/"
        payload = {"username": self.leader.username, "first_name": "", "last_name": "", "email": "",
                   "role": "group_leader", "is_active": "on", "manager": self.sector.pk,
                   "reports": [self.employee.pk,self.unassigned.pk], "_save": "1"}
        self.assertEqual(self.client.post(url,payload).status_code,302)
        self.assertEqual(UserProfile.objects.get(user=self.unassigned).manager_id,self.leader.pk)
        response = self.client.post(url,{**payload,"manager":self.employee.pk})
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.context["adminform"].form.errors)
        response = self.client.post(url,{**payload,"reports":[self.peer.pk]})
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.context["adminform"].form.errors)
        self.client.force_login(self.leader)
        self.assertEqual(self.client.post(url,payload).status_code,403)

    def test_employee_cannot_escalate_via_profile_or_change_other_answers(self):
        self.client.force_login(self.employee)
        self.client.post("/profile/", {"display_name":"Ник", "avatar":"🦊", "role":"sector_leader", "manager":self.sector.pk})
        self.assertEqual(UserProfile.objects.get(user=self.employee).role,"employee")
        self.client.force_login(self.leader)
        attempt = self.attempts[self.employee.pk]
        for action in ["draft", "submit"]:
            self.assertEqual(self.client.post(f"/attempts/{attempt.pk}/{action}/", data='{}', content_type="application/json").status_code,404)

    def test_shared_sidebar_in_admin_and_application(self):
        self.client.force_login(self.admin)
        for url in ["/", "/admin/", "/admin/auth/user/", "/team/", "/analytics/"]:
            response = self.client.get(url)
            self.assertEqual(response.status_code,200)
            self.assertContains(response,'class="ton-sidebar"')
            self.assertContains(response,'sidebar.css')

    def test_role_fields_filter_choices_and_hide_irrelevant_controls(self):
        from .user_admin import TeamChangeForm
        from django import forms
        employee = TeamChangeForm(instance=self.employee)
        self.assertIsInstance(employee.fields["reports"].widget, forms.HiddenInput)
        self.assertEqual(set(employee.fields["manager"].queryset), {self.leader, self.peer, self.outside})
        sector = TeamChangeForm(instance=self.sector)
        self.assertIsInstance(sector.fields["manager"].widget, forms.HiddenInput)
        self.assertEqual(set(sector.fields["reports"].queryset), {self.leader, self.peer, self.outside})
        leader = TeamChangeForm(instance=self.leader)
        self.assertEqual(set(leader.fields["manager"].queryset), {self.sector, self.other_sector})
        self.assertNotIn(self.sector, leader.fields["reports"].queryset)
        admin = TeamChangeForm(instance=self.admin)
        for field in ["manager", "reports", "unit_name"]:
            self.assertIsInstance(admin.fields[field].widget, forms.HiddenInput)
        self.client.force_login(self.admin)
        response = self.client.get(f"/admin/auth/user/{self.employee.pk}/change/?role=sector_leader")
        self.assertIsInstance(response.context["adminform"].form.fields["manager"].widget, forms.HiddenInput)
        self.assertNotContains(response, 'name="_addanother"')
        self.assertNotContains(response, 'name="_continue"')
        self.assertContains(response, "Изменить пользователя")
        self.assertNotContains(response, "pbkdf2_sha256")

    def test_team_excludes_viewer_counts_all_descendants_and_saves_name(self):
        self.client.force_login(self.admin)
        response = self.client.get("/team/")
        rows = {row["person"].pk: row for row in response.context["rows"]}
        self.assertNotIn(self.admin.pk, rows)
        self.assertEqual(rows[self.sector.pk]["reports_count"], 4)
        url = f"/admin/auth/user/{self.leader.pk}/change/"
        response = self.client.post(url, {"username": self.leader.username, "role": "group_leader",
            "manager": self.sector.pk, "reports": [self.employee.pk], "unit_name": "Поддержка бизнеса",
            "is_active": "on", "_save": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(UserProfile.objects.get(user=self.leader).unit_name, "Поддержка бизнеса")
        self.client.force_login(self.leader)
        self.assertNotIn(self.leader.pk, [r["person"].pk for r in self.client.get("/team/").context["rows"]])

    def test_invalid_sector_manager_cannot_be_submitted(self):
        self.client.force_login(self.admin)
        response = self.client.post(f"/admin/auth/user/{self.sector.pk}/change/", {
            "username": self.sector.username, "role": "sector_leader", "manager": self.leader.pk,
            "reports": [self.leader.pk, self.peer.pk], "is_active": "on", "_save": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("manager", response.context["adminform"].form.errors)
        self.assertIsNone(UserProfile.objects.get(user=self.sector).manager_id)

    def test_leaderboard_photos_do_not_change_archived_standings(self):
        from .reports import standings
        UserProfile.objects.filter(user=self.employee).update(avatar_version="photo-version")
        original = standings(self.contest)
        self.contest.final_standings = original
        self.contest.status = "finished"
        self.contest.save()
        self.client.force_login(self.employee)
        response = self.client.get(f"/leaderboard/?contest={self.contest.pk}")
        self.assertContains(response, f"/avatars/{self.employee.pk}/?v=photo-version")
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.final_standings, original)
