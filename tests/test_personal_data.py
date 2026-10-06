"""真实 SQLite 个人库隔离、迁移恢复、候选绑定及失效行为。"""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from finance_gateway import FinanceGateway
from personal_data import PersonalDataStore
from simulated_bank import BankError
from simulated_bank.agent_guard import context_hash
from user_memory import privacy_revision


class PersonalDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = Fernet.generate_key()
        self.env = patch.dict(os.environ, {'NAVREN_PERSONAL_DATA_KEY':self.key.decode()})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.g = FinanceGateway(Path(self.tmp.name)/'bank.sqlite')
        self.w, _ = self.g.create_session()
        self.other, _ = self.g.create_session()
        self.store = self.g.memories.store

    def enable(self):
        return self.g.memories.set_enabled(self.w, {'enabled':True,'expected_revision':self.store.revision(self.w)})

    def proposal(self):
        self.enable()
        return self.store.propose_candidates(self.w,'我的长期目标是毕业前存下30000元','turn-'+uuid.uuid4().hex)[0]

    @staticmethod
    def confirmation(proposal):
        return {k:proposal[k] for k in ('proposal_id','source_ref','confirmation_token','expected_revision')}

    def test_private_file_encrypted_no_bank_body_and_cross_user_read(self):
        state = self.g.memories.create(self.w, {'category':'goal','value':'一年后准备支付留学学费'})
        item = state['items'][0]
        self.assertEqual(self.g.memories.state(self.other)['items'], [])
        self.assertEqual(self.store.database.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.store.database, self.g.bank.database)
        with self.g.bank.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM user_memories').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM user_memory_settings').fetchone()[0], 0)
        with sqlite3.connect(self.store.database) as db:
            raw = db.execute('SELECT value FROM user_memories WHERE workspace_id=? AND id=?',(self.w,item['node_id'])).fetchone()[0]
        self.assertTrue(raw.startswith('pd1:'))
        self.assertNotIn(item['value'].encode(),self.store.database.read_bytes())
        self.assertNotIn(self.key,self.store.database.read_bytes())
        with self.assertRaises(BankError):
            self.g.memories.correct(self.other,item['node_id'],{'value':'跨用户覆盖'})
        self.assertEqual(self.g.memories.state(self.w)['items'][0]['value'],item['value'])

    def test_candidate_must_be_server_owned_and_value_cannot_be_forged(self):
        proposal = self.proposal()
        body = self.confirmation(proposal)
        self.assertEqual(self.store.revision(self.w), proposal['expected_revision'])
        for owner, change in ((self.other,{}),(self.w,{'confirmation_token':'0'*32}),
                              (self.w,{'source_ref':'wrong'}),(self.w,{'value':'伪造更高风险意愿'})):
            with self.assertRaises(BankError):
                self.g.memories.create(owner,{**body,**change})
        self.assertEqual(self.g.memories.state(self.w)['items'],[])
        saved = self.g.memories.create(self.w,body)
        self.assertEqual(saved['items'][0]['value'],proposal['value'])
        self.assertEqual(saved['items'][0]['source_ref'],proposal['source_ref'])
        with self.assertRaises(BankError):
            self.g.memories.create(self.w,body)

    def test_bare_source_ref_does_not_authorize_conversation_memory(self):
        with self.assertRaises(BankError):
            self.g.memories.create(self.w,{'category':'goal','value':'伪造目标',
                                          'source_type':'conversation_user_statement','source_ref':'a'*32})

    def test_hypothesis_third_person_speculation_and_sensitive_fields_are_not_candidates(self):
        self.enable()
        for index,text in enumerate(('假设我的长期目标是存30000元','我朋友说我的长期目标是存30000元',
                                     '我可能长期关注债券基金','我的长期目标是存30000元？',
                                     '我的长期目标是记住银行卡号6222000000000000000')):
            self.assertEqual(self.store.propose_candidates(self.w,text,'bad-'+str(index)),[])
            with self.assertRaises(BankError):
                self.g.personal_graph.remember(self.w,text,'profile-'+str(index),{'category':'goal','quote':text,'value':'我想储蓄'})

    def test_disabled_memory_does_not_save_source_and_cannot_confirm(self):
        self.assertFalse(self.store.register_conversation_source(self.w,'disabled','我的长期目标是储蓄')['saved'])
        self.assertEqual(self.store.rows(self.w,'conversation_sources'),[])
        proposal = self.proposal()
        self.g.memories.set_enabled(self.w,{'enabled':False,'expected_revision':proposal['expected_revision']})
        with self.assertRaises(BankError):
            self.g.memories.create(self.w,self.confirmation(proposal))
        self.assertEqual(self.g.memories.read_relevant(self.w,'目标',workflow='wealth').nodes,())

    def test_concurrent_updates_do_not_overwrite_each_other(self):
        state=self.g.memories.create(self.w,{'category':'goal','value':'一年后支付学费'})
        node=state['items'][0]['node_id'];revision=state['personal_revision']
        gate=threading.Barrier(2);results=[]
        def write(value):
            gate.wait()
            try:
                self.g.memories.correct(self.w,node,{'value':value,'expected_revision':revision})
                results.append(('saved',value))
            except BankError as error:
                results.append((error.code,value))
        threads=[threading.Thread(target=write,args=(value,)) for value in ('支付本科留学学费','支付研究生学费')]
        for t in threads:t.start()
        for t in threads:t.join(15)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(sorted(x[0] for x in results),['PERSONAL_VERSION_CONFLICT','saved'])
        winner=next(x[1] for x in results if x[0]=='saved')
        self.assertEqual(self.g.memories.state(self.w)['items'][0]['value'],winner)

    def test_candidate_creation_keeps_agent_context_and_confirmed_change_invalidates_it(self):
        self.enable()
        with self.g.bank.connect() as db:
            before=context_hash(db,self.w);privacy=privacy_revision(db,self.w)
        self.store.propose_candidates(self.w,'我的长期目标是存30000元','quiet-candidate')
        candidate=self.g.personal_graph.remember(self.w,'我计划明年去留学','quiet-profile',{'category':'goal','quote':'我计划明年去留学','value':'模型伪造'})
        self.assertEqual(candidate['value'],'我计划明年去留学')
        with self.g.bank.connect() as db:
            self.assertEqual(context_hash(db,self.w),before)
            self.assertEqual(privacy_revision(db,self.w),privacy)
        self.g.personal_graph.update(self.w,candidate['observation_id'],{'decision':'confirm',
           'confirmation_token':candidate['confirmation_token'],'expected_revision':candidate['expected_revision']})
        with self.g.bank.connect() as db:
            self.assertNotEqual(context_hash(db,self.w),before)
            self.assertGreater(privacy_revision(db,self.w),privacy)

    def test_profile_confirmation_rejects_wrong_owner_token_and_requires_local_source(self):
        self.enable()
        candidate=self.g.personal_graph.remember(self.w,'我计划明年去留学','trusted-profile',{'category':'goal','quote':'我计划明年去留学','value':'虚构'})
        for owner,body in ((self.other,{'decision':'confirm'}),(self.w,{'decision':'confirm'}),
                           (self.w,{'decision':'confirm','confirmation_token':'bad','expected_revision':candidate['expected_revision']})):
            with self.assertRaises(BankError):self.g.personal_graph.update(owner,candidate['observation_id'],body)
        self.assertFalse(any(n.get('observation_id') for n in self.g.personal_graph.read(self.w,for_agent=True)['nodes']))

    def test_confirmed_personal_statement_cannot_change_bank_risk_or_balances(self):
        self.enable()
        before=self.g.bank.customer_archive(self.w)
        candidate=self.g.personal_graph.remember(self.w,'我的风险等级是R5','risk-statement',
            {'category':'wealth','quote':'我的风险等级是R5'})
        self.g.personal_graph.update(self.w,candidate['observation_id'],{'decision':'confirm',
            'confirmation_token':candidate['confirmation_token'],'expected_revision':candidate['expected_revision']})
        after=self.g.bank.customer_archive(self.w)
        self.assertEqual(after['profile'],before['profile'])
        self.assertEqual(after['accounts'],before['accounts'])
        self.assertTrue(any(n.get('observation_id')==candidate['observation_id'] for n in self.g.personal_graph.read(self.w,for_agent=True)['nodes']))
        self.assertEqual(next(n['label'] for n in self.g.personal_graph.read(self.w,for_agent=True)['nodes'] if n['id']=='risk'),
                         '模拟配置风险等级 R'+str(before['profile']['risk_level']))

    def test_change_clears_history_graph_projection_and_user_only_relations(self):
        self.enable()
        first=self.g.memories.create(self.w,{'category':'goal','value':'一年后学费目标'})['items'][0]['node_id']
        second=self.g.memories.create(self.w,{'category':'liquidity_preference','value':'保留应急资金'})['items'][0]['node_id']
        self.g.memories.add_relation(self.w,first,{'target_node_id':second,'relation':'supports'})
        with self.assertRaises(BankError):
            self.g.memories.add_relation(self.other,first,{'target_node_id':second,'relation':'supports'})
        graph=self.g.financial_graph.query(self.w,{'view':'preferences'})
        self.assertEqual(graph['personal_revision'],self.store.revision(self.w))
        revision=self.store.revision(self.w)
        self.store.save_history(self.w,[{'role':'assistant','content':'旧推荐含旧偏好'}],expected_revision=revision)
        self.store.save_widgets(self.w,'old',[{'type':'old-advice'}],expected_revision=revision)
        self.g.memories.correct(self.w,first,{'value':'修改后目标','expected_revision':revision})
        self.assertEqual(self.store.read_history(self.w),[])
        self.assertEqual(self.store.read_widgets(self.w),[])
        self.assertFalse(self.store.save_history(self.w,[{'role':'assistant','content':'迟到的旧回答'}],expected_revision=revision))
        with self.g.bank.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM graph_entities WHERE workspace_id=?',(self.w,)).fetchone()[0],0)
        with self.assertRaises(BankError):self.g.financial_graph.query(self.w,{'view':'preferences','expected_version':graph['version']})

    def test_history_is_owner_scoped_redacted_and_delete_removes_sources_and_pending_candidates(self):
        proposal=self.proposal()
        self.store.save_history(self.w,[{'role':'user','content':'手机号13800138000，验证码123456'}])
        self.assertEqual(self.store.read_history(self.other),[])
        self.assertNotIn('13800138000',json.dumps(self.store.read_history(self.w)))
        self.assertNotIn('123456',json.dumps(self.store.read_history(self.w)))
        self.store.delete_history(self.w,expected_revision=proposal['expected_revision'])
        self.assertEqual(self.store.read_history(self.w),[])
        self.assertEqual(self.store.rows(self.w,'conversation_sources'),[])
        self.assertEqual(self.store.rows(self.w,'memory_candidates'),[])
        with self.assertRaises(BankError):self.g.memories.create(self.w,self.confirmation(proposal))

    def test_unknown_legacy_candidate_is_preserved_without_becoming_agent_fact(self):
        node=uuid.uuid4().hex
        with self.g.bank.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS profile_observations(id TEXT PRIMARY KEY,workspace_id TEXT,category TEXT,value TEXT,quote TEXT,source_ref TEXT,status TEXT,created_at REAL)')
            db.execute('INSERT INTO profile_observations VALUES(?,?,?,?,?,?,?,?)',(node,self.w,'goal','旧目标','别人说的旧原话','unknown','confirmed',1))
        self.store.migrate_legacy()
        rows=self.store.rows(self.w,'profile_observations')
        self.assertEqual(rows[0]['value'],'旧目标')
        self.assertEqual(rows[0]['status'],'candidate')
        self.assertFalse(rows[0]['source_verified'])
        with self.assertRaises(BankError):
            self.g.personal_graph.update(self.w,node,{'decision':'confirm','expected_revision':self.store.revision(self.w),'confirmation_token':'forged'})
        self.assertFalse(any(n.get('observation_id') for n in self.g.personal_graph.read(self.w,for_agent=True)['nodes']))

    def test_unverified_legacy_memory_is_not_displayed_as_confirmed(self):
        self.enable()
        node=uuid.uuid4().hex
        with self.g.bank.transaction() as db:
            db.execute("INSERT INTO user_memories(id,workspace_id,category,value,origin,user_confirmed,created_at,updated_at,source_type,source_ref) VALUES(?,?,?,?, 'user_declared',1,1,1,'conversation_user_statement','old-turn')",
                       (node,self.w,'goal','旧对话里来源未经证明的目标'))
        self.store.migrate_legacy()
        memory=next(n for n in self.g.personal_graph.read(self.w)['nodes'] if n['id']=='memory:'+node)
        self.assertEqual(memory['status'],'unverified')
        self.assertEqual(memory['source'],'unverified_personal')
        graph=self.g.financial_graph.query(self.w,{'view':'preferences'})
        memory=next(n for n in graph['nodes'] if n['id']=='memory:'+node)
        self.assertEqual(memory['status'],'unverified')
        ref=memory['evidence_refs'][0]
        self.assertEqual(self.g.financial_graph.evidence(self.w,{'source':ref['source_scope'],'table':ref['source_table'],'id':ref['source_id']})['record']['value'],memory['label'])
        with self.assertRaises(BankError) as error:
            self.g.financial_graph.evidence(self.w,{'source':'user_confirmed','table':'user_memories','id':node})
        self.assertEqual(error.exception.code,'GRAPH_EVIDENCE_UNVERIFIED')
        self.assertFalse(any(n['id']=='memory:'+node for n in self.g.financial_graph.query(self.w,{'view':'preferences'},for_agent=True)['nodes']))

    def test_correct_delete_and_withdraw_profile_remove_unreferenced_originals(self):
        proposal=self.proposal()
        state=self.g.memories.create(self.w,self.confirmation(proposal))
        node=state['items'][0]['node_id']
        self.g.memories.correct(self.w,node,{'value':'一年后支付进修学费','expected_revision':state['personal_revision']})
        self.assertEqual(self.store.rows(self.w,'conversation_sources'),[])
        self.assertEqual(self.store.rows(self.w,'memory_candidates'),[])
        proposal=self.store.propose_candidates(self.w,'我的长期目标是明年准备旅行预算','delete-turn')[0]
        state=self.g.memories.create(self.w,self.confirmation(proposal))
        node=next(m['node_id'] for m in state['items'] if m['source_ref']=='delete-turn')
        self.g.memories.delete(self.w,node,{'expected_revision':state['personal_revision']})
        self.assertEqual(self.store.rows(self.w,'conversation_sources'),[])
        self.assertEqual(self.store.rows(self.w,'memory_candidates'),[])
        candidate=self.g.personal_graph.remember(self.w,'我计划明年去留学','withdraw-turn',{'category':'goal','quote':'我计划明年去留学'})
        self.g.personal_graph.update(self.w,candidate['observation_id'],{'decision':'delete','expected_revision':candidate['expected_revision']})
        self.assertEqual(self.store.rows(self.w,'conversation_sources'),[])
        self.assertEqual(self.store.rows(self.w,'profile_observations'),[])
        self.assertEqual(self.store.rows(self.other,'conversation_sources'),[])

    def test_migration_recovers_after_private_commit_and_is_idempotent(self):
        node=uuid.uuid4().hex
        with self.g.bank.transaction() as db:
            db.execute("INSERT INTO user_memories(id,workspace_id,category,value,origin,user_confirmed,created_at,updated_at,source_type) VALUES(?,?,?,?, 'user_declared',1,1,1,'user_entered')",(node,self.w,'goal','不能丢失的旧学费目标'))
        with self.assertRaises(RuntimeError):self.store.migrate_legacy(fail_after_copy=True)
        with self.g.bank.connect() as db:
            self.assertEqual(db.execute('SELECT value FROM user_memories WHERE id=?',(node,)).fetchone()[0],'不能丢失的旧学费目标')
        self.store.migrate_legacy()
        self.store.migrate_legacy()
        self.assertEqual([x['value'] for x in self.store.rows(self.w,'user_memories')],['不能丢失的旧学费目标'])
        with self.g.bank.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM user_memories').fetchone()[0],0)
        with self.store.connection(self.w) as db:
            self.assertEqual(db.execute('SELECT status FROM migration_state').fetchone()[0],'complete')

    def test_pending_invalidation_replays_after_private_commit_failure(self):
        before=self.store.revision(self.w)
        with patch.object(self.store,'_invalidate_bank',side_effect=RuntimeError('模拟跨库第二阶段失败')):
            with self.assertRaises(RuntimeError):self.g.memories.create(self.w,{'category':'goal','value':'已提交但未完成失效的目标'})
        self.assertEqual(self.store.revision(self.w),before+1)
        with self.store.connection(self.w) as db:
            self.assertEqual(db.execute('SELECT pending_invalidation FROM personal_versions WHERE workspace_id=?',(self.w,)).fetchone()[0],1)
        self.store.recover_invalidations()
        with self.store.connection(self.w) as db:
            self.assertEqual(db.execute('SELECT pending_invalidation FROM personal_versions WHERE workspace_id=?',(self.w,)).fetchone()[0],0)
        with self.g.bank.connect() as db:self.assertEqual(db.execute('SELECT revision FROM personal_data_versions WHERE workspace_id=?',(self.w,)).fetchone()[0],before+1)

    def test_wrong_key_fails_without_overwriting_existing_data(self):
        self.g.memories.create(self.w,{'category':'goal','value':'原密钥保护的目标'})
        with self.assertRaises(BankError):PersonalDataStore(self.store.database,encryption_key=Fernet.generate_key())
        self.assertEqual(self.g.memories.state(self.w)['items'][0]['value'],'原密钥保护的目标')

    def test_save_turn_checks_both_versions_without_bank_lock_reentry(self):
        revision=self.store.revision(self.w)
        with self.g.bank.connect() as db:privacy=privacy_revision(db,self.w)
        self.assertTrue(self.store.save_turn(self.w,[{'role':'assistant','content':'当前回答'}],
                         'turn-ok',[{'type':'safe'}],expected_revision=revision,expected_privacy=privacy))
        self.assertEqual(self.store.read_history(self.w)[0]['content'],'当前回答')
        self.assertEqual(self.store.read_widgets(self.w)[0]['response_id'],'turn-ok')
        self.assertFalse(self.store.save_turn(self.w,[{'role':'assistant','content':'错误权限'}],
                          'turn-bad',[],expected_revision=revision,expected_privacy=privacy+1))
        self.assertEqual(self.store.read_history(self.w)[0]['content'],'当前回答')
        self.g.memories.set_enabled(self.w,{'enabled':False,'expected_revision':revision})
        self.assertFalse(self.store.save_turn(self.w,[{'role':'assistant','content':'迟到回答'}],
                          'turn-late',[],expected_revision=revision,expected_privacy=privacy))
        self.assertEqual(self.store.read_history(self.w),[])

    def test_delete_memory_removes_relations_and_personal_audit_has_no_body(self):
        first=self.g.memories.create(self.w,{'category':'goal','value':'需要撤回的敏感学费目标'})['items'][0]['node_id']
        second=self.g.memories.create(self.w,{'category':'liquidity_preference','value':'保留应急金'})['items'][0]['node_id']
        self.g.memories.add_relation(self.w,first,{'target_node_id':second,'relation':'supports'})
        self.g.memories.delete(self.w,first,{'expected_revision':self.store.revision(self.w)})
        self.assertEqual(self.store.rows(self.w,'user_memory_relations'),[])
        with self.store.connection(self.w) as db:
            audit=json.dumps([dict(r) for r in db.execute('SELECT * FROM personal_audit WHERE workspace_id=?',(self.w,))],ensure_ascii=False)
        self.assertNotIn('需要撤回的敏感学费目标',audit)
        self.assertFalse(any(row['id']==first for row in self.store.rows(self.w,'user_memories')))


if __name__ == '__main__':unittest.main()
