"""四个真实接入的合成画像：冻结源分录预期、临时账本及本机HTTP验收。"""
from contextlib import closing
import hashlib
import http.client
import http.server
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet
from agent_runtime.model_adapters import OpenAICompatibleJsonClient
from agent_runtime.native_client import DeepSeekToolClient
from finance_gateway import COOKIE, FinanceGateway
from server import ProxyHandler
from simulated_bank import BankError
import fund_history

ORACLE_PATH = ROOT / 'evaluation/window_integration/four_profile_oracles.json'
ORACLE = json.loads(ORACLE_PATH.read_text())
ORACLE_SHA = hashlib.sha256(ORACLE_PATH.read_bytes()).hexdigest()
SOURCE = ROOT / ORACLE['source_directory']


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


class FourProfileIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (SOURCE / 'financial_data.sqlite3').is_file():
            raise unittest.SkipTest('冻结源包未复制到当前候选outputs/contest-finances；不使用默认随机账户替代')
        for name, expected in ORACLE['source_file_sha256'].items():
            if file_hash(SOURCE / name) != expected:
                raise AssertionError('冻结四画像源文件已漂移：' + name)
        cls.base_temp = tempfile.TemporaryDirectory(prefix='navren-four-profile-integration-')
        cls.addClassCleanup(cls.base_temp.cleanup)
        cls.base = Path(cls.base_temp.name).resolve()
        cls.key = Fernet.generate_key().decode()
        environment = patch.dict(os.environ, {
            'NAVREN_PERSONAL_DATA_KEY': cls.key,
            'NAVREN_KNOWLEDGE_DB': str(cls.base / 'knowledge.sqlite3'),
            'NAVREN_MARKET_DB': str(cls.base / 'market.sqlite3'),
            'NAVREN_FUND_CACHE': str(cls.base / 'funds'),
            'NAVREN_ALLOW_CLOUD_ASR': '0'})
        environment.start(); cls.addClassCleanup(environment.stop)
        original = socket.create_connection
        def only_local(address, *args, **kwargs):
            if address[0] not in ('127.0.0.1', 'localhost', '::1'):
                raise AssertionError('四画像验收禁止真实外网连接')
            return original(address, *args, **kwargs)
        guards = [patch.object(socket, 'create_connection', side_effect=only_local),
                  patch.object(DeepSeekToolClient, 'complete', side_effect=AssertionError('禁止云模型')),
                  patch.object(OpenAICompatibleJsonClient, 'complete_json', side_effect=AssertionError('禁止云审核')),
                  patch.object(fund_history, 'urlopen', side_effect=AssertionError('禁止外部行情')),
                  patch.object(fund_history, 'CACHE', cls.base / 'funds'),
                  patch.object(fund_history.cached, '__defaults__', (cls.base / 'funds',))]
        for guard in guards:
            guard.start(); cls.addClassCleanup(guard.stop)
        cls.template = FinanceGateway(cls.base / 'template.sqlite3')
        cls.template.demo_accounts.source_dir = SOURCE
        cls.initialization = cls.template.demo_accounts.initialize_offline(allow_offline=True)
        if cls.initialization['initialized_count'] != 4:
            raise AssertionError('正式四画像离线初始化未交付4个画像')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-four-profile-case-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        database = self.directory / 'bank.sqlite3'
        with closing(self.template.bank.connect()) as source, closing(sqlite3.connect(database)) as target:
            source.backup(target)
        private = database.with_name(database.stem + '.personal.sqlite3')
        with closing(self.template.personal_data.connect()) as source, closing(sqlite3.connect(private)) as target:
            source.backup(target)
        env = patch.dict(os.environ, {'NAVREN_KNOWLEDGE_DB': str(self.directory / 'knowledge.sqlite3'),
                                     'NAVREN_MARKET_DB': str(self.directory / 'market.sqlite3'),
                                     'NAVREN_FUND_CACHE': str(self.directory / 'funds')})
        env.start(); self.addCleanup(env.stop)
        self.g = FinanceGateway(database)
        self.g.demo_accounts.source_dir = SOURCE
        self.service = self.g.demo_accounts
        self.owners = {uid: self.service.workspace_for(uid) for uid in ORACLE['profiles']}
        self.assertTrue(self.g.bank.database.is_relative_to(self.directory))
        self.assertTrue(self.g.personal_data.database.is_relative_to(self.directory))

    def evidence(self, **values):
        print('WINDOW_INTEGRATION_EVIDENCE ' + json.dumps({
            'test': self.id().split('.')[-1], 'oracle_sha256': ORACLE_SHA,
            'scope': 'four_frozen_synthetic_profiles_temporary_bank', **values}, ensure_ascii=False), flush=True)

    def finances(self):
        with closing(self.g.bank.connect()) as db:
            return {uid: {
                'cash': [(r['id'], r['balance_minor']) for r in db.execute(
                    'SELECT id,balance_minor FROM accounts WHERE workspace_id=? ORDER BY id', (workspace,))],
                'transactions': db.execute('SELECT count(*) FROM transactions WHERE workspace_id=?', (workspace,)).fetchone()[0],
                'orders': db.execute('SELECT count(*) FROM orders WHERE workspace_id=?', (workspace,)).fetchone()[0],
            } for uid, workspace in self.owners.items()}

    def prepare_transfer(self, uid='tycoon'):
        owner = self.owners[uid]
        state = self.g.overview(owner)
        account = max(state['accounts'], key=lambda a: a['balance_minor'])
        return self.g.prepare(owner, {'action': 'transfer', 'account_id': account['id'],
            'beneficiary_id': state['beneficiaries'][0]['id'], 'amount_minor': 10000,
            'idempotency_key': 'four-profile-isolation'})

    def start_http(self):
        class Quiet(ProxyHandler):
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        server.finance, server.app_release = self.g, 'navren-isolated-four-profile'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown(); server.server_close(); thread.join(3)
        self.addCleanup(stop)
        self.server = server

    def request(self, method, path, *, token=None, payload=None, csrf=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers = {'Origin': 'http://127.0.0.1:' + str(self.server.server_port)}
        if token:
            headers['Cookie'] = COOKIE + '=' + token
        if csrf:
            headers['X-QZ-CSRF'] = csrf
        body = None if payload is None else json.dumps(payload).encode()
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            result, cookie = json.loads(response.read()), response.getheader('Set-Cookie')
            new_token = None
            if cookie:
                parsed = SimpleCookie(); parsed.load(cookie); new_token = parsed[COOKIE].value
            return response.status, result, new_token
        finally:
            connection.close()

    def test_terminal_source_journal_replay_matches_imported_four_profiles(self):
        catalog = self.service.catalog()
        self.assertEqual({r['user_id'] for r in catalog['accounts']}, set(ORACLE['profiles']))
        self.assertEqual(len(set(self.owners.values())), 4)
        with closing(sqlite3.connect((SOURCE / 'financial_data.sqlite3').resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            db.row_factory = sqlite3.Row
            for uid, owner in self.owners.items():
                expected = ORACLE['profiles'][uid]
                rows = [dict(r) for r in db.execute(ORACLE['terminal_reconciliation_sql'], (uid, ORACLE['business_as_of']))]
                cash = sum(r['balance_minor'] for r in rows if r['account_class'] == 'cash')
                investment = sum(r['balance_minor'] for r in rows if r['account_class'] == 'investment')
                real_assets = sum(r['balance_minor'] for r in rows if r['account_class'] == 'real_asset')
                liabilities = -sum(r['balance_minor'] for r in rows if r['account_class'] == 'liability')
                replayed = {'cash_minor': cash, 'investment_minor': investment,
                            'real_assets_minor': real_assets, 'liabilities_minor': liabilities,
                            'total_assets_minor': cash + investment + real_assets,
                            'net_worth_minor': cash + investment + real_assets - liabilities}
                self.assertEqual(replayed, expected['terminal_journal_totals'])
                current = self.service.current(owner)
                self.assertEqual(current['source'], 'navren_synthetic')
                self.assertEqual(current['as_of'], ORACLE['business_as_of'])
                self.assertFalse(current['is_real_bank_data'])
                for key, value in expected['terminal_journal_totals'].items():
                    self.assertEqual(current['source_snapshot']['latest_balances'][key], value, (uid, key))
                self.assertEqual(current['bank_state']['cash_minor'], cash)
                self.assertEqual(current['bank_state']['holding_principal_minor'], expected['runtime_holding_cost_minor'])
                self.assertEqual(current['bank_state']['liabilities_minor'], expected['terminal_journal_totals']['liabilities_minor'])
                self.assertTrue(all(h['quantity'] is None and not h['execution_enabled'] for h in current['bank_state']['holdings']))
                accounts = {a['source_account_id']: a['balance_minor'] for a in current['bank_state']['accounts']}
                self.assertEqual(accounts, {a['account_id']: a['balance_minor'] for a in expected['cash_accounts']})
        self.evidence(profile_ids=list(self.owners), total_profiles=4, history_replayed=False)

    def test_same_sixty_day_question_has_independently_frozen_differences(self):
        before = self.finances()
        answers = {}
        for uid, owner in self.owners.items():
            expected = ORACLE['profiles'][uid]
            budget = self.g.budget(owner)
            flow = self.g.cashflow_timeline(owner, 60)
            self.assertEqual(budget['cash_minor'], expected['terminal_journal_totals']['cash_minor'])
            self.assertEqual(budget['reserve_minor'], expected['reserve_minor'])
            self.assertEqual(budget['investable_minor'], expected['initial_recorded_only_budget_minor'])
            self.assertEqual((budget['upcoming_due_minor'], budget['held_minor']), (0, 0))
            for key in ('confirmed_inflow_minor', 'confirmed_outflow_minor'):
                self.assertEqual(flow['totals'][key], 0)
            self.assertEqual(flow['totals']['candidate_outflow_minor'], expected['candidate_outflow_60d_minor'])
            self.assertEqual(flow['horizon_end'], ORACLE['horizon_end'])
            self.assertEqual(sum(a['minimum_available_after_confirmed_minor'] for a in flow['accounts']), expected['aggregate_minimum_after_confirmed_minor'])
            self.assertEqual(sum(a['minimum_available_if_candidates_occur_minor'] for a in flow['accounts']), expected['aggregate_minimum_if_candidates_minor'])
            events = [e for slot in flow['timeline'] for e in slot['events']]
            self.assertTrue(all(e['kind'] == 'recorded_forecast' and e['certainty'] == 'candidate' for e in events))
            actual = sorted((e['evidence_id'], e['date'], e['amount_minor']) for e in events)
            frozen = sorted((e['obligation_id'], e['date'], e['amount_minor']) for e in expected['forecast_occurrences_60d'])
            self.assertEqual(actual, frozen)
            answers[uid] = [budget['investable_minor'], flow['totals']['candidate_outflow_minor']]
        self.assertEqual(self.finances(), before)
        self.assertEqual(len({tuple(v) for v in answers.values()}), 4)
        self.evidence(answers_minor=answers, no_financial_execution=True)

    def test_four_profiles_isolate_history_memory_and_pending_operation(self):
        account_ids = []
        for uid, owner in self.owners.items():
            history = self.service.history(owner, 'daily_balances', limit=2)
            self.assertEqual(history['total'], ORACLE['profiles'][uid]['daily_balance_count'])
            self.assertTrue(all(r['user_id'] == uid for r in history['rows']))
            annual = self.service.history(owner, 'annual_summaries', period='2016')
            self.assertTrue(annual['coverage_partial_period'])
            self.assertEqual(annual['rows'][0]['period_start'], '2016-10-04')
            self.g.memories.create(owner, {'category': 'goal', 'value': '四画像隔离目标-' + uid})
            self.g.personal_data.save_history(owner, [{'role': 'user', 'content': '四画像隔离历史-' + uid}])
            account_ids.append({a['id'] for a in self.g.overview(owner)['accounts']})
        self.assertEqual(len(set.union(*account_ids)), sum(map(len, account_ids)))
        for uid, owner in self.owners.items():
            self.assertEqual(self.g.personal_data.read_history(owner)[0]['content'], '四画像隔离历史-' + uid)
            memories = self.g.memories.state(owner)['items']
            self.assertEqual(len(memories), 1)
            self.assertEqual(memories[0]['value'], '四画像隔离目标-' + uid)
        before = self.finances()
        view = self.prepare_transfer()
        for uid, owner in self.owners.items():
            if uid != 'tycoon':
                with self.assertRaises(BankError) as caught:
                    self.g.operation_view(owner, view['operation']['id'])
                self.assertEqual(caught.exception.code, 'NOT_FOUND')
        self.assertEqual(self.finances(), before)
        self.evidence(profile_ids=list(self.owners), foreign_operation_reads_denied=3)

    def test_http_select_enforces_csrf_workspace_and_rebinds_current_history(self):
        self.start_http()
        owner, token = self.g.demo_sessions.create('tycoon')
        payload = {'user_id': 'working_landlady', 'expected_workspace_id': owner}
        status, _, _ = self.request('POST', '/api/finance/demo/select', token=token, payload=payload)
        self.assertEqual(status, 403)
        status, result, _ = self.request('POST', '/api/finance/demo/select', token=token,
            csrf=self.g.csrf(token), payload={**payload, 'expected_workspace_id': self.owners['grain_farmer']})
        self.assertEqual(status, 409)
        self.assertEqual(result['error']['code'], 'WORKSPACE_CHANGED')
        status, result, new_token = self.request('POST', '/api/finance/demo/select', token=token,
                                               csrf=self.g.csrf(token), payload=payload)
        self.assertEqual(status, 200)
        self.assertEqual(result['account_id'], 'working_landlady')
        self.assertEqual(result['workspace_id'], self.owners['working_landlady'])
        self.assertNotEqual(new_token, token)
        status, current, _ = self.request('GET', '/api/finance/demo/current', token=new_token)
        self.assertEqual(status, 200)
        self.assertEqual(current['bank_state']['cash_minor'], ORACLE['profiles']['working_landlady']['terminal_journal_totals']['cash_minor'])
        status, history, _ = self.request('GET', '/api/finance/demo/history?table=daily_balances&limit=2', token=new_token)
        self.assertEqual(status, 200)
        self.assertEqual(history['total'], 3652)
        self.assertTrue(all(r['user_id'] == 'working_landlady' for r in history['rows']))
        self.evidence(csrf_required=True, expected_workspace_required=True, account_id=result['account_id'])

    def test_switch_revokes_old_token_permanently_and_denies_foreign_draft(self):
        self.start_http()
        owner, old_token = self.g.demo_sessions.create('tycoon')
        before = self.finances()
        view = self.prepare_transfer()
        confirm = {'confirmed': True, 'confirmation_digest': view['operation']['confirmation_digest'],
                   'verification_code': self.g.demo_code(old_token)}
        path = '/api/finance/operations/' + view['operation']['id'] + '/confirm'
        status, state, token = self.request('POST', '/api/finance/demo/select', token=old_token,
            csrf=self.g.csrf(old_token), payload={'user_id': 'grain_farmer', 'expected_workspace_id': owner})
        self.assertEqual(status, 200)
        for active, expected_status, expected_code in ((old_token, 409, 'SESSION_CHANGED'),
                                                       (token, 404, 'NOT_FOUND')):
            status, rejected, _ = self.request('POST', path, token=active, csrf=self.g.csrf(active), payload=confirm)
            self.assertEqual(status, expected_status)
            self.assertEqual(rejected['error']['code'], expected_code)
        status, _, returned_token = self.request('POST', '/api/finance/demo/select', token=token,
            csrf=self.g.csrf(token), payload={'user_id': 'tycoon', 'expected_workspace_id': state['workspace_id']})
        self.assertEqual(status, 200)
        self.assertNotEqual(returned_token, old_token)
        status, rejected, _ = self.request('GET', '/api/finance/session', token=old_token)
        self.assertEqual(status, 409)
        self.assertEqual(rejected['error']['code'], 'SESSION_CHANGED')
        self.assertEqual(self.g.operation_view(owner, view['operation']['id'])['operation']['state'], 'pending_confirmation')
        self.assertEqual(self.finances(), before)
        self.evidence(old_token_permanently_revoked=True, foreign_draft_denied=True, no_financial_execution=True)

    def test_offline_reimport_preserves_running_state_and_personal_history(self):
        owner, token = self.g.demo_sessions.create('tycoon')
        self.g.personal_data.save_history(owner, [{'role': 'user', 'content': '重新导入保留这条隔离历史'}])
        view = self.prepare_transfer()
        before = self.finances()
        reimported = self.service.initialize_offline(allow_offline=True)
        self.assertTrue(reimported['already_initialized'])
        self.assertFalse(reimported['history_replayed'])
        self.assertEqual(self.finances(), before)
        self.assertEqual(self.g.bank.session(token), (owner, 'human'))
        self.assertEqual(self.g.personal_data.read_history(owner)[0]['content'], '重新导入保留这条隔离历史')
        self.assertEqual(self.g.operation_view(owner, view['operation']['id'])['operation']['state'], 'pending_confirmation')
        self.evidence(initialized_count=4, reimport_resets_cash=False, history_replayed=False)


if __name__ == '__main__':
    unittest.main()
