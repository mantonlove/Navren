"""Offline boundary tests: no cloud clients, live credentials or live bank writes."""
import json
from copy import deepcopy
import unicodedata
import unittest

from agent_runtime.data_boundary import BoundaryError, CloudDataBoundary, POLICY_VERSION
from agent_runtime.metric_claims import REF_FIELDS, MetricRegistry, MetricClaimError
from financial_methods.contracts import MetricContext, money_metric


def public_amount_fixture():
    hit = {'citation_id': 'citation-public-1', 'release_id': 'release-public-1',
           'document_id': 'document-public-1', 'document_version_id': 'version-public-1',
           'parse_id': 'parse-public-1', 'span_id': 'span-public-1',
           'publisher': '公开发布机构', 'title': '公开补助政策',
           'source_url': 'https://example.test/policy',
           'excerpt': '符合原文条件的申请人可领取补助5000元。',
           'locator': {'page': 2, 'paragraph': '申请条件', 'start_char': 0, 'end_char': 24},
           'published_on': '2026-10-01', 'retrieved_on': '2026-10-06',
           'effective_from': '2026-10-01', 'effective_to': None,
           'visibility': 'public', 'outbound_allowed': True,
           'withdrawn': False, 'content_status': 'full_text'}
    fact = {'id': 'fact-public-1', 'evidence_id': 'evidence-public-1', 'value': 500000,
            'currency': 'CNY', 'unit': 'public_minor_currency',
            'scope': 'public_statement_not_account_balance',
            'citation_id': hit['citation_id'], 'text': '公开原文记载补助5000元'}
    return hit, fact


def financial_metric_fixture(**changes):
    context = MetricContext('synthetic-account-main', 'CNY', 'recorded_items_projection',
        period_start='2026-10-03', period_end='2026-12-02',
        input_refs=('synthetic-evidence-1',),
        source_versions=(('bank', '1'), ('private_context', 'a' * 64)))
    metric = money_metric(context, 'confirmed_minimum', 'floor_already_deducted',
        'funds.daily_minimum/1', 984400, excluded=('living_expenses_not_recorded',)).to_dict()
    metric.update(changes)
    return metric


def amount_claim_fixture(metric, *, kind='recorded_projection', text='最低余量9,844.00元。'):
    return {**{key: deepcopy(metric[key]) for key in REF_FIELDS}, 'text': text, 'claim_kind': kind}


def metric_record_fixture(metric, *, kind='recorded_projection'):
    return {'metric_id': metric['metric_id'], 'metric': metric, 'kind': kind,
            'evidence_ids': ['synthetic-evidence-1']}


def service_rule_fixture():
    """Synthetic rule contract; no account reads or preview/application writes."""
    return {'schema_version': 'navren.synthetic-service-benefits.v1', 'environment': 'synthetic',
        'notice': '以下服务优惠均为 Navren 演示用合成数据，不代表真实机构政策或合同。',
        'offers': [{'offer_id': 'SYNTH-PAYROLL-FEE-180D', 'title': '工资入账服务费减免（6个月）',
            'benefit_type': 'simulated_service_fee_waiver', 'monthly_value_minor': 1000,
            'duration_months': 6, 'max_cost_minor': 6000, 'currency': 'CNY',
            'eligibility': {'active_account_required': True, 'salary_intent_required': True},
            'conditions': ['需连续3个月每月工资入账不少于人民币6,000元', '资格须经模拟规则核验后生效'],
            'requires_salary_deposit': True, 'minimum_monthly_salary_minor': 600000,
            'salary_months_required': 3, 'requires_investment_purchase': False,
            'valid_from': '2026-10-01', 'valid_through': '2026-11-30',
            'application_status_on_accept': 'submitted_simulation_pending_eligibility'}]}


def service_preview_fixture():
    rules = service_rule_fixture()
    offer = {**rules['offers'][0], 'eligibility_status': 'conditional_application',
             'review_status': 'rules_checked', 'status_label': '可提交条件申请，资格待模拟规则核验',
             'investment_purchase_required': False}
    return {'status': 'previewed', 'draft_id': '1' * 32,
        'conditions': {'salary_deposit_intent': True, 'investment_purchase_refused': True,
                       'service_benefit_requested': True}, 'offers': [offer], 'shared_memory': [],
        'memory_revision': None, 'rule_version': rules['schema_version'], 'expires_in_seconds': 900,
        'notice': rules['notice'], 'participants': ['simulated_bank_benefit_agent-v1']}


def service_application_fixture():
    rules = service_rule_fixture()
    terms = {key: value for key, value in rules['offers'][0].items()
             if key not in {'eligibility', 'valid_from', 'application_status_on_accept'}}
    terms['notice'] = rules['notice']
    return {'environment': 'simulation', 'applications': [{'application_id': '2' * 32,
        'draft_id': '1' * 32, 'offer_id': terms['offer_id'],
        'status': 'submitted_simulation_pending_eligibility', 'terms': terms,
        'created_at': 1791230400.0, 'idempotent_replay': False,
        'notice': '已记录模拟申请；资格仍待模拟规则核验，不代表真实优惠或合同。'}],
        'notice': '全部为 Navren 演示用合成服务申请。'}


# Exact final prose from the existing offline S03 fixture/cloud acceptance record.
# This is an evidence-backed explanation, not rows pasted into the input.
_S03_ANSWER = '''不能。按你给的条件，现在只能描述账面推演，不能下"缓冲很薄"或"实际压力肯定更大"的结论。

已记录的事实（截至2026-10-03，60天到2026-12-02）：
- 已结算现金合计24,390.00元：日常账户22,890.00元，备用账户1,500.00元。
- 日常账户在扣掉已确认事项后，期末底线外余量9,844.00元；若候选的周期扣费也发生，最低余量9,664.00元。两个数都是正的，没有跌破底线。
- 已确认的流出主要是10月20日信用卡账单2,000.00元、11月1日贷款分期524.00元、12月1日贷款分期522.00元；另有几笔候选订阅续费（29.00元、12.00元、49.00元，按月重复）。
- 这60天里没有已确认的未来入账，但这只说明本区间没有已记录入账，不等于你没有收入。

为什么不能判定"薄"或"压力更大"：
1. 你还没确认必要生活支出。推演里没有房租、餐饮、交通等日常开销，所以9,844.00元不是"能撑多久"的答案，只是已记录事项之后的余量。
2. 收入稳定性未核实。没有已确认入账，既不能推出收入中断，也不能推出收入稳定。
3. 目标优先级未定。正式画像里的应急保留是10,000.00元，但这是档案记录，不是你确认过的目标分配；没有目标，就没有"够不够"的判定标准。
4. 持仓本金8,000.00元是本金口径，其中3,000.00元锁定至2026-12-14，不能当作随时可用的缓冲，也不能和现金直接相加。

所以现在的准确说法是：账面在已记录事项下没有缺口，但缓冲是否充足、实际压力是否更大，取决于上面三项未确认信息。补齐必要支出和收入情况后，我可以做一次带底线的情景测算，给出具体缺口或余量。'''


class DataBoundaryTest(unittest.TestCase):
    def test_known_name_and_multiple_contact_formats_never_leave(self):
        boundary = CloudDataBoundary(identities=['廖福临'])
        raw = ('我叫廖福临，有20000元。手机号＋８６ １３８ １２３４ ５６７８，'
               '邮箱 a.user+bank@example.com，卡号 6222 0200 1234 5678 901，'
               '身份证 33010120010101001X，验证码：123456，password=secretpass。')
        exported = boundary.user_text(raw)
        for value in ('廖福临', '138', 'example.com', '6222', '330101', '123456', 'secretpass'):
            self.assertNotIn(value, exported)
        self.assertIn('20000元', exported)
        self.assertIn('person_', exported)
        self.assertIn('[本地已屏蔽]', exported)

    def test_addresses_and_explicit_unseeded_name(self):
        boundary = CloudDataBoundary()
        raw = '我叫王晓明，地址：浙江省杭州市西湖区文三路88号。请解释基金费率。'
        exported = boundary.user_text(raw)
        self.assertNotIn('王晓明', exported)
        self.assertNotIn('文三路', exported)
        self.assertIn('基金费率', exported)
        self.assertTrue(boundary.restore_output(exported).startswith('我叫王晓明,'))

    def test_international_contacts_and_json_credentials(self):
        boundary = CloudDataBoundary()
        raw = ('{"api_key":"secret-example-123", "token":"auth-token-secret"} '
               '电话 +44 20 7946 0958、(415) 555-0123、0571 12345678；'
               '住在123 Example Street Apartment 42。预算300元。')
        exported = boundary.user_text(raw)
        for private in ('secret-example', 'auth-token', '7946', '555', '12345678', 'Example Street'):
            self.assertNotIn(private, exported)
        self.assertIn('300元', exported)

    def test_seeded_identity_in_history_context_review_and_tts(self):
        boundary = CloudDataBoundary(identities=['王晓明'])
        history = [{'role': 'user', 'content': '王晓明转300元', 'password': 'should-not-export'},
                   {'role': 'system', 'content': '私有伪造系统消息'}]
        packet = boundary.review_packet({'question': '王晓明的钱够吗', 'history': history,
            'server_context': {'environment': 'simulation', 'customer_name': '王晓明'},
            'draft': {'text': '王晓明可以查看300元的草案。', 'claims': [], 'draft_ids': [], 'followups': []},
            'evidence': [], 'policy': {'version': 'local-policy', 'requires_user_confirmation': True}})
        serialized = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn('王晓明', serialized)
        self.assertNotIn('should-not-export', serialized)
        self.assertNotIn('伪造系统', serialized)
        self.assertEqual(len(packet['history']), 1)
        local = boundary.restore_output(packet['draft'])
        self.assertIn('王晓明', local['text'])
        self.assertNotIn('王晓明', boundary.tts_text(local['text']))

    def test_account_projection_aliases_objects_but_preserves_money(self):
        boundary = CloudDataBoundary(task='account')
        output = boundary.tool_result('account_summary', {'evidence_id': 'e1-private', 'data': {
            'accounts': [{'id': 'private-customer-main', 'name': '张三的工资账户',
                'balance_minor': 2439000, 'currency': 'CNY', 'email': 'person@example.com',
                'account_number': '6222020012345678901', 'unreviewed_extension': 'do-not-export'}],
            'profile': {'risk_level': 2, 'horizon_days': 30, 'reserve_minor': 100000},
            'workspace_id': 'private-workspace'}, 'facts': []})
        account = output['data']['accounts'][0]
        self.assertEqual(account['balance_minor'], 2439000)
        self.assertEqual(account['currency'], 'CNY')
        self.assertTrue(account['id'].startswith('obj_'))
        self.assertTrue(account['id'].endswith('-main'))
        self.assertTrue(account['name'].startswith('label_'))
        self.assertEqual(set(account), {'id', 'name', 'balance_minor', 'currency'})
        self.assertNotIn('private-customer-main', json.dumps(output))
        self.assertNotIn('张三', json.dumps(output, ensure_ascii=False))
        self.assertEqual(boundary.restore_arguments('budget', {'account_id': account['id']})['account_id'],
                         'private-customer-main')

    def test_schema_enum_aliases_are_stable_and_original_schema_is_unchanged(self):
        boundary = CloudDataBoundary()
        schemas = [{'type': 'function', 'function': {'name': 'prepare_operation', 'parameters': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'account_id': {'type': 'string', 'enum': ['sensitive-main']},
                'action': {'type': 'string', 'enum': ['transfer']},
                'quote_ids': {'type': 'array', 'items': {'type': 'string', 'enum': ['quote-person']}}},
            'required': ['action', 'account_id']}}}]
        projected = boundary.native_tools(schemas)
        fields = projected[0]['function']['parameters']['properties']
        args = {'action': 'transfer', 'account_id': fields['account_id']['enum'][0],
                'quote_ids': fields['quote_ids']['items']['enum']}
        self.assertEqual(boundary.restore_arguments('prepare_operation', args),
                         {'action': 'transfer', 'account_id': 'sensitive-main', 'quote_ids': ['quote-person']})
        self.assertEqual(fields['action']['enum'], ['transfer'])
        self.assertEqual(schemas[0]['function']['parameters']['properties']['account_id']['enum'], ['sensitive-main'])
        self.assertFalse(projected[0]['function']['parameters']['additionalProperties'])

    def test_names_restore_for_local_operation_without_granting_permission(self):
        boundary = CloudDataBoundary(task='payments')
        projected = boundary.tool_result('business_records', {'data': {'beneficiaries': [
            {'id': 'person-real-id', 'display_name': '陈文华', 'alias': '妈妈', 'verified': True,
             'phone': '+86 13812345678', 'masked_account': '****2001'}]}, 'facts': []})
        recipient = projected['data']['beneficiaries'][0]
        self.assertEqual(recipient['alias'], '妈妈')
        restored = boundary.restore_arguments('prepare_operation', {'action': 'transfer',
            'beneficiary_id': recipient['id'], 'amount_minor': 30000})
        self.assertEqual(restored['beneficiary_id'], 'person-real-id')
        self.assertEqual(restored['amount_minor'], 30000)
        self.assertNotIn('confirmed', restored)
        self.assertNotIn('陈文华', boundary.tts_text(boundary.restore_output(recipient['display_name'])))

    def test_unknown_alias_or_tool_is_controlled_and_does_not_echo_values(self):
        boundary = CloudDataBoundary()
        with self.assertRaises(BoundaryError) as error:
            boundary.restore_arguments('budget', {'account_id': 'obj_0000000000000000'})
        self.assertEqual(error.exception.code, 'BOUNDARY_UNKNOWN_ALIAS')
        self.assertNotIn('000000', str(error.exception))
        result = boundary.tool_result('unregistered_export_all', {'raw_text': '秘密正文'})
        self.assertEqual(result['boundary']['status'], 'withheld')
        self.assertNotIn('秘密正文', json.dumps(result, ensure_ascii=False))

    def test_raw_ledger_is_not_sent_but_local_summary_is(self):
        boundary = CloudDataBoundary(task='statements')
        raw = '\n'.join(f'2026-09-{day:02d},王晓明,999,餐饮' for day in range(1, 15))
        with self.assertRaises(BoundaryError) as error:
            boundary.user_text(raw)
        self.assertEqual(error.exception.code, 'BOUNDARY_RAW_RECORDS')
        projected = boundary.tool_result('monthly_statement', {'data': {
            'month': '2026-09', 'transaction_count': 14,
            'totals_minor': {'income': 650000, 'expense': 13986},
            'category_expense_minor': {'餐饮': 13986},
            'transactions': [{'merchant': '秘密个人商户', 'amount_minor': 999}] * 14,
            'rows': raw, 'unknown_blob': {'secret': raw}}, 'facts': []})
        self.assertEqual(projected['data']['totals_minor']['income'], 650000)
        self.assertEqual(projected['data']['category_expense_minor']['餐饮'], 13986)
        self.assertNotIn('transactions', projected['data'])
        self.assertNotIn('秘密', json.dumps(projected, ensure_ascii=False))

    def test_bulk_record_formats_still_cannot_leave_input_history_or_tts(self):
        rows = [f'2026-09-{day:02d},王晓明,999,餐饮' for day in range(1, 15)]
        formats = [
            '\n'.join(rows),
            '\n'.join(row.replace(',', '\t') for row in rows),
            '\n'.join('| ' + row.replace(',', ' | ') + ' |' for row in rows),
            '\n'.join(row.replace(',', ' ') for row in rows),
            '\n'.join('王晓明,999,餐饮' for _ in rows),
            '\n'.join(json.dumps({'date': f'2026-09-{day:02d}', 'amount_minor': 999,
                                   'merchant': '私人商户原文'}, ensure_ascii=False)
                      for day in range(1, 15)),
        ]
        for raw in formats:
            with self.subTest(format=raw[:12]):
                boundary = CloudDataBoundary(task='statements')
                for method in (boundary.user_text, boundary.tts_text):
                    with self.assertRaises(BoundaryError) as error:
                        method(raw)
                    self.assertEqual(error.exception.code, 'BOUNDARY_RAW_RECORDS')
                self.assertEqual(boundary.history([{'role': 'user', 'content': raw},
                                                   {'role': 'assistant', 'content': raw}]), [])
                result = boundary.tool_result('monthly_statement', {'data': {
                    'summary': raw, 'raw_rows': raw, 'totals_minor': {'expense': 13986}}})
                self.assertEqual(result['data'], {'totals_minor': {'expense': 13986}})

    def test_original_s03_prose_is_not_csv_in_answer_review_history_or_tts(self):
        self.assertEqual(len(_S03_ANSWER), 728)
        self.assertEqual(len(_S03_ANSWER.splitlines()), 15)
        boundary = CloudDataBoundary()
        final = {'text': _S03_ANSWER, 'claims': [], 'draft_ids': [], 'followups': []}
        self.assertEqual(boundary.check_output(final), final)
        normalized = unicodedata.normalize('NFKC', _S03_ANSWER)
        self.assertEqual(boundary.user_text(_S03_ANSWER), normalized)
        self.assertEqual(boundary.history([{'role': 'assistant', 'content': _S03_ANSWER}])[0]['content'],
                         normalized)
        self.assertEqual(boundary.review_packet({'question': '应急缓冲是否充足',
                                                  'draft': final})['draft']['text'], normalized)
        self.assertEqual(boundary.tts_text(_S03_ANSWER), normalized)

    def test_answer_filter_keeps_sensitive_checks_without_input_csv_heuristic(self):
        boundary = CloudDataBoundary(identities=['王晓明'])
        dated_prose = '\n'.join(f'{day}. 2026-09-{day:02d}的资料已经过期，需要核对新版本。'
                                for day in range(1, 16))
        self.assertEqual(boundary.check_output({'text': dated_prose})['text'], dated_prose)
        for private in ('验证码：123456', 'password=unsafe-secret',
                        '手机13812345678', 'user@example.com', 'account-foreign-person-0001'):
            with self.subTest(private=private), self.assertRaises(BoundaryError) as error:
                boundary.check_output({'text': dated_prose + '\n' + private})
            self.assertEqual(error.exception.code, 'BOUNDARY_UNKNOWN_ALIAS'
                             if private.startswith('account-') else 'BOUNDARY_OUTPUT')
        safe_tts = boundary.tts_text(dated_prose + '\n王晓明，手机13812345678。')
        self.assertNotIn('王晓明', safe_tts)
        self.assertNotIn('13812345678', safe_tts)

    def test_private_list_limit_and_unknown_nested_fields(self):
        boundary = CloudDataBoundary(max_private_items=3)
        result = boundary.tool_result('account_summary', {'data': {'accounts': [
            {'id': f'a{n}', 'currency': 'CNY', 'balance_minor': n, 'unknown_private': 'secret'}
            for n in range(20)]}, 'facts': []})
        self.assertEqual(len(result['data']['accounts']), 3)
        self.assertGreater(boundary.audit_summary()['omitted'], 0)
        self.assertNotIn('secret', json.dumps(result))

    def test_excluded_rows_cannot_return_as_facts_or_reviewer_evidence(self):
        boundary = CloudDataBoundary(task='statements')
        data = {'totals_minor': {'expense': 30000},
                'transactions': [{'amount_minor': 1234, 'merchant': '私人商户原文'}]}
        facts = [{'id': 'sum', 'evidence_id': 'e1', 'value': 30000, 'currency': 'CNY',
                  'unit': 'minor_currency', 'text': '私人商户原文 · 支出300元'},
                 {'id': 'raw', 'evidence_id': 'e1', 'value': 1234, 'currency': 'CNY',
                  'unit': 'minor_currency', 'text': '原始流水'}]
        exported = boundary.tool_result('monthly_statement', {'evidence_id': 'e1', 'data': data, 'facts': facts})
        self.assertEqual([f['value'] for f in exported['facts']], [30000])
        self.assertNotIn('私人商户', json.dumps(exported, ensure_ascii=False))
        packet = boundary.review_packet({'question': '本月总支出是多少',
            'evidence': [{'id': 'e1', 'tool': 'monthly_statement', 'data': data}],
            'monetary_evidence': facts, 'draft': {'text': '已核对本月支出。'}})
        self.assertEqual([f['value'] for f in packet['monetary_evidence']], [30000])
        self.assertNotIn('1234', json.dumps(packet))

    def test_calendar_summary_keeps_required_money_and_coverage(self):
        boundary = CloudDataBoundary(task='calendar')
        result = boundary.tool_result('calendar_query', {'data': {
            'summary_by_currency': [{'currency': 'CNY', 'recorded_outflow_minor': 320000,
                                    'confirmed_due_minor': 180000, 'investment_pnl_minor': None,
                                    'pnl_coverage': 'unknown', 'raw_rows': '私人流水'}],
            'coverage': 'partial'}, 'facts': []})
        total = result['data']['summary_by_currency'][0]
        self.assertEqual(total['recorded_outflow_minor'], 320000)
        self.assertIsNone(total['investment_pnl_minor'])
        self.assertEqual(total['pnl_coverage'], 'unknown')
        self.assertNotIn('raw_rows', total)

    def test_task_switch_blocks_previous_private_domain(self):
        boundary = CloudDataBoundary(task='account')
        self.assertIn('data', boundary.tool_result('account_summary', {'data': {'accounts': []}}))
        boundary.set_task('public_research')
        self.assertEqual(boundary.tool_result('account_summary', {'data': {'balance_minor': 30000}})['boundary']['status'], 'withheld')
        context = boundary.server_context({'calendar_events': [{'title': '私人生日'}],
            'recent_operations': [{'id': 'private-operation'}], 'environment': 'simulation',
            'market_context': {'kind': 'article', 'id': 'public-article'}})
        self.assertEqual(set(context), {'environment', 'market_context'})

    def test_public_sources_and_product_names_survive_projection(self):
        boundary = CloudDataBoundary(task='comparison')
        result = boundary.tool_result('knowledge_search', {'evidence_id': 'e-public', 'data': {'hits': [
            {'product_id': 'PUBLIC-FUND-A', 'name': '公开债券基金A', 'publisher': '公开发行机构',
             'excerpt': '赎回费率为0.5%。', 'url': 'https://example.org/products/a',
             'email': 'private@example.org', 'arbitrary_private_payload': '私人持仓'}]}, 'facts': []})
        hit = result['data']['hits'][0]
        self.assertEqual(hit['name'], '公开债券基金A')
        self.assertEqual(hit['publisher'], '公开发行机构')
        self.assertEqual(hit['product_id'], 'PUBLIC-FUND-A')
        self.assertEqual(boundary.public_request({'query': '公开债券基金A赎回费用', 'product_id': 'PUBLIC-FUND-A'}),
                         {'query': '公开债券基金A赎回费用', 'product_id': 'PUBLIC-FUND-A'})
        self.assertNotIn('私人持仓', json.dumps(result, ensure_ascii=False))

    def test_public_canonical_evidence_keeps_locality_dates_and_version(self):
        boundary = CloudDataBoundary(task='public_research')
        evidence = {'citation_id': 'public-citation', 'document_id': 'public-document',
            'document_version_id': 'public-version', 'parse_id': 'public-parse', 'span_id': 'public-span',
            'publisher': '公开发行机构', 'published_on': '2026-09-01', 'retrieved_on': '2026-10-06',
            'effective_from': '2026-09-01', 'effective_to': None, 'visibility': 'public',
            'outbound_allowed': True, 'content_status': 'full_text', 'withdrawn': False,
            'locator': {'page': 2, 'paragraph': '费用/第一段', 'start_char': 3, 'end_char': 12}}
        hit = boundary.tool_result('knowledge_search', {'data': {'hits': [evidence]}})['data']['hits'][0]
        self.assertEqual(hit, evidence)

    def test_public_request_rejects_private_portfolios_and_amounts(self):
        boundary = CloudDataBoundary()
        for args in ({'query': '我持有基金20万元怎么配'}, {'query': '账户现金流'},
                     {'query': '理财', 'holdings': [{'amount_minor': 30000}]},
                     {'query': '理财', 'product_id': 'unverified-private-product'},
                     {'query': '王晓明的基金'}, {'query': '未知机构私密文件'}):
            with self.subTest(args=args), self.assertRaises(BoundaryError) as error:
                boundary.public_request(args)
            self.assertEqual(error.exception.code, 'BOUNDARY_PUBLIC_QUERY')
        self.assertEqual(boundary.public_request({'query': '货币基金费率', 'facets': ['fees'], 'limit': 2})['query'],
                         '货币基金费率')

    def test_original_public_smoke_question_and_tool_arguments_are_preserved(self):
        boundary = CloudDataBoundary(task='public_research')
        question = ('请仅使用knowledge_status和knowledge_search核查本地知识库中理财产品的风险原文。'
                    '原文过期或缺失就明确说明缺口，不能推荐具体产品或引用收益数字，也不要查询个人账户。')
        self.assertEqual(boundary.user_text(question), unicodedata.normalize('NFKC', question))
        self.assertEqual(boundary.local_source(boundary.source_ref(question)), question)
        args = {'query': '理财产品 风险等级 风险揭示 条款原文',
                'facets': ['risk', 'terms'], 'limit': 8}
        self.assertEqual(boundary.public_request(args), args)
        for query in ('产品的赎回条件', '请帮我核查理财产品的风险原文',
                      '请问，产品赎回多久可以到账？', '本地知识库中的风险揭示原文是否过期'):
            with self.subTest(query=query):
                self.assertEqual(boundary.public_request({'query': query})['query'], query)

    def test_public_request_clamps_only_positive_limits_not_unknown_entities(self):
        boundary = CloudDataBoundary(task='public_research')
        boundary.register_public_terms(['官方的债券基金A'], ['PUBLIC-A'])
        args = {'query': '官方的债券基金A赎回条件', 'product_id': 'PUBLIC-A', 'limit': 10}
        self.assertEqual(boundary.public_request(args), {**args, 'limit': 8})
        self.assertEqual(args['limit'], 10)
        for invalid in (0, -1, True, 8.0, '10', None):
            with self.subTest(limit=invalid), self.assertRaises(BoundaryError):
                boundary.public_request({'query': '产品的赎回条件', 'limit': invalid})
        for args in ({'query': '产品赎回条件', 'product_id': 'UNKNOWN-A'},
                     {'query': '产品赎回条件', 'product_id': {'id': 'PUBLIC-A'}},
                     {'query': '产品赎回条件', 'facets': [{}]},
                     {'query': '请帮我查看我的持仓基金的赎回条件'},
                     {'query': '请帮我核查账户余额20万元的风险'},
                     {'query': '王晓明的基金风险等级'},
                     {'query': '产品风险 password=unsafe-secret'},
                     {'query': '产品风险 account-foreign-person-0001'},
                     {'query': '未知私有产品的赎回条件'},
                     {'query': '产品风险', 'raw_text': '不准发送原文'}):
            with self.subTest(args=args), self.assertRaises(BoundaryError):
                boundary.public_request(args)

    def test_output_references_are_restored_then_checked_locally(self):
        boundary = CloudDataBoundary()
        projected = boundary.tool_result('prepare_operation', {'evidence_id': 'e1', 'data': {
            'operation': {'id': 'op1', 'state': 'pending_confirmation'}, 'widget_id': 'w1'}, 'facts': []})
        final = {'text': '请核对草案。', 'claims': [{'text': '来自本轮记录', 'evidence_ids': [projected['evidence_id']]}],
                 'draft_ids': [projected['data']['operation']['id']], 'widget_ids': [projected['data']['widget_id']],
                 'followups': []}
        restored = boundary.check_output(final, evidence_ids=['e1'], draft_ids=['op1'], widget_ids=['w1'])
        self.assertEqual(restored['draft_ids'], ['op1'])
        self.assertEqual(restored['claims'][0]['evidence_ids'], ['e1'])
        with self.assertRaises(BoundaryError):
            boundary.check_output(final, evidence_ids=['different'])
        with self.assertRaises(BoundaryError):
            boundary.check_output({'text': '验证码是123456', 'claims': [], 'draft_ids': [], 'followups': []})

    def test_contract_feedback_preserves_ids_and_safe_instructions(self):
        boundary = CloudDataBoundary()
        output = boundary.server_context({'contract_error': 'INVALID_EVIDENCE_REFERENCE',
            'valid_evidence_ids': ['local-evidence'], 'valid_draft_ids': ['local-draft'],
            'instruction': '请按本轮证据修正', 'raw_private_content': '秘密'})
        self.assertEqual(output['contract_error'], 'INVALID_EVIDENCE_REFERENCE')
        self.assertTrue(output['valid_evidence_ids'][0].startswith('obj_'))
        self.assertEqual(boundary.restore_output(output)['valid_draft_ids'], ['local-draft'])
        self.assertNotIn('秘密', json.dumps(output, ensure_ascii=False))

    def test_original_source_binding_is_local_and_audit_is_counts_only(self):
        boundary = CloudDataBoundary(identities=['王晓明'])
        raw = '王晓明计划投入50000元，邮箱personal@example.com。'
        boundary.user_text(raw)
        ref = boundary.source_ref(raw)
        self.assertEqual(boundary.local_source(ref), raw)
        summary = boundary.audit_summary()
        self.assertEqual(summary['policy_version'], POLICY_VERSION)
        self.assertTrue(all(type(v) is int for k, v in summary.items() if k != 'policy_version'))
        self.assertNotIn('王晓明', json.dumps(summary, ensure_ascii=False))
        self.assertNotIn('personal', json.dumps(summary))
        other = CloudDataBoundary(identities=['王晓明'])
        self.assertNotEqual(boundary.user_text('王晓明'), other.user_text('王晓明'))
        with self.assertRaises(BoundaryError):
            other.local_source(ref)

    def test_private_identifier_registration_precedes_history_and_context(self):
        boundary = CloudDataBoundary()
        identifiers = ['12345678abcdef0012345678abcdef00-main', 'my-private-workspace',
                       'recipient-record', 'calendar-widget', 'operation-record']
        boundary.register_private_identifiers(identifiers)
        raw = json.dumps({'account_id': identifiers[0], 'workspace_id': identifiers[1],
            'beneficiary_id': identifiers[2], 'widget_id': identifiers[3],
            'operation_id': identifiers[4]}, ensure_ascii=False)
        projected = boundary.history([{'role': 'assistant', 'content': raw}])[0]['content']
        for identifier in identifiers:
            self.assertNotIn(identifier, projected)
        self.assertEqual(boundary.restore_output(projected), raw)
        self.assertEqual(boundary.user_text(identifiers[0]),
                         json.loads(projected)['account_id'])

    def test_unknown_raw_identifiers_are_withheld_and_public_catalog_ids_survive(self):
        boundary = CloudDataBoundary()
        public_id = 'f1234567890abcdef1234567890abcdef'
        boundary.register_public_terms(identifiers=[public_id])
        boundary.register_private_identifiers([public_id, 'owner-account'])
        self.assertEqual(boundary.user_text(public_id), public_id)
        unknown = ['00112233445566778899aabbccddeeff-main',
                   '12345678-abcd-abcd-abcd-1234567890ab', 'account-other-person-0001']
        for value in unknown:
            self.assertNotIn(value, boundary.user_text(value))
        self.assertEqual(boundary.restore_output(boundary.user_text(unknown[0])), '[本地已屏蔽]')
        with self.assertRaises(BoundaryError):
            boundary.restore_arguments('budget', {'account_id': unknown[0]})
        late_public = CloudDataBoundary()
        late_public.register_private_identifiers([public_id])
        late_public.register_public_terms(identifiers=[public_id])
        self.assertEqual(late_public.user_text(public_id), public_id)

    def test_invalid_values_and_unknown_schema_fail_closed(self):
        boundary = CloudDataBoundary()
        self.assertEqual(boundary.tool_result('monthly_statement', {'raw_blob': b'private'})['boundary']['status'], 'withheld')
        with self.assertRaises(BoundaryError):
            boundary.user_text({'query': 'private'})
        with self.assertRaises(BoundaryError):
            boundary.native_tools([{'function': {'name': 'export_secrets', 'parameters': {}}}])
        result = boundary.tool_result('money_calculation', {'data': {'result_minor': float('nan'), 'currency': 'CNY'}})
        self.assertNotIn('result_minor', result['data'])
        malformed = boundary.tool_result('account_summary', {'data': {
            'balance_minor': {'name': '不能借金额字段发送正文'}, 'risk_level': '原始私人资料'}})
        self.assertNotIn('balance_minor', malformed.get('data', {}))
        self.assertNotIn('原始私人资料', json.dumps(malformed, ensure_ascii=False))

    def test_six_public_summaries_are_prose_but_raw_rows_still_rejected(self):
        summaries = '\n\n'.join(
            f'{n}. 公开文章摘要：2026-10-0{n}发布，原文列示5,000元条件，尚待核对生效版本。\n'
            '分析说明：这只是政策原文中的金额，不代表本人已获补助，也不能用作账户可用余额。'
            + '本摘要只列公开内容与待核验条件。' * 20
            for n in range(1, 7))
        self.assertGreater(len(summaries), 2000)
        boundary = CloudDataBoundary(task='public_research')
        normalized = unicodedata.normalize('NFKC', summaries)
        self.assertEqual(boundary.check_output({'text': summaries})['text'], summaries)
        self.assertEqual(boundary.user_text(summaries), normalized)
        self.assertEqual(boundary.review_packet({'draft': {'text': summaries}})['draft']['text'], normalized)
        for raw in ('\n'.join(f'2026-10-{n:02d},王晓明,999,餐饮' for n in range(1, 15)),
                    '\n'.join('王晓明,999,餐饮' for _ in range(14)),
                    '\n'.join('2026-10-03\t999\t私人商户' for _ in range(14))):
            with self.subTest(format=raw.splitlines()[0]), self.assertRaises(BoundaryError) as error:
                boundary.check_output({'text': raw})
            self.assertEqual(error.exception.code, 'BOUNDARY_RAW_RECORDS')

    def test_load_skill_is_a_dedicated_registered_document_projection(self):
        # Skill examples intentionally resemble records; only this local tool's
        # reviewed document path can supply instruction prose of this shape.
        instructions = '# 方法\n' + '\n'.join('2026-10-03,999,示例' for _ in range(12))
        instructions += '\npassword=synthetic-secret\n联系private@example.test'
        data = {'skill': 'cashflow', 'reference': None, 'instructions': instructions,
                'references': ['cashflow-rules.md', '../unsafe.md'],
                'tools': ['cashflow', 'budget', 'export_secrets'], 'private_extension': '不准发送'}
        boundary = CloudDataBoundary()
        result = boundary.tool_result('load_skill', {'data': data, 'facts': []})['data']
        self.assertIn('# 方法', result['instructions'])
        self.assertIn('2026-10-03,999,示例', result['instructions'])
        self.assertEqual(result['skill'], 'cashflow')
        self.assertEqual(result['references'], ['cashflow-rules.md'])
        self.assertEqual(result['tools'], ['cashflow', 'budget'])
        self.assertIsNone(result['reference'])
        for private in ('synthetic-secret', 'private@example.test', '不准发送'):
            self.assertNotIn(private, json.dumps(result, ensure_ascii=False))
        self.assertNotIn('instructions', boundary.tool_result('money_calculation', {'data': data})['data'])
        with self.assertRaises(BoundaryError):
            boundary.native_tools([{'function': {'name': 'export_secrets', 'parameters': {}}}])

    def test_all_reported_public_query_phrases_preserve_original(self):
        boundary = CloudDataBoundary(task='public_research')
        for query in ('产品的赎回条件', '复查补助',
                      '产品说明书 风险揭示书 风险等级 适合投资者'):
            with self.subTest(query=query):
                self.assertEqual(boundary.public_request({'query': query}), {'query': query})
        for query in ('复查王晓明的补助', '我有5000元补助', '账户适合投资者',
                      '未知私有机构产品说明书', '产品password=synthetic-secret'):
            with self.subTest(query=query), self.assertRaises(BoundaryError):
                boundary.public_request({'query': query})

    def test_public_major_to_minor_retains_exact_quote_identity_for_both_models(self):
        hit, fact = public_amount_fixture()
        data = {'hits': [hit]}
        boundary = CloudDataBoundary(task='public_research')
        result = boundary.tool_result('knowledge_search', {'evidence_id': fact['evidence_id'],
            'data': data, 'facts': [fact]})
        self.assertEqual(len(result['facts']), 1)
        proof = result['facts'][0]
        self.assertEqual((proof['value'], proof['unit'], proof['currency']), (500000, 'public_minor_currency', 'CNY'))
        self.assertEqual(proof['citation_id'], hit['citation_id'])
        self.assertEqual(proof['text'], unicodedata.normalize('NFKC', hit['excerpt']))
        self.assertEqual(proof['scope'], 'public_statement_not_account_balance')
        self.assertTrue(proof['id'].startswith('obj_'))
        self.assertEqual(boundary.restore_output(proof)['id'], fact['id'])
        packet = boundary.review_packet({'evidence': [{'id': fact['evidence_id'],
            'tool': 'knowledge_search', 'data': data}], 'monetary_evidence': [fact],
            'draft': {'text': '原文补助为5000元，资格待核实。'}})
        self.assertEqual(packet['monetary_evidence'][0]['value'], 500000)
        self.assertEqual(packet['monetary_evidence'][0]['citation_id'], hit['citation_id'])
        self.assertEqual(packet['evidence'][0]['data']['hits'][0], hit)
        self.assertEqual(packet['monetary_evidence'][0]['evidence_id'], result['evidence_id'])

    def test_public_amount_needs_same_citation_active_disclosure_and_currency(self):
        original, fact = public_amount_fixture()
        variants = [({**original, 'citation_id': 'other-citation'}, fact),
                    ({**original, 'visibility': 'private'}, fact),
                    ({**original, 'outbound_allowed': False}, fact),
                    ({**original, 'withdrawn': True}, fact),
                    ({**original, 'content_status': 'metadata_only', 'page': 500000}, fact),
                    (original, {**fact, 'currency': 'USD'}),
                    (original, {**fact, 'value': 500001}),
                    (original, {**fact, 'citation_id': None})]
        for hit, candidate in variants:
            with self.subTest(hit=hit['citation_id'], currency=candidate['currency']):
                boundary = CloudDataBoundary(task='public_research')
                data = {'hits': [hit], 'count': 500000}
                result = boundary.tool_result('knowledge_search', {'data': data, 'facts': [candidate]})
                self.assertEqual(result['facts'], [])
                packet = boundary.review_packet({'evidence': [{'id': fact['evidence_id'],
                    'tool': 'knowledge_search', 'data': data}], 'monetary_evidence': [candidate]})
                self.assertEqual(packet['monetary_evidence'], [])

    def test_canonical_retrieval_return_can_omit_already_checked_withdrawal_flag(self):
        hit, fact = public_amount_fixture()
        hit.pop('withdrawn')
        boundary = CloudDataBoundary(task='public_research')
        self.assertEqual(boundary.tool_result('knowledge_search', {'data': {'hits': [hit]},
            'facts': [fact]})['facts'][0]['value'], 500000)
        for field in ('release_id', 'document_version_id', 'parse_id', 'span_id', 'source_url', 'locator'):
            partial = {key: value for key, value in hit.items() if key != field}
            with self.subTest(missing=field):
                self.assertEqual(boundary.tool_result('knowledge_search', {'data': {'hits': [partial]},
                    'facts': [fact]})['facts'], [])

    def test_public_conversion_has_exact_integer_units_and_no_private_shortcut(self):
        hit, fact = public_amount_fixture()
        for excerpt, amount, currency in (('原文金额为5,000.00元。', 500000, 'CNY'),
                                          ('原文金额为1.4万亿元。', 140000000000000, 'CNY'),
                                          ('原文金额为50美元。', 5000, 'USD')):
            with self.subTest(excerpt=excerpt):
                boundary = CloudDataBoundary(task='public_research')
                result = boundary.tool_result('knowledge_search', {'data': {'hits': [{**hit, 'excerpt': excerpt}]},
                    'facts': [{**fact, 'value': amount, 'currency': currency}]})
                self.assertEqual(result['facts'][0]['value'], amount)
        for excerpt in ('原文金额为0.001元。', '原文金额为5,00元。', '没有金额的元数据。'):
            boundary = CloudDataBoundary(task='public_research')
            self.assertEqual(boundary.tool_result('knowledge_search', {'data': {'hits': [{**hit, 'excerpt': excerpt}]},
                'facts': [fact]})['facts'], [])
        boundary = CloudDataBoundary(task='statements')
        private = {'totals_minor': {'expense': 30000}, 'transactions': [{'amount_minor': 500000}]}
        self.assertEqual(boundary.tool_result('monthly_statement', {'data': private, 'facts': [fact]})['facts'], [])

    def test_existing_hidden_marker_never_masks_a_new_credential(self):
        boundary = CloudDataBoundary()
        self.assertEqual(boundary.check_output({'text': '[本地已屏蔽]；剩余资料待核对。'})['text'],
                         '[本地已屏蔽]；剩余资料待核对。')
        for field in ('text', 'followups', 'claims'):
            final = {'text': '[本地已屏蔽]；金额待核对。', 'followups': [], 'claims': []}
            credential = '[本地已屏蔽] password=synthetic-secret'
            final[field] = credential if field == 'text' else [credential] if field == 'followups' else [{'text': credential}]
            with self.subTest(field=field), self.assertRaises(BoundaryError) as error:
                boundary.check_output(final)
            self.assertEqual(error.exception.code, 'BOUNDARY_OUTPUT')
        # The address scrub overlaps both the old marker and a new password;
        # marker counts alone would incorrectly remain unchanged.
        with self.assertRaises(BoundaryError) as error:
            boundary.check_output({'text': '地址：[本地已屏蔽] password=synthetic-secret。'})
        self.assertEqual(error.exception.code, 'BOUNDARY_OUTPUT')

    def test_complete_metric_projection_aliases_identity_and_restores_all_signed_fields(self):
        boundary = CloudDataBoundary(task='cashflow')
        metric = financial_metric_fixture()
        record = {'metric_id': metric['metric_id'], 'metric': metric,
                  'kind': 'recorded_projection', 'evidence_ids': ['synthetic-evidence-1']}
        result = boundary.tool_result('cashflow', {'data': {'minimum_available_minor': 984400}, 'metrics': [record]})
        projected = result['metrics'][0]
        self.assertEqual(set(projected['metric']), set(metric))
        self.assertEqual(projected['metric_id'], projected['metric']['metric_id'])
        self.assertEqual(projected['evidence_ids'], projected['metric']['input_refs'])
        self.assertEqual(boundary.restore_output(projected), record)
        self.assertEqual(boundary.user_text('底线之外仍有9,844.00元。'), '底线之外仍有9,844.00元。')
        self.assertNotIn('synthetic-account-main', json.dumps(projected))
        self.assertNotIn('a' * 64, json.dumps(projected))
        # Named service maps use the same contract and task/domain policy.
        nested = boundary.tool_result('cashflow', {'data': {'metrics': {'confirmed_minimum': metric}}})
        self.assertEqual(boundary.restore_output(nested['data']['metrics']['confirmed_minimum']), metric)
        self.assertEqual(CloudDataBoundary(task='public_research').tool_result('cashflow',
            {'data': {}, 'metrics': [record]})['boundary']['status'], 'withheld')

    def test_metric_missing_is_not_zero_and_malformed_snapshot_is_atomic(self):
        boundary = CloudDataBoundary(task='cashflow')
        unavailable = financial_metric_fixture(value_minor=None, calculation_status='unavailable',
                                               coverage_gaps=['income_coverage_unknown'])
        safe = boundary.tool_result('cashflow', {'data': {'metrics': [unavailable]}})['data']['metrics'][0]
        self.assertIsNone(safe['value_minor'])
        self.assertEqual(safe['coverage_gaps'], ['income_coverage_unknown'])
        variants = [financial_metric_fixture(value_minor=None), financial_metric_fixture(value_minor=True),
                    financial_metric_fixture(value_minor=float('nan')), financial_metric_fixture(period_end='2026-02-30'),
                    financial_metric_fixture(assumed_inputs=[{'raw_text': '私人原始流水'}]),
                    financial_metric_fixture(source_versions={'bank': 'password=synthetic-secret'}),
                    financial_metric_fixture(coverage_gaps=['password=synthetic-secret'])]
        incomplete = financial_metric_fixture()
        incomplete.pop('excluded_items')
        variants.append(incomplete)
        for metric in variants:
            with self.subTest(fields=list(metric)):
                row = boundary.tool_result('cashflow', {'data': {'metrics': [metric]}})['data']['metrics'][0]
                self.assertEqual(row['boundary']['status'], 'withheld')
                self.assertNotIn('metric_id', row)
                self.assertNotIn('私人原始流水', json.dumps(row, ensure_ascii=False))
                self.assertNotIn('synthetic-secret', json.dumps(row))

    def test_metric_collection_obeys_existing_private_item_limit(self):
        boundary = CloudDataBoundary(task='cashflow', max_private_items=3)
        metric = financial_metric_fixture()
        result = boundary.tool_result('cashflow', {'data': {'metrics': [metric] * 20}})
        self.assertEqual(len(result['data']['metrics']), 3)
        self.assertGreaterEqual(boundary.audit_summary()['omitted'], 17)
        self.assertTrue(all(row.get('value_minor') == 984400 for row in result['data']['metrics']))

    def test_only_declared_method_assumption_structures_survive(self):
        boundary = CloudDataBoundary(task='cashflow')
        assumptions = [{'type': 'user_planning_allocation', 'reserve_id': 'reserve-private-1', 'target_minor': 100000},
            {'type': 'user_stress_assumption', 'event_id': 'event-private-1', 'original_on': '2026-10-03',
             'assumed_on': '2026-10-04', 'original_amount_minor': 10000, 'assumed_amount_minor': 20000, 'probability': None},
            {'type': 'recorded_forecast_candidate', 'event_id': 'event-private-2', 'notice': '已记录预测，只进入候选分支。'},
            {'kind': 'method_limit', 'future_proceeds': 'not_projected', 'execution': 'planning_only'}]
        metric = financial_metric_fixture(assumed_inputs=assumptions)
        safe = boundary.tool_result('cashflow', {'data': {'metrics': [metric]}})['data']['metrics'][0]
        self.assertEqual(boundary.restore_output(safe), metric)
        for invalid in ({**assumptions[0], 'raw_text': '私人资料'},
                        {**assumptions[1], 'probability': 0.8},
                        {**assumptions[2], 'notice': 'password=synthetic-secret'},
                        {**assumptions[3], 'execution': 'already_executed'}):
            with self.subTest(type=invalid.get('type', invalid.get('kind'))):
                result = boundary.tool_result('cashflow', {'data': {'metrics': [financial_metric_fixture(assumed_inputs=[invalid])]}})
                self.assertEqual(result['data']['metrics'][0]['boundary']['status'], 'withheld')

    def test_amount_contract_to_reviewer_keeps_complete_snapshot_and_binding(self):
        boundary = CloudDataBoundary(task='cashflow')
        metric = financial_metric_fixture()
        fields = ('metric_id', 'subject_id', 'metric_name', 'currency', 'unit', 'value_minor', 'as_of',
                  'period_start', 'period_end', 'data_scope', 'calculation_status', 'basis', 'formula_id', 'source_versions')
        text = '底线外最低余量9,844.00元。'
        claim = {**{key: metric[key] for key in fields}, 'text': text,
                 'claim_kind': 'recorded_projection', 'occurrence_start': 0}
        record = {'metric_id': metric['metric_id'], 'metric': metric, 'kind': 'recorded_projection',
                  'evidence_ids': ['synthetic-evidence-1'], 'claim': claim}
        contract = {'status': 'validated', 'amount_claims': [record], 'amount_occurrence_count': 1,
                    'independent_review_required': True}
        packet = boundary.review_packet({'draft': {'text': text, 'claims': [], 'amount_claims': [claim]},
                                         'amount_contract': contract})
        self.assertEqual(boundary.restore_output(packet['amount_contract']), contract)
        self.assertEqual(boundary.restore_output(packet['draft']['amount_claims']), [claim])
        self.assertEqual(packet['amount_contract']['amount_claims'][0]['metric'],
                         boundary.tool_result('cashflow', {'data': {'metrics': [metric]}})['data']['metrics'][0])
        forged = deepcopy(contract)
        forged['amount_claims'][0]['metric']['assumed_inputs'] = [{'raw_text': '私人资料'}]
        withheld = boundary.review_packet({'draft': {'text': text}, 'amount_contract': forged})['amount_contract']
        self.assertEqual(withheld['boundary']['status'], 'withheld')
        self.assertNotIn('status', withheld)

    def test_alias_and_nfkc_offset_changes_keep_exact_amount_occurrence(self):
        boundary = CloudDataBoundary(task='cashflow', identities=['王晓明'])
        metric = financial_metric_fixture()
        projected_metric = boundary.tool_result('cashflow', {'data': {'metrics': [metric]}})['data']['metrics'][0]
        signature = {key: projected_metric[key] for key in ('metric_id', 'subject_id', 'metric_name', 'currency',
            'unit', 'value_minor', 'as_of', 'period_start', 'period_end', 'data_scope', 'calculation_status',
            'basis', 'formula_id', 'source_versions')}
        prefix = boundary.user_text('王晓明，')
        fragment = '最低余量９，８４４．００元。'
        final = {'text': prefix + fragment, 'claims': [], 'amount_claims': [{**signature, 'text': fragment,
                 'claim_kind': 'recorded_projection', 'occurrence_start': len(prefix)}]}
        restored = boundary.restore_arguments('answer_reviewed', final)
        self.assertEqual(restored['amount_claims'][0]['occurrence_start'], len('王晓明,'))
        checked = boundary.check_output(restored)
        self.assertEqual(checked['text'], '王晓明,最低余量9,844.00元。')
        claim = checked['amount_claims'][0]
        start = claim['occurrence_start']
        self.assertEqual(checked['text'][start:start + len(claim['text'])], claim['text'])
        packet = boundary.review_packet({'draft': checked})
        cloud_claim = packet['draft']['amount_claims'][0]
        start = cloud_claim['occurrence_start']
        self.assertEqual(packet['draft']['text'][start:start + len(cloud_claim['text'])], cloud_claim['text'])
        bad = deepcopy(final)
        bad['amount_claims'][0]['occurrence_start'] += 1
        with self.assertRaises(BoundaryError):
            boundary.restore_arguments('answer_reviewed', bad)

    def test_metric_native_enum_and_unknown_version_key_alias_stay_controlled(self):
        boundary = CloudDataBoundary()
        metric = financial_metric_fixture()
        boundary.tool_result('cashflow', {'data': {'metrics': [metric]}})
        schema = [{'function': {'name': 'answer_reviewed', 'parameters': {'properties': {
            'amount_claims': {'items': {'properties': {'metric_id': {'enum': [metric['metric_id']]}}}}}}}}]
        projected = boundary.native_tools(schema)
        token = projected[0]['function']['parameters']['properties']['amount_claims']['items']['properties']['metric_id']['enum'][0]
        self.assertEqual(boundary.restore_arguments('answer_reviewed', {'metric_id': token})['metric_id'], metric['metric_id'])
        with self.assertRaises(BoundaryError):
            boundary.restore_arguments('answer_reviewed', {'source_versions': {'obj_0123456789abcdef': '1'}})

    def test_service_rules_preserve_complete_cost_conditions_and_deadlines(self):
        boundary = CloudDataBoundary()
        rules = service_rule_fixture()
        original = deepcopy(rules)
        rules['revision'] = 'rule-revision-7'
        rules['offers'][0]['unknown_merchant'] = {'client_id': 'historical-client-37', 'email': 'a@example.com'}
        safe = boundary.tool_result('service_offer_rules', {'data': rules, 'facts': []})['data']
        self.assertEqual(safe['shared_memory'], [])
        offer = safe['offers'][0]
        for key in set(original['offers'][0]) - {'offer_id'}:
            self.assertEqual(offer[key], original['offers'][0][key])
        self.assertRegex(offer['offer_id'], r'^obj_[a-f0-9]{16}$')
        self.assertRegex(safe['revision'], r'^obj_[a-f0-9]{16}$')
        self.assertNotIn('historical-client-37', json.dumps(safe))
        self.assertNotIn('example.com', json.dumps(safe))
        self.assertNotIn(original['offers'][0]['offer_id'], json.dumps(safe))
        # A service offer is a local synthetic business ID, not a public search ID.
        with self.assertRaises(BoundaryError):
            boundary.public_request({'product_id': offer['offer_id']})

    def test_service_preview_never_exports_existing_memories_or_participants(self):
        boundary = CloudDataBoundary()
        raw = service_preview_fixture()
        raw['shared_memory'] = [{'node_id': 'old-client-memory', 'category': 'goal',
            'value': '私人医疗目标', 'password': 'memory-password-37'}]
        raw.update(memory_revision='personal-revision-4', share_memory=True,
                   customer_id='historical-client-37', current_request='password=secret-service-37')
        original = deepcopy(raw)
        safe = boundary.tool_result('service_offer_preview', raw)
        self.assertEqual(safe['shared_memory'], [])
        self.assertEqual(safe['conditions'], raw['conditions'])
        self.assertEqual(safe['expires_in_seconds'], 900)
        self.assertEqual(safe['offers'][0]['eligibility_status'], 'conditional_application')
        self.assertEqual(safe['offers'][0]['investment_purchase_required'], False)
        self.assertEqual(safe['offers'][0]['conditions'], raw['offers'][0]['conditions'])
        for key in ('draft_id', 'rule_version', 'memory_revision'):
            self.assertRegex(safe[key], r'^obj_[a-f0-9]{16}$')
        serialized = json.dumps(safe, ensure_ascii=False)
        for value in ('old-client-memory', '私人医疗目标', 'memory-password-37', 'historical-client-37',
                      'secret-service-37', 'simulated_bank_benefit_agent-v1', 'personal-revision-4'):
            self.assertNotIn(value, serialized)
        self.assertNotIn('participants', safe)
        self.assertNotIn('share_memory', safe)
        self.assertEqual(raw, original)

    def test_service_application_aliases_link_with_preview_only_in_this_turn(self):
        boundary = CloudDataBoundary()
        preview = boundary.tool_result('service_offer_preview', service_preview_fixture())
        raw = service_application_fixture()
        raw['applications'][0]['client_id'] = 'historical-client-37'
        safe = boundary.tool_result('service_application_status', raw)
        application = safe['applications'][0]
        self.assertEqual(application['draft_id'], preview['draft_id'])
        self.assertEqual(application['offer_id'], preview['offers'][0]['offer_id'])
        self.assertEqual(application['terms']['offer_id'], application['offer_id'])
        self.assertEqual(application['status'], 'submitted_simulation_pending_eligibility')
        self.assertEqual(application['terms']['monthly_value_minor'], 1000)
        self.assertEqual(application['terms']['max_cost_minor'], 6000)
        self.assertEqual(application['terms']['minimum_monthly_salary_minor'], 600000)
        self.assertFalse(application['idempotent_replay'])
        self.assertEqual(safe['shared_memory'], [])
        self.assertNotIn('historical-client-37', json.dumps(safe))
        self.assertEqual(boundary.restore_output(application)['application_id'], '2' * 32)
        with self.assertRaises(BoundaryError):
            CloudDataBoundary().restore_output(application)

    def test_service_incomplete_conditions_credentials_and_bad_amounts_are_withheld(self):
        for mutate in (lambda row: row.pop('conditions'),
                       lambda row: row['eligibility'].update({'unreviewed_client_condition': True}),
                       lambda row: row.update(title='费用方案 password=secret-service-37'),
                       lambda row: row['conditions'].append('旧客户 account_historical1234'),
                       lambda row: row.update(monthly_value_minor=True),
                       lambda row: row.update(max_cost_minor=float('nan'))):
            raw = service_rule_fixture()
            mutate(raw['offers'][0])
            safe = CloudDataBoundary().tool_result('service_offer_rules', raw)
            self.assertEqual(set(safe['offers'][0]), {'boundary'})
            self.assertEqual(safe['offers'][0]['boundary']['status'], 'withheld')
            self.assertNotIn('secret-service-37', json.dumps(safe))
            self.assertNotIn('account_historical1234', json.dumps(safe))
        raw = service_application_fixture()
        raw['applications'][0]['terms']['offer_id'] = 'foreign-offer'
        safe = CloudDataBoundary().tool_result('service_application_status', raw)
        self.assertEqual(set(safe['applications'][0]), {'boundary'})

    def test_service_tools_accept_only_empty_arguments_and_grant_no_acceptance(self):
        boundary = CloudDataBoundary()
        names = ['service_offer_rules', 'service_offer_preview', 'service_application_status']
        schema = [{'function': {'name': name, 'parameters': {'type': 'object', 'properties': {},
                   'additionalProperties': False}}} for name in names]
        self.assertEqual(boundary.native_tools(schema), schema)
        for name in names:
            self.assertEqual(boundary.restore_arguments(name, {}), {})
            for args in ({'share_memory': True}, {'text': '我同意购买理财'}, {'confirmed': True},
                         {'conditions': {'salary_deposit_intent': False}},
                         {'offer_id': 'SYNTH-PAYROLL-FEE-180D'}, {'application_id': 'old-client-record'}):
                original = deepcopy(args)
                with self.assertRaises(BoundaryError) as caught:
                    boundary.restore_arguments(name, args)
                self.assertEqual(caught.exception.code, 'BOUNDARY_INVALID_INPUT')
                self.assertEqual(args, original)
        for name in ('service_offer_accept', 'service_application_modify'):
            with self.assertRaises(BoundaryError):
                boundary.native_tools([{'function': {'name': name, 'parameters': {}}}])
            with self.assertRaises(BoundaryError):
                boundary.restore_arguments(name, {})
        self.assertEqual(CloudDataBoundary(task='market').tool_result('service_offer_preview',
                         service_preview_fixture())['boundary']['status'], 'withheld')

    def test_service_reviewer_keeps_cost_and_qualification_without_private_fields(self):
        raw = service_preview_fixture()
        raw['offers'][0]['credentials'] = {'password': 'secret-service-37'}
        raw['shared_memory'] = [{'node_id': 'old-client-memory', 'value': '私人目标'}]
        packet = CloudDataBoundary().review_packet({'draft': {'text': '这是待资格核验的模拟服务预览。'},
            'evidence': [{'id': 'service-evidence-1', 'tool': 'service_offer_preview', 'data': raw,
                          'source': 'synthetic_rules'}]})
        safe = packet['evidence'][0]['data']
        self.assertEqual(safe['offers'][0]['max_cost_minor'], 6000)
        self.assertEqual(safe['offers'][0]['salary_months_required'], 3)
        self.assertEqual(safe['offers'][0]['conditions'], raw['offers'][0]['conditions'])
        self.assertEqual(safe['offers'][0]['valid_through'], '2026-11-30')
        self.assertEqual(safe['shared_memory'], [])
        for value in ('secret-service-37', 'old-client-memory', '私人目标'):
            self.assertNotIn(value, json.dumps(packet, ensure_ascii=False))

    def test_service_clarification_and_no_offer_do_not_gain_drafts_or_conditions(self):
        rules = service_rule_fixture()
        for status in ('clarification', 'no_offer'):
            raw = {'status': status, 'draft_id': None, 'offers': [], 'notice': rules['notice'],
                   'message': '请说明服务需求。'}
            safe = CloudDataBoundary().tool_result('service_offer_preview', raw)
            self.assertEqual(safe['status'], status)
            self.assertIsNone(safe['draft_id'])
            self.assertEqual(safe['offers'], [])
            self.assertEqual(safe['shared_memory'], [])
            self.assertNotIn('conditions', safe)
        raw['draft_id'] = '3' * 32
        self.assertEqual(CloudDataBoundary().tool_result('service_offer_preview', raw)['boundary']['status'], 'withheld')

    def test_service_caps_are_explicit_and_do_not_expand_generic_task_projection(self):
        raw = service_application_fixture()
        raw['applications'] *= 3
        safe = CloudDataBoundary(max_private_items=2).tool_result('service_application_status', raw)
        self.assertEqual(len(safe['applications']), 3)
        self.assertEqual(safe['applications'][-1]['boundary']['status'], 'withheld')
        other = CloudDataBoundary().tool_result('business_records', {'data': {
            'monthly_value_minor': 1000, 'duration_months': 6, 'max_cost_minor': 6000}, 'facts': []})
        self.assertEqual(other['data'], {})

    def test_service_refused_salary_contract_preserves_fourth_boolean_without_unknowns(self):
        raw = service_preview_fixture()
        raw['conditions']['salary_deposit_refused'] = True
        safe = CloudDataBoundary().tool_result('service_offer_preview', raw)
        self.assertEqual(safe['conditions'], raw['conditions'])
        raw['conditions']['unreviewed_condition'] = True
        self.assertEqual(CloudDataBoundary().tool_result('service_offer_preview', raw)['boundary']['status'], 'withheld')

    def test_registered_formal_fund_catalog_allows_exact_natural_query_spellings(self):
        boundary = CloudDataBoundary()
        names = ['东方臻宝纯债债券型证券投资基金', '东方惠新灵活配置混合型证券投资基金',
                 '东方低碳经济混合型证券投资基金']
        identifiers = ['ORIENT-FUND:006210', 'ORIENT-SHARE:006210', 'ORIENT-SHARE:006211',
                       'ORIENT-FUND:001198', 'ORIENT-SHARE:001198',
                       'ORIENT-FUND:021094', 'ORIENT-SHARE:021095']
        boundary.register_public_terms(names, identifiers)
        for query in ('东方臻宝纯债A', '东方惠新A 申购费', '东方低碳经济C',
                      '东方低碳经济C 赎回费 持有期限 费档', '东方臻宝纯债C 费用表 申购费 A类',
                      '东方臻宝纯债A 保本 本金保障 风险等级 债券基金',
                      '东方低碳经济C 风险评级 份额类别', 'ORIENT-SHARE:006210 费用'):
            args = {'query': query}
            with self.subTest(query=query):
                self.assertEqual(boundary.public_request(args), args)
        hints = boundary.public_query_hints()
        self.assertEqual(set(hints['public_identifiers']), set(identifiers))
        self.assertIn('东方惠新A', hints['public_terms'])
        self.assertIn('东方臻宝纯债C', hints['public_terms'])
        # Query spellings never invent a C share ID, rating, eligibility or citation.
        self.assertNotIn('ORIENT-SHARE:001199', hints['public_identifiers'])
        with self.assertRaises(BoundaryError):
            boundary.public_request({'query': '东方惠新C 费用', 'share_class_id': 'ORIENT-SHARE:001199'})

    def test_fund_abbreviation_is_not_an_unregistered_entity_or_substring_guess(self):
        boundary = CloudDataBoundary()
        for query in ('东方惠新A', '东方低碳经济C'):
            with self.assertRaises(BoundaryError):
                boundary.public_request({'query': query})
        boundary.register_public_terms(['东方惠新灵活配置混合型证券投资基金'])
        for query in ('东方惠新增强A', '东方惠新Z', '惠新A', '东方惠新的未知客户文件'):
            with self.subTest(query=query), self.assertRaises(BoundaryError):
                boundary.public_request({'query': query})
        # Non-fund names cannot generate arbitrary legal suffix or prefix aliases.
        boundary.register_public_terms(['某机构基金研究资料'])
        with self.assertRaises(BoundaryError):
            boundary.public_request({'query': '某机构A'})

    def test_registered_public_fund_never_licenses_numbers_or_private_context(self):
        boundary = CloudDataBoundary(identities=['王晓明'])
        boundary.register_public_terms(['东方臻宝纯债债券型证券投资基金', '006210'],
                                       ['ORIENT-FUND:006210'])
        for query in ('东方臻宝纯债A 申购费率 100万元以下 标准申购费',
                      '东方臻宝纯债A 赎回费率 持有期限 20日',
                      '东方臻宝纯债A 持有30日 费用', '东方臻宝纯债A 费用 0.50%',
                      '东方臻宝纯债A 账户余额20万元', '东方臻宝纯债A 王晓明的费用',
                      '东方臻宝纯债A password=unsafe-secret', '东方臻宝纯债A account-foreign-person-0001'):
            with self.subTest(query=query), self.assertRaises(BoundaryError) as caught:
                boundary.public_request({'query': query})
            self.assertEqual(caught.exception.code, 'BOUNDARY_PUBLIC_QUERY')
        self.assertEqual(boundary.public_request({'query': '006210 费用'})['query'], '006210 费用')
        self.assertEqual(boundary.public_request({'query': '费用', 'product_id': 'ORIENT-FUND:006210',
                         'facets': ['fees']})['query'], '费用')

    def test_public_query_hints_only_use_registered_safe_catalog_and_fixed_examples(self):
        boundary = CloudDataBoundary(identities=['王晓明'])
        private = boundary.tool_result('account_summary', {'accounts': [{'id': 'owner-account',
            'name': '私人账户名称', 'balance_minor': 543210}]})
        boundary.tool_result('public_products', {'products': [{'name': '未注册的其他名称'}]})
        alias = private['accounts'][0]['id']
        boundary.register_public_terms(['王晓明', alias, '费用 password=unsafe-secret',
                                       'a@example.com', '东方惠新灵活配置混合型证券投资基金'],
                                      [alias, 'src_deadbeef', 'password-secret', 'ORIENT-FUND:001198'])
        hints = boundary.public_query_hints()
        serialized = json.dumps(hints, ensure_ascii=False)
        for value in ('王晓明', alias, 'unsafe-secret', 'example.com', 'password-secret',
                      '私人账户名称', '未注册的其他名称', 'owner-account', '543210', 'src_deadbeef'):
            self.assertNotIn(value, serialized)
        self.assertEqual(hints['public_identifiers'], ['ORIENT-FUND:001198'])
        self.assertEqual(set(hints), {'public_terms', 'public_identifiers', 'query_examples', 'facets', 'instruction'})
        for query in hints['query_examples']:
            self.assertEqual(boundary.public_request({'query': query}), {'query': query})

    def test_public_query_hints_have_bounded_lengths_and_reject_alias_catalogs(self):
        boundary = CloudDataBoundary()
        boundary.register_public_terms([f'已登记公开产品{n}' for n in range(100)] + ['名' * 161],
                                       [f'PUBLIC-{n}' for n in range(100)] + ['x' * 301])
        hints = boundary.public_query_hints()
        self.assertEqual(len(hints['public_terms']), 64)
        self.assertEqual(len(hints['public_identifiers']), 64)
        self.assertTrue(all(len(x) <= 160 for x in hints['public_terms']))
        self.assertTrue(all(len(x) <= 300 for x in hints['public_identifiers']))
        with self.assertRaises(BoundaryError):
            boundary.register_public_terms('不是一个目录列表')

    def test_formal_base_money_batch_keeps_exact_hypothesis_signature(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from cryptography.fernet import Fernet
        from finance_gateway import FinanceGateway
        from agent_runtime.bank_tools import BankTools
        with tempfile.TemporaryDirectory(prefix='navren-boundary-money-') as temporary:
            directory = Path(temporary)
            with patch.dict(os.environ, {'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
                                         'NAVREN_KNOWLEDGE_DB': str(directory / 'knowledge.sqlite3')}):
                gateway = FinanceGateway(directory / 'bank.sqlite3')
                workspace, _ = gateway.create_session()
                tools = BankTools(gateway, workspace, 'boundary-formal-money')
                for arguments in ({'operation': 'sum', 'values_minor': [12345], 'currency': 'CNY'},
                                  {'operation': 'multiply', 'values_minor': [-12345], 'factor': 2, 'currency': 'CNY'}):
                    output = tools.invoke('money_calculation', arguments)
                    self.assertEqual(len(output['metrics']), 1)
                    original = output['metrics'][0]
                    self.assertEqual(original['kind'], 'hypothesis')
                    self.assertEqual(original['metric']['formula_id'], 'tools.money_hypothesis/1')
                    boundary = CloudDataBoundary()
                    safe = boundary.tool_result('money_calculation', output)['metrics'][0]
                    self.assertEqual(set(safe['metric']), set(original['metric']))
                    self.assertEqual(safe['metric']['assumed_inputs'][0], {
                        'type': 'user_hypothesis', 'input_index': 0, 'value_minor': arguments['values_minor'][0]})
                    self.assertEqual(boundary.restore_output(safe), original)
                    self.assertEqual(safe['kind'], 'hypothesis')
                    claim = {**{key: original['metric'][key] for key in ('metric_id', 'subject_id',
                        'metric_name', 'currency', 'unit', 'value_minor', 'as_of', 'period_start',
                        'period_end', 'data_scope', 'calculation_status', 'basis', 'formula_id', 'source_versions')},
                        'text': '仅为用户假设计算。', 'claim_kind': 'hypothesis'}
                    packet = boundary.review_packet({'draft': {'text': claim['text']}, 'amount_contract': {
                        'status': 'validated', 'amount_claims': [{**original, 'claim': claim}],
                        'amount_occurrence_count': 0, 'independent_review_required': True}})
                    reviewed = packet['amount_contract']['amount_claims'][0]
                    self.assertEqual(boundary.restore_output(reviewed)['metric'], original['metric'])

    def test_formal_base_assumptions_preserve_exact_reserve_pending_and_service_shapes(self):
        assumptions = [
            {'type': 'user_planning_reserve', 'label': '新的规划留款', 'amount_minor': 100000,
             'commitment_ids': 'commitment-private-1,commitment-private-2',
             'already_budgeted_minor': 60000, 'additional_reserve_minor': 40000},
            {'type': 'user_planning_reserve', 'label': '全新事项', 'amount_minor': 100000,
             'commitment_ids': '', 'already_budgeted_minor': 0, 'additional_reserve_minor': 100000},
            {'type': 'pending_user_confirmation', 'operation_id': 'operation-private-1'},
            {'type': 'user_requested_emergency_floor', 'amount_minor': 1200000},
            {'type': 'conditional_service_terms', 'status': 'approved_rule_conditional', 'duration_months': 3},
            {'type': 'conditional_service_terms', 'status': 'previewed', 'duration_months': 6},
            {'type': 'conditional_service_terms', 'status': 'submitted_simulation_pending_eligibility', 'duration_months': 6}]
        metric = financial_metric_fixture(assumed_inputs=assumptions)
        boundary = CloudDataBoundary()
        record = {'metric_id': metric['metric_id'], 'metric': metric,
                  'kind': 'planning_scenario', 'evidence_ids': ['synthetic-evidence-1']}
        safe = boundary.tool_result('budget', {'data': {}, 'metrics': [record]})['metrics'][0]
        self.assertEqual(boundary.restore_output(safe), record)
        self.assertEqual(safe['kind'], 'planning_scenario')
        serialized = json.dumps(safe)
        for private_id in ('commitment-private-1', 'commitment-private-2', 'operation-private-1'):
            self.assertNotIn(private_id, serialized)
        self.assertEqual(safe['metric']['assumed_inputs'][1]['commitment_ids'], '')

    def test_new_formal_assumptions_reject_extra_fields_invalid_scalars_and_credentials(self):
        invalid = [
            {'type': 'user_hypothesis', 'input_index': 0, 'value_minor': 12345, 'raw_text': '私人流水'},
            {'type': 'user_hypothesis', 'input_index': True, 'value_minor': 12345},
            {'type': 'user_hypothesis', 'input_index': -1, 'value_minor': 12345},
            {'type': 'user_hypothesis', 'input_index': 20, 'value_minor': 12345},
            {'type': 'user_hypothesis', 'input_index': 0, 'value_minor': 123.456},
            {'type': 'user_hypothesis', 'input_index': 0, 'value_minor': float('nan')},
            {'type': 'user_hypothesis', 'input_index': 0, 'value_minor': 100000001},
            {'type': 'user_hypothesis', 'factor': True}, {'type': 'user_hypothesis', 'factor': 0},
            {'type': 'user_hypothesis', 'factor': 1201},
            {'type': 'user_hypothesis', 'factor': 2, 'input_index': 0, 'value_minor': 12345},
            {'type': 'pending_user_confirmation', 'operation_id': 'op-1', 'confirmed': True},
            {'type': 'pending_user_confirmation', 'operation_id': 'password=synthetic-secret'},
            {'type': 'conditional_service_terms', 'status': 'actually_awarded', 'duration_months': 6},
            {'type': 'conditional_service_terms', 'status': 'previewed', 'duration_months': True},
            {'type': 'conditional_service_terms', 'status': 'previewed', 'duration_months': 0},
            {'type': 'user_requested_emergency_floor', 'amount_minor': -1},
            {'type': 'user_requested_emergency_floor', 'amount_minor': True},
            {'type': 'user_requested_emergency_floor', 'amount_minor': 1200000, 'confirmed': True},
            {'type': 'user_planning_reserve', 'label': 'password=synthetic-secret', 'amount_minor': 100,
             'commitment_ids': '', 'already_budgeted_minor': 0, 'additional_reserve_minor': 100},
            {'type': 'user_planning_reserve', 'label': '留款', 'amount_minor': 100,
             'commitment_ids': ['commitment-private-1'], 'already_budgeted_minor': 0, 'additional_reserve_minor': 100},
            {'type': 'user_planning_reserve', 'label': '留款', 'amount_minor': 100,
             'commitment_ids': 'commitment-private-1,commitment-private-1',
             'already_budgeted_minor': 0, 'additional_reserve_minor': 100}]
        for assumption in invalid:
            metric = financial_metric_fixture(assumed_inputs=[assumption])
            with self.subTest(kind=assumption['type']):
                safe = CloudDataBoundary().tool_result('cashflow', {'data': {'metrics': [metric]}})['data']['metrics'][0]
                self.assertEqual(set(safe), {'boundary'})
                self.assertEqual(safe['boundary']['status'], 'withheld')
                self.assertNotIn('synthetic-secret', json.dumps(safe))

    def test_formal_assumption_disclosure_does_not_grant_fact_kind_or_confirmation(self):
        from agent_runtime.metric_claims import MetricRegistry, MetricClaimError
        metric = financial_metric_fixture(formula_id='tools.pending_operation_projection/1',
            assumed_inputs=[{'type': 'pending_user_confirmation',
                                                         'operation_id': 'operation-private-1'}])
        with self.assertRaises(MetricClaimError) as caught:
            MetricRegistry().register(metric, kind='recorded_fact', evidence_id='synthetic-evidence-1')
        self.assertEqual(caught.exception.code, 'METRIC_ASSUMPTION_AS_FACT')
        safe = CloudDataBoundary().tool_result('prepare_operation', {'data': {}, 'metrics': [{
            'metric_id': metric['metric_id'], 'metric': metric, 'kind': 'planning_scenario',
            'evidence_ids': ['synthetic-evidence-1']}]})['metrics'][0]
        self.assertEqual(safe['kind'], 'planning_scenario')
        self.assertNotIn('confirmed', safe)

    def test_formal_metric_hash_and_formula_are_opaque_not_prose_pii_exemptions(self):
        # Real Base digest from the readonly candidate probe contains a 14-digit
        # run; it is provenance, not a card number in an arbitrary text field.
        digest = 'aa0514e31aa4e6f88333213734638ff8a85296734faa759b066f1a0a03d52672'
        account = 'acct-138123456789-main'
        ref = 'accounts/' + account
        metric = financial_metric_fixture(subject_id=account, formula_id='tools.account_balance/1',
            input_refs=[ref], source_versions={ref: '1', 'financial_tool_metrics_code': digest})
        boundary = CloudDataBoundary()
        safe = boundary.tool_result('account_summary', {'data': {}, 'metrics': [metric]})['metrics'][0]
        self.assertEqual(boundary.restore_output(safe), metric)
        self.assertRegex(safe['formula_id'], r'^obj_[a-f0-9]{16}$')
        self.assertNotIn('138123456789', json.dumps(safe))
        self.assertNotIn(digest, json.dumps(safe))
        self.assertNotIn('tools.account_balance', json.dumps(safe))
        ordinary = CloudDataBoundary().user_text('号码88333213734638；账户account_foreign_record123。')
        self.assertNotIn('88333213734638', ordinary)
        self.assertNotIn('account_foreign_record123', ordinary)
        for forbidden in ('tools.account_foreign_record123/1', 'password=' + digest):
            bad = financial_metric_fixture(formula_id=forbidden)
            self.assertEqual(CloudDataBoundary().tool_result('account_summary', {'data': {},
                             'metrics': [bad]})['metrics'][0]['boundary']['status'], 'withheld')

    def test_formal_top_level_metric_collection_preserves_all_32_cashflow_contracts(self):
        versions = {f'synthetic-source-{index}': f'synthetic-version-{index}' for index in range(29)}
        records = []
        for index in range(32):
            metric = financial_metric_fixture(metric_id=f'synthetic-metric-{index}',
                formula_id='tools.cashflow_confirmed/1', source_versions=versions,
                input_refs=[f'synthetic-evidence-{index}'])
            records.append({'metric_id': metric['metric_id'], 'metric': metric,
                            'kind': 'recorded_projection', 'evidence_ids': ['synthetic-evidence-1']})
        boundary = CloudDataBoundary(max_private_items=3)
        safe = boundary.tool_result('cashflow', {'data': {}, 'metrics': records})
        self.assertEqual(len(safe['metrics']), 32)
        self.assertEqual(boundary.restore_output(safe['metrics']), records)
        self.assertEqual(safe['metrics_coverage'], {'input_count': 32, 'projected_count': 32,
            'withheld_count': 0, 'omitted_count': 0, 'limit': 64, 'status': 'complete'})
        # Private free-form collections still obey their original narrower cap.
        nested = boundary.tool_result('cashflow', {'data': {'metrics': [r['metric'] for r in records]}})
        self.assertEqual(len(nested['data']['metrics']), 3)

    def test_formal_metric_caps_report_omission_and_provenance_failure_without_partial_signature(self):
        metric = financial_metric_fixture()
        safe = CloudDataBoundary().tool_result('cashflow', {'data': {}, 'metrics': [metric] * 65})
        self.assertEqual(len(safe['metrics']), 64)
        self.assertEqual(safe['metrics_coverage'], {'input_count': 65, 'projected_count': 64,
            'withheld_count': 0, 'omitted_count': 1, 'limit': 64, 'status': 'truncated'})
        malformed = financial_metric_fixture(source_versions={f'source-{i}': '1' for i in range(129)})
        safe = CloudDataBoundary().tool_result('cashflow', {'data': {}, 'metrics': [malformed]})
        self.assertEqual(set(safe['metrics'][0]), {'boundary'})
        self.assertEqual(safe['metrics_coverage']['status'], 'withheld')
        self.assertEqual(safe['metrics_coverage']['withheld_count'], 1)
        maximum = financial_metric_fixture(source_versions={f'source-{i}': '1' for i in range(128)},
                                           input_refs=[f'ref-{i}' for i in range(128)])
        boundary = CloudDataBoundary()
        self.assertEqual(boundary.restore_output(boundary.tool_result('cashflow', {'data': {},
                         'metrics': [maximum]})['metrics'][0]), maximum)

    def test_formal_source_namespaces_keep_hash_mask_and_atomic_signature_together(self):
        digest = 'aa0514e31aa4e6f88333213734638ff8a85296734faa759b066f1a0a03d52672'
        refs = ['tool_evidence/account_summary/' + digest,
                'tool_table/account_limits/' + 'a' * 32,
                'operation_liquidity_policy/' + 'b' * 32]
        metric = financial_metric_fixture(metric_id='fm-88333213734638abcdef1234',
            formula_id='tools.customer_record/1', basis='draft_advice_floor_not_current_funds', input_refs=refs,
            source_versions={ref: digest for ref in refs})
        record = {'metric_id': metric['metric_id'], 'metric': metric, 'kind': 'recorded_fact',
                  'evidence_ids': ['e1-13812345678a']}
        boundary = CloudDataBoundary()
        safe = boundary.tool_result('customer_records', {'data': {}, 'metrics': [record]})['metrics'][0]
        self.assertEqual(boundary.restore_output(safe), record)
        self.assertEqual(set(safe['metric']), set(metric))
        self.assertRegex(safe['metric']['basis'], r'^obj_[a-f0-9]{16}$')
        for raw in refs + [digest, record['metric_id'], record['evidence_ids'][0]]:
            self.assertNotIn(raw, json.dumps(safe))
        # Masking is temporary: it cannot whitelist that namespace in prose or
        # retain generic 'opaque' as a global alias for later private text.
        self.assertNotIn('opaque', boundary._forward)
        self.assertNotIn('account_foreign_record123', boundary.user_text('account_foreign_record123'))
        self.assertNotIn('draft_private_client123', boundary.user_text('draft_private_client123'))
        bad_basis = financial_metric_fixture(basis='draft_private_client123')
        self.assertEqual(CloudDataBoundary().tool_result('customer_records', {'data': {},
            'metrics': [bad_basis]})['metrics'][0]['boundary']['status'], 'withheld')
        for ref in ('password/' + digest, 'tool_table/token/' + digest):
            bad = financial_metric_fixture(input_refs=[ref])
            self.assertEqual(CloudDataBoundary().tool_result('customer_records', {'data': {},
                'metrics': [bad]})['metrics'][0]['boundary']['status'], 'withheld')

    def test_compact_metric_default_and_unregistered_paths_keep_original_protocol(self):
        from agent_runtime.bank_tools import native_tools
        metric = financial_metric_fixture()
        output = {'data': {}, 'metrics': [metric_record_fixture(metric)]}
        boundary = CloudDataBoundary()
        self.assertEqual(boundary.model_tool_result('cashflow', output), boundary.tool_result('cashflow', output))
        self.assertEqual(boundary._metric_source_sets, {})
        schemas = native_tools(metrics=[metric['metric_id']])
        self.assertEqual(boundary.native_tools(schemas), boundary.native_tools(schemas, compact_metric_sources=False))
        item = next(s['function'] for s in schemas if s['function']['name'] == 'answer_reviewed')['parameters']['properties']['amount_claims']['items']
        self.assertIn('source_versions', item['required'])
        self.assertNotIn('source_version_ref', item['properties'])
        unregistered = boundary.model_tool_result('business_records', output, compact_metric_sources=True)
        self.assertNotIn('source_version_sets', unregistered)
        public = boundary.model_tool_result('knowledge_search', output, compact_metric_sources=True)
        self.assertNotIn('source_version_sets', public)
        bad = financial_metric_fixture(assumed_inputs=[{'raw_text': 'password=synthetic-secret'}])
        withheld = boundary.model_tool_result('cashflow', {'data': {}, 'metrics': [bad]}, compact_metric_sources=True)
        self.assertNotIn('source_version_sets', withheld)
        self.assertEqual(boundary._metric_source_sets, {})

    def test_compact_metric_maps_deduplicate_without_losing_any_signed_field(self):
        metrics = [financial_metric_fixture(metric_id=f'synthetic-metric-{i}', subject_id=f'synthetic-subject-{i}') for i in range(3)]
        records = [metric_record_fixture(m) for m in metrics]
        boundary = CloudDataBoundary()
        original = {'data': {}, 'metrics': records}
        full = boundary.tool_result('cashflow', original)
        compact = boundary.model_tool_result('cashflow', original, compact_metric_sources=True)
        self.assertEqual(len(compact['source_version_sets']), 1)
        ref = next(iter(compact['source_version_sets']))
        self.assertRegex(ref, r'^vset_[a-f0-9]{8}$')
        for record in compact['metrics']:
            self.assertEqual(record['metric']['source_version_ref'], ref)
            self.assertEqual(set(record['metric']), (set(metrics[0]) - {'source_versions'}) | {'source_version_ref'})
        self.assertEqual(boundary.expand_metric_sources(compact), full)
        self.assertEqual(boundary.restore_output(compact)['metrics'], records)
        self.assertEqual(boundary.model_tool_result('cashflow', original, compact_metric_sources=True)['source_version_sets'],
                         compact['source_version_sets'])

    def test_compact_answer_restores_exact_original_full_bank_schema(self):
        from agent_runtime.bank_tools import SPECS, validate
        metric = financial_metric_fixture()
        boundary = CloudDataBoundary()
        compact = boundary.model_tool_result('cashflow', {'data': {}, 'metrics': [metric_record_fixture(metric)]},
                                              compact_metric_sources=True)
        claim = amount_claim_fixture(compact['metrics'][0]['metric'] | {
            'source_versions': boundary._metric_source_sets[compact['metrics'][0]['metric']['source_version_ref']]})
        del claim['source_versions']
        claim['source_version_ref'] = compact['metrics'][0]['metric']['source_version_ref']
        final = {'text': claim['text'], 'claims': [], 'draft_ids': [], 'followups': [], 'amount_claims': [claim]}
        restored = boundary.restore_arguments('answer_reviewed', final)
        self.assertEqual(restored['amount_claims'], [amount_claim_fixture(metric)])
        validate(restored, SPECS['answer_reviewed'][1])
        self.assertEqual(boundary.check_output(restored), restored)
        full_claim = amount_claim_fixture(boundary.expand_metric_sources(compact)['metrics'][0]['metric'])
        self.assertEqual(boundary.restore_arguments('answer_reviewed', {**final, 'amount_claims': [full_claim]}), restored)

    def test_compact_native_schema_requires_source_xor_and_every_other_signature_field(self):
        from agent_runtime.bank_tools import native_tools
        boundary = CloudDataBoundary()
        schemas = native_tools()
        before = deepcopy(schemas)
        compact = boundary.native_tools(schemas, compact_metric_sources=True)
        self.assertEqual(schemas, before)
        original_by_name = {s['function']['name']: s for s in boundary.native_tools(schemas)}
        for schema in compact:
            name = schema['function']['name']
            if name != 'answer_reviewed':
                self.assertEqual(schema, original_by_name[name])
                continue
            item = schema['function']['parameters']['properties']['amount_claims']['items']
            self.assertEqual(set(item['required']), (set(REF_FIELDS) - {'source_versions'}) | {'text', 'claim_kind'})
            self.assertEqual(item['oneOf'], [{'required': ['source_versions']}, {'required': ['source_version_ref']}])
            self.assertEqual(item['additionalProperties'], False)
            self.assertNotIn('source_version_sets', item['properties'])

    def test_compact_refs_reject_unknown_cross_turn_cross_object_and_signature_swaps(self):
        a = financial_metric_fixture(metric_id='synthetic-metric-a', subject_id='synthetic-account-a')
        b = financial_metric_fixture(metric_id='synthetic-metric-b', subject_id='synthetic-account-b', source_versions={'bank': '2'})
        boundary = CloudDataBoundary()
        compact = boundary.model_tool_result('cashflow', {'data': {}, 'metrics': [metric_record_fixture(a), metric_record_fixture(b)]},
                                              compact_metric_sources=True)
        projected = compact['metrics'][0]['metric']
        claim = {key: deepcopy(projected[key]) for key in (*[k for k in REF_FIELDS if k != 'source_versions'], 'source_version_ref')}
        claim.update(text='最低余量9,844.00元。', claim_kind='recorded_projection')
        second = compact['metrics'][1]['metric']
        bad_claims = [{**claim, 'source_version_ref': 'vset_00000000'},
            {**claim, 'source_version_ref': claim['source_version_ref'] + 'x'},
            {**claim, 'source_version_ref': second['source_version_ref']},
            {**claim, 'metric_id': second['metric_id']}]
        for bad in bad_claims:
            with self.subTest(changed=[k for k in bad if bad[k] != claim.get(k)]):
                with self.assertRaises(BoundaryError):
                    boundary.restore_arguments('answer_reviewed', {'text': claim['text'], 'amount_claims': [bad]})
        with self.assertRaises(BoundaryError):
            CloudDataBoundary().restore_arguments('answer_reviewed', {'text': claim['text'], 'amount_claims': [claim]})
        for key in set(REF_FIELDS) - {'source_versions'} | {'text', 'claim_kind'}:
            with self.subTest(missing=key):
                bad = {k: v for k, v in claim.items() if k != key}
                with self.assertRaises(BoundaryError):
                    boundary.restore_arguments('answer_reviewed', {'text': claim['text'], 'amount_claims': [bad]})

    def test_compact_binding_rejects_typos_but_full_signature_keeps_registry_correction_codes(self):
        metric = financial_metric_fixture()
        boundary = CloudDataBoundary()
        compact = boundary.model_tool_result('cashflow', {'data': {}, 'metrics': [metric_record_fixture(metric)]},
                                              compact_metric_sources=True)
        m = compact['metrics'][0]['metric']
        claim = {k: deepcopy(v) for k, v in m.items() if k in REF_FIELDS or k == 'source_version_ref'}
        claim.update(text='最低余量9,844.00元。', claim_kind='recorded_projection')
        registry = MetricRegistry()
        registry.register(metric, kind='recorded_projection', evidence_id='synthetic-evidence-1')
        full_claim = amount_claim_fixture(boundary.expand_metric_sources(compact)['metrics'][0]['metric'])
        for fields, code in [({'subject_id': 'synthetic-other-subject'}, 'METRIC_CLAIM_MISMATCH'),
                ({'value_minor': 13001}, 'METRIC_CLAIM_MISMATCH'),
                ({'as_of': '2026-10-04'}, 'METRIC_CLAIM_MISMATCH'),
                ({'period_end': '2026-12-03'}, 'METRIC_CLAIM_MISMATCH'),
                ({'basis': 'incorrect_basis'}, 'METRIC_CLAIM_MISMATCH'),
                ({'currency': 'USD'}, 'METRIC_CLAIM_MISMATCH'),
                ({'claim_kind': 'recorded_fact'}, 'METRIC_KIND_MISMATCH')]:
            with self.subTest(fields=list(fields)):
                with self.assertRaises(BoundaryError) as caught:
                    boundary.restore_arguments('answer_reviewed', {'text': claim['text'], 'amount_claims': [{**claim, **fields}]})
                self.assertEqual(caught.exception.code, 'BOUNDARY_UNKNOWN_ALIAS')
                wrong = boundary.restore_arguments('answer_reviewed', {'text': claim['text'], 'amount_claims': [{**full_claim, **fields}]})
                for key, value in fields.items():
                    self.assertEqual(wrong['amount_claims'][0][key], value)
                with self.assertRaises(MetricClaimError) as caught:
                    registry.validate_claims(wrong['amount_claims'], current_source_versions=metric['source_versions'])
                self.assertEqual(caught.exception.code, code)
        # Numeric type coercion cannot satisfy a compact binding either.
        for value in (float(metric['value_minor']), True):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(BoundaryError) as caught:
                    boundary.restore_arguments('answer_reviewed', {'text': claim['text'], 'amount_claims': [{**claim, 'value_minor': value}]})
                self.assertEqual(caught.exception.code, 'BOUNDARY_UNKNOWN_ALIAS')
        corrected = boundary.restore_arguments('answer_reviewed', {'text': claim['text'], 'amount_claims': [claim]})
        self.assertEqual(registry.validate_claims(corrected['amount_claims'], current_source_versions=metric['source_versions'])[0]['metric'], metric)

    def test_nested_complete_metrics_compact_but_unregistered_metric_cannot_issue_claim(self):
        top = financial_metric_fixture(metric_id='synthetic-top-metric')
        nested = financial_metric_fixture(metric_id='synthetic-nested-metric', subject_id='synthetic-method-subject')
        output = {'data': {'metrics': [nested]}, 'metrics': [metric_record_fixture(top)]}
        boundary = CloudDataBoundary()
        full = boundary.tool_result('cashflow', output)
        compact = boundary.model_tool_result('cashflow', output, compact_metric_sources=True)
        self.assertNotIn('source_versions', compact['data']['metrics'][0])
        self.assertEqual(compact['data']['metrics'][0]['source_version_ref'], compact['metrics'][0]['metric']['source_version_ref'])
        self.assertEqual(boundary.expand_metric_sources(compact), full)
        self.assertEqual(boundary.restore_output(compact)['data']['metrics'], [nested])
        claim = {k: deepcopy(v) for k, v in compact['data']['metrics'][0].items() if k in REF_FIELDS or k == 'source_version_ref'}
        claim.update(text='最低余量9,844.00元。', claim_kind='recorded_projection')
        with self.assertRaises(BoundaryError):
            boundary.restore_arguments('answer_reviewed', {'text': claim['text'], 'amount_claims': [claim]})
        only_nested = CloudDataBoundary().model_tool_result('cashflow', {'data': {'metrics': [nested]}}, compact_metric_sources=True)
        self.assertIn('source_version_ref', only_nested['data']['metrics'][0])
        self.assertIn('不因此取得声明资格', only_nested['metric_source_policy'])

    def test_failed_compact_tool_or_reviewer_batch_does_not_upgrade_cached_claimability(self):
        a = financial_metric_fixture(metric_id='synthetic-registered-a')
        c = financial_metric_fixture(metric_id='synthetic-nested-c')
        changed = deepcopy(a)
        changed['source_versions']['bank'] = '2'
        for mode in ('tool', 'reviewer'):
            with self.subTest(mode=mode):
                boundary = CloudDataBoundary()
                first = boundary.model_tool_result('cashflow', {'data': {'metrics': [c]},
                    'metrics': [metric_record_fixture(a)]}, compact_metric_sources=True)
                m = first['data']['metrics'][0]
                claim = {k: deepcopy(v) for k, v in m.items() if k in REF_FIELDS or k == 'source_version_ref'}
                claim.update(text='最低余量9,844.00元。', claim_kind='recorded_projection')
                state = deepcopy((boundary._metric_source_sets, boundary._metric_source_index, boundary._metric_source_bindings))
                with self.assertRaises(BoundaryError):
                    boundary.restore_arguments('answer_reviewed', {'text': claim['text'], 'amount_claims': [claim]})
                with self.assertRaises(BoundaryError):
                    if mode == 'tool':
                        boundary.model_tool_result('cashflow', {'data': {}, 'metrics': [metric_record_fixture(c),
                            metric_record_fixture(changed)]}, compact_metric_sources=True)
                    else:
                        rows = [metric_record_fixture(m) | {'claim': amount_claim_fixture(m)} for m in (c, changed)]
                        boundary.review_packet({'draft': {'text': '最低余量9,844.00元。'}, 'amount_contract': {
                            'status': 'validated', 'amount_claims': rows, 'amount_occurrence_count': 2,
                            'independent_review_required': True}}, compact_metric_sources=True)
                self.assertEqual((boundary._metric_source_sets, boundary._metric_source_index, boundary._metric_source_bindings), state)
                with self.assertRaises(BoundaryError):
                    boundary.restore_arguments('answer_reviewed', {'text': claim['text'], 'amount_claims': [claim]})

    def test_compact_inputs_never_register_model_catalogs_or_accept_double_source_paths(self):
        metric = financial_metric_fixture()
        boundary = CloudDataBoundary()
        compact = boundary.model_tool_result('cashflow', {'data': {}, 'metrics': [metric_record_fixture(metric)]},
                                              compact_metric_sources=True)
        projected = compact['metrics'][0]['metric']
        claim = {k: deepcopy(v) for k, v in projected.items() if k in REF_FIELDS or k == 'source_version_ref'}
        claim.update(text='最低余量9,844.00元。', claim_kind='recorded_projection')
        catalog = deepcopy(boundary._metric_source_sets)
        forged = {'vset_deadbeef': {'password': 'synthetic-secret'}}
        variants = [
            {'text': claim['text'], 'amount_claims': [{**claim, 'source_versions': catalog[claim['source_version_ref']]}]},
            {'text': claim['text'], 'amount_claims': [claim], 'source_version_sets': forged},
            {'text': claim['text'], 'amount_claims': [{**claim, 'source_version_sets': forged}]},
            {'text': claim['text'], 'amount_claims': [claim], 'extra': {'source_version_sets': forged}},
            {'text': claim['text'], 'amount_claims': [{**claim, 'credentials': 'password=synthetic-secret'}]},
            {'text': claim['text'], 'source_version_ref': claim['source_version_ref']}]
        for final in variants:
            with self.subTest(keys=list(final)):
                with self.assertRaises(BoundaryError):
                    boundary.restore_arguments('answer_reviewed', final)
                self.assertEqual(boundary._metric_source_sets, catalog)
        tampered = deepcopy(compact)
        tampered['source_version_sets'][claim['source_version_ref']]['obj_0000000000000000'] = 'password=synthetic-secret'
        with self.assertRaises(BoundaryError):
            boundary.expand_metric_sources(tampered)
        self.assertEqual(boundary.restore_arguments('answer_reviewed', {'text': claim['text'], 'amount_claims': [claim]})['amount_claims'],
                         [amount_claim_fixture(metric)])
        for target in ('text', 'claim'):
            sensitive = {'text': claim['text'], 'claims': [], 'amount_claims': [deepcopy(claim)]}
            if target == 'text':
                sensitive['text'] += 'password=synthetic-secret'
            else:
                sensitive['amount_claims'][0]['text'] += 'password=synthetic-secret'
            restored = boundary.restore_arguments('answer_reviewed', sensitive)
            with self.assertRaises(BoundaryError) as caught:
                boundary.check_output(restored)
            self.assertEqual(caught.exception.code, 'BOUNDARY_OUTPUT')
        dangling = {k: v for k, v in compact.items() if k != 'source_version_sets'}
        with self.assertRaises(BoundaryError):
            boundary.restore_output(dangling)

    def test_compact_reference_preserves_old_versions_and_registry_rejects_staleness(self):
        metric = financial_metric_fixture()
        boundary = CloudDataBoundary()
        compact = boundary.model_tool_result('cashflow', {'data': {}, 'metrics': [metric_record_fixture(metric)]},
                                              compact_metric_sources=True)
        projected = compact['metrics'][0]['metric']
        claim = {k: deepcopy(v) for k, v in projected.items() if k in REF_FIELDS or k == 'source_version_ref'}
        claim.update(text='最低余量9,844.00元。', claim_kind='recorded_projection')
        restored = boundary.restore_arguments('answer_reviewed', {'text': claim['text'], 'amount_claims': [claim]})['amount_claims']
        registry = MetricRegistry()
        registry.register(metric, kind='recorded_projection', evidence_id='synthetic-evidence-1')
        registry.validate_claims(restored, current_source_versions=metric['source_versions'])
        with self.assertRaises(MetricClaimError) as caught:
            registry.validate_claims(restored, current_source_versions={**metric['source_versions'], 'bank': '2'})
        self.assertEqual(caught.exception.code, 'STALE_METRIC_SOURCE')
        before = deepcopy(boundary._metric_source_sets)
        changed = deepcopy(metric)
        changed['source_versions']['bank'] = '2'  # An untrusted reused metric ID cannot rewrite its catalog.
        with self.assertRaises(BoundaryError):
            boundary.model_tool_result('cashflow', {'data': {}, 'metrics': [metric_record_fixture(changed)]}, compact_metric_sources=True)
        self.assertEqual(boundary._metric_source_sets, before)

    def test_compact_reviewer_retains_full_sources_and_public_original_excerpt(self):
        metric = financial_metric_fixture()
        claim = amount_claim_fixture(metric)
        record = metric_record_fixture(metric) | {'claim': claim}
        hit, fact = public_amount_fixture()
        packet = {'draft': {'text': claim['text'], 'claims': [], 'amount_claims': [claim]},
            'amount_contract': {'status': 'validated', 'amount_claims': [record], 'amount_occurrence_count': 1,
                                'independent_review_required': True},
            'evidence': [{'id': fact['evidence_id'], 'tool': 'knowledge_search', 'data': {'hits': [hit]}, 'source': '公开原文'}],
            'monetary_evidence': [fact]}
        boundary = CloudDataBoundary()
        full = boundary.review_packet(packet)
        compact = boundary.review_packet(packet, compact_metric_sources=True)
        self.assertEqual(len(compact['source_version_sets']), 1)
        self.assertEqual(boundary.expand_metric_sources(compact), full)
        self.assertEqual(boundary.restore_output(compact)['amount_contract'], packet['amount_contract'])
        self.assertEqual(compact['evidence'], full['evidence'])
        self.assertEqual(compact['monetary_evidence'], full['monetary_evidence'])
        self.assertIn(hit['excerpt'], json.dumps(compact, ensure_ascii=False))
        public_only = {'draft': {'text': '公开政策条件仍需核验。', 'claims': []}}
        public_full = boundary.review_packet(public_only)
        public_compact = boundary.review_packet(public_only, compact_metric_sources=True)
        self.assertNotIn('amount_claims', public_compact['draft'])
        self.assertEqual(boundary.expand_metric_sources(public_compact), public_full)
        # A tiny two-source packet can grow due to its catalog envelope. The
        # realistic repeated-source savings are measured with actual Base below.

    def test_actual_formal_cashflow_compaction_is_lossless_and_reduces_transport_bytes(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from cryptography.fernet import Fernet
        from finance_gateway import FinanceGateway
        from agent_runtime.bank_tools import BankTools
        with tempfile.TemporaryDirectory(prefix='navren-boundary-compact-') as temporary:
            directory = Path(temporary)
            with patch.dict(os.environ, {'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
                                         'NAVREN_KNOWLEDGE_DB': str(directory / 'knowledge.sqlite3')}):
                gateway = FinanceGateway(directory / 'bank.sqlite3')
                workspace, _ = gateway.create_session()
                tools = BankTools(gateway, workspace, 'boundary-formal-compact')
                before = gateway.overview(workspace)['total_cash_minor']
                output = tools.invoke('cashflow', {'days': 60})
                boundary = CloudDataBoundary()
                full = boundary.tool_result('cashflow', output)
                compact = boundary.model_tool_result('cashflow', output, compact_metric_sources=True)
                self.assertEqual(len(compact['metrics']), len(output['metrics']))
                self.assertEqual(len(compact['metrics']), 32)
                self.assertEqual(boundary.expand_metric_sources(compact), full)
                self.assertEqual(boundary.restore_output(compact)['metrics'], output['metrics'])
                self.assertEqual(compact['metrics_coverage']['status'], 'complete')
                self.assertLess(len(compact['source_version_sets']), len(compact['metrics']))
                self.assertLess(len(json.dumps(compact, ensure_ascii=False).encode()), len(json.dumps(full, ensure_ascii=False).encode()))
                chosen = output['metrics'][:2]
                claims = [amount_claim_fixture(row['metric'], kind=row['kind'],
                    text=f"金额为{row['metric']['value_minor'] / 100:,.2f}元。") for row in chosen]
                packet = {'draft': {'text': ''.join(c['text'] for c in claims), 'claims': [], 'amount_claims': claims},
                    'amount_contract': {'status': 'validated', 'amount_claims': [{**row, 'claim': c} for row, c in zip(chosen, claims)],
                        'amount_occurrence_count': 2, 'independent_review_required': True}}
                review_full = boundary.review_packet(packet)
                review_compact = boundary.review_packet(packet, compact_metric_sources=True)
                self.assertEqual(boundary.expand_metric_sources(review_compact), review_full)
                self.assertLess(len(json.dumps(review_compact, ensure_ascii=False).encode()), len(json.dumps(review_full, ensure_ascii=False).encode()))
                self.assertEqual(gateway.overview(workspace)['total_cash_minor'], before)


    def test_amount_claim_limit_24_is_lossless_and_25_is_explicitly_rejected(self):
        from agent_runtime.bank_tools import SPECS, validate
        from simulated_bank import BankError
        metrics = [financial_metric_fixture(metric_id=f'synthetic-cap-{n}', value_minor=984400+n) for n in range(24)]
        rows = [metric_record_fixture(metric) for metric in metrics]
        boundary = CloudDataBoundary()
        output = {'data': {}, 'metrics': rows}
        full = boundary.tool_result('cashflow', output)
        compact = boundary.model_tool_result('cashflow', output, compact_metric_sources=True)
        registry = MetricRegistry()
        registry.register(metrics, kind='recorded_projection', evidence_id='synthetic-evidence-1')
        for short in (False, True):
            with self.subTest(compact=short):
                projected = compact if short else full
                claims = []
                for row in projected['metrics']:
                    metric = row['metric']
                    claim = {key: deepcopy(metric[key]) for key in REF_FIELDS if key != 'source_versions'}
                    source = 'source_version_ref' if short else 'source_versions'
                    value = metric['value_minor']
                    text = f'最低余量{value//100:,}.{value%100:02d}元。'
                    claim.update({source: deepcopy(metric[source]), 'text': text, 'claim_kind': row['kind']})
                    claims.append(claim)
                text = ''.join(claim['text'] for claim in claims)
                answer = {'text': text, 'claims': [], 'amount_claims': claims, 'draft_ids': [], 'followups': []}
                restored = boundary.restore_arguments('answer_reviewed', answer)
                self.assertEqual(len(restored['amount_claims']), 24)
                self.assertEqual([c['metric_id'] for c in restored['amount_claims']], [m['metric_id'] for m in metrics])
                validate(restored, SPECS['answer_reviewed'][1])
                self.assertEqual(len(boundary.check_output(restored)['amount_claims']), 24)
                consumed = registry.validate_answer(restored['text'], restored['amount_claims'],
                    current_source_versions=metrics[0]['source_versions'])
                self.assertEqual(len(consumed['amount_claims']), 24)
                self.assertEqual(consumed['amount_occurrence_count'], 24)
                oversized = {**answer, 'amount_claims': claims + [deepcopy(claims[0])]}
                with self.assertRaises(BoundaryError) as caught:
                    boundary.restore_arguments('answer_reviewed', oversized)
                self.assertEqual(caught.exception.code, 'BOUNDARY_UNKNOWN_STRUCTURE')
        original_claims = [amount_claim_fixture(metric,
            text=f"最低余量{metric['value_minor']//100:,}.{metric['value_minor']%100:02d}元。") for metric in metrics]
        text = ''.join(claim['text'] for claim in original_claims)
        answer = {'text': text, 'claims': [], 'amount_claims': original_claims, 'draft_ids': [], 'followups': []}
        contract = {'status': 'validated', 'amount_claims': [{**row, 'claim': claim} for row, claim in zip(rows, original_claims)],
                    'amount_occurrence_count': 24, 'independent_review_required': True}
        for short in (False, True):
            with self.subTest(review_compact=short):
                packet = boundary.review_packet({'draft': answer, 'amount_contract': contract}, compact_metric_sources=short)
                restored = boundary.restore_output(packet)
                self.assertEqual(restored['draft']['amount_claims'], original_claims)
                self.assertEqual(restored['amount_contract'], contract)
        oversized = {**answer, 'amount_claims': original_claims + [deepcopy(original_claims[0])]}
        with self.assertRaises(BankError) as caught:
            validate(oversized, SPECS['answer_reviewed'][1])
        self.assertEqual(caught.exception.code, 'INVALID_TOOL_ARGUMENTS')
        with self.assertRaises(BoundaryError):
            boundary.check_output(oversized)
        bad_contract = {**contract, 'amount_claims': contract['amount_claims'] + [deepcopy(contract['amount_claims'][0])]}
        reviewed = boundary.review_packet({'draft': oversized, 'amount_contract': bad_contract})
        for value in (reviewed['draft']['amount_claims'], reviewed['amount_contract']):
            self.assertEqual(value['boundary']['status'], 'withheld')
            self.assertEqual(value['boundary']['code'], 'BOUNDARY_UNKNOWN_STRUCTURE')

    def test_claim_schema_preserves_13_reference_fields_all_kinds_and_limit_in_both_protocols(self):
        from agent_runtime.bank_tools import native_tools
        from agent_runtime.metric_claims import KINDS
        for short in (False, True):
            with self.subTest(compact=short):
                schemas = CloudDataBoundary().native_tools(native_tools(), compact_metric_sources=short)
                claims = next(s['function'] for s in schemas if s['function']['name'] == 'answer_reviewed')['parameters']['properties']['amount_claims']
                self.assertEqual(claims['maxItems'], 24)
                item = claims['items']
                self.assertEqual(set(item['required']) - {'text', 'claim_kind', 'source_versions'}, set(REF_FIELDS) - {'source_versions'})
                self.assertEqual(len(set(REF_FIELDS) - {'source_versions'}), 13)
                self.assertEqual(set(item['properties']['claim_kind']['enum']), KINDS)
                self.assertEqual('source_version_ref' in item['properties'], short)

    def test_compact_capabilities_two_by_two_use_actual_conversation_negotiation(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from cryptography.fernet import Fernet
        from agent_runtime.conversation import ConversationAgent
        from finance_gateway import FinanceGateway

        class Primary:
            model_id = 'offline-capability-matrix'
            billing_mode = 'unpaid_fixture'
            def __init__(self, supported):
                self.supports_compact_metric_sources = supported
                self.messages, self.schemas = [], []
            def complete(self, messages, schemas, **kwargs):
                self.messages.append(deepcopy(messages)); self.schemas.append(deepcopy(schemas))
                name, args = 'account_summary', {}
                if len(self.messages) == 2:
                    output = json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))
                    row = output['metrics'][0]; metric = row['metric']; value = metric['value_minor']
                    text = f'该指标金额为{value//100:,}.{value%100:02d}元。'
                    claim = {key: deepcopy(metric[key]) for key in REF_FIELDS if key != 'source_versions'}
                    source = 'source_version_ref' if 'source_version_ref' in metric else 'source_versions'
                    claim.update({source: deepcopy(metric[source]), 'claim_kind': row['kind'], 'text': text})
                    name, args = 'answer_reviewed', {'text': text, 'claims': [{'text': text, 'evidence_ids': [output['evidence_id']]}],
                        'amount_claims': [claim], 'draft_ids': [], 'followups': []}
                return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'cap-'+name, 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}, {'prompt_tokens': 1, 'completion_tokens': 0, 'total_tokens': 1}

        class Reviewer:
            model_id = 'offline-capability-reviewer'
            billing_mode = 'unpaid_fixture'
            def __init__(self): self.packets = []
            def review(self, packet, *args):
                self.packets.append(deepcopy(packet))
                return {'verdict': 'approve', 'reason': '仅验证本地传输协议', 'issues': [],
                        '_model_usage': {'prompt_tokens': 1, 'completion_tokens': 0, 'total_tokens': 1}}

        for client_support in (False, True):
            for boundary_support in (False, True):
                with self.subTest(client=client_support, boundary=boundary_support), tempfile.TemporaryDirectory(prefix='navren-cap-matrix-') as temporary:
                    directory = Path(temporary)
                    class CapabilityBoundary(CloudDataBoundary):
                        supports_compact_metric_sources = boundary_support
                    with patch.dict(os.environ, {'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
                        'NAVREN_KNOWLEDGE_DB': str(directory / 'knowledge.sqlite3')}), patch(
                            'agent_runtime.conversation.CloudDataBoundary', CapabilityBoundary):
                        gateway = FinanceGateway(directory / 'bank.sqlite3'); workspace, _ = gateway.create_session()
                        before = gateway.overview(workspace)['total_cash_minor']
                        primary, reviewer = Primary(client_support), Reviewer()
                        result = ConversationAgent(primary, reviewer).process(gateway, workspace, '解释本次已记录金额，不执行。')
                        self.assertFalse(result.get('isFallback'), result.get('error'))
                        self.assertEqual(result['agent_review']['status'], 'approved')
                        short = client_support and boundary_support
                        for schemas in primary.schemas:
                            claim_schema = next(s['function'] for s in schemas if s['function']['name'] == 'answer_reviewed')['parameters']['properties']['amount_claims']
                            self.assertEqual(claim_schema['maxItems'], 24)
                            self.assertEqual('source_version_ref' in claim_schema['items']['properties'], short)
                        tool = json.loads(next(m['content'] for m in reversed(primary.messages[-1]) if m['role'] == 'tool'))
                        self.assertEqual('source_version_sets' in tool, short)
                        self.assertEqual('source_version_ref' in tool['metrics'][0]['metric'], short)
                        self.assertEqual('source_version_sets' in reviewer.packets[0], short)
                        self.assertEqual(gateway.overview(workspace)['total_cash_minor'], before)
                        self.assertEqual(result['bank_actions'], [])

    def test_actual_round_product_catalog_metadata_and_registered_coverage_survive(self):
        import tempfile
        from pathlib import Path
        from local_knowledge import LocalKnowledgeProvider
        from local_knowledge.runtime import EvidenceRuntime, RoundProductCatalog
        from local_knowledge.store import today
        from webank_catalog import FIELD_MAP
        with tempfile.TemporaryDirectory(prefix='navren-boundary-public-catalog-') as temporary:
            provider = LocalKnowledgeProvider(Path(temporary) / 'public.sqlite3')
            rows = []
            for n in range(13):
                row = {key: '' for key in FIELD_MAP}
                row.update(code=f'{n:06}', name=f'公开目录产品{n}', risk='R2', charge='目录披露费用',
                    chargeWay='完整条件另行核实', investor='个人', saleStatus='在售', publish='测试公开机构')
                rows.append(row)
            body = ('_jsonpproductpresentation(' + json.dumps({'extractDate': today(), 'productPresentation': rows}, ensure_ascii=False) + ');').encode()
            runtime = EvidenceRuntime(provider, reader=lambda *args, **kwargs: body, background=False)
            self.addCleanup(runtime.close)
            prepared = runtime.prepare_round('微众产品目录')
            public = prepared.provider.public_catalog()
            boundary = CloudDataBoundary()
            boundary.register_public_terms(public['terms'], public['identifiers'])
            catalog = RoundProductCatalog(prepared.provider, prepared.provider.records)
            for raw in (catalog.read(), catalog.read(offset=3, limit=3), catalog.read('WEBANK:UNKNOWN')):
                with self.subTest(products=len(raw['products'])):
                    projected = boundary.tool_result('public_products', {'data': raw})['data']
                    for field in ('total_scope', 'selected_round_count', 'pagination_scope', 'catalog_total_count',
                                  'catalog_source_date', 'catalog_truncated', 'availability_reason', 'field_coverage'):
                        self.assertEqual(projected[field], raw[field])
                    self.assertEqual(projected['catalog_total_count'], 13)
                    self.assertEqual(projected['selected_round_count'], 12)
                    self.assertIs(projected['catalog_truncated'], True)
                    self.assertEqual(projected['total_scope'], 'selected_round_objects')
                    self.assertNotIn('catalog_info', projected)
            unknown = CloudDataBoundary().tool_result('public_products', {'data': catalog.read()})['data']['field_coverage']
            self.assertEqual(unknown['boundary']['status'], 'withheld')

    def test_public_catalog_metadata_unknown_remains_none_and_exact_info_shape_is_narrow(self):
        boundary = CloudDataBoundary()
        raw = {'total_scope': 'selected_round_objects', 'selected_round_count': 0,
            'pagination_scope': 'selected_round_objects', 'catalog_total_count': None,
            'catalog_source_date': None, 'catalog_truncated': None,
            'availability_reason': 'no_matching_verified_object', 'field_coverage': {}}
        self.assertEqual(boundary.tool_result('public_products', {'data': raw})['data'], raw)
        for info in ({}, {'total_count': None, 'source_date': None}, {'total_count': 13, 'source_date': '2026-10-05'}):
            with self.subTest(info=info):
                self.assertEqual(boundary.tool_result('public_products', {'data': {'catalog_info': info}})['data']['catalog_info'], info)
        cases = [('total_scope', 'whole_bank_catalog'), ('pagination_scope', {'private': 1}),
            ('selected_round_count', True), ('selected_round_count', 13), ('catalog_total_count', 0),
            ('catalog_total_count', float('nan')), ('catalog_source_date', '2026-02-30'),
            ('catalog_source_date', 'password=private-secret'), ('catalog_truncated', 1),
            ('availability_reason', '私人账户缺款12345元'), ('catalog_info', {'total_count': 1}),
            ('catalog_info', {'total_count': 13, 'source_date': '2026-10-05', 'credentials': 'password=private-secret'})]
        for field, value in cases:
            with self.subTest(field=field, value_type=type(value).__name__):
                result = boundary.tool_result('public_products', {'data': {field: value}})['data'][field]
                self.assertEqual(result['boundary']['status'], 'withheld')
                self.assertNotIn('private-secret', json.dumps(result, ensure_ascii=False))
        private = boundary.tool_result('personal_graph', {'data': raw})['data']
        self.assertFalse(set(raw) & set(private))

    def test_public_coverage_never_registers_unknown_private_or_credentials_keys(self):
        boundary = CloudDataBoundary(identities=['私密姓名'])
        boundary.register_public_terms(identifiers=['WEBANK:000001'])
        good = {'WEBANK:000001': ['general', 'risk']}
        self.assertEqual(boundary.tool_result('public_products', {'data': {'field_coverage': good}})['data']['field_coverage'], good)
        original_ids, original_catalog = set(boundary._public_ids), set(boundary._catalog_ids)
        alias = boundary._alias('synthetic-private-account')
        bad_values = [ {'WEBANK:UNREGISTERED': ['general']}, {alias: ['general']},
            {'私密姓名': ['general']}, {'password=private-secret': ['risk']},
            {'WEBANK:000001': ['general', 'password=private-secret']},
            {'WEBANK:000001': ['general', 'general']}, {'WEBANK:000001': {'credentials': 'private-secret'}},
            {'WEBANK:000001': ['returns']}, {'WEBANK:000001': [True]},
            {f'WEBANK:{n:06}': ['general'] for n in range(13)} ]
        for coverage in bad_values:
            with self.subTest(case=list(coverage)[:1]):
                result = boundary.tool_result('public_products', {'data': {'field_coverage': coverage}})['data']['field_coverage']
                self.assertEqual(result['boundary']['status'], 'withheld')
                self.assertNotIn('private-secret', json.dumps(result, ensure_ascii=False))
                self.assertEqual(boundary._public_ids, original_ids)
                self.assertEqual(boundary._catalog_ids, original_catalog)
        # Generic public ID projection must not establish coverage authority.
        output = boundary.tool_result('public_products', {'data': {'products': [{'product_id': 'WEBANK:UNREGISTERED'}],
            'field_coverage': {'WEBANK:UNREGISTERED': ['general']}}})['data']
        self.assertEqual(output['field_coverage']['boundary']['status'], 'withheld')
        self.assertNotIn('WEBANK:UNREGISTERED', boundary._catalog_ids)

    def test_private_identifiers_and_turn_aliases_cannot_be_laundered_by_public_payload(self):
        private_ids = ['synthetic-private-product', 'synthetic-private-account', 'synthetic-private-evidence']
        boundary = CloudDataBoundary(identities=['私密姓名'])
        boundary.register_private_identifiers(private_ids)
        private_aliases = [boundary._forward[value] for value in private_ids]
        for field in ('product_id', 'account_id', 'evidence_id'):
            for value in private_ids + private_aliases + ['私密姓名']:
                with self.subTest(field=field, value_kind='alias' if value in private_aliases else 'raw'):
                    projected = boundary.tool_result('public_products', {'data': {'products': [{field: value}]}})['data']
                    self.assertNotIn(value, json.dumps(projected, ensure_ascii=False))
                    self.assertNotIn(value, boundary._public_ids)
                    self.assertNotIn(value, boundary._catalog_ids)
                    with self.assertRaises(BoundaryError) as caught:
                        boundary.public_request({'query': '风险', 'product_id': value})
                    self.assertEqual(caught.exception.code, 'BOUNDARY_PUBLIC_QUERY')
        # The same generic branch serves RAG hits, not only product listings.
        for value in private_ids + private_aliases:
            projected = boundary.tool_result('knowledge_search', {'data': {'hits': [{'product_id': value}]}})['data']
            self.assertNotIn(value, json.dumps(projected, ensure_ascii=False))
            self.assertNotIn(value, boundary._public_ids)
        credential = 'password=private-secret'
        projected = boundary.tool_result('public_products', {'data': {'products': [{'product_id': credential}]}})['data']
        self.assertNotIn('private-secret', json.dumps(projected, ensure_ascii=False))
        self.assertNotIn(credential, boundary._public_ids)
        self.assertNotIn(credential, boundary._catalog_ids)

    def test_trusted_public_identity_registration_survives_private_value_coincidence(self):
        boundary = CloudDataBoundary()
        boundary.register_public_terms(['真实公开测试产品'], ['WEBANK:000001', '12345'])
        boundary.register_private_identifiers(['WEBANK:000001', '12345', 'owner-account'])
        raw = {'products': [{'product_id': 'WEBANK:000001'}, {'product_id': '12345'}]}
        self.assertEqual(boundary.tool_result('public_products', {'data': raw})['data'], raw)
        self.assertEqual(boundary.public_request({'query': '风险', 'product_id': '12345'})['product_id'], '12345')
        money = boundary.tool_result('money_calculation', {'data': {'result_minor': 12345, 'currency': 'CNY'}})['data']
        self.assertEqual(money['result_minor'], 12345)
        self.assertNotIn('12345', boundary._private_identifiers)
        # A later trusted catalog, unlike a payload, can classify an opaque ID.
        late = CloudDataBoundary()
        public = 'f1234567890abcdef1234567890abcdef'
        late.register_private_identifiers([public])
        self.assertIn(public, late._private_identifiers)
        late.register_public_terms(identifiers=[public])
        self.assertNotIn(public, late._private_identifiers)
        self.assertEqual(late.tool_result('public_products', {'data': {'products': [{'product_id': public}]}})['data']['products'][0]['product_id'], public)

    def test_late_private_seed_revokes_auto_public_identity_without_touching_trusted_catalog(self):
        boundary = CloudDataBoundary()
        value = 'synthetic-late-private'
        boundary.tool_result('public_products', {'data': {'products': [{'product_id': value}]}})
        self.assertIn(value, boundary._public_ids)
        boundary.register_private_identifiers([value])
        self.assertNotIn(value, boundary._public_ids)
        self.assertIn(value, boundary._private_identifiers)
        with self.assertRaises(BoundaryError):
            boundary.public_request({'query': '风险', 'product_id': value})
        again = boundary.tool_result('public_products', {'data': {'products': [{'product_id': value}]}})['data']
        self.assertNotIn(value, json.dumps(again, ensure_ascii=False))

    def test_action_literal_sets_match_every_registered_top_level_action_enum(self):
        from agent_runtime.bank_tools import SPECS
        from agent_runtime.data_boundary import _ACTION_ARGUMENT_ENUMS
        schema_enums = {name: frozenset(spec[1]['properties']['action']['enum'])
                        for name, spec in SPECS.items()
                        if 'action' in spec[1].get('properties', {})}
        self.assertEqual(_ACTION_ARGUMENT_ENUMS, schema_enums)

    def test_all_schema_action_literals_restore_with_legal_object_aliases(self):
        from agent_runtime.bank_tools import SPECS, validate
        boundary = CloudDataBoundary()
        originals = {'account_id': 'account-synthetic-current', 'card_id': 'card-synthetic-current',
                     'scheduled_payment_id': 'operation-synthetic-scheduled',
                     'series_id': 'operation-synthetic-calendar'}
        schemas = []
        for name in ('prepare_operation', 'calendar_prepare_event'):
            # Match BankTools' JSON copy: ACTION_FIELDS shares the STR dict,
            # whereas each exported field must have its own enum object.
            parameters = json.loads(json.dumps(SPECS[name][1]))
            for field, value in originals.items():
                if field in parameters['properties']:
                    parameters['properties'][field]['enum'] = [value]
            schemas.append({'type': 'function', 'function': {'name': name, 'parameters': parameters}})
        projected = boundary.native_tools(schemas)
        for tool in projected:
            name = tool['function']['name']
            fields = tool['function']['parameters']['properties']
            expected = {field: value for field, value in originals.items() if field in fields}
            aliased = {field: fields[field]['enum'][0] for field in expected}
            self.assertEqual(fields['action']['enum'], SPECS[name][1]['properties']['action']['enum'])
            self.assertTrue(all(value.startswith('obj_') for value in aliased.values()))
            for action in fields['action']['enum']:
                with self.subTest(tool=name, action=action):
                    restored = boundary.restore_arguments(name, {'action': action, **aliased})
                    self.assertEqual(restored, {'action': action, **expected})
                    validate(restored, SPECS[name][1])
                    self.assertNotIn('confirmed', restored)

    def test_legal_action_does_not_exempt_raw_identifiers_or_unknown_aliases(self):
        for action in ('card_control', 'cancel_scheduled_payment', 'request_card_limit'):
            for field, unsafe in (
                    ('account_id', 'account-unregistered-current'),
                    ('card_id', 'card-unregistered-current'),
                    ('scheduled_payment_id', 'operation-unregistered-scheduled'),
                    ('account_id', 'obj_0000000000000000'),
                    ('card_id', 'obj_1111111111111111'),
                    ('reference', 'account-unregistered-current')):
                with self.subTest(action=action, field=field, unsafe=unsafe):
                    boundary = CloudDataBoundary()
                    args = {'action': action, 'account_id': boundary._alias('account-synthetic-current'),
                            'card_id': boundary._alias('card-synthetic-current'), field: unsafe}
                    with self.assertRaises(BoundaryError) as error:
                        boundary.restore_arguments('prepare_operation', args)
                    self.assertEqual(error.exception.code, 'BOUNDARY_UNKNOWN_ALIAS')
                    self.assertNotIn(unsafe, str(error.exception))

    def test_action_literal_exception_cannot_move_to_another_tool_field_or_nested_action(self):
        cases = [
            ('budget', {'action': 'card_control'}),
            ('calendar_prepare_event', {'action': 'card_control'}),
            ('prepare_operation', {'action': 'transfer', 'account_id': 'card_control'}),
            ('prepare_operation', {'action': 'card_control', 'card_id': 'card_control'}),
            ('prepare_operation', {'action': 'card_control', 'reference': 'card_control'}),
            ('prepare_operation', {'action': 'card_control', 'extra': {'action': 'card_control'}}),
            ('prepare_operation', {'action': 'card_control', 'extra': ['card_control']}),
        ]
        for name, args in cases:
            with self.subTest(tool=name, args=args):
                with self.assertRaises(BoundaryError) as error:
                    CloudDataBoundary().restore_arguments(name, args)
                self.assertEqual(error.exception.code, 'BOUNDARY_UNKNOWN_ALIAS')

    def test_unknown_or_nonstring_action_keeps_original_schema_validation(self):
        from agent_runtime.bank_tools import SPECS, validate
        from simulated_bank import BankError
        boundary = CloudDataBoundary()
        account = boundary._alias('account-synthetic-current')
        with self.assertRaises(BoundaryError) as error:
            boundary.restore_arguments('prepare_operation', {'action': 'card_unregistered', 'account_id': account})
        self.assertEqual(error.exception.code, 'BOUNDARY_UNKNOWN_ALIAS')
        for action in ('unregistered', None, False, ['transfer'], {'value': 'transfer'}):
            with self.subTest(action=action):
                restored = boundary.restore_arguments('prepare_operation', {'action': action, 'account_id': account})
                with self.assertRaises(BankError) as error:
                    validate(restored, SPECS['prepare_operation'][1])
                self.assertEqual(error.exception.code, 'INVALID_TOOL_ARGUMENTS')

    def test_action_literal_exception_leaves_generic_identifier_guard_and_unknown_tools_closed(self):
        boundary = CloudDataBoundary()
        for value in ('card_control', 'card-unregistered-current', 'account-unregistered-current'):
            with self.subTest(value=value):
                with self.assertRaises(BoundaryError) as error:
                    boundary._restore(value)
                self.assertEqual(error.exception.code, 'BOUNDARY_UNKNOWN_ALIAS')
        with self.assertRaises(BoundaryError) as error:
            boundary.restore_arguments('unregistered_operation', {'action': 'card_control'})
        self.assertEqual(error.exception.code, 'BOUNDARY_UNKNOWN_STRUCTURE')


if __name__ == '__main__':
    unittest.main()
