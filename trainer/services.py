"""Бизнес-правила. Все изменения попытки выполняются под блокировкой строки."""
import hashlib
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import Assignment, Attempt, AuditEvent, Contest, Exercise, Evaluation, default_rubric, demo_rubric
from .evaluation_profiles import profile_for_rubric, profile_has_key
from .people import can_review, is_manager

MAX_ANSWER_LENGTH = 6000

def audit(actor, action: str, obj, **details):
    AuditEvent.objects.create(actor=actor, action=action, object_id=str(obj.pk), details=details)

def check_answer(answer: str) -> str:
    if len(answer) > MAX_ANSWER_LENGTH:
        raise ValidationError(f"Ответ должен быть не длиннее {MAX_ANSWER_LENGTH} символов.")
    return answer

@transaction.atomic
def start_next(user, contest_id: int) -> Attempt:
    # Блокируем сотрудника: двойной клик и две вкладки получают одну попытку.
    get_user_model().objects.select_for_update().get(pk=user.pk)
    # Создание попытки и завершение конкурса используют одну блокировку.
    contest = Contest.objects.select_for_update().get(pk=contest_id)
    if not contest.participants.filter(pk=user.pk).exists():
        raise PermissionDenied
    ensure_real_profile(contest.rubric)
    if not contest.accepts_answers:
        raise ValidationError(contest.availability_message)
    now = timezone.now()
    day = timezone.localdate(now)
    assignments = list(Assignment.objects.filter(contest=contest, user=user, day=day))
    if not assignments:
        exercises = list(contest.exercises.all())
        # Одинаковый набор и порядок для всех; меняются с московской датой.
        exercises.sort(key=lambda ex: hashlib.sha256(f"{contest.pk}:{day}:{ex.pk}".encode()).hexdigest())
        if len(exercises) < contest.daily_limit:
            raise ValidationError("В конкурсе недостаточно заданий. Сообщите администратору.")
        assignments = Assignment.objects.bulk_create([
            Assignment(contest=contest, user=user, day=day, slot=index+1, exercise=exercise)
            for index, exercise in enumerate(exercises[:contest.daily_limit])])
    for assignment in assignments:
        existing = Attempt.objects.filter(assignment=assignment).first()
        if existing and existing.status == Attempt.Status.WRITING:
            if existing.expires_at <= now:
                finish_attempt(existing.pk, user=None, expired=True)
            else:
                return existing
        if not existing:
            attempt = Attempt.objects.create(user=user, contest=contest, assignment=assignment,
                exercise=assignment.exercise, snapshot=assignment.exercise.snapshot(), rubric=contest.rubric,
                expires_at=min(now+timedelta(seconds=contest.time_limit_seconds), contest.effective_end))
            from .tasks import expire_attempt
            # DjangoConnector использует ту же транзакцию: попытка и задание очереди сохранятся вместе.
            expire_attempt.configure(schedule_at=attempt.expires_at).defer(attempt_id=str(attempt.pk))
            audit(user, "attempt_started", attempt, contest_id=contest.pk)
            return attempt
    raise ValidationError("Все задания на сегодня уже начаты. Результаты появятся после проверки.")

@transaction.atomic
def save_draft(attempt_id, user, answer: str, revision: int) -> Attempt:
    attempt = Attempt.objects.select_for_update().get(pk=attempt_id, user=user)
    if attempt.status != Attempt.Status.WRITING:
        return attempt
    if timezone.now() >= attempt.expires_at:
        return finish_attempt(attempt.pk, user=None, expired=True)
    if revision > attempt.draft_revision:
        attempt.answer = check_answer(answer)
        attempt.draft_revision = revision
        attempt.save(update_fields=["answer", "draft_revision"])
    return attempt

@transaction.atomic
def finish_attempt(attempt_id, user=None, answer: str | None = None, expired: bool = False) -> Attempt:
    attempt = Attempt.objects.select_for_update().get(pk=attempt_id)
    if user is not None and attempt.user_id != user.pk:
        raise PermissionDenied
    if attempt.status != Attempt.Status.WRITING:
        return attempt  # Идемпотентность: повтор POST не создаёт новую оценку.
    now = timezone.now()
    deadline_passed = now >= attempt.expires_at
    if expired and not deadline_passed:
        return attempt
    if not deadline_passed and answer is not None:
        attempt.answer = check_answer(answer)
    if not deadline_passed and not attempt.answer.strip():
        raise ValidationError("Напишите ответ клиенту.")
    attempt.timed_out = deadline_passed
    # При истечении срока оцениваем только черновик, сохранённый ДО дедлайна.
    attempt.submitted_at = attempt.expires_at if deadline_passed else now
    attempt.status = Attempt.Status.QUEUED
    from .tasks import evaluate_attempt
    attempt.queue_job_id = evaluate_attempt.defer(attempt_id=str(attempt.pk))
    attempt.save()
    audit(user, "attempt_submitted", attempt, timed_out=deadline_passed, job_id=attempt.queue_job_id)
    return attempt

@transaction.atomic
def start_sandbox(user, exercise: Exercise, scenario: str, answer: str | None = None) -> Attempt:
    if not is_manager(user):
        raise PermissionDenied
    if scenario not in Attempt.Scenario.values:
        raise ValidationError("Неизвестный тестовый сценарий.")
    if scenario != Attempt.Scenario.REAL:
        from django.conf import settings
        if not getattr(settings, "ALLOW_TEST_EVALUATOR", False):
            raise ValidationError("Тестовые оценки отключены. Выберите проверку нейросетью.")
    rubric = default_rubric() if scenario == Attempt.Scenario.REAL else demo_rubric()
    if scenario == Attempt.Scenario.REAL and not profile_has_key(profile_for_rubric(rubric)):
        raise ValidationError("Для проверки откройте Управление → Нейросеть и сохраните API-ключ.")
    attempt = Attempt.objects.create(user=user, exercise=exercise, mode=Attempt.Mode.SANDBOX,
        snapshot=exercise.snapshot(), rubric=rubric, demo_scenario=scenario,
        expires_at=timezone.now()+timedelta(seconds=180))
    from .tasks import expire_attempt
    expire_attempt.configure(schedule_at=attempt.expires_at).defer(attempt_id=str(attempt.pk))
    if answer is not None:
        attempt = finish_attempt(attempt.pk, user=user, answer=answer)
    audit(user, "sandbox_started", attempt, scenario=scenario)
    return attempt

def lock_attempt_with_contest(attempt_id):
    """Везде, где нужны обе блокировки, порядок один: конкурс → попытка.

    Сохранение черновиков и очередь блокируют только попытку. Такой порядок
    исключает взаимное ожидание с завершением конкурса и ручным пересмотром.
    """
    contest_id = Attempt.objects.values_list("contest_id", flat=True).get(pk=attempt_id)
    if contest_id:
        Contest.objects.select_for_update().get(pk=contest_id)
    return Attempt.objects.select_for_update().get(pk=attempt_id)


@transaction.atomic
def retry_attempt(user, attempt_id) -> Attempt:
    if not can_review(user):
        raise PermissionDenied
    attempt = lock_attempt_with_contest(attempt_id)
    if not can_review(user, attempt):
        raise PermissionDenied
    if attempt.contest_id and attempt.contest.status == Contest.Status.FINISHED:
        raise ValidationError("Итоги конкурса уже зафиксированы.")
    if attempt.status != Attempt.Status.REVIEW:
        raise ValidationError("Повтор доступен только для попытки, требующей пересмотра.")
    if Evaluation.objects.filter(attempt=attempt).exists():
        raise ValidationError("Разбор уже получен. Используйте ручной пересмотр, чтобы сохранить первоначальную оценку.")
    from .tasks import evaluate_attempt
    attempt.status = Attempt.Status.QUEUED
    attempt.last_error = ""
    previous_tries = attempt.evaluation_tries
    attempt.evaluation_tries = 0  # Ручной повтор запускает новый цикл до трёх попыток.
    attempt.queue_job_id = evaluate_attempt.defer(attempt_id=str(attempt.pk))
    attempt.save(update_fields=["status", "last_error", "queue_job_id", "evaluation_tries"])
    audit(user, "evaluation_requeued", attempt, previous_tries=previous_tries)
    return attempt

@transaction.atomic
def review_attempt(user, attempt_id, score=None, hard_verdict="passed", reason="", soft_scores=None):
    if not can_review(user):
        raise PermissionDenied
    attempt = lock_attempt_with_contest(attempt_id)
    if not can_review(user, attempt):
        raise PermissionDenied
    if attempt.contest_id:
        contest = attempt.contest
        if contest.status == Contest.Status.FINISHED:
            raise ValidationError("Итоги конкурса уже зафиксированы.")
    if attempt.status not in (Attempt.Status.GRADED, Attempt.Status.REVIEW):
        raise ValidationError("Дождитесь завершения автоматической проверки.")
    if soft_scores is not None:
        from .evaluation import calculate_score, PermanentEvaluationError
        try:
            score = calculate_score({"skills": soft_scores, "skill_scale": 100, "hard_verdict": hard_verdict},
                                    attempt.answer, attempt.rubric)
        except PermanentEvaluationError as exc:
            raise ValidationError(str(exc)) from exc
    if not reason.strip() or type(score) is not int or not 0 <= score <= 100:
        raise ValidationError("Укажите причину и оценку от 0 до 100.")
    if hard_verdict not in ("passed", "violated", "unverified"):
        raise ValidationError("Неизвестный результат проверки hard-части.")
    if hard_verdict == "unverified" and profile_for_rubric(attempt.rubric).get("provider") != "demo":
        raise ValidationError("Для реального ответа подтвердите сохранение или искажение hard-части.")
    old = {"score": attempt.score, "hard": attempt.hard_verdict, "status": attempt.status,
           "reviewed_skills": attempt.reviewed_skills}
    if soft_scores is not None:
        attempt.reviewed_skills = soft_scores
    attempt.score = 0 if hard_verdict == "violated" or not attempt.answer.strip() else score
    attempt.hard_verdict = hard_verdict
    attempt.status = Attempt.Status.GRADED
    attempt.graded_at = timezone.now()
    attempt.last_error = ""
    attempt.save()
    audit(user, "manual_review", attempt, previous=old, score=attempt.score, hard=hard_verdict,
          reviewed_skills=attempt.reviewed_skills, reason=reason)
    return attempt

@transaction.atomic
def activate_contest(user, contest_id):
    if not user.is_superuser:
        raise PermissionDenied
    contest = Contest.objects.select_for_update().get(pk=contest_id)
    if contest.status != Contest.Status.DRAFT:
        raise ValidationError("Запустить можно только черновик конкурса.")
    from django.conf import settings
    if not getattr(settings, "ALLOW_TEST_EVALUATOR", False):
        from .evaluation_profiles import current_profile
        contest.rubric = {**contest.rubric, "version": "soft-v1", "evaluator": current_profile()}
    contest.full_clean()
    if contest.ends_at <= timezone.now():
        raise ValidationError("Укажите будущую дату окончания.")
    if contest.exercises.count() < contest.daily_limit:
        raise ValidationError("Добавьте не меньше заданий, чем дневной лимит.")
    if contest.exercises.exclude(status=Exercise.Status.PUBLISHED).exists():
        raise ValidationError("Сначала опубликуйте все задания конкурса.")
    if not contest.participants.exists():
        raise ValidationError("Добавьте участников конкурса.")
    ensure_real_profile(contest.rubric)
    profile = profile_for_rubric(contest.rubric)
    if profile.get("provider") != "demo" and not profile_has_key(profile):
        raise ValidationError("Для запуска конкурса настройте API-ключ провайдера, указанного в профиле оценки.")
    contest.status = Contest.Status.ACTIVE
    contest.save(update_fields=["status", "rubric"])
    audit(user, "contest_activated", contest)

@transaction.atomic
def finalize_contest(user, contest_id):
    if not user.is_superuser:
        raise PermissionDenied
    contest = Contest.objects.select_for_update().get(pk=contest_id)
    list(Attempt.objects.select_for_update().filter(contest_id=contest_id).values_list("pk", flat=True))
    if contest.status == Contest.Status.FINISHED:
        return contest
    if contest.status != Contest.Status.ACTIVE or contest.effective_end > timezone.now():
        raise ValidationError("Сначала завершите приём ответов кнопкой «Завершить досрочно» или дождитесь окончания конкурса.")
    if Attempt.objects.filter(contest=contest).exclude(status=Attempt.Status.GRADED).exists():
        raise ValidationError("Дождитесь проверки всех ответов и разрешите спорные оценки.")
    freeze_standings(contest, user, "contest_finalized")
    return contest


def freeze_standings(contest, actor, event):
    """Вызывается только внутри транзакции после блокировки конкурса и работ."""
    from .reports import standings
    contest.final_standings = standings(contest, live=True)
    contest.status = Contest.Status.FINISHED
    contest.finalized_at = timezone.now()
    contest.save(update_fields=["final_standings", "status", "finalized_at"])
    audit(actor, event, contest, participants=len(contest.final_standings))


@transaction.atomic
def close_contest_early(user, contest_id):
    """Остановить конкурс сейчас, не теряя черновики и ожидающие оценки.

    Два этапа нужны из-за очереди: приём прекращается сразу, а рейтинг становится
    неизменным лишь после проверки всех работ. Повторный POST безопасен.
    """
    if not user.is_superuser:
        raise PermissionDenied
    contest = Contest.objects.select_for_update().get(pk=contest_id)
    # Блокируем строки, но не грузим все тексты месячного конкурса в память.
    list(Attempt.objects.select_for_update().filter(contest=contest).values_list("pk", flat=True))
    if contest.status == Contest.Status.FINISHED:
        return contest
    if contest.status != Contest.Status.ACTIVE:
        raise ValidationError("Завершить можно только запущенный конкурс.")
    if not contest.closed_at and contest.ends_at > timezone.now():
        contest.closed_at = timezone.now()
        contest.save(update_fields=["closed_at"])
        audit(user, "contest_closed_early", contest, planned_end=contest.ends_at.isoformat(),
              closed_at=contest.closed_at.isoformat())
    for attempt in Attempt.objects.filter(contest=contest, status=Attempt.Status.WRITING):
        attempt.expires_at = min(attempt.expires_at, contest.effective_end)
        attempt.save(update_fields=["expires_at"])
        finish_attempt(attempt.pk, expired=True)
    if not Attempt.objects.filter(contest=contest).exclude(status=Attempt.Status.GRADED).exists():
        freeze_standings(contest, user, "contest_finalized")
    return contest


def auto_finalize_expired_contests(limit=100):
    """Зафиксировать конкурсы, срок которых закончился и все ответы проверены.

    Фоновая задача вызывает это раз в минуту. Конкурс с ожидающей или спорной
    оценкой не закрывается автоматически: после ручного пересмотра администратор
    может нажать обычное действие «Зафиксировать итоговый рейтинг».
    """
    candidate_ids = list(Contest.objects.filter(status=Contest.Status.ACTIVE).filter(
        Q(ends_at__lte=timezone.now()) | Q(closed_at__isnull=False)).values_list("pk", flat=True)[:limit])
    finalized = []
    for contest_id in candidate_ids:
        with transaction.atomic():
            contest = Contest.objects.select_for_update().get(pk=contest_id)
            list(Attempt.objects.select_for_update().filter(contest_id=contest_id).values_list("pk", flat=True))
            if contest.status != Contest.Status.ACTIVE or contest.effective_end > timezone.now():
                continue
            if Attempt.objects.filter(contest=contest).exclude(status=Attempt.Status.GRADED).exists():
                continue
            freeze_standings(contest, None, "contest_auto_finalized")
            finalized.append(contest.pk)
    return finalized


def ensure_real_profile(rubric):
    from django.conf import settings
    if getattr(settings, "ALLOW_TEST_EVALUATOR", False):
        return
    profile = profile_for_rubric(rubric)
    if profile.get("provider") not in ("openai", "openrouter"):
        raise ValidationError("Этот конкурс использовал тестовые оценки. Создайте новый конкурс с нейросетью; прежние результаты сохранены в истории.")
    if not profile_has_key(profile):
        raise ValidationError("Проверка пока не настроена. Администратору нужно добавить API-ключ: Управление → Нейросеть.")
