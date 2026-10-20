"""只读宿主流程的来源、权限、工具预算和条件节点回退测试；无业务调用。"""
import ast
import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from agent_runtime.intent_routing import RouteInputError, canonical_json, normalize_route_answers
from agent_runtime.workflow_registry import READONLY_TOOLS, WorkflowRunner
from tests.test_intent_routing import answers, messages, route_input


ACCOUNT = 'obj_0123456789abcdef'


def schema(name, props=None, required=()):
    return {'type': 'function', 'function': {'name': name, 'description': name,
            'parameters': {'type': 'object', 'properties': props or {},
                           'required': list(required), 'additionalProperties': False}}}


STRING = {'type': 'string', 'minLength': 1, 'maxLength': 128}
SCHEMAS = [schema(name) for name in ('account_summary', 'knowledge_status', 'wealth_versions',
                                    'statement_source', 'periodic_charges', 'anomalies')]
SCHEMAS.extend([
    schema('cashflow', {'days': {'type': 'integer', 'minimum': 1, 'maximum': 365}}, ('days',)),
    schema('budget', {'account_id': {**STRING, 'enum': [ACCOUNT]}}, ('account_id',)),
    schema('public_products', {'query': STRING}),
    schema('knowledge_search', {'query': STRING, 'facets': {'type': 'array', 'items':
                {**STRING, 'enum': ['fees', 'liquidity', 'risk']}, 'maxItems': 10}, 'as_of': STRING}, ('query',)),
    schema('wealth_product', {'fund_id': STRING, 'on': STRING, 'portfolio': STRING},
           ('fund_id', 'on', 'portfolio')),
    schema('business_records', {'kind': {**STRING, 'enum': ['payments', 'cards', 'subscriptions',
                                                           'tasks', 'investments']}}, ('kind',)),
    # 即使白名单暂时可见，平台注册流程也不能选择它们。
    schema('prepare_operation', {'action': STRING}), schema('remember_profile'),
    schema('withdraw_draft'), schema('answer_facts'), schema('answer_reviewed'),
])


def runner(text, *, capabilities, fit, terms=(), context=None):
    route = route_input(text, terms=terms, context=context)
    decision = normalize_route_answers(route, answers(route, capabilities=capabilities, fits=(fit,)), provider='native')
    return WorkflowRunner(route, decision), messages(text, terms=terms, context=context)


def append_results(view, message, data_by_tool, *, evidence_prefix='e_'):
    result = copy.deepcopy(view)
    result.append(copy.deepcopy(message))
    for index, call in enumerate(message['tool_calls']):
        name = call['function']['name']
        result.append({'role': 'tool', 'tool_call_id': call['id'],
                       'content': canonical_json({'evidence_id': evidence_prefix + str(index),
                                                  'data': data_by_tool[name], 'facts': [], 'metrics': []})})
    return result


class WorkflowRegistryTests(unittest.TestCase):
    def test_original_public_fixture_full_catalog_compiles_only_literal_risk_query(self):
        # 原公开 fixture 与旧问题不变；保留真实概念 hints，绝不删词造正例。
        text = '查测试产品风险'
        original = ast.parse((Path(__file__).resolve().parents[1] / 'tests/test_agent_window.py').read_text())
        method = next(node for node in ast.walk(original) if isinstance(node, ast.FunctionDef)
            and node.name == 'test_rejected_private_query_can_recover_without_provider_disclosure')
        requests = [node.args[2].value for node in ast.walk(method) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == 'process'
            and len(node.args) > 2 and isinstance(node.args[2], ast.Constant)]
        self.assertIn(text, requests)
        terms = ('本金损失', '测试产品', '风险')
        identifiers = ('TEST-P',)
        for provider in ('native', 'jev'):
            with self.subTest(provider=provider):
                route = route_input(text, terms=terms, identifiers=identifiers,
                    context={'simulation': True, 'as_of': '2026-10-03'})
                decision = normalize_route_answers(route, answers(route,
                    capabilities=('knowledge-retrieval',), fits=('public_product_terms_read',),
                    provider=provider), provider=provider)
                self.assertEqual(decision.status, 'workflow')
                self.assertEqual({item.value for item in route.values('public_term')}, {'测试产品', '风险'})
                flow = WorkflowRunner(route, decision)
                view = messages(text, terms=terms, identifiers=identifiers,
                    context={'simulation': True, 'as_of': '2026-10-03'})
                first, _ = flow.local_workflow_step(view, SCHEMAS, 0, 2)
                self.assertEqual([call['function']['name'] for call in first['tool_calls']], ['knowledge_status'])
                view = append_results(view, first, {'knowledge_status':
                    {'ready': True, 'public_release_id': 'unit-public-release'}})
                second, _ = flow.local_workflow_step(view, SCHEMAS, 1, 1)
                self.assertEqual(json.loads(second['tool_calls'][0]['function']['arguments']),
                                 {'query': '测试产品', 'facets': ['risk']})
                # 未把 TEST-P 猜配给该拼写，也未把 context.as_of 当原文日期。
                self.assertEqual(route.values('product_id'), ())

    def test_nested_public_spellings_and_multiple_facets_share_one_explicit_subject(self):
        text = '查测试产品的风险和本金损失、申购费、赎回费'
        terms = ('测试', '测试产品', '风险', '本金损失', '申购费', '赎回费')
        route = route_input(text, terms=terms)
        decision = normalize_route_answers(route, answers(route,
            capabilities=('knowledge-retrieval',), fits=('public_product_terms_read',)), provider='native')
        self.assertEqual(decision.status, 'workflow')
        # 嵌套“测试”仍留在来源候选中，只在选完整 query 拼写时消歧。
        self.assertIn('测试', {item.value for item in route.values('public_term')})
        flow = WorkflowRunner(route, decision)
        view = messages(text, terms=terms)
        first, _ = flow.local_workflow_step(view, SCHEMAS, 0, 2)
        view = append_results(view, first, {'knowledge_status': {'ready': True, 'public_release_id': 'r'}})
        second, _ = flow.local_workflow_step(view, SCHEMAS, 1, 1)
        self.assertEqual(json.loads(second['tool_calls'][0]['function']['arguments']),
                         {'query': '测试产品', 'facets': ['fees', 'risk']})

    def test_flat_catalog_two_subjects_ambiguous_concepts_or_extra_goals_never_read_subset(self):
        cases = (
            ('查测试产品和另一产品风险', ('测试产品', '另一产品', '风险')),
            ('查测试产品风险和另一产品费用', ('测试产品', '另一产品', '风险', '费用')),
            ('查测试产品风险、债券风险', ('测试产品', '债券', '风险')),
            ('查测试产品和风险', ('测试产品', '风险')),
            ('查风险和本金损失', ('风险', '本金损失')),
            ('查测试产品风险和投资策略', ('测试产品', '风险', '投资策略')),
            ('查测试产品风险；不要查本金损失', ('测试产品', '风险', '本金损失')),
            ('查测试产品风险，然后申购1000元', ('测试产品', '风险')),
        )
        for text, terms in cases:
            for provider in ('native', 'jev'):
                with self.subTest(text=text, provider=provider):
                    route = route_input(text, terms=terms)
                    decision = normalize_route_answers(route, answers(route,
                        capabilities=('knowledge-retrieval',), fits=('public_product_terms_read',),
                        provider=provider), provider=provider)
                    self.assertEqual(decision.status, 'primary')
                    flow = WorkflowRunner(route, decision)
                    self.assertIsNone(flow.local_workflow_step(messages(text, terms=terms), SCHEMAS, 0, 10))
                    self.assertEqual(flow._pending, ())

    def test_registered_public_concept_query_preserves_literal_downstream_search(self):
        for text, term, facet in (('查风险原文', '风险', 'risk'),
                                  ('查本金损失原文', '本金损失', 'risk'),
                                  ('查申购费原文', '申购费', 'fees')):
            with self.subTest(text=text):
                flow, view = runner(text, capabilities=('knowledge-retrieval',),
                    fit='public_product_terms_read', terms=(term,))
                self.assertEqual(flow.decision.status, 'workflow')
                first, _ = flow.local_workflow_step(view, SCHEMAS, 0, 2)
                view = append_results(view, first, {'knowledge_status': {'ready': True, 'public_release_id': 'r'}})
                second, _ = flow.local_workflow_step(view, SCHEMAS, 1, 1)
                self.assertEqual(json.loads(second['tool_calls'][0]['function']['arguments']),
                                 {'query': term, 'facets': [facet]})

    def test_entire_public_catalog_change_after_route_cannot_reuse_plan(self):
        text = '查测试产品风险'
        terms = ('本金损失', '测试产品', '风险')
        route = route_input(text, terms=terms, identifiers=('TEST-P',))
        decision = normalize_route_answers(route, answers(route,
            capabilities=('knowledge-retrieval',), fits=('public_product_terms_read',)), provider='native')
        for new_terms, ids in ((terms + ('费用',), ('TEST-P',)), (terms, ('OTHER-P',))):
            with self.subTest(terms=new_terms, ids=ids):
                flow = WorkflowRunner(route, decision)
                self.assertIsNone(flow.local_workflow_step(
                    messages(text, terms=new_terms, identifiers=ids), SCHEMAS, 0, 10))
                self.assertEqual(flow.last_reason, 'workflow_state_or_candidates_changed')

    def test_original_p05_high_fit_requests_fees_without_inventing_date_or_fee_truth(self):
        original = next(case for case in json.loads((Path(__file__).resolve().parents[1]
            / 'evaluation/jev_business/cases.json').read_text())['cases'] if case['id'] == 'P05')
        text = original['question']
        self.assertEqual(text, '华夏纯债债券A的申购费、赎回费具体多少？')
        self.assertEqual(original['as_of'], '2026-09-24')
        self.assertEqual(original['fixture_status'], 'historical_context_unavailable')
        for provider in ('native', 'jev'):
            with self.subTest(provider=provider):
                route = route_input(text, terms=('华夏纯债债券A',))
                decision = normalize_route_answers(route, answers(route, capabilities=('knowledge-retrieval',),
                    fits=('public_product_terms_read',), provider=provider), provider=provider)
                self.assertEqual(decision.status, 'workflow')
                flow = WorkflowRunner(route, decision)
                view = messages(text, terms=('华夏纯债债券A',))
                first, _ = flow.local_workflow_step(view, SCHEMAS, 0, 2)
                view = append_results(view, first, {'knowledge_status':
                    {'ready': True, 'public_release_id': 'unit-readonly-source'}})
                detail, _ = flow.local_workflow_step(view, SCHEMAS, 1, 1)
                self.assertEqual(json.loads(detail['tool_calls'][0]['function']['arguments']),
                                 {'query': '华夏纯债债券A', 'facets': ['fees']})
                # 仅核编译消息，不伪造原历史夹具可用、费率数值或补入metadata日期。
                self.assertEqual(flow.route_input.source_request, text)

    def test_fee_and_separate_liquidity_risk_facets_are_all_present(self):
        for text, expected in (
            ('查询货币基金管理费、托管费、销售服务费和转换费', ['fees']),
            ('查询货币基金申购费、赎回费和赎回条件、风险披露', ['fees', 'liquidity', 'risk']),
        ):
            with self.subTest(text=text):
                flow, view = runner(text, capabilities=('knowledge-retrieval',),
                    fit='public_product_terms_read', terms=('货币基金',))
                first, _ = flow.local_workflow_step(view, SCHEMAS, 0, 2)
                view = append_results(view, first, {'knowledge_status': {'ready': True, 'public_release_id': 'r'}})
                detail, _ = flow.local_workflow_step(view, SCHEMAS, 1, 1)
                self.assertEqual(json.loads(detail['tool_calls'][0]['function']['arguments'])['facets'], expected)

    def test_original_channel_disclosure_and_uncovered_facets_return_primary_even_with_high_fit(self):
        original = next(case for case in json.loads((Path(__file__).resolve().parents[1]
            / 'evaluation/jev_business/cases.json').read_text())['cases'] if case['id'] == 'P04')
        cases = [
            (original['question'], ('华夏纯债债券A',), None),
            (original['question'], ('华夏纯债债券A',), {'page_context': {'channel_id': 'unregistered-channel'}}),
            ('货币基金现在在哪个渠道买？', ('货币基金',), None),
            ('查货币基金风险和分红规则', ('货币基金',), None),
            ('查货币基金申购费和投资策略', ('货币基金',), None),
            ('只查货币基金申购费，不查赎回费', ('货币基金',), None),
        ]
        for text, terms, context in cases:
            for provider in ('native', 'jev'):
                with self.subTest(text=text, provider=provider):
                    route = route_input(text, terms=terms, context=context)
                    decision = normalize_route_answers(route, answers(route, capabilities=('knowledge-retrieval',),
                        fits=('public_product_terms_read',), provider=provider), provider=provider)
                    self.assertEqual(decision.status, 'primary')
                    self.assertEqual(decision.workflow_ids, ())
                    flow = WorkflowRunner(route, decision)
                    self.assertIsNone(flow.local_workflow_step(messages(text, terms=terms, context=context),
                                                              SCHEMAS, 0, 10))
                    self.assertEqual(flow._pending, ())

    def test_negative_or_unrelated_scope_never_emits_partial_reads_even_with_positive_model_fit(self):
        cases = [
            (f'查{ACCOUNT}可安排资金，但不查未来30天现金流', ('financial-diagnosis',),
             'budget_and_cashflow_read', (), {'page_context': {'account_id': ACCOUNT}}),
            (f'查{ACCOUNT}可安排资金；查30天内的预算', ('financial-diagnosis',),
             'budget_and_cashflow_read', (), {'page_context': {'account_id': ACCOUNT}}),
            (f'查{ACCOUNT}可安排资金和未来30天现金流；不要读取个人账户', ('financial-diagnosis',),
             'budget_and_cashflow_read', (), {'page_context': {'account_id': ACCOUNT}}),
            ('查历史组合中某基金的收益；不查2026-09-15的基金值', ('portfolio-review',),
             'portfolio_facts_read', ('某基金',), None),
            ('不查历史组合；查某基金2026-09-15的收益', ('portfolio-review',),
             'portfolio_facts_read', ('某基金',), None),
            ('不查某基金；查历史组合2026-09-15的收益', ('portfolio-review',),
             'portfolio_facts_read', ('某基金',), None),
            ('查历史组合中某基金的收益；2026-09-15是我的生日', ('portfolio-review',),
             'portfolio_facts_read', ('某基金',), None),
            ('查货币基金风险原文；不查2026-09-15的历史条款', ('knowledge-retrieval',),
             'public_product_terms_read', ('货币基金',), None),
            ('查周期扣费候选，但不要查异常重复扣费', ('subscriptions',),
             'subscription_candidates_read', (), None),
            (f'查{ACCOUNT}可安排资金和未来30天现金流；不要不查未来现金流',
             ('financial-diagnosis',), 'budget_and_cashflow_read', (),
             {'page_context': {'account_id': ACCOUNT}}),
        ]
        for text, capabilities, fit, terms, context in cases:
            for provider in ('native', 'jev'):
                with self.subTest(text=text, provider=provider):
                    route = route_input(text, terms=terms, context=context)
                    decision = normalize_route_answers(route, answers(route, capabilities=capabilities,
                        fits=(fit,), provider=provider), provider=provider)
                    self.assertEqual(decision.status, 'primary')
                    self.assertEqual(decision.workflow_ids, ())
                    flow = WorkflowRunner(route, decision)
                    self.assertIsNone(flow.local_workflow_step(
                        messages(text, terms=terms, context=context), SCHEMAS, 0, 10))
                    self.assertEqual(flow._pending, ())
                    self.assertEqual(flow._results, {})

    def test_negated_page_typed_object_cannot_be_bound_to_affirmative_portfolio_query(self):
        fund_id = 'obj_fedcba9876543210'
        text = f'查历史组合2026-09-15的收益；不查{fund_id}的基金值'
        flow, view = runner(text, capabilities=('portfolio-review',), fit='portfolio_facts_read',
                            context={'page_context': {'fund_id': fund_id}})
        self.assertEqual(flow.decision.status, 'primary')
        self.assertIsNone(flow.local_workflow_step(view, SCHEMAS, 0, 10))

    def test_clear_write_exclusions_preserve_affirmative_read_without_authorizing_extra_views(self):
        text = '只查周期扣费候选，不取消订阅，不关闭代扣'
        for provider in ('native', 'jev'):
            with self.subTest(provider=provider):
                route = route_input(text)
                decision = normalize_route_answers(route, answers(route, capabilities=('subscriptions',),
                    fits=('subscription_candidates_read',), provider=provider), provider=provider)
                self.assertEqual(decision.status, 'workflow')
                flow = WorkflowRunner(route, decision)
                message, _ = flow.local_workflow_step(messages(text), SCHEMAS, 0, 2)
                self.assertEqual([call['function']['name'] for call in message['tool_calls']],
                                 ['statement_source', 'periodic_charges'])
                self.assertNotIn('business_records', [call['function']['name'] for call in message['tool_calls']])

    def test_fee_queries_cannot_be_compiled_as_subscription_renewals(self):
        for text in ('查询手续费', '查询申购费率和赎回手续费',
                     '查询手续费和周期扣费候选'):
            for provider in ('native', 'jev'):
                with self.subTest(text=text, provider=provider):
                    route = route_input(text)
                    decision = normalize_route_answers(route, answers(route,
                        capabilities=('subscriptions',), fits=('subscription_candidates_read',),
                        provider=provider), provider=provider)
                    self.assertEqual(decision.status, 'primary')
                    self.assertEqual(decision.workflow_ids, ())
                    flow = WorkflowRunner(route, decision)
                    self.assertIsNone(flow.local_workflow_step(messages(text), SCHEMAS, 0, 10))

    def test_budget_first_reads_are_independent_and_id_comes_from_current_enum_and_result(self):
        text = f'查{ACCOUNT}可安排资金与未来30天现金流'
        flow, view = runner(text, capabilities=('financial-diagnosis', 'goal-planning'),
                            fit='budget_and_cashflow_read', context={'page_context': {'account_id': ACCOUNT}})
        self.assertEqual(flow.minimum_rounds_remaining(), 3)
        message, receipt = flow.local_workflow_step(view, SCHEMAS, 0, 10)
        self.assertEqual(flow.minimum_rounds_remaining(), 2)
        self.assertEqual([c['function']['name'] for c in message['tool_calls']], ['account_summary', 'cashflow'])
        self.assertEqual(json.loads(message['tool_calls'][1]['function']['arguments']), {'days': 30})
        self.assertFalse(receipt['request_sent'])
        self.assertEqual(receipt['model_calls'], 0)
        self.assertIsNone(receipt['model_usage'])
        view = append_results(view, message, {'account_summary': {'accounts': [{'id': ACCOUNT}]},
                                               'cashflow': {'accounts': []}})
        second, receipt = flow.local_workflow_step(view, SCHEMAS, 1, 8)
        self.assertEqual(flow.minimum_rounds_remaining(), 1)
        self.assertEqual([c['function']['name'] for c in second['tool_calls']], ['budget'])
        self.assertEqual(json.loads(second['tool_calls'][0]['function']['arguments']), {'account_id': ACCOUNT})
        view = append_results(view, second, {'budget': {'account_id': ACCOUNT}})
        self.assertIsNone(flow.local_workflow_step(view, SCHEMAS, 2, 7))
        self.assertEqual(flow.last_reason, 'workflow_reads_completed_primary_must_finalize')

    def test_multistage_plans_require_two_read_batches_and_final_before_any_read(self):
        cases = [
            (f'查{ACCOUNT}可安排资金与未来30天现金流', ('financial-diagnosis',),
             'budget_and_cashflow_read', (), {'page_context': {'account_id': ACCOUNT}}),
            ('查货币基金风险原文', ('knowledge-retrieval',),
             'public_product_terms_read', ('货币基金',), None),
            ('查历史组合中某基金2026-09-15的收益', ('portfolio-review',),
             'portfolio_facts_read', ('某基金',), None),
        ]
        for text, capabilities, fit, terms, context in cases:
            with self.subTest(workflow=fit):
                flow, _ = runner(text, capabilities=capabilities, fit=fit, terms=terms, context=context)
                # 两轮虽可发首次读取再终结，却不足以取得必要的detail；
                # 宿主必须回原规划，getter在反复准入检查中不能暗中推进。
                for available in (1, 2):
                    self.assertGreater(flow.minimum_rounds_remaining(), available)
                    self.assertEqual(flow._pending, ())
                    self.assertEqual(flow._results, {})
                self.assertEqual(flow.minimum_rounds_remaining(), 3)

    def test_single_batch_subscription_reserves_one_final_round_without_tool_double_debit(self):
        flow, view = runner('查银行授权和商户合同', capabilities=('subscriptions',),
                            fit='subscription_candidates_read')
        self.assertEqual(flow.minimum_rounds_remaining(), 2)
        # remaining_tools已由Conversation减去终结位，此处一个业务读取即可执行。
        message, _ = flow.local_workflow_step(view, SCHEMAS, 0, 1)
        self.assertEqual(len(message['tool_calls']), 1)
        self.assertEqual(flow.minimum_rounds_remaining(), 1)

    def test_unknown_or_multiple_accounts_do_not_choose_first_or_read_before_enum(self):
        text = f'查{ACCOUNT}可安排资金与未来30天现金流'
        context = {'page_context': {'account_id': ACCOUNT}}
        flow, view = runner(text, capabilities=('financial-diagnosis',), fit='budget_and_cashflow_read', context=context)
        schemas = copy.deepcopy(SCHEMAS)
        next(s for s in schemas if s['function']['name'] == 'budget')['function']['parameters']['properties']['account_id']['enum'] = []
        self.assertIsNone(flow.local_workflow_step(view, schemas, 0, 10))
        self.assertEqual(flow.last_reason, 'workflow_account_not_current_enum')
        flow, view = runner(text, capabilities=('financial-diagnosis',), fit='budget_and_cashflow_read', context=context)
        message, _ = flow.local_workflow_step(view, SCHEMAS, 0, 10)
        view = append_results(view, message, {'account_summary': {'accounts': [{'id': 'other'}]}, 'cashflow': {'accounts': []}})
        self.assertIsNone(flow.local_workflow_step(view, SCHEMAS, 1, 8))
        self.assertEqual(flow.last_reason, 'workflow_account_result_unbound')

    def test_missing_mandatory_tool_and_insufficient_whole_budget_do_not_partial_read(self):
        text = f'查{ACCOUNT}可安排资金与未来30天现金流'
        context = {'page_context': {'account_id': ACCOUNT}}
        for schemas, budget in (([s for s in SCHEMAS if s['function']['name'] != 'cashflow'], 10),
                                 (SCHEMAS, 2)):
            flow, view = runner(text, capabilities=('financial-diagnosis',), fit='budget_and_cashflow_read', context=context)
            self.assertIsNone(flow.local_workflow_step(view, schemas, 0, budget))
        flow, view = runner('查周期扣费候选', capabilities=('subscriptions',), fit='subscription_candidates_read')
        duplicate = SCHEMAS + [SCHEMAS[0]]
        self.assertIsNone(flow.local_workflow_step(view, duplicate, 0, 10))

    def test_conditional_subscription_views_are_selected_separately_no_forced_account(self):
        flow, view = runner('查银行授权和商户合同', capabilities=('subscriptions',), fit='subscription_candidates_read')
        message, _ = flow.local_workflow_step(view, SCHEMAS, 0, 1)
        self.assertEqual([c['function']['name'] for c in message['tool_calls']], ['business_records'])
        self.assertEqual(json.loads(message['tool_calls'][0]['function']['arguments']), {'kind': 'subscriptions'})
        flow, view = runner('检查异常重复扣费候选', capabilities=('subscriptions',), fit='subscription_candidates_read')
        message, _ = flow.local_workflow_step(view, SCHEMAS, 0, 2)
        self.assertEqual([c['function']['name'] for c in message['tool_calls']], ['statement_source', 'anomalies'])
        self.assertNotIn('account_summary', [c['function']['name'] for c in message['tool_calls']])
        self.assertTrue({c['function']['name'] for c in message['tool_calls']} <= READONLY_TOOLS)

    def test_public_query_copies_registered_public_literal_not_private_whole_question(self):
        text = '查货币基金费用和赎回条件'
        flow, view = runner(text, capabilities=('product-research', 'knowledge-retrieval'),
                            fit='public_product_terms_read', terms=('货币基金',))
        message, _ = flow.local_workflow_step(view, SCHEMAS, 0, 3)
        self.assertEqual(json.loads(message['tool_calls'][1]['function']['arguments']), {'query': '货币基金'})
        view = append_results(view, message, {'knowledge_status': {'ready': True, 'public_release_id': 'release-v1'},
                                               'public_products': {'products': []}})
        second, _ = flow.local_workflow_step(view, SCHEMAS, 1, 1)
        self.assertEqual(json.loads(second['tool_calls'][0]['function']['arguments']),
                         {'query': '货币基金', 'facets': ['fees', 'liquidity']})
        self.assertNotIn('product_id', second['tool_calls'][0]['function']['arguments'])
        self.assertNotIn('share_class_id', second['tool_calls'][0]['function']['arguments'])

    def test_unready_source_does_not_run_search_or_fabricate_completion(self):
        text = '查货币基金风险'
        flow, view = runner(text, capabilities=('knowledge-retrieval',),
                            fit='public_product_terms_read', terms=('货币基金',))
        message, _ = flow.local_workflow_step(view, SCHEMAS, 0, 10)
        view = append_results(view, message, {'knowledge_status': {'ready': False, 'public_release_id': None},
                                               'public_products': {'products': []}})
        self.assertIsNone(flow.local_workflow_step(view, SCHEMAS, 1, 8))
        self.assertEqual(flow.last_reason, 'workflow_public_source_not_ready')

    def test_knowledge_only_does_not_force_optional_public_catalog_read(self):
        flow, view = runner('查货币基金风险原文', capabilities=('knowledge-retrieval',),
                            fit='public_product_terms_read', terms=('货币基金',))
        message, _ = flow.local_workflow_step(view, SCHEMAS, 0, 2)
        self.assertEqual([call['function']['name'] for call in message['tool_calls']], ['knowledge_status'])
        view = append_results(view, message, {'knowledge_status': {'ready': True, 'public_release_id': 'r'}})
        detail, _ = flow.local_workflow_step(view, SCHEMAS, 1, 1)
        self.assertEqual([call['function']['name'] for call in detail['tool_calls']], ['knowledge_search'])

    def test_portfolio_copies_fund_id_from_own_current_versions_and_explicit_date(self):
        text = '查历史组合中某基金2026-09-15的收益'
        flow, view = runner(text, capabilities=('portfolio-review',), fit='portfolio_facts_read', terms=('某基金',))
        message, _ = flow.local_workflow_step(view, SCHEMAS, 0, 2)
        self.assertEqual([c['function']['name'] for c in message['tool_calls']], ['wealth_versions'])
        view = append_results(view, message, {'wealth_versions': {'versions': [],
            'products': [{'name': '某基金', 'fund_id': 'registered-fund-id'}]}})
        detail, _ = flow.local_workflow_step(view, SCHEMAS, 1, 1)
        self.assertEqual(json.loads(detail['tool_calls'][0]['function']['arguments']),
                         {'fund_id': 'registered-fund-id', 'on': '2026-09-15', 'portfolio': 'historical'})
        # 本地消息不是持仓/估值证明；原wealth_product仍会校验组合实际持有。
        self.assertEqual(detail['tool_calls'][0]['function']['name'], 'wealth_product')

    def test_same_fund_name_two_objects_never_pick_first(self):
        text = '查历史组合中某基金2026-09-15的收益'
        flow, view = runner(text, capabilities=('portfolio-review',), fit='portfolio_facts_read', terms=('某基金',))
        message, _ = flow.local_workflow_step(view, SCHEMAS, 0, 2)
        view = append_results(view, message, {'wealth_versions': {'products':
            [{'name': '某基金', 'fund_id': 'A'}, {'name': '某基金', 'fund_id': 'C'}]}})
        self.assertIsNone(flow.local_workflow_step(view, SCHEMAS, 1, 1))
        self.assertEqual(flow.last_reason, 'workflow_fund_result_unbound')

    def test_source_or_projected_state_changes_invalidate_bound_plan(self):
        text = '查周期扣费候选'
        for replacement in (messages('查周期扣费候选，然后关闭代扣'),
                            messages(text, context={'attachment_status': 'changed'})):
            flow, _ = runner(text, capabilities=('subscriptions',), fit='subscription_candidates_read')
            self.assertIsNone(flow.local_workflow_step(replacement, SCHEMAS, 0, 10))
            self.assertIn(flow.last_reason, ('workflow_request_changed', 'workflow_state_or_candidates_changed'))

    def test_unissued_foreign_duplicate_and_missing_tool_results_are_not_evidence(self):
        text = '查货币基金费用'
        for mode in ('foreign', 'duplicate', 'missing', 'assistant_changed', 'tool_error'):
            flow, view = runner(text, capabilities=('knowledge-retrieval',),
                                fit='public_product_terms_read', terms=('货币基金',))
            message, _ = flow.local_workflow_step(view, SCHEMAS, 0, 10)
            view = append_results(view, message, {'knowledge_status': {'ready': True, 'public_release_id': 'r'},
                                                   'public_products': {'products': []}})
            tool_index = len(view) - len(message['tool_calls'])
            assistant_index = tool_index - 1
            if mode == 'foreign':
                view[tool_index]['tool_call_id'] = 'someone-elses-call'
            elif mode == 'duplicate':
                view.append(copy.deepcopy(view[tool_index]))
            elif mode == 'missing':
                view.pop()
            elif mode == 'assistant_changed':
                view[assistant_index]['tool_calls'][0]['function']['name'] = 'prepare_operation'
            elif mode == 'tool_error':
                view[tool_index]['content'] = canonical_json({'error': 'not_ready'})
            with self.subTest(mode=mode):
                self.assertIsNone(flow.local_workflow_step(view, SCHEMAS, 1, 8))
                self.assertEqual(flow.last_reason, 'workflow_result_chain_invalid')

    def test_forged_readonly_label_and_cross_request_decision_rejected(self):
        route = route_input('查银行授权')
        decision = normalize_route_answers(route, answers(route, capabilities=('subscriptions',),
            fits=('subscription_candidates_read',)), provider='native')
        with self.assertRaisesRegex(RouteInputError, 'derived_from_bound_answers'):
            WorkflowRunner(route, replace(decision, workflow_ids=('subscription_candidates_read',),
                                          capability_ids=('payments', 'subscriptions'),
                                          routing_skills=('payments', 'subscriptions')))
        with self.assertRaisesRegex(RouteInputError, 'not_registered|derived_from_bound_answers'):
            WorkflowRunner(route, replace(decision, workflow_ids=('prepare_operation',)))
        another = route_input('查银行授权', request_id='r2')
        with self.assertRaisesRegex(RouteInputError, 'binding_mismatch'):
            WorkflowRunner(another, decision)

    def test_returned_receipt_mutation_cannot_change_private_pending_message_or_route(self):
        text = '查周期扣费候选'
        flow, view = runner(text, capabilities=('subscriptions',), fit='subscription_candidates_read')
        message, receipt = flow.local_workflow_step(view, SCHEMAS, 0, 2)
        receipt['binding']['request_id'] = 'other'
        self.assertEqual(flow.decision.binding['request_id'], 'r1')
        message['tool_calls'][0]['function']['arguments'] = canonical_json({'action': 'transfer'})
        view = append_results(view, message, {'statement_source': {}, 'periodic_charges': {}})
        self.assertIsNone(flow.local_workflow_step(view, SCHEMAS, 1, 0))
        self.assertEqual(flow.last_reason, 'workflow_result_chain_invalid')


if __name__ == '__main__':
    unittest.main()
