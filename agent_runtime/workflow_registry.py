"""维护者登记的四个只读流程；只生成本轮白名单消息，不执行代码或工具。

流程是本地有限状态机，没有生成式 DAG、动态脚本或外部运行时。Jev/原生
模型只能认证候选能力及整项覆盖；参数和下一步由宿主来源绑定决定。
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
import json
import re
from typing import Mapping
import uuid

from .intent_routing import (RouteInputError, canonical_json, digest, parse_json,
                             registered_catalog, source_envelope, host_safety_reason,
                             build_route_input, normalize_route_answers)


@dataclass(frozen=True, slots=True)
class Workflow:
    workflow_id: str
    skills: tuple[str, ...]
    readonly_tools: tuple[str, ...]
    whole_request_fit: str
    slots: tuple[str, ...]

    def record(self):
        return {'id': self.workflow_id, 'skills': list(self.skills),
                'readonly_tools': list(self.readonly_tools),
                'whole_request_fit': self.whole_request_fit, 'slots': list(self.slots)}


WORKFLOWS = (
    Workflow('budget_and_cashflow_read', ('financial-diagnosis', 'goal-planning'),
             ('account_summary', 'cashflow', 'budget'),
             '整项本轮请求是否仅需一个已明确账户的当前可安排资金与明确天数现金流？须覆盖全部目标；全面诊断、月/年账单、导入/历史覆盖、负债保障、新留款情景、推荐或写入均答否。',
             ('account_id', 'days')),
    Workflow('public_product_terms_read', ('product-research', 'knowledge-retrieval'),
             ('knowledge_status', 'public_products', 'knowledge_search'),
             '整项本轮请求是否仅查一个已登记公开名称/概念的费用、流动性或风险原文？须覆盖全部目标；多产品比较、份额/渠道歧义、个人适配、最新可买证明、收益比较或写入均答否。',
             ('public_term', 'facets', 'date?')),
    Workflow('portfolio_facts_read', ('portfolio-review',),
             ('wealth_versions', 'wealth_product'),
             '整项本轮请求是否仅查明确组合、明确日期、一个明确基金的只读收益事实？须覆盖全部目标；多基金、组合总览、当前账户/订单、期间比较、目标偏离、配置风险、调仓建议或写入均答否。',
             ('public_term_or_fund_id', 'portfolio', 'date')),
    Workflow('subscription_candidates_read', ('subscriptions',),
             ('statement_source', 'periodic_charges', 'anomalies', 'business_records'),
             '整项本轮请求是否仅查周期/异常扣费候选、账单来源或当前商户合同/银行授权？必须逐项覆盖；取消/关闭、执行结果、资金预测、省费建议、月/年账单明细或其他目标均答否。',
             ('requested_candidate_or_contract_views',)),
)
WORKFLOW_BY_ID = {item.workflow_id: item for item in WORKFLOWS}
READONLY_TOOLS = frozenset(tool for item in WORKFLOWS for tool in item.readonly_tools)
MANIFEST_SHA256 = digest([item.record() for item in WORKFLOWS])


def manifest():
    return {'schema_version': 'navren.readonly-workflows/1',
            'manifest_sha256': MANIFEST_SHA256,
            'workflows': [item.record() for item in WORKFLOWS],
            'generated_code': False, 'terminal_tools': False}


_NEGATIVE_SCOPE = re.compile(r'不|勿|别|无需|无须|禁止|拒绝|避免|排除|除外|之外|'
                             r'\b(?:no|not|never|without|except|excluding)\b|don[\'’]t', re.I)
_READ_REQUEST = re.compile(r'查|看|读|分析|评估|计算|核对|列出|显示|展示|了解|获取|检索|搜索|预测')
_PUBLIC_FEES = re.compile(r'(?:认购|申购|赎回|管理|托管|销售服务|销售|服务|交易|转换|转账|办理|退出)'
                          r'(?:手续费|费用|费率|费)|手续费|费用|费率|收费')
_PUBLIC_LIQUIDITY = re.compile(r'赎回|退出|流动性|期限|到账|持有期|锁定期|到期|开放期')
_PUBLIC_RISK = re.compile(r'风险(?:等级|评级|披露)?|本金损失')
_PUBLIC_QUERY_WORDS = re.compile(r'查询|查看|查|核对|看看|看|读取|了解|请|告诉我|说明|披露|'
                                r'具体|多少|是什么|有什么|有哪些|如何|怎样|怎么样|是否|'
                                r'原文|条款|条件|规则|标准|详情|明细|信息|情况|资料|'
                                r'时间|天数|日期|截至|截止|当时|历史|当日|在|的|及|和|与|以及|或|'
                                r'这个|这只|该|基金|产品|债券|[表吗呢呀]')


def _public_facets(text, term):
    """只编译宿主能逐项覆盖的有限字段；残留目标不能靠模型 fit 擦掉。"""
    scope = text.replace(term, '')
    scope = re.sub(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)', '', scope)
    facets = []
    if _PUBLIC_FEES.search(scope):
        facets.append('fees')
    # 赎回费/退出费是费用字段；独立赎回条件/到账时间才请求流动性。
    scope = _PUBLIC_FEES.sub('', scope)
    if _PUBLIC_LIQUIDITY.search(scope):
        facets.append('liquidity')
    scope = _PUBLIC_LIQUIDITY.sub('', scope)
    if _PUBLIC_RISK.search(scope):
        facets.append('risk')
    scope = _PUBLIC_RISK.sub('', scope)
    scope = _PUBLIC_QUERY_WORDS.sub('', scope)
    if re.sub(r'[\s，,、；;。.!！?？:：()（）\-]', '', scope):
        return None
    return facets or None


def _positive_clauses(request):
    """有限宿主准入，不把删除否定文字后的剩余片段当新用户授权。

    只保留已支持的“不要取消/关闭”独立写入限制。其他否定可能约束
    本地模板的隐含读取，整项回规划器；同句转折/双重否定也不自行拆解。
    """
    result = []
    for match in re.finditer(r'[^，,；;。.!！?？\n]+', request):
        clause = match.group()
        if _NEGATIVE_SCOPE.search(clause):
            limited = re.match(r'^\s*(?:不|不要|别|勿|不需要|不必)(?:取消|关闭)', clause)
            if (limited and not _READ_REQUEST.search(clause)
                    and not _NEGATIVE_SCOPE.search(clause[limited.end():])):
                continue
            return ()
        result.append((match.start(), match.end(), clause))
    return tuple(result)


def _candidate_in_clause(route_input, candidate, clause):
    start, end, text = clause
    if candidate.source_path == 'current_request':
        return (type(candidate.start) is int and type(candidate.end) is int
                and start <= candidate.start < candidate.end <= end
                and route_input.source_request[candidate.start:candidate.end] == candidate.source_text)
    # 页面只证明类型；执行范围仍须在本轮肯定原文中明确出现该对象。
    return (candidate.source_path.startswith('server_context.page_context.')
            and candidate.start is None and candidate.end is None
            and bool(candidate.source_text) and candidate.source_text in text)


def _positive_values(route_input, kind, clauses):
    return tuple(candidate for candidate in route_input.values(kind)
                 if any(_candidate_in_clause(route_input, candidate, clause) for clause in clauses))


def _one(route_input, kind, clauses):
    values = {candidate.value_json: candidate.value
              for candidate in _positive_values(route_input, kind, clauses)}
    return next(iter(values.values())) if len(values) == 1 else None


def _clause_has_value(route_input, kind, value, clause):
    return any(candidate.value == value and _candidate_in_clause(route_input, candidate, clause)
               for candidate in route_input.values(kind))


def _public_query_slots(route_input, clauses, text):
    """以肯定原文的完整拼写和整项覆盖选 query，不宣称 term 的实体类型。

    provider 的公开目录把名称与概念放在一个扁平列表。所有原候选保留，
    这里只消解同一原文 span 内的短拼写，以及可被完整字段语法解释的词。
    两个独立对象、剩余目标或两个完整解释仍回主规划器。
    """
    candidates = _positive_values(route_input, 'public_term', clauses)
    maximal = tuple(candidate for candidate in candidates if not any(
        other.start is not None and candidate.start is not None
        and other.start <= candidate.start and candidate.end <= other.end
        and other.end - other.start > candidate.end - candidate.start
        for other in candidates))
    values = {candidate.value for candidate in maximal}
    if len(values) > 1 and all(_public_facets(value, '') is not None for value in values):
        # 没有从属的完整名称时，多个概念仍是多个可能的 query；不挑末项。
        return None
    complete = []
    for term in sorted(values):
        # 一个明确名称可有多个查询字段；独立的第二个非字段拼写不能被
        # “基金/产品”等句法虚词擦掉。概念仅作词义解释，不升级为产品。
        if any(other != term and _public_facets(other, '') is None for other in values):
            continue
        # “产品和风险”缺少明确从属关系，可能是产品加另一个公开概念。
        # 直接的“产品风险”/“产品的风险及费用”则保留全部字段。
        if any(candidate.value == term and any(
                _candidate_in_clause(route_input, candidate, clause)
                and re.match(r'\s*(?:以及|和|与|及|或|、|跟|同)',
                             route_input.source_request[candidate.end:clause[1]])
                for clause in clauses) for candidate in maximal):
            continue
        facets = _public_facets(text, term)
        # 单独询问一个已登记公开概念（如“查风险原文”）不需要伪造产品。
        if facets is None and len(values) == 1 and _public_facets(term, '') is not None:
            facets = _public_facets(text, '')
        if facets:
            complete.append({'query': term, 'facets': facets})
    return complete[0] if len(complete) == 1 else None


def _slots(route_input, workflow_id):
    clauses = _positive_clauses(route_input.source_request)
    if not clauses:
        return None
    text = '，'.join(clause[2] for clause in clauses)
    if workflow_id == 'budget_and_cashflow_read':
        if (re.search(r'账单|流水|明细|导入|全年|历史|负债|贷款|保障|保险|新增|额外|预留|留款|情景|元|万元|收益|推荐|比较', text)
                or not re.search(r'可安排|可投资|能投资|预算|可用资金', text)):
            return None
        account = _one(route_input, 'account_id', clauses)
        days = _one(route_input, 'days', clauses)
        # 预算并不隐含未来现金流授权；天数也不能来自另一个目标的参数。
        if (account is None or days is None or not any(
                re.search(r'现金流', clause[2])
                and _clause_has_value(route_input, 'days', days, clause) for clause in clauses)):
            return None
        return {'account_id': account, 'days': days}
    if workflow_id == 'public_product_terms_read':
        if re.search(r'比较|对比|适合我|我的|份额|[AC]类|可买|能买吗|收益|最新报价', text, re.I):
            return None
        if '渠道' in text:
            # 历史渠道披露与“在哪买”不是同一目标。当前 RouteInput 尚无
            # 类型化 channel 候选，不能把基金名/A字或用户自由文本当渠道ID。
            if (not re.search(r'渠道风险披露', text)
                    or _one(route_input, 'channel_id', clauses) is None):
                return None
            # 即使以后输入增加候选，当前模板还未登记渠道绑定/回查节点。
            return None
        dates = _positive_values(route_input, 'date', clauses)
        if len({item.value_json for item in dates}) > 1:
            return None
        slots = _public_query_slots(route_input, clauses, text)
        if slots is None:
            return None
        if dates:
            slots['as_of'] = _one(route_input, 'date', clauses)
        return slots
    if workflow_id == 'portfolio_facts_read':
        if re.search(r'比较|对比|总览|整体|期间|配置|集中|目标|调整|调仓|建议|风险|账户|订单', text):
            return None
        term = _one(route_input, 'public_term', clauses)
        fund = _one(route_input, 'fund_id', clauses)
        portfolio = _one(route_input, 'portfolio', clauses)
        on = _one(route_input, 'date', clauses)
        if (term is None and fund is None) or portfolio is None or on is None:
            return None
        identity_kind, identity = ('fund_id', fund) if fund is not None else ('public_term', term)
        if not any((_READ_REQUEST.search(clause[2]) or re.search(r'收益|估值|净值', clause[2]))
                   and _clause_has_value(route_input, identity_kind, identity, clause)
                   and _clause_has_value(route_input, 'portfolio', portfolio, clause)
                   and _clause_has_value(route_input, 'date', on, clause) for clause in clauses):
            return None
        return {'fund_name': term, 'fund_id': fund, 'portfolio': portfolio, 'on': on}
    if workflow_id == 'subscription_candidates_read':
        if re.search(r'取消|关闭|执行结果|回执|未来|可安排|预算|余额|建议|省费|节省|月账单|年账单|明细|资金预测|手续费|费率|申购|赎回|基金|净值|本金', text):
            return None
        periodic = bool(re.search(r'周期|每月|按月|定期|(?<!手)续费', text))
        anomalies = bool(re.search(r'异常|重复|多扣', text))
        contracts = bool(re.search(r'商户合同|银行授权|代扣|订阅状态|当前订阅', text))
        source = bool(re.search(r'账单来源|流水来源|覆盖范围', text)) or periodic or anomalies
        if not (periodic or anomalies or contracts or source):
            return None
        return {'source': source, 'periodic': periodic, 'anomalies': anomalies, 'contracts': contracts}
    return None


def eligible_workflows(route_input, capability_ids, discrete_answers):
    requested = frozenset(capability_ids)
    fitted = [item for item in WORKFLOWS
              if discrete_answers.get('fit_' + item.workflow_id) == 'yes'
              and requested.intersection(item.skills) and _slots(route_input, item.workflow_id) is not None]
    # 只有整项覆盖且所有槽位有来源的最小集合才执行；绝不做匹配到的一半。
    for count in range(1, len(fitted) + 1):
        for group in combinations(fitted, count):
            covered = frozenset(skill for item in group for skill in item.skills) & requested
            if covered == requested:
                return tuple(item.workflow_id for item in group), 'complete_registered_workflow'
    return (), 'known_capabilities_require_primary'


def _validate(value, schema):
    """本地只校验当前宿主 schema；实际调用仍由原 BankTools 再校验。"""
    if not isinstance(schema, dict):
        raise RouteInputError('invalid_visible_schema')
    kind = schema.get('type')
    if isinstance(kind, list):
        for option in kind:
            try:
                _validate(value, {**schema, 'type': option})
                return
            except RouteInputError:
                pass
        raise RouteInputError('workflow_argument_type')
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'integer': type(value) is int,
             'boolean': type(value) is bool, 'null': value is None}.get(kind, False)
    if not valid or ('enum' in schema and value not in schema['enum']):
        raise RouteInputError('workflow_argument_type_or_enum')
    if kind == 'object':
        properties = schema.get('properties', {})
        if (not isinstance(properties, dict) or set(schema.get('required', ())) - set(value)
                or (set(value) - set(properties) and schema.get('additionalProperties', False) is False)):
            raise RouteInputError('workflow_argument_fields')
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key])
            elif isinstance(schema.get('additionalProperties'), dict):
                _validate(item, schema['additionalProperties'])
    elif kind == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 64):
            raise RouteInputError('workflow_argument_length')
        for item in value:
            _validate(item, schema['items'])
    elif kind == 'string':
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 8000):
            raise RouteInputError('workflow_argument_length')
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
            raise RouteInputError('workflow_argument_pattern')
    elif kind == 'integer':
        if not schema.get('minimum', -10**15) <= value <= schema.get('maximum', 10**15):
            raise RouteInputError('workflow_argument_range')


class WorkflowRunner:
    """每请求独立实例。只消费自己发出的本轮工具消息，无法调用业务端。"""
    def __init__(self, route_input, decision):
        if route_input.binding != decision.source_binding:
            raise RouteInputError('route_binding_mismatch')
        if decision.status not in ('primary', 'workflow'):
            raise RouteInputError('route_status_unknown')
        verified = normalize_route_answers(route_input, decision.raw_scores, provider=decision.provider,
                                           positive_threshold=decision.positive_threshold,
                                           negative_threshold=decision.negative_threshold)
        if (decision.status, decision.capability_ids, decision.workflow_ids, decision.routing_skills) != (
                verified.status, verified.capability_ids, verified.workflow_ids, verified.routing_skills):
            raise RouteInputError('decision_not_derived_from_bound_answers')
        self.route_input = route_input
        self.decision = decision
        self._slots = {}
        self._stage = {}
        self._pending = ()
        self._results = {}
        self._last_round = -1
        self._disabled = decision.status != 'workflow'
        self.last_reason = decision.reason
        catalog_items = parse_json(route_input.catalog_json)
        registered_tools = {tool for item in catalog_items if item['id'] in decision.routing_skills
                            for tool in item['tools']}
        for name in decision.workflow_ids:
            if name not in WORKFLOW_BY_ID:
                raise RouteInputError('workflow_not_registered')
            item = WORKFLOW_BY_ID[name]
            if set(item.readonly_tools) - registered_tools:
                raise RouteInputError('workflow_not_covered_by_registered_skills')
            slots = _slots(route_input, name)
            if slots is None:
                raise RouteInputError('workflow_slots_unbound')
            self._slots[name] = canonical_json(slots)
            self._stage[name] = 'initial'
        if decision.status == 'workflow' and not decision.workflow_ids:
            raise RouteInputError('workflow_ids_missing')

    def _stop(self, reason):
        self._disabled = True
        self.last_reason = reason
        return None

    def minimum_rounds_remaining(self):
        """本地尚需的最大读取批次加一轮终结；独立流程可在同一批提出。

        不读取数据、不推进 stage、不消费工具数。Conversation 在调用
        local_workflow_step 之前使用此值作轮次准入；工具终结位由它保留。
        """
        if self._disabled:
            return 1
        reads = []
        for name, stage in self._stage.items():
            if stage == 'done':
                continue
            reads.append(1 if name == 'subscription_candidates_read' or stage == 'detail' else 2)
        return max(reads, default=0) + 1

    def _remaining(self):
        count = 0
        for name, stage in self._stage.items():
            if stage == 'done':
                continue
            if name == 'budget_and_cashflow_read':
                count += 3 if stage == 'initial' else 1
            elif name == 'public_product_terms_read':
                # 只查原文的请求不强制额外读取产品目录。
                count += (2 + int('product-research' in self.decision.capability_ids)) if stage == 'initial' else 1
            elif name == 'portfolio_facts_read':
                count += 2 if stage == 'initial' else 1
            elif name == 'subscription_candidates_read':
                count += sum(parse_json(self._slots[name]).values())
        return count

    def _consume(self, view_messages):
        if not self._pending:
            return True
        expected = {call_id: (workflow_id, tool, args) for call_id, workflow_id, tool, args in self._pending}
        issued = None
        for index, message in enumerate(view_messages):
            if not isinstance(message, Mapping) or message.get('role') != 'assistant':
                continue
            calls = message.get('tool_calls', [])
            if not isinstance(calls, list):
                continue
            ids = [call.get('id') for call in calls if isinstance(call, dict)]
            if set(ids).intersection(expected):
                if issued is not None or len(ids) != len(set(ids)) or set(ids) != set(expected):
                    return False
                for call in calls:
                    function = call.get('function', {})
                    _, tool, arguments = expected[call['id']]
                    if (call.get('type') != 'function' or function.get('name') != tool
                            or function.get('arguments') != arguments):
                        return False
                issued = index
        if issued is None:
            return False
        found = {}
        for message in view_messages[issued + 1:]:
            if not isinstance(message, Mapping) or message.get('role') != 'tool':
                continue
            call_id = message.get('tool_call_id')
            if call_id not in expected:
                continue
            if call_id in found:
                return False
            try:
                body = parse_json(message.get('content'))
            except RouteInputError:
                return False
            if (not isinstance(body, dict) or 'error' in body or not isinstance(body.get('data'), dict)
                    or not isinstance(body.get('evidence_id'), str) or not body['evidence_id']):
                return False
            workflow_id, tool, _ = expected[call_id]
            found[call_id] = body
            self._results[(workflow_id, tool)] = canonical_json(body)
        if set(found) != set(expected):
            return False
        self._pending = ()
        return True

    def _data(self, workflow_id, tool):
        return parse_json(self._results[(workflow_id, tool)])['data']

    def local_workflow_step(self, view_messages, schemas, round_number, remaining_tools):
        if self._disabled:
            return None
        if (type(round_number) is not int or type(remaining_tools) is not int
                or round_number < 0 or remaining_tools < 0 or round_number <= self._last_round):
            return self._stop('workflow_round_or_budget_invalid')
        self._last_round = round_number
        try:
            _, envelope = source_envelope(view_messages)
            if digest(envelope['current_request']) != self.route_input.binding.source_request_sha256:
                return self._stop('workflow_request_changed')
            if digest(registered_catalog()) != self.route_input.binding.catalog_sha256:
                return self._stop('workflow_catalog_changed')
            projected = build_route_input(view_messages, request_id=self.route_input.binding.request_id,
                                          context_version=self.route_input.binding.context_version)
            if projected.binding != self.route_input.binding:
                return self._stop('workflow_state_or_candidates_changed')
            if host_safety_reason(envelope['current_request']):
                return self._stop('workflow_host_safety_gate')
            if not self._consume(view_messages):
                return self._stop('workflow_result_chain_invalid')
            if self._remaining() > remaining_tools:
                return self._stop('workflow_tools_budget_insufficient')
            visible = {}
            for schema in schemas:
                if not isinstance(schema, dict) or schema.get('type') != 'function':
                    return self._stop('workflow_visible_schema_invalid')
                function = schema.get('function', {})
                name = function.get('name')
                if not isinstance(name, str) or name in visible:
                    return self._stop('workflow_visible_schema_duplicate')
                visible[name] = function.get('parameters')
            proposed = []
            next_stages = {}
            for name in self.decision.workflow_ids:
                stage = self._stage[name]
                slots = parse_json(self._slots[name])
                if stage == 'done':
                    continue
                if name == 'budget_and_cashflow_read':
                    # 当前匿名 enum 必须已经注册，不能读后选择任意第一个账户。
                    account_schema = visible.get('budget', {}).get('properties', {}).get('account_id', {})
                    if slots['account_id'] not in account_schema.get('enum', []):
                        return self._stop('workflow_account_not_current_enum')
                    if stage == 'initial':
                        proposed.extend([(name, 'account_summary', {}),
                                         (name, 'cashflow', {'days': slots['days']})])
                        next_stages[name] = 'detail'
                    else:
                        accounts = self._data(name, 'account_summary').get('accounts', [])
                        matches = [item for item in accounts if isinstance(item, dict) and item.get('id') == slots['account_id']]
                        if len(matches) != 1:
                            return self._stop('workflow_account_result_unbound')
                        proposed.append((name, 'budget', {'account_id': slots['account_id']}))
                        next_stages[name] = 'done'
                elif name == 'public_product_terms_read':
                    if stage == 'initial':
                        proposed.append((name, 'knowledge_status', {}))
                        if 'product-research' in self.decision.capability_ids:
                            proposed.append((name, 'public_products', {'query': slots['query']}))
                        next_stages[name] = 'detail'
                    else:
                        status = self._data(name, 'knowledge_status')
                        if (status.get('ready') is not True or not isinstance(status.get('public_release_id'), str)
                                or not status['public_release_id']):
                            return self._stop('workflow_public_source_not_ready')
                        args = {key: value for key, value in slots.items() if key in ('query', 'facets', 'as_of')}
                        proposed.append((name, 'knowledge_search', args))
                        next_stages[name] = 'done'
                elif name == 'portfolio_facts_read':
                    if stage == 'initial':
                        proposed.append((name, 'wealth_versions', {}))
                        next_stages[name] = 'detail'
                    else:
                        data = self._data(name, 'wealth_versions')
                        products = data.get('products', [])
                        if not isinstance(products, list):
                            return self._stop('workflow_portfolio_products_invalid')
                        matches = [item for item in products if isinstance(item, dict)
                                   and ((slots['fund_id'] is not None and item.get('fund_id') == slots['fund_id'])
                                        or (slots['fund_id'] is None and item.get('name') == slots['fund_name']))]
                        if len(matches) != 1 or not isinstance(matches[0].get('fund_id'), str):
                            return self._stop('workflow_fund_result_unbound')
                        proposed.append((name, 'wealth_product', {'fund_id': matches[0]['fund_id'],
                                                                 'on': slots['on'], 'portfolio': slots['portfolio']}))
                        next_stages[name] = 'done'
                elif name == 'subscription_candidates_read':
                    for flag, tool, args in (('source', 'statement_source', {}),
                                             ('periodic', 'periodic_charges', {}),
                                             ('anomalies', 'anomalies', {}),
                                             ('contracts', 'business_records', {'kind': 'subscriptions'})):
                        if slots[flag]:
                            proposed.append((name, tool, args))
                    next_stages[name] = 'done'
            if not proposed:
                return self._stop('workflow_reads_completed_primary_must_finalize')
            if len(proposed) > remaining_tools:
                return self._stop('workflow_tools_budget_insufficient')
            pending = []
            calls = []
            for workflow_id, tool, args in proposed:
                if tool not in READONLY_TOOLS or tool not in WORKFLOW_BY_ID[workflow_id].readonly_tools:
                    return self._stop('workflow_tool_not_readonly')
                if tool not in visible:
                    return self._stop('workflow_tool_not_visible')
                _validate(args, visible[tool])
                call_id = 'hostwf_' + uuid.uuid4().hex
                arguments = canonical_json(args)
                calls.append({'id': call_id, 'type': 'function',
                              'function': {'name': tool, 'arguments': arguments}})
                pending.append((call_id, workflow_id, tool, arguments))
            message = {'role': 'assistant', 'content': None, 'tool_calls': calls}
            receipt = {'source': 'registered_host_workflow', 'stage': 'local_read_step',
                       'workflow_ids': list(self.decision.workflow_ids),
                       'manifest_sha256': MANIFEST_SHA256, 'binding': self.decision.binding,
                       'round_number': round_number, 'routing_skills': list(self.decision.routing_skills),
                       'schema_sha256': digest(schemas), 'view_sha256': digest(view_messages),
                       'message_sha256': digest(message), 'call_ids': [item['id'] for item in calls],
                       'tool_names': [item['function']['name'] for item in calls],
                       'request_sent': False, 'model_calls': 0, 'model_usage': None,
                       'status': 'proposed_for_original_tool_pipeline'}
            self._pending = tuple(pending)
            self._stage.update(next_stages)
            self.last_reason = 'workflow_step_proposed'
            return message, receipt
        except (RouteInputError, KeyError, TypeError, AttributeError, ValueError):
            return self._stop('workflow_binding_or_schema_invalid')
