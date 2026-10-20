"""正式ProxyHandler研究接口纵向接线；不代表真实公开资料和当前入口整体验收。"""
from copy import deepcopy
import unittest

import test_account_state_http as http_fixture
import test_research_workspace as research_fixture


class ResearchWorkspaceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.h=http_fixture.AccountStateHTTPTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.h.g.research_workspace._provider=research_fixture.EvidenceFixture()

    def test_actual_http_public_and_private_save_read_cas_and_identity_rejection(self):
        status,public,_=self.h.request(path='/api/finance/research/catalog',token=False)
        self.assertEqual(status,200,public);self.assertEqual(public['data_scope'],'public');self.assertNotIn('registered_user_id',public)
        status,entity,_=self.h.request(path='/api/finance/research/entities/fixture:share:A',token=False)
        self.assertEqual(status,200,entity);self.assertEqual(entity['evidence_version'],'fixture-evidence-1')
        status,state,headers=self.h.request(path='/api/finance/research/workspace')
        self.assertEqual(status,200,state);self.assertEqual(state['revision'],0);self.assertEqual(headers['Cache-Control'],'no-store')
        payload={'expected_revision':0,'value':research_fixture.ResearchWorkspaceTests.value(),'confirmed':True}
        status,saved,_=self.h.request(payload,path='/api/finance/research/workspace');self.assertEqual(status,200,saved)
        rid=saved['record']['id']
        status,read,_=self.h.request(path='/api/finance/research/records/'+rid);self.assertEqual(status,200,read)
        self.assertEqual(read['record']['value'],payload['value'])
        for body,expected in [(payload,409),({**payload,'owner':'forged'},422),({**payload,'expected_revision':1,'confirmed':False},409)]:
            status,result,_=self.h.request(body,path='/api/finance/research/workspace');self.assertEqual(status,expected,result)
        self.assertEqual(self.h.request(path='/api/finance/research/workspace',token=False)[0],401)
        self.assertEqual(self.h.request({**payload,'expected_revision':1},path='/api/finance/research/workspace',headers={'X-Navren-Workspace':'forged'})[0],409)

    def test_binary_original_http_and_research_list_preconditions(self):
        import hashlib
        import socket
        import urllib.request
        import urllib.error
        from unittest.mock import patch
        body=b'%PDF-1.7\n'+b'actual-owned-http-original\x00'*60_000+b'\n%%EOF'
        lease=self.h.a;token=self.h.f.f.token(lease)
        headers={'Origin':self.h.base,'Cookie':'qz_bank_user='+token,'X-QZ-CSRF':self.h.g.csrf(token),
            'X-Navren-Workspace':lease['workspace_id'],'X-Navren-Privacy-Revision':str(lease['privacy_revision']),
            'Content-Type':'application/pdf','X-Navren-Upload-Key':'http-original','X-Navren-Expected-Revision':'0',
            'X-Navren-Confirmed':'true','X-Navren-Health-Consent':'true'}
        def request(path,data=None,h=None):
            req=urllib.request.Request(self.h.base+path,data=data,headers=h or headers)
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with patch.object(socket,'create_connection',http_fixture.CONNECT):
                try:response=opener.open(req,timeout=10)
                except urllib.error.HTTPError as error:response=error
                with response:return response.status,response.read(),response.headers
        status,raw,_=request('/api/finance/research/attachments',body)
        import json
        saved=json.loads(raw);self.assertEqual(status,200,saved);aid=saved['attachment']['id']
        status,original,h=request('/api/finance/research/attachments/'+aid)
        self.assertEqual(status,200);self.assertEqual(original,body)
        self.assertEqual(h['X-Navren-Content-SHA256'],hashlib.sha256(body).hexdigest());self.assertEqual(h['Cache-Control'],'no-store')
        self.assertEqual(request('/api/finance/research/attachments/'+aid,h={'Origin':self.h.base})[0],401)
        forged={**headers,'X-Navren-Expected-Revision':'1','X-Navren-Upload-Key':'no-consent','X-Navren-Health-Consent':'false'}
        self.assertEqual(request('/api/finance/research/attachments',body,h=forged)[0],409)
        status,state,_=self.h.request(path='/api/finance/research/policies?limit=1&offset=0')
        self.assertEqual(status,200,state);self.assertTrue(state['existing_policy_records']['items'])
        self.assertEqual(self.h.request(path='/api/finance/research/workspace?owner=forged')[0],400)
        self.assertEqual(self.h.request(path='/api/finance/research/workspace?limit=251')[0],422)

    def test_actual_welcome_proxy_raw_upload_limits_identity_persona_and_late_logout(self):
        from pathlib import Path
        import hashlib
        import http.client
        import json
        import socket
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        import test_sms_auth as sms_fixture
        from welcome_auth import create_welcome_server,COOKIE,MAX_BODY,MAX_RESEARCH_UPLOAD_BODY
        transport=sms_fixture.TestTransport()
        welcome=create_welcome_server(0,Path(self.h.f.f.temp.name)/'research-welcome.sqlite3',
            root=Path(self.h.f.f.temp.name),upstream_port=self.h.server.server_port,clock=self.h.g.bank.clock,
            sms_transport=transport,allow_test_sms=True,experience_provider=self.h.g.experience_provider)
        thread=threading.Thread(target=welcome.serve_forever,daemon=True);thread.start()
        self.addCleanup(welcome.server_close);self.addCleanup(thread.join,2);self.addCleanup(welcome.shutdown)
        auth=welcome.auth;anonymous=auth.new_session()[0]
        challenge=auth.send_sms(anonymous,'register',sms_fixture.PHONE)['challenge_id']
        token,session=auth.sms_register(sms_fixture.PHONE,sms_fixture.PASSWORD,challenge,transport.code(challenge),
            anonymous,password_confirmation=sms_fixture.PASSWORD)
        welcome.experiences.select(token,'tycoon')
        base='http://127.0.0.1:'+str(welcome.server_port)
        def request(path,body=None,headers=None):
            h={'Origin':base,'Cookie':COOKIE+'='+token}
            h.update(headers or {})
            with patch.object(socket,'create_connection',http_fixture.CONNECT):
                connection=http.client.HTTPConnection('127.0.0.1',welcome.server_port,timeout=20)
                try:
                    connection.request('POST' if body is not None else 'GET',path,body=body,headers=h)
                    response=connection.getresponse();return response.status,response.read(),dict(response.getheaders())
                finally:connection.close()
        status,raw,_=request('/api/finance/session');self.assertEqual(status,200,raw)
        bank_session=json.loads(raw);w=bank_session['workspace_id']
        body=b'%PDF-1.7\n'+b'private-welcome-original\x00'*65_000+b'\n%%EOF'
        self.assertGreater(len(body),MAX_BODY)
        headers={'Content-Type':'application/pdf','X-QZ-CSRF':bank_session['csrf'],
            'X-Navren-Workspace':w,'X-Navren-Privacy-Revision':str(bank_session['privacy_revision']),
            'X-Navren-Upload-Key':'welcome-original','X-Navren-Expected-Revision':'0',
            'X-Navren-Confirmed':'true','X-Navren-Health-Consent':'true'}
        status,raw,_=request('/api/finance/research/attachments',body,headers)
        saved=json.loads(raw);self.assertEqual(status,200,saved);aid=saved['attachment']['id']
        self.assertEqual(saved['registered_user_id'],session['registered_user_id'])
        status,original,h=request('/api/finance/research/attachments/'+aid)
        self.assertEqual((status,original),(200,body));self.assertEqual(h['X-Navren-Content-SHA256'],hashlib.sha256(body).hexdigest())
        self.assertIn('attachment',h['Content-Disposition'])
        # 私人JSON也经精确路径透传；普通JSON上限及非精确上传路径保持。
        value=research_fixture.ResearchWorkspaceTests.value()
        json_headers={**headers,'Content-Type':'application/json'}
        status,raw,_=request('/api/finance/research/workspace',json.dumps({'expected_revision':1,'value':value,'confirmed':True}).encode(),json_headers)
        self.assertEqual(status,200,raw)
        for path,length in [('/api/finance/research/workspace',MAX_BODY+1),
                ('/api/finance/research/attachments?unexpected=1',MAX_BODY+1),
                ('/api/finance/research/attachments/',MAX_BODY+1),
                ('/api/finance/research/attachments',MAX_RESEARCH_UPLOAD_BODY+1)]:
            status,raw,_=request(path,b'',{**headers,'Content-Length':str(length)})
            self.assertEqual(status,413,(path,raw))
        status,raw,_=request('/api/finance/research/attachments',body,{**headers,'X-Navren-Upload-Key':'forged-workspace','X-Navren-Workspace':'0'*32,'X-Navren-Expected-Revision':'2'})
        self.assertEqual(status,409,raw)
        # 服务器welcome Cookie jar决定bank主体，客户端伪造bank Cookie不能覆盖。
        status,original,_=request('/api/finance/research/attachments/'+aid,headers={'Cookie':COOKIE+'='+token+'; qz_bank_user=forged'})
        self.assertEqual((status,original),(200,body))
        welcome.experiences.select(token,'grain_farmer')
        self.assertEqual(request('/api/finance/research/attachments/'+aid)[0],404)
        self.assertIn(request('/api/finance/research/attachments',body,headers)[0],(403,409))
        welcome.experiences.select(token,'tycoon')
        self.assertEqual(request('/api/finance/research/attachments/'+aid)[1],body)
        # 原件等待期间退出：原认证撤销与晚到检查保持，旧原始字节不得发送。
        ready=threading.Event();release=threading.Event();original_read=self.h.g.research_workspace.attachment
        def delayed(workspace,attachment_id):
            ready.set()
            if not release.wait(10):raise AssertionError('controlled_delay_not_released')
            return original_read(workspace,attachment_id)
        with patch.object(self.h.g.research_workspace,'attachment',delayed),ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(request,'/api/finance/research/attachments/'+aid)
            self.assertTrue(ready.wait(10))
            try:
                status,raw,_=request('/api/welcome/auth/logout',b'{}',{'Content-Type':'application/json','X-Welcome-CSRF':session['csrf']})
                self.assertEqual(status,200,raw)
            finally:release.set()
            status,raw,_=pending.result(timeout=15)
            self.assertIn(status,(401,409));self.assertNotIn(b'private-welcome-original',raw)
