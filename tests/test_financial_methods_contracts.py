import unittest
from dataclasses import replace
from decimal import Decimal
from financial_methods.contracts import MetricContext, money_metric, decimal_value, require_minor


def context(**changes):
    base = dict(subject_id="account-A", currency="CNY", data_scope="navren_synthetic",
                as_of="2026-10-03", input_refs=("snapshot:A",), source_versions=(("bank", "v1"),))
    return MetricContext(**{**base, **changes})


class ContractTests(unittest.TestCase):
    def test_zero_and_missing_are_distinct(self):
        zero = money_metric(context(), "cash", "ledger_cash_balance", "f/1", 0)
        missing = money_metric(context(), "cash", "ledger_cash_balance", "f/1", None,
                               status="unavailable", gaps=("missing_balance",))
        self.assertEqual(zero.value_minor, 0)
        self.assertIsNone(missing.value_minor)
        self.assertNotEqual(zero.metric_id, missing.metric_id)

    def test_serialized_contract_and_stable_identity(self):
        m = money_metric(context(), "cash", "ledger_cash_balance", "f/1", 123)
        self.assertEqual(m.metric_id, money_metric(context(), "cash", "ledger_cash_balance", "f/1", 123).metric_id)
        self.assertEqual(m.to_dict()["source_versions"], {"bank": "v1"})
        for change in (dict(subject_id="account-B"), dict(as_of="2026-10-04"), dict(source_versions=(("bank", "v2"),))):
            self.assertNotEqual(m.metric_id, money_metric(replace(context(), **change), "cash", "ledger_cash_balance", "f/1", 123).metric_id)

    def test_period_and_asof_exclusive_and_dates_strict(self):
        for changes in (dict(period_start="2026-10-03", period_end="2026-10-04"), dict(as_of="2026-1-1"),
                        dict(as_of=None), dict(currency="cny"), dict(input_refs=()), dict(source_versions=()),
                        dict(input_refs="evidence"), dict(source_versions={"aa": "v1"})):
            with self.assertRaises(ValueError):
                context(**changes)

    def test_missing_and_boolean_are_not_amounts(self):
        for value in (None, True, 1.0, "1"):
            with self.assertRaises(ValueError):
                require_minor(value, "amount")
        for value in (None, True, 0.1, "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                decimal_value(value, "nav")
        self.assertEqual(decimal_value("1.2345", "nav"), Decimal("1.2345"))

    def test_incomplete_scope_not_full_total(self):
        m = money_metric(context(coverage_complete=False), "total", "market_value", "f/1", 100)
        self.assertIsNone(m.value_minor)
        self.assertIn("input_coverage_incomplete", m.coverage_gaps)
        with self.assertRaises(ValueError):
            money_metric(context(), "total", "market_value", "f/1", None)


if __name__ == "__main__":
    unittest.main()
