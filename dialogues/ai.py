"""Общий transport/ключ/модель; отдельные схемы и промпты диалогов."""
import json
import re
from trainer.ai_configuration import api_key
from trainer.evaluation import PermanentEvaluationError, TemporaryEvaluationError
from trainer.evaluation_http import post_json, object_schema, usage_from_response, checked_text
from trainer.models import SKILLS
from .prompts import GUARD


def topic_map(session):
    return {topic['code']: topic for topic in session.snapshot['topics']}


def transcript(session):
    return [{'number': turn.number, 'topic_code': turn.topic_code,
             'client_message': turn.client_message, 'answer': turn.answer,
             'timed_out': turn.timed_out} for turn in session.turns.filter(submitted_at__isnull=False)]


def client_context(session):
    history = transcript(session)
    current = topic_map(session)[history[-1]['topic_code']]
    permitted = current['allowed_next'] or list(topic_map(session))
    allowed = sorted(set(permitted + [current['code']]))
    return {'scenario': session.snapshot, 'history': history, 'allowed_topics': allowed,
            'force_finish': len(history) >= session.snapshot['max_turns']}


def client_schema(session):
    return object_schema({'client_message': {'type': 'string'},
        'topic_code': {'type': 'string', 'enum': client_context(session)['allowed_topics']},
        'emotion': {'type': 'string'}, 'finish': {'type': 'boolean'}})


def evaluation_schema():
    return object_schema({'turns': {'type': 'array', 'items': object_schema({
        'number': {'type': 'integer'}, 'hard_verdict': {'type': 'string', 'enum': ['passed', 'violated', 'uncertain']},
        'skills': object_schema({key: {'type': 'integer', 'minimum': 0, 'maximum': 100} for key in SKILLS}),
        'explanation': {'type': 'string'}, 'improved_answer': {'type': 'string'}})},
        'summary': {'type': 'string'}, 'strengths': {'type': 'array', 'items': {'type': 'string'}},
        'improvements': {'type': 'array', 'items': {'type': 'string'}}})


def normalize_client(session, raw):
    if not isinstance(raw, dict) or set(raw) != {'client_message', 'topic_code', 'emotion', 'finish'}:
        raise TemporaryEvaluationError('Неверный формат реплики клиента.')
    context = client_context(session)
    if not isinstance(raw['topic_code'], str) or raw['topic_code'] not in context['allowed_topics']:
        raise TemporaryEvaluationError('Клиент вышел за разрешённые темы сценария.')
    if type(raw['finish']) is not bool or (context['force_finish'] and not raw['finish']):
        raise TemporaryEvaluationError('Клиент не завершил диалог в пределах лимита.')
    message = checked_text(raw['client_message'], limit=1500)
    # Дешёвая дополнительная защита от новых чисел. Смысловую достоверность
    # она не гарантирует: промпт и закрытая диагностика остаются необходимыми.
    known = json.dumps({'scenario': session.snapshot, 'history': context['history']}, ensure_ascii=False)
    numbers = set(re.findall(r'\d+(?:[.,]\d+)?', known))
    if set(re.findall(r'\d+(?:[.,]\d+)?', message)) - numbers:
        raise TemporaryEvaluationError('Клиент добавил число, которого нет в сценарии и переписке.')
    return {**raw, 'client_message': message, 'emotion': checked_text(raw['emotion'], limit=80)}


def normalize_evaluation(session, raw):
    if not isinstance(raw, dict) or set(raw) != {'turns', 'summary', 'strengths', 'improvements'}:
        raise TemporaryEvaluationError('Неверный формат оценки диалога.')
    history = {item['number']: item for item in transcript(session)}
    if not isinstance(raw['turns'], list) or len(raw['turns']) != len(history):
        raise TemporaryEvaluationError('Оценщик проверил не все ответы диалога.')
    result, seen = [], set()
    for row in raw['turns']:
        if not isinstance(row, dict) or set(row) != {'number', 'hard_verdict', 'skills', 'explanation', 'improved_answer'}:
            raise TemporaryEvaluationError('Неверный формат оценки ответа.')
        number = row['number']
        if type(number) is not int or number not in history or number in seen:
            raise TemporaryEvaluationError('Оценщик перепутал номера ответов.')
        seen.add(number)
        skills = row['skills']
        if not isinstance(skills, dict) or set(skills) != set(SKILLS) or any(type(v) is not int or not 0 <= v <= 100 for v in skills.values()):
            raise TemporaryEvaluationError('Оценка навыков вне шкалы 0–100.')
        if row['hard_verdict'] not in ('passed', 'violated', 'uncertain'):
            raise TemporaryEvaluationError('Неизвестный результат hard-проверки.')
        verdict = row['hard_verdict'] if history[number]['answer'].strip() else 'violated'
        score = 0 if verdict == 'violated' else None if verdict == 'uncertain' else round(sum(skills.values())/len(SKILLS))
        result.append({'number': number, 'hard_verdict': verdict, 'skills': skills, 'score': score,
                       'explanation': checked_text(row['explanation'], limit=3000),
                       'improved_answer': checked_text(row['improved_answer'], limit=6000, allow_empty=True)})
    feedback = {}
    for key in ('strengths', 'improvements'):
        if not isinstance(raw[key], list) or len(raw[key]) > 6:
            raise TemporaryEvaluationError('Некорректный список рекомендаций.')
        feedback[key] = [checked_text(x, limit=2000) for x in raw[key]]
    scores = [row['score'] for row in result]
    return {'turns': sorted(result, key=lambda x: x['number']),
            'score': None if None in scores else round(sum(scores)/len(scores)),
            'summary': checked_text(raw['summary'], limit=4000), **feedback}


def call_model(session, stage, trace):
    profile = session.profile
    provider, model = profile['provider'], profile['model']
    if provider not in ('openai', 'openrouter'):
        raise PermanentEvaluationError('Подключите реальную модель в общих настройках нейросети.')
    key = api_key(provider)
    if not key:
        raise PermanentEvaluationError('API-ключ не настроен. Откройте общие настройки нейросети.')
    schema = client_schema(session) if stage == 'client' else evaluation_schema()
    context = client_context(session) if stage == 'client' else {'scenario': session.snapshot, 'history': transcript(session)}
    prompt = session.prompts['client_text' if stage == 'client' else 'evaluator_text']
    body = {'model': model, 'temperature': 0.4 if stage == 'client' else 0,
            'messages': [{'role': 'system', 'content': GUARD + '\n\n' + prompt},
                         {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}],
            'response_format': {'type': 'json_schema', 'json_schema': {
                'name': 'dialogue_' + stage, 'strict': True, 'schema': schema}}}
    budget = 900 if stage == 'client' else 7000
    if provider == 'openai':
        body.update(max_completion_tokens=budget, store=False)
    else:
        body.update(max_tokens=budget, provider={'require_parameters': True, 'allow_fallbacks': False})
        if 'reasoning_enabled' in profile:
            body['reasoning'] = {'enabled': profile['reasoning_enabled']}
    trace['request_payload'] = body
    response = post_json(provider, key, body, trace=trace)
    trace['response_payload'] = response
    trace['usage'] = usage_from_response(response)
    try:
        choice = response['choices'][0]
        if choice['finish_reason'] != 'stop' or choice['message'].get('refusal'):
            raise TemporaryEvaluationError('Модель не завершила ответ. Повторите запрос.')
        raw = json.loads(choice['message']['content'])
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise TemporaryEvaluationError('Модель вернула некорректный JSON.') from error
    normalized = normalize_client(session, raw) if stage == 'client' else normalize_evaluation(session, raw)
    trace['normalized_payload'] = normalized
    return normalized
