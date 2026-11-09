"""消费正式金融指标，不计算金额、不把旧工具标量升级为指标。

只有本地服务可 register；kind 来自正式服务或工具分支固定映射，不能来自
模型参数。模型提交 amount_claim 后，调用 validate_answer；结果携带完整
指标快照给独立 reviewer。结构一致不证明自然语言里的资格、充分性或推荐
结论正确。公开引文金额由另外的原文 provenance 合同审核。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import json
import re
import unicodedata

from financial_methods.contracts import CONTRACT_VERSION, FinancialMetric, STATUSES


KINDS = frozenset({"recorded_fact", "recorded_projection", "candidate_scenario",
                   "planning_scenario", "forecast", "hypothesis"})
METRIC_FIELDS = frozenset({"contract_version", "metric_id", "subject_id", "metric_name",
    "currency", "unit", "value_minor", "as_of", "period_start", "period_end", "data_scope",
    "calculation_status", "basis", "formula_id", "input_refs", "source_versions",
    "excluded_items", "coverage_gaps", "assumed_inputs"})
REF_FIELDS = ("metric_id", "subject_id", "metric_name", "currency", "unit", "value_minor",
              "as_of", "period_start", "period_end", "data_scope", "calculation_status",
              "basis", "formula_id", "source_versions")
# 已存在纯方法的固定语义，不用名称关键词推断用户资料。
_FIXED_KINDS = {
    "funds.planning_budget/1": "planning_scenario",
    "funds.daily_minimum/1": "recorded_projection",
    "funds.daily_minimum_candidates/1": "candidate_scenario",
    "funds.closing_cash/1": "recorded_projection",
    "funds.closing_cash_candidates/1": "candidate_scenario",
}
# Closed common-amount grammar for the existing cashflow minimum pair. This
# checks a bounded declared relationship, not arbitrary natural-language scope.
_JOINT_SCENARIO_PREFIX = re.compile(
    r'(?:两种|已确认(?:事项)?(?:与|和|及)候选(?:事项)?)情景(?:下|中)?'
    r'(?:最低底线外余量)?(?:均|都)(?:为|是)\s*\Z')
_SCENARIO_MINIMUM_ROLES = {
    ('minimum_available_after_confirmed', 'confirmed_floor_surplus',
     'tools.cashflow_confirmed/1', 'recorded_projection'): 'confirmed',
    ('minimum_available_if_candidates_occur', 'candidate_floor_surplus',
     'tools.cashflow_candidates/1', 'candidate_scenario'): 'candidate',
}


def _require_joint_scenario_scope(text, occurrences, bound_amounts,
                                 public_quote_spans, scenario_metric_ids, scenario_context):
    """Only signatures bound at this occurrence can authorize its joint scope.

    Equal raw data, a registry entry, or another occurrence supplies no branch.
    Existing funding-assessment choices may additionally pin the request period.
    Free prose outside this closed grammar still needs semantic review.
    """
    allowed = None if scenario_metric_ids is None else frozenset(scenario_metric_ids)
    for literal in occurrences:
        start, end = literal['start'], literal['end']
        if any(left <= start and end <= right for left, right in public_quote_spans):
            continue
        prefix = unicodedata.normalize('NFKC', text[:start])
        if _JOINT_SCENARIO_PREFIX.search(prefix) is None:
            continue
        groups = {}
        for row in bound_amounts.get((start, end), ()):
            metric = row['metric']
            role = _SCENARIO_MINIMUM_ROLES.get((metric['metric_name'], metric['basis'],
                                               metric['formula_id'], row['kind']))
            if role is None:
                continue
            if allowed is not None and metric['metric_id'] not in allowed:
                _fail('METRIC_SCENARIO_SCOPE_REQUIRED')
            key = tuple(metric[field] for field in ('subject_id', 'currency', 'unit',
                'data_scope', 'as_of', 'period_start', 'period_end', 'value_minor'))
            groups.setdefault(key, set()).add(role)
        if not groups:
            if scenario_context:
                _fail('METRIC_SCENARIO_SCOPE_REQUIRED')
            continue
        if len(groups) != 1 or next(iter(groups.values())) != {'confirmed', 'candidate'}:
            _fail('METRIC_SCENARIO_SCOPE_REQUIRED')
_MONEY = re.compile(
    r"([-+−＋－]?\d[\d.,，]*)\s*(万亿|亿|万)?\s*"
    r"(人民币|美元|欧元|英镑|日元|元|圆|CNY|RMB|USD|EUR|GBP|JPY)", re.I)
_CURRENCIES = {"人民币": "CNY", "元": "CNY", "圆": "CNY", "RMB": "CNY",
               "美元": "USD", "欧元": "EUR", "英镑": "GBP", "日元": "JPY"}
# 中文/大写金额只识别其出现，不解释其数值或擅自补币种。
_CN_DIGITS = "零〇一二两三四五六七八九十百千万亿兆壹贰叁肆伍陆柒捌玖拾佰仟萬億"
_NUMBER_TOKEN = rf"[-+−＋－]?(?:\d+(?:[.,，]\d+)*|[{_CN_DIGITS}]+)(?:\s*(?:万亿|亿|万|萬億|億|萬))?"
_CN_MONEY = re.compile(rf"[{_CN_DIGITS}]+(?:点[{_CN_DIGITS}]+)?\s*"
                       r"(?:人民币|美元|欧元|英镑|日元|元|圆|圓|块|塊)")
_BARE_SCALE = re.compile(rf"[-+−＋－]?(?:[\d.,，]+|[{_CN_DIGITS}]+)\s*(?:万亿|亿|万|萬億|億|萬)")
_FINANCIAL_TERM = (
    r"(?:账户余额|余额|现金|金额|资金|本金|净资产|净值|资产|预算|房租|应急金|"
    r"应急保留|应急底线|留款|退款|补助|到账|收入|支出|损益|盈利|亏损|费用|"
    r"手续费|总额|余量|可安排|可用|可花|能花|可投|能投|贷款|付款|还款|收益)")
_FINANCIAL_NUMBER_LINK = (
    r"(?:\s*(?:为|是|有|达|达到|约|大约|共|合计|预计|预测|至少|最多|仅|只剩|"
    r"剩|了|增加|减少|已|已经|：|:|=))*\s*")
_FINANCIAL_NUMBER = re.compile(
    _FINANCIAL_TERM + _FINANCIAL_NUMBER_LINK + rf"(?P<number>{_NUMBER_TOKEN})")
_CONDITIONAL_FINANCIAL_TAIL = re.compile(
    _FINANCIAL_NUMBER_LINK + rf"(?P<number>{_NUMBER_TOKEN})")
_NUMBER_FINANCIAL = re.compile(
    rf"(?P<number>{_NUMBER_TOKEN})\s*(?:(?:为|是|的|你|我|本人|当前|本期|总)\s*)*" +
    _FINANCIAL_TERM)
_PREFIX_MONEY = re.compile(
    r"(?:人民币|美元|欧元|英镑|日元|CNY|RMB|USD|EUR|GBP|JPY|¥|￥|\$|€|£)\s*"
    rf"(?P<number>{_NUMBER_TOKEN})", re.I)
_NON_MONEY_UNIT = re.compile(
    r"\s*(?:天|日|周|月|年|小时|分钟|秒|笔|次|项|条|个|人|户|份|张|期|级|档|"
    r"套|种|类|组|%|％|百分点|倍|GB|MB|KB)", re.I)
_COMPARISON_WORDS = re.compile(r"一致|一样|两回事|两码事")
_DATE_TOKEN = re.compile(r"(?<![0-9A-Za-z_])\d{4}-\d{2}-\d{2}(?![0-9A-Za-z_])")


class MetricClaimError(ValueError):
    def __init__(self, code, field=None, *, money_span=None):
        self.code, self.field = code, field
        # Local diagnostic only; as_dict and all rejection decisions stay unchanged.
        self._money_span = money_span
        super().__init__(code + (":" + field if field else ""))

    @property
    def money_span(self):
        return self._money_span

    def as_dict(self):
        return {"code": self.code, **({"field": self.field} if self.field else {})}


def _fail(code, field=None, *, money_span=None):
    raise MetricClaimError(code, field, money_span=money_span)


def _json_copy(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, OverflowError):
        _fail("METRIC_INVALID_JSON")


def _string(value, field):
    if not isinstance(value, str) or not value.strip():
        _fail("METRIC_INVALID_CONTRACT", field)


def _date(value, field):
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            _fail("METRIC_INVALID_CONTRACT", field)
    except ValueError:
        _fail("METRIC_INVALID_CONTRACT", field)


def _metric_snapshot(metric):
    if isinstance(metric, FinancialMetric):
        metric = metric.to_dict()
    if not isinstance(metric, Mapping) or set(metric) != METRIC_FIELDS:
        _fail("METRIC_CONTRACT_REQUIRED")
    m = _json_copy(dict(metric))
    if m["contract_version"] != CONTRACT_VERSION:
        _fail("METRIC_CONTRACT_VERSION", "contract_version")
    for field in ("metric_id", "subject_id", "metric_name", "data_scope", "basis", "formula_id"):
        _string(m[field], field)
    if not isinstance(m["currency"], str) or not re.fullmatch(r"[A-Z]{3}", m["currency"]):
        _fail("METRIC_INVALID_CONTRACT", "currency")
    if m["unit"] != "minor_currency":
        _fail("METRIC_UNIT_REQUIRED", "unit")
    if m["as_of"] is not None:
        _date(m["as_of"], "as_of")
        if m["period_start"] is not None or m["period_end"] is not None:
            _fail("METRIC_INVALID_CONTRACT", "period")
    else:
        _date(m["period_start"], "period_start")
        _date(m["period_end"], "period_end")
        if m["period_start"] > m["period_end"]:
            _fail("METRIC_INVALID_CONTRACT", "period")
    if not isinstance(m["calculation_status"], str) or m["calculation_status"] not in STATUSES:
        _fail("METRIC_INVALID_CONTRACT", "calculation_status")
    available = m["calculation_status"] in {"calculated", "partial"}
    if available and type(m["value_minor"]) is not int:
        _fail("METRIC_UNKNOWN_NOT_ZERO", "value_minor")
    if not available and m["value_minor"] is not None:
        _fail("METRIC_UNKNOWN_NOT_ZERO", "value_minor")
    for field in ("input_refs", "excluded_items", "coverage_gaps"):
        if not isinstance(m[field], list) or any(not isinstance(v, str) or not v for v in m[field]):
            _fail("METRIC_INVALID_CONTRACT", field)
    if not m["input_refs"] or (not available and not m["coverage_gaps"]):
        _fail("METRIC_INVALID_CONTRACT", "coverage_gaps" if not available else "input_refs")
    versions = m["source_versions"]
    if not isinstance(versions, dict) or not versions or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or not v
            for k, v in versions.items()):
        _fail("METRIC_INVALID_CONTRACT", "source_versions")
    if not isinstance(m["assumed_inputs"], list) or any(not isinstance(v, dict) for v in m["assumed_inputs"]):
        _fail("METRIC_INVALID_CONTRACT", "assumed_inputs")
    return m


def monetary_occurrences(text):
    """解析标准数字+币种；中文金额/无币种金额保守拒绝，不补金额。"""
    if not isinstance(text, str):
        _fail("METRIC_INVALID_TEXT")
    result = []
    for match in _MONEY.finditer(text):
        number, scale, unit = match.groups()
        # 不把科学记数或断裂数字的后缀当作另一笔合法金额。
        if match.start() and text[match.start() - 1] in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.,，+-−":
            # NFKC turns a Chinese prose comma into ASCII. Only a separator
            # after nonnumeric prose is punctuation; numeric/sign prefixes keep
            # the original broken-number rejection.
            before_comma = text[:match.start() - 1].rstrip()
            prose_comma = (text[match.start() - 1] in {",", "，"} and bool(before_comma)
                           and before_comma[-1] not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.,，+-−＋－")
            if not prose_comma:
                _fail("INVALID_MONEY_TEXT")
        number = unicodedata.normalize("NFKC", number).replace("−", "-")
        if not re.fullmatch(r"[-+]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", number):
            _fail("INVALID_MONEY_TEXT")
        try:
            with localcontext() as context:
                context.prec = max(50, len(number) + 16)
                value = Decimal(number.replace(",", "")) * 100 * {
                    None: 1, "万": 10000, "亿": 100000000, "万亿": 1000000000000}[scale]
            if value != value.to_integral_value():
                _fail("INVALID_MONEY_TEXT")
        except InvalidOperation:
            _fail("INVALID_MONEY_TEXT")
        result.append({"start": match.start(), "end": match.end(),
                       "value_minor": int(value), "currency": _CURRENCIES.get(unit.upper(), unit.upper())})
    _reject_unsupported_money(text, result)
    return result


def _reject_unsupported_money(text, canonical):
    def covered(start, end):
        return any(row["start"] <= start and end <= row["end"] for row in canonical)
    dates = []
    for token in _DATE_TOKEN.finditer(text):
        try:
            date.fromisoformat(token.group(0))
        except ValueError:
            continue
        dates.append(token.span())

    for match in _CN_MONEY.finditer(text):
        if not covered(*match.span()):
            _fail("METRIC_MONEY_SYNTAX_REQUIRED", money_span=match.span())
    for match in _BARE_SCALE.finditer(text):
        if not covered(*match.span()) and not _NON_MONEY_UNIT.match(text, match.end()):
            _fail("METRIC_MONEY_SYNTAX_REQUIRED", money_span=match.span())
    for pattern in (_FINANCIAL_NUMBER, _NUMBER_FINANCIAL, _PREFIX_MONEY):
        for match in pattern.finditer(text):
            start, end = match.span("number")
            # “一旦” is a conditional word, not an amount. Reuse the same
            # financial link after it so a later bare amount is still rejected
            # at its actual span by the original checks below.
            if (pattern is _FINANCIAL_NUMBER and text[start:end] == "一"
                    and text.startswith("一旦", start)):
                tail = _CONDITIONAL_FINANCIAL_TAIL.match(text, end + 1)
                if tail is None:
                    continue
                start, end = tail.span("number")
            # Numeral characters inside a complete comparison word are not
            # amounts. Explicit currencies/scales still ran above; all other
            # numbers, including a later number in this clause, remain checked.
            if (pattern in (_FINANCIAL_NUMBER, _NUMBER_FINANCIAL)
                    and (any(word.start() <= start < end <= word.end()
                             for word in _COMPARISON_WORDS.finditer(text))
                         or any(left <= start < end <= right for left, right in dates))):
                continue
            # A directly negated zero-fee comparison asserts no money value.
            # Currency/scale checks above and all other financial terms remain.
            if (pattern is _FINANCIAL_NUMBER and text[start:end] in ("零", "0", "〇")
                    and re.fullmatch(r"(?:费用|手续费)\s*为\s*(?:零|0|〇)", match.group(0))
                    and re.match(r"\s*(?:$|[，,。；;!?！？]|\.(?!\d))", text[end:])):
                negation = re.search(r"(?:不(?:能|可|应|得)?\s*(?:直接\s*)?"
                                     r"(?:等于|代表|意味着|说明|理解为|视为|当作|认定为|认为)|并非|不是)"
                                     r"(?P<scope>[\u4e00-\u9fffA-Za-z \t]{0,24})\Z", text[:match.start()])
                if negation is not None and not re.search(r"不|非|未|没|无|否认|零|〇", negation.group("scope")):
                    clause = re.split(r"[，,。；;!?！？\n]", text[:negation.start()])[-1]
                    if not re.search(r"不|非|未|没|无|否认", clause):
                        continue
            # The complete relational modifiers “同一” and “单一” are
            # nonnumeric here. Currency/scale amounts such as “单一万元”
            # still pass through the earlier strict money guards first.
            if (pattern is _NUMBER_FINANCIAL and text[start:end] == "一"
                    and start > 0 and text[start - 1] in ("同", "单")):
                continue
            # A complete “一半” fraction may end here or at an explicit
            # clause boundary. Attached units (including unknown currency or
            # parenthesized units) stay in the original money rejection path.
            if (pattern is _FINANCIAL_NUMBER and text[start:end] == "一"
                    and re.match(r"\s*半\s*(?=$|[，,。；;!?！？])", text[end:])):
                continue
            # R4等正式等级不是“4元”，即便其后紧接“资金配置”。
            if start and text[start - 1] in "Rr" and text[start:end].isdigit():
                continue
            # Only a complete, standalone T+N arrival-period token is temporal.
            # Currency/scale guards above still run; later money is scanned too.
            if (pattern is _NUMBER_FINANCIAL and start > 0 and text[start - 1] == "T"
                    and re.fullmatch(r"\+(?:0|[1-9][0-9]*)", text[start:end])
                    and re.match(r"\s*到账", text[end:])):
                before_t = text[start - 2] if start > 1 else ""
                if (not before_t or before_t.isspace()
                        or ("\u4e00" <= before_t <= "\u9fff" and before_t not in _CN_DIGITS)
                        or (unicodedata.category(before_t).startswith("P")
                            and before_t not in "_+.-−＋－")):
                    continue
            if not covered(start, end) and not _NON_MONEY_UNIT.match(text, end):
                _fail("METRIC_MONEY_SYNTAX_REQUIRED", money_span=(start, end))


def merge_source_versions(metrics, *, base_versions=None):
    """合并可信已验证snapshot的输入版本，不计算或声明来源真实/最新。

    financial_methods.bind_inputs会添加逐项input_ref版本，故不能只传基座的
    顶层versions()。调用方先FinancialMetrics.validate，再使用本辅助函数。
    """
    if isinstance(metrics, Mapping):
        values = [metrics] if "metric_id" in metrics else list(metrics.values())
    elif isinstance(metrics, Sequence) and not isinstance(metrics, (str, bytes)):
        values = metrics
    else:
        _fail("METRIC_CONTRACT_REQUIRED")
    if base_versions is not None and not isinstance(base_versions, Mapping):
        _fail("METRIC_INVALID_CONTRACT", "source_versions")
    versions = dict(base_versions or {})
    for metric in values:
        for key, version in _metric_snapshot(metric)["source_versions"].items():
            if key in versions and versions[key] != version:
                _fail("METRIC_SOURCE_VERSION_CONFLICT", key)
            versions[key] = version
    return versions


def amount_claim_schema(metric_ids=()):
    """只给当前已登记且交付模型的少量ID动态枚举；空库仍由校验拒绝。"""
    string = {"type": "string", "minLength": 1, "maxLength": 256}
    nullable_date = {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"}
    props = {k: deepcopy(string) for k in REF_FIELDS}
    props.update({"text": {"type": "string", "minLength": 1, "maxLength": 1200},
                  "claim_kind": {**string, "enum": sorted(KINDS)},
                  "value_minor": {"type": ["integer", "null"]},
                  "unit": {"type": "string", "enum": ["minor_currency"]},
                  "calculation_status": {"type": "string", "enum": sorted(STATUSES)},
                  "source_versions": {"type": "object", "additionalProperties": string},
                  "as_of": deepcopy(nullable_date), "period_start": deepcopy(nullable_date),
                  "period_end": deepcopy(nullable_date),
                  "occurrence_start": {"type": "integer", "minimum": 0}})
    if metric_ids:
        props["metric_id"]["enum"] = sorted(set(metric_ids))
    return {"type": "object", "properties": props,
            "required": [*REF_FIELDS, "text", "claim_kind"], "additionalProperties": False}


class MetricRegistry:
    """单轮可信服务指标登记。不能对用户/模型暴露register工具。"""
    def __init__(self):
        self._records = {}

    @property
    def metric_ids(self):
        return tuple(self._records)

    def register(self, metrics, *, kind, evidence_id):
        """接受原FinancialMetric/完整mapping，或服务返回的命名metrics mapping。

        不扫描旧输出猜指标，也不生成metric_id。批次校验原子化；相同ID不同
        快照/种类拒绝，同快照重复查询仅追加本轮证据ID。
        """
        if not isinstance(kind, str) or kind not in KINDS:
            _fail("METRIC_KIND_REQUIRED")
        _string(evidence_id, "evidence_id")
        if isinstance(metrics, FinancialMetric) or (isinstance(metrics, Mapping) and "metric_id" in metrics):
            values = [metrics]
        elif isinstance(metrics, Mapping):
            values = list(metrics.values())
        elif isinstance(metrics, Sequence) and not isinstance(metrics, (str, bytes)):
            values = list(metrics)
        else:
            _fail("METRIC_CONTRACT_REQUIRED")
        pending = deepcopy(self._records)
        ids = []
        for value in values:
            metric = _metric_snapshot(value)
            required_kind = _FIXED_KINDS.get(metric["formula_id"])
            if required_kind and kind not in {required_kind, "hypothesis"}:
                _fail("METRIC_KIND_MISMATCH", "formula_id")
            # 基座这一正式标记描述“已记录未付预测在候选分支”，不是用户
            # 假设；确认分支不消费其现金。本层保留标记供reviewer核对范围。
            hypothetical = any(a.get("type") != "recorded_forecast_candidate"
                               for a in metric["assumed_inputs"])
            if hypothetical and kind in {"recorded_fact", "recorded_projection"}:
                _fail("METRIC_ASSUMPTION_AS_FACT")
            mid = metric["metric_id"]
            if mid in pending:
                if pending[mid]["metric"] != metric:
                    _fail("METRIC_SNAPSHOT_CONFLICT", "metric_id")
                if pending[mid]["kind"] != kind:
                    _fail("METRIC_KIND_MISMATCH", "claim_kind")
                if evidence_id not in pending[mid]["evidence_ids"]:
                    pending[mid]["evidence_ids"].append(evidence_id)
            else:
                pending[mid] = {"metric_id": mid, "metric": metric, "kind": kind,
                                "evidence_ids": [evidence_id]}
            ids.append(mid)
        self._records = pending
        return ids

    def lookup(self, metric_id):
        if not isinstance(metric_id, str) or metric_id not in self._records:
            _fail("METRIC_NOT_FOUND", "metric_id")
        return deepcopy(self._records[metric_id])

    def binding(self, metric_id):
        """复制精确签名给模型，无文字生成或金额推导；text由模型另填。"""
        record = self.lookup(metric_id)
        return {**{k: record["metric"][k] for k in REF_FIELDS}, "claim_kind": record["kind"]}

    def validate_claims(self, amount_claims, *, current_source_versions):
        if not isinstance(amount_claims, list) or not isinstance(current_source_versions, Mapping):
            _fail("METRIC_INVALID_CLAIMS")
        validated = []
        allowed = {*REF_FIELDS, "text", "claim_kind", "occurrence_start"}
        for claim in amount_claims:
            if not isinstance(claim, Mapping) or set(claim) - allowed or set(REF_FIELDS) - set(claim):
                _fail("METRIC_CLAIM_REQUIRED")
            if "text" not in claim or "claim_kind" not in claim:
                _fail("METRIC_CLAIM_REQUIRED")
            _string(claim["text"], "text")
            record = self.lookup(claim["metric_id"])
            metric = record["metric"]
            if claim["claim_kind"] != record["kind"]:
                _fail("METRIC_KIND_MISMATCH", "claim_kind")
            for key in REF_FIELDS:
                if claim[key] != metric[key] or (key == "value_minor" and type(claim[key]) is not type(metric[key])):
                    _fail("METRIC_CLAIM_MISMATCH", key)
            for source, version in metric["source_versions"].items():
                if current_source_versions.get(source) != version:
                    _fail("STALE_METRIC_SOURCE", source)
            amounts = monetary_occurrences(claim["text"])
            if metric["value_minor"] is None:
                if amounts:
                    _fail("METRIC_UNKNOWN_NOT_ZERO", "text")
            elif len(amounts) != 1:
                _fail("METRIC_ONE_AMOUNT_PER_CLAIM", "text")
            elif (amounts[0]["value_minor"], amounts[0]["currency"]) != (metric["value_minor"], metric["currency"]):
                _fail("METRIC_LITERAL_MISMATCH", "text")
            validated.append({**record, "claim": _json_copy(dict(claim))})
        return validated

    def validate_answer(self, text, amount_claims, *, current_source_versions, public_quote_spans=(),
                        scenario_metric_ids=None, scenario_context=False):
        """每个金额出现位置都须被准确片段覆盖，同数别处不自动获准。

        public_quote_spans只可由本地独立原文验证器产生，不能直接采用模型
        声称的public范围；本层不判断个人资格。无正式指标时金融金额失败，
        调用方可明确缺失，或改用既有只读answer_facts，不注册伪指标。
        """
        occurrences = monetary_occurrences(text)
        validated = self.validate_claims(amount_claims, current_source_versions=current_source_versions)
        covered = set()
        bound_amounts = {}
        for claim_index, row in enumerate(validated):
            claim = row["claim"]
            fragment = claim["text"]
            if "occurrence_start" in claim:
                start = claim["occurrence_start"]
                if type(start) is not int or start < 0 or text[start:start + len(fragment)] != fragment:
                    _fail("METRIC_CLAIM_TEXT_MISMATCH")
            else:
                start = text.find(fragment)
                if start < 0:
                    _fail("METRIC_CLAIM_TEXT_MISMATCH", f"amount_claims[{claim_index}].text")
                if text.find(fragment, start + 1) >= 0:
                    _fail("METRIC_CLAIM_TEXT_AMBIGUOUS", f"amount_claims[{claim_index}].text")
            for literal in monetary_occurrences(fragment):
                span = (start + literal["start"], start + literal["end"])
                covered.add(span)
                bound_amounts.setdefault(span, []).append(row)
        # 这些区间是可信原文验证的结果，仍验证边界，防止错位漏检。
        for span in public_quote_spans:
            if not isinstance(span, (tuple, list)) or len(span) != 2 or any(type(v) is not int for v in span):
                _fail("PUBLIC_QUOTE_SPAN_INVALID")
            start, end = span
            if not 0 <= start < end <= len(text):
                _fail("PUBLIC_QUOTE_SPAN_INVALID")
            covered.update((v["start"], v["end"]) for v in occurrences
                           if start <= v["start"] and v["end"] <= end)
        if any((v["start"], v["end"]) not in covered for v in occurrences):
            _fail("METRIC_AMOUNT_UNBOUND")
        _require_joint_scenario_scope(text, occurrences, bound_amounts,
                                     public_quote_spans, scenario_metric_ids, scenario_context)
        return {"status": "validated", "amount_claims": validated,
                "amount_occurrence_count": len(occurrences), "independent_review_required": True}
