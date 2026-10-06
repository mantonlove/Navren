"""会话范围内的白名单工具；金额和权限由代码决定，不读取用户指定身份。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from decimal import Decimal, InvalidOperation
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone

from simulated_bank import BankError
from simulated_bank.agent_guard import context_hash
from simulated_bank.service import encoded
from .skills import catalog as skill_catalog, load_advisor
from .knowledge_retrieval import KnowledgeRetrieval
from .metric_claims import MetricRegistry, MetricClaimError, amount_claim_schema, monetary_occurrences
from .financial_mcp_client import FinancialMCPClient, FinancialMCPError, tool_specs


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
        'report_card_lost','request_card_limit','apply_card','request_split',
        'collect_split_payment','reserve_funds','release_funds','book_services']},
    **{k: STR for k in ('account_id','product_id','mandate_id','card_id','beneficiary_id',
                       'reference','next_due_on','end_on','scheduled_payment_id','split_id',
                       'participant_id','subscription_id','task_id','expires_on','hold_id')},
    **{k: MONEY for k in ('amount_minor','requested_limit_minor','limit_minor','liquidity_floor_minor')},
    'enabled': {'type': 'boolean'},
    'frequency': {**STR, 'enum': ['once','monthly']},
    'control_type': {**STR, 'enum': ['online','overseas','contactless','single_limit']},
    'requested_type': {**STR, 'enum': ['debit','credit']},
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
                        obj({'query':{'type':'string','maxLength':80}},())),
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
                         obj({'year': {'type': 'integer', 'minimum': 2025, 'maximum': 2100}}, ('year',))),
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
SPECS['answer_reviewed'][1]['properties']['amount_claims']=array(amount_claim_schema(),24)
SPECS['answer_reviewed']=(SPECS['answer_reviewed'][0]+' 每个个人金融金额片段在amount_claims中复制本轮metrics的完整绑定签名并填text；同数其他对象或期间不构成依据。无对应metrics时只作缺项说明或answer_facts，不能自造metric_id。',SPECS['answer_reviewed'][1])
FINANCIAL_MCP_TOOL_NAMES = frozenset(tool_specs())
SPECS.update(tool_specs())


def native_tools(facts=(), evidence=(), drafts=(), identifiers=None, widgets=(), metrics=(), *,
                 financial_mcp_tools=None):
    # JSON round-trip breaks shared schema dicts (STR/MONEY). deepcopy preserves
    # aliasing and would accidentally put participant IDs into account_id enums.
    specs = json.loads(json.dumps(SPECS))
    if financial_mcp_tools is not None:
        enabled = set(financial_mcp_tools)
        specs = {name: spec for name, spec in specs.items()
                 if name not in FINANCIAL_MCP_TOOL_NAMES or name in enabled}
    specs['load_skill'][1]['properties']['name']['enum'] = [item['name'] for item in skill_catalog()]
    specs['answer_reviewed'][1]['properties']['amount_claims']['items']=amount_claim_schema(metrics)
    for name in ('answer_facts','answer_reviewed'):
        if widgets: specs[name][1]['properties']['widget_ids']['items']['enum']=list(widgets)
        else: specs[name][1]['properties']['widget_ids']['maxItems']=0
    for name, key, values in [('answer_facts','fact_ids',facts), ('answer_reviewed','draft_ids',drafts)]:
        if values:
            specs[name][1]['properties'][key]['items']['enum'] = list(values)
    if evidence:
        specs['answer_reviewed'][1]['properties']['claims']['items']['properties']['evidence_ids']['items']['enum'] = list(evidence)
    commitment_ids = sorted((identifiers or {}).get('commitment_id', ()))
    if commitment_ids:
        specs['budget'][1]['properties']['reserves']['items']['properties']['commitment_ids']['items']['enum'] = commitment_ids
    for _, schema in specs.values():
        for field, choices in (identifiers or {}).items():
            if field in schema['properties'] and choices:
                schema['properties'][field]['enum'] = sorted(choices)
    return [{'type':'function','function':{'name': name,'description': description,'parameters': schema}}
            for name, (description, schema) in specs.items()]


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


class BankTools:
    def __init__(self, gateway, workspace, request_id):
        self.g, self.w, self.request_id = gateway, workspace, request_id
        self.financial_mcp = None
        self.financial_mcp_unavailable = 'CONFIGURATION_REQUIRED'
        try:
            self.financial_mcp = FinancialMCPClient.from_workspace(gateway, workspace)
            self.financial_mcp_unavailable = None
        except FinancialMCPError as error:
            self.financial_mcp_unavailable = error.code
        self.financial_mcp_tool_names = (self.financial_mcp.tool_names if self.financial_mcp else ())
        self.g.wealth.provision(workspace)
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
        self.metrics=MetricRegistry()
        self.metric_batches=[]
        self.service_preview=None
        self._service_snapshot=None
        self.widgets,self.calendar_drafts={},[]
        self.calendar_edit_attempted=False
        self.market_sources={}
        self.market_context=None
        self.market_reads={}
        self._default_market=None
        # 提供器只由本地应用代码注入，用户和模型没有配置索引、路径或身份的工具。
        self.knowledge = KnowledgeRetrieval(getattr(gateway, 'knowledge_provider', None))
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
        return [self.metrics.lookup(mid) for mid in registered]

    def amount_contract(self, final):
        self.fresh()
        versions={}
        for batch in self.metric_batches:
            for metric in batch['metrics']:
                for key,value in metric['source_versions'].items():
                    if key in versions and versions[key]!=value:
                        raise BankError('STALE_METRIC_SOURCE','同轮金融指标来源版本冲突，请重新查询。',409)
                    versions[key]=value
        public_spans=[]
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
            return self.metrics.validate_answer(final['text'],final.get('amount_claims',[]),
                current_source_versions=versions,public_quote_spans=public_spans)
        except MetricClaimError as error:
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
        if (not isinstance(reference,dict) or reference.get('kind') not in ('event','article','weekly')
                or reference.get('source_scope','market')!='market'
                or reference.get('execution_authority',False) is not False):
            raise BankError('MARKET_CONTEXT_INVALID','市场页面引用无效。')
        raw={k:deepcopy(v) for k,v in reference.items() if k!='execution_authority'}
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
        transient={'checked_at','fetched_at','queried_at','last_success_at','last_attempt_at',
                   'last_success_check_at','last_checked_at','coverage','history','schedule','updating'}
        def stable(value):
            if isinstance(value,list):return [stable(v) for v in value]
            if isinstance(value,dict):return {k:stable(v) for k,v in value.items() if k not in transient}
            return value
        return hashlib.sha256(encoded(stable(data)).encode()).hexdigest()

    def _pin_market_read(self,name,args,data):
        if name=='market_status':return
        key=encoded([name,args]);digest=self._market_digest(data)
        market=self._market_store();source=str(getattr(market,'path',id(market)))
        previous=self.market_reads.get(key)
        if previous and (previous['digest']!=digest or previous['source']!=source):
            raise BankError('STALE_MARKET_EVIDENCE','市场版本或来源状态已变化，请重新查阅。',409)
        if previous is None:self.market_reads[key]={'name':name,'args':deepcopy(args),'digest':digest,'source':source}

    def _market_fresh(self):
        if self.market_reads:self.assert_market_snapshot(self._market_store(),self.market_snapshot())

    def market_snapshot(self):
        """本地最终展示闸门的查询身份快照；不携带原文或用户金融数据。"""
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

    def add_facts(self, evidence_id, value, path='', currency='CNY'):
        if isinstance(value, dict):
            currency = value.get('currency', currency)
            groups = {'account_summary':'账户','profile':'资金设置','principal_summary':'理财本金',
                      'budget':'资金安排','totals_minor':'账单','cashflow':'资金预测',
                      'monthly_statement':'月度账单','annual_statement':'年度账单'}
            group = next((groups[p] for p in reversed(path.split('/')) if p in groups),'查询记录')
            prefix = value.get('name') or value.get('account_name') or value.get('plan_name') or value.get('merchant') or group
            if value.get('source_scope') not in ('user_confirmed','conversation'):
                prefix=re.sub(r'^模拟(?=备用账户|日常账户|活钱|工资|房租|餐饮|视频会员|云盘|健身会员|百货|在线课程|产品|服务)', '', str(prefix))
            for key, item in value.items():
                if key == 'opening_balance_minor': continue
                label = LABELS.get(key)
                if key == 'investable_minor' and value.get('budget_scope') == 'planning_scenario':
                    label = '情景可安排资金'
                if type(item) is int and (key.endswith('_minor') or label or path.endswith('_minor')):
                    is_money = key.endswith('_minor') or path.endswith('_minor')
                    display = money(item, currency) if is_money else str(item)
                    fact_id = 'f'+hashlib.sha256((evidence_id+path+'/'+key).encode()).hexdigest()[:20]
                    self.facts[fact_id] = {'id':fact_id,'evidence_id':evidence_id,'value':item,
                        'unit':'minor_currency' if is_money else 'scalar','currency':currency if is_money else None,
                        'text':str(prefix)+' · '+(label or key.removesuffix('_minor'))+'：'+display}
                elif isinstance(item, (dict,list)):
                    self.add_facts(evidence_id, item, (str(prefix)+'/'+key).strip('/'), currency)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                self.add_facts(evidence_id, item, path+'/'+str(i), currency)

    def add_market_facts(self, evidence_id, data):
        """公开金额有独立单位和来源，不能成为账户余额或纯事实免审通道。"""
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
                        'text':row['text'],'scope':'public_statement_not_account_balance'}
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

    def invoke(self, name, args):
        if name not in SPECS:
            raise BankError('TOOL_FORBIDDEN', '该工具不在允许调用范围。', 403)
        validate(args, SPECS[name][1])
        if name.startswith('market_'):args=self._market_arguments(name,args)
        self.fresh()
        if name in ('answer_facts','answer_reviewed'):
            return args
        if name.startswith('market_'):
            data=self._read_market(name,args)
            self._pin_market_read(name,args,data)
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
            self.advice=True
        elif name == 'graph_scenario':
            data=self.g.financial_graph.scenario(self.w,args)
            self.advice=True
        elif name == 'remember_profile':
            data=self.g.personal_graph.remember(self.w,self.user_text,self.request_id,args)
        elif name == 'public_products':
            data=self.g.public_products.read(args.get('query',''),limit=12)
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
            currencies = {p['id']:p['currency'] for p in self.g.bank.products(self.w)}
            principal = {}
            for h in s['holdings']:
                c = currencies[h['product_id']]
                principal.setdefault(c, {'currency':c,'holding_principal_minor':0,'pending_redemption_minor':0})
                principal[c]['holding_principal_minor'] += h['amount_minor']
            for order in s['orders']:
                if order['status']=='pending_settlement':
                    c = currencies[order['product_id']]
                    principal.setdefault(c, {'currency':c,'holding_principal_minor':0,'pending_redemption_minor':0})
                    principal[c]['pending_redemption_minor'] += order['amount_minor']
            data['principal_summary'] = list(principal.values())
            data['financial_scope_basis'] = '已结算现金、持仓本金、待到账赎回分别说明；仅凭持仓本金不能声称全部持仓被锁定或都有同一退出规则。正式风险等级是适当性条件，不对应权益比例或确定回撤；假设条件不覆盖正式画像。'
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
            data['amount_basis'] = '可安排资金已扣应急底线、生效预留及未来30天已知到期款；reserves仅做规划，不产生真实预留。空commitment_ids按全新事项扣全额，同一已知义务须关联去重；不能把情景金额说成未追加留款的当前预算。'
            self.current_budget = data
        elif name == 'statement_source': data = self.g.source(self.w)
        elif name == 'monthly_statement': data = self.g.query_read_capability(self.w,'statements.monthly_analysis',args)
        elif name == 'annual_statement': data = self.g.query_read_capability(self.w,'statements.annual_analysis',args)
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
        self.remember_identifiers(name,data)
        evidence_id = 'e'+str(len(self.evidence)+1)+'-'+hashlib.sha256(encoded(data).encode()).hexdigest()[:12]
        evidence_source = ('financial_mcp_historical_synthetic' if name in FINANCIAL_MCP_TOOL_NAMES else
            'public_knowledge' if name=='knowledge_search' else 'local_knowledge_status' if name=='knowledge_status' else
            'verified_public_market' if name.startswith('market_') else 'webank_public_disclosure' if name=='public_products' else
            'profile_sources' if name=='personal_graph' else 'conversation_candidate' if name=='remember_profile' else
            'maintainer_skill' if name=='load_skill' else 'simulation' if name not in ('monthly_statement','annual_statement','money_calculation') else data.get('source'))
        self.evidence[evidence_id] = {'id':evidence_id,'tool':name,'parameters':safe_data(args),
                                      'data':data,'source':evidence_source,
                                      'context_hash':self.start_context}
        if name.startswith('market_'):self.add_market_facts(evidence_id,data)
        elif name=='knowledge_search':self.add_knowledge_facts(evidence_id,data)
        elif name in ('load_skill','knowledge_status'):pass
        else:self.add_facts(evidence_id, data, name)
        metric_records=self._register_metrics(name,args,data,evidence_id)
        facts = [f for f in self.facts.values() if f['evidence_id']==evidence_id]
        self.trace.append({'tool':name,'evidence_id':evidence_id,'status':'completed'})
        return {'evidence_id':evidence_id,'data':data,'facts':facts,'metrics':metric_records}
