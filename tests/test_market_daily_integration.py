"""历史精选跨页引用的权威身份检查；仅隔离公共库，不调用模型。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from market_content import MarketStore,CN
from simulated_bank import BankError
from view_context import PageContexts
import test_market_daily as fixtures


class HistoricalReferenceTests(unittest.TestCase):
    add=fixtures.DailyTests.add

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=MarketStore(Path(self.tmp.name)/'market.sqlite3')
        self.now=datetime.now(CN)
        self.article,self.event=self.add(1)
        self.store.update_daily(now=self.now,run_id='isolated-integration',valid_check=True)
        self.contexts=PageContexts(SimpleNamespace(market=self.store))
        self.ref={'kind':'event','id':'e_1','source_scope':'market','version':1,
                  'parameters':{'edition_date':self.now.date().isoformat(),'revision':1,'event_version':1}}

    def test_bound_reference_retains_day_edition_event_version(self):
        result=self.contexts.resolve('isolated-workspace',self.ref)
        self.assertEqual(result['parameters'],self.ref['parameters'])
        self.assertFalse(result['execution_authority']);self.assertEqual(result['source_scope'],'market')

    def test_rejects_partial_unknown_or_wrong_historical_identity(self):
        for params in ({'edition_date':self.now.date().isoformat()},
                       {**self.ref['parameters'],'extra':'anything'},
                       {**self.ref['parameters'],'revision':2},
                       {**self.ref['parameters'],'event_version':2}):
            with self.subTest(params=params),self.assertRaises(BankError):
                self.contexts.resolve('isolated-workspace',{**self.ref,'parameters':params})
        with self.assertRaises(BankError):self.contexts.resolve('isolated-workspace',{**self.ref,'id':'not-a-member'})

    def test_invalidated_archive_never_silently_resolves_current_event(self):
        self.store.save_article({**self.article,'fingerprint':'revised-test-only','evidence_text':'已更正的材料。'})
        with self.assertRaises(BankError) as error:self.contexts.resolve('isolated-workspace',self.ref)
        self.assertEqual(error.exception.status,409)

    def test_legacy_event_reference_keeps_compatible_identity(self):
        result=self.contexts.resolve('isolated-workspace',{'kind':'event','id':'e_1','version':1})
        self.assertEqual(result['id'],'e_1');self.assertEqual(result['parameters'],{})


if __name__=='__main__':unittest.main()
