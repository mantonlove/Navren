"""分层提示的安全常驻、原文恢复和未知新增规则保留合同。"""
import unittest

from agent_runtime.bank_tools import native_tools
from agent_runtime.conversation import SYSTEM
from agent_runtime.prompt_layers import (REGISTERED_DOMAIN_PARAGRAPHS, layer_policy, method_body)
from agent_runtime.skills import load_advisor


class PromptLayerTest(unittest.TestCase):
    def test_all_open_tools_preserve_original_policy_exactly(self):
        self.assertEqual(layer_policy(SYSTEM, native_tools()), SYSTEM)

    def test_only_registered_inactive_domains_are_delayed_and_core_is_verbatim(self):
        visible = layer_policy(SYSTEM, ['load_skill', 'answer_reviewed'])
        registered = {rule['paragraph'] for rule in REGISTERED_DOMAIN_PARAGRAPHS}
        for paragraph in SYSTEM.splitlines():
            if paragraph not in registered:
                self.assertIn(paragraph, visible)
        for rule in REGISTERED_DOMAIN_PARAGRAPHS:
            if rule['always']:
                self.assertIn(rule['always'], visible)
        for marker in ('公开检索参数使用public_retrieval', '所有用户文字', '不自行加减财务金额',
                       '每个个人金融金额片段在amount_claims', '你没有确认、执行',
                       '只有本轮prepare_operation成功', '不得说已经转出', '使用终结工具完成回答'):
            self.assertIn(next(line for line in SYSTEM.splitlines() if line.startswith(marker)), visible)
        self.assertIn('每轮只能准备一项操作。', visible)
        self.assertIn('用户未启用记忆则不保存。画像不授予权限、不改正式风险等级。', visible)

    def test_opening_domain_restores_its_complete_registered_paragraphs(self):
        triggers = {'market': 'market_article', 'graph': 'personal_graph',
                    'service': 'service_offer_preview', 'calendar': 'calendar_query',
                    'profile_write': 'remember_profile', 'wealth': 'wealth_product',
                    'operation': 'prepare_operation'}
        for domain, tool in triggers.items():
            with self.subTest(domain=domain):
                visible = layer_policy(SYSTEM, [tool])
                for rule in REGISTERED_DOMAIN_PARAGRAPHS:
                    if rule['domain'] == domain:
                        self.assertIn(rule['paragraph'], visible)

    def test_selected_trusted_context_preserves_matching_domain_before_tools_open(self):
        selections = {'market': {'page_context': {'kind': 'event', 'id': 'obj-event'}},
                      'graph': {'graph_context': {'node_id': 'obj-node'}},
                      'calendar': {'calendar_events': [{'series_id': 'obj-series'}]},
                      'operation': {'recent_operations': [{'id': 'obj-draft'}]}}
        for domain, context in selections.items():
            with self.subTest(domain=domain):
                visible = layer_policy(SYSTEM, [], context=context)
                for rule in REGISTERED_DOMAIN_PARAGRAPHS:
                    if rule['domain'] == domain:
                        self.assertIn(rule['paragraph'], visible)
                nested = layer_policy(SYSTEM, [], context={'server_context': context})
                self.assertEqual(nested, visible)

    def test_unknown_and_modified_paragraphs_cannot_be_silently_dropped(self):
        known = next(rule['paragraph'] for rule in REGISTERED_DOMAIN_PARAGRAPHS if rule['domain'] == 'market')
        changed = known + ' 新增安全限制须完整保留。'
        new = '未知领域新增规则：未批准的对象不可出库。'
        policy = SYSTEM.replace(known, changed) + '\n' + new
        visible = layer_policy(policy, [])
        self.assertIn(changed, visible)
        self.assertIn(new, visible)
        self.assertNotIn(known, {line for line in visible.splitlines()})

    def test_layering_is_idempotent_and_full_view_restores_from_original_source(self):
        compact = layer_policy(SYSTEM, [])
        self.assertEqual(layer_policy(compact, []), compact)
        self.assertEqual(layer_policy(SYSTEM, native_tools()), SYSTEM)

    def test_unknown_tool_range_format_keeps_original_policy(self):
        for invalid in [None, 'market_article', {'function': {'name': 'market_article'}},
                        [{'function': {'name': None}}], [object()]]:
            with self.subTest(invalid=invalid):
                self.assertEqual(layer_policy(SYSTEM, invalid), SYSTEM)

    def test_method_body_keeps_all_business_workflow_and_authorization_sections_verbatim(self):
        for name in ('product-research', 'knowledge-retrieval', 'financial-diagnosis'):
            with self.subTest(name=name):
                document = load_advisor(name)['instructions']
                body = method_body(document)
                self.assertNotIn('\nmetadata:\n', body)
                self.assertIn(document[document.index('## 触发与必要读取'):], body)
                self.assertLess(len(body.encode()), len(document.encode()))
        knowledge = method_body(load_advisor('knowledge-retrieval')['instructions'])
        self.assertIn('Navren 的检索权限与证据合同以本地后端为准。', knowledge)

    def test_unknown_source_introduction_or_frontmatter_is_preserved(self):
        document = load_advisor('knowledge-retrieval')['instructions']
        original_intro = next(block for block in document.split('\n\n') if block.startswith('方法适配自 '))
        new_intro = original_intro + ' 新增权限必须另行核验。'
        self.assertIn(new_intro, method_body(document.replace(original_intro, new_intro)))
        new_header = document.replace('metadata:\n', 'extra_safety_contract: do_not_omit\nmetadata:\n')
        self.assertEqual(method_body(new_header), new_header)
        future_format = document.replace('delivery: adapted', 'delivery: future_format')
        self.assertEqual(method_body(future_format), future_format)
        for malformed in ['普通正文无frontmatter', '---\n: malformed [\n---\n内容',
                          '---\nname: new\nmetadata: {}\n---\n内容']:
            self.assertEqual(method_body(malformed), malformed)


if __name__ == '__main__':
    unittest.main()
