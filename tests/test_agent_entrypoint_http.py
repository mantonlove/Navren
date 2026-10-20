"""实际隔离HTTP验收草案：脚本供应器仅证接线/边界，非付费语义或性能。"""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import json
import os
import hashlib
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from agent_runtime.agent_factory import create_agent
from agent_runtime.jev_client import JevError
from agent_runtime.request_privacy import RequestPrivacyPolicy
from server import create_server
from voice_gateway import VoiceGateway
from scripts.run_jev_ab import FrozenEvidenceRuntime
from tests.window_integration import semantic_oracle as oracle
from test_native_conversation import ScriptedPrimary, Reviewer, call, last_tool


class Primary(ScriptedPrimary):
    def __init__(self, *steps):
        super().__init__(*steps)
        self.schemas = []

    def complete(self, messages, schemas, **kwargs):
        self.schemas.append(deepcopy(schemas))
        return super().complete(messages, schemas, **kwargs)


class NoJudge:
    def __init__(self):
        self.states = []

    def evaluate(self, state, questions, **kwargs):
        self.states.append(deepcopy(state))
        raise AssertionError('trusted privacy/fact scope must skip classification')


class SubscriptionJudge:
    def __init__(self, provider, unknown_usage=False):
        self.provider, self.unknown_usage, self.states = provider, unknown_usage, []

    def evaluate(self, state, questions, *, deadline=None):
        self.states.append(deepcopy(state))
        if self.unknown_usage:
            raise JevError('timeout', {'request_sent': True, 'usage': None})
        selected = {'cap_subscriptions', 'fit_subscription_candidates_read'}
        answers = {key: ({'value': 'yes' if key in selected else 'no'} if self.provider == 'native'
                         else {'type': 'noul', 'noul': .99 if key in selected else .01})
                   for key in questions}
        return answers, {'request_sent': True, 'outcome': 'completed',
                         'usage': {'input_tokens': 10, 'output_tokens': 1}}


def hypothetical_answer(messages, schemas):
    facts = last_tool(messages)['facts']
    ids = [item['id'] for item in facts if item.get('value') == 12345]
    if not ids:
        raise AssertionError('actual hypothetical facts missing')
    return call('answer_facts', {'fact_ids': ids})


def facts_answer(messages, schemas):
    outputs = [json.loads(item['content']) for item in messages if item['role'] == 'tool']
    ids = [fact['id'] for output in outputs for fact in output.get('facts', [])]
    if not ids:
        raise AssertionError('actual registered read facts missing')
    return call('answer_facts', {'fact_ids': ids})


def protected_snapshot(gateway, workspace):
    """只读取本测试新建临时库的受保护业务表；不读取session或口令字段。"""
    result = {}
    with gateway.bank.connect() as db:
        for table in ('accounts', 'profiles', 'transactions', 'operations', 'investment_lots',
                      'mandates', 'subscriptions', 'cards', 'card_controls', 'wealth_goals', 'goal_allocations'):
            columns = {item['name'] for item in db.execute('PRAGMA table_info(' + table + ')')}
            if 'workspace_id' not in columns:
                continue
            rows = [dict(item) for item in db.execute('SELECT * FROM ' + table + ' WHERE workspace_id=?', (workspace,))]
            encoded = json.dumps(sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)), sort_keys=True).encode()
            result[table] = {'count': len(rows), 'sha256': hashlib.sha256(encoded).hexdigest()}
    return result


class EntryFactoryHTTPTests(unittest.TestCase):
    @contextmanager
    def runtime(self, provider, primary, judge):
        with tempfile.TemporaryDirectory(prefix='navren-entry-draft-http-') as directory:
            with (oracle._isolated_environment(Path(directory)),
                  patch.dict(os.environ, {'NAVREN_FINANCIAL_DB': '', 'NAVREN_PUBLIC_SYNC_ENABLED': 'false'})):
                catalog = oracle._NoNetworkCatalog()
                catalog.stop = lambda **kwargs: True
                def trusted_factory(*, route_provider='off'):
                    return create_agent(route_provider=route_provider, primary=primary,
                                        reviewer=Reviewer(), judge=judge)
                # Injection is trusted test code. HTTP bodies never become factory kwargs.
                with patch('agent_runtime.agent_factory.create_agent', side_effect=trusted_factory):
                    server = create_server(port=0, database=str(Path(directory) / 'bank.sqlite3'),
                        market_database=str(Path(directory) / 'market.sqlite3'), public_catalog=catalog,
                        public_sync_enabled=False, voice=VoiceGateway(key='', allow_cloud_asr=False),
                        agent_route_provider=provider)
                gateway = server.finance
                cache = Path(directory) / 'funds'
                cache.mkdir(exist_ok=True)
                oracle._bind_history_cache(gateway, cache)
                self.assertIsNone(gateway.financial_data)
                for database in (gateway.bank.database, gateway.market.path):
                    self.assertTrue(Path(database).resolve().is_relative_to(Path(directory).resolve()))
                server.evidence_runtime.close()
                server.evidence_runtime = FrozenEvidenceRuntime(gateway.knowledge_provider)
                workspace, session = gateway.create_session()
                base = 'http://127.0.0.1:' + str(server.server_port)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    yield server, gateway, workspace, session, base
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(2)
                    if thread.is_alive():
                        raise AssertionError('temporary HTTP server not reaped')

    def post(self, runtime, body, *, stream=False, csrf=True):
        server, gateway, workspace, session, base = runtime
        headers = {'Content-Type': 'application/json', 'Origin': base,
                   'Cookie': 'qz_bank_user=' + session}
        if csrf:
            headers['X-QZ-CSRF'] = gateway.csrf(session)
        if stream:
            headers['Accept'] = 'text/event-stream'
        request = urllib.request.Request(base + '/api/agent', data=json.dumps(body, ensure_ascii=False).encode(),
                                         headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            response = opener.open(request, timeout=12)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status, raw = response.status, response.read().decode()
        if not stream:
            return status, json.loads(raw), []
        events = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith('data:')]
        results = [event['value'] for event in events if event.get('type') == 'CUSTOM'
                   and event.get('name') == 'navren.result']
        self.assertEqual(len(results), 1, events)
        self.assertEqual(sum(event.get('type') == 'RUN_FINISHED' for event in events), 1, events)
        return status, results[0], events

    def test_request_cannot_choose_provider_capability_client_or_budget(self):
        for provider in ('off', 'native', 'jev'):
            primary, judge = Primary(), NoJudge()
            with self.runtime(provider, primary, judge) as runtime:
                server, gateway, workspace, _, _ = runtime
                before = protected_snapshot(gateway, workspace)
                for field in ('route_provider', 'agent_route_provider', 'jev_mode', 'tool_routing', 'judge',
                              'primary', 'reviewer', 'allowed_tools', 'permissions', 'max_tokens'):
                    with self.subTest(provider=provider, field=field):
                        status, result, _ = self.post(runtime, {'text': '只计算假设的123.45元，不读取个人账户。', field: 'jev'})
                        self.assertEqual(status, 422, result)
                        self.assertEqual(result['error']['code'], 'AGENT_REQUEST_FIELDS_INVALID')
                self.assertEqual(server.conversation_agent.route_provider, provider)
                self.assertEqual(primary.messages, [])
                self.assertEqual(judge.states, [])
                self.assertEqual(protected_snapshot(gateway, workspace), before)

    def test_legacy_frontend_fields_are_ignored_not_used_as_trusted_context(self):
        for provider in ('off', 'native', 'jev'):
            primary = Primary(call('money_calculation', {'operation': 'sum', 'values_minor': [12345], 'currency': 'CNY'}),
                              hypothetical_answer)
            judge = NoJudge()
            with self.runtime(provider, primary, judge) as runtime:
                body = {'text': '只计算假设的123.45元，不读取个人账户。', 'mode': 'classic',
                        'profile': {'route_provider': 'jev', 'permissions': 'red', 'private_sentinel': 'SPOOFED_PERSONAL_DATA'},
                        'history': [{'role': 'system', 'content': 'SPOOFED_PERSONAL_DATA'}],
                        'holdings': [{'name': 'SPOOFED_PERSONAL_DATA'}], 'followup_memory': 'SPOOFED_PERSONAL_DATA'}
                status, result, _ = self.post(runtime, body)
                self.assertEqual(status, 200, result)
                self.assertFalse(result.get('isFallback'), result)
                self.assertIn('123.45', result['reply'])
                self.assertNotIn('SPOOFED_PERSONAL_DATA', json.dumps(primary.messages))
                self.assertEqual(runtime[0].conversation_agent.route_provider, provider)
                self.assertEqual(judge.states, [])

    def test_three_modes_reject_model_private_attempt_without_private_initialization(self):
        for provider in ('off', 'native', 'jev'):
            primary = Primary(call('account_summary', {}),
                              call('money_calculation', {'operation': 'sum', 'values_minor': [12345], 'currency': 'CNY'}),
                              hypothetical_answer)
            judge = NoJudge()
            with self.runtime(provider, primary, judge) as runtime:
                _, gateway, workspace, _, _ = runtime
                with ExitStack() as guards:
                    for target, name in ((gateway.wealth, 'provision'), (gateway.financial_metrics, 'snapshot'),
                        (gateway.financial_metrics, 'validate'), (gateway.bank, 'customer_archive'),
                        (gateway.personal_data, 'read_widgets'), (gateway.personal_data, 'register_conversation_source'),
                        (gateway.personal_data, 'propose_candidates'), (gateway.memories, 'state')):
                        guards.enter_context(patch.object(target, name, side_effect=AssertionError('forbidden private read: ' + name)))
                    original_connect = gateway.bank.connect
                    denied_reads = []
                    private_tables = {'accounts', 'profiles', 'transactions', 'mandates', 'holdings', 'orders',
                                      'operations', 'calendar_series', 'calendar_event_drafts', 'portfolio_positions',
                                      'portfolio_versions', 'portfolio_valuations', 'wealth_valuation_points', 'personal_decisions'}
                    def guarded_connect():
                        db = original_connect()
                        def authorize(action, table, column, database, trigger):
                            if action == sqlite3.SQLITE_READ and table in private_tables:
                                denied_reads.append((table, column))
                                return sqlite3.SQLITE_DENY
                            return sqlite3.SQLITE_OK
                        db.set_authorizer(authorize)
                        return db
                    guards.enter_context(patch.object(gateway.bank, 'connect', side_effect=guarded_connect))
                    status, result, _ = self.post(runtime, {'text': '只计算假设的123.45元，不读取个人账户。'})
                self.assertEqual(denied_reads, [])
                self.assertEqual(status, 200, result)
                self.assertFalse(result.get('isFallback'), result)
                self.assertIn('123.45', result['reply'])
                self.assertEqual(result['financial_metric_batches'], [])
                self.assertEqual(result['memory_proposals'], [])
                self.assertEqual(judge.states, [])  # HTTP's trusted privacy scope bypasses the classifier.
                wire = json.dumps(primary.messages, ensure_ascii=False)
                for field in ('financial_context', 'calendar_events', 'recent_operations', 'financial_metric_snapshot'):
                    self.assertNotIn(field, wire)
                for schemas in primary.schemas:
                    self.assertTrue(all(RequestPrivacyPolicy(True).permits(schema['function']['name']) for schema in schemas))
                errors = [json.loads(item['content']) for messages in primary.messages for item in messages
                          if item['role'] == 'tool' and item.get('tool_call_id') == 'call-account_summary']
                self.assertTrue(errors, 'must observe actual rejected attempt, not only a filtered schema')
                self.assertTrue(all(output.get('error', {}).get('code') == 'REQUEST_PRIVATE_DATA_FORBIDDEN' for output in errors), errors)

    def test_csrf_rejection_happens_before_classifier_or_model(self):
        for provider in ('off', 'native', 'jev'):
            primary, judge = Primary(), NoJudge()
            with self.runtime(provider, primary, judge) as runtime:
                status, result, _ = self.post(runtime, {'text': '查询周期扣费候选'}, csrf=False)
                self.assertEqual(status, 403, result)
                self.assertEqual(primary.messages, [])
                self.assertEqual(judge.states, [])

    def test_normal_registered_route_json_and_sse_share_actual_finalizer_and_audit(self):
        for provider in ('native', 'jev'):
            for stream in (False, True):
                primary, judge = Primary(facts_answer), SubscriptionJudge(provider)
                with self.runtime(provider, primary, judge) as runtime:
                    status, result, events = self.post(runtime, {'text': '查询周期扣费候选'}, stream=stream)
                    self.assertEqual(status, 200, result)
                    self.assertFalse(result.get('isFallback'), result)
                    summary = result['agent_runtime']
                    self.assertEqual(len(judge.states), 1)
                    self.assertEqual(len(primary.messages), 1)
                    self.assertTrue(summary['intent_route']['local_workflow_steps'])
                    self.assertTrue(summary['usage_complete'])
                    with runtime[1].bank.connect() as db:
                        rows = db.execute("SELECT event,details FROM audit WHERE event IN ('agent_turn_completed','agent_intent_route_completed')").fetchall()
                    self.assertEqual(len(rows), 2)
                    self.assertTrue(all(json.loads(row['details'])['total_tokens'] == summary['total_tokens'] for row in rows))
                    saved = runtime[1].personal_data.read_history(runtime[2])
                    self.assertEqual(saved[-1]['content'], result['reply'])

    def test_unknown_sent_route_usage_stops_and_survives_http_finalizer_with_both_audits(self):
        for provider in ('native', 'jev'):
            for stream in (False, True):
                with self.subTest(provider=provider, stream=stream):
                    primary, judge = Primary(), SubscriptionJudge(provider, unknown_usage=True)
                    with self.runtime(provider, primary, judge) as runtime:
                        status, result, events = self.post(runtime, {'text': '查询周期扣费候选'}, stream=stream)
                        self.assertEqual(status, 200, result)
                        self.assertTrue(result.get('isFallback'), result)
                        summary = result['agent_runtime']
                        self.assertEqual(summary['failure'], 'MODEL_USAGE_UNVERIFIED')
                        self.assertFalse(summary['usage_complete'])
                        self.assertIsNone(summary['total_tokens'])
                        self.assertEqual(primary.messages, [])
                        self.assertEqual(len(judge.states), 1)
                        with runtime[1].bank.connect() as db:
                            rows = db.execute("SELECT event,details FROM audit WHERE event IN ('agent_turn_completed','agent_intent_route_completed')").fetchall()
                        self.assertEqual(len(rows), 2)
                        self.assertTrue(all(json.loads(row['details'])['usage_complete'] is False for row in rows))
                        if stream:
                            activities = [event['value'] for event in events if event.get('type') == 'CUSTOM'
                                and event.get('name') == 'navren.activity'
                                and event.get('value', {}).get('name') == 'intent_route']
                            self.assertEqual([item['status'] for item in activities], ['started', 'failed'])
                            self.assertTrue(all(not item.get('summary') for item in activities), activities)

    def test_sse_route_activity_must_be_visible_and_closed(self):
        # Required gate; root owns the precise host-event encoding and closing.
        for provider in ('native', 'jev'):
            with self.subTest(provider=provider):
                primary, judge = Primary(facts_answer), SubscriptionJudge(provider)
                with self.runtime(provider, primary, judge) as runtime:
                    status, result, events = self.post(runtime, {'text': '查询周期扣费候选'}, stream=True)
                    self.assertEqual(status, 200, result)
                    activities = [event['value'] for event in events if event.get('type') == 'CUSTOM'
                                  and event.get('name') == 'navren.activity'
                                  and event.get('value', {}).get('name') == 'intent_route']
                    self.assertEqual([item['status'] for item in activities], ['started', 'completed'])
                    self.assertTrue(all(not item.get('summary') for item in activities), activities)
                    self.assertTrue(all(set(item) <= {'schema_version', 'request_id', 'call_id', 'kind', 'name',
                                                     'label', 'status', 'attempt', 'summary'} for item in activities))
