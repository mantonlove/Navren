"""技能注册、按需加载和产品权限边界；不以文本匹配代替行为验证。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_runtime import skills
from agent_runtime.bank_tools import BankTools, SPECS, native_tools
from finance_gateway import FinanceGateway
from simulated_bank import BankError


class SkillRegistryTest(unittest.TestCase):
    def test_catalog_and_native_enum_share_the_actual_registry(self):
        definitions = skills.definitions()
        expected = {item['name'] for item in definitions if item['role']=='advisor'}
        self.assertEqual({item['name'] for item in skills.catalog()}, expected)
        tool = next(t['function'] for t in native_tools() if t['function']['name']=='load_skill')
        self.assertEqual(set(tool['parameters']['properties']['name']['enum']), expected)
        self.assertNotIn('reviewer', expected)
        self.assertIn('product-research', expected)
        self.assertIn('knowledge-retrieval', expected)
        for item in definitions:
            with self.subTest(skill=item['name']):
                self.assertFalse(set(item['tools'])-set(SPECS))
                self.assertTrue(skills.load(item['name']))
                for reference in item['references']:
                    self.assertTrue(skills.load(item['name'], reference))

    def test_reviewer_remains_available_only_to_the_internal_review_chain(self):
        self.assertTrue(skills.load('reviewer'))
        with self.assertRaisesRegex(ValueError, 'reviewer_skill_forbidden'):
            skills.load_advisor('reviewer')

    def test_unknown_paths_and_cross_skill_references_are_not_read(self):
        for name in ('../banking','/etc/passwd','banking/SKILL.md','missing'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                skills.load(name)
        for reference in ('../SKILL.md','/etc/passwd','../../reviewer/SKILL.md','missing.md'):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                skills.load('banking', reference)

    def test_new_maintainer_skill_is_discovered_without_a_second_enum(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(skills, 'ROOT', Path(temp)):
            folder = Path(temp)/'new-workflow'; folder.mkdir()
            (folder/'SKILL.md').write_text('---\nname: new-workflow\ndescription: 新方法\n---\n具体方法。')
            self.assertEqual([item['name'] for item in skills.catalog()], ['new-workflow'])
            tool = next(t['function'] for t in native_tools() if t['function']['name']=='load_skill')
            self.assertEqual(tool['parameters']['properties']['name']['enum'], ['new-workflow'])
            self.assertIn('具体方法', skills.load_advisor('new-workflow')['instructions'])

    def test_malformed_metadata_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(skills, 'ROOT', Path(temp)):
            folder = Path(temp)/'sample'; folder.mkdir(); entry=folder/'SKILL.md'
            for content in ('missing frontmatter', '---\nname: another\ndescription: 描述\n---\n内容',
                            '---\nname: sample\ndescription: 描述\nmetadata:\n  role: admin\n---\n内容'):
                entry.write_text(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    skills.catalog()
            entry.unlink()
            outside = Path(temp)/'source.md'; outside.write_text('---\nname: sample\ndescription: 描述\n---\n内容')
            entry.symlink_to(outside)
            with self.assertRaises(ValueError): skills.catalog()


class SkillToolBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gateway = FinanceGateway(Path(self.temp.name)/'bank.sqlite3')
        self.workspace, _ = self.gateway.create_session()
        self.tools = BankTools(self.gateway, self.workspace, 'skill-test')
    def tearDown(self): self.temp.cleanup()

    def test_new_skill_and_reference_can_be_loaded_without_business_side_effects(self):
        before = self.gateway.overview(self.workspace)
        output = self.tools.invoke('load_skill', {'name':'asset-allocation'})
        self.assertEqual(output['data']['skill'], 'asset-allocation')
        self.assertEqual(output['facts'], [])
        self.assertFalse(self.tools.changed)
        reference = skills.describe('asset-allocation')['references'][0]
        result = self.tools.invoke('load_skill', {'name':'asset-allocation','reference':reference})
        self.assertEqual(result['data']['reference'], reference)
        after = self.gateway.overview(self.workspace)
        for key in ('total_cash_minor','orders','holdings','fund_holds'):
            self.assertEqual(before[key], after[key])

    def test_role_and_file_boundary_apply_even_when_enum_is_bypassed(self):
        for args in ({'name':'reviewer'},{'name':'../reviewer'},
                     {'name':'banking','reference':'missing.md'}):
            with self.subTest(args=args), self.assertRaises(BankError):
                self.tools.invoke('load_skill', args)
        self.assertEqual(self.tools.evidence, {})
        with self.assertRaises(BankError) as error:
            self.tools.invoke('execute_transfer', {'amount_minor':100})
        self.assertEqual(error.exception.code, 'TOOL_FORBIDDEN')

    def test_default_knowledge_connection_is_explicitly_unavailable(self):
        status = self.tools.invoke('knowledge_status', {})
        self.assertFalse(status['data']['ready'])
        self.assertEqual(status['facts'], [])
        result = self.tools.invoke('knowledge_search', {'query':'当前申购费用','facets':['fees']})
        self.assertEqual(result['data']['status'], 'unavailable')
        self.assertEqual(result['data']['hits'], [])
        self.assertEqual(result['facts'], [])
        self.assertTrue(self.tools.advice)
        self.assertFalse(self.tools.changed)
        self.assertEqual(self.tools.evidence[result['evidence_id']]['source'], 'public_knowledge')

    def test_knowledge_tool_does_not_allow_private_scope_paths_or_unbounded_queries(self):
        for args in ({'query':'资料','scope':'private'}, {'query':'资料','workspace_id':'other'},
                     {'query':'资料','path':'/etc/passwd'},{'query':'x'*501},
                     {'query':'资料','limit':9},{'query':'资料','facets':['unrestricted']}):
            with self.subTest(args=args), self.assertRaises(BankError):
                self.tools.invoke('knowledge_search', args)
        self.assertFalse(self.tools.evidence)

    def test_cashflow_floor_flags_use_the_already_reserved_available_amount(self):
        data = self.tools.invoke('cashflow', {'days':60})['data']
        main = next(row for row in data['accounts'] if row['account_id'].endswith('-main'))
        # 回归真实云冒烟暴露的错误：9844元小于10000元底线，仍是底线外余量。
        self.assertEqual(main['reserve_floor_minor'], 1000000)
        self.assertEqual(main['minimum_available_after_confirmed_minor'], 984400)
        self.assertFalse(main['confirmed_floor_breached'])
        self.assertFalse(main['candidate_floor_breached'])
        self.assertEqual(main['projected_balance_after_confirmed_minor'], 1984400)


if __name__ == '__main__': unittest.main()
