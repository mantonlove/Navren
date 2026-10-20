"""本地账户绑定3类窄检查：本人闭环、身份隔离、资金历史保持。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import io
import json
import os
import sqlite3
import threading
import unittest
from unittest.mock import patch

import test_aa_payments as fixture


class AccountConnectionsTests(unittest.TestCase):
    setUpClass=fixture.AAPaymentsTests.__dict__['setUpClass']
    tearDownClass=fixture.AAPaymentsTests.__dict__['tearDownClass']
    setUp=fixture.AAPaymentsTests.setUp
    error=fixture.AAPaymentsTests.error

    def read(self,u=None,kind='payment',institution=None):
        u=u or self.a;query={'kind':[kind]}
        if institution:query['institution']=[institution]
        return self.g.account_connections.read(u['workspace_id'],query)

    def bind(self,u=None,kind='payment',institution='wechat'):
        u=u or self.a
        with self.g.demo_sessions.request(self.tokens[u['user_id']],u['workspace_id']):
            return self.g.account_connections.change(u['workspace_id'],{'kind':kind,'institution':institution,'confirmed':True})

    def disconnect(self,binding,u=None):
        u=u or self.a
        with self.g.demo_sessions.request(self.tokens[u['user_id']],u['workspace_id']):
            return self.g.account_connections.change(u['workspace_id'],{'confirmed':True},binding['id'])

    def bank_rows(self,tables):
        with closing(self.g.bank.connect()) as db:
            return {t:hashlib.sha256(json.dumps(sorted(tuple(r) for r in db.execute('SELECT * FROM '+t)),default=str,ensure_ascii=False).encode()).hexdigest() for t in tables}

    def test_owner_zero_wallet_securities_idempotency_and_http(self):
        w=self.a['workspace_id'];before=self.g.connected_assets.overview(w)['data']['summary']
        self.assertEqual(self.read(institution='wechat')['channels'][0]['status'],'unbound')
        with closing(self.g.bank.connect()) as db:
            count=db.execute('SELECT count(*) FROM accounts WHERE workspace_id=?',(w,)).fetchone()[0]
            transactions=db.execute('SELECT count(*) FROM transactions').fetchone()[0]
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.bind(),range(2)))
        self.assertEqual(sum(r['account_created'] for r in results),1);self.assertEqual(sum(r['changed'] for r in results),1)
        binding=results[0]['binding'];self.assertEqual(binding['accounts'][0]['balance_minor'],0)
        self.assertEqual(self.bind()['binding'],binding);self.assertFalse(self.bind()['changed'])
        with closing(self.g.bank.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM accounts WHERE workspace_id=?',(w,)).fetchone()[0],count+1)
            self.assertEqual(db.execute('SELECT count(*) FROM transactions').fetchone()[0],transactions)
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')),[])
        current=self.g.connected_assets.overview(w)['data'];self.assertEqual(current['summary'],before)
        new=next(a for a in current['accounts'] if a['account_id']==binding['accounts'][0]['account_id'])
        self.assertEqual(new['details']['origin'],'runtime_account_connection');self.assertEqual(new['opened_on'],'2026-10-10')
        self.assertFalse(new['details']['history_initialized'])
        self.assertEqual(self.g.connected_assets.transactions(w,month='2026-09',account_id=new['account_id'])['data']['total'],0)
        securities=self.bind(kind='securities',institution='huatai')['binding']
        self.assertEqual(securities['accounts'][0]['account_type'],'securities_cash');self.assertEqual(securities['accounts'][0]['balance_minor'],0)
        self.assertFalse(self.bind(kind='securities',institution='huatai')['changed'])
        self.assertTrue(self.disconnect(binding)['changed']);self.assertFalse(self.disconnect(binding)['changed'])
        self.assertEqual(self.read(institution='wechat')['active_bindings'],[])
        self.assertEqual(self.bind()['binding']['id'],binding['id'])
        # 真HTTP只验本地关联与权限，无金融交易或模型调用。
        from server import create_server,ProxyHandler
        from webank_catalog import PublicProductCatalog
        from test_bank_integration import Browser
        from finance_gateway import COOKIE
        with patch.dict(os.environ,{'NAVREN_FINANCIAL_DB':str(self.g.financial_data.database_path)}),patch.object(ProxyHandler,'log_message',lambda *args:None):
            server=create_server(0,self.path,public_catalog=PublicProductCatalog(lambda *a,**k:io.BytesIO()),public_sync_enabled=False)
            server.finance.bank.clock=lambda:self.now[0];thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                def call(path,method='GET',body=None,csrf=True):
                    browser=Browser('http://127.0.0.1:'+str(server.server_port));token=self.tokens[self.a['user_id']]
                    browser.csrf=self.g.csrf(token) if csrf else None
                    return browser.call(path,method,body,headers={'Cookie':COOKIE+'='+token})
                path='/api/finance/account-connections';body={'kind':'payment','institution':'wechat','confirmed':True}
                self.assertEqual(call(path,'POST',body,csrf=False)[0],403)
                status,data,headers=call(path+'?kind=payment&institution=wechat');self.assertEqual(status,200);self.assertIn('no-store',headers['Cache-Control'])
                self.assertEqual(data['bindings'][0]['id'],binding['id'])
                self.assertEqual(call(path+'?workspace_id=other')[0],400)
                self.assertEqual(call(path+'?foo=')[0],400)
                self.assertEqual(call(path+'?workspace_id=other','POST',body)[0],400)
                self.assertEqual(call(path+'?workspace_id=','POST',body)[0],400)
                status,data,_=call(path+'/'+binding['id']+'/disconnect','POST',{'confirmed':True});self.assertEqual(status,200);self.assertEqual(data['binding']['status'],'disconnected')
                status,data,_=call(path,'POST',body);self.assertEqual(status,200);self.assertFalse(data['account_created'])
            finally:server.shutdown();server.server_close();thread.join(2)

    def test_other_user_source_spoof_and_switched_session_rejected(self):
        binding=self.read(institution='alipay')['bindings'][0];b=self.b
        self.error('ACCOUNT_CONNECTION_NOT_FOUND',lambda:self.disconnect(binding,b))
        body={'kind':'payment','institution':'wechat','confirmed':True}
        before=self.bank_rows(['accounts','connected_account_links','holdings','transactions','navren_account_connections'])
        for key,value in [('workspace_id',b['workspace_id']),('user_id',b['user_id']),('account_id',binding['accounts'][0]['account_id']),('connection_id',binding['connection_id']),('path','/tmp/foreign')]:
            self.error('INVALID_FIELDS',lambda:self.g.account_connections.change(self.a['workspace_id'],{**body,key:value}))
        self.error('CONNECTION_CONFIRMATION_REQUIRED',lambda:self.g.account_connections.change(self.a['workspace_id'],{**body,'confirmed':False}))
        self.error('CONNECTION_CHANNEL_INVALID',lambda:self.g.account_connections.change(self.a['workspace_id'],{**body,'institution':'unknown'}))
        self.error('INVALID_QUERY',lambda:self.g.account_connections.read(self.a['workspace_id'],{'kind':['payment','securities']}))
        token=self.tokens[self.a['user_id']];self.g.demo_sessions.select(token,b['user_id'],self.a['workspace_id'])
        self.error('SESSION_CHANGED',lambda:self.bind())
        self.assertEqual(self.bank_rows(['accounts','connected_account_links','holdings','transactions','navren_account_connections']),before)

    def test_migrated_defaults_disconnect_preserves_funds_source_history_and_positions(self):
        w=self.a['workspace_id'];source=self.g.financial_data.database_path;source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
        for u in self.users:
            self.assertTrue(self.read(u,kind='securities',institution='citic')['bindings'][0]['active'])
            self.assertTrue(all(b['active'] for b in self.read(u)['bindings']))
        binding=self.read(institution='alipay')['bindings'][0]
        overview=self.g.connected_assets.overview(w)['data'];cash=self.g.overview(w)['total_cash_minor']
        old_account=binding['accounts'][0]['account_id'];history=self.g.connected_assets.transactions(w,month='2026-09',account_id=old_account)['data']
        tables=['accounts','connected_account_links','holdings','investment_lots','connected_position_seeds','transactions','cards','loans','connected_asset_migrations']
        before=self.bank_rows(tables);self.assertTrue(self.disconnect(binding)['changed'])
        self.assertEqual(self.bank_rows(tables),before)
        result=self.g.connected_assets.overview(w)['data'];self.assertEqual(result['summary'],overview['summary']);self.assertEqual(result['positions'],overview['positions'])
        account=next(a for a in result['accounts'] if a['account_id']==old_account);self.assertEqual(account['connection_status'],'disconnected');self.assertFalse(account['connection_active']);self.assertEqual(account['status'],'active')
        self.assertEqual(self.g.overview(w)['total_cash_minor'],cash)
        self.assertEqual(self.g.connected_assets.transactions(w,month='2026-09',account_id=old_account)['data'],history)
        from finance_gateway import FinanceGateway
        reopened=FinanceGateway(self.path,financial_database=source);reopened.bank.clock=lambda:self.now[0]
        self.assertEqual(reopened.account_connections.read(w,{'kind':['payment'],'institution':['alipay']})['bindings'][0]['status'],'disconnected')
        self.assertEqual(self.bind(institution='alipay')['binding']['accounts'],binding['accounts'])
        broker=self.read(kind='securities',institution='citic')['bindings'][0];self.disconnect(broker)
        self.assertEqual(self.g.connected_assets.overview(w)['data']['positions'],overview['positions'])
        self.assertEqual(self.g.connected_assets.overview(w)['data']['summary'],overview['summary'])
        self.assertEqual(self.bank_rows(tables),before);self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),source_hash)


if __name__=='__main__':unittest.main()
