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

    def __init__(self, code, message, status, *, location=None, locations=(), assertion_details=()):
        super().__init__(code, message, status)
        self._operation_wording_location = location
        self._operation_wording_locations = tuple(dict.fromkeys(locations))
        # Diagnostic only: exact assertions already found by the real guard.
        self._operation_wording_assertions = tuple(assertion_details)

    @property
    def location(self):
        return self._operation_wording_location

    @property
    def locations(self):
        return self._operation_wording_locations

    @property
    def assertion_details(self):
        return self._operation_wording_assertions


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
# An omitted subject is accepted as an explicit arrival claim only when the
# whole comma-delimited clause is this finite completed predicate. It must use
# the same settled receipt check below; a research complement cannot match it.
_ELLIPTICAL_ARRIVAL = re.compile(
    r'^(?:不过|然而|并且|但|且)?(?:已经|已)(?:成功)?到账(?:了)?$')
_PATTERNS = (
    ('pending', re.compile(r'待结算|等待结算|尚未结算|待到账|尚未到账')),
    ('unknown', re.compile(r'(?:状态|结果)(?:仍|尚|还)?(?:未知|不明|无法确认|未确定)|暂时无法确认(?:结果|状态)|不能确认(?:交易|操作)?是否成功')),
    ('unexecuted', re.compile(r'(?:尚未|还未|没有|未)(?:实际)?(?:执行|转账|转出|付款|支付|买入|申购(?!手续费|费)|挂失)|(?:待|等待)(?:你|您|我|本人|用户)?.{0,8}确认|已撤回.{0,8}草案|草案已(?:撤回|取消)')),
    ('prepared', re.compile(r'已.{0,8}准备.{0,12}(?:草案|转账|操作)|准备了.{0,12}(?:草案|转账|操作)|草案已(?:准备|生成)')),
    ('failed', re.compile(_ACTION + r'(?:已)?失败|(?:未能|未成功)' + _ACTION + r'|执行未成功')),
    ('success', re.compile(r'(?:已经|已)(?:成功)?' + _ACTION + r'(?!失败|未成功)|' + _ACTION + r'(?:已经|已)?(?:成功|完成)|(?:资金|钱|款项)已到账|已经到账|' + _ELLIPTICAL_ARRIVAL.pattern)),
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
    r'|^(?:也|并)?不代表交易已执行(?:或独立验证(?:了)?原始数据真实(?:性)?)?$')
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
    r'(?:(?:本轮|本次对话|本次)(?:中)?\s*)?(?:没有|未|尚未)(?:实际)?执行'
    r'(?:任何(?:金融|资金)?(?:操作|交易)|(?:金融|资金)(?:操作|交易))')
_CODE_SUBJECT = re.compile(r'代码|沙箱|\bwat\b|\bwasm\b|\bcode\b|\bsandbox\b')
# Only a closed comma continuation can inherit the immediately preceding code
# subject. It still needs the original receipt/freshness checks in _code_clause.
_CODE_RESULT_CONTINUATION = re.compile(r'执行结果与既有指标一致')
# 这些封闭的既有指标复现表述仍须走原真实 session/receipt 与末端复验。
# 可变部分仅为指标标签；标签里出现任何金融动作时不进入代码回执通道。
_CODE_REPRODUCTION_PROOF = re.compile(
    r'已对(?P<metric_label>[^，,。；;！!\n]{1,64})指标执行受限代码复现'
    r'|(?:隔离)?执行结果与(?:既有)?指标一致'
    r'|工具成功(?:只|仅)表示隔离执行结果与(?:既有)?指标一致'
    r'|成功回执(?:只|仅)(?:说明|表示)隔离执行结果与既有指标一致'
    r'|不复用本次执行结果')
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


def _capability_context_is_closed(text):
    """Unknown or affirmative commitments disable this new projection only.

    An immediately governing finite denial may have any object, including
    returns. Earlier negation makes that denial ambiguous/double-negative and
    fails closed; original arrival-denial semantics are never changed here.
    """
    for clause in re.split(r'[，,。；;！!\n]', text):
        for commitment in re.finditer(r'保证|确保|承诺', clause):
            prefix = clause[:commitment.start()]
            denial = re.search(r'(?:不能|无法|并不|不)$', prefix)
            if denial is None or re.search(r'不|无|未|非|没', prefix[:denial.start()]):
                return False
    return True


def _capability_arrival_projection(clause):
    """Mask arrival only inside a complete generic capability list item.

    Actual statuses were scanned first, and the caller checked commitments in
    the complete text. Private/actualized colon topics remain in scope, while
    a private term in an independent list item does not become the topic.
    """
    item = re.compile(r'(?:以及|并且|且|并)?' + _ACTION
                      + r'(?:能够|可以|能|可)(?:及时|按时|按期|如期)?'
                      r'(?P<arrival>到账)')
    chars = list(clause)
    prior_item, specific_topic = '', False
    for part in re.finditer(r'(?P<boundary>^|[:：、])(?P<item>[^:：、]+)', clause):
        if part.group('boundary') in (':', '：'):
            specific_topic = specific_topic or bool(
                _REFERENCE_PRIVATE_SUBJECT.search(prior_item)
                or re.search(r'资金|款项|钱|已|刚|曾|到账|完成|成功|失败|结算|状态|结果', prior_item))
        raw = part.group('item')
        prior_item = raw
        node = raw.strip()
        relation = item.fullmatch(node)
        if specific_topic or relation is None or _REFERENCE_PRIVATE_SUBJECT.search(node):
            continue
        start = (part.start('item') + len(raw) - len(raw.lstrip())
                 + relation.start('arrival'))
        chars[start:start + 2] = '  '
    return ''.join(chars)


_CLOSED_NOMINAL_CONNECTOR = re.compile(r'(?:并且|而且|但是|然而|不过|且|并|但)')


def _closed_nominal_clause(clause):
    """One optional discourse head for closed nominal relations, with raw offsets.

    Never preprocess the assertion scanner's input. Each relation owns its full
    grammar and projects only its captured property/result span into the original
    clause. A second connector is unknown syntax, not another optional head.
    """
    value = clause.strip()
    offset = len(clause) - len(clause.lstrip())
    connector = _CLOSED_NOMINAL_CONNECTOR.match(value)
    if connector is not None:
        offset += connector.end()
        value = value[connector.end():]
        if _CLOSED_NOMINAL_CONNECTOR.match(value) is not None:
            return None
    return value, offset


def _research_attribute_projection(clause, *, sentence=None):
    """Project only state words used inside a closed research object's NP.

    The original explicit patterns examine the unmodified clause first. A
    complete verification relation, including coordinated attribute heads,
    distinguishes a property noun from an observed operation predicate. No
    unknown tail or factual copula is consumed; public truth is still reviewed.
    """
    entry = _closed_nominal_clause(clause)
    if entry is None:
        return None
    value, offset = entry
    join = r'(?:以及|和|与|及|或|、)'
    process = r'(?:赎回(?:申请)?到账|(?:' + _ACTION + r')(?:确认)?|到账|结算)'
    attribute = (r'(?:(?:具体)?资产类别|赎回申请截止时间|开放日限制|最低持有(?:要求|期限)|(?:' + process + r'(?:' + join + process + r'){0,3})?'
                 r'(?:确认时间|条件|条款|规则|时间|时点|日期|期限|费用|费率))')
    # Labels are nominal modifiers, never the projected span. Predicate-bearing
    # labels cannot lend a property head to a personal/extra state assertion.
    noun = r'[a-z0-9_\u3400-\u9fff\-]{1,80}'
    note_item = r'[a-z0-9_\u3400-\u9fff\-]{1,40}'
    # NFKC already ran: one balanced explanatory parenthesis, with a bounded
    # nominal list. Slash is structural only here, never a free label character.
    note = r'\(' + note_item + r'(?:[/、]' + note_item + r'){0,7}\)'
    label_noun = noun + r'(?:' + note + r'(?:' + noun + r')?)?的'
    nominal = (r'(?P<label>(?:' + label_noun + r'|其|该|这|本))?'
               r'(?P<attributes>' + attribute + r'(?:' + join + attribute + r'){0,7})')
    modifier = r'(?:重点)?(?:先|进一步|逐一|逐项)?'
    verify = r'(?:核验|核实|核对|核查|研究)'
    # A complete object-first request owns a finite product-attribute list.
    # Bare process nouns share the final conditions/rules head; only the
    # attribute span is projected, after all explicit state patterns ran.
    # Unknown tails, private subjects and completed predicates cannot close it.
    research_member = r'(?:期限|赎回|到账|结算|费用|费率)'
    object_first = re.fullmatch(
        r'(?:请)?(?:帮我)?(?:把|将)(?:候选|相关|所选)(?:产品|基金)的'
        r'(?P<attributes>' + research_member + r'(?:' + join + research_member + r'){1,7})'
        r'(?:条件|条款|规则)(?:逐条|逐一|逐项)?'
        r'(?:(?:核对|核实|核验|核查)(?:出来|清楚)?|核出来)', value)
    if object_first is not None:
        left, right = object_first.span('attributes')
        chars = list(clause)
        for token in re.finditer(r'到账|结算', value[left:right]):
            start = offset + left + token.start()
            chars[start:start + len(token.group())] = ' ' * len(token.group())
        return ''.join(chars)
    # A necessity/modal head can qualify the read-only method, rather than an
    # operation's state. The source PP and complete nominal object own distinct
    # roles; only the object's attribute span is projected below. Unknown tails,
    # personal operation objects and extra predicates cannot close this grammar.
    modal_relation = None
    if len(value) <= 640 and len(re.findall(join, value)) < 8:
        necessity = r'(?:需要|需|应该|应当|应|须|必须|有待|待)'
        method_source = (r'(?:按(?:照)?|依据|根据)'
                         r'(?:(?:本轮|本次|当前|现行|原|公开)(?:的)?)?'
                         r'(?:产品|基金)?(?:条款|规则|原文|文档|资料)')
        modal_relation = re.fullmatch(
            r'(?:我们|本助手|我)?(?:仍|尚|还)?' + necessity
            + r'(?:' + method_source + r')?' + modifier + verify
            + r'(?P<object>' + nominal + r')', value)
        if (modal_relation is not None
                and _REFERENCE_PRIVATE_SUBJECT.search(modal_relation['object'])):
            modal_relation = None
    # A complete verification-focus list owns only its finite property NPs.
    # All members must close; private subjects, statuses and instruction tails
    # cannot borrow the first member's research scope.
    focus = re.fullmatch(r'(?:真正|实际)?(?:需要|需|有待|待)'
                         + verify + r'的是[:：]?(?P<items>[^，,。；;！!\n]{1,640})', value)
    if focus is not None:
        items = focus['items'].split('、')
        if not 1 <= len(items) <= 8 or any(not item for item in items):
            return None
        chars = list(clause);cursor = focus.start('items')
        for raw in items:
            item = re.fullmatch(r'(?:是否(?:有|存在))?(?P<attributes>' + attribute + r')', raw)
            if item is None:
                return None
            left, right = item.span('attributes')
            for token in re.finditer(r'到账|结算', raw[left:right]):
                start = offset + cursor + left + token.start()
                chars[start:start + len(token.group())] = ' ' * len(token.group())
            cursor += len(raw) + 1
        return ''.join(chars)
    # A read-only listing/selection owns a complete coordinated object tree.
    # Parse the finite object NPs separately, so right-shared property heads
    # and outer NP coordination never form nested ambiguous repetitions.
    category_np = r'(?:候选)?(?:产品|资产)?类别'
    public_label = r'(?:(?:候选|相关|具体)?(?:产品|基金)的)?'
    verify_np = r'(?:(?:需要|需|有待|待|可)' + verify + r'的)?'
    property_np = (r'(?:赎回申请截止时间|开放日限制|最低持有(?:要求|期限)|(?:'
                   + process + r'(?:' + join + process + r'){0,3})?'
                   r'(?:确认时间|条件|条款|规则|时间|时点|日期|期限|费用|费率))')
    object_member = re.compile(r'(?:可研究的)?(?:' + category_np
                               + r'|' + verify_np + public_label + property_np + r')')
    request = re.match(r'(?:请)?(?:列出|筛选|筛出)', value)
    if request is not None and len(value) <= 640:
        objects = value[request.end():]
        links = list(re.finditer(join, objects))
        if len(links) < 8 and not _REFERENCE_PRIVATE_SUBJECT.search(objects):
            # Every join is either internal to a shared-head property NP or
            # between complete NPs. At most eight coordinated leaves are
            # admitted; reachable boundaries avoid exponential repartitioning.
            ends = [(link.start(), link.end()) for link in links]
            ends.append((len(objects), len(objects) + 1))
            reachable = {0}
            for left in [0] + [link.end() for link in links]:
                if left not in reachable:
                    continue
                for right, next_left in ends:
                    if right > left and object_member.fullmatch(objects[left:right]):
                        reachable.add(next_left)
            if len(objects) + 1 in reachable:
                # Only attributes can contain these tokens in the closed tree.
                # Keep all process/action words and separators; explicit status
                # predicates have already scanned the untouched input first.
                chars = list(clause)
                for token in re.finditer(r'到账|结算', objects):
                    start = offset + request.end() + token.start()
                    chars[start:start + len(token.group())] = ' ' * len(token.group())
                return ''.join(chars)
    # Preserve the existing parenthesized condition-list relation. Bare process
    # nouns are attributes only inside that complete finite list.
    list_attribute = r'(?:期限|赎回|到账|费用)'
    research_list = re.fullmatch(
        r'(?:请)?列出可研究的(?P<label>)产品条件清单\('
        r'(?P<attributes>' + list_attribute + r'(?:、' + list_attribute + r'){1,7})\)', value)
    relation = modal_relation or research_list or (
        re.fullmatch(r'(?:我们|本助手|我)?(?:可以|可|需要|需)?'
                     + modifier + verify + nominal, value)
        or re.fullmatch(nominal
                        + r'(?:(?:本轮)?(?:仍|尚|还)?未|有待|待|(?:都)?(?:需要|需|要))'
                        + modifier + verify, value)
        or re.fullmatch(nominal + r'(?:都)?(?:仍|尚|还)?(?:需要|需)按原文'
                        + verify, value)
        or re.fullmatch(r'(?P<label>)(?P<attributes>(?:实际)?到账(?:时间|周期|条件))'
                        r'(?:(?:仍|还|尚)?(?:需要|需)按赎回规则(?:核实|确认)'
                        r'|待按赎回规则核实)', value))
    # Parse a finite read-only selection request into its period, research
    # predicate and existing nominal object. Only redemption/arrival conditions
    # with no personal label can fill this object; unknown tails never close.
    relation_offset = 0
    if relation is None:
        request = re.match(r'帮我按' + _GUARANTEE_TIME + r'用款期限筛出可'
                           + verify, value)
        if request is not None and value.endswith('的候选'):
            member = re.fullmatch(nominal, value[request.end():-3])
            condition = r'(?:赎回(?:' + join + r'到账)?|到账)条件'
            if (member is not None and not member.group('label')
                    and re.fullmatch(condition, member.group('attributes'))):
                relation, relation_offset = member, request.end()
    if relation is None and sentence is not None:
        # Preserve comma-connected original sentence scope. Ideographic list
        # separators remain inside one guard clause; each projected item must
        # independently be a complete attribute NP under this explicit head.
        head = (r'(?:下一步|接下来)?(?:我们|本助手|我)?(?:仍|尚|还)?'
                r'(?:需要|需|有待|待)' + modifier + verify + r'的是[:：]')
        listing = re.fullmatch(head + r'(?P<items>[^。；;！!\n]{1,640})', sentence.strip())
        if listing is not None:
            items = re.split(r'[，,、]', listing['items'])
            if 1 <= len(items) <= 8 and all(item.strip() for item in items):
                chars = list(clause)
                projected = False
                for item in items:
                    raw = item.strip()
                    candidate = re.sub(r'^(?:以及|和|与|及|或)', '', raw, count=1).strip()
                    member = re.fullmatch(nominal, candidate)
                    if member is None:
                        continue
                    label = member.group('label') or ''
                    if len(label) > 81 or re.search(_ACTION + r'|到账|结算|完成|成功|失败|状态|结果|已|未|曾|刚'
                            r'|收到|获得|变成|成为|等于|意味着|核验|核实|核对|核查|研究'
                            r'|是|为|把|将|并|但|且', label):
                        continue
                    left, right = member.span('attributes')
                    for occurrence in re.finditer(re.escape(raw), value):
                        prefix = raw.find(candidate)
                        offset_in_clause = offset + occurrence.start() + prefix + left
                        for token in re.finditer(r'到账|结算', candidate[left:right]):
                            start = offset_in_clause + token.start()
                            chars[start:start + len(token.group())] = ' ' * len(token.group())
                            projected = True
                if projected:
                    return ''.join(chars)
    if relation is None:
        return None
    label = relation.group('label') or ''
    if len(label) > 81 or re.search(_ACTION + r'|到账|结算|完成|成功|失败|状态|结果|已|未|曾|刚'
                 r'|收到|获得|变成|成为|等于|意味着|核验|核实|核对|核查|研究'
                 r'|是|为|把|将|并|但|且', label):
        return None
    left, right = relation.span('attributes')
    left, right = left + relation_offset, right + relation_offset
    chars = list(clause)
    for token in re.finditer(r'到账|结算', value[left:right]):
        start = offset + left + token.start()
        chars[start:start + len(token.group())] = ' ' * len(token.group())
    return ''.join(chars)


def _planning_timing_buffer_projection(clause):
    """A closed planning-time noun is not an observed redemption state.

    Only the arrival token inside a complete before-maturity/use timing-buffer
    instruction is projected. Explicit statuses have already run on the
    unchanged clause; subjects, commitments, extra predicates and tails cannot
    enter this grammar. No conditional context is inherited across clauses.
    """
    entry = _closed_nominal_clause(clause)
    if entry is None:
        return None
    value, offset = entry
    relation = re.fullmatch(
        r'(?:(?:需要|需|应|须|必须)(?:在)?|在)?(?:到期|用款)前'
        r'(?:预留|留出|留足)赎回(?:确认)?(?:和|与|及|、)'
        r'(?P<arrival>到账)(?:所需的|的)?时间', value)
    if relation is None:
        return None
    start, end = relation.span('arrival')
    return clause[:offset + start] + ' ' * (end - start) + clause[offset + end:]


# A closed noun atom names a timing property, never an operation result.
# Project only its arrival characters after original explicit statuses ran.
_REDEMPTION_PERIOD_PROPERTY = re.compile(
    r'赎回(?:/|和|与|及|、)(?P<arrival>到账)周期(?![a-z0-9_])')
_REDEMPTION_PERIOD_REMAINDER = re.compile(
    _ACTION + '|' + _EN_ACTION
    + r'|资金|款项|到账|完成|成功|失败|结算|状态|结果|已|曾|处理|完毕'
      r'|忽略|绕过|跳过|completed|success|failed|pending|unknown|arrived'
      r'|already|processed|bypass|ignore', re.I)
_LIQUID_PRODUCT_PROPERTY = re.compile(
    r'(?:建议|可以|可)?(?:优先)?(?:考虑|选择|核对)?'
    r'可随时赎回(?:、|和|与|及)(?P<arrival>到账)快的'
    r'(?:低|较低)风险(?:产品|基金)')


def _redemption_property_projection(clause, *, sentence=None):
    stripped = clause.strip()
    if _REFERENCE_PRIVATE_SUBJECT.search(stripped):
        return clause
    periods = tuple(_REDEMPTION_PERIOD_PROPERTY.finditer(stripped))
    if periods:
        # Keep the original prefix/suffix. Another action or status predicate
        # cannot borrow a noun atom's non-status projection.
        context = stripped if sentence is None else sentence
        remainder = _REDEMPTION_PERIOD_PROPERTY.sub(' ', context)
        if (_REFERENCE_PRIVATE_SUBJECT.search(context)
                or _REDEMPTION_PERIOD_REMAINDER.search(remainder)):
            return clause
        offset = len(clause) - len(clause.lstrip())
        projected = clause
        for period in reversed(periods):
            start, end = period.span('arrival')
            projected = (projected[:offset + start] + ' ' * (end - start)
                         + projected[offset + end:])
        return projected
    liquid = _LIQUID_PRODUCT_PROPERTY.fullmatch(stripped)
    if liquid:
        offset = len(clause) - len(clause.lstrip())
        start, end = liquid.span('arrival')
        return clause[:offset + start] + ' ' * (end - start) + clause[offset + end:]
    return clause


def _redemption_sentence_context(parts, index):
    # Commas cannot launder a private/action premise into a noun-only clause.
    left, right = index, index + 1
    while left >= 2 and parts[left - 1] in ('，', ','):
        left -= 2
    while right + 1 < len(parts) and parts[right] in ('，', ','):
        right += 2
    return ''.join(parts[left:right])


def _product_liquidity_requirement_projection(clause):
    """Project one arrival noun in a complete product timing requirement.

    Only this closed requirement grammar reaches the fallback, after every
    original explicit state pattern. Personal subjects, status assertions,
    guarantees, extra predicates and instruction tails cannot match it.
    """
    entry = _closed_nominal_clause(clause)
    if entry is None:
        return None
    value, offset = entry
    relation = re.fullmatch(
        r'(?:(?:(?:期限|用款|流动性)(?:约束|要求))?(?:意味着|要求))?'
        r'(?:所选|候选|相关)?(?:产品|基金)(?:必须|须|需要|应当|应)'
        r'(?:在)?(?:到期|用款)前(?:可|可以|能够)赎回'
        r'(?:且|并且|并)(?:可|可以|能够)?(?P<arrival>到账)', value)
    if relation is None:
        return None
    start, end = relation.span('arrival')
    return clause[:offset + start] + ' ' * (end - start) + clause[offset + end:]


def _non_equivalent_basis_projection(clause, *, sentence=None):
    """A closed nonfactive comparison does not assert its result's state.

    Project only result-state tokens after a complete raw-clause match, before
    explicit state recognition. An omitted basis inherits only the preceding
    nominal premise in this same comma-connected sentence, never another
    sentence, a transaction or an arbitrary model assertion.
    """
    entry = _closed_nominal_clause(clause)
    if entry is None:
        return None
    value, offset = entry
    result_noun = r'(?:已结算现金|已到账现金|赎回(?:已|已经)?(?:完成|到账)|赎回成功|赎回|(?:已|已经)?到账|市值)'
    result = result_noun + r'(?:(?:或|和|与|及|、)' + result_noun + r'){0,3}'
    basis = r'(?:持仓(?:账面)?本金|账面本金|本金|解除(?:持仓)?锁定|解锁)'
    negative_equivalence = r'(?:也|仍)?(?:不等于|不是|不意味着|不代表|不能等同于)'
    relation = re.fullmatch(
        r'(?:(?:另)?(?:一|[1-9])只锁定日已到但)?'
        + basis + negative_equivalence + r'(?P<result>' + result + r')', value)
    if relation is None and sentence is not None:
        omitted = re.fullmatch(r'也不等于(?P<result>' + result + r')', value)
        premise = re.fullmatch(r'账面本金不是市值[，,]\s*也不等于' + result,
                               sentence.strip())
        if omitted is not None and premise is not None:
            relation = omitted
    if relation is None:
        return None
    left, right = relation.span('result')
    chars = list(clause)
    for state in re.finditer(r'到账|结算|完成|成功', value[left:right]):
        start = offset + left + state.start()
        chars[start:start + len(state.group())] = ' ' * len(state.group())
    return ''.join(chars)


def _non_status_fallback_projection(clause, *, allow_capability_arrival_projection=False,
                                    redemption_sentence=None):
    non_equivalence = _non_equivalent_basis_projection(clause, sentence=redemption_sentence)
    if non_equivalence is not None:
        return non_equivalence
    timing_buffer = _planning_timing_buffer_projection(clause)
    if timing_buffer is not None:
        return timing_buffer
    requirement = _product_liquidity_requirement_projection(clause)
    if requirement is not None:
        return requirement
    research = _research_attribute_projection(clause, sentence=redemption_sentence)
    if research is not None:
        return research
    projected = _redemption_property_projection(_application_condition_projection(clause),
                                                sentence=redemption_sentence)
    if allow_capability_arrival_projection:
        projected = _capability_arrival_projection(projected)
    # A verification request concerns a product attribute, not an arrival
    # report. Only mask the arrival characters inside named attributes. Other
    # status predicates, including a coordinated or appended claim, remain.
    # The original explicit-state patterns run before this fallback.
    if (not _REFERENCE_PRIVATE_SUBJECT.search(clause)
            and re.search(r'产品|基金', clause)):
        projected = re.sub(r'到账(?=条件|条款|规则|时间|时点|日期|期限)',
                           '  ', projected)
    # A cash-basis rule names a qualifying noun, not a completed operation.
    # Full matching keeps individual status reports and appended predicates
    # under the original receipt checks.
    if (re.fullmatch(r'(?:但|不过|然而)?(?:当前|本轮|现在)?'
                    r'(?:可投资金|可投现金|可用资金|可安排资金)'
                    r'(?:仍|只|仅|应|必须)?以已结算(?:的)?现金(?:余额)?为准',
                    clause.strip())
            or re.fullmatch(r'(?:但|不过|然而)?(?:当前|本轮|现在)?(?:真正|实际)?'
                            r'(?:可投(?:的)?资金|可投(?:的)?现金|可用(?:的)?资金|可安排(?:的)?资金)'
                            r'(?:来自|来源于)(?:模拟)?账户(?:中)?(?:的)?'
                            r'已结算(?:的)?现金(?:余额)?',clause.strip())):
        return ' ' * len(clause)
    # Complete general timing/rule wording has no observed operation state.
    # This fallback runs only after every original explicit status pattern;
    # full matching admits no personal transaction, amount or extra predicate.
    timing_action = r'(?:赎回|转账|转出|汇款|汇出|付款|支付|申购|买入)'
    timing_rule = re.fullmatch(
        r'(?:且|并且|而且)?' + timing_action
        + r'确认(?:和|与|及)到账(?:本身)?(?:需要|需|要)时间', clause.strip())
    rule_query = re.fullmatch(
        r'(?:请)?(?:需要|需)?(?:先|再|进一步|逐一|逐项)?(?:核验|核实|核对)'
        r'(?:其|(?:具体)?(?:产品|基金)(?:的)?)?' + timing_action
        + r'(?:确认|规则|条件|条款)(?:和|与|及|、)到账(?:规则|条件|条款|时间|时点|日期|期限)'
        r'(?:后(?:才能|才可|方可|再)?确认)?',
        clause.strip())
    if (timing_rule or rule_query) and not _REFERENCE_PRIVATE_SUBJECT.search(clause):
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
                   r'|当前费用|赎回到账|赎回到账条件|赎回条件|到账时间|到账期限|到账条件)')
    if re.fullmatch(r'(?:不能|无法)据此(?:判断|确认)' + public_item
                    + r'(?:(?:、|或|及|与|和)' + public_item + r'){0,7}',
                    clause.strip()):
        return ' ' * len(clause)
    # A complete inability-to-provide relation concerns public product
    # attribute nouns, not a claimed private transaction result. Compose the
    # same nominal vocabulary instead of admitting a whole answer sentence.
    # Only NP-internal state tokens are projected; extra predicates survive.
    disclosure_item = (r'(?:(?:具体)?(?:产品|基金)|(?:当前)?(?:费用|费率)'
                       r'|赎回(?:(?:与|及|和)?到账)?(?:条件|条款|规则|时间|期限)'
                       r'|(?:当前)?在售状态)')
    disclosure = re.fullmatch(
        r'(?:因此|所以|因而|但|不过|然而)?(?:不能|无法)(?:给出|提供|确认|判断)'
        r'(?P<attributes>' + disclosure_item
        + r'(?:(?:、|或|及|与|和)' + disclosure_item + r'){0,7})', clause.strip())
    if disclosure is not None:
        start, end = disclosure.span('attributes')
        offset = len(clause) - len(clause.lstrip())
        for token in re.finditer(r'到账|状态', clause.strip()[start:end]):
            left = offset + start + token.start()
            projected = (projected[:left] + ' ' * len(token.group())
                         + projected[left + len(token.group()):])
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
    quantity = r'(?:(?:若干|几|数)(?:款|只|个)|(?:[一二三四五六七八九十两]{1,3}|[1-9]\d?)(?:款|只|个))?'
    category = r'(?:(?:短期|中期|长期)?(?:现金管理|货币市场|货币|存款|债券|权益|混合)(?:类)?|(?:短期|中期|长期))?'
    subject = (r'(?:具体|相关|候选|部分|这些|各类)?' + quantity + category
               + r'(?:产品|基金)(?:候选)?(?:的|、)?')
    property_head = r'(?:条款|(?:赎回(?:(?:和|与|及|、)?到账)?|到账)(?:条件|条款))'
    modifier = r'(?:先|进一步|逐一)?'
    verify = r'(?:核实|核验|核对)'
    prefix = r'(?:但是|不过|但)?'
    reference = (
        re.fullmatch(prefix + r'(?P<verifier>我可以帮你|我们|本助手|我)?'
                     + r'(?:可以|可|需要|需)?' + modifier + verify
                     + subject + r'(?P<property>' + property_head + r')', stripped)
        or re.fullmatch(prefix + subject + r'(?P<property>' + property_head + r')'
                        + r'(?:仍|尚|还)?(?:待|有待|需要|需|可以|可)'
                        + modifier + verify
                        + r'(?:后(?:才能|才可|方可|再)?确认)?', stripped))
    # A verifier subject belongs to the verification predicate, not the
    # product's operation. Only this captured role in a complete public
    # property query is removed from the private-subject check; the object
    # and every other private/specific-operation subject remain visible.
    private_context = stripped
    if reference and reference.groupdict().get('verifier'):
        start, end = reference.span('verifier')
        private_context = stripped[:start] + ' ' * (end - start) + stripped[end:]
    if (legacy or reference) and not _REFERENCE_PRIVATE_SUBJECT.search(private_context):
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


def _catalog_pending_noun_projection(text):
    """Only closed metric-directory/balance-basis nouns lose pending syntax.

    Specific operation subjects, status predicates, guarantees and arbitrary
    tails cannot match these complete frames. Metric truth remains checked by
    the original amount/source contract and independent full review.
    """
    item = (r'(?:两个账户的已结算余额|同币种现金合计|(?:正式画像)?应急保留'
            r'|(?:按产品的)?持仓(?:账面)?本金|待结算赎回)')
    directory = re.compile(
        r'(?P<boundary>^|[。；;！!\n])(?P<body>本轮账户读取返回[1-9][0-9]{0,2}项已签发指标[，,]'
        r'覆盖' + item + r'(?:[、]|及|和|与)' + item
        + r'(?:(?:[、]|及|和|与)' + item + r'){0,14}'
        r'[，,]指标状态均为已计算且无覆盖缺口)(?=$|[。；;！!\n])')
    balance = re.compile(
        r'(?P<boundary>(?:^|[。；;！!\n])(?:[0-9]{1,18}\.[0-9]{2}元|[0-9]{1,3}(?:,[0-9]{3}){1,5}\.[0-9]{2}元)?)'
        r'(?P<body>这是(?:[两三四五六七八九十0-9]{1,3}个)?账户已结算余额的合计口径[，,]'
        r'(?:不含|不包括)(?:持仓(?:账面)?本金(?:和|与|及|、))?待结算赎回)(?=$|[。；;！!\n])')
    def project(match):
        return match['boundary'] + re.sub(r'待结算(?=赎回)|已结算(?=余额)', '   ', match['body'])
    return balance.sub(project, directory.sub(project, text))


def _compound_no_operation_projection(text, tools, final, *, location):
    """Project only current-turn NoOps predicates proved by host-owned state.

    Parse comma clauses rather than matching a model-facing example sentence.
    A draft denial can inherit the explicit turn only from the immediately
    preceding financial NoOps clause in this same sentence. Other clauses stay
    visible to the original financial receipt guard.
    """
    current_no_execution = re.compile(
        r'(?:本轮|本次对话|本次)(?:中)?\s*(?:没有|未|尚未)(?:实际)?执行'
        r'(?:任何(?:金融|资金)?(?:操作|交易)|(?:金融|资金)(?:操作|交易))')
    no_draft = re.compile(r'(?:也|并)?(?:没有|未|尚未)(?:准备|创建|生成)(?:任何)?(?:操作)?草案')
    spans = []
    for sentence in re.finditer(r'[^。；;！!\n]+', text):
        offset, previous_no_execution = sentence.start(), None
        for item in re.finditer(r'[^，,]+', sentence.group(0)):
            raw = item.group(0); clause = raw.strip()
            current = bool(current_no_execution.fullmatch(clause))
            draft = bool(previous_no_execution and no_draft.fullmatch(clause))
            left = offset + item.start() + len(raw) - len(raw.lstrip())
            span = (left, left + len(clause), clause)
            if draft:
                spans.extend((previous_no_execution, span))
            previous_no_execution = span if current else None
    if not spans:
        return text
    trace = getattr(tools, 'trace', None)
    evidence = getattr(tools, 'evidence', None)
    forbidden = _TOOLS | {'confirm_operation', 'execute_operation', 'execute_payment',
                          'confirm_payment', 'execute_transfer'}
    host_empty = (getattr(tools, 'changed', None) is False
        and type(getattr(tools, 'drafts', None)) in (dict, set, list, tuple)
        and not tools.drafts and not final.get('draft_ids')
        and type(trace) is list and type(evidence) is dict
        and all(isinstance(row, dict) and not (row.get('status') == 'completed'
            and row.get('tool') in forbidden) for row in trace)
        and all(isinstance(row, dict) and row.get('tool') not in forbidden
            for row in evidence.values()))
    if not host_empty:
        _deny('OPERATION_STATUS_EVIDENCE_REQUIRED', location=location,
              locations=(location,), assertion_details=((location, spans[0][2], ('unexecuted',)),))
    projected = list(text)
    for start, end, _ in spans:
        projected[start:end] = ' ' * (end - start)
    return ''.join(projected)


def _conditional_code_reuse_projection(text):
    """A closed conditional non-reuse policy is not a completed-code report.

    Preserve source/identity/deadline conditions in this same sentence. Only
    the result noun in the negative consequent is projected; standalone claims,
    financial actions, extra tails and cross-sentence continuations stay intact.
    """
    factors = r'(?:来源|身份|期限)'
    pattern = re.compile(
        r'(?P<head>(?:^|[。；;！!\n])(?:- |[1-9][0-9]?\. )?'
        r'(?:若|如果)?' + factors + r'(?:[、]|或|和|与)' + factors
        + r'(?:(?:[、]|或|和|与)' + factors + r'){0,1}'
        r'(?:发生)?变化(?:时)?[，,]?(?:应|需|必须)停止(?:复现)?[，,](?:不|不能|不得)复用本次)'
        r'(?P<result>执行结果)(?=$|[。；;！!\n])')
    return pattern.sub(lambda match: match['head'] + ' ' * len(match['result']), text)


def _code_execution_clause(clause):
    """Classify one bounded code-result clause; this never supplies evidence.

    Code/result subjects and a reproduction predicate must share this clause.
    A financial predicate, promise or operation identifier cannot borrow that
    subject, even when the clause also reports a successful code result.
    """
    body = re.sub(r'^(?:[1-9][0-9]?\.|[-*])\s+', '', clause).strip()
    if (not body or len(body) > 240
            or any(verb in body for verb in _VERBS)
            or re.search(r'银行|资金|款项|交易|操作|到账|结算|保证|确保|承诺|授权|权限'
                         r'|money|funds|bank|(?:op_id|operation_id|transaction_id)\s*[:：#]?', body, re.I)):
        return False
    result = re.search(r'(?:隔离)?执行结果与(?:既有)?指标一致$', body)
    if result:
        # A result noun is explicit here; no earlier sentence/list item is
        # needed or allowed to lend this clause its subject.
        return bool(_CODE_SUBJECT.search(body) or re.search(r'(?:隔离)?执行(?:结果|回执)', body))
    return bool(_CODE_SUBJECT.search(body)
                and re.search(r'复现(?:了)?[^，,。；;！!\n]{0,80}指标$', body))


def _assertions(text, exact_quotes=(), *, allow_denied_arrival_projection=True,
                code_result_contexts=None):
    normalized = _normalize(text)
    for quote in exact_quotes:
        for left, right in (('“', '”'), ('"', '"'), ('「', '」')):
            normalized = normalized.replace(left + _normalize(quote) + right, ' ')
    normalized = _catalog_pending_noun_projection(normalized)
    normalized = _conditional_code_reuse_projection(normalized)
    allow_capability_arrival_projection = _capability_context_is_closed(normalized)
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
        code_result = bool(_code_execution_clause(clause) or (
            code_scope and _CODE_RESULT_CONTINUATION.fullmatch(clause)))
        code_scope = bool(code_scope and (code_result or (
            _CODE_METRIC_CONTINUATION.fullmatch(clause)
            and not any(verb in clause for verb in _VERBS)
            and not re.search(r'银行|资金|款项|交易|操作|money|funds|bank', clause))))
        code_scope = code_scope or _code_execution_clause(clause)
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
        # Only a complete comparison in the untouched clause scopes its result.
        # Other predicates and clauses retain their original receipt checks.
        comparison = _non_equivalent_basis_projection(clause,
            sentence=_redemption_sentence_context(parts, index))
        status_clause = clause if comparison is None else comparison
        masked = _PUBLIC_REFERENCE_STATUS.sub(' ',
            _MISSING_FEE_ENTRY.sub(' ', _NEGATED_ASSERTION.sub(' ', _STATE_INTERROGATIVE_COMPLEMENT.sub(' ', _EVIDENCE_NONASSERTION.sub(' ', status_clause)))))
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
            fallback = _non_status_fallback_projection(masked,
                allow_capability_arrival_projection=allow_capability_arrival_projection,
                redemption_sentence=_redemption_sentence_context(parts, index))
            state_comparison = _non_equivalent_basis_projection(state_clause,
                sentence=_redemption_sentence_context(state_parts, index))
            status_state_clause = state_clause if state_comparison is None else state_comparison
            state_masked = _PUBLIC_REFERENCE_STATUS.sub(' ',
                _MISSING_FEE_ENTRY.sub(' ', _NEGATED_ASSERTION.sub(' ', _STATE_INTERROGATIVE_COMPLEMENT.sub(' ', _EVIDENCE_NONASSERTION.sub(' ', status_state_clause)))))
            state_fallback = _non_status_fallback_projection(state_masked,
                allow_capability_arrival_projection=allow_capability_arrival_projection,
                redemption_sentence=_redemption_sentence_context(state_parts, index))
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


def _deny(code, operations=(), *, location=None, locations=(), assertion_details=()):
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
    error = OperationWordingError(code, '操作状态必须与同一原操作真实回执一致。' + ' '.join(templates) + feedback, 422, location=location, locations=locations, assertion_details=assertion_details)
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
                   and (_CODE_RESULT_CONTINUATION.fullmatch(clause)
                        or _code_execution_clause(clause)))
    reproduction = (_CODE_REPRODUCTION_PROOF.fullmatch(clause)
                    if kinds == {'unsupported'} else None)
    # A bounded ASCII list marker belongs only to the closed proof sentence.
    # Keep the original clause for all financial and receipt checks below.
    if reproduction is None and kinds == {'unsupported'}:
        numbered = re.fullmatch(r'(?:[1-9][0-9]?\.|[-*]) (?P<proof>.+)', clause)
        if numbered:
            reproduction = _CODE_REPRODUCTION_PROOF.fullmatch(numbered['proof'])
    if reproduction and reproduction['metric_label'] and re.search(
            _ACTION + '|' + _EN_ACTION + r'|银行|资金|款项|money|funds|bank',
            reproduction['metric_label'], re.I):
        reproduction = None
    if not _CODE_SUBJECT.search(clause) and not code_result and not reproduction:
        return False
    if any(verb in clause for verb in _VERBS) or re.search(r'银行|资金|款项|money|funds|bank', clause):
        return False
    if code_result or reproduction:
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


def _catalog_properties_clause(clause, kinds, tools):
    """Classify only complete, evidence-bound catalog timing properties.

    The unsupported fallback has already retained all explicit status claims.
    A name/class alias must resolve to one current products row; every stated
    integer property is compared with that same row. This issues no operation
    receipt, guarantee, suitability decision or trading authority.
    """
    if kinds != {'unsupported'} or len(clause) > 320:
        return False
    entry = _closed_nominal_clause(clause)
    if entry is None:
        return False
    value, _ = entry
    relation = re.fullmatch(
        r'(?:本轮|当前)?(?:模拟)?(?:产品|基金)?目录(?:中的|里|中)'
        r'(?P<label>[a-z0-9_\u3400-\u9fff]{1,80}(?:产品|基金))'
        r'(?P<properties>[^，,。；;！!\n]{1,180})', value)
    if relation is None or _REFERENCE_PRIVATE_SUBJECT.search(relation['label']):
        return False
    members = re.split(r'、|和|与|及', relation['properties'])
    if not 1 <= len(members) <= 2:
        return False
    expected = {}
    for member in members:
        prop = re.fullmatch(
            r'(?P<field>锁定(?:期限|期)?|赎回到账(?:目录)?(?:时延|天数|延迟天数))'
            r'\s*(?:为|是)?\s*(?P<days>0|[1-9][0-9]{0,3})\s*天', member)
        if prop is None:
            return False
        field = 'term_days' if prop['field'].startswith('锁定') else 'redemption_delay_days'
        if field in expected:
            return False
        expected[field] = int(prop['days'])
    evidence, trace = getattr(tools, 'evidence', None), getattr(tools, 'trace', None)
    context = getattr(tools, 'start_context', None)
    if type(evidence) is not dict or type(trace) is not list or not isinstance(context, str) or not context:
        return False
    matches = {}
    for evidence_id, item in evidence.items():
        if (not isinstance(item, dict) or item.get('id') != evidence_id
                or item.get('tool') != 'products' or item.get('source') != 'simulation'
                or item.get('context_hash') != context or type(item.get('data')) is not list
                or not any(isinstance(row, dict) and row.get('tool') == 'products'
                    and row.get('evidence_id') == evidence_id and row.get('status') == 'completed'
                    for row in trace)):
            continue
        for product in item['data']:
            if (not isinstance(product, dict) or product.get('source') != 'simulation'
                    or not isinstance(product.get('name'), str)):
                continue
            name = _normalize(product['name'])
            plain_name = name[2:] if name.startswith('模拟') else name
            aliases = {name, plain_name}
            if plain_name.endswith(('产品', '基金')):
                aliases.add(plain_name[:-2] + '类' + plain_name[-2:])
            if relation['label'] not in aliases:
                continue
            identity = product.get('id')
            if not isinstance(identity, str) or not identity or identity in matches and matches[identity] != product:
                return False
            matches[identity] = product
    if len(matches) != 1:
        return False
    product = next(iter(matches.values()))
    return all(type(product.get(field)) is int and product[field] == days
               for field, days in expected.items())


def _product_terms_clause(clause, kinds, tools):
    """A finite product-terms noun needs an actual current products result.

    This only classifies an unsupported fallback clause. It supplies no
    operation receipt, arrival guarantee, model-selected evidence or amount.
    Explicit state patterns and every other clause keep their original guard.
    """
    if _catalog_properties_clause(clause, kinds, tools):
        return True
    if kinds != {'unsupported'} or re.fullmatch(
            r'(?:可(?:以)?考虑的)?(?:只有|仅有)?活钱(?:类|产品)'
            r'\(无锁定、赎回当日到账\)', clause) is None:
        return False
    evidence = getattr(tools, 'evidence', None)
    trace = getattr(tools, 'trace', None)
    context = getattr(tools, 'start_context', None)
    if type(evidence) is not dict or type(trace) is not list or not isinstance(context, str) or not context:
        return False
    products = {}
    for evidence_id, item in evidence.items():
        if (not isinstance(item, dict) or item.get('id') != evidence_id
                or item.get('tool') != 'products' or item.get('source') != 'simulation'
                or item.get('context_hash') != context or type(item.get('data')) is not list
                or not any(isinstance(row, dict) and row.get('tool') == 'products'
                    and row.get('evidence_id') == evidence_id and row.get('status') == 'completed'
                    for row in trace)):
            continue
        for product in item['data']:
            if not isinstance(product, dict) or not isinstance(product.get('name'), str) or '活钱' not in product['name']:
                continue
            identity = product.get('id')
            if not isinstance(identity, str) or not identity or (identity in products and products[identity] != product):
                return False
            products[identity] = product
    if len(products) != 1:
        return False
    product = next(iter(products.values()))
    return (product.get('source') == 'simulation'
        and type(product.get('term_days')) is int and product['term_days'] == 0
        and type(product.get('redemption_delay_days')) is int and product['redemption_delay_days'] == 0
        and product.get('early_redemption') is True and product.get('profile_eligible') is True
        and product.get('exclusion_reasons') == [])


def _current_budget_supports(tools, required_basis):
    """A completed current-budget original proves its nominal scope invariant.

    Planning reserves change the amount, not the settled-cash definition.
    This supplies no reservation, payment or individual redemption receipt;
    the original amount/source/review guards still validate every amount.
    """
    evidence, trace = getattr(tools, 'evidence', None), getattr(tools, 'trace', None)
    context = getattr(tools, 'start_context', None)
    if (type(evidence) is not dict or type(trace) is not list
            or not isinstance(context, str) or not context
            or getattr(tools, 'changed', None) is not False or getattr(tools, 'drafts', None)):
        return False
    for evidence_id, item in evidence.items():
        if (not isinstance(item, dict) or item.get('id') != evidence_id
                or item.get('tool') != 'budget' or item.get('source') != 'simulation'
                or item.get('context_hash') != context
                or not any(isinstance(row, dict) and row.get('tool') == 'budget'
                    and row.get('evidence_id') == evidence_id and row.get('status') == 'completed'
                    for row in trace)):
            continue
        data, parameters = item.get('data'), item.get('parameters')
        if not isinstance(data, dict) or not isinstance(parameters, dict):
            continue
        account = data.get('account_id')
        if (not isinstance(account, str) or not account or parameters.get('account_id') != account
                or not isinstance(data.get('as_of'), str)
                or data.get('budget_scope') != 'current_budget'
                or parameters.get('reserves', []) != [] or data.get('planning_reserves') != []
                or not isinstance(data.get('amount_basis'), str)
                or required_basis not in _normalize(data['amount_basis'])):
            continue
        return True
    return False


def _holding_attribute_query_clause(clause, kinds, tools):
    """A bound holding-property speech act asserts no operation outcome.

    Request, interrogative and offer heads all own one closed property object.
    Only the unsupported fallback may use this relation; original explicit
    states, extra clauses and events retain their operation-receipt contract.
    """
    if kinds != {'unsupported'} or len(clause) > 640:
        return False
    modifier = r'(?:先|进一步|逐一|逐项)?'
    verb = r'(?:查看|看看|核实|核验|核对|研究|了解)'
    speech = (r'(?:(?:请)?(?:帮我)?' + modifier + verb
              + r'|(?:是否|要不要|可否|能否|可不可以|能不能)' + modifier + verb
              + r'|(?:我|我们|本助手)(?:可以|可)(?:帮你|为你)?' + modifier + verb + r')')
    subject = r'(?:(?:我|本人)(?:的)?|(?:当前|现有)(?:的)?)?持仓的'
    query = re.fullmatch(speech + subject + r'(?P<attributes>[^，,。；;!?\n]{1,480})[?]?', clause)
    if query is None:
        return False
    # Reuse the closed, bounded attribute-object grammar. A category or event
    # cannot acquire an attribute head from text outside its own object.
    attributes = query['attributes']
    if (_research_attribute_projection('列出' + attributes) is None
            or not re.search(r'到账|结算', attributes)):
        return False
    evidence, trace = getattr(tools, 'evidence', None), getattr(tools, 'trace', None)
    context, workspace = getattr(tools, 'start_context', None), getattr(tools, 'w', None)
    if (type(evidence) is not dict or type(trace) is not list
            or not isinstance(context, str) or not context
            or not isinstance(workspace, str) or not workspace
            or getattr(tools, 'changed', None) is not False or getattr(tools, 'drafts', None)):
        return False
    for evidence_id, item in evidence.items():
        if (not isinstance(item, dict) or item.get('id') != evidence_id
                or item.get('tool') != 'account_summary' or item.get('source') != 'simulation'
                or item.get('context_hash') != context or item.get('parameters') != {}
                or item.get('workspace_id', workspace) != workspace
                or not any(isinstance(row, dict) and row.get('tool') == 'account_summary'
                    and row.get('evidence_id') == evidence_id and row.get('status') == 'completed' for row in trace)):
            continue
        data = item.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('as_of'), str):
            continue
        accounts, holdings = data.get('accounts'), data.get('holdings')
        if type(accounts) is not list or type(holdings) is not list or not holdings:
            continue
        account_ids = [row.get('id') for row in accounts if isinstance(row, dict)]
        if (len(account_ids) != len(accounts) or any(not isinstance(v, str) or not v for v in account_ids)
                or len(set(account_ids)) != len(account_ids)):
            continue
        if all(isinstance(row, dict) and row.get('account_id') in account_ids
               and isinstance(row.get('product_id'), str) and row['product_id']
               and type(row.get('amount_minor')) is int and row['amount_minor'] >= 0 for row in holdings):
            return True
    return False


def _budget_exclusion_clause(clause, kinds, tools):
    """Compose a budget asset NP with a negative inclusion relation.

    State words occur only in a category noun such as unsettled redemption,
    never an individual order. A comma can split a parenthetical budget note,
    so one outer parenthesis may remain at either edge; no content is erased.
    Private subjects, positive statuses and extra predicates cannot full-match.
    """
    if kinds not in ({'unsupported'}, {'pending'}):
        return False
    value = clause.strip()
    if value.startswith('('):
        value = value[1:].lstrip()
    if value.endswith(')'):
        value = value[:-1].rstrip()
    asset = (r'(?:持仓(?:账面)?本金|(?:未到账|待到账|未结算|待结算)'
             r'(?:的)?赎回(?:款(?:项)?)?)')
    assets = asset + r'(?:(?:和|及|与|以及|或|、)' + asset + r'){0,3}'
    scope = (r'(?:当前|本轮)?(?:cash|(?:已结算)?现金(?:预算)?'
             r'|预算|可安排资金)(?:口径)?')
    negative_add = r'(?:未|没有|不)(?:被)?(?:计入|纳入|算入|加进|算进来)'
    subject_first = (assets + r'(?:均|都)?(?:' + negative_add
                     + r'(?:' + scope + r')?|(?:不|未|没有)(?:被)?包含在'
                     + scope + r'(?:中|内)?)')
    relation_first = (r'(?:' + scope + r'(?:均|都)?)?'
                      r'(?:不含|不包括|不包含|未包含|没有包含|'
                      + negative_add + r')' + assets)
    if re.fullmatch(r'(?:' + subject_first + '|' + relation_first + ')', value) is None:
        return False
    return _current_budget_supports(tools,
        '持仓本金及未到账赎回款未加进cash或可安排资金')


def _budget_formula_clause(clause, kinds, tools):
    """A finite current-budget formula is not an observed settlement status.

    The explanation must name the cash base and every original deduction.
    Unknown horizons, extra predicates and individual receipts keep the
    original status guard; the formula creates no amount or operation proof.
    """
    if kinds != {'unsupported'} or re.fullmatch(
            r'(?:口径|(?:当前|本轮)?预算口径|可安排资金口径)(?:为|是)'
            r'(?:所选)?账户(?:的)?已结算现金(?:余额)?(?:扣除|减去)'
            r'应急(?:底线|保留)、生效(?:资金)?预留(?:和|及|与)'
            r'未来30天已知到期款', clause) is None:
        return False
    return _current_budget_supports(tools,
        '当前预算的cash只取所选账户已结算现金,可安排资金再扣应急底线、生效预留及未来30天已知到期款')



def _paired_current_budget_formula_clause(clause, kinds, final, location, tools):
    """Bind a two-amount anaphor to its immediately preceding budget sentence.

    No amount equality guess or lone budget receipt supplies this relation.
    Both exact occurrences must carry full signed current-budget references,
    with distinct subjects and their own same-turn completed budget evidence.
    """
    if kinds != {'unsupported'} or location != 'text' or len(clause) > 640:
        return False
    # The demonstrative is a nominal role, not one fixed output sentence.
    # Its referent is proved below by the adjacent signed amount occurrences.
    group = r'(?:这|上述|以上|前述)(?:些|(?:两|二)(?:笔|项|个))?(?:数字|金额|结果|资金)?'
    # Copula, aspect and result head are optional grammatical roles of the
    # same subtraction relation. No event verb or open tail fits these slots.
    relation = re.fullmatch(group + r'(?:都|均)?(?:是|为)?(?:已经|已)?(?:扣掉|扣除|减去)'
                            r'(?P<deductions>[^，,。；;!?\n]{1,240}?)(?:后(?:的)?(?:结果|金额|资金))?', clause)
    roles = {'应急底线': 'floor', '应急保留': 'floor',
             '生效预留': 'holds', '生效资金预留': 'holds',
             '未来30天已知到期款': 'obligations'}
    deductions = re.split(r'以及|和|及|与|、', relation['deductions']) if relation else []
    if (len(deductions) != 3 or any(item not in roles for item in deductions)
            or {roles[item] for item in deductions} != {'floor', 'holds', 'obligations'}):
        return False
    text, claims = final.get('text'), final.get('amount_claims')
    evidence, trace = getattr(tools, 'evidence', None), getattr(tools, 'trace', None)
    context, registry = getattr(tools, 'start_context', None), getattr(tools, 'metrics', None)
    workspace = getattr(tools, 'w', None)
    if (not isinstance(text, str) or type(claims) is not list
            or type(evidence) is not dict or type(trace) is not list
            or not isinstance(context, str) or not context
            or not isinstance(workspace, str) or not workspace
            or getattr(tools, 'changed', None) is not False
            or getattr(tools, 'drafts', None) or final.get('draft_ids')):
        return False
    from .metric_claims import MetricRegistry, MetricClaimError, REF_FIELDS, monetary_occurrences
    from datetime import date
    if type(registry) is not MetricRegistry:
        return False
    # A repeated identical clause cannot borrow another occurrence's context.
    positions = [m for m in re.finditer(r'[^，,。；;！!？?\n]+', text)
                 if _normalize(m.group().strip()) == clause]
    if len(positions) != 1:
        return False
    start = positions[0].start() + len(positions[0].group()) - len(positions[0].group().lstrip())
    previous = re.search(r'(?P<body>[^。；;！!？?\n]+)[。；;！!？?]\s*$', text[:start])
    if previous is None:
        return False
    sentence, offset = previous['body'], previous.start('body')
    try:
        amounts = monetary_occurrences(sentence)
    except MetricClaimError:
        return False
    if len(amounts) != 2:
        return False
    first, second = amounts
    # The two exact amounts are account-budget predicates, not transaction
    # receipts or unrelated numbers elsewhere in the preceding sentence.
    account = r'(?P<account>[a-z0-9_\u3400-\u9fff\-]{1,64}账户)'
    head, middle, tail = (sentence[:first['start']].strip(),
        sentence[first['end']:second['start']].strip(), sentence[second['end']:].strip())
    # Closed account-budget predicates; the second may share its first role.
    topic = r'(?:(?:先|再)?(?:说|看)(?:钱|资金(?:面)?)[:：])?'
    left_role = re.fullmatch(topic + account + r'(?:当前)?可安排资金(?:为|是)', _normalize(head))
    right_role = re.fullmatch(r',\s*' + account + r'(?:当前)?(?:可安排资金)?(?:为|是)', _normalize(middle))
    if (left_role is None or right_role is None or tail
            or re.search(_ACTION + r'|到账|结算|成功|失败|交易|保证|确保|承诺|已经|已', head + middle)):
        return False
    labels = (left_role['account'], right_role['account'])
    checked = []
    basis = 'floor_and_holds_and_30_day_obligations_deducted'
    scope = '当前预算的cash只取所选账户已结算现金,可安排资金再扣应急底线、生效预留及未来30天已知到期款'
    for occurrence in amounts:
        absolute = offset + occurrence['start']
        literal = sentence[occurrence['start']:occurrence['end']]
        matches = [v for v in claims if isinstance(v, dict)
                   and type(v.get('occurrence_start')) is int and v['occurrence_start'] == absolute
                   and v.get('text') == literal]
        if len(matches) != 1:
            return False
        claim = matches[0]
        try:
            record = registry.lookup(claim.get('metric_id'));metric = record['metric']
            if (record['kind'] != 'recorded_fact' or claim.get('claim_kind') != record['kind']
                    or any(type(claim.get(k)) is not type(metric[k]) or claim.get(k) != metric[k]
                           for k in REF_FIELDS)
                    or metric['metric_name'] != 'arrangeable_now'
                    or metric['formula_id'] != 'tools.budget_current/1' or metric['basis'] != basis
                    or metric['data_scope'] != 'simulation_bank_ledger'
                    or metric['calculation_status'] != 'calculated' or metric['unit'] != 'minor_currency'
                    or type(metric['value_minor']) is not int or metric['value_minor'] < 0
                    or metric['assumed_inputs'] or metric['coverage_gaps'] or metric['as_of'] is not None
                    or occurrence['value_minor'] != metric['value_minor']
                    or occurrence['currency'] != metric['currency']
                    or 'accounts/' + metric['subject_id'] not in metric['input_refs']
                    or 'profiles/' + workspace not in metric['input_refs']
                    or (date.fromisoformat(metric['period_end']) - date.fromisoformat(metric['period_start'])).days != 30):
                return False
        except (KeyError, TypeError, ValueError, MetricClaimError):
            return False
        support = set()
        for evidence_id in record['evidence_ids']:
            item = evidence.get(evidence_id)
            if (not isinstance(item, dict) or item.get('id') != evidence_id
                    or item.get('tool') != 'budget' or item.get('source') != 'simulation'
                    or item.get('context_hash') != context
                    or not any(isinstance(v, dict) and v.get('tool') == 'budget'
                        and v.get('evidence_id') == evidence_id and v.get('status') == 'completed' for v in trace)):
                continue
            data, parameters = item.get('data'), item.get('parameters')
            if (not isinstance(data, dict) or not isinstance(parameters, dict)
                    or parameters.get('account_id') != metric['subject_id']
                    or data.get('account_id') != metric['subject_id']
                    or data.get('as_of') != metric['period_start'] or data.get('currency') != metric['currency']
                    or data.get('budget_scope') != 'current_budget'
                    or parameters.get('reserves', []) != [] or data.get('planning_reserves') != []
                    or ('floor_minor' in parameters and (type(parameters['floor_minor']) is not int or parameters['floor_minor'] != 0))
                    or type(data.get('investable_minor')) is not int or data['investable_minor'] != metric['value_minor']
                    or not isinstance(data.get('amount_basis'), str) or scope not in _normalize(data['amount_basis'])):
                continue
            support.add(evidence_id)
        if not support:
            return False
        checked.append((metric, support))
    left, right = checked
    if (left[0]['metric_id'] == right[0]['metric_id'] or left[0]['subject_id'] == right[0]['subject_id']
            or left[1] & right[1] or any(left[0][k] != right[0][k]
                for k in ('as_of', 'period_start', 'period_end', 'currency', 'basis'))):
        return False
    # The public account labels must identify these exact typed subjects in
    # the same completed account-summary original, never another equal amount.
    labelled = False
    for evidence_id, item in evidence.items():
        if (not isinstance(item, dict) or item.get('id') != evidence_id
                or item.get('tool') != 'account_summary' or item.get('source') != 'simulation'
                or item.get('context_hash') != context or item.get('parameters') != {}
                or not any(isinstance(row, dict) and row.get('tool') == 'account_summary'
                    and row.get('evidence_id') == evidence_id and row.get('status') == 'completed' for row in trace)):
            continue
        data = item.get('data')
        rows = data.get('accounts') if isinstance(data, dict) else None
        if type(rows) is not list or data.get('as_of') != left[0]['period_start']:
            continue
        matches = []
        for (metric, _), label in zip(checked, labels):
            accounts = [row for row in rows if isinstance(row, dict) and row.get('id') == metric['subject_id']]
            if len(accounts) != 1 or not isinstance(accounts[0].get('name'), str):
                break
            name = _normalize(accounts[0]['name'])
            if label not in (name, name.removeprefix('模拟')):
                break
            matches.append(metric['subject_id'])
        if len(matches) == 2 and len(set(matches)) == 2:
            labelled = True
            break
    if not labelled:
        return False
    return all(left[0]['source_versions'][k] == right[0]['source_versions'][k]
               for k in set(left[0]['source_versions']) & set(right[0]['source_versions']))


def _pending_redemption_aggregate_clause(clause, kinds, final, location, tools):
    """A bound account aggregate amount is not an individual order status.

    Keep the original pending-state patterns. Only a complete aggregate noun
    clause in the actual rendered text can use its existing amount binding;
    every receipt predicate and appended operation statement stays in scope.
    """
    if kinds != {'pending'} or location != 'text' or re.fullmatch(
            r'待结算赎回(?:总额|金额)?(?:为|是)?\s*[-+0-9.,]+\s*'
            r'(?:人民币|美元|欧元|英镑|日元|元|圆|cny|rmb|usd|eur|gbp|jpy)', clause) is None:
        return False
    text, claims = final.get('text'), final.get('amount_claims')
    evidence, trace = getattr(tools, 'evidence', None), getattr(tools, 'trace', None)
    context, registry = getattr(tools, 'start_context', None), getattr(tools, 'metrics', None)
    if (not isinstance(text, str) or type(claims) is not list
            or type(evidence) is not dict or type(trace) is not list
            or not isinstance(context, str) or not context):
        return False
    from .metric_claims import MetricRegistry, MetricClaimError, REF_FIELDS, monetary_occurrences
    if type(registry) is not MetricRegistry:
        return False
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        start, amount = claim.get('occurrence_start'), claim.get('text')
        if (type(start) is not int or start < 0 or not isinstance(amount, str)
                or text[start:start + len(amount)] != amount):
            continue
        head = re.search(r'待结算赎回(?:总额|金额)?(?:为|是)?\s*$', text[:start])
        if head is None or _normalize(text[head.start():start + len(amount)]) != clause:
            continue
        try:
            record = registry.lookup(claim.get('metric_id'))
            metric = record['metric']
            if (record['kind'] != 'recorded_fact' or claim.get('claim_kind') != record['kind']
                    or any(type(claim.get(key)) is not type(metric[key])
                           or claim.get(key) != metric[key] for key in REF_FIELDS)
                    or metric['metric_name'] != 'pending_redemption'
                    or metric['subject_id'] != getattr(tools, 'w', None)
                    or metric['formula_id'] != 'tools.account_principal/1'
                    or metric['basis'] != 'pending_redemption_not_settled_cash'
                    or metric['data_scope'] != 'simulation_bank_ledger'
                    or metric['calculation_status'] != 'calculated'
                    or type(metric['value_minor']) is not int or metric['value_minor'] < 0
                    or metric['assumed_inputs']):
                continue
            occurrences = monetary_occurrences(amount)
            if (len(occurrences) != 1 or occurrences[0]['start'] != 0
                    or occurrences[0]['end'] != len(amount)
                    or occurrences[0]['value_minor'] != metric['value_minor']
                    or occurrences[0]['currency'] != metric['currency']):
                continue
        except (KeyError, TypeError, ValueError, MetricClaimError):
            continue
        for evidence_id in record['evidence_ids']:
            item = evidence.get(evidence_id)
            if (not isinstance(item, dict) or item.get('id') != evidence_id
                    or item.get('tool') != 'account_summary' or item.get('source') != 'simulation'
                    or item.get('context_hash') != context or item.get('parameters') != {}
                    or not any(isinstance(row, dict) and row.get('tool') == 'account_summary'
                        and row.get('evidence_id') == evidence_id and row.get('status') == 'completed'
                        for row in trace)):
                continue
            data = item.get('data')
            rows = data.get('principal_summary') if isinstance(data, dict) else None
            if type(rows) is not list or data.get('as_of') != metric['as_of']:
                continue
            matching = [row for row in rows if isinstance(row, dict)
                        and row.get('currency') == metric['currency']]
            if (len(matching) == 1 and type(matching[0].get('pending_redemption_minor')) is int
                    and matching[0]['pending_redemption_minor'] == metric['value_minor']):
                return True
    return False


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
    from .read_completion import validate_holding_horizon_wording
    validate_holding_horizon_wording(final, tools)
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
        value = _compound_no_operation_projection(value, tools, final, location=location)
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
                if not (_code_clause(assertion[0], assertion[1], tools, bank_db,
                                     code_result_context=code_result_context)
                        or _product_terms_clause(assertion[0], assertion[1], tools)
                        or _holding_attribute_query_clause(assertion[0], assertion[1], tools)
                        or _budget_exclusion_clause(assertion[0], assertion[1], tools)
                        or _budget_formula_clause(assertion[0], assertion[1], tools)
                        or _paired_current_budget_formula_clause(
                            assertion[0], assertion[1], final, location, tools)
                        or _pending_redemption_aggregate_clause(
                            assertion[0], assertion[1], final, location, tools))]
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
              locations=tuple(dict.fromkeys(assertion_locations)),
              assertion_details=tuple((location, clause, tuple(sorted(kinds)))
                  for (clause, kinds), location in zip(assertions, assertion_locations)))
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
        if (re.search(r'(?:资金|钱|款项)已到账|已经到账', clause)
                or _ELLIPTICAL_ARRIVAL.fullmatch(clause)) and (
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
