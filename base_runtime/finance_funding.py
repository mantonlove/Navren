"""内部只读资金来源合同；金额未知保留为 None，由纯方法计算预算。

provider 只由维护方注入，不是 HTTP 或模型工具。调用者提供已开启的银行
事务，并核验 period_start 与银行业务日期；本模块不开连接、不写库、不注册
指标。来源中的 holds 仅属于该来源范围，不证明银行账本已经扣除同一占用。
"""
from dataclasses import fields, replace
from datetime import timedelta
import hashlib
import json

from financial_methods import MetricContext
from financial_methods.contracts import iso_date, money_metric
from financial_methods.funding import CashSnapshot, FundingEvent, Hold, evaluate_funding
from simulated_bank import BankError


CONTRACT = 'navren.finance-funding-source/1'
_ENVELOPE_FIELDS = {'subject_id', 'currency', 'data_scope', 'as_of', 'period_start',
                    'period_end', 'coverage_complete', 'input_refs', 'source_versions',
                    'records', 'catalog', 'provenance'}
_RESERVED_NAMES = {'bank', 'business_date', 'private_context', 'personal', 'demo_dataset'}
_RESERVED_PREFIXES = ('financial_', 'finance_input/', 'finance_adapter_',
                      'finance_funding_', 'finance_funding_input/', 'tool_table/')
_CATEGORIES = {'cash', 'events', 'holds', 'coverage'}


def _invalid(message='资金来源合同无效。'):
    raise BankError('METRIC_ARGUMENTS_INVALID', message, 422)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _json_copy(value):
    def check(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                _invalid('来源字段名必须是字符串。')
            for child in item.values():
                check(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, int, bool):
            _invalid('来源必须使用 JSON 字符串、整数、布尔值或 null。')
    check(value)
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        _invalid()


def _refs(value):
    if (not isinstance(value, list) or any(not _text(ref) for ref in value)
            or len(set(value)) != len(value)):
        _invalid('来源引用或目录 ID 必须是无重复的非空字符串列表。')
    return sorted(value)


def _day(value):
    try:
        return iso_date(value)
    except (TypeError, ValueError):
        _invalid('资金来源需要完整有效的 ISO 日期。')


def _minor(value):
    if value is not None and (type(value) is not int or value < 0):
        _invalid('来源金额必须是非负整数分，未知金额使用 null。')


def _identity(row, workspace):
    if row['subject_id'] != workspace:
        raise BankError('METRIC_SUBJECT_MISMATCH', '资金来源不属于当前工作区。', 403)
    if row['currency'] != 'CNY':
        raise BankError('METRIC_CURRENCY_UNSUPPORTED', '资金来源仅支持单一人民币。', 422)


def _row(value, cls, workspace, category, source):
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
        _invalid('现金、事项和占用必须提供完整的受限原件字段。')
    _identity(value, workspace)
    ref, version = value['evidence_ref'], value['source_version']
    if (not _text(ref) or not _text(version) or ref not in source['input_refs']
            or source['source_versions'].get(ref) != version
            or ref not in source['provenance'][category]):
        _invalid('实际消费原件必须绑定本类证明目录及精确来源版本。')
    return value


def _normalize(value, workspace, days):
    source = _json_copy(value)
    if not isinstance(source, dict) or set(source) != _ENVELOPE_FIELDS:
        _invalid('资金来源缺少完整 envelope 或包含未支持字段。')
    _identity(source, workspace)
    if not _text(source['data_scope']) or source['as_of'] is not None:
        _invalid('资金来源必须说明范围并使用完整期间。')
    start, end = _day(source['period_start']), _day(source['period_end'])
    if end != start + timedelta(days=days):
        _invalid('资金来源期间必须匹配所请求的天数。')
    if type(source['coverage_complete']) is not bool:
        _invalid('资金来源覆盖状态必须显式使用布尔值。')
    source['input_refs'] = _refs(source['input_refs'])
    versions = source['source_versions']
    if (not source['input_refs'] or not isinstance(versions, dict)
            or any(not _text(ref) or not _text(version) for ref, version in versions.items())
            or set(versions) != set(source['input_refs'])):
        _invalid('每个实际输入引用必须有且仅有精确非空来源版本。')
    if any(ref in _RESERVED_NAMES or ref.startswith(_RESERVED_PREFIXES) for ref in versions):
        _invalid('资金来源不得覆盖银行或指标服务的保留版本名称。')
    source['source_versions'] = {ref: versions[ref] for ref in sorted(versions)}
    provenance = source['provenance']
    if not isinstance(provenance, dict) or set(provenance) != _CATEGORIES:
        _invalid('资金来源必须分别声明现金、事项、占用和覆盖证明目录。')
    for category in sorted(_CATEGORIES):
        provenance[category] = _refs(provenance[category])
        if any(ref not in versions for ref in provenance[category]):
            _invalid('证明目录引用必须进入来源 envelope。')
    if set(source['input_refs']) != {ref for refs in provenance.values() for ref in refs}:
        _invalid('来源输入引用不能游离于证明目录。')
    catalog = source['catalog']
    if (not isinstance(catalog, dict)
            or set(catalog) != {'complete', 'coverage_ref', 'expected_event_ids', 'expected_hold_ids'}
            or type(catalog['complete']) is not bool
            or catalog['complete'] != source['coverage_complete']
            or not _text(catalog['coverage_ref'])
            or catalog['coverage_ref'] not in provenance['coverage']):
        _invalid('资金来源需要独立且显式完整或不完整的覆盖目录。')
    catalog['expected_event_ids'] = _refs(catalog['expected_event_ids'])
    catalog['expected_hold_ids'] = _refs(catalog['expected_hold_ids'])
    records = source['records']
    if (not isinstance(records, dict) or set(records) != {'cash', 'events', 'holds'}
            or not isinstance(records['events'], list) or not isinstance(records['holds'], list)):
        _invalid('资金来源原件容器无效。')
    cash = _row(records['cash'], CashSnapshot, workspace, 'cash', source)
    if cash['basis'] != 'ledger_cash_balance':
        _invalid('现金原件必须是尚未扣除底线或占用的账面余额。')
    if _day(cash['as_of']) != start:
        _invalid('现金原件时点必须是期间起点。')
    coverage_start, coverage_end = _day(cash['coverage_start']), _day(cash['coverage_end'])
    if coverage_start > coverage_end or type(cash['coverage_complete']) is not bool:
        _invalid('现金原件覆盖期间或状态无效。')
    if source['coverage_complete'] and (not cash['coverage_complete']
            or coverage_start > start or coverage_end < end):
        _invalid('完整来源的现金覆盖必须涵盖请求期间。')
    for key in ('balance_minor', 'frozen_minor', 'emergency_floor_minor'):
        _minor(cash[key])
    for category, cls, id_field in (('events', FundingEvent, 'event_id'), ('holds', Hold, 'hold_id')):
        ids = []
        for row in records[category]:
            _row(row, cls, workspace, category, source)
            if not _text(row[id_field]):
                _invalid('事项和占用必须有稳定 ID。')
            ids.append(row[id_field])
            _minor(row['amount_minor'])
            if category == 'events':
                _day(row['on'])
                if (row['direction'] not in ('inflow', 'outflow')
                        or row['certainty'] not in ('confirmed', 'candidate', 'forecast', 'assumption', 'historical')
                        or not _text(row['kind'])):
                    _invalid('事项方向、确认状态或种类无效。')
            else:
                if type(row['release_on_payment']) is not bool:
                    _invalid('占用付款释放规则必须明确使用布尔值。')
                if row['release_on'] is not None:
                    _day(row['release_on'])
                links = row['commitment_allocations']
                if not isinstance(links, list):
                    _invalid('占用必须提供明确的义务分配列表。')
                linked = []
                for pair in links:
                    if not isinstance(pair, list) or len(pair) != 2 or not _text(pair[0]):
                        _invalid('占用义务关联必须是 ID 与整数金额对。')
                    if type(pair[1]) is not int or pair[1] < 0:
                        _invalid('占用义务分配金额必须是非负整数分。')
                    linked.append(pair[0])
                if len(set(linked)) != len(linked):
                    _invalid('同一占用不能重复关联一个义务。')
                row['commitment_allocations'] = sorted(links)
        if len(set(ids)) != len(ids):
            _invalid('资金来源不能重复提供同一个事项或占用 ID。')
        expected = set(catalog['expected_' + ('event_ids' if category == 'events' else 'hold_ids')])
        if set(ids) - expected or (catalog['complete'] and set(ids) != expected):
            _invalid('实际原件与独立覆盖目录不匹配。')
        records[category] = sorted(records[category], key=lambda row: row[id_field])
    return source


def source_fingerprint(source):
    """完整归一化消费原件的摘要；同版本内容变化仍改变摘要。"""
    body = {key: value for key, value in source.items() if key != 'envelope_digest'}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def read_funding_source(provider, workspace, days, db):
    """在调用者现有事务中读取内部 provider；不打开连接或嵌套事务。"""
    if (not _text(workspace) or type(days) is not int or not 1 <= days <= 365
            or db is None or not callable(getattr(provider, 'snapshot', None))):
        _invalid('资金来源需要内部 snapshot 服务、有效工作区/期限和现有事务。')
    source = _normalize(provider.snapshot(workspace, days, db=db), workspace, days)
    source['envelope_digest'] = source_fingerprint(source)
    return source


def evaluate_source(source, *, additional_versions=()):
    """仅计算已归一化的原件；不补金额、不改指标、不注册或签名。"""
    if not isinstance(source, dict) or not _text(source.get('envelope_digest')):
        _invalid('计算需要已读取并绑定摘要的资金来源。')
    copied = _json_copy(source)
    if copied['envelope_digest'] != source_fingerprint(copied):
        _invalid('读取后资金来源内容已变化。')
    body = {key: value for key, value in copied.items() if key != 'envelope_digest'}
    if set(body) != _ENVELOPE_FIELDS or not _text(body.get('subject_id')):
        _invalid('计算需要完整且对象明确的资金来源。')
    days = (_day(body['period_end']) - _day(body['period_start'])).days
    if not 1 <= days <= 365:
        _invalid('资金来源期限无效。')
    body = _normalize(body, body['subject_id'], days)
    # 只有调用方的指标服务可附加银行/算法版本，provider 不能写这些保留键。
    versions = dict(body['source_versions'])
    try:
        pairs = tuple(additional_versions)
    except TypeError:
        _invalid('指标服务附加版本需要精确的来源版本对。')
    for pair in pairs:
        if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                or any(not _text(value) for value in pair)):
            _invalid('指标服务附加版本需要精确的非空来源版本对。')
        ref, version = pair
        if ref in versions and versions[ref] != version:
            raise BankError('METRIC_VERSION_CONFLICT', '资金原件与指标服务来源版本冲突。', 409)
        versions[ref] = version
    context = MetricContext(body['subject_id'], body['currency'], body['data_scope'],
        period_start=body['period_start'], period_end=body['period_end'],
        input_refs=tuple(body['input_refs']), source_versions=tuple(versions.items()),
        coverage_complete=body['coverage_complete'])
    cash = CashSnapshot(**body['records']['cash'])
    events = tuple(FundingEvent(**row) for row in body['records']['events'])
    holds = tuple(Hold(**{**row, 'commitment_allocations':tuple(tuple(pair) for pair in row['commitment_allocations'])})
                  for row in body['records']['holds'])
    result = evaluate_funding(context, cash, events, holds)
    if cash.balance_minor is None:
        # 诊断只描述原件中确实缺失的余额，并保留纯引擎的全部失败原因。
        # 经 money_metric 重建内容摘要；不能修改 gap 后沿用旧 metric_id。
        metrics = {}
        for key, metric in result.metrics.items():
            metric_context = MetricContext(metric.subject_id, metric.currency, metric.data_scope,
                as_of=metric.as_of, period_start=metric.period_start, period_end=metric.period_end,
                input_refs=metric.input_refs, source_versions=metric.source_versions)
            metrics[key] = money_metric(metric_context, metric.metric_name, metric.basis,
                metric.formula_id, metric.value_minor, status=metric.calculation_status,
                gaps=(*metric.coverage_gaps, 'missing_balance'), excluded=metric.excluded_items,
                assumptions=metric.assumed_inputs)
        result = replace(result, metrics=metrics,
                         coverage_gaps=tuple(dict.fromkeys((*result.coverage_gaps, 'missing_balance'))))
    return result.to_dict()
