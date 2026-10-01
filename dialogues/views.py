import json
from functools import wraps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from trainer.pagination import page_links
from .models import Scenario, Session, Turn
from . import services


def admin_only(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        services.require_admin(request.user)
        return view(request, *args, **kwargs)
    return wrapped


@admin_only
@require_GET
def index(request):
    scenarios = Paginator(Scenario.objects.all(), 10).get_page(request.GET.get('scenarios_page'))
    sessions = Paginator(Session.objects.select_related('user'), 10).get_page(request.GET.get('page'))
    return render(request, 'dialogues/index.html', {'scenarios': scenarios, 'sessions': sessions,
        'scenario_links': page_links(request, scenarios, 'scenarios_page'), 'session_links': page_links(request, sessions)})


@admin_only
@require_POST
def start(request, scenario_id):
    get_object_or_404(Scenario, pk=scenario_id, enabled=True)
    try:
        session = services.start(request.user, scenario_id)
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
        return redirect('dialogues:index')
    return redirect('dialogues:session', session_id=session.pk)


@transaction.atomic
def state(session, user):
    session = Session.objects.select_for_update().get(pk=session.pk)
    now = timezone.now()
    turns = list(session.turns.filter(opened_at__isnull=False))
    current = next((t for t in turns if t.submitted_at is None), None)
    topics = {topic['code']: topic for topic in session.snapshot['topics']}
    def stamp(value):
        return value.isoformat() if value else None
    return {'status': session.status, 'status_label': session.get_status_display(), 'server_time': stamp(now),
        'can_edit': session.user_id == user.pk, 'auto_advance': session.auto_advance,
        'max_turns': session.snapshot['max_turns'], 'time_limit_seconds': session.snapshot['time_limit_seconds'],
        'error': session.error, 'closing_message': session.closing_message,
        'turns': [{'id': t.pk, 'number': t.number, 'client_message': t.client_message,
                   'answer': t.answer, 'submitted': t.submitted_at is not None, 'timed_out': t.timed_out,
                   'topic_title': topics[t.topic_code]['title'], 'hard_answer': topics[t.topic_code]['hard_answer']} for t in turns],
        'current': {'id': current.pk, 'number': current.number, 'answer': current.answer,
                    'revision': current.revision, 'expires_at': stamp(current.expires_at),
                    'hard_answer': topics[current.topic_code]['hard_answer'],
                    'topic_title': topics[current.topic_code]['title']} if current else None,
        'result': session.result if session.status == Session.Status.COMPLETED else None}


@admin_only
@require_GET
def session_page(request, session_id):
    session = get_object_or_404(Session, pk=session_id)
    session = services.expire(session.pk)
    return render(request, 'dialogues/session.html', {'session': session, 'initial': state(session, request.user)})


@admin_only
@require_GET
def status(request, session_id):
    get_object_or_404(Session, pk=session_id)
    response = JsonResponse(state(services.expire(session_id), request.user))
    response['Cache-Control'] = 'no-store'
    return response


@admin_only
@require_POST
def action(request, session_id, command):
    get_object_or_404(Session, pk=session_id)
    try:
        if command == 'open':
            session = services.open_turn(session_id, request.user)
        elif command == 'retry':
            session = services.retry(session_id, request.user)
        elif command in ('draft', 'submit'):
            try:
                payload = json.loads(request.body)
            except (ValueError, UnicodeDecodeError):
                return JsonResponse({'error': 'Некорректный запрос.'}, status=400)
            if not isinstance(payload, dict) or type(payload.get('turn_id')) is not int:
                return JsonResponse({'error': 'Некорректный номер хода.'}, status=400)
            session = services.save_answer(session_id, request.user, payload['turn_id'],
                payload.get('answer'), payload.get('revision'), submit=command == 'submit')
        else:
            return JsonResponse({'error': 'Неизвестное действие.'}, status=404)
    except (ValidationError, Turn.DoesNotExist) as error:
        return JsonResponse({'error': '; '.join(error.messages) if isinstance(error, ValidationError) else 'Ход не найден.'}, status=409)
    response = JsonResponse(state(session, request.user))
    response['Cache-Control'] = 'no-store'
    return response
