import copy
import json
import uuid
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from trainer.models import Attempt, SKILLS
from trainer.people import apply_role
from trainer.evaluation import TemporaryEvaluationError, PermanentEvaluationError
from .models import Scenario, Topic, DialoguePrompt, Session, Turn, DialogueTrace
from . import services, ai
from .tasks import process_dialogue
from .views import state


@override_settings(ALLOW_TEST_EVALUATOR=True, EVALUATOR_BACKEND='openai', EVALUATOR_MODEL='shared-model',
                   OPENAI_API_KEY='test-key-only', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DialogueTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('dialogue-admin', password='test')
        self.scenario = Scenario.objects.create(title='Тест', situation='Возврат вчера. Срок до 5 дней.',
            goal='Узнать срок и ускорение.', persona='Раздражён', first_message='Когда вернут деньги?', first_topic='timing')
        self.topic = Topic.objects.create(scenario=self.scenario, code='timing', title='Срок',
            hard_answer='Возврат до 5 дней.', required_facts='До 5 дней.', allowed_next='speed')
        Topic.objects.create(scenario=self.scenario, code='speed', title='Ускорение',
            hard_answer='Ускорение невозможно.', required_facts='Нельзя ускорить.')
        self.queue = patch('dialogues.tasks.process_dialogue.configure').start()
        self.expiry = patch('dialogues.tasks.expire_dialogue_turn.configure').start()
        self.addCleanup(patch.stopall)

    def start(self, opened=True):
        session = services.start(self.admin, self.scenario.pk)
        if opened:
            session = services.open_turn(session.pk, self.admin)
        return session

    def submit(self, session, answer='Вернут до 5 дней.'):
        turn = session.turns.get(submitted_at__isnull=True)
        return services.save_answer(session.pk, self.admin, turn.pk, answer, 1, submit=True)

    def run_client(self, session, finish=False):
        session.refresh_from_db()
        raw = {'client_message': 'Можно ускорить?' if not finish else 'Хорошо, спасибо.',
               'topic_code': 'speed', 'emotion': 'Спокойный', 'finish': finish}
        with patch('dialogues.tasks.ai.call_model', return_value=raw):
            process_dialogue(str(session.pk), str(session.operation), 'client')
        session.refresh_from_db()
        return session

    def evaluation(self, session):
        return {'turns': [{'number': t.number, 'hard_verdict': 'passed', 'skills': {key: 80 for key in SKILLS},
                          'explanation': 'Смысл сохранён.', 'improved_answer': 'Улучшенный ответ.'}
                         for t in session.turns.filter(submitted_at__isnull=False)],
                'summary': 'Последовательный разговор.', 'strengths': ['Понятно.'], 'improvements': ['Короче.']}

    def test_routes_and_admin_models_deny_every_non_admin_role(self):
        session = self.start()
        for role in ('unassigned', 'employee', 'group_leader', 'sector_leader'):
            user = get_user_model().objects.create_user(role)
            apply_role(user, role)
            self.client.force_login(user)
            urls = [reverse('dialogues:index'), reverse('dialogues:session', args=[session.pk]),
                    reverse('dialogues:status', args=[session.pk]), '/admin/dialogues/scenario/',
                    '/admin/dialogues/dialogueprompt/', '/admin/dialogues/dialoguetrace/']
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 403)
            for command in ('open', 'retry', 'draft', 'submit'):
                self.assertEqual(self.client.post(reverse('dialogues:action', args=[session.pk, command]),
                    data='{}', content_type='application/json').status_code, 403)
            self.assertEqual(self.client.post(reverse('dialogues:start', args=[self.scenario.pk])).status_code, 403)
            with self.assertRaises(PermissionDenied):
                services.start(user, self.scenario.pk)

    def test_anonymous_redirects_to_login(self):
        self.assertEqual(self.client.get(reverse('dialogues:index')).status_code, 302)

    def test_other_admin_can_read_but_cannot_change_run(self):
        session = self.start()
        other = get_user_model().objects.create_superuser('other-admin', password='test')
        self.client.force_login(other)
        response = self.client.get(reverse('dialogues:status', args=[session.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['can_edit'])
        self.assertEqual(self.client.post(reverse('dialogues:action', args=[session.pk, 'open'])).status_code, 403)

    def test_start_snapshots_scenario_and_distinct_prompts_without_key(self):
        session = self.start(False)
        self.assertEqual(session.status, 'ready')
        self.assertIsNone(session.turns.get().expires_at)
        self.assertEqual(session.profile, {'provider': 'openai', 'model': 'shared-model'})
        self.assertNotIn('test-key-only', json.dumps(session.profile))
        self.assertNotIn('prompt_text', session.profile)
        before = copy.deepcopy(session.prompts)
        DialoguePrompt.objects.update(client_text='Новый клиент')
        self.topic.hard_answer = 'Новый hard'
        self.topic.save()
        session.refresh_from_db()
        self.assertEqual(session.prompts, before)
        self.assertEqual(session.snapshot['topics'][0]['hard_answer'], 'Возврат до 5 дней.')
        self.assertEqual(Attempt.objects.count(), 0)

    def test_ready_state_does_not_expose_unopened_messages_or_hard(self):
        session = self.start(False)
        data = state(session, self.admin)
        self.assertEqual(data['turns'], [])
        self.assertIsNone(data['current'])
        self.assertNotIn(self.scenario.first_message, json.dumps(data, ensure_ascii=False))

    def test_reopening_turn_does_not_reset_deadline(self):
        session = self.start()
        deadline = session.turns.get().expires_at
        services.open_turn(session.pk, self.admin)
        self.assertEqual(session.turns.get().expires_at, deadline)
        self.expiry.assert_called_once()

    def test_revision_conflict_preserves_first_saved_text(self):
        session = self.start()
        turn = session.turns.get()
        services.save_answer(session.pk, self.admin, turn.pk, 'Первый текст', 1)
        for revision in (1,):
            with self.assertRaises(ValidationError):
                services.save_answer(session.pk, self.admin, turn.pk, 'Из второй вкладки', revision, submit=True)
        turn.refresh_from_db()
        self.assertEqual(turn.answer, 'Первый текст')
        self.assertIsNone(turn.submitted_at)

    def test_late_submit_uses_saved_draft_and_pauses_next_turn(self):
        session = self.start()
        turn = session.turns.get()
        services.save_answer(session.pk, self.admin, turn.pk, 'Сохранённый текст', 1)
        turn.expires_at = timezone.now()-timedelta(seconds=1)
        turn.save(update_fields=['expires_at'])
        services.save_answer(session.pk, self.admin, turn.pk, 'Опоздавший текст', 2, submit=True)
        turn.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(turn.answer, 'Сохранённый текст')
        self.assertTrue(turn.timed_out)
        self.assertFalse(session.auto_advance)
        self.run_client(session)
        self.assertFalse(session.auto_advance)
        self.assertIsNone(session.turns.get(number=2).expires_at)

    def test_double_submit_and_duplicate_worker_do_not_duplicate_turns(self):
        session = self.start()
        turn = session.turns.get()
        session = self.submit(session)
        operation = str(session.operation)
        services.save_answer(session.pk, self.admin, turn.pk, 'Другой текст', 2, submit=True)
        self.queue.assert_called_once()
        self.run_client(session)
        with patch('dialogues.tasks.ai.call_model') as call:
            process_dialogue(str(session.pk), operation, 'client')
        call.assert_not_called()
        self.assertEqual(session.turns.count(), 2)

    def test_reaction_gets_actual_answer_and_history_and_new_hard_matches_topic(self):
        session = self.submit(self.start(), answer='Ускорить могу!')
        context = ai.client_context(session)
        self.assertEqual(context['history'][0]['answer'], 'Ускорить могу!')
        self.run_client(session)
        pending = state(session, self.admin)
        self.assertEqual(len(pending['turns']), 1)
        self.assertIsNone(pending['current'])
        session = services.open_turn(session.pk, self.admin)
        data = state(session, self.admin)
        self.assertEqual(data['current']['hard_answer'], 'Ускорение невозможно.')
        self.assertEqual(len(data['turns']), 2)

    def test_unknown_topic_and_new_number_are_rejected(self):
        session = self.submit(self.start())
        raw = {'client_message': 'Когда?', 'topic_code': 'outside', 'emotion': 'Спокойный', 'finish': False}
        with self.assertRaises(TemporaryEvaluationError):
            ai.normalize_client(session, raw)
        raw.update(topic_code='speed', client_message='Почему ждать 987 дней?')
        with self.assertRaises(TemporaryEvaluationError):
            ai.normalize_client(session, raw)

    def test_configured_transitions_are_enforced(self):
        Topic.objects.create(scenario=self.scenario, code='extra', title='Иное', hard_answer='Факт', required_facts='Факт')
        session = self.submit(self.start())
        self.assertNotIn('extra', ai.client_context(session)['allowed_topics'])

    def test_max_turns_requires_closing_reply(self):
        session = self.submit(self.start())
        session.snapshot['max_turns'] = 1
        session.save()
        with self.assertRaises(TemporaryEvaluationError):
            ai.normalize_client(session, {'client_message':'Ещё вопрос?', 'topic_code':'timing','emotion':'Спокойный','finish':False})

    def test_invalid_scenario_cannot_start(self):
        self.topic.allowed_next = 'missing'
        self.topic.save()
        with self.assertRaises(ValidationError):
            services.start(self.admin, self.scenario.pk)
        self.assertFalse(Session.objects.exists())

    @override_settings(OPENAI_API_KEY='')
    def test_missing_key_does_not_create_run(self):
        with self.assertRaises(ValidationError):
            services.start(self.admin, self.scenario.pk)
        self.assertFalse(Session.objects.exists())

    def test_transient_failure_retries_then_preserves_answer_for_manual_retry(self):
        session = self.submit(self.start())
        with patch('dialogues.tasks.ai.call_model', side_effect=TemporaryEvaluationError('Сбой API')):
            process_dialogue(str(session.pk), str(session.operation), 'client')
            session.refresh_from_db()
            self.assertEqual(session.status, 'generating')
            process_dialogue(str(session.pk), str(session.operation), 'client')
        session.refresh_from_db()
        self.assertEqual(session.status, 'failed')
        self.assertEqual(session.turns.get().answer, 'Вернут до 5 дней.')
        old_operation = session.operation
        services.retry(session.pk, self.admin)
        session.refresh_from_db()
        self.assertEqual(session.status, 'generating')
        self.assertNotEqual(session.operation, old_operation)
        with patch('dialogues.tasks.ai.call_model') as call:
            process_dialogue(str(session.pk), str(old_operation), 'client')
        call.assert_not_called()

    def test_unknown_error_does_not_leak_secret(self):
        session = self.submit(self.start())
        with patch('dialogues.tasks.ai.call_model', side_effect=RuntimeError('secret-value')):
            process_dialogue(str(session.pk), str(session.operation), 'client')
        session.refresh_from_db()
        self.assertNotIn('secret-value', session.error)
        self.assertNotIn('secret-value', DialogueTrace.objects.get(session=session).error)

    def test_evaluation_is_separate_and_does_not_change_normal_attempts(self):
        session = self.run_client(self.submit(self.start()), finish=True)
        self.assertEqual(session.status, 'evaluating')
        normalized = ai.normalize_evaluation(session, self.evaluation(session))
        operation = str(session.operation)
        with patch('dialogues.tasks.ai.call_model', return_value=normalized):
            process_dialogue(str(session.pk), operation, 'evaluation')
            process_dialogue(str(session.pk), operation, 'evaluation')
        session.refresh_from_db()
        self.assertEqual(session.status, 'completed')
        self.assertEqual(session.score, 80)
        self.assertEqual(session.traces.filter(stage='evaluation').count(), 1)
        self.assertEqual(Attempt.objects.count(), 0)

    def test_empty_and_violated_answers_are_zero_uncertain_is_not_zero(self):
        session = self.start()
        turn = session.turns.get()
        turn.expires_at = timezone.now()-timedelta(seconds=1)
        turn.save(update_fields=['expires_at'])
        session = services.expire(session.pk)
        result = self.evaluation(session)
        self.assertEqual(ai.normalize_evaluation(session, result)['score'], 0)
        turn.answer = 'Ответ'
        turn.save(update_fields=['answer'])
        result['turns'][0]['hard_verdict'] = 'uncertain'
        self.assertIsNone(ai.normalize_evaluation(session, result)['score'])
        result['turns'][0]['hard_verdict'] = 'violated'
        self.assertEqual(ai.normalize_evaluation(session, result)['score'], 0)

    def test_evaluator_cannot_drop_or_duplicate_turns_or_return_invalid_scores(self):
        session = self.submit(self.start())
        raw = self.evaluation(session)
        raw['turns'] = []
        with self.assertRaises(TemporaryEvaluationError):
            ai.normalize_evaluation(session, raw)
        raw = self.evaluation(session)
        raw['turns'][0]['skills']['tone'] = True
        with self.assertRaises(TemporaryEvaluationError):
            ai.normalize_evaluation(session, raw)

    def test_transport_uses_shared_key_and_separate_client_prompt(self):
        session = self.submit(self.start())
        payload = {'client_message':'Можно ускорить?', 'topic_code':'speed', 'emotion':'Тревога', 'finish':False}
        response = {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(payload)}}],
                    'usage':{'prompt_tokens':25,'completion_tokens':12}}
        trace = {}
        with patch('dialogues.ai.post_json', return_value=response) as transport:
            result = ai.call_model(session, 'client', trace)
        provider, key, body = transport.call_args.args
        self.assertEqual((provider,key,body['model']), ('openai','test-key-only','shared-model'))
        self.assertIn(session.prompts['client_text'], body['messages'][0]['content'])
        self.assertNotIn('test-key-only', json.dumps(trace))
        self.assertEqual(result['topic_code'], 'speed')
        self.assertEqual(trace['usage']['input_tokens'], 25)

    def test_stale_recovery_and_expiry_do_not_need_open_browser(self):
        session = self.start()
        Turn.objects.filter(session=session).update(expires_at=timezone.now()-timedelta(seconds=5))
        services.recover()
        session.refresh_from_db()
        self.assertEqual(session.status, 'generating')
        self.assertTrue(session.turns.get().timed_out)
        Session.objects.filter(pk=session.pk).update(updated_at=timezone.now()-timedelta(minutes=10), processing_token=uuid.uuid4())
        services.recover()
        session.refresh_from_db()
        self.assertIsNone(session.processing_token)

    def test_lab_lists_are_paginated_and_link_is_only_in_admin(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('dialogues:index'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['scenarios'].paginator.per_page, 10)
        self.assertEqual(response.context['sessions'].paginator.per_page, 10)
        self.assertContains(self.client.get('/admin/'), reverse('dialogues:index'))
        self.assertNotContains(self.client.get('/'), reverse('dialogues:index'))

    def test_csrf_required_for_state_changes(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(reverse('dialogues:start', args=[self.scenario.pk])).status_code, 403)

    def test_unopened_turn_rejects_draft_and_invalid_payload(self):
        session = self.start(False)
        with self.assertRaises(ValidationError):
            services.save_answer(session.pk, self.admin, session.turns.get().pk, 'Текст', 1)
        self.client.force_login(self.admin)
        url = reverse('dialogues:action', args=[session.pk,'draft'])
        self.assertEqual(self.client.post(url, '[]', content_type='application/json').status_code, 400)
        self.assertEqual(self.client.post(url, '{', content_type='application/json').status_code, 400)

    def test_stale_worker_result_is_discarded_after_operation_changes(self):
        session = self.submit(self.start())
        operation = str(session.operation)
        def changed(*args):
            Session.objects.filter(pk=session.pk).update(operation=uuid.uuid4())
            return {'client_message':'Можно ускорить?', 'topic_code':'speed', 'emotion':'Спокойно', 'finish':False}
        with patch('dialogues.tasks.ai.call_model', side_effect=changed):
            process_dialogue(str(session.pk), operation, 'client')
        self.assertEqual(session.turns.count(), 1)
        self.assertFalse(session.traces.exists())

    def test_prompt_change_increments_version_without_touching_regular_prompt(self):
        from trainer.models import EvaluationPrompt
        from .admin import PromptAdmin
        from django.contrib.admin import site
        from django.test import RequestFactory
        prompt = DialoguePrompt.objects.get(pk=1)
        previous_version = prompt.version
        normal = EvaluationPrompt.objects.get(pk=1).text
        request = RequestFactory().post('/')
        request.user = self.admin
        prompt.client_text = 'Обновлённый промпт клиента.'
        form = type('Form', (), {'changed_data':['client_text']})()
        PromptAdmin(DialoguePrompt, site).save_model(request, prompt, form, True)
        self.assertEqual(prompt.version, previous_version+1)
        self.assertEqual(EvaluationPrompt.objects.get(pk=1).text, normal)

    def test_admin_rejects_scenario_with_no_matching_first_topic(self):
        from .admin import TopicInline
        from django.contrib.admin import site
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.user = self.admin
        formset_class = TopicInline(Scenario, site).get_formset(request, self.scenario)
        formset = formset_class(data={'topics-TOTAL_FORMS':'1','topics-INITIAL_FORMS':'0',
            'topics-0-code':'other','topics-0-title':'Иное','topics-0-hard_answer':'Факт',
            'topics-0-required_facts':'Факт'}, instance=self.scenario, prefix='topics')
        self.assertFalse(formset.is_valid())
        self.assertTrue(formset.non_form_errors())


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.db import connection, close_old_connections
from django.test import TransactionTestCase


@skipUnless(connection.vendor == 'postgresql', 'Row-lock concurrency requires PostgreSQL')
@override_settings(ALLOW_TEST_EVALUATOR=True, EVALUATOR_BACKEND='openai', EVALUATOR_MODEL='shared-model',
                   OPENAI_API_KEY='test-key-only', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DialogueConcurrencyTests(TransactionTestCase):
    def test_two_tabs_cannot_overwrite_same_revision(self):
        user = get_user_model().objects.create_superuser('concurrent-admin', password='test')
        scenario = Scenario.objects.create(title='Тест', situation='Факты.', goal='Узнать.',
            persona='Спокоен', first_message='Когда?', first_topic='timing')
        Topic.objects.create(scenario=scenario, code='timing', title='Срок',
            hard_answer='До пяти дней.', required_facts='До пяти дней.')
        with patch('dialogues.tasks.expire_dialogue_turn.configure'):
            session = services.open_turn(services.start(user, scenario.pk).pk, user)
        turn_id = session.turns.get().pk
        barrier = Barrier(2)
        def save(text):
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=user.pk)
                barrier.wait(timeout=10)
                try:
                    services.save_answer(session.pk, actor, turn_id, text, 1)
                    return 'saved'
                except ValidationError:
                    return 'conflict'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, ['Первый текст', 'Второй текст']))
        self.assertCountEqual(outcomes, ['saved', 'conflict'])
        self.assertEqual(Turn.objects.get(pk=turn_id).revision, 1)
