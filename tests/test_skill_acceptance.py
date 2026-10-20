"""冻结题集采集器的离线失败、追踪和隔离合同；不调用云模型。"""
from contextlib import ExitStack, closing
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_runtime.conversation import ConversationAgent
from finance_gateway import FinanceGateway
from scripts import run_skill_acceptance as acceptance


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def native_call(name, arguments, call_id='offline-call'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': call_id, 'type': 'function',
        'function': {'name': name, 'arguments': arguments},
    }]}


class NativeResponse:
    status = 200

    def __init__(self, message):
        self.data = json.dumps({
            'model': 'offline-native-fixture', 'id': 'offline-response',
            'choices': [{'message': message, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 0, 'total_tokens': 1},
        }, ensure_ascii=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.data[:limit]


class NeverReviewer:
    model_id = 'offline-never-reviewer'
    billing_mode = 'unpaid_fixture'

    def review(self, *args):
        raise AssertionError('只读事实纠正路径不应调用审核模型')


class TraceSafetyTest(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB': '',
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_native_invalid_arguments_reach_the_original_correction_loop(self):
        for arguments in ('{broken', '{"amount_minor": NaN}'):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temp:
                gateway = FinanceGateway(Path(temp)/'bank.sqlite3')
                workspace, _ = gateway.create_session()
                cash_before = gateway.overview(workspace)['total_cash_minor']
                client = acceptance.TracePrimary(Path(temp)/'calls.jsonl')
                client.billing_mode = 'unpaid_fixture'
                client.key_loader = lambda: 'offline-placeholder'
                requests = []

                def respond(request, **kwargs):
                    payload = json.loads(request.data)
                    requests.append(payload)
                    if len(requests) == 1:
                        message = native_call('money_calculation', arguments, 'invalid-arguments')
                    elif len(requests) == 2:
                        message = native_call('money_calculation', json.dumps({
                            'operation': 'sum', 'values_minor': [12345], 'currency': 'CNY',
                        }), 'hypothetical-calculation')
                    else:
                        tool = next(json.loads(item['content']) for item in reversed(payload['messages'])
                                    if item['role'] == 'tool')
                        fact_ids = [fact['id'] for fact in tool['facts']
                                    if fact.get('unit') == 'minor_currency'
                                    and fact.get('currency') == 'CNY' and fact.get('value') == 12345]
                        self.assertTrue(fact_ids)
                        message = native_call('answer_facts', json.dumps({'fact_ids': fact_ids}), 'finish')
                    return NativeResponse(message)

                client.urlopen = respond
                result = ConversationAgent(client, NeverReviewer()).process(
                    gateway, workspace, '只计算假设的123.45元，不读取个人账户。')
                self.assertFalse(result.get('isFallback'))
                self.assertEqual(result['answer_contract']['kind'], 'verified_facts')
                self.assertEqual(result['agent_review']['status'], 'not_required')
                self.assertIn('INVALID_TOOL_ARGUMENTS', json.dumps(requests[1]['messages']))
                self.assertEqual(client.tool_outputs[0]['output']['error']['code'], 'INVALID_TOOL_ARGUMENTS')
                self.assertEqual(gateway.overview(workspace)['total_cash_minor'], cash_before)
                json.dumps(client.calls, allow_nan=False)
                json.dumps(client.tool_outputs, allow_nan=False)

    def test_failed_argument_parse_records_safe_metadata_without_changing_native_message(self):
        for arguments in ('{"private":"do-not-log-this",', '{"amount_minor": NaN}',
                          '{"amount_minor": Infinity}', '{"amount_minor": 1, "amount_minor": 2}'):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temp:
                client = acceptance.TracePrimary(Path(temp)/'calls.jsonl')
                client.key_loader = lambda: 'offline-placeholder'
                message = native_call('money_calculation', arguments)
                message['reasoning_content'] = 'do-not-log-hidden-reasoning'
                client.urlopen = lambda *args, **kwargs: NativeResponse(message)
                actual, _ = client.complete([{'role': 'user', 'content': '离线'}], [],
                                           request_id='offline-trace', remaining_seconds=3)
                self.assertEqual(actual['tool_calls'][0]['function']['arguments'], arguments)
                self.assertEqual(actual['reasoning_content'], message['reasoning_content'])
                self.assertEqual(client.calls[0]['id'], 'offline-call')
                self.assertEqual(client.calls[0]['name'], 'money_calculation')
                self.assertEqual(client.calls[0]['arguments'], {
                    'unparsed': True, 'length': len(arguments), 'sha256': sha256(arguments.encode()),
                })
                self.assertEqual(client.calls[0]['arguments_parse_error'], 'invalid_trace_json')
                captured = json.dumps(client.calls, allow_nan=False)
                self.assertNotIn('do-not-log-this', captured)
                self.assertNotIn('do-not-log-hidden-reasoning', captured)

    def test_normal_arguments_remain_structured_in_the_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            client = acceptance.TracePrimary(Path(temp)/'calls.jsonl')
            client.key_loader = lambda: 'offline-placeholder'
            client.urlopen = lambda *args, **kwargs: NativeResponse(
                native_call('knowledge_search', '{"query":"公开资料","limit":2}'))
            client.complete([{'role': 'user', 'content': '离线'}], [],
                            request_id='offline-normal', remaining_seconds=3)
            self.assertEqual(client.calls[0]['arguments'], {'query': '公开资料', 'limit': 2})

    def test_native_plain_text_with_null_tool_calls_is_returned_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            client = acceptance.TracePrimary(Path(temp)/'calls.jsonl')
            client.key_loader = lambda: 'offline-placeholder'
            original = {'role': 'assistant', 'content': '继续补充信息。', 'tool_calls': None}
            client.urlopen = lambda *args, **kwargs: NativeResponse(original)
            actual, _ = client.complete([{'role': 'user', 'content': '离线'}], [],
                                       request_id='offline-null-tools', remaining_seconds=3)
            self.assertEqual(actual, original)
            self.assertEqual(client.calls, [])


class DummyTrace:
    def __init__(self, *args, **kwargs):
        self.calls, self.tool_outputs, self.records, self.response_metadata = [], [], [], []


class OfflineHarness:
    """仅替代 IO 与服务生命周期，保留 main 的选择、哈希、落盘与失败逻辑。"""
    def __init__(self, root):
        self.root = root
        self.dataset_path = root/'evaluation/frozen-cases.json'
        self.out = root/'results'
        guard = root/'guard.py'
        guard.write_bytes(b'# frozen production source\n')
        self.dataset = {
            'dataset_id': 'offline-frozen-cases', 'version': '1.0.0',
            'source_sha256': {'guard.py': sha256(guard.read_bytes())},
            'fixture_policy': {'fresh_database_per_case': True, 'fresh_workspace_per_case': True},
            'fixtures': {'default_temp': {'type': 'default'}},
            'cases': [{
                'id': case_id, 'prompt': '离线问题'+case_id,
                'fixture': 'default_temp', 'fixture_type': 'default',
                'expected': {'facts': [{'label': '冻结预期', 'expected': number}]},
            } for number, case_id in enumerate(('CASE-A', 'CASE-B', 'CASE-C'), 1)],
        }
        self.write_dataset()
        self.servers, self.database_paths, self.openers, self.cookie_jars = [], [], [], []
        self.sessions, self.configurations, self.oracle_cases, self.threads = [], [], [], []

    def write_dataset(self):
        self.dataset_path.parent.mkdir(parents=True, exist_ok=True)
        # 非标准空白是有意的，哈希应取读入字节，而非 JSON 重序列化结果。
        self.dataset_bytes = ('\n  '+json.dumps(self.dataset, ensure_ascii=False, indent=1)+'\n\n').encode()
        self.dataset_path.write_bytes(self.dataset_bytes)

    def create_server(self, *args, **kwargs):
        database = Path(kwargs['database'])
        self.database_paths.append(database)
        self.assert_temporary_database(database)
        database.write_bytes(b'offline bank placeholder')
        server = SimpleNamespace(
            server_port=19000+len(self.servers),
            finance=SimpleNamespace(public_products=object(), knowledge_provider=None),
            serve_forever=MagicMock(), shutdown=MagicMock(), server_close=MagicMock(),
        )
        self.servers.append(server)
        return server

    def assert_temporary_database(self, database):
        if not database.is_absolute() or 'navren-skill-acceptance-' not in str(database):
            raise AssertionError('必须向 create_server 显式传入独立 TemporaryDirectory 的绝对数据库路径')

    def thread(self, **kwargs):
        thread = SimpleNamespace(start=MagicMock(), join=MagicMock())
        self.threads.append(thread)
        return thread

    def opener(self, *handlers):
        cookie_jar = next(handler.cookiejar for handler in handlers
                          if isinstance(handler, acceptance.HTTPCookieProcessor))
        opener = SimpleNamespace(cookie_jar=cookie_jar)
        self.cookie_jars.append(cookie_jar)
        self.openers.append(opener)
        return opener

    def request(self, opener, base, path, payload, csrf=None):
        if path != '/api/finance/session':
            raise AssertionError('offline 不允许发起 Agent/云请求')
        session = {'workspace_id': 'workspace-'+str(len(self.sessions)), 'csrf': 'offline-csrf'}
        self.sessions.append((opener, base, deepcopy(session)))
        return 200, session

    def configure(self, gateway, workspace, fixture, original_catalog):
        self.configurations.append((gateway, workspace, deepcopy(fixture)))

    def oracle(self, gateway, workspace, case):
        self.oracle_cases.append(deepcopy(case))
        return [dict(deepcopy(fact), actual=fact['expected'], passed=True) for fact in case['expected']['facts']]

    def snapshot(self, *args):
        return {'total_cash_minor': 12345, 'profile': {'risk_level': 1}, 'orders': [],
                'holdings': [], 'fund_holds': [], 'memories': [], 'observations': 0}

    def run(self, *extra, configure=None, oracle=None, request=None, offline=True,
            import_error=None, knowledge_db=''):
        argv = ['run_skill_acceptance.py', '--dataset', str(self.dataset_path),
                '--out-dir', str(self.out), *(['--offline'] if offline else []), *extra]
        with ExitStack() as stack:
            for name, value in (
                ('ROOT', self.root), ('create_server', self.create_server),
                ('configure', configure or self.configure), ('oracle', oracle or self.oracle),
                ('snapshot', self.snapshot), ('request', request or self.request),
                ('build_opener', self.opener), ('TracePrimary', DummyTrace), ('TraceReviewer', DummyTrace),
                ('_PRODUCTION_IMPORT_ERROR', import_error),
            ):
                stack.enter_context(patch.object(acceptance, name, value))
            stack.enter_context(patch.dict(os.environ, {'NAVREN_KNOWLEDGE_DB': knowledge_db}))
            stack.enter_context(patch.object(acceptance.threading, 'Thread', self.thread))
            stack.enter_context(patch.object(acceptance, 'ConversationAgent', MagicMock()))
            stack.enter_context(patch.object(sys, 'argv', argv))
            stack.enter_context(patch('sys.stdout', io.StringIO()))
            stack.enter_context(patch('sys.stderr', io.StringIO()))
            result = acceptance.main()
        report = json.loads((self.out/'semantic-results.json').read_bytes())
        return result, report


class RunnerLifecycleTest(unittest.TestCase):
    def test_startup_import_failure_is_saved_without_constructing_any_server(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = OfflineHarness(Path(temp))
            result, report = harness.run(import_error=SyntaxError('do-not-log-source-payload'))
            self.assertEqual(result, 1)
            self.assertFalse(report['collection_complete'])
            self.assertEqual(report['failure_stage'], 'startup')
            self.assertEqual(report['stop_reason'], 'startup_import_failed')
            self.assertEqual(report['runner_failure']['exception_type'], 'SyntaxError')
            self.assertTrue(report['final_source_sha256'])
            self.assertEqual(report['final_dataset_sha256'], sha256(harness.dataset_bytes))
            self.assertEqual(report['tested_source_root'], str(harness.root.resolve()))
            self.assertNotIn('do-not-log-source-payload', json.dumps(report))
            self.assertFalse(harness.servers)

    def test_knowledge_environment_override_is_refused_before_any_storage_is_constructed(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = OfflineHarness(Path(temp))
            outside = Path(temp)/'private-rag.sqlite3'
            result, report = harness.run(knowledge_db=str(outside))
            self.assertEqual(result, 1)
            self.assertFalse(report['collection_complete'])
            self.assertEqual(report['failure_stage'], 'environment')
            self.assertEqual(report['runner_failure']['exception_type'], 'ValueError')
            self.assertFalse(harness.servers)
            self.assertFalse(outside.exists())

    def test_selected_case_ids_preserve_frozen_order_expectations_and_oracles(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = OfflineHarness(Path(temp))
            before = harness.dataset_path.read_bytes()
            result, report = harness.run('--case-ids', 'CASE-C,CASE-A')
            self.assertEqual(result, 0)
            self.assertTrue(report['collection_complete'])
            self.assertEqual(report['expected_case_count'], 2)
            self.assertEqual([row['id'] for row in report['records']], ['CASE-A', 'CASE-C'])
            self.assertEqual(harness.oracle_cases, [harness.dataset['cases'][0], harness.dataset['cases'][2]])
            self.assertEqual([row['oracle'][0]['expected'] for row in report['records']], [1, 3])
            self.assertEqual(harness.dataset_path.read_bytes(), before)
            self.assertEqual(report['dataset_sha256'], sha256(before))
            self.assertEqual(report['semantic_grading'], 'pending_independent_annotation')
            self.assertTrue(report['source_unchanged'])
            self.assertTrue(report['dataset_unchanged'])
            self.assertEqual(report['tested_source_root'], str(harness.root.resolve()))
            self.assertEqual(report['selected_case_ids'], ['CASE-A', 'CASE-C'])

    def test_legacy_cases_option_remains_compatible(self):
        with tempfile.TemporaryDirectory() as temp:
            result, report = OfflineHarness(Path(temp)).run('--cases', 'CASE-B')
            self.assertEqual(result, 0)
            self.assertEqual([row['id'] for row in report['records']], ['CASE-B'])

    def test_unknown_duplicate_and_conflicting_selection_is_rejected_before_server(self):
        for extra in (('--case-ids', 'MISSING'), ('--case-ids', 'CASE-A,CASE-A'),
                      ('--case-ids', 'CASE-A', '--cases', 'CASE-B')):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temp:
                harness = OfflineHarness(Path(temp))
                try:
                    result, report = harness.run(*extra)
                except (ValueError, SystemExit):
                    pass
                else:
                    self.assertEqual(result, 1)
                    self.assertFalse(report['collection_complete'])
                    self.assertTrue(report.get('stop_reason'))
                self.assertFalse(harness.servers)
                self.assertEqual(harness.dataset_path.read_bytes(), harness.dataset_bytes)

    def test_each_case_uses_fresh_cookies_workspace_and_explicit_temporary_database(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = OfflineHarness(Path(temp))
            result, report = harness.run()
            self.assertEqual(result, 0)
            self.assertEqual(len(report['records']), 3)
            self.assertEqual(len(harness.servers), 3)
            self.assertEqual(len(set(harness.database_paths)), 3)
            self.assertEqual(len({id(jar) for jar in harness.cookie_jars}), 3)
            self.assertEqual(len({id(opener) for opener in harness.openers}), 3)
            workspaces = [configuration[1] for configuration in harness.configurations]
            self.assertEqual(len(set(workspaces)), 3)
            self.assertEqual(workspaces, [session[2]['workspace_id'] for session in harness.sessions])
            for server, thread, database in zip(harness.servers, harness.threads, harness.database_paths):
                server.shutdown.assert_called_once()
                server.server_close.assert_called_once()
                thread.join.assert_called_once()
                self.assertFalse(database.exists())

    def test_configuration_and_oracle_exceptions_are_finalized_on_first_and_later_case(self):
        for stage in ('configure', 'oracle'):
            for failed_index in (0, 1):
                with self.subTest(stage=stage, failed_index=failed_index), tempfile.TemporaryDirectory() as temp:
                    harness = OfflineHarness(Path(temp))
                    calls = [0]
                    original = getattr(harness, stage)

                    def fail_at_case(*args):
                        current = calls[0]
                        calls[0] += 1
                        if current == failed_index:
                            raise ValueError('offline-fixture-failure')
                        return original(*args)

                    result, report = harness.run(**{stage: fail_at_case})
                    self.assertEqual(result, 1)
                    self.assertFalse(report['collection_complete'])
                    self.assertEqual(report['expected_case_count'], 3)
                    self.assertEqual(len(report['records']), failed_index+1)
                    failed = report['records'][-1]
                    self.assertEqual(failed['id'], harness.dataset['cases'][failed_index]['id'])
                    self.assertEqual(failed['failure_stage'], stage)
                    self.assertTrue(failed['runner_failure'])
                    self.assertTrue(report['stop_reason'])
                    self.assertIn('completed_at', report)
                    self.assertTrue(report['source_unchanged'])
                    self.assertTrue(report['dataset_unchanged'])
                    self.assertTrue(report['final_source_sha256'])
                    self.assertEqual(report['final_dataset_sha256'], sha256(harness.dataset_bytes))
                    for server, thread in zip(harness.servers, harness.threads):
                        server.shutdown.assert_called_once()
                        server.server_close.assert_called_once()
                        thread.join.assert_called_once()

    def test_dataset_hash_is_from_initial_bytes_and_detects_change_during_run(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = OfflineHarness(Path(temp))
            original_hash = sha256(harness.dataset_bytes)

            def change_dataset(*args):
                harness.configure(*args)
                harness.dataset_path.write_bytes(harness.dataset_bytes+b' ')

            result, report = harness.run('--case-ids', 'CASE-A', configure=change_dataset)
            self.assertEqual(result, 1)
            self.assertEqual(report['dataset_sha256'], original_hash)
            self.assertFalse(report['dataset_unchanged'])
            self.assertEqual(report['final_dataset_sha256'], sha256(harness.dataset_bytes+b' '))

    def test_source_change_during_collection_is_detected_with_final_source_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = OfflineHarness(Path(temp))
            changed = b'# changed while running\n'

            def change_source(*args):
                harness.configure(*args)
                (harness.root/'guard.py').write_bytes(changed)

            result, report = harness.run('--case-ids', 'CASE-A', configure=change_source)
            self.assertEqual(result, 1)
            self.assertFalse(report['source_unchanged'])
            self.assertEqual(report['source_sha256']['guard.py'],
                             harness.dataset['source_sha256']['guard.py'])
            self.assertEqual(report['final_source_sha256']['guard.py'], sha256(changed))

    def test_http_fallback_is_collected_but_is_not_a_technical_or_semantic_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = OfflineHarness(Path(temp))
            payloads = []

            def fixture_request(opener, base, path, payload, csrf=None):
                if path == '/api/finance/session':
                    return harness.request(opener, base, path, payload, csrf)
                self.assertEqual(path, '/api/agent')
                payloads.append(deepcopy(payload))
                return 200, {'reply': '未完成分析。', 'isFallback': True, 'bank_actions': [],
                             'agent_runtime': {'failure': 'AGENT_BUDGET_EXCEEDED'}}

            result, report = harness.run('--case-ids', 'CASE-A', request=fixture_request, offline=False)
            self.assertEqual(result, 0)
            self.assertTrue(report['collection_complete'])
            row = report['records'][0]
            self.assertFalse(row['technical_completed'])
            self.assertFalse(row.get('runner_failure'))
            self.assertEqual(report['semantic_grading'], 'pending_independent_annotation')
            self.assertEqual(payloads, [{'text': harness.dataset['cases'][0]['prompt'], 'mode': 'classic'}])

    def test_agent_transport_exception_is_persisted_and_closes_the_server(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = OfflineHarness(Path(temp))

            def broken_transport(opener, base, path, payload, csrf=None):
                if path == '/api/finance/session':
                    return harness.request(opener, base, path, payload, csrf)
                self.assertEqual(path, '/api/agent')
                raise TimeoutError('do-not-log-transport-private-payload')

            result, report = harness.run('--case-ids', 'CASE-A', request=broken_transport, offline=False)
            self.assertEqual(result, 1)
            self.assertFalse(report['collection_complete'])
            self.assertEqual(len(report['records']), 1)
            failed = report['records'][0]
            self.assertEqual(failed['failure_stage'], 'agent_http')
            self.assertEqual(failed['runner_failure']['exception_type'], 'TimeoutError')
            self.assertFalse(failed['technical_completed'])
            self.assertEqual(report['stop_reason'], 'runner_exception')
            self.assertTrue(report['final_source_sha256'])
            self.assertEqual(report['final_dataset_sha256'], sha256(harness.dataset_bytes))
            self.assertNotIn('do-not-log-transport-private-payload', json.dumps(report))
            harness.servers[0].shutdown.assert_called_once()
            harness.servers[0].server_close.assert_called_once()
            harness.threads[0].join.assert_called_once()

    def test_browser_hold_uses_a_fourth_independent_default_database(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = OfflineHarness(Path(temp))
            harness.out.mkdir()
            (harness.out/'browser-done.json').write_text('{}')

            def collected_request(opener, base, path, payload, csrf=None):
                if path == '/api/finance/session':
                    return harness.request(opener, base, path, payload, csrf)
                self.assertEqual(path, '/api/agent')
                return 200, {'reply': '离线采集答复。', 'bank_actions': []}

            result, report = harness.run('--browser-hold-seconds', '1', request=collected_request, offline=False)
            self.assertEqual(result, 0)
            self.assertTrue(report['collection_complete'])
            self.assertEqual(len(harness.servers), 4)
            self.assertEqual(len(set(harness.database_paths)), 4)
            self.assertEqual(len(harness.configurations), 3)
            self.assertEqual(len(harness.sessions), 3)
            browser_server = harness.servers[3]
            self.assertIsNone(browser_server.finance.knowledge_provider)
            self.assertNotIn(browser_server.finance, [entry[0] for entry in harness.configurations])
            browser = json.loads((harness.out/'browser-ready.json').read_bytes())
            self.assertEqual(browser['environment'], 'separate_default_temporary_synthetic_browser_bank')
            self.assertEqual(browser['url'], 'http://127.0.0.1:'+str(browser_server.server_port)+'/chat.html')
            for server, thread, database in zip(harness.servers, harness.threads, harness.database_paths):
                server.shutdown.assert_called_once()
                server.server_close.assert_called_once()
                thread.join.assert_called_once()
                self.assertFalse(database.exists())

    def test_author_baseline_differences_are_recorded_without_rewriting_frozen_expectations(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = OfflineHarness(Path(temp))
            (harness.root/'guard.py').write_bytes(b'# changed after expectations froze\n')
            result, report = harness.run('--case-ids', 'CASE-A')
            self.assertEqual(result, 0)
            self.assertTrue(report['collection_complete'])
            self.assertTrue(report['source_unchanged'])
            self.assertTrue(report['author_baseline_differences'])
            self.assertIn('guard.py', json.dumps(report['author_baseline_differences']))
            self.assertEqual(harness.oracle_cases, [harness.dataset['cases'][0]])
            self.assertEqual(harness.dataset_path.read_bytes(), harness.dataset_bytes)

    def test_existing_semantic_results_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            harness = OfflineHarness(Path(temp))
            harness.out.mkdir()
            saved = '{"existing":"不可覆盖的旧结果"}\n'.encode()
            result_path = harness.out/'semantic-results.json'
            result_path.write_bytes(saved)
            with self.assertRaisesRegex(ValueError, 'refuse_overwrite_run'):
                harness.run()
            self.assertEqual(result_path.read_bytes(), saved)
            self.assertFalse(harness.servers)


class SourceManifestTest(unittest.TestCase):
    def test_manifest_covers_production_fixtures_active_skills_runner_test_and_dataset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            required = {
                'server.py', 'finance_gateway.py', 'cashflow_timeline.py', 'personal_graph.py',
                'agent_runtime/conversation.py', 'agent_runtime/nested/contract.py',
                'simulated_bank/backend.py', 'simulated_bank/customer_seed.py',
                'simulated_bank/fixtures/demo.json', 'simulated_bank/fixtures/nested/scenario.json',
                'skills/financial-diagnosis/SKILL.md',
                'skills/financial-diagnosis/references/intake-check.md', 'skills/reviewer/SKILL.md',
                'scripts/run_skill_acceptance.py', 'tests/test_skill_acceptance.py',
                'evaluation/frozen-cases.json',
                'chat.html', 'js/chat.js', 'css/style.css', 'assets/icon.svg',
                'knowledge/financial-concepts.json', 'local_knowledge/schemas/evidence.json',
                'local_knowledge/schemas/schema.sql', 'local_knowledge/supplement.py',
            }
            excluded = {'skills/upstream/SKILL.md', 'skills/upstream/references/copied.md',
                        'scripts/unrelated.py', 'tests/unrelated.py',
                        'nested/outputs/stale.py', 'nested/upstream/copied.py'}
            for relative in required|excluded:
                file = root/relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(relative.encode())
            with patch.object(acceptance, 'ROOT', root):
                manifest = acceptance.source_manifest(root/'evaluation/frozen-cases.json')
            self.assertTrue(required <= set(manifest))
            self.assertFalse(excluded & set(manifest))
            for relative in required:
                self.assertEqual(manifest[relative], sha256((root/relative).read_bytes()))


class ProcessIntegrationTest(unittest.TestCase):
    def candidate(self, root, *, omit_concepts=False):
        dataset = ROOT/'evaluation/navren_skill_semantic_cases.json'
        for relative in acceptance.source_manifest(dataset):
            source = ROOT/relative
            target = root/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        if omit_concepts:
            (root/'knowledge/financial-concepts.json').unlink()
        return root/'evaluation/navren_skill_semantic_cases.json'

    def run_preflight(self, root):
        output = root/'outputs/integration-proof'
        environment = dict(os.environ)
        environment.pop('NAVREN_KNOWLEDGE_DB', None)
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        # 每次全新进程/模块；假模型之外的模型客户端一旦实例化即失败。
        script = "from scripts import run_skill_acceptance as a; " \
                 "a.TracePrimary=lambda *args,**kw: (_ for _ in ()).throw(AssertionError('cloud_primary_forbidden')); " \
                 "a.TraceReviewer=lambda *args,**kw: (_ for _ in ()).throw(AssertionError('cloud_reviewer_forbidden')); " \
                 "raise SystemExit(a.main())"
        process = subprocess.run([sys.executable, '-c', script, '--process-preflight', '--case-ids',
                                  'S04_short_horizon_no_fabricated_allocation', '--out-dir', str(output)],
                                 cwd=root, env=environment, capture_output=True, text=True, timeout=25)
        return process, json.loads((output/'semantic-results.json').read_text())

    def test_real_http_full_process_uses_only_fake_models_and_preserves_funds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset = self.candidate(root)
            before = dataset.read_bytes()
            process, report = self.run_preflight(root)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(report['planned_exit_code'], 0)
            self.assertTrue(report['collection_complete'])
            self.assertTrue(report['source_unchanged'])
            self.assertTrue(report['dataset_unchanged'])
            self.assertTrue(report['process_preflight'])
            self.assertEqual(report['semantic_grading'], 'not_evaluated_in_scripted_process_preflight')
            row = report['records'][0]
            self.assertEqual(row['http_status'], 200)
            self.assertTrue(row['process_preflight_completed'])
            self.assertTrue(set(acceptance.PREFLIGHT_TOOLS) <= set(row['completed_preflight_tools']))
            self.assertTrue(row['no_financial_execution'])
            self.assertTrue(row['profile_unchanged'])
            self.assertEqual(row['observation_delta'], 0)
            self.assertEqual(row['memory_delta'], 0)
            self.assertEqual(len(row['independent_reviews']), 1)
            self.assertTrue(row['result']['agent_runtime']['primary_model'].startswith('offline-'))
            self.assertTrue(row['result']['agent_review']['model_id'].startswith('offline-'))
            runtime = row['result']['agent_runtime']
            self.assertTrue(runtime['usage_complete'])
            self.assertEqual(runtime['primary_requests_sent'], 0)
            self.assertEqual(runtime['reviewer_requests_sent'], 0)
            self.assertEqual({call['stage'] for call in runtime['model_calls']}, {'primary', 'reviewer'})
            for call in runtime['model_calls']:
                self.assertEqual(call['billing_mode'], 'unpaid_fixture')
                self.assertFalse(call['request_sent'])
                self.assertEqual(call['usage'], {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0})
            self.assertEqual(row['response_metadata'], {'primary': [], 'reviewer': []})
            self.assertEqual(dataset.read_bytes(), before)

    def test_missing_graph_execution_input_fails_with_original_stack_before_cloud(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.candidate(root, omit_concepts=True)
            process, report = self.run_preflight(root)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(report['planned_exit_code'], 1)
            self.assertFalse(report['collection_complete'])
            self.assertEqual(report['stop_reason'], 'process_preflight_failed')
            row = report['records'][0]
            self.assertEqual(row['http_status'], 500)
            self.assertEqual(row['failure_stage'], 'agent_process')
            self.assertEqual(row['runner_failure']['exception_type'], 'FileNotFoundError')
            self.assertIn('financial_graph.py', [frame['file'] for frame in row['runner_failure']['frames']])
            self.assertFalse(row['process_preflight_completed'])

    def test_unavailable_fixture_has_explicit_empty_public_interface(self):
        gateway = SimpleNamespace(knowledge_provider=object(), public_products=object(),
                                  wealth=SimpleNamespace(provision=MagicMock()))
        acceptance.configure(gateway, 'synthetic-workspace', {'type': 'default'}, gateway.public_products)
        self.assertEqual(gateway.knowledge_provider.public_catalog(), {'terms': [], 'identifiers': []})
        self.assertFalse(gateway.knowledge_provider.status()['ready'])
        self.assertEqual(gateway.knowledge_provider.search_public({'scope': 'public'}, None)['citation_ids'], [])
        self.assertIsNone(gateway.knowledge_provider.resolve_public_evidence('missing', 'none', '2026-10-05'))

    def test_public_fixture_catalog_contains_only_declared_synthetic_public_fields(self):
        provider = acceptance.EvalPublicProvider({'canonical_evidence': {
            'title': '合成公开条款', 'citation_id': 'eval-citation', 'product_id': 'eval-product',
            'private_name': 'private-canary', 'account_id': 'private-account-canary'}})
        catalog = provider.public_catalog()
        self.assertEqual(catalog['terms'], ['合成公开条款'])
        self.assertEqual(set(catalog['identifiers']), {'eval-citation', 'eval-product'})
        self.assertNotIn('private-canary', json.dumps(catalog))
        self.assertNotIn('private-account-canary', json.dumps(catalog))


class OracleCreditBindingTest(unittest.TestCase):
    """以真实临时银行验证同账户唯一绑定；扩展案例不改原题金标。"""

    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.dict(os.environ, {
            'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB': '',
            'NAVREN_FINANCIAL_DB': '',
        }))
        for target in ('urllib.request.urlopen', 'socket.create_connection',
                       'subprocess.run', 'subprocess.Popen'):
            self.context.enter_context(patch(target, side_effect=AssertionError('oracle_test_external_io_forbidden')))
        self.root = Path(self.context.enter_context(tempfile.TemporaryDirectory()))
        self.gateway = FinanceGateway(self.root/'bank.sqlite3',
                                      public_catalog=acceptance.EvalCatalog({
                                          'public_catalog_result': {'products': [], 'items': [], 'ready': False},
                                      }))
        self.workspace, _ = self.gateway.create_session()
        acceptance.assert_isolated_storage(self.gateway, self.root)
        accounts = self.gateway.overview(self.workspace)['accounts']
        self.main = next(row['id'] for row in accounts if row['id'].endswith('-main'))
        self.other = next(row['id'] for row in accounts if row['id'].endswith('-savings'))
        self.original_credit = next(row for row in self.gateway.budget(self.workspace, self.main)['commitments']
                                    if row['kind'] == 'credit_card' and row['account_id'] == self.main)
        dataset = json.loads((ROOT/'evaluation/navren_skill_semantic_cases.json').read_text())
        self.original_case = next(row for row in dataset['cases'] if row['id'] == 'S10_linked_reserve_dedup')

    def _clone(self, table, original_id, new_id, **changes):
        if table not in {'cards', 'credit_statements'}:
            raise AssertionError('unexpected_test_table')
        with closing(self.gateway.bank.connect()) as database:
            row = dict(database.execute('SELECT * FROM '+table+' WHERE workspace_id=? AND id=?',
                                        (self.workspace, original_id)).fetchone())
            row.update(id=new_id, **changes)
            database.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES ('+
                             ','.join('?' for _ in row)+')', tuple(row.values()))
            database.commit()

    def _add_credit(self, account, prefix, *, zero_due=False):
        statement_id = self.original_credit['evidence_id']
        with closing(self.gateway.bank.connect()) as database:
            original_card = database.execute(
                'SELECT card_id FROM credit_statements WHERE workspace_id=? AND id=?',
                (self.workspace, statement_id)).fetchone()['card_id']
        card_id, added_statement = prefix+'-card', prefix+'-statement'
        card_changes = {'outstanding_minor': 0} if zero_due else {}
        statement_changes = {'total_due_minor': 0, 'minimum_due_minor': 0, 'paid_minor': 0} if zero_due else {}
        self._clone('cards', original_card, card_id, account_id=account, **card_changes)
        self._clone('credit_statements', statement_id, added_statement, card_id=card_id, **statement_changes)
        return card_id, added_statement

    def _pay_main_credit(self):
        with closing(self.gateway.bank.connect()) as database:
            database.execute("UPDATE credit_statements SET status='paid', paid_minor=total_due_minor "
                             'WHERE workspace_id=? AND id=?',
                             (self.workspace, self.original_credit['evidence_id']))
            database.execute('UPDATE cards SET outstanding_minor=0 WHERE workspace_id=? AND account_id=? AND type=?',
                             (self.workspace, self.main, 'credit'))
            database.commit()

    def test_original_s10_facts_keep_original_amounts(self):
        before = acceptance.snapshot(self.gateway, self.workspace)
        checked = acceptance.oracle(self.gateway, self.workspace, self.original_case)
        self.assertEqual(len(checked), 5)
        self.assertTrue(all(row['passed'] for row in checked))
        self.assertEqual([row['actual'] for row in checked[-2:]], [50000, 977600])
        self.assertEqual(acceptance.snapshot(self.gateway, self.workspace), before)

    def test_other_account_credit_before_or_after_main_does_not_change_binding(self):
        for prefix in ('000-other', 'zzz-other'):
            with self.subTest(order=prefix):
                # 零额有效账单只改变候选顺序，保持原S10全部金额和金标。
                card_id, statement_id = self._add_credit(self.other, prefix, zero_due=True)
                try:
                    self.assertFalse(self.gateway.demo_accounts.is_demo(self.workspace))
                    credits = [row for row in self.gateway.budget(self.workspace, self.main)['commitments']
                               if row['kind'] == 'credit_card']
                    self.assertEqual(len(credits), 2)
                    index = 0 if prefix.startswith('000') else 1
                    self.assertEqual(credits[index]['evidence_id'], statement_id)
                    before = acceptance.snapshot(self.gateway, self.workspace)
                    checked = acceptance.oracle(self.gateway, self.workspace, self.original_case)
                    self.assertEqual(len(checked), 5)
                    self.assertTrue(all(row['passed'] for row in checked))
                    self.assertEqual([row['actual'] for row in checked[-2:]], [50000, 977600])
                    self.assertEqual(acceptance.snapshot(self.gateway, self.workspace), before)
                finally:
                    with closing(self.gateway.bank.connect()) as database:
                        database.execute('DELETE FROM credit_statements WHERE workspace_id=? AND id=?',
                                         (self.workspace, statement_id))
                        database.execute('DELETE FROM cards WHERE workspace_id=? AND id=?',
                                         (self.workspace, card_id))
                        database.commit()

    def test_only_other_account_credit_is_explicitly_refused(self):
        self._add_credit(self.other, '000-other')
        self._pay_main_credit()
        with self.assertRaisesRegex(ValueError, 'ambiguous_or_missing_credit_commitment'):
            acceptance.oracle(self.gateway, self.workspace, self.original_case)

    def test_no_open_credit_commitment_is_explicitly_refused_when_referenced(self):
        self._pay_main_credit()
        self.assertFalse(any(row['kind'] == 'credit_card'
                             for row in self.gateway.budget(self.workspace, self.main)['commitments']))
        with self.assertRaisesRegex(ValueError, 'ambiguous_or_missing_credit_commitment'):
            acceptance.oracle(self.gateway, self.workspace, self.original_case)

    def test_multiple_main_account_credits_are_explicitly_refused(self):
        self._add_credit(self.main, '000-second-main')
        with self.assertRaisesRegex(ValueError, 'ambiguous_or_missing_credit_commitment'):
            acceptance.oracle(self.gateway, self.workspace, self.original_case)

    def test_cash_fact_without_credit_variable_survives_zero_or_multiple_credits(self):
        amount = next(row['balance_minor'] for row in self.gateway.overview(self.workspace)['accounts']
                      if row['id'] == self.main)
        case = {'id': 'regression_no_credit_dependency', 'expected': {'facts': [{
            'label': '当前主账户现金', 'tool': 'account_summary', 'arguments': {},
            'path': 'data.accounts.balance_minor', 'select': {'id_suffix': '-main'}, 'expected': amount,
        }]}}
        self._add_credit(self.main, '000-second-main', zero_due=True)
        checked = acceptance.oracle(self.gateway, self.workspace, case)
        self.assertTrue(checked[0]['passed'])
        with closing(self.gateway.bank.connect()) as database:
            database.execute("UPDATE credit_statements SET status='paid', paid_minor=total_due_minor WHERE workspace_id=?",
                             (self.workspace,))
            database.execute("UPDATE cards SET outstanding_minor=0 WHERE workspace_id=? AND type='credit'",
                             (self.workspace,))
            database.commit()
        checked = acceptance.oracle(self.gateway, self.workspace, case)
        self.assertTrue(checked[0]['passed'])


class StorageIsolationTest(unittest.TestCase):
    def test_bank_personal_and_knowledge_paths_are_all_explicitly_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            gateway = SimpleNamespace(
                bank=SimpleNamespace(database=root/'bank.sqlite3'),
                personal_data=SimpleNamespace(database=root/'personal.sqlite3'),
                knowledge_provider=SimpleNamespace(path=root/'public-knowledge.sqlite3'),
            )
            actual = acceptance.assert_isolated_storage(gateway, root)
            self.assertEqual(actual, {
                'bank': str((root/'bank.sqlite3').resolve()),
                'personal': str((root/'personal.sqlite3').resolve()),
                'knowledge': str((root/'public-knowledge.sqlite3').resolve()),
            })

    def test_any_store_outside_the_temporary_root_is_refused(self):
        for store in ('bank', 'personal', 'knowledge'):
            with self.subTest(store=store), tempfile.TemporaryDirectory() as temp:
                parent = Path(temp)
                root = parent/'isolated'
                root.mkdir()
                paths = {key: root/(key+'.sqlite3') for key in ('bank', 'personal', 'knowledge')}
                paths[store] = parent/'outside.sqlite3'
                gateway = SimpleNamespace(
                    bank=SimpleNamespace(database=paths['bank']),
                    personal_data=SimpleNamespace(database=paths['personal']),
                    knowledge_provider=SimpleNamespace(path=paths['knowledge']),
                )
                with self.assertRaisesRegex(ValueError, 'storage_outside_temporary_root'):
                    acceptance.assert_isolated_storage(gateway, root)
                self.assertFalse(paths[store].exists())

    def test_snapshot_counts_both_stores_and_uses_personal_observations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stores = {}
            for name, count in (('bank', 2), ('personal', 5)):
                path = root/(name+'.sqlite3')
                connection = sqlite3.connect(path)
                try:
                    connection.execute('CREATE TABLE profile_observations (workspace_id TEXT)')
                    connection.executemany('INSERT INTO profile_observations VALUES (?)',
                                           [('this-workspace',)]*count+[('another-workspace',)]*3)
                    connection.commit()
                finally:
                    connection.close()
                stores[name] = SimpleNamespace(database=path, connect=lambda path=path: sqlite3.connect(path))
            overview = {'total_cash_minor': 12345, 'profile': {'risk_level': 1},
                        'orders': [], 'holdings': [], 'fund_holds': []}
            gateway = SimpleNamespace(
                bank=stores['bank'], personal_data=stores['personal'],
                overview=lambda workspace: deepcopy(overview),
                memories=SimpleNamespace(state=lambda workspace: {'items': []}),
            )
            state = acceptance.snapshot(gateway, 'this-workspace')
            self.assertEqual(state['storage_state'], {
                'bank': {'profile_observations': 2}, 'personal': {'profile_observations': 5},
            })
            self.assertEqual(state['observations'], 5)
            self.assertEqual(state['total_cash_minor'], 12345)


if __name__ == '__main__':
    unittest.main()
