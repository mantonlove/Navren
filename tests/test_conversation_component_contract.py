import unittest
from agent_runtime import skills
from agent_runtime.tool_routing import TurnToolScope
from voice_gateway import speech_text, _component_speech_markup

class ConversationComponentContractTests(unittest.TestCase):
    def test_native_skill_is_discoverable_and_has_no_tools(self):
        item=next(x for x in skills.catalog() if x['name']=='conversation-components')
        self.assertEqual(item['tools'],[])
        loaded=skills.load_advisor(item['name'])
        self.assertIn('component-format.md',loaded['references'])
        self.assertIn('navren-compare',skills.load_advisor(item['name'],'component-format.md')['instructions'])
        scope=TurnToolScope();before=set(scope.names);scope.add_skill(item['name'])
        self.assertEqual(scope.names,before)
    def test_speech_keeps_values_and_conditions_for_all_components(self):
        for kind in ('facts','compare','steps','note'):
            with self.subTest(kind=kind):
                value=speech_text('```navren-'+kind+' 条件\n示例 | 1,234.56元 | 尚未执行，不能保证收益。\n```\n仍需核对来源。')
                self.assertNotIn('navren-',value)
                for span in ('条件','1,234.56元','尚未执行，不能保证收益。','仍需核对来源。'):self.assertIn(span,value)
    def test_unknown_unclosed_and_nested_markup_is_retained(self):
        values=('```navren-unknown 条件\n原文\n```','```navren-facts 条件\n原文','````python\n```navren-note\n内部示例\n```\n````')
        for value in values:self.assertEqual(_component_speech_markup(value),value)
    def test_ordinary_speech_behavior_is_unchanged(self):
        self.assertEqual(speech_text('建议。\n⚠️ 请确认风险。'),'建议。。注意： 请确认风险。')
        self.assertEqual(speech_text('比例1.25%，金额¥1,234.56。'),'比例百分之1.25，金额1234.56元。')

if __name__=='__main__':unittest.main()
