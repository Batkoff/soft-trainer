"""Статистика с той же областью доступа, что и карточки ответов."""
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from .models import Attempt, Assignment, Contest, UserProfile
from .dashboard import dashboard_data
from .people import visible_users, managed_users, is_manager, display_name, user_role, ROLE_CHOICES, unit_names
from .pagination import page_links
from . import services


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
    show_all_contests = request.GET.get("contest") == "all"
    contest = None if show_all_contests else selected_contest(request)
    attempts = Attempt.objects.filter(user__in=scope, mode="rated")
    if show_all_contests:
        pass
    elif contest:
        attempts = attempts.filter(contest=contest)
    else:
        attempts = attempts.none()
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
    rows_page = Paginator(rows, 10).get_page(request.GET.get("people_page"))
    page = Paginator(attempts.select_related("user", "user__profile"), 10).get_page(request.GET.get("page"))
    try:
        chart_days = int(request.GET.get("days", "7"))
    except (TypeError, ValueError):
        chart_days = 7
    if chart_days not in (7, 14, 30):
        chart_days = 7
    chart_options = []
    for option_days in (7, 14, 30):
        query = request.GET.copy()
        query.pop("page", None)
        query.pop("people_page", None)
        query["days"] = str(option_days)
        chart_options.append({"days": option_days, "active": option_days == chart_days,
                              "url": f"{request.path}?{query.urlencode()}"})
    return render(request, "trainer/analytics.html", {"nav": "analytics", "dashboard": dashboard_data(graded, chart_days), "stats": stats, "target": target,
        "target_name": display_name(target) if target else "", "rows": rows, "recent": page,
        "rows_page": rows_page, "contest": contest, "show_all_contests": show_all_contests,
        "filter_contests": visible_contests(request.user), "chart_days": chart_days,
        "chart_options": chart_options,
        "recent_page_links": page_links(request, page),
        "rows_page_links": page_links(request, rows_page, "people_page"),
        "can_manage_team": request.user.is_superuser, "team_access": is_manager(request.user)})


@login_required
def team(request):
    if not is_manager(request.user):
        raise PermissionDenied
    people = managed_users(request.user).filter(is_active=True, is_superuser=False).exclude(pk=request.user.pk).select_related("profile__manager", "profile__manager__profile").order_by("first_name", "username", "pk")
    page = Paginator(people, 10).get_page(request.GET.get("page"))
    rows = [{"person": person, "name": display_name(person), "role": dict(ROLE_CHOICES)[user_role(person)],
             "unit": unit_names(person),
             "reports_count": managed_users(person).exclude(pk=person.pk).count() if not person.is_superuser else None,
             "manager": display_name(person.profile.manager) if getattr(person, "profile", None) and person.profile.manager_id else "—"}
            for person in page.object_list]
    return render(request, "trainer/team.html", {"rows": rows, "page": page,
        "page_links": page_links(request, page), "nav": "team"})


@login_required
@require_http_methods(["GET", "POST"])
def remove_from_team(request, user_id):
    if not is_manager(request.user):
        raise PermissionDenied
    person = get_object_or_404(managed_users(request.user).filter(is_superuser=False), pk=user_id)
    person_profile = getattr(person, "profile", None)
    if not person_profile or not person_profile.manager_id:
        raise PermissionDenied
    direct_reports = UserProfile.objects.filter(manager=person)
    if request.method == "POST" and request.POST.get("confirm_remove") == "1":
        with transaction.atomic():
            locked = get_object_or_404(get_user_model().objects.select_for_update(), pk=person.pk)
            if not managed_users(request.user).filter(pk=locked.pk).exists() or locked.is_superuser:
                raise PermissionDenied
            profile = UserProfile.objects.select_for_update().filter(user=locked).first()
            if not profile or not profile.manager_id:
                raise PermissionDenied
            previous_manager = profile.manager_id
            reports = UserProfile.objects.filter(manager=locked)
            profile.manager = None
            profile.save(update_fields=["manager"])
            services.audit(request.user, "team_released", locked,
                           previous_manager=previous_manager, retained_reports=reports.count())
        messages.success(request, "Пользователь убран из команды. Аккаунт, доступ и история сохранены.")
        return redirect("team")
    return render(request, "trainer/remove_from_team.html", {
        "person": person, "name": display_name(person), "direct_reports_count": direct_reports.count(),
        "nav": "team",
    })


@login_required
@require_http_methods(["GET", "POST"])
def delete_account(request, user_id):
    if not request.user.is_superuser:
        raise PermissionDenied
    person = get_object_or_404(get_user_model(), pk=user_id)
    if person.is_superuser or person.pk == request.user.pk:
        raise PermissionDenied
    attempts = Attempt.objects.filter(user=person)
    assignments = Assignment.objects.filter(user=person)
    archived_ranks = sum(
        1 for contest in Contest.objects.filter(status=Contest.Status.FINISHED).only("final_standings").iterator()
        for row in (contest.final_standings or []) if str(row.get("user_id")) == str(person.pk)
    )
    direct_reports_count = UserProfile.objects.filter(manager=person).count()
    if request.method == "POST" and request.POST.get("confirm_delete") == "1":
        try:
            deleted = services.delete_user_account(request.user, person.pk)
        except ValidationError as error:
            for message in error.messages:
                messages.error(request, message)
            return redirect("team")
        messages.success(request, f"Аккаунт удалён. Обезличено строк в итоговых рейтингах: {deleted['archived_ranks']}.")
        return redirect("team")
    return render(request, "trainer/delete_account.html", {
        "person": person, "name": display_name(person), "attempt_count": attempts.count(),
        "assignment_count": assignments.count(), "archived_ranks_count": archived_ranks,
        "direct_reports_count": direct_reports_count,
        "nav": "team",
    })
