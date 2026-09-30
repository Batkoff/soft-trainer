"""Общий рейтинг короткий, но своё место видно вне первой десятки."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .models import Assignment, Attempt, Contest, Exercise, UserProfile
from .models import demo_rubric


class LeaderboardLimitTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.users = [User.objects.create_user(f"member-{index}", password="test-pass") for index in range(12)]
        for user in self.users:
            UserProfile.objects.get_or_create(user=user)
        self.exercise = Exercise.objects.create(title="Ответ", customer_message="Вопрос",
            hard_answer="Факт", required_facts="Факт")
        self.contest = Contest.objects.create(title="Рейтинг", starts_at=timezone.now()-timedelta(days=1),
            ends_at=timezone.now()+timedelta(days=1), status=Contest.Status.ACTIVE, rubric=demo_rubric())
        self.contest.participants.set(self.users)
        for index, user in enumerate(self.users):
            assignment = Assignment.objects.create(contest=self.contest, user=user, exercise=self.exercise,
                day=timezone.localdate(), slot=1)
            Attempt.objects.create(user=user, contest=self.contest, assignment=assignment,
                exercise=self.exercise, snapshot=self.exercise.snapshot(), rubric=demo_rubric(),
                status=Attempt.Status.GRADED, score=100-index, hard_verdict="passed",
                expires_at=timezone.now())

    def test_leaderboard_shows_top_ten_and_separate_current_place(self):
        self.client.force_login(self.users[-1])
        response = self.client.get("/leaderboard/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 10)
        self.assertEqual(response.context["participant_count"], 12)
        self.assertEqual(response.context["my_row"]["place"], 12)
        self.assertContains(response, "ТВОЯ ПОЗИЦИЯ")
        self.assertContains(response, f"@{self.users[-1].username}")
        self.assertContains(response, "12")
        self.client.force_login(self.users[2])
        response = self.client.get("/leaderboard/")
        self.assertIsNone(response.context["my_row"])
        self.assertTrue(any(row["user_id"] == self.users[2].pk for row in response.context["rows"]))
