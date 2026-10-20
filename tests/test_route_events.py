"""实际宿主与AG-UI编码的路由生命周期；提供器为离线边界注入。"""
import json
from pathlib import Path
import tempfile
import unittest

from agent_runtime.complex_agent import ComplexConversationAgent, AIPingIntentJudge
from agent_runtime.events import RuntimeEvents
from agent_runtime.jev_client import JevError
from finance_gateway import FinanceGateway
from test_complex_agent import Judge, Primary, NeverReviewer


def route_activities(chunks):
    events = [json.loads(line[6:]) for chunk in chunks for line in chunk.decode().splitlines()
              if line.startswith('data: ')]
    return [event['value'] for event in events if event.get('name') == 'navren.activity'
            and event.get('value', {}).get('name') == 'intent_route']


class RouteEventsTests(unittest.TestCase):
    def test_real_process_encodes_one_start_and_one_terminal_for_each_outcome(self):
        cases = (
            (Judge(), 'completed', None),
            (Judge(malformed=True), 'failed', None),
            (Judge(error=JevError('timeout', {'request_sent': True, 'usage': None})),
             'failed', 'MODEL_USAGE_UNVERIFIED'),
            (Judge(error=JevError('model_version_mismatch', {'request_sent': True,
                 'usage': {'input_tokens': 10, 'output_tokens': 1}})),
             'failed', 'ROUTING_PROVIDER_INVALID'),
        )
        for judge, terminal, failure in cases:
            with self.subTest(terminal=terminal, failure=failure), tempfile.TemporaryDirectory() as root:
                gateway = FinanceGateway(Path(root)/'bank.sqlite3')
                workspace, _ = gateway.create_session()
                chunks = []
                events = RuntimeEvents(chunks.append, 'fixture-thread', 'fixture-route')
                primary = Primary()
                result = ComplexConversationAgent(primary=primary, reviewer=NeverReviewer(),
                    route_provider='native', judge=judge).process(gateway, workspace,
                        '查询周期扣费候选', on_event=events.progress)
                activities = route_activities(chunks)
                self.assertEqual([item['status'] for item in activities], ['started', terminal])
                self.assertTrue(all('summary' not in item for item in activities))
                if failure:
                    self.assertEqual(result['agent_runtime']['failure'], failure)
                    self.assertEqual(primary.messages, [])
                    with gateway.bank.connect() as db:
                        rows = db.execute("SELECT event FROM audit WHERE event IN "
                            "('agent_turn_completed','agent_intent_route_completed')").fetchall()
                    self.assertEqual(len(rows), 2)

    def test_pre_admission_and_deterministic_bypass_do_not_emit_started(self):
        with tempfile.TemporaryDirectory() as root:
            gateway = FinanceGateway(Path(root)/'bank.sqlite3')
            workspace, _ = gateway.create_session()
            for text, budget in (('你好', 100000), ('查询余额', 100000),
                                 ('查询周期扣费候选', 100)):
                with self.subTest(text=text):
                    primary = Primary()
                    primary.billing_mode = 'paid'
                    forbidden = lambda *args, **kwargs: self.fail('router credential/network I/O')
                    chunks = []
                    events = RuntimeEvents(chunks.append, 'fixture-thread', 'fixture-route')
                    ComplexConversationAgent(primary=primary, reviewer=NeverReviewer(),
                        route_provider='native', max_tokens=budget,
                        judge=AIPingIntentJudge(urlopen=forbidden,key_loader=forbidden)).process(
                            gateway, workspace, text, on_event=events.progress)
                    self.assertEqual(route_activities(chunks), [])

    def test_route_summary_cannot_leak_arbitrary_payload_or_change_skill_policy(self):
        base = {'kind':'activity','call_id':'intent-route','activity_kind':'skill',
                'name':'intent_route','label':'业务目标识别','status':'started','attempt':1}
        events = RuntimeEvents(lambda value: None, 'fixture-thread', 'fixture-route')
        self.assertIsNotNone(events._activity_value(base))
        for summary in ({'reason':'private-input'}, {'provider':'jev'},
                        {'skills':['subscriptions']}, {'skill':'subscriptions'},
                        {'probability':.99}, {'account':'synthetic-private'}):
            with self.subTest(summary=summary):
                self.assertIsNone(events._activity_value({**base,'summary':summary}))
        self.assertIsNone(events._activity_value({**base,'name':'arbitrary_route'}))
        self.assertIsNotNone(events._activity_value({**base,'name':'load_skill',
                                                  'summary':{'skill':'subscriptions'}}))

if __name__ == '__main__':
    unittest.main()
