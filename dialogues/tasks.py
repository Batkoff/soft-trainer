"""Генерация и оценка выполняются воркером, без долгих HTTP-запросов страницы."""
import logging
import time
import uuid
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from procrastinate.contrib.django import app
from trainer.evaluation import TemporaryEvaluationError, PermanentEvaluationError
from trainer.services import audit
from .models import Session, Turn, DialogueTrace
from . import ai, services

logger = logging.getLogger('trainer.dialogues')


@app.task(queue='evaluation', retry=0)
def process_dialogue(session_id: str, operation: str, stage: str):
    expected = Session.Status.GENERATING if stage == 'client' else Session.Status.EVALUATING
    with transaction.atomic():
        session = Session.objects.select_for_update().filter(pk=session_id).first()
        if not session or str(session.operation) != operation or session.status != expected:
            return
        if session.processing_token and session.claimed_at and (timezone.now()-session.claimed_at).total_seconds() < settings.EVALUATOR_TIMEOUT+30:
            return
        token = uuid.uuid4()
        session.processing_token, session.claimed_at = token, timezone.now()
        session.job_tries += 1
        session.save()
    started, trace, result, failure = time.monotonic(), {}, None, None
    try:
        result = ai.call_model(session, stage, trace)
    except Exception as error:
        failure = error
    with transaction.atomic():
        current = Session.objects.select_for_update().filter(pk=session_id).first()
        if not current or current.processing_token != token or str(current.operation) != operation or current.status != expected:
            return
        known = isinstance(failure, (TemporaryEvaluationError, PermanentEvaluationError))
        reason = str(failure)[:300] if known else 'Внутренняя ошибка запроса. Ответы сохранены.' if failure else ''
        row = DialogueTrace.objects.create(session=current, stage=stage, provider=current.profile['provider'],
            model=current.profile['model'], prompt_version=current.prompts['version'],
            request_payload=trace.get('request_payload', {}), response_payload=trace.get('response_payload', {}),
            normalized_payload=trace.get('normalized_payload', {}), error=reason,
            usage=trace.get('usage', {}), duration_ms=round((time.monotonic()-started)*1000))
        current.processing_token, current.claimed_at = None, None
        if failure:
            transient = isinstance(failure, TemporaryEvaluationError) and current.job_tries < 2
            audit(None, 'dialogue_request_failed', current, stage=stage, trace_id=row.pk,
                  error_type=type(failure).__name__, will_retry=transient)
            logger.warning('dialogue_request_failed', extra={'context': {'session_id': str(current.pk),
                'stage': stage, 'error_type': type(failure).__name__, 'trace_id': row.pk}})
            if transient:
                services.schedule(current, stage, new=False, delay=2)
            else:
                current.status, current.error = Session.Status.FAILED, reason
                current.save()
            return
        if stage == 'client':
            if result['finish']:
                current.closing_message = result['client_message']
                services.schedule(current, 'evaluation')
            else:
                number = current.turns.count()+1
                Turn.objects.create(session=current, number=number, topic_code=result['topic_code'],
                    client_message=result['client_message'], emotion=result['emotion'])
                current.status = Session.Status.READY
                current.error = ''
                current.save()
        else:
            current.result, current.score = result, result['score']
            current.status = Session.Status.COMPLETED
            current.error = ''
            current.save()
        audit(None, 'dialogue_request_completed', current, stage=stage, trace_id=row.pk)


@app.task(queue='maintenance', retry=3)
def expire_dialogue_turn(session_id: str, turn_id: int):
    if Session.objects.filter(pk=session_id).exists():
        services.expire(session_id, turn_id)
