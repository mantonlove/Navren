"""持真实银行写锁的历史保存/撤回；隔离库、临时 Fernet、禁止网络与银行重入。"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from personal_data import PersonalDataStore
from simulated_bank import BankError, SimulatedBank
from user_memory import UserMemoryStore, privacy_revision


class GuardedHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=tempfile.TemporaryDirectory(prefix='navren-guarded-history-')
        cls.network=patch.object(socket,'create_connection',side_effect=AssertionError('network_forbidden'))
        cls.network.start();cls.now=[1700000000.0]
        cls.bank=SimulatedBank(Path(cls.folder.name)/'bank.sqlite3',clock=lambda:cls.now[0])
        cls.store=PersonalDataStore(cls.bank,encryption_key=Fernet.generate_key())
        cls.memories=UserMemoryStore(cls.bank)

    @classmethod
    def tearDownClass(cls):cls.network.stop();cls.folder.cleanup()

    def setUp(self):
        self.w=self.bank.create_workspace()['workspace_id']
        self.memories.set_enabled(self.w,{'enabled':True})
        self.revision=self.store.revision(self.w)
        with closing(self.bank.connect()) as db:self.privacy=privacy_revision(db,self.w)
        self.base=[{'role':'user','content':'原历史'},{'role':'assistant','content':'原回答'}]
        self.messages=[*self.base,{'role':'user','content':'本轮问题'},{'role':'assistant','content':'已核验答复'}]
        self.widgets=[{'type':'cashflow','data':{'cash_minor':12300}}]

    def save(self,response_id='round-a',messages=None,widgets=None,**versions):
        with self.bank.transaction() as db:
            with (patch.object(self.bank,'connect',side_effect=AssertionError('extra_bank_connection')),
                  patch.object(self.bank,'transaction',side_effect=AssertionError('nested_bank_transaction')),
                  patch.object(self.bank,'create_workspace',side_effect=AssertionError('provision_forbidden'))):
                return self.store.save_turn_guarded(self.w,self.messages if messages is None else messages,
                    response_id,self.widgets if widgets is None else widgets,bank_db=db,
                    expected_revision=versions.get('expected_revision',self.revision),
                    expected_privacy=versions.get('expected_privacy',self.privacy))

    def discard(self,response_id='round-a',**versions):
        with self.bank.transaction() as db:
            with (patch.object(self.bank,'connect',side_effect=AssertionError('extra_bank_connection')),
                  patch.object(self.bank,'transaction',side_effect=AssertionError('nested_bank_transaction'))):
                return self.store.discard_turn_guarded(self.w,response_id,bank_db=db,
                    expected_revision=versions.get('expected_revision',self.revision),
                    expected_privacy=versions.get('expected_privacy',self.privacy))

    def saved(self,response_id='round-a'):
        with closing(self.store.connect()) as db:
            row=db.execute('SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?',(self.w,response_id)).fetchone()
        return dict(row) if row else None

    def test_actual_bank_write_lock_no_reentry_and_readonly_bank_queries(self):
        writes={sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,sqlite3.SQLITE_TRANSACTION,
                sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_ALTER_TABLE}
        with self.bank.transaction() as db:
            with closing(sqlite3.connect(self.bank.database,timeout=0)) as contender:
                with self.assertRaises(sqlite3.OperationalError):contender.execute('BEGIN IMMEDIATE')
            before=db.total_changes
            db.set_authorizer(lambda action,*args:sqlite3.SQLITE_DENY if action in writes else sqlite3.SQLITE_OK)
            with (patch.object(self.bank,'connect',side_effect=AssertionError('extra_bank_connection')),
                  patch.object(self.bank,'transaction',side_effect=AssertionError('nested_bank_transaction'))):
                self.assertTrue(self.store.save_turn_guarded(self.w,self.messages,'round-a',self.widgets,
                    bank_db=db,expected_revision=self.revision,expected_privacy=self.privacy))
            self.assertEqual(db.total_changes,before);db.set_authorizer(None)
        self.assertEqual(self.store.read_history(self.w),self.messages)
        self.assertEqual(self.store.read_widgets(self.w)[0]['response_id'],'round-a')

    def test_plain_connection_closed_connection_and_wrong_database_rejected(self):
        with closing(self.bank.connect()) as db:
            self.assertFalse(self.store.save_turn_guarded(self.w,self.messages,'round-a',[],bank_db=db,
                expected_revision=self.revision,expected_privacy=self.privacy))
        self.assertFalse(self.store.save_turn_guarded(self.w,self.messages,'round-a',[],bank_db=db,
            expected_revision=self.revision,expected_privacy=self.privacy))
        with closing(sqlite3.connect(':memory:')) as other:
            other.execute('BEGIN IMMEDIATE')
            other.execute('ATTACH DATABASE ? AS actual_bank',(str(self.bank.database),))
            self.assertFalse(self.store.save_turn_guarded(self.w,self.messages,'round-a',[],bank_db=other,
                expected_revision=self.revision,expected_privacy=self.privacy))
        self.assertEqual(self.store.read_history(self.w),[])

    def test_wrong_owner_and_private_database_binding_rejected(self):
        with self.bank.transaction() as db:
            self.assertFalse(self.store.save_turn_guarded('missing-owner',self.messages,'round-a',[],bank_db=db,
                expected_revision=self.revision,expected_privacy=self.privacy))
            db.execute('UPDATE personal_data_binding SET database_path=? WHERE id=1',('/not/the/private/database',))
            self.assertFalse(self.store.save_turn_guarded(self.w,self.messages,'round-a',[],bank_db=db,
                expected_revision=self.revision,expected_privacy=self.privacy))
            db.execute('UPDATE personal_data_binding SET database_path=? WHERE id=1',(str(self.store.database),))
        self.assertEqual(self.store.read_history(self.w),[])

    def test_standalone_private_store_cannot_claim_bank_guard(self):
        standalone=PersonalDataStore(Path(self.folder.name)/'standalone.sqlite3',encryption_key=Fernet.generate_key())
        with self.bank.transaction() as db:
            self.assertFalse(standalone.save_turn_guarded(self.w,self.messages,'round-a',[],bank_db=db,
                expected_revision=self.revision,expected_privacy=self.privacy))

    def test_versions_are_explicit_and_exact_including_bool(self):
        with self.bank.transaction() as db:
            with self.assertRaises(TypeError):
                self.store.save_turn_guarded(self.w,self.messages,'round-a',[],bank_db=db)
        for field in ('expected_revision','expected_privacy'):
            for value in (None,True,'1',1.0,-1,9999):
                with self.subTest(field=field,value=value):self.assertFalse(self.save(**{field:value}))
        self.assertEqual(self.store.read_history(self.w),[])

    def assert_no_long_term_artifacts(self, owner):
        self.assertFalse(self.memories.state(owner)['enabled'])
        with closing(self.store.connect()) as db:
            for table in ('user_memories','user_memory_relations','profile_observations',
                          'conversation_sources','memory_candidates'):
                with self.subTest(table=table):
                    self.assertEqual(db.execute('SELECT count(*) FROM '+table+' WHERE workspace_id=?',
                                                (owner,)).fetchone()[0],0)

    def disable_before_round(self):
        self.memories.set_enabled(self.w,{'enabled':False,'expected_revision':self.revision})
        self.revision=self.store.revision(self.w)
        with closing(self.bank.connect()) as db:self.privacy=privacy_revision(db,self.w)

    def test_memory_default_off_saves_history_widgets_and_receipt_without_long_term_artifacts(self):
        untouched=self.bank.create_workspace()['workspace_id']
        with self.bank.transaction() as db:
            self.assertTrue(self.store.save_turn_guarded(untouched,self.messages,'round-a',self.widgets,bank_db=db,
                expected_revision=0,expected_privacy=privacy_revision(db,untouched)))
        self.assertEqual(self.store.read_history(untouched),self.messages)
        self.assertEqual(self.store.read_widgets(untouched)[0]['widgets'],self.widgets)
        with closing(self.store.connect()) as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM guarded_turn_saves WHERE workspace_id=?',(untouched,)).fetchone())
            self.assertIsNone(db.execute('SELECT 1 FROM personal_versions WHERE workspace_id=?',(untouched,)).fetchone())
            self.assertIsNone(db.execute('SELECT 1 FROM user_memory_settings WHERE workspace_id=?',(untouched,)).fetchone())
        self.assert_no_long_term_artifacts(untouched)

    def test_memory_disabled_current_epoch_saves_history_but_old_epoch_is_rejected(self):
        self.memories.set_enabled(self.w,{'enabled':False,'expected_revision':self.revision})
        self.assertFalse(self.save())
        fresh=self.store.revision(self.w)
        with self.bank.transaction() as db:
            self.assertTrue(self.store.save_turn_guarded(self.w,self.messages,'round-a',self.widgets,bank_db=db,
                expected_revision=fresh,expected_privacy=privacy_revision(db,self.w)))
        self.assertEqual(self.store.read_history(self.w),self.messages)
        self.assertEqual(self.store.read_widgets(self.w)[0]['widgets'],self.widgets)
        self.assertIsNotNone(self.saved());self.assert_no_long_term_artifacts(self.w)

    def test_same_epoch_disabled_discard_restores_original_session_history(self):
        self.disable_before_round()
        self.store.save_history(self.w,self.base,expected_revision=self.revision);self.assertTrue(self.save())
        self.assertTrue(self.discard());self.assertEqual(self.store.read_history(self.w),self.base)
        self.assertEqual(self.store.read_widgets(self.w),[])
        self.assertTrue(self.saved()['previous_messages'].startswith('pd1:'))
        self.assert_no_long_term_artifacts(self.w)

    def test_disabled_memory_history_does_not_register_or_extract_long_term_source(self):
        self.disable_before_round();self.assertTrue(self.save())
        text='我的长期目标是毕业前存下30000元'
        self.assertFalse(self.store.register_conversation_source(self.w,'off-source',text,
                         expected_revision=self.revision)['saved'])
        self.assertEqual(self.store.propose_candidates(self.w,text,'off-source'),[])
        self.assert_no_long_term_artifacts(self.w)
        self.assertEqual(self.store.read_history(self.w),self.messages)

    def test_disabled_memory_user_deletion_rejects_old_save_and_discard(self):
        self.disable_before_round();self.assertTrue(self.save())
        self.store.delete_history(self.w,expected_revision=self.revision)
        self.assertFalse(self.save(response_id='late-off'));self.assertFalse(self.discard())
        self.assertEqual(self.store.read_history(self.w),[]);self.assertEqual(self.store.read_widgets(self.w),[])
        self.assertIsNone(self.saved());self.assert_no_long_term_artifacts(self.w)

    def test_disabled_memory_new_history_and_widgets_survive_old_discard(self):
        self.disable_before_round();self.assertTrue(self.save())
        edited=[{'role':'user','content':'后来修改的历史'}];widgets=[{'type':'later-widget'}]
        self.store.save_history(self.w,edited,expected_revision=self.revision)
        self.store.save_widgets(self.w,'round-a',widgets,expected_revision=self.revision)
        self.assertFalse(self.discard());self.assertFalse(self.save())
        self.assertEqual(self.store.read_history(self.w),edited)
        self.assertEqual(self.store.read_widgets(self.w)[0]['widgets'],widgets)
        self.assert_no_long_term_artifacts(self.w)

    def test_disabled_memory_nested_discard_skips_rejected_round(self):
        self.disable_before_round();self.store.save_history(self.w,self.base,expected_revision=self.revision)
        self.assertTrue(self.save())
        later=[*self.messages,{'role':'assistant','content':'新轮回答'}]
        self.assertTrue(self.save(response_id='round-b',messages=later))
        self.assertFalse(self.discard('round-a'));self.assertTrue(self.discard('round-b'))
        self.assertEqual(self.store.read_history(self.w),self.base)
        self.assertEqual(self.store.read_widgets(self.w),[]);self.assert_no_long_term_artifacts(self.w)

    def test_disabling_memory_during_round_rejects_stale_discard_and_resave(self):
        self.store.save_history(self.w,self.base,expected_revision=self.revision);self.assertTrue(self.save())
        self.memories.set_enabled(self.w,{'enabled':False,'expected_revision':self.revision})
        self.assertFalse(self.discard());self.assertFalse(self.save(response_id='stale-after-disable'))
        self.assertEqual(self.store.read_history(self.w),[]);self.assertEqual(self.store.read_widgets(self.w),[])
        self.assertIsNone(self.saved());self.assert_no_long_term_artifacts(self.w)

    def test_user_deletion_and_memory_revision_cannot_revive_old_round(self):
        self.assertTrue(self.save());self.store.delete_history(self.w,expected_revision=self.revision)
        self.assertFalse(self.save(response_id='late-old-round'))
        self.assertFalse(self.discard());self.assertEqual(self.store.read_history(self.w),[])
        self.assertEqual(self.store.read_widgets(self.w),[]);self.assertIsNone(self.saved())

    def test_user_memory_correction_clears_rollback_body(self):
        created=self.memories.create(self.w,{'category':'goal','value':'本人原目标'})
        self.revision=self.store.revision(self.w)
        with closing(self.bank.connect()) as db:self.privacy=privacy_revision(db,self.w)
        self.assertTrue(self.save())
        self.memories.correct(self.w,created['items'][0]['node_id'],{'value':'本人更改后的目标','expected_revision':self.revision})
        self.assertFalse(self.save(response_id='late-old-round'));self.assertFalse(self.discard())
        self.assertIsNone(self.saved());self.assertEqual(self.store.read_history(self.w),[])

    def test_pending_invalidation_cannot_save_or_restore(self):
        self.assertTrue(self.save())
        with closing(self.store.connect()) as db,db:
            db.execute('UPDATE personal_versions SET pending_invalidation=1 WHERE workspace_id=?',(self.w,))
        self.assertFalse(self.save(response_id='late'));self.assertFalse(self.discard())

    def test_save_atomic_private_failure_leaves_previous_history_and_widgets(self):
        self.store.save_history(self.w,self.base,expected_revision=self.revision)
        with closing(self.store.connect()) as db,db:
            db.execute("CREATE TRIGGER reject_guarded_save BEFORE INSERT ON guarded_turn_saves BEGIN SELECT RAISE(ABORT,'injected_private_failure'); END")
        try:
            with self.assertRaises(sqlite3.IntegrityError):self.save()
            self.assertEqual(self.store.read_history(self.w),self.base)
            self.assertEqual(self.store.read_widgets(self.w),[]);self.assertIsNone(self.saved())
        finally:
            with closing(self.store.connect()) as db,db:db.execute('DROP TRIGGER reject_guarded_save')

    def test_discard_restores_only_previous_encrypted_history_and_this_widget(self):
        self.store.save_history(self.w,self.base,expected_revision=self.revision)
        self.store.save_widgets(self.w,'other-round',[{'type':'other'}],expected_revision=self.revision)
        self.assertTrue(self.save());saved=self.saved()
        self.assertTrue(saved['previous_messages'].startswith('pd1:'))
        self.assertNotIn('原回答',saved['previous_messages'])
        self.assertTrue(self.discard());self.assertEqual(self.store.read_history(self.w),self.base)
        self.assertEqual([r['response_id'] for r in self.store.read_widgets(self.w)],['other-round'])
        self.assertTrue(self.discard());self.assertFalse(self.save())
        self.assertEqual(self.saved()['save_generation'],saved['save_generation'])

    def test_discard_empty_prior_history_only_removes_this_round(self):
        self.assertTrue(self.save(widgets=[]));self.assertTrue(self.discard())
        self.assertEqual(self.store.read_history(self.w),[]);self.assertEqual(self.store.read_widgets(self.w),[])

    def test_discard_unknown_round_or_wrong_versions_leaves_saved_round(self):
        self.assertTrue(self.save())
        self.assertFalse(self.discard('another-round'));self.assertFalse(self.discard(expected_privacy=self.privacy+1))
        self.assertFalse(self.discard(expected_revision=self.revision+1))
        self.assertEqual(self.store.read_history(self.w),self.messages)
        self.assertEqual(self.store.read_widgets(self.w)[0]['response_id'],'round-a')

    def test_one_response_has_one_generation_and_same_original_only_is_idempotent(self):
        self.assertTrue(self.save());generation=self.saved()['save_generation']
        self.assertTrue(self.save());self.assertEqual(self.saved()['save_generation'],generation)
        self.assertFalse(self.save(messages=self.base));self.assertFalse(self.save(widgets=[]))
        self.store.save_history(self.w,self.messages,expected_revision=self.revision)
        self.assertFalse(self.save());self.assertFalse(self.discard())

    def test_legacy_widget_response_id_collision_cannot_overwrite_other_original(self):
        self.store.save_history(self.w,self.base,expected_revision=self.revision)
        self.store.save_widgets(self.w,'round-a',[{'type':'legacy-original'}],expected_revision=self.revision)
        self.assertFalse(self.save());self.assertFalse(self.discard())
        self.assertEqual(self.store.read_history(self.w),self.base)
        self.assertEqual(self.store.read_widgets(self.w)[0]['widgets'],[{'type':'legacy-original'}])

    def test_empty_original_widget_is_not_idempotency_wildcard(self):
        self.assertTrue(self.save(widgets=[]))
        added=[{'type':'new-user-component'}]
        self.store.save_widgets(self.w,'round-a',added,expected_revision=self.revision)
        self.assertFalse(self.save(widgets=[]));self.assertTrue(self.discard())
        self.assertEqual(self.store.read_widgets(self.w)[0]['widgets'],added)

    def test_new_round_history_is_not_overwritten_by_old_discard(self):
        self.store.save_history(self.w,self.base,expected_revision=self.revision)
        self.assertTrue(self.save())
        later=[*self.messages,{'role':'assistant','content':'新轮回答'}]
        self.assertTrue(self.save(response_id='round-b',messages=later,widgets=[{'type':'new'}]))
        self.assertFalse(self.discard());self.assertFalse(self.discard())
        self.assertEqual(self.store.read_history(self.w),later)
        self.assertEqual([r['response_id'] for r in self.store.read_widgets(self.w)],['round-b'])

    def test_nested_discard_does_not_restore_previously_rejected_round(self):
        self.store.save_history(self.w,self.base,expected_revision=self.revision)
        self.assertTrue(self.save())
        later=[*self.messages,{'role':'assistant','content':'新轮回答'}]
        self.assertTrue(self.save(response_id='round-b',messages=later))
        self.assertFalse(self.discard('round-a'));self.assertTrue(self.discard('round-b'))
        self.assertEqual(self.store.read_history(self.w),self.base)
        self.assertEqual(self.store.read_widgets(self.w),[])

    def test_new_user_history_edit_is_never_replaced_by_rollback_backup(self):
        self.store.save_history(self.w,self.base,expected_revision=self.revision);self.assertTrue(self.save())
        edited=[{'role':'user','content':'用户后续自行修改的记录'}]
        self.store.save_history(self.w,edited,expected_revision=self.revision)
        self.assertFalse(self.discard());self.assertEqual(self.store.read_history(self.w),edited)
        self.assertEqual(self.store.read_widgets(self.w),[])

    def test_new_widget_same_id_is_not_deleted_by_old_original_hash(self):
        self.assertTrue(self.save())
        changed=[{'type':'user-edited-widget'}]
        self.store.save_widgets(self.w,'round-a',changed,expected_revision=self.revision)
        self.assertFalse(self.discard());self.assertEqual(self.store.read_widgets(self.w)[0]['widgets'],changed)

    def test_rejected_pending_save_does_not_prune_other_round_widgets(self):
        for i in range(35):self.store.save_widgets(self.w,'old-'+str(i),[{'type':'old'}],expected_revision=self.revision)
        self.assertTrue(self.save());self.assertEqual(len(self.store.read_widgets(self.w,100)),36)
        self.assertTrue(self.discard());self.assertEqual(len(self.store.read_widgets(self.w,100)),35)

    def test_bank_rollback_after_private_commit_can_be_compensated(self):
        self.store.save_history(self.w,self.base,expected_revision=self.revision)
        with self.assertRaisesRegex(RuntimeError,'injected_bank_commit_failure'):
            with self.bank.transaction() as db:
                self.assertTrue(self.store.save_turn_guarded(self.w,self.messages,'round-a',self.widgets,
                    bank_db=db,expected_revision=self.revision,expected_privacy=self.privacy))
                raise RuntimeError('injected_bank_commit_failure')
        self.assertEqual(self.store.read_history(self.w),self.messages)
        self.assertTrue(self.discard());self.assertEqual(self.store.read_history(self.w),self.base)

    def test_original_save_turn_compatibility_remains(self):
        self.assertTrue(self.store.save_turn(self.w,self.base,'old-api',[{'type':'legacy'}],
            expected_revision=self.revision,expected_privacy=self.privacy))
        self.assertEqual(self.store.read_history(self.w),self.base)
        self.assertEqual(self.store.read_widgets(self.w)[0]['response_id'],'old-api')

    def test_auxiliary_body_and_history_are_encrypted_and_audit_has_no_body(self):
        self.store.save_history(self.w,self.base,expected_revision=self.revision);self.assertTrue(self.save())
        with closing(sqlite3.connect(self.store.database)) as db:
            stored=db.execute('SELECT messages FROM agent_dialogues WHERE workspace_id=?',(self.w,)).fetchone()[0]
            widget=db.execute('SELECT widgets FROM agent_widget_history WHERE workspace_id=?',(self.w,)).fetchone()[0]
            backup=db.execute('SELECT previous_messages FROM guarded_turn_saves WHERE workspace_id=?',(self.w,)).fetchone()[0]
            audit=json.dumps(db.execute('SELECT * FROM personal_audit WHERE workspace_id=?',(self.w,)).fetchall())
        for body in (stored,widget,backup):self.assertTrue(body.startswith('pd1:'));self.assertNotIn('原历史',body)
        self.assertNotIn('已核验答复',audit)


class DefaultOffHistoryHTTPTests(unittest.TestCase):
    def test_normal_http_two_turn_history_is_saved_without_long_term_memory(self):
        from agent_runtime.conversation import ConversationAgent
        from server import create_server
        from test_bank_integration import Browser
        from test_native_conversation import ScriptedPrimary, Reviewer
        with tempfile.TemporaryDirectory(prefix='navren-default-off-http-') as folder:
            primary=ScriptedPrimary({'role':'assistant','content':'已收到你的问题。'},
                                    {'role':'assistant','content':'可以继续讨论。'})
            agent=ConversationAgent(primary,Reviewer())
            server=create_server(0,Path(folder)/'http.sqlite3',conversation_agent=agent)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                browser=Browser(f'http://127.0.0.1:{server.server_port}')
                session,_=browser.start();owner=session['workspace_id'];store=server.finance.personal_data
                self.assertFalse(server.finance.memories.state(owner)['enabled'])
                question='我的长期目标是毕业前准备留学学费'
                status,first,_=browser.call('/api/agent','POST',{'text':question})
                self.assertEqual(status,200);self.assertEqual(first['reply'],'已收到你的问题。')
                self.assertEqual(store.read_history(owner),[
                    {'role':'user','content':question},{'role':'assistant','content':'已收到你的问题。'}])
                status,second,_=browser.call('/api/agent','POST',{'text':'继续聊一下'})
                self.assertEqual(status,200);self.assertEqual(second['reply'],'可以继续讨论。')
                self.assertEqual(store.read_history(owner),[
                    {'role':'user','content':question},{'role':'assistant','content':'已收到你的问题。'},
                    {'role':'user','content':'继续聊一下'},{'role':'assistant','content':'可以继续讨论。'}])
                self.assertIn(question,json.dumps(primary.messages[1],ensure_ascii=False))
                self.assertFalse(server.finance.memories.state(owner)['enabled'])
                with closing(store.connect()) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM guarded_turn_saves WHERE workspace_id=?',
                                                (owner,)).fetchone()[0],2)
                    for table in ('user_memory_settings','user_memories','profile_observations',
                                  'conversation_sources','memory_candidates'):
                        with self.subTest(table=table):
                            self.assertEqual(db.execute('SELECT count(*) FROM '+table+' WHERE workspace_id=?',
                                                        (owner,)).fetchone()[0],0)
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)
                self.assertFalse(thread.is_alive())


if __name__=='__main__':unittest.main()
