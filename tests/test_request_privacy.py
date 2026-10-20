"""真实本地宿主的禁止资料边界；不调用云模型或读取钥匙串。"""
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from agent_runtime.bank_tools import BankTools
from agent_runtime.complex_agent import ComplexConversationAgent
from agent_runtime.conversation import ConversationAgent, GLMConversationReviewer
from agent_runtime.request_privacy import RequestPrivacyPolicy
from finance_gateway import FinanceGateway
from simulated_bank import BankError


def tool(name, args):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': 'call-' + name, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}


def finish_hypothesis(messages, schemas):
    outputs = [json.loads(item['content']) for item in messages if item['role'] == 'tool']
    facts = [fact['id'] for output in outputs for fact in output.get('facts', [])
             if fact.get('value') == 12345 and fact.get('currency') == 'CNY']
    if not facts:
        raise AssertionError('hypothesis must consume the real local 12345 fact')
    return tool('answer_facts', {'fact_ids': facts})


class Primary:
    model_id = 'offline-privacy-script'
    billing_mode = 'unpaid_fixture'

    def __init__(self, *steps):
        self.steps = list(steps)
        self.messages, self.schemas = [], []

    def complete(self, messages, schemas, **kwargs):
        self.messages.append(deepcopy(messages))
        self.schemas.append(deepcopy(schemas))
        step = self.steps.pop(0)
        return (step(messages, schemas) if callable(step) else step), {
            'prompt_tokens': 1, 'completion_tokens': 0, 'total_tokens': 1}


class NeverReviewer:
    model_id = 'offline-never-reviewer'
    billing_mode = 'unpaid_fixture'

    def review(self, *args, **kwargs):
        raise AssertionError('hypothesis fact path must not need independent review')


class Judge:
    """可故意尝试选私人技能；真实宿主限制不能由标签解除。"""
    def __init__(self, provider):
        self.provider, self.states = provider, []

    def evaluate(self, state, questions, *, deadline=None):
        self.states.append(deepcopy(state))
        selected = {'cap_profile'}
        answers = {key: ({'value': 'yes' if key in selected else 'no'}
                         if self.provider == 'native' else
                         {'type': 'noul', 'noul': .99 if key in selected else .01}) for key in questions}
        return answers, {'request_sent': True, 'outcome': 'completed',
                         'usage': {'input_tokens': 1, 'output_tokens': 0}}


class RequestPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {
            'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB': str(root/'knowledge.sqlite3'),
            'NAVREN_FINANCIAL_DB': '', 'NAVREN_PUBLIC_SYNC_ENABLED': 'false',
        })
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.g = FinanceGateway(root/'bank.sqlite3')
        self.w, _ = self.g.create_session()

    def forbidden_private_reads(self, primary):
        """Getter spies plus real SQLite authorizer catch hidden account hashes."""
        stack = ExitStack()
        for owner, name in ((self.g.bank, 'customer_archive'), (self.g.wealth, 'provision'),
                            (self.g.memories, 'state'), (self.g.personal_data, 'read_widgets'),
                            (self.g.personal_data, 'register_conversation_source'),
                            (self.g.calendar, 'clock'), (self.g.financial_metrics, 'metrics_for_tool')):
            stack.enter_context(patch.object(owner, name, side_effect=AssertionError('private getter: ' + name)))
        original_history = self.g.personal_data.read_history
        def history_only_after_models(workspace):
            if primary.steps:
                raise AssertionError('old visible history read before final local append')
            return original_history(workspace)
        stack.enter_context(patch.object(self.g.personal_data, 'read_history', side_effect=history_only_after_models))
        original_connect = self.g.bank.connect
        self.denied_sql_reads = []
        private_tables = {'accounts', 'profiles', 'transactions', 'mandates', 'holdings', 'orders',
                          'operations', 'calendar_series', 'calendar_event_drafts',
                          'portfolio_positions', 'portfolio_versions', 'portfolio_valuations',
                          'wealth_valuation_points', 'personal_decisions'}
        def guarded_connect():
            db = original_connect()
            def authorize(action, table, column, database, trigger):
                if action == sqlite3.SQLITE_READ and table in private_tables:
                    self.denied_sql_reads.append((table, column))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            return db
        stack.enter_context(patch.object(self.g.bank, 'connect', side_effect=guarded_connect))
        return stack

    def primary(self, *, malicious=False):
        steps = [tool('money_calculation', {'operation': 'sum', 'values_minor': [12345], 'currency': 'CNY'}),
                 finish_hypothesis]
        if malicious: steps.insert(0, tool('account_summary', {'private_context_disabled': False}))
        return Primary(*steps)

    def assert_inputs_restricted(self, primary):
        business = next(json.loads(item['content']) for item in primary.messages[0]
                        if item['role'] == 'user' and 'current_request' in json.loads(item['content']))
        for key in ('recent_operations', 'calendar_events', 'calendar_draft_receipts',
                    'recent_calendar_widgets', 'graph_context', 'page_context'):
            self.assertNotIn(key, business['server_context'])
        wire = json.dumps(primary.messages, ensure_ascii=False)
        self.assertNotIn('OLD_PRIVATE_BALANCE_99988765', wire)
        for schemas in primary.schemas:
            self.assertTrue(all(RequestPrivacyPolicy(True).permits(schema['function']['name']) for schema in schemas))
        self.assertEqual(self.denied_sql_reads, [])

    def test_explicit_denials_cover_words_order_and_multiple_private_domains(self):
        for text in ('只计算假设的123.45元，不读取个人账户。',
                     '不要使用我的财务画像，请解释概念。', '我的账户不要读取。',
                     '不使用私人资料。', '不要读取或使用本人的账户；只算假设。',
                     '不用本人账户或画像。', '别参考我的风险画像。', '不查账单。',
                     'without using my personal data'):
            with self.subTest(text=text):
                self.assertTrue(RequestPrivacyPolicy.from_text(text).private_context_disabled)
        for text in ('查我的账户余额', '查公开产品的风险', '解释个人账户的概念'):
            self.assertFalse(RequestPrivacyPolicy.from_text(text).private_context_disabled)

    def test_original_x01_full_and_progressive_skip_private_initialization_and_keep_history_append(self):
        old = [{'role': 'user', 'content': 'OLD_PRIVATE_BALANCE_99988765'},
               {'role': 'assistant', 'content': '旧可见对话'}]
        for mode in ('full', 'progressive'):
            with self.subTest(mode=mode):
                self.g.personal_data.save_history(self.w, old)
                primary = self.primary()
                with self.forbidden_private_reads(primary):
                    result = ConversationAgent(primary, NeverReviewer(), tool_routing=mode).process(
                        self.g, self.w, '只计算假设的123.45元，不读取个人账户。')
                self.assertFalse(result.get('isFallback'), result)
                self.assertEqual(result['answer_contract']['kind'], 'verified_facts')
                self.assertIn('123.45', result['reply'])
                self.assertIsNone(result['_answer_context'])
                self.assert_inputs_restricted(primary)
                saved = self.g.personal_data.read_history(self.w)
                self.assertEqual(saved[:2], old)
                self.assertEqual(len(saved), 4)

    def test_three_arms_cannot_restore_private_data_from_model_route_labels_or_parameters(self):
        for provider in ('off', 'native', 'jev'):
            with self.subTest(provider=provider):
                self.g.personal_data.save_history(self.w, [{'role': 'user', 'content': 'OLD_PRIVATE_BALANCE_99988765'}])
                primary, judge = self.primary(malicious=True), Judge(provider)
                with self.forbidden_private_reads(primary):
                    result = ComplexConversationAgent(primary=primary, reviewer=NeverReviewer(),
                        route_provider=provider, judge=judge).process(
                            self.g, self.w, '只计算假设的123.45元，不读取个人账户。')
                self.assertFalse(result.get('isFallback'), result)
                errors = [json.loads(item['content']).get('error', {}).get('code')
                          for item in primary.messages[-1] if item['role'] == 'tool']
                self.assertIn('REQUEST_PRIVATE_DATA_FORBIDDEN', errors)
                self.assert_inputs_restricted(primary)
                self.assertNotIn('OLD_PRIVATE_BALANCE_99988765', json.dumps(judge.states, ensure_ascii=False))
                if provider != 'off': self.assertEqual(len(judge.states), 1)

    def test_direct_backend_rejects_private_write_and_financial_tools_before_validation(self):
        primary = Primary()
        with self.forbidden_private_reads(primary):
            tools = BankTools(self.g, self.w, 'private-denied', private_context_disabled=True)
            for name in ('account_summary', 'budget', 'customer_records', 'user_memory',
                         'financial_list_transfers', 'calendar_query', 'service_offer_preview',
                         'remember_profile', 'prepare_operation', 'withdraw_draft'):
                with self.subTest(name=name), self.assertRaises(BankError) as caught:
                    tools.invoke(name, {'private_context_disabled': False})
                self.assertEqual(caught.exception.code, 'REQUEST_PRIVATE_DATA_FORBIDDEN')
            tools.fresh()
        self.assertEqual(self.denied_sql_reads, [])

    def test_confirmed_private_skill_preload_does_not_unlock_private_context_or_schema(self):
        for provider in ('native', 'jev'):
            with self.subTest(provider=provider):
                primary, judge = self.primary(malicious=True), Judge(provider)
                with self.forbidden_private_reads(primary):
                    result = ComplexConversationAgent(primary=primary, reviewer=NeverReviewer(),
                        route_provider=provider, judge=judge).process(
                            self.g, self.w, '不使用我的账户或画像，计算123.45元。')
                self.assertFalse(result.get('isFallback'), result)
                self.assertEqual(result['agent_runtime']['intent_route']['selection']['skills'], ['profile'])
                self.assert_inputs_restricted(primary)

    def test_restriction_stays_active_for_mixed_read_denial_and_write_request(self):
        primary = self.primary()
        with self.forbidden_private_reads(primary):
            result = ConversationAgent(primary, NeverReviewer()).process(self.g, self.w,
                '不使用我的账户或画像。只计算假设的123.45元；还请转账给朋友。',
                graph_context={'node_id': 'private-node'},
                page_context={'kind': 'account', 'source_scope': 'simulation', 'id': 'private-account'})
        self.assertFalse(result.get('isFallback'), result)
        self.assert_inputs_restricted(primary)

    def test_authorization_revision_change_still_blocks_restricted_turn(self):
        primary = Primary()
        with self.forbidden_private_reads(primary):
            tools = BankTools(self.g, self.w, 'metadata-stale', private_context_disabled=True)
            with self.g.bank.transaction() as db:
                db.execute('CREATE TABLE IF NOT EXISTS conversation_privacy_versions(workspace_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)')
                db.execute('INSERT INTO conversation_privacy_versions VALUES(?,1) ON CONFLICT(workspace_id) DO UPDATE SET revision=revision+1', (self.w,))
            with self.assertRaises(BankError) as caught: tools.fresh()
        self.assertEqual(caught.exception.code, 'STALE_EVIDENCE')

    def test_current_request_pii_is_masked_without_private_archive_seed(self):
        primary = self.primary()
        with self.forbidden_private_reads(primary):
            result = ConversationAgent(primary, NeverReviewer()).process(self.g, self.w,
                '我叫张三，邮箱test.private@example.com，手机号13812345678；'
                '不读取个人账户，只计算假设的123.45元。')
        self.assertFalse(result.get('isFallback'), result)
        actual = json.dumps(primary.messages, ensure_ascii=False)
        for private in ('张三', 'test.private@example.com', '13812345678', self.w):
            self.assertNotIn(private, actual)

    def test_public_rag_read_still_uses_original_published_evidence_without_private_initialization(self):
        from test_rag_integration import publish_fixture, answer as public_answer
        publish_fixture(self.g)
        primary = Primary(tool('knowledge_search', {'query': '测试产品', 'product_id': 'TEST-P',
                          'facets': ['risk']}), public_answer)
        class Reviewer(GLMConversationReviewer):
            model_id = 'offline-public-reviewer'
            billing_mode = 'unpaid_fixture'
            def __init__(inner_self):
                import io
                from agent_runtime.model_adapters import OpenAICompatibleJsonClient, AIPING_URL
                inner_self.packets = []
                inner_self.fixture_wire_requests = []
                class Response(io.BytesIO):
                    status = 200
                def transport(request, **kwargs):
                    wire = bytes(request.data)
                    payload = json.loads(wire)
                    if payload['model'] != inner_self.model_id:
                        raise AssertionError('fixture reviewer model mismatch')
                    inner_self.fixture_wire_requests.append(wire)
                    verdict = {'verdict': 'approve', 'reason': '离线接线检查', 'issues': []}
                    content = json.dumps(verdict, ensure_ascii=False)
                    usage = {'prompt_tokens': len(wire),
                             'completion_tokens': len(content.encode('utf-8')),
                             'total_tokens': len(wire) + len(content.encode('utf-8'))}
                    return Response(json.dumps({'choices': [{'finish_reason': 'stop',
                        'message': {'role': 'assistant', 'content': content}}],
                        'usage': usage}, ensure_ascii=False).encode('utf-8'))
                client = OpenAICompatibleJsonClient(provider='ai_ping',
                    model_id=inner_self.model_id, url=AIPING_URL,
                    env_name='PRIVACY_UNPAID_FIXTURE_ONLY', keychain_service='unused-fixture',
                    key_loader=lambda: 'local-fixture-marker-not-provider-key',
                    urlopen=transport, max_retries=0,
                    log_path=str(Path(self.temp.name) / 'fixture-reviewer.jsonl'))
                super().__init__(client)
            def review(self, packet, *args, **kwargs):
                self.packets.append(deepcopy(packet))
                return super().review(packet, *args, **kwargs)
        reviewer = Reviewer()
        with self.forbidden_private_reads(primary):
            result = ConversationAgent(primary, reviewer).process(self.g, self.w,
                '不读取个人账户或画像，请查测试产品的公开风险。')
        self.assertFalse(result.get('isFallback'), result)
        self.assertEqual(result['agent_review']['status'], 'approved')
        self.assertIn('knowledge_search', result['capabilities_used'])
        self.assert_inputs_restricted(primary)
        self.assertEqual(len(reviewer.packets), 1)
        self.assertEqual(reviewer.packets[0].get('history', []), [])

    def test_unrestricted_balance_query_keeps_original_private_reads_and_fact_contract(self):
        def finish_account(messages, schemas):
            outputs = [json.loads(item['content']) for item in messages if item['role'] == 'tool']
            ids = [fact['id'] for output in outputs for fact in output.get('facts', [])
                   if '已结算余额' in fact['text']]
            return tool('answer_facts', {'fact_ids': ids})
        primary = Primary(tool('account_summary', {}), finish_account)
        with patch.object(self.g.bank, 'customer_archive', wraps=self.g.bank.customer_archive) as archive:
            result = ConversationAgent(primary, NeverReviewer()).process(self.g, self.w, '查账户余额')
        self.assertFalse(result.get('isFallback'), result)
        self.assertGreater(archive.call_count, 0)
        self.assertEqual(result['answer_contract']['kind'], 'verified_facts')
        self.assertIsNotNone(result['_answer_context'])


if __name__ == '__main__':
    unittest.main()
