"""独立消费已验证的 Base RatioMetric，不计算收益、不授予金融权限。

register 只能由服务端已验 HMAC 的 returns 分支调用，kind 固定为
recorded_fact。完整比例原件来自 batch.coverage.ratio_reports，不能从
金额指标、旧标量、模型参数或公共百分比推造。来源真实性与最终 fresh
仍由 Base/Observer 的原验签承担，本模块校验消费签名和逐处显示绑定。
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext, MAX_EMAX, MIN_EMIN
import json
import math
import re
import unicodedata


CONTRACT_VERSION = '1.0.0'
RATIO_FIELDS = frozenset(('contract_version', 'metric_id', 'subject_id', 'metric_name',
    'currency', 'unit', 'value_decimal', 'as_of', 'period_start', 'period_end',
    'data_scope', 'calculation_status', 'basis', 'formula_id', 'input_refs',
    'source_versions', 'excluded_items', 'coverage_gaps', 'assumed_inputs'))
REF_FIELDS = ('metric_id', 'subject_id', 'metric_name', 'currency', 'unit',
    'value_decimal', 'as_of', 'period_start', 'period_end', 'data_scope',
    'calculation_status', 'basis', 'formula_id', 'source_versions')
CLAIM_LIMIT = 24
_DECIMAL_PATTERN = r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?'
_PERCENT = re.compile(r'(?P<number>[-+−＋－]?[0-9０-９.,，．]+)\s*[%％]')
_PERCENT_SYMBOL = re.compile(r'[%％]')
_NUMBER_TOKEN = r'[-+−＋－]?(?:[0-9０-９]+(?:[.,，．][0-9０-９]+)*|[.．][0-9０-９]+)'
_RATIO_TERM = r'(?:收益率|回报率|收益比例|回报比例|权益占比|资产占比|资金占比|持仓占比|投资占比|配置比例|权益比例|资金比例|资产比例|投资比例|仓位|权重)'
_RATE_CONNECTOR = r'(?:(?:为|是|约|大约|达到|达|共|合计|总计|当前|本期|预计|预期|保持|的|：|:|=)\s*)*'
_BARE_RATE = re.compile(_RATIO_TERM + r'\s*' + _RATE_CONNECTOR +
                        r'(?P<number>' + _NUMBER_TOKEN + r')')
_BARE_RATE_SUFFIX = re.compile(r'(?P<number>' + _NUMBER_TOKEN + r')\s*' + _RATE_CONNECTOR + _RATIO_TERM)
_CN_RATE = re.compile(_RATIO_TERM + r'\s*' + _RATE_CONNECTOR +
                     r'[负正零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟]+'
                     r'(?=[%％，。；、!?！？,.\s]|$)'
                     r'|[负正零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟]+\s*' + _RATIO_TERM +
                     r'(?=[%％，。；、!?！？,.\s]|$)')
_UNSUPPORTED_UNITS = re.compile(r'(?:' + _NUMBER_TOKEN + r'|[零〇一二两三四五六七八九十百千万亿]+)'
                                r'\s*(?:‰|bps?\b|基点|个?百分点)', re.I)
_CN_RATIO_NUMBER = r'[-+−＋－负正]?(?:[零〇一二两三四五六七八九十百千万亿]+(?:成|折)(?:仓)?|[零〇一二两三四五六七八九十百千万亿]+分之[零〇一二两三四五六七八九十百千万亿]+|一?半(?:仓|成)?)'
_CN_RATIO_UNITS = re.compile(_RATIO_TERM + r'\s*' + _RATE_CONNECTOR + _CN_RATIO_NUMBER +
                            r'|' + _CN_RATIO_NUMBER + r'\s*' + _RATE_CONNECTOR + _RATIO_TERM)


class RatioClaimError(ValueError):
    def __init__(self, code, field=None):
        self.code, self.field = code, field
        super().__init__(code + (':' + field if field else ''))

    def as_dict(self):
        return {'code': self.code, **({'field': self.field} if self.field else {})}


def _fail(code, field=None):
    raise RatioClaimError(code, field)


def _check_json(value):
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _check_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _check_json(item)
        return
    _fail('RATIO_INVALID_JSON')


def _json_copy(value):
    try:
        _check_json(value)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        return deepcopy(value)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        _fail('RATIO_INVALID_JSON')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':'))


def _string(value, field):
    if type(value) is not str or not value.strip():
        _fail('RATIO_INVALID_CONTRACT', field)


def _date(value, field):
    try:
        if type(value) is not str or date.fromisoformat(value).isoformat() != value:
            _fail('RATIO_INVALID_CONTRACT', field)
    except (ValueError, TypeError):
        _fail('RATIO_INVALID_CONTRACT', field)


def _decimal(value):
    if type(value) is not str or not re.fullmatch(_DECIMAL_PATTERN, value):
        _fail('RATIO_DECIMAL_REQUIRED', 'value_decimal')
    try:
        result = Decimal(value)
        if not result.is_finite():
            _fail('RATIO_DECIMAL_REQUIRED', 'value_decimal')
        return result
    except InvalidOperation:
        _fail('RATIO_DECIMAL_REQUIRED', 'value_decimal')


def format_percent(value_decimal):
    """只显示原 ratio 的精确 Decimal×100；不舍入、不转 float/null 为零。

    超长展开无法显示时显式失败，原件仍可登记为完整结构；不截断数值。
    """
    if value_decimal is None:
        return None
    value = _decimal(value_decimal)
    digits = len(value.as_tuple().digits)
    # 防止指数形式恶意申请巨大字符串；失败不改变原 ratio 或覆盖状态。
    if max(digits + abs(min(value.as_tuple().exponent + 2, 0)), value.adjusted() + 3) > 4096:
        _fail('RATIO_DISPLAY_UNSUPPORTED', 'value_decimal')
    try:
        with localcontext() as ctx:
            ctx.prec = max(28, digits + 2)
            ctx.Emax, ctx.Emin = MAX_EMAX, MIN_EMIN
            scaled = value * Decimal(100)
        text = format(scaled, 'f')
    except (InvalidOperation, OverflowError, ValueError):
        _fail('RATIO_DISPLAY_UNSUPPORTED', 'value_decimal')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text + '%'


def _quote_spans(text, spans):
    if not isinstance(spans, (tuple, list)):
        _fail('PUBLIC_RATIO_QUOTE_SPAN_INVALID')
    checked = []
    for span in spans:
        if (not isinstance(span, (tuple, list)) or len(span) != 2
                or any(type(v) is not int for v in span)):
            _fail('PUBLIC_RATIO_QUOTE_SPAN_INVALID')
        start, end = span
        if not 0 <= start < end <= len(text):
            _fail('PUBLIC_RATIO_QUOTE_SPAN_INVALID')
        checked.append((start, end))
    return checked


def percentage_occurrences(text, *, public_quote_spans=()):
    """逐处扫描精确数字百分比；不把金额、100天或R4当比例。

    个人比例只支持阿拉伯数字%；其他数字比例语法不猜值。公开原文区间
    只能由本地原文验证器产生，区间内保留原文，不能从模型输入取区间。
    位置始终绑定原文本，不在此规范化整个答复。
    """
    if type(text) is not str:
        _fail('RATIO_INVALID_TEXT')
    spans = _quote_spans(text, public_quote_spans)
    def quoted(start, end):
        return any(left <= start and end <= right for left, right in spans)
    occurrences = []
    for match in _PERCENT.finditer(text):
        if match.start():
            previous = unicodedata.normalize('NFKC', text[match.start() - 1])
            if any(c in '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.,+-−' for c in previous):
                if quoted(match.start(), match.end()):
                    continue
                _fail('INVALID_RATIO_TEXT')
        number = unicodedata.normalize('NFKC', match['number']).replace('−', '-')
        if not re.fullmatch(r'[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?', number):
            if quoted(match.start(), match.end()):
                continue
            _fail('INVALID_RATIO_TEXT')
        try:
            value = Decimal(number.replace(',', ''))
        except InvalidOperation:
            _fail('INVALID_RATIO_TEXT')
        occurrences.append({'start': match.start(), 'end': match.end(), 'percent_decimal': str(value)})
    for symbol in _PERCENT_SYMBOL.finditer(text):
        if (not any(item['start'] <= symbol.start() < item['end'] for item in occurrences)
                and not quoted(symbol.start(), symbol.end())):
            _fail('INVALID_RATIO_TEXT')
    for pattern in (re.compile('百分之'), _CN_RATE, _UNSUPPORTED_UNITS, _CN_RATIO_UNITS):
        for match in pattern.finditer(text):
            if not quoted(match.start(), match.end()):
                _fail('RATIO_UNSUPPORTED_TEXT')
    for pattern in (_BARE_RATE, _BARE_RATE_SUFFIX):
        for match in pattern.finditer(text):
            start, end = match.span('number')
            if (not any(item['start'] <= start and end <= item['end'] for item in occurrences)
                    and not quoted(match.start(), match.end())):
                _fail('RATIO_UNSUPPORTED_TEXT')
    return occurrences


def _snapshot(raw):
    if not isinstance(raw, Mapping) or set(raw) != RATIO_FIELDS:
        _fail('RATIO_CONTRACT_REQUIRED')
    metric = _json_copy(dict(raw))
    if metric['contract_version'] != CONTRACT_VERSION:
        _fail('RATIO_CONTRACT_VERSION', 'contract_version')
    for field in ('metric_id', 'subject_id', 'metric_name', 'data_scope', 'basis', 'formula_id'):
        _string(metric[field], field)
    if type(metric['currency']) is not str or not re.fullmatch('[A-Z]{3}', metric['currency']):
        _fail('RATIO_INVALID_CONTRACT', 'currency')
    if metric['unit'] != 'ratio':
        _fail('RATIO_UNIT_REQUIRED', 'unit')
    if metric['as_of'] is not None:
        _date(metric['as_of'], 'as_of')
        if metric['period_start'] is not None or metric['period_end'] is not None:
            _fail('RATIO_INVALID_CONTRACT', 'period')
    else:
        _date(metric['period_start'], 'period_start')
        _date(metric['period_end'], 'period_end')
        if metric['period_start'] > metric['period_end']:
            _fail('RATIO_INVALID_CONTRACT', 'period')
    for field in ('input_refs', 'excluded_items', 'coverage_gaps'):
        if type(metric[field]) is not list or any(type(v) is not str or not v for v in metric[field]):
            _fail('RATIO_INVALID_CONTRACT', field)
    if not metric['input_refs']:
        _fail('RATIO_INVALID_CONTRACT', 'input_refs')
    if (type(metric['source_versions']) is not dict or not metric['source_versions'] or any(
            type(key) is not str or not key or type(version) is not str or not version
            for key, version in metric['source_versions'].items())):
        _fail('RATIO_INVALID_CONTRACT', 'source_versions')
    if (type(metric['assumed_inputs']) is not list
            or any(type(v) is not dict for v in metric['assumed_inputs'])):
        _fail('RATIO_INVALID_CONTRACT', 'assumed_inputs')
    if metric['assumed_inputs']:
        # 当前正式 B RatioMetric 固定无假设；未来预测协议不能升成 recorded_fact。
        _fail('RATIO_ASSUMPTION_AS_FACT', 'assumed_inputs')
    if metric['calculation_status'] == 'calculated':
        _decimal(metric['value_decimal'])
        if metric['coverage_gaps']:
            _fail('RATIO_INCOMPLETE_NOT_FACT', 'coverage_gaps')
    elif metric['calculation_status'] == 'unavailable':
        if metric['value_decimal'] is not None or not metric['coverage_gaps']:
            _fail('RATIO_UNKNOWN_NOT_ZERO', 'value_decimal')
    else:
        _fail('RATIO_INVALID_CONTRACT', 'calculation_status')
    return metric


def validate_snapshot(raw):
    """完整19字段类型/覆盖校验及副本；不验HMAC、不登记权限或kind。"""
    return _snapshot(raw)


def ratio_claim_schema(metric_ids=()):
    """完整比例签名，source ref只由外层已绑定边界精确展开后进入本模块。"""
    properties = {key: {'type': 'string', 'minLength': 1, 'maxLength': 256} for key in REF_FIELDS}
    properties.update(unit={'type': 'string', 'enum': ['ratio']},
        currency={'type': 'string', 'pattern': '^[A-Z]{3}$'},
        value_decimal={'type': ['string', 'null'], 'maxLength': 256, 'pattern': '^' + _DECIMAL_PATTERN + '$'},
        calculation_status={'type': 'string', 'enum': ['calculated', 'unavailable']},
        source_versions={'type': 'object', 'minProperties': 1, 'additionalProperties': {'type': 'string', 'minLength': 1, 'maxLength': 128}},
        claim_kind={'type': 'string', 'enum': ['recorded_fact']},
        text={'type': 'string', 'minLength': 1, 'maxLength': 1200}, occurrence_start={'type': 'integer', 'minimum': 0})
    for key in ('as_of', 'period_start', 'period_end'):
        properties[key] = {'type': ['string', 'null']}
    if metric_ids:
        properties['metric_id']['enum'] = list(metric_ids)
    return {'type': 'array', 'maxItems': CLAIM_LIMIT, 'items': {'type': 'object',
        'properties': properties, 'required': [*REF_FIELDS, 'text', 'claim_kind'], 'additionalProperties': False}}


class RatioRegistry:
    """单轮已验服务原件登记；register 不是模型工具，固定 returns kind。"""
    def __init__(self):
        self._records = {}

    @property
    def metric_ids(self):
        return tuple(self._records)

    def register(self, ratios, *, kind, evidence_id):
        if kind != 'recorded_fact' or type(kind) is not str:
            _fail('RATIO_KIND_REQUIRED')
        _string(evidence_id, 'evidence_id')
        values = [ratios] if isinstance(ratios, Mapping) and 'metric_id' in ratios else ratios
        if type(values) is not list:
            _fail('RATIO_CONTRACT_REQUIRED')
        pending, ids = deepcopy(self._records), []
        for raw in values:
            metric = _snapshot(raw)
            mid = metric['metric_id']
            if mid in pending:
                if _canonical(pending[mid]['metric']) != _canonical(metric):
                    _fail('RATIO_SNAPSHOT_CONFLICT', 'metric_id')
                if evidence_id not in pending[mid]['evidence_ids']:
                    pending[mid]['evidence_ids'].append(evidence_id)
            else:
                pending[mid] = {'metric_id': mid, 'metric': metric, 'kind': 'recorded_fact',
                                'evidence_ids': [evidence_id]}
            ids.append(mid)
        self._records = pending
        return ids

    def lookup(self, metric_id):
        if type(metric_id) is not str or metric_id not in self._records:
            _fail('RATIO_NOT_FOUND', 'metric_id')
        return deepcopy(self._records[metric_id])

    def records(self):
        return deepcopy(list(self._records.values()))

    def all(self):
        return self.records()

    def binding(self, metric_id):
        record = self.lookup(metric_id)
        return {**{key: deepcopy(record['metric'][key]) for key in REF_FIELDS},
                'claim_kind': 'recorded_fact'}

    def validate_claims(self, ratio_claims, *, current_source_versions):
        if type(ratio_claims) is not list or not isinstance(current_source_versions, Mapping):
            _fail('RATIO_INVALID_CLAIMS')
        if len(ratio_claims) > CLAIM_LIMIT:
            _fail('RATIO_CLAIM_LIMIT')
        validated = []
        for raw in ratio_claims:
            if (not isinstance(raw, Mapping) or set(raw) - {*REF_FIELDS, 'text', 'claim_kind', 'occurrence_start'}
                    or {*REF_FIELDS, 'text', 'claim_kind'} - set(raw)):
                _fail('RATIO_CLAIM_REQUIRED')
            claim = _json_copy(dict(raw))
            _string(claim['text'], 'text')
            record = self.lookup(claim['metric_id'])
            metric = record['metric']
            if claim['claim_kind'] != 'recorded_fact':
                _fail('RATIO_KIND_MISMATCH', 'claim_kind')
            for key in REF_FIELDS:
                if _canonical(claim[key]) != _canonical(metric[key]):
                    _fail('RATIO_CLAIM_MISMATCH', key)
            for source, version in metric['source_versions'].items():
                if current_source_versions.get(source) != version:
                    _fail('STALE_RATIO_SOURCE', 'source_versions')
            if 'occurrence_start' in claim and (type(claim['occurrence_start']) is not int or claim['occurrence_start'] < 0):
                _fail('RATIO_CLAIM_TEXT_MISMATCH')
            occurrences = percentage_occurrences(claim['text'])
            if metric['value_decimal'] is None:
                if occurrences:
                    _fail('RATIO_UNKNOWN_NOT_ZERO', 'text')
            elif len(occurrences) != 1:
                _fail('RATIO_ONE_PERCENT_PER_CLAIM', 'text')
            elif Decimal(occurrences[0]['percent_decimal']) != Decimal(format_percent(metric['value_decimal'])[:-1]):
                _fail('RATIO_LITERAL_MISMATCH', 'text')
            validated.append({**record, 'claim': claim})
        return validated

    def validate_answer(self, text, ratio_claims, *, current_source_versions, public_quote_spans=()):
        """每处百分比独立绑定；公开豁免只能来自本地已验全文出处的范围。"""
        occurrences = percentage_occurrences(text, public_quote_spans=public_quote_spans)
        rows = self.validate_claims(ratio_claims, current_source_versions=current_source_versions)
        covered = set()
        for row in rows:
            claim, fragment = row['claim'], row['claim']['text']
            if 'occurrence_start' in claim:
                start = claim['occurrence_start']
                if text[start:start + len(fragment)] != fragment:
                    _fail('RATIO_CLAIM_TEXT_MISMATCH')
            else:
                start = text.find(fragment)
                if start < 0 or text.find(fragment, start + 1) >= 0:
                    _fail('RATIO_CLAIM_TEXT_AMBIGUOUS')
            covered.update((start + item['start'], start + item['end'])
                           for item in percentage_occurrences(fragment))
        for span in public_quote_spans:
            if (not isinstance(span, (tuple, list)) or len(span) != 2
                    or any(type(v) is not int for v in span)):
                _fail('PUBLIC_RATIO_QUOTE_SPAN_INVALID')
            start, end = span
            if not 0 <= start < end <= len(text):
                _fail('PUBLIC_RATIO_QUOTE_SPAN_INVALID')
            covered.update((item['start'], item['end']) for item in occurrences
                           if start <= item['start'] and item['end'] <= end)
        if any((item['start'], item['end']) not in covered for item in occurrences):
            _fail('RATIO_PERCENT_UNBOUND')
        return {'status': 'validated', 'ratio_claims': rows,
                'ratio_occurrence_count': len(occurrences), 'independent_review_required': True}
