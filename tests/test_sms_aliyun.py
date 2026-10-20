"""真实SDK模型配合隔离客户端；核验状态、供应商隔离、硬限额和异常脱敏。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import types
import unittest

from sms_aliyun import AliyunSmsTransport, SmsFailure


class Body:
    def __init__(self,value): self.value=value
    def to_map(self): return self.value


class Client:
    def __init__(self): self.sent=[]; self.checked=[]; self.result='PASS'; self.error=None
    def send_sms_verify_code_with_options(self,request,runtime):
        self.sent.append(request)
        if self.error: raise RuntimeError(self.error)
        return types.SimpleNamespace(body=Body({'Success':True,'Code':'OK','RequestId':'test-send-id'}))
    def check_sms_verify_code_with_options(self,request,runtime):
        self.checked.append(request)
        if self.error: raise RuntimeError(self.error)
        return types.SimpleNamespace(body=Body({'Success':True,'Code':'OK','RequestId':'test-check-id',
                                               'Model':{'VerifyResult':self.result}}))


class AliyunTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.client=Client(); self.now=1000.0
        self.config={'enabled':True,'sign_name':'恒创联众','allowed_numbers':['13800000001'],
            'templates':{'register':'100001','login':'100001','bind':'100004','operation':'100005'},
            'budget_database':str(Path(self.tmp.name)/'budget.sqlite3'),'max_send_attempts':2}
        self.transport=AliyunSmsTransport(self.config,client=self.client,clock=lambda:self.now)
    def tearDown(self): self.tmp.cleanup()
    def send(self,transport=None,purpose='login'):
        return (transport or self.transport).send('13800000001',purpose,'a'*32,expires_at=1120,deadline=1010)
    def test_send_never_requests_raw_code_and_schemes_are_separate(self):
        self.send(); self.send(purpose='operation')
        login,operation=self.client.sent
        self.assertIs(login.return_verify_code,False)
        self.assertEqual(login.code_length,6)
        self.assertIn('##code##',login.template_param)
        self.assertLessEqual(login.valid_time,120)
        self.assertNotEqual(login.scheme_name,operation.scheme_name)
        self.assertEqual(operation.template_code,'100005')
    def test_successful_http_request_does_not_equal_valid_code(self):
        self.client.result='UNKNOWN'
        self.assertIsNone(self.transport.verify('13800000001','login','a'*32,'123456',deadline=1010))
        self.client.result='PASS'
        self.assertTrue(self.transport.verify('13800000001','login','a'*32,'123456',deadline=1010))
    def test_external_errors_do_not_reveal_phone_code_or_keys(self):
        self.client.error='13800000001 123456 secret-sensitive-payload'
        with self.assertRaises(SmsFailure) as raised: self.send()
        for value in ('13800000001','123456','secret-sensitive-payload'):
            self.assertNotIn(value,str(raised.exception))
    def test_authorized_total_is_shared_between_instances_and_concurrent_calls(self):
        another=AliyunSmsTransport(self.config,client=self.client,clock=lambda:self.now)
        def attempt(i):
            try: self.send(self.transport if i%2 else another); return True
            except SmsFailure: return False
        with ThreadPoolExecutor(8) as pool: passed=list(pool.map(attempt,range(8)))
        self.assertEqual(sum(passed),2)
        self.assertEqual(len(self.client.sent),2)
    def test_unapproved_number_disabled_service_and_expiry_never_call_api(self):
        with self.assertRaises(SmsFailure): self.transport.send('13900000002','login','a'*32,expires_at=1120,deadline=1010)
        self.now=1011
        with self.assertRaises(SmsFailure): self.send()
        disabled=AliyunSmsTransport(dict(self.config,enabled=False),client=self.client)
        self.assertFalse(disabled.configured)
        self.assertEqual(len(self.client.sent),0)
    def test_budget_wait_cannot_send_after_deadline(self):
        reserve = self.transport._reserve_send_budget
        def delayed_reserve():
            reserve()
            self.now = 1011
        self.transport._reserve_send_budget = delayed_reserve
        with self.assertRaises(SmsFailure): self.send()
        self.assertEqual(len(self.client.sent),0)


if __name__=='__main__': unittest.main()
