"""Local, task-scoped projections for the cloud conversation and speech boundary.

One instance belongs to one local turn. It never sends requests, reads credentials,
grants permissions, logs input, or replaces the bank's evidence/authorization checks.
Keep original requests and evidence locally; only pass this module's projections
to a cloud client. Known identities and object identifiers use reversible random
turn aliases. Credentials/contact details are removed and are never reversible.
This is a conservative first boundary, not a universal PII detector.
"""
from __future__ import annotations

import copy
import csv
import json
import math
import re
import secrets
import unicodedata
from decimal import Decimal, InvalidOperation
from datetime import date
from collections import Counter
from collections.abc import Mapping
from urllib.parse import urlsplit


POLICY_VERSION = 'navren-cloud-boundary-v1'
_HIDDEN = '[本地已屏蔽]'
_ALIAS = re.compile(r'(?<![A-Za-z0-9_])(?:obj|person|label)_[a-f0-9]{16}(?:-main)?(?![A-Za-z0-9_])')
_RAW_BUSINESS_ID = re.compile(
    r'(?<![A-Za-z0-9_])(?:[a-f0-9]{32,64}(?:-[A-Za-z0-9_-]{1,64})?'
    r'|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}'
    r'|(?:account|acc|operation|op|beneficiary|ben|card|transaction|txn|workspace|ws|draft|widget)[_-][A-Za-z0-9_.:-]{4,128})'
    r'(?![A-Za-z0-9_])', re.I)
_DROP = object()


class BoundaryError(ValueError):
    """Contains a fixed code/message only, never rejected values or field names."""
    MESSAGES = {
        'BOUNDARY_INVALID_INPUT': '资料格式无法安全处理，请提供必要的业务问题。',
        'BOUNDARY_UNKNOWN_TASK': '此任务尚未配置本地出库规则。',
        'BOUNDARY_UNKNOWN_ALIAS': '对象引用不属于本轮，请重新查询。',
        'BOUNDARY_UNKNOWN_STRUCTURE': '资料结构尚未配置安全投影，原始内容未发送。',
        'BOUNDARY_RAW_RECORDS': '原始流水或成批正文不能直接发送，请先在本地汇总。',
        'BOUNDARY_PUBLIC_QUERY': '公开检索只能使用已确认的公开产品标识或金融概念。',
        'BOUNDARY_OUTPUT': '回答包含未核验对象或敏感内容，请重新生成。',
    }

    def __init__(self, code):
        self.code = code
        self.message = self.MESSAGES.get(code, self.MESSAGES['BOUNDARY_INVALID_INPUT'])
        super().__init__(self.message)

    def as_dict(self):
        return {'code': self.code, 'message': self.message, 'policy_version': POLICY_VERSION}


# Every tool is explicitly registered. A new tool/schema must be reviewed locally.
_DOMAINS = {
    'load_skill': 'general', 'knowledge_status': 'public', 'knowledge_search': 'public',
    'market_hotspots': 'public', 'market_status': 'public', 'market_event': 'public',
    'market_article': 'public', 'market_weekly': 'public', 'public_products': 'public',
    'account_summary': 'account', 'budget': 'cashflow', 'statement_source': 'statements',
    'monthly_statement': 'statements', 'annual_statement': 'statements', 'statement_details': 'statements',
    'anomalies': 'statements', 'periodic_charges': 'subscriptions', 'cashflow': 'cashflow',
    'money_calculation': 'general', 'products': 'wealth', 'wealth_product': 'wealth',
    'wealth_versions': 'wealth', 'research_plans': 'wealth', 'service_offer_rules': 'tasks',
    'service_offer_preview': 'tasks', 'service_application_status': 'tasks',
    'business_records': 'business', 'customer_records': 'business',
    'operation_status': 'operations', 'prepare_operation': 'operations',
    'withdraw_draft': 'operations', 'user_memory': 'profile', 'remember_profile': 'profile',
    'personal_graph': 'profile', 'graph_scenario': 'cashflow',
    'calendar_query': 'calendar', 'calendar_day': 'calendar', 'calendar_item': 'calendar',
    'calendar_budget_scenario': 'cashflow', 'calendar_prepare_event': 'calendar',
    'financial_list_connections': 'financial_mcp', 'financial_list_accounts': 'financial_mcp',
    'financial_list_cards': 'financial_mcp', 'financial_get_account_overview': 'financial_mcp',
    'financial_list_transactions': 'financial_mcp', 'financial_list_positions': 'financial_mcp',
    'financial_list_insurance': 'financial_mcp', 'financial_get_policy': 'financial_mcp',
    'financial_list_assets': 'financial_mcp', 'financial_list_obligations': 'financial_mcp',
    'financial_get_financial_summary': 'financial_mcp',
}
# Fixed, schema-declared literals are not object identifiers. Exempt only the
# top-level action field of its own tool; other fields and arbitrary card_* text
# still use the strict identifier guard. Tests bind these sets to BankTools SPECS.
_ACTION_ARGUMENT_ENUMS = {
    'prepare_operation': frozenset('''purchase redeem transfer schedule_transfer
        cancel_scheduled_payment close_mandate cancel_subscription card_control
        report_card_lost request_card_limit apply_card request_split
        collect_split_payment reserve_funds release_funds book_services'''.split()),
    'calendar_prepare_event': frozenset({'create', 'edit', 'delete'}),
}
_SERVICE_TOOLS = frozenset({'service_offer_rules', 'service_offer_preview', 'service_application_status'})
_SERVICE_RULE_VERSION = 'navren.synthetic-service-benefits.v1'
_SERVICE_PENDING = 'submitted_simulation_pending_eligibility'
_SERVICE_CONDITIONS = frozenset({'salary_deposit_intent', 'investment_purchase_refused',
                                'service_benefit_requested'})
_SERVICE_CONDITION_FIELDS = _SERVICE_CONDITIONS | {'salary_deposit_refused'}
_SERVICE_TERM_FIELDS = frozenset('''offer_id title benefit_type monthly_value_minor duration_months
    max_cost_minor currency conditions requires_salary_deposit minimum_monthly_salary_minor
    salary_months_required requires_investment_purchase valid_through'''.split())
_SERVICE_OFFER_FIELDS = _SERVICE_TERM_FIELDS | {'eligibility', 'valid_from', 'application_status_on_accept'}
_SERVICE_PREVIEW_FIELDS = frozenset({'eligibility_status', 'review_status', 'status_label',
                                    'investment_purchase_required'})
_TASK_DOMAINS = {
    'general': frozenset(_DOMAINS.values()),
    'account': {'general', 'account', 'financial_mcp'},
    'statements': {'general', 'statements', 'financial_mcp'},
    'cashflow': {'general', 'account', 'cashflow', 'calendar', 'financial_mcp'},
    'payments': {'general', 'account', 'cashflow', 'business', 'operations', 'financial_mcp'},
    'cards': {'general', 'business', 'operations', 'financial_mcp'},
    'subscriptions': {'general', 'statements', 'subscriptions', 'business', 'operations', 'financial_mcp'},
    'tasks': {'general', 'account', 'cashflow', 'business', 'tasks', 'operations', 'calendar', 'financial_mcp'},
    'wealth': {'general', 'account', 'cashflow', 'wealth', 'public', 'operations', 'financial_mcp'},
    'calendar': {'general', 'calendar', 'cashflow'},
    'profile': {'general', 'profile', 'cashflow', 'wealth', 'calendar', 'financial_mcp'},
    'recommendation': {'general', 'account', 'cashflow', 'wealth', 'public', 'profile', 'operations', 'financial_mcp'},
    'comparison': {'general', 'wealth', 'public', 'cashflow', 'account', 'profile', 'operations', 'financial_mcp'},
    'market': {'general', 'public'}, 'public_research': {'general', 'public'},
}

_SENSITIVE_KEYS = frozenset('''workspace_id workspace_binding customer_id customer_name
 real_name full_name legal_name first_name last_name identity identity_number id_number
 national_id passport dob birth_date birthday_date address home_address residential_address
 street postcode postal_code mobile phone telephone email contact contacts masked_mobile
 masked_account card_number account_number iban pan cvv cvc pin pin_hash password passcode
 verification_code verification_token verification_hash confirmation_digest idempotency_key
 token access_token refresh_token token_hash agent_token_hash csrf secret api_key private_key
 authorization cookie credentials raw raw_text original_text user_text current_request
 raw_rows rows transactions transaction_rows archived_transactions statement_text
 memo description details raw_record record original_request reasoning_content'''.split())
_NAMES = frozenset('name display_name alias label merchant counterparty institution_name account_name product_name title plan_name relation_label'.split())
_SAFE_LABELS = frozenset('''日常账户 备用账户 工资账户 储蓄账户 模拟日常账户 模拟备用账户
 现金合计 妈妈 爸爸 母亲 父亲 家人 本人 应急金 工资 房租 餐饮 住房 购物 订阅 健康
 模拟工资 模拟房租 模拟餐饮 模拟视频会员 模拟云盘 模拟健身会员 模拟百货
 视频会员 云盘 健身会员'''.split())
_IDS = frozenset('''id evidence_id fact_id account_id product_id share_class_id channel_id
 fund_id portfolio operation_id draft_id widget_id view_id item_id series_id node_id edge_id
 insight_id beneficiary_id card_id mandate_id subscription_id scheduled_payment_id split_id
 participant_id task_id quote_id hold_id commitment_id goal_id loan_id policy_id article_id
 event_id citation_id source_id evidence_ids fact_ids draft_ids widget_ids quote_ids
 commitment_ids linked_commitment_ids linked_memory_ids transaction_ids reservation_ids
 parent_id source_refs source_ref evidence_ref evidence_refs source_revision context_hash
 expected_version version dataset_id release_id public_release_id provider_id source_document_id
 document_id document_version_id parse_id span_id parser_version original_version'''.split())
_IDS = _IDS | frozenset('metric_id subject_id input_refs offer_id application_id'.split())
_BASE = frozenset('''id status state type kind action source source_scope source_table
 source_type currency unit as_of clock_mode version schema_version notice limitations
 evidence_id evidence_ids available ready enabled verified user_confirmed origin
 count total transaction_count covered_months complete_calendar_year live_count archived_count
 from to to_exclusive on date occurred_at due_on matures_on locked_until settlement_date
 next_due_on end_on expires_on start_on period period_start period_end_exclusive year month
 range source_date stale label name title merchant display_name alias account_name product_name
 error code message executed domain record_count coverage_gaps unavailable_entities gaps
 coverage scope accounting_basis portfolio expected_version source_revision historical_snapshot
 missing_fields warnings reasons exclusion_reasons source_notice environment attachment_status
 policy_version requires_user_confirmation effect tool_trace tool parameters data facts
 value published_at published_precision queried_at read_scope link_status source_url url
 source_id evidence_ref evidence_refs source_ref source_refs'''.split()) | _IDS
_NUMBERS = frozenset('''amount_minor balance_minor opening_balance_minor principal_minor
 outstanding_minor limit_minor used_minor total_minor share_minor paid_minor target_minor
 saved_minor budget_minor reserve_minor cash_minor cash_delta_minor result_minor delta_minor
 floor_minor liquidity_floor_minor upcoming_due_minor held_minor investable_minor
 holding_principal_minor pending_redemption_minor net_assets_principal_minor liabilities_minor
 additional_reserve_minor current_available_minor minimum_available_after_confirmed_minor
 projected_available_after_confirmed_minor minimum_available_if_candidates_occur_minor
 remaining_minor requested_limit_minor credit_limit_minor original_principal_minor
 confirmed_inflow_minor confirmed_outflow_minor candidate_outflow_minor committed_minor
 available_minor reserved_minor minimum_available_minor cash_after_minor reserved_delta_minor
 unit_nav shares nav_minor fees_minor return_minor pnl_minor distribution_minor total_equity_minor
 risk_level horizon_days term_days redemption_delay_days min_purchase_minor max_purchase_minor
 max_amount_minor remaining_limit_minor single_limit_minor daily_limit_minor risk_score
 confidence probability factor values_minor observed_count baseline_count baseline_median_minor
 average_minor change_minor historical_principal_minor start_value_minor end_value_minor
 equity_minor market_value_minor purchase_fee_minor redemption_fee_minor cost_minor'''.split())
_NUMBERS = _NUMBERS | frozenset('''recorded_inflow_minor recorded_outflow_minor recorded_net_minor
 income_minor expense_minor refund_minor transfer_in_minor transfer_out_minor
 investment_principal_in_minor investment_principal_out_minor confirmed_due_minor candidate_due_minor
 user_budget_minor investment_pnl_minor settled_dividend_minor known_investment_pnl_minor
 holdings_pnl_minor daily_pnl_minor unrealized_pnl_minor closing_equity_minor
 baseline_investable_minor income_expense_surplus_minor already_budgeted_minor'''.split())
_FIELDS = {
    'general': {'content', 'name', 'references', 'reference', 'instructions', 'result_minor', 'operation', 'values_minor', 'factor'},
    'account': {'accounts', 'profile', 'cash_by_currency', 'holdings', 'principal_summary', 'total_cash_minor', 'financial_scope_basis'},
    'cashflow': {'accounts', 'timeline', 'events', 'commitments', 'reserves', 'goals', 'scenario',
                 'horizon_end', 'certainty', 'date_status', 'held_funds_by_account',
                 'confirmed_floor_breached', 'candidate_floor_breached', 'available_amount_basis',
                 'future_inflow_basis', 'deduplicated', 'linked_commitment_ids', 'comparison', 'baseline',
                 'budget_scope', 'planning_reserves', 'amount_basis'},
    'statements': {'totals_minor', 'category_expense_minor', 'category_income_minor', 'categories',
                   'active_source', 'imported_available', 'duplicates', 'high_amounts', 'rule',
                   'baseline_count', 'baseline_median_minor', 'completeness', 'periods', 'summary',
                   'items', 'category', 'offset', 'limit', 'next_offset', 'has_more'},
    'subscriptions': {'candidates', 'classification', 'expected_next_date', 'mandates', 'subscriptions',
                      'frequency', 'merchant_status', 'bank_status', 'minimum_months'},
    'wealth': {'products', 'product', 'profile_eligible', 'risk_notice', 'early_redemption',
              'holdings', 'lots', 'orders', 'principal_summary', 'versions', 'items', 'assumptions',
              'fee_rules', 'rate', 'basis', 'source_date', 'latest_nav', 'nav_on', 'fund_id',
              'detail_url', 'current_channel_status', 'product_code', 'share_class', 'fees',
              'positions', 'valuation', 'performance', 'calculations', 'scope_notice',
              'coverage', 'basis_version', 'simulation', 'is_simulation', 'execution_coverage',
              'selected_products', 'snapshot', 'requirements', 'decision_basis', 'comparison'},
    'business': {'records', 'beneficiaries', 'scheduled_payments', 'split_bills', 'split_participants',
                 'cards', 'card_applications', 'mandates', 'subscriptions', 'orchestration_tasks',
                 'fund_holds', 'service_quotes', 'service_reservations', 'holdings', 'lots', 'orders',
                 'loans', 'loan_installments', 'insurance_policies', 'insurance_claims', 'deposit_contracts',
                 'wealth_goals', 'budgets', 'advice_records', 'disputes', 'support_cases', 'permissions',
                 'permission_grants', 'consents', 'account_limits', 'account_restrictions',
                 'frequency', 'control_type', 'controls', 'network', 'requested_type',
                 'verified', 'bank_name', 'loan_type', 'deposit_type', 'coverage_type', 'steps'},
    'operations': {'operation', 'payload', 'result', 'account', 'product', 'mandate', 'card',
                   'beneficiary', 'budget', 'projected_after_confirmation', 'agent_review',
                   'withdrawn_payload', 'frequency', 'control_type', 'requested_type',
                   'verification_required', 'account_id', 'beneficiary_id', 'reference',
                   'schedule', 'split', 'participant', 'subscription', 'task', 'hold', 'quotes'},
    'calendar': {'items', 'days', 'preview', 'event', 'events', 'draft', 'snapshot', 'totals',
                 'recurrence', 'month_policy', 'remind_days', 'relation_label', 'original_on',
                 'series_id', 'date_status', 'result', 'has_more', 'offset', 'limit', 'next_offset',
                 'confirmed', 'historical_snapshot', 'kinds', 'sources', 'view', 'include_life_events',
                 'summary_by_currency', 'simulation_overlay_summary', 'coverage_basis', 'wealth_status',
                 'item_count', 'recorded_count', 'unknown_direction_count', 'pnl_available_count',
                 'pnl_missing_count', 'pnl_coverage', 'returns', 'nav_asof', 'timezone'},
    'financial_mcp': {'schema_version', 'source', 'dataset_version', 'as_of', 'data', 'evidence', 'notice',
        'items', 'total', 'has_more', 'limit', 'offset', 'coverage_partial', 'account', 'cards',
        'credit_facilities', 'recent_card_statements', 'contracts', 'policy', 'premium_funding', 'claims', 'rule',
        'connection_id', 'institution_name', 'institution_type', 'status', 'account_id', 'name', 'account_type',
        'currency', 'scope', 'opened_on', 'opening_balance_minor', 'balance_minor', 'card_id', 'card_type',
        'issued_on', 'closed_on', 'replaces_card_id', 'facility_id', 'role', 'limit_minor', 'outstanding_minor',
        'statement_id', 'period_start', 'period_end', 'due_on', 'statement_minor', 'paid_minor', 'contract_id',
        'contract_type', 'asset_id', 'start_date', 'end_date', 'principal_minor', 'position_id', 'cost_basis_minor',
        'book_value_minor', 'quantity', 'valuation_date', 'valuation_source', 'policy_id', 'insurer_connection_id',
        'covered_asset_id', 'coverage_key', 'insurance_type', 'terms_version', 'sum_insured_minor', 'premium_minor',
        'farmer_paid_minor', 'subsidy_minor', 'deductible_minor', 'cash_value_minor', 'payment_account_id', 'rule_id',
        'payer_type', 'amount_minor', 'personal_cash_effect', 'claim_id', 'occurred_on', 'verified_on', 'effective_from',
        'effective_to', 'region', 'provenance', 'transaction_id', 'occurred_at', 'booked_on', 'signed_minor',
        'counterparty', 'category', 'kind', 'economic_event_id', 'expense_minor', 'ownership_scope', 'asset_type',
        'obligation_id', 'cash_minor', 'investment_minor', 'real_assets_minor', 'loan_principal_minor',
        'credit_card_outstanding_minor', 'liabilities_minor', 'total_assets_minor', 'net_worth_minor',
        'insurance_cash_value_minor', 'insurance_sum_excluded', 'credit_limits_excluded', 'runtime_operations_included',
        'coverage_start', 'coverage_end', 'bank_integration', 'synthetic_contracts', 'valuation', 'dataset_state'},
    'profile': {'items', 'category', 'value', 'quote', 'nodes', 'edges', 'links', 'insights', 'calculations',
                'predicate', 'derivation_type', 'metric_family', 'metric_definition', 'score',
                'amount_minor', 'related_knowledge', 'consent', 'observations', 'question', 'view',
                'certainty', 'account_id', 'source_scope', 'source_table', 'source_id',
                'observation_id', 'memory_id', 'refreshed_at', 'relation_type', 'summary', 'text',
                'role', 'group', 'calculation_id', 'input_refs', 'goal_amount_basis', 'memory_enabled'},
    'tasks': {'offers', 'rules', 'eligibility', 'conditions', 'services', 'required_fields', 'quotes', 'fee_rules'},
    'public': {'events', 'facts', 'articles', 'supporting_events', 'report', 'reading', 'history',
               'changes', 'citations', 'observations', 'next_week_schedule', 'transmission',
               'observation_notice', 'schedule_notice', 'summary', 'conditions', 'heading', 'quote',
               'publisher', 'title_cn', 'expected_period', 'products', 'publish', 'sale_status',
               'risk', 'product_code', 'fund_id', 'verified_nav_products', 'detail_url', 'nav_on',
               'latest_nav', 'unit_nav', 'current_channel_status', 'hits', 'excerpt', 'locator',
               'page', 'start', 'end', 'valid_from', 'valid_to', 'source_status', 'outbound_policy',
               'scope', 'public_release_id', 'source_kind', 'source_format', 'coverage', 'readable',
               'legal_title', 'channel_status', 'source_version', 'document_hash', 'binding',
               'market_timezone', 'window_days', 'status_notice', 'evidence_excerpt', 'content',
               'facets', 'query', 'limit', 'source_registry', 'providers', 'last_collection',
               'sources', 'source_name', 'permission', 'failure_count', 'available_count'},
}
_FIELDS['public'].update({'text', 'issuer', 'issuer_name', 'institution', 'distributor',
               'source_title', 'document_title', 'public_terms'})
_FIELDS['public'].update({'paragraph', 'start_char', 'end_char', 'published_on', 'retrieved_on',
                          'effective_from', 'effective_to', 'data_on', 'visibility',
                          'outbound_allowed', 'withdrawn', 'content_status', 'source_version',
                          'effective_status', 'claims_are_account_facts'})
# These complete monetary contracts have a separate atomic projection. They are
# not added to the generic field allowlist: unknown objects cannot use contract
# fields to smuggle private data or make an incomplete metric look valid.
_METRIC_REF_FIELDS = frozenset('''metric_id subject_id metric_name currency unit value_minor
 as_of period_start period_end data_scope calculation_status basis formula_id source_versions'''.split())
_METRIC_FIELDS = _METRIC_REF_FIELDS | frozenset('''contract_version input_refs excluded_items
 coverage_gaps assumed_inputs'''.split())
_FORMAL_METRIC_TOOLS = frozenset('''account_summary budget cashflow monthly_statement annual_statement
 customer_records money_calculation prepare_operation service_offer_rules service_offer_preview
 service_application_status'''.split())
_FORMAL_METRIC_LIMIT = 64
_AMOUNT_CLAIM_LIMIT = 24
_METRIC_PROVENANCE_LIMIT = 128
_PUBLIC_CATALOG_METADATA = frozenset('''total_scope selected_round_count pagination_scope
 catalog_total_count catalog_source_date catalog_truncated availability_reason catalog_info
 field_coverage'''.split())
_PUBLIC_CATALOG_FACETS = frozenset({'general', 'fees', 'risk', 'eligibility', 'sale_status'})
_FORMAL_TOOL_FORMULAS = frozenset('tools.' + name + '/1' for name in '''account_balance
 account_cash_total account_principal budget_inputs budget_baseline budget_planning budget_current
 budget_reserve_dedup cashflow_point cashflow_candidates cashflow_confirmed cashflow_account_flow
 cashflow_flow_total statement_observed_totals statement_category customer_record money_hypothesis
 pending_operation_current pending_operation_amount pending_operation_projection service_terms'''.split())
# A fixed method label is not a draft identifier. This exact literal contains
# no user data; unknown draft_* values still take the ordinary identifier guard.
_FORMAL_METRIC_BASES = frozenset({'draft_advice_floor_not_current_funds'})
_METRIC_SOURCE_REF = re.compile(r'vset_[a-f0-9]{8}\Z')
_METRIC_SOURCE_POLICY = ('来源集合仅用于可逆传输；金额声明只能引用本轮正式登记的 metrics 记录'
                         '或本地 validated amount_contract。data 内其他指标不因此取得声明资格。')
# Typed opaque identifiers can contain digit runs that resemble financial numbers.
# Only these exact business/hash shapes are masked before credential scanning;
# ordinary prose, contact details and arbitrary assumption text keep the guard.
_METRIC_OPAQUE_REF = re.compile(r'(?:[a-f0-9]{32}|[a-f0-9]{64}'
    r'|fm-[a-f0-9]{24}|e[1-9][0-9]{0,2}-[a-f0-9]{12}'
    r'|acct-[a-f0-9]{12}-(?:main|savings)'
    r'|(?:[A-Za-z][A-Za-z0-9_.:-]*/)+acct-[a-f0-9]{12}-(?:main|savings)'
    r'|(?:[A-Za-z][A-Za-z0-9_.:-]*/)+(?:[a-f0-9]{32}|[a-f0-9]{64}))\Z')
_METRIC_KINDS = frozenset('''recorded_fact recorded_projection candidate_scenario
 planning_scenario forecast hypothesis'''.split())
_METRIC_STATUSES = frozenset('calculated partial unavailable infeasible'.split())
_CONTRACT_FIELDS = frozenset('''contract_error unsupported_amounts instruction
 valid_evidence_ids valid_draft_ids valid_widget_ids required_calendar_draft_ids
 review_revision observed_identifiers'''.split())
_AGGREGATE_MAPS = frozenset('totals_minor category_expense_minor category_income_minor held_funds_by_account'.split())
_AGGREGATE_LABELS = frozenset('''income expense refund transfer investment_purchase investment_redemption
 cash_delta income_expense_surplus 工资 房租 餐饮 住房 交通 通讯 教育 医疗 健康 购物
 娱乐 生活 订阅 其他 收入 支出 退款 CNY USD EUR GBP JPY'''.split())
_PUBLIC_TERMS = frozenset('''基金 理财 债券 存款 保险 费率 费用 申购 赎回 期限 锁定
 流动性 风险 收益 净值 产品 条款 规则 公开 官方 查询 对比 比较 区别
ETF 货币基金 债券基金 股票基金 混合基金 指数基金 七日年化 业绩比较基准
 风险等级 风险揭示 风险揭示书 风险提示 原文 原始文本 全文 内容 条件 限制
 销售 在售 状态 渠道 机构 发行机构 管理人 托管人 公告 合同 说明书
 投资范围 投资策略 投资目标 投资方向 投资期限 到账 确认 交易 开放 预约 暂停
 开放日 起购 资格 额度 上限 下限 损失 波动 回撤 本金 兑付 保本 非保本
 历史 业绩 时间 日期 发布 生效 到期 失效 过期 缺失 缺口 覆盖 时效
 证据 出处 来源 版本 文档 文件 资料 依据 本地 知识库'''.split())
# This is request grammar, not an identity recognizer or new public-entity registry.
# Removing it for validation never rewrites the outbound query or registers IDs.
_PUBLIC_QUERY_GRAMMAR = frozenset('''请 帮我 帮助我 为我 告诉我 请问 帮忙 查找 检索
 查阅 查看 核查 核对 核验 了解 说明 解释 介绍 判断 分析 仅 只 使用 根据 基于
 怎么 如何 是否 有 没有 哪些 哪个 什么 什么时候 多久 多少 何时 所需 需要 要求
 能否 可否 可以 能 不能 不要 不 是 否 和 与 的 及 对 关于 有关 中 内 分别
 复查 适合 投资者'''.split())
_PUBLIC_TERMS = _PUBLIC_TERMS | frozenset('政策 补助 金额 产品说明书'.split())
# Public document concepts only. No amount, percentage or holding-duration
# selector is permitted merely because a query also mentions a public fund.
_PUBLIC_TERMS = _PUBLIC_TERMS | frozenset('''申购费 赎回费 申购费用 赎回费用 申购费率 赎回费率 费用表
 标准申购费 费档 持有期限 持有期 锁定期 本金保障 风险评级 份额类别 A类 C类'''.split())
_PUBLIC_FACETS = frozenset({'fees', 'liquidity', 'risk', 'strategy', 'eligibility',
                           'sale_status', 'returns', 'general', 'terms'})
_ROW_DATE = re.compile(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?')
_ROW_NUMBER = re.compile(r'[-+]?[￥¥$€]?\d+(?:\.\d{1,4})?(?:元|CNY|USD|EUR|GBP|JPY)?', re.I)
_RECORD_KEYS = frozenset('''amount amount_minor balance balance_minor merchant transaction_id
 account_id occurred_at date currency income expense description memo'''.split())
_FACT_LABELS = frozenset('''已结算余额 账户现金 应急保留 假设计算结果 30天已知到期款
 生效资金预留 当前可安排资金 情景可安排资金 持仓本金 待结算赎回 收入 消费支出
 退款 账单收支结余 账单现金变动 流水条数 正式模拟风险等级 最长投资期限（天）
 期末可用（已确认事项） 未追加留款的当前可安排资金 尚未扣未来事项的底线外余量
 最低底线外余量（已确认事项） 最低底线外余量（若候选发生） 额外去重留款
 锁定天数 赎回到账天数 状态 商户合同状态'''.split())


class CloudDataBoundary:
    """Pure local projection. Local bank permissions remain mandatory after restore.

    ``restore_output`` is for local UI/evidence validation, not cloud/TTS reuse.
    ``tts_text`` deliberately keeps names anonymized even after display restoration.
    ``public_request`` is also required before a provider's outbound retrieval call.
    Identity seeds should come from current local records, never model guesses.
    """
    supports_compact_metric_sources = True
    def __init__(self, task='general', identities=(), *, max_private_items=12):
        self._forward = {}
        self._reverse = {}
        self._nonprose_identifiers = set()
        self._identities = set()
        self._private_identifiers = set()
        self._public_terms = set(_PUBLIC_TERMS)
        self._public_ids = set()
        self._catalog_terms = set()
        self._catalog_ids = set()
        self._sources = {}
        # Transport-only, one-turn catalogs. Original Base/MetricRegistry
        # contracts never contain these references.
        self._metric_source_sets = {}
        self._metric_source_index = {}
        self._metric_source_bindings = {}
        self._counts = Counter()
        self.max_private_items = min(24, max(1, int(max_private_items)))
        self.set_task(task)
        self.seed_identities(identities)

    def set_task(self, task):
        if task not in _TASK_DOMAINS:
            raise BoundaryError('BOUNDARY_UNKNOWN_TASK')
        self.task = task
        return self

    def seed_identities(self, identities):
        if isinstance(identities, Mapping):
            identities = identities.values()
        if isinstance(identities, str):
            identities = [identities]
        for value in identities or ():
            if isinstance(value, str) and value.strip() and value not in _SAFE_LABELS:
                self._identities.add(value)
                self._alias(value, 'person')
        return self

    def register_public_terms(self, terms=(), identifiers=()):
        """Only a locally verified public catalog/provider may call this.

        Fund abbreviations are deterministic *query spellings*, not product or
        share identities. A/C never creates an ID, asserts a share exists or
        changes the provider's subject/citation checks.
        """
        if isinstance(terms, str) or isinstance(identifiers, str):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        for value in identifiers:
            if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,299}', value)
                    or _ALIAS.search(value) or value.startswith('src_') or value in self._identities):
                self._counts['omitted'] += 1
                continue
            existed = value in self._public_ids
            self._public_ids.add(value)
            if self._text(value, maximum=300, raw_guard=False) != value:
                if not existed:
                    self._public_ids.discard(value)
                self._counts['omitted'] += 1
                continue
            self._catalog_ids.add(value)
            # Trusted local catalog registration can resolve a previously
            # opaque ID as public; a tool payload cannot make that decision.
            self._private_identifiers.discard(value)
        for value in terms:
            if (not isinstance(value, str) or not 1 <= len(value) <= 160 or value != value.strip()
                    or _ALIAS.search(value) or value.startswith('src_') or value in self._identities
                    or self._text(value, maximum=160, raw_guard=False) != value):
                self._counts['omitted'] += 1
                continue
            spellings = {value}
            fund = re.fullmatch(r'(.{4,80}?)(?:灵活配置)?(?:债券型|混合型|股票型|货币市场|指数型|货币型)证券投资基金', value)
            if fund and re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9]{4,80}', fund[1]):
                base = fund[1]
                spellings.update({base, base + 'A', base + 'C', base + 'A类', base + 'C类'})
            self._catalog_terms.update(spellings)
            self._public_terms.update(spellings)

    def public_query_hints(self):
        """Bounded verified catalog guidance; never echo a rejected query.

        Generic public tool projections cannot populate this catalog. Only
        explicit local registration above can do so; private aliases/history
        and user-provided labels never become public retrieval candidates.
        """
        def clean(values, maximum):
            result = []
            for value in sorted(values):
                if (_ALIAS.search(value) or value.startswith('src_') or value in self._identities
                        or self._text(value, maximum=maximum, raw_guard=False) != value):
                    continue
                result.append(value)
                if len(result) == 64:
                    break
            return result
        return {'public_terms': clean(self._catalog_terms, 160),
                'public_identifiers': clean(self._catalog_ids, 300),
                'query_examples': ['费用', '产品赎回条件', '风险提示原文'],
                'facets': sorted(_PUBLIC_FACETS),
                'instruction': '使用已登记公开名称或标识，以及费用、赎回、风险等公开概念；'
                               '不发送整句私人问题、个人金额、持有日数、费率数字或身份。'
                               '简称及A/C只是查询拼写，产品、份额和渠道必须分别由原文核验。'}

    def register_private_identifiers(self, identifiers):
        """Seed only locally authorized owner IDs before history/input projection.

        This creates no cloud payload or permission. Public catalog IDs must be
        registered separately and are never converted into private aliases here.
        The caller must exclude public products/articles and foreign-owner IDs.
        """
        if isinstance(identifiers, str):
            identifiers = [identifiers]
        if not isinstance(identifiers, (list, tuple, set, frozenset)):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        for value in identifiers:
            if not isinstance(value, str) or not 1 <= len(value) <= 300:
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            if value not in self._catalog_ids:
                self._private_identifiers.add(value)
                self._public_ids.discard(value)
                self._alias(value)
        return self

    def _alias(self, value, kind='obj', *, prose=True):
        if not isinstance(value, str) or not value:
            return value
        if value in self._reverse:
            return value
        if value not in self._forward:
            suffix = '-main' if kind == 'obj' and value.endswith('-main') else ''
            alias = kind + '_' + secrets.token_hex(8) + suffix
            self._forward[value] = alias
            self._reverse[alias] = value
            if not prose:
                self._nonprose_identifiers.add(value)
            self._counts['aliases'] += 1
        elif prose:
            self._nonprose_identifiers.discard(value)
        return self._forward[value]

    def _remove(self, match):
        self._counts['redactions'] += 1
        return _HIDDEN

    @staticmethod
    def _bulk_records(text):
        """Recognize repeated record rows, not ordinary dated/comma-rich prose.

        Inputs/history still reject bulk CSV, TSV, tables, whitespace ledgers and
        JSONL. A date somewhere in a sentence or thousands separators in an
        evidence-backed explanation do not make that sentence a raw record.
        This heuristic supplements field projection; it is not a PII guarantee.
        """
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) <= 8:
            return False

        def record(line):
            if line.startswith('{'):
                try:
                    value = json.loads(line)
                except (ValueError, RecursionError):
                    value = None
                if isinstance(value, dict) and len(set(value) & _RECORD_KEYS) >= 2:
                    return True
            for delimiter in ('\t', '|', ','):
                if line.count(delimiter) < 2:
                    continue
                try:
                    # A formatted number embedded in prose is one value, not
                    # multiple CSV columns. Dates/real row delimiters stay.
                    parsed_line = re.sub(r'(?<![\d/\-])[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?',
                                         lambda match: match.group(0).replace(',', ''), line) if delimiter == ',' else line
                    cells = next(csv.reader([parsed_line], delimiter=delimiter))
                except csv.Error:
                    continue
                cells = [cell.strip() for cell in cells if cell.strip()]
                if len(cells) >= 3:
                    numbers = [cell for cell in cells if _ROW_NUMBER.fullmatch(cell)]
                    dates = [cell for cell in cells if _ROW_DATE.fullmatch(cell)]
                    # Thousands separators can create a numeric CSV cell in a
                    # normal sentence. Require record-like compact fields, or a
                    # whole date field together with an actual numeric field.
                    if numbers and (dates or all(len(cell) <= 32 and not re.search(r'[。!?！？:：;；]', cell)
                                                 for cell in cells)):
                        return True
            cells = line.split()
            return (len(cells) >= 3 and bool(_ROW_DATE.fullmatch(cells[0]))
                    and any(_ROW_NUMBER.fullmatch(cell) for cell in cells[1:]))

        return sum(record(line) for line in lines) > 4

    def _text(self, text, *, maximum=6000, raw_guard=True):
        if not isinstance(text, str):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        if len(text) > maximum:
            self._counts['refusals'] += 1
            raise BoundaryError('BOUNDARY_RAW_RECORDS')
        normalized = unicodedata.normalize('NFKC', text)
        # Raw pasted records are handled locally; ordinary prose is not CSV.
        if raw_guard and self._bulk_records(normalized):
            self._counts['refusals'] += 1
            raise BoundaryError('BOUNDARY_RAW_RECORDS')
        value = normalized
        # Credentials/contact information are never reversible, including known seeds.
        for pattern in (
            r'(?i)(?:验证码|密码|口令|密钥|\bpassword\b|\bpasscode\b|\bpin\b|\botp\b|\bapi[ _-]?key\b|\btoken\b|\bsecret\b|\bauthorization\b)[\x22\x27]?\s*[:：=]?\s*[\x22\x27]?[\w./+\-=]{3,}',
            r'(?i)\b(?:sk|rk|ak)-[a-z0-9_-]{10,}\b',
            r'(?i)\bBearer\s+[a-z0-9._\-+/=]{8,}',
            r'(?i)[a-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+',
            r'(?<!\d)(?:\+?86[ -]?)?1[3-9](?:[ -]?\d){9}(?!\d)',
            r'(?<!\w)\+\d{1,3}[ -]?(?:\d[ -]?){7,12}(?!\d)',
            r'(?<!\d)(?:\+?1[ -]?)?(?:\(\d{3}\)|\d{3})[ -]?\d{3}[ -]\d{4}(?!\d)',
            r'(?<!\d)(?:\+?\d{1,3}[ -])?(?:\(\d{2,4}\)[ -]?|0\d{2,3}[ -])\d{5,8}(?!\d)',
            r'(?<!\d)\d(?:[ -]?\d){12,18}[xX]?(?!\d)',
            r'(?<!\d)\d{17}[0-9xX](?!\w)',
            r'(?:住址|地址|住在|居住于|address)\s*[:：]?\s*[^，,。;；\n]{3,100}',
            r'[\u4e00-\u9fff]{2,8}(?:省|自治区)[\u4e00-\u9fff0-9]{2,35}(?:市|区|县)[^，,。;；\n]{0,40}(?:路|街|巷|弄|村)[^，,。;；\n]{0,20}',
            r'[\u4e00-\u9fff]{2,8}市[\u4e00-\u9fff]{2,8}(?:区|县)[^，,。;；\n]{0,30}(?:路|街|巷|弄|村)[^，,。;；\n]{0,20}',
            r'(?i)\b\d{1,6}\s+[a-z0-9 .-]{2,50}\s+(?:street|st|road|rd|avenue|ave|lane|ln)\b[^,;\n]{0,30}',
        ):
            value = re.sub(pattern, self._remove, value)
        # Explicitly introduced names are local deterministic hints, not an NER guarantee.
        for pattern in (r'(?:我叫|姓名[：:]?|真实姓名[：:]?)\s*([\u4e00-\u9fff]{2,4})(?=[，,。\s]|$)',
                        r'(?:转给|给)\s*([\u4e00-\u9fff]{2,4})(?=转账|汇款|转\d|\s*\d)'):
            for match in re.finditer(pattern, value):
                self.seed_identities([match.group(1)])
        for original, alias in sorted(self._forward.items(), key=lambda p: len(p[0]), reverse=True):
            if original in self._public_ids or original in self._nonprose_identifiers:
                continue
            value = value.replace(unicodedata.normalize('NFKC', original), alias)
        # Unknown opaque business identifiers are not implicit authority to read
        # another person's record. A local re-query can establish an owner alias.
        value = _RAW_BUSINESS_ID.sub(
            lambda match: match.group(0) if match.group(0) in self._public_ids else self._remove(match), value)
        return value

    def user_text(self, text):
        projected = self._text(text)
        self.source_ref(text)
        return projected

    def source_ref(self, text):
        """Local opaque reference to original prose; never a cloud permission token."""
        if not isinstance(text, str):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        for ref, original in self._sources.items():
            if original == text:
                return ref
        ref = 'src_' + secrets.token_hex(8)
        self._sources[ref] = text
        return ref

    def local_source(self, ref):
        """Use locally for exact quote/candidate binding, never in cloud payloads."""
        if ref not in self._sources:
            raise BoundaryError('BOUNDARY_UNKNOWN_ALIAS')
        return self._sources[ref]

    def history(self, messages):
        if not isinstance(messages, list):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        output = []
        for message in messages[-16:]:
            if not isinstance(message, Mapping) or message.get('role') not in ('user', 'assistant', 'ai'):
                self._counts['omitted'] += 1
                continue
            try:
                content = self._text(message.get('content', ''))
            except BoundaryError:
                self._counts['omitted'] += 1
                continue
            output.append({'role': 'assistant' if message['role'] == 'ai' else message['role'], 'content': content})
        return output

    def _project(self, value, domain, *, key='', depth=0, public=False):
        if depth > 12:
            self._counts['omitted'] += 1
            return _DROP
        if key in _NUMBERS and key != 'values_minor' and value is not None and type(value) not in (int, float):
            self._counts['omitted'] += 1
            return _DROP
        if isinstance(value, Mapping):
            if key in _AGGREGATE_MAPS:
                result = {}
                for field, item in value.items():
                    if not isinstance(field, str) or type(item) is not int:
                        self._counts['omitted'] += 1
                        continue
                    label = field if field in _AGGREGATE_LABELS else self._alias(field, 'label')
                    result[label] = item
                return result
            result = {}
            allowed = _BASE | _NUMBERS | _FIELDS[domain] | _CONTRACT_FIELDS
            for field, item in value.items():
                if field == 'metrics' and not public:
                    result[field] = self._metric_collection(item)
                    continue
                if not isinstance(field, str) or field in _SENSITIVE_KEYS or field not in allowed:
                    self._counts['omitted'] += 1
                    continue
                if field in ('quote', 'value', 'text', 'content', 'reference'):
                    if field in ('quote', 'value') and domain not in ('profile', 'public') and isinstance(item, str):
                        self._counts['omitted'] += 1
                        continue
                projected = self._project(item, domain, key=field, depth=depth + 1, public=public)
                if projected is not _DROP:
                    result[field] = projected
            return result
        if isinstance(value, (list, tuple)):
            limit = 24 if public else self.max_private_items
            result = []
            for item in value[:limit]:
                projected = self._project(item, domain, key=key, depth=depth + 1, public=public)
                if projected is not _DROP:
                    result.append(projected)
            self._counts['omitted'] += max(0, len(value) - limit)
            return result
        if value is None or isinstance(value, bool):
            return value
        if type(value) in (int, float):
            if isinstance(value, float) and not math.isfinite(value):
                self._counts['omitted'] += 1
                return _DROP
            return value
        if isinstance(value, str):
            if key in _IDS or key.endswith('_id') or key.endswith('_ids'):
                if public:
                    if (value not in self._catalog_ids and (
                            value in self._private_identifiers or value in self._identities
                            or value in self._reverse or _ALIAS.search(value)
                            or (value in self._forward and value not in self._public_ids))):
                        self._counts['omitted'] += 1
                        return _DROP
                    existed = value in self._public_ids
                    self._public_ids.add(value)
                    clean = self._text(value, maximum=300, raw_guard=False)
                    if clean != value and not existed:
                        self._public_ids.discard(value)
                    return clean
                return self._alias(value)
            if key in ('url', 'source_url', 'detail_url'):
                parsed = urlsplit(value)
                if not public or parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    self._counts['omitted'] += 1
                    return _DROP
                return self._text(value, maximum=1500, raw_guard=False)
            if key in _NAMES:
                if public or (domain == 'wealth' and key in ('name', 'product_name')):
                    clean = self._text(value, maximum=160, raw_guard=False)
                    if clean != value:
                        return clean
                    if public:
                        self._public_terms.add(value)
                    return value
                if value in _SAFE_LABELS:
                    return value
                clean = self._text(value, maximum=160, raw_guard=False)
                if _HIDDEN in clean:
                    return clean
                return self._alias(value, 'person' if key in ('display_name', 'alias') else 'label')
            try:
                maximum = 240 if domain == 'profile' and key in ('value', 'quote') else 12000 if public or domain == 'general' else 2000
                return self._text(value, maximum=maximum, raw_guard=not public)
            except BoundaryError:
                self._counts['omitted'] += 1
                return _DROP
        self._counts['omitted'] += 1
        return _DROP

    def _gap(self, code='BOUNDARY_UNKNOWN_STRUCTURE'):
        self._counts['refusals'] += 1
        return {'boundary': {'status': 'withheld', 'code': code, 'policy_version': POLICY_VERSION,
                             'message': BoundaryError.MESSAGES.get(code, BoundaryError.MESSAGES['BOUNDARY_UNKNOWN_STRUCTURE'])}}

    def server_context(self, context):
        if not isinstance(context, Mapping):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        basic = {'interface_mode', 'environment', 'attachment_status', 'as_of', 'clock_mode'}
        contextual = {'recent_operations': 'operations', 'calendar_events': 'calendar',
                      'calendar_draft_receipts': 'calendar', 'recent_calendar_widgets': 'calendar',
                      'graph_context': 'profile', 'market_context': 'public', 'page_context': 'profile'}
        output = {}
        for key, value in context.items():
            if key in basic:
                output[key] = self._text(value, maximum=100, raw_guard=False) if isinstance(value, str) else self._project(value, 'general')
            elif key in contextual and contextual[key] in _TASK_DOMAINS[self.task]:
                output[key] = self._project(value, contextual[key], public=contextual[key] == 'public')
            elif key in _CONTRACT_FIELDS or key == 'error':
                output[key] = self._project(value, 'general', key=key)
            else:
                self._counts['omitted'] += 1
        return output

    def tool_result(self, name, output):
        domain = _DOMAINS.get(name)
        if not domain or domain not in _TASK_DOMAINS[self.task]:
            return self._gap()
        if not isinstance(output, (Mapping, list)):
            return self._gap()
        # Project data before facts so private labels are already registered aliases.
        if isinstance(output, Mapping) and 'data' in output:
            projected = (self._skill_data(output['data']) if name == 'load_skill' else
                         self._service_data(name, output['data']) if name in _SERVICE_TOOLS else
                         self._public_product_data(output['data']) if name == 'public_products' else
                         self._project(output['data'], domain, public=domain == 'public'))
            result = {'data': projected}
            if isinstance(output.get('evidence_id'), str):
                result['evidence_id'] = self._alias(output['evidence_id'])
            # Monetary/scalar facts survive, but only locally known fact envelope keys.
            facts = []
            scalars = self._scalars(projected)
            for fact in output.get('facts', [])[:48]:
                if not isinstance(fact, Mapping):
                    self._counts['omitted'] += 1
                    continue
                # Excluded raw rows/unknown fields must not reappear through flattened facts.
                fact_value = fact.get('value')
                if not isinstance(fact_value, (str, int, float, bool)):
                    self._counts['omitted'] += 1
                    continue
                try:
                    safe_value = self._text(fact_value, raw_guard=False) if isinstance(fact_value, str) else fact_value
                except BoundaryError:
                    self._counts['omitted'] += 1
                    continue
                public_source = self._public_fact_source(name, projected, fact) if domain == 'public' else None
                if safe_value not in scalars and public_source is None:
                    self._counts['omitted'] += 1
                    continue
                if fact.get('unit') == 'public_minor_currency' and public_source is None:
                    self._counts['omitted'] += 1
                    continue
                fields = {k: fact[k] for k in ('id', 'evidence_id', 'value', 'currency', 'unit', 'text', 'scope', 'article_id', 'citation_id') if k in fact}
                if public_source is not None:
                    fields['text'] = public_source
                    fields['scope'] = 'public_statement_not_account_balance'
                if domain != 'public' and 'text' in fields:
                    fields['text'] = self._fact_text(fields['text'])
                facts.append(self._project_fact(fields, public=domain == 'public'))
            result['facts'] = facts
            if 'metrics' in output and domain != 'public':
                formal = name in _FORMAL_METRIC_TOOLS and isinstance(output['metrics'], list)
                result['metrics'] = self._metric_collection(output['metrics'], formal=formal)
                if formal:
                    rows = result['metrics']
                    omitted = max(0, len(output['metrics']) - _FORMAL_METRIC_LIMIT)
                    withheld = sum(isinstance(row, Mapping) and 'boundary' in row for row in rows)
                    result['metrics_coverage'] = {
                        'input_count': len(output['metrics']), 'projected_count': len(rows) - withheld,
                        'withheld_count': withheld, 'omitted_count': omitted, 'limit': _FORMAL_METRIC_LIMIT,
                        'status': 'truncated' if omitted else 'withheld' if withheld else 'complete'}
            if projected in ({}, []) and output['data'] not in ({}, []):
                result['boundary'] = self._gap()['boundary']
            return result
        projected = (self._service_data(name, output) if name in _SERVICE_TOOLS else
                     self._public_product_data(output) if name == 'public_products' else
                     self._project(output, domain, public=domain == 'public'))
        return projected if projected not in ({}, []) else self._gap()

    def _public_product_data(self, data):
        """Narrow round-catalog metadata, not a generic dynamic public mapping.

        Coverage IDs must have been registered by the trusted catalog before
        this output. A product field in this output cannot register its own key.
        ``catalog_info`` is normally an internal RoundProvider property; if a
        trusted caller exports it, only its real empty/two-field shape is read.
        """
        if not isinstance(data, Mapping):
            return self._project(data, 'public', public=True)
        registered = frozenset(self._catalog_ids)
        result = self._project({key: value for key, value in data.items()
                                if key not in _PUBLIC_CATALOG_METADATA}, 'public', public=True)

        def count(value, maximum, *, nullable=False):
            if nullable and value is None:
                return None
            if type(value) is not int or not (1 if nullable else 0) <= value <= maximum:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            return value

        def dated(value):
            if value is None:
                return None
            if not isinstance(value, str):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            try:
                valid = date.fromisoformat(value).isoformat() == value
            except ValueError:
                valid = False
            if not valid:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            return value

        for field in _PUBLIC_CATALOG_METADATA & set(data):
            value = data[field]
            try:
                if field in ('total_scope', 'pagination_scope'):
                    if value != 'selected_round_objects':
                        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    projected = value
                elif field == 'selected_round_count':
                    projected = count(value, 12)
                elif field == 'catalog_total_count':
                    projected = count(value, 100000, nullable=True)
                elif field == 'catalog_source_date':
                    projected = dated(value)
                elif field == 'catalog_truncated':
                    if value is not None and type(value) is not bool:
                        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    projected = value
                elif field == 'availability_reason':
                    if value is not None and value != 'no_matching_verified_object':
                        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    projected = value
                elif field == 'catalog_info':
                    if not isinstance(value, Mapping) or set(value) not in (set(), {'total_count', 'source_date'}):
                        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    projected = ({'total_count': count(value['total_count'], 100000, nullable=True),
                                  'source_date': dated(value['source_date'])} if value else {})
                else:  # field_coverage: no arbitrary keys, facets or strings.
                    if not isinstance(value, Mapping) or len(value) > 12:
                        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    projected = {}
                    for identity, facets in value.items():
                        if (not isinstance(identity, str) or identity not in registered
                                or identity in self._identities or _ALIAS.search(identity) or identity.startswith('src_')
                                or self._text(identity, maximum=300, raw_guard=False) != identity
                                or not isinstance(facets, list) or len(facets) > len(_PUBLIC_CATALOG_FACETS)
                                or any(not isinstance(facet, str) or facet not in _PUBLIC_CATALOG_FACETS for facet in facets)
                                or len(set(facets)) != len(facets)):
                            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                        projected[identity] = list(facets)
                result[field] = projected
            except (BoundaryError, TypeError):
                # A known but invalid metadata field is an explicit gap, never
                # a silently empty coverage map or a partial authoritative one.
                result[field] = self._gap()
        return result

    def model_tool_result(self, name, output, *, compact_metric_sources=False):
        """Optional lossless source-map transport after the normal projection."""
        result = self.tool_result(name, output)
        if (not compact_metric_sources or name not in _FORMAL_METRIC_TOOLS
                or not isinstance(result, Mapping)):
            return result
        return self._compact_metric_sources_atomically(lambda: self._compact_projected_metric_message(result))

    def _compact_metric_sources_atomically(self, project):
        before = tuple(copy.deepcopy(value) for value in (
            self._metric_source_sets, self._metric_source_index, self._metric_source_bindings))
        try:
            return project()
        except Exception:
            self._metric_source_sets, self._metric_source_index, self._metric_source_bindings = before
            raise

    def _compact_projected_metric_message(self, result, *, used=None):
        """Compact all complete projected snapshots, not just the top list.

        Only actual top-level registered records grant answer-ref bindings.
        Nested method snapshots remain transport-only until formally registered.
        """
        used = {} if used is None else used
        for row in result.get('metrics', []):
            if (isinstance(row, Mapping) and set(row) == {'metric_id', 'metric', 'kind', 'evidence_ids'}
                    and isinstance(row['metric'], Mapping) and set(row['metric']) == _METRIC_FIELDS):
                self._register_metric_sources(row['metric'], kind=row['kind'], claimable=True)
        def compact(value):
            if isinstance(value, Mapping):
                if set(value) == _METRIC_FIELDS:
                    ref = self._register_metric_sources(value)
                    used[ref] = copy.deepcopy(self._metric_source_sets[ref])
                    return {**{key: copy.deepcopy(item) for key, item in value.items() if key != 'source_versions'},
                            'source_version_ref': ref}
                return {key: compact(item) for key, item in value.items()}
            if isinstance(value, list):
                return [compact(item) for item in value]
            return value
        result = compact(result)
        if used:
            result['source_version_sets'] = used
            result['metric_source_policy'] = _METRIC_SOURCE_POLICY
        return result

    def _register_metric_sources(self, metric, *, kind=None, claimable=False):
        """Only already-projected complete local snapshots can enter a catalog."""
        if (not isinstance(metric, Mapping) or set(metric) != _METRIC_FIELDS
                or not isinstance(metric['source_versions'], Mapping)
                or not metric['source_versions'] or metric['metric_id'] not in self._reverse):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        sources = metric['source_versions']
        if (len(sources) > _METRIC_PROVENANCE_LIMIT
                or any(key not in self._reverse or value not in self._reverse for key, value in sources.items())):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        signature = {key: copy.deepcopy(metric[key]) for key in _METRIC_REF_FIELDS}
        previous = self._metric_source_bindings.get(metric['metric_id'])
        if previous and (previous['signature'] != signature
                         or (previous['kind'] is not None and kind is not None and previous['kind'] != kind)):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        canonical = json.dumps(sources, sort_keys=True, ensure_ascii=False, allow_nan=False)
        ref = self._metric_source_index.get(canonical)
        if ref is None:
            ref = 'vset_' + secrets.token_hex(4)
            while ref in self._metric_source_sets:
                ref = 'vset_' + secrets.token_hex(4)
            self._metric_source_sets[ref] = copy.deepcopy(dict(sources))
            self._metric_source_index[canonical] = ref
        self._metric_source_bindings[metric['metric_id']] = {
            'ref': ref, 'signature': signature, 'kind': kind if kind is not None else previous['kind'] if previous else None,
            'claimable': claimable or bool(previous and previous['claimable'])}
        return ref

    def _compact_amount_claim(self, claim, used):
        if not isinstance(claim, Mapping) or not _METRIC_REF_FIELDS <= set(claim):
            return claim
        binding = self._metric_source_bindings.get(claim['metric_id'])
        if (binding is None or not binding['claimable']
                or any(claim[key] != binding['signature'][key] for key in _METRIC_REF_FIELDS)
                or (binding['kind'] is not None and claim.get('claim_kind') != binding['kind'])):
            return claim
        claim['source_version_ref'] = binding['ref']
        del claim['source_versions']
        used[binding['ref']] = copy.deepcopy(self._metric_source_sets[binding['ref']])
        return claim

    def _expand_metric_signature(self, signature, *, claim=False):
        """Expand one bound reference; never fill missing identity/date/basis fields."""
        if (not isinstance(signature, Mapping) or 'source_versions' in signature
                or not (_METRIC_REF_FIELDS - {'source_versions'}) <= set(signature)):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        ref, mid = signature.get('source_version_ref'), signature.get('metric_id')
        if not isinstance(ref, str) or not _METRIC_SOURCE_REF.fullmatch(ref) or not isinstance(mid, str):
            raise BoundaryError('BOUNDARY_UNKNOWN_ALIAS')
        binding = self._metric_source_bindings.get(mid)
        if (binding is None or ref != binding['ref'] or ref not in self._metric_source_sets
                or (claim and not binding['claimable'])
                or any(type(signature[key]) is not type(binding['signature'][key])
                       or signature[key] != binding['signature'][key]
                       for key in _METRIC_REF_FIELDS - {'source_versions'})
                or ((claim or 'claim_kind' in signature)
                    and signature.get('claim_kind') != binding['kind'])):
            raise BoundaryError('BOUNDARY_UNKNOWN_ALIAS')
        result = dict(signature)
        del result['source_version_ref']
        result['source_versions'] = copy.deepcopy(self._metric_source_sets[ref])
        return result

    def _expand_answer_metric_sources(self, output):
        """Model input cannot define catalogs or use refs outside exact claims."""
        def inspect(value, *, claim=False):
            if isinstance(value, Mapping):
                if 'source_version_sets' in value:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                if 'source_version_ref' in value:
                    allowed = (_METRIC_REF_FIELDS - {'source_versions'}) | {'source_version_ref', 'text', 'claim_kind', 'occurrence_start'}
                    if (not claim or set(value) - allowed or not {'text', 'claim_kind'} <= set(value)):
                        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    return self._expand_metric_signature(value, claim=True)
                return {key: inspect(item, claim=False) for key, item in value.items()}
            if isinstance(value, list):
                return [inspect(item, claim=claim) for item in value]
            return value
        if not isinstance(output, Mapping):
            return output
        if isinstance(output.get('amount_claims'), list) and len(output['amount_claims']) > _AMOUNT_CLAIM_LIMIT:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result = {}
        for key, value in output.items():
            if key in ('source_version_sets', 'source_version_ref'):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result[key] = inspect(value, claim=key == 'amount_claims' and isinstance(value, list))
        return result

    def _project_fact(self, fields, *, public=False):
        identifiers = {key: fields[key] for key in ('id', 'evidence_id') if key in fields}
        projected = self._project({key: value for key, value in fields.items() if key not in identifiers},
                                  'public' if public else 'profile', public=public)
        projected.update({key: self._alias(value) for key, value in identifiers.items()})
        return projected

    def _skill_data(self, data):
        """Only load_skill's registered local document is trusted instruction prose.

        This never expands the native tool registry or removes credential/PII
        filtering. Other tools cannot smuggle arbitrary text as instructions.
        """
        if not isinstance(data, Mapping):
            return self._gap()
        result = {}
        for key in ('skill', 'reference', 'instructions', 'notice', 'references', 'tools'):
            value = data.get(key)
            if key == 'instructions' and isinstance(value, str):
                result[key] = self._text(value, maximum=65536, raw_guard=False)
            elif key == 'tools' and isinstance(value, list):
                result[key] = [name for name in value[:64] if isinstance(name, str)
                               and (name in _DOMAINS or name in ('answer_facts', 'answer_reviewed'))]
            elif key == 'references' and isinstance(value, list):
                result[key] = [name for name in value[:24] if isinstance(name, str)
                               and re.fullmatch(r'[a-z0-9][a-z0-9._-]*\.md', name)]
            elif value is None and key == 'reference':
                result[key] = None
            elif isinstance(value, str):
                result[key] = self._text(value, maximum=500, raw_guard=False)
        self._counts['omitted'] += len(set(data) - set(result))
        return result

    def _service_terms(self, data, *, offer=False):
        """Exact synthetic service fields, never a generic merchant/rules object.

        An incomplete price/condition row is withheld atomically. Unknown
        eligibility requirements cannot silently turn into an easier offer.
        This is a disclosure schema, not acceptance or entitlement authority.
        """
        required = _SERVICE_OFFER_FIELDS if offer else _SERVICE_TERM_FIELDS | {'notice'}
        allowed = required | (_SERVICE_PREVIEW_FIELDS if offer else set())
        if not isinstance(data, Mapping) or not required <= set(data):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result = {'offer_id': self._contract_string(data['offer_id'], identifier=True),
                  'title': self._contract_string(data['title'], maximum=160)}
        if data['benefit_type'] != 'simulated_service_fee_waiver' or data['currency'] != 'CNY':
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result.update(benefit_type=data['benefit_type'], currency=data['currency'])
        for key in ('monthly_value_minor', 'duration_months', 'max_cost_minor',
                    'minimum_monthly_salary_minor', 'salary_months_required'):
            value = data[key]
            if type(value) is not int or value < 0 or (key == 'duration_months' and value == 0):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result[key] = value
        for key in ('requires_salary_deposit', 'requires_investment_purchase'):
            if type(data[key]) is not bool:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result[key] = data[key]
        conditions = data['conditions']
        if not isinstance(conditions, list) or not 1 <= len(conditions) <= 12:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result['conditions'] = [self._contract_string(item, maximum=500) for item in conditions]
        result['valid_through'] = self._contract_date(data['valid_through'])
        if offer:
            eligibility = data['eligibility']
            if (not isinstance(eligibility, Mapping) or not eligibility
                    or set(eligibility) - {'active_account_required', 'salary_intent_required'}
                    or any(type(value) is not bool for value in eligibility.values())):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result['eligibility'] = dict(eligibility)
            result['valid_from'] = self._contract_date(data['valid_from'])
            if data['application_status_on_accept'] != _SERVICE_PENDING:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result['application_status_on_accept'] = _SERVICE_PENDING
            if _SERVICE_PREVIEW_FIELDS & set(data):
                if (not _SERVICE_PREVIEW_FIELDS <= set(data)
                        or data['eligibility_status'] != 'conditional_application'
                        or data['review_status'] != 'rules_checked'
                        or type(data['investment_purchase_required']) is not bool):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                result.update(eligibility_status='conditional_application', review_status='rules_checked',
                              status_label=self._contract_string(data['status_label'], maximum=160),
                              investment_purchase_required=data['investment_purchase_required'])
        else:
            result['notice'] = self._contract_string(data['notice'], maximum=1500)
        self._counts['omitted'] += len(set(data) - allowed)
        return result

    def _service_application(self, data):
        required = {'application_id', 'draft_id', 'offer_id', 'status', 'terms',
                    'created_at', 'idempotent_replay', 'notice'}
        if (not isinstance(data, Mapping) or not required <= set(data)
                or data['status'] != _SERVICE_PENDING or type(data['idempotent_replay']) is not bool
                or type(data['created_at']) not in (int, float)
                or not math.isfinite(data['created_at']) or data['created_at'] < 0):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        terms = self._service_terms(data['terms'])
        if data['offer_id'] != data['terms']['offer_id']:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result = {key: self._contract_string(data[key], identifier=True)
                  for key in ('application_id', 'draft_id', 'offer_id')}
        result.update(status=_SERVICE_PENDING, terms=terms, created_at=data['created_at'],
                      idempotent_replay=data['idempotent_replay'],
                      notice=self._contract_string(data['notice'], maximum=1500))
        self._counts['omitted'] += len(set(data) - required)
        return result

    def _service_rows(self, rows, *, applications=False):
        if not isinstance(rows, list):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result = []
        for row in rows[:self.max_private_items]:
            try:
                result.append(self._service_application(row) if applications else
                              self._service_terms(row, offer=True))
            except (BoundaryError, TypeError, ValueError, OverflowError):
                result.append(self._gap())
        if len(rows) > self.max_private_items:
            self._counts['omitted'] += len(rows) - self.max_private_items
            # A cap is explicit; an incomplete list cannot look like all applications.
            result.append(self._gap())
        return result

    def _service_data(self, name, data):
        """Service tools cannot disclose saved memories or accept model conditions."""
        try:
            if not isinstance(data, Mapping):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result = {'shared_memory': []}
            if name == 'service_offer_rules':
                required = {'schema_version', 'environment', 'notice', 'offers'}
                allowed = required | {'revision'}
                if (not required <= set(data) or data['schema_version'] != _SERVICE_RULE_VERSION
                        or data['environment'] != 'synthetic'):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                result.update(schema_version=_SERVICE_RULE_VERSION, environment='synthetic',
                              offers=self._service_rows(data['offers']))
            elif name == 'service_offer_preview':
                required = {'status', 'draft_id', 'offers', 'notice'}
                allowed = required | {'conditions', 'message', 'memory_revision', 'rule_version',
                                      'expires_in_seconds', 'revision'}
                if not required <= set(data) or data['status'] not in {'previewed', 'no_offer', 'clarification'}:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                result.update(status=data['status'], offers=self._service_rows(data['offers']))
                result['draft_id'] = (None if data['draft_id'] is None else
                                      self._contract_string(data['draft_id'], identifier=True))
                if data['status'] == 'previewed':
                    if (data['draft_id'] is None or not data['offers']
                            or not {'conditions', 'rule_version', 'expires_in_seconds', 'memory_revision'} <= set(data)
                            or data['rule_version'] != _SERVICE_RULE_VERSION
                            or type(data['expires_in_seconds']) is not int or data['expires_in_seconds'] != 900):
                        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    result['expires_in_seconds'] = data['expires_in_seconds']
                elif data['draft_id'] is not None or data['offers']:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                if 'conditions' in data:
                    conditions = data['conditions']
                    if (not isinstance(conditions, Mapping) or not _SERVICE_CONDITIONS <= set(conditions)
                            or set(conditions) - _SERVICE_CONDITION_FIELDS
                            or any(type(value) is not bool for value in conditions.values())):
                        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    result['conditions'] = dict(conditions)
                if 'message' in data:
                    result['message'] = self._contract_string(data['message'], maximum=1500)
                for key in ('rule_version', 'memory_revision'):
                    if key in data:
                        result[key] = (None if data[key] is None and key == 'memory_revision' else
                                       self._contract_string(data[key], identifier=True, prose=False))
            else:
                required = {'environment', 'applications', 'notice'}
                allowed = required | {'revision'}
                if not required <= set(data) or data['environment'] != 'simulation':
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                result.update(environment='simulation',
                              applications=self._service_rows(data['applications'], applications=True))
            result['notice'] = self._contract_string(data['notice'], maximum=1500)
            if 'revision' in data:
                result['revision'] = self._contract_string(data['revision'], identifier=True, prose=False)
            self._counts['omitted'] += len(set(data) - allowed)
            return result
        except (BoundaryError, TypeError, ValueError, OverflowError):
            return self._gap()

    def _contract_string(self, value, *, identifier=False, maximum=256, prose=True):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        alias = self._alias(value, prose=prose) if identifier else None
        nonprose = value in self._nonprose_identifiers
        # Check the original for credentials before exporting its alias. The
        # temporary replacement avoids classifying an opaque version hash as an
        # unregistered business ID; it never changes another text's projection.
        if nonprose:
            self._nonprose_identifiers.discard(value)
        temporary = None
        try:
            checked = value
            if identifier and _METRIC_OPAQUE_REF.fullmatch(value):
                checked = re.sub(r'(?<![A-Za-z0-9])[a-f0-9]{12,64}(?![A-Za-z0-9])', 'opaque', value)
                if checked != value:
                    # Keep the entire typed reference reversible after masking
                    # its hash. Credential scanning still runs before aliasing;
                    # account_summary/account_limits in a formal source path
                    # are not unregistered record IDs in ordinary prose.
                    temporary = (checked, self._forward.get(checked), checked in self._nonprose_identifiers)
                    self._forward[checked] = alias
                    self._nonprose_identifiers.discard(checked)
            clean = self._text(checked, maximum=maximum, raw_guard=False)
        finally:
            if temporary is not None:
                checked, previous, was_nonprose = temporary
                if previous is None:
                    self._forward.pop(checked, None)
                else:
                    self._forward[checked] = previous
                if was_nonprose:
                    self._nonprose_identifiers.add(checked)
            if nonprose:
                self._nonprose_identifiers.add(value)
        if _HIDDEN in clean:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        if identifier:
            return alias
        # Signed method labels/assumptions retain their exact original spelling.
        # Apply known aliases without changing punctuation through NFKC. Final
        # answer fragments use the answer's normalization separately below.
        safe = value
        for original, replacement in sorted(self._forward.items(), key=lambda item: len(item[0]), reverse=True):
            if original not in self._public_ids and original not in self._nonprose_identifiers:
                safe = safe.replace(original, replacement)
        if unicodedata.normalize('NFKC', safe) != clean:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        return safe

    @staticmethod
    def _contract_date(value):
        if not isinstance(value, str):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE') from None
        return value

    def _metric_signature(self, metric):
        """Project every signed field or reject the entire monetary signature."""
        if not isinstance(metric, Mapping) or not _METRIC_REF_FIELDS <= set(metric):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        status = metric['calculation_status']
        if (not isinstance(status, str) or status not in _METRIC_STATUSES or metric['unit'] != 'minor_currency'
                or not isinstance(metric['currency'], str)
                or not re.fullmatch(r'[A-Z]{3}', metric['currency'])):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        amount = metric['value_minor']
        if ((status in ('calculated', 'partial') and type(amount) is not int)
                or (status in ('unavailable', 'infeasible') and amount is not None)):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        dates = {key: metric[key] for key in ('as_of', 'period_start', 'period_end')}
        if dates['as_of'] is not None:
            self._contract_date(dates['as_of'])
            if dates['period_start'] is not None or dates['period_end'] is not None:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        else:
            self._contract_date(dates['period_start'])
            self._contract_date(dates['period_end'])
            if dates['period_start'] > dates['period_end']:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        versions = metric['source_versions']
        if not isinstance(versions, Mapping) or not 1 <= len(versions) <= _METRIC_PROVENANCE_LIMIT:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result = {'value_minor': amount, 'unit': metric['unit'], 'currency': metric['currency'],
                  'calculation_status': status, **dates}
        for key in ('metric_id', 'subject_id', 'metric_name', 'data_scope', 'basis', 'formula_id'):
            if key == 'basis' and metric[key] in _FORMAL_METRIC_BASES:
                result[key] = self._contract_string(metric[key], identifier=True, prose=False)
            elif key == 'formula_id' and metric[key] in _FORMAL_TOOL_FORMULAS:
                result[key] = self._contract_string(metric[key], identifier=True, prose=False)
            else:
                result[key] = self._contract_string(metric[key], identifier=key in ('metric_id', 'subject_id'))
        result['source_versions'] = {
            self._contract_string(key, identifier=True, prose=False):
            self._contract_string(value, identifier=True, prose=False)
            for key, value in versions.items()}
        return result

    def _metric_assumption(self, assumption):
        if not isinstance(assumption, Mapping):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        kind = assumption.get('type', assumption.get('kind'))
        if not isinstance(kind, str):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        # Formal Base money_calculation emits indexed input values and a
        # separate multiplication factor. These are immutable typed scalars,
        # not permission to export arbitrary user text as a hypothesis.
        if kind == 'user_hypothesis':
            if set(assumption) == {'type', 'input_index', 'value_minor'}:
                index, value = assumption['input_index'], assumption['value_minor']
                if (type(index) is not int or not 0 <= index < 20
                        or type(value) is not int or not -100000000 <= value <= 100000000):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            elif set(assumption) == {'type', 'factor'}:
                factor = assumption['factor']
                if type(factor) is not int or not 1 <= factor <= 1200:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            else:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            return dict(assumption)
        fields = {
            'user_planning_allocation': {'reserve_id', 'target_minor', 'type'},
            'user_stress_assumption': {'event_id', 'type', 'original_on', 'assumed_on',
                                      'original_amount_minor', 'assumed_amount_minor', 'probability'},
            'recorded_forecast_candidate': {'event_id', 'type', 'notice'},
            'user_planning_reserve': {'type', 'label', 'amount_minor', 'commitment_ids',
                                      'already_budgeted_minor', 'additional_reserve_minor'},
            'user_requested_emergency_floor': {'type', 'amount_minor'},
            'pending_user_confirmation': {'type', 'operation_id'},
            'conditional_service_terms': {'type', 'status', 'duration_months'},
            'method_limit': {'kind', 'future_proceeds', 'execution'},
        }.get(kind)
        if fields is None or set(assumption) != fields:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        if kind == 'method_limit':
            if (assumption['future_proceeds'] != 'not_projected'
                    or assumption['execution'] != 'planning_only'):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            return dict(assumption)
        result = {}
        for key, value in assumption.items():
            if key.endswith('_id'):
                result[key] = self._contract_string(value, identifier=True)
            elif key == 'commitment_ids':
                # Base contract stores the exact comma-separated scalar, not a
                # list. Alias each local reference and preserve that signature.
                if not isinstance(value, str) or len(value) > 6000:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                refs = value.split(',') if value else []
                if len(refs) > 32 or len(set(refs)) != len(refs):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                result[key] = ','.join(self._contract_string(ref, identifier=True) for ref in refs)
            elif key.endswith('_minor'):
                if type(value) is not int or value < 0:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                result[key] = value
            elif key.endswith('_on'):
                result[key] = self._contract_date(value)
            elif key == 'probability':
                if value is not None:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                result[key] = None
            elif kind == 'conditional_service_terms' and key == 'duration_months':
                if type(value) is not int or value < 1:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                result[key] = value
            elif kind == 'conditional_service_terms' and key == 'status':
                if value not in {'approved_rule_conditional', 'previewed', _SERVICE_PENDING}:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                result[key] = value
            else:
                result[key] = self._contract_string(value, maximum=240)
        return result

    def _metric_snapshot(self, metric):
        if not isinstance(metric, Mapping) or set(metric) != _METRIC_FIELDS or metric['contract_version'] != '1.0.0':
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result = self._metric_signature(metric)
        result['contract_version'] = metric['contract_version']
        for key in ('input_refs', 'excluded_items', 'coverage_gaps'):
            values = metric[key]
            limit = _METRIC_PROVENANCE_LIMIT if key == 'input_refs' else 24
            if not isinstance(values, list) or len(values) > limit:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result[key] = [self._contract_string(value, identifier=key == 'input_refs', maximum=500)
                           for value in values]
        if not result['input_refs'] or (metric['value_minor'] is None and not result['coverage_gaps']):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        assumptions = metric['assumed_inputs']
        if not isinstance(assumptions, list) or len(assumptions) > 64:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result['assumed_inputs'] = [self._metric_assumption(value) for value in assumptions]
        return result

    def _metric_record(self, record, *, claim=False):
        allowed = {'metric_id', 'metric', 'kind', 'evidence_ids'} | ({'claim'} if claim else set())
        if (not isinstance(record, Mapping) or set(record) != allowed
                or not isinstance(record['kind'], str) or record['kind'] not in _METRIC_KINDS
                or not isinstance(record['metric'], Mapping)
                or record['metric_id'] != record['metric'].get('metric_id')
                or not isinstance(record['evidence_ids'], list)
                or not 1 <= len(record['evidence_ids']) <= 24):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result = {'metric_id': self._contract_string(record['metric_id'], identifier=True),
                  'metric': self._metric_snapshot(record['metric']), 'kind': record['kind'],
                  'evidence_ids': [self._contract_string(value, identifier=True) for value in record['evidence_ids']]}
        if claim:
            result['claim'] = self._amount_claim(record['claim'])
        return result

    def _metric_collection(self, metrics, *, formal=False):
        limit = _FORMAL_METRIC_LIMIT if formal else self.max_private_items
        def project(metric):
            try:
                return self._metric_record(metric) if isinstance(metric, Mapping) and 'metric' in metric else self._metric_snapshot(metric)
            except (BoundaryError, TypeError):
                return self._gap()
        if isinstance(metrics, Mapping):
            if 'metric_id' in metrics:
                return project(metrics)
            result = {}
            for key, metric in list(metrics.items())[:limit]:
                if not isinstance(key, str):
                    self._counts['omitted'] += 1
                    continue
                label = key if re.fullmatch(r'[a-z][a-z0-9_]{0,79}', key) else self._alias(key, 'label')
                result[label] = project(metric)
            self._counts['omitted'] += max(0, len(metrics) - limit)
            return result
        if isinstance(metrics, list):
            self._counts['omitted'] += max(0, len(metrics) - limit)
            return [project(metric) for metric in metrics[:limit]]
        return self._gap()

    def _amount_claim(self, claim):
        allowed = _METRIC_REF_FIELDS | {'text', 'claim_kind', 'occurrence_start'}
        if (not isinstance(claim, Mapping) or set(claim) - allowed
                or not (_METRIC_REF_FIELDS | {'text', 'claim_kind'}) <= set(claim)
                or not isinstance(claim.get('claim_kind'), str) or claim['claim_kind'] not in _METRIC_KINDS):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result = self._metric_signature(claim)
        self._contract_string(claim['text'], maximum=1200)
        result['text'] = self._text(claim['text'], maximum=1200, raw_guard=False)
        result['claim_kind'] = claim['claim_kind']
        if 'occurrence_start' in claim:
            if type(claim['occurrence_start']) is not int or claim['occurrence_start'] < 0:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result['occurrence_start'] = claim['occurrence_start']
        return result

    def _amount_claims(self, claims, *, source_text=None, target_text=None):
        if not isinstance(claims, list) or len(claims) > _AMOUNT_CLAIM_LIMIT:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result = []
        for claim in claims:
            projected = self._amount_claim(claim)
            if 'occurrence_start' in projected and source_text is not None and target_text is not None:
                start = claim['occurrence_start']
                if (not isinstance(source_text, str) or not isinstance(target_text, str)
                        or source_text[start:start + len(claim['text'])] != claim['text']):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                transformed_start = len(self._text(source_text[:start], raw_guard=False))
                if target_text[transformed_start:transformed_start + len(projected['text'])] != projected['text']:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                projected['occurrence_start'] = transformed_start
            result.append(projected)
        return result

    def _amount_contract(self, contract, *, source_text=None, target_text=None):
        if (not isinstance(contract, Mapping) or set(contract) != {'status', 'amount_claims',
                'amount_occurrence_count', 'independent_review_required'}
                or contract['status'] != 'validated' or contract['independent_review_required'] is not True
                or type(contract['amount_occurrence_count']) is not int or contract['amount_occurrence_count'] < 0
                or not isinstance(contract['amount_claims'], list) or len(contract['amount_claims']) > _AMOUNT_CLAIM_LIMIT):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        records = []
        for record in contract['amount_claims']:
            projected = self._metric_record(record, claim=True)
            projected['claim'] = self._amount_claims([record['claim']], source_text=source_text,
                                                    target_text=target_text)[0]
            records.append(projected)
        return {**{key: contract[key] for key in ('status', 'amount_occurrence_count', 'independent_review_required')},
                'amount_claims': records}

    @staticmethod
    def _public_amounts(text):
        """Exact display-unit conversion, not a calculation of personal funds."""
        if not isinstance(text, str):
            return set()
        currencies = {'元': 'CNY', '圆': 'CNY', '人民币': 'CNY', 'RMB': 'CNY',
                      '美元': 'USD', '欧元': 'EUR', '英镑': 'GBP', '日元': 'JPY'}
        amounts = set()
        for number, scale, unit in re.findall(r'([-+]?[\d.,]+)\s*(万亿|亿|万)?\s*(人民币|美元|欧元|英镑|日元|元|圆|CNY|RMB|USD|EUR|GBP|JPY)', text, re.I):
            try:
                if not re.fullmatch(r'[-+]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', number):
                    return set()
                value = Decimal(number.replace(',', '')) * 100 * {'': 1, '万': 10000, '亿': 100000000, '万亿': 1000000000000}[scale]
                if not value.is_finite() or value != value.to_integral_value():
                    return set()
            except (InvalidOperation, ValueError):
                return set()
            amounts.add((int(value), currencies.get(unit.upper(), unit.upper())))
        return amounts

    def _public_fact_source(self, name, data, fact):
        if (not isinstance(data, Mapping) or fact.get('unit') != 'public_minor_currency'
                or type(fact.get('value')) is not int or not isinstance(fact.get('currency'), str)):
            return None
        amount = (fact['value'], fact['currency'])
        if name == 'knowledge_search':
            for hit in data.get('hits', []):
                # KnowledgeRetrieval's canonical consumer checks withdrawn=False
                # before projecting a hit, and omits that flag in its return.
                # Accept that complete return schema, not arbitrary text/metadata.
                canonical = (isinstance(hit, Mapping)
                    and all(isinstance(hit.get(key), str) and bool(hit[key]) for key in (
                        'citation_id', 'release_id', 'document_id', 'document_version_id',
                        'parse_id', 'span_id', 'source_url'))
                    and isinstance(hit.get('locator'), Mapping))
                if (isinstance(hit, Mapping) and hit.get('citation_id') == fact.get('citation_id')
                        and isinstance(hit.get('citation_id'), str) and hit.get('visibility') == 'public'
                        and hit.get('outbound_allowed') is True and canonical
                        and (hit.get('withdrawn') is False or 'withdrawn' not in hit)
                        and hit.get('content_status') == 'full_text'
                        and amount in self._public_amounts(hit.get('excerpt'))):
                    return hit['excerpt']
        elif name.startswith('market_'):
            if (data.get('id') == fact.get('article_id') and isinstance(fact.get('article_id'), str)
                    and data.get('status') == 'active' and data.get('read_scope') == 'full_text'
                    and data.get('link_status') == 'verified'
                    and amount in self._public_amounts(data.get('evidence_excerpt'))):
                return data['evidence_excerpt']
            events = ([data] if 'facts' in data else []) + data.get('events', []) + data.get('supporting_events', [])
            for event in events:
                if not isinstance(event, Mapping) or event.get('status') != 'published':
                    continue
                articles = {article.get('id') for article in event.get('articles', []) if isinstance(article, Mapping)
                            and article.get('status') == 'active' and article.get('read_scope') == 'full_text'
                            and article.get('link_status') == 'verified'}
                for row in event.get('facts', []):
                    if (isinstance(row, Mapping) and row.get('article_id') == fact.get('article_id')
                            and isinstance(fact.get('article_id'), str) and fact['article_id'] in articles
                            and amount in self._public_amounts(row.get('text')) & self._public_amounts(row.get('quote'))):
                        return row['quote']
        return None

    def _fact_text(self, text):
        """Retain known financial labels, never arbitrary raw fact prose/prefixes."""
        if not isinstance(text, str) or ' · ' not in text:
            return '本地已核验业务字段'
        prefix, tail = text.rsplit(' · ', 1)
        label, separator, amount = tail.partition('：')
        if not separator or label not in _FACT_LABELS:
            return '本地已核验业务字段'
        clean_prefix = self._text(prefix, maximum=160, raw_guard=False)
        if clean_prefix not in _SAFE_LABELS and not _ALIAS.fullmatch(clean_prefix):
            clean_prefix = '查询记录'
        clean_amount = self._text(amount, maximum=80, raw_guard=False)
        if not re.fullmatch(r'-?[\d,.]+(?:元| [A-Z]{3})?', clean_amount):
            return clean_prefix + ' · ' + label
        return clean_prefix + ' · ' + label + '：' + clean_amount

    @staticmethod
    def _scalars(value):
        if isinstance(value, Mapping):
            return {scalar for item in value.values() for scalar in CloudDataBoundary._scalars(item)}
        if isinstance(value, list):
            return {scalar for item in value for scalar in CloudDataBoundary._scalars(item)}
        return {value} if isinstance(value, (str, int, float, bool)) else set()

    def native_tools(self, schemas, *, compact_metric_sources=False):
        if not isinstance(schemas, list):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        result = copy.deepcopy(schemas)
        def walk(schema, field=''):
            if not isinstance(schema, dict):
                return
            if 'enum' in schema and field in _IDS:
                schema['enum'] = [self._alias(v) if isinstance(v, str) and v not in self._public_ids else v for v in schema['enum']]
            for name, child in schema.get('properties', {}).items():
                walk(child, name)
            if 'items' in schema:
                walk(schema['items'], field)
        for tool in result:
            function = tool.get('function', {})
            if function.get('name') not in _DOMAINS and function.get('name') not in ('answer_facts', 'answer_reviewed'):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            walk(function.get('parameters', {}))
            if compact_metric_sources and function.get('name') == 'answer_reviewed':
                item = function.get('parameters', {}).get('properties', {}).get('amount_claims', {}).get('items')
                if (not isinstance(item, Mapping) or not _METRIC_REF_FIELDS <= set(item.get('properties', {}))
                        or not (_METRIC_REF_FIELDS | {'text', 'claim_kind'}) <= set(item.get('required', []))):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                item['properties']['source_version_ref'] = {
                    'type': 'string', 'pattern': r'^vset_[a-f0-9]{8}$',
                    'description': '复制本轮对应 metric_id 的 source_version_ref；与完整 source_versions 二选一，其他签名字段仍须完整复制。'}
                item['required'] = [key for key in item['required'] if key != 'source_versions']
                item['oneOf'] = [{'required': ['source_versions']}, {'required': ['source_version_ref']}]
        return result

    def _restore(self, value, *, strict=True):
        if isinstance(value, Mapping):
            # Contract source_versions and aggregate labels use aliased keys as
            # well as values. Restore only exact turn aliases, never schema keys.
            return {self._restore(k, strict=strict) if isinstance(k, str) and _ALIAS.fullmatch(k) else k:
                    self._restore(v, strict=strict) for k, v in value.items()}
        if isinstance(value, list):
            return [self._restore(v, strict=strict) for v in value]
        if isinstance(value, str):
            for token in _ALIAS.findall(value):
                if token not in self._reverse and strict:
                    raise BoundaryError('BOUNDARY_UNKNOWN_ALIAS')
            if strict and value not in self._forward and any(token not in self._public_ids and token not in self._forward
                              for token in _RAW_BUSINESS_ID.findall(value)):
                raise BoundaryError('BOUNDARY_UNKNOWN_ALIAS')
            return _ALIAS.sub(lambda m: self._reverse.get(m.group(0), m.group(0)), value)
        return value

    def restore_arguments(self, name, args):
        if name not in _DOMAINS and name not in ('answer_facts', 'answer_reviewed'):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        if not isinstance(args, Mapping):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        if name in _SERVICE_TOOLS:
            if args:
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            return {}
        action = args.get('action')
        if isinstance(action, str) and action in _ACTION_ARGUMENT_ENUMS.get(name, ()):
            restored = self._restore({key: value for key, value in args.items() if key != 'action'})
            restored['action'] = action
            return restored
        # This only restores references; BankTools.invoke still validates and authorizes.
        return self._restore_answer(args) if name == 'answer_reviewed' else self._restore(args)

    def _restore_answer(self, output):
        output = self._expand_answer_metric_sources(output)
        restored = self._restore(output)
        if isinstance(output, Mapping) and 'amount_claims' in output:
            claims, text = output['amount_claims'], output.get('text', '')
            if not isinstance(claims, list) or not isinstance(text, str):
                raise BoundaryError('BOUNDARY_OUTPUT')
            for source, target in zip(claims, restored['amount_claims']):
                if not isinstance(source, Mapping):
                    raise BoundaryError('BOUNDARY_OUTPUT')
                if 'occurrence_start' in source:
                    start, fragment = source['occurrence_start'], source.get('text')
                    if (type(start) is not int or start < 0 or not isinstance(fragment, str)
                            or text[start:start + len(fragment)] != fragment):
                        raise BoundaryError('BOUNDARY_OUTPUT')
                    target['occurrence_start'] = len(self._restore(text[:start]))
        return restored

    def expand_metric_sources(self, output):
        """Local verification of trusted transport, including its exact catalog.

        This does not register any supplied set or grant financial authority.
        Model arguments use the stricter answer-only path above.
        """
        def has_ref(value):
            if isinstance(value, Mapping):
                return 'source_version_ref' in value or any(has_ref(item) for item in value.values())
            if isinstance(value, list):
                return any(has_ref(item) for item in value)
            return False
        if not isinstance(output, Mapping) or 'source_version_sets' not in output:
            if has_ref(output):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            return copy.deepcopy(output)
        catalog = output['source_version_sets']
        if (not isinstance(catalog, Mapping) or not catalog
                or any(not isinstance(ref, str) or ref not in self._metric_source_sets
                       or versions != self._metric_source_sets[ref] for ref, versions in catalog.items())):
            raise BoundaryError('BOUNDARY_UNKNOWN_ALIAS')
        if 'metric_source_policy' in output and output['metric_source_policy'] != _METRIC_SOURCE_POLICY:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        used = set()
        def expand(value):
            if isinstance(value, Mapping):
                if 'source_version_sets' in value:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                if 'source_version_ref' in value:
                    ref = value['source_version_ref']
                    if not isinstance(ref, str) or ref not in catalog:
                        raise BoundaryError('BOUNDARY_UNKNOWN_ALIAS')
                    used.add(ref)
                    value = self._expand_metric_signature(value)
                return {key: expand(item) for key, item in value.items()}
            if isinstance(value, list):
                return [expand(item) for item in value]
            return value
        result = expand({key: value for key, value in output.items() if key not in ('source_version_sets', 'metric_source_policy')})
        if used != set(catalog):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        return result

    def restore_output(self, output):
        return self._restore(self.expand_metric_sources(output))

    def check_output(self, output, *, evidence_ids=None, draft_ids=None, widget_ids=None):
        """Local pre-display filter; callers still check money, truth and permissions."""
        restored = self._restore_answer(output)
        if not isinstance(restored, Mapping):
            raise BoundaryError('BOUNDARY_OUTPUT')
        for key, allowed in (('draft_ids', draft_ids), ('widget_ids', widget_ids)):
            if allowed is not None and set(restored.get(key, [])) - set(allowed):
                raise BoundaryError('BOUNDARY_OUTPUT')
        if evidence_ids is not None:
            used = {i for c in restored.get('claims', []) for i in c.get('evidence_ids', [])}
            if used - set(evidence_ids):
                raise BoundaryError('BOUNDARY_OUTPUT')
        # Names represented by this turn's aliases can be restored for local display.
        # Credentials/contact details are never valid model output, even if user supplied.
        for text in [restored.get('text', ''), *restored.get('followups', []),
                     *[claim.get('text', '') for claim in restored.get('claims', [])],
                     *[claim.get('text', '') for claim in restored.get('amount_claims', [])]]:
            # Completed prose still cannot contain batches of raw records;
            # the record detector distinguishes prose and number formatting.
            redactions_before = self._counts['redactions']
            self._text(text)
            # A new sensitive match is rejected even if a later overlapping
            # match consumes a pre-existing placeholder in the same sentence.
            if self._counts['redactions'] > redactions_before:
                raise BoundaryError('BOUNDARY_OUTPUT')
        if 'amount_claims' in restored:
            # New monetary envelopes use one normalization for display and the
            # referenced text. Recompute offsets rather than trusting old ones
            # after NFKC changes character counts. Preserve local display names.
            original = restored.get('text', '')
            restored = dict(restored)
            restored['text'] = unicodedata.normalize('NFKC', original)
            source_claims = restored['amount_claims']
            checked = self._amount_claims(source_claims)
            restored['amount_claims'] = self._restore(checked)
            for source_claim, claim in zip(source_claims, restored['amount_claims']):
                # Use restored source fragments, not the cloud alias lengths.
                claim['text'] = unicodedata.normalize('NFKC', source_claim['text'])
                if 'occurrence_start' in source_claim:
                    start = source_claim['occurrence_start']
                    if original[start:start + len(source_claim['text'])] != source_claim['text']:
                        raise BoundaryError('BOUNDARY_OUTPUT')
                    claim['occurrence_start'] = len(unicodedata.normalize('NFKC', original[:start]))
                    offset = claim['occurrence_start']
                    if restored['text'][offset:offset + len(claim['text'])] != claim['text']:
                        raise BoundaryError('BOUNDARY_OUTPUT')
        else:
            # FinalResponseProofs compares the displayed reply with the
            # reviewer's normalized draft projection. Keep plain prose on that
            # same NFKC form even when there are no amount-offset contracts.
            restored = dict(restored)
            restored['text'] = unicodedata.normalize('NFKC', restored.get('text', ''))
        return restored

    def review_packet(self, packet, *, compact_metric_sources=False):
        if not isinstance(packet, Mapping):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        result = {}
        if 'question' in packet:
            result['question'] = self.user_text(packet['question'])
        if 'history' in packet:
            result['history'] = self.history(packet['history'])
        if 'server_context' in packet:
            result['server_context'] = self.server_context(packet['server_context'])
        result['evidence'] = []
        for evidence in packet.get('evidence', [])[:48]:
            if not isinstance(evidence, Mapping):
                self._counts['omitted'] += 1
                continue
            name = evidence.get('tool')
            projected = self.tool_result(name, {'evidence_id': evidence.get('id'), 'data': evidence.get('data'), 'facts': []})
            if 'boundary' in projected:
                continue
            result['evidence'].append({'id': projected.get('evidence_id'), 'tool': name,
                                       'data': projected['data'], 'source': self._text(evidence.get('source', ''), maximum=100)})
        # Do not send operation_view credentials/raw receipts to the reviewer.
        result['operations'] = self._project(packet.get('operations', []), 'operations') if 'operations' in _TASK_DOMAINS[self.task] else []
        result['calendar_widgets'] = self._project(packet.get('calendar_widgets', []), 'calendar') if 'calendar' in _TASK_DOMAINS[self.task] else []
        result['draft'] = {}
        # Final-answer envelope has a separate explicit schema.
        draft = packet.get('draft', {})
        if isinstance(draft, Mapping):
            result['draft'] = {k: self._text(v) if k == 'text' else self._project(v, 'profile', key=k)
                               for k, v in draft.items()
                               if k in {'text', 'claims', 'draft_ids', 'widget_ids', 'followups'}}
            for k in ('claims', 'followups'):
                if k in result['draft']:
                    result['draft'][k] = self._project(draft[k], 'profile', key=k)
            if 'amount_claims' in draft:
                try:
                    result['draft']['amount_claims'] = self._amount_claims(draft['amount_claims'],
                        source_text=draft.get('text'), target_text=result['draft'].get('text'))
                except (BoundaryError, TypeError):
                    result['draft']['amount_claims'] = self._gap()
        if 'amount_contract' in packet:
            try:
                result['amount_contract'] = self._amount_contract(packet['amount_contract'],
                    source_text=draft.get('text') if isinstance(draft, Mapping) else None,
                    target_text=result['draft'].get('text'))
            except (BoundaryError, TypeError):
                result['amount_contract'] = self._gap()
        # The reviewer receives only facts supported by projected local evidence.
        # Flattened facts from excluded raw rows must not bypass the same boundary.
        evidence_scalars = {
            evidence['id']: self._scalars(evidence['data']) for evidence in result['evidence']
        }
        projected_evidence = {evidence['id']: evidence for evidence in result['evidence']}
        result['monetary_evidence'] = []
        for fact in packet.get('monetary_evidence', [])[:48]:
            if not isinstance(fact, Mapping):
                self._counts['omitted'] += 1
                continue
            evidence_id = self._alias(fact.get('evidence_id'))
            value = fact.get('value')
            evidence = projected_evidence.get(evidence_id, {})
            source = self._public_fact_source(evidence.get('tool', ''), evidence.get('data', {}), fact)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or (fact.get('unit') == 'public_minor_currency' and source is None)
                    or (value not in evidence_scalars.get(evidence_id, set()) and source is None)):
                self._counts['omitted'] += 1
                continue
            fields = {k: fact[k] for k in ('id', 'evidence_id', 'value', 'currency', 'unit', 'scope', 'citation_id', 'article_id') if k in fact}
            if source is not None:
                fields['scope'] = 'public_statement_not_account_balance'
                fields['text'] = source
            result['monetary_evidence'].append(self._project_fact(fields, public=source is not None))
        policy = packet.get('policy', {})
        result['policy'] = self._project(policy, 'general') if isinstance(policy, Mapping) else {}
        self._counts['omitted'] += sum(k not in {'question', 'history', 'server_context', 'evidence', 'draft', 'monetary_evidence', 'amount_contract', 'operations', 'calendar_widgets', 'policy'} for k in packet)
        if compact_metric_sources:
            return self._compact_metric_sources_atomically(lambda: self._compact_review_metric_sources(result))
        return result

    def _compact_review_metric_sources(self, result):
        used = {}
        contract = result.get('amount_contract', {})
        if isinstance(contract, Mapping) and contract.get('status') == 'validated':
            for row in contract['amount_claims']:
                metric = row['metric']
                ref = self._register_metric_sources(metric, kind=row['kind'], claimable=True)
                row['claim'] = self._compact_amount_claim(row['claim'], used)
                metric['source_version_ref'] = ref
                del metric['source_versions']
                used[ref] = copy.deepcopy(self._metric_source_sets[ref])
        claims = result['draft'].get('amount_claims', [])
        if 'amount_claims' in result['draft'] and isinstance(claims, list):
            result['draft']['amount_claims'] = [self._compact_amount_claim(claim, used) for claim in claims]
        return self._compact_projected_metric_message(result, used=used)

    def tts_text(self, text):
        """Cloud speech gets aliases, never the local display's restored names."""
        safe = self._text(text, maximum=6000)
        return _ALIAS.sub(lambda m: '该联系人' if m.group(0).startswith('person_') else
                          '相关事项' if m.group(0).startswith('label_') else '该业务对象', safe)

    def public_request(self, args, *, tool_name='knowledge_search'):
        if tool_name not in ('knowledge_search', 'public_products') or not isinstance(args, Mapping):
            raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
        allowed = {'query', 'product_id', 'share_class_id', 'channel_id', 'as_of', 'facets', 'limit'}
        if set(args) - allowed:
            raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
        output = {}
        for key, value in args.items():
            if key in ('product_id', 'share_class_id', 'channel_id'):
                if not isinstance(value, str):
                    raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
                restored = self._restore(value)
                if restored not in self._public_ids:
                    raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
                output[key] = restored
            elif key == 'query':
                if not isinstance(value, str) or not 1 <= len(value) <= 500:
                    raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
                clean = self._text(value, maximum=500)
                normalized = unicodedata.normalize('NFKC', value)
                residual = clean
                for identifier in sorted(self._catalog_ids, key=len, reverse=True):
                    residual = residual.replace(identifier, '')
                for term in sorted(self._public_terms, key=len, reverse=True):
                    residual = residual.replace(term, '')
                for word in sorted(_PUBLIC_QUERY_GRAMMAR, key=len, reverse=True):
                    residual = residual.replace(word, '')
                if (clean != normalized or _ALIAS.search(clean)
                        or re.search(r'我|本人|个人|账户|持仓|工资|余额|现金流|\d', residual, re.I)):
                    raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
                if residual.strip(' \t\n，,。.;；:：/、和与的及？?()（）-'):
                    raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
                output[key] = value
            elif key == 'as_of':
                if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
                output[key] = value
            elif key == 'facets':
                choices = _PUBLIC_FACETS
                if not isinstance(value, list) or len(value) > 8 or any(not isinstance(x, str) or x not in choices for x in value):
                    raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
                output[key] = list(value)
            elif key == 'limit':
                if type(value) is not int or value < 1:
                    raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
                # Result limits are resource policy, not evidence of a private
                # query. A valid positive request uses the local provider cap.
                output[key] = min(value, 8)
        return output

    def audit_summary(self):
        """Counts only: never payloads, names, field names, aliases or hashed values."""
        return {'policy_version': POLICY_VERSION, 'redactions': self._counts['redactions'],
                'omitted': self._counts['omitted'], 'aliases': self._counts['aliases'],
                'refusals': self._counts['refusals']}
