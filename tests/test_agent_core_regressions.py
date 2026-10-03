"""金额解析和配置比例的确定性回归测试，无需模型 API。"""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from agent_core import ASSET_VOLATILITY, QingzhouAgent


class AgentCoreRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with contextlib.redirect_stdout(io.StringIO()):
            cls.agent = QingzhouAgent()

    @staticmethod
    def products(*risks):
        return [
            {'name': f'测试产品{i + 1}', 'risk_level': risk, 'underlying': []}
            for i, risk in enumerate(risks)
        ]

    def test_amount_followed_by_chinese_or_horizon(self):
        cases = {
            '20万闲钱，3年不用': 200000,
            '20万3年不用，帮我配置': 200000,
            '有20万元闲钱': 200000,
            '20w闲钱': 200000,
            '20W，3年不用': 200000,
            '2.01万元，放半年': 20100,
            '200000元闲钱': 200000,
            '有200000': 200000,
            '500元，放1个月': 500,
            '10000000元': 10000000,
        }
        for text, amount in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.agent.extract_slots(text)['amount'], amount)
        slots = self.agent.extract_slots('20万3年不用，帮我配置')
        self.assertEqual(slots['horizon'], '3年')

    def test_unrelated_numbers_are_not_investment_amounts(self):
        for text in ['年化2.5%，3年不用', '风险等级R3', '20岁，3年不用', '-20万']:
            with self.subTest(text=text):
                self.assertIsNone(self.agent.extract_slots(text)['amount'])

    def test_equal_products_share_rounding_remainder(self):
        result = self.agent.calculate_allocation(
            {'risk_level': 'R2'}, self.products('R2', 'R2', 'R2'))
        ratios = [item['ratio'] for item in result['allocation']]
        self.assertEqual(sum(ratios), 100)
        self.assertLessEqual(max(ratios) - min(ratios), 1)
        self.assertEqual(result, self.agent.calculate_allocation(
            {'risk_level': 'R2'}, self.products('R2', 'R2', 'R2')))

    def test_all_risk_levels_and_product_counts_total_100(self):
        for risk in ASSET_VOLATILITY:
            for count in range(1, 7):
                with self.subTest(risk=risk, count=count):
                    result = self.agent.calculate_allocation(
                        {'risk_level': risk}, self.products(*([risk] * count)))
                    ratios = [item['ratio'] for item in result['allocation']]
                    self.assertEqual(sum(ratios), 100)
                    self.assertTrue(all(isinstance(r, int) and 0 <= r <= 100 for r in ratios))
        self.assertEqual(self.agent.calculate_allocation(
            {'risk_level': 'R2'}, [])['allocation'], [])

    def test_volatility_matches_final_mixed_allocation(self):
        result = self.agent.calculate_allocation(
            {'risk_level': 'R3'}, self.products('R4', 'R2', 'R3'))
        expected = sum(
            item['ratio'] / 100 * ASSET_VOLATILITY[item['risk']]
            for item in result['allocation']
        )
        self.assertEqual(sum(item['ratio'] for item in result['allocation']), 100)
        self.assertEqual(result['est_portfolio_vol'], f'{expected * 100:.1f}%')

    def test_chat_request_can_build_allocation_from_natural_amount(self):
        with patch.object(self.agent, 'call_llm', side_effect=RuntimeError('离线测试')), \
                patch.object(self.agent, 'write_model_log'):
            result = self.agent.process(
                '20万闲钱，3年不用，帮我配置', profile={'risk_level': 'R2'})
        self.assertEqual(result['agent_chain']['profile_snapshot']['amount'], 200000)
        self.assertTrue(result['products'])
        self.assertEqual(sum(item['ratio'] for item in result['allocation']['allocation']), 100)

    def test_market_news_does_not_become_personal_amount_or_horizon(self):
        for headline in ['10年期国债收益率下行至2.68%', '银行理财规模突破30万亿元',
                         '稳健理财人群关注短债基金', '市场\'双引号"与 & 百分比100%：测试']:
            text = '帮我分析这条市场热点对我的配置有什么影响：' + headline
            with self.subTest(headline=headline):
                self.assertEqual(self.agent.classify_intent(text)['intent'], 'market_interpretation')
                slots = self.agent.extract_slots(text)
                for field in ['amount', 'horizon', 'risk', 'goal']:
                    self.assertIsNone(slots[field])
        with patch.object(self.agent, 'call_llm', side_effect=RuntimeError('离线测试')), \
                patch.object(self.agent, 'write_model_log'):
            result = self.agent.process(
                '帮我分析这条市场热点对我的配置有什么影响：10年期国债收益率下行至2.68%',
                profile={'risk_level': 'R2'})
        self.assertEqual(result['intent'], 'market_interpretation')
        self.assertIsNone(result['agent_chain']['profile_snapshot']['horizon'])
        self.assertEqual(result['products'], [])


class ServerStartupRegression(unittest.TestCase):
    def test_text_server_imports_without_voice_dependency_and_accepts_port(self):
        env = {**os.environ, 'QINGZHOU_PORT': '18766'}
        result = subprocess.run(
            [sys.executable, '-S', '-B', '-c', 'import server; print(server.PORT)'],
            cwd=Path(__file__).resolve().parent.parent, env=env,
            capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), '18766')


if __name__ == '__main__':
    unittest.main()
