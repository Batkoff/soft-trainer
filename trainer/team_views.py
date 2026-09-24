"""Статистика с той же областью доступа, что и карточки ответов."""
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Q, Sum
from django.shortcuts import get_object_or_404, render
from .models import Attempt
from .people import visible_users, managed_users, is_manager, display_name, user_role, ROLE_CHOICES, unit_names


@login_required
def analytics(request):
    from .views import selected_contest, visible_contests
    allowed = visible_users(request.user).select_related("profile__manager")
    target_id = request.GET.get("person")
    target = get_object_or_404(allowed, pk=target_id) if target_id and target_id.isdecimal() else None
    if target_id and not target:
        from django.http import Http404
        raise Http404
    if not target and is_manager(request.user) and not request.user.is_superuser:
        target = request.user
    # Сотруднику доступна только собственная статистика, включая прямые URL.
    scope = (allowed.filter(pk=target.pk) if target and target.is_superuser else
             allowed.filter(pk__in=visible_users(target)) if target else allowed)
    contest = selected_contest(request) if request.GET.get("contest") else None
    attempts = Attempt.objects.filter(user__in=scope, mode="rated")
    if contest:
        attempts = attempts.filter(contest=contest)
    graded = attempts.filter(status="graded")
    stats = {"participants": scope.count(), "started": attempts.count(), "graded": graded.count(),
        "average": round(graded.aggregate(value=Avg("score"))["value"] or 0, 1),
        "points": graded.aggregate(value=Sum("score"))["value"] or 0,
        "hard_errors": graded.filter(hard_verdict="violated").count(),
        "timed_out": attempts.filter(timed_out=True).count(),
        "pending": attempts.filter(status__in=["queued", "evaluating", "retry"]).count(),
        "review": attempts.filter(status="review").count()}
    # Один агрегирующий запрос на всех сотрудников вместо запроса на каждую строку.
    totals = {row["user_id"]: row for row in graded.values("user_id").annotate(
        count=Count("id"), points=Sum("score"))}
    people = list(scope)
    ids = {person.pk for person in people}
    parent = {person.pk: getattr(getattr(person, "profile", None), "manager_id", None) for person in people}
    children = [p for p in people if parent[p.pk] == target.pk] if target else [
        p for p in people if parent[p.pk] not in ids]
    rows = []
    for person in children:
        group_ids = {person.pk} | {pk for pk, manager in parent.items() if manager == person.pk}
        group_ids |= {pk for pk, manager in parent.items() if manager in group_ids}
        count = sum(totals.get(pk, {}).get("count", 0) for pk in group_ids)
        points = sum(totals.get(pk, {}).get("points", 0) or 0 for pk in group_ids)
        rows.append({"person": person, "name": display_name(person), "role": dict(ROLE_CHOICES)[user_role(person)],
                     "unit": unit_names(person), "size": len(group_ids)-1, "count": count, "points": points,
                     "average": round(points/count, 1) if count else None})
    page = Paginator(attempts.select_related("user", "user__profile"), 25).get_page(request.GET.get("page"))
    return render(request, "trainer/analytics.html", {"nav": "analytics", "stats": stats, "target": target,
        "target_name": display_name(target) if target else "", "rows": rows, "recent": page,
        "contest": contest, "filter_contests": visible_contests(request.user),
        "can_manage_team": request.user.is_superuser, "team_access": is_manager(request.user)})


@login_required
def team(request):
    if not is_manager(request.user):
        raise PermissionDenied
    people = managed_users(request.user).exclude(pk=request.user.pk).select_related("profile__manager", "profile__manager__profile").order_by("first_name", "username")
    rows = [{"person": person, "name": display_name(person), "role": dict(ROLE_CHOICES)[user_role(person)],
             "unit": unit_names(person),
             "reports_count": managed_users(person).exclude(pk=person.pk).count() if not person.is_superuser else None,
             "manager": display_name(person.profile.manager) if getattr(person, "profile", None) and person.profile.manager_id else "—"}
            for person in people]
    return render(request, "trainer/team.html", {"rows": rows, "nav": "team"})
