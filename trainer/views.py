import hashlib
import hmac
import json
from datetime import timedelta
from functools import wraps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Avg, Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from .forms import ProfileForm, ReviewForm, SandboxForm
from .models import Attempt, AuditEvent, CalibrationRun, Contest, LoginThrottle, SKILLS, UserProfile
from .reports import standings
from . import services
from .evaluation_profiles import profile_for_rubric
from .people import can_review, is_manager, visible_users
from .releases import RELEASES

def staff_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not is_manager(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped

class TrainerLoginView(LoginView):
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        identity = f"{request.POST.get('username', '').casefold()}:{request.META.get('REMOTE_ADDR', '')}"
        key = hmac.new(settings.SECRET_KEY.encode(), identity.encode(), hashlib.sha256).hexdigest()
        with transaction.atomic():
            item, _ = LoginThrottle.objects.get_or_create(key=key)
            item = LoginThrottle.objects.select_for_update().get(pk=item.pk)
            now = timezone.now()
            if now-item.window_start > timedelta(minutes=10):
                item.count, item.window_start = 0, now
            if item.count >= 10:
                return render(request, "registration/login.html", {"form": self.get_form(),
                    "throttled": True}, status=429)
            item.count += 1
            item.save()
        response = super().post(request, *args, **kwargs)
        if response.status_code == 302:
            LoginThrottle.objects.filter(key=key).delete()
        return response

def visible_contests(user):
    contests = Contest.objects.exclude(status=Contest.Status.DRAFT)
    return contests if user.is_superuser else contests.filter(participants__in=visible_users(user)).distinct()

def selected_contest(request):
    contests = visible_contests(request.user)
    contest_id = request.GET.get("contest")
    if contest_id:
        if not contest_id.isdecimal():
            from django.http import Http404
            raise Http404
        contest = get_object_or_404(contests, pk=contest_id)
        if contest.status == Contest.Status.ACTIVE:
            request.session["selected_contest"] = contest.pk
        return contest
    if request.GET.get("archive") == "1":
        return contests.filter(status=Contest.Status.FINISHED).order_by("-finalized_at", "-pk").first()
    # Архив не занимает место текущего конкурса. Будущий старт тоже не
    # перекрывает уже доступные задания. Выбор между идущими сохраняем.
    current = sorted(contests.filter(status=Contest.Status.ACTIVE),
        key=lambda c: ({"running": 0, "scheduled": 1, "closing": 2}[c.phase], c.effective_end, c.pk))
    remembered = request.session.get("selected_contest")
    selected = next((c for c in current if c.pk == remembered and c.phase in ("running", "scheduled")), None)
    return selected or (current[0] if current else None)


def contest_navigation(request, contest):
    contests = visible_contests(request.user)
    return {"contests": contests.filter(status=Contest.Status.ACTIVE).order_by("ends_at", "pk"),
            "archived_contests": contests.filter(status=Contest.Status.FINISHED).order_by("-finalized_at", "-pk"),
            "archive_mode": contest.status == Contest.Status.FINISHED if contest else request.GET.get("archive") == "1"}

def error_text(error):
    return "; ".join(error.messages)

@login_required
def home(request):
    contest = selected_contest(request)
    if contest and contest.status == Contest.Status.FINISHED:
        return redirect(f"{reverse('leaderboard')}?contest={contest.pk}")
    grading_unavailable = ""
    if contest:
        try:
            services.ensure_real_profile(contest.rubric)
        except ValidationError as error:
            grading_unavailable = "; ".join(error.messages)
    today = timezone.localdate()
    attempts = Attempt.objects.filter(user=request.user, mode=Attempt.Mode.RATED)
    if contest:
        attempts = attempts.filter(contest=contest)
    else:
        attempts = attempts.none()
    daily = list(attempts.filter(assignment__day=today).order_by("assignment__slot"))
    today_rows = {attempt.assignment.slot: attempt for attempt in daily}
    slots = [{"number": i, "attempt": today_rows.get(i)} for i in range(1, (contest.daily_limit if contest else 0)+1)]
    writing = next((a for a in daily if a.status == Attempt.Status.WRITING), None)
    total = attempts.filter(status=Attempt.Status.GRADED).aggregate(average=Avg("score"))
    row = next((r for r in standings(contest) if r["user_id"] == request.user.pk), None) if contest else None
    return render(request, "trainer/home.html", {"contest": contest, **contest_navigation(request, contest),
        "slots": slots, "writing": writing, "today": today,
        "submitted": sum(a.status != Attempt.Status.WRITING for a in daily),
        "pending": attempts.filter(status__in=["queued", "evaluating", "retry"]).count(),
        "grading_unavailable": grading_unavailable,
        "can_start": not grading_unavailable and contest and contest.accepts_answers and contest.participants.filter(pk=request.user.pk).exists() and
            (writing or len(daily) < contest.daily_limit),
        "recent": attempts.exclude(status=Attempt.Status.WRITING)[:8], "standing": row,
        "average": round(total["average"] or 0, 1), "nav": "training",
        "is_participant": bool(contest and contest.participants.filter(pk=request.user.pk).exists()),
        **({"grading_is_demo": profile_for_rubric(contest.rubric).get("provider") == "demo"} if contest else {})})

@login_required
@require_POST
def start(request, contest_id):
    try:
        attempt = services.start_next(request.user, contest_id)
        return redirect("attempt", attempt_id=attempt.pk)
    except Contest.DoesNotExist:
        return HttpResponse(status=404)
    except ValidationError as exc:
        messages.info(request, error_text(exc))
        return redirect("home")

def owned_attempt(request, attempt_id):
    qs = Attempt.objects.filter(user__in=visible_users(request.user))
    return get_object_or_404(qs.select_related("exercise", "contest", "user"), pk=attempt_id)

@login_required
def attempt_page(request, attempt_id):
    attempt = owned_attempt(request, attempt_id)
    if attempt.status == Attempt.Status.WRITING and attempt.expires_at <= timezone.now():
        attempt = services.finish_attempt(attempt.pk, expired=True)
    result = getattr(attempt, "evaluation", None)
    payload = dict(result.payload) if result else {}
    # Показанные навыки меняются после пересмотра; первоначальный JSON остаётся неизменным.
    if attempt.reviewed_skills:
        payload.update(skills=attempt.reviewed_skills, skill_scale=100, skill_notes={})
    scale = payload.get("skill_scale", 4)
    skills = [{"name": label, "value": payload.get("skills", {}).get(key), "scale": scale,
               "percent": round(payload.get("skills", {}).get(key, 0)*100/scale),
               "note": payload.get("skill_notes", {}).get(key, "")} for key, label in SKILLS.items()]
    review = AuditEvent.objects.filter(object_id=str(attempt.pk), action="manual_review").first()
    return render(request, "trainer/attempt.html", {"attempt": attempt, "payload": payload, "skills": skills,
        "review": review, "skills_reviewed": bool(attempt.reviewed_skills),
        "editable": attempt.user_id == request.user.pk, "can_review": can_review(request.user, attempt),
        "nav": "training" if attempt.user_id == request.user.pk else "analytics", "server_now": timezone.now(),
        "grading_is_demo": payload.get("is_demo", profile_for_rubric(attempt.rubric).get("provider") == "demo")})

def body_json(request):
    try:
        value = json.loads(request.body)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError):
        raise ValidationError("Некорректный запрос.")

def attempt_state(attempt):
    return {"status": attempt.status, "label": attempt.get_status_display(),
        "revision": attempt.draft_revision, "server_time": timezone.now().isoformat(),
        "deadline": attempt.expires_at.isoformat(), "url": reverse("attempt", args=[attempt.pk]),
        "score": attempt.score, "error": attempt.last_error}

@login_required
@require_POST
def draft(request, attempt_id):
    get_object_or_404(Attempt, pk=attempt_id, user=request.user)
    try:
        body = body_json(request)
        if type(body.get("revision")) is not int or not 0 < body["revision"] < 2**31 or not isinstance(body.get("answer"), str):
            raise ValidationError("Некорректный черновик.")
        attempt = services.save_draft(attempt_id, request.user, body["answer"], body["revision"])
        return JsonResponse({**attempt_state(attempt), "draft_saved": attempt.answer == body["answer"]})
    except ValidationError as exc:
        return JsonResponse({"error": error_text(exc)}, status=400)

@login_required
@require_POST
def submit(request, attempt_id):
    get_object_or_404(Attempt, pk=attempt_id, user=request.user)
    try:
        body = body_json(request)
        answer = body.get("answer")
        if not isinstance(answer, str):
            raise ValidationError("Некорректный ответ.")
        attempt = services.finish_attempt(attempt_id, user=request.user, answer=answer)
        return JsonResponse(attempt_state(attempt))
    except ValidationError as exc:
        return JsonResponse({"error": error_text(exc)}, status=400)

@login_required
@require_GET
def status(request, attempt_id):
    attempt = owned_attempt(request, attempt_id)
    if attempt.status == Attempt.Status.WRITING and attempt.expires_at <= timezone.now():
        attempt = services.finish_attempt(attempt.pk, expired=True)
    response = JsonResponse(attempt_state(attempt))
    response["Cache-Control"] = "no-store"
    return response

@login_required
def leaderboard(request):
    contest = selected_contest(request)
    return render(request, "trainer/leaderboard.html", {"contest": contest, **contest_navigation(request, contest),
        "rows": standings(contest) if contest else [], "nav": "leaderboard",
        **({"grading_is_demo": profile_for_rubric(contest.rubric).get("provider") == "demo"} if contest else {})})

@staff_required
def sandbox(request):
    form = SandboxForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            attempt = services.start_sandbox(request.user, **form.cleaned_data)
            return redirect("attempt", attempt_id=attempt.pk)
        except ValidationError as exc:
            form.add_error(None, error_text(exc))
    return render(request, "trainer/sandbox.html", {"form": form, "nav": "sandbox",
        "recent": Attempt.objects.filter(mode="sandbox", user=request.user)[:10]})

@staff_required
def review(request, attempt_id):
    if not can_review(request.user):
        raise PermissionDenied
    attempt = owned_attempt(request, attempt_id)
    if not can_review(request.user, attempt):
        raise PermissionDenied
    evaluation = getattr(attempt, "evaluation", None)
    payload = evaluation.payload if evaluation else {}
    initial_skills = attempt.reviewed_skills or {key: round(value*100/payload.get("skill_scale", 4))
                                               for key, value in payload.get("skills", {}).items()}
    form = ReviewForm(request.POST or None, initial={"hard_verdict": attempt.hard_verdict}, soft_scores=initial_skills)
    if request.method == "POST" and form.is_valid():
        try:
            services.review_attempt(request.user, attempt.pk, **form.cleaned_data)
            messages.success(request, "Оценка пересмотрена. Причина сохранена в журнале.")
            return redirect("attempt", attempt_id=attempt.pk)
        except ValidationError as exc:
            form.add_error(None, error_text(exc))
    return render(request, "trainer/review.html", {"attempt": attempt, "form": form})

@login_required
def profile(request):
    item = getattr(request.user, "profile", None) or UserProfile(user=request.user)
    form = ProfileForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        form.save()
        services.audit(request.user, "profile_updated", item, fields=list(form.changed_data))
        messages.success(request, "Профиль сохранён.")
        return redirect("profile")
    from django.contrib.auth import get_user_model
    from .people import user_role, display_name, unit_names
    role = user_role(request.user)
    supervisors = []
    group = item.manager if role == "employee" and item.manager_id else None
    sector = (getattr(group, "profile", None).manager if group and getattr(group, "profile", None)
              else item.manager if role == "group_leader" and item.manager_id else None)
    if role == "employee":
        supervisors.append({"role": "Руководитель группы", "name": display_name(group) if group else "Не назначен"})
    if role in ("employee", "group_leader"):
        supervisors.append({"role": "Руководитель сектора", "name": display_name(sector) if sector else "Не назначен"})
    if role in ("group_leader", "sector_leader"):
        supervisors.extend({"role": "Администратор", "name": display_name(admin)} for admin in
            get_user_model().objects.filter(is_superuser=True, is_active=True).select_related("profile"))
    return render(request, "trainer/profile.html", {"form": form, "nav": "profile", "supervisors": supervisors, "unit_name": unit_names(request.user)})

@staff_required
def guide(request):
    return render(request, "trainer/guide.html", {"nav": "guide"})

@login_required
def updates(request):
    return render(request, "trainer/updates.html", {"releases": RELEASES})

@staff_required
@require_POST
def retry(request, attempt_id):
    try:
        services.retry_attempt(request.user, attempt_id)
        messages.success(request, "Ответ снова поставлен в очередь.")
    except ValidationError as exc:
        messages.error(request, error_text(exc))
    return redirect("attempt", attempt_id=attempt_id)

@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
