"""研究状态实际注册隔离、CAS、加密及版本；公共资料为显式工程夹具。"""
from contextlib import closing, contextmanager
import json
import sqlite3
import unittest
import uuid

import test_registered_instances as registered_fixture
from simulated_bank import BankError


class EvidenceFixture:
    """只检验provider消费，不代表真实产品资料完成。"""
    def __init__(self):self.version='fixture-evidence-1'
    def revision(self):return self.version
    @contextmanager
    def guard(self,revision):
        if revision!=self.version:raise BankError('RESEARCH_EVIDENCE_CHANGED','工程夹具版本变化。',409)
        yield self
    def detail(self,eid):return {'entity_id':eid,'entity_version':self.version,'evidence_version':self.version,'provider_revision':self.version,'facts':[],'source':'isolated_engineering_fixture'}
    def catalog(self,query):return {'items':[self.detail('fixture:share:A')],'query':query}
    def documents(self,eid):return {'entity_id':eid,'provider_revision':self.version,'items':[]}


class ResearchWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.f=registered_fixture.RegisteredInstancesTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.g=self.f.g;self.lease=self.f.select();self.w=self.lease['workspace_id'];self.service=self.g.research_workspace
        self.evidence=EvidenceFixture();self.service._provider=self.evidence

    @contextmanager
    def scope(self,lease=None):
        lease=lease or self.lease
        with self.g.demo_sessions.request(self.f.token(lease),lease['workspace_id']):yield lease['workspace_id']

    @staticmethod
    def value(name='本人研究标记'):
        return {'name':name,'goal':'核对一年持有费用','conditions':{'amount':'10000.00','currency':'CNY','holding_days':365},
                'entity_ids':['fixture:share:A'],'deadline':'2027-10-18','notes':'工程状态，不是真实条款'}

    def test_edit_history_evidence_change_and_private_cipher_preserved(self):
        with self.scope():
            first=self.service.save(self.w,{'expected_revision':0,'value':self.value(),'confirmed':True})
            rid=first['record']['id'];self.assertEqual(first['revision'],1)
            revised=self.service.save(self.w,{'expected_revision':1,'id':rid,'value':self.value('更正研究标记'),'confirmed':True})
            self.assertEqual(revised['record']['record_revision'],2)
            history=self.service.read(self.w,rid=rid,history=True)
            self.assertEqual(history['items'][0]['value']['name'],'本人研究标记')
            self.evidence.version='fixture-evidence-2'
            self.assertEqual(self.service.read(self.w,rid=rid)['record']['source_state'],'changed')
            self.assertEqual(self.service.read(self.w,rid=rid)['record']['evidence_versions']['fixture:share:A'],'fixture-evidence-1')
        with closing(sqlite3.connect(self.g.personal_data.database)) as db:
            raw=db.execute('SELECT value FROM research_workspace_records WHERE id=?',(rid,)).fetchone()[0]
            self.assertTrue(raw.startswith('pd1:'));self.assertNotIn('更正研究标记',raw)

    def test_registered_owner_persona_cas_and_retracted_history(self):
        with self.scope():
            saved=self.service.save(self.w,{'expected_revision':0,'value':self.value(),'confirmed':True});rid=saved['record']['id']
            with self.assertRaises(BankError) as e:self.service.save(self.w,{'expected_revision':0,'value':self.value(),'confirmed':True})
            self.assertEqual(e.exception.code,'RESEARCH_VERSION_CONFLICT')
            with self.assertRaises(BankError):self.service.save(self.w,{'expected_revision':1,'value':self.value(),'confirmed':True,'owner':self.f.owner})
        other=self.f.select(owner=uuid.uuid4().hex,subject='welcome-'+uuid.uuid4().hex)
        with self.scope(other) as w:
            self.assertEqual(self.service.read(w)['records'],[])
            with self.assertRaises(BankError) as e:self.service.read(w,rid=rid)
            self.assertEqual(e.exception.code,'RESEARCH_NOT_FOUND')
        persona=self.f.select('grain_farmer',expected=self.w)
        with self.scope(persona) as w:self.assertEqual(self.service.read(w)['records'],[])
        back=self.f.select('tycoon',expected=persona['workspace_id'])
        with self.scope(back):
            self.assertEqual(self.service.read(self.w)['records'][0]['id'],rid)
            self.service.delete(self.w,rid,{'expected_revision':1,'confirmed':True})
            self.assertEqual(self.service.read(self.w)['records'],[])

    def test_policy_pins_historical_contract_and_edit_needs_confirmation(self):
        policy={'label':'本人保险档案','entity_id':'fixture:policy:medical','contract_version':'original-contract-v1',
                'insured_object':{'kind':'person','id':'family-self','label':'本人'},'start_on':'2026-01-01','end_on':'2026-12-31',
                'premium':{'amount':'500.00','currency':'CNY','frequency':'yearly','next_due_on':'2026-12-15'},
                'coverages':[],'attachment_ids':[],'consent':{'purpose':'保存本人保单档案','health_data_authorized':False}}
        with self.scope():
            with self.assertRaises(BankError):self.service.save(self.w,{'expected_revision':0,'value':policy,'confirmed':False},kind='policy')
            first=self.service.save(self.w,{'expected_revision':0,'value':policy,'confirmed':True},kind='policy');rid=first['record']['id']
            self.evidence.version='fixture-evidence-2'
            updated=self.service.save(self.w,{'expected_revision':1,'id':rid,'value':{**policy,'label':'保单备注更正'},'confirmed':True},kind='policy')
            self.assertEqual(updated['record']['evidence_versions']['fixture:policy:medical'],'fixture-evidence-1')
            self.assertEqual(updated['record']['source_state'],'historical_contract')

    def test_task_bank_commit_failure_recovers_reminders_exactly_once(self):
        value={'title':'续保材料核对','kind':'renewal','record_ids':[],'due_on':'2026-11-01',
               'status':'needs_input','conditions':{},'notes':'本人确认的待办',
               'reminder':{'enabled':True,'calendar':True,'message':True}}
        original=self.g.bank.transaction
        @contextmanager
        def fail_before_bank_commit():
            with original() as bank:
                yield bank
                raise RuntimeError('controlled_bank_commit_abort')
        with self.scope():
            self.g.bank.transaction=fail_before_bank_commit
            try:
                with self.assertRaises(RuntimeError):self.service.save(self.w,{'expected_revision':0,'value':value,'confirmed':True},kind='task')
            finally:self.g.bank.transaction=original
            with closing(self.g.personal_data.connect()) as private,closing(self.g.bank.connect()) as bank:
                self.assertEqual(private.execute("SELECT count(*) FROM research_workspace_records WHERE workspace_id=?",(self.w,)).fetchone()[0],1)
                self.assertEqual(private.execute("SELECT count(*) FROM research_workspace_outbox WHERE workspace_id=? AND status='pending'",(self.w,)).fetchone()[0],1)
                self.assertEqual(bank.execute("SELECT count(*) FROM navren_messages WHERE workspace_id=? AND kind='research_task'",(self.w,)).fetchone()[0],0)
            recovered=self.service.read(self.w);record=recovered['records'][0]
            self.assertEqual(recovered['revision'],1)
            for _ in range(2):self.service.read(self.w)
            with closing(self.g.personal_data.connect()) as private,closing(self.g.bank.connect()) as bank:
                self.assertEqual(private.execute("SELECT count(*) FROM research_workspace_outbox WHERE workspace_id=? AND status='pending'",(self.w,)).fetchone()[0],0)
                self.assertEqual(bank.execute("SELECT count(*) FROM navren_messages WHERE workspace_id=? AND id=?",(self.w,record['links']['message_id'])).fetchone()[0],1)
                self.assertEqual(bank.execute("SELECT count(*) FROM calendar_series WHERE workspace_id=? AND id=?",(self.w,record['links']['calendar_id'])).fetchone()[0],1)
                self.assertEqual(bank.execute("SELECT count(*) FROM research_workspace_receipts WHERE workspace_id=?",(self.w,)).fetchone()[0],1)

    def test_wrong_public_entity_is_not_accepted_as_requested_evidence(self):
        original=self.evidence.detail
        self.evidence.detail=lambda eid:{**original(eid),'entity_id':'different:entity'}
        with self.scope(),self.assertRaises(BankError) as e:
            self.service.save(self.w,{'expected_revision':0,'value':self.value(),'confirmed':True})
        self.assertEqual(e.exception.code,'RESEARCH_DEPENDENCY_UNAVAILABLE')

    def test_actual_provider_standard_fee_comparison_and_missing_webank_fee(self):
        from research_evidence import ResearchEvidenceStore
        self.service._provider=ResearchEvidenceStore()
        ids=['fund:CN:002604','fund:CN:008213']
        versions={eid:self.service.public_detail(eid)['evidence_version'] for eid in ids}
        body={'expected_revision':0,'entity_ids':ids,'amount':'10000.00','currency':'CNY','holding_days':365,
              'channel':'manager_standard','evidence_versions':versions,'confirmed':True,'valuation_basis':'constant_gross_value'}
        with self.scope():
            saved=self.service.compare(self.w,body);result=saved['record']['value']['result']
            self.assertEqual(result['status'],'complete',result)
            rows={r['entity_id']:r for r in result['rows']}
            self.assertEqual(rows[ids[0]]['subscription_fee_minor'],14778)
            self.assertEqual(rows[ids[0]]['ongoing_fee_estimate_minor'],9852)
            self.assertEqual(rows[ids[0]]['scenario_proceeds_minor'],975370)
            self.assertEqual(rows[ids[1]]['ongoing_fee_estimate_minor'],14000)
            self.assertEqual(rows[ids[1]]['scenario_proceeds_minor'],986000)
            self.assertTrue(all(not r['future_return_prediction'] for r in result['rows']))
            original_id=saved['record']['id']
            recalculated=self.service.compare(self.w,{**body,'expected_revision':1,'id':original_id,'holding_days':180})
            self.assertEqual(recalculated['record']['record_revision'],2)
            self.assertEqual(self.service.read(self.w,rid=original_id,history=True)['items'][0]['value']['holding_days'],365)
            missing=self.service.compare(self.w,{**body,'expected_revision':2,'channel':'WEBANK'})['record']['value']['result']
            self.assertNotEqual(missing['status'],'complete')
            self.assertTrue(missing['missing'])
            self.assertTrue(all(r.get('scenario_proceeds_minor') is None for r in missing['rows']))

    def test_attachment_exact_original_encrypted_isolated_idempotent_and_process_restart(self):
        import hashlib
        import subprocess
        import sys
        import base64
        body=b'%PDF-1.7\n'+b'private-health-original\x00'*60_000+b'\n%%EOF'
        with self.scope():
            saved=self.service.upload(self.w,body,mime_type='application/pdf',upload_key='owned-original',
                expected_revision=0,confirmed=True,health_data_authorized=True)
            aid=saved['attachment']['id']
            self.assertEqual(saved['attachment']['sha256'],hashlib.sha256(body).hexdigest())
            self.assertEqual(self.service.upload(self.w,body,mime_type='application/pdf',upload_key='owned-original',
                expected_revision=0,confirmed=True,health_data_authorized=True)['revision'],1)
            self.assertEqual(self.service.attachment(self.w,aid)[1],body)
            with self.assertRaises(BankError):self.service.upload(self.w,body+b'x',mime_type='application/pdf',upload_key='owned-original',expected_revision=1,confirmed=True,health_data_authorized=True)
            with self.assertRaises(BankError):self.service.upload(self.w,body,mime_type='application/pdf',upload_key='unauthorized',expected_revision=1,confirmed=True,health_data_authorized=False)
        with closing(sqlite3.connect(self.g.personal_data.database)) as private:
            raw=private.execute('SELECT payload_cipher FROM research_workspace_attachments WHERE id=?',(aid,)).fetchone()[0]
            self.assertNotIn(b'private-health-original',raw)
        script='''import sys,json,hashlib
from finance_gateway import FinanceGateway
x=json.load(sys.stdin)
g=FinanceGateway(x['path'],financial_database=x['financial'],registered_mode=True)
g.bank.clock=lambda:x['clock']
with g.demo_sessions.request(x['token'],x['workspace']):
 meta,body=g.research_workspace.attachment(x['workspace'],x['id'])
 print(json.dumps({'sha256':hashlib.sha256(body).hexdigest(),'byte_count':len(body),'revision':meta['revision']}))
'''
        child=subprocess.run([sys.executable,'-W','error','-c',script],input=json.dumps({'path':str(self.f.path),
            'financial':str(registered_fixture.FINANCIAL),'clock':self.f.now[0],'token':self.f.token(self.lease),
            'workspace':self.w,'id':aid}),capture_output=True,text=True,timeout=30,check=True)
        self.assertEqual(json.loads(child.stdout),{'sha256':hashlib.sha256(body).hexdigest(),'byte_count':len(body),'revision':1})
        other=self.f.select('grain_farmer',expected=self.w)
        with self.scope(other) as w:
            with self.assertRaises(BankError):self.service.attachment(w,aid)
        back=self.f.select('tycoon',expected=other['workspace_id'])
        with self.scope(back):
            self.service.delete_attachment(self.w,aid,{'expected_revision':1,'confirmed':True})
            with self.assertRaises(BankError):self.service.attachment(self.w,aid)

    def reserve_task(self,related=()):
        with self.scope():
            account=next(a for a in self.g.overview(self.w)['accounts'] if a['balance_minor']>100000)
            state=self.service.read(self.w)
            value={'title':'预留保费和房租，余款暂留现金','kind':'reserve','record_ids':list(related),'due_on':'2026-11-01',
                'status':'ready','conditions':{'amount':'100.00','currency':'CNY','account_id':account['id']},
                'reminder':{'enabled':True,'calendar':True,'message':True}}
            saved=self.service.save(self.w,{'expected_revision':state['revision'],'value':value,'confirmed':True},kind='task')
            return saved,{'expected_revision':saved['revision'],'record_id':saved['record']['id'],'action':'reserve_funds',
                'account_id':account['id'],'amount':'100.00','expires_on':'2026-11-01','idempotency_key':uuid.uuid4().hex,
                'execution_choice':'reserve_and_keep_cash','confirmed':True}

    def confirm_research(self,op):
        from test_sms_permissions import LocalSms,CODE
        self.g.bank.permissions.provider=LocalSms(self.g.bank.clock);self.g.bank.permissions.allow_test_provider=True
        payload={'confirmed':True,'confirmation_digest':op['confirmation_digest']}
        if op['permission']=='red' and self.g.bank.operation(self.w,op['id'])['state']=='pending_confirmation':
            challenge=self.g.bank.start_sms_verification(self.w,op['id'])
            payload.update(challenge_id=challenge['challenge_id'],verification_code=CODE)
        return self.g.confirm(self.w,op['id'],payload)

    def test_research_reserve_uses_original_confirmation_hold_receipt_and_edit_guard(self):
        saved,body=self.reserve_task()
        with self.scope():
            prepared=self.service.prepare(self.w,body);op=prepared['operation']
            self.assertFalse(prepared['public_share_execution'])
            self.assertEqual(self.service.operation(self.w,op['id'])['operation']['state'],'pending_confirmation')
            before=self.g.bank.snapshot(self.w)['accounts']
            result=self.confirm_research(op)
            self.assertEqual(result['operation']['state'],'succeeded')
            self.assertEqual(self.g.bank.snapshot(self.w)['accounts'],before)
            holds=[h for h in self.g.overview(self.w)['fund_holds'] if h['task_id']==op['payload']['task_id']]
            self.assertEqual([(h['status'],h['amount_minor']) for h in holds],[('held',10000)])
            with self.assertRaises(BankError):self.g.bank.start_sms_verification(self.w,op['id'])
            replay=self.confirm_research(op)
            self.assertEqual(replay['operation']['result']['hold_id'],result['operation']['result']['hold_id'])
            with self.assertRaises(BankError) as error:self.service.prepare(self.w,{**body,'action':'purchase'})
            self.assertEqual(error.exception.code,'RESEARCH_MAPPING_UNAVAILABLE')
        saved,body=self.reserve_task()
        with self.scope():
            prepared=self.service.prepare(self.w,body);op=prepared['operation']
            changed={**saved['record']['value'],'title':'修改后的资金安排'}
            self.service.save(self.w,{'expected_revision':prepared['revision'],'id':saved['record']['id'],'value':changed,'confirmed':True},kind='task')
            with self.assertRaises(BankError):self.confirm_research(op)
            self.assertEqual(self.g.bank.operation(self.w,op['id'])['state'],'invalidated')

    def test_actual_provider_worker_raw_replay_and_controlled_publication_blocks_old_confirmation(self):
        import hashlib
        import io
        from pathlib import Path
        import shutil
        from research_evidence import ResearchEvidenceStore
        from webank_catalog import PublicProductCatalog,URL
        root=Path(self.f.temp.name)/'public-evidence'
        shutil.copytree(registered_fixture.ROOT/'data/research_evidence',root,
            ignore=shutil.ignore_patterns('*.sqlite3','*.sqlite3-*','*.lock'))
        provider=ResearchEvidenceStore(root)
        self.service._provider=provider
        manifest=json.loads((root/'manifest.json').read_text())
        original=(root/manifest['catalog']['path']).read_bytes()
        current=[original]
        class Response(io.BytesIO):
            status=200
            headers={'ETag':'isolated-source-replay'}
            def geturl(self):return URL
        catalog=PublicProductCatalog(opener=lambda request,timeout:Response(current[0]),data_dir=Path(self.f.temp.name)/'catalog')
        catalog.set_snapshot_consumer(self.service.publish_catalog_snapshot)
        before=provider.revision()
        self.assertTrue(catalog.refresh(force=True))
        self.assertEqual(provider.revision(),before)
        self.assertEqual(catalog.sync_status()['catalog_sync']['research_publication']['status'],'unchanged')
        self.assertIsNone(catalog._thread)  # 直接复用refresh，未创建第二抓取线程。
        with self.scope():
            value={**self.value(),'entity_ids':['fund:CN:002604']}
            research=self.service.save(self.w,{'expected_revision':0,'value':value,'confirmed':True})
        saved,body=self.reserve_task([research['record']['id']])
        with self.scope():prepared=self.service.prepare(self.w,body)
        # 受控源更新：以真实原件的同一JSONP结构回放变化，经现役worker与实际provider发布。
        # 这不是宣称微众已发布新内容；真实在线抓取另有current-source-probe.json。
        raw=json.loads(original.decode().strip()[len('_jsonpproductpresentation('):].rstrip(';').rstrip(')'))
        target=next(r for r in raw['productPresentation'] if r['code']=='002604')
        target['saleStatus']='controlled_test_changed_status'
        current[0]=('_jsonpproductpresentation('+json.dumps(raw,ensure_ascii=False,separators=(',',':'))+');').encode()
        self.assertTrue(catalog.refresh(force=True))
        publication=catalog.sync_status()['catalog_sync']['research_publication']
        self.assertEqual(publication['status'],'published')
        self.assertNotEqual(provider.revision(),before)
        self.assertEqual(publication['source_date'],raw['extractDate'])
        with self.scope():
            self.assertEqual(self.service.read(self.w,rid=research['record']['id'])['record']['source_state'],'changed')
            self.assertEqual(self.g.bank.operation(self.w,prepared['operation']['id'])['state'],'invalidated')
            with self.assertRaises(BankError):self.confirm_research(prepared['operation'])
            self.assertFalse(any(h['task_id']==prepared['operation']['payload']['task_id'] for h in self.g.overview(self.w)['fund_holds']))
        last=provider.revision();current[0]=b'bad public document'
        self.assertTrue(catalog.refresh(force=True))
        self.assertEqual(catalog.sync_status()['fetch_status'],'failed')
        self.assertEqual(provider.revision(),last)
        reloaded=ResearchEvidenceStore(root)
        self.assertEqual(reloaded.revision(),last)
        self.assertEqual(reloaded.document_bytes(manifest['catalog']['document_id'],revision=before),original)
        import threading
        caught=threading.Event()
        restarted=PublicProductCatalog(opener=lambda *a,**k:(_ for _ in ()).throw(OSError('controlled_network_offline')),
            data_dir=Path(self.f.temp.name)/'catalog')
        def catchup(body,**metadata):
            receipt=self.service.publish_catalog_snapshot(body,**metadata);caught.set();return receipt
        restarted.set_snapshot_consumer(catchup)
        try:
            self.assertTrue(restarted.start())
            self.assertTrue(caught.wait(15),'cached original not published on worker startup')
            self.assertEqual(provider.revision(),last)
        finally:self.assertTrue(restarted.stop(timeout=3))


    def test_reserve_calculation_trusted_cash_and_existing_policies_remain_visible(self):
        with self.scope():
            policies=self.service.read(self.w,kind='policy')
            self.assertEqual(policies['items'],[])
            self.assertTrue(policies['existing_policy_records']['items'])
            account=next(a for a in self.g.overview(self.w)['accounts'] if a['balance_minor']>100000)
            state=self.g.overview(self.w)
            from datetime import date,timedelta
            until=(date.fromisoformat(state['as_of'])+timedelta(days=30)).isoformat()
            calc=self.service.calculate(self.w,{'expected_revision':0,'method':'reserve_obligations',
                'account_id':account['id'],'until':until,'policy_record_ids':[],'confirmed':True})
            result=calc['record']['value']['result'];self.assertEqual(result['status'],'complete',result)
            inputs=calc['record']['value']['inputs']
            self.assertEqual(inputs['available_cash']['value'],format(account['balance_minor']/100,'.2f'))
            self.assertTrue(calc['record']['value']['source_scope']['inventory_scope'])
            with self.assertRaises(BankError):self.service.calculate(self.w,{'expected_revision':1,'method':'reserve_obligations',
                'account_id':account['id'],'until':until,'confirmed':True,'available_cash':{'value':'99999999','state':'verified'}})

    def test_process_crash_after_private_commit_recovers_pending_outbox_once(self):
        import subprocess
        import sys
        script='''import json,sys,os
from finance_gateway import FinanceGateway
x=json.load(sys.stdin);g=FinanceGateway(x['path'],financial_database=x['financial'],registered_mode=True)
g.bank.clock=lambda:x['clock']
with g.demo_sessions.request(x['token'],x['workspace']):
 g.research_workspace._failpoint=lambda stage:os._exit(71)
 g.research_workspace.save(x['workspace'],{'expected_revision':0,'confirmed':True,'value':{
  'title':'重启恢复续保资料提醒','kind':'claim_materials','record_ids':[],'due_on':'2026-11-01',
  'status':'needs_input','conditions':{},'reminder':{'enabled':True,'calendar':True,'message':True}}},kind='task')
'''
        child=subprocess.run([sys.executable,'-W','error','-c',script],input=json.dumps({'path':str(self.f.path),
            'financial':str(registered_fixture.FINANCIAL),'clock':self.f.now[0],'token':self.f.token(self.lease),
            'workspace':self.w}),capture_output=True,text=True,timeout=30)
        self.assertEqual(child.returncode,71,child.stderr)
        with closing(self.g.personal_data.connect()) as private,closing(self.g.bank.connect()) as bank:
            self.assertEqual(private.execute("SELECT count(*) FROM research_workspace_outbox WHERE workspace_id=? AND status='pending'",(self.w,)).fetchone()[0],1)
            self.assertEqual(bank.execute('SELECT count(*) FROM research_workspace_receipts WHERE workspace_id=?',(self.w,)).fetchone()[0],0)
        with self.scope():
            first=self.service.read(self.w);again=self.service.read(self.w)
            self.assertEqual(first,again)
            self.assertEqual(first['revision'],1)
        with closing(self.g.personal_data.connect()) as private,closing(self.g.bank.connect()) as bank:
            self.assertEqual(private.execute("SELECT count(*) FROM research_workspace_outbox WHERE workspace_id=? AND status='pending'",(self.w,)).fetchone()[0],0)
            self.assertEqual(bank.execute("SELECT count(*) FROM navren_messages WHERE workspace_id=? AND kind='research_task'",(self.w,)).fetchone()[0],1)
            self.assertEqual(bank.execute('SELECT count(*) FROM calendar_series WHERE workspace_id=?',(self.w,)).fetchone()[0],1)
            self.assertEqual(bank.execute('SELECT count(*) FROM research_workspace_receipts WHERE workspace_id=?',(self.w,)).fetchone()[0],1)

    def test_actual_insurance_pension_adapters_report_unrepresented_rules_without_claim(self):
        from research_evidence import ResearchEvidenceStore
        self.service._provider=ResearchEvidenceStore()
        eid='insurance:CN:ZHONGAN:2052674-2052369-V1'
        policy={'label':'本人条件情景档案','entity_id':eid,'contract_version':'user-confirmed-conditional-context-v1',
            'insured_object':{'kind':'person','id':'self','label':'本人'},'start_on':'2026-01-01','end_on':'2026-12-31',
            'premium':{'amount':'500.00','currency':'CNY','frequency':'yearly','next_due_on':'2026-12-15'},
            'coverages':[],'attachment_ids':[],'consent':{'purpose':'本人保障研究','health_data_authorized':True}}
        with self.scope():
            existing=self.service.read(self.w,kind='policy')['existing_policy_records']
            original=existing['items'][0]
            policy.update(existing_policy_id=original.get('policy_id',original.get('id')),existing_policy_version=existing['dataset_version'])
            saved=self.service.save(self.w,{'expected_revision':0,'value':policy,'confirmed':True},kind='policy')
            scenario={'event_date':'2026-10-18','conditions':{'in_force':True,'covered_event':True,'exclusions_cleared':True,'provider_eligible':True},
                'costs':[{'cost_id':'bill-1','amount':'10000.00','other_reimbursements':'0.00'}],'fixed_events':[]}
            calc=self.service.calculate(self.w,{'expected_revision':1,'method':'check_coverage_scenario','entity_id':eid,
                'policy_record_id':saved['record']['id'],'scenario':scenario,'confirmed':True})
            result=calc['record']['value']['result'];self.assertEqual(result['status'],'missing',result)
            self.assertFalse(result['execution_authority']);self.assertIsNone(result['calculations'])
            pension=self.service.calculate(self.w,{'expected_revision':2,'method':'pension_cashflows',
                'entity_id':'pension:CN:personal-pension-account-2025-39','day_basis':365,'confirmed':True})
            self.assertEqual(pension['record']['value']['result']['status'],'missing')
            self.assertEqual(self.service.read(self.w,kind='policy')['existing_policy_records']['items'],existing['items'])
            with self.assertRaises(BankError):self.service.save(self.w,{'expected_revision':3,'id':saved['record']['id'],
                'value':{**policy,'entity_id':'insurance:CN:ZHONGAN:2042150-2040360-V1'},'confirmed':True},kind='policy')

    def test_same_clock_pending_save_cancel_delete_replays_in_workspace_revision_order(self):
        value={'title':'故障后应撤销的提醒','kind':'renewal','record_ids':[],'due_on':'2026-11-01',
            'status':'ready','conditions':{},'reminder':{'enabled':True,'calendar':True,'message':True}}
        original=self.g.bank.transaction
        @contextmanager
        def abort_bank_commit():
            with original() as bank:
                yield bank
                raise RuntimeError('controlled_abort_same_clock')
        with self.scope():
            self.g.bank.transaction=abort_bank_commit
            try:
                with self.assertRaises(RuntimeError):self.service.save(self.w,{'expected_revision':0,'value':value,'confirmed':True},kind='task')
                with closing(self.g.personal_data.connect()) as private:
                    rid=private.execute('SELECT id FROM research_workspace_records WHERE workspace_id=?',(self.w,)).fetchone()[0]
                with self.assertRaises(RuntimeError):self.service.save(self.w,{'expected_revision':1,'id':rid,'value':{**value,'status':'cancelled'},'confirmed':True},kind='task')
                with self.assertRaises(RuntimeError):self.service.delete(self.w,rid,{'expected_revision':2,'confirmed':True})
            finally:self.g.bank.transaction=original
            self.assertEqual(self.service.read(self.w)['records'],[])
            self.service.read(self.w)
            with closing(self.g.bank.connect()) as bank,closing(self.g.personal_data.connect()) as private:
                self.assertEqual(bank.execute('SELECT count(*) FROM research_workspace_receipts WHERE workspace_id=?',(self.w,)).fetchone()[0],3)
                self.assertEqual(bank.execute("SELECT count(*) FROM navren_messages WHERE workspace_id=? AND kind='research_task' AND status!='cancelled'",(self.w,)).fetchone()[0],0)
                self.assertEqual(bank.execute("SELECT count(*) FROM calendar_series WHERE workspace_id=? AND status='active'",(self.w,)).fetchone()[0],0)
                versions=[json.loads(r['value'])['workspace_revision'] for r in private.execute('SELECT value FROM research_workspace_outbox WHERE workspace_id=?',(self.w,))]
                self.assertEqual(set(versions),{1,2,3})

    def test_policy_change_invalidates_reserve_through_calculation_dependency(self):
        policy={'label':'本人预留缴费档案','entity_id':None,'contract_version':'user-declared-v1',
            'insured_object':{'kind':'person','id':'self','label':'本人'},'start_on':'2026-01-01','end_on':'2026-12-31',
            'premium':{'amount':'500.00','currency':'CNY','frequency':'yearly','next_due_on':'2026-10-20'},
            'coverages':[],'attachment_ids':[],'consent':{'purpose':'本人缴费研究','health_data_authorized':False}}
        with self.scope():
            saved=self.service.save(self.w,{'expected_revision':0,'value':policy,'confirmed':True},kind='policy')
            account=next(a for a in self.g.overview(self.w)['accounts'] if a['balance_minor']>100000)
            calc=self.service.calculate(self.w,{'expected_revision':1,'method':'reserve_obligations','account_id':account['id'],
                'until':'2026-11-01','policy_record_ids':[saved['record']['id']],'confirmed':True})
        task,body=self.reserve_task([calc['record']['id']])
        with self.scope():
            task_value=task['record']['value']
            task_value['conditions']['obligation_ids']=['policy:'+saved['record']['id']]
            task=self.service.save(self.w,{'expected_revision':task['revision'],'id':task['record']['id'],'value':task_value,'confirmed':True},kind='task')
            body['expected_revision']=task['revision']
            prepared=self.service.prepare(self.w,body)
            corrected={**policy,'premium':{**policy['premium'],'amount':'600.00'}}
            self.service.save(self.w,{'expected_revision':prepared['revision'],'id':saved['record']['id'],'value':corrected,'confirmed':True},kind='policy')
            self.assertEqual(self.g.bank.operation(self.w,prepared['operation']['id'])['state'],'invalidated')
            with self.assertRaises(BankError):self.confirm_research(prepared['operation'])
            with self.assertRaises(BankError) as error:self.service.prepare(self.w,{**body,'expected_revision':prepared['revision']+1,'idempotency_key':uuid.uuid4().hex})
            self.assertEqual(error.exception.code,'RESEARCH_VERSION_CONFLICT')

    def test_public_rules_private_usage_and_pension_date_anchor_are_bound_separately(self):
        # 显式工程规范夹具，不是已取得真实规范合同正例。
        from copy import deepcopy
        from decimal import Decimal
        from research_methods import check_coverage_scenario,pension_cashflows
        medical='fixture:insurance:rules';pension='fixture:pension:rules'
        rules={'schema_version':1,'scope':'single-general-responsibility-engineering-fixture','representation_complete':True,'unrepresented_terms':[],
            'annual_period_basis':'policy_year','waiting_by_category':{'ordinary':{'days':30,'start_day_counts_as_one':True,'end_inclusive':True}},
            'tier_basis':{'general':'annual_cumulative_eligible_cost'},'mutually_exclusive_groups':[],
            'terms_template':{'deduction_order':['other_reimbursements','deductible','ratio','limits'],'policy_annual_limit':'1000.00',
                'shared_limits':[{'group_id':'shared','limit':'1000.00'}],'deductibles':[{'deductible_id':'base','amount':'0.00','period':'annual'}],
                'responsibilities':[{'responsibility_id':'general','mechanism':'expense_compensation','shared_limit_id':'shared','deductible_id':'base',
                    'annual_limit':'1000.00','per_claim_limit':'1000.00','reimbursement_tiers':[{'up_to':'100.00','rate':'0.60'},{'up_to':None,'rate':'1.00'}]}],
                'payment_order':['general']}}
        pension_rules={'schema_version':1,'scenario_id':'fixture:pension:benchmark','source_scenario':'explicit engineering example, not insurer disclosure',
            'coordinate_basis':'calendar_anniversary','complete':True,'projection_end_offset_years':1,'projection_end_offset_months':0,
            'terminal_basis':'explicit fixture guaranteed terminal','unsupported_terms':[],
            'items':[{'cashflow_id':'premium','offset_years':0,'offset_months':0,'offset_days':0,'amount':'-100.00','kind':'premium','guaranteed':True},
                {'cashflow_id':'terminal','offset_years':1,'offset_months':0,'offset_days':0,'amount':'110.00','kind':'terminal_value','guaranteed':True},
                {'cashflow_id':'illustration','offset_years':1,'offset_months':0,'offset_days':0,'amount':'20.00','kind':'benefit','guaranteed':False}]}
        def detail(eid):
            sid='fixture:public-contract:'+eid
            field='coverage_scenario_rules' if eid==medical else 'pension_cashflow_rule_set'
            value=rules if eid==medical else pension_rules
            fact={'entity_id':eid,'field':field,'value':deepcopy(value),'state':'verified','source_id':sid,'source_version':'fixture-contract-v1',
                'currency':'CNY','effective_from':'2026-01-01','effective_to':None,'locator':{'page':1,'scope':'engineering_fixture'}}
            return {'entity_id':eid,'entity_version':'fixture-evidence-1','evidence_version':'fixture-evidence-1','provider_revision':'fixture-evidence-1',
                'facts':[fact],'source_versions':{sid:'fixture-contract-v1'},'source':'isolated_engineering_fixture'}
        self.evidence.detail=detail
        policy={'label':'工程情景保单','entity_id':medical,'contract_version':'engineering-fixture-v1',
            'insured_object':{'kind':'person','id':'self','label':'本人'},'start_on':'2026-01-01','end_on':'2026-12-31',
            'premium':{'amount':'100.00','currency':'CNY','frequency':'yearly','next_due_on':'2026-12-15'},
            'coverages':[],'attachment_ids':[],'consent':{'purpose':'工程条件核验','health_data_authorized':True}}
        context={'usage_period_start':'2026-01-01','usage_period_end':'2026-12-31','policy_annual_used':'30.00','shared_used':{'shared':'30.00'},
            'deductible_used':{'base':'0.00'},'responsibility_used':{'general':'30.00'},'responsibility_cost_ids':{'general':['bill-1']},
            'prior_eligible_costs':{'general':'50.00'},'waiting_category':'ordinary'}
        scenario={'event_date':'2026-10-18','conditions':{'in_force':True,'covered_event':True,'exclusions_cleared':True,'provider_eligible':True},
            'costs':[{'cost_id':'bill-1','amount':'100.00','other_reimbursements':'0.00'}],'fixed_events':[]}
        with self.scope():
            saved=self.service.save(self.w,{'expected_revision':0,'value':policy,'confirmed':True},kind='policy')
            request={'expected_revision':1,'method':'check_coverage_scenario','entity_id':medical,'policy_record_id':saved['record']['id'],
                'coverage_context':context,'scenario':scenario,'confirmed':True}
            calculated=self.service.calculate(self.w,request);value=calculated['record']['value'];result=value['result']
            self.assertEqual(result['status'],'complete',result)
            self.assertEqual(result['rows'][0]['conditional_payment_minor'],8000)
            self.assertEqual(value['inputs']['terms']['private_assumptions_state'],'user_confirmed_not_insurer_verified')
            public=calculated['record']['evidence'][medical]['entity']
            self.assertFalse(any(k.startswith('research-derived:') for k in public['source_versions']))
            self.assertNotIn('policy_annual_used',public['facts'][0]['value']['terms_template'])
            with self.assertRaises(BankError):self.service.calculate(self.w,{**request,'expected_revision':2,
                'coverage_context':{**context,'usage_period_start':'2025-01-01','usage_period_end':'2025-12-31'}})
            missing=self.service.calculate(self.w,{**request,'expected_revision':2,'coverage_context':{**context,'policy_annual_used':None}})
            self.assertEqual(missing['record']['value']['result']['status'],'missing')
            pension_result=self.service.calculate(self.w,{'expected_revision':3,'method':'pension_cashflows','entity_id':pension,
                'pension_context':{'scenario_id':'fixture:pension:benchmark','policy_start_on':'2026-01-01'},'confirmed':True})
            actual=pension_result['record']['value'];self.assertEqual(actual['result']['status'],'complete',actual['result'])
            self.assertEqual(actual['inputs']['cashflow_schedule']['value']['projection_end'],'2027-01-01')
            totals=actual['result']['calculations']
            self.assertEqual(Decimal(totals['guaranteed_IRR']['value']),Decimal('0.10'))
            self.assertEqual(Decimal(totals['illustrative_IRR']['value']),Decimal('0.30'))
            self.assertFalse(totals['illustrative_is_guaranteed'])
            with self.assertRaises(BankError):self.service.calculate(self.w,{**request,'expected_revision':4,
                'coverage_context':{**context,'reimbursement_rate':'1.00'}})
            self.assertIn('本人确认的周年日',actual['inputs']['cashflow_schedule']['locator']['private_binding']['date_convention'])

    def test_actual_published_medical_and_critical_templates_bind_private_scenarios(self):
        from copy import deepcopy
        from research_evidence import ResearchEvidenceStore
        self.service._provider=ResearchEvidenceStore()
        entities=['insurance:CN:ZHONGAN:2052674-2052369-V1','insurance:CN:ZHONGAN:2042150-2040360-V1']
        with self.scope():
            revision=0
            for index,eid in enumerate(entities):
                medical=index==0
                public=self.service.public_detail(eid)
                template=next(f for f in public['entity']['facts'] if f['field']=='coverage_scenario_terms')
                policy={'label':'真实公开规则与明确合成私人情景','entity_id':eid,'contract_version':'synthetic-private-matching-plan-v1',
                    'insured_object':{'kind':'person','id':'synthetic-self','label':'合成情景本人'},'start_on':'2026-01-01','end_on':'2026-12-31',
                    'premium':{'amount':'500.00','currency':'CNY','frequency':'yearly','next_due_on':'2026-12-15'},
                    'coverages':[],'attachment_ids':[],'consent':{'purpose':'已确认的条件研究，不是保险公司核赔','health_data_authorized':True}}
                saved=self.service.save(self.w,{'expected_revision':revision,'value':policy,'confirmed':True},kind='policy');revision=saved['revision']
                binding=[]
                for declared in template['private_bindings']:
                    pointer=declared['pointer']
                    if declared['scope']!='terms_value' or pointer in ('/policy_start','/policy_end'):continue
                    if pointer=='/waiting_end':v='2026-01-31' if medical else '2026-04-01'
                    elif pointer=='/waiting_end_inclusive':v=True
                    elif pointer=='/usage_period_start':v='2026-01-01'
                    elif pointer=='/usage_period_end':v='2026-12-31'
                    elif pointer=='/policy_annual_limit':v='unlimited'  # 明确合成私人合同条件，不是公开默认。
                    elif pointer.endswith('/cost_ids'):v=['general_ordinary_diagnosis_treatment'] if pointer=='/responsibilities/0/cost_ids' else []
                    else:v='0.00'  # 每一私人空位在本合成情景显式确认0，生产接口无默认值。
                    binding.append({'pointer':pointer,'value':v})
                facts={'optional_responsibility_ids':[],'prior_claim_count':0,'prior_payout_count':0,'prior_eligible_medical_expenses':'0.00'}
                if medical:facts.update(social_insured=True,social_settled=True,special_disease_reduction_applies=False,
                    cost_categories={'general_ordinary_diagnosis_treatment':'ordinary_general_medical_no_sublimit'})
                else:facts.update(functional_impairment_event=False,default_fixed_terms_confirmed=True,policy_cap_confirmed=True)
                context={'template_id':template['template_id'],'bindings':binding,
                    'attestations':{c['id']:True for c in template['supported_scenario_constraints']},'scenario_facts':facts}
                scenario={'event_date':'2026-10-19','conditions':{'in_force':True,'covered_event':True,'exclusions_cleared':True,'provider_eligible':True},
                    'costs':[{'cost_id':'general_ordinary_diagnosis_treatment','amount':'50000.00','other_reimbursements':'10000.00'}] if medical else [],
                    'fixed_events':[] if medical else ['ZXG316']}
                request={'expected_revision':revision,'method':'check_coverage_scenario','entity_id':eid,
                    'policy_record_id':saved['record']['id'],'coverage_context':context,'scenario':scenario,'confirmed':True}
                calculated=self.service.calculate(self.w,request);revision=calculated['revision'];value=calculated['record']['value'];result=value['result']
                self.assertEqual(result['status'],'complete',result)
                self.assertEqual(value['inputs']['policy_id'],saved['record']['id'])
                self.assertEqual(value['inputs']['terms']['entity_id'],saved['record']['id'])
                self.assertEqual(value['inputs']['terms']['public_entity_id'],eid)
                self.assertEqual(result['calculations']['expense_compensation_minor'],3_200_000 if medical else 0)
                self.assertEqual(result['calculations']['fixed_benefit_minor'],0 if medical else 20_000_000)
                self.assertFalse(result['calculations']['claim_approved'])
                self.assertFalse(result['execution_authority'])
                # 降低比例分支/重复首次赔付均不得被模板默默忽略。
                bad=deepcopy(context)
                if medical:bad['scenario_facts']['social_settled']=False
                else:bad['scenario_facts']['prior_payout_count']=1
                refused=self.service.calculate(self.w,{**request,'expected_revision':revision,'coverage_context':bad})
                revision=refused['revision'];self.assertEqual(refused['record']['value']['result']['status'],'missing')
                absent=deepcopy(context);absent['bindings'].pop()
                refused=self.service.calculate(self.w,{**request,'expected_revision':revision,'coverage_context':absent})
                revision=refused['revision'];self.assertEqual(refused['record']['value']['result']['status'],'missing')

    def test_deep_refresh_singleflight_light_recovery_and_failed_check_keep_original(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        class RefreshFixture(EvidenceFixture):
            def __init__(self):
                super().__init__();self.calls=0;self.entered=threading.Event();self.release=threading.Event();self.adapter=None
            def refresh_plan(self,eid):return {'entity_id':eid,'provider_revision':self.version,'performance_supported':True,
                'nav_interval_seconds':3600,'documents':[{'due':True}]}
            def freshness(self,eid):return {'entity_id':eid,'performance_check':None,'checks':[]}
            def refresh_entity(self,eid,**kwargs):
                self.calls+=1;self.adapter=kwargs['performance_provider'];self.entered.set()
                if not self.release.wait(10):raise AssertionError('controlled_refresh_not_released')
                return {'status':'unchanged','provider_revision':self.version,'checks':[{'status':'unavailable','checked_at':'2026-10-19T00:00:00+08:00'}],
                    'source_calls':0,'performance_status':'unavailable'}
        provider=RefreshFixture();self.service._provider=provider;self.g.product_performance=object()
        with self.scope():saved=self.service.save(self.w,{'expected_revision':0,'value':self.value(),'confirmed':True})
        # 普通目录/详情/旧研究恢复不调用任何刷新函数。
        for _ in range(3):self.service.public_detail('fixture:share:A')
        with self.scope():self.service.read(self.w)
        self.assertEqual(provider.calls,0)
        started=time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            first=pool.submit(self.service.deep_refresh,'fixture:share:A')
            self.assertTrue(provider.entered.wait(5))
            concurrent=self.service.deep_refresh('fixture:share:A')
            self.assertEqual(concurrent['refresh']['status'],'in_progress')
            with self.scope():self.assertEqual(self.service.read(self.w)['records'][0]['id'],saved['record']['id'])
            provider.release.set();cold=first.result(timeout=10)
        self.assertEqual(provider.calls,1);self.assertIs(provider.adapter,self.g.product_performance)
        self.assertEqual(cold['refresh']['source_check_status'],'failed')
        self.assertEqual(cold['evidence_version'],'fixture-evidence-1')
        warm=self.service.deep_refresh('fixture:share:A')
        self.assertEqual(warm['refresh']['status'],'warm_cache');self.assertFalse(warm['refresh']['network_recheck_performed'])
        self.assertEqual(provider.calls,1);self.assertLess(time.monotonic()-started,10)


class ProtectionFundingTests(unittest.TestCase):
    """受影响的原保护/私人义务绑定；真实隔离账本，冻结T01金额不改。"""
    setUp=ResearchWorkspaceTests.setUp
    scope=ResearchWorkspaceTests.scope
    confirm_research=ResearchWorkspaceTests.confirm_research

    def seed(self,rent_due='2026-10-10'):
        with self.g.bank.transaction() as db:
            rows=self.g.bank._accounts(db,self.w)
            self.account=next(r['id'] for r in rows if r['id'].endswith('-main'))
            db.execute('UPDATE accounts SET opening_balance_minor=0,balance_minor=0 WHERE workspace_id=?',(self.w,))
            db.execute('UPDATE accounts SET opening_balance_minor=2500000,balance_minor=2500000 WHERE workspace_id=? AND id=?',(self.w,self.account))
            db.execute('UPDATE profiles SET reserve_minor=500000 WHERE workspace_id=?',(self.w,))
            db.execute("UPDATE loan_installments SET status='paid',paid_minor=principal_minor+interest_minor+fees_minor WHERE workspace_id=?",(self.w,))
            db.execute("UPDATE credit_statements SET status='paid',paid_minor=total_due_minor WHERE workspace_id=?",(self.w,))
            db.execute("UPDATE subscriptions SET merchant_status='cancelled' WHERE workspace_id=?",(self.w,))
            db.execute("UPDATE mandates SET status='closed' WHERE workspace_id=?",(self.w,))
            db.execute("UPDATE scheduled_payments SET status='cancelled' WHERE workspace_id=?",(self.w,))
            db.execute("UPDATE fund_holds SET status='released' WHERE workspace_id=?",(self.w,))
            for label,amount,due in [('rent',350000,rent_due),('bill',50000,'2026-10-15')]:
                db.execute('INSERT INTO mandates VALUES(?,?,?,?,?,?,?,?)',('t01-'+label,self.w,self.account,label,amount,'active',due,'active'))
            db.execute('UPDATE workspaces SET version=version+1 WHERE id=?',(self.w,))

    def revision(self):return self.service.read(self.w)['revision']

    def policy(self,amount='1800.00',due='2026-10-20',**extra):
        value={'label':'明确合成的本人保费义务','entity_id':None,'contract_version':'t01-synthetic-private-v1',
            'insured_object':{'kind':'person','id':'synthetic-self','label':'合成主体'},'start_on':'2026-01-01','end_on':'2026-12-31',
            'premium':{'amount':amount,'currency':'CNY','frequency':'yearly','next_due_on':due},'coverages':[],
            'attachment_ids':[],'consent':{'purpose':'本人资金保护核对','health_data_authorized':False},**extra}
        return self.service.save(self.w,{'expected_revision':self.revision(),'value':value,'confirmed':True},kind='policy')['record']

    def quote(self,policies,until='2026-11-01'):
        return self.service.calculate(self.w,{'expected_revision':self.revision(),'method':'reserve_obligations',
            'account_id':self.account,'until':until,'policy_record_ids':[p['id'] for p in policies],'confirmed':True})

    def task(self,quote,policies,amount='1800.00',explicit=True):
        conditions={'amount':amount,'currency':'CNY','account_id':self.account}
        if explicit:conditions['obligation_ids']=['policy:'+p['id'] for p in policies]
        value={'title':'明确的本人保费资金安排','kind':'reserve','record_ids':[quote['record']['id']] if quote else [],
            'due_on':'2026-11-01','status':'ready','conditions':conditions,'reminder':{'enabled':True,'calendar':True,'message':True}}
        saved=self.service.save(self.w,{'expected_revision':self.revision(),'value':value,'confirmed':True},kind='task')
        return {'expected_revision':saved['revision'],'record_id':saved['record']['id'],'action':'reserve_funds',
            'account_id':self.account,'amount':amount,'expires_on':'2026-11-01','idempotency_key':uuid.uuid4().hex,
            'execution_choice':'reserve_and_keep_cash','confirmed':True}

    def denied(self,code,callback):
        with self.assertRaises(BankError) as e:callback()
        self.assertIn(e.exception.code,code if isinstance(code,tuple) else (code,))

    def test_t01_only_new_1800_hold_recompute_replay_and_process_recovery(self):
        import subprocess,sys
        with self.scope():
            self.seed();p=self.policy();q=self.quote([p]);v=q['record']['value'];c=v['result']['calculations']
            self.assertEqual((c['required_reserve_minor'],c['additional_reserve_minor'],c['uncommitted_cash_minor'],c['already_protected_minor']),
                (1080000,180000,1420000,900000))
            self.assertEqual(sum(row['additional_reserve_minor'] for row in v['result']['rows']),180000)
            self.assertEqual(sum(row['bank_protected_minor'] for row in v['result']['bank_protection_rows']),400000)
            self.assertEqual(self.g.budget(self.w,self.account)['investable_minor'],1600000)
            body=self.task(q,[p]);before=self.g.budget(self.w,self.account)
            prepared=self.service.prepare(self.w,body);self.assertEqual(self.g.budget(self.w,self.account),before)
            result=self.confirm_research(prepared['operation']);hid=result['operation']['result']['hold_id']
            self.assertEqual(self.confirm_research(prepared['operation'])['operation']['result']['hold_id'],hid)
            bank=self.g.budget(self.w,self.account)
            self.assertEqual((bank['cash_minor'],bank['held_minor'],bank['investable_minor']),(2500000,180000,1420000))
            after=self.quote([p]);av=after['record']['value'];ac=av['result']['calculations']
            self.assertEqual((ac['required_reserve_minor'],ac['additional_reserve_minor'],ac['uncommitted_cash_minor'],ac['actual_held_minor']),
                (1080000,0,1420000,180000))
            self.assertEqual(len(av['funding_inputs']['holds']),1)
            self.assertEqual(av['source_scope']['actual_hold_allocations'][0]['policy_record_id'],p['id'])
            zero=self.task(after,[p],amount='0.00')
            with closing(self.g.bank.connect()) as db:n=db.execute('SELECT count(*) FROM operations WHERE workspace_id=?',(self.w,)).fetchone()[0]
            self.denied('RESEARCH_NO_ADDITIONAL_HOLD_REQUIRED',lambda:self.service.prepare(self.w,zero))
            with closing(self.g.bank.connect()) as db:self.assertEqual(n,db.execute('SELECT count(*) FROM operations WHERE workspace_id=?',(self.w,)).fetchone()[0])
            script='''import json,sys,socket
from unittest.mock import patch
from finance_gateway import FinanceGateway
x=json.load(sys.stdin)
with patch.object(socket,'create_connection',side_effect=AssertionError('network_forbidden')):
 g=FinanceGateway(x['path'],financial_database=x['financial'],registered_mode=True);g.bank.clock=lambda:x['clock']
 with g.demo_sessions.request(x['token'],x['workspace']):
  s=g.research_workspace;r=s.read(x['workspace']);q=s.calculate(x['workspace'],{'expected_revision':r['revision'],'method':'reserve_obligations','account_id':x['account'],'until':'2026-11-01','policy_record_ids':[x['policy']],'confirmed':True})
  print(json.dumps(q['record']['value']['result']['calculations']))
'''
            child=subprocess.run([sys.executable,'-W','error','-c',script],input=json.dumps({'path':str(self.f.path),
                'financial':str(registered_fixture.FINANCIAL),'clock':self.f.now[0],'token':self.f.token(self.lease),
                'workspace':self.w,'account':self.account,'policy':p['id']}),capture_output=True,text=True,timeout=40)
            self.assertEqual(child.returncode,0,child.stderr);recovered=json.loads(child.stdout)
            self.assertEqual((recovered['additional_reserve_minor'],recovered['uncommitted_cash_minor']),(0,1420000))

    def test_full_total_rejected_missing_allocations_and_old_quote_rejected(self):
        with self.scope():
            self.seed();p=self.policy();q=self.quote([p]);body=self.task(q,[p],amount='10800.00')
            self.denied('RESEARCH_INCREMENTAL_AMOUNT_EXCEEDED',lambda:self.service.prepare(self.w,body))
            body=self.task(q,[p],explicit=False)
            self.denied('RESEARCH_ALLOCATION_REQUIRED',lambda:self.service.prepare(self.w,body))
            from copy import deepcopy
            old_value=deepcopy(q['record']['value']);old_value['algorithm_version']='navren.research-methods/1'
            old=self.service._persist(self.w,{'expected_revision':self.revision(),'confirmed':True},'comparison',old_value,{})
            body=self.task(old,[p])
            self.denied('RESEARCH_CALCULATION_REQUIRED',lambda:self.service.prepare(self.w,body))
            # 保留旧一般现金预留；不声称其覆盖保单。
            body=self.task(None,[],amount='100.00',explicit=False)
            op=self.service.prepare(self.w,body)['operation'];self.confirm_research(op)
            c=self.quote([p])['record']['value']['result']['calculations']
            self.assertEqual((c['actual_held_minor'],c['additional_reserve_minor'],c['uncommitted_cash_minor']),(10000,180000,1410000))

    def test_short_window_keeps_original_30day_bank_obligations(self):
        with self.scope():
            self.seed(rent_due='2026-11-10');p=self.policy(due='2026-10-19');q=self.quote([p],until='2026-10-20')
            v=q['record']['value'];c=v['result']['calculations']
            self.assertEqual((c['required_reserve_minor'],c['additional_reserve_minor'],c['uncommitted_cash_minor']),(1080000,180000,1420000))
            self.assertEqual(v['source_scope']['target_obligation_window_end'],'2026-10-20')
            self.assertEqual(v['source_scope']['bank_protection_window_end'],'2026-11-15')
            self.assertLessEqual(c['uncommitted_cash_minor'],self.g.budget(self.w,self.account)['investable_minor'])

    def test_multi_policy_single_real_hold_allocations_conserve_total(self):
        with self.scope():
            self.seed();ps=[self.policy(),self.policy(amount='700.00')];q=self.quote(ps)
            self.assertEqual(q['record']['value']['result']['calculations']['additional_reserve_minor'],250000)
            wrong=self.task(q,ps,amount='2400.00')
            self.denied('RESEARCH_ALLOCATION_AMOUNT_MISMATCH',lambda:self.service.prepare(self.w,wrong))
            duplicate=self.task(q,ps,amount='2500.00');task=self.service.read(self.w,rid=duplicate['record_id'])['record']
            value=task['value'];value['conditions']['obligation_ids']=['policy:'+ps[0]['id']]*2
            self.denied('RESEARCH_INVALID',lambda:self.service.save(self.w,{'expected_revision':self.revision(),'id':task['id'],'value':value,'confirmed':True},kind='task'))
            body=self.task(q,ps,amount='2500.00');op=self.service.prepare(self.w,body)['operation'];self.confirm_research(op)
            v=self.quote(ps)['record']['value'];c=v['result']['calculations']
            self.assertEqual((c['required_reserve_minor'],c['actual_held_minor'],c['additional_reserve_minor'],c['uncommitted_cash_minor']),(1150000,250000,0,1350000))
            holds=v['funding_inputs']['holds'];self.assertEqual(len(holds),1)
            self.assertEqual(sum(part for _,part in holds[0]['commitment_allocations']),holds[0]['amount_minor'])
            self.assertEqual(len(v['source_scope']['actual_hold_allocations']),2)
            # 只重算其中一保单时，整笔hold只计一次，未选部分也不会释放。
            one=self.quote(ps[:1])['record']['value']['result']['calculations']
            self.assertEqual((one['actual_held_minor'],one['additional_reserve_minor'],one['uncommitted_cash_minor']),(250000,0,1350000))

    def test_protection_inputs_changed_without_version_bump_reject_prepare_sms_confirm(self):
        with self.scope():
            self.seed();p=self.policy();q=self.quote([p]);body=self.task(q,[p])
            with self.g.bank.transaction() as db:db.execute('UPDATE profiles SET reserve_minor=500001 WHERE workspace_id=?',(self.w,))
            self.denied('RESEARCH_VERSION_CONFLICT',lambda:self.service.prepare(self.w,body))
            with self.g.bank.transaction() as db:db.execute('UPDATE profiles SET reserve_minor=500000 WHERE workspace_id=?',(self.w,))
            op=self.service.prepare(self.w,body)['operation']
            with self.g.bank.transaction() as db:db.execute("UPDATE mandates SET merchant='new-identity' WHERE workspace_id=? AND id='t01-rent'",(self.w,))
            self.denied(('RESEARCH_VERSION_CONFLICT','METRIC_VERSION_CONFLICT'),lambda:self.g.bank.start_sms_verification(self.w,op['id']))
            self.denied(('RESEARCH_VERSION_CONFLICT','METRIC_VERSION_CONFLICT'),lambda:self.confirm_research(op))

    def test_explicit_existing_policy_due_dedup_and_unlinked_equal_amount_stays_separate(self):
        with self.scope():
            self.seed();existing=self.service.read(self.w,kind='policy')['existing_policy_records'];original=existing['items'][0]
            pid=original.get('policy_id',original.get('id'));source=self.g.financial_query(self.w,'get_policy',{'policy_id':pid})['data']['policy']
            with closing(self.g.bank.connect()) as db:
                account=db.execute('SELECT account_id FROM connected_account_links WHERE workspace_id=? AND source_account_id=?',(self.w,source['payment_account_id'])).fetchone()[0]
            with closing(self.g.financial_data._connect()) as db:
                mid=db.execute('SELECT mandate_id FROM mandates WHERE subject_id=? AND contract_id=? AND account_id=?',
                    (self.g.demo_accounts.subject_for(self.w),source['contract_id'],source['payment_account_id'])).fetchone()[0]
            self.account=account
            with self.g.bank.transaction() as db:
                db.execute('UPDATE accounts SET balance_minor=0,opening_balance_minor=0 WHERE workspace_id=?',(self.w,))
                db.execute('UPDATE accounts SET balance_minor=2500000,opening_balance_minor=2500000 WHERE workspace_id=? AND id=?',(self.w,account))
                db.execute("UPDATE mandates SET status='closed' WHERE workspace_id=?",(self.w,))
                old=db.execute('SELECT 1 FROM mandates WHERE workspace_id=? AND id=?',(self.w,mid)).fetchone()
                if old:db.execute("UPDATE mandates SET account_id=?,amount_minor=50000,status='active',next_charge_date='2026-10-20',merchant_contract_status='active' WHERE workspace_id=? AND id=?",(account,self.w,mid))
                else:db.execute('INSERT INTO mandates VALUES(?,?,?,?,?,?,?,?)',(mid,self.w,account,'explicit-source-contract',50000,'active','2026-10-20','active'))
                db.execute('UPDATE workspaces SET version=version+1 WHERE id=?',(self.w,))
            extra={'existing_policy_id':pid,'existing_policy_version':existing['dataset_version']}
            p=self.policy(amount='500.00',**extra);p2=self.policy(amount='500.00',**extra);unlinked=self.policy(amount='500.00')
            q=self.quote([p,p2,unlinked]);v=q['record']['value'];c=v['result']['calculations']
            self.assertEqual((c['bank_commitments_minor'],c['additional_reserve_minor'],c['required_reserve_minor']),(50000,50000,600000))
            aliases=v['source_scope']['policy_obligation_aliases']
            self.assertEqual(aliases['policy:'+p['id']],aliases['policy:'+p2['id']])
            self.assertNotEqual(aliases['policy:'+p['id']],aliases['policy:'+unlinked['id']])
            body=self.task(q,[p],amount='500.00')
            self.denied('RESEARCH_ALREADY_PROTECTED',lambda:self.service.prepare(self.w,body))
            with closing(self.g.bank.connect()) as db:
                wrong_account=db.execute('SELECT id FROM accounts WHERE workspace_id=? AND id!=? LIMIT 1',(self.w,account)).fetchone()[0]
            self.account=wrong_account
            self.denied('RESEARCH_POLICY_ACCOUNT_MISMATCH',lambda:self.quote([p]))
            self.account=account
            # 原账单关闭后，仅以同一来源保单/版本/付款账户/缴费期去重补充档案。
            with self.g.bank.transaction() as db:
                db.execute("UPDATE mandates SET status='closed' WHERE workspace_id=? AND id=?",(self.w,mid))
                db.execute('UPDATE workspaces SET version=version+1 WHERE id=?',(self.w,))
            q=self.quote([p,p2,unlinked]);c=q['record']['value']['result']['calculations']
            self.assertEqual(c['additional_reserve_minor'],100000)
            body=self.task(q,[p,p2],amount='1000.00')
            self.denied('RESEARCH_DUPLICATE_OBLIGATION',lambda:self.service.prepare(self.w,body))
