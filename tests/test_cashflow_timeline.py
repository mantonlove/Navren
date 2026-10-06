import unittest

from cashflow_timeline import build_cashflow_timeline, validate_horizon_days


def empty_archive():
    names = (
        'loans', 'loan_installments', 'cards', 'credit_statements', 'subscriptions',
        'mandates', 'beneficiaries', 'scheduled_payments', 'orders', 'fund_holds', 'operations',
    )
    return {
        'as_of': '2026-03-15', 'version': 7,
        'accounts': [
            {'id': 'demo-main', 'name': '日常账户', 'currency': 'CNY', 'balance_minor': 100_000},
            {'id': 'demo-savings', 'name': '储蓄账户', 'currency': 'CNY', 'balance_minor': 60_000},
        ],
        'profile': {'reserve_minor': 20_000},
        'records': {name: [] for name in names},
    }


class CashflowTimelineTest(unittest.TestCase):
    def setUp(self):
        self.archive = empty_archive()
        records = self.archive['records']
        records['loans'] = [{'id': 'loan-1', 'repayment_account_id': 'demo-main'}]
        records['loan_installments'] = [{
            'id': 'installment-1', 'loan_id': 'loan-1', 'status': 'overdue',
            'due_on': '2026-03-14', 'principal_minor': 9_000,
            'interest_minor': 800, 'fees_minor': 200, 'paid_minor': 0,
        }]
        records['cards'] = [{'id': 'card-1', 'account_id': 'demo-main'}]
        records['credit_statements'] = [{
            'id': 'statement-1', 'card_id': 'card-1', 'status': 'open',
            'due_on': '2026-03-15', 'total_due_minor': 5_000, 'paid_minor': 1_000,
        }]
        records['subscriptions'] = [{
            'id': 'subscription-1', 'mandate_id': 'mandate-linked',
            'merchant_status': 'active', 'next_renewal_on': '2026-01-15',
            'cycle': 'monthly', 'amount_minor': 2_000, 'account_id': 'demo-main',
            'plan_name': '云服务',
        }]
        records['mandates'] = [
            {'id': 'mandate-linked', 'status': 'active', 'account_id': 'demo-main',
             'next_charge_date': '2026-03-16', 'amount_minor': 2_000, 'merchant': '云服务'},
            {'id': 'mandate-independent', 'status': 'active', 'account_id': 'demo-main',
             'next_charge_date': '2026-04-04', 'amount_minor': 500, 'merchant': '健身房'},
        ]
        records['beneficiaries'] = [{'id': 'person-1', 'display_name': '林同学'}]
        records['scheduled_payments'] = [
            {'id': 'monthly-1', 'status': 'active', 'next_due_on': '2026-02-01',
             'end_on': None, 'frequency': 'monthly', 'account_id': 'demo-main',
             'amount_minor': 3_000, 'beneficiary_id': 'person-1'},
            {'id': 'once-1', 'status': 'active', 'next_due_on': '2026-04-02',
             'end_on': None, 'frequency': 'once', 'account_id': 'demo-main',
             'amount_minor': 9_000, 'beneficiary_id': 'person-1'},
            {'id': 'draft-1', 'status': 'draft', 'next_due_on': '2026-04-03',
             'end_on': None, 'frequency': 'once', 'account_id': 'demo-main',
             'amount_minor': 99_000, 'beneficiary_id': 'person-1'},
        ]
        records['orders'] = [{
            'id': 'redeem-1', 'status': 'pending_settlement', 'settlement_date': '2026-04-02',
            'account_id': 'demo-main', 'amount_minor': 7_000,
        }]
        records['fund_holds'] = [
            {'id': 'hold-1', 'status': 'held', 'account_id': 'demo-main',
             'amount_minor': 5_000, 'expires_on': '2026-03-20'},
            {'id': 'hold-draft', 'status': 'proposed', 'account_id': 'demo-main',
             'amount_minor': 88_000},
        ]
        records['operations'] = [{
            'id': 'op-1', 'state': 'pending_confirmation', 'action': 'purchase',
            'payload': {'account_id': 'demo-main', 'amount_minor': 77_000},
        }]

    def test_recurrences_catch_up_once_and_deduplicate_linked_mandate(self):
        result = build_cashflow_timeline(self.archive, 90, workspace_id='workspace-demo')
        self.assertEqual(result['workspace_id'], 'workspace-demo')
        subscription_events = [
            event for day in result['timeline'] for event in day['events']
            if event['kind'] == 'subscription_renewal'
        ]
        self.assertEqual([event['scheduled_date'] for event in subscription_events], [
            '2026-02-15', '2026-03-15', '2026-04-15', '2026-05-15',
        ])
        mandate_events = [
            event for day in result['timeline'] for event in day['events']
            if event['kind'] == 'bank_mandate'
        ]
        self.assertEqual([event['evidence_id'] for event in mandate_events], ['mandate-independent'])
        self.assertEqual(subscription_events[0]['date'], '2026-03-15')
        self.assertEqual(subscription_events[0]['date_status'], 'candidate')

    def test_daily_net_balances_reservations_and_confirmation_boundaries(self):
        result = build_cashflow_timeline(self.archive, 90)
        by_date = {row['date']: row for row in result['timeline']}
        march = next(a for a in by_date['2026-03-15']['accounts'] if a['account_id'] == 'demo-main')
        self.assertEqual(march['confirmed_outflow_minor'], 17_000)
        self.assertEqual(march['balance_after_confirmed_minor'], 83_000)
        self.assertEqual(march['available_after_confirmed_minor'], 58_000)
        self.assertEqual(march['balance_if_candidates_occur_minor'], 79_000)
        self.assertEqual(march['available_if_candidates_occur_minor'], 54_000)

        release = next(a for a in by_date['2026-03-21']['accounts'] if a['account_id'] == 'demo-main')
        self.assertEqual(release['released_reservation_minor'], 5_000)
        self.assertEqual(release['available_after_confirmed_minor'], 63_000)

        april = next(a for a in by_date['2026-04-02']['accounts'] if a['account_id'] == 'demo-main')
        self.assertEqual(april['confirmed_inflow_minor'], 7_000)
        self.assertEqual(april['confirmed_outflow_minor'], 9_000)
        self.assertEqual(april['balance_after_confirmed_minor'], 78_000)
        self.assertEqual(april['available_after_confirmed_minor'], 58_000)

        main = next(a for a in result['accounts'] if a['account_id'] == 'demo-main')
        self.assertEqual(main['current_available_minor'], 75_000)
        self.assertEqual(main['minimum_available_after_confirmed_minor'], 52_000)
        self.assertEqual(result['reserve_unallocated_minor'], 0)
        excluded = result['not_included']
        self.assertFalse(excluded['proposed_fund_reservations'][0]['counted_in_projection'])
        self.assertFalse(excluded['unconfirmed_operations'][0]['counted_in_projection'])
        self.assertFalse(excluded['unconfirmed_scheduled_transfers'][0]['counted_in_projection'])
        self.assertEqual(result['totals']['confirmed_inflow_minor'], 7_000)
        self.assertEqual(result['totals']['candidate_outflow_minor'], 8_500)

    def test_horizon_and_reserve_allocation_are_explicit(self):
        self.assertEqual(build_cashflow_timeline(self.archive, 1)['horizon_days'], 1)
        self.assertEqual(build_cashflow_timeline(self.archive, 365)['horizon_days'], 365)
        for bad in (0, 366, True, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_horizon_days(bad)
        self.archive['accounts'] = [
            {'id': 'account-a', 'name': '甲账户', 'currency': 'CNY', 'balance_minor': 10_000},
            {'id': 'account-b', 'name': '乙账户', 'currency': 'CNY', 'balance_minor': 20_000},
        ]
        self.archive['records'] = {key: [] for key in self.archive['records']}
        result = build_cashflow_timeline(self.archive, 30)
        self.assertEqual(result['reserve_unallocated_minor'], 20_000)
        self.assertTrue(all(row['reserve_floor_minor'] == 0 for row in result['accounts']))

    def test_multiple_currencies_are_never_summed_together(self):
        self.archive['accounts'][1]['currency'] = 'USD'
        self.archive['records']['orders'].append({
            'id': 'usd-redemption', 'status': 'pending_settlement',
            'settlement_date': '2026-03-20', 'account_id': 'demo-savings',
            'amount_minor': 12_345,
        })
        result = build_cashflow_timeline(self.archive, 90)
        self.assertIsNone(result['currency'])
        self.assertIsNone(result['totals'])
        self.assertEqual(result['totals_by_currency']['CNY']['confirmed_outflow_minor'], 35_000)
        self.assertEqual(result['totals_by_currency']['USD']['confirmed_inflow_minor'], 12_345)
        usd_event = next(event for day in result['timeline'] for event in day['events']
                         if event['evidence_id'] == 'usd-redemption')
        self.assertEqual(usd_event['currency'], 'USD')
        self.assertEqual(result['reserve_unallocated_minor'], 0)


if __name__ == '__main__':
    unittest.main()
