import unittest

from finance_agent import supports
from finance_capabilities import (is_account_balance_query, select_workflow_capabilities,
                                  workflow_capability_catalog)


class FinanceCapabilityCatalogTests(unittest.TestCase):
    def test_all_six_business_areas_route_through_one_catalog(self):
        cases = {
            "转账": "transfer",
            "分析9月账单": "statements",
            "对比模拟理财产品": "wealth",
            "信用卡挂失": "cards",
            "取消会员订阅": "subscriptions",
            "安排生日礼物并预留预算": "cross_scene",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIn(expected, select_workflow_capabilities(text))
                self.assertTrue(supports(text))

    def test_catalog_marks_every_write_as_confirmation_gated(self):
        catalog = workflow_capability_catalog()
        ids = {item["id"] for item in catalog}
        self.assertTrue({
            "transfer", "statements", "wealth", "cards", "subscriptions", "cross_scene",
        }.issubset(ids))
        self.assertIn("service_negotiation", ids)
        self.assertEqual({item["id"] for item in catalog if item["kind"] == "required_business_capability"},
                         {"transfer", "statements", "wealth", "cards", "subscriptions", "cross_scene"})
        self.assertTrue(all(item["execution_requires_user_confirmation"] for item in catalog))
        writes = [action for item in catalog for action in item["draft_actions"]]
        self.assertIn("transfer", writes)
        self.assertIn("purchase", writes)
        self.assertIn("cancel_subscription", writes)
        self.assertNotIn("transaction.execute", writes)

    def test_unrelated_chat_stays_outside_bank_router(self):
        self.assertFalse(supports("今天的天气怎么样？"))

    def test_colloquial_personal_balance_questions_route_to_wealth_without_product_pricing_false_positive(self):
        for text in ("我还有多少钱", "我有多少钱可以投资", "账户里还有多少钱", "我能动用多少钱", "我有多少现金"):
            with self.subTest(text=text):
                self.assertTrue(is_account_balance_query(text))
                self.assertIn("wealth", select_workflow_capabilities(text))
                self.assertTrue(supports(text))
        self.assertFalse(is_account_balance_query("这款产品多少钱"))
        self.assertFalse(supports("这款产品多少钱"))


if __name__ == "__main__":
    unittest.main()
