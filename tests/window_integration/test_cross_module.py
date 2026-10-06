"""独立跨模块验收：隔离当前源码、真实 SQLite/HTTP、脚本模型协议。

冻结预期位于同目录 contracts.json。任何业务缺陷应交实现负责人；本文件
不调用现役数据库、真实模型或外网。成功证据输出供集成执行器保存。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, timedelta
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet
from agent_runtime.bank_tools import BankTools
from agent_runtime.conversation import ConversationAgent
from agent_runtime.model_adapters import ModelAdapterError, OpenAICompatibleJsonClient
from agent_runtime.native_client import DeepSeekToolClient
from finance_gateway import COOKIE, FinanceGateway
from server import ProxyHandler, app_release
from simulated_bank import BankError
from simulated_bank.agent_guard import bind_review, context_hash
import fund_history

CONTRACT_PATH = Path(__file__).with_name('contracts.json')
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding='utf-8'))
EXPECT = CONTRACT['expectations']
CONTRACT_HASH = hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()


def call(name, arguments):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': 'integration-' + name, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}


def last_result(messages):
    return json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))


class ScriptedPrimary:
    model_id = 'integration-scripted-protocol-only'
    billing_mode = 'unpaid_fixture'

    def __init__(self, steps):
        self.steps = list(steps)
        self.sent = []

    def complete(self, messages, tools, **kwargs):
        self.sent.append(json.loads(json.dumps(messages)))
        if not self.steps:
            raise AssertionError('脚本步骤已用完；禁止回落云模型')
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return (step(messages, tools) if callable(step) else step), {
            'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}


class ScriptedReviewer:
    model_id = 'integration-scripted-review-protocol-only'
    billing_mode = 'unpaid_fixture'

    def __init__(self, verdict='approve', hook=None):
        self.verdict, self.hook, self.packets = verdict, hook, []

    def review(self, packet, *args):
        self.packets.append(json.loads(json.dumps(packet)))
        if self.hook:
            self.hook()
        return {'verdict': self.verdict, 'reason': '隔离协议测试裁决', 'issues': [],
                '_model_usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}


def reviewed_answer(messages, tools):
    output = last_result(messages)
    operation = output.get('data', {}).get('operation')
    return call('answer_reviewed', {
        'text': '已准备具体草案，待本人核对确认，尚未执行。' if operation else '这是本轮有原文依据的分析。',
        'claims': [{'text': '依据来自本轮工具。', 'evidence_ids': [output['evidence_id']]}],
        'draft_ids': [operation['id']] if operation else [], 'followups': []})


class CrossModuleIntegrationTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-window-integration-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.key = Fernet.generate_key()
        self.environment = patch.dict(os.environ, {
            'NAVREN_PERSONAL_DATA_KEY': self.key.decode(),
            'NAVREN_SIM_DB': str(self.directory / 'bank.sqlite3'),
            'NAVREN_KNOWLEDGE_DB': str(self.directory / 'knowledge.sqlite3'),
            'NAVREN_MARKET_DB': str(self.directory / 'market.sqlite3'),
            'NAVREN_FUND_CACHE': str(self.directory / 'funds'),
            'NAVREN_ALLOW_CLOUD_ASR': '0'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.cache = patch.object(fund_history, 'CACHE', self.directory / 'funds')
        self.cache.start()
        self.addCleanup(self.cache.stop)
        # cached 的默认参数在模块导入时绑定；只改 CACHE 不能隔离已导入别名。
        cache_default = patch.object(fund_history.cached, '__defaults__', (self.directory / 'funds',))
        cache_default.start()
        self.addCleanup(cache_default.stop)
        original_connect = socket.create_connection
        def local_connections_only(address, *args, **kwargs):
            if address[0] not in ('127.0.0.1', 'localhost', '::1'):
                raise AssertionError('隔离验收禁止非本机网络连接')
            return original_connect(address, *args, **kwargs)
        network_guard = patch.object(socket, 'create_connection', side_effect=local_connections_only)
        network_guard.start()
        self.addCleanup(network_guard.stop)
        for target in (patch.object(DeepSeekToolClient, 'complete', side_effect=AssertionError('禁止真实模型调用')),
                       patch.object(OpenAICompatibleJsonClient, 'complete_json', side_effect=AssertionError('禁止真实审核调用')),
                       patch.object(fund_history, 'urlopen', side_effect=AssertionError('禁止外部行情请求'))):
            target.start()
            self.addCleanup(target.stop)
        self.g = FinanceGateway(self.directory / 'bank.sqlite3')
        self.w, self.token = self.g.create_session()
        self.initial = self.g.overview(self.w)
        self.main = next(a['id'] for a in self.initial['accounts'] if a['id'].endswith('-main'))
        self.assertEqual(self.initial['total_cash_minor'], EXPECT['default_fixture_cash_minor'])
        self.assertEqual(self.initial['holding_principal_minor'], EXPECT['default_fixture_holding_minor'])
        self.assertTrue(self.g.bank.database.is_relative_to(self.directory))
        self.assertTrue(self.g.personal_data.database.is_relative_to(self.directory))
        self.assertTrue(self.g.knowledge_provider.path.is_relative_to(self.directory))

    def evidence(self, **values):
        print('WINDOW_INTEGRATION_EVIDENCE ' + json.dumps({
            'test': self.id().split('.')[-1], 'contract_sha256': CONTRACT_HASH,
            'workspace': self.w, 'scope': 'temporary_synthetic_current_source', **values},
            ensure_ascii=False, sort_keys=True, default=str), flush=True)

    def raw_state(self, workspace=None):
        owner = workspace or self.w
        tables = ('accounts', 'holdings', 'orders', 'transactions', 'fund_holds',
                  'subscriptions', 'mandates', 'card_controls')
        with closing(self.g.bank.connect()) as db:
            return {table: [dict(row) for row in db.execute(
                'SELECT * FROM ' + table + ' WHERE workspace_id=? ORDER BY rowid', (owner,))]
                for table in tables}

    def operation_count(self, workspace=None):
        with closing(self.g.bank.connect()) as db:
            return db.execute('SELECT count(*) FROM operations WHERE workspace_id=?',
                              (workspace or self.w,)).fetchone()[0]

    def action_payload(self, action='transfer', key='integration-action', **extra):
        payload = {'action': action, 'account_id': self.main, 'idempotency_key': key}
        if action == 'transfer':
            payload.update(amount_minor=EXPECT['transfer_minor'], beneficiary_id=next(
                b['id'] for b in self.initial['beneficiaries'] if b['alias'] == '妈妈'))
        elif action == 'purchase':
            payload.update(amount_minor=EXPECT['purchase_minor'], product_id='SIM-CASH-01')
        return {**payload, **extra}

    def prepare(self, action='transfer', **extra):
        return self.g.prepare(self.w, self.action_payload(action, **extra))

    def confirm_body(self, view, *, pin=True):
        body = {'confirmed': True, 'confirmation_digest': view['operation']['confirmation_digest']}
        if pin:
            body['verification_code'] = self.g.demo_code(self.token)
        return body

    def confirm(self, view, *, pin=True):
        return self.g.confirm(self.w, view['operation']['id'], self.confirm_body(view, pin=pin))

    def ledger_for(self, operation):
        with closing(self.g.bank.connect()) as db:
            return [dict(r) for r in db.execute(
                'SELECT * FROM transactions WHERE workspace_id=? AND operation_id=?', (self.w, operation))]

    def assert_bank_error(self, expected, operation):
        with self.assertRaises(BankError) as caught:
            operation()
        if expected:
            self.assertEqual(caught.exception.code, expected)
        return caught.exception.code

    def public_fixture(self, *, text='本产品净值可能波动，投资者可能损失本金。', suffix='risk'):
        p = self.g.knowledge_provider
        p.register_source('integration-source', publisher='隔离公共测试来源',
                          endpoints=['https://official.example/'], freshness={'risk': None, 'terms': None},
                          permission_basis='isolated_integration_fixture_only')
        p.register_product('WI-P', issuer='WI-ISSUER', scheme='integration', code='WI-P',
                           name='隔离测试产品', verified=True)
        version = p.stage_document(source_id='integration-source',
            source_url='https://official.example/' + suffix, title='隔离测试风险条款', text=text,
            published_on='2026-01-01', retrieved_on=EXPECT['public_evidence_as_of'],
            effective_from='2026-01-01', effective_to='2099-01-01', product_id='WI-P', facets=['risk'])
        p.approve_version(version, verification_basis='isolated_fixture_verified')
        p.publish()
        return version

    def test_isolated_paths_and_local_provider_wiring(self):
        paths = {self.g.bank.database, self.g.personal_data.database, self.g.knowledge_provider.path}
        self.assertEqual(len(paths), 3)
        self.public_fixture()
        tools = BankTools(self.g, self.w, 'provider-wiring')
        result = tools.invoke('knowledge_search', {'query': '风险', 'product_id': 'WI-P',
            'facets': ['risk'], 'as_of': EXPECT['public_evidence_as_of']})['data']
        self.assertTrue(result['hits'], result)
        self.assertTrue(result['public_release_id'])
        self.evidence(paths=[str(p.relative_to(self.directory)) for p in sorted(paths)],
                      release_id=result['public_release_id'], citations=[h['citation_id'] for h in result['hits']])

    def test_six_business_reads_and_analysis_have_no_financial_effect(self):
        before = self.raw_state()
        tools = BankTools(self.g, self.w, 'six-business-read')
        requests = [('business_records', {'kind': kind}) for kind in ('payments', 'cards', 'subscriptions', 'tasks')]
        requests += [('monthly_statement', {'month': '2026-09'}), ('products', {}),
                     ('account_summary', {}), ('cashflow', {'days': 60})]
        outputs = [tools.invoke(name, parameters) for name, parameters in requests]
        self.assertTrue(all(o['evidence_id'] in tools.evidence for o in outputs))
        self.assertTrue(all(o['data'] for o in outputs))
        self.assertEqual(self.raw_state(), before)
        self.assertEqual(self.operation_count(), 0)
        primary = ScriptedPrimary([call('cashflow', {'days': 60}), reviewed_answer])
        reviewer = ScriptedReviewer()
        reply = ConversationAgent(primary, reviewer).process(self.g, self.w,
            '查看未来60天已记录的缴费和续费，先不要执行。')
        self.assertFalse(reply.get('isFallback'), reply.get('error'))
        self.assertEqual(reply['bank_actions'], [])
        self.assertEqual(self.operation_count(), 0)
        self.assertEqual(self.raw_state(), before)
        self.evidence(tools=list(reply['capabilities_used']), operations=0,
                      cash_minor=self.initial['total_cash_minor'], reviewed=len(reviewer.packets))

    def test_four_workspaces_isolate_accounts_history_memory_calendar_and_operations(self):
        owners = [(self.w, self.token)] + [self.g.create_session() for _ in range(3)]
        accounts, memory_ids, views = [], [], []
        for index, (owner, token) in enumerate(owners):
            state = self.g.overview(owner)
            accounts.append({a['id'] for a in state['accounts']})
            saved = self.g.memories.create(owner, {'category': 'goal', 'value': f'隔离储蓄目标{index}'})
            memory_ids.append(saved['items'][0]['node_id'])
            draft = self.g.calendar.events.prepare(owner, {'action': 'create', 'event': {
                'title': f'隔离计划{index}', 'kind': 'life_event', 'start_on': '2026-11-05',
                'recurrence': 'once', 'budget_minor': 10000 + index}})
            self.g.calendar.events.confirm(owner, draft['draft_id'], {'confirmed': True})
            views.append(self.g.calendar.query(owner, {'from': '2026-11-05', 'to': '2026-11-05'}))
            # 授权资料修改会主动清历史；固定本工作区最终资料版本后保存。
            self.g.personal_data.save_history(owner, [{'role': 'user', 'content': f'隔离历史{index}'}])
        self.assertEqual(len(set.union(*accounts)), sum(map(len, accounts)))
        for index, (owner, _) in enumerate(owners):
            self.assertEqual(self.g.personal_data.read_history(owner)[0]['content'], f'隔离历史{index}')
            self.assertEqual([i['value'] for i in self.g.memories.state(owner)['items']], [f'隔离储蓄目标{index}'])
            other = owners[(index + 1) % len(owners)][0]
            self.assert_bank_error(None, lambda: self.g.budget(other, next(iter(accounts[index]))))
            self.assert_bank_error(None, lambda: self.g.memories.correct(other, memory_ids[index], {'value': '越权'}))
            self.assert_bank_error('CALENDAR_NOT_FOUND', lambda: self.g.calendar.detail(other, views[index]['view_id'], '2026-11-05'))
        before_other = {w: self.raw_state(w) for w, _ in owners[1:]}
        prepared = self.prepare()
        for owner, _ in owners[1:]:
            self.assert_bank_error('NOT_FOUND', lambda: self.g.operation_view(owner, prepared['operation']['id']))
            self.assert_bank_error('NOT_FOUND', lambda: self.g.confirm(owner, prepared['operation']['id'], self.confirm_body(prepared)))
        executed = self.confirm(prepared)
        self.assertEqual(executed['overview']['total_cash_minor'], EXPECT['default_fixture_cash_minor'] - EXPECT['transfer_minor'])
        for owner, _ in owners[1:]:
            self.assertEqual(self.raw_state(owner), before_other[owner])
        self.evidence(workspaces=[w for w, _ in owners], account_counts=[len(a) for a in accounts],
                      operation_id=prepared['operation']['id'], unaffected_others=3)

    def test_four_contest_profiles_current_data_connection(self):
        oracle = json.loads((ROOT / 'evaluation/window_integration/four_profile_oracles.json').read_text())
        source = ROOT / oracle['source_directory']
        if not (source / 'financial_data.sqlite3').is_file():
            self.skipTest('候选缺少已冻结outputs/contest-finances来源副本；不使用随机工作区替代四画像')
        self.g.demo_accounts.source_dir = source
        initialized = self.g.demo_accounts.initialize_offline(allow_offline=True)
        self.assertEqual(initialized['initialized_count'], 4)
        self.assertEqual(self.g.demo_accounts.catalog()['initialized_count'], 4)
        owners = {uid: self.g.demo_accounts.workspace_for(uid) for uid in oracle['profiles']}
        self.assertEqual(len(set(owners.values())), 4)
        results = []
        for uid, owner in owners.items():
            profile = oracle['profiles'][uid]
            state = self.g.overview(owner)
            self.assertEqual(state['profile']['risk_level'], profile['risk_level'])
            self.assertEqual(state['profile']['reserve_minor'], profile['reserve_minor'])
            self.assertEqual(state['total_cash_minor'], profile['terminal_journal_totals']['cash_minor'])
            result = self.g.cashflow_timeline(owner, 60)
            self.assertEqual(result['totals']['candidate_outflow_minor'], profile['candidate_outflow_60d_minor'])
            results.append(result)
        self.assertGreater(len({json.dumps(r['accounts'], sort_keys=True) for r in results}), 1)
        self.evidence(profile_ids=list(owners), workspaces=list(owners.values()), source='navren_synthetic')

    def test_transfer_draft_confirmation_unique_ledger_and_receipt(self):
        before = self.raw_state()
        view = self.prepare()
        self.assertEqual(view['operation']['state'], 'pending_confirmation')
        self.assertEqual(self.raw_state(), before)
        self.assert_bank_error(None, lambda: self.g.confirm(self.w, view['operation']['id'], self.confirm_body(view, pin=False)))
        self.assertEqual(self.raw_state(), before)
        result = self.confirm(view)
        op = result['operation']
        self.assertEqual(op['state'], 'succeeded')
        self.assertEqual(result['overview']['total_cash_minor'], self.initial['total_cash_minor'] - EXPECT['transfer_minor'])
        ledger = self.ledger_for(op['id'])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['delta_minor'], -EXPECT['transfer_minor'])
        self.assertEqual(op, self.g.operation_view(self.w, op['id'])['operation'])
        self.evidence(operation_id=op['id'], state=op['state'], ledger_rows=len(ledger),
                      cash_before=self.initial['total_cash_minor'], cash_after=result['overview']['total_cash_minor'])

    def test_purchase_draft_receipt_preserves_cash_plus_principal(self):
        before = self.raw_state()
        view = self.prepare('purchase')
        self.assertEqual(self.raw_state(), before)
        result = self.confirm(view)
        state, op = result['overview'], result['operation']
        self.assertEqual(state['total_cash_minor'], self.initial['total_cash_minor'] - EXPECT['purchase_minor'])
        self.assertEqual(state['holding_principal_minor'], self.initial['holding_principal_minor'] + EXPECT['purchase_minor'])
        self.assertEqual(state['total_cash_minor'] + state['holding_principal_minor'],
                         self.initial['total_cash_minor'] + self.initial['holding_principal_minor'])
        self.assertEqual(len(state['orders']), 1)
        self.assertEqual(state['orders'][0]['operation_id'], op['id'])
        self.assertEqual(len(self.ledger_for(op['id'])), 1)
        self.assertTrue(state['integrity']['ok'])
        self.evidence(operation_id=op['id'], cash_minor=state['total_cash_minor'],
                      principal_minor=state['holding_principal_minor'], order_ids=[r['id'] for r in state['orders']])

    def test_subscription_cancel_keeps_bank_mandate_and_cash(self):
        subscription = next(s for s in self.initial['subscriptions'] if s['merchant_status'] == 'active')
        before = self.raw_state()
        view = self.prepare('cancel_subscription', subscription_id=subscription['id'])
        self.assertEqual(self.raw_state(), before)
        result = self.confirm(view)
        updated = next(s for s in result['overview']['subscriptions'] if s['id'] == subscription['id'])
        self.assertEqual(updated['merchant_status'], 'cancelled')
        self.assertEqual(result['overview']['mandates'], self.initial['mandates'])
        self.assertEqual(result['overview']['total_cash_minor'], self.initial['total_cash_minor'])
        self.assertEqual(self.ledger_for(view['operation']['id']), [])
        self.evidence(operation_id=view['operation']['id'], subscription_id=subscription['id'],
                      merchant_status=updated['merchant_status'], cash_minor=result['overview']['total_cash_minor'])

    def test_card_control_changes_only_confirmed_control(self):
        card = next(c for c in self.initial['cards'] if c['type'] == 'debit' and c['status'] == 'active')
        control = next(c for c in card['controls'] if c['control_type'] == 'overseas')
        target = not bool(control['enabled'])
        before = self.raw_state()
        view = self.prepare('card_control', account_id=card['account_id'], card_id=card['id'],
                            control_type='overseas', enabled=target)
        self.assertEqual(view['operation']['permission'], 'yellow')
        self.assertEqual(self.raw_state(), before)
        result = self.confirm(view, pin=False)
        updated = next(c for c in result['overview']['cards'] if c['id'] == card['id'])
        self.assertEqual(bool(next(c for c in updated['controls'] if c['control_type'] == 'overseas')['enabled']), target)
        after = self.raw_state()
        self.assertEqual({k: v for k, v in after.items() if k != 'card_controls'},
                         {k: v for k, v in before.items() if k != 'card_controls'})
        self.evidence(operation_id=view['operation']['id'], card_id=card['id'], control='overseas', enabled=target)

    def test_statement_import_receipt_source_and_unknown_cash_direction(self):
        before = self.raw_state()
        csv = ('流水号,日期,商户,金额,类型,分类,币种,备注\n'
            'wi-income,2026-09-01,工资,100.01,收入,,CNY,\n'
            'wi-expense,2026-09-02,餐厅,20.10,支出,餐饮,CNY,\n'
            'wi-refund,2026-09-03,餐厅,5.10,退款,,CNY,\n'
            'wi-transfer,2026-09-04,内部转账,33.00,转账,,CNY,\n')
        preview = self.g.statements.preview(self.w, csv)
        other, _ = self.g.create_session()
        self.assert_bank_error(None, lambda: self.g.statements.commit(other, {'batch_id': preview['batch_id'], 'confirmed': True}))
        self.assert_bank_error('CONFIRMATION_REQUIRED', lambda: self.g.statements.commit(self.w, {'batch_id': preview['batch_id'], 'confirmed': False}))
        receipt = self.g.statements.commit(self.w, {'batch_id': preview['batch_id'], 'confirmed': True})
        replay = self.g.statements.commit(self.w, {'batch_id': preview['batch_id'], 'confirmed': True})
        self.assertEqual(receipt['inserted_count'], 4)
        self.assertTrue(replay['idempotent_replay'])
        self.g.statements.set_source(self.w, {'source': 'imported'})
        report = self.g.month_analysis(self.w, '2026-09') if hasattr(self.g, 'month_analysis') else self.g.statements.analysis(self.w, '2026-09')
        for kind in ('income', 'expense', 'refund', 'transfer'):
            self.assertEqual(report['totals_minor'][kind], EXPECT['imported_' + kind + '_minor'])
        self.assertEqual(report['income_expense_surplus_minor'], EXPECT['imported_surplus_minor'])
        self.assertIsNone(report['cash_delta_minor'])
        self.assertEqual(self.raw_state(), before)
        self.assertIsNone(self.g.statements.source(other)['dataset'])
        calendar = self.g.calendar.query(self.w, {'source': 'imported', 'from': '2026-09-01', 'to': '2026-09-30'})
        self.assertTrue(calendar['source_scope'])
        self.evidence(batch_id=preview['batch_id'], inserted=receipt['inserted_count'],
                      surplus_minor=report['income_expense_surplus_minor'], cash_delta_minor=report['cash_delta_minor'], bank_cash_unchanged=True)

    def test_cross_scene_reserve_then_release_never_books_cash(self):
        task = next(t for t in self.initial['orchestration_tasks'] if t['status'] == 'draft')
        due = date.fromisoformat(task['due_on'])
        expiry = min(due, date.fromisoformat(self.initial['as_of']) + timedelta(days=5)).isoformat()
        before = self.raw_state()
        view = self.prepare('reserve_funds', task_id=task['id'], amount_minor=EXPECT['reserve_minor'], expires_on=expiry)
        self.assertEqual(self.raw_state(), before)
        result = self.confirm(view)
        self.assertEqual(result['overview']['total_cash_minor'], self.initial['total_cash_minor'])
        self.assertEqual(result['overview']['held_funds_minor'], self.initial['held_funds_minor'] + EXPECT['reserve_minor'])
        self.assertEqual(result['overview']['investable_minor'], self.initial['investable_minor'] - EXPECT['reserve_minor'])
        hold = next(h for h in result['overview']['fund_holds'] if h['status'] == 'held' and h['task_id'] == task['id'])
        release = self.prepare('release_funds', key='release-action', hold_id=hold['id'])
        done = self.confirm(release)
        self.assertEqual(done['overview']['held_funds_minor'], self.initial['held_funds_minor'])
        self.assertEqual(done['overview']['investable_minor'], self.initial['investable_minor'])
        self.assertEqual(done['overview']['total_cash_minor'], self.initial['total_cash_minor'])
        self.assertEqual(self.ledger_for(view['operation']['id']), [])
        self.assertEqual(self.ledger_for(release['operation']['id']), [])
        self.evidence(operation_ids=[view['operation']['id'], release['operation']['id']], hold_id=hold['id'],
                      held_minor=done['overview']['held_funds_minor'], cash_minor=done['overview']['total_cash_minor'])

    def test_cross_scene_booking_requires_receipt_and_exact_quote_total(self):
        task = next(t for t in self.initial['orchestration_tasks'] if t['status'] == 'draft')
        quotes = [q for q in self.initial['service_quotes'] if q['status'] == 'illustrative']
        self.assertTrue(quotes)
        with closing(self.g.bank.connect()) as db:
            prices = {r['id']: r['price_minor'] for r in db.execute(
                'SELECT id,price_minor FROM service_quotes WHERE workspace_id=?', (self.w,))}
        total = sum(prices[q['id']] for q in quotes)
        reserve = self.prepare('reserve_funds', amount_minor=total, task_id=task['id'], expires_on=task['due_on'])
        held = self.confirm(reserve)
        self.assertEqual(held['overview']['total_cash_minor'], self.initial['total_cash_minor'])
        hold = next(h for h in held['overview']['fund_holds'] if h['status'] == 'held' and h['task_id'] == task['id'])
        booking = self.prepare('book_services', key='service-booking', task_id=task['id'],
                               hold_id=hold['id'], quote_ids=[q['id'] for q in quotes])
        self.assertEqual(self.g.overview(self.w)['total_cash_minor'], self.initial['total_cash_minor'])
        done = self.confirm(booking)
        self.assertEqual(done['overview']['total_cash_minor'], self.initial['total_cash_minor'] - total)
        self.assertEqual(done['overview']['held_funds_minor'], 0)
        old_ids = {r['id'] for r in self.initial['service_reservations']}
        reservations = [r for r in done['overview']['service_reservations']
                        if r['task_id'] == task['id'] and r['id'] not in old_ids]
        self.assertTrue(all(r['status'] == 'confirmed' for r in reservations))
        self.assertEqual({r['quote_id'] for r in reservations}, {q['id'] for q in quotes})
        self.assertEqual(sum(r['amount_minor'] for r in reservations), total)
        ledger = self.ledger_for(booking['operation']['id'])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['delta_minor'], -total)
        replay = self.confirm(booking, pin=False)
        self.assertEqual(replay['operation'], done['operation'])
        self.assertEqual(replay['overview']['service_reservations'], done['overview']['service_reservations'])
        self.evidence(operation_ids=[reserve['operation']['id'], booking['operation']['id']],
                      quote_total_minor=total, reservation_ids=[r['id'] for r in reservations],
                      ledger_rows=1, cash_after=done['overview']['total_cash_minor'])

    def test_insufficient_budget_risk_and_horizon_reject_before_operation(self):
        before = self.raw_state()
        budget = self.g.budget(self.w, self.main)
        errors = [self.assert_bank_error('LIQUIDITY_GUARD', lambda: self.prepare(
            amount_minor=budget['investable_minor'] + 1))]
        for risk, horizon, key in ((1, 365, 'risk-only'), (5, 30, 'horizon-only')):
            self.g.bank.profile(self.w, {'risk_level': risk, 'horizon_days': horizon,
                                        'reserve_minor': self.initial['profile']['reserve_minor'], 'confirmed': True})
            errors.append(self.assert_bank_error('UNSUITABLE_PRODUCT', lambda: self.prepare(
                'purchase', key=key, product_id='SIM-BOND-90', amount_minor=100000)))
        self.assertEqual(self.raw_state(), before)
        self.assertEqual(self.operation_count(), 0)
        self.evidence(rejection_codes=errors, operations=0, cash_minor=self.g.overview(self.w)['total_cash_minor'])

    def test_model_timeout_has_no_fabricated_actions_or_money_effect(self):
        before = self.raw_state()
        primary = ScriptedPrimary([ModelAdapterError('provider_timeout', model_id='timeout-injection', attempts=1)])
        result = ConversationAgent(primary, ScriptedReviewer()).process(self.g, self.w, '查看未来60天现金流')
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['agent_runtime']['failure'], 'provider_timeout')
        self.assertEqual(result['bank_actions'], [])
        self.assertEqual(self.operation_count(), 0)
        self.assertEqual(self.raw_state(), before)
        self.evidence(failure=result['agent_runtime']['failure'], operations=0, cash_unchanged=True)

    def test_cancelled_and_withdrawn_drafts_cannot_execute(self):
        before = self.raw_state()
        view = self.prepare()
        self.g.bank.cancel(self.w, view['operation']['id'])
        self.assert_bank_error('INVALID_STATE', lambda: self.confirm(view))
        self.assertEqual(self.g.operation_view(self.w, view['operation']['id'])['operation']['state'], 'cancelled')
        second = self.g.prepare(self.w, self.action_payload(key='agent-withdraw'), agent_origin=True)
        tools = BankTools(self.g, self.w, 'withdraw-test')
        tools.invoke('withdraw_draft', {'operation_id': second['operation']['id']})
        self.assert_bank_error(None, lambda: self.confirm(second))
        self.assertEqual(self.raw_state(), before)
        self.evidence(operation_ids=[view['operation']['id'], second['operation']['id']],
                      states=[self.g.operation_view(self.w, i)['operation']['state'] for i in (view['operation']['id'], second['operation']['id'])], cash_unchanged=True)

    def test_repeat_and_concurrent_same_confirmation_are_idempotent(self):
        view = self.prepare('purchase')
        barrier = threading.Barrier(2)
        def run(_):
            barrier.wait(timeout=5)
            try:
                return {'state': 'succeeded', 'result': self.confirm(view)}
            except BankError as error:
                return {'state': error.code}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        succeeded = [r['result']['operation'] for r in results if r['state'] == 'succeeded']
        self.assertTrue(succeeded, results)
        self.assertTrue(all(r['state'] in ('succeeded', 'INVALID_STATE') for r in results), results)
        self.assertTrue(all(r == succeeded[0] for r in succeeded))
        replay = self.confirm(view, pin=False)
        self.assertEqual(replay['operation'], succeeded[0])
        self.assertEqual(len(self.ledger_for(view['operation']['id'])), 1)
        self.assertEqual(len(replay['overview']['orders']), 1)
        self.assertEqual(replay['overview']['total_cash_minor'], self.initial['total_cash_minor'] - EXPECT['purchase_minor'])
        executed = [r for r in self.g.bank.audit(self.w) if r['event'] == 'operation_executed']
        self.assertEqual(len(executed), 1)
        self.evidence(operation_id=view['operation']['id'], confirmations=3,
                      concurrent_outcomes=[r['state'] for r in results],
                      recovered_state=replay['operation']['state'], ledger_rows=1, execution_audit_rows=1)

    def test_concurrent_distinct_drafts_allow_only_one_account_version(self):
        views = [self.prepare(key='race-' + str(i), amount_minor=EXPECT['transfer_minor'] * (i + 1)) for i in range(2)]
        barrier = threading.Barrier(2)
        def run(view):
            barrier.wait(timeout=5)
            try:
                return self.confirm(view)['operation']['state']
            except BankError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            states = list(pool.map(run, views))
        self.assertEqual(states.count('succeeded'), 1)
        self.assertIn('STALE_CONFIRMATION', states)
        ledger = [r for view in views for r in self.ledger_for(view['operation']['id'])]
        self.assertEqual(len(ledger), 1)
        cash = self.g.overview(self.w)['total_cash_minor']
        self.assertEqual(cash, self.initial['total_cash_minor'] + ledger[0]['delta_minor'])
        self.evidence(operation_ids=[v['operation']['id'] for v in views], outcomes=states, ledger_rows=1, cash_minor=cash)

    def test_lost_http_receipt_recovers_original_operation_after_restart(self):
        view = self.prepare()
        operation = view['operation']['id']
        path = '/api/finance/operations/' + operation + '/confirm'
        dropped = threading.Event()
        class DropConfirmedResponse(ProxyHandler):
            def log_message(self, *args):
                pass
            def _finance_json(handler, status, data, cookie=None):
                if handler.path == path and status == 200 and not dropped.is_set():
                    dropped.set()
                    handler.connection.shutdown(socket.SHUT_RDWR)
                    handler.connection.close()
                    raise BrokenPipeError('integration_response_dropped')
                return super()._finance_json(status, data, cookie)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), DropConfirmedResponse)
        server.finance, server.app_release = self.g, app_release()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            body = json.dumps(self.confirm_body(view))
            connection.request('POST', path, body=body, headers={
                'Content-Type': 'application/json', 'Cookie': COOKIE + '=' + self.token,
                'X-QZ-CSRF': self.g.csrf(self.token)})
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionResetError)):
                connection.getresponse()
            connection.close()
            self.assertTrue(dropped.is_set())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        replacement = FinanceGateway(self.directory / 'bank.sqlite3')
        recovered = replacement.operation_view(self.w, operation)['operation']
        self.assertEqual(recovered['state'], 'succeeded')
        replay = replacement.confirm(self.w, operation, self.confirm_body(view, pin=False))
        self.assertEqual(replay['operation'], recovered)
        self.assertEqual(replay['overview']['total_cash_minor'], self.initial['total_cash_minor'] - EXPECT['transfer_minor'])
        self.assertEqual(len(self.ledger_for(operation)), 1)
        self.evidence(operation_id=operation, transport='actual_http_response_disconnect',
                      recovered_state=recovered['state'], gateway_reconstructed=True, ledger_rows=1)

    def test_agent_review_rejection_and_version_change_block_execution(self):
        before = self.raw_state()
        def prepare_from_observed(messages, schemas):
            payment = last_result(messages)['data']
            account_result = next(json.loads(m['content'])['data'] for m in messages
                if m['role'] == 'tool' and 'accounts' in json.loads(m['content']).get('data', {}))
            account = next(a['id'] for a in account_result['accounts'] if a['id'].endswith('-main'))
            beneficiary = next(b['id'] for b in payment['beneficiaries'] if b['alias'] == '妈妈')
            return call('prepare_operation', {'action': 'transfer', 'account_id': account,
                'beneficiary_id': beneficiary, 'amount_minor': EXPECT['transfer_minor']})
        primary = ScriptedPrimary([call('account_summary', {}),
            call('business_records', {'kind': 'payments'}), prepare_from_observed, reviewed_answer])
        rejected = ConversationAgent(primary, ScriptedReviewer('reject')).process(
            self.g, self.w, '转给妈妈100元')
        self.assertTrue(rejected['isFallback'])
        self.assertEqual(rejected['bank_actions'], [])
        self.assertEqual(rejected['agent_runtime']['failure'], 'AGENT_REVIEW_BLOCKED')
        with closing(self.g.bank.connect()) as db:
            rejected_id = db.execute('SELECT operation_id FROM agent_draft_reviews WHERE workspace_id=?',
                                     (self.w,)).fetchone()[0]
        rejected_view = self.g.operation_view(self.w, rejected_id)
        self.assert_bank_error('AGENT_REVIEW_REQUIRED', lambda: self.confirm(rejected_view))
        self.g.bank.cancel(self.w, rejected_id)
        pending = self.g.prepare(self.w, self.action_payload(key='unreviewed'), agent_origin=True)
        self.assert_bank_error('AGENT_REVIEW_REQUIRED', lambda: self.confirm(pending))
        context = context_hash_for(self.g, self.w)
        bind_review(self.g.bank, self.w, pending['operation']['id'], expected_context=context,
                    evidence_hash='a' * 64, reviewer_model='protocol-test-only', review_id='integration-review')
        self.assertEqual(self.raw_state(), before)
        self.g.bank.profile(self.w, {**{k: self.initial['profile'][k] for k in ('risk_level', 'horizon_days', 'reserve_minor')}, 'confirmed': True})
        self.assert_bank_error('STALE_CONFIRMATION', lambda: self.confirm(pending))
        self.assertEqual(self.raw_state(), before)
        self.evidence(rejected_operation_id=rejected_id, review_failure='AGENT_REVIEW_BLOCKED',
                      operation_id=pending['operation']['id'], old_state=self.g.operation_view(self.w, pending['operation']['id'])['operation']['state'], cash_unchanged=True)

    def test_public_evidence_withdrawal_blocks_tool_final_and_speech(self):
        version = self.public_fixture()
        tools = BankTools(self.g, self.w, 'withdraw-public')
        found = tools.invoke('knowledge_search', {'query': '风险', 'product_id': 'WI-P', 'as_of': EXPECT['public_evidence_as_of']})['data']
        self.assertTrue(found['hits'])
        snapshot = {'release_id': found['public_release_id'], 'citations': [
            {'citation_id': h['citation_id'], 'as_of': found['as_of']} for h in found['hits']]}
        receipt = self.g.approve_speech(self.w, {'reply': '原文提示可能损失本金。',
            'compliance_pass': True, 'answer_contract': {'kind': 'reviewed_text'},
            **self.g.session_data(self.w, self.token), 'knowledge_snapshot': snapshot})
        self.assertIsNotNone(receipt)
        self.g.knowledge_provider.withdraw(version)
        self.assert_bank_error('STALE_KNOWLEDGE_EVIDENCE', tools.fresh)
        def finish():
            with self.g.response_guard(self.w, **guard_arguments(self.g, self.w), knowledge_snapshot=snapshot):
                pass
        self.assert_bank_error('STALE_KNOWLEDGE_EVIDENCE', finish)
        self.assert_bank_error('STALE_KNOWLEDGE_EVIDENCE', lambda: self.g.resolve_speech(self.w, receipt))
        self.evidence(version_id=version, release_id=snapshot['release_id'],
                      citations=[h['citation_id'] for h in found['hits']], blocked=['tool_fresh', 'final_guard', 'speech'])

    def test_public_text_revision_invalidates_old_citation(self):
        first = self.public_fixture()
        tools = BankTools(self.g, self.w, 'revise-public')
        found = tools.invoke('knowledge_search', {'query': '风险', 'product_id': 'WI-P', 'as_of': EXPECT['public_evidence_as_of']})['data']
        self.assertTrue(found['hits'])
        second = self.public_fixture(text='修订条款：投资者可能损失本金，赎回还受开放日期限制。')
        self.assertNotEqual(first, second)
        self.assert_bank_error('STALE_KNOWLEDGE_EVIDENCE', tools.fresh)
        self.assertIsNone(self.g.knowledge_provider.resolve_public_evidence(
            found['hits'][0]['citation_id'], found['public_release_id'], found['as_of']))
        fresh = BankTools(self.g, self.w, 'revised-query').invoke('knowledge_search',
            {'query': '风险', 'product_id': 'WI-P', 'as_of': EXPECT['public_evidence_as_of']})['data']
        self.assertTrue(fresh['hits'])
        self.assertTrue(any('开放日期' in h['excerpt'] for h in fresh['hits']))
        self.evidence(old_version=first, new_version=second, old_citation_invalid=True,
                      new_release=fresh['public_release_id'])

    def test_public_share_channel_mismatch_and_private_outbound_boundary(self):
        self.public_fixture()
        tools = BankTools(self.g, self.w, 'identity-mismatch')
        for field, value in (('share_class_id', 'wrong-share'), ('channel_id', 'wrong-channel')):
            output = tools.invoke('knowledge_search', {'query': '风险', 'product_id': 'WI-P',
                'as_of': EXPECT['public_evidence_as_of'], field: value})['data']
            self.assertEqual(output['hits'], [])
        primary = ScriptedPrimary([call('knowledge_search', {'query': '风险', 'product_id': 'WI-P',
            'as_of': EXPECT['public_evidence_as_of']}), reviewed_answer])
        reviewer = ScriptedReviewer()
        text = '我叫张三，电话13812345678，邮箱private.integration@example.com，查这个账户 ' + self.main + ' 相关产品风险'
        result = ConversationAgent(primary, reviewer).process(self.g, self.w, text)
        self.assertFalse(result.get('isFallback'), result.get('error'))
        sent = json.dumps(primary.sent + reviewer.packets, ensure_ascii=False)
        for private in ('张三', '13812345678', 'private.integration@example.com', self.main, self.w):
            self.assertNotIn(private, sent)
        self.assertEqual(self.operation_count(), 0)
        self.evidence(share_channel_rejected=2, private_literals_omitted=5,
                      reviewer_calls=len(reviewer.packets), operations=0)

    def test_cashflow_calendar_graph_share_period_and_independent_cash_basis(self):
        before = self.raw_state()
        transactions = self.g.bank.transactions(self.w, '2026-09')
        calendar = self.g.calendar.query(self.w, {'source': 'simulation', 'from': '2026-09-01', 'to': '2026-09-30'})
        summary = next(s for s in calendar['summary_by_currency'] if s['currency'] == 'CNY')
        inflow = sum(max(r['delta_minor'], 0) for r in transactions)
        outflow = sum(max(-r['delta_minor'], 0) for r in transactions)
        self.assertEqual(summary['recorded_inflow_minor'], inflow)
        self.assertEqual(summary['recorded_outflow_minor'], outflow)
        self.assertEqual(summary['recorded_net_minor'], inflow - outflow)
        graph = self.g.financial_graph.query(self.w, {'view': 'cashflow', 'source': 'simulation',
            'from': '2026-09-01', 'to': '2026-09-30'})
        self.assertEqual(graph['period']['to_exclusive'], '2026-10-01')
        self.assertTrue(graph['calculations'])
        for calculation in graph['calculations']:
            rows = [r for r in calculation['input_refs'] if not r.get('comparison_period')]
            self.assertTrue(rows)
            self.assertEqual(calculation['amount_minor'], sum(
                -r['amount_minor'] if r['kind'] == 'refund' else r['amount_minor'] for r in rows))
            for row in rows:
                source_id = row.get('source_id', row.get('id'))
                self.assertTrue(source_id)
        timeline = self.g.cashflow_timeline(self.w, 60)
        for account in timeline['accounts']:
            bank_account = next(a for a in self.initial['accounts'] if a['id'] == account['account_id'])
            self.assertEqual(account['current_available_minor'], bank_account['balance_minor'] -
                             account['reserve_floor_minor'] - account['held_minor'])
        self.assertEqual(self.raw_state(), before)
        self.evidence(period={'from': '2026-09-01', 'to': '2026-09-30'},
                      raw_transaction_count=len(transactions), cash_inflow_minor=inflow, cash_outflow_minor=outflow,
                      graph_version=graph['version'], graph_calculation_count=len(graph['calculations']),
                      timeline_horizon=timeline['horizon_days'])

    def test_calendar_plan_is_not_cash_and_cross_owner_details_are_denied(self):
        before = self.raw_state()
        draft = self.g.calendar.events.prepare(self.w, {'action': 'create', 'event': {
            'title': '生日礼物计划', 'kind': 'birthday', 'start_on': '2026-11-05',
            'recurrence': 'yearly', 'budget_minor': 50000}})
        self.assertEqual(self.raw_state(), before)
        done = self.g.calendar.events.confirm(self.w, draft['draft_id'], {'confirmed': True})
        query = self.g.calendar.query(self.w, {'from': '2026-11-05', 'to': '2026-11-05'})
        item = next(i for day in query['days'] for i in day['preview'] if i.get('series_id') == done['series_id'])
        self.assertEqual(item['budget_minor'], 50000)
        self.assertEqual(self.raw_state(), before)
        other, _ = self.g.create_session()
        self.assert_bank_error('CALENDAR_NOT_FOUND', lambda: self.g.calendar.detail(other, query['view_id'], '2026-11-05'))
        self.assert_bank_error('CALENDAR_NOT_FOUND', lambda: self.g.calendar.item(other, item['id']))
        unknown = self.g.calendar.query(self.w, {'from': '2030-01-01', 'to': '2030-01-01'})
        self.assertEqual(unknown['days'][0]['coverage'], 'unknown')
        self.assertEqual(unknown['summary_by_currency'], [])
        self.evidence(draft_id=draft['draft_id'], series_id=done['series_id'], item_id=item['id'],
                      budget_minor=50000, cash_unchanged=True, missing_coverage='unknown')

    def test_linked_reserve_is_deduplicated_against_actual_commitment(self):
        tools = BankTools(self.g, self.w, 'reserve-deduplication')
        baseline = tools.invoke('budget', {'account_id': self.main})['data']
        commitment = next(c for c in baseline['commitments'] if c['account_id'] == self.main and c['amount_minor'] > 0)
        requested = commitment['amount_minor'] + 10000
        result = tools.invoke('budget', {'account_id': self.main, 'reserves': [{
            'label': '同笔已记责任的额外留款', 'amount_minor': requested,
            'commitment_ids': [commitment['evidence_id']]}]})['data']
        self.assertEqual(result['additional_reserve_minor'], 10000)
        self.assertEqual(result['investable_minor'], baseline['investable_minor'] - 10000)
        self.assertEqual(self.operation_count(), 0)
        self.evidence(commitment_id=commitment['evidence_id'], already_counted_minor=commitment['amount_minor'],
                      requested_minor=requested, incremental_minor=result['additional_reserve_minor'])


def context_hash_for(gateway, workspace):
    with closing(gateway.bank.connect()) as db:
        return context_hash(db, workspace)


def guard_arguments(gateway, workspace):
    from user_memory import privacy_revision
    with closing(gateway.bank.connect()) as db:
        revision = privacy_revision(db, workspace)
    return {'expected_privacy': revision, 'expected_personal': gateway.personal_data.revision(workspace)}


if __name__ == '__main__':
    unittest.main(verbosity=2)
