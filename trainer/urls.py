from django.urls import path
from . import views, team_views
from .ai_views import ai_settings

urlpatterns = [
    path("settings/ai/", ai_settings, name="ai_settings"),
    path("", views.home, name="home"),
    path("contests/<int:contest_id>/start/", views.start, name="start"),
    path("attempts/<uuid:attempt_id>/", views.attempt_page, name="attempt"),
    path("attempts/<uuid:attempt_id>/draft/", views.draft, name="draft"),
    path("attempts/<uuid:attempt_id>/submit/", views.submit, name="submit"),
    path("attempts/<uuid:attempt_id>/status/", views.status, name="status"),
    path("attempts/<uuid:attempt_id>/review/", views.review, name="review"),
    path("attempts/<uuid:attempt_id>/retry/", views.retry, name="retry"),
    path("leaderboard/", views.leaderboard, name="leaderboard"),
    path("sandbox/", views.sandbox, name="sandbox"),
    path("profile/", views.profile, name="profile"),
    path("guide/", views.guide, name="guide"),
    path("updates/", views.updates, name="updates"),
    path("analytics/", team_views.analytics, name="analytics"),
    path("team/", team_views.team, name="team"),
    path("health/", views.health, name="health"),
]
