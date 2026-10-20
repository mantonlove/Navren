"""冻结后的 12 项独立任务：公开市场、语音边界和合成服务协商。

这不是原十题正式三轮，也不评判真实模型语义质量。全部数据库和密钥
显式隔离，真实本机 HTTP/WS 连接的语音上游仅为内存协议对象。
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta
import copy
import hashlib
import http.client
import http.server
import json
from pathlib import Path
import queue
import sys
import threading
import time
import traceback
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
# 引入模块，不把已有 TestCase 导入本模块，避免 discover 重复执行旧 24 项。
import test_cross_module as _cross

from agent_runtime.bank_tools import BankTools
from agent_runtime.conversation import ConversationAgent
from finance_gateway import COOKIE
from market_content import CN, MarketStore, event_input_hash
from market_editor import MarketEditor
from server import ProxyHandler, app_release
from service_negotiation import ServiceNegotiationService
from voice_gateway import SpeechTask, VoiceGateway, speech_text

MANIFEST_PATH = _cross.ROOT / 'evaluation/window_integration/unseen_task_cases.json'
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
MANIFEST_HASH = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
FROZEN_MANIFEST_HASH = '67f3517e13778c4962c365e9c8a4575dd1de9e9084c94fcca44a10e184399472'
CASE_IDS = {case['test']: case['id'] for case in MANIFEST['cases']}
ELECTRONIC = 'SYNTH-E-STATEMENT-FEE-90D'
PAYROLL = 'SYNTH-PAYROLL-FEE-180D'
PENDING = 'submitted_simulation_pending_eligibility'
NEGOTIATION_REQUEST = {
    'text': '现在我可以转入工资。明确拒绝购买理财；请协商额外的账户服务费减免，'
            '列清资格与期限，先不要提交申请。',
    'share_memory': False}


class InMemoryAudioProvider:
    """合成提供器；不建立网络、不存录音、不接受真实提供器凭据。"""
    def __init__(self, *, hold=False):
        self.hold = hold
        self.events = []
        self.responses = queue.Queue()
        self.closed = threading.Event()

    def settimeout(self, value):
        pass

    def send(self, raw):
        value = json.loads(raw)
        self.events.append(value)
        self.task_id = value['header']['task_id']
        action = value['header']['action']
        if action == 'run-task':
            self.kind = value['payload']['task']
            self.emit('task-started')
        elif action == 'finish-task' and not self.hold:
            if self.kind != 'tts':
                raise AssertionError('本套合成提供器只验收 receipt-only TTS')
            self.responses.put(b'\x00\x00\x10\x00' * 100)
            self.emit('task-finished')

    def emit(self, event):
        self.responses.put(json.dumps({'header': {'event': event, 'task_id': self.task_id},
                                      'payload': {}}))

    def recv(self):
        try:
            return self.responses.get(timeout=1)
        except queue.Empty:
            raise TimeoutError() from None

    def close(self, timeout=1):
        self.closed.set()
        self.responses.put('')


class UnpaidEventJsonClient:
    """给真实MarketEditor注入固定的隔离JSON结果，不调用任何模型服务。"""
    billing_mode = 'unpaid_fixture'

    def __init__(self, result, model_id):
        self.result, self.model_id = copy.deepcopy(result), model_id

    def complete_json(self, **kwargs):
        return copy.deepcopy(self.result)


class MarketVoiceNegotiationIntegrationTests(unittest.TestCase):
    maxDiff = None
    # 共用同一隔离实现；只继承其辅助方法，不继承或修改既有测试。
    setUp = _cross.CrossModuleIntegrationTests.setUp
    raw_state = _cross.CrossModuleIntegrationTests.raw_state
    operation_count = _cross.CrossModuleIntegrationTests.operation_count
    assert_bank_error = _cross.CrossModuleIntegrationTests.assert_bank_error
    public_fixture = _cross.CrossModuleIntegrationTests.public_fixture

    def evidence(self, **values):
        test = self.id().split('.')[-1]
        self.assertEqual(MANIFEST_HASH, FROZEN_MANIFEST_HASH, '冻结预期被修改，必须另立新版本')
        self.assertEqual(len(CASE_IDS), 12)
        print('WINDOW_UNSEEN_TASK_EVIDENCE ' + json.dumps({
            'case': CASE_IDS[test], 'test': test, 'manifest_sha256': MANIFEST_HASH,
            'scope': 'temporary_synthetic_current_source', 'formal_ten_questions_three_rounds': False,
            'workspace': self.w, **values}, ensure_ascii=False, sort_keys=True, default=str), flush=True)

    def unchanged(self, before):
        self.assertEqual(self.raw_state(), before)
        self.assertEqual(self.operation_count(), 0)

    def need(self, owner, *names):
        missing = [name for name in names if not callable(getattr(owner, name, None))]
        if missing:
            self.skipTest('当前真实接口尚未实现：' + type(owner).__name__ + '.' + ', '.join(missing))

    def new_market(self):
        self.market = MarketStore(self.directory / 'unseen-market.sqlite3')
        self.g.market = self.market
        self.now = datetime.now(CN).replace(hour=12, minute=30, second=0, microsecond=0)
        self.assertTrue(self.market.path.is_relative_to(self.directory))
        return self.market

    def event_fixture(self):
        """官方域名身份只是准入协议 fixture；正文明确标记合成。"""
        when = (self.now - timedelta(minutes=30)).isoformat()
        article = {
            'id': 'unseen-article', 'source_id': 'nbs', 'publisher': '国家统计局',
            'origin_publisher': '国家统计局', 'title': '隔离合成事件原文',
            'url': 'https://www.stats.gov.cn/sj/zxfb/202610/t20261006_900001.html',
            'published_at': when, 'published_precision': 'minute',
            'checked_at': self.now.isoformat(), 'read_scope': 'full_text',
            'link_status': 'verified', 'evidence_text': '隔离测试合成事实，不能解释为真实发布。',
            'evidence_truncated': False, 'identity': 'official_release',
            'fingerprint': 'unseen-article-v1', 'version': 1}
        self.market.save_article(article)
        article = next(row for row in self.market.articles() if row['id'] == article['id'])
        event = self.issue_event(article, {
            'id': 'unseen-event', 'headline': '隔离合成事件', 'summary': '第一版合成摘要。',
            'analysis': '合成分析的影响仍取决于后续条件。', 'uncertainty': '没有真实行情端点。',
            'facts': [{'text': '隔离测试合成事实', 'article_id': article['id'],
                       'quote': '隔离测试合成事实'}]})
        return article, event

    def issue_event(self, article, draft):
        group = {'id': draft['id'], 'articles': [article]}
        generation = self.market.capture_event_generation([group])
        editor = MarketEditor(
            primary=UnpaidEventJsonClient({'events': [draft]}, 'test-unseen-event-primary'),
            reviewer=UnpaidEventJsonClient({'verdict': 'approve', 'reason': '隔离来源与草稿绑定核对通过。',
                                             'issues': []}, 'test-unseen-event-reviewer'))
        event = editor.events(generation['groups'], 'unseen-event-fixture',
                              generations=generation['generations'])[0]
        event['input_hash'] = event_input_hash(generation['groups'][0])
        self.market._record_event_review(event)
        self.market.publish('market_events', event['id'], event, event['input_hash'])
        return event

    def revise_event(self, event, summary):
        article = next(row for row in self.market.articles() if row['id'] == event['primary_article_id'])
        draft = {key: copy.deepcopy(event[key]) for key in (
            'id', 'headline', 'summary', 'analysis', 'uncertainty', 'facts')}
        draft['summary'] = summary
        return self.issue_event(article, draft)

    def event_reference(self, *, historical=False):
        reference = {'kind': 'event', 'id': 'unseen-event', 'source_scope': 'market', 'version': 1}
        if historical:
            reference['parameters'] = {'edition_date': self.now.date().isoformat(),
                                       'revision': 1, 'event_version': 1}
        return reference

    def approved_receipt(self, text='本轮答复已经核验。理财有风险，可能损失本金。'):
        self.need(self.g, 'approve_speech', 'resolve_speech', 'speech_guard')
        receipt = self.g.approve_speech(self.w, {
            'reply': text, 'compliance_pass': True, 'answer_contract': {'kind': 'reviewed_text'},
            **self.g.session_data(self.w, self.token)})
        self.assertIsInstance(receipt, str)
        return receipt

    @contextmanager
    def local_server(self, *, hold=False, assembled=False):
        try:
            import websocket
            import wsproto
        except ImportError as error:
            self.skipTest('语音本机 WS 验收缺依赖：' + error.name)
        self.providers = []

        def factory(kind, options, key, url):
            if kind != 'tts':
                raise AssertionError('禁止真实 ASR 或自由文字上游调用')
            provider = InMemoryAudioProvider(hold=hold)
            self.providers.append(provider)
            return SpeechTask(kind, options, key, url, connector=lambda *a, **k: provider)

        handler_records, handler_lock = [], threading.Lock()

        class QuietHandler(ProxyHandler):
            def log_message(self, *args):
                pass

            def handle(self):
                record = {'thread': threading.current_thread(), 'done': threading.Event(),
                          'started_clock': time.monotonic(), 'finished_clock': None,
                          'route': None, 'responses': [], 'error_type': None}
                self._fixture_record = record
                with handler_lock:
                    handler_records.append(record)
                try:
                    return super().handle()
                except Exception as error:
                    record['error_type'] = type(error).__name__
                    raise
                finally:
                    path = getattr(self, 'path', '')
                    record['route'] = '/api/speech/ws' if path.startswith('/api/speech/ws') else path.split('?', 1)[0]
                    record['finished_clock'] = time.monotonic()
                    record['done'].set()

            def _finance_json(self, status, value, cookie=None):
                record = getattr(self, '_fixture_record', None)
                if record is not None:
                    record['responses'].append({'status': status,
                        'response_id': value.get('bank_response_id') if isinstance(value, dict) else None,
                        'error_code': value.get('error', {}).get('code') if isinstance(value, dict)
                                      and isinstance(value.get('error'), dict) else None})
                return super()._finance_json(status, value, cookie)

        def handler_snapshot():
            frames = sys._current_frames()
            with handler_lock:
                records = list(handler_records)
            return [{'thread_name': record['thread'].name, 'thread_ident': record['thread'].ident,
                     'alive': record['thread'].is_alive(), 'done': record['done'].is_set(),
                     'route': record['route'], 'started_clock': record['started_clock'],
                     'finished_clock': record['finished_clock'], 'responses': list(record['responses']),
                     'error_type': record['error_type'],
                     'stack': [{'file': str(Path(frame.filename).name), 'line': frame.lineno,
                                'function': frame.name}
                               for frame in traceback.extract_stack(frames[record['thread'].ident])[-12:]]
                              if record['thread'].ident in frames else []}
                    for record in records]

        def wait_handlers(seconds=5):
            deadline = time.monotonic() + seconds
            with handler_lock:
                records = list(handler_records)
            for record in records:
                record['thread'].join(max(0, deadline - time.monotonic()))
            return handler_snapshot()

        voice = VoiceGateway(key='synthetic-not-a-live-credential', url='wss://synthetic.invalid/not-used',
                             task_factory=factory, allow_cloud_asr=False)
        if assembled:
            import os
            import server as server_module
            from base_runtime.service_assembly import AssembledGateway
            with patch.dict(os.environ, {
                    'NAVREN_SIM_DB': str(self.g.bank.database),
                    'NAVREN_KNOWLEDGE_DB': str(self.g.knowledge_provider.path),
                    'NAVREN_FINANCIAL_DB': '',
                    'NAVREN_PUBLIC_SYNC_ENABLED': '0',
                    'NAVREN_MESSAGE_WORKER_ENABLED': '0'}), \
                    patch.object(server_module, 'ProxyHandler', QuietHandler):
                server = server_module.create_server(port=0, database=str(self.g.bank.database),
                    voice=voice, market_database=str(self.directory / 'assembled-market.sqlite3'),
                    public_sync_enabled=False)
            if not isinstance(server.finance, AssembledGateway):
                server.server_close()
                raise AssertionError('assembled_service_factory_not_used')
        else:
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
            server.finance, server.voice, server.app_release = self.g, voice, app_release()
            server.market = getattr(self.g, 'market', None)
        # 正式/api/agent必须有EvidenceRuntime；仅临时公共库与拒绝网络的reader。
        from local_knowledge.runtime import EvidenceRuntime
        def forbidden_reader(*args, **kwargs):
            raise AssertionError('public_network_reader_forbidden')
        if assembled:
            server.evidence_runtime.close()
        runtime = EvidenceRuntime(server.finance.knowledge_provider, background=False, reader=forbidden_reader)
        server.evidence_runtime = runtime
        server.fixture_handler_snapshot = handler_snapshot
        server.fixture_wait_handlers = wait_handlers
        self.server, self.base = server, f'http://127.0.0.1:{server.server_port}'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            remaining = wait_handlers(5)
            server.server_close()
            runtime.close()
            print('WINDOW_FIXTURE_HANDLER_SUMMARY ' + json.dumps({'test': self._testMethodName,
                'handlers': handler_snapshot(), 'runtime_closed': runtime.closed,
                'external_reader': 'forbidden'}, ensure_ascii=False), flush=True)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), '本机测试服务器未清理')
            self.assertFalse(any(record['alive'] for record in remaining),
                             '本机请求handler未在临时库/patch退出前有界清理')

    def http_json(self, method, path, body=None, *, token=None):
        token = token or self.token
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        headers = {'Content-Type': 'application/json', 'Cookie': COOKIE + '=' + token,
                   'X-QZ-CSRF': self.g.csrf(token), 'Origin': self.base}
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None,
                               headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def connect_ticket(self, path, *, token=None):
        from websocket import create_connection
        return create_connection(self.base.replace('http:', 'ws:') + path,
                                 origin=self.base, cookie=COOKIE + '=' + (token or self.token), timeout=3)

    def temporary_rules(self, transform):
        rules = copy.deepcopy(self.g.service_negotiations.rules())
        transform(rules)
        path = self.directory / 'service-benefits.json'
        path.write_text(json.dumps(rules, ensure_ascii=False), encoding='utf-8')
        return ServiceNegotiationService(self.g, path)

    def test_daily_reference_keeps_date_revision_and_event_version(self):
        before = self.raw_state()
        market = self.new_market()
        self.need(market, 'history_event', 'update_daily')
        _, event = self.event_fixture()
        edition = market.update_daily(run_id='unseen-initial', valid_check=True, now=self.now)
        self.assertEqual(edition['revision'], 1)
        reference = self.event_reference(historical=True)
        resolved = self.g.page_contexts.resolve(self.w, reference)
        tools = BankTools(self.g, self.w, 'unseen-daily')
        self.need(tools, 'bind_market_context', 'market_snapshot')
        tools.bind_market_context(resolved)
        output = tools.invoke('market_event', {'event_id': event['id']})['data']
        self.assertEqual(output['archive']['edition_date'], reference['parameters']['edition_date'])
        self.assertEqual(output['archive']['revision'], 1)
        self.assertEqual(output['version'], 1)
        reads = tools.market_snapshot()['reads']
        self.assertEqual(reads[0]['arguments'], {'event_id': event['id'], **reference['parameters']})
        rejected = []
        for parameters in (
                {'edition_date': reference['parameters']['edition_date']},
                {**reference['parameters'], 'revision': 999},
                {**reference['parameters'], 'event_version': 2},
                {**reference['parameters'], 'edition_date': (self.now.date() + timedelta(days=1)).isoformat()}):
            with self.subTest(parameters=parameters):
                rejected.append(self.assert_bank_error(None, lambda: self.g.page_contexts.resolve(
                    self.w, {**reference, 'parameters': parameters})))
        self.assert_bank_error('INVALID_TOOL_ARGUMENTS', lambda: tools.invoke('market_event', {
            'event_id': event['id'], 'edition_date': reference['parameters']['edition_date']}))
        self.revise_event(event, '第二版合成摘要。')
        old = market.history_event(reference['parameters']['edition_date'], event['id'], 1,
                                   now=self.now, expected_version=1)
        self.assertEqual(old['version'], 1)
        self.assertEqual(old['archive']['revision'], 1)
        self.assertEqual(old['archive']['position'], 1)
        self.assertEqual(old['latest_pointer']['version'], 2)
        self.assertEqual(old['availability'], 'revised')
        self.assertEqual(old['summary'], '')
        self.assertEqual(old['facts'], [])
        self.assert_bank_error('STALE_MARKET_EVIDENCE', tools.fresh)
        self.assert_bank_error('MARKET_ARCHIVE_UNAVAILABLE', lambda: self.g.page_contexts.resolve(self.w, reference))
        self.unchanged(before)
        self.evidence(parameters=reference['parameters'], old_event_version=old['version'],
                      latest_event_version=old['latest_pointer']['version'], availability=old['availability'],
                      rejected_identity_codes=rejected, operations=0, cash_unchanged=True)

    def test_weekly_reference_pins_period_and_supporting_event_version(self):
        before = self.raw_state()
        market = self.new_market()
        self.need(market, 'weekly')
        article, event = self.event_fixture()
        period = '2026-09-28'
        report = {'period': period, 'title': '指定期间的隔离合成周报', 'summary': '合成周报摘要。',
                  'article_ids': [article['id']], 'article_versions': {article['id']: 1},
                  'citations': [{'event_id': event['id'], 'event_version': 1}],
                  'review': {'status': 'approved'}, 'limitations': ['只有合成证据，没有行情端点。']}
        market.publish('market_weeklies', period, report, 'unseen-weekly-v1')
        reference = self.g.page_contexts.resolve(self.w, {
            'kind': 'weekly', 'id': period, 'source_scope': 'market', 'version': 1})
        tools = BankTools(self.g, self.w, 'unseen-weekly')
        self.need(tools, 'bind_market_context', 'market_snapshot')
        tools.bind_market_context(reference)
        found = tools.invoke('market_weekly', {})['data']
        self.assertEqual(found['report']['period'], period)
        self.assertEqual(found['report']['version'], 1)
        self.assertEqual([(e['id'], e['version']) for e in found['supporting_events']], [(event['id'], 1)])
        snapshot = tools.market_snapshot()
        self.assertEqual(snapshot['reads'][0]['arguments'], {'period': period})
        self.assert_bank_error('MARKET_CONTEXT_CONFLICT', lambda: tools.invoke('market_weekly', {'period': 'latest'}))
        self.revise_event(event, '第二版合成摘要。')
        self.assertEqual(market.weekly(period)['report']['citations'][0]['event_version'], 1)
        self.assert_bank_error('STALE_MARKET_EVIDENCE', tools.fresh)
        self.assert_bank_error('STALE_MARKET_EVIDENCE', lambda: BankTools(self.g, self.w, 'unseen-weekly-new').invoke(
            'market_weekly', {'period': period}))
        self.unchanged(before)
        self.evidence(period=period, weekly_version=1, cited_event_version=1, current_event_version=2,
                      source_snapshot=snapshot, stale_code='STALE_MARKET_EVIDENCE', operations=0)

    def test_market_revision_during_review_withholds_answer(self):
        before = self.raw_state()
        self.new_market()
        article, event = self.event_fixture()
        reviewer = _cross.ScriptedReviewer(hook=lambda: self.market.save_article({
            **article, 'fingerprint': 'unseen-during-review-v2', 'evidence_text': '修订后的合成事实。'}))
        primary = _cross.ScriptedPrimary([_cross.call('market_event', {'event_id': event['id']}),
                                         _cross.reviewed_answer])
        result = ConversationAgent(primary, reviewer).process(
            self.g, self.w, '解释选中事件的事实、条件和不确定性，审核后再展示。',
            market_context=self.g.page_contexts.resolve(self.w, self.event_reference()))
        self.assertEqual(len(reviewer.packets), 1)
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'], 'STALE_MARKET_EVIDENCE')
        self.assertEqual(result['bank_actions'], [])
        self.assertEqual(result['market_sources'], [])
        self.assertIsNone(result['market_snapshot'])
        self.assertIsNone(self.g.approve_speech(self.w, result))
        self.assertNotIn('这是本轮有原文依据的分析。', result['reply'])
        self.assertEqual(self.market.article(article['id'])['version'], 2)
        self.unchanged(before)
        self.evidence(review_calls=1, article_version=2, failure=result['error']['code'],
                      withheld=['old_answer', 'market_sources', 'speech', 'bank_actions'], operations=0)

    def test_public_withdrawal_during_review_withholds_answer_and_speech(self):
        before = self.raw_state()
        version = self.public_fixture()
        # 云端 packet 会投影内部 release 标识；本地撤回解析用隔离原始 witness。
        local_found = BankTools(self.g, self.w, 'unseen-withdrawal-witness').invoke('knowledge_search', {
            'query': '风险', 'product_id': 'WI-P', 'as_of': '2026-10-06'})['data']
        self.assertTrue(local_found['hits'])
        reviewer = _cross.ScriptedReviewer(hook=lambda: self.g.knowledge_provider.withdraw(version))
        primary = _cross.ScriptedPrimary([_cross.call('knowledge_search', {
            'query': '风险', 'product_id': 'WI-P', 'as_of': '2026-10-06'}), _cross.reviewed_answer])
        result = ConversationAgent(primary, reviewer).process(
            self.g, self.w, '请根据本轮原文解释本金风险，先不交易；审核后才允许朗读。')
        self.assertEqual(len(reviewer.packets), 1)
        self.assertTrue(result['isFallback'])
        self.assertEqual(result['error']['code'], 'STALE_KNOWLEDGE_EVIDENCE')
        self.assertEqual(result['bank_actions'], [])
        self.assertIsNone(self.g.approve_speech(self.w, result))
        self.assertNotIn('这是本轮有原文依据的分析。', result['reply'])
        found = next(json.loads(m['content'])['data'] for m in primary.sent[-1]
                     if m['role'] == 'tool' and 'hits' in json.loads(m['content']).get('data', {}))
        self.assertTrue(found['hits'])
        self.assertIsNone(self.g.knowledge_provider.resolve_public_evidence(
            local_found['hits'][0]['citation_id'], local_found['public_release_id'], local_found['as_of']))
        self.unchanged(before)
        self.evidence(review_calls=1, withdrawn_version=version, failure=result['error']['code'],
                      invalid_citation=local_found['hits'][0]['citation_id'], speech_receipt_issued=False, operations=0)

    def test_receipt_only_tts_ignores_client_substitution(self):
        before = self.raw_state()
        text = '余额：￥12,345.67，收益率3.5%。' + ('完整说明。' * 130) + '理财非存款，可能损失本金。'
        receipt = self.approved_receipt(text)
        approved_text = self.g.resolve_speech(self.w, receipt)
        with self.local_server():
            status, rejected = self.http_json('POST', '/api/speech/session', {
                'kind': 'tts', 'speech_receipt': receipt, 'text': '替代正文'})
            self.assertEqual(status, 400)
            self.assertEqual(rejected['error']['code'], 'INVALID_SPEECH_REQUEST')
            self.assertEqual(self.providers, [])
            status, session = self.http_json('POST', '/api/speech/session', {'kind': 'tts', 'speech_receipt': receipt})
            self.assertEqual(status, 200)
            ws = self.connect_ticket(session['path'])
            try:
                ws.send(json.dumps({'type': 'start', 'text': '浏览器替换正文，免除风险', 'voice': 'male_warm'}))
                ready = json.loads(ws.recv())
                audio = ws.recv()
                done = json.loads(ws.recv())
                self.assertEqual(ready['type'], 'ready')
                self.assertEqual(ready['sample_rate'], 24000)
                self.assertIsInstance(audio, bytes)
                self.assertGreater(len(audio), 0)
                self.assertEqual(done['type'], 'done')
                self.assertEqual(len(self.providers), 1)
                combined = ''.join(e['payload']['input']['text'] for e in self.providers[0].events
                                   if e['header']['action'] == 'continue-task')
                self.assertEqual(combined, speech_text(approved_text))
                self.assertIn('12345.67元', combined)
                self.assertIn('百分之3.5', combined)
                self.assertIn('可能损失本金', combined)
                self.assertGreater(len(combined), 500)
                self.assertNotIn('浏览器替换正文', combined)
            finally:
                ws.close()
            status, state = self.http_json('GET', '/api/speech/status')
            self.assertEqual(status, 200)
            self.assertTrue(state['configured'])
            self.assertFalse(state['asr_available'])
            self.assertNotIn('synthetic-not-a-live-credential', json.dumps(state))
        self.unchanged(before)
        self.evidence(transport='actual_loopback_http_ws', upstream='synthetic_in_memory',
                      events=[ready['type'], done['type']], pcm_bytes=len(audio),
                      approved_chars=len(combined), disclosure_preserved=True, client_substitution_rejected=True)

    def test_cancel_stops_tts_upstream_and_releases_slots(self):
        before = self.raw_state()
        receipt = self.approved_receipt()
        with self.local_server(hold=True) as server:
            status, session = self.http_json('POST', '/api/speech/session', {'kind': 'tts', 'speech_receipt': receipt})
            self.assertEqual(status, 200)
            ws = self.connect_ticket(session['path'])
            try:
                ws.send(json.dumps({'type': 'start'}))
                self.assertEqual(json.loads(ws.recv())['type'], 'ready')
                self.assertEqual(len(self.providers), 1)
                provider = self.providers[0]
                self.assertFalse(provider.closed.is_set())
                ws.send(json.dumps({'type': 'cancel'}))
                # Close 帧和 semaphore 释放相邻；允许 finally 完成，避免帧先到的竞态。
                # 当前 pump 可能将取消导致的断连报告为异常；保留观察而不伪称干净 UI。
                after_cancel = []
                for _ in range(3):
                    frame = ws.recv()
                    if frame == '':
                        break
                    self.assertIsInstance(frame, str)
                    event = json.loads(frame)
                    self.assertEqual(event['type'], 'error')
                    self.assertEqual(event['code'], 'SPEECH_DISCONNECTED')
                    after_cancel.append(event['code'])
                self.assertEqual(frame, '', '取消后 WS 没有在有界帧数内关闭')
                self.assertTrue(provider.closed.wait(timeout=2))
                acquired = 0
                try:
                    for _ in range(3):
                        self.assertTrue(server.voice.slots.acquire(timeout=2))
                        acquired += 1
                    self.assertFalse(server.voice.slots.acquire(blocking=False))
                finally:
                    for _ in range(acquired):
                        server.voice.slots.release()
            finally:
                ws.close()
        self.unchanged(before)
        self.evidence(transport='actual_loopback_ws_cancel', provider_closed=provider.closed.is_set(),
                      returned_slots=acquired, after_cancel_events=after_cancel,
                      asr_cloud_allowed=False, cash_unchanged=True)

    def test_workspace_change_cannot_reuse_speech_receipt_or_ticket(self):
        from websocket import WebSocketBadStatusException
        before = self.raw_state()
        other, other_token = self.g.create_session()
        self.assertNotEqual(other, self.w, '独立临时工作区不可用，不能冒充跨账户验收')
        other_before = self.raw_state(other)
        receipt = self.approved_receipt()
        self.assert_bank_error('SPEECH_RECEIPT_INVALID', lambda: self.g.resolve_speech(other, receipt))
        with self.local_server():
            status, denied = self.http_json('POST', '/api/speech/session', {
                'kind': 'tts', 'speech_receipt': receipt}, token=other_token)
            self.assertEqual(status, 403)
            self.assertEqual(denied['error']['code'], 'SPEECH_RECEIPT_INVALID')
            status, session = self.http_json('POST', '/api/speech/session', {'kind': 'tts', 'speech_receipt': receipt})
            self.assertEqual(status, 200)
            with self.assertRaises(WebSocketBadStatusException) as caught:
                self.connect_ticket(session['path'], token=other_token)
            self.assertEqual(caught.exception.status_code, 403)
            self.assertEqual(self.providers, [])
        self.unchanged(before)
        self.assertEqual(self.raw_state(other), other_before)
        self.evidence(other_workspace=other, receipt_http_status=403, ticket_ws_status=403,
                      provider_tasks=0, verified_scope='backend_workspace_identity_only', cash_unchanged=True)

    def test_privacy_change_invalidates_queued_speech_before_provider(self):
        before = self.raw_state()
        receipt = self.approved_receipt()
        revision_before = self.g.personal_data.revision(self.w)
        with self.local_server() as server:
            status, session = self.http_json('POST', '/api/speech/session', {'kind': 'tts', 'speech_receipt': receipt})
            self.assertEqual(status, 200)
            self.g.memories.create(self.w, {'category': 'goal', 'value': '用户主动修订的隔离储蓄目标'})
            self.assertNotEqual(self.g.personal_data.revision(self.w), revision_before)
            # 首次失效由真实WS检查；预先resolve会撤销receipt并把它变成重放。
            with self.g._speech_lock:
                self.assertIn(receipt, self.g._speech_receipts)
            ws = self.connect_ticket(session['path'])
            events, pcm, closed = [], [], False
            try:
                ws.send(json.dumps({'type': 'start'}))
                for _ in range(4):
                    frame = ws.recv()
                    if frame == '':
                        closed = True
                        break
                    if isinstance(frame, bytes):
                        pcm.append(frame)
                    else:
                        events.append(json.loads(frame))
                self.assertTrue(closed, '首次版本拒绝后WS必须有界关闭，不能漏读后续音频')
                self.assertEqual(sum(map(len, pcm)), 0)
                self.assertEqual([event['type'] for event in events], ['error'])
                error = events[0]
                self.assertEqual(error['type'], 'error')
                self.assertEqual(error['code'], 'PERSONAL_VERSION_CONFLICT')
                self.assertEqual(self.providers, [])
            finally:
                ws.close()
            acquired = 0
            try:
                for _ in range(3):
                    self.assertTrue(server.voice.slots.acquire(timeout=2))
                    acquired += 1
                self.assertFalse(server.voice.slots.acquire(blocking=False))
            finally:
                for _ in range(acquired):
                    server.voice.slots.release()
            with self.g._speech_lock:
                revoked = receipt not in self.g._speech_receipts
            self.assertTrue(revoked)
            # 首次WS已撤权之后，直接读取和申请新ticket均是明确的重放子情境。
            self.assert_bank_error('SPEECH_RECEIPT_INVALID', lambda: self.g.resolve_speech(self.w, receipt))
            replay_status, replay = self.http_json('POST', '/api/speech/session',
                {'kind': 'tts', 'speech_receipt': receipt})
            self.assertEqual(replay_status, 403)
            self.assertEqual(replay['error']['code'], 'SPEECH_RECEIPT_INVALID')
            self.assertEqual(self.providers, [])
        self.unchanged(before)
        self.evidence(old_personal_revision=revision_before,
                      current_personal_revision=self.g.personal_data.revision(self.w),
                      ws_failure=error['code'], event_types=[event['type'] for event in events],
                      pcm_bytes=sum(map(len, pcm)), ws_closed=closed, returned_slots=acquired,
                      receipt_revoked=revoked, replay_status=replay_status,
                      replay_error=replay['error']['code'], provider_tasks=0, cash_unchanged=True)

    def test_negotiation_respects_floor_and_exposes_pending_salary_eligibility(self):
        before = self.raw_state()
        self.need(self.g.service_negotiations, 'preview', 'applications')
        def with_inducement(rules):
            rule = {**rules['offers'][1], 'offer_id': 'SYNTH-INDUCEMENT-REJECTED',
                    'title': '隔离诱导理财条件方案', 'requires_investment_purchase': True,
                    'conditions': ['申请前必须购买合成理财产品']}
            rules['offers'].append(rule)
        service = self.temporary_rules(with_inducement)
        result = service.preview(self.w, NEGOTIATION_REQUEST)
        self.assertEqual(result['status'], 'previewed')
        self.assertTrue(result['conditions']['salary_deposit_intent'])
        self.assertTrue(result['conditions']['investment_purchase_refused'])
        self.assertEqual({r['offer_id'] for r in result['offers']}, {ELECTRONIC, PAYROLL})
        salary = next(r for r in result['offers'] if r['offer_id'] == PAYROLL)
        self.assertEqual(salary['minimum_monthly_salary_minor'], 600000)
        self.assertEqual(salary['salary_months_required'], 3)
        self.assertEqual(salary['eligibility_status'], 'conditional_application')
        self.assertIn('待', salary['status_label'])
        for offer in result['offers']:
            self.assertFalse(offer['requires_investment_purchase'])
            self.assertFalse(offer['investment_purchase_required'])
            self.assertEqual(offer['review_status'], 'rules_checked')
            self.assertEqual(offer['monthly_value_minor'] * offer['duration_months'], offer['max_cost_minor'])
            self.assertEqual(offer['application_status_on_accept'], PENDING)
            self.assertLessEqual(offer['valid_from'], self.initial['as_of'])
            self.assertGreaterEqual(offer['valid_through'], self.initial['as_of'])
        self.assertIn('合成数据', result['notice'])
        self.assertEqual(service.applications(self.w)['applications'], [])
        self.unchanged(before)
        self.evidence(draft_id=result['draft_id'], offers=[r['offer_id'] for r in result['offers']],
                      rejected_offer='SYNTH-INDUCEMENT-REJECTED',
                      salary_minor=salary['minimum_monthly_salary_minor'], salary_months=salary['salary_months_required'],
                      eligibility=salary['eligibility_status'], application_count=0, cash_unchanged=True)

    def test_modified_negotiation_supersedes_draft_and_rejects_excluded_offer(self):
        before = self.raw_state()
        service = self.g.service_negotiations
        self.need(service, 'preview', 'accept', 'applications')
        old = service.preview(self.w, NEGOTIATION_REQUEST)
        new = service.preview(self.w, {
            'text': '工资账户保持原状。现在只要电子账单服务费减免，我拒绝购买理财。',
            'share_memory': False})
        self.assertFalse(new['conditions']['salary_deposit_intent'])
        self.assertEqual([r['offer_id'] for r in new['offers']], [ELECTRONIC])
        stale = self.assert_bank_error('SERVICE_DRAFT_STALE', lambda: service.accept(
            self.w, old['draft_id'], {'offer_id': ELECTRONIC, 'confirmed': True}))
        excluded = self.assert_bank_error('SERVICE_OFFER_NOT_ELIGIBLE', lambda: service.accept(
            self.w, new['draft_id'], {'offer_id': PAYROLL, 'confirmed': True}))
        with closing(self.g.bank.connect()) as db:
            state = db.execute('SELECT status FROM service_negotiation_drafts WHERE id=?',
                               (old['draft_id'],)).fetchone()[0]
        self.assertEqual(state, 'superseded')
        self.assertEqual(service.applications(self.w)['applications'], [])
        self.unchanged(before)
        self.evidence(old_draft_id=old['draft_id'], new_draft_id=new['draft_id'], old_status=state,
                      rejection_codes=[stale, excluded], application_count=0, cash_unchanged=True)

    def test_expired_negotiation_rules_produce_no_offer_or_application(self):
        before = self.raw_state()
        def expire(rules):
            for offer in rules['offers']:
                offer['valid_from'] = '2026-09-01'
                offer['valid_through'] = '2026-09-30'
        service = self.temporary_rules(expire)
        result = service.preview(self.w, NEGOTIATION_REQUEST)
        self.assertEqual(result['status'], 'no_offer')
        self.assertEqual(result['offers'], [])
        self.assertIsNone(result['draft_id'])
        self.assertTrue(result['conditions']['investment_purchase_refused'])
        self.assertEqual(service.applications(self.w)['applications'], [])
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM service_negotiation_drafts WHERE workspace_id=?',
                                        (self.w,)).fetchone()[0], 0)
        self.unchanged(before)
        self.evidence(status=result['status'], rule_valid_through='2026-09-30', bank_as_of=self.initial['as_of'],
                      offer_count=0, draft_count=0, application_count=0, cash_unchanged=True)

    def test_negotiation_explicit_accept_and_replay_have_single_simulation_receipt(self):
        before = self.raw_state()
        other, other_token = self.g.create_session()
        self.assertNotEqual(other, self.w)
        other_before = self.raw_state(other)
        self.need(self.g.service_negotiations, 'preview', 'accept', 'applications')
        with self.local_server():
            status, preview = self.http_json('POST', '/api/finance/service-negotiations/preview', NEGOTIATION_REQUEST)
            self.assertEqual(status, 200)
            path = '/api/finance/service-negotiations/' + preview['draft_id'] + '/accept'
            status, rejected = self.http_json('POST', path, {'offer_id': PAYROLL, 'confirmed': False})
            self.assertEqual(status, 400)
            self.assertEqual(rejected['error']['code'], 'CONFIRMATION_REQUIRED')
            status, outsider = self.http_json('POST', path, {'offer_id': PAYROLL, 'confirmed': True}, token=other_token)
            self.assertEqual(status, 404)
            self.assertEqual(outsider['error']['code'], 'SERVICE_DRAFT_NOT_FOUND')
            status, accepted = self.http_json('POST', path, {'offer_id': PAYROLL, 'confirmed': True})
            self.assertEqual(status, 200)
            status, replay = self.http_json('POST', path, {'offer_id': PAYROLL, 'confirmed': True})
            self.assertEqual(status, 200)
            status, listed = self.http_json('GET', '/api/finance/service-negotiations')
            self.assertEqual(status, 200)
            self.assertEqual(accepted['application_id'], replay['application_id'])
            self.assertFalse(accepted['idempotent_replay'])
            self.assertTrue(replay['idempotent_replay'])
            self.assertEqual(accepted['status'], PENDING)
            self.assertEqual(replay['status'], PENDING)
            self.assertEqual(len(listed['applications']), 1)
            self.assertEqual(listed['applications'][0]['application_id'], accepted['application_id'])
            self.assertFalse(accepted['terms']['requires_investment_purchase'])
            self.assertEqual(accepted['terms']['minimum_monthly_salary_minor'], 600000)
            self.assertIn('资格仍待', accepted['notice'])
        audit = [r for r in self.g.bank.audit(self.w) if r['event'] == 'service_negotiation_application_submitted']
        self.assertEqual(len(audit), 1)
        self.assertEqual(self.g.service_negotiations.applications(other)['applications'], [])
        self.unchanged(before)
        self.assertEqual(self.raw_state(other), other_before)
        self.evidence(transport='actual_loopback_http', draft_id=preview['draft_id'],
                      application_id=accepted['application_id'], status=accepted['status'],
                      application_count=1, submission_audit_count=len(audit), replay=True,
                      denied_codes=[rejected['error']['code'], outsider['error']['code']],
                      financial_operations=0, cash_unchanged=True)


if __name__ == '__main__':
    unittest.main()
