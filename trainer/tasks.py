"""Очередь в PostgreSQL. Длительная проверка не держит транзакцию базы открытой."""
import logging
import time
from asgiref.sync import sync_to_async
from django.db import transaction
from django.utils import timezone
from procrastinate import RetryStrategy
from procrastinate.contrib.django import app
from .evaluation import EvaluationInput, TemporaryEvaluationError, calculate_score, get_evaluator
from .models import Attempt, Evaluation, EvaluationTrace

logger = logging.getLogger("trainer.evaluation")

@app.task(queue="evaluation", retry=RetryStrategy(max_attempts=3, exponential_wait=3, retry_exceptions=[TemporaryEvaluationError]))
def evaluate_attempt(attempt_id: str):
    with transaction.atomic():
        attempt = Attempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status not in (Attempt.Status.QUEUED, Attempt.Status.RETRY, Attempt.Status.EVALUATING):
            return
        attempt.status = Attempt.Status.EVALUATING
        attempt.evaluation_tries += 1
        attempt.save(update_fields=["status", "evaluation_tries"])
        data = EvaluationInput(attempt.snapshot, attempt.answer, attempt.rubric, attempt.demo_scenario,
                               attempt.evaluation_tries, str(attempt.pk))
    start = time.monotonic()
    evaluator = None
    trace = {}
    trace_id = None
    try:
        evaluator = get_evaluator(data.rubric)
        trace = getattr(evaluator, "last_trace", {}) or {}
        payload = evaluator.evaluate(data)
        trace = payload.pop("_trace", None) or getattr(evaluator, "last_trace", {}) or trace
        score = calculate_score(payload, data.answer, data.rubric)
    except Exception as exc:
        trace = getattr(evaluator, "last_trace", {}) or trace
        transient = isinstance(exc, TemporaryEvaluationError) and attempt.evaluation_tries < 3
        with transaction.atomic():
            current = Attempt.objects.select_for_update().get(pk=attempt_id)
            if current.status != Attempt.Status.GRADED:
                current.status = Attempt.Status.RETRY if transient else Attempt.Status.REVIEW
                from .evaluation import PermanentEvaluationError
                known_error = isinstance(exc, (TemporaryEvaluationError, PermanentEvaluationError))
                # Только собственные безопасные сообщения: никогда не сохраняем тело ответа API.
                from .diagnostics import error_reason
                reason = error_reason(str(exc)[:240]) if known_error else "Внутренняя ошибка проверки."
                current.last_error = reason + (" Ответ сохранён; ожидается автоматический повтор." if transient
                    else " Автопроверка остановлена. Нужен повтор администратором или пересмотр.")
                current.save(update_fields=["status", "last_error"])
                trace_row = EvaluationTrace.objects.create(
                    attempt=current,
                    try_number=attempt.evaluation_tries,
                    provider=str(trace.get("provider", ""))[:40],
                    model=str(trace.get("model", ""))[:160],
                    prompt_version=str(trace.get("prompt_version", ""))[:40],
                    request_payload=trace.get("request_payload", {}),
                    response_payload=trace.get("response_payload", {}),
                    normalized_payload=trace.get("normalized_payload", {}),
                    status=EvaluationTrace.Status.RETRY if transient else EvaluationTrace.Status.FAILED,
                    error_type=type(exc).__name__,
                    error_message=reason,
                    error_metadata=trace.get("error", {}),
                    duration_ms=round((time.monotonic()-start)*1000),
                    input_tokens=trace.get("input_tokens"),
                    output_tokens=trace.get("output_tokens"),
                )
                trace_id = trace_row.pk
                from .services import audit
                audit(None, "evaluation_error", current, trace_id=trace_row.pk,
                      provider=trace.get("provider", ""), model=trace.get("model", ""),
                      status=EvaluationTrace.Status.RETRY if transient else EvaluationTrace.Status.FAILED,
                      error_type=type(exc).__name__, error_message=reason,
                      try_number=attempt.evaluation_tries, will_retry=transient)
        logger.warning("evaluation_error", extra={"context": {"attempt_id": attempt_id,
            "error_type": type(exc).__name__, "will_retry": transient, "try": attempt.evaluation_tries,
            "provider": trace.get("provider"), "model": trace.get("model"), "trace_id": trace_id}})
        if transient:
            raise
        return
    duration = round((time.monotonic()-start)*1000)
    with transaction.atomic():
        current = Attempt.objects.select_for_update().get(pk=attempt_id)
        if current.status == Attempt.Status.GRADED:
            return  # Повтор задания очереди никогда не начисляет баллы повторно.
        trace_row = EvaluationTrace.objects.create(
            attempt=current,
            try_number=attempt.evaluation_tries,
            provider=str(trace.get("provider", payload.get("provider", "demo")))[:40],
            model=str(trace.get("model", payload.get("model", "")))[:160],
            prompt_version=str(trace.get("prompt_version", payload.get("prompt_version", "")))[:40],
            request_payload=trace.get("request_payload", {}),
            response_payload=trace.get("response_payload", {}),
            normalized_payload=payload,
            status=EvaluationTrace.Status.REVIEW if score is None else EvaluationTrace.Status.SUCCESS,
            duration_ms=duration,
            input_tokens=trace.get("input_tokens", payload.get("usage", {}).get("input_tokens")),
            output_tokens=trace.get("output_tokens", payload.get("usage", {}).get("output_tokens")),
        )
        trace_id = trace_row.pk
        _, created = Evaluation.objects.get_or_create(attempt=current, defaults={"payload": payload, "duration_ms": duration,
                                                                                "backend": payload.get("provider", "demo")})
        if not created:
            return  # Конкурирующий worker уже сохранил разбор; балл должен соответствовать именно ему.
        current.score, current.hard_verdict = score, payload["hard_verdict"]
        current.status = Attempt.Status.GRADED if score is not None else Attempt.Status.REVIEW
        current.graded_at = timezone.now() if score is not None else None
        current.last_error = "" if score is not None else "Модель не смогла однозначно проверить hard-часть. Разбор сохранён для пересмотра."
        current.save(update_fields=["score", "hard_verdict", "status", "graded_at", "last_error"])
        from .services import audit
        audit(None, "evaluation_completed", current, trace_id=trace_row.pk, provider=payload.get("provider", "demo"),
              model=payload.get("model", ""), score=score, status=current.status)
    logger.info("evaluation_completed", extra={"context": {"attempt_id": attempt_id, "model": payload["model"],
        "duration_ms": duration, "queue_wait_ms": max(0, round((timezone.now()-attempt.submitted_at).total_seconds()*1000)-duration),
        "score": score, "provider": payload.get("provider", "demo"), "status": current.status, "trace_id": trace_id,
        **payload.get("usage", {})}})

@app.task(queue="maintenance", retry=3)
def expire_attempt(attempt_id: str):
    from .services import finish_attempt
    finish_attempt(attempt_id, expired=True)

@app.periodic(cron="* * * * *")
@app.task(queue="maintenance", retry=3)
async def recover_jobs(timestamp: int):
    """После аварии воркера возвращаем зависшие задания; дедлайны остаются серверными."""
    for job in await app.job_manager.get_stalled_jobs(seconds_since_heartbeat=120):
        await app.job_manager.retry_job(job)
        logger.warning("stalled_job_recovered", extra={"context": {"job_id": job.id}})
    from .services import finish_attempt
    expired = Attempt.objects.filter(status=Attempt.Status.WRITING, expires_at__lte=timezone.now()).values_list("pk", flat=True)[:100]
    for attempt_id in await sync_to_async(list)(expired):
        await sync_to_async(finish_attempt)(attempt_id, expired=True)
    # Завершение конкурса не зависит от открытой вкладки администратора.
    # Если остались спорные/непроверенные ответы, сервис оставит конкурс ACTIVE
    # до ручного решения и не потеряет их из финального рейтинга.
    from .services import auto_finalize_expired_contests
    finalized = await sync_to_async(auto_finalize_expired_contests)()
    if finalized:
        logger.info("contests_auto_finalized", extra={"context": {"contest_ids": finalized}})
