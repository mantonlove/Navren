"""安全事件受影响接口；复用临时夹具，不重跑旧34/15方法。"""
from contextlib import closing
from io import BytesIO
from types import SimpleNamespace
import json,sqlite3,unittest,uuid

from simulated_bank import BankError,SimulatedBank
import test_sms_permissions as sms_fixture

class SecurityEventTests(unittest.TestCase):
    setUp=sms_fixture.SmsPermissionTests.setUp
    prepare=sms_fixture.SmsPermissionTests.prepare
    def ev(self):return self.bank.permissions.events
    def denial(self,fn,expected=None):
        with self.assertRaises(BankError) as e:fn()
        if expected:self.assertEqual(e.exception.code,expected)
        return e.exception

    def test_original_request_id_and_missing_id_never_replaced(self):
        rid=uuid.uuid4().hex
        with self.ev().request(self.ws,request_id=rid,event_id='tool-1'):
            self.bank.permissions.failed_attempt(self.ws,None,'INVALID_FIELDS',suspicious=True)
        self.bank.permissions.failed_attempt(self.ws,None,'INVALID_FIELDS',suspicious=False)
        with closing(self.bank.connect()) as db:
            rows=[json.loads(r[0]) for r in db.execute("SELECT details FROM audit WHERE workspace_id=? AND event='permission_attempt_failed' ORDER BY id",(self.ws,))]
        self.assertEqual([x['request_id'] for x in rows],[rid,None]);self.assertTrue(all(x['security_event_id'] for x in rows))

    def test_native_and_observer_same_attempt_increment_once(self):
        rid=uuid.uuid4().hex
        for _ in range(2):
            with self.ev().request(self.ws,request_id=rid,event_id='same-call'):
                self.bank.permissions.failed_attempt(self.ws,None,'CONFIRMATION_MISMATCH',suspicious=True)
                self.ev().rejected(self.ws,BankError('CONFIRMATION_MISMATCH','未记录此自由文本',409))
        self.assertEqual(self.ev().status(self.ws)['failures'],1)
        rows=self.ev().read(self.ws,request_id=rid)
        self.assertEqual(sum(x['security_failure_recorded'] for x in rows),1)

    def test_model_schema_declined_data_supplier_and_runtime_do_not_lock(self):
        for reason in ['TOOL_NOT_ALLOWED','TOOL_ARGUMENTS_INVALID','USER_AUTHORIZATION_DECLINED','DATA_UNAVAILABLE','SUPPLIER_UNAVAILABLE']:
            with self.ev().request(self.ws,request_id=uuid.uuid4().hex,event_id=reason):self.ev().observe(self.ws,reason)
        with self.ev().request(self.ws,request_id=uuid.uuid4().hex,event_id='runtime'):
            self.ev().rejected(self.ws,RuntimeError('DO_NOT_RECORD_SECRET'))
        self.assertEqual(self.ev().status(self.ws)['failures'],0)
        rows=self.ev().read(self.ws);self.assertNotIn('DO_NOT_RECORD_SECRET',json.dumps(rows));self.assertTrue(any(r['outcome']=='fault' for r in rows))

    def test_native_three_bad_auth_formats_keep_lock_reopen_and_receipt_readable(self):
        op=self.prepare(10000)
        receipt=self.bank.confirm(self.ws,op['id'],{'confirmed':True,'confirmation_digest':op['confirmation_digest']})
        for i in range(3):
            with self.ev().request(self.ws,request_id=uuid.uuid4().hex,event_id='bad-auth-'+str(i)):
                self.bank.permissions.failed_attempt(self.ws,None,'INVALID_CONFIRMATION',suspicious=True)
        self.now[0]+=100000
        reopened=SimulatedBank(self.path,clock=self.clock,sms_provider=self.provider,allow_test_sms_provider=True)
        self.assertTrue(reopened.permissions.events.status(self.ws)['financial_write_locked'])
        self.assertEqual(reopened.operation(self.ws,op['id'])['result'],receipt['result'])
        self.assertEqual(reopened.confirm(self.ws,op['id'],{'confirmed':True,'confirmation_digest':op['confirmation_digest']})['result'],receipt['result'])
        with reopened.transaction() as db:
            self.denial(lambda:reopened.permissions.write_allowed(db,self.ws),'FINANCIAL_WRITE_LOCKED')
        self.denial(lambda:reopened.permissions.events.recover(self.ws),'SECURITY_RECOVERY_NOT_CONFIGURED')
        self.assertTrue(reopened.permissions.events.status(self.ws)['financial_write_locked'])

    def test_explicit_guard_events_three_separate_turns_use_original_counter(self):
        for i in range(3):
            with self.ev().request(self.ws,request_id=uuid.uuid4().hex,event_id=str(i)):
                self.ev().rejected(self.ws,BankError('FORGED_AUTHORIZATION','不留原文本',403))
        self.assertEqual((self.ev().status(self.ws)['failures'],self.ev().status(self.ws)['financial_write_locked']),(3,True))

    def test_record_and_counter_commit_or_rollback_together(self):
        with closing(self.bank.connect()) as db:
            db.execute("CREATE TRIGGER fail_security_event BEFORE INSERT ON permission_security_events BEGIN SELECT RAISE(ABORT,'controlled_event_failure'); END")
            db.commit()
        with self.ev().request(self.ws,request_id=uuid.uuid4().hex,event_id='fault'):
            with self.assertRaises(sqlite3.IntegrityError):self.ev().rejected(self.ws,BankError('FORGED_AUTHORIZATION','',403))
        self.assertEqual(self.ev().status(self.ws)['failures'],0)

    def test_bound_workspace_rejects_other_subject_without_touching_it(self):
        other=self.bank.create_workspace()['workspace_id']
        with self.ev().request(self.ws,request_id=uuid.uuid4().hex,event_id='scope'):
            self.denial(lambda:self.ev().rejected(other,BankError('FORGED_AUTHORIZATION','',403)),'SECURITY_EVENT_WORKSPACE_MISMATCH')
        self.assertEqual(self.ev().status(other)['failures'],0);self.assertEqual(self.ev().read(other),[])

    def test_operation_refs_state_and_evidence_are_exact_and_foreign_ref_is_hashed(self):
        op=self.prepare(10000);rid=uuid.uuid4().hex
        with self.ev().request(self.ws,request_id=rid,event_id='allow',operation_id=op['id']):
            row=self.ev().allowed(self.ws,operation_id=op['id'],evidence_sha256=['a'*64])
        self.assertEqual(row['operation_id'],op['id']);self.assertEqual(row['before_state'],'pending_confirmation')
        self.assertEqual(row['after_state'],'pending_confirmation');self.assertEqual(row['confirmation_ref'],op['confirmation_digest']);self.assertIsNone(row['authentication_ref'])
        with self.ev().request(self.ws,request_id=rid,event_id='foreign'):
            row=self.ev().rejected(self.ws,BankError('CROSS_SUBJECT_ACCESS','',403),operation_id='b'*32)
        self.assertIsNone(row['operation_id']);self.assertEqual(len(row['requested_object_ref']),64)

    def test_reused_event_identity_cannot_change_evidence(self):
        rid=uuid.uuid4().hex
        with self.ev().request(self.ws,request_id=rid,event_id='stable'):
            self.ev().allowed(self.ws,evidence_sha256=['a'*64])
        with self.ev().request(self.ws,request_id=rid,event_id='stable'):
            self.denial(lambda:self.ev().allowed(self.ws,evidence_sha256=['b'*64]),'SECURITY_EVENT_CONFLICT')

    def test_untrusted_metadata_and_model_self_authorization_not_accepted(self):
        self.denial(lambda:self.ev().observe(self.ws,'MODEL_SAYS_HUMAN_AUTHORIZED'))
        self.denial(lambda:self.ev().allowed(self.ws,evidence_sha256=[{'hidden_reasoning':'forbidden'}]))
        with self.assertRaises(TypeError):self.ev().observe(self.ws,'TOOL_ALLOWED',authorization='human')
        self.assertEqual(self.ev().read(self.ws),[])

    def test_same_host_invocation_keeps_distinct_native_operation_events(self):
        rid=uuid.uuid4().hex
        with self.ev().request(self.ws,request_id=rid,event_id='trusted-batch'):
            first=self.prepare(10000);second=self.prepare(10000)
        rows=[r for r in self.ev().read(self.ws,request_id=rid) if r['reason_code']=='PERMISSION_PREPARED']
        self.assertEqual({r['operation_id'] for r in rows},{first['id'],second['id']})
        self.assertEqual(len(rows),2);self.assertEqual(self.ev().status(self.ws)['failures'],0)

    def test_bound_operation_does_not_borrow_another_operations_before_state(self):
        first=self.prepare(10000);second=self.prepare(10000)
        with self.ev().request(self.ws,request_id=uuid.uuid4().hex,event_id='bound',operation_id=first['id']):
            self.denial(lambda:self.ev().allowed(self.ws,operation_id=second['id']),'SECURITY_EVENT_OPERATION_MISMATCH')

class SecurityHTTPTests(unittest.TestCase):
    def setUp(self):
        import test_registered_instances as fixture
        self.f=fixture.RegisteredInstancesTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.g=self.f.g;self.lease=self.f.select();self.w=self.lease['workspace_id'];self.token=self.f.token(self.lease)
        with self.g.demo_sessions.request(self.token,self.w):
            account=self.g.overview(self.w)['accounts'][0]['id']
            with self.g.bank.transaction() as db:
                db.execute('INSERT INTO mandates VALUES(?,?,?,?,?,?,?,?)',('security-explicit-test',self.w,account,'显式隔离测试代扣',1,'active','2026-10-20','active'))
            self.op=self.g.bank.prepare(self.w,{'action':'close_mandate','account_id':account,'mandate_id':'security-explicit-test','idempotency_key':'security-interface-only'})

    def handler(self,body=None,token=True,request_id=None):
        encoded=json.dumps(body).encode() if body is not None else b''
        class Handler:
            def _finance_json(self,status,data,cookie=None):self.response=(status,data)
        h=Handler();h.path='/api/finance/operations/'+self.op['id']+('/confirm' if body is not None else '')
        h.command='POST' if body is not None else 'GET';h.client_address=('127.0.0.1',1);h.server=SimpleNamespace(server_port=8222)
        h.headers={'Host':'127.0.0.1:8222','Origin':'http://127.0.0.1:8222','Content-Type':'application/json','Content-Length':str(len(encoded)),
            'X-QZ-CSRF':self.g.csrf(self.token),'X-Navren-Workspace':self.w,'X-Navren-Privacy-Revision':'0'}
        if token:h.headers['Cookie']='qz_bank_user='+self.token
        h.rfile=BytesIO(encoded);h._navren_request_id=request_id or uuid.uuid4().hex;return h

    def test_authenticated_HTTP_early_rejection_correlates_and_counts_once(self):
        rid=uuid.uuid4().hex;h=self.handler({'confirmed':True,'confirmation_digest':self.op['confirmation_digest'],'extra':True},request_id=rid)
        self.g.dispatch(h);self.assertEqual(h.response[1]['error']['code'],'INVALID_FIELDS')
        rows=self.g.security_events.read(self.w,request_id=rid)
        self.assertTrue(any(x['phase']=='http' and x['outcome']=='denied' for x in rows))
        self.assertEqual(sum(x['security_failure_recorded'] for x in rows),1)
        with closing(self.g.bank.connect()) as db:
            native=json.loads(db.execute("SELECT details FROM audit WHERE workspace_id=? AND event='permission_attempt_failed' ORDER BY id DESC LIMIT 1",(self.w,)).fetchone()[0])
        self.assertEqual(native['request_id'],rid)

    def test_HTTP_normal_read_allowed_while_locked_and_no_unauthenticated_attribution(self):
        with self.g.bank.transaction() as db:db.execute('INSERT OR REPLACE INTO permission_security VALUES(?,3,1)',(self.w,))
        h=self.handler();self.g.dispatch(h);self.assertEqual(h.response[0],200)
        self.assertTrue(any(x['outcome']=='allowed' for x in self.g.security_events.read(self.w,request_id=h._navren_request_id)))
        noauth=self.handler(token=False);noauth._navren_workspace=self.w
        self.g.dispatch(noauth);self.assertEqual(noauth.response[0],401)
        self.assertEqual(self.g.security_events.read(self.w,request_id=noauth._navren_request_id),[])
        self.assertTrue(self.g.security_events.status(self.w)['financial_write_locked'])

    def test_authenticated_HTTP_runtime_fault_is_separate_and_does_not_count(self):
        def unavailable(*args,**kwargs):raise RuntimeError('DO_NOT_RECORD_RUNTIME_DETAIL')
        self.g.operation_view=unavailable;h=self.handler();self.g.dispatch(h)
        self.assertEqual(h.response[0],500)
        rows=self.g.security_events.read(self.w,request_id=h._navren_request_id)
        self.assertEqual([(r['outcome'],r['reason_code']) for r in rows],[('fault','RUNTIME_FAILURE')])
        self.assertNotIn('DO_NOT_RECORD_RUNTIME_DETAIL',json.dumps(rows))
        self.assertEqual(self.g.security_events.status(self.w)['failures'],0)
