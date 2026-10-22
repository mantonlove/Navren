"""有限操作状态断言合同；只读真实原操作，不授予确认或执行权。

普通解释保持原样。已准备、未执行、成功、失败、待结算、未知等有限
状态断言必须绑定本轮宿主工具证据及当前银行行；不声称完整自然语言分类。
"""
from contextlib import closing
from pathlib import Path
import hashlib
import json
import re
import unicodedata

from simulated_bank import BankError


class OperationWordingError(BankError):
    """Local OP rejection metadata; JSON error codes cannot grant recovery."""

    def __init__(self, code, message, status, *, location=None, locations=()):
        super().__init__(code, message, status)
        self._operation_wording_location = location
        self._operation_wording_locations = tuple(dict.fromkeys(locations))

    @property
    def location(self):
        return self._operation_wording_location

    @property
    def locations(self):
        return self._operation_wording_locations


_TOOLS = frozenset(('prepare_operation', 'operation_status', 'withdraw_draft'))
_TERMINAL_SUCCESS = frozenset(('succeeded', 'settled'))
_FAILED = frozenset(('failed', 'rejected'))
_UNEXECUTED = frozenset(('pending_confirmation', 'cancelled', 'invalidated', 'expired'))
_KNOWN = _TERMINAL_SUCCESS | _FAILED | _UNEXECUTED | {'pending_settlement'}
_ACTION = r'(?:转账|转出|汇款|汇出|付款|支付|买入|申购|购买|赎回|挂失|取消|还款|发卡|核发|解挂|解除挂失|密码变更|修改密码|预留|释放|预约|执行|操作|交易|办理)'
# Fee nouns describe a tariff, not an executed action. This applies only to
# the unsupported-state fallback; explicit status patterns remain unchanged.
_FEE_TERMS = re.compile(_ACTION + r'(?:手续费|费用|费率|费)')
# Finite public reference-property heads: they name a catalog/document
# attribute, not a bank operation state. These are used only on the fallback's
# state side, after every original explicit state pattern has run.
_REFERENCE_PROPERTY_HEADS = re.compile(
    r'(?:当前)?在售状态'
    r'|(?:文档|资料|条款)(?:的)?(?:效期|有效期|有效性|生效)状态')
_REFERENCE_PRIVATE_SUBJECT = re.compile(
    r'我|本人|你|您|个人|用户|客户|账户|账号|银行卡|卡号|尾号'
    r'|原操作|同一操作|该操作|此操作|本次操作|这次操作|这笔|该笔|此笔|本笔|那笔'
    r'|\b(?:my|your|our|his|her|personal|account|operation|transaction)\b', re.I)
# Application-success + 后 is a temporal antecedent, not a completion report.
# Only the unsupported fallback projects this span. Explicit status patterns
# first examine the original clause, and personal/past/specific subjects remain.
_APPLICATION_SUCCESS_CONDITION = re.compile(_ACTION + r'申请成功后')
_APPLICATION_CONDITION_SUBJECT = re.compile(r'(?:在)?(?:投资人|投资者|申请人|持有人|客户)?')
_APPLICATION_CONDITION_REFERENCE = re.compile(r'招募说明书|基金合同|产品资料概要|销售文件|公开条款|业务规则')
_APPLICATION_CONDITION_ATTRIBUTION = re.compile(
    r'(?:载明|规定|约定|说明|披露|列明|明确)[:：]?\s*'
    r'(?P<applicant>(?:在)?(?:投资人|投资者|申请人|持有人|客户)?)$')
_APPLICATION_CONDITION_SPECIFIC = re.compile(
    r'我|本人|你|您|该笔|这笔|该次|此次|本次|这次|刚|已经|曾|已'
    r'|昨天|昨日|今天|今日|昨晚|此前|之前|先前|实际')
# Denying an arrival guarantee and an explicitly unverified product-channel
# time are not statements that an identified bank operation has a given state.
# Both projections run only after original explicit statuses were checked.
# Denial scopes over the commitment predicate, not a duration. Match a
# complete sentence (commas retained) with a positive, bounded nominal/time
# grammar. Unknown complements and coordination retain the original fallback.
_GUARANTEE_DENIAL = r'(?:并非|并不|不)\s{0,4}(?:保证|确保|承诺)'
_GUARANTEE_LABEL = r'(?:[0-9a-z_\u3400-\u9fff()\-\s]{1,160}的)?'
_GUARANTEE_NOMINAL = r'(?:' + _ACTION + r'(?:资金|款项)?|资金|款项)'
_GUARANTEE_TIME = (
    r'(?:(?:在|于)\s{0,4})?(?:t\s{0,4}\+\s{0,4})?'
    r'(?:[0-9]{1,12}(?:\.[0-9]{1,6})?|[零一二三四五六七八九十百千万两]{1,12})'
    r'\s{0,4}(?:个)?(?:自然|工作)?(?:天|日|周|月|年)(?:内|后|之前|以内)?')
_GUARANTEE_TOPIC_FIRST = re.compile(
    r'(?P<label>' + _GUARANTEE_LABEL + r')(?P<action>' + _GUARANTEE_NOMINAL + r')'
    r'(?P<arrival>到账)\s{0,4}' + _GUARANTEE_DENIAL + r'(?:' + _GUARANTEE_TIME + r')?')
_GUARANTEE_OBJECT_LAST = re.compile(
    r'(?P<label>' + _GUARANTEE_LABEL + r')(?P<before>' + _GUARANTEE_NOMINAL + r')?'
    + _GUARANTEE_DENIAL + r'(?P<after>' + _GUARANTEE_NOMINAL + r')?'
    r'(?:' + _GUARANTEE_TIME + r')?(?P<arrival>到账)')
_UNVERIFIED_PUBLIC_CHANNEL_TIMING = re.compile(
    r'^(?:[-*]\s*)?(?:本次|本轮)?(?:尚未|未)(?:核实|核验|确认)'
    r'(?P<context>[^，,。；;！!\n]{1,80}?)(?P<timing>到账(?:时间|时点|期限))$')
_PRIVATE_TIMING_SUBJECT = re.compile(r'我|本人|你|您|账户|账号|银行卡|持仓|个人|该笔|这笔|上笔|该次|这次')
# A missing fee entry and a document's sale/validity status are not bank
# operation predicates. Mask only these spans; keep other same-clause actions.
_MISSING_FEE_ENTRY = re.compile(r'没有(?:申购费|赎回费)(?:条目|项目)')
_PUBLIC_REFERENCE_STATUS = re.compile(
    r'(?:当前)?在售状态(?:仍|尚|还)?(?:未知|不明|无法确认|未确定)'
    r'|(?:文档|资料|条款)(?:的)?(?:效期|有效期|有效性|生效)状态'
    r'(?:仍|尚|还)?(?:未知|不明|无法确认|未确定)'
    r'|(?:费率|费用)(?:的)?(?:时效|效期|有效期)待确认')
_PATTERNS = (
    ('pending', re.compile(r'待结算|等待结算|尚未结算|待到账|尚未到账')),
    ('unknown', re.compile(r'(?:状态|结果)(?:仍|尚|还)?(?:未知|不明|无法确认|未确定)|暂时无法确认(?:结果|状态)|不能确认(?:交易|操作)?是否成功')),
    ('unexecuted', re.compile(r'(?:尚未|还未|没有|未)(?:实际)?(?:执行|转账|转出|付款|支付|买入|申购(?!手续费|费)|挂失)|(?:待|等待)(?:你|您|我|本人|用户)?.{0,8}确认|已撤回.{0,8}草案|草案已(?:撤回|取消)')),
    ('prepared', re.compile(r'已.{0,8}准备.{0,12}(?:草案|转账|操作)|准备了.{0,12}(?:草案|转账|操作)|草案已(?:准备|生成)')),
    ('failed', re.compile(_ACTION + r'(?:已)?失败|(?:未能|未成功)' + _ACTION + r'|执行未成功')),
    ('success', re.compile(r'(?:已经|已)(?:成功)?' + _ACTION + r'(?!失败|未成功)|' + _ACTION + r'(?:已经|已)?(?:成功|完成)|(?:资金|钱|款项)已到账|已经到账')),
)
_EXPLANATION = re.compile(r'^(?:如果|假设|假如|例如|举例|只有|当(?!前)|若|假定)|^(?:就会|则会|会|将|才能|才会)')
_QUESTION = re.compile(r'^(?:是否|能否|有没有|会不会)|(?:吗|么)[?？]?$')
# Late interrogative grammar applies only after explicit state patterns found
# nothing. A question mark alone is insufficient, and a factual completed
# premise before the query is never projected away.
_INTERROGATIVE_SYNTAX = re.compile(
    r'(?P<open>什么时候|为什么|怎么(?:样)?|谁|什么|为何|如何|怎样|何时|何处|何人|何种'
    r'|哪里|哪儿|哪(?:个|些|种|天|日|一|几)|多少|多久|多长|多大|多远'
    r'|几(?:个|天|日|周|月|年|次|笔|份|点|时|成|倍))'
    r'|(?P<choice>还是)'
    r'|(?P<polar>能不能|会不会|可不可以|应不应该|该不该|要不要|需不需要|是不是|有没有|是否|能否|可否)')
# A positive, bounded query grammar: no arbitrary words are allowed before or
# after the interrogative operator. Action names are nouns here; unknown
# predicates/result complements fall back to the original receipt guard.
_QUERY_NEUTRAL_ATOM = (
    _ACTION + r'|到账时间|到账时点|到账日期|到账期限|结算时间|结算日期'
    r'|基金|产品|业务|渠道|银行|销售机构|工作日|自然日|款项|资金|费用|费率'
    r'|时间|日期|时点|期限|条件|范围|规则|方式|安排|类型|原因|影响|差异|流程'
    r'|一般|通常|正常|具体|大概|预计|可能|当前|实际|需要|应该|可以'
    r'|该|这|本|其|的|在|按|以|及|与|和|或|为|是|要|需|还|才|能|会|应'
    r'|个|天|日|周|月|年|次|笔|份|点|时|成|倍')
_QUERY_NEUTRAL_PREFIX = re.compile(r'(?:' + _QUERY_NEUTRAL_ATOM + r')')
_QUERY_INTERROGATIVE_SUFFIX = re.compile(
    r'(?:' + _QUERY_NEUTRAL_ATOM
    + r'|到账|查询|核实|确认|受理|计算|计提|处理|延迟|发生|适用|使用|采用|更快)')
_STATE_QUERY = re.compile(r'^(?:请)?(?:先)?(?:查询|查看|核对|恢复)(?:同一|该|原)?操作(?:状态|回执)$')
_QUOTED_EXPLANATION = re.compile(r'(?:只是|仅是|作为|用于).{0,10}(?:示例|假设|概念|教学)|不代表.{0,12}(?:当前|本人|实际)|状态.{0,8}(?:含义|解释)')
_NEGATED_ASSERTION = re.compile(r'(?:不能|不得|不应|不可)(?:据此)?(?:声称|宣称|认为|认定|保证|说).{0,12}?(?:成功|完成|到账)')
# A complete statement that evidence does not prove arrival is not itself an
# arrival report. Keep the grammar closed: no arbitrary verb, object, status,
# coordination or suffix can borrow the negation. Other clauses are scanned
# independently by the original receipt checks.
_EVIDENCE_NONASSERTION = re.compile(
    r'^(?:(?:公开|现有)(?:条款|资料|材料|证据)|(?:本|该|这)(?:条款|资料|材料|证据|回执))?'
    r'(?:本身)?(?:也|并|还)?(?:不|不能|无法)(?:据此)?(?:证明|证实)'
    r'(?:款项|资金|钱|账户资金|账户款项)(?:(?:已经|已)(?:成功)?)?到账$'
    r'|^(?:也|并)?不代表交易已执行(?:或独立验证了原始数据真实性)?$')
# A closed "subject whether predicate" complement asks for a state; it does
# not assert one. Completed premises, extra predicates and suffixes remain
# outside this grammar and continue through the original receipt checks.
_STATE_INTERROGATIVE_COMPLEMENT = re.compile(
    r'^(?:以及|及|和)?(?:款项|资金|钱|账户资金|账户款项)是否'
    r'(?:实际|已经|已)?(?:到账|成功到账)$')
_EN_ACTION = r'(?:operation|transaction|transfer|payment|purchase|card issuance|unblock|password change|funds? reservation|funds? release|booking)'
_REFERENCE_ACTION_SUBJECT = re.compile(_ACTION + '|' + _EN_ACTION + r'|资金|款项|\bfunds?\b', re.I)
# A generic no-funds-action statement is about this response, not an
# unidentified prior transaction. Specific actions and targets still need proof.
_GENERAL_NO_EXECUTION = re.compile(
    r'(?:(?:本轮|本次对话)(?:中)?\s*)?(?:没有|未|尚未)(?:实际)?执行'
    r'(?:任何(?:金融|资金)?(?:操作|交易)|(?:金融|资金)(?:操作|交易))')
_CODE_SUBJECT = re.compile(r'代码|沙箱|\bwat\b|\bwasm\b|\bcode\b|\bsandbox\b')
# Only a closed comma continuation can inherit the immediately preceding code
# subject. It still needs the original receipt/freshness checks in _code_clause.
_CODE_RESULT_CONTINUATION = re.compile(r'执行结果与既有指标一致')
_CODE_METRIC_CONTINUATION = re.compile(
    r'复现了已签发指标\s+[a-z][a-z0-9_]*(?:\([^()]{1,40}\))?')

_EN_PATTERNS = {
    'pending': r'\b(?:pending|awaiting) (?:settlement|payment|transfer)\b|\b(?:payment|transfer) (?:is )?pending\b|\bnot (?:yet )?settled\b',
    'unknown': r'\b(?:status|result) (?:is )?(?:unknown|uncertain)\b|\b(?:transfer|payment) (?:is )?unknown\b',
    'unexecuted': r'\bnot (?:yet )?(?:executed|transferred|paid|purchased|issued|unblocked|reserved|released|booked)\b|\bawaiting (?:your|user|personal) confirmation\b',
    'prepared': r'\b(?:draft|proposal) (?:is )?(?:prepared|ready)\b|\bprepared (?:a |the )?(?:transfer|payment|operation) draft\b',
    'failed': r'\b' + _EN_ACTION + r' (?:has |is )?(?:failed|rejected)\b',
    'success': r'\b' + _EN_ACTION + r' (?:has |is )?(?:completed|succeeded|successful)\b|\b(?:transferred|paid|purchased|issued|unblocked|reserved|released|booked)\b|\bfunds? (?:have |has )?arrived\b',
}
_PATTERNS = tuple((kind, re.compile(pattern.pattern + '|' + _EN_PATTERNS[kind], re.I))
                  for kind, pattern in _PATTERNS)
_PATTERNS = tuple((kind, re.compile(pattern.pattern + (
    r'|(?:代码|沙箱|wat|wasm|code|sandbox).{0,20}(?:已|already|has).{0,20}(?:执行|运行|executed|ran)'
    if kind == 'success' else ''), re.I)) for kind, pattern in _PATTERNS)
_VERBS = {
    '转账': {'transfer'}, '转出': {'transfer'}, '汇款': {'transfer'}, '汇出': {'transfer'},
    '买入': {'purchase'}, '申购': {'purchase'}, '购买': {'purchase'}, '赎回': {'redeem'},
    '挂失': {'report_card_lost'},
    '取消': {'cancel_subscription', 'close_mandate', 'cancel_scheduled_payment'},
    '发卡': {'apply_card'}, '核发': {'apply_card'}, '解挂': {'unblock_card'},
    '解除挂失': {'unblock_card'}, '密码变更': {'password_change'}, '修改密码': {'password_change'},
    '预留': {'reserve_funds'}, '释放': {'release_funds'}, '预约': {'book_services'},
    'transfer': {'transfer'}, 'payment': {'transfer'}, 'purchase': {'purchase'},
    'issued': {'apply_card'}, 'issuance': {'apply_card'}, 'unblock': {'unblock_card'},
    'password': {'password_change'}, 'reserved': {'reserve_funds'}, 'reservation': {'reserve_funds'},
    'released': {'release_funds'}, 'booking': {'book_services'}, 'booked': {'book_services'},
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _history_quote_projection(final, tools, text, bank_db):
    """Exclude only original-character spans returned by the trusted issuer API.

    No final/model field supplies spans. The BankTools API owns proof/session/
    selected-member validation and repeats it on the supplied publication DB.
    The rest of the body and every nonmatching claim remain assertions.
    """
    validate = getattr(tools, 'validated_history_quote_spans', None)
    if not callable(validate):
        return text, (), frozenset()
    spans = validate(final, bank_db=bank_db)
    if type(spans) is not tuple:
        _deny('OPERATION_HISTORY_QUOTE_SPAN_INVALID')
    last = 0
    chunks, fragments = [], []
    for span in spans:
        if (type(span) is not tuple or len(span) != 2
                or any(type(value) is not int for value in span)):
            _deny('OPERATION_HISTORY_QUOTE_SPAN_INVALID')
        start, end = span
        if not (last <= start < end <= len(text)):
            _deny('OPERATION_HISTORY_QUOTE_SPAN_INVALID')
        chunks.extend((text[last:start], ' ' * (end - start)))
        fragments.append(text[start:end])
        last = end
    chunks.append(text[last:])
    claim_validation = getattr(tools, 'validated_history_quote_claims', None)
    if fragments and not callable(claim_validation):
        _deny('OPERATION_HISTORY_QUOTE_CLAIM_INVALID')
    claims = claim_validation(final, bank_db=bank_db) if fragments else frozenset()
    if type(claims) is not frozenset or any(type(value) is not str for value in claims):
        _deny('OPERATION_HISTORY_QUOTE_CLAIM_INVALID')
    # The issuer's current canonical claims contain one exact excerpt enclosed
    # in Chinese quotes. Fail closed on API shape drift rather than masking a
    # same-wording personal assertion appended to an otherwise trusted claim.
    if claims != frozenset('“' + fragment + '”' for fragment in fragments):
        _deny('OPERATION_HISTORY_QUOTE_CLAIM_INVALID')
    return ''.join(chunks), tuple(fragments), claims


def _statistic_atom_ranges(final, tools, bank_db):
    """Use only issuer-validated, field-local original-character ranges.

    The issuer binds the whole final/current turn and revalidates formal metric
    sources on the caller's publication connection. Neither equal text/value nor
    model evidence IDs create a range. Everything outside these atoms is scanned.
    """
    from .typed_statistics import validated_statistic_atom_ranges
    ranges = validated_statistic_atom_ranges(tools, final, bank_db=bank_db)
    fields = {}
    if isinstance(final.get('text'), str):
        fields['text'] = final['text']
    for index, claim in enumerate(final.get('claims') or ()):
        if isinstance(claim, dict) and isinstance(claim.get('text'), str):
            fields['claims[' + str(index) + '].text'] = claim['text']
    if type(ranges) is not dict:
        raise BankError('STATISTIC_BINDING_INVALID', '统计原子字段范围无效。', 422)
    checked = {}
    for location, spans in ranges.items():
        if type(location) is not str or location not in fields or type(spans) not in (tuple, list):
            raise BankError('STATISTIC_BINDING_INVALID', '统计原子字段范围无效。', 422)
        last = 0
        checked[location] = []
        for span in spans:
            if (type(span) not in (tuple, list) or len(span) != 2
                    or any(type(value) is not int for value in span)):
                raise BankError('STATISTIC_BINDING_INVALID', '统计原子字段范围无效。', 422)
            start, end = span
            if not (last <= start < end <= len(fields[location])):
                raise BankError('STATISTIC_BINDING_INVALID', '统计原子字段范围无效。', 422)
            checked[location].append((start, end))
            last = end
    return checked


def _normalize(text):
    return unicodedata.normalize('NFKC', text).lower()



def _application_condition_projection(clause):
    """Mask only a generic temporal antecedent in the unsupported fallback.

    A source-attributed public rule may precede a generic applicant. This is
    grammar classification, never evidence that any real application exists.
    Every other action/status in the prefix or suffix stays in the clause.
    """
    projected = clause
    for match in reversed(tuple(_APPLICATION_SUCCESS_CONDITION.finditer(clause))):
        prefix = clause[:match.start()].strip()
        attribution = _APPLICATION_CONDITION_ATTRIBUTION.search(prefix)
        attributed = _APPLICATION_CONDITION_REFERENCE.search(prefix) and attribution
        applicant = attribution.group('applicant') if attributed else prefix
        if _APPLICATION_CONDITION_SPECIFIC.search(applicant):
            continue
        generic = _APPLICATION_CONDITION_SUBJECT.fullmatch(prefix)
        if generic or attributed:
            projected = (projected[:match.start()] + ' ' * (match.end() - match.start())
                         + projected[match.end():])
    return projected



def _arrival_denial_relation(sentence):
    """A bounded, positive grammar; unknown complements grant no scope."""
    if (not sentence or len(sentence) > 512
            or _REFERENCE_PRIVATE_SUBJECT.search(sentence)):
        return None
    relation = _GUARANTEE_TOPIC_FIRST.fullmatch(sentence)
    if relation is None:
        relation = _GUARANTEE_OBJECT_LAST.fullmatch(sentence)
        if relation is None or (relation.group('before') and relation.group('after')):
            return None
    label = relation.group('label')
    if (_REFERENCE_ACTION_SUBJECT.search(label)
            or _APPLICATION_CONDITION_SPECIFIC.search(label)
            or re.search(r'到账|完成|成功|失败|结算|状态|结果|completed|success|failed|pending|unknown|arrived', label)
            or len(re.findall(r'保证|确保|承诺', sentence)) != 1):
        return None
    return relation


def _denial_context_is_closed(text, exact_quotes=()):
    """Withhold projection when any visible commitment lacks closed negation.

    This grants no truth, state or execution authority. A bare affirmative
    commitment is not a new rejection gate; it only prevents another clause
    from borrowing a nonfactual arrival projection. Trusted quote exclusions
    are exactly those already supplied to the existing body/claim collector.
    """
    normalized = _normalize(text)
    for quote in exact_quotes:
        for left, right in (('“', '”'), ('"', '"'), ('「', '」')):
            normalized = normalized.replace(left + _normalize(quote) + right, ' ')
    for clause in re.split(r'[，,。；;！!\n]', normalized):
        if re.search(r'保证|确保|承诺', clause) and _arrival_denial_relation(clause.strip()) is None:
            return False
    return True


def _denied_arrival_state_projection(text):
    """Change only arrival tokens; preserve positions and sentence boundaries.

    Every visible commitment in this text must first have a closed negative
    relation. A complete sentence (commas retained) then owns only its own
    arrival span; identical wording elsewhere grants no shared authorization.
    """
    if not _denial_context_is_closed(text):
        return text
    chunks, last = [], 0
    for sentence_match in re.finditer(r'[^。；;！!\n]+', text):
        raw = sentence_match.group(0)
        relation = _arrival_denial_relation(raw.strip())
        if relation is None:
            continue
        offset = sentence_match.start() + len(raw) - len(raw.lstrip())
        start, end = (offset + value for value in relation.span('arrival'))
        chunks.extend((text[last:start], ' ' * (end - start)))
        last = end
    if not chunks:
        return text
    chunks.append(text[last:])
    return ''.join(chunks)


def _non_status_fallback_projection(clause):
    projected = _application_condition_projection(clause)
    # A verification request concerns a product attribute, not an arrival
    # report. Only mask the arrival characters inside named attributes. Other
    # status predicates, including a coordinated or appended claim, remain.
    # The original explicit-state patterns run before this fallback.
    if (not _REFERENCE_PRIVATE_SUBJECT.search(clause)
            and re.search(r'产品|基金', clause)):
        projected = re.sub(r'到账(?=条件|条款|时间|时点|日期|期限)',
                           '  ', projected)
    # A cash-basis rule names a qualifying noun, not a completed operation.
    # Full matching keeps individual status reports and appended predicates
    # under the original receipt checks.
    if re.fullmatch(r'(?:但|不过|然而)?(?:当前|本轮|现在)?'
                    r'(?:可投资金|可投现金|可用资金|可安排资金)'
                    r'(?:仍|只|仅|应|必须)?以已结算(?:的)?现金(?:余额)?为准',
                    clause.strip()):
        return ' ' * len(clause)
    # Complete bounded public rule queries are not transaction states.
    # Original explicit predicates run first; private subjects remain.
    timing_action = r'(?:赎回|转账|转出|汇款|汇出|付款|支付|申购|买入)'
    rule_query = re.fullmatch(
        r'(?:请)?(?:需要|需)?(?:先|再|进一步|逐一|逐项)?(?:核验|核实|核对)'
        r'(?:其|(?:具体)?(?:产品|基金)(?:的)?)?' + timing_action
        + r'(?:确认|规则|条件|条款)(?:和|与|及|、)到账(?:规则|条件|条款|时间|时点|日期|期限)'
        r'(?:后(?:才能|才可|方可|再)?确认)?',
        clause.strip())
    if rule_query and not _REFERENCE_PRIVATE_SUBJECT.search(clause):
        return ' ' * len(clause)
    # A closed processing-dependency relation names a timing constraint, not
    # an observed arrival. Mask only its arrival token; explicit statuses ran
    # first, and private subjects or extra predicates cannot borrow this span.
    process_step = r'(?:申请受理|受理|确认|支付|银行处理)'
    timing_dependency = re.fullmatch(
        r'(?:' + timing_action + r'(?:款项|资金))?(?:实际)?(?P<arrival>到账)'
        r'(?:时间|时点|日期|期限)?(?:还|仍|也)?受' + process_step
        + r'(?:(?:、|及|和|与)' + process_step + r'){1,5}环节影响',
        clause.strip())
    if timing_dependency and not _REFERENCE_PRIVATE_SUBJECT.search(clause):
        offset = len(clause) - len(clause.lstrip())
        start, end = timing_dependency.span('arrival')
        projected = (projected[:offset + start] + ' ' * (end - start)
                     + projected[offset + end:])
    # A complete epistemic denial about public product attributes is not a
    # claim about an operation. The bounded full grammar admits no personal
    # subject, actual status predicate, extra coordinated assertion or suffix.
    # Explicit state patterns still run before this fallback projection.
    public_item = (r'(?:当前)?(?:费用孰低|费用高低|在售渠道|渠道在售状态|在售状态'
                   r'|赎回到账|赎回条件|到账时间|到账期限|到账条件)')
    if re.fullmatch(r'(?:不能|无法)据此(?:判断|确认)' + public_item
                    + r'(?:(?:、|或|及|与|和)' + public_item + r'){0,7}',
                    clause.strip()):
        return ' ' * len(clause)
    # Complete public product-property wording only; explicit states have
    # already run. No personal subject or extra coordinated predicate fits.
    # Preserve the complete original postfix language, then add only bounded
    # reference-property verification word orders. Explicit state patterns have
    # already run in _assertions; no personal/specific-operation subject fits.
    stripped = clause.strip()
    legacy = re.fullmatch(
            r'(?:但是|不过|但)?(?:具体)?(?:产品|基金)(?:的|、)?'
            r'(?:赎回(?:(?:和|与|及|、)?到账)?|到账)(?:条件|条款)'
            r'(?:仍|尚|还)?(?:(?:待|有待)(?:逐一)?(?:核实|核验)'
            r'|(?:需要|需)(?:逐一)?(?:核实|核验)(?:后(?:才能|才可|方可|再)?确认)?)',
            stripped)
    subject = r'(?:具体)?(?:产品|基金)(?:候选)?(?:的|、)?'
    property_head = r'(?:条款|(?:赎回(?:(?:和|与|及|、)?到账)?|到账)(?:条件|条款))'
    modifier = r'(?:先|进一步|逐一)?'
    verify = r'(?:核实|核验|核对)'
    prefix = r'(?:但是|不过|但)?'
    reference = (
        re.fullmatch(prefix + r'(?:可以|可|需要|需)?' + modifier + verify
                     + subject + r'(?P<property>' + property_head + r')', stripped)
        or re.fullmatch(prefix + subject + r'(?P<property>' + property_head + r')'
                        + r'(?:仍|尚|还)?(?:待|有待|需要|需|可以|可)'
                        + modifier + verify
                        + r'(?:后(?:才能|才可|方可|再)?确认)?', stripped))
    if (legacy or reference) and not _REFERENCE_PRIVATE_SUBJECT.search(stripped):
        if reference:
            start, end = reference.span('property')
        else:
            # Legacy fullmatch admits only this property head, never an extra
            # predicate; keeping its original regex preserves old wordings.
            head = re.search(property_head, stripped)
            start, end = head.span()
        offset = len(clause) - len(clause.lstrip())
        chars = list(projected)
        for arrival in re.finditer('到账', stripped[start:end]):
            left = offset + start + arrival.start()
            chars[left:left + 2] = '  '
        projected = ''.join(chars)
    unverified = _UNVERIFIED_PUBLIC_CHANNEL_TIMING.fullmatch(projected.strip())
    if unverified:
        context = unverified.group('context')
        if (re.search(r'基金|产品', context) and '渠道' in context
                and not _PRIVATE_TIMING_SUBJECT.search(context)):
            # Remove the time noun only. Other verbs/status predicates, if any,
            # remain visible to the same unsupported-state fallback.
            projected = projected[:projected.rfind(unverified.group('timing'))] + ' '
    return projected




def _query_token_sequence(value, token_pattern):
    """Consume known tokens deterministically; never backtrack over a sequence.

    The existing literal alternatives put longer overlapping tokens first.
    Each match is a single token, and unknown or zero-width matches fail closed.
    """
    offset = 0
    while offset < len(value):
        token = token_pattern.match(value, offset)
        if token is None or token.end() <= offset:
            return False
        offset = token.end()
    return True


def _late_interrogative_clause(clause):
    """Recognize one bounded question, never a completed status premise.

    The original early question rules and explicit status loop are unchanged.
    This helper runs only when that loop has no match. Unknown constructions
    fail closed into the original unsupported-state fallback.
    """
    if not clause.endswith('?') or clause.count('?') != 1:
        return False
    body = clause[:-1].strip()
    query = _INTERROGATIVE_SYNTAX.search(body)
    if not query:
        return False
    prefix, suffix = body[:query.start()].strip(), body[query.end():].strip()
    if query.lastgroup == 'choice' and (not prefix or not suffix):
        return False
    return (_query_token_sequence(prefix, _QUERY_NEUTRAL_PREFIX)
            and _query_token_sequence(suffix, _QUERY_INTERROGATIVE_SUFFIX))



def _fallback_reference_state_projection(text):
    """Mask only eligible attribute heads, with equal-length whitespace.

    Private/account/specific-operation subjects fail closed. An action subject
    preceding an attribute also prevents projection. Other same-clause status
    words remain untouched; this does not prove any public attribute's value.
    """
    if _REFERENCE_PRIVATE_SUBJECT.search(text):
        return text
    # A bounded product-duration attribute is not a report that a particular
    # redemption arrived. Explicit states are checked before this fallback.
    # Preserve every other word and refuse transaction or guarantee contexts.
    if not re.search(r'已|刚|昨日|昨天|今日|今天|本次|本轮|这笔|该笔|交易|操作|订单|保证|承诺|确保',text):
        text=re.sub(r'赎回(?P<arrival>到账)(?=\s{0,4}[:：]?\s{0,4}[0-9]{1,3}(?:个)?(?:工作日|交易日|自然日|天)(?:\b|[^0-9]))',
                    lambda match:match.group(0)[:-2]+'  ',text)
    action = _REFERENCE_ACTION_SUBJECT.search(text)
    first_action = action.start() if action is not None else len(text) + 1
    chunks, last = [], 0
    for attribute in _REFERENCE_PROPERTY_HEADS.finditer(text):
        if first_action < attribute.start():
            continue
        chunks.extend((text[last:attribute.start()], ' ' * (attribute.end() - attribute.start())))
        last = attribute.end()
    if not chunks:
        return text
    chunks.append(text[last:])
    return ''.join(chunks)


def _assertions(text, exact_quotes=(), *, allow_denied_arrival_projection=True,
                code_result_contexts=None):
    normalized = _normalize(text)
    for quote in exact_quotes:
        for left, right in (('“', '”'), ('"', '"'), ('「', '」')):
            normalized = normalized.replace(left + _normalize(quote) + right, ' ')
    state_normalized = (_denied_arrival_state_projection(normalized)
                        if allow_denied_arrival_projection else normalized)
    result = []
    # The second channel changes only arrival characters, never separators.
    # Positional pairing keeps identical clauses in different sentence scopes
    # independent; no text-set membership grants another occurrence a span.
    parts = re.split(r'([，,。；;！!\n])', normalized)
    state_parts = re.split(r'([，,。；;！!\n])', state_normalized)
    code_scope = False
    for index in range(0, len(parts), 2):
        if index and parts[index - 1] not in ('，', ','):
            code_scope = False
        clause = parts[index].strip()
        state_clause = state_parts[index].strip()
        code_result = bool(code_scope and _CODE_RESULT_CONTINUATION.fullmatch(clause))
        code_scope = bool(code_scope and (code_result or (
            _CODE_METRIC_CONTINUATION.fullmatch(clause)
            and not any(verb in clause for verb in _VERBS)
            and not re.search(r'银行|资金|款项|交易|操作|money|funds|bank', clause))))
        if _QUOTED_EXPLANATION.search(clause):
            # A locally obvious status-name definition can be quoted data;
            # a quoted completed personal action cannot erase itself merely
            # by adding "example" elsewhere in this sentence/paragraph.
            def definition(match):
                quoted = match.group(0)
                return quoted if re.search(r'已|本人|我|\bmy\b|\balready\b|\bcompleted\b', quoted, re.I) else ' '
            clause = re.sub(r'“[^”]*”|"[^"\n]*"|「[^」]*」', definition, clause)
            state_clause = re.sub(r'“[^”]*”|"[^"\n]*"|「[^」]*」', definition, state_clause)
        if not clause or _EXPLANATION.search(clause) or _QUESTION.search(clause) or _STATE_QUERY.fullmatch(clause):
            continue
        if re.match(r'^(?:if |suppose |assuming |for example |would |will |could )', clause):
            continue
        found = set()
        masked = _PUBLIC_REFERENCE_STATUS.sub(' ',
            _MISSING_FEE_ENTRY.sub(' ', _NEGATED_ASSERTION.sub(' ', _STATE_INTERROGATIVE_COMPLEMENT.sub(' ', _EVIDENCE_NONASSERTION.sub(' ', clause)))))
        # Specific negations and pending language are removed before success
        # matching, so "尚未执行" is not misread as "已执行".
        for kind, pattern in _PATTERNS:
            if pattern.search(masked):
                found.add(kind)
                masked = pattern.sub(' ', masked)
        if found:
            result.append((clause, found))
            if code_result_contexts is not None:
                code_result_contexts.append(code_result)
            if (found == {'success'} and _CODE_SUBJECT.search(clause)
                    and not any(verb in clause for verb in _VERBS)
                    and not re.search(r'银行|资金|款项|交易|操作|money|funds|bank', clause)):
                code_scope = True
        else:
            if _late_interrogative_clause(clause):
                continue
            fallback = _non_status_fallback_projection(masked)
            state_masked = _PUBLIC_REFERENCE_STATUS.sub(' ',
                _MISSING_FEE_ENTRY.sub(' ', _NEGATED_ASSERTION.sub(' ', _STATE_INTERROGATIVE_COMPLEMENT.sub(' ', _EVIDENCE_NONASSERTION.sub(' ', state_clause)))))
            state_fallback = _non_status_fallback_projection(state_masked)
            if (re.search(_ACTION + '|' + _EN_ACTION + r'|资金|款项|funds', _FEE_TERMS.sub(' ', fallback))
                    and re.search(r'到账|完成|成功|失败|结算|状态|结果|completed|success|failed|pending|unknown|arrived', _fallback_reference_state_projection(state_fallback))):
                result.append((clause, {'unsupported'}))
                if code_result_contexts is not None:
                    code_result_contexts.append(code_result)
    return result


def _template(operation):
    state = operation['state']
    if state == 'pending_confirmation':
        return '已准备模拟操作草案，待本人核对确认，尚未执行。'
    if state in ('cancelled', 'invalidated', 'expired'):
        label = {'cancelled': '撤回', 'invalidated': '失效', 'expired': '过期'}[state]
        return '这份草案已' + label + '，尚未执行原操作。'
    if state == 'pending_settlement':
        return '该模拟操作待结算，尚不能认定资金到账。'
    if state in _FAILED:
        return '原操作失败或被拒绝；不能声称交易成功。'
    if state in _TERMINAL_SUCCESS:
        return '同一模拟操作已完成；以本机账本回执为准，不代表真实外部账户到账。'
    return '原操作结果尚未确定，请先查询原操作状态，不要重新发起交易。'


def _deny(code, operations=(), *, location=None, locations=()):
    if code == 'OPERATION_CODE_STATUS_EVIDENCE_REQUIRED':
        error = OperationWordingError(code, '本轮没有匹配且已核验的成功沙箱回执，不能声称代码已成功执行；代码回执不能证明金融操作成功。', 422, location=location)
        error.operation_wording_instruction = '只修正代码状态说明，不重新创建或执行金融操作。'
        raise error
    templates = list(dict.fromkeys(_template(op) for op in operations))
    if not templates:
        templates = ['尚未取得对应原操作的真实回执，不能声称交易成功；'
                     + ('请先查询原操作状态。' if location is None
                        else '未核实的真实交易状态不能作为事实。')]
    feedback = ''
    if location is not None:
        feedback = ('触发字段：' + location + '。仅修正这个可见字段。'
                    '若为公开规则说明或查询，保留条件并使用明确的条件式或疑问式；'
                    '若声明真实交易状态，必须绑定同一原操作的真实回执。'
                    '不得把未核实的状态陈述改成问句规避校验，不得虚构操作ID，'
                    '不得重新创建、重发或执行金融操作。')
        if location.startswith('followups['):
            feedback += '非必要的后续建议问题可以省略；不能通过提问洗白真实状态陈述。'
    error = OperationWordingError(code, '操作状态必须与同一原操作真实回执一致。' + ' '.join(templates) + feedback, 422, location=location, locations=locations)
    # Local recovery metadata only, never a new model protocol/permission field.
    # The same feedback is in message because the existing consumer serializes
    # BankError.as_dict(); adding this attribute alone would not reach the model.
    error.operation_wording_instruction = '只修正状态表述，不重新创建或执行操作。' + ' '.join(templates) + feedback
    raise error


def _records(final, tools):
    selected = set(final.get('draft_ids') or ())
    relevant = set()
    for claim in final.get('claims') or ():
        if isinstance(claim, dict) and _assertions(claim.get('text', '')):
            relevant.update(claim.get('evidence_ids') or ())
    records = {}
    for evidence_id, evidence in tools.evidence.items():
        if evidence.get('tool') not in _TOOLS:
            continue
        data = evidence.get('data', {})
        operation = data.get('operation')
        if evidence['tool'] == 'withdraw_draft':
            operation = {'id': data.get('operation_id'), 'state': data.get('state')}
        if not isinstance(operation, dict) or not isinstance(operation.get('id'), str):
            _deny('OPERATION_STATUS_BINDING_INVALID')
        if selected and operation['id'] not in selected:
            continue
        if relevant and evidence_id not in relevant and operation['id'] not in selected:
            continue
        if (evidence.get('source') != 'simulation' or
                evidence.get('context_hash') != tools.start_context):
            _deny('OPERATION_STATUS_BINDING_INVALID')
        records.setdefault(operation['id'], []).append((evidence_id, evidence['tool'], operation))
    return records


def _code_clause(clause, kinds, tools, bank_db, *, code_result_context=False):
    code_result = (code_result_context and kinds == {'unsupported'}
                   and _CODE_RESULT_CONTINUATION.fullmatch(clause))
    if not _CODE_SUBJECT.search(clause) and not code_result:
        return False
    if any(verb in clause for verb in _VERBS) or re.search(r'银行|资金|款项|money|funds|bank', clause):
        return False
    if code_result:
        kinds = {'success'}
    session = getattr(tools, 'generated_code_session', None)
    if kinds <= {'unexecuted'} and (session is None or not getattr(session, '_jobs', ())):
        return True
    from .generated_code import GeneratedCodeSession
    if (type(session) is not GeneratedCodeSession or session.tools is not tools
            or session.g is not tools.g or session.w != tools.w or not session._jobs
            or kinds != {'success'}):
        _deny('OPERATION_CODE_STATUS_EVIDENCE_REQUIRED')
    references = session.receipt_refs()
    supported = [e for e in tools.evidence.values() if e.get('source') == 'restricted_generated_code'
                 and e.get('tool') == 'run_generated_code' and e.get('data') in references
                 and e.get('context_hash') == tools.start_context]
    if not supported:
        _deny('OPERATION_CODE_STATUS_EVIDENCE_REQUIRED')
    for job in session._jobs:
        receipt = job['receipt']
        if (receipt.get('status') != 'COMPLETED_CANDIDATE'
                or receipt.get('worker_reaped') is not True
                or receipt.get('worker_group_absent') is not True
                or receipt.get('temporary_directory_removed') is not True
                or job['result'].get('receipt_sha256') != _digest(receipt)):
            _deny('OPERATION_CODE_STATUS_EVIDENCE_REQUIRED')
    if bank_db is None:
        session.verify_fresh()
    else:
        session.verify_for_publication(bank_db)
    return True


def validate_operation_wording(final, tools, *, bank_db=None):
    """Read-only first-review and same-bank publication check; never nests guards.

    Return local bindings for the trusted caller. The original final/model tool
    schema is unchanged. Publication supplies its already-held bank connection;
    the initial check uses one read-only-lifetime connection, without BEGIN.
    """
    if not isinstance(final, dict):
        _deny('OPERATION_WORDING_UNSUPPORTED')
    text = final.get('text', final.get('reply', ''))
    if not isinstance(text, str):
        _deny('OPERATION_WORDING_UNSUPPORTED')
    exact_quotes = [h['excerpt'] for e in tools.evidence.values()
                    if e.get('tool') == 'knowledge_search' and e.get('source') == 'public_knowledge'
                    for h in e.get('data', {}).get('hits', ())
                    if isinstance(h, dict) and isinstance(h.get('excerpt'), str)]
    state_text, history_fragments, history_claims = _history_quote_projection(final, tools, text, bank_db)
    statistic_ranges = _statistic_atom_ranges(final, tools, bank_db)
    assertions, assertion_locations, code_result_contexts, visible_values = [], [], [], []
    def collect(value, location, quotes=()):
        # History projection also preserves length, so issuer offsets still
        # address the exact original field. Never mask an entire free field.
        for start, end in statistic_ranges.get(location, ()):
            value = value[:start] + ' ' * (end - start) + value[end:]
        visible_values.append((value, location, quotes))
    collect(state_text, 'text', exact_quotes)
    for index, claim in enumerate(final.get('claims') or ()):
        if isinstance(claim, dict) and isinstance(claim.get('text'), str):
            claim_text = claim['text']
            # Only exact claims independently checked by the typed issuer can
            # quote these fragments. A different claim cannot borrow a body's
            # exemption, even when it repeats the same historical wording.
            if claim_text in history_claims:
                for fragment in history_fragments:
                    if claim_text == '“' + fragment + '”':
                        claim_text = '“' + ' ' * len(fragment) + '”'
                        break
            collect(claim_text, 'claims[' + str(index) + '].text', exact_quotes)
    for index, followup in enumerate(final.get('followups', ())):
        collect(followup, 'followups[' + str(index) + ']')
    interaction = final.get('interaction')
    if isinstance(interaction, dict):
        visible_fields = [('interaction.prompt', interaction.get('prompt'))]
        visible_fields.extend(('interaction.options[' + str(index) + '].label', option.get('label'))
                              for index, option in enumerate(interaction.get('options', ()))
                              if isinstance(option, dict))
        for location, visible in visible_fields:
            if isinstance(visible, str):
                collect(visible, location)
    allow_denied_arrival_projection = all(
        _denial_context_is_closed(value, quotes) for value, _, quotes in visible_values)
    for value, location, quotes in visible_values:
        found = _assertions(value, quotes,
                            allow_denied_arrival_projection=allow_denied_arrival_projection,
                            code_result_contexts=code_result_contexts)
        assertions.extend(found)
        assertion_locations.extend([location] * len(found))
    if not assertions:
        return {'version': 'host-operation-wording/1', 'bindings': []}
    retained = [(assertion, location) for assertion, location, code_result_context in zip(
                    assertions, assertion_locations, code_result_contexts)
                if not _code_clause(assertion[0], assertion[1], tools, bank_db,
                                    code_result_context=code_result_context)]
    assertions = [assertion for assertion, _ in retained]
    assertion_locations = [location for _, location in retained]
    if not assertions:
        return {'version': 'host-operation-wording/1', 'bindings': []}
    records = _records(final, tools)
    if not records:
        if (not final.get('draft_ids') and assertions
                and all(kinds <= {'unexecuted'} and _GENERAL_NO_EXECUTION.fullmatch(clause)
                        for clause, kinds in assertions)):
            return {'version': 'host-operation-wording/1', 'bindings': []}
        # A general refusal describes this chat's lack of execution, rather
        # than the result of an unseen prior transaction.
        if all(kinds <= {'unexecuted'} for _, kinds in assertions) and re.search(
                r'(?:本轮|本次对话).{0,40}(?:没有|未|尚未).{0,8}' + _ACTION, _normalize(text)):
            return {'version': 'host-operation-wording/1', 'bindings': []}
        if all(kinds <= {'unknown'} for _, kinds in assertions) and re.search(
                r'(?:尚未取得|未取得|没有收到).{0,12}回执', _normalize(text)):
            return {'version': 'host-operation-wording/1', 'bindings': []}
        if all(kinds <= {'unexecuted'} for _, kinds in assertions) and re.search(
                r'\b(?:this turn|this conversation).{0,40}not (?:yet )?executed\b', _normalize(text)):
            return {'version': 'host-operation-wording/1', 'bindings': []}
        _deny('OPERATION_STATUS_EVIDENCE_REQUIRED', location=assertion_locations[0],
              locations=tuple(dict.fromkeys(assertion_locations)))
    if bank_db is None:
        with closing(tools.g.bank.connect()) as connection:
            return validate_operation_wording(final, tools, bank_db=connection)
    database = next((row[2] for row in bank_db.execute('PRAGMA database_list') if row[1] == 'main'), None)
    if not database or Path(database).resolve() != Path(tools.g.bank.database).resolve():
        _deny('OPERATION_STATUS_BINDING_INVALID')
    from .bank_tools import safe_data
    current = []
    for operation_id, sources in records.items():
        try:
            actual = safe_data(tools.g.bank._public_operation(
                tools.g.bank._operation(bank_db, tools.w, operation_id)))
        except BankError:
            _deny('OPERATION_STATUS_BINDING_INVALID')
        # The latest real status observation supersedes a preparation snapshot;
        # neither a draft nor an old successful operation can create a receipt.
        statuses = [s for s in sources if s[1] == 'operation_status']
        source = statuses[-1] if statuses else sources[-1]
        observed = source[2]
        for key in ('state', 'action', 'version', 'result'):
            if key in observed and observed[key] != actual.get(key):
                _deny('OPERATION_STATUS_CHANGED', [actual])
        current.append({'operation': actual, 'evidence_id': source[0], 'tool': source[1]})
    if len(current) != 1:
        _deny('OPERATION_WORDING_AMBIGUOUS', [r['operation'] for r in current])
    record = current[0]
    operation = record['operation']
    state, action = operation['state'], operation['action']
    for clause, kinds in assertions:
        references = re.findall(r'(?:原?操作|operation(?:_id)?|op_id)\s*[:：#]?\s*([a-f0-9]{32})', clause, re.I)
        if any(reference != operation['id'] for reference in references):
            _deny('OPERATION_STATUS_BINDING_INVALID', [operation])
        if 'unsupported' in kinds:
            _deny('OPERATION_WORDING_UNSUPPORTED', [operation])
        for verb, actions in _VERBS.items():
            if verb in clause and kinds & {'success', 'failed', 'pending'} and action not in actions:
                _deny('OPERATION_STATUS_ACTION_MISMATCH', [operation])
        if 'prepared' in kinds and (state != 'pending_confirmation' or record['tool'] not in ('prepare_operation', 'operation_status')):
            _deny('OPERATION_STATUS_CLAIM_MISMATCH', [operation])
        if 'unexecuted' in kinds and state not in _UNEXECUTED:
            _deny('OPERATION_STATUS_CLAIM_MISMATCH', [operation])
        if 'success' in kinds and (record['tool'] != 'operation_status' or
                state not in _TERMINAL_SUCCESS or not isinstance(operation.get('result'), dict) or not operation['result']):
            _deny('OPERATION_STATUS_CLAIM_MISMATCH', [operation])
        if 'failed' in kinds and (record['tool'] != 'operation_status' or state not in _FAILED):
            _deny('OPERATION_STATUS_CLAIM_MISMATCH', [operation])
        if 'pending' in kinds and (record['tool'] != 'operation_status' or state != 'pending_settlement'):
            _deny('OPERATION_STATUS_CLAIM_MISMATCH', [operation])
        if 'unknown' in kinds and (record['tool'] != 'operation_status' or state in _KNOWN):
            _deny('OPERATION_STATUS_CLAIM_MISMATCH', [operation])
        if re.search(r'(?:资金|钱|款项)已到账|已经到账', clause) and (
                state != 'settled' or operation.get('result', {}).get('status') != 'settled'):
            _deny('OPERATION_STATUS_CLAIM_MISMATCH', [operation])
        if re.search(r'\bfunds? (?:have |has )?arrived\b', clause) and (
                state != 'settled' or operation.get('result', {}).get('status') != 'settled'):
            _deny('OPERATION_STATUS_CLAIM_MISMATCH', [operation])
        if re.search(r'发卡|核发|\bissued\b|\bissuance\b', clause) and 'success' in kinds and operation.get('result', {}).get('status') != 'issued':
            _deny('OPERATION_STATUS_CLAIM_MISMATCH', [operation])
    return {'version': 'host-operation-wording/1', 'bindings': [{
        'workspace': tools.w, 'operation_id': operation['id'], 'action': action,
        'state': state, 'version': operation['version'],
        'receipt_hash': _digest(operation.get('result')),
        'evidence_id': record['evidence_id'], 'tool': record['tool'],
        'template': _template(operation)}]}
