"""Серверные дедлайны и переходы диалога. Любая мутация блокирует прохождение."""
import uuid
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from trainer.evaluation_profiles import current_profile, profile_has_key
from trainer.services import audit
from .models import Scenario, DialoguePrompt, Session, Turn


def require_admin(user):
    if not user or not user.is_active or not user.is_superuser:
        raise PermissionDenied


def snapshot_scenario(scenario):
    scenario.full_clean()
    topics = []
    for topic in scenario.topics.all():
        topic.full_clean()
        topics.append({'code': topic.code, 'title': topic.title, 'hard_answer': topic.hard_answer,
                       'required_facts': topic.required_facts,
                       'allowed_next': [x.strip() for x in topic.allowed_next.split(',') if x.strip()]})
    codes = {x['code'] for x in topics}
    if not 1 <= len(topics) <= 10 or scenario.first_topic not in codes:
        raise ValidationError('Добавьте от 1 до 10 hard-блоков и укажите существующую первую тему.')
    if any(set(x['allowed_next']) - codes for x in topics):
        raise ValidationError('В переходах указан несуществующий код темы.')
    for topic in topics:
        if not topic['hard_answer'].strip() or not topic['required_facts'].strip():
            raise ValidationError('Каждой теме нужны hard-ответ и обязательные факты.')
    snapshot = {field: getattr(scenario, field) for field in
                ('title', 'situation', 'goal', 'persona', 'first_message', 'first_topic', 'time_limit_seconds', 'max_turns')}
    snapshot['topics'] = topics
    import json
    if len(json.dumps(snapshot, ensure_ascii=False)) > 24000:
        raise ValidationError('Сократите сценарий с hard-блоками до 24 000 символов.')
    return snapshot


@transaction.atomic
def start(user, scenario_id):
    require_admin(user)
    scenario = Scenario.objects.select_for_update().get(pk=scenario_id, enabled=True)
    snapshot = snapshot_scenario(scenario)
    profile = current_profile()
    if not profile_has_key(profile):
        raise ValidationError('Сохраните API-ключ в общих настройках нейросети.')
    profile = {key: profile[key] for key in ('provider', 'model', 'reasoning_enabled') if key in profile}
    if profile['provider'] not in ('openai', 'openrouter'):
        raise ValidationError('Для диалогов нужна реальная модель.')
    prompts, _ = DialoguePrompt.objects.get_or_create(pk=1)
    prompts.full_clean()
    session = Session.objects.create(user=user, scenario=scenario, snapshot=snapshot, profile=profile,
        prompts={'version': prompts.version, 'client_text': prompts.client_text, 'evaluator_text': prompts.evaluator_text})
    Turn.objects.create(session=session, number=1, topic_code=snapshot['first_topic'], client_message=snapshot['first_message'])
    audit(user, 'dialogue_started', session, scenario_id=scenario.pk, prompt_version=prompts.version)
    return session


def locked(session_id, user=None):
    if user is not None:
        require_admin(user)
    session = Session.objects.select_for_update().get(pk=session_id)
    if user is not None and session.user_id != user.pk:
        raise PermissionDenied
    return session


def schedule(session, stage, *, new=True, delay=0):
    from .tasks import process_dialogue
    session.status = Session.Status.GENERATING if stage == 'client' else Session.Status.EVALUATING
    if new:
        session.operation = uuid.uuid4()
        session.job_tries = 0
    session.processing_token = None
    session.claimed_at = None
    session.failed_stage = stage
    session.error = ''
    session.save()
    process_dialogue.configure(schedule_at=timezone.now()+timedelta(seconds=delay)).defer(
        session_id=str(session.pk), operation=str(session.operation), stage=stage)


@transaction.atomic
def open_turn(session_id, user):
    session = locked(session_id, user)
    if session.status == Session.Status.WRITING:
        expire_locked(session)
        return session
    if session.status != Session.Status.READY:
        return session
    turn = session.turns.get(submitted_at__isnull=True)
    now = timezone.now()
    turn.opened_at = now
    turn.expires_at = now + timedelta(seconds=session.snapshot['time_limit_seconds'])
    turn.save(update_fields=['opened_at', 'expires_at'])
    session.status = Session.Status.WRITING
    session.save(update_fields=['status', 'updated_at'])
    from .tasks import expire_dialogue_turn
    expire_dialogue_turn.configure(schedule_at=turn.expires_at).defer(session_id=str(session.pk), turn_id=turn.pk)
    audit(user, 'dialogue_turn_opened', session, turn=turn.number)
    return session


def check_input(answer, revision):
    if not isinstance(answer, str) or len(answer) > 6000 or type(revision) is not int or revision < 1 or revision > 2147483647:
        raise ValidationError('Некорректный текст или номер версии черновика.')


def conflict(turn, answer, revision):
    if revision < turn.revision or (revision == turn.revision and answer != turn.answer):
        raise ValidationError('Черновик изменён в другой вкладке. Скопируйте текст и обновите страницу.')


def finish_locked(session, turn, *, timed_out):
    turn.submitted_at = turn.expires_at if timed_out else timezone.now()
    turn.timed_out = timed_out
    turn.save()
    session.auto_advance = not timed_out
    schedule(session, 'client')
    audit(session.user, 'dialogue_turn_submitted', session, turn=turn.number, timed_out=timed_out)


def expire_locked(session):
    if session.status != Session.Status.WRITING:
        return False
    turn = session.turns.get(submitted_at__isnull=True)
    if turn.expires_at <= timezone.now():
        finish_locked(session, turn, timed_out=True)
        return True
    return False


@transaction.atomic
def save_answer(session_id, user, turn_id, answer, revision, submit=False):
    check_input(answer, revision)
    session = locked(session_id, user)
    turn = session.turns.get(pk=turn_id)
    if turn.submitted_at is not None:
        return session
    if session.status != Session.Status.WRITING or turn.opened_at is None:
        raise ValidationError('Этот ход ещё не открыт.')
    if expire_locked(session):
        return session  # Поздний запрос не меняет последний сохранённый черновик.
    conflict(turn, answer, revision)
    if submit and not answer.strip():
        raise ValidationError('Напишите ответ перед отправкой.')
    turn.answer, turn.revision = answer, revision
    turn.save(update_fields=['answer', 'revision'])
    if submit:
        finish_locked(session, turn, timed_out=False)
    return session


@transaction.atomic
def expire(session_id, turn_id=None):
    session = locked(session_id)
    if turn_id is not None and not session.turns.filter(pk=turn_id, submitted_at__isnull=True).exists():
        return session
    expire_locked(session)
    return session


@transaction.atomic
def retry(session_id, user):
    session = locked(session_id, user)
    if session.status == Session.Status.FAILED and session.failed_stage in ('client', 'evaluation'):
        schedule(session, session.failed_stage)
        audit(user, 'dialogue_retried', session, stage=session.failed_stage)
    return session


def recover():
    now = timezone.now()
    ids = list(Session.objects.filter(status=Session.Status.WRITING,
        turns__submitted_at__isnull=True, turns__expires_at__lte=now).values_list('pk', flat=True)[:100])
    for session_id in ids:
        expire(session_id)
    # Очередь восстанавливает stalled jobs; дополнительно возвращаем потерянный lease.
    cutoff = now-timedelta(seconds=max(settings.EVALUATOR_TIMEOUT+60, 180))
    for session_id in Session.objects.filter(status__in=['generating', 'evaluating'], updated_at__lt=cutoff).values_list('pk', flat=True)[:100]:
        with transaction.atomic():
            session = locked(session_id)
            if session.status in ('generating', 'evaluating') and session.updated_at < cutoff:
                if session.job_tries >= 3:
                    session.status = Session.Status.FAILED
                    session.error = 'Запрос прервался. Ответы сохранены; повторите запрос.'
                    session.processing_token = None
                    session.save()
                else:
                    schedule(session, session.failed_stage, new=False)
