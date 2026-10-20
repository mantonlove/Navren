"""签名持仓来源中的数字哈希不能被当成联系方式；尾部凭据仍须拒绝。"""
import unittest

from agent_runtime.data_boundary import BoundaryError, CloudDataBoundary


class SignedHoldingReferenceBoundaryTests(unittest.TestCase):
    workspace = 'ab' + '13800138000' + 'c' * 19

    def reference(self, product, account='main'):
        return ('holdings/' + self.workspace + '/acct-' +
                self.workspace[:12] + '-' + account + '/' + product)

    def test_numeric_hashes_round_trip_without_changing_source(self):
        for account in ('main', 'savings'):
            for product in ('SIM-BOND-90', 'SIM-CASH-01', 'fund:006210'):
                with self.subTest(account=account, product=product):
                    boundary = CloudDataBoundary()
                    source = self.reference(product, account)
                    projected = boundary._contract_string(source, identifier=True, prose=False)
                    self.assertNotIn(self.workspace, projected)
                    self.assertEqual(boundary._restore(projected), source)

    def test_product_tail_credentials_and_contacts_remain_blocked(self):
        for product in ('SIM-13800138000', 'sk-abcdefghijklmnop'):
            with self.subTest(product=product), self.assertRaises(BoundaryError):
                CloudDataBoundary()._contract_string(
                    self.reference(product), identifier=True, prose=False)

    def test_regular_user_contact_and_secret_filter_is_unchanged(self):
        for text in ('手机号13800138000', 'password=abcdefg', 'sk-abcdefghijklmnop'):
            with self.subTest(text=text):
                self.assertIn('[本地已屏蔽]', CloudDataBoundary().user_text(text))

    def test_statement_period_source_preserves_numeric_workspace_hash(self):
        workspace = '13800138000a' + 'c' * 20
        for period in ('2026', '2026-09'):
            with self.subTest(period=period):
                boundary = CloudDataBoundary()
                source = 'statement/' + workspace + '/' + period
                projected = boundary._contract_string(source, identifier=True, prose=False)
                self.assertEqual(boundary._restore(projected), source)
                self.assertNotIn(workspace, projected)

    def test_obligation_source_hash_is_distinct_from_sensitive_record_tail(self):
        for record in ('13800138000a-installment-0',
                       'mandate-13800138000a-video', 'e1-13800138000a'):
            with self.subTest(record=record):
                boundary = CloudDataBoundary()
                source = 'commitments/' + record
                projected = boundary._contract_string(source, identifier=True, prose=False)
                self.assertEqual(boundary._restore(projected), source)
        with self.assertRaises(BoundaryError):
            CloudDataBoundary()._contract_string(
                'commitments/13800138000a-loan-13800138000',
                identifier=True, prose=False)


if __name__ == '__main__':
    unittest.main()
