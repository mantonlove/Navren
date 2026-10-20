"""AA消费/私有图片增量的必要边界检查；只创建合成隔离库，不运行旧全量检查。"""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

from PIL import Image
from simulated_bank import BankError
from simulated_bank import aa_evidence as evidence
import test_aa_payments as legacy

ROOT,NOW=legacy.ROOT,legacy.NOW


class AAEvidenceTests(unittest.TestCase):
    # 仅借用合成夹具/确认帮助器，不继承旧测试方法。
    setUpClass=legacy.AAPaymentsTests.__dict__['setUpClass']
    tearDownClass=legacy.AAPaymentsTests.__dict__['tearDownClass']
    setUp=legacy.AAPaymentsTests.setUp
    card=legacy.AAPaymentsTests.card
    error=legacy.AAPaymentsTests.error
    counts=legacy.AAPaymentsTests.counts
    balance=legacy.AAPaymentsTests.balance
    prepare_create=legacy.AAPaymentsTests.prepare_create
    confirm=legacy.AAPaymentsTests.confirm
    create=legacy.AAPaymentsTests.create
    prepare_pay=legacy.AAPaymentsTests.prepare_pay

    def runtime(self,u=None,amount=10000,kind='expense',merchant='隔离聚餐'):
        u=u or self.a
        with self.g.bank.transaction() as db:
            return self.g.bank._ledger(db,u['workspace_id'],self.card(u)['account_id'],
                datetime.fromtimestamp(NOW).date(),merchant,amount,-amount if kind=='expense' else amount,
                kind,None,category='餐饮',note='AA凭证隔离检查')

    def image(self,*,fmt='PNG',size=(32,24),metadata=False):
        out=io.BytesIO();img=Image.new('RGB',size,(30,90,130));options={}
        if metadata:
            exif=Image.Exif();exif[315]='PRIVATE_AUTHOR';exif[270]='PRIVATE_RECEIPT';options['exif']=exif
        img.save(out,format=fmt,**options)
        return {'mime_type':{'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp'}[fmt],
                'data_base64':base64.b64encode(out.getvalue()).decode()}

    def upload(self,u=None,**options):
        u=u or self.a
        return self.g.aa_attachment(u['workspace_id'],payload=self.image(**options))['attachment']

    def history(self,u=None):
        u=u or self.a
        with closing(self.g.financial_data._connect()) as db:
            return dict(db.execute("SELECT * FROM transactions WHERE subject_id=? AND kind='expense' AND amount_minor>10000 ORDER BY occurred_at DESC LIMIT 1",(u['user_id'],)).fetchone())

    def private_history(self):
        from financial_data_service.service import FinancialDataService
        path=Path(self.temp.name)/'history.sqlite3'
        with closing(self.g.financial_data._connect()) as source,sqlite3.connect(path) as target:source.backup(target)
        self.rebind_history(path)
        return path

    def rebind_history(self,path):
        from financial_data_service.service import FinancialDataService
        self.g.financial_data=FinancialDataService(path)
        with self.g.bank.transaction() as db:
            db.execute('UPDATE connected_asset_migrations SET source_path=?,source_sha256=?,dataset_version=?',
                (str(path.resolve()),hashlib.sha256(path.read_bytes()).hexdigest(),self.g.financial_data.dataset_version))

    def test_own_discovery_filters_safe_fields_and_foreign_invalid_sources(self):
        tx=self.runtime();before=self.counts()
        result=self.g.aa_consumptions(self.a['workspace_id'],{'q':['隔离聚餐'],'from':['2026-10-10'],'to':['2026-10-10'],'limit':['1']})
        self.assertEqual(result['total'],1);self.assertEqual(result['consumptions'][0]['id'],'runtime:'+tx)
        self.assertEqual(result['consumptions'][0]['available_minor'],10000)
        self.assertFalse(result['has_more']);self.assertEqual(self.g.aa_consumptions(self.a['workspace_id'],{'q':['隔离聚餐'],'offset':['1']})['consumptions'],[])
        self.assertEqual(set(result['consumptions'][0]),set(evidence.PUBLIC_FIELDS)|{'allocated_minor','available_minor'})
        foreign=self.runtime(self.b);credit=self.runtime(kind='refund')
        for ref in ('runtime:'+foreign,'runtime:'+credit,'history:'+self.history(self.b)['transaction_id']):
            self.error('AA_CONSUMPTION_INVALID',lambda:self.prepare_create(source_consumption_id=ref))
        self.error('INVALID_QUERY',lambda:self.g.aa_consumptions(self.a['workspace_id'],{'workspace':['other']}))
        self.error('INVALID_QUERY',lambda:self.g.aa_consumptions(self.a['workspace_id'],{'from':['2026-02-30']}))
        self.assertEqual(self.counts(),before|{'transactions':before['transactions']+2})

    def test_history_snapshot_server_derived_and_old_runtime_fk_compatible(self):
        row=self.history();ref='history:'+row['transaction_id'];a=self.upload();before=self.counts()
        body={'action':'aa_create','account_id':self.card(self.a)['account_id'],'idempotency_key':'server-evidence',
            'aa_bill':{'title':'核对消费','total_minor':2001,'split_mode':'equal',
                'participants':[{'user_id':u['user_id']} for u in (self.a,self.b)],'source_consumption_id':ref,
                'attachment_ids':[a['id']],'consumption_snapshot':{'paid_minor':999999999},
                'consumption_fingerprint':'fake','attachment_snapshots':[{'sha256':'fake'}]}}
        with self.g.demo_sessions.request(self.tokens[self.a['user_id']],self.a['workspace_id']):view=self.g.prepare(self.a['workspace_id'],body)
        self.assertEqual(self.counts(),before);form=view['operation']['payload']['aa_bill']
        self.assertEqual(form['consumption_snapshot']['paid_minor'],row['amount_minor'])
        self.assertEqual(form['attachment_snapshots'][0]['sha256'],a['sha256']);self.assertNotEqual(form['consumption_fingerprint'],'fake')
        done=self.confirm(self.a,view['operation']);bill=self.g.aa_bills(self.b['workspace_id'],done['result']['bill_id'])
        self.assertEqual(bill['consumption'],form['consumption_snapshot']);self.assertEqual(bill['attachments'],[a])
        with closing(self.g.bank.connect()) as db:
            self.assertIsNone(db.execute('SELECT source_transaction_id FROM navren_aa_bills WHERE id=?',(bill['id'],)).fetchone()[0])
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')),[])
        text=json.dumps(bill['consumption']);self.assertNotIn(row['account_id'],text);self.assertNotIn('card_id',text)
        tx=self.runtime();legacy=self.create(source_transaction_id='runtime:'+tx,idempotency_key='legacy')
        self.assertEqual(legacy['source_transaction_id'],tx);self.assertEqual(legacy['consumption']['id'],'runtime:'+tx)
        self.error('AA_SOURCE_INVALID',lambda:self.prepare_create(source_transaction_id='missing',idempotency_key='legacy-missing'))
        self.error('AA_SOURCE_INVALID',lambda:self.prepare_create(source_transaction_id='history:opaque',idempotency_key='legacy-history'))
        self.assertIsNone(self.create(idempotency_key='manual')['consumption'])

    def test_changed_source_refund_version_and_attachment_reject_before_execution(self):
        tx=self.runtime();op=self.prepare_create(source_consumption_id='runtime:'+tx)['operation'];before=self.counts()
        with self.g.bank.transaction() as db:db.execute("UPDATE transactions SET merchant='变更消费' WHERE id=?",(tx,))
        self.error('AA_CONSUMPTION_CHANGED',lambda:self.confirm(self.a,op));self.assertEqual(self.counts(),before)
        # 精确历史退款关系在独立副本变更；重新登记版本仍不能沿用旧快照。
        path=self.private_history();row=self.history();ref='history:'+row['transaction_id']
        op=self.prepare_create(source_consumption_id=ref,idempotency_key='history-change')['operation']
        with sqlite3.connect(path) as db:
            original=dict(row);original.update(transaction_id='test-refund',source_transaction_id='test-refund',
                original_transaction_id=row['transaction_id'],kind='refund',amount_minor=1000,signed_minor=1000,expense_minor=0)
            keys=list(original);db.execute('INSERT INTO transactions ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',[original[k] for k in keys])
        self.rebind_history(path);self.error('AA_CONSUMPTION_CHANGED',lambda:self.confirm(self.a,op))
        with closing(self.g.bank.connect()) as db:snap,_,_=evidence.resolve(self.g.bank,db,self.a['workspace_id'],ref)
        self.assertGreaterEqual(snap['refunded_minor'],1000)
        op=self.prepare_create(source_consumption_id=ref,idempotency_key='version-change')['operation']
        with sqlite3.connect(path) as db:db.execute("UPDATE metadata SET value=value||'-revision' WHERE key='dataset_version'")
        self.rebind_history(path);self.error('AA_CONSUMPTION_CHANGED',lambda:self.confirm(self.a,op))
        a=self.upload();op=self.prepare_create(attachment_ids=[a['id']],idempotency_key='image-change')['operation']
        with self.g.bank.transaction() as db:db.execute("UPDATE navren_aa_attachments SET content=? WHERE id=?",(b'changed',a['id']))
        self.error('AA_ATTACHMENT_INVALID',lambda:self.confirm(self.a,op))
        self.assertEqual(self.counts()['navren_aa_bills'],0)

    def test_cumulative_allocation_concurrency_and_paid_cancelled_retained(self):
        tx=self.runtime();ref='runtime:'+tx
        op=self.prepare_create(total_minor=6000,source_consumption_id=ref,idempotency_key='concurrent-a')['operation']
        other=self.prepare_create(total_minor=6000,source_consumption_id=ref,idempotency_key='concurrent-b')['operation']
        def run(o):
            try:return self.confirm(self.a,o)['state']
            except BankError as e:return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[op,other]))
        self.assertEqual(results.count('succeeded'),1)
        self.error('AA_CONSUMPTION_LIMIT',lambda:self.prepare_create(total_minor=4001,source_consumption_id=ref,idempotency_key='overflow'))
        tx=self.runtime(merchant='隔离部分付款');ref='runtime:'+tx
        bill=self.create(total_minor=3000,source_consumption_id=ref,idempotency_key='partial',participants=[{'user_id':u['user_id']} for u in (self.a,self.b,self.c)])
        self.confirm(self.b,self.prepare_pay(bill));balances=(self.balance(self.a),self.balance(self.b))
        op=self.g.prepare_aa(self.a['workspace_id'],{'idempotency_key':'cancel-partial'},kind='cancel',object_id=bill['id'])['operation'];self.confirm(self.a,op)
        self.assertEqual((self.balance(self.a),self.balance(self.b)),balances)
        self.error('AA_CONSUMPTION_LIMIT',lambda:self.prepare_create(total_minor=9001,source_consumption_id=ref,idempotency_key='cancel-overflow'))
        new=self.create(total_minor=9000,source_consumption_id=ref,idempotency_key='remaining')
        self.assertEqual(new['total_minor'],9000)

    def test_real_decode_strip_metadata_input_limits_and_max_three(self):
        a=self.upload(fmt='JPEG',metadata=True);read=self.g.aa_attachment(self.a['workspace_id'],a['id'])
        raw=base64.b64decode(read['data_base64']);self.assertNotIn(b'PRIVATE',raw)
        with Image.open(io.BytesIO(raw)) as img:self.assertEqual(img.format,'PNG');self.assertFalse(img.getexif());self.assertFalse(img.info)
        for body in ({'mime_type':'image/png','data_base64':base64.b64encode(b'<svg/>').decode()},
                     {**self.image(),'mime_type':'image/jpeg'},self.image(size=(2001,2000)),
                     {'mime_type':'image/png','data_base64':base64.b64encode(b'x'*524289).decode()}):
            self.error('AA_ATTACHMENT_INVALID',lambda:self.g.aa_attachment(self.a['workspace_id'],payload=body))
        out=io.BytesIO();Image.new('RGB',(5,5),'red').save(out,format='PNG',save_all=True,append_images=[Image.new('RGB',(5,5),'blue')])
        self.error('AA_ATTACHMENT_INVALID',lambda:self.g.aa_attachment(self.a['workspace_id'],payload={'mime_type':'image/png','data_base64':base64.b64encode(out.getvalue()).decode()}))
        ids=[a['id']]+[self.upload()['id'] for _ in range(3)]
        self.error('AA_ATTACHMENT_LIMIT',lambda:self.prepare_create(attachment_ids=ids))
        self.error('AA_ATTACHMENT_LIMIT',lambda:self.prepare_create(attachment_ids=[a['id'],a['id']]))

    def test_author_draft_official_members_read_delete_and_switch_guards(self):
        a=self.upload();path=a['id'];self.error('AA_ATTACHMENT_NOT_FOUND',lambda:self.g.aa_attachment(self.b['workspace_id'],path))
        op=self.prepare_create(attachment_ids=[path])['operation']
        self.error('AA_ATTACHMENT_NOT_FOUND',lambda:self.g.aa_attachment(self.b['workspace_id'],path))
        self.error('AA_ATTACHMENT_BOUND',lambda:self.g.aa_attachment(self.a['workspace_id'],path,{},delete=True))
        self.g.bank.cancel(self.a['workspace_id'],op['id'])
        self.assertTrue(self.g.aa_attachment(self.a['workspace_id'],path,{},delete=True)['deleted'])
        a=self.upload();bill=self.create(attachment_ids=[a['id']],idempotency_key='official')
        self.assertEqual(self.g.aa_attachment(self.b['workspace_id'],a['id'])['attachment'],a)
        self.error('AA_ATTACHMENT_NOT_FOUND',lambda:self.g.aa_attachment(self.c['workspace_id'],a['id']))
        self.error('AA_ATTACHMENT_BOUND',lambda:self.g.aa_attachment(self.a['workspace_id'],a['id'],{},delete=True))
        from finance_gateway import FinanceGateway
        reopened=FinanceGateway(self.path,financial_database=self.g.financial_data.database_path);reopened.bank.clock=lambda:self.now[0]
        self.assertEqual(reopened.aa_bills(self.b['workspace_id'],bill['id'])['attachments'],[a])
        token=self.tokens[self.b['user_id']];self.g.demo_sessions.select(token,self.c['user_id'],self.b['workspace_id'])
        def stale_read():
            with self.g.demo_sessions.request(token,self.b['workspace_id']):return self.g.aa_attachment(self.b['workspace_id'],a['id'])
        self.error('SESSION_CHANGED',stale_read)

    def test_replay_stable_evidence_and_unpaid_source_change_cannot_pay(self):
        tx=self.runtime();a=self.upload();args={'source_consumption_id':'runtime:'+tx,'attachment_ids':[a['id']]}
        op=self.prepare_create(**args)['operation'];self.assertEqual(self.prepare_create(**args)['operation']['id'],op['id'])
        extra=self.upload();self.error('IDEMPOTENCY_CONFLICT',lambda:self.prepare_create(**{**args,'attachment_ids':[extra['id']]}))
        done=self.confirm(self.a,op);bill=self.g.aa_bills(self.a['workspace_id'],done['result']['bill_id'])
        pay=self.prepare_pay(bill);before=(self.balance(self.a),self.balance(self.b),self.counts())
        self.runtime(kind='refund',merchant='未关联退款')
        self.error('AA_CONSUMPTION_CHANGED',lambda:self.confirm(self.b,pay))
        self.assertEqual((self.balance(self.a),self.balance(self.b)),before[:2]);self.assertEqual(self.counts()['navren_aa_receipts'],0)
        self.assertEqual(self.prepare_create(**args)['operation']['result'],done['result'])
        self.assertEqual(self.confirm(self.a,op)['result'],done['result'])
        self.assertEqual(self.g.aa_bills(self.b['workspace_id'],bill['id'])['consumption'],bill['consumption'])

    def test_http_upload_consumer_draft_member_receipt_and_csrf(self):
        from server import create_server,ProxyHandler
        from webank_catalog import PublicProductCatalog
        from test_bank_integration import Browser
        from finance_gateway import COOKIE
        tx=self.runtime();before=(self.balance(self.a),self.balance(self.b))
        with patch.dict(os.environ,{'NAVREN_FINANCIAL_DB':str(self.g.financial_data.database_path)}),patch.object(ProxyHandler,'log_message',lambda *args:None):
            server=create_server(0,self.path,public_catalog=PublicProductCatalog(lambda *a,**k:io.BytesIO()),public_sync_enabled=False)
            server.finance.bank.clock=lambda:self.now[0];thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base='http://127.0.0.1:'+str(server.server_port)
                def call(u,path,method='GET',body=None,csrf=True):
                    token=self.tokens[u['user_id']];browser=Browser(base);browser.csrf=self.g.csrf(token) if csrf else None
                    return browser.call(path,method,body,headers={'Cookie':COOKIE+'='+token})
                self.assertEqual(call(self.a,'/api/finance/aa/consumptions?workspace=other')[0],400)
                status,data,_=call(self.a,'/api/finance/aa/consumptions?q=%E9%9A%94%E7%A6%BB%E8%81%9A%E9%A4%90&from=2026-10-10&to=2026-10-10')
                self.assertEqual(status,200);self.assertEqual(data['consumptions'][0]['id'],'runtime:'+tx)
                self.assertEqual(call(self.a,'/api/finance/aa/attachments','POST',self.image(),csrf=False)[0],403)
                status,data,_=call(self.a,'/api/finance/aa/attachments','POST',self.image());self.assertEqual(status,200);image=data['attachment']
                image_path='/api/finance/aa/attachments/'+image['id'];self.assertEqual(call(self.b,image_path)[0],404)
                body={'account_id':self.card(self.a)['account_id'],'title':'HTTP消费凭证','total_minor':2642,'split_mode':'equal',
                    'participants':[{'user_id':u['user_id']} for u in (self.a,self.b)],'idempotency_key':'http-evidence',
                    'source_consumption_id':'runtime:'+tx,'attachment_ids':[image['id']]}
                status,data,_=call(self.a,'/api/finance/aa/bills/prepare','POST',body);self.assertEqual(status,200);op=data['operation']
                self.assertEqual(call(self.a,image_path+'/delete','POST',{})[0],409);self.assertEqual(call(self.b,image_path)[0],404)
                def accept(u,op):return call(u,'/api/finance/operations/'+op['id']+'/confirm','POST',{'confirmed':True,'confirmation_digest':op['confirmation_digest'],'verification_code':self.g.demo_code(self.tokens[u['user_id']])})
                status,done,_=accept(self.a,op);self.assertEqual(status,200);bill_id=done['operation']['result']['bill_id']
                status,read,headers=call(self.b,image_path);self.assertEqual(status,200);self.assertIn('no-store',headers['Cache-Control'])
                self.assertEqual(read['attachment'],image);self.assertEqual(call(self.c,image_path)[0],404)
                path='/api/finance/aa/bills/'+bill_id;bill=call(self.b,path)[1];self.assertEqual(bill['attachments'],[image]);self.assertEqual(bill['consumption']['paid_minor'],10000)
                card=self.card(self.b);status,data,_=call(self.b,'/api/finance/aa/shares/'+bill['my_share']['id']+'/pay/prepare','POST',{'account_id':card['account_id'],'card_id':card['id'],'idempotency_key':'http-evidence-pay'})
                self.assertEqual(status,200);status,done,_=accept(self.b,data['operation']);self.assertEqual(status,200)
                self.assertEqual(done['operation']['result']['amount_minor'],1321)
                self.assertEqual((self.balance(self.a),self.balance(self.b)),(before[0]+1321,before[1]-1321))
                self.assertEqual(call(self.b,path)[1]['attachments'],[image])
            finally:server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
