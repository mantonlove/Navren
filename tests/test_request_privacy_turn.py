"""已授权 privacy v2 的原固定反例与调用兼容；无云模型和私人数据库。"""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from agent_runtime.request_privacy import (
    RequestPrivacyPolicy, _active_private_request, _scope_authorization_text,
)
from agent_runtime.bank_tools import native_tools
from simulated_bank import BankError


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / ('outputs/rag-overall-acceptance-20261012/'
    'joint-integration-preparation/compound-cashflow-privacy-repair-v2')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finite_literal(node):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'replace'):
        return finite_literal(node.func.value).replace(
            *(finite_literal(arg) for arg in node.args))
    if isinstance(node, ast.List):
        return [finite_literal(item) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(finite_literal(item) for item in node.elts)
    return ast.literal_eval(node)


class RequestPrivacyTurnTests(unittest.TestCase):
    def test_original26_probes_three_original_public_contexts(self):
        path = ORIGINAL / 'differential-probes.json'
        identity = json.loads((ORIGINAL / 'input-identity.json').read_text())
        self.assertEqual(digest(path), identity['probe_sha256'])
        probes = json.loads(path.read_text())['probes']
        self.assertEqual(len(probes), 26)
        self.assertEqual(sum(p['was_semantically_concerning_new_opening']
                             for p in probes), 17)
        for probe in probes:
            self.assertEqual(len(probe['contexts']), 3)
            for item in probe['contexts']:
                with self.subTest(probe=probe['id'], context=item['context']):
                    policy = RequestPrivacyPolicy.for_turn(
                        probe['text'], page_context=item['context'])
                    self.assertEqual(policy.private_context_disabled,
                                     item['expected_v2_private_context_disabled'])
                    self.assertEqual(_active_private_request(
                        probe['text'], _scope_authorization_text(probe['text'])),
                        probe['expected_active_private_request'])
                    self.assertEqual(policy.permits('cashflow'),
                                     not policy.private_context_disabled)
                    self.assertEqual(policy.permits('prepare_operation'),
                                     not policy.private_context_disabled)

    def test_original_finite44_grammar_three_contexts(self):
        path = ORIGINAL / 'finite_cases.py'
        identity = json.loads((ORIGINAL / 'input-identity.json').read_text())
        self.assertEqual(digest(path), identity['finite_cases_sha256'])
        groups = {}
        for node in ast.parse(path.read_text()).body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in (
                        'POSITIVE', 'NEGATIVE', 'TAIL_PRIVATE_DENIED', 'PUBLIC_CONTEXTS')):
                groups[node.targets[0].id] = finite_literal(node.value)
        self.assertEqual(sum(len(groups[k]) for k in (
            'POSITIVE', 'NEGATIVE', 'TAIL_PRIVATE_DENIED')), 44)
        for group in ('POSITIVE', 'NEGATIVE', 'TAIL_PRIVATE_DENIED'):
            for case_id, text in groups[group]:
                for context in groups['PUBLIC_CONTEXTS']:
                    with self.subTest(group=group, case=case_id, context=context):
                        policy = RequestPrivacyPolicy.for_turn(text, page_context=context)
                        expected_disabled = group != 'POSITIVE'
                        self.assertEqual(policy.private_context_disabled, expected_disabled)
                        self.assertEqual(_active_private_request(
                            text, _scope_authorization_text(text)), not expected_disabled)
                        self.assertEqual(policy.permits('cashflow'), not expected_disabled)
                        self.assertEqual(policy.permits('prepare_operation'), not expected_disabled)

    def test_from_text_keeps_original_explicit_denial_contract(self):
        for text in ('比较公开基金产品。', '查我的账户余额', '解释个人账户的概念'):
            with self.subTest(text=text):
                self.assertFalse(RequestPrivacyPolicy.from_text(text).private_context_disabled)
        for context in (None, {'kind': 'account', 'id': 'host-validated-private'},
                        {'kind': 'product', 'source_scope': 'public_catalog'}):
            with self.subTest(context=context):
                text = '不要读取我的账户，查看未来现金流后准备申购产品。'
                policy = RequestPrivacyPolicy.for_turn(
                    text, page_context=context,
                    graph_context={'node_id': 'host-validated-private'})
                self.assertTrue(policy.private_context_disabled)
                self.assertEqual(policy.scope_reason, 'explicit_restriction')
                self.assertEqual(RequestPrivacyPolicy.from_text(text).version,
                                 'explicit-private-data-restriction/1')
                with self.assertRaises(BankError) as caught:
                    policy.require_tool('cashflow')
                self.assertEqual(caught.exception.code, 'REQUEST_PRIVATE_DATA_FORBIDDEN')

    def test_host_selected_private_context_and_public_defaults(self):
        public = {'kind': 'product', 'source_scope': 'public_nav', 'id': 'public'}
        before = deepcopy(public)
        self.assertTrue(RequestPrivacyPolicy.for_turn(
            '比较两款基金产品的费率。', page_context=public).private_context_disabled)
        self.assertFalse(RequestPrivacyPolicy.for_turn(
            '核对这个已选择账户', page_context={'kind': 'account', 'id': 'private'}
        ).private_context_disabled)
        self.assertFalse(RequestPrivacyPolicy.for_turn(
            '核对已选择节点', graph_context={'node_id': 'private'}
        ).private_context_disabled)
        self.assertEqual(public, before)

    def test_policy_does_not_add_model_confirmation_or_execution_tools(self):
        names = {schema['function']['name'] for schema in native_tools({}, {}, [])}
        self.assertIn('prepare_operation', names)
        self.assertNotIn('confirm_operation', names)
        self.assertNotIn('execute_operation', names)
        policy = RequestPrivacyPolicy.for_turn(
            '查看未来现金流后准备申购100元现金类模拟产品，等待我本人确认。')
        self.assertFalse(policy.private_context_disabled)
        self.assertTrue(policy.permits('cashflow'))
        self.assertTrue(policy.permits('prepare_operation'))


if __name__ == '__main__':
    unittest.main()
