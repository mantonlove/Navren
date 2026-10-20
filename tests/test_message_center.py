"""消息首版窄验收。仅新建四用户合成库副本，禁止写现役数据或调用模型。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import io
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

import test_aa_payments as fixture
from finance_gateway import FinanceGateway, COOKIE
from simulated_bank.service import encoded
from user_memory import privacy_revision


class MessageCenterTests(unittest.TestCase):
    setUpClass=fixture.AAPaymentsTests.__dict__['setUpClass']
    tearDownClass=fixture.AAPaymentsTests.__dict__['tearDownClass']
    setUp=fixture.AAPaymentsTests.setUp
    card=fixture.AAPaymentsTests.card
    error=fixture.AAPaymentsTests.error
    balance=fixture.AAPaymentsTests.balance
    counts=fixture.AAPaymentsTests.counts
    prepare_create=fixture.AAPaymentsTests.prepare_create
    create=fixture.AAPaymentsTests.create
    confirm=fixture.AAPaymentsTests.confirm
    prepare_pay=fixture.AAPaymentsTests.prepare_pay

    def messages(self,u=None,**q):
        return self.g.messages.read((u or self.a)['workspace_id'],q)

    def payment_messages(self,u): return self.messages(u,category='payments')['items']

    def persisted(self):
        with closing(self.g.bank.connect()) as db:
            return [tuple(r) for r in db.execute('SELECT * FROM navren_messages ORDER BY id')]

    def event(self,**changes):
        w=self.a['workspace_id'];e=self.g.calendar.events
        draft=e.prepare(w,{'action':'create','event':{'title':'合成日历事项','kind':'life_event','start_on':'2026-10-10',**changes}})
        # 日历草案存在也不能成为通知。
        self.assertFalse(any(m['title']=='合成日历事项' for m in self.messages()['items']))
        return e.confirm(w,draft['draft_id'],{'confirmed':True})['series_id']

    def edit(self,sid,change=None,action='edit',**extra):
        w=self.a['workspace_id'];p={'action':action,'series_id':sid,**extra}
        if change is not None:p['event']=change
        draft=self.g.calendar.events.prepare(w,p)
        return self.g.calendar.events.confirm(w,draft['draft_id'],{'confirmed':True})

    def test_aa_confirm_only_atomic_settlement_replay_and_three_user_privacy(self):
        prepared=self.prepare_create(total_minor=3000,participants=[{'user_id':u['user_id']} for u in (self.a,self.b,self.c)])['operation']
        self.assertEqual(self.payment_messages(self.b),[])
        bill_id=self.confirm(self.a,prepared)['result']['bill_id']
        bill=self.g.aa_bills(self.a['workspace_id'],bill_id)
        self.assertEqual(self.payment_messages(self.a),[])
        pending=self.payment_messages(self.b);self.assertEqual(len(pending),1)
        self.assertEqual(pending[0]['status'],'pending');self.assertEqual(pending[0]['amount_minor'],1000)
        marked=self.g.messages.mark_read(self.b['workspace_id'],{},message_id=pending[0]['id'])['item']
        self.assertEqual(marked['status'],'pending');self.assertIsNotNone(marked['read_at'])
        aa=self.balance(self.a);bb=self.balance(self.b)
        pay=self.prepare_pay(bill);other=self.prepare_pay(bill,key='second')
        def execute(op):
            try:return self.confirm(self.b,op)['state']
            except Exception as e:return getattr(e,'code',type(e).__name__)
        with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(execute,[pay,other]))
        self.assertEqual(outcomes.count('succeeded'),1)
        self.assertEqual(self.balance(self.a),aa+1000);self.assertEqual(self.balance(self.b),bb-1000)
        winner=pay if self.g.bank.operation(self.b['workspace_id'],pay['id'])['state']=='succeeded' else other
        self.confirm(self.b,winner);self.confirm(self.b,winner)
        payer=self.payment_messages(self.b);owner=self.payment_messages(self.a);third=self.payment_messages(self.c)
        old=next(m for m in payer if m['id']==pending[0]['id'])
        self.assertEqual(old['status'],'completed');self.assertEqual(old['read_at'],marked['read_at'])
        self.assertEqual(len(payer),2);self.assertEqual(len(owner),1);self.assertEqual(len(third),1)
        self.assertEqual(third[0]['status'],'pending');self.assertEqual(self.payment_messages(self.d),[])
        for messages,foreign in [(owner,self.b),(third,self.b),(payer,self.a)]:
            text=json.dumps(messages,ensure_ascii=False)
            for forbidden in (self.card(foreign)['id'],self.card(foreign)['account_id'],pay['id'],other['id']):self.assertNotIn(forbidden,text)
            for field in ('balance_before_minor','balance_after_minor','payer_account_id','recipient_account_id','operation_id'):self.assertNotIn(field,text)
        receipt=self.g.aa_bills(self.b['workspace_id'],bill_id)['receipts'][0]
        self.assertEqual(next(m for m in payer if m['kind']=='aa_payer')['action']['receipt_id'],receipt['id'])
        self.assertEqual(receipt['amount_minor'],1000)
        snapshot=self.persisted();self.messages(self.b);self.messages(self.a)
        self.assertEqual(snapshot,self.persisted())

    def test_aa_write_failure_rolls_back_and_reconcile_recovers_confirmed_only(self):
        bill=self.create();pay=self.prepare_pay(bill);before=self.counts();aa=self.balance(self.a);bb=self.balance(self.b)
        snapshot=self.persisted()
        with patch('message_center.sync_aa_bill',side_effect=RuntimeError('injected message failure')):
            with self.assertRaises(RuntimeError):self.confirm(self.b,pay)
        self.assertEqual(self.counts(),before);self.assertEqual(self.balance(self.a),aa);self.assertEqual(self.balance(self.b),bb)
        self.assertEqual(self.persisted(),snapshot)
        self.confirm(self.b,pay)
        with self.g.bank.transaction() as db:db.execute("DELETE FROM navren_messages WHERE category='payments'")
        self.assertEqual(len(self.payment_messages(self.a)),1)
        self.assertEqual(len(self.payment_messages(self.b)),2)
        self.assertEqual(self.counts()['navren_aa_receipts'],1)

    def test_cancel_decline_deadline_terminal_and_read_persistence(self):
        bill=self.create(total_minor=3000,participants=[{'user_id':u['user_id']} for u in (self.a,self.b,self.c)])
        b=self.payment_messages(self.b)[0];c=self.payment_messages(self.c)[0]
        self.g.messages.mark_read(self.b['workspace_id'],{},message_id=b['id'])
        op=self.g.prepare_aa(self.b['workspace_id'],{'idempotency_key':'decline'},kind='decline',object_id=b['action']['share_id'])['operation']
        self.confirm(self.b,op)
        cancellation=self.g.prepare_aa(self.a['workspace_id'],{'idempotency_key':'cancel'},kind='cancel',object_id=bill['id'])['operation']
        self.confirm(self.a,cancellation)
        self.assertEqual(next(m for m in self.payment_messages(self.b) if m['id']==b['id'])['status'],'declined')
        self.assertEqual(next(m for m in self.payment_messages(self.c) if m['id']==c['id'])['status'],'cancelled')
        self.assertFalse(self.messages(self.b,view='pending',category='payments')['items'])
        self.assertFalse(self.messages(self.c,view='pending',category='payments')['items'])
        second=self.create(idempotency_key='expiry',due_at='2026-10-10T00:01:00+08:00')
        item=next(m for m in self.payment_messages(self.b) if m['action']['object_id']==second['id'])
        self.now[0]+=61
        expired=self.g.messages.read(self.b['workspace_id'],message_id=item['id'])['item']
        self.assertEqual(expired['status'],'pending');self.assertNotIn('payable',expired['action'])
        self.assertIn(item['id'],[m['id'] for m in self.messages(self.b,view='pending',category='payments')['items']])
        self.assertIn('付款入口已关闭',expired['body'])
        self.assertFalse(self.g.aa_bills(self.b['workspace_id'],second['id'])['my_share']['payable'])
        with patch.object(self.g,'prepare_aa',side_effect=AssertionError('message must not prepare')),patch.object(self.g,'confirm',side_effect=AssertionError('message must not execute')):
            self.assertEqual(self.g.messages.read(self.b['workspace_id'],message_id=item['id'])['item']['status'],'pending')
        self.error('MESSAGE_NOT_FOUND',lambda:self.g.messages.read(self.d['workspace_id'],message_id=b['id']))
        self.error('MESSAGE_NOT_FOUND',lambda:self.g.messages.mark_read(self.a['workspace_id'],{},message_id=b['id']))
        before=self.persisted();new=FinanceGateway(self.path,financial_database=fixture.ROOT/'outputs/financial-data/navren-financial-v3.sqlite3');new.bank.clock=lambda:self.now[0]
        self.addCleanup(new.messages.stop)
        new.messages.read(self.b['workspace_id']);self.assertEqual(self.persisted(),before)

    def test_calendar_shared_read_receipt_revision_completion_and_deletion(self):
        sid=self.event();w=self.a['workspace_id']
        reminder=self.g.calendar.events.reminders(w,'2026-10-10')['items']
        receipt=next(r for r in reminder if r['item']['series_id']==sid)
        self.g.calendar.events.read_reminder(w,receipt['id'])
        original=next(m for m in self.messages()['items'] if m['action'] and m['action'].get('occurrence_id')==sid+':2026-10-10')
        self.assertIsNotNone(original['read_at'])
        self.assertEqual(self.g.calendar.item(w,original['action']['object_id'])['series_id'],sid)
        self.edit(sid,{'title':'修订后的合成事项'})
        revised=self.g.messages.read(w,message_id=original['id'])['item']
        self.assertEqual(revised['title'],'修订后的合成事项');self.assertEqual(revised['read_at'],original['read_at'])
        self.edit(sid,{'start_on':'2026-10-12'})
        moved=self.g.messages.read(w,message_id=original['id'])['item']
        self.assertEqual(moved['due_at'],'2026-10-12');self.assertEqual(moved['read_at'],original['read_at'])
        self.assertEqual(moved['action']['occurrence_id'],sid+':2026-10-12')
        self.assertEqual(len([m for m in self.messages()['items'] if m['title']=='修订后的合成事项']),1)
        self.edit(sid,{'status':'completed'})
        completed=self.g.messages.read(w,message_id=original['id'])['item']
        self.assertEqual(completed['status'],'completed');self.assertIn('不代表已支付',completed['body'])
        self.edit(sid,action='delete')
        deleted=self.g.messages.read(w,message_id=original['id'])['item']
        self.assertEqual(deleted['status'],'cancelled');self.assertEqual(deleted['read_at'],original['read_at'])
        sid2=self.event(title='另一期事项')
        second=next(m for m in self.messages()['items'] if m['action'] and m['action'].get('occurrence_id')==sid2+':2026-10-10')
        self.g.messages.mark_read(w,{},message_id=second['id'])
        self.assertFalse(any(r['item']['series_id']==sid2 for r in self.g.calendar.events.reminders(w,'2026-10-10')['items']))
        self.assertEqual(self.g.messages.read(w,message_id=second['id'])['item']['status'],'pending')
        self.edit(sid2,{'start_on':'2026-10-11'},scope='occurrence',original_on='2026-10-10')
        self.assertEqual(self.g.messages.read(w,message_id=second['id'])['item']['action']['date'],'2026-10-11')

    def test_due_source_business_clock_candidate_and_no_money_effect(self):
        w=self.a['workspace_id'];before=self.counts();balance=self.balance(self.a)
        # 原连接账本as_of=10-03；实时业务钟推进10-10，提醒不能固定在历史日期。
        with self.g.bank.transaction() as db:
            # 隔离夹具初始化已经对齐当天；明确退回业务日构造历史时钟反例。
            db.execute("UPDATE workspaces SET as_of='2026-10-03' WHERE id=?",(w,))
            raw=db.execute('SELECT as_of FROM workspaces WHERE id=?',(w,)).fetchone()[0]
            self.assertLess(raw,'2026-10-10')
            sub=db.execute('SELECT * FROM subscriptions WHERE workspace_id=? LIMIT 1',(w,)).fetchone()
            if sub is None:
                # 此接入夹具没有商户合同；补入明确标记的隔离来源，不给现役用户造数据。
                source=db.execute('SELECT id FROM data_sources ORDER BY id LIMIT 1').fetchone()[0]
                account=self.card(self.a)['account_id']
                db.execute('INSERT INTO mandates VALUES(?,?,?,?,?,?,?,?)',('fixture-message-mandate',w,account,'隔离来源商户',1200,'active','2026-10-11','active'))
                db.execute('INSERT INTO merchants(id,workspace_id,source_id,data_origin,name,mcc,category,channel) VALUES(?,?,?,?,?,?,?,?)',
                           ('fixture-message-merchant',w,source,'qingzhou_synthetic','隔离来源商户','0000','测试合同','test'))
                db.execute('''INSERT INTO subscriptions(id,workspace_id,source_id,data_origin,account_id,mandate_id,merchant_id,plan_name,amount_minor,cycle,next_renewal_on,merchant_status,cancellation_route)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',('fixture-message-subscription',w,source,'qingzhou_synthetic',account,'fixture-message-mandate','fixture-message-merchant','隔离来源月费',1200,'monthly','2026-10-11','active','fixture'))
                sub=db.execute('SELECT * FROM subscriptions WHERE workspace_id=? AND id=?',(w,'fixture-message-subscription')).fetchone()
            self.assertIsNotNone(sub)
            db.execute("UPDATE subscriptions SET next_renewal_on='2026-10-11',merchant_status='active' WHERE workspace_id=? AND id=?",(w,sub['id']))
        started=time.monotonic();result=self.messages();message_seconds=time.monotonic()-started
        self.assertEqual(result['as_of'],'2026-10-10')
        candidate=next(m for m in result['items'] if m['kind']=='subscription_renewal' and m['due_at']=='2026-10-11')
        self.assertEqual(candidate['certainty'],'candidate');self.assertIn('未记录本期扣款',candidate['body'])
        self.assertEqual(candidate['action']['date'],'2026-10-11')
        started=time.monotonic();actual=self.g.calendar.item(w,candidate['action']['object_id']);detail_seconds=time.monotonic()-started
        self.assertEqual(actual['state'],'candidate');self.assertIn(sub['id'],actual['source_ref'])
        self.g.messages.mark_read(w,{},message_id=candidate['id'])
        with self.g.bank.transaction() as db:db.execute("UPDATE subscriptions SET merchant_status='cancelled' WHERE workspace_id=? AND id=?",(w,sub['id']))
        cancelled=self.g.messages.read(w,message_id=candidate['id'])['item']
        self.assertEqual(cancelled['status'],'cancelled');self.assertIsNotNone(cancelled['read_at'])
        self.assertEqual(self.counts(),before);self.assertEqual(self.balance(self.a),balance)
        self.now[0]=datetime.fromisoformat('2026-10-15T00:00:00+08:00').timestamp()
        self.assertEqual(self.messages()['as_of'],'2026-10-15')
        self.assertEqual(self.messages(category='security')['items'],[])
        self.assertEqual(self.messages(category='service')['items'],[])
        self.due_evidence={'message_read_seconds':round(message_seconds,3),'existing_calendar_item_seconds':round(detail_seconds,3),
                           'candidate_source':'isolated explicit subscription/mandate fixture','source_ref':actual['source_ref'],
                           'clock':'stored business day 2026-10-03, provider 2026-10-10 then 2026-10-15'}

    def test_read_all_snapshot_http_validation_csrf_identity_and_privacy(self):
        self.create();item=self.payment_messages(self.b)[0];w=self.b['workspace_id']
        count=len(self.persisted());marked=self.g.messages.mark_read(w,{})
        self.assertEqual(marked['unread_count'],0);self.assertEqual(len(self.persisted()),count)
        self.create(idempotency_key='later');self.assertEqual(len([m for m in self.payment_messages(self.b) if m['read_at'] is None]),1)
        with self.g.bank.transaction() as db:
            old=privacy_revision(db,w)
            db.execute('CREATE TABLE IF NOT EXISTS conversation_privacy_versions(workspace_id TEXT PRIMARY KEY,revision INTEGER NOT NULL)')
            db.execute('INSERT INTO conversation_privacy_versions VALUES(?,?) ON CONFLICT(workspace_id) DO UPDATE SET revision=excluded.revision',(w,old+1))
        snapshot=self.persisted()
        self.error('PRIVACY_REVISION_CHANGED',lambda:self.g.messages.read(w,expected_privacy=old))
        self.error('PRIVACY_REVISION_CHANGED',lambda:self.g.messages.mark_read(w,{},expected_privacy=old))
        self.assertEqual(self.persisted(),snapshot)
        from server import create_server,ProxyHandler
        from webank_catalog import PublicProductCatalog
        from test_bank_integration import Browser
        with patch.dict(os.environ,{'NAVREN_FINANCIAL_DB':str(self.g.financial_data.database_path),'NAVREN_MESSAGE_WORKER_ENABLED':'false'}),patch.object(ProxyHandler,'log_message',lambda *args:None):
            server=create_server(0,self.path,public_catalog=PublicProductCatalog(lambda *a,**k:io.BytesIO()),public_sync_enabled=False)
            server.finance.bank.clock=lambda:self.now[0]
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                def call(path,method='GET',body=None,user=None,csrf=True,auth=True,raw=None):
                    user=user or self.b;token=self.tokens[user['user_id']]
                    browser=Browser('http://127.0.0.1:'+str(server.server_port));browser.csrf=self.g.csrf(token) if csrf else None
                    return browser.call(path,method,body,headers={'Cookie':COOKIE+'='+token} if auth else {},raw=raw)
                root='/api/finance/messages'
                self.assertEqual(call(root,auth=False)[0],401)
                status,data,headers=call(root+'?category=payments&limit=1');self.assertEqual(status,200)
                self.assertEqual(len(data['items']),1);self.assertTrue(data['has_more'])
                self.assertEqual(headers['X-Navren-Workspace'],w);self.assertEqual(headers['X-Navren-Privacy-Revision'],str(old+1));self.assertIn('no-store',headers['Cache-Control'])
                self.assertGreaterEqual(data['unread_count'],1)
                for q in ('workspace_id=x','user_id=','foo=','limit=0','limit=01','offset=-1','limit=101','unread=','unread=yes','view=x','category=x','limit=1&limit=2'):
                    self.assertEqual(call(root+'?'+q)[0],400,q)
                self.assertEqual(call(root+'/summary?foo=')[0],400)
                self.assertEqual(call(root+'/'+item['id']+'?foo=')[0],400)
                self.assertEqual(call(root+'/'+item['id'],user=self.d)[0],404)
                self.assertEqual(call(root+'/'+item['id']+'/read','POST',{},user=self.d)[0],404)
                self.assertEqual(call(root+'/read-all','POST',{},csrf=False)[0],403)
                self.assertEqual(call(root+'/read-all?workspace_id=','POST',{})[0],400)
                self.assertEqual(call(root+'/read-all','POST',{'workspace_id':w})[0],400)
                self.assertEqual(call(root+'/read-all','POST',raw=b'{"x":1,"x":2}')[0],400)
                self.assertEqual(call(root+'/'+item['id']+'/read','POST',{})[0],200)
                self.assertEqual(call(root+'/read-all','POST',{})[1]['unread_count'],0)
                self.assertEqual(call(root+'?unread=true')[1]['items'],[])
                self.assertTrue(call(root+'?unread=false')[1]['items'])
                self.assertEqual(call(root+'?view=pending&category=payments')[1]['items'][0]['status'],'pending')
                token=self.tokens[self.b['user_id']];self.g.demo_sessions.select(token,self.c['user_id'],w)
                self.assertEqual(call(root)[0],409)
            finally:server.shutdown();server.server_close();thread.join(2)

    def test_goal_deadline_confirmed_source_edit_complete_delete_no_frozen_money(self):
        w=self.a['workspace_id'];goals=self.g.goals;balance=self.balance(self.a);before=self.counts()
        draft=goals.prepare(w,{'action':'create','name':'已保存目标截止测试','target_minor':20000,'due_on':'2026-10-10',
                              'expected_version':goals.state(w)['version'],'idempotency_key':'message-goal-create'})
        self.assertFalse(any(m['kind']=='goal_deadline' and m['title'].startswith('已保存目标截止测试') for m in self.messages()['items']))
        done=goals.confirm(w,draft['id'],{'confirmed':True,'confirmation_digest':draft['confirmation_digest']})
        goal=done['goal'];gid=goal['id']
        message=next(m for m in self.messages()['items'] if m['kind']=='goal_deadline' and m['title'].startswith('已保存目标截止测试'))
        self.assertEqual(message['status'],'pending');self.assertIsNone(message['amount_minor']);self.assertEqual(message['certainty'],'user_defined')
        self.assertIn('不等于资金冻结',message['body'])
        self.assertEqual(self.g.calendar.item(w,message['action']['object_id'])['goal_id'],gid)
        read_at=self.g.messages.mark_read(w,{},message_id=message['id'])['item']['read_at']
        for mode in ('update','complete','delete'):
            goal=goals.state(w,gid)['goal']
            body={'action':'delete' if mode=='delete' else 'update','goal_id':gid,'expected_version':goal['object_version'],'idempotency_key':'message-goal-'+mode}
            if mode!='delete':body.update(name='修订后的截止目标',target_minor=20000,due_on='2026-10-11',status='completed' if mode=='complete' else 'active')
            draft=goals.prepare(w,body);goals.confirm(w,draft['id'],{'confirmed':True,'confirmation_digest':draft['confirmation_digest']})
            updated=self.g.messages.read(w,message_id=message['id'])['item']
            self.assertEqual(updated['read_at'],read_at)
            self.assertEqual(updated['status'],{'update':'pending','complete':'completed','delete':'cancelled'}[mode])
            if mode=='update':self.assertEqual(updated['action']['date'],'2026-10-11')
            else:
                if mode=='complete' and updated['action'] is not None:
                    # 真实确认后的反例：已完成目标在旧日历入口会回退为cancelled。
                    self.assertEqual(self.g.calendar.item(w,updated['action']['object_id'])['state'],'cancelled')
                self.assertIsNone(updated['action'])
        # 从仍待处理的目标直接删除，避免“先完成已清空action”掩盖取消分支。
        draft=goals.prepare(w,{'action':'create','name':'待处理目标删除测试','target_minor':10000,'due_on':'2026-10-10',
                              'expected_version':goals.state(w)['version'],'idempotency_key':'message-goal-delete-active-create'})
        active=goals.confirm(w,draft['id'],{'confirmed':True,'confirmation_digest':draft['confirmation_digest']})['goal']
        active_message=next(m for m in self.messages()['items'] if m['kind']=='goal_deadline' and m['title'].startswith('待处理目标删除测试'))
        self.assertEqual(active_message['status'],'pending');self.assertIsNotNone(active_message['action'])
        active_read=self.g.messages.mark_read(w,{},message_id=active_message['id'])['item']['read_at']
        draft=goals.prepare(w,{'action':'delete','goal_id':active['id'],'expected_version':active['object_version'],'idempotency_key':'message-goal-delete-active-confirm'})
        goals.confirm(w,draft['id'],{'confirmed':True,'confirmation_digest':draft['confirmation_digest']})
        cancelled=self.g.messages.read(w,message_id=active_message['id'])['item']
        self.assertEqual(cancelled['status'],'cancelled');self.assertIsNone(cancelled['action']);self.assertEqual(cancelled['read_at'],active_read)
        self.assertEqual(cancelled['created_at'],active_message['created_at'])
        self.assertEqual(self.balance(self.a),balance);self.assertEqual(self.counts(),before)

    def test_imported_calendar_actual_clock_separate_from_legacy_bank_clock(self):
        credentials=self.g.bank.create_workspace();w=credentials['workspace_id']
        with self.g.bank.transaction() as db:db.execute("UPDATE workspaces SET as_of='2026-10-03' WHERE id=?",(w,))
        preview=self.g.statements.preview(w,'流水号,日期,商户,金额,类型,分类,币种,备注\nclock-1,2026-10-09,隔离导入,10.00,支出,测试,CNY,\n')
        self.g.statements.commit(w,{'batch_id':preview['batch_id'],'confirmed':True})
        self.g.set_source(w,{'source':'imported'})
        e=self.g.calendar.events
        draft=e.prepare(w,{'action':'create','event':{'title':'导入来源实际日期事项','kind':'life_event','start_on':'2026-10-10'}})
        sid=e.confirm(w,draft['draft_id'],{'confirmed':True})['series_id']
        self.assertEqual(self.g.calendar.clock(w),('2026-10-10','actual_records'))
        self.assertTrue(any(r['item']['series_id']==sid for r in e.reminders(w,'2026-10-10')['items']))
        result=self.g.messages.read(w)
        self.assertEqual(result['as_of'],'2026-10-03');self.assertEqual(result['calendar_as_of'],'2026-10-10');self.assertEqual(result['calendar_source'],'imported')
        message=next(m for m in result['items'] if m['title']=='导入来源实际日期事项')
        self.assertEqual(message['due_at'],'2026-10-10');self.assertEqual(message['status'],'pending')
        actual=self.g.calendar.item(w,message['action']['object_id']);self.assertEqual(actual['series_id'],sid)
        with closing(self.g.bank.connect()) as db:
            ref=json.loads(db.execute('SELECT reference FROM calendar_items WHERE workspace_id=? AND id=?',(w,message['action']['object_id'])).fetchone()[0])
            self.assertEqual(ref['source'],'imported')
        marked=self.g.messages.mark_read(w,{},message_id=message['id'])['item']['read_at']
        self.g.set_source(w,{'source':'simulation'})
        self.assertEqual(self.g.messages.read(w,message_id=message['id'])['item']['read_at'],marked)

    def test_worker_startup_dedup_multiple_gateways_30s_alive_and_true_stop(self):
        sid=self.event(start_on='2026-10-08',title='启动前有源到期事项')
        # 删除纯消息元数据，源日历保留；只用于证明启动重放，无金融状态改动。
        with self.g.bank.transaction() as db:db.execute('DELETE FROM navren_messages')
        other=FinanceGateway(self.path,financial_database=fixture.ROOT/'outputs/financial-data/navren-financial-v3.sqlite3');other.bank.clock=lambda:self.now[0]
        self.addCleanup(other.messages.stop);self.addCleanup(self.g.messages.stop)
        self.assertEqual(self.g.messages.interval_seconds,60)
        started=time.monotonic();self.assertTrue(self.g.messages.start());self.assertFalse(self.g.messages.start());self.assertTrue(other.messages.start())
        deadline=time.monotonic()+8
        while time.monotonic()<deadline and not (self.g.messages.status()['cycles'] and other.messages.status()['cycles']):time.sleep(.05)
        self.assertIsNone(self.g.messages.status()['last_error']);self.assertIsNone(other.messages.status()['last_error'])
        self.assertGreaterEqual(self.g.messages.status()['cycles'],1);self.assertGreaterEqual(other.messages.status()['cycles'],1)
        own=self.messages();reminder=next(m for m in own['items'] if m['title']=='启动前有源到期事项')
        self.assertEqual(reminder['status'],'pending');self.assertEqual(reminder['due_at'],'2026-10-08')
        count=len(self.persisted());self.messages();self.messages();self.assertEqual(len(self.persisted()),count)
        while time.monotonic()-started<30:time.sleep(.05)
        self.assertTrue(self.g.messages.status()['running']);self.assertTrue(other.messages.status()['running'])
        self.assertEqual(self.g.messages.status()['model_calls'],0)
        self.assertTrue(self.g.messages.stop());self.assertTrue(other.messages.stop())
        self.assertFalse(self.g.messages.status()['running']);self.assertFalse(other.messages.status()['running'])
        threads=[t.name for t in threading.enumerate() if t.name=='navren-message-center']
        self.assertEqual(threads,[])
        self.worker_evidence={'alive_after_seconds':round(time.monotonic()-started,2),'first':self.g.messages.status(),
                              'second':other.messages.status(),'threads_after_stop':threads}


if __name__=='__main__':unittest.main()
