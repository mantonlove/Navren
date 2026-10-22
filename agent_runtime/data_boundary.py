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
import hashlib
import json
import math
import re
import secrets
import time
import unicodedata
from decimal import Decimal, InvalidOperation
from datetime import date
from collections import Counter
from collections.abc import Mapping
from urllib.parse import urlsplit

from base_runtime.reply_interaction import validate_interaction
from simulated_bank import BankError
from .knowledge_semantics import SEMANTIC_GAP_CODES, boundary_projection, unique_semantic_gaps
from .knowledge_retrieval import GAP_MESSAGES as KNOWLEDGE_GAP_MESSAGES, FACETS as KNOWLEDGE_FACETS


POLICY_VERSION = 'navren-cloud-boundary-v1'
_HIDDEN = '[本地已屏蔽]'
_ALIAS = re.compile(r'(?<![A-Za-z0-9_])(?:obj|person|label)_[a-f0-9]{16}(?:-main)?(?![A-Za-z0-9_])')
_RAW_BUSINESS_ID = re.compile(
    r'(?<![A-Za-z0-9_])(?:[a-f0-9]{32,64}(?:-[A-Za-z0-9_-]{1,64})?'
    r'|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}'
    r'|(?:account|acct|acc|operation|op|beneficiary|ben|card|transaction|txn|workspace|ws|draft|widget)[_-][A-Za-z0-9_.:-]{4,128})'
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
    'money_calculation': 'general', 'run_generated_code': 'general', 'products': 'wealth', 'wealth_product': 'wealth',
    'wealth_versions': 'wealth', 'research_plans': 'wealth', 'service_offer_rules': 'tasks',
    'service_offer_preview': 'tasks', 'service_application_status': 'tasks',
    'business_records': 'business', 'customer_records': 'business',
    'operation_status': 'operations', 'prepare_operation': 'operations',
    'withdraw_draft': 'operations', 'user_memory': 'profile', 'remember_profile': 'profile',
    'personal_graph': 'profile', 'graph_scenario': 'cashflow',
    'presentation_query':'calendar', 'presentation_detail':'calendar',
    'calendar_query': 'calendar', 'calendar_day': 'calendar', 'calendar_item': 'calendar',
    'calendar_budget_scenario': 'cashflow', 'calendar_prepare_event': 'calendar',
    'financial_list_connections': 'financial_mcp', 'financial_list_accounts': 'financial_mcp',
    'financial_list_cards': 'financial_mcp', 'financial_get_account_overview': 'financial_mcp',
    'financial_list_transactions': 'financial_mcp', 'financial_list_positions': 'financial_mcp',
    'financial_list_insurance': 'financial_mcp', 'financial_get_policy': 'financial_mcp',
    'financial_list_assets': 'financial_mcp', 'financial_list_obligations': 'financial_mcp',
    'financial_get_financial_summary': 'financial_mcp',
    'financial_list_transfers': 'financial_mcp',
}
# Fixed, schema-declared literals are not object identifiers. Exempt only the
# top-level action field of its own tool; other fields and arbitrary card_* text
# still use the strict identifier guard. Tests bind these sets to BankTools SPECS.
_ACTION_ARGUMENT_ENUMS = {
    'prepare_operation': frozenset('''purchase redeem transfer schedule_transfer
        cancel_scheduled_payment close_mandate cancel_subscription card_control
        report_card_lost unblock_card request_card_limit apply_card request_split
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
 打车 外卖 医药费 饮品 油费 充电费
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
_IDS = _IDS | frozenset('metric_id statistic_id subject_id input_refs offer_id application_id point_ref item_ref'.split())
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
                   'items', 'category', 'counterparty', 'offset', 'limit', 'next_offset', 'has_more'},
    'subscriptions': {'candidates', 'classification', 'expected_next_date', 'mandates', 'subscriptions',
                      'frequency', 'merchant_status', 'bank_status', 'minimum_months'},
    'wealth': {'products', 'product', 'profile_eligible', 'risk_notice', 'early_redemption',
              'holdings', 'lots', 'orders', 'principal_summary', 'versions', 'items', 'assumptions',
              'fee_rules', 'rate', 'basis', 'source_date', 'latest_nav', 'nav_on', 'fund_id',
              'detail_url', 'current_channel_status', 'product_code', 'share_class', 'fees',
              'positions', 'valuation', 'performance', 'calculations', 'scope_notice',
              'coverage', 'basis_version', 'simulation', 'is_simulation', 'execution_coverage',
              'selected_products', 'snapshot', 'requirements', 'decision_basis', 'comparison'},
    'business': {'records', 'accounts', 'beneficiaries', 'scheduled_payments', 'split_bills', 'split_participants',
                 'cards', 'card_applications', 'mandates', 'subscriptions', 'orchestration_tasks',
                 'fund_holds', 'service_quotes', 'service_reservations', 'holdings', 'lots', 'orders',
                 'loans', 'loan_installments', 'insurance_policies', 'insurance_claims', 'deposit_contracts',
                 'wealth_goals', 'budgets', 'advice_records', 'disputes', 'support_cases', 'permissions',
                 'permission_grants', 'consents', 'account_limits', 'account_restrictions',
                 'frequency', 'control_type', 'controls', 'network', 'requested_type', 'last_four',
                 'verified', 'bank_name', 'loan_type', 'deposit_type', 'coverage_type', 'steps'},
    'operations': {'operation', 'payload', 'result', 'account', 'product', 'mandate', 'card',
                   'beneficiary', 'budget', 'projected_after_confirmation', 'agent_review',
                   'superseded_operations', 'replacement_id',
                   'withdrawn_payload', 'frequency', 'control_type', 'requested_type', 'last_four',
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
_FORMAL_METRIC_TOOLS = frozenset('''account_summary products business_records budget cashflow monthly_statement annual_statement
 customer_records money_calculation prepare_operation service_offer_rules service_offer_preview
 service_application_status'''.split())
_FORMAL_METRIC_TOOLS = _FORMAL_METRIC_TOOLS | {'presentation_query','presentation_detail'}
_RATIO_REF_FIELDS = (_METRIC_REF_FIELDS - {'value_minor'}) | {'value_decimal'}
_RATIO_FIELDS = (_METRIC_FIELDS - {'value_minor'}) | {'value_decimal'}
_FORMAL_METRIC_LIMIT = 64
_AMOUNT_CLAIM_LIMIT = 24
_METRIC_PROVENANCE_LIMIT = 128
_PUBLIC_CATALOG_METADATA = frozenset('''total_scope selected_round_count pagination_scope
 catalog_total_count catalog_source_date catalog_truncated availability_reason catalog_info
 field_coverage'''.split())
_PUBLIC_CATALOG_FACETS = frozenset({'general', 'fees', 'risk', 'eligibility', 'sale_status'})
_FORMAL_TOOL_FORMULAS = frozenset('tools.' + name + '/1' for name in '''account_balance
 account_cash_total account_principal product_holding_principal simulated_product_minimum simulated_unit_principal
 budget_inputs budget_baseline budget_planning budget_current
 budget_reserve_dedup cashflow_point cashflow_candidates cashflow_confirmed cashflow_account_flow
 cashflow_flow_total statement_observed_totals statement_category customer_record money_hypothesis
 pending_operation_current pending_operation_amount pending_operation_projection service_terms
 current_business_record current_task_record current_service_quote_set'''.split())
_FORMAL_TOOL_FORMULAS = _FORMAL_TOOL_FORMULAS | {'presentation.unavailable_component/1'}
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
    r'|acct-[a-f0-9]{32}-(?:main|reserve|operating)'
    r'|(?:[A-Za-z][A-Za-z0-9_.:-]*/)+acct-[a-f0-9]{12}-(?:main|savings)'
    r'|(?:[A-Za-z][A-Za-z0-9_.:-]*/)+acct-[a-f0-9]{32}-(?:main|reserve|operating)'
    r'|(?:[A-Za-z][A-Za-z0-9_.:-]*/)+(?:[a-f0-9]{32}|[a-f0-9]{64}))\Z')
_METRIC_HOLDING_REF = re.compile(r'holdings/(?P<workspace>[a-f0-9]{32})/'
    r'acct-(?P<prefix>[a-f0-9]{32}|[a-f0-9]{12})-(?P<account>main|savings|reserve|operating)/'
    r'(?P<product>[A-Za-z][A-Za-z0-9_.:-]{0,127})\Z')
_METRIC_SCOPED_REF = re.compile(r'(?:statement/(?P<statement>[a-f0-9]{32})/'
    r'[0-9]{4}(?:-(?:0[1-9]|1[0-2]))?'
    r'|commitments/(?:e[1-9][0-9]{0,2}-|mandate-)?(?P<commitment>[a-f0-9]{12})'
    r'(?:-[A-Za-z][A-Za-z0-9_.:-]{0,127})?)\Z')
_METRIC_KINDS = frozenset('''recorded_fact recorded_projection candidate_scenario
 planning_scenario forecast hypothesis'''.split())
_METRIC_STATUSES = frozenset('calculated partial unavailable infeasible'.split())
_CONTRACT_FIELDS = frozenset('''contract_error unsupported_amounts instruction
 valid_evidence_ids valid_draft_ids valid_widget_ids required_calendar_draft_ids
 review_revision observed_identifiers'''.split())
_AGGREGATE_MAPS = frozenset('totals_minor category_expense_minor category_income_minor held_funds_by_account'.split())
_AGGREGATE_LABELS = frozenset('''income expense refund transfer investment_purchase investment_redemption
 cash_delta income_expense_surplus 工资 房租 餐饮 住房 交通 通讯 教育 医疗 健康 购物
 娱乐 生活 订阅 其他 收入 支出 退款 打车 外卖 医药费 饮品 油费 充电费 CNY USD EUR GBP JPY'''.split())
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
 标准申购费 收费方式 收取 费档 优惠 折扣 持有期限 持有期 锁定期 本金保障 风险评级 份额类别 A类 C类
 份额 分类 销售服务费'''.split())
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
    supports_financial_metric_catalog = True
    metric_selection_support = True
    def __init__(self, task='general', identities=(), *, max_private_items=12):
        from .alias_text_index import AliasMap
        self._forward = AliasMap()
        self._reverse = {}
        self._nonprose_identifiers = set()
        self._identities = set()
        self._private_identifiers = set()
        self._public_terms = set(_PUBLIC_TERMS)
        self._public_ids = set()
        self._catalog_terms = set()
        self._catalog_ids = set()
        self._public_identity_rows = []
        self._sources = {}
        # Transport-only, one-turn catalogs. Original Base/MetricRegistry
        # contracts never contain these references.
        self._metric_source_sets = {}
        self._metric_source_index = {}
        self._metric_source_bindings = {}
        # Eligibility comes only from successfully delivered top-level records,
        # never from arbitrary aliases allocated while building a schema.
        self._metric_selection_aliases = set()
        self._delivered_metric_choices = {}
        self._statistic_selection_aliases = set()
        self._metric_selection_mode = False
        self._reply_parts_mode = False
        self._reply_parts_reference_field = None
        self._private_parts_turn_nonce = secrets.token_hex(32)
        self._private_parts_repair = None
        self._private_parts_repair_used = False
        self._private_parts_repair_schema_active = False
        self._private_parts_repair_receipts = []
        self._public_quote_fact_aliases = set()
        self._market_quote_fact_aliases = set()
        self._public_quote_fact_texts = {}
        self._public_quote_model_answer = None
        self._public_quote_restored_answer = None
        self._public_quote_repair = None
        self._public_quote_repair_used = False
        self._public_quote_repair_applying = False
        self._public_quote_repair_schema_active = False
        self._public_quote_joint_source_reference = None
        self._public_fee_delivery_searches = {}
        self._explicit_public_as_of = None
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

    def register_current_account_reference(self,reference,account_name):
        """A trusted current-row reference link, never a record-read authority."""
        if (reference not in {'日常账户','备用账户','工资账户','储蓄账户'}
                or not isinstance(account_name,str)or not 1<=len(account_name)<=80
                or reference not in account_name or reference in self._public_ids):return None
        if reference==account_name:return reference
        if _HIDDEN in self._text(account_name,maximum=80,raw_guard=False):return None
        alias=self._alias(account_name,'label')
        if self._forward.get(reference)not in(None,alias):return None
        self._forward[reference]=alias
        return alias

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

    def register_public_identities(self, catalog):
        """Trusted host only; map existing catalog identities, never authorize new IDs."""
        self._public_identity_rows = []
        if catalog is None:
            return
        if (type(catalog) is not dict or catalog.get('schema') != 'navren.public-identity-catalog/1'
                or type(catalog.get('entities')) is not list or len(catalog['entities']) > 64):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        candidates = []
        for row in catalog['entities']:
            if type(row) is not dict:
                continue
            kind, ref, label, parent = (row.get(k) for k in
                ('kind', 'public_ref', 'label', 'parent_product_ref'))
            if kind not in ('product', 'share_class', 'channel'):
                continue
            if (not isinstance(ref, str) or ref not in self._catalog_ids
                    or ref in self._private_identifiers
                    or not isinstance(label, str) or label not in self._catalog_terms
                    or label in self._identities
                    or self._text(ref, maximum=300, raw_guard=False) != ref
                    or self._text(label, maximum=160, raw_guard=False) != label):
                continue
            if (kind == 'share_class' and (not isinstance(parent, str)
                    or parent not in self._catalog_ids or parent in self._private_identifiers
                    or self._text(parent, maximum=300, raw_guard=False) != parent)
                    or kind != 'share_class' and parent is not None):
                continue
            item = {'label': label, 'kind': kind, 'public_ref': ref, 'parent_product_ref': parent}
            if item not in candidates:
                candidates.append(item)
        parents = {row['public_ref'] for row in candidates if row['kind'] == 'product'}
        self._public_identity_rows = [row for row in candidates
            if row['kind'] != 'share_class' or row['parent_product_ref'] in parents]

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
        hints = {'public_terms': clean(self._catalog_terms, 160),
                'public_identifiers': clean(self._catalog_ids, 300),
                'query_examples': ['费用', '产品赎回条件', '风险提示原文'],
                'facets': sorted(_PUBLIC_FACETS),
                'instruction': '使用已登记公开名称或标识，以及费用、赎回、风险等公开概念；'
                               '不发送整句私人问题、个人金额、持有日数、费率数字或身份。'
                               '简称及A/C只是查询拼写，产品、份额和渠道必须分别由原文核验。'
                               '身份目录只提供准入图的名称、代码及父子关系；代码标签不代表A/C类别。'
                               '先用knowledge_search按目标公开名称和general核验身份，再用原文身份查fees；'
                               '不遍历无关产品。基金费用资料用knowledge_search；public_products仅用于银行渠道披露，'
                               '目录未命中不能据此否定基金原文存在，身份目录也不是费率、在售或执行证据。'}
        if self._explicit_public_as_of is not None:
            hints['as_of']=self._explicit_public_as_of
            hints['instruction']+='本轮已明确公开条款核对日；knowledge_search必须传这里的as_of，缺失或换成其他日期会在读取前拒绝。'
        if self._public_identity_rows:
            hints['public_identity_discovery'] = copy.deepcopy(self._public_identity_rows)
        return hints

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
            # Opaque aliases must not merge a trailing digit with an adjacent
            # user amount. Keep the same 16-hex schema and reversible mapping.
            alias = kind + '_' + secrets.token_hex(8)[:-1] + secrets.choice('abcdef') + suffix
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
            r'(?i)(?<![A-Za-z0-9_])(?:sk|rk|ak)-[a-z0-9_-]{10,}(?![A-Za-z0-9_])',
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
                        r'(?:转给|给)\s*([\u4e00-\u9fff]{2,4}?)(?=转账|汇款|转\d|\s*\d)'):
            for match in re.finditer(pattern, value):
                self.seed_identities([match.group(1)])
        value = self._forward.replace_text(value,self._public_ids,self._nonprose_identifiers,normalize=True)
        # Unknown opaque business identifiers are not implicit authority to read
        # another person's record. A local re-query can establish an owner alias.
        value = _RAW_BUSINESS_ID.sub(
            lambda match: match.group(0) if match.group(0) in self._public_ids else self._remove(match), value)
        return value

    def _plain_reply_text(self, text):
        """Keep safe reply glyphs after the existing normalized privacy scan.

        Amount/ratio-bearing envelopes keep their existing NFKC/offset path.
        The exact alias projection must normalize to the scanned value; raw
        credentials, unknown IDs and differently spelled identities cannot
        bypass the boundary by asking to retain their original glyphs.
        """
        clean = self._text(text)
        safe = text
        safe = self._forward.replace_text(safe,self._public_ids,self._nonprose_identifiers,normalize=False)
        for alias in _ALIAS.findall(clean):
            if alias not in self._reverse or alias not in safe:
                raise BoundaryError('BOUNDARY_UNKNOWN_ALIAS')
        if unicodedata.normalize('NFKC', safe) != clean:
            raise BoundaryError('BOUNDARY_OUTPUT')
        return safe

    def _separate_alias_amounts(self,text):
        # Only the outgoing question/history gets a lexical separator. Raw
        # requests and signed financial bodies remain unchanged locally.
        return re.sub(r'(?:obj|person|label)_[a-f0-9]{16}(?:-main)?(?=[0-9])',
            lambda match:match.group(0)+' 'if match.group(0)in self._reverse else match.group(0),text)

    def user_text(self, text):
        projected = self._separate_alias_amounts(self._text(text))
        self.source_ref(text)
        # Bind only an explicit ISO day modifying a public reference property.
        # This constrains a read; it grants no identity, entitlement or write.
        dates=set(re.findall(r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)'
            r'(?:当日|当天|时|的)?(?:有效)?(?:的)?'
            r'(?:条款|政策|费率|费用|在售状态|公开资料)', projected))
        self._explicit_public_as_of = None
        if len(dates)==1:
            day=next(iter(dates))
            try:date.fromisoformat(day)
            except ValueError:pass
            else:self._explicit_public_as_of=day
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
                content = self._separate_alias_amounts(self._text(message.get('content', '')))
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
            if (domain == 'general' and key == 'references'
                    and value.get('kind') == 'public_quote_joint_source_rendering'):
                registered = self._public_quote_joint_source_reference
                if registered is None or value != registered['reference']:
                    raise BoundaryError('BOUNDARY_INVALID_INPUT')
                self._validate_public_quote_joint_source_reference(value)
                result = copy.deepcopy(dict(value))
                for leaf in result['references']:
                    leaf['evidence_id'] = self._alias(leaf['evidence_id'])
                return result
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
            # The current registered subscription needs its service label and
            # merchant state for object selection and independent review. Keep
            # them confined to actual owned subscription rows; labels still
            # pass through the ordinary reversible name projection below.
            subscription_row = (domain in ('business', 'operations') and not public
                and key in ('subscriptions', 'subscription')
                and all(isinstance(value.get(name), str) and value[name]
                        for name in ('id', 'account_id', 'mandate_id', 'plan_name')))
            if subscription_row:
                allowed = allowed | {'plan_name', 'merchant_status'}
            for field, item in value.items():
                if field == 'merchant_status' and subscription_row and item not in (
                        'active', 'cancelled', 'closed', 'inactive', 'expired'):
                    self._counts['omitted'] += 1
                    continue
                if field == 'last_four':
                    # Only a masked identity from an authorized card record is
                    # evidence. Never truncate a PAN or coerce a number here.
                    if (domain in ('business', 'operations') and not public
                            and isinstance(value.get('id'), str) and value['id']
                            and isinstance(value.get('account_id'), str) and value['account_id']
                            and value.get('type') in ('debit', 'credit', 'virtual')
                            and isinstance(item, str) and re.fullmatch(r'[0-9]{4}', item)):
                        result[field] = item
                    else:
                        self._counts['omitted'] += 1
                    continue
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
            if key == 'policy_version':
                # This exact host policy marker is a schema literal. Its
                # projection must not depend on whether another field already
                # registered the same spelling as a turn alias.
                from simulated_bank.agent_guard import POLICY_VERSION as AGENT_POLICY_VERSION
                if value == AGENT_POLICY_VERSION:
                    return value
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

    @staticmethod
    def terminal_draft_ack():
        # Receipt only: no model prose, signatures, approval or execution claim.
        return {'terminal_draft': {'status': 'received_pending_validation',
            'approved': False, 'execution_authority': False}}

    @staticmethod
    def knowledge_batch_deferred():
        # Fixed host receipt only: no query, alias, source, fact or evidence input.
        return {'tool':'knowledge_search','status':'deferred','code':'KNOWLEDGE_BATCH_DEFERRED',
            'executed':False,'evidence_created':False,
            'instruction':'本批次已启动一次知识检索；本请求尚未执行、没有读取结果。若完整回答仍需要该对象，请在下一轮重新调用合法检索；不得声称已查阅。'}

    @staticmethod
    def result_reference_feedback(kind, required_count, covered_count):
        """Fixed diagnostic state only; no values, identifiers or metric choices."""
        if (kind not in ('budget', 'balance', 'statement_budget_comparison') or type(required_count) is not int
                or type(covered_count) is not int or not 0 <= covered_count < required_count):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        instruction=('本轮比较须分别引用所问月份的账单收支结余、对应账户当前现金余额、当前可安排资金。'
            '三种口径分开，不把结余或持仓本金替代现金或预算。'
            if kind=='statement_budget_comparison'else '')
        return {'contract_error':'READ_RESULT_REQUIRED','repair':{
            'contract':'navren.read-result-reference-repair/1','field':'reply_parts',
            'query_kind':kind,'required_result_count':required_count,
            'covered_result_count':covered_count,
            'metric_authority':'current_turn_delivered_metrics_only'},
            'instruction':instruction+'本轮所问来源已读取，但终稿尚未引用全部所问结果。'
                '只修正完整reply_parts：逐一使用本轮已交付、对应所问账户及币种的结果metric_id片，'
                '不能只引用部分账户，也不能在text中填写金额或猜测指标。'
                '由你选择并提交引用，宿主不补写答案；全部原金额、来源、权限和GLM审核仍须通过。'
                '不得重读或执行金融操作；剩余时间、tokens及轮次上限不变。'}

    def metric_parts_feedback(self, code, parts):
        """Local diagnostic paths only; never quote values or choose a metric."""
        from .metric_claims import MetricClaimError, monetary_occurrences
        if (code not in ('METRIC_AMOUNT_UNBOUND', 'INVALID_MONEY_TEXT', 'METRIC_MONEY_SYNTAX_REQUIRED')
                or not isinstance(parts, list) or not 1 <= len(parts) <= 64):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        indices = []
        for index, part in enumerate(parts):
            if not isinstance(part, Mapping) or set(part) not in ({'text'}, {'metric_id'}, {'public_quote_fact_id'}, {'statistic_id'}):
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            if 'text' not in part:
                value=next(iter(part.values()))
                if not isinstance(value,str) or not 1<=len(value)<=256:
                    raise BoundaryError('BOUNDARY_INVALID_INPUT')
                continue
            text = part['text']
            if not isinstance(text, str) or len(text) > 6000:
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            try:
                detected = bool(monetary_occurrences(unicodedata.normalize('NFKC', text)))
            except MetricClaimError as error:
                if error.code not in ('INVALID_MONEY_TEXT', 'METRIC_MONEY_SYNTAX_REQUIRED'):
                    raise BoundaryError('BOUNDARY_INVALID_INPUT') from None
                detected = True
            if detected:
                indices.append(index)
        if self._reply_parts_reference_field=='public_quote_fact_id':
            return {'contract_error':code,'repair':{'contract':'navren.metric-parts-repair/1',
                'field':'reply_parts','text_part_indices':indices,'index_base':0,
                'replacement_field':'public_quote_fact_id','metric_authority':'current_turn_delivered_public_quote_facts_only'},
                'instruction':'所列reply_parts索引的text含未绑定金额表达。修改这些text字段：'
                    '公开金额及阈值改用对应本轮public_quote_fact_id片，宿主按原fact.text全文生成引号及同源claim；'
                    '不要抄写、截取或压平原文，不能只追加quote片而保留裸金额。'
                    'text仅说明条件和缺项，不猜定义、资格或渠道；不要省略所问必需信息。'
                    '索引不是金额判定或权限，已满足原完整公共引述合同的引文不因此失效。'
                    '仅修正终稿，原金额/来源/GLM校验仍保留，不重读或执行业务。'}
        return {'contract_error': code, 'repair': {
            'contract': 'navren.metric-parts-repair/1', 'field': 'reply_parts',
            'text_part_indices': indices, 'index_base': 0,
            'replacement_field': 'metric_id',
            'metric_authority': 'current_turn_delivered_metrics_only'},
            'instruction': '所列reply_parts索引的text含金额表达。每一个列出的索引都必须逐一完整处理，不能只改其中一项。'
                '个人金额写成0、零或去掉货币单位仍是金额，不能保留在这些text字段：'
                '每次所问个人金额用对应的本轮已交付metric_id独立片替换，'
                '不能只在裸金额说明旁追加metric_id片而保留原金额。'
                'text仅保留说明、期间和标点；不口算、不猜指标、不省略用户所问必需金额。'
                '索引仅定位需修字段，不提供金额或指标绑定；由你提交完整修正终稿，'
                '全部原校验和独立审核仍须通过，不重读或执行业务。'}

    @staticmethod
    def _private_parts_digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()

    def private_parts_repair_pending(self):
        return (self._private_parts_repair is not None
                and self._private_parts_repair['status'] == 'pending')

    def private_parts_repair_receipts(self):
        return copy.deepcopy(self._private_parts_repair_receipts)

    def private_parts_repair_feedback(self, code, model_args, restored_args, *, deadline,
                                      evidence_ids=None, draft_ids=None):
        from .metric_claims import MetricClaimError, monetary_occurrences
        # Only this existing private metric-parts contract can issue a patch.
        if (not self._metric_selection_mode or not self._reply_parts_mode
                or self._reply_parts_reference_field != 'metric_id'
                or self._private_parts_repair is not None or self._private_parts_repair_used
                or self._public_quote_repair is not None
                or not isinstance(model_args, Mapping) or not isinstance(restored_args, Mapping)
                or not isinstance(deadline, (int, float)) or isinstance(deadline, bool)
                or not math.isfinite(deadline) or time.monotonic() >= deadline):
            raise BoundaryError('BOUNDARY_OUTPUT')
        verified = self._restore_reply_parts(model_args)
        if verified != restored_args:
            raise BoundaryError('BOUNDARY_OUTPUT')
        # Validate every visible envelope slot before capturing the model draft.
        # check_output's normalized return is deliberately NOT the captured draft.
        self.check_output(verified, evidence_ids=evidence_ids, draft_ids=draft_ids)
        feedback = self.metric_parts_feedback(code, verified['reply_parts'])
        indices = feedback['repair']['text_part_indices']
        if not indices:
            raise BoundaryError('BOUNDARY_OUTPUT')
        blocked = copy.deepcopy(dict(model_args))
        original_digest = self._private_parts_digest(blocked)
        aliases = sorted(self._metric_selection_aliases)
        base = self._private_parts_digest({'turn': self._private_parts_turn_nonce,
            'draft': original_digest, 'indices': indices, 'delivered': aliases})
        self._private_parts_repair = {'status': 'pending', 'blocked': blocked,
            'indices': indices, 'base_sha256': base, 'deadline': deadline,
            'aliases': aliases, 'evidence_ids': None if evidence_ids is None else list(evidence_ids),
            'draft_ids': None if draft_ids is None else list(draft_ids),
            'original_draft_sha256': original_digest}
        # Offsets refer to the unchanged model paragraph, not a normalized copy.
        # Only canonical currency occurrences may be mechanically spliced.
        occurrences={}
        for index in indices:
            try:occurrences[index]=monetary_occurrences(blocked['reply_parts'][index]['text'])
            except MetricClaimError:occurrences[index]=[]
        self._private_parts_repair['occurrences']=occurrences
        choices={alias:copy.deepcopy(self._delivered_metric_choices[alias])
            for alias in aliases if alias in self._delivered_metric_choices}
        return {'contract_error': code, 'repair': {
            'contract': 'navren.private-metric-parts-bound-repair/3',
            'field': 'reply_parts', 'text_part_indices': list(indices), 'index_base': 0,
            'paths':['reply_parts['+str(i)+']'for i in indices],
            'metric_counts':{'reply_parts['+str(i)+']':len(occurrences[i])for i in indices},
            'metric_choices':list(choices.values()),
            'original_occurrences':{'reply_parts['+str(i)+']':[
                {'currency':span['currency'],'value_minor':span['value_minor'],
                 'same_value_candidates':[alias for alias,choice in choices.items()
                     if choice['currency']==span['currency'] and choice['value_minor']==span['value_minor']]}
                for span in occurrences[i]]for i in indices},
            'base_sha256': base, 'model_explicit_only': True},
            'instruction': '下一调用只能使用answer_reviewed的本轮补丁参数：base_sha256与replacements。'
                'replacements是对象，每个所列reply_parts路径是必填键。值只能是metric_ids数组或omit=true。'
                'metric_ids按该段金额出现顺序逐项选择，数量必须等于metric_counts；同一指标重复出现可重复选择。'
                'metric_choices只复列本轮已交付记录；逐项核对metric_name、subject_id、currency和口径。'
                'same_value_candidates仅帮助定位原文金额，不证明语义正确；原文金额有误时可选其他本轮指标纠正。'
                '只替换原段的金额片段，全部其他文字逐字保留，不会删除整段说明。'
                '所问必要金额必须明确选择本轮已交付的对应metric_id；非必要的金额段落可由你明确omit=true省略。'
                '不要数组index/parts、不要任何金额或自由text、不要claims或完整原稿。'
                '宿主只应用你选定的指标或省略，原稿其余字段原样保留，'
                '不代选指标、不计算金额、不补写结论。只允许一次补丁，原预算、全部校验及GLM审核不变；'
                '不得重读或执行金融操作。'}

    def _private_parts_repair_schema(self, original):
        repair = self._private_parts_repair
        if not self.private_parts_repair_pending():
            return None
        if repair.get('mode')=='fields':return self._private_fields_repair_schema(original)
        reference = copy.deepcopy(next(variant['properties']['metric_id']
            for variant in original['properties']['reply_parts']['items']['oneOf']
            if 'metric_id' in variant['properties']))
        reference['enum'] = [alias for alias in repair['aliases']
            if alias in self._metric_selection_aliases and alias in self._reverse]
        if not reference['enum']:
            raise BoundaryError('BOUNDARY_OUTPUT')
        def obj(properties):return {'type':'object','additionalProperties':False,
            'required':list(properties),'properties':properties}
        choices={}
        for index in repair['indices']:
            count=len(repair['occurrences'][index])
            variants=([obj({'metric_ids':{'type':'array','minItems':count,'maxItems':count,
                'items':reference}})]if count else [])
            choices['reply_parts['+str(index)+']']={'oneOf':variants+[obj({'omit':{'type':'boolean','enum':[True]}})]}
        return obj({'base_sha256':{'type':'string','enum':[repair['base_sha256']]},
            'replacements':obj(choices)})

    def _restore_private_parts_repair(self, args):
        from .metric_claims import MetricClaimError, monetary_occurrences
        if self._private_parts_repair is not None and self._private_parts_repair.get('mode')=='fields':
            return self._restore_private_fields_repair(args)
        repair = self._private_parts_repair
        if (not self._metric_selection_mode or not self._reply_parts_mode
                or self._reply_parts_reference_field != 'metric_id'
                or not self._private_parts_repair_schema_active
                or not self.private_parts_repair_pending() or self._private_parts_repair_used):
            raise BoundaryError('BOUNDARY_OUTPUT')
        self._private_parts_repair_used = True
        repair['status'] = 'failed'
        receipt = {'original_draft_sha256': repair['original_draft_sha256'],
            'base_sha256': repair['base_sha256'], 'patch_sha256': self._private_parts_digest(args),
            'indices': list(repair['indices']), 'model_explicit_only': True, 'status': 'rejected'}
        self._private_parts_repair_receipts.append(receipt)
        omitted=set()
        if isinstance(args,Mapping)and isinstance(args.get('replacements'),Mapping):
            keyed=args['replacements'];paths=['reply_parts['+str(i)+']'for i in repair['indices']]
            if set(keyed)!=set(paths):raise BoundaryError('BOUNDARY_OUTPUT')
            replacements=[]
            for index,path in zip(repair['indices'],paths):
                item=keyed[path]
                if isinstance(item,Mapping)and set(item)=={'omit'}and item['omit']is True:
                    parts=[];omitted.add(index)
                elif (isinstance(item,Mapping)and set(item)=={'metric_ids'}
                        and isinstance(item['metric_ids'],list)and 1<=len(item['metric_ids'])<=64
                        and all(isinstance(v,str)for v in item['metric_ids'])):
                    spans=repair['occurrences'][index]
                    if len(spans)!=len(item['metric_ids']):raise BoundaryError('BOUNDARY_OUTPUT')
                    original=repair['blocked']['reply_parts'][index]['text'];parts=[];cursor=0
                    for span,selected in zip(spans,item['metric_ids']):
                        if original[cursor:span['start']]:parts.append({'text':original[cursor:span['start']]})
                        parts.append({'metric_id':selected});cursor=span['end']
                    if original[cursor:]:parts.append({'text':original[cursor:]})
                else:raise BoundaryError('BOUNDARY_OUTPUT')
                replacements.append({'index':index,'parts':parts})
            args={**args,'replacements':replacements}
        if (time.monotonic() >= repair['deadline']
                or set(args) != {'base_sha256', 'replacements'}
                or args['base_sha256'] != repair['base_sha256']
                or not isinstance(args['replacements'], list)
                or len(args['replacements']) != len(repair['indices'])):
            raise BoundaryError('BOUNDARY_OUTPUT')
        updates = []; seen = set()
        for replacement in args['replacements']:
            if not isinstance(replacement, Mapping) or set(replacement) != {'index', 'parts'}:
                raise BoundaryError('BOUNDARY_OUTPUT')
            index, parts = replacement['index'], replacement['parts']
            if (type(index) is not int or index not in repair['indices'] or index in seen
                    or not isinstance(parts, list) or not (1<=len(parts)<=64 or index in omitted and parts==[])
                    or parts == [repair['blocked']['reply_parts'][index]]):
                raise BoundaryError('BOUNDARY_OUTPUT')
            for part in parts:
                if not isinstance(part, Mapping) or set(part) not in ({'text'}, {'metric_id'}):
                    raise BoundaryError('BOUNDARY_OUTPUT')
                if 'text' in part:
                    value = part['text']
                    if not isinstance(value, str) or not value.strip() or len(value) > 6000:
                        raise BoundaryError('BOUNDARY_OUTPUT')
                    try:
                        if monetary_occurrences(unicodedata.normalize('NFKC', value)):
                            raise BoundaryError('BOUNDARY_OUTPUT')
                    except MetricClaimError:
                        raise BoundaryError('BOUNDARY_OUTPUT') from None
                elif (not isinstance(part['metric_id'], str)
                        or part['metric_id'] not in repair['aliases']
                        or part['metric_id'] not in self._metric_selection_aliases
                        or part['metric_id'] not in self._reverse):
                    raise BoundaryError('BOUNDARY_OUTPUT')
            seen.add(index); updates.append((index, copy.deepcopy(parts)))
        if seen != set(repair['indices']):
            raise BoundaryError('BOUNDARY_OUTPUT')
        assembled = copy.deepcopy(repair['blocked'])
        for index, parts in sorted(updates, reverse=True):
            assembled['reply_parts'][index:index + 1] = parts
        if len(assembled['reply_parts']) > 64:
            raise BoundaryError('BOUNDARY_OUTPUT')
        unchanged = {key: value for key, value in assembled.items() if key != 'reply_parts'}
        if unchanged != {key: value for key, value in repair['blocked'].items() if key != 'reply_parts'}:
            raise BoundaryError('BOUNDARY_OUTPUT')
        restored = self._restore_reply_parts(assembled)
        self.check_output(restored, evidence_ids=repair['evidence_ids'], draft_ids=repair['draft_ids'])
        receipt.update(status='restored', rebuilt_draft_sha256=self._private_parts_digest(assembled),
            unchanged_envelope_sha256=self._private_parts_digest(unchanged),explicit_omitted_indices=sorted(omitted),
            nonmonetary_context_preserved_indices=sorted(set(repair['indices'])-omitted))
        repair['assembled_model_args']=copy.deepcopy(assembled)
        repair['status'] = 'restored'
        return restored

    def private_parts_revision_context(self):
        repair=self._private_parts_repair
        if not repair or repair.get('status')!='restored' or 'assembled_model_args'not in repair:return None
        # This exact alias-bearing model draft already passed restoration and
        # check_output. Never return local restored identifiers or bank scalars.
        return {'current_typed_draft':copy.deepcopy(repair['assembled_model_args']),
            'current_typed_draft_sha256':self._private_parts_digest(repair['assembled_model_args'])}

    def private_fields_repair_feedback(self, model_args, restored_args, *, claim_index=None,
                                       claim_indices=None, body_error=False, deadline, evidence_ids=None, draft_ids=None,
                                       reason_code='OPERATION_STATUS_EVIDENCE_REQUIRED'):
        # Locations come from a trusted OP exception or local monetary-field scan.
        if (not self._metric_selection_mode or not self._reply_parts_mode
                or self._reply_parts_reference_field!='metric_id'
                or self._private_parts_repair is not None or self._private_parts_repair_used
                or self._public_quote_repair is not None or type(body_error)is not bool
                or reason_code not in ('OPERATION_STATUS_EVIDENCE_REQUIRED','INVALID_MONEY_TEXT',
                    'METRIC_AMOUNT_UNBOUND','METRIC_MONEY_SYNTAX_REQUIRED')
                or not isinstance(model_args,Mapping) or not isinstance(restored_args,Mapping)
                or type(deadline)not in (int,float) or not math.isfinite(deadline)
                or time.monotonic()>=deadline):raise BoundaryError('BOUNDARY_OUTPUT')
        verified=self._restore_reply_parts(model_args)
        if verified!=restored_args:raise BoundaryError('BOUNDARY_OUTPUT')
        self.check_output(verified,evidence_ids=evidence_ids,draft_ids=draft_ids)
        claims=verified.get('claims')
        if claim_indices is None:claim_indices=()if claim_index is None else (claim_index,)
        elif claim_index is not None:raise BoundaryError('BOUNDARY_OUTPUT')
        if (not isinstance(claim_indices,(list,tuple))or not isinstance(claims,list)
                or any(type(i)is not int or not 0<=i<len(claims)for i in claim_indices)
                or len(set(claim_indices))!=len(claim_indices)
                or any(not isinstance(claims[i],Mapping)or set(claims[i])!={'text','evidence_ids'}for i in claim_indices)):
            raise BoundaryError('BOUNDARY_OUTPUT')
        claim_indices=sorted(claim_indices)
        if reason_code=='OPERATION_STATUS_EVIDENCE_REQUIRED':
            indices=([i for i,part in enumerate(model_args['reply_parts'])if set(part)=={'text'}]
                     if body_error else [])
        else:
            indices=self.metric_parts_feedback('METRIC_AMOUNT_UNBOUND',verified['reply_parts'])['repair']['text_part_indices']
        if not claim_indices and (not body_error or not indices):raise BoundaryError('BOUNDARY_OUTPUT')
        paths=['claims['+str(i)+']'for i in claim_indices]+['reply_parts['+str(i)+']' for i in indices]
        blocked=copy.deepcopy(dict(model_args));digest=self._private_parts_digest(blocked)
        aliases=sorted(self._metric_selection_aliases);statistics=sorted(self._statistic_selection_aliases)
        base=self._private_parts_digest({'turn':self._private_parts_turn_nonce,'draft':digest,
            'paths':paths,'delivered':aliases,'statistics':statistics})
        self._private_parts_repair={'status':'pending','mode':'fields','blocked':blocked,
            'indices':indices,'claim_indices':claim_indices,'paths':paths,'base_sha256':base,
            'deadline':deadline,'aliases':aliases,'statistics':statistics,
            'evidence_ids':None if evidence_ids is None else list(evidence_ids),
            'draft_ids':None if draft_ids is None else list(draft_ids),'original_draft_sha256':digest}
        if reason_code != 'OPERATION_STATUS_EVIDENCE_REQUIRED':
            from .metric_claims import MetricClaimError, monetary_occurrences
            occurrences={}
            for index in indices:
                try:occurrences[index]=monetary_occurrences(blocked['reply_parts'][index]['text'])
                except MetricClaimError:occurrences[index]=[]
            typed_indices=[i for i,part in enumerate(blocked['reply_parts'])if set(part)=={'metric_id'}]
            paths=['claims['+str(i)+']'for i in claim_indices]+[
                'reply_parts['+str(i)+']'for i in sorted(indices+typed_indices)]
            self._private_parts_repair.update(money_only=True,occurrences=occurrences,
                typed_indices=typed_indices,paths=paths)
            choices={alias:copy.deepcopy(self._delivered_metric_choices[alias])
                for alias in aliases if alias in self._delivered_metric_choices}
            return {'contract_error':reason_code,'repair':{
                'contract':'navren.private-money-fields-bound-repair/1','base_sha256':base,
                'paths':paths,'model_explicit_only':True,
                'claim_paths':['claims['+str(i)+']'for i in claim_indices],
                'typed_metric_paths':{'reply_parts['+str(i)+']':blocked['reply_parts'][i]['metric_id']
                    for i in typed_indices},
                'metric_counts':{'reply_parts['+str(i)+']':len(occurrences[i])for i in indices},
                'metric_choices':list(choices.values()),
                'original_occurrences':{'reply_parts['+str(i)+']':[
                    {'currency':span['currency'],'value_minor':span['value_minor'],
                     'same_value_candidates':[alias for alias,choice in choices.items()
                         if choice['currency']==span['currency']and choice['value_minor']==span['value_minor']]}
                    for span in occurrences[i]]for i in indices}},'instruction':
                '下一调用只提交base_sha256与replacements对象，每个所列路径恰好一个必填键。'
                'claims路径只提交omit=true：移除这条重复金额claim，正文所选metric_id仍按原指标生成完整金额声明。'
                'typed_metric_paths是原稿已有的指标引用，也须逐项提交metric_id明确保留或更正；'
                '查询当前可安排资金应选择对应账户的arrangeable_now，不能以同值但口径不同的baseline_investable替代。'
                'metric_counts大于零的reply_parts路径只提交metric_ids数组或omit=true；'
                '数组按原段金额出现顺序逐项选择，数量必须等于metric_counts。同一指标重复出现可重复选。'
                '宿主只替换这些金额跨度，原段其他文字逐字保留；不再重抄原文，不提交parts或text。'
                'metric_counts为零表示原段含无币种或无法安全定位的金额表达；该路径才可提交parts，'
                '逐片用非金额text和本轮metric_id重写，或显式omit=true。资金为0也不能留在text。'
                'metric_choices只复列本轮已交付指标；必须核对metric_name、subject_id、币种和口径。'
                'same_value_candidates只诊断原文金额，不保证语义正确，不能把同值指标混用。'
                '非必要复述可显式省略，但必须保留用户所问事实、对应账户与币种的必要结果和未核实边界。'
                '未指定字段原样保留；宿主不选指标、不计算金额、不补结论。仅一次补丁，'
                '不重读或执行业务，全部原金额、来源、权限、GLM和预算限制保持。'}
        return {'contract_error':reason_code,'repair':{
            'contract':'navren.private-fields-bound-repair/2','base_sha256':base,
            'paths':paths,'model_explicit_only':True,
            'current_values':{path:copy.deepcopy(blocked['claims'][int(path[7:-1])]
                if path.startswith('claims[')else blocked['reply_parts'][int(path[12:-1])])
                for path in paths}},'instruction':
            '下一调用只提交base_sha256与replacements对象；对象以每个指定path为唯一键，值是该字段的替换对象，全部键必填。不要提交数组，也不要在值内重复path。'
            'claims路径只能显式选本轮statistic_id统计原子，或omit=true省略该非必要claim；不能改成自由交易状态。'
            'reply_parts路径提交非空parts：text仅非金额说明，金额显式选择本轮metric_id，统计可选择statistic_id。'
            'current_values复列已交付的原字段。没有问题的指定段可以原样复列，但整份补丁至少改动一个字段；'
            '去掉无法证明的操作状态断言，仍须保留所问事实与未核实边界，不补写操作结果。'
            '未指定原字段全部保留。统计原子不证明任何单笔交易状态；真实操作仍需同一原操作回执。'
            '只允许一次显式补丁，不重读、不执行业务，不补写结论，全部原金额/来源/权限/GLM校验与预算不变。'}

    def _private_fields_repair_schema(self, original):
        repair=self._private_parts_repair
        def obj(properties):return {'type':'object','additionalProperties':False,
            'properties':properties,'required':list(properties)}
        metric={'type':'string','enum':[a for a in repair['aliases'] if a in self._metric_selection_aliases and a in self._reverse]}
        statistic={'type':'string','enum':[a for a in repair['statistics'] if a in self._statistic_selection_aliases and a in self._reverse]}
        part_variants=[obj({'text':{'type':'string','minLength':1,'maxLength':6000}}),obj({'metric_id':metric})]
        if statistic['enum']:part_variants.append(obj({'statistic_id':statistic}))
        replacements={}
        for path in repair['paths']:
            if path.startswith('claims['):
                choices=[obj({'omit':{'type':'boolean','enum':[True]}})]
                if statistic['enum']and not repair.get('money_only'):choices.append(obj({'statistic_id':statistic}))
                replacements[path]={'oneOf':choices}if len(choices)>1 else choices[0]
            elif repair.get('money_only'):
                index=int(path[len('reply_parts['):-1])
                if index in repair['typed_indices']:
                    replacements[path]=obj({'metric_id':metric});continue
                count=len(repair['occurrences'][index])
                choices=([obj({'metric_ids':{'type':'array','minItems':count,'maxItems':count,
                    'items':metric}})]if count else [obj({'parts':{'type':'array','minItems':1,
                        'maxItems':64,'items':{'oneOf':part_variants[:2]}}})])
                replacements[path]={'oneOf':choices+[obj({'omit':{'type':'boolean','enum':[True]}})]}
            else:
                replacements[path]=obj({'parts':{'type':'array','minItems':1,'maxItems':64,
                    'items':{'oneOf':part_variants}}})
        return obj({'base_sha256':{'type':'string','enum':[repair['base_sha256']]},
            'replacements':obj(replacements)})

    def _restore_private_fields_repair(self,args):
        repair=self._private_parts_repair
        if (not self._private_parts_repair_schema_active or not self.private_parts_repair_pending()
                or self._private_parts_repair_used):raise BoundaryError('BOUNDARY_OUTPUT')
        self._private_parts_repair_used=True;repair['status']='failed'
        receipt={'original_draft_sha256':repair['original_draft_sha256'],'base_sha256':repair['base_sha256'],
            'patch_sha256':self._private_parts_digest(args),'paths':list(repair['paths']),
            'model_explicit_only':True,'status':'rejected'}
        self._private_parts_repair_receipts.append(receipt)
        def reject(reason):
            receipt['failure_reason']=reason
            raise BoundaryError('BOUNDARY_OUTPUT')
        if repair.get('money_only')and (not isinstance(args,Mapping)
                or not isinstance(args.get('replacements'),Mapping)):
            reject('private_fields_contract_1264')
        # The wire object names every trusted target once. This structural
        # normalization never chooses facts, text, or omitted claims for a model.
        if isinstance(args,Mapping)and isinstance(args.get('replacements'),Mapping):
            keyed=args['replacements']
            if (set(keyed)!=set(repair['paths'])
                    or any(not isinstance(value,Mapping)or 'path'in value for value in keyed.values())):
                reject('private_fields_contract_1271')
            args={**args,'replacements':[{'path':path,**keyed[path]}for path in repair['paths']]}
        if (not isinstance(args,Mapping) or set(args)!={'base_sha256','replacements'}
                or args['base_sha256']!=repair['base_sha256'] or time.monotonic()>=repair['deadline']
                or not isinstance(args['replacements'],list)
                or len(args['replacements'])!=len(repair['paths'])):reject('repair_envelope_or_deadline')
        assembled=copy.deepcopy(repair['blocked']);seen=set();body=[];claim_updates=[]
        for replacement in args['replacements']:
            if not isinstance(replacement,Mapping):reject('private_fields_contract_1279')
            path=replacement.get('path')
            if not isinstance(path,str) or path not in repair['paths'] or path in seen:reject('private_fields_contract_1281')
            seen.add(path)
            if path.startswith('claims['):
                if set(replacement)=={'path','omit'} and replacement['omit'] is True:claim=[]
                elif (set(replacement)=={'path','statistic_id'}
                        and not repair.get('money_only')
                        and replacement['statistic_id'] in repair['statistics']
                        and replacement['statistic_id'] in self._statistic_selection_aliases
                        and replacement['statistic_id'] in self._reverse):
                    claim=[{'statistic_id':replacement['statistic_id']}]
                else:reject('private_fields_contract_1291')
                claim_updates.append((int(path[len('claims['):-1]),claim))
            else:
                index=int(path[len('reply_parts['):-1])
                if repair.get('money_only'):
                    from .metric_claims import MetricClaimError, monetary_occurrences
                    spans=repair['occurrences'].get(index,[])
                    if index in repair['typed_indices']:
                        mid=replacement.get('metric_id')
                        if (set(replacement)!={'path','metric_id'}or not isinstance(mid,str)
                                or mid not in repair['aliases']or mid not in self._metric_selection_aliases
                                or mid not in self._reverse):reject('private_fields_contract_1302')
                        body.append((index,[{'metric_id':mid}]));continue
                    if set(replacement)=={'path','omit'}and replacement['omit']is True:
                        body.append((index,[]));continue
                    if spans:
                        selected=replacement.get('metric_ids')
                        if (set(replacement)!={'path','metric_ids'}or not isinstance(selected,list)
                                or len(selected)!=len(spans)
                                or any(not isinstance(mid,str)or mid not in repair['aliases']
                                    or mid not in self._metric_selection_aliases or mid not in self._reverse
                                    for mid in selected)):reject('private_fields_contract_1312')
                        original=repair['blocked']['reply_parts'][index]['text'];parts=[];cursor=0
                        for span,mid in zip(spans,selected):
                            if original[cursor:span['start']]:parts.append({'text':original[cursor:span['start']]})
                            parts.append({'metric_id':mid});cursor=span['end']
                        if original[cursor:]:parts.append({'text':original[cursor:]})
                        replacement={'path':path,'parts':parts}
                if set(replacement)!={'path','parts'}:reject('private_fields_contract_1319')
                parts=replacement['parts']
                if not isinstance(parts,list) or not 1<=len(parts)<=64:reject('invalid_replacement_parts')
                if self.metric_parts_feedback('METRIC_AMOUNT_UNBOUND',parts)['repair']['text_part_indices']:
                    reject('private_fields_contract_1323')
                for part in parts:
                    if set(part)=={'metric_id'}:
                        if part['metric_id'] not in repair['aliases'] or part['metric_id'] not in self._metric_selection_aliases:reject('private_fields_contract_1326')
                    elif set(part)=={'statistic_id'}:
                        if repair.get('money_only'):reject('private_fields_contract_1328')
                        if part['statistic_id'] not in repair['statistics'] or part['statistic_id'] not in self._statistic_selection_aliases:reject('private_fields_contract_1329')
                    elif set(part)!={'text'} or not part['text'].strip():reject('private_fields_contract_1330')
                body.append((index,copy.deepcopy(parts)))
        if seen!=set(repair['paths']):reject('private_fields_contract_1332')
        for index,parts in sorted(body,reverse=True):assembled['reply_parts'][index:index+1]=parts
        for index,claim in sorted(claim_updates,reverse=True):assembled['claims'][index:index+1]=claim
        if assembled==repair['blocked']:reject('unchanged_complete_patch')
        if len(assembled['reply_parts'])>64:reject('private_fields_contract_1336')
        untouched={key:value for key,value in assembled.items()if key not in ('claims','reply_parts')}
        if untouched!={key:value for key,value in repair['blocked'].items()if key not in ('claims','reply_parts')}:reject('private_fields_contract_1338')
        restored=self._restore_reply_parts(assembled)
        self.check_output(restored,evidence_ids=repair['evidence_ids'],draft_ids=repair['draft_ids'])
        receipt.update(status='restored',rebuilt_draft_sha256=self._private_parts_digest(assembled),
            unchanged_envelope_sha256=self._private_parts_digest(untouched))
        repair['assembled_model_args']=copy.deepcopy(assembled)
        repair['status']='restored'
        return restored

    def server_context(self, context):
        if not isinstance(context, Mapping):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        basic = {'interface_mode', 'environment', 'attachment_status', 'as_of', 'clock_mode'}
        contextual = {'recent_operations': 'operations', 'calendar_events': 'calendar',
                      'calendar_draft_receipts': 'calendar', 'recent_calendar_widgets': 'calendar',
                      'graph_context': 'profile', 'market_context': 'public', 'page_context': 'profile'}
        output = {}
        for key, value in context.items():
            if key == 'request_mode':
                if value in ('reference_overview','reference_question'):
                    output[key]=value
            elif key == 'page_context' and isinstance(value,Mapping):
                public=((value.get('kind') in ('event','article','weekly') and value.get('source_scope')=='market')
                    or (value.get('kind')=='product' and value.get('source_scope') in ('public_catalog','public_nav')))
                if public and 'public' in _TASK_DOMAINS[self.task]:
                    output[key]=self._public_page_context(value)
                elif 'profile' in _TASK_DOMAINS[self.task]:
                    output[key]=self._project(value,'profile')
            elif key == 'market_context' and isinstance(value,Mapping) and value.get('reading_mode')=='historical_audit':
                if 'public' in _TASK_DOMAINS[self.task]:output[key]=self._public_history_page_context(value)
            elif key == 'transfer_history_read' and 'financial_mcp' in _TASK_DOMAINS[self.task]:
                output[key]=self.tool_result('financial_list_transfers',value)
            elif key == 'transfer_query_notice' and isinstance(value,str):
                output[key]=self._text(value,maximum=500,raw_guard=False)
            elif key in basic:
                output[key] = self._text(value, maximum=100, raw_guard=False) if isinstance(value, str) else self._project(value, 'general')
            elif key in contextual and contextual[key] in _TASK_DOMAINS[self.task]:
                output[key] = self._project(value, contextual[key], public=contextual[key] == 'public')
            elif key in _CONTRACT_FIELDS or key == 'error':
                output[key] = self._project(value, 'general', key=key)
            else:
                self._counts['omitted'] += 1
        return output

    @staticmethod
    def _history_parameters(value):
        # Only the typed, already server-validated market archive tuple is
        # eligible here; no generic private/public allowlist is broadened.
        if not isinstance(value,Mapping):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        day=value.get('edition_date')
        if not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        try:date.fromisoformat(day)
        except ValueError:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE') from None
        if any(type(value.get(k)) is not int or not 1<=value[k]<=999999999 for k in ('revision','event_version')):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        return {k:value[k] for k in ('edition_date','revision','event_version')}

    def _public_page_context(self, value):
        if isinstance(value,Mapping) and value.get('parameters')=={'reading_mode':'historical_audit'}:
            return self._public_history_page_context(value)
        fields=('kind','id','source_scope','version','label')
        result=self._project({k:value[k] for k in fields if k in value},'public',public=True)
        if value.get('execution_authority') is not False or not result.get('id') or result.get('version') is None:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result['execution_authority']=False
        parameters=value.get('parameters',{})
        if parameters:
            if value.get('kind')!='event' or set(parameters)!= {'edition_date','revision','event_version'}:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result['parameters']=self._history_parameters(parameters)
            if str(result['version'])!=str(result['parameters']['event_version']):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        else:result['parameters']={}
        if value.get('request_mode') in ('reference_overview','reference_question'):
            result['request_mode']=value['request_mode']
        return result

    def _public_market_data(self, value):
        result=self._project(value,'public',public=True)
        if not isinstance(value,Mapping) or 'archive' not in value:
            return result
        archive=value['archive']
        projected=self._history_parameters(archive)
        if str(value.get('version'))!=str(projected['event_version']):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        for key in ('origin','selected_at'):
            if isinstance(archive.get(key),str):
                projected[key]=self._text(archive[key],maximum=100,raw_guard=False)
        if type(archive.get('position')) is int and 1<=archive['position']<=6:
            projected['position']=archive['position']
        if isinstance(archive.get('primary_article_id'),str):
            identifier=self._project(archive['primary_article_id'],'public',key='article_id',public=True)
            if identifier is not _DROP:projected['primary_article_id']=identifier
        if 'article_versions' in archive:
            versions=archive['article_versions']
            if not isinstance(versions,Mapping) or len(versions)>48:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            projected['article_versions']={}
            for identifier,version in versions.items():
                clean=self._project(identifier,'public',key='article_id',public=True)
                if clean is _DROP or type(version) is not int or not 1<=version<=999999999:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                projected['article_versions'][clean]=version
        result['archive']=projected
        return result

    def _financial_transfer_data(self, value):
        # This tool alone carries event counts and paired account identities.
        # Its client has already verified every row's subject before dropping it.
        if not isinstance(value,Mapping) or not isinstance(value.get('data'),Mapping):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        data=value['data']
        fields={'items','transfer_count','ledger_row_count','totals_by_currency_and_direction',
            'actual_coverage_start','actual_coverage_end','offset','limit','has_more','query_complete',
            'item_page_complete','missing_fields','coverage_partial','runtime_ledger_included'}
        row_fields={'transfer_id','transaction_ids','booked_on','occurred_at','kind','currency',
            'amount_minor','signed_minor','direction','source_account_id','target_account_id','counterparty','pair_status'}
        if set(data)!=fields or not isinstance(data['items'],list) or len(data['items'])>12:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result=self._project(value,'financial_mcp')
        projected={k:self._project(data[k],'financial_mcp',key=k) for k in fields
                   if k not in ('items','totals_by_currency_and_direction')}
        projected['items']=[]
        for row in data['items']:
            if not isinstance(row,Mapping) or set(row)!=row_fields:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            projected['items'].append({k:self._project(v,'financial_mcp',key=k) for k,v in row.items()})
        totals=data['totals_by_currency_and_direction']
        if not isinstance(totals,list) or len(totals)>96:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        projected['totals_by_currency_and_direction']=[]
        for row in totals:
            if not isinstance(row,Mapping) or set(row)!={'currency','direction','transfer_count','amount_minor'}:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            projected['totals_by_currency_and_direction'].append({k:self._project(v,'financial_mcp',key=k) for k,v in row.items()})
        result['data']=projected
        return result

    def _generated_code_data(self, data):
        fixed={'schema':'navren.generated-code-result/1','kind':'registered_metric_reproduction',
            'operation':'identity_minor/1','independent_review_required':True,
            'financial_execution_authorized':False,'new_financial_metric_registered':False,
            'formal_fresh_and_publication_guard_required':True}
        identifiers={'metric_id','execution_id'}
        digests={'code_sha256','binding_sha256','receipt_sha256'}
        if (not isinstance(data,dict) or set(data)!=set(fixed)|identifiers|digests
                or any(type(data[k]) is not type(v) or data[k]!=v for k,v in fixed.items())
                or any(not isinstance(data[k],str) or not 1<=len(data[k])<=128 for k in identifiers)
                or any(not isinstance(data[k],str) or not re.fullmatch('[0-9a-f]{64}',data[k]) for k in digests)):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        return {**fixed,**{k:self._alias(data[k]) for k in identifiers},
                **{k:data[k] for k in digests}}

    def _personal_graph_data(self, data):
        """Graph UI summaries are not signed FinancialMetrics records.

        Only this tool's top-level data.metrics has the legacy summary schema.
        Formal output.metrics and all nested/general metrics keep their original
        strict signed-metric projection. Unknown summary fields reject whole rows;
        no output truncation or registration as claimable monetary metrics.
        """
        if not isinstance(data, Mapping):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        if 'metrics' not in data:
            return self._project(data, 'profile')
        rows = data['metrics']
        from base_runtime.money_range import MAX_SAFE_MINOR
        shapes = {
            'principal_only': {
                'currency', 'cash_minor', 'holding_principal_minor',
                'pending_redemption_minor', 'liabilities_minor', 'net_assets_minor',
                'known_principal_net_assets_minor', 'coverage', 'as_of'},
            'recorded_evidence_only': {
                'currency', 'income_minor', 'net_consumption_minor',
                'daily_surplus_minor', 'coverage', 'period'},
            'historical_simulation_only': {
                'currency', 'investment_equity_minor', 'known_investment_equity_minor',
                'as_of', 'nav_asof', 'coverage', 'equity_coverage'},
        }
        legacy = (type(rows) is list and all(type(row) is dict and
                  type(row.get('coverage')) is str and row['coverage'] in shapes
                  for row in rows))
        if not legacy:
            # Keep all originally legal formal collection forms and caps.
            # Validation on a copy never changes live alias registration order.
            formal = copy.deepcopy(self)._metric_collection(rows)
            def has_gap(value):
                if isinstance(value, Mapping):
                    marker = value.get('boundary')
                    if isinstance(marker, Mapping) and marker.get('status') in ('withheld', 'gap'):
                        return True
                    return any(has_gap(item) for item in value.values())
                return isinstance(value, list) and any(has_gap(item) for item in value)
            if has_gap(formal):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            return self._project(data, 'profile')
        if len(rows) > self.max_private_items:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        summaries = []
        for row in rows:
            if type(row) is not dict or type(row.get('coverage')) is not str:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            coverage = row['coverage']
            if coverage not in shapes or set(row) != shapes[coverage]:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            if type(row['currency']) is not str or not re.fullmatch(r'[A-Z]{3}', row['currency']):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            for field, value in row.items():
                if (field.endswith('_minor') and value is not None and
                        (type(value) is not int or abs(value) > MAX_SAFE_MINOR)):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            if coverage == 'principal_only':
                if (row['net_assets_minor'] is not None or
                        any(type(row[field]) is not int for field in (
                            'cash_minor', 'holding_principal_minor', 'pending_redemption_minor',
                            'liabilities_minor', 'known_principal_net_assets_minor'))):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                self._contract_date(row['as_of'])
            elif coverage == 'recorded_evidence_only':
                period = row['period']
                if type(period) is not dict or set(period) != {'from', 'to_exclusive'}:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                self._contract_date(period['from'])
                self._contract_date(period['to_exclusive'])
                if period['from'] >= period['to_exclusive']:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            else:
                self._contract_date(row['as_of'])
                if row['nav_asof'] is not None:
                    self._contract_date(row['nav_asof'])
                expected = 'complete' if row['investment_equity_minor'] is not None else 'partial'
                if type(row['equity_coverage']) is not str or row['equity_coverage'] != expected:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            summaries.append(copy.deepcopy(row))
        projected = self._project({key: value for key, value in data.items() if key != 'metrics'}, 'profile')
        projected['metrics'] = summaries
        return projected

    def tool_result(self, name, output):
        domain = _DOMAINS.get(name)
        if not domain or domain not in _TASK_DOMAINS[self.task]:
            return self._gap()
        if not isinstance(output, (Mapping, list)):
            return self._gap()
        # Project data before facts so private labels are already registered aliases.
        if isinstance(output, Mapping) and 'data' in output:
            projected = (self._public_history_data(output['data']) if name in ('market_article','market_weekly')
                         and isinstance(output['data'],Mapping) and output['data'].get('reading_mode')=='historical_audit' else
                         self._presentation_data(output['data']) if name in ('presentation_query','presentation_detail') else
                         self._generated_code_data(output['data']) if name == 'run_generated_code' else
                         self._skill_data(output['data']) if name == 'load_skill' else
                         self._service_data(name, output['data']) if name in _SERVICE_TOOLS else
                         self._public_product_data(output['data']) if name == 'public_products' else
                         self._public_market_data(output['data']) if name == 'market_event' else
                         self._public_knowledge_data(output['data']) if name == 'knowledge_search' else
                         self._financial_transfer_data(output['data']) if name == 'financial_list_transfers' else
                         self._personal_graph_data(output['data']) if name == 'personal_graph' else
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
                visible_fact = self._project_fact(fields, public=domain == 'public')
                # A copyable claim is transport assistance, not new public evidence.
                # Generate it only after the source-bound public projection, using
                # the full visible canonical excerpt and the same evidence alias.
                if (name in ('knowledge_search','market_hotspots','market_event','market_article','market_weekly')
                        and public_source is not None
                        and visible_fact.get('unit') == 'public_minor_currency'
                        and visible_fact.get('scope') == 'public_statement_not_account_balance'
                        and visible_fact.get('text') == public_source
                        and isinstance(visible_fact.get('evidence_id'), str)
                        and 0 < len(public_source) <= 1198):
                    visible_fact['reviewed_quote_claim'] = {
                        'text': '“' + public_source + '”',
                        'evidence_ids': [visible_fact['evidence_id']]}
                facts.append(visible_fact)
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
            if name=='account_summary' and 'statistics' in output:
                records={row.get('metric',{}).get('metric_id'):row for row in output.get('metrics',[]) if isinstance(row,Mapping)}
                statistics=[]
                for row in output['statistics']:
                    if (not isinstance(row,Mapping) or set(row)!={'statistic_id','metric_id','canonical_statement','metric_name'}
                            or row['metric_id'] not in records
                            or records[row['metric_id']].get('kind')!='recorded_fact'
                            or records[row['metric_id']]['metric'].get('metric_name')!=row['metric_name']
                            or row['metric_name'] not in ('holding_principal','pending_redemption')):
                        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    statistics.append({'statistic_id':self._alias(row['statistic_id']),
                        'metric_id':self._alias(row['metric_id']),'metric_name':row['metric_name'],
                        'canonical_statement':self._text(row['canonical_statement'],maximum=1200,raw_guard=False)})
                result['statistics']=statistics
            if projected in ({}, []) and output['data'] not in ({}, []):
                result['boundary'] = self._gap()['boundary']
            return result
        projected = (self._service_data(name, output) if name in _SERVICE_TOOLS else
                     self._public_product_data(output) if name == 'public_products' else
                     self._public_knowledge_data(output) if name == 'knowledge_search' else
                     self._project(output, domain, public=domain == 'public'))
        return projected if projected not in ({}, []) else self._gap()

    def _public_knowledge_data(self, data):
        """语义范围只在公共知识工具内投影，并按实际可见原件重新核验。"""
        if not isinstance(data, Mapping):
            return self._gap()
        if not isinstance(data.get('hits', []), list) or len(data.get('hits', [])) > 8:
            return self._gap()
        # 通用公共投影会登记收到的 ID；它不能为零命中对象提供可信身份。
        registered = frozenset(self._catalog_ids)
        private_ids = (self._private_identifiers | self._identities | set(self._reverse)
                       | {value for value in self._forward if value not in self._public_ids})
        allowed = {'schema_version', 'status', 'ready', 'scope', 'provider_id', 'public_release_id',
                   'as_of', 'market_timezone', 'hits', 'rejected_count', 'notice'}
        base = {key: value for key, value in data.items() if key in allowed}
        gaps = data.get('coverage_gaps', [])
        base['coverage_gaps'] = [gap for gap in gaps if isinstance(gap, Mapping)
                                 and (not isinstance(gap.get('code'), str)
                                      or gap['code'] not in SEMANTIC_GAP_CODES)] if isinstance(gaps, list) else []
        ordinary_gaps = base.pop('coverage_gaps')
        result = self._project(base, 'public', public=True)
        result['coverage_gaps'] = []
        visible_citations = {hit.get('citation_id') for hit in result.get('hits', [])
                             if isinstance(hit, Mapping) and isinstance(hit.get('citation_id'), str)}
        for gap in ordinary_gaps[:96]:
            code = gap.get('code')
            code = code if isinstance(code, str) and code in KNOWLEDGE_GAP_MESSAGES else 'provider_reported_gap'
            fixed = {'code': code, 'message': KNOWLEDGE_GAP_MESSAGES[code]}
            if isinstance(gap.get('facet'), str) and gap['facet'] in KNOWLEDGE_FACETS:
                fixed['facet'] = gap['facet']
            if isinstance(gap.get('citation_id'), str) and gap['citation_id'] in visible_citations:
                fixed['citation_id'] = gap['citation_id']
            result['coverage_gaps'].append(fixed)
        # 受信消费器输出中的原件哈希是公共引用标识。泛化文本脱敏会把其中
        # 连续数字误当作联系方式，故仅在此工具的成功原件合同内保留完整ID。
        if (type(data.get('schema_version')) is int and data['schema_version'] == 1 and data.get('scope') == 'public'
                and data.get('ready') is True and data.get('status') in ('partial', 'ready')):
            structural = ('citation_id', 'release_id', 'document_id', 'document_version_id',
                          'parse_id', 'span_id')
            originals = data.get('hits', [])
            for original, visible in zip(originals if isinstance(originals, list) else [], result.get('hits', [])):
                if (not isinstance(original, Mapping) or not isinstance(visible, Mapping)
                        or original.get('visibility') != 'public'
                        or original.get('outbound_allowed') is not True
                        or original.get('withdrawn', False) is not False
                        or original.get('content_status') != 'full_text'):
                    continue
                for key in structural:
                    value = original.get(key)
                    if (isinstance(value, str)
                            and re.fullmatch(r'(?:[cdvp][a-f0-9]{28,64}|pub-[a-f0-9]{40})', value)
                            and value not in private_ids):
                        visible[key] = value
            release = data.get('public_release_id')
            if (isinstance(release, str) and re.fullmatch(r'pub-[a-f0-9]{40}', release)
                    and release not in private_ids):
                result['public_release_id'] = release
        semantic = boundary_projection({**data, 'hits': result.get('hits', [])},
                                       trusted_public_product_ids=registered)
        result['coverage_gaps'] = unique_semantic_gaps(result.get('coverage_gaps', []) + semantic['coverage_gaps'])
        if semantic.get('semantic_coverage') is not None:
            result['semantic_coverage'] = semantic['semantic_coverage']
        if result.get('hits') and result['coverage_gaps']:
            result['status'] = 'partial'
        return result

    def _public_product_data(self, data):
        """Narrow round-catalog metadata, not a generic dynamic public mapping.

        Coverage IDs must have been registered by the trusted catalog before
        this output. A product field in this output cannot register its own key.
        ``catalog_info`` is normally an internal RoundProvider property; if a
        trusted caller exports it, only its real empty/two-field shape is read.
        """
        if not isinstance(data, Mapping):
            return self._project(data, 'public', public=True)
        if 'catalog_information' in data:
            from local_knowledge.catalog_information import project_information_data
            return project_information_data(dict(data))
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

    def model_tool_result(self, name, output, *, compact_metric_sources=False, compact_financial_metrics=False):
        """Optional lossless source-map transport after the normal projection."""
        result = self.tool_result(name, output)
        if compact_metric_sources and name in _FORMAL_METRIC_TOOLS and isinstance(result, Mapping):
            result=self._compact_metric_sources_atomically(lambda: self._compact_projected_metric_message(result))
        if compact_financial_metrics and name in ('presentation_query','presentation_detail'):
            from .review_evidence import pack_financial_evidence
            from .provider_evidence_transport import pack_provider_evidence
            result=pack_provider_evidence(pack_financial_evidence(result))
        self._remember_delivered_metric_aliases(name, result)
        if name=='account_summary' and isinstance(result,Mapping):
            for row in result.get('statistics',[]):
                if row.get('metric_id') in self._metric_selection_aliases:
                    self._statistic_selection_aliases.add(row['statistic_id'])
        self._remember_delivered_public_quote_facts(name, result)
        if self._metric_selection_mode:
            result = self._metric_choice_view(name, result)
        if (name=='knowledge_search' and isinstance(output,Mapping) and isinstance(result,Mapping)
                and isinstance(output.get('evidence_id'),str) and isinstance(output.get('data'),Mapping)
                and result.get('evidence_id')in self._reverse
                and self._reverse[result['evidence_id']]==output['evidence_id']):
            # One-turn host snapshot of the actual tool projection, not a new
            # monetary fact or a model-supplied source catalog.
            snapshot={'data':copy.deepcopy(output['data']),
                'visible':self._restore(result.get('data'),strict=False)}
            eid=output['evidence_id'];previous=self._public_fee_delivery_searches.get(eid)
            if previous is not None and previous!=snapshot:raise BoundaryError('BOUNDARY_INVALID_INPUT')
            self._public_fee_delivery_searches[eid]=snapshot
        return result

    def _metric_choice_view(self, name, result):
        """Model-only choices, never a FinancialMetric or review signature.

        Only top-level records are reduced, after the normal complete projection
        and codecs succeeded and the delivered-alias registry was updated.
        Nested monetary/ratio contracts and the local metric pool are untouched.
        """
        if name not in _FORMAL_METRIC_TOOLS or not isinstance(result, Mapping):
            return result
        rows = result.get('metrics')
        if not isinstance(rows, list) or not rows:
            return result
        fields = ('metric_id', 'metric_name', 'subject_id', 'currency', 'unit',
                  'value_minor', 'as_of', 'period_start', 'period_end', 'basis',
                  'calculation_status', 'data_scope', 'coverage_gaps', 'assumed_inputs')
        projected = []
        for row in rows:
            if (isinstance(row, Mapping)
                    and set(row) == {'metric_id', 'metric', 'kind', 'evidence_ids'}
                    and row['metric_id'] in self._metric_selection_aliases
                    and isinstance(row['metric'], Mapping)
                    and set(fields) <= set(row['metric'])):
                projected.append({**row, 'metric': {
                    key: copy.deepcopy(row['metric'][key]) for key in fields}})
            else:
                # Gaps and any non-choice structure retain their original shape.
                projected.append(copy.deepcopy(row))
        result = {**result, 'metrics': projected,
                  'metric_choice_contract': {
                      'contract': 'navren.metric-choice-view/1', 'full_signature': False}}
        if name == 'budget':
            result['metric_choice_contract']['semantic_roles'] = {
                'arrangeable_now': '本轮当前预算可安排结果。',
                'planning_arrangeable_now': '本轮追加规划留款后情景结果。',
                'baseline_investable': '未追加规划留款基线，不能替代前两种结果引用，即使value相同也不能互代。'}
        def has_ref(value):
            if isinstance(value, Mapping):
                return ('source_version_ref' in value
                        or any(has_ref(item) for item in value.values()))
            if isinstance(value, list):
                return any(has_ref(item) for item in value)
            return False
        if not has_ref(result):
            result.pop('source_version_sets', None)
            result.pop('metric_source_policy', None)
        return result

    def _remember_delivered_public_quote_facts(self, name, result):
        if name not in ('knowledge_search','market_hotspots','market_event','market_article','market_weekly') or not isinstance(result,Mapping):return
        for fact in result.get('facts',[]):
            if (not isinstance(fact,Mapping) or fact.get('unit')!='public_minor_currency'
                    or fact.get('scope')!='public_statement_not_account_balance'
                    or not isinstance(fact.get('id'),str) or not _ALIAS.fullmatch(fact['id'])
                    or fact['id'] not in self._reverse or not isinstance(fact.get('text'),str)):
                continue
            claim=fact.get('reviewed_quote_claim')
            if (isinstance(claim,Mapping) and set(claim)=={'text','evidence_ids'}
                    and claim['text']=='“'+fact['text']+'”'
                    and claim['evidence_ids']==[fact.get('evidence_id')]):
                previous=self._public_quote_fact_texts.get(fact['id'])
                if previous is not None and previous!=fact['text']:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                self._public_quote_fact_aliases.add(fact['id'])
                self._public_quote_fact_texts[fact['id']]=fact['text']
                if name.startswith('market_'):self._market_quote_fact_aliases.add(fact['id'])

    def _remember_delivered_metric_aliases(self, name, result):
        if name not in _FORMAL_METRIC_TOOLS or not isinstance(result, Mapping):
            return
        rows = result.get('metrics')
        if not isinstance(rows, list):
            return
        full_fields = _METRIC_FIELDS
        compact_fields = (_METRIC_FIELDS - {'source_versions'}) | {'source_version_ref'}
        eligible = set()
        for row in rows:
            if (not isinstance(row, Mapping)
                    or set(row) != {'metric_id', 'metric', 'kind', 'evidence_ids'}
                    or row['kind'] not in _METRIC_KINDS
                    or not isinstance(row['metric'], Mapping)
                    or set(row['metric']) not in (full_fields, compact_fields)
                    or row['metric_id'] != row['metric'].get('metric_id')
                    or not isinstance(row['metric_id'], str)
                    or not _ALIAS.fullmatch(row['metric_id'])
                    or row['metric_id'] not in self._reverse
                    or not isinstance(row['evidence_ids'], list) or not row['evidence_ids']):
                continue
            eligible.add(row['metric_id'])
        self._metric_selection_aliases.update(eligible)
        # These exact aliases and scalar fields already passed the outbound
        # projection above. Recall is neither a new metric nor a host choice.
        fields=('metric_id','metric_name','subject_id','currency','value_minor',
                'as_of','period_start','period_end','basis','data_scope')
        self._delivered_metric_choices.update({row['metric_id']:{key:copy.deepcopy(row['metric'][key])
            for key in fields} for row in rows if row.get('metric_id') in eligible})

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
        for key in ('skill', 'reference', 'instructions', 'instructions_sha256', 'notice', 'references', 'tools'):
            value = data.get(key)
            if key == 'instructions_sha256':
                if isinstance(value,str) and re.fullmatch(r'[0-9a-f]{64}',value):result[key]=value
            elif key == 'instructions' and isinstance(value, str):
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
            holding = _METRIC_HOLDING_REF.fullmatch(value) if identifier else None
            valid_holding = holding is not None and holding['prefix'] == holding['workspace'][:len(holding['prefix'])]
            scoped = _METRIC_SCOPED_REF.fullmatch(value) if identifier else None
            if identifier and (_METRIC_OPAQUE_REF.fullmatch(value) or valid_holding or scoped):
                # A signed holding source includes both workspace and account
                # hashes. Mask only those typed hash segments; its product ID
                # still passes the original credential/contact scan unchanged.
                checked = (('holdings/opaque/acct-opaque-'+holding['account']+'/'+holding['product'])
                    if valid_holding else
                    value[:scoped.start('statement'if scoped['statement']is not None else'commitment')]+'opaque'+
                    value[scoped.end('statement'if scoped['statement']is not None else'commitment'):]if scoped else
                    re.sub(r'(?<![A-Za-z0-9])[a-f0-9]{12,64}(?![A-Za-z0-9])', 'opaque', value))
                match=re.fullmatch(r'presentation_table/([a-z_]{1,64})/[a-f0-9]{32}',value)
                if match:
                    from base_runtime.presentation import TABLES
                    if match.group(1) in TABLES:checked='registered_presentation_table/opaque'
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
        safe = self._forward.replace_text(safe,self._public_ids,self._nonprose_identifiers,normalize=False)
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

    def _presentation_data(self, data):
        from .presentation_consumer import project_review
        if not isinstance(data,Mapping) or set(data) not in (
                {'widget_id','review_projection'}, {'widget_id','review_projection','provider_object'}):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        provider=None
        if 'provider_object' in data:
            from .provider_projection import project_provider_object
            if data['provider_object']['result']['presentation']!=data['review_projection']['presentation']:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            provider=project_provider_object(self,data['provider_object'])
        review=project_review(self,data['review_projection'])
        if data['widget_id']!=data['review_projection']['presentation']['view_id']:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        output={'widget_id':self._contract_string(data['widget_id'],identifier=True,prose=False),
                'review_projection':review}
        if provider is not None:output['provider_object']=provider
        return output

    def _ratio_signature(self, metric):
        from .ratio_claims import validate_snapshot, RatioClaimError
        try:
            # Claim signatures have 14 fields; build no monetary surrogate.
            if not _RATIO_REF_FIELDS<=set(metric):raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            if metric['unit']!='ratio' or metric['calculation_status'] not in ('calculated','unavailable'):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            if metric['value_decimal'] is not None:
                from decimal import Decimal, InvalidOperation
                try:value=Decimal(metric['value_decimal'])
                except (InvalidOperation,TypeError):raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                if not isinstance(metric['value_decimal'],str) or not value.is_finite() or metric['calculation_status']!='calculated':
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            elif metric['calculation_status']!='unavailable':raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            if not re.fullmatch(r'[A-Z]{3}',metric['currency']):raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            dates={k:metric[k] for k in ('as_of','period_start','period_end')}
            if dates['as_of'] is not None:
                self._contract_date(dates['as_of'])
                if dates['period_start'] is not None or dates['period_end'] is not None:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            else:
                self._contract_date(dates['period_start']);self._contract_date(dates['period_end'])
                if dates['period_start']>dates['period_end']:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            versions=metric['source_versions']
            if not isinstance(versions,Mapping) or not 1<=len(versions)<=128:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result={k:metric[k] for k in ('value_decimal','unit','currency','calculation_status',*dates)}
            for k in ('metric_id','subject_id','metric_name','data_scope','basis','formula_id'):
                result[k]=self._contract_string(metric[k],identifier=k in ('metric_id','subject_id','formula_id'),prose=k!='formula_id')
            result['source_versions']={self._contract_string(k,identifier=True,prose=False):
                self._contract_string(v,identifier=True,prose=False,maximum=512) for k,v in versions.items()}
            return result
        except (RatioClaimError,TypeError,ValueError) as error:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE') from error

    def _ratio_snapshot(self, metric):
        from .ratio_claims import validate_snapshot, RatioClaimError
        try:metric=validate_snapshot(metric)
        except RatioClaimError as error:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE') from error
        result=self._ratio_signature(metric)
        result['contract_version']=metric['contract_version']
        for key in ('input_refs','excluded_items','coverage_gaps'):
            if len(metric[key])>(128 if key=='input_refs' else 64):raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result[key]=[self._contract_string(v,identifier=key=='input_refs',maximum=500) for v in metric[key]]
        result['assumed_inputs']=[self._metric_assumption(v) for v in metric['assumed_inputs']]
        return result

    def _ratio_claim(self, claim):
        if (not isinstance(claim,Mapping) or set(claim)-_RATIO_REF_FIELDS-{'text','claim_kind','occurrence_start'}
                or not _RATIO_REF_FIELDS|{'text','claim_kind'}<=set(claim) or claim['claim_kind']!='recorded_fact'):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result={**self._ratio_signature(claim),'claim_kind':'recorded_fact','text':self._text(claim['text'],maximum=1200)}
        if 'occurrence_start' in claim:
            if type(claim['occurrence_start']) is not int or claim['occurrence_start']<0:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result['occurrence_start']=claim['occurrence_start']
        return result

    def _ratio_claims(self, claims, *, source_text=None, target_text=None):
        if not isinstance(claims,list) or len(claims)>24:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result = []
        for claim in claims:
            projected = self._ratio_claim(claim)
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

    def _ratio_contract(self, contract, *, source_text=None, target_text=None):
        fields={'status','ratio_claims','ratio_occurrence_count','independent_review_required'}
        if (not isinstance(contract,Mapping) or set(contract)!=fields or contract['status']!='validated'
                or not isinstance(contract['ratio_claims'],list) or len(contract['ratio_claims'])>24
                or type(contract['ratio_occurrence_count']) is not int or contract['ratio_occurrence_count']<0
                or contract['independent_review_required'] is not True):raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        records = []
        for row in contract['ratio_claims']:
            projected = self._metric_record(row, claim=True, ratio=True)
            projected['claim'] = self._ratio_claims([row['claim']], source_text=source_text,
                                                      target_text=target_text)[0]
            records.append(projected)
        return {**{k:contract[k] for k in fields if k!='ratio_claims'}, 'ratio_claims':records}

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

    def _metric_record(self, record, *, claim=False, ratio=False):
        allowed = {'metric_id', 'metric', 'kind', 'evidence_ids'} | ({'claim'} if claim else set())
        if (not isinstance(record, Mapping) or set(record) != allowed
                or not isinstance(record['kind'], str) or record['kind'] not in _METRIC_KINDS
                or not isinstance(record['metric'], Mapping)
                or record['metric_id'] != record['metric'].get('metric_id')
                or not isinstance(record['evidence_ids'], list)
                or not 1 <= len(record['evidence_ids']) <= 24):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result = {'metric_id': self._contract_string(record['metric_id'], identifier=True),
                  'metric': self._ratio_snapshot(record['metric']) if ratio else self._metric_snapshot(record['metric']), 'kind': record['kind'],
                  'evidence_ids': [self._contract_string(value, identifier=True) for value in record['evidence_ids']]}
        if claim:
            result['claim'] = self._ratio_claim(record['claim']) if ratio else self._amount_claim(record['claim'])
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
                if (not isinstance(event, Mapping) or event.get('status') != 'published'
                        or event.get('availability','active')!='active'):
                    continue
                articles = {article.get('id') for article in event.get('articles', []) if isinstance(article, Mapping)
                            and article.get('status') == 'active' and article.get('read_scope') == 'full_text'
                            and article.get('link_status') == 'verified'}
                for row in event.get('facts', []):
                    if (isinstance(row, Mapping) and row.get('article_id') == fact.get('article_id')
                            and isinstance(fact.get('article_id'), str) and fact['article_id'] in articles
                            and (fact.get('text') is None or (isinstance(fact['text'],str)
                                 and row.get('quote') == unicodedata.normalize('NFKC',fact['text'])))
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

    def _cashflow_scenario_sources(self, data, days):
        """The same typed source classification for primary and review wires."""
        from datetime import timedelta
        def fail():
            raise BoundaryError('BOUNDARY_OUTPUT')
        try:
            if (data['source'] != 'simulated_bank_archive' or type(days) is not int
                    or data['horizon_days'] != days or not isinstance(data['timeline'], list)
                    or date.fromisoformat(data['horizon_end']) != date.fromisoformat(data['as_of']) + timedelta(days=days)):
                fail()
            accounts = {a['account_id'] for a in data['accounts']}
            kinds = {'loan_installment': ('confirmed', 'loan_installments'),
                     'credit_statement': ('confirmed', 'credit_statements'),
                     'subscription_renewal': ('candidate', 'subscriptions'),
                     'bank_mandate': ('candidate', 'mandates'),
                     'scheduled_transfer': ('confirmed', 'scheduled_payments'),
                     'redemption_settlement': ('confirmed', 'orders'),
                     'recorded_forecast': ('candidate', 'recurring_obligations'),
                     'reservation_release': ('confirmed', 'fund_holds')}
            included = Counter()
            for slot in data['timeline']:
                if not isinstance(slot['events'], list):
                    fail()
                for row in slot['events']:
                    kind = row['kind']; expected = kinds.get(kind)
                    if (expected is None or (row['certainty'], row['source_table']) != expected
                            or row['account_id'] not in accounts or row['date'] != slot['date']
                            or not data['as_of'] <= row['date'] <= data['horizon_end']):
                        fail()
                    included[(row['account_id'], row['certainty'], kind, row['source_table'])] += 1
            excluded = data['not_included']
            rules = {
                'unconfirmed_operations': ('pending_confirmation', 'operations', '操作尚待用户确认，不计入现金流。'),
                'unconfirmed_scheduled_transfers': ('draft', 'scheduled_payments', '定时转账草案未启用，不作为已确认的支付承诺。'),
                'proposed_fund_reservations': ('proposed', 'fund_holds', '用户尚未确认的资金预留草案，不扣减可用余额。')}
            texts = {'future_salary_and_other_unconfirmed_income', 'unrecorded_daily_spending', 'imported_statements'}
            if not isinstance(excluded, dict) or set(excluded) != set(rules) | texts:
                fail()
            scope = {}
            for key, (status, table, reason) in rules.items():
                rows = excluded[key]
                if not isinstance(rows, list) or any(not isinstance(row, dict)
                        or row.get('counted_in_projection') is not False or row.get('status') != status
                        or row.get('source_table') != table or row.get('reason') != reason for row in rows):
                    fail()
                scope[key] = {'record_count': len(rows), 'counted_in_projection': False,
                              'metadata_scope': 'excluded_record_type_contract',
                              'status': status, 'source_table': table,
                              'reason': self._text(reason, raw_guard=False), 'details_retained_locally': True}
            for key in texts:
                if not isinstance(excluded[key], str) or not excluded[key]:
                    fail()
                scope[key] = self._text(excluded[key], raw_guard=False)
            return {'included_scenario_sources': [
                {'account_id': self._alias(aid), 'certainty': certainty, 'kind': kind,
                 'source_table': table, 'record_count': count, 'counted_in_projection': True}
                for (aid, certainty, kind, table), count in sorted(included.items())], 'not_included': scope}
        except (KeyError, TypeError, ValueError):
            fail()

    def funding_assessment_tool_result(self, name, output, days, *, compact_metric_sources=False):
        """Primary-only assessment choices; full evidence stays in BankTools.

        Roles and coverage are explicit. No holding, event amount, balance or
        unselected metric is disclosed or registered as a selectable alias.
        """
        from .read_completion import funding_assessment_metric_choices
        def fail():
            raise BoundaryError('BOUNDARY_OUTPUT')
        if (name not in ('account_summary', 'budget', 'cashflow') or not isinstance(output, dict)
                or _DOMAINS[name] not in _TASK_DOMAINS[self.task]
                or not isinstance(output.get('data'), dict) or not isinstance(output.get('evidence_id'), str)):
            fail()
        data = output['data']
        if name == 'account_summary':
            accounts = data.get('accounts')
            if (not isinstance(accounts, list) or not accounts or any(not isinstance(a, dict)
                    or not {'id', 'name', 'currency'} <= set(a) for a in accounts)):
                fail()
            visible = {k: data[k] for k in ('as_of', 'version') if k in data}
            visible['accounts'] = [{k: account[k] for k in ('id', 'name', 'currency')} for account in accounts]
            return self.model_tool_result(name, {'evidence_id': output['evidence_id'],
                'data': visible, 'facts': [], 'metrics': []}, compact_metric_sources=compact_metric_sources)
        try:
            choices = funding_assessment_metric_choices(name, data, output.get('metrics', []),
                                                        output['evidence_id'], days)
        except BankError:
            fail()
        if name == 'budget':
            if not {'account_id', 'name', 'currency', 'as_of', 'budget_scope', 'investable_minor', 'amount_basis'} <= set(data):
                fail()
            visible = {k: data[k] for k in ('account_id', 'name', 'currency', 'as_of',
                                           'budget_scope', 'investable_minor', 'amount_basis')}
        else:
            if (not {'source', 'environment', 'as_of', 'horizon_days', 'horizon_end', 'currency',
                     'available_amount_basis', 'notice'} <= set(data)
                    or any('account_name' not in a for a in data['accounts'])):
                fail()
            visible = {k: data[k] for k in ('source', 'environment', 'as_of', 'horizon_days',
                                           'horizon_end', 'currency', 'available_amount_basis', 'notice')}
            visible['accounts'] = [{k: account[k] for k in (
                'account_id', 'account_name', 'currency', 'minimum_available_after_confirmed_minor',
                'minimum_available_if_candidates_occur_minor', 'confirmed_floor_breached',
                'candidate_floor_breached')} for account in data['accounts']]
        result = self.model_tool_result(name, {'evidence_id': output['evidence_id'], 'data': visible,
            'facts': [], 'metrics': [row for _, row in choices]}, compact_metric_sources=compact_metric_sources)
        rows = result.get('metrics', [])
        if (len(rows) != len(choices) or any(not isinstance(row, dict)
                or row.get('metric_id') not in self._metric_selection_aliases for row in rows)):
            fail()
        result['metric_choices'] = [{
            'role': role, 'metric_id': projected['metric_id'],
            'subject_id': projected['metric']['subject_id'], 'evidence_ids': projected['evidence_ids'],
            'value_minor': projected['metric']['value_minor'], 'currency': projected['metric']['currency'],
            'unit': projected['metric']['unit'], 'kind': projected['kind'],
            'period_start': projected['metric']['period_start'], 'period_end': projected['metric']['period_end'],
            'basis': projected['metric']['basis']}
            for (role, _), projected in zip(choices, rows)]
        if name == 'cashflow':
            # These original host scope statements were omitted by the general
            # field allowlist. Preserve their meaning, never a label alias.
            for field in ('adequacy_basis', 'future_inflow_basis'):
                value = data.get(field)
                if not isinstance(value, str) or not value:
                    fail()
                result['data'][field] = self._text(value, raw_guard=False)
            result['data'].update(self._cashflow_scenario_sources(data, days))
        return result

    def income_window_tool_result(self, name, output, query):
        """Only the two source-bound facts of the host's bounded income lookup.

        Fixed roles make the choice interpretable without exposing source paths,
        other financial fields or opaque facts whose labels were withheld.
        Complete tool evidence and signatures remain in BankTools unchanged.
        """
        from datetime import datetime, timedelta
        def fail():
            raise BoundaryError('BOUNDARY_OUTPUT')
        if (name not in ('monthly_statement', 'cashflow') or not isinstance(query, dict)
                or set(query) != {'month', 'days'} or not isinstance(output, dict)
                or _DOMAINS[name] not in _TASK_DOMAINS[self.task]):
            fail()
        data = output.get('data')
        if not isinstance(data, dict) or not isinstance(output.get('evidence_id'), str):
            fail()
        historical = name == 'monthly_statement'
        role = 'historical_income' if historical else 'future_confirmed_inflow'
        source = 'simulation' if historical else 'simulated_bank_archive'
        path = '/totals_minor/income' if historical else '/totals/confirmed_inflow_minor'
        field = 'income' if historical else 'confirmed_inflow_minor'
        metric_name = 'income' if historical else 'confirmed_inflow'
        kind = 'recorded_fact' if historical else 'recorded_projection'
        totals = data.get('totals_minor' if historical else 'totals')
        value = totals.get(field) if isinstance(totals, dict) else None
        if data.get('source') != source or type(value) is not int:
            fail()
        matching = [f for f in output.get('facts', []) if isinstance(f, dict)
                    and f.get('evidence_id') == output['evidence_id'] and f.get('source_path') == path]
        if len(matching) != 1:
            fail()
        fact = matching[0]; currency = fact.get('currency')
        if (not isinstance(fact.get('id'), str) or not re.fullmatch(r'f[0-9a-f]{20}', fact['id'])
                or fact.get('field') != field or fact.get('unit') != 'minor_currency'
                or type(fact.get('value')) is not int or fact['value'] != value
                or not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency)):
            fail()
        try:
            if historical:
                recorded = data['recorded_range']
                if (data['month'] != query['month'] or not isinstance(recorded, list) or len(recorded) != 2
                        or any(not isinstance(v, str) or datetime.fromisoformat(v).strftime('%Y-%m') != query['month'] for v in recorded)
                        or datetime.fromisoformat(recorded[0]) > datetime.fromisoformat(recorded[1])):
                    fail()
                start, end = recorded[0][:10], recorded[1][:10]
                visible = {'source': source, 'month': query['month'], 'recorded_range': list(recorded),
                           'currency': currency, 'totals_minor': {'income': value}}
            else:
                start, end = data['as_of'], data['horizon_end']
                if (type(query['days']) is not int or not 1 <= query['days'] <= 365
                        or data['horizon_days'] != query['days'] or data.get('currency') != currency
                        or date.fromisoformat(end) != date.fromisoformat(start) + timedelta(days=query['days'])):
                    fail()
                visible = {'source': source, 'as_of': start, 'horizon_end': end, 'horizon_days': query['days'],
                           'currency': currency, 'totals': {'confirmed_inflow_minor': value}}
        except (KeyError, TypeError, ValueError):
            fail()
        # The simulation report has no currency key: consume its already issued
        # formal metric rather than assuming CNY or matching by amount alone.
        if not any(isinstance(row, dict) and row.get('kind') == kind
                and output['evidence_id'] in row.get('evidence_ids', [])
                and isinstance(row.get('metric'), dict)
                and row['metric'].get('metric_name') == metric_name
                and row['metric'].get('currency') == currency
                and type(row['metric'].get('value_minor')) is int and row['metric']['value_minor'] == value
                for row in output.get('metrics', [])):
            fail()
        fid, eid = self._alias(fact['id']), self._alias(output['evidence_id'])
        choice = {'role': role, 'fact_id': fid, 'evidence_id': eid, 'value_minor': value,
                  'unit': 'minor_currency', 'currency': currency, 'source': source,
                  'period_start': start, 'period_end': end}
        return {'evidence_id': eid, 'data': visible, 'fact_choices': [choice],
                'facts': [{'id': fid, 'evidence_id': eid, 'value': value, 'unit': 'minor_currency',
                           'currency': currency, 'text': ('历史月报已记录收入' if historical else '未来窗口已确认入账')}]}

    def native_tools(self, schemas, *, compact_metric_sources=False, metric_selections=False):
        if type(metric_selections) is not bool or not isinstance(schemas, list):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        result = copy.deepcopy(schemas)
        if self.private_parts_repair_pending():
            result = [tool for tool in result if tool.get('function', {}).get('name') == 'answer_reviewed']
            if len(result) != 1:
                raise BoundaryError('BOUNDARY_OUTPUT')
        private_parts_repair_mode = False
        def walk(schema, field=''):
            if not isinstance(schema, dict):
                return
            if 'enum' in schema and field in _IDS:
                schema['enum'] = [self._alias(v) if isinstance(v, str) and v not in self._public_ids else v for v in schema['enum']]
            for name, child in schema.get('properties', {}).items():
                walk(child, name)
            if 'items' in schema:
                walk(schema['items'], field)
        reply_parts_mode = False
        public_quote_answer_mode = False
        public_quote_repair_mode = False
        reply_parts_reference_field = None
        for tool in result:
            function = tool.get('function', {})
            if function.get('name') not in _DOMAINS and function.get('name') not in ('answer_facts', 'answer_reviewed'):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            walk(function.get('parameters', {}))
            if function.get('name') in ('knowledge_search', 'public_products'):
                parameters=function.get('parameters',{})
                properties=parameters.get('properties',{})
                for field,kind in (('product_id','product'),('share_class_id','share_class'),('channel_id','channel')):
                    if field not in properties:continue
                    properties[field]=copy.deepcopy(properties[field])
                    identifier=properties[field]
                    if not isinstance(identifier,dict) or identifier.get('type')!='string':
                        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    minimum,maximum=identifier.get('minLength',0),identifier.get('maxLength',300)
                    if type(minimum)is not int or type(maximum)is not int or not 0<=minimum<=maximum:
                        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    choices=[]
                    for row in self._public_identity_rows:
                        ref=row['public_ref']
                        if row['kind']!=kind or not isinstance(ref,str) or not minimum<=len(ref)<=maximum:
                            continue
                        try:
                            accepted=self.public_request({field:ref},tool_name=function['name'])
                        except BoundaryError:
                            continue
                        if accepted=={field:ref}:choices.append(ref)
                    if choices:
                        identifier['enum']=sorted(set(choices))
                        identifier['description']='单选本轮已核验的对应类型公开标识；名称或代码标签不等于标识。目标身份尚未核实，先用安全query查证，不猜测或拼接标识。'
                    elif field in parameters.get('required',[]):
                        raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
                    else:
                        properties.pop(field)
            if function.get('name') == 'knowledge_search':
                if self._explicit_public_as_of is not None:
                    parameters=function['parameters']
                    parameters['properties']['as_of']['enum']=[self._explicit_public_as_of]
                    if 'as_of' not in parameters['required']:parameters['required'].append('as_of')
                query=function.get('parameters',{}).get('properties',{}).get('query')
                if not isinstance(query,dict) or query.get('type')!='string':
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                choices=[]
                for concept in sorted(self._public_terms):
                    try:
                        accepted=self.public_request({'query':concept})
                    except BoundaryError:
                        continue
                    if accepted=={'query':concept}:choices.append(concept)
                if not choices:raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
                query['enum']=choices
                query['description']='单选本轮已登记的公开名称或金融概念；主体使用已核验product_id/share_class_id，查询维度可组合facets。不得填完整用户问题、个人身份、账户、持有天数或金额；未列概念不作为出站查询。'
            if metric_selections and function.get('name') == 'answer_reviewed':
                parameters = function.get('parameters', {})
                properties = parameters.get('properties', {})
                if 'direct_answer' in properties:
                    self._limit_public_quote_answer_schema(parameters)
                    public_quote_answer_mode = True
                    repair_schema=self._public_quote_repair_schema(parameters)
                    if repair_schema is not None:
                        function['parameters']=repair_schema
                        function['description']='先为每个指定路径写出修复后的完整自然表达，保留原段有据结论、非金额条件与未核实边界，仍须直接回答完整用户问题；不能用占位文字或引文代替回答。再选择该段必需的本轮公开货币引文，无需金额时引用列表为空。提交base_sha256与覆盖全部路径的replacements，其他原参数由宿主原样保留；不得遗漏、重复或原样重交。'
                        if self._public_quote_repair.get('market_recovery') is not None:
                            function['description']='本次市场新闻仅提交base_sha256、selections和paragraphs三个字段。selections逐一覆盖每个slot_id，只能选对应source_choices；paragraphs逐一覆盖指定原text路径，只写完整自然的非货币解释，不添加新事实，不抄货币数值或占位句。其他段落、claims、条件和未知边界原样保留；所选完整原文由宿主展开，原金额、来源、时效、GLM及最终发布校验不变。'
                        if self._public_quote_repair.get('fee_recovery') is not None:
                            function['description']='仅提交base_sha256与replacements，每个指定path各一次，仅含path/text。仅围绕用户所问费用，按本轮原文清楚说明费种、份额、费率周期、费率和扣除口径，不用同数值的其他费种代替答复，不新增无关时限。删除或按原文修正无据费用计提、扣除、频率、净值作用及持有期限适用关系；若同时给出计日反馈，本次一起补足同稿已有期限的同版口径、前提及例外。时间专用append_only_paths须逐字保留原片前缀再追加，重合费用路径也不得删原有据期限和例外。不得以常识新增结论；保留年率、费档、条件和未知边界，其余结构及证据绑定由宿主保留，不选择货币引文或添加字段。此为共享唯一修稿，金额、来源、发布与GLM仍须通过。'
                        if (self._public_quote_repair.get('fee_recovery')or {}).get('time_basis_rejections'):
                            function['description']='仅提交base_sha256与覆盖费用路径的replacements，各项仅path/text。只修无据费用关系，按本轮原文清楚说明所问费种、份额、费率周期、费率及扣除口径；不能用其他费种的同数值代替答复。原期限完整句、前提和例外必须原样保留，不新增期限、单位或T日口径，不补抄计日定义；绑定的规范来源原文由宿主独立核验后本地装配。此为共享唯一修稿，其余结构及证据由宿主保留，原费用、时间、金额、完整GLM和发布门仍须通过。'
                        if self._public_quote_repair.get('time_basis_only') is True:
                            function['description']='仅提交原base_sha256及覆盖所列路径的replacements，各项仅path、text、public_quote_fact_ids=[]。text必须逐字保留原片作为前缀，只在末尾补本轮同版本原文支持的计日口径、前提及例外，不删除原条件或未知字段，不添加无据期限；宿主保留其余整稿。此为原唯一修稿，来源时间、金额、完整GLM与发布校验仍照常。'
                        public_quote_repair_mode=True
                elif 'reply_parts' in properties:
                    reply_parts_reference_field=self._limit_reply_parts_schema(parameters)
                    claim_item=parameters['properties']['claims']['items']
                    if 'oneOf' in claim_item:
                        variants=claim_item['oneOf']
                        if len(variants)!=2 or set(variants[1].get('properties',{}))!={'statistic_id'}:
                            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                        walk(variants[0])
                        reference=variants[1]['properties']['statistic_id']
                        reference['enum']=[a for value in reference.get('enum',[])
                            for a in [value if value in self._statistic_selection_aliases else self._forward.get(value)]
                            if a in self._statistic_selection_aliases]
                        if not reference['enum']:parameters['properties']['claims']['items']=variants[0]
                    reply_parts_mode = True
                    if reply_parts_reference_field == 'metric_id':
                        repair_schema = self._private_parts_repair_schema(parameters)
                        if repair_schema is not None:
                            function['parameters'] = repair_schema
                            function['description'] = ('仅提交本轮base_sha256与replacements对象。claims路径必须omit=true；有标准金额跨度的正文路径必须metric_ids数组或omit=true，无可安全定位跨度的正文路径才可用非金额text/metric_id的parts。严格按反馈metric_counts选择，禁止重抄含金额正文。未指定字段保留，单次显式补丁，原全部校验和审核不变。'
                                if self._private_parts_repair.get('money_only') else
                                '仅提交本轮base_sha256与replacements对象；每个指定path都是必填唯一键，值为statistic_id或omit=true（claim），或parts（reply_parts）。不要数组或内层path。未指定原字段保留，只允许一次，原全部校验仍须通过。'
                                if self._private_parts_repair.get('mode')=='fields' else
                                '仅提交本轮base_sha256和replacements对象；每个指定reply_parts路径都是必填键。metric_ids按原段金额顺序选择，数量等于metric_counts，其他文字逐字保留；非必要段落可omit=true。不要自由text、index/parts数组或完整终稿。只允许一次，原全部校验和GLM仍须通过。')
                            private_parts_repair_mode = True
                elif 'metric_selections' in properties:
                    self._limit_metric_selection_schema(parameters)
                elif (not isinstance(properties.get('amount_claims'), Mapping)
                      or properties['amount_claims'].get('maxItems') != 0):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            if compact_metric_sources and not metric_selections and function.get('name') == 'answer_reviewed':
                item = function.get('parameters', {}).get('properties', {}).get('amount_claims', {}).get('items')
                if (not isinstance(item, Mapping) or not _METRIC_REF_FIELDS <= set(item.get('properties', {}))
                        or not (_METRIC_REF_FIELDS | {'text', 'claim_kind'}) <= set(item.get('required', []))):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                item['properties']['source_version_ref'] = {
                    'type': 'string', 'pattern': r'^vset_[a-f0-9]{8}$',
                    'description': '复制本轮对应 metric_id 的 source_version_ref；与完整 source_versions 二选一，其他签名字段仍须完整复制。'}
                item['required'] = [key for key in item['required'] if key != 'source_versions']
                item['oneOf'] = [{'required': ['source_versions']}, {'required': ['source_version_ref']}]
        self._metric_selection_mode = metric_selections
        self._public_quote_answer_mode = public_quote_answer_mode
        self._public_quote_repair_schema_active = public_quote_repair_mode
        self._private_parts_repair_schema_active = private_parts_repair_mode
        self._reply_parts_mode = reply_parts_mode
        self._reply_parts_reference_field = reply_parts_reference_field
        return result

    def _limit_public_quote_answer_schema(self, schema):
        properties=schema.get('properties',{})
        if ({'text','reply_parts','metric_selections','public_quotes'}&set(properties)
                or not {'direct_answer','conditions','unknowns'}<=set(schema.get('required',[]))
                or properties.get('amount_claims',{}).get('maxItems')!=0):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        def limit_parts(parts,maximum):
            if (not isinstance(parts,dict) or parts.get('type')!='array'
                    or type(parts.get('minItems'))is not int or parts['minItems']!=1
                    or type(parts.get('maxItems'))is not int or parts['maxItems']!=64):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            item=parts.get('items')
            if not isinstance(item,Mapping):raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            variants=item.get('oneOf')
            if not isinstance(variants,list)or len(variants)!=2:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            seen=set()
            for variant in variants:
                if (not isinstance(variant,Mapping)or variant.get('type')!='object'
                        or variant.get('additionalProperties')is not False
                        or not isinstance(variant.get('properties'),Mapping)or len(variant['properties'])!=1):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                field=next(iter(variant['properties']))
                if field not in ('text','public_quote_fact_id')or field in seen or variant.get('required')!=[field]:
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                seen.add(field);value=variant['properties'][field]
                if (not isinstance(value,dict)or value.get('type')!='string'
                        or type(value.get('minLength'))is not int or value['minLength']!=(0 if field=='text'else 1)
                        or type(value.get('maxLength'))is not int or value['maxLength']!=(maximum if field=='text'else 256)):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                if field=='public_quote_fact_id':
                    choices=value.get('enum')
                    if not isinstance(choices,list)or any(not isinstance(v,str)for v in choices):
                        raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    delivered=[]
                    for choice in choices:
                        alias=choice if choice in self._public_quote_fact_aliases else self._forward.get(choice)
                        if alias in self._public_quote_fact_aliases and alias not in delivered:delivered.append(alias)
                    if not delivered:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    value['enum']=delivered
            if seen!={'text','public_quote_fact_id'}:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        limit_parts(properties.get('direct_answer'),1200)
        for field in ('conditions','unknowns'):
            outer=properties.get(field,{})
            if (not isinstance(outer,Mapping)or outer.get('type')!='array'or type(outer.get('minItems'))is not int or outer['minItems']!=0
                    or type(outer.get('maxItems'))is not int or outer['maxItems']!=6):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            limit_parts(outer.get('items'),600)

    def _public_quote_repair_schema(self, original):
        repair=self._public_quote_repair
        if repair is None or repair['status']!='pending':return None
        if repair.get('market_recovery') is not None:
            from .market_quote_recovery import market_repair_schema
            return market_repair_schema(repair['market_recovery'])
        if repair.get('fee_recovery') is not None:
            from .public_quote_recovery import fee_repair_schema
            return fee_repair_schema(repair['fee_recovery'])
        if repair.get('choice_recovery') is not None:
            from .public_quote_recovery import choice_repair_schema
            return choice_repair_schema(repair['choice_recovery'])
        reference=copy.deepcopy(next(variant['properties']['public_quote_fact_id']
            for variant in original['properties']['direct_answer']['items']['oneOf']
            if 'public_quote_fact_id'in variant['properties']))
        variants=[]
        for maximum,direct in ((1200,True),(600,False)):
            paths=[path for path in repair['paths']if path.startswith('direct_answer[')==direct]
            if paths:
                variants.append({'type':'object','additionalProperties':False,
                    'required':['path','text','public_quote_fact_ids'],
                    'properties':{'path':{'type':'string','enum':paths},
                        'text':{'type':'string','minLength':1,'maxLength':maximum,
                            'description':'text与绑定的本轮可信引文共同完整回答问题；text须直接表达有据的非货币结论、非金额资格及渠道条件和未核实边界，必需金额及阈值由引文完整展开；即使填写引用ID，text中的货币数值仍会被拒绝；金额只由引用原文展示，text不得继续包含货币数值或金额阈值，也不能只删币种或改写数字；用户必需金额由引用显示，不用占位文字替代回答。'},
                        'public_quote_fact_ids':{'type':'array','minItems':0,'maxItems':6,'items':copy.deepcopy(reference),
                            'description':'只选本段必需货币金额或阈值的本轮可信原件；不需金额时为空，同一原文不必多段重复，不能省略必需金额或条件。'}}})
        if repair.get('time_basis_only') is True:
            for variant in variants:
                variant['properties']['text']['description']='逐字保留原片作为前缀，仅在末尾补同版本原文支持的计日说明、申请前提和例外；不得删除或改写原片、原条件与未知边界。'
                variant['properties']['public_quote_fact_ids'].update(maxItems=0,
                    description='此次仅补计日说明，必须为空；不替换本轮原有引用。')
        return {'type':'object','additionalProperties':False,
            'properties':{'base_sha256':{'type':'string','enum':[repair['base_sha256']]},
                'replacements':{'type':'array','minItems':len(repair['paths']),'maxItems':len(repair['paths']),
                    'items':{'oneOf':variants}}},
            'required':['base_sha256','replacements']}

    def _restore_public_quote_repair(self,args):
        repair=self._public_quote_repair
        if (not self._metric_selection_mode or not getattr(self,'_public_quote_answer_mode',False)
                or not self._public_quote_repair_schema_active or repair is None
                or repair['status']!='pending' or self._public_quote_repair_used):
            raise BoundaryError('BOUNDARY_OUTPUT')
        # Consume the sole attempt before validation; malformed patches are fatal.
        self._public_quote_repair_used=True;repair['status']='failed'
        if repair.get('market_recovery') is not None:
            from .market_quote_recovery import apply_market_repair
            try:
                assembled=apply_market_repair(repair['blocked'],repair['market_recovery'],
                    self._public_quote_fact_texts,args)
            except (ValueError,KeyError,IndexError,TypeError):raise BoundaryError('BOUNDARY_OUTPUT')
            repair['status']='applying';self._public_quote_repair_applying=True
            try:restored=self._restore_public_quote_answer(assembled)
            except BaseException:
                repair['status']='failed'
                raise
            finally:self._public_quote_repair_applying=False
            repair['status']='restored'
            return restored
        if repair.get('fee_recovery') is not None:
            from .public_quote_recovery import apply_fee_repair
            try:assembled=apply_fee_repair(repair['blocked'],repair['fee_recovery'],args)
            except (ValueError,KeyError,IndexError,TypeError):raise BoundaryError('BOUNDARY_OUTPUT')
            repair['status']='applying';self._public_quote_repair_applying=True
            try:restored=self._restore_public_quote_answer(assembled)
            except BaseException:
                repair['status']='failed'
                raise
            finally:self._public_quote_repair_applying=False
            repair['status']='restored'
            return restored
        if repair.get('choice_recovery') is not None:
            from .public_quote_recovery import apply_choice_repair
            try:
                assembled=apply_choice_repair(repair['blocked'],repair['choice_recovery'],
                    self._public_quote_fact_texts,args)
            except (ValueError,KeyError,IndexError,TypeError):raise BoundaryError('BOUNDARY_OUTPUT')
            repair['status']='applying';self._public_quote_repair_applying=True
            try:restored=self._restore_public_quote_answer(assembled)
            except BaseException:
                repair['status']='failed'
                raise
            finally:self._public_quote_repair_applying=False
            repair['status']='restored'
            return restored
        if (set(args)!={'base_sha256','replacements'} or args['base_sha256']!=repair['base_sha256']
                or not isinstance(args['replacements'],list)
                or len(args['replacements'])!=len(repair['paths'])):raise BoundaryError('BOUNDARY_OUTPUT')
        updates=[];seen=set()
        for replacement in args['replacements']:
            if not isinstance(replacement,Mapping) or set(replacement)!={'path','text','public_quote_fact_ids'}:
                raise BoundaryError('BOUNDARY_OUTPUT')
            path=replacement['path'];text=replacement['text'];references=replacement['public_quote_fact_ids']
            if (not isinstance(path,str) or path not in repair['paths'] or path in seen
                    or not isinstance(text,str) or not text.strip()
                    or len(text)>(1200 if path.startswith('direct_answer[')else 600)
                    or not isinstance(references,list) or len(references)>6
                    or any(not isinstance(value,str)or not 1<=len(value)<=256 for value in references)):
                raise BoundaryError('BOUNDARY_OUTPUT')
            parts=[{'text':text}]+[{'public_quote_fact_id':value}for value in references]
            seen.add(path)
            match=re.fullmatch(r'(direct_answer|conditions|unknowns)\[(\d+)\](?:\[(\d+)\])?\.text',path)
            if match is None:raise BoundaryError('BOUNDARY_OUTPUT')
            field,first,second=match.groups();row_index=None if field=='direct_answer'else int(first)
            index=int(first)if row_index is None else int(second)
            row=repair['blocked'][field]if row_index is None else repair['blocked'][field][row_index]
            original=row[index]
            if (repair.get('time_basis_only') is True
                    and (references or not text.startswith(original['text']))):
                raise BoundaryError('BOUNDARY_OUTPUT')
            if not references and text==original['text']:raise BoundaryError('BOUNDARY_OUTPUT')
            updates.append((field,row_index,index,copy.deepcopy(parts)))
        if seen!=set(repair['paths']):raise BoundaryError('BOUNDARY_OUTPUT')
        assembled=copy.deepcopy(repair['blocked'])
        # Multiple replacements in one row use original positions, never shifted ones.
        for field,row_index,index,parts in sorted(updates,key=lambda x:(x[0],-1 if x[1]is None else x[1],x[2]),reverse=True):
            row=assembled[field]if row_index is None else assembled[field][row_index]
            row[index:index+1]=parts
        repair['status']='applying';self._public_quote_repair_applying=True
        try:restored=self._restore_public_quote_answer(assembled)
        except BaseException:
            repair['status']='failed'
            raise
        finally:self._public_quote_repair_applying=False
        repair['status']='restored'
        return restored

    def _restore_public_quote_answer(self,args):
        repair=self._public_quote_repair
        if (repair is not None and repair['status'] in ('pending','failed','applying')
                and not self._public_quote_repair_applying):raise BoundaryError('BOUNDARY_OUTPUT')
        if (not self._metric_selection_mode or not getattr(self,'_public_quote_answer_mode',False)
                or {'text','reply_parts','metric_selections','public_quotes'}&set(args)
                or not {'direct_answer','conditions','unknowns'}<=set(args)):
            raise BoundaryError('BOUNDARY_OUTPUT')
        envelope={k:v for k,v in args.items()if k not in {'direct_answer','conditions','unknowns'}}
        result=self._restore_answer(envelope);part_count=0;quote_count=0
        for field,maximum in (('direct_answer',1200),('conditions',600),('unknowns',600)):
            rows=[args[field]]if field=='direct_answer'else args[field]
            if not isinstance(rows,list)or(field!='direct_answer'and len(rows)>6):raise BoundaryError('BOUNDARY_OUTPUT')
            restored_rows=[]
            for row in rows:
                if not isinstance(row,list)or not 1<=len(row)<=64:raise BoundaryError('BOUNDARY_OUTPUT')
                restored=[];authored=[];has_quote=False
                for part in row:
                    if not isinstance(part,Mapping)or set(part)not in ({'text'},{'public_quote_fact_id'}):
                        raise BoundaryError('BOUNDARY_OUTPUT')
                    part_count+=1
                    if part_count>64:raise BoundaryError('BOUNDARY_OUTPUT')
                    if 'text'in part:
                        value=part['text']
                        if not isinstance(value,str)or not 0<=len(value)<=maximum:raise BoundaryError('BOUNDARY_OUTPUT')
                        text=self._restore(value);before=self._counts['redactions'];self._text(text,maximum=maximum)
                        if self._counts['redactions']>before:raise BoundaryError('BOUNDARY_OUTPUT')
                        restored.append({'text':text});authored.append(text)
                    else:
                        alias=part['public_quote_fact_id']
                        if (not isinstance(alias,str)or not _ALIAS.fullmatch(alias)
                                or alias not in self._public_quote_fact_aliases or alias not in self._reverse):
                            raise BoundaryError('BOUNDARY_OUTPUT')
                        quote_count+=1
                        if quote_count>6:raise BoundaryError('BOUNDARY_OUTPUT')
                        restored.append({'public_quote_fact_id':self._reverse[alias]});has_quote=True
                text=''.join(authored)
                if len(text)>maximum or(not text.strip()and(field=='direct_answer'or not has_quote)):
                    raise BoundaryError('BOUNDARY_OUTPUT')
                restored_rows.append(restored)
            result[field]=restored_rows[0]if field=='direct_answer'else restored_rows
        if not 0<=quote_count<=6:raise BoundaryError('BOUNDARY_OUTPUT')
        # Capture only the successfully checked model-visible aliases version.
        self._public_quote_model_answer=copy.deepcopy(dict(args))
        self._public_quote_restored_answer=copy.deepcopy(result)
        return result

    def public_quote_time_feedback(self,args,binding):
        if self._public_quote_repair_used:
            raise BankError('PUBLIC_QUOTE_REPAIR_EXHAUSTED','受限修复后来源时间条件仍未通过，本轮停止。',422)
        if (not self._metric_selection_mode or not getattr(self,'_public_quote_answer_mode',False)
                or self._public_quote_repair is not None or self._public_quote_model_answer is None
                or args!=self._public_quote_restored_answer or not isinstance(binding,Mapping)
                or binding.get('answer_status')!='missing'
                or binding.get('counting_unit')not in ('工作日','自然日')
                or binding.get('excludes_t')not in (None,True)
                or not isinstance(binding.get('definition'),str) or not binding['definition']):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        notation=binding.get('notation');match=re.fullmatch(r'T\+([0-9]+)',notation or '')
        if match is None:raise BoundaryError('BOUNDARY_INVALID_INPUT')
        from .knowledge_semantics import _concrete_time_conclusions
        paths=[]
        for field in ('direct_answer','conditions','unknowns'):
            rows=[args[field]]if field=='direct_answer'else args[field]
            for row_index,row in enumerate(rows):
                for part_index,part in enumerate(row):
                    if ('text'in part
                            and match[1]in _concrete_time_conclusions(part['text'])):
                        paths.append(field+'['+str(part_index)+'].text'if field=='direct_answer'else
                            field+'['+str(row_index)+']['+str(part_index)+'].text')
        if not paths:
            raise BankError('PUBLIC_QUOTE_REPAIR_UNAVAILABLE','没有可核验的原期限文字路径，本轮停止。',422)
        blocked=copy.deepcopy(self._public_quote_model_answer)
        canonical=json.dumps(blocked,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        base=hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        self._public_quote_repair={'blocked':blocked,'base_sha256':base,'paths':tuple(paths),
            'status':'pending','time_basis_only':True}
        return {'contract_error':'PUBLIC_TIME_BASIS_REQUIRED',
            'repair':{'contract':'navren.public-quote-answer-repair/3','base_sha256':base,
                'fields':paths,'replacement_fields':['text','public_quote_fact_ids']},
            'source_time_basis':{key:binding[key]for key in
                ('notation','definition','counting_unit','excludes_t')},
            'instruction':'沿用原结构和原条件，只补本轮同版本原文支持的计日口径及其前提、例外。'
                '对每个指定路径提交一次完整text，必须逐字保留该原片作为前缀，仅在末尾追加说明；'
                'public_quote_fact_ids必须为空，其他分区、条件、claims和引用由宿主原样保留。'
                '不能删条件、退回text终稿或只把条件留在claims；不新增检索，不补造工作日或交易结论。'
                '这是与费用、金额修复共享的唯一一次机会；来源时间、金额、完整GLM和发布校验仍全部执行。'}

    def public_quote_fee_feedback(self,args,rejected,*,time_basis_rejections=(),time_endpoint_sources=()):
        if self._public_quote_repair_used:
            raise BankError('PUBLIC_QUOTE_REPAIR_EXHAUSTED','受限修复后费用来源仍未通过，本轮停止。',422)
        if (not self._metric_selection_mode or not getattr(self,'_public_quote_answer_mode',False)
                or self._public_quote_repair is not None or self._public_quote_model_answer is None
                or args!=self._public_quote_restored_answer):raise BoundaryError('BOUNDARY_INVALID_INPUT')
        from .public_quote_recovery import build_fee_repair
        blocked=copy.deepcopy(self._public_quote_model_answer)
        try:context=build_fee_repair(blocked,rejected,time_basis_rejections=time_basis_rejections,
            time_endpoint_sources=time_endpoint_sources)
        except (ValueError,KeyError,IndexError,TypeError):
            raise BankError('PUBLIC_QUOTE_REPAIR_UNAVAILABLE','本轮没有可核验的费用来源修复路径。',422)
        self._public_quote_repair={'blocked':blocked,'base_sha256':context['base_sha256'],
            'paths':tuple(context['paths']),'status':'pending','fee_recovery':context}
        feedback={'contract_error':'PUBLIC_FEE_SOURCE_REQUIRED',
            'repair':{key:copy.deepcopy(context[key])for key in ('contract','base_sha256','paths')},
            'instruction':'本轮已交付原文未支持所列费用计提、频率、持有期限无条件适用或净值作用关系。下一次answer_reviewed只提交base_sha256和replacements；'
                '每个path各提交一次非空text，只修所列正文及claims字符串，其他参数由宿主逐字保留。'
                '删除或按本轮原文修正所有无据的费用作用、范围、计提及频率关系，不能只删计提词；不得用会计常识新增结论。资产扣除与年费率不能证明持有期限无条件适用、净值影响或每日计提。'
                '仅围绕用户所问费用，按本轮原文清楚说明相关费种、份额、费率周期、费率及扣除口径；不能用同数值的其他费种代替答复，不新增与问题无关的时限。'
                '如同时提供source_time_basis，须在本次补片补足同稿已有期限的同版原文计日口径、前提及例外。append_only_paths须逐字保留原片作为前缀，仅末尾追加；费用与时间重合路径仍须保留原有据期限和例外，不能删条件规避时间门。'
                '不要把这些费用或计日口径指向无关原文、费种或版本；保留原费率档、条件、风险及未知边界，不重读、不执行操作。'
                '此为原public_quote唯一一次修稿，费用与时间须一起修复，失败即停止；原金额、来源、发布与GLM门保持不变。'}
        if context.get('time_basis_rejections'):
            feedback['source_time_basis']=[{key:binding[key]for key in
                ('notation','definition','counting_unit','excludes_t')}for binding in context['time_basis_rejections']]
            feedback['instruction']='本轮已交付原文未支持所列费用关系。下一次answer_reviewed只提交base_sha256和覆盖费用路径的replacements，每个path各一次非空text；其他参数由宿主逐字保留。只删除或按原文修正无据费用计提、频率、净值作用及持有期限适用关系，不得用常识新增结论。仅围绕用户所问费用清楚说明费种、份额、费率周期、费率及扣除口径，不能把其他费种的同数值当答复。必须原样保留原稿已有期限的完整独立句、前提及例外，不新增或改写期限、单位或T日口径；规范计日原文将由宿主按本轮同版绑定独立核验后本地装配，模型不要补抄定义。不得删时间句规避校验；已有明确或含糊计日口径不属于自动补全范围。此为费用与时间共享的唯一修稿，原费用、时间、金额、完整GLM、FinalProof及发布校验保持不变。'
        if context.get('fee_safe_edits'):
            feedback['instruction']+=' 本次仅删除已定位的独立无据连续承担分句；每个path的text须选择schema内对应枚举，除此以外原字节、费种、份额、费率档、年率、全部时间句、前提、例外、claims及未知边界都不得更改。'
        return feedback

    def _public_quote_fee_source_context(self,restored_args):
        repair=self._public_quote_repair
        context=repair.get('fee_recovery')if isinstance(repair,Mapping)else None
        if not isinstance(context,Mapping)or context.get('time_basis_rejections'):return None
        if (self._public_quote_repair_used is not True or repair.get('status')!='restored'
                or restored_args!=self._public_quote_restored_answer):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        from .public_quote_recovery import _digest, _fee_source_time_sentences
        protected=context.get('fee_source_time_sentences')
        if (type(protected)is not list or protected!=_fee_source_time_sentences(repair['blocked'])
                or protected!=_fee_source_time_sentences(restored_args)):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        return copy.deepcopy({'contract':'navren.public-quote-fee-source-context/1',
            'original_draft_sha256':context['base_sha256'],'restored_args_sha256':_digest(restored_args),
            'fee_paths':context['paths'],'protected_time_sentences':protected})

    def public_quote_joint_source_context(self,restored_args):
        repair=self._public_quote_repair
        context=repair.get('fee_recovery')if isinstance(repair,Mapping)else None
        if not isinstance(context,Mapping):
            if (not self._metric_selection_mode or not getattr(self,'_public_quote_answer_mode',False)
                    or self._public_quote_model_answer is None
                    or not isinstance(restored_args,Mapping)or 'direct_answer'not in restored_args):return None
            if (restored_args!=self._public_quote_restored_answer or repair is not None
                    and (self._public_quote_repair_used is not True or repair.get('status')!='restored')):
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            from .public_quote_recovery import _digest
            original=repair['blocked']if repair is not None else self._public_quote_model_answer
            return {'contract':'navren.public-quote-fee-delivery-context/1',
                'original_draft_sha256':_digest(original),'restored_args_sha256':_digest(restored_args)}
        if not context.get('time_basis_rejections'):
            return self._public_quote_fee_source_context(restored_args)
        if (self._public_quote_repair_used is not True or repair.get('status')!='restored'
                or restored_args!=self._public_quote_restored_answer):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        from .public_quote_recovery import _digest
        result=copy.deepcopy({'contract':'navren.public-quote-joint-source-context/1',
            'original_draft_sha256':context['base_sha256'],
            'restored_args_sha256':_digest(restored_args),
            'fee_paths':context['fee_paths'],'time_paths':context['time_paths'],
            'time_basis_rejections':context['time_basis_rejections'],
            'protected_time_sentences':context['protected_time_sentences']})
        if context.get('time_endpoint_sources'):result['time_endpoint_sources']=copy.deepcopy(context['time_endpoint_sources'])
        return result

    def _public_fee_delivery_sources(self):
        """Rebuild all cited fee originals from this boundary's delivered views."""
        cited={eid for claim in self._public_quote_restored_answer['claims']for eid in claim['evidence_ids']}
        selected={}
        for eid in sorted(cited):
            snapshot=self._public_fee_delivery_searches.get(eid)
            if snapshot is None:continue
            data=snapshot['data'];visible=snapshot['visible']
            if not isinstance(visible,Mapping):raise BoundaryError('BOUNDARY_INVALID_INPUT')
            for hit in data.get('hits',[]):
                if 'fees'not in hit.get('facets',[]):continue
                keys=('citation_id','document_id','document_version_id','release_id',
                    'product_id','share_class_id','channel_id')
                matched=[item for item in visible.get('hits',[])if isinstance(item,Mapping)
                    and all(item.get(key)==hit.get(key)for key in keys)
                    and unicodedata.normalize('NFKC',item.get('excerpt',''))==
                        unicodedata.normalize('NFKC',hit['excerpt'])]
                if (data.get('ready')is not True or data.get('scope')!='public'
                        or data.get('public_release_id')!=hit['release_id']or len(matched)!=1):
                    raise BoundaryError('BOUNDARY_INVALID_INPUT')
                row={'evidence_id':eid,'hit':copy.deepcopy(hit),'as_of':data['as_of']}
                key=(hit['citation_id'],data['as_of'])
                if key in selected and selected[key]['hit']!=hit:raise BoundaryError('BOUNDARY_INVALID_INPUT')
                selected.setdefault(key,row)
        from .public_fee_statement_contract import canonical_source_rows
        return canonical_source_rows(list(selected.values()))

    def _public_fee_statement_evidence(self):
        """Rebuild request-local evidence solely from this boundary's snapshots."""
        cited={eid for claim in self._public_quote_restored_answer['claims']for eid in claim['evidence_ids']}
        evidence={}
        keys=('citation_id','document_id','document_version_id','release_id',
            'product_id','share_class_id','channel_id','visibility','outbound_allowed',
            'content_status','claims_are_account_facts')
        for eid in sorted(cited):
            snapshot=self._public_fee_delivery_searches.get(eid)
            if snapshot is None:continue
            data=snapshot['data'];visible=snapshot['visible']
            if not isinstance(visible,Mapping):raise BoundaryError('BOUNDARY_INVALID_INPUT')
            for hit in data.get('hits',[]):
                if not {'fees','liquidity','terms'}&set(hit.get('facets',[])):continue
                matches=[item for item in visible.get('hits',[])if isinstance(item,Mapping)
                    and all(item.get(key)==hit.get(key)for key in keys)
                    and unicodedata.normalize('NFKC',item.get('excerpt',''))==
                        unicodedata.normalize('NFKC',hit['excerpt'])]
                if len(matches)!=1:raise BoundaryError('BOUNDARY_INVALID_INPUT')
            evidence[eid]={'id':eid,'tool':'knowledge_search','source':'public_knowledge',
                'context_hash':'boundary_actual_delivered_snapshot','data':copy.deepcopy(data)}
        return evidence

    @staticmethod
    def _validate_public_quote_joint_source_reference(reference):
        stages=('original_draft_sha256','restored_args_sha256','pre_quote_answer_sha256',
            'rendered_pre_boundary_answer_sha256','quote_claims_sha256')
        leaf_keys={'evidence_id','citation_id','document_id','document_version_id','release_id',
            'product_id','share_class_id','channel_id','as_of','provider_id','source_ref','source_revision'}
        def sha(value):
            return type(value)is str and re.fullmatch('[a-f0-9]{64}',value)is not None
        if (type(reference)is not dict or set(reference)!={'kind','source_ref','reference','references'}
                or reference['kind']!='public_quote_joint_source_rendering' or not sha(reference['source_ref'])):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        stage=reference['reference']
        if (type(stage)is not dict or set(stage)!={'kind','references'} or stage['kind']!='rendering_hashes'
                or type(stage['references'])is not list or len(stage['references'])!=len(stages)):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        for name,row in zip(stages,stage['references']):
            if (type(row)is not dict or set(row)!={'kind','source_ref'}
                    or row['kind']!=name or not sha(row['source_ref'])):
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
        leaves=reference['references']
        if type(leaves)is not list or not 1<=len(leaves)<=6:
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        for leaf in leaves:
            if (type(leaf)is not dict or set(leaf)!=leaf_keys
                    or any(type(leaf[key])is not str
                        or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,299}',leaf[key])
                        or _ALIAS.search(leaf[key])for key in leaf_keys-{'channel_id','source_ref','source_revision'})
                    or not (leaf['channel_id']is None or type(leaf['channel_id'])is str
                        and re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,299}',leaf['channel_id'])
                        and not _ALIAS.search(leaf['channel_id']))
                    or not sha(leaf['source_ref'])or not sha(leaf['source_revision'])):
                raise BoundaryError('BOUNDARY_INVALID_INPUT')

    def register_public_quote_joint_source_reference(self,plan,receipt):
        """Trusted host only: one restored joint plan, no new tool or public identity."""
        from .public_quote_recovery import _digest
        try:
            context=self.public_quote_joint_source_context(self._public_quote_restored_answer)
            if (context is None or type(plan)is not dict
                    or set(plan)!={'status','arguments_sha256','context','sources'}
                    or plan['status']!='consumed' or _digest(plan['context'])!=_digest(context)
                    or plan['arguments_sha256']!=context['restored_args_sha256']):
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            sources=plan['sources'];delivery=context['contract']=='navren.public-quote-fee-delivery-context/1'
            fee_only=context['contract']=='navren.public-quote-fee-source-context/1'or delivery
            if delivery and sources!=self._public_fee_delivery_sources():
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            identity_fields=('product_id','share_class_id','channel_id','release_id')
            if fee_only:
                if type(sources)is not list or not 1<=len(sources)<=6:raise BoundaryError('BOUNDARY_INVALID_INPUT')
                cited={eid for claim in self._public_quote_restored_answer['claims']for eid in claim['evidence_ids']}
                if any(type(row)is not dict or row.get('evidence_id')not in cited for row in sources):
                    raise BoundaryError('BOUNDARY_INVALID_INPUT')
                bindings=[]
                identities={tuple(row['hit'].get(key)for key in identity_fields)for row in sources}
            else:
                bindings=context['time_basis_rejections']
                identities={tuple(item.get(key)for key in identity_fields)for item in bindings}
            from .public_fee_statement_contract import canonical_source_rows
            if sources!=canonical_source_rows(sources,bindings):
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            if len(identities)!=1 or type(sources)is not list or not 1<=len(sources)<=6:
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            identity=next(iter(identities));leaves=[];seen=set();fee_versions=set();as_ofs=set()
            if (not identity[0]or not identity[1]or type(receipt)is not dict
                    or type(receipt.get('source_claims'))is not list
                    or len(receipt['source_claims'])!=len(sources)):
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            providers={leaf.get('provider_id')for leaf in receipt['source_claims']if type(leaf)is dict}
            if len(providers)!=1:raise BoundaryError('BOUNDARY_INVALID_INPUT')
            provider=next(iter(providers))
            if (type(provider)is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._:-]{0,299}',provider)
                    or _ALIAS.search(provider)):
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            if delivery and any(self._public_fee_delivery_searches[row['evidence_id']]['data'].get('provider_id')
                    !=provider for row in sources):raise BoundaryError('BOUNDARY_INVALID_INPUT')
            for row in sources:
                if type(row)is not dict or set(row)!={'evidence_id','hit','as_of'}:
                    raise BoundaryError('BOUNDARY_INVALID_INPUT')
                hit=row['hit'];day=row['as_of'];eid=row['evidence_id']
                if (type(hit)is not dict or type(eid)is not str or not 1<=len(eid)<=300
                        or _ALIAS.search(eid)or type(day)is not str or date.fromisoformat(day).isoformat()!=day
                        or tuple(hit.get(key)for key in identity_fields)!=identity
                        or hit.get('visibility')!='public' or hit.get('outbound_allowed')is not True
                        or hit.get('content_status')!='full_text' or hit.get('claims_are_account_facts')is not False
                        or hit.get('withdrawn',False)is not False
                        or type(hit.get('excerpt'))is not str or not 0<len(hit['excerpt'])<=1198
                        or any(type(hit.get(key))is not str or not re.fullmatch(prefix+'[a-f0-9]{28,64}',hit[key])
                            for key,prefix in (('citation_id','c'),('document_id','d'),('document_version_id','v')))
                        or type(hit.get('release_id'))is not str
                        or not re.fullmatch('pub-[a-f0-9]{40}',hit['release_id'])):
                    raise BoundaryError('BOUNDARY_INVALID_INPUT')
                source=(hit['citation_id'],day)
                if source in seen:raise BoundaryError('BOUNDARY_INVALID_INPUT')
                seen.add(source);as_ofs.add(day);facets=hit.get('facets',[])
                if type(facets)is not list:raise BoundaryError('BOUNDARY_INVALID_INPUT')
                if 'fees'in facets:fee_versions.add((hit['document_id'],hit['document_version_id']))
                elif not any(eid==item.get(role+'_evidence_id')and hit['citation_id']==item.get(role+'_citation_id')
                        and all(hit.get(key)==item.get(key)for key in
                            (*identity_fields,'document_id','document_version_id'))
                        for item in bindings for role in ('limit','definition')):
                    raise BoundaryError('BOUNDARY_INVALID_INPUT')
                canonical=hit['excerpt'];display=unicodedata.normalize('NFKC',canonical)
                leaves.append({'evidence_id':eid,'citation_id':hit['citation_id'],
                    'document_id':hit['document_id'],'document_version_id':hit['document_version_id'],
                    'release_id':hit['release_id'],'product_id':hit['product_id'],'share_class_id':hit['share_class_id'],
                    'channel_id':hit.get('channel_id'),'as_of':day,'provider_id':provider,
                    'source_ref':hashlib.sha256(canonical.encode()).hexdigest(),
                    'source_revision':hashlib.sha256(display.encode()).hexdigest()})
            if len(as_ofs)!=1 or len(fee_versions)!=1:raise BoundaryError('BOUNDARY_INVALID_INPUT')
            for item in bindings:
                for role in ('limit','definition'):
                    matched=[row for row in sources if row['evidence_id']==item.get(role+'_evidence_id')
                        and row['hit']['citation_id']==item.get(role+'_citation_id')
                        and all(row['hit'].get(key)==item.get(key)for key in
                            (*identity_fields,'document_id','document_version_id'))
                        and {'liquidity','terms'}&set(row['hit']['facets'])]
                    if len(matched)!=1:raise BoundaryError('BOUNDARY_INVALID_INPUT')
            stages=('original_draft_sha256','restored_args_sha256','pre_quote_answer_sha256',
                'rendered_pre_boundary_answer_sha256','quote_claims_sha256')
            # Rebuild the authored statement proof from this boundary's own
            # restored draft and actual delivered source snapshots, not the
            # producer's catalogue or assertion of validation.
            from .public_fee_statement_contract import build_catalog,validate_authored,extend_time_catalog
            fee_sources=self._public_fee_delivery_sources()
            if ([row for row in sources if 'fees'in row['hit'].get('facets',[])]!=fee_sources
                    or any(self._public_fee_delivery_searches[row['evidence_id']]['data'].get('provider_id')
                        !=provider for row in fee_sources)):
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            statement_proof=validate_authored(self._public_quote_restored_answer,
                extend_time_catalog(build_catalog(fee_sources,provider),self._public_fee_statement_evidence()))
            receipt_keys={'contract',*stages,'source_claims','host_generated_conclusion',
                'additional_primary_calls','additional_business_calls','original_guards_status','fee_statement_proof'}
            if (set(receipt)!=receipt_keys or receipt['contract']!='navren.public-quote-joint-source-rendering/2'
                    or receipt['fee_statement_proof']!=statement_proof
                    or receipt['source_claims']!=leaves
                    or receipt['original_draft_sha256']!=context['original_draft_sha256']
                    or receipt['restored_args_sha256']!=context['restored_args_sha256']
                    or any(type(receipt[key])is not str or not re.fullmatch('[a-f0-9]{64}',receipt[key])for key in stages)
                    or receipt['host_generated_conclusion']is not False
                    or any(type(receipt[key])is not int or receipt[key]!=0
                        for key in ('additional_primary_calls','additional_business_calls'))
                    or receipt['original_guards_status']!='pending_original_full_GLM_and_FinalProof'):
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            reference={'kind':'public_quote_joint_source_rendering','source_ref':_digest(receipt),
                'reference':{'kind':'rendering_hashes','references':[
                    {'kind':key,'source_ref':receipt[key]}for key in stages]},'references':leaves}
            self._validate_public_quote_joint_source_reference(reference)
            registration={'reference':reference,'plan_sha256':_digest(plan),'receipt_sha256':_digest(receipt)}
            if (self._public_quote_joint_source_reference is not None
                    and self._public_quote_joint_source_reference!=registration):
                raise BoundaryError('BOUNDARY_INVALID_INPUT')
            self._public_quote_joint_source_reference=copy.deepcopy(registration)
            return copy.deepcopy(reference)
        except BoundaryError:raise
        except (KeyError,ValueError,TypeError,AttributeError,OverflowError):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')from None

    def public_quote_answer_feedback(self,code,args):
        if (code not in ('METRIC_AMOUNT_UNBOUND','INVALID_MONEY_TEXT','METRIC_MONEY_SYNTAX_REQUIRED')
                or not isinstance(args,Mapping)or not {'direct_answer','conditions','unknowns'}<=set(args)):
            raise BoundaryError('BOUNDARY_INVALID_INPUT')
        if self._public_quote_repair_used:
            raise BankError('PUBLIC_QUOTE_REPAIR_EXHAUSTED','受限修复后金额仍未通过，本轮停止。',422)
        if (self._public_quote_repair is not None or self._public_quote_model_answer is None
                or args!=self._public_quote_restored_answer):raise BoundaryError('BOUNDARY_INVALID_INPUT')
        from .metric_claims import MetricClaimError,monetary_occurrences
        paths=[];money_fragments={};part_count=0
        for field,maximum in (('direct_answer',1200),('conditions',600),('unknowns',600)):
            rows=[args[field]]if field=='direct_answer'else args[field]
            if not isinstance(rows,list)or(field!='direct_answer'and len(rows)>6):raise BoundaryError('BOUNDARY_INVALID_INPUT')
            for row_index,row in enumerate(rows):
                if not isinstance(row,list)or not 1<=len(row)<=64:raise BoundaryError('BOUNDARY_INVALID_INPUT')
                for part_index,part in enumerate(row):
                    if not isinstance(part,Mapping)or set(part)not in ({'text'},{'public_quote_fact_id'}):
                        raise BoundaryError('BOUNDARY_INVALID_INPUT')
                    part_count+=1
                    if part_count>64:raise BoundaryError('BOUNDARY_INVALID_INPUT')
                    if 'text'not in part:
                        value=part['public_quote_fact_id']
                        if not isinstance(value,str)or not 1<=len(value)<=256:raise BoundaryError('BOUNDARY_INVALID_INPUT')
                        continue
                    text=part['text']
                    if not isinstance(text,str)or len(text)>maximum:raise BoundaryError('BOUNDARY_INVALID_INPUT')
                    normalized=unicodedata.normalize('NFKC',text);parser_error=None
                    try:occurrences=monetary_occurrences(normalized)
                    except MetricClaimError as error:
                        occurrences=[];parser_error=error.code
                    if occurrences or parser_error is not None:
                        path=(field+'['+str(part_index)+'].text'if field=='direct_answer'else
                              field+'['+str(row_index)+']['+str(part_index)+'].text')
                        paths.append(path)
                        model_rows=([self._public_quote_model_answer[field]]if field=='direct_answer'
                                    else self._public_quote_model_answer[field])
                        visible=unicodedata.normalize('NFKC',model_rows[row_index][part_index]['text'])
                        visible_error=None
                        try:visible_occurrences=monetary_occurrences(visible)
                        except MetricClaimError as error:
                            visible_occurrences=[];visible_error=error.code
                        diagnostic={'normalization':'NFKC','fragments':[
                            visible[row['start']:row['end']]for row in visible_occurrences]}
                        if not visible_occurrences:diagnostic['fragments_unavailable']=True
                        if visible_error is not None:diagnostic['parser_error']=visible_error
                        money_fragments[path]=diagnostic
        if not paths:raise BankError('PUBLIC_QUOTE_REPAIR_UNAVAILABLE','本轮没有可核验的金额文字修复路径。',422)
        blocked=copy.deepcopy(self._public_quote_model_answer)
        canonical=json.dumps(blocked,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        base=hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        self._public_quote_repair={'blocked':blocked,'base_sha256':base,'paths':tuple(paths),'status':'pending'}
        if self._market_quote_fact_aliases and set(self._public_quote_fact_texts)<=self._market_quote_fact_aliases:
            from .market_quote_recovery import build_market_repair
            try:market=build_market_repair(blocked,paths,self._public_quote_fact_texts)
            except (ValueError,KeyError,IndexError,TypeError):market=None
            if market is not None:
                self._public_quote_repair['market_recovery']=market
                return {'contract_error':code,'repair':copy.deepcopy(market),
                    'instruction':'本次唯一修复提交base_sha256、selections和paragraphs。每个slot_id仍只能选择其source_choices中的原文编号；paragraphs仅重写列出的受阻段，保留因果、资格和未知边界，用自然完整的非货币句子解释背景。金额只通过所选完整引文展示，不写货币数值或占位句，不改其他段、claims、条件或未知字段。选择与重写不是语义批准，原金额、来源、新鲜度、完整GLM和最终发布仍全部核验。'}
        from .public_quote_recovery import build_choice_repair
        try:choice=build_choice_repair(blocked,paths,self._public_quote_fact_texts)
        except (ValueError,KeyError,IndexError,TypeError):choice=None
        if choice is not None:
            self._public_quote_repair['choice_recovery']=choice
            return {'contract_error':code,'repair':copy.deepcopy(choice),
                'instruction':'本次唯一修复只选择每个slot_id对应的本轮完整原文line_ref，逐一覆盖全部slot。不得提交text或删除原稿结论、费率、产品、资格或未知边界。宿主逐字保留全部非货币原稿，只把货币字面量换为所选原文行定位并保留完整引文；定位选择不是语义证明，原金额、来源和GLM仍逐项核验。'}
        return {'contract_error':code,'repair':{'contract':'navren.public-quote-answer-repair/3',
            'base_sha256':base,'fields':paths,'replacement_fields':['text','public_quote_fact_ids'],
            'money_fragments':money_fragments,
            'authority':'current_turn_delivered_public_quote_facts_only'},
            'instruction':'每个修复段的text与绑定的本轮可信引文共同表达完整答复；direct_answer的text须直接表达有据的非货币结论，保留非金额资格或渠道条件和未核实边界；必需金额及阈值由引文完整展开，text不得重复，也不能只写占位文字或只堆引文。'
                '本轮由宿主按原完整引述规则展开所选ID并补入claims；模型不再手工重复复制同一引文。'
                '下一次answer_reviewed只提交base_sha256和replacements，必须逐一覆盖全部所列text路径，各一次；每项仅path、非空text与public_quote_fact_ids数组。'
                'money_fragments仅列按模型原可见文本的NFKC形式定位的金额片段；这些片段是错误诊断，不是可用金额证据。parser_error表示无法可靠定位，不能把归一化位置当原串位置。'
                '即使填写引用ID，text中的货币数值仍会被拒绝；金额只由引用原文展示：text不得继续包含货币数值或金额阈值，不能仅追加引用、只删币种或换数字写法。保留完整非金额结论、资格渠道条件和未核实边界；必需金额由本轮可信引文显示。'
                '无需金额时引用列表为空，同一完整原文不必在多段重复。百分比本身不是货币金额，不给原文裸数字补造货币单位，不口算或换算转述；不能删除用户必需金额或适用条件。不得原样重交。'
                '宿主只按你给的text和引用列表替换指定片，其他模型原参数逐字保留；不生成金额、结论或资格。'
                '仅有一次受限修复机会，原正文长度、来源、金额及GLM仍须全部通过。'}

    def _limit_reply_parts_schema(self, schema):
        properties = schema.get('properties', {})
        parts = properties.get('reply_parts')
        claims = properties.get('amount_claims')
        if ('text' in properties or 'metric_selections' in properties
                or not isinstance(parts, dict) or parts.get('type') != 'array'
                or type(parts.get('minItems')) is not int or parts['minItems'] != 1
                or type(parts.get('maxItems')) is not int or parts['maxItems'] != 64
                or 'reply_parts' not in schema.get('required', [])
                or not isinstance(claims, Mapping) or claims.get('type') != 'array'
                or type(claims.get('maxItems')) is not int or claims['maxItems'] != 0):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        item = parts.get('items')
        variants = item.get('oneOf') if isinstance(item, Mapping) else None
        if not isinstance(variants, list) or len(variants) not in (2,3):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        # Statistics are optional: schema availability never issues delivery.
        variants[:]=[variant for variant in variants if 'statistic_id' not in variant.get('properties',{})
            or any((choice if choice in self._statistic_selection_aliases else self._forward.get(choice))
                in self._statistic_selection_aliases for choice in variant['properties']['statistic_id'].get('enum',[]))]
        seen = set()
        for variant in variants:
            if (not isinstance(variant, Mapping) or variant.get('type') != 'object'
                    or variant.get('additionalProperties') is not False
                    or not isinstance(variant.get('properties'), Mapping)
                    or len(variant['properties']) != 1):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            field = next(iter(variant['properties']))
            if field not in {'text', 'metric_id', 'public_quote_fact_id', 'statistic_id'} or field in seen or variant.get('required') != [field]:
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            seen.add(field)
            value = variant['properties'][field]
            maximum = 6000 if field == 'text' else 256
            if (not isinstance(value, dict) or value.get('type') != 'string'
                    or type(value.get('minLength')) is not int or value['minLength'] != (0 if field == 'text' else 1)
                    or type(value.get('maxLength')) is not int or value['maxLength'] != maximum):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            if field != 'text':
                aliases=(self._metric_selection_aliases if field=='metric_id' else self._statistic_selection_aliases if field=='statistic_id' else self._public_quote_fact_aliases)
                choices = value.get('enum')
                if not isinstance(choices, list) or any(not isinstance(v, str) for v in choices):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                # The legacy schema walker does not traverse oneOf. Resolve
                # only aliases already issued by real delivery; never _alias()
                # an undisclosed ID merely to build this new schema branch.
                delivered = []
                for choice in choices:
                    alias = choice if choice in aliases else self._forward.get(choice)
                    if alias in aliases and alias not in delivered:
                        delivered.append(alias)
                if delivered:
                    value['enum'] = delivered
                else:
                    # Never emit an invalid empty-enum schema or manufacture a
                    # selectable ID when no metric was safely delivered.
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        references=seen-{'text','statistic_id'}
        if ('statistic_id' in seen and references!={'metric_id'}) or 'text' not in seen or len(references)!=1:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        return next(iter(references))

    def _restore_reply_parts(self, args):
        if (not self._metric_selection_mode or not self._reply_parts_mode
                or 'text' in args or 'metric_selections' in args
                or ('amount_claims' in args and args['amount_claims'] != [])):
            raise BoundaryError('BOUNDARY_OUTPUT')
        parts = args['reply_parts']
        if not isinstance(parts, list) or not 1 <= len(parts) <= 64:
            raise BoundaryError('BOUNDARY_OUTPUT')
        restored = self._restore_answer({key: value for key, value in args.items()
                                         if key != 'reply_parts'})
        result = []
        reference=self._reply_parts_reference_field
        aliases=self._public_quote_fact_aliases if reference=='public_quote_fact_id' else self._metric_selection_aliases
        if reference not in ('metric_id','public_quote_fact_id'):
            raise BoundaryError('BOUNDARY_OUTPUT')
        for claim in args.get('claims',[]):
            if isinstance(claim,Mapping) and 'statistic_id' in claim:
                if (set(claim)!={'statistic_id'} or claim['statistic_id'] not in self._statistic_selection_aliases
                        or claim['statistic_id'] not in self._reverse):raise BoundaryError('BOUNDARY_OUTPUT')
        for part in parts:
            if not isinstance(part, Mapping) or set(part) not in ({'text'}, {reference}, *([{'statistic_id'}]if reference=='metric_id' else [])):
                raise BoundaryError('BOUNDARY_OUTPUT')
            if 'text' in part:
                if not isinstance(part['text'], str) or not 0 <= len(part['text']) <= 6000:
                    raise BoundaryError('BOUNDARY_OUTPUT')
                text = self._restore(part['text'])
                redactions_before = self._counts['redactions']
                self._text(text)
                if self._counts['redactions'] > redactions_before:
                    raise BoundaryError('BOUNDARY_OUTPUT')
                result.append({'text': text})
            else:
                selected='statistic_id' if 'statistic_id' in part else reference
                alias = part[selected]
                allowed=self._statistic_selection_aliases if selected=='statistic_id' else aliases
                if (not isinstance(alias, str) or not _ALIAS.fullmatch(alias)
                        or alias not in allowed or alias not in self._reverse):
                    raise BoundaryError('BOUNDARY_OUTPUT')
                result.append({selected: self._reverse[alias]})
        restored['reply_parts'] = result
        return restored

    def _limit_metric_selection_schema(self, schema):
        properties = schema.get('properties', {})
        selections = properties.get('metric_selections')
        claims = properties.get('amount_claims')
        if (not isinstance(selections, dict) or selections.get('type') != 'array'
                or type(selections.get('maxItems')) is not int
                or not 0 <= selections['maxItems'] <= _AMOUNT_CLAIM_LIMIT
                or not isinstance(claims, Mapping) or claims.get('type') != 'array'
                or type(claims.get('maxItems')) is not int or claims['maxItems'] != 0):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        item = selections.get('items')
        if (not isinstance(item, Mapping) or item.get('type') != 'object'
                or item.get('additionalProperties') is not False
                or set(item.get('properties', {})) != {'metric_id', 'occurrence_index'}
                or set(item.get('required', [])) != {'metric_id', 'occurrence_index'}):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        metric = item['properties']['metric_id']
        index = item['properties']['occurrence_index']
        if (not isinstance(metric, dict) or metric.get('type') != 'string'
                or not isinstance(index, Mapping) or index.get('type') != 'integer'
                or type(index.get('minimum')) is not int or index['minimum'] != 0):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        choices = metric.get('enum', [])
        if (not isinstance(choices, list) or any(not isinstance(value, str) for value in choices)
                or (selections['maxItems'] and not choices)):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        delivered = [value for value in choices if value in self._metric_selection_aliases]
        if delivered:
            metric['enum'] = delivered
        else:
            metric.pop('enum', None)
            selections['maxItems'] = 0

    def _restore_metric_selection_answer(self, args):
        if not self._metric_selection_mode:
            raise BoundaryError('BOUNDARY_OUTPUT')
        selections = args['metric_selections']
        if (not isinstance(selections, list) or len(selections) > _AMOUNT_CLAIM_LIMIT
                or ('amount_claims' in args and args['amount_claims'] != [])):
            raise BoundaryError('BOUNDARY_OUTPUT')
        restored = self._restore_answer({key: value for key, value in args.items()
                                         if key != 'metric_selections'})
        from .metric_claims import MetricClaimError, monetary_occurrences
        try:
            occurrences = monetary_occurrences(restored.get('text', '')) if selections else []
        except MetricClaimError:
            raise BoundaryError('BOUNDARY_OUTPUT') from None
        result = []
        for row in selections:
            if (not isinstance(row, Mapping) or set(row) != {'metric_id', 'occurrence_index'}
                    or not isinstance(row['metric_id'], str)
                    or not _ALIAS.fullmatch(row['metric_id'])
                    or row['metric_id'] not in self._metric_selection_aliases
                    or row['metric_id'] not in self._reverse
                    or type(row['occurrence_index']) is not int
                    or not 0 <= row['occurrence_index'] < len(occurrences)):
                raise BoundaryError('BOUNDARY_OUTPUT')
            result.append({'metric_id': self._reverse[row['metric_id']],
                           'occurrence_index': row['occurrence_index']})
        restored['metric_selections'] = result
        return restored

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
        if (self._private_parts_repair is not None
                and self._private_parts_repair['status'] in ('pending', 'failed')
                and name != 'answer_reviewed'):
            raise BoundaryError('BOUNDARY_OUTPUT')
        if (name == 'answer_reviewed' and self._private_parts_repair is not None
                and (self._private_parts_repair['status'] in ('pending', 'failed')
                     or {'base_sha256', 'replacements'} & set(args))):
            return self._restore_private_parts_repair(args)
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
        if name=='answer_reviewed' and ({'base_sha256','replacements'}&set(args)
                or self._public_quote_repair is not None and self._public_quote_repair['status']=='pending'):
            return self._restore_public_quote_repair(args)
        if name == 'answer_reviewed' and {'direct_answer','conditions','unknowns','public_quotes'}&set(args):
            return self._restore_public_quote_answer(args)
        if name == 'answer_reviewed' and 'reply_parts' in args:
            return self._restore_reply_parts(args)
        if name == 'answer_reviewed' and 'metric_selections' in args:
            return self._restore_metric_selection_answer(args)
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
        # Transport envelopes are top-level mappings. Lists of complete local
        # metric signatures still use the original restore_output contract;
        # unbound source references below remain rejected for every shape.
        if isinstance(output, Mapping):
            from .provider_evidence_transport import unpack_provider_evidence
            from .review_evidence import unpack_financial_evidence
            output = unpack_financial_evidence(
                unpack_provider_evidence(output), review=('draft' in output))
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

    def _reply_interaction(self, spec, *, project=False):
        """Validate visible choice text; option IDs are never business aliases."""
        try:
            normalized = validate_interaction(spec)
        except BankError:
            raise BoundaryError('BOUNDARY_OUTPUT') from None
        if normalized is None:
            return None

        def visible(value, maximum):
            redactions_before = self._counts['redactions']
            checked = self._text(value, maximum=maximum)
            if self._counts['redactions'] > redactions_before:
                raise BoundaryError('BOUNDARY_OUTPUT')
            return checked if project else value

        result = {'kind': normalized['kind'],
                  'prompt': visible(normalized['prompt'], 240)}
        if 'options' in normalized:
            result['options'] = [
                {'id': option['id'], 'label': visible(option['label'], 48)}
                for option in normalized['options']]
        # Aliasing may change lengths or collide; never truncate reviewer input.
        try:
            return validate_interaction(result)
        except BankError:
            raise BoundaryError('BOUNDARY_OUTPUT') from None

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
        if 'interaction' in restored:
            restored = dict(restored)
            restored['interaction'] = self._reply_interaction(restored['interaction'])
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
        elif 'ratio_claims' in restored:
            # Ratio-bearing envelopes retain the existing normalized path.
            restored = dict(restored)
            restored['text'] = unicodedata.normalize('NFKC', restored.get('text', ''))
        # Plain safe prose retains its original spelling. The review producer
        # and independent expected projection both check its exact safe aliases.
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
            if name == 'cashflow':
                from .read_completion import funding_assessment_view_days
                assessment_days = funding_assessment_view_days(packet.get('question'))
                if assessment_days is not None:
                    # The full original evidence remains; append the same
                    # typed inclusion/exclusion contract the primary received.
                    projected['data'].update(self._cashflow_scenario_sources(evidence['data'], assessment_days))
            result['evidence'].append({'id': projected.get('evidence_id'), 'tool': name,
                                       'data': projected['data'], 'source': self._text(evidence.get('source', ''), maximum=100)})
        # Do not send operation_view credentials/raw receipts to the reviewer.
        result['operations'] = self._project(packet.get('operations', []), 'operations') if 'operations' in _TASK_DOMAINS[self.task] else []
        result['calendar_widgets'] = self._project(packet.get('calendar_widgets', []), 'calendar') if 'calendar' in _TASK_DOMAINS[self.task] else []
        if 'financial_evidence_transport' in packet:
            if packet['financial_evidence_transport']!='catalog-v1':raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result['financial_evidence_transport']='catalog-v1'
        if 'public_evidence_limits' in packet:
            from .review_scope import validate_review_scope
            result['public_evidence_limits']=validate_review_scope(packet['public_evidence_limits'])
        if 'catalog_information' in packet:
            from local_knowledge.catalog_information import validate_snapshot_shape
            result['catalog_information']=validate_snapshot_shape(packet['catalog_information'])
        if 'presentation_review' in packet:
            if 'calendar' not in _TASK_DOMAINS[self.task]:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            from .presentation_consumer import project_review
            result['presentation_review']=project_review(self,packet['presentation_review'])
        if 'provider_review' in packet:
            if 'calendar' not in _TASK_DOMAINS[self.task]:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            from .provider_projection import project_provider_receipt
            result['provider_review']=project_provider_receipt(self,packet['provider_review'])
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
            if 'interaction' in draft:
                result['draft']['interaction'] = self._reply_interaction(draft['interaction'], project=True)
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
        if 'ratio_claims' in draft:
            result['draft']['ratio_claims']=self._ratio_claims(draft['ratio_claims'],
                source_text=draft.get('text'),target_text=result['draft'].get('text'))
        if 'ratio_contract' in packet:
            result['ratio_contract']=self._ratio_contract(packet['ratio_contract'],
                source_text=draft.get('text'),target_text=result['draft'].get('text'))
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
        self._counts['omitted'] += sum(k not in {'question', 'history', 'server_context', 'evidence', 'draft', 'monetary_evidence', 'amount_contract', 'operations', 'calendar_widgets', 'policy','presentation_review','provider_review','public_evidence_limits','catalog_information','ratio_contract','financial_evidence_transport'} for k in packet)
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

    def public_request(self, args, *, tool_name='knowledge_search', enforce_request_context=False):
        if tool_name not in ('knowledge_search', 'public_products') or not isinstance(args, Mapping):
            raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
        if type(enforce_request_context) is not bool:
            raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
        if (enforce_request_context and tool_name=='knowledge_search'
                and self._explicit_public_as_of is not None
                and args.get('as_of')!=self._explicit_public_as_of):
            raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
        allowed = {'query', 'product_id', 'share_class_id', 'channel_id', 'as_of', 'facets', 'limit'}
        if tool_name=='public_products':allowed.add('view')
        if set(args) - allowed:
            raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
        output = {}
        for key, value in args.items():
            if key=='view':
                if tool_name!='public_products' or value not in ('current_disclosure','historical_conditions'):
                    raise BoundaryError('BOUNDARY_PUBLIC_QUERY')
                output[key]=value
            elif key in ('product_id', 'share_class_id', 'channel_id'):
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

    def _history_qualifiers(self,value):
        from .history_binding import checked_request, CONTRACT
        from simulated_bank import BankError as HistoryBindingError
        if (not isinstance(value,Mapping) or value.get('contract')!=CONTRACT
                or value.get('reading_mode')!='historical_audit' or value.get('historical_only') is not True
                or value.get('current_evidence_eligible') is not False or value.get('execution_authority') is not False
                or type(value.get('historical_numeric_rendering_supported')) is not bool):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        try:requested=checked_request(dict(value.get('requested',{})))
        except (ValueError,TypeError,HistoryBindingError):raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE') from None
        identifier=self._project(requested['id'],'public',key='id',public=True)
        if identifier is _DROP or identifier!=requested['id']:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        return requested

    def _public_history_page_context(self,value):
        from .history_binding import checked_request
        from simulated_bank import BankError as HistoryBindingError
        if (value.get('source_scope')!='market' or value.get('parameters')!={'reading_mode':'historical_audit'}
                or value.get('reading_mode')!='historical_audit' or value.get('historical_only') is not True
                or value.get('current_evidence_eligible') is not False or value.get('execution_authority') is not False):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        try:requested=checked_request({'kind':value.get('kind'),'id':value.get('id'),
            'version':value.get('version'),'reading_mode':'historical_audit'})
        except (ValueError,TypeError,HistoryBindingError):raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE') from None
        fields=('kind','id','source_scope','version','label')
        result=self._project({k:value[k] for k in fields if k in value},'public',public=True)
        if result.get('id')!=requested['id'] or result.get('version')!=requested['version']:
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result.update(parameters={'reading_mode':'historical_audit'},reading_mode='historical_audit',
            historical_only=True,current_evidence_eligible=False,execution_authority=False)
        if value.get('request_mode') in ('reference_overview','reference_question'):
            result['request_mode']=value['request_mode']
        return result

    def _history_project(self,value):
        """Dedicated public citation identity/version projection, no global allowlist change."""
        if not isinstance(value,Mapping):raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result=self._project(value,'public',public=True)
        if not isinstance(result,dict):raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        def public_id(identifier):
            if not isinstance(identifier,str):raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            clean=self._project(identifier,'public',key='article_id',public=True)
            if clean is _DROP or clean!=identifier:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            return clean
        if 'article_ids' in value:
            ids=value['article_ids']
            if (not isinstance(ids,list) or not 1<=len(ids)<=48
                    or any(not isinstance(identifier,str) for identifier in ids) or len(set(ids))!=len(ids)):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result['article_ids']=[public_id(identifier) for identifier in ids]
        if 'article_versions' in value:
            versions=value['article_versions']
            if (not isinstance(versions,Mapping) or not 1<=len(versions)<=48
                    or ('article_ids' in value and set(versions)!=set(value['article_ids']))
                    or any(type(version) is not int or not 1<=version<=999999999 for version in versions.values())):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result['article_versions']={public_id(identifier):version for identifier,version in versions.items()}
        if 'event_version' in value:
            version=value['event_version']
            if type(version) is not int or not 1<=version<=999999999:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result['event_version']=version
        for field in ('article','report'):
            if field in value:result[field]=self._history_project(value[field])
        for field in ('supporting_events','source_articles','articles','citations'):
            if field not in value:continue
            items=value[field]
            if not isinstance(items,list) or len(items)>48:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result[field]=[self._history_project(item) for item in items]
        return result

    def _public_history_data(self,value):
        requested=self._history_qualifiers(value)
        local_keys={'receipt','generation','source_permissions','source_permission','snapshot_id',
                    'permission_revision','market_identity','registry_sha256','definition_sha256'}
        def contains_local(obj):
            if isinstance(obj,Mapping):
                return any(key in local_keys or contains_local(item) for key,item in obj.items())
            if isinstance(obj,(list,tuple)):return any(contains_local(item) for item in obj)
            return False
        if contains_local(value):raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        result=self._history_project(value)
        quotes=value.get('historical_quotes',[])
        contract=value.get('historical_quote_contract',[])
        if (type(quotes) is not list or type(contract) is not list or len(quotes)>3
                or len(quotes)!=len(contract)
                or value.get('historical_numeric_rendering_supported') is not bool(quotes)):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        if quotes or 'historical_quotes' in value or 'historical_quote_contract' in value:
            from dataclasses import asdict
            from .historical_quotes import check_raw_sensitive, numeric_roles
            public_quotes=[]
            public_contract=[]
            seen=set()
            fields={'quote_id','article_id','version','source_id','body_sha256','start','end','text',
                    'publisher','published_at','url','identity','numeric_roles'}
            for quote,item in zip(quotes,contract):
                if (type(quote) is not dict or set(quote)!={'quote_id','text'}
                        or type(item) is not dict or set(item)!=fields
                        or type(quote.get('quote_id')) is not str
                        or not re.fullmatch(r'hq_[0-9a-f]{32}',quote['quote_id'])
                        or quote['quote_id'] in seen or item['quote_id']!=quote['quote_id']
                        or quote['text']!=item['text'] or type(item['text']) is not str
                        or not 1<=len(item['text'])<=120
                        or type(item['version']) is not int or not 1<=item['version']<=999999999
                        or type(item['start']) is not int or item['start']!=0
                        or type(item['end']) is not int or item['end']!=len(item['text'])
                        or type(item['body_sha256']) is not str
                        or not re.fullmatch(r'[0-9a-f]{64}',item['body_sha256'])
                        or type(item['numeric_roles']) is not list
                        or item['numeric_roles']!=[asdict(role) for role in numeric_roles(item['text'])]):
                    raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                for field in ('text','publisher','published_at','url','identity','article_id','source_id'):
                    if type(item[field]) is not str or not item[field]:raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
                    check_raw_sensitive(self,item[field])
                seen.add(quote['quote_id'])
                public_quotes.append(copy.deepcopy(quote))
                public_contract.append(copy.deepcopy(item))
            result['historical_quotes']=public_quotes
            result['historical_quote_contract']=public_contract
        if value.get('status') not in ('historical_readable','history_unavailable'):
            raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        if value.get('status')=='historical_readable' and requested['kind']=='article':
            article=value.get('article')
            if (not isinstance(article,Mapping) or article.get('id')!=requested['id']
                    or article.get('version')!=requested['version'] or article.get('status')!='historical_readable'):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
            result['article']=self._project(article,'public',public=True)
        if value.get('status')=='historical_readable' and requested['kind']=='weekly':
            report=value.get('report')
            if (not isinstance(report,Mapping) or report.get('period')!=requested['id']
                    or report.get('version')!=requested['version'] or report.get('status')!='historical_readable'):
                raise BoundaryError('BOUNDARY_UNKNOWN_STRUCTURE')
        if value.get('status')=='history_unavailable':
            allowed={'id','period','version','title','title_cn','headline','publisher','source_id','identity','url',
                     'published_at','published_precision','reported_at','released_at','read_scope','link_status','access_status'}
            result={key:item for key,item in result.items() if key in {'status','reason','notice'}}
            metadata=value.get('metadata',{})
            result['metadata']=self._project({k:v for k,v in metadata.items() if k in allowed},'public',public=True)
        result.update(contract='navren.market-history-audit.v1',requested=requested,reading_mode='historical_audit',
            historical_only=True,current_evidence_eligible=False,execution_authority=False,
            historical_numeric_rendering_supported=bool(quotes))
        return result
