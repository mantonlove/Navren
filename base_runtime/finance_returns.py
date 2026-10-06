"""受信 Base 快照到纯收益合同的受限映射；无 I/O、发行或执行权限。

价格只从 records.prices 读取，按独立 catalog 的产品/份额/渠道和日期匹配。
行内裸 unit_price、账面值、成本均不替代公共价格或实际持有数量。
"""
from dataclasses import replace

from financial_methods.contracts import MetricContext, decimal_value, iso_date, money_metric
from financial_methods.returns import (
    DividendReceivable, EquitySnapshot, HoldingEvent, HoldingValue, HistoryCoverage,
    OpeningPosition, RatioMetric, ReturnBoundary, ReturnSubperiod,
    holdings_market_value, period_profit, portfolio_equity, reconstruct_history,
    simple_return, time_weighted_return,
)
from simulated_bank import BankError


def _invalid(message):
    raise BankError('METRIC_ARGUMENTS_INVALID', message, 422)


def _mapping(value, name, *, missing=False):
    if value is None and missing:
        return {}
    if not isinstance(value, dict):
        _invalid(name + '必须为对象')
    return value


def _rows(value, name):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        _invalid(name + '必须为记录序列或 null')
    return tuple(_mapping(row, name) for row in value)


def _scalar(value, name, kind, *, nonnegative=False):
    if value is None:
        return None
    if kind == 'money' or kind == 'integer':
        if type(value) is not int or nonnegative and value < 0:
            _invalid(name + '必须为整数，金额以分计')
    elif kind == 'bool':
        if type(value) is not bool:
            _invalid(name + '必须为布尔值')
    elif kind == 'text':
        if not isinstance(value, str) or not value.strip():
            _invalid(name + '必须为非空字符串')
    elif kind == 'date':
        try:
            iso_date(value, name)
        except ValueError as error:
            _invalid(str(error))
    elif kind == 'decimal':
        try:
            decimal_value(value, name, nonnegative=True)
        except ValueError as error:
            _invalid(str(error))
    return value


def _ids(value, name):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        _invalid(name + '必须为独立身份清单或 null')
    parsed = tuple(_scalar(item, name, 'text') for item in value)
    if any(item is None for item in parsed):
        _invalid(name + '中的身份不得为 null')
    return parsed


def _metadata(row, context, gaps, label, *, on=None):
    for key in ('subject_id', 'currency', 'data_scope', 'instrument_id', 'share_class_id', 'channel_id'):
        _scalar(row.get(key), label + '.' + key, 'text')
    for key, expected in (('subject_id', context.subject_id), ('currency', context.currency)):
        if row.get(key) is None:
            gaps.append(label + '_' + key + '_missing')
        elif row[key] != expected:
            gaps.append(label + '_' + key + '_mismatch')
    if row.get('data_scope') is not None and row['data_scope'] != context.data_scope:
        gaps.append(label + '_scope_mismatch')
    if on is not None:
        _scalar(row.get('on'), label + '.on', 'date')
        if row.get('on') is None:
            gaps.append(label + '_on_missing')
        elif row['on'] != on:
            gaps.append(label + '_date_mismatch')


def _provenance(context, provenance, categories):
    gaps = []
    refs, versions = set(context.input_refs), dict(context.source_versions)
    for category, values in provenance.items():
        _scalar(category, 'provenance.category', 'text')
        parsed = _ids(values, 'provenance.' + category)
        if parsed is not None:
            for ref in parsed:
                if ref not in refs:
                    gaps.append('provenance_reference_not_bound:' + ref)
                if ref not in versions:
                    gaps.append('provenance_version_missing:' + ref)
    for category in categories:
        if not provenance.get(category):
            gaps.append('provenance_missing:' + category)
    return gaps


def _catalog(catalog, context, gaps):
    expected = _ids(catalog.get('expected_instrument_ids'), 'catalog.expected_instrument_ids')
    if expected is None:
        gaps.append('expected_instrument_catalog_missing')
        expected = ()
    if len(set(expected)) != len(expected):
        gaps.append('duplicate_expected_instrument_identity')
    rows = _rows(catalog.get('instruments'), 'catalog.instruments')
    if rows is None:
        gaps.append('instrument_identity_catalog_missing')
        rows = ()
    directory = {}
    for row in rows:
        for key in ('currency', 'data_scope'):
            _scalar(row.get(key), 'catalog.' + key, 'text')
        identity = tuple(_scalar(row.get(key), 'catalog.' + key, 'text')
                         for key in ('instrument_id', 'share_class_id', 'channel_id'))
        if None in identity:
            gaps.append('instrument_identity_catalog_incomplete')
            continue
        if identity[0] in directory:
            gaps.append('duplicate_instrument_catalog_identity')
        directory[identity[0]] = identity
        if row.get('currency') is not None and row['currency'] != context.currency:
            gaps.append('instrument_catalog_currency_mismatch')
        if row.get('data_scope') is not None and row['data_scope'] != context.data_scope:
            gaps.append('instrument_catalog_scope_mismatch')
    if set(directory) != set(expected):
        gaps.append('instrument_identity_catalog_coverage_mismatch')
    return expected, directory


def _prices(records):
    rows = _rows(records.get('prices'), 'records.prices') or ()
    for row in rows:
        for key in ('instrument_id', 'share_class_id', 'channel_id', 'currency', 'data_scope'):
            _scalar(row.get(key), 'price.' + key, 'text')
        _scalar(row.get('on'), 'price.on', 'date')
        _scalar(row.get('unit_price'), 'price.unit_price', 'decimal')
        _scalar(row.get('unit'), 'price.unit', 'text')
    return rows


def _identity(row, directory, gaps, label):
    identity = tuple(_scalar(row.get(key), label + '.' + key, 'text')
                     for key in ('instrument_id', 'share_class_id', 'channel_id'))
    if None in identity:
        gaps.append(label + '_identity_missing')
    elif directory.get(identity[0]) != identity:
        gaps.append(label + '_identity_catalog_mismatch')
    return identity


def _price(identity, on, currency, scope, prices):
    matching = [row for row in prices if tuple(row.get(key) for key in
        ('instrument_id', 'share_class_id', 'channel_id')) == identity and row.get('on') == on
        and row.get('currency') == currency and row.get('data_scope', scope) == scope
        and row.get('unit', 'major_currency_per_share') == 'major_currency_per_share']
    # 缺失、重复或矛盾原件都不选择一个价格凑成覆盖。
    if len(matching) != 1:
        return None
    return matching[0].get('unit_price')


def _snapshot(raw, context, expected, directory, prices, on, gaps, label):
    row = _mapping(raw, label, missing=True)
    if not row:
        # 只有身份/时点元数据来自已受信context；没有制造任何账户数值。
        return EquitySnapshot(context.subject_id, context.currency, on, None, expected,
                              None, None, None, False)
    _metadata(row, context, gaps, label, on=on)
    own_expected = _ids(row.get('expected_instrument_ids'), label + '.expected_instrument_ids')
    if own_expected is not None and (len(own_expected) != len(expected) or set(own_expected) != set(expected)):
        gaps.append(label + '_expected_catalog_mismatch')
    holdings = _rows(row.get('holdings'), label + '.holdings')
    mapped = None if holdings is None else []
    for holding in holdings or ():
        identity = _identity(holding, directory, gaps, label + '_holding')
        _scalar(holding.get('currency'), label + '.holding.currency', 'text')
        _scalar(holding.get('data_scope'), label + '.holding.data_scope', 'text')
        if holding.get('data_scope') is not None and holding['data_scope'] != context.data_scope:
            gaps.append(label + '_holding_scope_mismatch')
        quantity = _scalar(holding.get('quantity'), label + '.holding.quantity', 'decimal')
        mapped.append(HoldingValue(identity[0] or '', holding.get('currency'), quantity,
            _price(identity, on, context.currency, context.data_scope, prices)))
    components = {key: _scalar(row.get(key), label + '.' + key, 'money', nonnegative=True)
                  for key in ('cash_minor', 'receivables_minor', 'payables_minor')}
    complete = _scalar(row.get('coverage_complete'), label + '.coverage_complete', 'bool')
    return EquitySnapshot(row.get('subject_id', context.subject_id), row.get('currency', context.currency),
        row.get('on') or on, None if mapped is None else tuple(mapped), expected,
        components['cash_minor'], components['receivables_minor'], components['payables_minor'], complete is True)


def _boundaries(records, context, gaps):
    parts = _rows(records.get('subperiods'), 'records.subperiods') or ()
    borders = _rows(records.get('boundaries'), 'records.boundaries') or ()
    converted_parts, converted_borders = [], []
    for row in parts:
        _metadata(row, context, gaps, 'subperiod')
        for name in ('start', 'end'):
            _scalar(row.get(name), 'subperiod.' + name, 'date')
            if row.get(name) is None:
                gaps.append('subperiod_' + name + '_missing')
        values = {name: _scalar(row.get(name), 'subperiod.' + name, 'money', nonnegative=True)
                  for name in ('opening_equity_minor', 'closing_equity_minor',
                               'internal_contributions_minor', 'internal_withdrawals_minor')}
        converted_parts.append(ReturnSubperiod(row.get('subject_id'), row.get('currency'),
            row.get('start') or context.period_start, row.get('end') or context.period_end, **values))
    for row in borders:
        _metadata(row, context, gaps, 'boundary')
        _scalar(row.get('on'), 'boundary.on', 'date')
        if row.get('on') is None:
            gaps.append('boundary_on_missing')
        converted_borders.append(ReturnBoundary(row.get('subject_id'), row.get('currency'),
            row.get('on') or context.period_start,
            _scalar(row.get('contribution_minor'), 'boundary.contribution_minor', 'money', nonnegative=True),
            _scalar(row.get('withdrawal_minor'), 'boundary.withdrawal_minor', 'money', nonnegative=True)))
    return tuple(converted_parts), tuple(converted_borders)


def _unavailable(metric, reasons):
    if not reasons:
        return metric
    context = MetricContext(metric.subject_id, metric.currency, metric.data_scope,
        metric.as_of, metric.period_start, metric.period_end, metric.input_refs, metric.source_versions)
    return money_metric(context, metric.metric_name, metric.basis, metric.formula_id, None,
        status='unavailable', gaps=tuple(dict.fromkeys((*metric.coverage_gaps, *reasons))),
        excluded=metric.excluded_items, assumptions=metric.assumed_inputs)


def _equity(context, records, catalog, expected, directory, prices, gaps):
    if context.as_of is not None:
        snapshot = _snapshot(records.get('snapshot'), context, expected, directory, prices,
                             context.as_of, gaps, 'snapshot')
        active = replace(context, coverage_complete=False) if gaps else context
        return {'holdings_market_value': holdings_market_value(active, snapshot),
                'portfolio_equity': portfolio_equity(active, snapshot)}, [], {}
    opening = _snapshot(records.get('opening'), context, expected, directory, prices,
                        context.period_start, gaps, 'opening')
    closing = _snapshot(records.get('closing'), context, expected, directory, prices,
                        context.period_end, gaps, 'closing')
    contributions = _scalar(records.get('owner_contributions_minor'), 'owner_contributions_minor', 'money', nonnegative=True)
    withdrawals = _scalar(records.get('owner_withdrawals_minor'), 'owner_withdrawals_minor', 'money', nonnegative=True)
    has_twr = 'subperiods' in records or 'boundaries' in records
    parts, borders = _boundaries(records, context, gaps) if has_twr else ((), ())
    active = replace(context, coverage_complete=False) if gaps else context
    start_context = replace(active, as_of=context.period_start, period_start=None, period_end=None)
    end_context = replace(active, as_of=context.period_end, period_start=None, period_end=None)
    begin = portfolio_equity(start_context, opening)
    end = portfolio_equity(end_context, closing)
    metrics = {'opening_holdings_market_value': holdings_market_value(start_context, opening),
        'opening_portfolio_equity': begin, 'closing_holdings_market_value': holdings_market_value(end_context, closing),
        'closing_portfolio_equity': end,
        'period_pnl': period_profit(active, opening, closing,
            owner_contributions_minor=contributions, owner_withdrawals_minor=withdrawals)}
    ratios = [simple_return(active, opening_equity_minor=begin.value_minor,
        closing_equity_minor=end.value_minor, owner_contributions_minor=contributions,
        owner_withdrawals_minor=withdrawals)]
    if has_twr:
        boundary_gaps = []
        # 独立子期可以在整期快照缺失时计算；两者都有原件时端点必须相同。
        if parts and begin.value_minor is not None and parts[0].opening_equity_minor is not None and begin.value_minor != parts[0].opening_equity_minor:
            boundary_gaps.append('return_endpoint_opening_conflict')
        if parts and end.value_minor is not None and parts[-1].closing_equity_minor is not None and end.value_minor != parts[-1].closing_equity_minor:
            boundary_gaps.append('return_endpoint_closing_conflict')
        expected_dates = _ids(catalog.get('expected_boundary_dates'), 'catalog.expected_boundary_dates')
        for on in expected_dates or ():
            _scalar(on, 'catalog.expected_boundary_dates', 'date')
        if expected_dates is None:
            boundary_gaps.append('return_boundary_catalog_missing')
        elif tuple(border.on for border in borders) != expected_dates:
            boundary_gaps.append('return_boundary_catalog_coverage_mismatch')
        for total, field in ((contributions, 'contribution_minor'), (withdrawals, 'withdrawal_minor')):
            values = [getattr(border, field) for border in borders]
            if total is not None and all(value is not None for value in values) and sum(values) != total:
                boundary_gaps.append('owner_flow_boundary_total_mismatch:' + field)
        twr = time_weighted_return(replace(active, coverage_complete=False) if boundary_gaps else active,
                                   parts, borders)
        if boundary_gaps:
            twr = RatioMetric(twr.context, twr.metric_name, twr.basis, twr.formula_id, None,
                              tuple(dict.fromkeys((*twr.coverage_gaps, *boundary_gaps))))
        ratios.append(twr)
    return metrics, ratios, {}


def _history(context, records, catalog, expected, directory, prices, gaps):
    if context.as_of is not None:
        _invalid('history_moving_average必须使用完整期间')
    row = _mapping(records.get('opening'), 'opening', missing=True)
    _metadata(row, context, gaps, 'opening', on=context.period_start)
    identity = _identity(row, directory, gaps, 'opening')
    if len(expected) != 1 or expected != (identity[0],):
        gaps.append('history_single_instrument_catalog_required')
    quantity = _scalar(row.get('quantity'), 'opening.quantity', 'decimal')
    amounts = {key: _scalar(row.get(key), 'opening.' + key, 'money', nonnegative=True)
               for key in ('remaining_cost_minor', 'cash_minor', 'payables_minor')}
    receivables = _rows(row.get('dividend_receivables'), 'opening.dividend_receivables')
    mapped_receivables = None if receivables is None else []
    for dividend in receivables or ():
        name = _scalar(dividend.get('dividend_id'), 'dividend.dividend_id', 'text')
        amount = _scalar(dividend.get('amount_minor'), 'dividend.amount_minor', 'money', nonnegative=True)
        if name is None or amount is None:
            gaps.append('opening_dividend_receivable_incomplete')
        mapped_receivables.append(DividendReceivable(name or '', amount))
    opening = OpeningPosition(row.get('subject_id'), row.get('currency'), identity[0] or '',
        row.get('on') or context.period_start, quantity, **amounts,
        dividend_receivables=None if mapped_receivables is None else tuple(mapped_receivables),
        unit_price=_price(identity, context.period_start, context.currency, context.data_scope, prices))
    raw_events = _rows(records.get('events'), 'events')
    if raw_events is None:
        gaps.append('events_missing')
    events = []
    for raw in raw_events or ():
        _metadata(raw, context, gaps, 'event')
        _scalar(raw.get('event_id'), 'event.event_id', 'text')
        _scalar(raw.get('kind'), 'event.kind', 'text')
        _scalar(raw.get('on'), 'event.on', 'date')
        if raw.get('on') is None:
            gaps.append('event_on_missing')
        if raw.get('instrument_id') != identity[0]:
            gaps.append('event_instrument_mismatch')
        for key, exact in (('share_class_id', identity[1]), ('channel_id', identity[2])):
            if raw.get(key) is not None and raw[key] != exact:
                gaps.append('event_' + key + '_mismatch')
        values = {key: _scalar(raw.get(key), 'event.' + key, 'decimal' if key in ('quantity', 'split_factor') else
                              'money' if key in ('gross_minor', 'fee_minor') else 'text', nonnegative=True)
                  for key in ('quantity', 'gross_minor', 'fee_minor', 'fee_id', 'fee_basis', 'dividend_id', 'split_factor')}
        events.append(HoldingEvent(raw.get('event_id') or '', _scalar(raw.get('sequence'), 'event.sequence', 'integer'),
            raw.get('on') or context.period_start, raw.get('subject_id'), raw.get('currency'),
            raw.get('instrument_id') or '', raw.get('kind') or '', **values))
    coverage = _mapping(records.get('coverage'), 'coverage', missing=True)
    for key, exact in (('period_start', context.period_start), ('period_end', context.period_end)):
        _scalar(coverage.get(key), 'coverage.' + key, 'date')
        if coverage.get(key) != exact:
            gaps.append('coverage_' + key + '_missing_or_mismatch')
    event_ids = _ids(catalog.get('expected_event_ids'), 'catalog.expected_event_ids')
    own_events = _ids(coverage.get('expected_event_ids'), 'coverage.expected_event_ids')
    if own_events is not None and own_events != event_ids:
        gaps.append('coverage_expected_event_catalog_mismatch')
    sequence = _scalar(coverage.get('first_sequence'), 'coverage.first_sequence', 'integer')
    if sequence is None:
        gaps.append('first_sequence_missing')
    elif sequence < 1:
        _invalid('coverage.first_sequence必须为正整数')
    flags = {key: _scalar(coverage.get(key), 'coverage.' + key, 'bool') is True
             for key in ('transactions_complete', 'entitlements_complete', 'corporate_actions_complete', 'fees_complete')}
    cost_method = _scalar(coverage.get('cost_method'), 'coverage.cost_method', 'text')
    active = replace(context, coverage_complete=False) if gaps else context
    result = reconstruct_history(active, opening, tuple(events), HistoryCoverage(
        coverage.get('period_start') or context.period_start, coverage.get('period_end') or context.period_end,
        event_ids, sequence or 1, **flags, cost_method=cost_method or ''),
        closing_unit_price=_price(identity, context.period_end, context.currency, context.data_scope, prices))
    return dict(result.metrics), [], {'quantity_decimal': str(result.quantity_decimal) if result.quantity_decimal is not None else None,
        'event_trace': [dict(value) for value in result.event_trace], 'instrument_id': result.instrument_id,
        'cost_method': 'moving_average'}


def returns_report(context: MetricContext, records: dict, catalog: dict, provenance: dict) -> dict:
    """只消费已受信的 Base 输入；版本真实性/撤回/HMAC由调用方守卫。

    缺事实、清单或来源绑定返回 null/unavailable；类型和日期结构非法为422。
    provenance每个引用必须同时在context.input_refs和source_versions中绑定。
    ratios是独立只读报告，不进入金额registry，也不产生金融执行授权。
    """
    if not isinstance(context, MetricContext):
        _invalid('需要已验证MetricContext')
    records, catalog, provenance = (_mapping(records, 'records'), _mapping(catalog, 'catalog'),
                                    _mapping(provenance, 'provenance'))
    basis = records.get('accounting_basis')
    _scalar(basis, 'accounting_basis', 'text')
    if basis not in (None, 'equity', 'history_moving_average'):
        _invalid('不支持的accounting_basis')
    gaps = [] if basis is not None else ['accounting_basis_missing']
    basis = basis or 'equity'
    categories = ('opening', 'closing') if context.as_of is None else ('snapshot',)
    categories += ('holdings', 'prices', 'cash', 'receivables', 'payables', 'owner_flows', 'coverage')
    if basis == 'history_moving_average':
        categories += ('costs', 'events', 'fees', 'dividends', 'splits')
    gaps.extend(_provenance(context, provenance, categories))
    expected, directory = _catalog(catalog, context, gaps)
    prices = _prices(records)
    try:
        metrics, ratios, extras = (_history(context, records, catalog, expected, directory, prices, gaps)
            if basis == 'history_moving_average' else _equity(context, records, catalog, expected, directory, prices, gaps))
    except (ValueError, TypeError) as error:
        _invalid(str(error))
    metrics = {name: _unavailable(metric, gaps).to_dict() for name, metric in metrics.items()}
    ratio_reports = [RatioMetric(ratio.context, ratio.metric_name, ratio.basis, ratio.formula_id,
        None, tuple(dict.fromkeys((*ratio.coverage_gaps, *gaps)))).to_dict() if gaps else ratio.to_dict()
        for ratio in ratios]
    outputs = [*metrics.values(), *ratio_reports]
    known = sum(item['calculation_status'] == 'calculated' for item in outputs)
    status = 'calculated' if known == len(outputs) else 'partial' if known else 'unavailable'
    reasons = list(dict.fromkeys([*gaps, *(gap for item in outputs for gap in item['coverage_gaps'])]))
    return {'schema_version': 1, 'subject_id': context.subject_id, 'currency': context.currency,
        'data_scope': context.data_scope, 'as_of': context.as_of,
        'period_start': context.period_start, 'period_end': context.period_end,
        'accounting_basis': basis, 'calculation_status': status, 'metrics': metrics,
        'ratio_reports': ratio_reports, 'coverage_gaps': reasons, 'reasons': reasons,
        'summary': {'calculation_status': status, 'known_results': known,
                    'unavailable_results': len(outputs) - known, 'reasons': reasons},
        'execution_authority': False, **extras}
