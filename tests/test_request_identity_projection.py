"""匿名化不得吞掉转账动词、拼接金额或拆散同一当前账户的引用。"""
import unittest
from unittest.mock import patch

from agent_runtime.data_boundary import CloudDataBoundary
from agent_runtime.metric_claims import monetary_occurrences
from agent_runtime.payment_completion import bind_current_operation_account_references


class RequestIdentityProjectionTests(unittest.TestCase):
    request = '改成从备用账户给妈妈转100.00元，先查这张账户能否安排。'

    def test_transfer_verb_and_original_amount_survive_anonymization(self):
        with patch('agent_runtime.data_boundary.secrets.token_hex',
                   lambda n: 'a' * (2 * n - 1) + '1'):
            projected = CloudDataBoundary().user_text(self.request)
        self.assertIn('妈妈转100.00元', projected)
        self.assertNotIn('1100.00元', projected)

    def test_adjacent_person_alias_cannot_change_money_span(self):
        boundary = CloudDataBoundary()
        projected = boundary.user_text('给张三100.00元')
        self.assertNotIn('张三', projected)
        amounts = monetary_occurrences(projected)
        self.assertEqual([row['value_minor'] for row in amounts], [10000])

    def test_current_account_short_reference_matches_its_projected_name(self):
        boundary = CloudDataBoundary()
        rows = [{'id': 'test-savings', 'name': '正式验收模拟备用账户', 'currency': 'CNY'}]
        links = bind_current_operation_account_references(
            boundary, {'accounts': rows}, self.request)
        self.assertEqual(len(links), 1)
        self.assertIn(boundary._text(rows[0]['name']), boundary.user_text(self.request))
        rows.append({'id': 'test-other', 'name': '第二备用账户', 'currency': 'CNY'})
        self.assertEqual(bind_current_operation_account_references(
            CloudDataBoundary(), {'accounts': rows}, self.request), [])

    def test_cjk_adjacent_credentials_never_become_reversible_account_links(self):
        boundary = CloudDataBoundary()
        self.assertIsNone(boundary.register_current_account_reference(
            '备用账户', '备用账户sk-abcdefghijklmnop'))
        self.assertIn('[本地已屏蔽]', boundary.user_text('密钥是sk-abcdefghijklmnop请保密'))


if __name__ == '__main__':
    unittest.main()
