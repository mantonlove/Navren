"""独立冻结收益 oracle 经受信输入映射后的验收；不启动服务或模型。"""
import copy
import json
from pathlib import Path
import unittest

from base_runtime.finance_returns import returns_report
from financial_methods.contracts import MetricContext
from simulated_bank import BankError


ROOT = Path(__file__).resolve().parents[1]
ORACLES = json.loads((ROOT / 'outputs/window-progress/finance/implementation/adapter-oracles.json').read_text())
CASES = {case['case_id']: case for case in ORACLES['cases']}
CATEGORIES = ('opening', 'closing', 'snapshot', 'holdings', 'prices', 'cash', 'receivables',
              'payables', 'owner_flows', 'coverage', 'costs', 'events', 'fees', 'dividends', 'splits')


def _path_item(value, key):
    if isinstance(value, list):
        return value[int(key)] if key.isdigit() else next(row for row in value if row.get('event_id') == key)
    return value[key]


def oracle_inputs(case_id):
    case = CASES[case_id]
    data = oracle_inputs(case['base_case']) if 'base_case' in case else copy.deepcopy(case['inputs'])
    for path, replacement in case.get('mutation', {}).items():
        pieces = path.split('.')
        target = data
        for piece in pieces[:-1]:
            target = _path_item(target, piece)
        if isinstance(target, list):
            target[int(pieces[-1])] = copy.deepcopy(replacement)
        else:
            target[pieces[-1]] = copy.deepcopy(replacement)
    return data


def trusted_case(case_id):
    """产品/事件目录来自冻结 oracle 的独立清单，不由当前返回行反推。"""
    records = oracle_inputs(case_id)
    context_fields = copy.deepcopy(ORACLES['contexts'][records.pop('context_ref')])
    refs = {category: ['fixture:' + category] for category in CATEGORIES}
    context_fields['input_refs'] = list(context_fields['input_refs']) + [r[0] for r in refs.values()]
    source_versions = dict(context_fields['source_versions'])
    # 冻结纯函数原件以fixture命名版本；受信适配显式绑定其adapter:fixture引用。
    source_versions['adapter:fixture'] = source_versions['fixture']
    source_versions.update({r[0]: 'fixture-v1' for r in refs.values()})
    context_fields['source_versions'] = tuple(source_versions.items())
    context = MetricContext(**context_fields)
    history = 'instrument_id' in records.get('opening', {})
    records['accounting_basis'] = 'history_moving_average' if history else 'equity'
    # B13的纯函数 scalar fixture 转为同对象/同期间的已确认零持仓现金快照。
    if 'opening_equity_minor' in records:
        for label, on in (('opening', context.period_start), ('closing', context.period_end)):
            records[label] = {'subject_id': context.subject_id, 'currency': context.currency,
                'on': on, 'holdings': [], 'expected_instrument_ids': [],
                'cash_minor': records.pop(label + '_equity_minor'), 'receivables_minor': 0,
                'payables_minor': 0, 'coverage_complete': True}
    has_instrument = history or any(records.get(key, {}).get('holdings') for key in ('snapshot', 'opening', 'closing'))
    catalog = {'expected_instrument_ids': ['P/share'] if has_instrument else [],
        'instruments': [{'instrument_id': 'P/share', 'share_class_id': 'share', 'channel_id': 'fixture-bank'}]
                       if has_instrument else []}
    if history:
        # 清单明确来自原件的 coverage 声明，刻意不读取 events 当前返回行。
        catalog['expected_event_ids'] = copy.deepcopy(records['coverage']['expected_event_ids'])
    if 'subperiods' in records:
        # 独立冻结边界目录；不能从变更后的boundaries或subperiods反推。
        catalog['expected_boundary_dates'] = ['2026-10-20']
    prices = []
    if history:
        opening = records['opening']
        opening.update(share_class_id='share', channel_id='fixture-bank')
        for on, price in ((context.period_start, opening.pop('unit_price')),
                          (context.period_end, records.pop('closing_unit_price'))):
            prices.append({'instrument_id': 'P/share', 'share_class_id': 'share',
                'channel_id': 'fixture-bank', 'currency': context.currency, 'on': on, 'unit_price': price})
    else:
        for label in ('snapshot', 'opening', 'closing'):
            snapshot = records.get(label)
            if snapshot is None:
                continue
            for holding in snapshot['holdings']:
                holding.update(share_class_id='share', channel_id='fixture-bank')
                prices.append({'instrument_id': holding['instrument_id'], 'share_class_id': 'share',
                    'channel_id': 'fixture-bank', 'currency': holding['currency'], 'on': snapshot['on'],
                    'unit_price': holding.pop('unit_price')})
    records['prices'] = prices
    return context, records, catalog, refs


class ReturnsAdapterTests(unittest.TestCase):
    def report(self, case_id):
        return returns_report(*trusted_case(case_id))

    def error(self, fields):
        with self.assertRaises(BankError) as raised:
            returns_report(*fields)
        self.assertEqual(raised.exception.code, 'METRIC_ARGUMENTS_INVALID')
        self.assertEqual(raised.exception.status, 422)

    def assert_unavailable(self, result, fragment):
        self.assertEqual(result['calculation_status'], 'unavailable')
        self.assertTrue(all(m['value_minor'] is None for m in result['metrics'].values()))
        self.assertTrue(any(fragment in reason for reason in result['reasons']), result['reasons'])

    def test_equity_17800_is_distinct_from_holdings_10000(self):
        r = self.report('B01_equity_scope')
        self.assertEqual(r['metrics']['portfolio_equity']['value_minor'], 17800)
        self.assertEqual(r['metrics']['holdings_market_value']['value_minor'], 10000)
        self.assertEqual(r['calculation_status'], 'calculated')
        self.assertEqual(r['ratio_reports'], [])

    def test_internal_settlement_is_only_200_profit(self):
        r = self.report('B02_internal_settlement')
        self.assertEqual(r['metrics']['period_pnl']['value_minor'], 200)
        self.assertEqual(r['metrics']['opening_portfolio_equity']['value_minor'], 17800)
        self.assertEqual(r['metrics']['closing_portfolio_equity']['value_minor'], 18000)
        self.assertEqual(r['metrics']['period_pnl']['period_start'], '2026-10-06')
        self.assertEqual(r['metrics']['closing_portfolio_equity']['as_of'], '2026-11-06')

    def test_history_5950_and_each_frozen_minor_amount(self):
        r = self.report('B03_capitalized_buy_fee')
        self.assertEqual(r['calculation_status'], 'calculated')
        self.assertEqual(r['quantity_decimal'], '160')
        for name, value in CASES['B03_capitalized_buy_fee']['expected']['metrics'].items():
            self.assertEqual(r['metrics'][name]['value_minor'], value, name)
        sale = next(row for row in r['event_trace'] if row['event_id'] == 'sale')
        self.assertEqual(sale['allocated_sale_cost_minor'], 4420)
        self.assertEqual(r['metrics']['remaining_cost']['as_of'], '2026-11-06')
        self.assertIsNone(r['metrics']['remaining_cost']['period_start'])
        self.assertEqual(r['metrics']['period_pnl']['period_end'], '2026-11-06')
        self.assertIsNone(r['metrics']['period_pnl']['as_of'])

    def test_ex_dividend_is_not_a_loss_and_split_is_not_profit(self):
        dividend = self.report('B04_ex_dividend')
        self.assertEqual(dividend['metrics']['period_pnl']['value_minor'], 0)
        self.assertEqual(dividend['metrics']['closing_dividend_receivables']['value_minor'], 1000)
        split = self.report('B05_split_no_profit')
        self.assertEqual(split['quantity_decimal'], '200')
        self.assertEqual(split['metrics']['remaining_cost']['value_minor'], 20000)
        self.assertEqual(split['metrics']['period_pnl']['value_minor'], 0)

    def test_stock_unrealized_gain_is_not_period_gain(self):
        r = self.report('B06_opening_gain_not_period_gain')
        self.assertEqual(r['metrics']['unrealized_pnl']['value_minor'], 3000)
        self.assertEqual(r['metrics']['period_pnl']['value_minor'], 1000)

    def test_nav_embedded_fee_is_not_charged_twice(self):
        r = self.report('B07_nav_embedded_fee')
        self.assertEqual(r['metrics']['closing_cash']['value_minor'], 20000)
        self.assertEqual(r['metrics']['external_fee_expense']['value_minor'], 0)
        self.assertEqual(r['metrics']['period_pnl']['value_minor'], -100)
        self.assertTrue(any('management' in v for v in r['metrics']['period_pnl']['excluded_items']))

    def test_external_fee_reduces_cash_and_pnl_exactly_once(self):
        fields = trusted_case('B03_capitalized_buy_fee')
        fields[1]['events'].append({'event_id': 'external', 'sequence': 5, 'on': '2026-10-11',
            'subject_id': 'fixture-A', 'currency': 'CNY', 'instrument_id': 'P/share',
            'kind': 'fee', 'gross_minor': 100, 'fee_id': 'explicit-charge', 'fee_basis': 'external'})
        fields[2]['expected_event_ids'] = ['buy', 'sale', 'dividend', 'settle', 'external']
        fields[1]['coverage']['expected_event_ids'] = ['buy', 'sale', 'dividend', 'settle', 'external']
        r = returns_report(*fields)
        self.assertEqual(r['metrics']['closing_cash']['value_minor'], 13450)
        self.assertEqual(r['metrics']['external_fee_expense']['value_minor'], 100)
        self.assertEqual(r['metrics']['period_pnl']['value_minor'], 5850)
        self.assertEqual(r['metrics']['remaining_cost']['value_minor'], 17680)

    def test_twr_negative_0025_is_ratio_and_never_minor_currency(self):
        r = self.report('B08_twr_external_flow')
        twr = next(m for m in r['ratio_reports'] if m['metric_name'] == 'time_weighted_return')
        self.assertEqual(twr['unit'], 'ratio')
        self.assertEqual(twr['value_decimal'], '-0.0025')
        self.assertNotIn('value_minor', twr)
        self.assertTrue(all(m['unit'] == 'minor_currency' for m in r['metrics'].values()))
        self.assertFalse(any(m['metric_id'].startswith('ratio-') for m in r['metrics'].values()))

    def test_twr_known_aggregate_equity_endpoints_must_match(self):
        for field in ('opening', 'closing'):
            with self.subTest(field=field):
                fields = trusted_case('B08_twr_external_flow')
                ctx, records, _, _ = fields
                for label, on, cash in (('opening', ctx.period_start, 10000), ('closing', ctx.period_end, 19475)):
                    records[label] = dict(subject_id=ctx.subject_id, currency=ctx.currency, on=on,
                        holdings=[], cash_minor=cash, receivables_minor=0, payables_minor=0, coverage_complete=True)
                records.update(owner_contributions_minor=10000, owner_withdrawals_minor=0)
                good = returns_report(*fields)
                self.assertEqual(next(x for x in good['ratio_reports'] if x['metric_name']=='time_weighted_return')['value_decimal'], '-0.0025')
                records[field]['cash_minor'] += 1
                result = returns_report(*fields)
                twr = next(x for x in result['ratio_reports'] if x['metric_name']=='time_weighted_return')
                self.assertIsNone(twr['value_decimal'])
                self.assertIn('return_endpoint_'+field+'_conflict', twr['coverage_gaps'])
                self.assertIsNotNone(result['metrics']['period_pnl']['value_minor'])

    def test_missing_quantity_is_not_cost_divided_by_price_or_zero(self):
        fields = trusted_case('B09_unknown_quantity')
        fields[1]['snapshot']['holdings'][0]['remaining_cost_minor'] = 10000
        self.assert_unavailable(returns_report(*fields), 'quantity_missing:P/share')

    def test_missing_closing_price_keeps_only_independent_known_history(self):
        r = self.report('B10_missing_closing_price')
        self.assertEqual(r['calculation_status'], 'partial')
        self.assertEqual(r['quantity_decimal'], '160')
        for name, value in CASES['B10_missing_closing_price']['expected']['known_metrics'].items():
            self.assertEqual(r['metrics'][name]['value_minor'], value)
        for name in CASES['B10_missing_closing_price']['expected']['null_metrics']:
            self.assertIsNone(r['metrics'][name]['value_minor'])
        self.assertIn('closing_price_missing', r['reasons'])

    def test_missing_opening_price_does_not_erase_known_closing_stocks(self):
        fields = trusted_case('B03_capitalized_buy_fee')
        fields[1]['prices'][0]['unit_price'] = None
        r = returns_report(*fields)
        self.assertEqual(r['calculation_status'], 'partial')
        self.assertEqual(r['metrics']['closing_holdings_value']['value_minor'], 22400)
        self.assertEqual(r['metrics']['remaining_cost']['value_minor'], 17680)
        self.assertEqual(r['metrics']['unrealized_pnl']['value_minor'], 4720)
        self.assertIsNone(r['metrics']['period_pnl']['value_minor'])
        self.assertIn('opening_price_missing', r['reasons'])

    def test_missing_corporate_action_coverage_and_duplicate_fee_are_unavailable(self):
        self.assert_unavailable(self.report('B11_incomplete_corporate_actions'), 'corporate_actions_complete_required')
        self.assert_unavailable(self.report('B12_duplicate_fee'), 'duplicate_or_missing_fee_identity')

    def test_simple_return_with_any_owner_flow_is_unavailable(self):
        r = self.report('B13_simple_return_with_flow')
        ratio = r['ratio_reports'][0]
        self.assertIsNone(ratio['value_decimal'])
        self.assertIn('cashflows_require_subperiod_valuations', ratio['coverage_gaps'])
        fields = trusted_case('B02_internal_settlement')
        fields[1]['owner_contributions_minor'] = fields[1]['owner_withdrawals_minor'] = 10000
        ratio = returns_report(*fields)['ratio_reports'][0]
        self.assertIsNone(ratio['value_decimal'])
        self.assertIn('cashflows_require_subperiod_valuations', ratio['coverage_gaps'])

    def test_twr_boundary_mismatch_and_missing_boundary_have_no_ratio(self):
        r = self.report('B14_twr_boundary_mismatch')
        ratio = next(m for m in r['ratio_reports'] if m['metric_name'] == 'time_weighted_return')
        self.assertIsNone(ratio['value_decimal'])
        self.assertIn('boundary_equity_does_not_reconcile', ratio['coverage_gaps'])
        fields = trusted_case('B08_twr_external_flow')
        fields[1].pop('boundaries')
        r = returns_report(*fields)
        ratio = next(m for m in r['ratio_reports'] if m['metric_name'] == 'time_weighted_return')
        self.assertIsNone(ratio['value_decimal'])
        self.assertIn('subperiod_boundary_coverage_mismatch', ratio['coverage_gaps'])

    def test_twr_cannot_hide_owner_flow_in_one_collapsed_subperiod(self):
        fields = trusted_case('B08_twr_external_flow')
        fields[1]['owner_contributions_minor'] = 10000
        fields[1]['owner_withdrawals_minor'] = 0
        fields[1]['subperiods'] = [{**fields[1]['subperiods'][0], 'end': '2026-11-06',
                                  'closing_equity_minor': 19475}]
        fields[1]['boundaries'] = []
        r = returns_report(*fields)
        twr = next(m for m in r['ratio_reports'] if m['metric_name'] == 'time_weighted_return')
        self.assertIsNone(twr['value_decimal'])
        self.assertIn('return_boundary_catalog_coverage_mismatch', twr['coverage_gaps'])
        self.assertIn('owner_flow_boundary_total_mismatch:contribution_minor', twr['coverage_gaps'])
        fields = trusted_case('B08_twr_external_flow')
        fields[2].pop('expected_boundary_dates')
        twr = next(m for m in returns_report(*fields)['ratio_reports'] if m['metric_name'] == 'time_weighted_return')
        self.assertIsNone(twr['value_decimal'])
        self.assertIn('return_boundary_catalog_missing', twr['coverage_gaps'])

    def test_catalog_does_not_shrink_to_returned_holdings_or_events(self):
        fields = trusted_case('B01_equity_scope')
        fields[1]['snapshot']['holdings'] = []
        self.assert_unavailable(returns_report(*fields), 'holding_identity_coverage_mismatch')
        fields = trusted_case('B03_capitalized_buy_fee')
        fields[1]['events'].pop()
        self.assert_unavailable(returns_report(*fields), 'event_identity_coverage_mismatch')
        fields[2].pop('expected_event_ids')
        self.assert_unavailable(returns_report(*fields), 'expected_event_identity_coverage_missing')

    def test_price_product_class_channel_currency_and_date_are_exact(self):
        for field, value in (('instrument_id', 'different'), ('share_class_id', 'other-share'),
                             ('channel_id', 'other-bank'), ('currency', 'USD'), ('on', '2026-10-07')):
            with self.subTest(field=field):
                fields = trusted_case('B01_equity_scope')
                fields[1]['prices'][0][field] = value
                # 任何旧的行内价格都不兜底。
                fields[1]['snapshot']['holdings'][0]['unit_price'] = '1.00'
                self.assert_unavailable(returns_report(*fields), 'price_missing:P/share')

    def test_duplicate_price_originals_are_not_selected_arbitrarily(self):
        fields = trusted_case('B01_equity_scope')
        fields[1]['prices'].append(copy.deepcopy(fields[1]['prices'][0]))
        self.assert_unavailable(returns_report(*fields), 'price_missing:P/share')

    def test_known_zero_quantity_does_not_require_a_price(self):
        fields = trusted_case('B01_equity_scope')
        fields[1]['snapshot']['holdings'][0]['quantity'] = '0'
        fields[1]['prices'] = []
        r = returns_report(*fields)
        self.assertEqual(r['metrics']['holdings_market_value']['value_minor'], 0)
        self.assertEqual(r['metrics']['portfolio_equity']['value_minor'], 7800)

    def test_holdings_can_be_known_while_cash_is_unknown(self):
        fields = trusted_case('B01_equity_scope')
        fields[1]['snapshot']['cash_minor'] = None
        r = returns_report(*fields)
        self.assertEqual(r['calculation_status'], 'partial')
        self.assertEqual(r['metrics']['holdings_market_value']['value_minor'], 10000)
        self.assertIsNone(r['metrics']['portfolio_equity']['value_minor'])
        self.assertIn('cash_minor_missing', r['reasons'])

    def test_provenance_missing_reference_and_version_do_not_sign_known_amount(self):
        for mode in ('missing_category', 'unbound_reference', 'missing_version'):
            with self.subTest(mode=mode):
                context, records, catalog, provenance = trusted_case('B01_equity_scope')
                if mode == 'missing_category':
                    provenance.pop('prices')
                elif mode == 'unbound_reference':
                    provenance['prices'] = ['not-bound']
                else:
                    context = MetricContext(context.subject_id, context.currency, context.data_scope,
                        as_of=context.as_of, input_refs=context.input_refs,
                        source_versions=tuple((key, value) for key, value in context.source_versions if key != 'fixture:prices'))
                self.assert_unavailable(returns_report(context, records, catalog, provenance), 'provenance_')

    def test_all_categories_and_full_context_survive_each_metric(self):
        fields = trusted_case('B03_capitalized_buy_fee')
        context = fields[0]
        r = returns_report(*fields)
        for metric in r['metrics'].values():
            self.assertEqual(metric['input_refs'], list(context.input_refs))
            self.assertEqual(metric['source_versions'], dict(context.source_versions))
            self.assertEqual(metric['subject_id'], context.subject_id)
            self.assertEqual(metric['currency'], context.currency)
            self.assertEqual(metric['data_scope'], context.data_scope)
            self.assertEqual(metric['contract_version'], '1.0.0')
            self.assertEqual(metric['formula_id'], 'returns.history.' + metric['metric_name'] + '.v1')
        self.assertFalse(r['execution_authority'])

    def test_legal_empty_source_and_null_facts_are_unavailable_not_422(self):
        context = trusted_case('B01_equity_scope')[0]
        self.assert_unavailable(returns_report(context, {}, {}, {}), 'accounting_basis_missing')
        fields = trusted_case('B03_capitalized_buy_fee')
        fields[1]['opening']['remaining_cost_minor'] = None
        self.assert_unavailable(returns_report(*fields), 'opening_remaining_cost_minor_missing')

    def test_same_subject_currency_scope_and_times_are_enforced(self):
        for field, value, reason in (('subject_id', 'other-user', 'subject_id_mismatch'),
                                   ('currency', 'USD', 'currency_mismatch'),
                                   ('data_scope', 'other-scope', 'scope_mismatch'),
                                   ('on', '2026-10-07', 'date_mismatch')):
            with self.subTest(field=field):
                fields = trusted_case('B01_equity_scope')
                fields[1]['snapshot'][field] = value
                self.assert_unavailable(returns_report(*fields), reason)

    def test_structural_bool_float_dates_types_and_unsupported_basis_are_422(self):
        changes = (('snapshot.cash_minor', True), ('snapshot.cash_minor', 1.5),
                   ('snapshot.on', '2026-02-30'), ('snapshot.coverage_complete', 'true'),
                   ('snapshot.holdings.0.quantity', 100.0), ('prices.0.unit_price', 1.2),
                   ('accounting_basis', 'imagined_basis'))
        for path, value in changes:
            with self.subTest(path=path):
                fields = trusted_case('B01_equity_scope')
                target = fields[1]
                pieces = path.split('.')
                for piece in pieces[:-1]:
                    target = _path_item(target, piece)
                target[pieces[-1]] = value
                self.error(fields)
        fields = trusted_case('B03_capitalized_buy_fee')
        fields[1]['events'][0]['sequence'] = True
        self.error(fields)
        fields = trusted_case('B01_equity_scope')
        fields[3]['prices'] = 'not-a-reference-list'
        self.error(fields)

    def test_input_snapshots_and_catalog_are_not_mutated(self):
        fields = trusted_case('B03_capitalized_buy_fee')
        before = copy.deepcopy(fields[1:])
        returns_report(*fields)
        self.assertEqual(fields[1:], before)


if __name__ == '__main__':
    unittest.main()
