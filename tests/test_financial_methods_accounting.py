import unittest
from dataclasses import replace
from financial_methods.contracts import MetricContext
from financial_methods.accounting import BalanceComponent, CashMovement, balance_sheet, cash_statement


def context(point=False):
    return MetricContext("owner-A", "CNY", "navren_synthetic", as_of="2026-10-03" if point else None,
                         period_start=None if point else "2026-10-01", period_end=None if point else "2026-10-03",
                         input_refs=("source:A",), source_versions=(("ledger", "v1"),))


class AccountingTests(unittest.TestCase):
    def test_assets_minus_liabilities_not_investable_cash(self):
        rows = (BalanceComponent("cash", "owner-A", "CNY", "2026-10-03", "cash", 100000,
                                 "ledger_cash_balance", "cash:A", "v1"),
                BalanceComponent("fund", "owner-A", "CNY", "2026-10-03", "financial_asset", 50000,
                                 "market_value", "fund:A", "v1"),
                BalanceComponent("tractor", "owner-A", "CNY", "2026-10-03", "nonfinancial_asset", 200000,
                                 "carrying_value", "tractor:A", "v1"),
                BalanceComponent("loan", "owner-A", "CNY", "2026-10-03", "liability", 80000,
                                 "outstanding_principal", "loan:A", "v1"))
        result = balance_sheet(context(True), rows, expected_object_ids=("cash", "fund", "tractor", "loan"))
        self.assertEqual(result["total_assets"].value_minor, 350000)
        self.assertEqual(result["net_assets"].value_minor, 270000)
        self.assertEqual(result["cash_balance"].value_minor, 100000)
        for bad in (rows[:-1], (*rows, rows[0]), (replace(rows[0], currency="USD"), *rows[1:]),
                    (replace(rows[0], amount_minor=None), *rows[1:])):
            self.assertIsNone(balance_sheet(context(True), bad, expected_object_ids=("cash", "fund", "tractor", "loan"))["net_assets"].value_minor)

    def test_borrowing_repayment_refund_investment_and_transfer(self):
        def row(mid, kind, delta, account="bank", link=None):
            return CashMovement(mid, "owner-A", account, "CNY", "2026-10-02", kind, delta, mid, "v1", link)
        rows = (row("borrow", "loan_proceeds", 100000), row("repay", "loan_principal_repayment", -50000),
                row("interest", "loan_interest", -1000), row("bill", "expense", -10000),
                row("refund", "refund", 2000, link="bill"), row("buy", "investment_purchase", -20000),
                row("transfer-out", "internal_transfer", -10000, "bank", "pair1"),
                row("transfer-in", "internal_transfer", 10000, "wallet", "pair1"))
        # 期末 = 100000 + 100000 - 50000 - 1000 - 10000 + 2000 - 20000 = 121000。
        result = cash_statement(context(), 100000, 121000, rows)
        self.assertEqual(result["cash_income"].value_minor, 0)
        self.assertEqual(result["net_cash_expenses"].value_minor, 9000)
        self.assertEqual(result["loan_principal_net"].value_minor, 50000)
        self.assertEqual(result["investment_transaction_cash_net"].value_minor, -20000)
        self.assertEqual(result["total_cash_change"].value_minor, 21000)

    def test_reconciliation_coverage_and_wrong_classification_fail(self):
        row = CashMovement("dep", "owner-A", "bank", "CNY", "2026-10-02", "nonfinancial_depreciation", -1000, "dep", "v1")
        self.assertIsNone(cash_statement(context(), 1000, 0, (row,))["net_cash_expenses"].value_minor)
        empty = cash_statement(context(), 1000, 999, ())
        self.assertIn("cash_reconciliation_failed", empty["total_cash_change"].coverage_gaps)
        incomplete = cash_statement(replace(context(), coverage_complete=False), 0, 0, ())
        self.assertIsNone(incomplete["cash_income"].value_minor)

    def test_zero_complete_cash_flow_and_negative_net_expense(self):
        self.assertEqual(cash_statement(context(), 0, 0, ())["total_cash_change"].value_minor, 0)
        refund = CashMovement("refund", "owner-A", "bank", "CNY", "2026-10-02", "refund", 2000,
                              "refund", "v1", "previous-period-bill")
        r = cash_statement(context(), 0, 2000, (refund,))
        self.assertEqual(r["net_cash_expenses"].value_minor, -2000)
        self.assertEqual(r["cash_income"].value_minor, 0)

    def test_one_shot_inputs_keep_income_expense_and_balance_components(self):
        rows = (CashMovement("salary", "owner-A", "bank", "CNY", "2026-10-02",
                             "income", 100, "salary", "v1"),
                CashMovement("bill", "owner-A", "bank", "CNY", "2026-10-02",
                             "expense", -100, "bill", "v1"))
        for supplied in (rows, list(rows), iter(rows), (row for row in rows)):
            with self.subTest(input_type=type(supplied).__name__):
                result = cash_statement(context(), 1000, 1000, supplied)
                self.assertEqual(result["cash_income"].value_minor, 100)
                self.assertEqual(result["cash_expenses_gross"].value_minor, 100)
                self.assertEqual(result["total_cash_change"].value_minor, 0)
                self.assertIn("salary", result["cash_income"].input_refs)
                self.assertIn("bill", result["cash_income"].input_refs)
        components = (BalanceComponent("cash", "owner-A", "CNY", "2026-10-03", "cash",
                                       1000, "ledger_cash_balance", "cash:A", "v1"),
                      BalanceComponent("loan", "owner-A", "CNY", "2026-10-03", "liability",
                                       200, "outstanding_principal", "loan:A", "v1"))
        result = balance_sheet(context(True), (row for row in components),
                               expected_object_ids=("cash", "loan"))
        self.assertEqual(result["net_assets"].value_minor, 800)
        self.assertEqual(result["cash_balance"].value_minor, 1000)


if __name__ == "__main__":
    unittest.main()
