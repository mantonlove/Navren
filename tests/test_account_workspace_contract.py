"""账户新视图依赖真实服务契约；使用隔离临时账本。"""
import tempfile
import unittest
from pathlib import Path
from finance_gateway import FinanceGateway
from simulated_bank import BankError


class AccountWorkspaceContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway = FinanceGateway(Path(self.temp.name) / 'bank.sqlite3')
        self.workspace, _ = self.gateway.create_session()

    def test_explicit_reports_keep_mutable_active_source_unchanged(self):
        g, w = self.gateway, self.workspace
        csv = '流水号,日期,商户,金额,类型,分类,币种,备注\nonly-imported,2026-09-01,咖啡,8.80,支出,餐饮,CNY,\n'
        preview = g.statements.preview(w, csv)
        self.assertTrue(preview['valid'])
        g.statements.commit(w, {'batch_id': preview['batch_id'], 'confirmed': True})
        g.set_source(w, {'source': 'imported'})
        sources = g.source(w)['sources']
        bank = g.reports_snapshot(w, {'source': 'simulation', 'source_version': sources['simulation']['source_version'], 'month': '2026-09', 'year': '2026'})
        imported = g.reports_snapshot(w, {'source': 'imported', 'source_version': sources['imported']['source_version'], 'month': '2026-09', 'year': '2026'})
        self.assertEqual(g.source(w)['active_source'], 'imported')
        self.assertEqual(bank['source_binding']['workspace_id'], w)
        self.assertEqual(bank['source_binding']['source_scope'], 'simulation')
        self.assertFalse(any(r['id'] == 'only-imported' for r in bank['transactions']))
        self.assertEqual(imported['analysis']['totals_minor']['expense'], 880)
        self.assertIsNone(imported['analysis']['cash_delta_minor'])
        self.assertEqual(imported['transactions'][0]['id'], 'only-imported')
        with self.assertRaises(BankError) as result:
            g.reports_snapshot(w, {'source': 'simulation', 'source_version': 'stale', 'month': '2026-09', 'year': '2026'})
        self.assertEqual(result.exception.code, 'SOURCE_VERSION_CONFLICT')

    def test_account_budget_uses_actual_selected_account(self):
        g, w = self.gateway, self.workspace
        overview = g.overview(w)
        account = next(r for r in overview['accounts'] if r['id'].endswith('-main'))
        budget = g.budget(w, account['id'])
        self.assertEqual(budget['cash_minor'], account['balance_minor'])
        self.assertEqual(budget['version'], overview['version'])
        self.assertEqual(budget['investable_minor'], max(0, budget['cash_minor'] - budget['reserve_minor'] - budget['upcoming_due_minor'] - budget['held_minor']))
        self.assertIn('version', g.financial_records(w))

    def test_agent_report_source_is_scoped_and_does_not_switch_workspace(self):
        from agent_runtime.bank_tools import BankTools
        g,w=self.gateway,self.workspace
        preview=g.statements.preview(w,'流水号,日期,商户,金额,类型,分类,币种,备注\nsource-only,2026-09-01,导入咖啡,8.80,支出,餐饮,CNY,\n')
        g.statements.commit(w,{'batch_id':preview['batch_id'],'confirmed':True});g.set_source(w,{'source':'imported'})
        version=g.source(w)['sources']['simulation']['source_version']
        scoped=g.bound_report_source(w,'simulation',version)
        tools=BankTools(scoped,w,'source-scope-test')
        data=tools.invoke('monthly_statement',{'month':'2026-09'})['data']
        self.assertNotEqual(data['source'],'imported_local_csv')
        self.assertEqual(g.source(w)['active_source'],'imported')
        self.assertEqual(scoped.source(w)['active_source'],'simulation')
        self.assertEqual(scoped.source(w)['workspace_active_source'],'imported')
        with self.assertRaises(BankError):g.bound_report_source(w,'simulation','expired')
        from agent_runtime.conversation import ConversationAgent
        from test_native_conversation import ScriptedPrimary,Reviewer,call,last_tool
        primary=ScriptedPrimary(call('monthly_statement',{'month':'2026-09'}),lambda messages,_:call('answer_facts',{'fact_ids':[last_tool(messages)['facts'][0]['id']]}))
        agent=ConversationAgent(primary=primary,reviewer=Reviewer())
        result=agent.process(g,w,'解释这份账户9月报告',page_context={'kind':'report','id':'monthly:2026-09','source_scope':'simulation','version':version,'parameters':{'month':'2026-09'},'execution_authority':False})
        self.assertFalse(result.get('isFallback'),result)
        self.assertEqual(result['answer_contract']['kind'],'verified_facts')
        self.assertNotEqual(last_tool(primary.messages[-1])['data']['source'],'imported_local_csv')
        self.assertEqual(g.source(w)['active_source'],'imported')

    def test_calendar_unsupported_source_pair_does_not_replace_source(self):
        g, w = self.gateway, self.workspace
        before = g.overview(w)
        versions = g.wealth.versions(w)
        historical = next(r for r in versions['versions'] if r['kind'] == 'historical' and r['active'])
        with self.assertRaises(BankError):
            g.calendar.query(w, {'from': '2026-09-01', 'to': '2026-09-30', 'source': 'imported', 'portfolio': historical['id'], 'accounting_basis': 'total_equity_v2'})
        self.assertEqual(g.overview(w)['accounts'], before['accounts'])
        self.assertEqual(g.source(w)['active_source'], 'simulation')


if __name__ == '__main__':
    unittest.main()
