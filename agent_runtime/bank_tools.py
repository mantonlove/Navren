"""会话范围内的白名单工具；金额和权限由代码决定，不读取用户指定身份。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from decimal import Decimal, InvalidOperation
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
import uuid
from copy import copy, deepcopy
from datetime import datetime, timezone

from simulated_bank import BankError
from simulated_bank.agent_guard import context_hash
from simulated_bank.service import encoded
from base_runtime.reply_interaction import INTERACTION_SCHEMA, validate_interaction
from base_runtime.position_currency import position_currency
from .skills import catalog as skill_catalog, load_advisor
from .knowledge_retrieval import KnowledgeRetrieval
from .metric_claims import MetricRegistry, MetricClaimError, amount_claim_schema, monetary_occurrences
from .financial_mcp_client import FinancialMCPClient, FinancialMCPError, tool_specs
from .provider_presentations import ProviderPresentations
from .generated_code import TOOL_SPEC as GENERATED_CODE_SPEC
from .ratio_claims import RatioRegistry, RatioClaimError
from .read_completion import (ReadResultReferenceIncomplete, ReadResultPrecheckFatal,
    cash_query_kind, require_cash_query_complete,
                              complete_statement_facts)


def obj(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {}, 'required': list(required),
            'additionalProperties': False}


STR = {'type': 'string', 'minLength': 1, 'maxLength': 128}
MONEY = {'type': 'integer', 'minimum': 0, 'maximum': 100000000}


def array(items, maximum=32, minimum=0):
    return {'type': 'array', 'items': items, 'maxItems': maximum, 'minItems': minimum}


def validate(value, schema):
    kind = schema['type']
    if isinstance(kind,list):
        for option in kind:
            try:
                validate(value,{**schema,'type':option})
                return
            except BankError:
                continue
        raise BankError('INVALID_TOOL_ARGUMENTS','工具参数类型不合法。')
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'integer': type(value) is int,
             'boolean': type(value) is bool,'null':value is None}[kind]
    if not valid or ('enum' in schema and value not in schema['enum']):
        raise BankError('INVALID_TOOL_ARGUMENTS', '工具参数类型或枚举不合法。')
    if kind == 'object':
        properties=schema.get('properties',{})
        additional=schema.get('additionalProperties',False)
        if (set(schema.get('required',()))-set(value)
                or (set(value)-set(properties) and additional is False)):
            raise BankError('INVALID_TOOL_ARGUMENTS', '工具包含未知字段或缺少必要字段。')
        for key, item in value.items():
            if key in properties:validate(item,properties[key])
            elif isinstance(additional,dict):validate(item,additional)
    elif kind == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 64):
            raise BankError('INVALID_TOOL_ARGUMENTS', '工具列表长度不合法。')
        for item in value:
            validate(item, schema['items'])
    elif kind == 'string':
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 8000):
            raise BankError('INVALID_TOOL_ARGUMENTS', '工具文字长度不合法。')
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
            raise BankError('INVALID_TOOL_ARGUMENTS', '工具文字格式不合法。')
    elif kind == 'integer':
        if not schema.get('minimum', -10**15) <= value <= schema.get('maximum', 10**15):
            raise BankError('INVALID_TOOL_ARGUMENTS', '工具整数范围不合法。')


ACTION_FIELDS = {
    'action': {**STR, 'enum': ['purchase','redeem','transfer','schedule_transfer',
        'cancel_scheduled_payment','close_mandate','cancel_subscription','card_control',
        'report_card_lost','unblock_card','request_card_limit','apply_card','request_split',
        'collect_split_payment','reserve_funds','release_funds','book_services']},
    **{k: STR for k in ('account_id','product_id','mandate_id','card_id','beneficiary_id',
                       'reference','next_due_on','end_on','scheduled_payment_id','split_id',
                       'participant_id','subscription_id','task_id','expires_on','hold_id')},
    **{k: MONEY for k in ('amount_minor','requested_limit_minor','limit_minor','liquidity_floor_minor')},
    'enabled': {'type': 'boolean'},
    'frequency': {**STR, 'enum': ['once','monthly']},
    'control_type': {**STR, 'enum': ['online','overseas','contactless','single_limit']},
    'requested_type': {**STR, 'enum': ['debit','credit','virtual']},
    'quote_ids': array(STR, 10, 1),
}

SPECS = {
    'load_skill': ('按任务加载Navren维护的技能。需要分支细节时指定该技能返回的reference文件名；技能不授予执行权限。', obj({'name':STR,'reference':{**STR,'pattern':r'[a-z0-9][a-z0-9._-]*\.md'}}, ('name',))),
    'knowledge_status': ('查询公共RAG消费接口的真实接入状态；未接入不能声称已检索。对用户用中文简述接入和资料缺口，不展示内部字段、状态码或提供器术语。私有原始资料不通过此工具送往云模型。',obj()),
    'knowledge_search': ('从受信本地公共RAG提供器查证产品条款与知识。固定发布版本、回查原文引用及公开出站许可；未接入明确返回缺口。引用不能证明当前可买或授权交易，必须独立审核。',obj({
        'query':{'type':'string','minLength':1,'maxLength':500},
        'product_id':STR,'share_class_id':STR,'channel_id':STR,
        'as_of':{**STR,'pattern':r'\d{4}-\d{2}-\d{2}'},
        'facets':array({**STR,'enum':['fees','liquidity','risk','strategy','eligibility','sale_status','returns','general','terms']},8),
        'limit':{'type':'integer','minimum':1,'maximum':8}},('query',))),
    'market_hotspots': ('查询真实时钟下已核验的市场热点及公开来源覆盖；原始发布日期不等于抓取日，不推测持仓收益。',obj()),
    'market_status': ('用户询问市场数据从哪里来、覆盖范围、更新与接入状态时读取真实本地来源注册及最近采集状态，不将未接入来源说成已接入。',obj()),
    'market_event': ('按市场事件ID读取事实、Navren分析、条件和原文证据。历史精选须同时传edition_date、revision、event_version；已绑定页面引用时省略三参数会沿用同一天同版次，不能改读最新。撤下或修订只返回元信息。',obj({
        'event_id':STR,'edition_date':{**STR,'pattern':r'\d{4}-\d{2}-\d{2}'},
        'revision':{'type':'integer','minimum':1,'maximum':999999999},
        'event_version':{'type':'integer','minimum':1,'maximum':999999999}},('event_id',))),
    'market_article': ('按文章ID读取核验原文身份与短证据。metadata_only不能推断全文或深度摘要。',obj({'article_id':STR},('article_id',))),
    'market_weekly': ('读取最新或指定YYYY-MM-DD期号的已发布市场周报。真实市场时间独立于模拟银行，不把旧期当本周。',obj({'period':STR})),
    'calendar_query': ('查询共享财务日历并产生可交互组件。相对期间由后端业务时钟解析；日历预算不扣款；每次最多93天。',obj({
        'from':STR,'to':STR,'portfolio':STR,'accounting_basis':{**STR,'enum':['total_equity_v2','holdings_only_v1']},'view':{**STR,'enum':['month','week','day','agenda']},
        'source':{**STR,'enum':['simulation','imported']},'include_life_events':{'type':'boolean'},'simulation_overlay':{'type':'boolean'},
        'period':{**STR,'enum':['today','tomorrow','last_week','this_week','next_week','last_month','this_month','next_month']},
        'kinds':array({**STR,'enum':['transaction','investment_pnl','distribution','maturity','settlement','bill','subscription','birthday','life_event']},9)})),
    'wealth_product': ('查询真实基金行情与独立历史模拟组合的完整权益、收益、份额、费用假设和来源；只读，不是推荐或真实交易。必须明确组合，不与当前现金相加。',obj({'fund_id':STR,'on':STR,'portfolio':STR},('fund_id','on','portfolio'))),
    'wealth_versions': ('读取当前账户可用的组合版本和真实基金来源状态；当前渠道未知不可申购。',obj()),
    'calendar_day': ('读取已查询视图中某一天的最新详情，最多100条并明确分页。',obj({'view_id':STR,'date':STR,'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':100}},('view_id','date'))),
    'calendar_item': ('读取当前账户日历事项的最新状态和可用操作；后续资金操作仍需重新查银行对象和用户确认。',obj({'item_id':{**STR,'maxLength':300}},('item_id',))),
    'calendar_budget_scenario': ('仅当用户明确要把日历预算纳入资金规划时，计算独立用户计划情景；有效承诺和生效预留去重，不扣款或新建预留。最多93天。',obj({'account_id':STR,'from':STR,'to':STR},('account_id','from','to'))),
    'calendar_prepare_event': ('只准备日历元数据草案。创建/编辑/删除必须用户明确要求，不能代用户确认；修改周期需明确范围。不支持农历。',obj({
        'action':{**STR,'enum':['create','edit','delete']},'series_id':STR,'original_on':STR,'scope':{**STR,'enum':['occurrence','future','series']},
        'event':obj({'title':STR,'kind':{**STR,'enum':['birthday','life_event','bill','subscription']},'start_on':STR,
            'recurrence':{**STR,'enum':['once','weekly','monthly','yearly']},'month_policy':{**STR,'enum':['skip','last_day']},
            'budget_minor':{**MONEY,'type':['integer','null'],'description':'整数分；null清空计划预算并恢复未知。'},'currency':{**STR,'enum':['CNY','USD','EUR','GBP','JPY']},'remind_days':{'type':'integer','minimum':0,'maximum':366},
            'note':{'type':'string','maxLength':1000},'relation_label':STR,'end_on':STR,'linked_commitment_ids':array(STR,20),'linked_memory_ids':array(STR,10),
            'status':{**STR,'enum':['planned','completed','cancelled']}})},('action',))),
    'personal_graph': ('根据具体问题读取可重算个人财务子图，期间同币种计算，读取实际关系证据；未核实资料不作事实，关闭记忆后不读个性化资料。资金/成本/目标/流动性/风险四板块：cashflow收支、wealth财富、plans目标责任、preferences条件认知；overview总览。历史组合不得与当前现金混算。',obj({'view':{**STR,'enum':['overview','cashflow','wealth','plans','preferences','funds','cost','goals','liquidity','risk','records']},'question':STR,'from':STR,'to':STR,'source':{**STR,'enum':['simulation','imported']},'node_id':STR,'insight_id':STR,'edge_id':STR,'portfolio':STR,'accounting_basis':{**STR,'enum':['total_equity_v2','holdings_only_v1']},'expected_version':STR})),
    'graph_scenario': ('按用户明确要求计算独立规划情景，复用现金流时间轴与日历去重，不扣款、不预留、不预支工资。必须先personal_graph取得version。',obj({'account_id':STR,'expected_version':STR,'floor_minor':MONEY,'horizon_days':{'type':'integer','minimum':1,'maximum':93},'goals':array(obj({'goal_id':STR,'amount_minor':MONEY,'date':STR,'monthly_contribution_minor':MONEY},('goal_id','amount_minor','date')),10)},('account_id','expected_version'))),
    'remember_profile': ('仅在记忆开启且用户本轮明确陈述自己的偏好或目标时，记录待核实画像；quote必须逐字引用本轮原话。不保存第三人、提问或假设。',
                         obj({'category':{**STR,'enum':['goal','shopping','wealth','liquidity','life','communication']},
                              'value':{'type':'string','minLength':1,'maxLength':240},
                              'quote':{'type':'string','minLength':4,'maxLength':240}},('category','value','quote'))),
    'public_products': ('查询微众官网真实公开产品资料；资料日期过期不可用于最新推荐。收益等缺失字段为未知，不能执行真实交易。',
                        obj({'query':{'type':'string','maxLength':80},'view':{**STR,'enum':['current_disclosure','historical_conditions']}},())),
    'research_plans': ('读取本人保存的研究备选及实际产品回执覆盖。可按稳定plan_id读取一项；过期依据与部分执行分别说明，不能把产品覆盖当作金额或期限假设全部实现。只读，不能生成申购授权。', obj({'plan_id':STR})),
    'customer_records': ('读取借贷、保障、存款、支持工单、账户权限或个人财务目标的模拟档案。无真实贷款/保险执行能力。',
                        obj({'domain':{**STR,'enum':['loans','insurance','deposits','support','permissions','goals']}},('domain',))),
    'account_summary': ('查询已结算现金、各币种账户、持仓本金、待到账赎回和正式模拟画像；不包含信用额度作为现金。', obj()),
    'budget': ('当前可安排资金必须用本工具，不用cashflow.current_available代替。规划额外留款前，先对同账户调用不带reserves的budget读取已扣义务及真实evidence_id，再把同一义务的ID填入commitment_ids去重；空列表表示全新留款。floor_minor为应急底线；全部金额单位分。',
               obj({'account_id': STR, 'floor_minor': MONEY,
                    'reserves': array(obj({'label': STR, 'amount_minor': MONEY,
                                          'commitment_ids': array(STR)}, ('label','amount_minor','commitment_ids')), 8)}, ('account_id',))),
    'statement_source': ('查询当前账单来源。CSV账单只反映消费流水，不能当作银行可用余额。', obj()),
    'monthly_statement': ('查询YYYY-MM消费分类、收支和来源证据；账单结余不是账户余额。',
                          obj({'month': {**STR, 'pattern': r'^\d{4}-\d{2}$'}}, ('month',))),
    'annual_statement': ('查询指定年份报告及覆盖月份；不假设缺失月份为零。',
                         obj({'year': {'type': 'integer', 'minimum': 2016, 'maximum': 2100}}, ('year',))),
    'statement_details': ('查询某月某类别的逐笔账单。笔数/合计由本地完整筛选后计算；items仅当前页，最多12笔。沿用本人上一轮月份并重新读取；类别须与月报或本人原话一致。',
                          obj({'month': {**STR, 'pattern': r'^\d{4}-(0[1-9]|1[0-2])$'},
                               'category': {'type':'string','minLength':1,'maxLength':80},
                               'kind': {**STR, 'enum':['expense','income','refund','transfer','investment_purchase','investment_redemption','loan_repayment']},
                               'currency': {**STR, 'enum':['CNY','USD','EUR','GBP','JPY']},
                               'offset': {'type':'integer','minimum':0,'maximum':200000},
                               'limit': {'type':'integer','minimum':1,'maximum':12}}, ('month',))),
    'anomalies': ('读取规则识别的疑似重复和异常消费，候选不等于欺诈。', obj()),
    'periodic_charges': ('读取周期扣费候选；候选不等于已确认的商户订阅。', obj()),
    'cashflow': ('按账户和币种推演1至365天事项。available/minimum_available是已扣底线与预留的余量，与零比较判断缺口；current_available尚未扣未来到期事项，不是当前可安排资金，后者查询budget。缺少必要支出、收入稳定性与目标依据时，不能定性缓冲薄、厚或足够。未来流入零不证明无历史收入。',
                 obj({'days': {'type':'integer','minimum':1,'maximum':365}}, ('days',))),
    'money_calculation': ('纯假设金额计算，不读取账户也不代表真实余额；用于用户给出的教育示例。values_minor单位分，subtract为首项减其余，sum为合计，multiply为首项乘factor。',
                          obj({'operation':{**STR,'enum':['sum','subtract','multiply']},
                               'values_minor':array({'type':'integer','minimum':-100000000,'maximum':100000000},20,1),
                               'factor':{'type':'integer','minimum':1,'maximum':1200},
                               'currency':{**STR,'enum':['CNY','USD','EUR','GBP','JPY']}},
                              ('operation','values_minor','currency'))),
    'products': ('读取模拟产品、正式画像匹配和拒绝原因；无真实收益报价。此工具必触发独立审核。', obj()),
    'service_offer_rules': ('读取合成服务优惠、费用及资格规则；无真实银行报价。答复独立审核后可让用户进入工作台协商界面，自主确认申请。', obj()),
    'service_offer_preview': ('按本轮用户原话读取受限服务条件预览，由代码检查获准规则/条件/期限/代价。无参数，固定不披露个人记忆；只产生可替代的非金融预览，不提交申请。需本人在工作台另行确认；本轮重复调用复用同一预览。', obj()),
    'service_application_status': ('只读本人服务申请真实回执；不准备新预览、不接受申请，不把条件待核验状态说成优惠已兑现。', obj()),
    'business_records': ('按业务读取必要对象：payments含收款人/计划/AA，cards含卡片控制，subscriptions含代扣与商户合同，tasks含礼物任务/报价/预留，investments含持仓批次和订单。',
                         obj({'kind': {**STR,'enum':['payments','cards','subscriptions','tasks','investments']}}, ('kind',))),
    'operation_status': ('按原操作ID查询回执；网络异常必须查原操作，不能重新创建交易。',
                         obj({'operation_id': STR}, ('operation_id',))),
    'user_memory': ('仅在用户启用后读取其手动确认的财务记忆；不可替代正式余额或风险画像。', obj()),
    'prepare_operation': ('准备一项待审模拟操作，不执行。必须先查对象和当前账户。金额单位分；不要传幂等键或验证码。每轮至多一项；会替代旧待确认草案。复合任务第二项必须等第一项执行回执后重新准备。',
                          obj(ACTION_FIELDS, ('action','account_id'))),
    'withdraw_draft': ('撤回当前待审/待确认Agent草案，防止旧卡继续确认；不撤销已执行金融操作。',
                       obj({'operation_id': STR}, ('operation_id',))),
    'answer_facts': ('完成纯事实回答，只选本轮工具返回的fact_ids；不允许任何自由文本。若有产品/草案或需建议解释，使用answer_reviewed。',
                     obj({'fact_ids': array(STR, 24, 1),'widget_ids':array(STR,4)}, ('fact_ids',))),
    'answer_reviewed': ('完成解释、建议、澄清或操作提案，经GLM核验后展示。每项事实claim引用本轮真实evidence_ids；draft_ids只能引用本轮准备的草案。不得宣称已执行。',
                        obj({'text': {'type':'string','minLength':1,'maxLength':6000},
                             'claims': array(obj({'text': {'type':'string','minLength':1,'maxLength':1200},
                                                 'evidence_ids': array(STR, 20, 1)}, ('text','evidence_ids')), 48),
                             'draft_ids': array(STR, 1),
                             'followups': array({'type':'string','minLength':1,'maxLength':120}, 3)},
                            ('text','claims','draft_ids','followups'))),
}
SPECS['answer_reviewed'][1]['properties']['widget_ids']=array(STR,4)
SPECS['answer_reviewed'][1]['properties']['interaction']=deepcopy(INTERACTION_SCHEMA)
SPECS['answer_reviewed'][1]['properties']['amount_claims']=array(amount_claim_schema(),24)
SPECS['answer_reviewed']=(SPECS['answer_reviewed'][0]+' 需要用户确认或补充选择时提供interaction：confirmation只含kind/prompt；single_choice另含2至4个options(id/label)。交互不授权金融执行，不能与操作或日历草案混用；问题和选项不填写金融金额，有交互时followups为空。 每个个人金融金额片段在amount_claims中复制本轮metrics的完整绑定签名并填text；同数其他对象或期间不构成依据。无对应metrics时只作缺项说明或answer_facts，不能自造metric_id。',SPECS['answer_reviewed'][1])
_PRESENTATION_MEASURES = ['recorded_net_minor','income_minor','expense_minor','refund_minor',
    'net_expense_minor','operating_surplus_minor','confirmed_due_minor','candidate_due_minor',
    'user_budget_minor','confirmed_balance_minor','candidate_balance_minor',
    'closing_equity_minor','interval_pnl_minor','arrangeable_now']
SPECS.update({
    'presentation_query': ('通过已注入的Base服务发行一份只读展示及完整签名原批次；来源、缺项、条件和全部值槽共审。相对期间用业务时钟；正文金额只能用所选展示原批次，暂不支持比例正文。没有提供器明确不可用。', obj({
        'intent':{**STR,'enum':['calendar','statement','cashflow','wealth','funds']},
        'days':{'type':'integer','minimum':1,'maximum':365},
        'section':{**STR,'enum':['summary','obligations','holds']},
        'from':STR,'to':STR,'period':{**STR,'enum':['today','tomorrow','last_week','this_week','next_week','last_month','this_month','next_month','this_year','last_year']},
        'source':{**STR,'enum':['simulation','imported']},'currency':{**STR,'enum':['CNY']},
        'portfolio':STR,'account_id':STR,'accounting_basis':{**STR,'enum':['total_equity_v2','cash_records_v1','funds.current_budget/1']},
        'granularity':{**STR,'enum':['day','week','month','year','category','valuation']},
        'measure':{**STR,'enum':_PRESENTATION_MEASURES},
        'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':12},
        'kinds':array({**STR,'enum':['transaction','investment_pnl','distribution','maturity','settlement','bill','subscription','birthday','life_event']},9),
        'include_life_events':{'type':'boolean'},'layout':{**STR,'enum':['month_grid','day_cards','grouped_bar','category_bar','step_line','time_line','funding_blocks','funding_items']}})),
    'presentation_detail': ('仅引用本轮实际发行view_id和point_ref/item_ref或视图内完整日期范围，发行新的子展示；旧view或旧审核不能跨请求复用。终结只选一个本轮widget，额外followups留空，保留Base本轮实际发行的全部建议（也可能没有）。', obj({
        'view_id':STR,'selector':obj({'point_ref':STR,'item_ref':STR,'from':STR,'to':STR,
            'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':8}})},('view_id','selector'))),
})
# Provider currently permits no ratio body; an empty array is protocol compatibility only.
SPECS['answer_reviewed'][1]['properties']['ratio_claims']=array(obj(),0)
SPECS['answer_reviewed'][1]['properties']['historical_quote_ids']=array(STR,3)
FINANCIAL_MCP_TOOL_NAMES = frozenset(tool_specs())
SPECS.update(tool_specs())
SPECS['run_generated_code']=GENERATED_CODE_SPEC


def native_tools(facts=(), evidence=(), drafts=(), identifiers=None, widgets=(), metrics=(), *,
                 financial_mcp_tools=None, historical_quote_ids=None, metric_selections=False,
                 public_quote_fact_ids=(), statistic_ids=()):
    # JSON round-trip breaks shared schema dicts (STR/MONEY). deepcopy preserves
    # aliasing and would accidentally put participant IDs into account_id enums.
    if type(metric_selections) is not bool:raise ValueError('metric_selection_mode_must_be_host_boolean')
    specs = json.loads(json.dumps(SPECS))
    if historical_quote_ids is None:
        specs['answer_reviewed'][1]['properties'].pop('historical_quote_ids')
    else:
        quote_schema=specs['answer_reviewed'][1]['properties']['historical_quote_ids']
        if historical_quote_ids:quote_schema['items']['enum']=list(historical_quote_ids)
        else:quote_schema['maxItems']=0
        specs['answer_reviewed'][0]+=' 历史数字仅选择本轮历史工具发行的historical_quote_ids；text/claims不抄写或改写数字，宿主将逐字组装受信原文，并独立审核。历史数字不具备当前账户、推荐或交易资格。'
    if financial_mcp_tools is not None:
        enabled = set(financial_mcp_tools)
        specs = {name: spec for name, spec in specs.items()
                 if name not in FINANCIAL_MCP_TOOL_NAMES or name in enabled}
    specs['load_skill'][1]['properties']['name']['enum'] = [item['name'] for item in skill_catalog()]
    specs['answer_reviewed'][1]['properties']['amount_claims']['items']=amount_claim_schema(metrics)
    if metrics:
        specs['answer_reviewed'][1]['required'].append('amount_claims')
        specs['answer_reviewed'][0]+=(' 本轮已有正式metrics，amount_claims必填。先完成text，再逐字复制正文中每次个人金额的连续片段，'
            '并复制对应metrics的完整签名和kind；普通claims不能替代amount_claims。无个人金额也显式提交amount_claims=[]；'
            '重复片段分别提交准确occurrence_start，不省略用户所问的必需金额。')
        specs['run_generated_code'][1]['properties']['metric_id']['enum']=list(metrics)
    else:
        specs.pop('run_generated_code')
        specs['answer_reviewed'][1]['properties']['amount_claims']['maxItems']=0
    if metric_selections and metrics:
        schema=specs['answer_reviewed'][1]
        schema['properties'].pop('text')
        schema['properties']['reply_parts']=metric_reply_parts_schema(metrics, statistic_ids)
        schema['properties']['amount_claims']=array(obj(),0)
        schema['required']=[key for key in schema['required'] if key not in ('text','amount_claims')]
        schema['required'].append('reply_parts')
        specs['answer_reviewed'][0]=specs['answer_reviewed'][0].split(' 每个个人金融金额片段在amount_claims中复制',1)[0]
        specs['answer_reviewed'][0]+=METRIC_PARTS_NOTICE
    elif metric_selections and public_quote_fact_ids:
        schema=specs['answer_reviewed'][1]
        schema['properties'].pop('text')
        schema['properties'].pop('interaction',None)
        schema['properties'].update(public_quote_answer_fields(public_quote_fact_ids))
        schema['properties']['amount_claims']=array(obj(),0)
        schema['required']=[key for key in schema['required'] if key not in ('text','amount_claims')]+['direct_answer','conditions','unknowns']
        specs['answer_reviewed'][0]=specs['answer_reviewed'][0].split(' 每个个人金融金额片段在amount_claims中复制',1)[0]
        specs['answer_reviewed'][0]+=PUBLIC_QUOTE_PARTS_NOTICE
        from .public_fee_statement_contract import source_rows_from_evidence, build_catalog, restrict_schema, extend_time_catalog
        fee_sources=source_rows_from_evidence(evidence)
        if fee_sources is not None:
            restrict_schema(schema,extend_time_catalog(build_catalog(*fee_sources),evidence))
            specs['answer_reviewed'][0]+=(' 本轮费用陈述采用来源绑定的有限文字目录；所有text和claims.text完整选择各自enum中的句子，'
                '不能追加、拼接改写或另造费用关系。每个正文或条件句必须有同文claims，evidence_ids精确指向支持该句的来源；'
                '直接回答至少含一个规范事实或结论句，不能只有引文。unknowns仅用受控未知句，不选引用ID。'
                'followups为空或选择受控问句；仍需完整回答问题，原一次修复、预算和完整独立审核不变。')
    for name in ('answer_facts','answer_reviewed'):
        if widgets: specs[name][1]['properties']['widget_ids']['items']['enum']=list(widgets)
        else: specs[name][1]['properties']['widget_ids']['maxItems']=0
    for name, key, values in [('answer_facts','fact_ids',facts), ('answer_reviewed','draft_ids',drafts)]:
        if values:
            specs[name][1]['properties'][key]['items']['enum'] = list(values)
    if evidence:
        specs['answer_reviewed'][1]['properties']['claims']['items']['properties']['evidence_ids']['items']['enum'] = list(evidence)
    if metric_selections and metrics and statistic_ids:
        claim=specs['answer_reviewed'][1]['properties']['claims']['items']
        specs['answer_reviewed'][1]['properties']['claims']['items']={'oneOf':[claim,statistic_reference_schema(statistic_ids)]}
        specs['answer_reviewed'][0]+=' 账户统计仅显式选择本轮statistic_id原子；可在reply_parts或独立claims项引用。它只表达已核验统计口径，不代表同一笔交易回执。'
    commitment_ids = sorted((identifiers or {}).get('commitment_id', ()))
    if commitment_ids:
        specs['budget'][1]['properties']['reserves']['items']['properties']['commitment_ids']['items']['enum'] = commitment_ids
    for _, schema in specs.values():
        for field, choices in (identifiers or {}).items():
            if field in schema['properties'] and choices:
                schema['properties'][field]['enum'] = sorted(choices)
    return [{'type':'function','function':{'name': name,'description': description,'parameters': schema}}
            for name, (description, schema) in specs.items()]


METRIC_SELECTION_NOTICE = (' 本轮金额使用metric_selections，每项只填本轮已交付metric_id与occurrence_index。'
    '先完成text，再按正文所有数字加币种的金额出现顺序从0编号；相同金额每次出现分别选择，完整公开原文中的金额也计入编号，'
    '但其合法公开引文无需个人选择。每个个人金额均须选择准确的对象、期间与口径对应的指标。'
    '后端保留正文并从本轮原指标复制完整签名、kind及精确文字位置；模型不填写日期、来源、text片段或offset。'
    '没有个人金额时显式提交metric_selections=[]；不自造ID、不漏选、不用普通claims替代、不省略用户所问的必需金额。')


METRIC_PARTS_NOTICE = (' 本轮有正式metrics时，终稿用reply_parts按顺序组合回复，每片只能是{text:普通文字}或{metric_id:本轮已交付指标ID}。'
    '金额只使用metric_id片，宿主按原指标数值和币种生成文字及完整签名；不填金额文字、日期签名、来源表、位置编号或offset。'
    '个人金额写成0、零或省去货币单位仍是金额，不能留在text。修稿反馈列出的每一个text索引都须逐一处理，不能只改其中一项。'
    'text片可写说明、非金额期间和标点，不手写金融金额；claims仅引用真实证据并说明其支持的事实。'
    '个人金额的claims由metric_id片沿原指标自动展开；claims.text只写非金额事实，不再复述金额。'
    '例如“当前可安排资金为”作为text片，后接对应metric_id片；不要先写含数字的完整句子再只改其中一处。'
    '只报原问题必需的金额并简短解释对应口径和缺项；无对应指标不口算派生金额，不得省略用户所问的必需信息避开校验。'
    '没有正式metrics且未提供public_quote_fact_id合同时沿原text终稿合同，公开金额仍仅允许完整原文引述。')


def statistic_reference_schema(statistic_ids):
    return obj({'statistic_id':{'type':'string','minLength':1,'maxLength':256,
        'enum':sorted(set(statistic_ids))}},('statistic_id',))


def metric_reply_parts_schema(metric_ids, statistic_ids=()):
    metric={'type':'string','minLength':1,'maxLength':256,'enum':sorted(set(metric_ids))}
    return {'type':'array','minItems':1,'maxItems':64,'items':{'oneOf':[
        obj({'text':{'type':'string','minLength':0,'maxLength':6000}},('text',)),
        obj({'metric_id':metric},('metric_id',))]+([statistic_reference_schema(statistic_ids)]if statistic_ids else [])}}


PUBLIC_QUOTE_PARTS_NOTICE = (' 本轮公开引文答复由text与本轮可信引文共同完整回答当前用户问题；text须直接表达有据的非货币结论、关键非金额条件和未核实边界，必需金额及阈值由绑定引文完整展开，text不得重复金额；不能只介绍资料或只堆引文。'
    '必须提交direct_answer、conditions、unknowns三个命名分区，不能用text、reply_parts或尾部public_quotes。'
    'direct_answer为text或public_quote_fact_id片数组，必须有非空模型文字；conditions和unknowns为片数组列表，每个内层数组是一条条件或缺项，外层可明确为空，分别最多6条。'
    '各片只含text或本轮已交付public_quote_fact_id一个字段；text写完整、有据的非金额结论与条件，必需的公开货币金额及阈值使用对应完整原文引用片，不抄写到text。'
    '百分比本身不是货币金额；不得给原文裸数字补造货币单位，不口算或换算转述。不能省略用户所问必需金额或适用条件来避开校验。'
    '仅在需要呈现必需货币金额或阈值的分区引用原文；同一完整原文无需在多个分区重复，其他分区可用非金额文字说明条件和缺口、不选引文。'
    '本轮由宿主按原完整引述规则展开所选ID并补入claims；模型不再手工重复复制同一引文。'
    '宿主在引用位置完整展开原fact.text的NFKC显示规范并保留换行，不再在尾部重复添加引文。'
    '三个分区合计最多64个模型片、6次引文引用，重复引用也计数；direct_answer模型文字合计最多1200字，每条条件或缺项的模型文字最多600字。'
    '完整展开后的全部正文仍受原6000字限制，不截断或删除证据；不猜定义、个人资格或未披露条件。'
    '宿主只组装你所写文字及原件，不替你下结论。全部原校验和GLM仍核对完整问题、来源和语义完成度。')


def public_quote_answer_fields(fact_ids):
    def parts(maximum):
        schema=deepcopy(public_quote_reply_parts_schema(fact_ids))
        schema['items']['oneOf'][0]['properties']['text']['maxLength']=maximum
        return schema
    direct=parts(1200)
    direct['description']='text与本轮可信引文共同完整回答当前问题；text须有非空、直接且有据的非货币结论，模型文字合计最多1200字；必需金额及阈值用绑定引文片，不抄写到text。'
    return {'direct_answer':direct,
        'conditions':{**array(parts(600),6),'description':'每条条件是片数组，模型文字合计最多600字；无已证相关条件时外层为空数组。'},
        'unknowns':{**array(parts(600),6),'description':'每条缺项是片数组，模型文字合计最多600字；未核实不等于不存在，可明确提交空数组。'}}


def public_quote_reply_parts_schema(fact_ids):
    reference={'type':'string','minLength':1,'maxLength':256,'enum':sorted(set(fact_ids))}
    return {'type':'array','minItems':1,'maxItems':64,'items':{'oneOf':[
        obj({'text':{'type':'string','minLength':0,'maxLength':6000,
            'description':'完整非货币结论、资格与渠道条件及未知边界；必需货币金额和阈值用本轮public_quote_fact_id片，由宿主展开完整原文与同源claims。百分比本身非货币，不给裸数字补币种、不换算、不猜定义。'}},('text',)),
        obj({'public_quote_fact_id':reference},('public_quote_fact_id',))]}}


def metric_selection_schema(metric_ids=()):
    metric={'type':'string','minLength':1,'maxLength':256}
    if metric_ids:metric['enum']=sorted(set(metric_ids))
    return array(obj({'metric_id':metric,'occurrence_index':{'type':'integer','minimum':0}},
                     ('metric_id','occurrence_index')),24 if metric_ids else 0)


def metric_selection_answer_schema(metric_ids=()):
    schema=deepcopy(SPECS['answer_reviewed'][1])
    schema['properties']['metric_selections']=metric_selection_schema(metric_ids)
    schema['properties']['amount_claims']['maxItems']=0
    return schema


def safe_data(value):
    if isinstance(value, list):
        return [safe_data(v) for v in value]
    if isinstance(value, dict):
        return {k: safe_data(v) for k,v in value.items() if k not in {
            'workspace_id','customer_id','details','confirmation_digest','verification_code',
            'verification_token','token','csrf','pin_hash','agent_token_hash','token_hash',
            'verification_hash','idempotency_key','masked_mobile','masked_account'}}
    return value


LABELS = {'balance_minor':'已结算余额','cash_minor':'账户现金','reserve_minor':'应急保留',
          'result_minor':'假设计算结果',
          'upcoming_due_minor':'30天已知到期款','held_minor':'生效资金预留',
          'investable_minor':'当前可安排资金','holding_principal_minor':'持仓本金',
          'pending_redemption_minor':'待结算赎回','income':'收入','expense':'消费支出',
          'refund':'退款','income_expense_surplus_minor':'账单收支结余',
          'cash_delta_minor':'账单现金变动','transaction_count':'流水条数',
          'transfer_count':'转账事件笔数','ledger_row_count':'转账账务行数',
          'risk_level':'正式模拟风险等级','horizon_days':'最长投资期限（天）',
          'projected_available_after_confirmed_minor':'期末可用（已确认事项）',
          'baseline_investable_minor':'未追加留款的当前可安排资金',
          'current_available_minor':'尚未扣未来事项的底线外余量',
          'minimum_available_after_confirmed_minor':'最低底线外余量（已确认事项）',
          'minimum_available_if_candidates_occur_minor':'最低底线外余量（若候选发生）',
          'additional_reserve_minor':'额外去重留款','term_days':'锁定天数',
          'redemption_delay_days':'赎回到账天数','status':'状态','merchant_status':'商户合同状态'}


def money(minor, currency='CNY'):
    if type(minor) is not int:
        raise ValueError('amount_not_integer')
    sign = '-' if minor < 0 else ''
    major, fraction = divmod(abs(minor), 100)
    return f'{sign}{major:,}.{fraction:02d}'+('元' if currency=='CNY' else ' '+currency)


def monetary_literals(text):
    currencies={'元':'CNY','圆':'CNY','人民币':'CNY','RMB':'CNY',
                '美元':'USD','欧元':'EUR','英镑':'GBP','日元':'JPY'}
    # 先捕获整个数字串再验证，不能把“1.2.3元”末尾的“3元”当作合法金额。
    for number, scale, unit in re.findall(r'([-+]?[\d.,]+)\s*(万亿|亿|万)?\s*(人民币|美元|欧元|英镑|日元|元|圆|CNY|RMB|USD|EUR|GBP|JPY)',text,re.I):
        try:
            if not re.fullmatch(r'[-+]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?',number):
                raise ValueError('invalid_number_format')
            minor=Decimal(number.replace(',',''))*100*{'':1,'万':10000,'亿':100000000,'万亿':1000000000000}[scale]
            if minor!=minor.to_integral_value():raise ValueError('fractional_minor')
        except (InvalidOperation,ValueError):
            raise BankError('INVALID_MONEY_TEXT','答复中的金额精度或格式无效。')
        yield int(minor),currencies.get(unit.upper(),unit.upper())


def market_agent_view(name, data):
    """模型只读投影：保留事实、引文、原始日期和来源，省掉后台审核/排名重复数据。"""
    if name == 'market_status':
        return data
    omit={'review','ranking_reason','input_hash','generated_at','released_at',
          'article_versions','schema_version','content_version'}
    metadata={'id','period','version','status','availability','headline','title','publisher',
        'published_at','reported_at','category','theme','archive','latest_pointer','origin',
        'url','source_id','identity','read_scope','link_status','published_precision',
        'access_status','edition_date','revision','selected_at','primary_article_id'}
    def project(value):
        if isinstance(value,list):return [project(v) for v in value]
        if not isinstance(value,dict):return value
        article='url' in value and 'read_scope' in value
        event='articles' in value and ('facts' in value or 'availability' in value)
        inactive=(event and (value.get('availability','active')!='active'
                             or value.get('status')!='published'))
        inactive_report='citations' in value and value.get('status')!='published'
        if inactive or inactive_report:
            result={k:project(v) for k,v in value.items() if k in metadata}
            # 旧归档的文章身份可以展示，旧引文和旧事实不能成为本轮证据。
            articles=value.get('articles',value.get('source_articles',[]))
            result['articles']=[{k:project(v) for k,v in a.items() if k in metadata}
                                for a in articles]
            result['facts']=[]
            result['notice']='资料当前不可用于事实分析，请先查阅修订或撤回状态。'
            return result
        return {k:project(v) for k,v in value.items() if k not in omit
                and not (article and (value.get('status')!='active'
                    or value.get('read_scope')!='full_text' or value.get('link_status')!='verified')
                    and k in ('evidence_excerpt','excerpt_notice'))
                and not (article and name in ('market_hotspots','market_weekly') and k in ('evidence_excerpt','excerpt_notice','fetched_at','checked_at','content_updated_at','evidence_truncated'))}
    result=project(data)
    if name=='market_event' and isinstance(data.get('archive'),dict):
        # Archive identity is one narrow receipt. Keep its article versions
        # without restoring duplicated version maps elsewhere in the view.
        archive=data['archive']
        archive_fields={'edition_date','revision','event_version','origin','primary_article_id',
                        'selected_at','position'}
        result['archive']={k:deepcopy(v) for k,v in archive.items() if k in archive_fields}
        if 'article_versions' in archive:
            versions=archive['article_versions']
            if (not isinstance(versions,dict) or len(versions)>48 or any(not isinstance(k,str)
                    or type(v) is not int or v<1 for k,v in versions.items())):
                raise BankError('INVALID_MARKET_EVIDENCE','归档文章版本映射无效。',503)
            result['archive']['article_versions']=deepcopy(versions)
    if name=='market_weekly':
        if result.get('report'):result['report'].pop('reading',None)  # 同一批source_articles已有身份和链接。
        result['supporting_events']=[{k:v for k,v in event.items()
            if k in ('id','status','version','published_at','facts','articles','uncertainty')}
            for event in result.get('supporting_events',[])]
    if 'coverage' in result:
        result['coverage']={k:v for k,v in result['coverage'].items()
                            if k in ('state','checked_at','timezone','updating','limitations')}
    result['queried_at']=datetime.now(timezone.utc).isoformat()
    result['market_timezone']='Asia/Shanghai'
    if name=='market_hotspots':result['window_days']=7
    return result


_ORIGINAL_AUDIT_REQUEST = object()


@dataclass(frozen=True)
class _HostSecurityInvocation:
    bank: object
    workspace: str
    request_id: object
    event_id: str
    operation_ids: tuple


_NORMAL_SECURITY_REASONS = {
    'TOOL_FORBIDDEN':'TOOL_NOT_ALLOWED', 'TOOL_SCOPE_REQUIRED':'TOOL_NOT_ALLOWED',
    'REQUEST_PRIVATE_DATA_FORBIDDEN':'AUTHORIZATION_DENIED',
    'INVALID_TOOL_ARGUMENTS':'TOOL_ARGUMENTS_INVALID', 'INVALID_FIELDS':'TOOL_ARGUMENTS_INVALID',
    'CONFIRMATION_REQUIRED':'USER_AUTHORIZATION_DECLINED',
    'USER_AUTHORIZATION_DECLINED':'USER_AUTHORIZATION_DECLINED',
    'AUTHORIZATION_DECLINED':'USER_AUTHORIZATION_DECLINED',
    'NOT_FOUND':'DATA_UNAVAILABLE', 'DATA_UNAVAILABLE':'DATA_UNAVAILABLE',
    'OBJECT_NOT_OBSERVED':'DATA_UNAVAILABLE', 'READ_RESULT_REQUIRED':'DATA_UNAVAILABLE',
    'PROVIDER_UNAVAILABLE':'SUPPLIER_UNAVAILABLE', 'MODEL_UNAVAILABLE':'SUPPLIER_UNAVAILABLE',
    'MCP_UNAVAILABLE':'SUPPLIER_UNAVAILABLE', 'CONFIGURATION_REQUIRED':'SUPPLIER_UNAVAILABLE',
}


def _host_security_rejection(gateway,workspace,error,*,request_id,event_id,phase='tool',
                             operation_ids=(),scope_active=False,reuse_marker=True):
    """Only the trusted host calls this; never registered in SPECS/native_tools."""
    marker=getattr(error,'_navren_host_security_invocation',None)
    if (reuse_marker and type(marker) is _HostSecurityInvocation and marker.bank is gateway.bank
            and marker.workspace==workspace and marker.request_id==request_id):
        if marker.event_id!=event_id:scope_active=False
        event_id,operation_ids=marker.event_id,marker.operation_ids
    events=gateway.security_events
    scope=nullcontext() if scope_active else events.request(workspace,
        request_id=request_id,event_id=event_id,phase=phase)
    with scope:
        for operation in operation_ids or (None,):
            reason=_NORMAL_SECURITY_REASONS.get(error.code) if isinstance(error,BankError) else None
            if not isinstance(error,BankError):
                from .data_boundary import BoundaryError
                if isinstance(error,BoundaryError):reason='AUTHORIZATION_DENIED'
            if reason:events.observe(workspace,reason,operation_id=operation)
            else:events.rejected(workspace,error,operation_id=operation)
    error._navren_host_security_invocation=_HostSecurityInvocation(
        gateway.bank,workspace,request_id,event_id,tuple(operation_ids))


class BankTools:
    def __init__(self, gateway, workspace, request_id, *, private_context_disabled=False, language='zh', request_privacy=None, metric_selections=False, audit_request_id=_ORIGINAL_AUDIT_REQUEST):
        if type(private_context_disabled) is not bool:
            raise ValueError('private_context_policy_must_be_host_boolean')
        from .request_privacy import RequestPrivacyPolicy
        if request_privacy is not None and (type(request_privacy) is not RequestPrivacyPolicy
                or request_privacy.private_context_disabled != private_context_disabled):
            raise ValueError('private_context_policy_must_match_host_policy')
        if type(metric_selections) is not bool:raise ValueError('metric_selection_mode_must_be_host_boolean')
        self.metric_selection_mode=metric_selections
        self.metric_selection_receipts=[]
        self.metric_part_receipts=[]
        self._joint_source_plan=None
        self._fee_delivery_context=None
        self._fee_statement_args=None
        self._fee_statement_rendered=None
        self.joint_source_receipts=[]
        self.request_privacy = request_privacy or RequestPrivacyPolicy(private_context_disabled)
        self.private_context_disabled = private_context_disabled
        self.g, self.w, self.request_id = gateway, workspace, request_id
        original=request_id if audit_request_id is _ORIGINAL_AUDIT_REQUEST else audit_request_id
        self._audit_request_id=(original if type(original) is str
            and re.fullmatch('[a-f0-9]{32}',original) else None)
        # A missing original ID stays null; uniqueness belongs only to event_id.
        self._audit_invocation_ref=uuid.uuid4().hex
        self._audit_call_index=0
        self._audit_active=None
        self._audit_operations=[]
        self.generated_code_session=None
        self.request_deadline=None
        self.financial_mcp = None
        self.financial_mcp_unavailable = 'CONFIGURATION_REQUIRED'
        if not private_context_disabled:
            try:
                self.financial_mcp = FinancialMCPClient.from_workspace(gateway, workspace)
                self.financial_mcp_unavailable = None
            except FinancialMCPError as error:
                self.financial_mcp_unavailable = error.code
        self.financial_mcp_tool_names = (self.financial_mcp.tool_names if self.financial_mcp else ())
        if not private_context_disabled:self.g.wealth.provision(workspace)
        self.evidence, self.facts, self.drafts, self.trace = {}, {}, {}, []
        self.advice, self.changed = False, False
        self.start_context = self.current_context()
        from user_memory import privacy_revision
        with closing(self.g.bank.connect()) as db:self.privacy_revision=privacy_revision(db,self.w)
        self.cashflow_timeline = None
        self.current_budget = None
        self.budget_baselines = {}
        self.identifiers = {}
        self.user_text = ''
        self.income_statement_query = None
        self.statement_fact_query = None
        self.metrics=MetricRegistry()
        self.ratios=RatioRegistry()
        self.presentations=(ProviderPresentations(gateway,workspace,request_id,language=language)
            if not private_context_disabled and getattr(gateway,'presentation_provider',None) is not None else None)
        self.metric_batches=[]
        from .typed_statistics import TypedStatisticSession
        self._statistic_session=TypedStatisticSession(self)
        self.service_preview=None
        self._service_snapshot=None
        self.widgets,self.calendar_drafts={},[]
        self.calendar_edit_attempted=False
        self.market_sources={}
        self.market_context=None
        self.market_reads={}
        self._history_reference=None
        self._history_binding=None
        self._history_snapshot=None
        self._history_render_literals=()
        self.history_quote_session=None
        self.history_quote_proof=None
        self._history_boundary=None
        self._default_market=None
        # 提供器只由本地应用代码注入，用户和模型没有配置索引、路径或身份的工具。
        self.knowledge = KnowledgeRetrieval(getattr(gateway, 'knowledge_provider', None))
        if not private_context_disabled:
            with closing(self.g.bank.connect()) as db:
                ids=[r[0] for r in db.execute("SELECT id FROM calendar_series WHERE workspace_id=? AND status='active' ORDER BY updated_at DESC LIMIT 20",(self.w,))]
            if ids:self.identifiers['series_id']=set(ids)

    def remember_identifiers(self, name, data):
        roots = {'products':'product_id'}
        collections = {'accounts':'account_id','beneficiaries':'beneficiary_id','cards':'card_id',
                       'mandates':'mandate_id','subscriptions':'subscription_id','scheduled_payments':'scheduled_payment_id',
                       'split_bills':'split_id','split_participants':'participant_id','orchestration_tasks':'task_id',
                       'fund_holds':'hold_id','service_quotes':'quote_id','items':'item_id','preview':'item_id'}
        def walk(value, collection=None):
            if isinstance(value,list):
                for item in value:walk(item,collection)
            elif isinstance(value,dict):
                if collection and isinstance(value.get('id'),str):
                    self.identifiers.setdefault(collection,set()).add(value['id'])
                for key,item in value.items():
                    if key.endswith('_id') and (key in ACTION_FIELDS or key in ('series_id','view_id','item_id')) and isinstance(item,str):
                        self.identifiers.setdefault(key,set()).add(item)
                    if key == 'commitments' and isinstance(item, list):
                        self.identifiers.setdefault('commitment_id', set()).update(
                            row['evidence_id'] for row in item if isinstance(row, dict)
                            and isinstance(row.get('evidence_id'), str))
                    if isinstance(item,(dict,list)):walk(item,collections.get(key))
        walk(data,roots.get(name))

    def current_context(self):
        if self.private_context_disabled:
            return self.request_privacy.metadata_context(self.g,self.w)
        # 同步公开行情/条款修订到私有派生版本，再检查本轮上下文。
        self.g.wealth.provision(self.w)
        with self.g.bank.transaction() as db:
            return context_hash(db, self.w)

    def fresh(self):
        if self.financial_mcp is not None:
            try:
                self.financial_mcp.assert_fresh()
            except FinancialMCPError as error:
                raise BankError(error.code, '独立财务历史已变化，请重新查询。', 409) from error
        if self.current_context() != self.start_context:
            raise BankError('STALE_EVIDENCE', '账户、账单来源或画像已变化，请重新查询。', 409)
        try:
            self.knowledge.assert_fresh()
        except ValueError as error:
            raise BankError('STALE_KNOWLEDGE_EVIDENCE','知识来源、权限或原文版本已变化，请重新检索。',409) from error
        self._market_fresh()
        if self._service_snapshot:
            self.assert_service_snapshot(self.g,self.w,self._service_snapshot)
        service=getattr(self.g,'financial_metrics',None)
        for batch in self.metric_batches:
            if service is None:
                raise BankError('STALE_METRIC_SOURCE','金融指标来源不可用，请重新查询。',409)
            service.validate(self.w,batch)

        if self.presentations is not None:self.presentations.fresh()
        if self.generated_code_session is not None:self.generated_code_session.verify_fresh()

    @staticmethod
    def _service_fingerprint(value):
        return hashlib.sha256(encoded(value).encode()).hexdigest()

    def _pin_service_rules(self, rules):
        digest=self._service_fingerprint(rules)
        if self._service_snapshot and self._service_snapshot['rules_sha256']!=digest:
            raise BankError('STALE_SERVICE_EVIDENCE','服务规则已变化，请重新查看条件。',409)
        if self._service_snapshot is None:
            self._service_snapshot={'schema_version':1,'workspace_id':self.w,'rules_sha256':digest,
                                    'applications_sha256':None,'preview':None}

    def service_snapshot(self):
        return deepcopy(self._service_snapshot)

    @classmethod
    def assert_service_snapshot(cls, gateway, workspace, snapshot):
        """供文字/TTS最终门复用；不赋予模型提交、接受或执行权限。"""
        if (not isinstance(snapshot,dict) or set(snapshot)!={'schema_version','workspace_id','rules_sha256',
                'applications_sha256','preview'} or type(snapshot['schema_version']) is not int
                or snapshot['schema_version']!=1 or snapshot['workspace_id']!=workspace):
            raise BankError('STALE_SERVICE_EVIDENCE','服务依据快照不属于本轮账户。',409)
        service=getattr(gateway,'service_negotiations',None)
        if service is None or cls._service_fingerprint(service.rules())!=snapshot['rules_sha256']:
            raise BankError('STALE_SERVICE_EVIDENCE','获准服务规则已变化，请重新查看。',409)
        if (snapshot['applications_sha256'] is not None and
                cls._service_fingerprint(service.applications(workspace))!=snapshot['applications_sha256']):
            raise BankError('STALE_SERVICE_EVIDENCE','服务申请回执已变化，请重新查询。',409)
        preview=snapshot['preview']
        if preview is not None:
            if not isinstance(preview,dict) or set(preview)!={'draft_id','sha256'}:
                raise BankError('STALE_SERVICE_EVIDENCE','服务预览快照无效。',409)
            with closing(gateway.bank.connect()) as db:
                row=db.execute('SELECT * FROM service_negotiation_drafts WHERE id=? AND workspace_id=?',
                               (preview['draft_id'],workspace)).fetchone()
            if (not row or row['status']!='previewed' or gateway.bank.clock()>=row['expires_at']
                    or cls._service_fingerprint(dict(row))!=preview['sha256']):
                raise BankError('STALE_SERVICE_EVIDENCE','服务预览已被替代、撤回或过期，请重新查看。',409)

    def _register_metrics(self, name, args, data, evidence_id):
        """只消费基座明确签名的指标批次，不把散落的标量升级成指标。"""
        # Even the hypothetical tool's metric issuer currently versions private
        # source tables. Restricted turns use its user_input_scenario fact
        # contract; they must not mint a personal financial metric indirectly.
        if self.private_context_disabled:return []
        service=getattr(self.g,'financial_metrics',None)
        describe=getattr(service,'metrics_for_tool',None)
        if describe is None:return []
        batch=describe(self.w,name,args,data)
        if batch is None:return []
        service.validate(self.w,batch)
        kinds=batch.get('metric_kinds',{})
        registered=[]
        pending=deepcopy(self.metrics)
        try:
            for metric in batch.get('metrics',[]):
                mid=metric.get('metric_id')
                registered.extend(pending.register(metric,kind=kinds.get(mid),evidence_id=evidence_id))
        except MetricClaimError as error:
            raise BankError(error.code,'金融指标合同缺失或冲突，请重新查询。',422) from error
        self.metric_batches.append(deepcopy(batch))
        self.metrics=pending
        records=[self.metrics.lookup(mid) for mid in registered]
        self._statistic_session.register(name,evidence_id,batch,records)
        return records

    def amount_contract(self, final):
        self.fresh()
        if self._history_reference is not None:self._assert_history_rendering(final)
        versions={}
        for batch in self.metric_batches:
            for metric in batch['metrics']:
                for key,value in metric['source_versions'].items():
                    if key in versions and versions[key]!=value:
                        raise BankError('STALE_METRIC_SOURCE','同轮金融指标来源版本冲突，请重新查询。',409)
                    versions[key]=value
        public_spans=list(self.validated_history_quote_spans(final))
        for claim in final['claims']:
            for fact in self.facts.values():
                if (fact.get('unit')!='public_minor_currency' or fact['evidence_id'] not in claim['evidence_ids']
                        or fact.get('scope')!='public_statement_not_account_balance'):
                    continue
                canonical=fact.get('text','')
                if not canonical or len(canonical)>2000:continue
                # 只有完整原文的显式引述豁免个人指标。公共同额不能支持个人余额。
                for excerpt in {canonical,unicodedata.normalize('NFKC',canonical)}:
                    for left,right in (('“','”'),('"','"')):
                        fragment=left+excerpt+right
                        if fragment not in claim['text']:continue
                        start=0
                        while (start:=final['text'].find(fragment,start))>=0:
                            public_spans.append((start,start+len(fragment)))
                            start+=len(fragment)
        try:
            from .read_completion import (funding_assessment_view_days,
                                          funding_assessment_metric_choices)
            # Applicability comes from the existing whole-request grammar,
            # independently of the primary selector's all-completed trace gate.
            scenario_days=funding_assessment_view_days(self.user_text)
            scenario_metrics=None
            if scenario_days is not None:
                scenario_metrics=[]
                flows=[e for e in self.evidence.values() if e.get('tool')=='cashflow'
                    and e.get('context_hash')==self.start_context
                    and e.get('parameters')=={'days':scenario_days}]
                if flows:
                    records=[self.metrics.lookup(mid)for mid in self.metrics.metric_ids]
                    scenario_metrics=[row['metric_id']for _,row in funding_assessment_metric_choices(
                        'cashflow',flows[-1]['data'],records,flows[-1]['id'],scenario_days)]
            return self.metrics.validate_answer(final['text'],final.get('amount_claims',[]),
                current_source_versions=versions,public_quote_spans=public_spans,
                scenario_metric_ids=scenario_metrics,scenario_context=scenario_days is not None)
        except MetricClaimError as error:
            if error.code=='METRIC_SCENARIO_SCOPE_REQUIRED':
                raise BankError(error.code,
                    '“两种情景均为”这类共同金额需要同一金额位置同时绑定同账户、同期间的已确认和候选分支正式指标。'
                    '同数原始数据、其他位置的金额声明或本轮未选择的指标不能代替。'
                    '请用本轮已交付的对应metric_id逐分支说明，或将合并表述改为准确的已支持单分支表述；'
                    '保留原问题必需的已支持结果，不省略必要金额或重读业务，不自造指标和声明。',422) from error
            raise BankError(error.code,'金额必须绑定本轮正式指标的对象、期间、币种和口径。',422) from error

    def _market_store(self):
        market=getattr(self.g,'market',None)
        if market is None:
            if self._default_market is None:
                from market_content import MarketStore
                self._default_market=MarketStore()
            market=self._default_market
        return market

    def bind_market_context(self, reference):
        """绑定服务端已消费的 PageContexts reference；模型无此工具/重绑定权限。

        root 接线应传 PageContexts.consume 返回的市场引用，不能传客户端自报版本。
        再次 resolve 校验当前源状态，固定身份及版本，并回查至最终 fresh。
        """
        if reference is None:return None
        from .history_binding import HostHistoryContext, requested_history
        if type(reference) is HostHistoryContext:
            return self._bind_history_context(reference)
        if requested_history(reference):
            raise BankError('HISTORY_CONTEXT_REQUIRED','历史引用必须来自本次已认证页面票据的消费。',403)
        if (not isinstance(reference,dict) or reference.get('kind') not in ('event','article','weekly')
                or reference.get('source_scope','market')!='market'
                or reference.get('execution_authority',False) is not False):
            raise BankError('MARKET_CONTEXT_INVALID','市场页面引用无效。')
        raw={k:deepcopy(v) for k,v in reference.items() if k not in ('execution_authority','request_mode')}
        resolved=self.g.page_contexts.resolve(self.w,raw)
        identity={k:deepcopy(resolved[k]) for k in ('kind','id','version','parameters')}
        if self.market_context is not None and identity!=self.market_context:
            raise BankError('MARKET_CONTEXT_CONFLICT','本轮已绑定另一市场版本，请重新选择后提问。',409)
        kind=identity['kind'];name='market_'+kind
        args={('period' if kind=='weekly' else kind+'_id'):identity['id']}
        if kind=='event':args.update(identity['parameters'])
        validate(args,SPECS[name][1])
        data=self._read_market(name,args)
        actual=(data.get('report') or {}).get('version') if kind=='weekly' else data.get('version')
        if str(actual)!=str(identity['version']):
            raise BankError('MARKET_CONTEXT_CONFLICT','引用的市场版本已变化，请重新查阅。',409)
        self._pin_market_read(name,args,data)
        self.market_context=identity
        return deepcopy(identity)

    def _market_arguments(self,name,args):
        if self._history_reference is not None:
            from .history_binding import bound_arguments
            return bound_arguments(self._history_binding['requested'],name,args)
        args=deepcopy(args)
        if name=='market_event':
            historical={'edition_date','revision','event_version'}
            present=historical&args.keys()
            if present and present!=historical:
                raise BankError('INVALID_TOOL_ARGUMENTS','历史热点需要完整的归档日、版次和事件版本。')
        context=self.market_context
        if context and name=='market_'+context['kind']:
            field='period' if context['kind']=='weekly' else context['kind']+'_id'
            if args.get(field,context['id'])!=context['id']:
                raise BankError('MARKET_CONTEXT_CONFLICT','工具对象与本轮页面引用不一致。',409)
            args.setdefault(field,context['id'])
            if name=='market_event':
                parameters=context['parameters']
                supplied={k:args[k] for k in ('edition_date','revision','event_version') if k in args}
                if supplied and supplied!=parameters:
                    raise BankError('MARKET_CONTEXT_CONFLICT','历史热点日期、版次或事件版本与页面引用不一致。',409)
                args.update(parameters)
        return args

    def _read_market(self,name,args):
        """每次读取和最终回查走同一公开接口，历史事件绝不降级到 latest。"""
        if self._history_reference is not None:
            from .history_binding import bound_arguments, read_exact
            bound_arguments(self._history_binding['requested'],name,args)
            return read_exact(self._market_store(),self._history_binding['requested'],expected_binding=self._history_binding)
        return self._read_market_data(self._market_store(),name,args)

    @staticmethod
    def _read_market_data(market,name,args):
        from market_daily import MarketDailyError
        try:
            if name=='market_hotspots':
                data=deepcopy(market.hotspots());data['events']=data['events'][:6]
            elif name=='market_status':data=deepcopy(market.status())
            elif name=='market_event':
                if 'edition_date' in args:
                    data=market.history_event(args['edition_date'],args['event_id'],args['revision'],
                                               expected_version=args['event_version'])
                else:data=market.event(args['event_id'])
            elif name=='market_article':data=market.article(args['article_id'])
            else:
                data=deepcopy(market.weekly(args.get('period','latest')))
                report=data.get('report')
                if report is not None and (not isinstance(report,dict) or report.get('status')!='published'):
                    raise BankError('STALE_MARKET_EVIDENCE','指定周报依赖已变化，历史身份不能作为当前事实证据。',409)
                data['supporting_events']=[]
                if (data.get('report') or {}).get('status')=='published':
                    for citation in data['report'].get('citations',[]):
                        try:event=market.event(citation['event_id'])
                        except KeyError:continue
                        if citation.get('event_version') is not None and citation['event_version']!=event.get('version'):
                            raise BankError('STALE_MARKET_EVIDENCE','周报引用的事件版本已变化，请重新查阅。',409)
                        data['supporting_events'].append(event)
            return deepcopy(data)
        except MarketDailyError as error:
            messages={400:'历史热点日期或版次无效。',404:'该事件不在指定历史精选中。',409:'历史事件版本不一致，请重新查阅。'}
            raise BankError(error.code,messages.get(error.status,'市场资料暂时无法核对。'),error.status) from error
        except KeyError as error:
            raise BankError('MARKET_NOT_FOUND','该市场对象不存在或已撤下。',404) from error
        except (sqlite3.Error,OSError) as error:
            raise BankError('MARKET_UNAVAILABLE','市场资料暂时无法核对，请稍后重试。',503) from error

    @staticmethod
    def _market_digest(data):
        # 核验时刻可刷新，版本、正文、来源准入、归档成员及状态不可变化。
        transient={'server_now','checked_at','fetched_at','queried_at','last_success_at','last_attempt_at',
                   'last_success_check_at','last_checked_at','coverage','history','schedule','updating'}
        def stable(value):
            if isinstance(value,list):return [stable(v) for v in value]
            if isinstance(value,dict):return {k:stable(v) for k,v in value.items() if k not in transient}
            return value
        return hashlib.sha256(encoded(stable(data)).encode()).hexdigest()

    def _pin_market_read(self,name,args,data):
        if self._history_reference is not None:
            from .history_binding import verify_packet
            verify_packet(data,self._history_binding['requested'])
            if data.get('receipt')!=self._history_binding['receipt']:
                raise BankError('STALE_HISTORY_EVIDENCE','历史原文或来源许可已变化。',409)
            return
        if name=='market_status':return
        key=encoded([name,args]);digest=self._market_digest(data)
        market=self._market_store();source=str(getattr(market,'path',id(market)))
        previous=self.market_reads.get(key)
        if previous and (previous['digest']!=digest or previous['source']!=source):
            raise BankError('STALE_MARKET_EVIDENCE','市场版本或来源状态已变化，请重新查阅。',409)
        if previous is None:self.market_reads[key]={'name':name,'args':deepcopy(args),'digest':digest,'source':source}

    def _market_fresh(self):
        if self._history_reference is not None:
            self._assert_history_context()
            self.assert_market_snapshot(self._market_store(),self.market_snapshot())
            return
        if self.market_reads:self.assert_market_snapshot(self._market_store(),self.market_snapshot())

    def market_snapshot(self):
        """本地最终展示闸门的查询身份快照；不携带原文或用户金融数据。"""
        if self._history_reference is not None:
            return deepcopy(self._history_snapshot)
        if not self.market_reads:return None
        return {'schema_version':1,'provider_id':'navren.public-market.v1',
            'context':deepcopy(self.market_context),
            'reads':[{'tool':r['name'],'arguments':deepcopy(r['args']),
                'source_token':hashlib.sha256(r['source'].encode()).hexdigest(),
                'sha256':r['digest']} for r in self.market_reads.values()]}

    @classmethod
    def assert_market_snapshot(cls,market,snapshot):
        """基座 JSON/SSE/TTS 输出前调用，失败不沿用旧版市场答复。"""
        if snapshot is None:return
        from .history_binding import PROVIDER, assert_snapshot
        if isinstance(snapshot,dict) and (snapshot.get('schema_version')==2 or snapshot.get('provider_id')==PROVIDER):
            return assert_snapshot(market,snapshot)
        allowed={'market_hotspots','market_event','market_article','market_weekly'}
        if (not isinstance(snapshot,dict) or snapshot.get('schema_version')!=1
                or snapshot.get('provider_id')!='navren.public-market.v1'
                or not isinstance(snapshot.get('reads'),list) or not snapshot['reads']):
            raise BankError('STALE_MARKET_EVIDENCE','市场引用快照无效，请重新查阅。',409)
        source=str(getattr(market,'path',id(market)))
        token=hashlib.sha256(source.encode()).hexdigest()
        for read in snapshot['reads']:
            if not isinstance(read,dict) or read.get('tool') not in allowed:
                raise BankError('STALE_MARKET_EVIDENCE','市场引用快照无效，请重新查阅。',409)
            name=read['tool'];args=read.get('arguments')
            try:
                validate(args,SPECS[name][1])
                if name=='market_event':
                    present={'edition_date','revision','event_version'}&args.keys()
                    if present and len(present)!=3:raise BankError('INVALID_TOOL_ARGUMENTS','历史参数不完整。')
                data=cls._read_market_data(market,name,args)
            except BankError as error:
                if error.status>=500:raise
                raise BankError('STALE_MARKET_EVIDENCE','市场对象或归档版本已失效，请重新查阅。',409) from error
            if token!=read.get('source_token') or cls._market_digest(data)!=read.get('sha256'):
                raise BankError('STALE_MARKET_EVIDENCE','市场资料已修订、撤回或精选版次变化，请重新查阅。',409)

    def add_facts(self, evidence_id, value, path='', currency='CNY', source_path=''):
        # source_path is a local JSON Pointer into evidence.data. The existing
        # display path still owns fact identity and must not be rewritten.
        if isinstance(value, dict):
            currency = value.get('currency', currency)
            groups = {'account_summary':'账户','profile':'资金设置','principal_summary':'理财本金',
                      'budget':'资金安排','totals_minor':'账单','cashflow':'资金预测',
                      'monthly_statement':'月度账单','annual_statement':'年度账单'}
            group = next((groups[p] for p in reversed(path.split('/')) if p in groups),'查询记录')
            prefix = value.get('name') or value.get('account_name') or value.get('plan_name') or value.get('merchant') or group
            if path.startswith('financial_list_transfers') or 'transfer_count' in value or 'transfer_id' in value:
                direction={'internal':'本人账户调拨','incoming':'转入','outgoing':'转出'}.get(value.get('direction'),'历史转账')
                prefix=(str(value['counterparty'])+' · '+direction) if value.get('counterparty') else direction
            if value.get('source_scope') not in ('user_confirmed','conversation'):
                prefix=re.sub(r'^模拟(?=备用账户|日常账户|活钱|工资|房租|餐饮|视频会员|云盘|健身会员|百货|在线课程|产品|服务)', '', str(prefix))
            for key, item in value.items():
                if key == 'opening_balance_minor': continue
                label = LABELS.get(key)
                if 'transfer_id' in value or ('transfer_count' in value and 'direction' in value):
                    label={'amount_minor':'金额','signed_minor':'账户收支变动'}.get(key,label)
                if path.startswith('statement_details'):
                    label = {'transaction_count':'笔数','total_minor':'合计','amount_minor':'金额'}.get(key, label)
                if key == 'investable_minor' and value.get('budget_scope') == 'planning_scenario':
                    label = '情景可安排资金'
                if type(item) is int and (key.endswith('_minor') or label or path.endswith('_minor')):
                    is_money = key.endswith('_minor') or path.endswith('_minor')
                    if is_money and (not isinstance(currency,str) or not re.fullmatch(r'[A-Z]{3}',currency)):
                        # Missing currency cannot become a CNY amount or an
                        # invalid verified fact; the source gap remains visible.
                        continue
                    display = money(item, currency) if is_money else str(item)
                    fact_id = 'f'+hashlib.sha256((evidence_id+path+'/'+key).encode()).hexdigest()[:20]
                    self.facts[fact_id] = {'id':fact_id,'evidence_id':evidence_id,'value':item,
                        'unit':'minor_currency' if is_money else 'scalar','currency':currency if is_money else None,
                        'text':str(prefix)+' · '+(label or key.removesuffix('_minor'))+'：'+display,
                        'path':path+'/'+key,
                        'source_path':source_path+'/'+key.replace('~','~0').replace('/','~1'),
                        'field':key,'label':label or key.removesuffix('_minor'),'name':str(prefix)}
                elif isinstance(item, (dict,list)):
                    child_path=(path+'/'+key) if path.startswith('statement_details') else (str(prefix)+'/'+key).strip('/')
                    child_source=source_path+'/'+key.replace('~','~0').replace('/','~1')
                    self.add_facts(evidence_id, item, child_path, currency, child_source)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                self.add_facts(evidence_id, item, path+'/'+str(i), currency, source_path+'/'+str(i))

    def add_market_facts(self, evidence_id, data):
        """公开金额有独立单位和来源，不能成为账户余额或纯事实免审通道。"""
        if self._history_reference is not None:return
        events=data.get('events',[]) if isinstance(data,dict) else []
        if 'facts' in data:events=[data]
        events=events+data.get('supporting_events',[])
        for event in events:
            if event.get('status')!='published' or event.get('availability','active')!='active':continue
            articles={a['id']:a for a in event.get('articles',[])
                      if a.get('status')=='active' and a.get('read_scope')=='full_text'
                      and a.get('link_status')=='verified'}
            for row in event.get('facts',[]):
                if row.get('article_id') not in articles:continue
                try:
                    # 只取已发布事实和对应原文引文中共同出现的金额；分析/审核理由不算证据。
                    amounts=set(monetary_literals(row.get('text','')))&set(monetary_literals(row.get('quote','')))
                except BankError:continue
                for value,currency in sorted(amounts):
                    fid='p'+hashlib.sha256(encoded([evidence_id,row,value,currency]).encode()).hexdigest()[:20]
                    self.facts[fid]={'id':fid,'evidence_id':evidence_id,'value':value,
                        'currency':currency,'unit':'public_minor_currency','article_id':row['article_id'],
                        'text':row['quote'],'scope':'public_statement_not_account_balance'}
        # 单篇官方原文可以直接取证；订阅标题/元数据不能证明正文金额。
        if (data.get('status')=='active' and data.get('read_scope')=='full_text'
                and data.get('link_status')=='verified' and 'url' in data):
            try:amounts=set(monetary_literals(data.get('evidence_excerpt','')))
            except BankError:amounts=set()
            for value,currency in sorted(amounts):
                fid='p'+hashlib.sha256(encoded([evidence_id,value,currency]).encode()).hexdigest()[:20]
                self.facts[fid]={'id':fid,'evidence_id':evidence_id,'value':value,'currency':currency,
                    'unit':'public_minor_currency','article_id':data['id'],
                    'text':data['evidence_excerpt'],'scope':'public_statement_not_account_balance'}

    def public_money_for_claims(self, claims):
        supported=set()
        for claim in claims:
            values={(f['value'],f['currency']) for f in self.facts.values()
                    if f['unit']=='public_minor_currency' and f['evidence_id'] in claim['evidence_ids']}
            # claim绑定来源即可；不要求在claims和正文重复抄写每个金额。
            # GLM核对全文中的金额语义与来源，代码仍拒绝原文没有的金额/币种。
            supported.update(values)
        return supported

    def add_knowledge_facts(self, evidence_id, data):
        # 只有通过本地权威回查的公开引文才登记公开金额；页码/检索分数不是财务事实。
        for hit in data.get('hits', []):
            try:
                amounts = set(monetary_literals(hit['excerpt']))
            except BankError:
                continue
            for value, currency in sorted(amounts):
                fid = 'p'+hashlib.sha256(encoded([evidence_id,hit['citation_id'],value,currency]).encode()).hexdigest()[:20]
                self.facts[fid] = {'id':fid,'evidence_id':evidence_id,'value':value,
                    'currency':currency,'unit':'public_minor_currency','citation_id':hit['citation_id'],
                    'text':hit['excerpt'],'scope':'public_statement_not_account_balance'}

    def interaction_contract(self, final):
        spec = validate_interaction(final.get('interaction'))
        if spec is None:
            return final
        if final.get('draft_ids') or self.calendar_drafts:
            raise BankError('INTERACTION_DRAFT_CONFLICT', '已有操作草案，需使用原确认卡。', 422)
        try:
            for text in [spec['prompt'], *[option['label'] for option in spec.get('options', [])]]:
                if monetary_occurrences(text):
                    raise BankError('INTERACTION_AMOUNT_FORBIDDEN', '交互选项不填金融金额，请在有证据的正文中说明。', 422)
        except MetricClaimError as error:
            raise BankError('INTERACTION_AMOUNT_FORBIDDEN', '交互选项不填金融金额，请在有证据的正文中说明。', 422) from error
        return {**final, 'interaction': spec, 'followups': []}

    def aggregate_budget_facts(self, final):
        """Append a local total only after complete current-budget fact coverage."""
        if cash_query_kind(self.user_text) != 'budget':
            return final
        if re.search(r'额外|另(?:外)?留|保留|预留|留出|留(?:下|款)|假设|假如|'
                     r'reserve|set aside|hypothetical|suppose|for example', self.user_text, re.I):
            return final
        ids = final['fact_ids']
        if len(set(ids)) != len(ids) or any(fid not in self.facts for fid in ids):
            return final
        summaries = [e for e in self.evidence.values() if e['tool'] == 'account_summary']
        if not summaries:
            return final
        summary = summaries[-1]
        state = summary['data']
        accounts = state.get('accounts', [])
        explicit = [a for a in accounts if any(value and value in self.user_text
                    for value in (a.get('id'), a.get('name')))]
        targets = explicit or accounts
        target_ids = {a['id'] for a in targets}
        if len(target_ids) < 2 or len(target_ids) != len(targets):
            return final
        as_of, version = state.get('as_of'), state.get('version')
        if (not isinstance(as_of, str) or not as_of or type(version) is not int
                or summary.get('context_hash') != self.start_context):
            return final
        budgets = {}
        for source in self.evidence.values():
            if source['tool'] != 'budget':
                continue
            data, params = source['data'], source['parameters']
            account_id = data.get('account_id')
            if (account_id in target_ids and (data.get('budget_scope') != 'current_budget'
                    or type(params.get('floor_minor', 0)) is not int or params.get('floor_minor', 0) != 0
                    or params.get('reserves', []) != [])):
                return final
            if (account_id in target_ids and data.get('budget_scope') == 'current_budget'
                    and type(params.get('floor_minor', 0)) is int and params.get('floor_minor', 0) == 0
                    and params.get('reserves', []) == []):
                budgets[account_id] = source
        if set(budgets) != target_ids:
            return final
        totals = {}
        for account in targets:
            source = budgets[account['id']]
            data = source['data']
            currency = account.get('currency')
            value = data.get('investable_minor')
            if (source.get('context_hash') != self.start_context or data.get('as_of') != as_of
                    or data.get('version') != version or source['parameters'].get('account_id') != account['id']
                    or not isinstance(currency, str) or not re.fullmatch(r'[A-Z]{3}', currency)
                    or data.get('currency') != currency or type(value) is not int or value < 0):
                return final
            matched = [self.facts[fid] for fid in ids if self.facts[fid]['evidence_id'] == source['id']
                       and self.facts[fid].get('source_path') == '/investable_minor'
                       and self.facts[fid].get('field') == 'investable_minor'
                       and self.facts[fid].get('unit') == 'minor_currency'
                       and self.facts[fid].get('currency') == currency
                       and type(self.facts[fid].get('value')) is int
                       and self.facts[fid].get('value') == value]
            if len(matched) != 1:
                return final
            total = totals.setdefault(currency, {'currency': currency, 'investable_minor': 0,
                'target_account_ids': [], 'source_fact_ids': [], 'source_evidence_ids': []})
            total['investable_minor'] += value
            total['target_account_ids'].append(account['id'])
            total['source_fact_ids'].append(matched[0]['id'])
            total['source_evidence_ids'].append(source['id'])
        if len(ids) + len(totals) > 24:
            return final
        title = '所查账户可安排资金合计' if explicit else '全户可安排资金合计'
        data = {'workspace_id': self.w, 'as_of': as_of, 'version': version,
            'name': title,
            'budget_scope': 'current_budget_aggregate', 'target_account_ids': sorted(target_ids),
            'account_summary_evidence_id': summary['id'], 'totals': [{'name': title, **totals[c]} for c in sorted(totals)],
            'amount_basis': '按各账户已扣应急底线、生效预留及30天已知到期款后的当前预算分别相加；'
                            '不合并币种，不计持仓本金，不预支未来收入，未估算其他生活开支。'}
        evidence_id = 'e-local-budget-total-' + hashlib.sha256(encoded([self.start_context, data]).encode()).hexdigest()[:16]
        self.evidence[evidence_id] = {'id': evidence_id, 'tool': 'budget_aggregate', 'parameters': {},
            'data': data, 'source': 'simulation', 'context_hash': self.start_context}
        self.add_facts(evidence_id, data, 'budget_aggregate')
        total_ids = [fid for fid, fact in self.facts.items() if fact['evidence_id'] == evidence_id]
        for fid in total_ids:
            row = data['totals'][int(self.facts[fid]['source_path'].split('/')[2])]
            self.facts[fid].update(source_fact_ids=list(row['source_fact_ids']),
                                   target_account_ids=list(row['target_account_ids']))
        return {**final, 'fact_ids': ids + [fid for fid in total_ids if fid not in ids]}

    def _structured_funds_completed(self, final, cash_kind):
        """A verified whole-workspace funds view can complete its own bounded read.

        Ordinary balance/account/reserve requests retain the existing completion
        contract. Request prose only narrows eligibility; it grants no authority.
        """
        if (cash_kind != 'budget' or self.presentations is None
                or self.private_context_disabled or final.get('draft_ids')):
            return False
        question=unicodedata.normalize('NFKC',self.user_text)
        if (not all(word in question for word in ('展示','资金范围','缺项'))
                or re.search(r'账户|银行卡|卡号|尾号|额外|留款|保留|预留|留出|应急|假设|如果|'
                             r'单独|只查|仅查|余额|日常|备用|工资|储蓄|floor|reserves',question,re.I)):
            return False
        selected=final.get('widget_ids',[])
        if len(selected)!=1 or selected[0] not in self.presentations.entries:
            return False
        entry=self.presentations.entries[selected[0]]
        obj=entry.get('provider_object')
        parameters=obj.get('parameters') if isinstance(obj,dict) else None
        if (not isinstance(parameters,dict) or parameters.get('intent')!='funds'
                or set(parameters)-{'intent','days'} or obj.get('operation')!='query'):
            return False
        self.presentations.provider.verify(entry['reserve_receipt'])
        result=entry['result'];presentation=result['presentation'];scope=presentation['scope']
        query=scope.get('funding_query',{});batch=result['metric_batch']
        if (presentation['workspace_binding']!=self.w
                or presentation['request_id']!=self.request_id or presentation['message_id']!=self.request_id
                or scope.get('source')!='simulation' or scope.get('currency')!='CNY'
                or scope.get('account_id') is not None or scope.get('portfolio')!='current'
                or scope.get('accounting_basis')!='funds.current_budget/1'
                or query.get('subject')!='whole_workspace' or query.get('method_id')!='funds.current_budget/1'
                or type(query.get('days')) is not int or not 1<=query['days']<=365
                or ('days' in parameters and parameters['days']!=query['days'])
                or presentation['range']['from']!=query.get('period_start')
                or presentation['range']['to']!=query.get('period_end')
                or batch.get('workspace_id')!=self.w
                or self.widgets.get(selected[0],{}).get('presentation')!=presentation):
            return False
        originals={m['metric_id']:m for m in batch['metrics']}
        if (len(originals)!=6 or {m['metric_name'] for m in originals.values()} != {
                'cash_balance','current_available','arrangeable_now','emergency_floor',
                'obligations_aggregate','holds_aggregate'}
                or any(m['subject_id']!=self.w or m['currency']!='CNY' for m in originals.values())
                or any(c.get('metric_id') not in originals for c in final.get('amount_claims',[]))):
            return False
        self.g.financial_metrics.validate(self.w,batch)
        if any(self.metrics.lookup(mid)['metric']!=metric for mid,metric in originals.items()):
            raise BankError('PRESENTATION_TAMPERED','本轮指标不是展示的完整签发原件。',409)
        # The unchanged monetary guard binds every body occurrence before this
        # equivalent read is accepted; GLM and complete Provider review follow.
        self.amount_contract(final)
        self.presentations.fresh()
        return True

    def public_quote_fact_ids(self):
        # Eligibility only; every actual rendering still runs fresh() before
        # checking the exact canonical original. No account or execution claim.
        allowed={'load_skill','knowledge_status','knowledge_search','public_products',
                 'market_status','market_hotspots','market_event','market_article','market_weekly'}
        if (self.metrics.metric_ids or self.metric_batches or self.changed or self.drafts
                or self.calendar_drafts or self.widgets or self.calendar_edit_attempted
                or self.generated_code_session is not None or self._history_reference is not None
                or self.history_quote_session is not None or self.history_quote_proof is not None):return []
        for row in self.trace:
            if type(row)is dict and row=={'tool':'knowledge_search','status':'deferred','code':'KNOWLEDGE_BATCH_DEFERRED'}:continue
            # Only this exact host record is emitted before public provider
            # invocation; it cannot authorize any rejected private/write tool.
            if (set(row)=={'tool','status','code'}
                    and row['tool'] in ('knowledge_search','public_products')
                    and row['status']=='rejected' and row['code']=='BOUNDARY_PUBLIC_QUERY'):continue
            if (row=={'tool':'answer_facts','status':'rejected','code':'REVIEW_REQUIRED','status_code':400}
                    and type(row.get('status_code')) is int):continue
            if (type(row)is dict and row=={'tool':'answer_reviewed','status':'rejected','code':'INVALID_TOOL_ARGUMENTS','status_code':400}
                    and type(row.get('status_code')) is int):continue
            if (type(row)is dict
                    and row=={'tool':'answer_reviewed','status':'rejected','code':'PUBLIC_FEE_SOURCE_REQUIRED','status_code':422}
                    and type(row.get('status_code')) is int):continue
            if (type(row)is dict
                    and row=={'tool':'answer_reviewed','status':'rejected','code':'PUBLIC_TIME_BASIS_REQUIRED','status_code':422}
                    and type(row.get('status_code')) is int):continue
            if row.get('tool') not in allowed or row.get('status')!='completed':return []
        pinned=self.knowledge._pinned_status
        market_evidence={}
        eligible=[]
        for fid,fact in self.facts.items():
            if (fact.get('id')!=fid or fact.get('unit')!='public_minor_currency'
                    or fact.get('scope')!='public_statement_not_account_balance'
                    or type(fact.get('value')) is not int):continue
            eid=fact.get('evidence_id');evidence=self.evidence.get(eid,{})
            data=evidence.get('data',{});as_of=data.get('as_of');text=fact.get('text')
            if evidence.get('source')=='verified_public_market':
                if eid not in market_evidence:
                    market_evidence[eid]=self._current_market_quote_data(eid,evidence)
                if self._market_quote_matches(fact,market_evidence[eid]):eligible.append(fid)
                continue
            if not isinstance(pinned,dict) or pinned.get('ready') is not True:continue
            if (evidence.get('id')!=eid or evidence.get('tool')!='knowledge_search'
                    or evidence.get('source')!='public_knowledge' or evidence.get('context_hash')!=self.start_context
                    or data.get('ready') is not True or data.get('scope')!='public'
                    or data.get('provider_id')!=pinned.get('provider_id')
                    or data.get('public_release_id')!=pinned.get('public_release_id')
                    or not isinstance(text,str) or not 0<len(text)<=1198):continue
            hit=next((h for h in data.get('hits',[])if h.get('citation_id')==fact.get('citation_id')),None)
            delivered=self.knowledge._delivered.get((fact.get('citation_id'),as_of))
            if (not hit or not delivered or hit!=delivered[0] or hit.get('excerpt')!=text
                    or hit.get('release_id')!=pinned.get('public_release_id')
                    or hit.get('visibility')!='public' or hit.get('outbound_allowed') is not True
                    or hit.get('content_status')!='full_text' or hit.get('claims_are_account_facts') is not False):continue
            try:amounts=set(monetary_literals(text))
            except BankError:continue
            if (fact['value'],fact.get('currency')) in amounts:eligible.append(fid)
        return eligible

    def _current_market_quote_data(self, eid, evidence):
        """Read the exact pinned public object; never substitute latest/history."""
        name=evidence.get('tool');args=evidence.get('parameters')
        if (evidence.get('id')!=eid or evidence.get('context_hash')!=self.start_context
                or name not in {'market_hotspots','market_event','market_article','market_weekly'}
                or not isinstance(args,dict)):
            return None
        pin=self.market_reads.get(encoded([name,args]))
        if not pin:return None
        try:
            raw=self._read_market(name,args)
        except BankError:return None
        if (self._market_digest(raw)!=pin['digest']
                or self._market_digest(market_agent_view(name,raw))!=self._market_digest(evidence.get('data'))):
            return None
        return evidence['data']

    @staticmethod
    def _market_quote_matches(fact, data):
        if not isinstance(data,dict):return False
        text=fact.get('text');article_id=fact.get('article_id')
        if not isinstance(text,str) or not 0<len(text)<=1198 or not isinstance(article_id,str):return False
        amount=(fact['value'],fact.get('currency'))
        def contains(value):
            try:return amount in set(monetary_literals(value))
            except BankError:return False
        if (data.get('id')==article_id and data.get('status')=='active'
                and data.get('read_scope')=='full_text' and data.get('link_status')=='verified'
                and text==data.get('evidence_excerpt') and contains(text)):
            return True
        events=([data] if 'facts' in data else [])+data.get('events',[])+data.get('supporting_events',[])
        for event in events:
            if event.get('status')!='published' or event.get('availability','active')!='active':continue
            articles={a['id'] for a in event.get('articles',[]) if a.get('status')=='active'
                      and a.get('read_scope')=='full_text' and a.get('link_status')=='verified'}
            if article_id not in articles:continue
            if any(row.get('article_id')==article_id and row.get('quote')==text
                   and contains(row.get('text','')) and contains(text) for row in event.get('facts',[])):
                return True
        return False

    @staticmethod
    def _joint_source_digest(value):
        return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
            separators=(',',':'),allow_nan=False).encode()).hexdigest()

    @staticmethod
    def _joint_source_invalid():
        raise BankError('PUBLIC_JOINT_SOURCE_PLAN_INVALID',
            '联合原文补引条件不完整、存在冲突或已失效，本轮停止，不再修稿。',409)

    def _joint_time_endpoint_sources(self, bindings):
        from .public_quote_recovery import delivered_time_endpoint_sources
        return delivered_time_endpoint_sources(bindings,evidence=self.evidence,
            delivered=self.knowledge._delivered,pinned=self.knowledge._pinned_status,
            context_hash=self.start_context)

    def _fee_source_quote_rows(self, args):
        """Only complete fee originals in evidence actually cited by this draft."""
        pinned=self.knowledge._pinned_status
        cited={eid for claim in args.get('claims',[])for eid in claim.get('evidence_ids',[])}
        if (not cited or not isinstance(pinned,dict)or pinned.get('ready')is not True
                or not pinned.get('provider_id')or not pinned.get('public_release_id')):
            self._joint_source_invalid()
        selected={};identities=set();versions=set();days=set()
        identity_fields=('product_id','share_class_id','channel_id','release_id')
        for eid in sorted(cited):
            entry=self.evidence.get(eid,{});data=entry.get('data',{})
            if (entry.get('id')!=eid or entry.get('context_hash')!=self.start_context
                    or entry.get('tool')!='knowledge_search'or entry.get('source')!='public_knowledge'
                    or data.get('ready')is not True or data.get('scope')!='public'
                    or data.get('provider_id')!=pinned['provider_id']
                    or data.get('public_release_id')!=pinned['public_release_id']
                    or type(data.get('as_of'))is not str
                    or datetime.strptime(data['as_of'],'%Y-%m-%d').date().isoformat()!=data['as_of']):
                self._joint_source_invalid()
            for hit in data.get('hits',[]):
                if 'fees'not in hit.get('facets',[]):continue
                delivered=self.knowledge._delivered.get((hit.get('citation_id'),data['as_of']))
                if (not delivered or delivered[0]!=hit or hit.get('visibility')!='public'
                        or hit.get('outbound_allowed')is not True or hit.get('content_status')!='full_text'
                        or hit.get('claims_are_account_facts')is not False or hit.get('withdrawn',False)is not False
                        or hit.get('release_id')!=pinned['public_release_id']
                        or any(type(hit.get(k))is not str or not hit[k]for k in
                            ('citation_id','document_id','document_version_id','product_id','share_class_id'))
                        or 'channel_id'not in hit or not(hit['channel_id']is None or type(hit['channel_id'])is str)
                        or type(hit.get('excerpt'))is not str or not 0<len(hit['excerpt'])<=1198
                        or monetary_occurrences(unicodedata.normalize('NFKC',hit['excerpt']))):
                    self._joint_source_invalid()
                identities.add(tuple(hit[k]for k in identity_fields))
                versions.add((hit['document_id'],hit['document_version_id']));days.add(data['as_of'])
                key=(hit['citation_id'],data['as_of']);row={'evidence_id':eid,'hit':deepcopy(hit),'as_of':data['as_of']}
                if key in selected:
                    if selected[key]['hit']!=hit:self._joint_source_invalid()
                else:selected[key]=row
        if len(identities)!=1 or len(versions)!=1 or len(days)!=1 or not 1<=len(selected)<=6:
            self._joint_source_invalid()
        from .public_fee_statement_contract import canonical_source_rows
        return canonical_source_rows(list(selected.values()))

    def _fee_source_time_bindings(self, args, identity):
        """An existing concrete limit needs the unchanged original explicit gate."""
        from .knowledge_semantics import _concrete_time_conclusions, public_time_basis_bindings
        from .public_quote_recovery import _fee_texts
        body=args.get('text')
        if body is None:
            body='\n'.join(part['text']for path,part,_ in _fee_texts(args)if not path.startswith('claims['))
        texts=[('text',body)]+[('claims['+str(i)+'].text',claim['text'])
            for i,claim in enumerate(args.get('claims',[]))]
        evidence=[entry for entry in self.evidence.values()if entry.get('context_hash')==self.start_context]
        cited={eid for claim in args.get('claims',[])for eid in claim.get('evidence_ids',[])}
        records=[];fields=('product_id','share_class_id','channel_id','release_id')
        for location,text in texts:
            numbers=_concrete_time_conclusions(text)
            if not numbers:continue
            bindings=public_time_basis_bindings({'text':text,'claims':args['claims']},evidence)
            if ({item['notation'][2:]for item in bindings}!=numbers
                    or any(item['answer_status']!='explicit'or tuple(item.get(k)for k in fields)!=identity
                        for item in bindings)):
                self._joint_source_invalid()
            for item in bindings:
                for role in ('limit','definition'):
                    eid=item[role+'_evidence_id'];entry=self.evidence.get(eid,{});data=entry.get('data',{})
                    hits=[hit for hit in data.get('hits',[])if hit.get('citation_id')==item[role+'_citation_id']]
                    if (eid not in cited or len(hits)!=1 or entry.get('id')!=eid
                            or entry.get('context_hash')!=self.start_context or entry.get('tool')!='knowledge_search'
                            or entry.get('source')!='public_knowledge' or data.get('ready')is not True
                            or data.get('scope')!='public' or data.get('provider_id')!=self.knowledge._pinned_status['provider_id']
                            or data.get('public_release_id')!=identity[3]):self._joint_source_invalid()
                    hit=hits[0];delivered=self.knowledge._delivered.get((hit['citation_id'],data.get('as_of')))
                    if (not delivered or delivered[0]!=hit or hit.get('visibility')!='public'
                            or hit.get('outbound_allowed')is not True or hit.get('content_status')!='full_text'
                            or hit.get('claims_are_account_facts')is not False or hit.get('withdrawn',False)is not False
                            or not {'liquidity','terms'}&set(hit.get('facets',[]))
                            or any(hit.get(k)!=item.get(k)for k in (*fields,'document_id','document_version_id'))):
                        self._joint_source_invalid()
            records.append({'location':location,'bindings':bindings})
        return records

    def _prepare_fee_source_quotes(self, args, context):
        """Separate fee-only admission; it never obtains identity from time rows."""
        if self._joint_source_plan is not None:self._joint_source_invalid()
        self._joint_source_plan={'status':'preparing'}
        try:
            self.fresh();self.validate_answer_arguments(args)
            keys={'contract','original_draft_sha256','restored_args_sha256','fee_paths','protected_time_sentences'}
            if (type(context)is not dict or set(context)!=keys
                    or context['contract']!='navren.public-quote-fee-source-context/1'
                    or not re.fullmatch('[a-f0-9]{64}',context['original_draft_sha256'])
                    or context['restored_args_sha256']!=self._joint_source_digest(args)):
                self._joint_source_invalid()
            from .public_quote_recovery import _fee_texts, _fee_source_time_sentences
            fields={path for path,_,_ in _fee_texts(args)};paths=context['fee_paths']
            if (type(paths)is not list or not 1<=len(paths)<=12 or len(paths)!=len(set(paths))
                    or any(type(path)is not str or path not in fields for path in paths)
                    or context['protected_time_sentences']!=_fee_source_time_sentences(args)):
                self._joint_source_invalid()
            sources=self._fee_source_quote_rows(args)
            identity=tuple(sources[0]['hit'][k]for k in ('product_id','share_class_id','channel_id','release_id'))
            self._fee_source_time_bindings(args,identity)
            self._joint_source_plan={'status':'prepared','arguments_sha256':self._joint_source_digest(args),
                'context':deepcopy(context),'sources':sources}
        except BankError:raise
        except (KeyError,ValueError,TypeError,MetricClaimError):self._joint_source_invalid()

    def prepare_joint_source_quotes(self, args, context):
        """Private one-shot host plan; no model selector or new financial fact."""
        self._fee_statement_args=deepcopy(args)
        if isinstance(context,dict)and context.get('contract')=='navren.public-quote-fee-delivery-context/1':
            # A descriptor is not a committed source plan. Original guards may
            # still reject this draft and use their existing single repair.
            if (self._joint_source_plan is not None or set(context)!=
                    {'contract','original_draft_sha256','restored_args_sha256'}
                    or not re.fullmatch('[a-f0-9]{64}',context['original_draft_sha256'])
                    or context['restored_args_sha256']!=self._joint_source_digest(args)):
                self._joint_source_invalid()
            self._fee_delivery_context=deepcopy(context)
            return
        if isinstance(context,dict)and context.get('contract')=='navren.public-quote-fee-source-context/1':
            return self._prepare_fee_source_quotes(args,context)
        if self._joint_source_plan is not None:self._joint_source_invalid()
        # A failed plan also consumes this host-only admission. Conversation
        # must not turn a rejected source plan into another model repair.
        self._joint_source_plan={'status':'preparing'}
        try:
            self.fresh();self.validate_answer_arguments(args)
            keys={'contract','original_draft_sha256','restored_args_sha256','fee_paths',
                'time_paths','time_basis_rejections','protected_time_sentences'}
            if 'time_endpoint_sources'in context:keys.add('time_endpoint_sources')
            if (type(context)is not dict or set(context)!=keys
                    or context['contract']!='navren.public-quote-joint-source-context/1'
                    or not re.fullmatch('[a-f0-9]{64}',context['original_draft_sha256'])
                    or context['restored_args_sha256']!=self._joint_source_digest(args)):
                self._joint_source_invalid()
            from .public_quote_recovery import _fee_texts, _joint_time_model_sentences
            fields={path:part['text']for path,part,_ in _fee_texts(args)}
            for key in ('fee_paths','time_paths'):
                paths=context[key]
                if (type(paths)is not list or not 1<=len(paths)<=12
                        or any(type(path)is not str or path not in fields for path in paths)
                        or len(paths)!=len(set(paths))):self._joint_source_invalid()
            protected=context['protected_time_sentences'];covered=set()
            if type(protected)is not list or not 1<=len(protected)<=12:self._joint_source_invalid()
            for row in protected:
                if (type(row)is not dict or set(row)!={'path','notation','text_nfkc',
                        'text_nfkc_sha256','min_occurrences'} or row['path']not in context['time_paths']
                        or row['path'].startswith('claims[') or type(row['text_nfkc'])is not str
                        or not row['text_nfkc'] or row['text_nfkc']!=unicodedata.normalize('NFKC',row['text_nfkc']).strip()
                        or hashlib.sha256(row['text_nfkc'].encode()).hexdigest()!=row['text_nfkc_sha256']
                        or type(row['min_occurrences'])is not int or row['min_occurrences']<1
                        or unicodedata.normalize('NFKC',fields[row['path']]).strip().count(row['text_nfkc'])<row['min_occurrences']):
                    self._joint_source_invalid()
                covered.add(row['path'])
            if covered!=set(context['time_paths']):self._joint_source_invalid()
            bindings=context['time_basis_rejections']
            identity_fields=('product_id','share_class_id','channel_id','release_id')
            if type(bindings)is not list or not 1<=len(bindings)<=12:self._joint_source_invalid()
            identities=set();notations=set()
            for item in bindings:
                if (type(item)is not dict or item.get('answer_status')!='missing'
                        or item.get('location')!='text' or item.get('scope')!='source_time_condition_not_execution_or_current_validity'
                        or not re.fullmatch(r'T\+(?:0|[1-9][0-9]{0,3})',item.get('notation',''))
                        or item.get('counting_unit')not in ('工作日','自然日')
                        or not (item.get('excludes_t')is None or item.get('excludes_t')is True)):
                    self._joint_source_invalid()
                identities.add(tuple(item.get(k)for k in identity_fields));notations.add(item['notation'])
            if (len(identities)!=1 or any(row['notation']not in notations for row in protected)):
                self._joint_source_invalid()
            # Reuse the same finite absence-only grammar over every restored
            # authored body/claim field. No second, looser time parser.
            endpoint_sources=context.get('time_endpoint_sources',())
            if endpoint_sources and self._joint_time_endpoint_sources(bindings)!=endpoint_sources:
                self._joint_source_invalid()
            _joint_time_model_sentences(args,bindings,time_endpoint_sources=endpoint_sources)
            identity=next(iter(identities));pinned=self.knowledge._pinned_status
            if (not identity[0] or not identity[1] or not isinstance(pinned,dict)
                    or pinned.get('ready')is not True or identity[3]!=pinned.get('public_release_id')):
                self._joint_source_invalid()
            sources={};fees={};as_ofs=set()
            cited={eid for claim in args.get('claims',[])for eid in claim.get('evidence_ids',[])}
            for eid in sorted(cited):
                evidence=self.evidence.get(eid,{})
                data=evidence.get('data',{})
                if (evidence.get('id')!=eid or evidence.get('context_hash')!=self.start_context
                        or evidence.get('tool')!='knowledge_search' or evidence.get('source')!='public_knowledge'
                        or data.get('ready')is not True or data.get('scope')!='public'
                        or data.get('provider_id')!=pinned.get('provider_id')
                        or data.get('public_release_id')!=identity[3]):continue
                for hit in data.get('hits',[]):
                    if tuple(hit.get(k)for k in identity_fields)!=identity:continue
                    delivered=self.knowledge._delivered.get((hit.get('citation_id'),data.get('as_of')))
                    if (not delivered or delivered[0]!=hit or hit.get('visibility')!='public'
                            or hit.get('outbound_allowed')is not True or hit.get('content_status')!='full_text'
                            or hit.get('claims_are_account_facts')is not False
                            or not all(isinstance(hit.get(k),str)and hit[k]for k in ('citation_id','document_id','document_version_id'))
                            or not isinstance(hit.get('excerpt'),str)or not 0<len(hit['excerpt'])<=1198):
                        self._joint_source_invalid()
                    row={'evidence_id':eid,'hit':deepcopy(hit),'as_of':data['as_of']}
                    sources[(eid,hit['citation_id'])]=row
                    if 'fees'in hit.get('facets',[]):
                        key=(hit['citation_id'],data['as_of'])
                        if key in fees and fees[key]['hit']!=hit:self._joint_source_invalid()
                        fees.setdefault(key,row);as_ofs.add(data['as_of'])
            if (not fees or len({(row['hit']['document_id'],row['hit']['document_version_id'])
                    for row in fees.values()})!=1):self._joint_source_invalid()
            selected=list(fees.values())
            for item in bindings:
                for role in ('limit','definition'):
                    row=sources.get((item.get(role+'_evidence_id'),item.get(role+'_citation_id')))
                    if (row is None or any(row['hit'].get(k)!=item.get(k)for k in
                            ('product_id','share_class_id','channel_id','document_id','document_version_id','release_id'))
                            or not {'liquidity','terms'}&set(row['hit'].get('facets',[]))):
                        self._joint_source_invalid()
                    as_ofs.add(row['as_of'])
                    if not any(old['hit']['citation_id']==row['hit']['citation_id']for old in selected):selected.append(row)
            if len(as_ofs)!=1 or not 1<=len(selected)<=6:self._joint_source_invalid()
            for row in selected:
                # Nonmonetary originals do not mint a public monetary fact or
                # borrow a different fact's amount exemption.
                if monetary_occurrences(unicodedata.normalize('NFKC',row['hit']['excerpt'])):
                    self._joint_source_invalid()
            from .public_fee_statement_contract import canonical_source_rows
            self._joint_source_plan={'status':'prepared','arguments_sha256':self._joint_source_digest(args),
                'context':deepcopy(context),'sources':canonical_source_rows(selected,bindings)}
        except BankError:raise
        except (KeyError,ValueError,TypeError,MetricClaimError):self._joint_source_invalid()

    def _render_joint_source_quotes(self, rendered, plan):
        """Reject the unchanged model body/claims before adding complete originals."""
        from .public_quote_recovery import public_fee_source_rejections
        from .knowledge_semantics import public_time_basis_bindings
        from .operation_claims import validate_operation_wording
        self.fresh();validate_operation_wording(rendered,self);self.amount_contract(rendered)
        if public_fee_source_rejections(rendered,self):self._joint_source_invalid()
        context=plan['context'];fee_only=context['contract']in(
            'navren.public-quote-fee-source-context/1','navren.public-quote-fee-delivery-context/1')
        evidence=[e for e in self.evidence.values()if e.get('context_hash')==self.start_context]
        if fee_only:
            if self._fee_source_quote_rows(rendered)!=plan['sources']:self._joint_source_invalid()
            fee_identity=tuple(plan['sources'][0]['hit'][k]for k in ('product_id','share_class_id','channel_id','release_id'))
            fee_time=self._fee_source_time_bindings(rendered,fee_identity)
        else:
            bindings=context['time_basis_rejections']
            actual=public_time_basis_bindings(rendered,evidence)
            if actual!=bindings:self._joint_source_invalid()
            endpoint_sources=context.get('time_endpoint_sources',())
            if endpoint_sources and self._joint_time_endpoint_sources(bindings)!=endpoint_sources:
                self._joint_source_invalid()
        quotes=[];leaves=[];seen=set()
        for row in plan['sources']:
            hit=row['hit'];entry=self.evidence.get(row['evidence_id'],{})
            delivered=self.knowledge._delivered.get((hit['citation_id'],row['as_of']))
            if (entry.get('id')!=row['evidence_id']or entry.get('context_hash')!=self.start_context
                    or entry.get('tool')!='knowledge_search' or entry.get('source')!='public_knowledge'
                    or entry.get('data',{}).get('provider_id')!=self.knowledge._pinned_status['provider_id']
                    or entry.get('data',{}).get('public_release_id')!=hit['release_id']
                    or entry.get('data',{}).get('as_of')!=row['as_of']
                    or hit not in entry.get('data',{}).get('hits',[])or not delivered or delivered[0]!=hit):
                self._joint_source_invalid()
            canonical=hit['excerpt'];display=unicodedata.normalize('NFKC',canonical)
            quote='“'+display+'”';identity=(hit['citation_id'],row['as_of'])
            if identity in seen:self._joint_source_invalid()
            seen.add(identity)
            # Always copy the whole delivered excerpt, not a selected table row
            # or a paraphrase. No fee/rate/threshold/unit/date is inferred.
            quotes.append({'text':quote,'evidence_ids':[row['evidence_id']]})
            leaves.append({'evidence_id':row['evidence_id'],'citation_id':hit['citation_id'],
                'document_id':hit['document_id'],'document_version_id':hit['document_version_id'],
                'release_id':hit['release_id'],'product_id':hit['product_id'],
                'share_class_id':hit['share_class_id'],'channel_id':hit.get('channel_id'),
                'as_of':row['as_of'],'provider_id':self.knowledge._pinned_status['provider_id'],
                'source_ref':hashlib.sha256(canonical.encode()).hexdigest(),
                'source_revision':hashlib.sha256(display.encode()).hexdigest()})
        before=self._joint_source_digest(rendered)
        result=deepcopy(rendered)
        result['text']+='\n\n'+'\n\n'.join(q['text']for q in quotes)
        result['claims'].extend(quotes)
        validate(result,SPECS['answer_reviewed'][1])
        if public_fee_source_rejections(result,self):self._joint_source_invalid()
        if fee_only:
            if self._fee_source_time_bindings(result,fee_identity)!=fee_time:self._joint_source_invalid()
        else:
            checked=public_time_basis_bindings(result,evidence)
            if (len(checked)!=len(bindings)or any(item['answer_status']!='explicit'for item in checked)):
                self._joint_source_invalid()
        receipt={'contract':'navren.public-quote-joint-source-rendering/1',
            'original_draft_sha256':context['original_draft_sha256'],
            'restored_args_sha256':plan['arguments_sha256'],'pre_quote_answer_sha256':before,
            'rendered_pre_boundary_answer_sha256':self._joint_source_digest(result),
            'quote_claims_sha256':self._joint_source_digest(quotes),'source_claims':leaves,
            'host_generated_conclusion':False,'additional_primary_calls':0,'additional_business_calls':0,
            'original_guards_status':'pending_original_full_GLM_and_FinalProof'}
        return result,receipt

    def _validate_public_fee_delivery(self, rendered, sources):
        """Whole originals in the visible body; claims alone cannot cover them."""
        from .public_quote_recovery import _FEES
        def missing():
            raise BankError('PUBLIC_FEE_DELIVERY_REQUIRED',
                '本轮费用原文或费用字段未完整进入可见答复，本轮停止。',422)
        originals=[unicodedata.normalize('NFKC',row['hit']['excerpt'])for row in sources]
        if not originals:missing()
        for row,display in zip(sources,originals):
            quote='“'+display+'”'
            if (quote not in rendered['text'] or not any(claim==
                    {'text':quote,'evidence_ids':[row['evidence_id']]}for claim in rendered['claims'])):
                missing()
        # Bind named operating-fee fields to their own annual table, never a
        # different fee's identical percentage. No rate is calculated or filled.
        named=set(_FEES.findall(rendered['text']))
        for fee in named:
            if not any('年费率'in original and '基金资产中扣除'in original
                    and re.search(re.escape(fee)+r'\s+[0-9]+(?:\.[0-9]+)?%',original)
                    for original in originals):missing()

    def _complete_public_fee_delivery(self, rendered):
        """Common successful terminal, after the original operation/fee/time gates."""
        from .public_quote_recovery import _FEES
        cited={eid for claim in rendered.get('claims',[])for eid in claim.get('evidence_ids',[])}
        has_fees=any('fees'in hit.get('facets',[])for eid in cited
            for hit in self.evidence.get(eid,{}).get('data',{}).get('hits',[]))
        if not has_fees and not _FEES.search(rendered.get('text','')):return rendered
        # Only the existing structured public-answer protocol obtains this
        # delivery contract; private/legacy answers keep their original path.
        if self._fee_delivery_context is None and self._joint_source_plan is None:return rendered
        self.fresh();self.amount_contract(rendered)
        # Validate authored fields only after the unchanged recoverable gates.
        # Bind their proof to the actual renderer result (NFKC is the existing
        # boundary display transform); no new repair or rewriting is allowed.
        from .public_fee_statement_contract import build_catalog, validate_authored, extend_time_catalog
        def statement_proof(sources,context):
            try:
                args=self._fee_statement_args;snapshot=self._fee_statement_rendered
                if (self._joint_source_digest(args)!=context['restored_args_sha256']
                        or snapshot is None or any(unicodedata.normalize('NFKC',rendered[key])!=
                            unicodedata.normalize('NFKC',snapshot[key])for key in ('text',))
                        or rendered['claims']!=snapshot['claims']
                        or rendered['followups']!=snapshot['followups']):self._joint_source_invalid()
                cited={eid for claim in args['claims']for eid in claim['evidence_ids']}
                catalog=extend_time_catalog(build_catalog(sources,self.knowledge._pinned_status['provider_id']),
                    self.evidence,cited_ids=cited)
                for unit in catalog['units']:
                    if unit['kind']!='time_exact':continue
                    for witness in unit['witnesses']:
                        entry=self.evidence[witness['evidence_id']]
                        receipt=self.knowledge._delivered.get((witness['citation_id'],witness['as_of']))
                        hits=[hit for hit in entry['data']['hits']if all(hit.get(key)==witness.get(key)
                            for key in ('citation_id','product_id','share_class_id','channel_id','release_id',
                                'document_id','document_version_id'))]
                        if (entry.get('context_hash')!=self.start_context or not receipt
                                or len(hits)!=1 or receipt[0]!=hits[0]
                                or hashlib.sha256(receipt[0]['excerpt'].encode()).hexdigest()!=witness['source_sha256']):
                            self._joint_source_invalid()
                return validate_authored(args,catalog)
            except (KeyError,ValueError,TypeError,AttributeError):self._joint_source_invalid()
        if self._joint_source_plan is not None:
            if self._joint_source_plan.get('status')!='consumed':self._joint_source_invalid()
            sources=self._fee_source_quote_rows(rendered)
            self._validate_public_fee_delivery(rendered,sources)
            proof=statement_proof(sources,self._joint_source_plan['context'])
            if not self.joint_source_receipts:self._joint_source_invalid()
            receipt=deepcopy(self.joint_source_receipts[-1])
            receipt.update(contract='navren.public-quote-joint-source-rendering/2',fee_statement_proof=proof)
            self.joint_source_receipts[-1]=receipt
            self.metric_part_receipts[-1]['joint_source_rendering']=deepcopy(receipt)
            return rendered
        # All original recoverable gates have passed. A failure in the new
        # source contract is terminal, never a fresh unrestricted model draft.
        self._joint_source_plan={'status':'preparing'}
        try:
            sources=self._fee_source_quote_rows(rendered)
            context=self._fee_delivery_context
            proof=statement_proof(sources,context)
            plan={'status':'consumed','arguments_sha256':context['restored_args_sha256'],
                'context':deepcopy(context),'sources':sources}
            selected=self.metric_part_receipts[-1]['selected_public_quote_fact_ids']
            if len(selected)+len(sources)>6:self._joint_source_invalid()
            result,receipt=self._render_joint_source_quotes(rendered,plan)
            receipt.update(contract='navren.public-quote-joint-source-rendering/2',fee_statement_proof=proof)
            self._validate_public_fee_delivery(result,sources)
            self.fresh();self.amount_contract(result)
            # Commit a receipt only for a fully checked result. The sentinel
            # makes new assembly failures fatal without spending a fresh repair.
            self._joint_source_plan=plan
            self.joint_source_receipts.append(deepcopy(receipt))
            self.metric_part_receipts[-1].update(joint_source_rendering=deepcopy(receipt),
                rendered_pre_boundary_text_sha256=hashlib.sha256(result['text'].encode()).hexdigest())
            return result
        except (KeyError,ValueError,TypeError,MetricClaimError):self._joint_source_invalid()

    def _render_public_quote_parts(self, args, *, joint_source_plan=None):
        self.fresh()
        eligible=set(self.public_quote_fact_ids());pieces=[];quotes=[];selected=[];source_bindings=[];displayed=set();duplicate_references=[]
        for part in args['reply_parts']:
            if 'text' in part:
                pieces.append(part['text'])
                continue
            fid=part['public_quote_fact_id']
            if fid not in eligible:
                raise BankError('PUBLIC_QUOTE_FACT_UNAVAILABLE','公开引文不属于本轮可用原件。',409)
            if fid in displayed:
                duplicate_references.append(fid)
                continue
            displayed.add(fid)
            fact=self.facts[fid];display_text=unicodedata.normalize('NFKC',fact['text']);quote='“'+display_text+'”'
            pieces.append(quote);quotes.append({'text':quote,'evidence_ids':[fact['evidence_id']]});selected.append(fid)
            source_bindings.append({'fact_id':fid,'evidence_id':fact['evidence_id'],
                'canonical_source_text_sha256':hashlib.sha256(fact['text'].encode()).hexdigest(),
                'nfkc_display_text_sha256':hashlib.sha256(display_text.encode()).hexdigest()})
        rendered={key:deepcopy(value)for key,value in args.items()if key!='reply_parts'}
        rendered.update(text=''.join(pieces),amount_claims=[],claims=deepcopy(args['claims'])+quotes)
        validate(rendered,SPECS['answer_reviewed'][1])
        joint_receipt=None
        if joint_source_plan is not None:
            if len(selected)+len(joint_source_plan['sources'])>6:self._joint_source_invalid()
            rendered,joint_receipt=self._render_joint_source_quotes(rendered,joint_source_plan)
        self.metric_part_receipts.append({'contract':'navren.public-quote-parts-rendering/1',
            'raw_reply_parts_sha256':hashlib.sha256(encoded(args['reply_parts']).encode()).hexdigest(),
            'rendered_pre_boundary_text_sha256':hashlib.sha256(rendered['text'].encode()).hexdigest(),
            'selected_public_quote_fact_ids':selected,'duplicate_public_quote_references_not_displayed':duplicate_references,'quote_claims_sha256':hashlib.sha256(encoded(quotes).encode()).hexdigest(),
            'quote_display_normalization':'NFKC','canonical_source_texts_unchanged':True,'quote_source_bindings':source_bindings,
            'host_public_quote_rendering':True,'host_metric_rendering':False,'model_original_answer_validated':False,
            'no_financial_calculation':True,'no_personal_entitlement':True,'additional_primary_calls':0,
            'additional_business_calls':0,'legacy_draft_repaired':False,
            'original_guards_status':'pending_read_amount_evidence_and_GLM'})
        if joint_receipt is not None:
            self.metric_part_receipts[-1]['joint_source_rendering']=deepcopy(joint_receipt)
            self.joint_source_receipts.append(deepcopy(joint_receipt))
        return rendered

    def _public_quote_answer_sections(self, args, public_ids):
        """Validate only typed sections and existing resource limits; no prose inference."""
        def invalid():
            raise BankError('INVALID_TOOL_ARGUMENTS','公开答复片段结构或额度不合法。',422)
        if (not isinstance(args,dict) or not {'direct_answer','conditions','unknowns'}<=set(args)
                or {'text','reply_parts','metric_selections','public_quotes'}&set(args)):invalid()
        variants=public_quote_reply_parts_schema(public_ids)['items']['oneOf']
        sections=[];part_count=0;quote_count=0
        for field,maximum in (('direct_answer',1200),('conditions',600),('unknowns',600)):
            rows=[args[field]] if field=='direct_answer' else args[field]
            if not isinstance(rows,list) or (field!='direct_answer' and len(rows)>6):invalid()
            for row in rows:
                if not isinstance(row,list) or not 1<=len(row)<=64:invalid()
                text=[];has_quote=False
                for part in row:
                    if not isinstance(part,dict) or set(part) not in ({'text'},{'public_quote_fact_id'}):invalid()
                    part_count+=1
                    if part_count>64:invalid()
                    if 'text' in part:
                        schema=deepcopy(variants[0]);schema['properties']['text']['maxLength']=maximum
                        validate(part,schema);text.append(part['text'])
                    else:
                        validate(part,variants[1]);quote_count+=1;has_quote=True
                        if quote_count>6:invalid()
                authored=''.join(text)
                if len(authored)>maximum or (not authored.strip() and (field=='direct_answer' or not has_quote)):invalid()
                sections.append((field,row))
        if part_count>64 or not 0<=quote_count<=6:invalid()
        return sections

    def render_public_quote_answer(self, args):
        self.validate_answer_arguments(args)
        sections=self._public_quote_answer_sections(args,self.public_quote_fact_ids())
        # Source limits belong only to the exact eligible citation selected below.
        parts=[];labelled=set();source_limits=[];noted=set()
        limit='这部分费用引文本身未核实销售渠道及其优惠折扣，不能据此确认渠道当前报价。'
        for field,row in sections:
            if field!='direct_answer' and field not in labelled:
                parts.append({'text':('适用条件：\n' if field=='conditions' else '尚未核实：\n')})
                labelled.add(field)
            for part in row:
                parts.append(deepcopy(part));fid=part.get('public_quote_fact_id')
                if not fid or fid in noted:continue
                noted.add(fid);fact=self.facts[fid];evidence=self.evidence[fact['evidence_id']]
                if evidence.get('source')!='public_knowledge' or evidence.get('tool')!='knowledge_search':continue
                data=evidence['data'];hit=next(h for h in data['hits']if h.get('citation_id')==fact.get('citation_id'))
                if ('channel_id' not in hit or hit['channel_id'] is not None or 'fees' not in hit.get('facets',[])
                        or not all(isinstance(hit.get(k),str)and hit[k]for k in ('product_id','share_class_id'))):continue
                parts.append({'text':'\n来源局限：'+limit+'\n'})
                source_limits.append({'fact_id':fid,'evidence_id':fact['evidence_id'],'citation_id':fact['citation_id'],
                    'product_id':hit['product_id'],'share_class_id':hit['share_class_id'],'channel_id':None,
                    'provider_id':data['provider_id'],'release_id':hit['release_id'],'as_of':data['as_of'],
                    'canonical_source_text_sha256':hashlib.sha256(fact['text'].encode()).hexdigest(),
                    'code':'SELECTED_FEE_CITATION_CHANNEL_UNBOUND','text':limit})
            parts.append({'text':'\n'})
        envelope={k:deepcopy(v)for k,v in args.items()if k not in {'direct_answer','conditions','unknowns'}}
        envelope['claims'].extend({'text':limit,'evidence_ids':[item['evidence_id']]}for item in source_limits)
        plan=self._joint_source_plan
        if plan is not None:
            if (plan.get('status')!='prepared'
                    or plan['arguments_sha256']!=self._joint_source_digest(args)):
                self._joint_source_invalid()
            plan['status']='consumed'
        result=self._render_public_quote_parts({**envelope,'reply_parts':parts},joint_source_plan=plan)
        self.metric_part_receipts[-1].update(answer_contract='navren.public-quote-answer/1',
            raw_structured_answer_sha256=hashlib.sha256(encoded(args).encode()).hexdigest(),
            model_authored_sections=['direct_answer','conditions','unknowns'],
            model_part_count=sum(len(row)for _,row in sections),
            quote_reference_count=sum('public_quote_fact_id'in part for _,row in sections for part in row),
            host_generated_conclusion=False,host_generated_source_limit=bool(source_limits),
            source_limit_receipts=source_limits,semantic_completion='pending_independent_GLM')
        self._fee_statement_rendered=deepcopy(result)
        return result

    def validate_answer_arguments(self, args):
        """Validate the selected protocol without changing the shared legacy SPECS."""
        public_ids=self.public_quote_fact_ids() if self.metric_selection_mode and not self.metrics.metric_ids else []
        if public_ids:
            self._public_quote_answer_sections(args,public_ids)
            envelope={key:value for key,value in args.items()if key not in {'direct_answer','conditions','unknowns'}}
            schema=deepcopy(SPECS['answer_reviewed'][1]);schema['properties']['amount_claims']=array(obj(),0)
            # validate() does not implement oneOf; each part was checked against
            # its exact existing variant above. Keep the original envelope guard.
            validate({**envelope,'text':'validated public sections envelope'},schema)
            return
        if isinstance(args,dict) and 'reply_parts' in args:
            public_ids=self.public_quote_fact_ids() if not self.metrics.metric_ids else []
            if not self.metric_selection_mode or not (self.metrics.metric_ids or public_ids):
                raise BankError('METRIC_PARTS_UNAVAILABLE','本轮未提供金额片段合同。',403)
            if 'text' in args or 'metric_selections' in args or args.get('amount_claims'):
                raise BankError('METRIC_PARTS_MIXED','金额片段不能混用整段正文、编号选择或模型金额签名。',422)
            parts=args['reply_parts']
            if not isinstance(parts,list) or not 1<=len(parts)<=64:
                raise BankError('METRIC_PARTS_INVALID','回复片段数量不合法。',422)
            schemas=(metric_reply_parts_schema(self.metrics.metric_ids,self._statistic_session.ids) if self.metrics.metric_ids
                     else public_quote_reply_parts_schema(public_ids))['items']['oneOf']
            reference='metric_id' if self.metrics.metric_ids else 'public_quote_fact_id'
            for part in parts:
                if not isinstance(part,dict) or set(part) not in ({'text'},{reference},*([{'statistic_id'}]if self.metrics.metric_ids and self._statistic_session.ids else [])):
                    raise BankError('METRIC_PARTS_INVALID','每个回复片段只允许文字或一个指标引用。',422)
                validate(part,schemas[0] if 'text' in part else schemas[2] if 'statistic_id' in part else schemas[1])
            envelope={key:value for key,value in args.items() if key!='reply_parts'}
            envelope=self._statistic_claim_envelope(envelope)
            validate({**envelope,'text':'validated parts envelope'},SPECS['answer_reviewed'][1])
        elif isinstance(args,dict) and 'metric_selections' in args:
            if not self.metric_selection_mode:
                raise BankError('METRIC_SELECTION_UNAVAILABLE','本轮未启用金额指标选择合同。',403)
            if isinstance(args.get('amount_claims'),list) and args['amount_claims']:
                raise BankError('METRIC_SELECTION_MIXED','金额选择不能混用模型填写的金额签名。',422)
            validate(args,metric_selection_answer_schema(self.metrics.metric_ids))
        else:
            validate(args,SPECS['answer_reviewed'][1])

    def expand_metric_selections(self, args):
        """Copy only the explicitly selected live metric; never choose by amount."""
        self.validate_answer_arguments(args)
        if 'metric_selections' not in args:return args
        self.fresh()
        try:
            occurrences=monetary_occurrences(args['text'])
            claims=[];seen=set()
            for selection in args['metric_selections']:
                index=selection['occurrence_index']
                if index in seen:
                    raise BankError('METRIC_SELECTION_DUPLICATE','同一金额位置只能选择一个指标。',422)
                if type(index) is not int or not 0<=index<len(occurrences):
                    raise BankError('METRIC_SELECTION_INDEX','金额选择位置超出当前正文。',422)
                seen.add(index)
                record=self.metrics.lookup(selection['metric_id'])
                metric=record['metric'];literal=occurrences[index]
                if ((metric['value_minor'],metric['currency'])!=(literal['value_minor'],literal['currency'])
                        or type(metric['value_minor']) is not int):
                    raise BankError('METRIC_SELECTION_VALUE_MISMATCH','所选指标的数值或币种与正文金额不符。',422)
                claims.append({**self.metrics.binding(selection['metric_id']),
                    'text':args['text'][literal['start']:literal['end']],
                    'occurrence_start':literal['start']})
        except MetricClaimError as error:
            raise BankError(error.code,'金额选择须对应本轮正式指标和标准金额表达。',422) from error
        expanded={key:deepcopy(value) for key,value in args.items() if key!='metric_selections'}
        expanded['amount_claims']=claims
        self.metric_selection_receipts.append({
            'contract':'navren.metric-selection-expansion/1',
            'text_sha256':hashlib.sha256(args['text'].encode()).hexdigest(),
            'selections_sha256':hashlib.sha256(encoded(args['metric_selections']).encode()).hexdigest(),
            'expanded_claims_sha256':hashlib.sha256(encoded(claims).encode()).hexdigest(),
            'selected_metric_ids':[row['metric_id'] for row in args['metric_selections']],
            'occurrence_indices':[row['occurrence_index'] for row in args['metric_selections']],
            'monetary_occurrence_count':len(occurrences),'expanded_claim_count':len(claims),
            'text_unchanged':expanded['text']==args['text'],
            'model_full_signature_supplied':False,'legacy_draft_repaired':False,
            'original_guards_status':'pending_read_amount_evidence_and_GLM'})
        return expanded

    def _statistic_claim_envelope(self,args):
        result=deepcopy(args)
        if not isinstance(result.get('claims'),list):return result
        for index,claim in enumerate(result['claims']):
            if isinstance(claim,dict) and set(claim)=={'statistic_id'}:
                atom=self._statistic_session.expand(claim['statistic_id'])
                result['claims'][index]={'text':atom['text'],'evidence_ids':atom['evidence_ids']}
        return result

    def render_metric_parts(self, args):
        """Render an explicit current metric reference; no amount calculation."""
        self.validate_answer_arguments(args)
        if 'reply_parts' not in args:
            self._statistic_session.bind_render(args,{}, {})
            return args
        if not self.metrics.metric_ids:return self._render_public_quote_parts(args)
        self.fresh()
        pieces=[];claims=[];position=0;stat_ranges={};stat_ids={}
        unit_normalizations=[];previous_cny_metric=None
        try:
            for part_index,part in enumerate(args['reply_parts']):
                preceding_cny_metric=previous_cny_metric;previous_cny_metric=None
                if 'text' in part:
                    literal=part['text']
                    # Only the immediately preceding explicit, validated CNY
                    # metric owns this unit. Never rewrite words or free text.
                    if (preceding_cny_metric is not None
                            and re.match(r'^元(?=$|[\s，。；：、！？,.;:!?）)\]】》」』])',literal)):
                        metric_index,metric_id,host_literal=preceding_cny_metric
                        original_literal=literal;literal=literal[1:]
                        unit_normalizations.append({
                            'metric_part_index':metric_index,'text_part_index':part_index,
                            'metric_id':metric_id,'currency':'CNY',
                            'removed_text_span':[0,1],'removed_text':'元',
                            'final_body_boundary_offset':position,
                            'host_amount_sha256':hashlib.sha256(host_literal.encode()).hexdigest(),
                            'raw_text_sha256':hashlib.sha256(original_literal.encode()).hexdigest(),
                            'normalized_text_sha256':hashlib.sha256(literal.encode()).hexdigest()})
                elif 'statistic_id' in part:
                    atom=self._statistic_session.expand(part['statistic_id']);literal=atom['text']
                    record=self.metrics.lookup(atom['metric_id']);metric=record['metric']
                    parsed=monetary_occurrences(literal)
                    if len(parsed)!=1 or (parsed[0]['value_minor'],parsed[0]['currency'])!=(metric['value_minor'],metric['currency']):
                        raise BankError('STATISTIC_BINDING_INVALID','统计原子绑定无效。',422)
                    row=parsed[0]
                    claims.append({**self.metrics.binding(atom['metric_id']),'text':literal[row['start']:row['end']],
                        'occurrence_start':position+row['start']})
                    stat_ranges.setdefault('text',[]).append((position,position+len(literal)))
                    stat_ids.setdefault('text',[]).append(part['statistic_id'])
                else:
                    mid=part['metric_id'];record=self.metrics.lookup(mid);metric=record['metric']
                    if type(metric['value_minor']) is not int:
                        raise BankError('METRIC_PARTS_VALUE_UNAVAILABLE','所选指标没有可展示的已核验金额。',422)
                    if not isinstance(metric['currency'],str) or not re.fullmatch(r'[A-Z]{3}',metric['currency']):
                        raise BankError('METRIC_PARTS_CURRENCY_UNSUPPORTED','所选指标的币种不能按原金额合同展示。',422)
                    literal=money(metric['value_minor'],metric['currency'])
                    parsed=monetary_occurrences(literal)
                    if (len(parsed)!=1 or parsed[0]['start']!=0 or parsed[0]['end']!=len(literal)
                            or (parsed[0]['value_minor'],parsed[0]['currency'])!=(metric['value_minor'],metric['currency'])):
                        raise BankError('METRIC_PARTS_CURRENCY_UNSUPPORTED','所选指标的币种不能按原金额合同展示。',422)
                    claims.append({**self.metrics.binding(mid),'text':literal,'occurrence_start':position})
                    if metric['currency']=='CNY' and literal.endswith('元'):
                        previous_cny_metric=(part_index,mid,literal)
                pieces.append(literal);position+=len(literal)
            rendered={key:deepcopy(value)for key,value in args.items()if key!='reply_parts'}
            rendered=self._statistic_claim_envelope(rendered)
            for index,claim in enumerate(args.get('claims',[])):
                if isinstance(claim,dict) and set(claim)=={'statistic_id'}:
                    field='claims['+str(index)+'].text'
                    stat_ranges[field]=[(0,len(rendered['claims'][index]['text']))]
                    stat_ids[field]=[claim['statistic_id']]
            rendered.update(text=''.join(pieces),amount_claims=claims)
            # Original whole-body length and 24-claim ceiling remain authoritative.
            validate(rendered,SPECS['answer_reviewed'][1])
            actual={(row['start'],row['end'],row['value_minor'],row['currency'])
                for row in monetary_occurrences(rendered['text'])}
            for claim in claims:
                expected=(claim['occurrence_start'],claim['occurrence_start']+len(claim['text']),
                          claim['value_minor'],claim['currency'])
                if expected not in actual:
                    raise BankError('METRIC_PARTS_SPAN','指标金额片段与最终正文解析位置不一致。',422)
        except MetricClaimError as error:
            raise BankError(error.code,'金额片段须符合原完整金额合同。',422) from error
        self._statistic_session.bind_render(rendered,stat_ranges,stat_ids)
        self.metric_part_receipts.append({'contract':'navren.metric-parts-rendering/1',
            'raw_reply_parts_sha256':hashlib.sha256(encoded(args['reply_parts']).encode()).hexdigest(),
            'rendered_pre_boundary_text_sha256':hashlib.sha256(rendered['text'].encode()).hexdigest(),
            'expanded_claims_sha256':hashlib.sha256(encoded(claims).encode()).hexdigest(),
            'selected_metric_ids':[part['metric_id']for part in args['reply_parts']if'metric_id'in part],
            'rendered_metric_count':len(claims),'host_metric_rendering':True,
            'boundary_unit_normalizations':unit_normalizations,
            'model_original_answer_validated':False,'no_financial_calculation':True,
            'additional_primary_calls':0,'additional_business_calls':0,
            'legacy_or_selector_draft_repaired':False,
            'original_guards_status':'pending_read_amount_evidence_and_GLM'})
        return rendered

    @contextmanager
    def security_call(self,*,force_new=False,phase='tool'):
        if self._audit_active is not None and not force_new:
            yield
            return
        self._audit_call_index+=1
        event_id=('tool:'+str(self._audit_call_index) if self._audit_request_id is not None
            else 'invocation:'+self._audit_invocation_ref+':tool:'+str(self._audit_call_index))
        previous=self._audit_active,self._audit_operations
        self._audit_active,self._audit_operations=event_id,[]
        try:
            with self.g.security_events.request(self.w,request_id=self._audit_request_id,
                    event_id=event_id,phase=phase):
                try:yield
                except Exception as error:
                    self.security_rejected(error,reuse_marker=not force_new)
                    raise
        finally:self._audit_active,self._audit_operations=previous

    def security_rejected(self,error,*,reuse_marker=True):
        _host_security_rejection(self.g,self.w,error,request_id=self._audit_request_id,
            event_id=self._audit_active,operation_ids=tuple(self._audit_operations),scope_active=True,reuse_marker=reuse_marker)

    def security_runtime_rejected(self,error,*,supplier=False):
        with self.security_call(phase='runtime'):
            if supplier:self.g.security_events.observe(self.w,'SUPPLIER_UNAVAILABLE',outcome='fault')
            else:self.security_rejected(error)

    def security_arguments_invalid(self):
        self.g.security_events.observe(self.w,'TOOL_ARGUMENTS_INVALID')

    def _security_add_operations(self,identifiers):
        # Target references do not grant identity; only persisted own objects bind.
        values=[x for x in identifiers if type(x) is str and re.fullmatch('[a-f0-9]{32}',x)]
        with closing(self.g.bank.connect()) as db:
            for value in values[:32]:
                if (value not in self._audit_operations and db.execute(
                        'SELECT 1 FROM operations WHERE workspace_id=? AND id=?',(self.w,value)).fetchone()):
                    self._audit_operations.append(value)

    def invoke(self,name,args):
        with self.security_call(force_new=True):
            if name in ('operation_status','withdraw_draft') and isinstance(args,dict):
                self._security_add_operations([args.get('operation_id')])
            output=self._invoke(name,args)
            data=output.get('data') if isinstance(output,dict) else None
            if isinstance(data,dict):
                operation=data.get('operation')
                identifiers=[operation.get('id')] if isinstance(operation,dict) else []
                if isinstance(data.get('superseded_operations'),list):
                    identifiers.extend(x.get('operation_id') for x in data['superseded_operations']
                        if isinstance(x,dict))
                self._security_add_operations(identifiers)
            evidence=([self.start_context] if type(self.start_context) is str
                and re.fullmatch('[a-f0-9]{64}',self.start_context) else [])
            for operation in self._audit_operations or [None]:
                self.g.security_events.allowed(self.w,operation_id=operation,evidence_sha256=evidence)
            return output

    def _invoke(self, name, args):
        self.request_privacy.require_tool(name)
        if name not in SPECS:
            raise BankError('TOOL_FORBIDDEN', '该工具不在允许调用范围。', 403)
        result_reference_parts = (name == 'answer_reviewed' and self.metric_selection_mode
            and isinstance(args, dict) and 'reply_parts' in args)
        if name=='answer_reviewed':
            self.validate_answer_arguments(args)
            args=(self.render_public_quote_answer(args) if 'direct_answer' in args else
                  self.render_metric_parts(args) if 'reply_parts' in args else self.expand_metric_selections(args))
        else:validate(args, SPECS[name][1])
        if name == 'answer_reviewed' and self.metrics.metric_ids and 'amount_claims' not in args:
            raise BankError('METRIC_CLAIMS_REQUIRED',
                '本轮已有正式指标，answer_reviewed必须显式提交amount_claims；普通claims不能代替金额签名。无个人金额时提交空数组。',422)
        if self._history_reference is None and 'historical_quote_ids' in args:
            raise BankError('HISTORY_CONTEXT_REQUIRED','历史引文需要本轮受信历史引用。',403)
        if self._history_reference is not None:self._require_history_tool(name,args)
        statement_query = self.statement_fact_query or self.income_statement_query
        if (statement_query and name == 'monthly_statement'
                and args.get('month') != statement_query['month']):
            raise BankError('READ_COMPLETION_REQUIRED', '请沿用本次账单查询月份：' + statement_query['month'] + '。')
        if name.startswith('market_'):args=self._market_arguments(name,args)
        self.fresh()
        catalog_information=getattr(self.g,'catalog_information_observer',None)
        if catalog_information is not None:
            catalog_information.assert_fresh()
            catalog_information.require_tool(name)
        if name == 'run_generated_code':
            from .generated_code import GeneratedCodeSession
            if self.generated_code_session is None:
                audit_dir=self.g.bank.database.parent/'generated-code-audit'/hashlib.sha256(self.request_id.encode()).hexdigest()
                self.generated_code_session=GeneratedCodeSession(self,audit_dir=audit_dir,
                    deadline=self.request_deadline)
            data=self.generated_code_session.execute(args)
            evidence_id='ev_'+self.request_id+'_sandbox_'+str(len(self.evidence)+1)
            self.evidence[evidence_id]={'id':evidence_id,'tool':name,'parameters':{'metric_id':args['metric_id']},
                'data':data,'source':'restricted_generated_code','context_hash':self.start_context}
            self.advice=True
            self.trace.append({'tool':name,'evidence_id':evidence_id,'status':'completed'})
            return {'evidence_id':evidence_id,'data':data,'facts':[]}
        if name in ('answer_facts','answer_reviewed'):
            cash_kind=cash_query_kind(self.user_text)
            if self._history_reference is None and not (name=='answer_reviewed' and self._structured_funds_completed(args,cash_kind)):
                try:
                    require_cash_query_complete(cash_kind, name, args, self)
                except ReadResultReferenceIncomplete as error:
                    if not result_reference_parts:
                        raise
                    # Only a failed result-coverage gate reaches this precheck.
                    # Reuse the complete original validator, including source
                    # versions and trusted public-quote spans; never repair text.
                    try:
                        self.amount_contract(args)
                    except BankError as amount_error:
                        if amount_error.status == 409:
                            raise ReadResultPrecheckFatal(amount_error) from amount_error
                        raise
                    error.amount_contract_checked = True
                    raise
            args = complete_statement_facts(statement_query, name, args, self)
            if name == 'answer_reviewed':
                statistic_before=deepcopy(args)
                args=self.interaction_contract(args)
                self._statistic_session.rebind_host_transform(statistic_before,args)
                if catalog_information is not None:
                    status=[e['data'].get('ready') for e in self.evidence.values() if e.get('tool')=='knowledge_status']
                    if len(status)!=1 or type(status[0]) is not bool:
                        raise BankError('CATALOG_INFORMATION_STATUS_REQUIRED','请先核对本轮实际资料检索状态。',409)
                    catalog_information.assert_answer(args,self.user_text,status[0])
                if self._history_reference is not None:args=self._compose_history_quotes(args)
                from .operation_claims import validate_operation_wording
                validate_operation_wording(args,self)
                from .public_quote_recovery import public_fee_source_rejections
                rejected=public_fee_source_rejections(args,self)
                if rejected:
                    from .knowledge_semantics import public_time_basis_bindings
                    time_basis_rejections=[item for item in public_time_basis_bindings(args,
                        [e for e in self.evidence.values()if e.get('context_hash')==self.start_context])
                        if item['answer_status']=='missing']
                    error=BankError('PUBLIC_FEE_SOURCE_REQUIRED',
                        '费用计提、频率、持有期限适用或净值作用缺少本轮同费种、同份额和同版本原文支持；只修答复及claims，不重新检索。',422)
                    error._fee_source_rejections=rejected
                    error._fee_time_basis_rejections=deepcopy(time_basis_rejections)
                    from .public_quote_recovery import has_deadline_endpoint
                    if time_basis_rejections and has_deadline_endpoint(args):
                        try:error._fee_time_endpoint_sources=self._joint_time_endpoint_sources(time_basis_rejections)
                        except (ValueError,KeyError,TypeError):self._joint_source_invalid()
                    raise error
                from .knowledge_semantics import public_time_basis_bindings
                time_bindings=public_time_basis_bindings(args,[e for e in self.evidence.values()
                    if e.get('context_hash')==self.start_context])
                missing=next((item for item in time_bindings if item['answer_status']=='missing'),None)
                if missing is not None:
                    error=BankError('PUBLIC_TIME_BASIS_REQUIRED',
                        '触发字段：text。保留具体'+missing['notation']+'支付或到账时限时，需保留同一版本原文的计日口径；'
                        '仅说明T日为开放日、或全文别处出现工作日，不能代替该期限的计日基础。'
                        '请按本轮引文'+missing['definition_citation_id']+'修正该期限及其适用条件；'
                        '若不作具体期限结论，不要为此补写期限。只修答复，不重新创建或执行操作。',422)
                    error._time_basis_rejection=deepcopy(missing)
                    raise error
                return args
            return self.aggregate_budget_facts(args)
        if name in ('presentation_query','presentation_detail'):
            if self.presentations is None:
                raise BankError('PRESENTATION_PROVIDER_UNAVAILABLE','本轮展示发行服务未装配。',503)
            entry=self.presentations.issue(name,args)
            presentation=entry['result']['presentation']
            data={'widget_id':presentation['view_id'],'review_projection':entry['review'],
                  'provider_object':deepcopy(entry['provider_object'])}
            evidence_id='e'+str(len(self.evidence)+1)+'-'+hashlib.sha256(encoded(data).encode()).hexdigest()[:12]
            records=self.presentations.register(self,entry,evidence_id)
            self.widgets[presentation['view_id']]={'type':'financial_presentation','schema_version':1,
                'widget_id':presentation['view_id'],'presentation':deepcopy(presentation)}
            self.evidence[evidence_id]={'id':evidence_id,'tool':name,'parameters':deepcopy(args),
                'data':data,'source':'base_presentation','context_hash':self.start_context}
            self.identifiers.setdefault('view_id',set()).add(presentation['view_id'])
            for point in presentation['points']:
                self.identifiers.setdefault('point_ref',set()).add(point['point_ref'])
                if 'item_ref' in point:self.identifiers.setdefault('item_ref',set()).add(point['item_ref'])
                self.identifiers.setdefault('item_ref',set()).update(x['item_ref'] for x in point.get('preview',[]))
            self.advice=True
            self.trace.append({'tool':name,'evidence_id':evidence_id,'status':'completed'})
            return {'evidence_id':evidence_id,'data':data,'facts':[],'metrics':records}
        if name.startswith('market_'):
            data=self._read_market(name,args)
            self._pin_market_read(name,args,data)
            if self._history_reference is not None:
                from .history_binding import public_view
                data=public_view(data)
                from .historical_quotes import public_quotes, public_quote_contract
                data['historical_quotes']=public_quotes(self.history_quote_session,self._market_store())
                data['historical_quote_contract']=public_quote_contract(self.history_quote_session,self._market_store())
                data['historical_numeric_rendering_supported']=bool(data['historical_quotes'])
                if data['historical_quotes']:
                    data['notice']='仅逐字引用本轮发行的历史公开原文；不是本人当前资金、比例建议或交易资格。'
            else:
                data=market_agent_view(name,data)
            self.advice=True
            def collect(value):
                if isinstance(value,dict):
                    if value.get('id','').startswith('a_') and 'url' in value:self.market_sources[value['id']]=value
                    for item in value.values():
                        if isinstance(item,(dict,list)):collect(item)
                elif isinstance(value,list):
                    for item in value:collect(item)
            collect(data)
        elif name in FINANCIAL_MCP_TOOL_NAMES:
            if self.financial_mcp is None:
                raise BankError(self.financial_mcp_unavailable or 'MCP_UNAVAILABLE',
                                '独立财务历史尚未启用或当前工作区未完成主体映射。', 503)
            try:
                data = self.financial_mcp.call(name, args)
            except FinancialMCPError as error:
                raise BankError(error.code, str(error), 503) from error
        elif name == 'calendar_query':
            view=self.g.calendar.query(self.w,args);widget=self.g.calendar.widget(view)
            self.widgets[widget['widget_id']]=widget
            data={k:view[k] for k in ('view_id','range','view','as_of','clock_mode','source_scope','coverage','summary_by_currency','limitations','portfolio','nav_asof','accounting_basis','holdings_pnl_minor')}
            if data.get('portfolio'):data['portfolio']={k:v for k,v in data['portfolio'].items()if k!='valuation_dates'}
            data.update(widget_id=widget['widget_id'],item_count=sum(d['item_count'] for d in view['days']),
                days=[{'date':d['date'],'item_count':d['item_count'],'preview':[{k:i.get(k) for k in ('id','title','kind','state','source_type')} for i in d['preview']]} for d in view['days']])
        elif name == 'wealth_versions':
            data=self.g.wealth.versions(self.w);data['archived_version_count']=sum(not v['active']for v in data['versions']);data['versions']=[{**v,'metadata':{k:x for k,x in v['metadata'].items()if k!='valuation_dates'}}for v in data['versions']if v['active']];data['products']=[{k:v for k,v in p.items()if k not in {'summary','report','feilv','corporate_actions','fee_rules'}}for p in data['products']]
        elif name == 'wealth_product':
            data=self.g.wealth.detail(self.w,args['fund_id'],args['on'],args['portfolio']);self.advice=True
            data['history_count']=len(data['history']);data['history']=[{'date':x['date'],'nav':x['nav'],'quantity':x['quantity'],'daily_pnl_minor':x['returns']['daily_pnl_minor']}for x in data['history'][-6:]]
            data['positions']=[{k:v for k,v in p.items()if k!='product'}for p in data['positions']]
            data['portfolio']={k:v for k,v in data['portfolio'].items()if k!='valuation_dates'}
            data['product']={k:v for k,v in data['product'].items()if k not in {'summary','report','feilv','corporate_actions'}}
            data['product']['fee_rules']={k:v for k,v in data['product'].get('fee_rules',{}).items()if k!='text'}
        elif name == 'calendar_day':data=self.g.calendar.detail(self.w,args['view_id'],args['date'],args.get('offset',0),args.get('limit',100))
        elif name == 'calendar_item':data=self.g.calendar.item(self.w,args['item_id'])
        elif name == 'calendar_budget_scenario':data=self.g.calendar.budget_scenario(self.w,args['account_id'],args['from'],args['to'])
        elif name == 'calendar_prepare_event':
            if args['action'] in ('edit','delete'):self.calendar_edit_attempted=True
            elif self.calendar_edit_attempted:
                raise BankError('CALENDAR_TARGET_REQUIRED','本轮已经请求修改/删除；应先查询原事件，不能改成创建重复事件。')
            if self.calendar_drafts:raise BankError('CALENDAR_DRAFT_LIMIT','本轮先核对一项日历变更。')
            data=self.g.calendar.events.prepare(self.w,args,agent=True);self.calendar_drafts.append(data['draft_id']);self.changed=True
            widget={'type':'calendar_event_draft','schema_version':1,'widget_id':data['draft_id'],'workspace_binding':self.w,'draft':data}
            self.widgets[widget['widget_id']]=widget;data['widget_id']=widget['widget_id']
        elif name == 'load_skill':
            try:
                data = load_advisor(args['name'], args.get('reference'))
            except ValueError as error:
                code = 'SKILL_FORBIDDEN' if str(error)=='reviewer_skill_forbidden' else 'INVALID_SKILL'
                raise BankError(code,'技能或参考文件不在主Agent可读目录中。',403 if code=='SKILL_FORBIDDEN' else 422) from error
        elif name == 'knowledge_status':
            data = self.knowledge.status()
        elif name == 'knowledge_search':
            try:
                data = self.knowledge.search(**args)
            except ValueError as error:
                raise BankError('STALE_KNOWLEDGE_EVIDENCE','知识来源、权限或原文版本已变化，请重新检索。',409) from error
            self.advice = True
        elif name == 'personal_graph':
            data=self.g.financial_graph.query(self.w,args,for_agent=True)
            data['goal_amount_basis'] = '目标saved_minor只是记录的已积累金额；没有本人确认的allocation或资金预留回执，不证明账户已分配、已预留或已付款。正式风险等级不对应配置百分比或确定回撤。'
        elif name == 'graph_scenario':
            data=self.g.financial_graph.scenario(self.w,args)
            self.advice=True
        elif name == 'remember_profile':
            data=self.g.personal_graph.remember(self.w,self.user_text,self.request_id,args)
        elif name == 'public_products':
            if args.get('view','current_disclosure')=='current_disclosure':
                data=self.g.public_products.read(args.get('query',''),limit=12)
            else:
                status=[e['data'].get('ready') for e in self.evidence.values() if e.get('tool')=='knowledge_status']
                if len(status)!=1 or type(status[0]) is not bool:
                    raise BankError('CATALOG_INFORMATION_STATUS_REQUIRED','请先核对本轮实际资料检索状态。',409)
                data=self.g.public_products.read(args.get('query',''),limit=12,view=args['view'])
                from local_knowledge.catalog_information import render_conditions
                # 受信派生草稿只供模型选择；原件六字段及其完整 SHA 不变。
                data['conditions_draft']=render_conditions(data,self.user_text,knowledge_ready=status[0])
            data['verified_nav_products']=[{'fund_id':x['product']['fund_id'],'name':x['product']['name'],'detail_url':x['product']['detail_url'],'nav_on':x['latest_nav']['nav_on']if x['latest_nav']else None,'unit_nav':x['latest_nav']['unit_nav']if x['latest_nav']else None,'current_channel_status':'unknown'}for x in data.get('verified_nav_products',[])]
            self.advice=True
        elif name == 'research_plans':
            data=self.g.research_plans.read(self.w)
            if args.get('plan_id'):
                data['items']=[p for p in data['items'] if p['id']==args['plan_id']]
                if not data['items']:raise BankError('PLAN_NOT_FOUND','研究备选不属于当前账户或已不可用。',404)
            self.advice=True
        elif name == 'customer_records':
            fields={'loans':['loans','loan_installments'], 'insurance':['insurance_policies','insurance_claims'],
                    'deposits':['deposit_contracts'], 'support':['disputes','support_cases'],
                    'permissions':['permission_grants','consents','account_limits','account_restrictions'],
                    'goals':['wealth_goals','budgets','advice_records']}[args['domain']]
            archive=self.g.bank.customer_archive(self.w)
            data={'as_of':archive['as_of'],'records':{k:archive['records'].get(k,[]) for k in fields},
                  'unavailable_entities':[k for k in fields if k not in archive['records']],
                  'notice':'缺失的档案表表示尚未接入，不能据此声称用户没有保险或保障。'}
        elif name == 'account_summary':
            s = self.g.overview(self.w)
            totals = {}
            for a in s['accounts']:
                totals[a['currency']] = totals.get(a['currency'], 0)+a['balance_minor']
            data = {k:s[k] for k in ('as_of','version','accounts','profile','notice')}
            data['cash_by_currency'] = [{'name':'现金合计','currency':c,'balance_minor':v} for c,v in totals.items()]
            data['holdings'] = s['holdings']
            products = {p['id']:p for p in self.g.bank.products(self.w)}
            accounts = {a['id']:a for a in s['accounts']}
            principal = {}
            for h in s['holdings']:
                c = position_currency(self.w, h, accounts, products)
                principal.setdefault(c, {'currency':c,'holding_principal_minor':0,'pending_redemption_minor':0})
                principal[c]['holding_principal_minor'] += h['amount_minor']
            for order in s['orders']:
                if order['status']=='pending_settlement':
                    c = position_currency(self.w, order, accounts, products)
                    principal.setdefault(c, {'currency':c,'holding_principal_minor':0,'pending_redemption_minor':0})
                    principal[c]['pending_redemption_minor'] += order['amount_minor']
            data['principal_summary'] = list(principal.values())
            data['financial_scope_basis'] = '*_minor金额字段是币种最小货币单位的整数，人民币为分，不能直接当作元；个人金额用本轮metric_id展示，不抄写字段数值。已结算现金、持仓本金、待到账赎回分别说明；持仓本金或解除锁定不等于已结算现金或赎回到账，当前现金预算不含仍属持仓本金或未到账赎回的金额，不能称这些持仓已加进可安排资金。仅凭持仓本金不能声称全部持仓被锁定或都有同一退出规则。正式风险等级是适当性条件，不对应权益比例或确定回撤；假设条件不覆盖正式画像。'
        elif name == 'budget':
            reserves = args.get('reserves', [])
            if reserves and args['account_id'] not in self.budget_baselines:
                raise BankError('BUDGET_BASELINE_REQUIRED', '请先对同一账户调用不带reserves的budget，读取已计入的到期义务及真实evidence_id；同一义务用commitment_ids关联后再算留款。')
            data = self.g.budget(self.w, args['account_id'], args.get('floor_minor', 0))
            account = next(a for a in self.g.overview(self.w)['accounts'] if a['id']==args['account_id'])
            data.update(account_id=account['id'],currency=account['currency'],name=account['name'])
            commitments = {c['evidence_id']:c for c in data['commitments'] if c['account_id']==args['account_id']}
            baseline = data['investable_minor']
            extra, used = 0, set()
            applied = []
            for reserve in reserves:
                ids = reserve['commitment_ids']
                if (len(set(ids)) != len(ids) or used.intersection(ids) or set(ids)-set(commitments)
                        or set(ids)-set(self.budget_baselines.get(args['account_id'], {}))):
                    raise BankError('INVALID_RESERVE_LINK', '留款的关联事项不存在或重复。')
                used.update(ids)
                linked = sum(commitments[i]['amount_minor'] for i in ids)
                additional = max(0, reserve['amount_minor']-linked)
                extra += additional
                applied.append({**reserve, 'already_budgeted_minor':linked,
                                'additional_reserve_minor':additional})
            if not reserves:
                self.budget_baselines[args['account_id']] = commitments
            data['baseline_investable_minor'] = baseline
            data['additional_reserve_minor'] = extra
            data['investable_minor'] = max(0, data['investable_minor']-extra)
            data['budget_scope'] = 'planning_scenario' if reserves else 'current_budget'
            data['planning_reserves'] = applied
            data['amount_basis'] = '*_minor金额字段以币种最小货币单位整数表示，人民币为分，不可直接当作元。当前预算的cash只取所选账户已结算现金，可安排资金再扣应急底线、生效预留及未来30天已知到期款；持仓本金及未到账赎回款未加进cash或可安排资金。解除持仓锁定仍不等于已结算或到账，不能因持仓出现在账户总览就称已计入现金预算。reserves仅做规划，不产生真实预留。空commitment_ids按全新事项扣全额，同一已知义务须关联去重；不能把情景金额说成未追加留款的当前预算。'
            self.current_budget = data
        elif name == 'statement_source': data = self.g.source(self.w)
        elif name == 'monthly_statement': data = self.g.query_read_capability(self.w,'statements.monthly_analysis',args)
        elif name == 'annual_statement': data = self.g.query_read_capability(self.w,'statements.annual_analysis',args)
        elif name == 'statement_details': data = self.g.query_read_capability(self.w,'statements.details',args)
        elif name == 'anomalies': data = self.g.anomalies(self.w)
        elif name == 'periodic_charges': data = self.g.periodic_charges(self.w)
        elif name == 'cashflow':
            data = self.g.cashflow_timeline(self.w, args['days'])
            data['available_amount_basis'] = 'available/minimum_available 是已扣应急底线与生效预留的底线外余量，与零比较判断缺口。正余量不破底线，不能再扣一次底线或与原底线金额比较。current_available尚未扣未来到期款，不能替代budget的当前可安排资金。'
            data['adequacy_basis'] = '这些结果仅验证本期已知事项是否破已设置底线；没有已核实必要生活支出、收入稳定性与目标条件，缓冲充分性未知，不能说薄、厚、足够或并不薄。'
            data['future_inflow_basis'] = 'confirmed_inflow 为零仅指本区间没有已确认未来入账，不证明用户无收入或历史收入资料缺失；未读取的档案只能称本次未核实。'
            for account in data.get('accounts', []):
                for field, flag in (
                        ('minimum_available_after_confirmed_minor','confirmed_floor_breached'),
                        ('minimum_available_if_candidates_occur_minor','candidate_floor_breached')):
                    amount = account.get(field)
                    account[flag] = amount < 0 if type(amount) is int else None
            self.cashflow_timeline = data
        elif name == 'money_calculation':
            values=args['values_minor'];operation=args['operation']
            if operation=='multiply':
                if len(values)!=1 or 'factor' not in args:
                    raise BankError('INVALID_CALCULATION','乘法需要一项金额和整数倍数。')
                calculated=values[0]*args['factor']
            elif 'factor' in args:raise BankError('INVALID_CALCULATION','只有multiply允许factor。')
            else:calculated=sum(values) if operation=='sum' else values[0]-sum(values[1:])
            data={'currency':args['currency'],'result_minor':calculated,'source':'user_input_scenario',
                  'notice':'纯假设计算，不是账户余额、正式风险画像或收益预测。'}
        elif name == 'products':
            data = self.g.bank.products(self.w)
            self.advice = True
        elif name == 'service_offer_rules':
            data = self.g.service_negotiations.rules()
            self._pin_service_rules(data)
            self.advice = True
        elif name == 'service_offer_preview':
            self._pin_service_rules(self.g.service_negotiations.rules())
            if self.service_preview is None:
                self.service_preview=self.g.service_negotiations.preview(self.w,
                    {'text':self.user_text,'share_memory':False})
                if self.service_preview.get('draft_id'):
                    with closing(self.g.bank.connect()) as db:
                        row=db.execute('SELECT * FROM service_negotiation_drafts WHERE id=? AND workspace_id=?',
                                       (self.service_preview['draft_id'],self.w)).fetchone()
                    self._service_snapshot['preview']={'draft_id':row['id'],
                        'sha256':self._service_fingerprint(dict(row))}
            data=deepcopy(self.service_preview)
            self.advice = True
        elif name == 'service_application_status':
            self._pin_service_rules(self.g.service_negotiations.rules())
            data=self.g.service_negotiations.applications(self.w)
            self._service_snapshot['applications_sha256']=self._service_fingerprint(data)
            self.advice = True
        elif name == 'business_records':
            fields = {'payments':['beneficiaries','scheduled_payments','split_bills','split_participants'],
                      'cards':['cards','card_applications'], 'subscriptions':['mandates','subscriptions'],
                      'tasks':['orchestration_tasks','fund_holds','service_quotes','service_reservations'],
                      'investments':['holdings','lots','orders']}[args['kind']]
            s = self.g.overview(self.w)
            data = {k:s[k] for k in fields}
            if args['kind']=='payments':
                # Current identity lookup only; no balances or guessed account.
                data['accounts']=[{key:row[key] for key in ('id','name','currency')}
                                  for row in s['accounts']]
            if args['kind']=='tasks':
                from financial_metrics import task_records_view
                data=task_records_view(s,self.g.bank.capabilities(),
                    self.g.bank.customer_archive(self.w)['records']['service_providers'])
        elif name == 'operation_status':
            data = self.g.operation_view(self.w, args['operation_id'])
        elif name == 'user_memory':
            state = self.g.memories.state(self.w)
            data = {'enabled':state['enabled'], 'items':[
                {k:r[k] for k in ('node_id','category','value','user_confirmed','origin')}
                for r in state['items'] if not r['expired']] if state['enabled'] else []}
        elif name == 'withdraw_draft':
            with self.g.bank.transaction() as db:
                r = db.execute('SELECT * FROM agent_draft_reviews WHERE operation_id=? AND workspace_id=?',
                               (args['operation_id'], self.w)).fetchone()
                if r is None: raise BankError('NOT_FOUND','当前账户没有这份Agent草案。',404)
                op = self.g.bank._operation(db, self.w, args['operation_id'])
                if op['state'] != 'pending_confirmation':
                    raise BankError('INVALID_STATE','只能撤回尚未确认的草案；已执行操作不能由对话撤销。',409)
                db.execute("UPDATE operations SET state='cancelled',verification_hash=NULL WHERE id=?", (op['id'],))
                db.execute("UPDATE agent_draft_reviews SET status='superseded' WHERE operation_id=?", (op['id'],))
                self.g.bank._audit(db,self.w,'agent_draft_withdrawn',{},op['id'])
            data = {'operation_id':op['id'],'state':'cancelled','executed':False,
                    'withdrawn_payload':json.loads(op['payload'])}
            self.changed = True
        elif name == 'prepare_operation':
            if self.drafts:
                raise BankError('ONE_OPERATION_AT_A_TIME','本轮已准备一项操作。请等确认后的实际回执，再准备后续操作。',409)
            with closing(self.g.bank.connect()) as db:
                unknown=db.execute("SELECT id FROM operations WHERE workspace_id=? AND state NOT IN "
                    "('pending_confirmation','succeeded','settled','pending_settlement','cancelled','invalidated','expired','failed','rejected') "
                    "ORDER BY created_at DESC LIMIT 1",(self.w,)).fetchone()
            if unknown:
                raise BankError('OPERATION_STATUS_REQUIRED','原操作状态尚未确定，请先查询原操作回执；不能准备重复付款。',409)
            # Agent 必须已经读取本轮对象；不允许根据客户端历史伪造已核验ID。
            for key, value in args.items():
                if key.endswith('_id') and key != 'product_id' and value not in self.identifiers.get(key,set()):
                    raise BankError('OBJECT_NOT_OBSERVED','请先读取当前账户中的 '+key+' 对象。',422)
                if key == 'product_id' and not any(e['tool']=='products' for e in self.evidence.values()):
                    raise BankError('OBJECT_NOT_OBSERVED','请先读取产品和正式风险匹配结果。',422)
                if key == 'quote_ids' and not set(value) <= self.identifiers.get('quote_id',set()):
                    raise BankError('OBJECT_NOT_OBSERVED','请先读取当前服务报价。',422)
            args = dict(args)
            # 额外留款是当前规划约束，不能让模型在准备时丢掉。
            if self.current_budget and args['action'] in ('purchase','transfer','reserve_funds'):
                if args['account_id'] != self.current_budget['account_id']:
                    raise BankError('BUDGET_ACCOUNT_MISMATCH','请读取付款账户的资金约束。',422)
                if args.get('amount_minor', 0) > self.current_budget['investable_minor']:
                    raise BankError('LIQUIDITY_GUARD','金额超过本轮去重留款后的可安排额度。',422)
                args['liquidity_floor_minor'] = max(args.get('liquidity_floor_minor',0),
                    self.current_budget['reserve_minor']+self.current_budget.get('additional_reserve_minor',0))
            key = 'agent-'+self.request_id+'-'+hashlib.sha256(encoded(args).encode()).hexdigest()[:20]
            view = self.g.prepare(self.w, {**args, 'idempotency_key':key}, agent_origin=True)
            op = view['operation']
            # Read the replacement relation recorded by prepare's transaction.
            # Old conversation text is not evidence that a draft was revoked.
            with closing(self.g.bank.connect()) as db:
                superseded = db.execute(
                    "SELECT a.operation_id,o.state FROM audit a JOIN operations o "
                    "ON o.id=a.operation_id AND o.workspace_id=a.workspace_id "
                    "WHERE a.workspace_id=? AND a.event='agent_draft_superseded' "
                    "AND json_extract(a.details,'$.replacement_id')=? ORDER BY a.id",
                    (self.w, op['id'])).fetchall()
            if any(row['state'] != 'invalidated' for row in superseded):
                raise BankError('OPERATION_STATUS_REQUIRED','旧草案状态已变化，请核对实际操作状态。',409)
            view = {**view, 'superseded_operations': [
                {'operation_id': row['operation_id'], 'state': 'invalidated',
                 'replacement_id': op['id']} for row in superseded]}
            self.drafts[op['id']] = view
            self.changed = True
            data = dict(view)
            if args['action'] in ('purchase','transfer','reserve_funds'):
                amount = args['amount_minor']
                data['projected_after_confirmation'] = {
                    'currency':view['account']['currency'],
                    'balance_minor':view['account']['balance_minor']-(amount if args['action']!='reserve_funds' else 0),
                    'investable_minor':max(0,view['budget']['investable_minor']-amount),
                    'notice':'这是草案按当前状态的计算，并非实际执行回执；确认时重新检查。'}
        else: raise BankError('TOOL_FORBIDDEN', '工具未实现。',403)
        self.fresh()
        data = safe_data(data)
        # Only a registered, side-effect-free optional read may be deferred.
        # Its complete original is checked before it becomes usable evidence.
        admit=getattr(self,'_optional_read_admission',None)
        if name=='personal_graph' and admit is not None:
            if not callable(admit):
                raise BankError('AGENT_BUDGET_ESTIMATOR_UNAVAILABLE','可选读取准入不可用。',503)
            candidate,output=self.preview_optional_read(name,args,data)
            admit(candidate,name,args,output)
        if name=='personal_graph':self.advice=True
        return self._record_result(name,args,data,catalog_information=catalog_information)

    def preview_optional_read(self,name,args,data):
        # Deliberately closed: other tools need their own no-side-effect proof.
        if name!='personal_graph':
            raise BankError('TOOL_FORBIDDEN','该工具不支持可选读取预览。',403)
        candidate=copy(self)
        for field in ('evidence','facts','drafts','trace','identifiers','widgets',
                      'metrics','ratios','metric_batches'):
            setattr(candidate,field,deepcopy(getattr(self,field)))
        candidate.advice=True
        output=candidate._record_result(name,args,deepcopy(data),catalog_information=None)
        # This optional tool must not issue monetary metrics or mutate a draft.
        if output['metrics'] or candidate.metric_batches!=self.metric_batches:
            raise BankError('AGENT_BUDGET_ESTIMATOR_UNAVAILABLE','可选读取发行合同已变化。',503)
        return candidate,output

    def _record_result(self,name,args,data,*,catalog_information=None):
        self.remember_identifiers(name,data)
        evidence_id = 'e'+str(len(self.evidence)+1)+'-'+hashlib.sha256(encoded(data).encode()).hexdigest()[:12]
        evidence_source = ('historical_public_statement_not_current' if self._history_reference is not None and name.startswith('market_') else 'financial_mcp_historical_synthetic' if name in FINANCIAL_MCP_TOOL_NAMES else
            'public_knowledge' if name=='knowledge_search' else 'local_knowledge_status' if name=='knowledge_status' else
            'verified_public_market' if name.startswith('market_') else ('catalog_information_'+data['catalog_information']['source_kind']+'_not_current' if 'catalog_information' in data else 'webank_public_disclosure') if name=='public_products' else
            'profile_sources' if name=='personal_graph' else 'conversation_candidate' if name=='remember_profile' else
            'maintainer_skill' if name=='load_skill' else 'simulation' if name not in ('monthly_statement','annual_statement','statement_details','money_calculation') else data.get('source'))
        # Every read above still ran through the actual provider and freshness
        # guards. Share only an identical, current-turn evidence original; the
        # caller still counts every tool attempt and receives the full result.
        parameters = safe_data(args)
        if name == 'knowledge_search' and data.get('ready') is True:
            for prior_id, prior in self.evidence.items():
                if (prior.get('id') == prior_id and prior.get('tool') == name
                        and prior.get('source') == evidence_source == 'public_knowledge'
                        and prior.get('context_hash') == self.start_context
                        and encoded(prior.get('parameters')) == encoded(parameters)
                        and encoded(prior.get('data')) == encoded(data)):
                    facts = [f for f in self.facts.values() if f['evidence_id'] == prior_id]
                    self.trace.append({'tool': name, 'evidence_id': prior_id,
                                       'status': 'completed', 'evidence_reused': True})
                    return {'evidence_id': prior_id, 'data': data,
                            'facts': facts, 'metrics': []}
        self.evidence[evidence_id] = {'id':evidence_id,'tool':name,'parameters':parameters,
                                      'data':data,'source':evidence_source,
                                      'context_hash':self.start_context}
        if name.startswith('market_'):self.add_market_facts(evidence_id,data)
        elif name=='knowledge_search':self.add_knowledge_facts(evidence_id,data)
        elif name in ('load_skill','knowledge_status'):pass
        elif name=='public_products' and catalog_information is not None:pass
        else:self.add_facts(evidence_id, data, name)
        metric_records=[] if self._history_reference is not None or catalog_information is not None else self._register_metrics(name,args,data,evidence_id)
        facts = [f for f in self.facts.values() if f['evidence_id']==evidence_id]
        self.trace.append({'tool':name,'evidence_id':evidence_id,'status':'completed'})
        output={'evidence_id':evidence_id,'data':data,'facts':facts,'metrics':metric_records}
        statistics=self._statistic_session.catalog(evidence_id)
        if statistics:output['statistics']=statistics
        return output

    def _bind_history_context(self,reference):
        from .history_binding import host_binding, read_exact, register_snapshot, render_literals
        binding=host_binding(reference,owner=self.g.page_contexts,workspace=self.w,market=self._market_store())
        requested=binding['requested']
        identity={k:reference[k] for k in ('kind','id','version','parameters')}
        identity.update(source_scope='market',execution_authority=False,reading_mode='historical_audit',
                        historical_only=True,current_evidence_eligible=False)
        if self.market_context is not None and self.market_context!=identity:
            raise BankError('MARKET_CONTEXT_CONFLICT','本轮已绑定另一市场版本，请重新选择。',409)
        packet=read_exact(self._market_store(),requested,expected_binding=binding)
        snapshot=register_snapshot(self._market_store(),binding,reference)
        self._history_reference=reference
        self._history_binding=deepcopy(binding)
        self._history_snapshot=snapshot
        self._history_render_literals=render_literals(packet)
        from .historical_quotes import issue_quotes
        self.history_quote_session=issue_quotes(self._market_store(),snapshot)
        self.market_context=identity
        return deepcopy(identity)

    def _assert_history_context(self):
        from .history_binding import host_binding
        binding=host_binding(self._history_reference,owner=self.g.page_contexts,workspace=self.w,market=self._market_store())
        if binding!=self._history_binding:
            raise BankError('STALE_HISTORY_EVIDENCE','历史资料绑定已变化。',409)

    def _assert_history_rendering(self,final):
        from .history_binding import require_safe_rendering
        self._assert_history_context()
        if self.history_quote_proof is not None:
            from .historical_quotes import assert_answer
            assert_answer(self._market_store(),self.history_quote_session,self.history_quote_proof,final)
            return
        require_safe_rendering(final,self._history_binding['requested'],self._history_render_literals)

    def _require_history_tool(self,name,args):
        wanted='market_'+self._history_binding['requested']['kind']
        if name not in {'load_skill',wanted,'answer_reviewed'}:
            raise BankError('HISTORY_READ_ONLY','历史解读不能读取当前财务、推荐或准备执行操作。',403)
        if name=='load_skill' and args.get('name')!='market':
            raise BankError('HISTORY_READ_ONLY','本轮历史解读只使用市场只读技能。',403)
        if name=='answer_reviewed':
            from .history_binding import require_safe_rendering
            require_safe_rendering(args,self._history_binding['requested'],self._history_render_literals)

    def _compose_history_quotes(self,final):
        from .historical_quotes import compose, seal_snapshot
        if self._history_boundary is None:
            raise BankError('HISTORY_CONTEXT_REQUIRED','历史引文的隐私边界尚未装配。',503)
        wanted='market_'+self._history_binding['requested']['kind']
        evidence_ids=[identifier for identifier,item in self.evidence.items()
                      if item['tool']==wanted and item['source']=='historical_public_statement_not_current']
        canonical,proof=compose(self._market_store(),self.history_quote_session,final,
            self._history_boundary,evidence_ids)
        self.history_quote_proof=proof
        self._history_snapshot=(seal_snapshot(self._market_store(),self.history_quote_session,proof)
                                if proof is not None else self.history_quote_session.snapshot)
        return canonical

    def history_quote_ids(self):
        if self.history_quote_session is None:return None
        from .historical_quotes import quote_ids
        return quote_ids(self.history_quote_session)

    def restore_history_final(self,final):
        if self.history_quote_proof is None:return final
        from .historical_quotes import assert_answer
        canonical=self.history_quote_proof.final
        expected=deepcopy(canonical)
        expected['text']=unicodedata.normalize('NFKC',expected['text'])
        if final not in (canonical,expected):
            raise BankError('HISTORY_QUOTE_CHANGED','历史答复完整内容已改变。',409)
        assert_answer(self._market_store(),self.history_quote_session,self.history_quote_proof,canonical)
        return canonical

    def validated_history_quote_spans(self,final,*,bank_db=None):
        if self.history_quote_proof is None:return ()
        from .historical_quotes import assert_answer
        if bank_db is not None:
            from .history_binding import assert_bank_session
            assert_bank_session(self.history_quote_session.snapshot,bank_db)
            locked=getattr(self.g.operation_evidence,'current_market_connection',None)
            if not callable(locked):
                raise BankError('HISTORY_PUBLICATION_GUARD_REQUIRED','历史引文的市场发布屏障尚未接线。',503)
            market_db=locked(self._market_store())
            return assert_answer(self._market_store(),self.history_quote_session,
                self.history_quote_proof,final,db=market_db)
        return assert_answer(self._market_store(),self.history_quote_session,self.history_quote_proof,final)

    def validated_history_quote_claims(self,final,*,bank_db=None):
        if self.history_quote_proof is None:return frozenset()
        self.validated_history_quote_spans(final,bank_db=bank_db)
        return frozenset(claim['text'] for claim in self.history_quote_proof.final['claims'])
