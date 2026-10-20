"""统一受信入口的离线验收草案；应用正式补丁后才能执行。"""
from pathlib import Path
import ast
import unittest
from unittest.mock import patch

from agent_runtime.agent_factory import create_agent
from agent_runtime.complex_agent import ComplexConversationAgent
from server import create_server

ROOT = Path(__file__).resolve().parents[1]


class AgentFactoryTests(unittest.TestCase):
    def test_default_off_and_fixed_shared_progressive_channels_without_io(self):
        with (patch('subprocess.run', side_effect=AssertionError('credential/process I/O')),
              patch('subprocess.check_output', side_effect=AssertionError('credential/process I/O')),
              patch('urllib.request.urlopen', side_effect=AssertionError('network I/O')),
              patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network I/O'))):
            for provider in ('off', 'native', 'jev'):
                agent = create_agent(route_provider=provider)
                self.assertIsInstance(agent, ComplexConversationAgent)
                self.assertEqual(agent.route_provider, provider)
                self.assertEqual(agent.tool_routing, 'progressive')
                self.assertEqual(agent.primary.endpoint, 'https://aiping.cn/api/v1/chat/completions')
                self.assertEqual(agent.primary.model_id, 'DeepSeek-V4-Flash')
                self.assertEqual(agent.reviewer.client.url, agent.primary.endpoint)
                self.assertEqual(agent.reviewer.model_id, 'GLM-5.3-Flash')
                self.assertEqual(agent.max_tokens, 100000)
                self.assertEqual(agent.timeout_seconds, 105)
        self.assertEqual(create_agent().route_provider, 'off')

    def test_invalid_configuration_does_not_allocate_server_or_database(self):
        for options in ({'agent_route_provider': 'active'},
                        {'agent_route_provider': True},
                        {'agent_route_provider': 'jev', 'conversation_agent': object()},
                        {'agent_route_provider': 'off', 'conversation_agent': object()}):
            with self.subTest(options=options), patch('server.NavrenHTTPServer') as http, patch('server.FinanceGateway') as bank:
                with self.assertRaises(ValueError):
                    create_server(**options)
                http.assert_not_called()
                bank.assert_not_called()

    def test_factory_does_not_accept_request_supplied_capability_or_channel_kwargs(self):
        for overrides in ({'tool_routing': 'full'}, {'allowed_tools': ['prepare_operation']},
                          {'provider': 'official'}, {'key': 'synthetic-not-a-credential'}):
            with self.subTest(fields=list(overrides)):
                with self.assertRaises(TypeError):
                    create_agent(**overrides)
        for budget in ({'max_tokens': True}, {'max_rounds': 0}, {'timeout_seconds': float('nan')}):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                create_agent(**budget)

    def test_three_entrypoints_use_same_trusted_selector(self):
        # 公开默认CLI必须委托已注册启动器；只有实际装配入口直接构造服务器。
        public_tree = ast.parse((ROOT / 'server.py').read_text())
        public_main = public_tree.body[-1]
        self.assertIsInstance(public_main, ast.If)
        self.assertEqual(ast.unparse(public_main.test), "__name__ == '__main__'")
        self.assertTrue(any(isinstance(node, ast.ImportFrom)
                            and node.module == 'scripts.serve_financial_frontend'
                            and any(alias.name == 'main' for alias in node.names)
                            for node in public_main.body))
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == 'main' for node in ast.walk(public_main)))
        self.assertFalse(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                             and node.func.id == 'create_server' for node in ast.walk(public_main)))
        for relative in ('scripts/serve_financial_frontend.py', 'scripts/serve_jev_agent.py'):
            tree = ast.parse((ROOT / relative).read_text())
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
            self.assertTrue(any(any(keyword.arg == 'agent_route_provider' for keyword in node.keywords)
                                and isinstance(node.func, ast.Name) and node.func.id == 'create_server'
                                for node in calls), relative)
        old_cli = (ROOT / 'scripts/serve_jev_agent.py').read_text()
        self.assertNotIn('JevConversationAgent', old_cli)
        self.assertNotIn("choices=['off', 'shadow', 'active']", old_cli)
