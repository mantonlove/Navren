"""原市场事务连接的生命周期与消费者门；不调用模型、不执行金融操作。"""
import asyncio
from contextvars import copy_context
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from base_runtime.evidence import OperationEvidence
from local_knowledge import LocalKnowledgeProvider
from market_content import MarketStore
from public_market_history import PublicMarketHistory,HistoryError
from simulated_bank import SimulatedBank,BankError


class HeldMarketConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='navren-held-market-');self.addCleanup(self.temp.cleanup)
        p=Path(self.temp.name)
        self.g=SimpleNamespace(bank=SimulatedBank(p/'bank.sqlite3'),market=MarketStore(p/'market.sqlite3'),
                               knowledge_provider=LocalKnowledgeProvider(p/'knowledge.sqlite3',encoder=False))
        self.e=OperationEvidence(self.g)
        # 仅替换本测试的元数据复验；连接与原BEGIN IMMEDIATE是真实SQLite路径。
        checker=patch.object(self.e,'assert_public_evidence',lambda snapshot:None)
        checker.start();self.addCleanup(checker.stop)
        self.snapshot={'market':{'fixture_only':True}}

    def denied(self,evidence=None,market=None):
        with self.assertRaises(BankError) as error:
            (evidence or self.e).current_market_connection(market or self.g.market)
        self.assertEqual(error.exception.code,'HISTORY_PUBLICATION_GUARD_REQUIRED')
        self.assertEqual(error.exception.status,503)

    def test_scope_required_and_knowledge_only_does_not_issue_market_connection(self):
        self.denied()
        with self.e.public_guard({'knowledge':{'fixture_only':True}}):self.denied()
        self.denied()

    def test_actual_original_connection_passes_original_consumer_gate_without_new_begin(self):
        traces=[];connections=[];original=self.g.market.db
        @contextmanager
        def tracked():
            with original() as db:
                connections.append(db);db.set_trace_callback(traces.append);yield db
        with patch.object(self.g.market,'db',tracked),self.e.public_guard(self.snapshot):
            db=self.e.current_market_connection(self.g.market)
            self.assertIs(db,connections[0]);self.assertTrue(db.in_transaction)
            history=PublicMarketHistory(self.g.market)
            with history._transaction(db=db) as borrowed:
                self.assertIs(borrowed,db);db.execute('SELECT COUNT(*) FROM market_articles').fetchone()
            self.assertTrue(db.in_transaction)
        self.assertEqual(sum(s.strip().upper().startswith('BEGIN') for s in traces),1)
        self.assertEqual(len(connections),1);self.denied()

    def test_wrong_market_object_or_other_evidence_instance_refused(self):
        same_file_other_object=MarketStore(self.g.market.path)
        other=OperationEvidence(self.g)
        with self.e.public_guard(self.snapshot):
            self.denied(market=same_file_other_object)
            self.denied(evidence=other)
            actual=self.g.market;self.g.market=same_file_other_object
            self.denied(market=actual)
            self.g.market=actual
            self.assertTrue(self.e.current_market_connection(actual).in_transaction)

    def test_rollback_inside_scope_is_not_a_live_publication_barrier(self):
        with self.e.public_guard(self.snapshot):
            db=self.e.current_market_connection(self.g.market);db.rollback();self.denied()
            with self.assertRaises(HistoryError):
                with PublicMarketHistory(self.g.market)._transaction(db=db):self.fail('consumer must refuse unheld transaction')

    def test_exception_and_failed_metadata_validation_reset_context(self):
        with self.assertRaises(RuntimeError):
            with self.e.public_guard(self.snapshot):raise RuntimeError('fixture body failure')
        self.denied()
        with patch.object(self.e,'assert_public_evidence',side_effect=BankError('STALE_MARKET_EVIDENCE','fixture',409)):
            with self.assertRaises(BankError):
                with self.e.public_guard(self.snapshot):self.fail('stale metadata cannot yield')
        self.denied()

    def test_escaped_context_after_exit_is_revoked_even_before_connection_cleanup(self):
        escaped=[];original=self.g.market.db
        @contextmanager
        def tracked():
            with original() as db:
                try:yield db
                finally:
                    if escaped:escaped[0].run(self.denied)
        with patch.object(self.g.market,'db',tracked),self.e.public_guard(self.snapshot):
            escaped.append(copy_context())
        escaped[0].run(self.denied);self.denied()

    def test_copied_context_in_other_thread_cannot_borrow_live_connection(self):
        outcomes=[]
        with self.e.public_guard(self.snapshot):
            copied=copy_context()
            def check():
                try:copied.run(self.denied);outcomes.append('denied')
                except Exception as error:outcomes.append(type(error).__name__)
            t=threading.Thread(target=check);t.start();t.join(3)
            self.assertFalse(t.is_alive());self.assertEqual(outcomes,['denied'])
            self.assertTrue(self.e.current_market_connection(self.g.market).in_transaction)

    def test_inherited_context_in_other_async_task_cannot_borrow_live_connection(self):
        with self.e.public_guard(self.snapshot):
            async def check():self.denied()
            asyncio.run(check())
            self.assertTrue(self.e.current_market_connection(self.g.market).in_transaction)

    def test_nested_other_instance_guard_restores_outer_held_connection(self):
        p=Path(self.temp.name)
        other_g=SimpleNamespace(bank=self.g.bank,market=MarketStore(p/'other-market.sqlite3'),
                                knowledge_provider=self.g.knowledge_provider)
        other=OperationEvidence(other_g)
        with patch.object(other,'assert_public_evidence',lambda snapshot:None),self.e.public_guard(self.snapshot):
            outer=self.e.current_market_connection(self.g.market)
            with other.public_guard(self.snapshot):
                self.assertTrue(other.current_market_connection(other_g.market).in_transaction)
                self.denied()
            self.assertIs(self.e.current_market_connection(self.g.market),outer)
        self.denied();self.denied(evidence=other,market=other_g.market)


if __name__=='__main__':unittest.main(verbosity=2)
