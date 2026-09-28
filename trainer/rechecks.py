"""Повторная оценка с сохранением результата до успешного ответа провайдера."""
import time
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from procrastinate import RetryStrategy
from procrastinate.contrib.django import app
from .models import Attempt, Contest, Evaluation, EvaluationRecheck, EvaluationTrace
from .people import can_review
from .evaluation import EvaluationInput, TemporaryEvaluationError, PermanentEvaluationError, calculate_score, get_evaluator

ACTIVE = ("queued", "running", "retry")


def pending_rechecks(**filters):
    return EvaluationRecheck.objects.filter(status__in=ACTIVE, **filters).exists()


@transaction.atomic
def request_recheck(user, attempt_id):
    from .services import audit, lock_attempt_with_contest, ensure_real_profile
    attempt = lock_attempt_with_contest(attempt_id)
    if not can_review(user, attempt):
        raise PermissionDenied
    if attempt.contest_id and attempt.contest.status == Contest.Status.FINISHED:
        raise ValidationError("Итоги конкурса уже зафиксированы.")
    if attempt.status not in (Attempt.Status.GRADED, Attempt.Status.REVIEW):
        raise ValidationError("Дождитесь первой проверки.")
    if pending_rechecks(attempt=attempt):
        raise ValidationError("Перепроверка уже выполняется.")
    ensure_real_profile(attempt.rubric)
    result = Evaluation.objects.filter(attempt=attempt).first()
    recheck = EvaluationRecheck.objects.create(attempt=attempt, requested_by=user, previous={
        "score": attempt.score, "hard_verdict": attempt.hard_verdict, "status": attempt.status,
        "reviewed_skills": attempt.reviewed_skills, "payload": result.payload if result else {},
    })
    evaluate_recheck.defer(recheck_id=recheck.pk)
    audit(user, "ai_recheck_requested", attempt, recheck_id=recheck.pk, previous=recheck.previous)
    return recheck


@app.task(queue="evaluation", retry=RetryStrategy(max_attempts=3, exponential_wait=3, retry_exceptions=[TemporaryEvaluationError]))
def evaluate_recheck(recheck_id):
    from .services import audit, lock_attempt_with_contest
    with transaction.atomic():
        item = EvaluationRecheck.objects.select_for_update().get(pk=recheck_id)
        if item.status not in ACTIVE:
            return
        item.status = "running"
        item.tries += 1
        item.save(update_fields=["status", "tries"])
        attempt = item.attempt
        data = EvaluationInput(attempt.snapshot, attempt.answer, attempt.rubric, attempt.demo_scenario, item.tries, str(attempt.pk))
    started = time.monotonic()
    evaluator, trace, payload = None, {}, {}
    failure = None
    try:
        evaluator = get_evaluator(data.rubric)
        payload = evaluator.evaluate(data)
        trace = payload.pop("_trace", None) or getattr(evaluator, "last_trace", {}) or {}
        score = calculate_score(payload, data.answer, data.rubric)
        if score is None:
            raise PermanentEvaluationError("Hard-часть неоднозначна. Прежняя оценка сохранена; нужен ручной пересмотр.")
    except Exception as exc:
        failure = exc
        trace = getattr(evaluator, "last_trace", {}) or trace
    transient = isinstance(failure, TemporaryEvaluationError) and item.tries < 3
    with transaction.atomic():
        # Порядок блокировок совпадает с завершением конкурса и ручным пересмотром.
        current = lock_attempt_with_contest(attempt.pk)
        item = EvaluationRecheck.objects.select_for_update().get(pk=recheck_id)
        if item.status not in ACTIVE:
            return
        duration = round((time.monotonic() - started) * 1000)
        reason = ""
        if failure:
            from .diagnostics import error_reason
            reason = error_reason(str(failure)[:240]) if isinstance(failure, (TemporaryEvaluationError, PermanentEvaluationError)) else "Внутренняя ошибка проверки."
        if current.contest_id and current.contest.status == Contest.Status.FINISHED:
            reason, transient = "Итоги конкурса уже зафиксированы.", False
            failure = PermanentEvaluationError(reason)
        trace_row = EvaluationTrace.objects.create(attempt=current, try_number=item.tries,
            provider=str(trace.get("provider", payload.get("provider", "")))[:40],
            model=str(trace.get("model", payload.get("model", "")))[:160],
            prompt_version=str(trace.get("prompt_version", payload.get("prompt_version", "")))[:40],
            request_payload=trace.get("request_payload", {}), response_payload=trace.get("response_payload", {}),
            normalized_payload=payload, duration_ms=duration,
            status="retry" if transient else "failed" if failure else "success",
            error_type=type(failure).__name__ if failure else "", error_message=reason,
            error_metadata=trace.get("error", {}), input_tokens=trace.get("input_tokens"), output_tokens=trace.get("output_tokens"))
        if failure:
            item.status = "retry" if transient else "failed"
            item.error = reason[:240]
        else:
            Evaluation.objects.update_or_create(attempt=current, defaults={"payload": payload, "duration_ms": duration, "backend": payload.get("provider", "")})
            current.score, current.hard_verdict = score, payload["hard_verdict"]
            current.status, current.graded_at = Attempt.Status.GRADED, timezone.now()
            current.reviewed_skills, current.last_error = {}, ""
            current.save(update_fields=["score", "hard_verdict", "status", "graded_at", "reviewed_skills", "last_error"])
            item.status, item.error = "completed", ""
        item.finished_at = None if transient else timezone.now()
        item.save(update_fields=["status", "error", "finished_at"])
        audit(item.requested_by, "ai_recheck_error" if failure else "ai_recheck_completed", current,
            recheck_id=item.pk, trace_id=trace_row.pk, score=current.score, error=reason, status=item.status)
    if transient:
        raise failure
