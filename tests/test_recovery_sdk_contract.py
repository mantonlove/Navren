"""恢复短信的官方SDK参数合同；客户端明确隔离，不发送短信。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import hashlib
import json
import tempfile
import unittest

from alibabacloud_dypnsapi20170525.models import (
    CheckSmsVerifyCodeRequest, SendSmsVerifyCodeRequest,
)
from sms_aliyun import AliyunSmsTransport, SCHEMES


class RecoverySdkContractTests(unittest.TestCase):
    def test_recovery_send_and_check_share_valid_scheme_without_operation_code_reuse(self):
        phone = '13800000001'
        codes = {'navren-operation': '246810', 'navren-fin-recovery': '135790'}
        sent, checked, issued = [], [], {}
        sign = '恒创联众'

        class Body:
            def __init__(self, value):
                self.value = value

            def to_map(self):
                return self.value

        def response(model):
            return SimpleNamespace(body=Body({
                'Success': True, 'Code': 'OK', 'RequestId': 'local-sdk-contract', 'Model': model,
            }))

        def send(request, runtime):
            self.assertIsInstance(request, SendSmsVerifyCodeRequest)
            wire = request.to_map()
            sent.append(wire)
            self.assertLessEqual(len(wire['SchemeName']), 20)
            self.assertEqual(wire['SignName'], sign)
            self.assertEqual(wire['TemplateCode'], '100005')
            self.assertIs(wire['ReturnVerifyCode'], False)
            self.assertEqual(json.loads(wire['TemplateParam'])['code'], '##code##')
            self.assertEqual(wire['CodeLength'], 6)
            self.assertEqual(wire['DuplicatePolicy'], 1)
            self.assertEqual(wire['Interval'], 60)
            self.assertEqual(wire['AutoRetry'], 0)
            self.assertIs(runtime.autoretry, False)
            issued[(wire['PhoneNumber'], wire['SchemeName'], wire['OutId'])] = codes[wire['SchemeName']]
            # 即使供应商异常带回明文，适配器也只向上返回摘要。
            return response({'VerifyCode': codes[wire['SchemeName']]})

        def check(request, runtime):
            self.assertIsInstance(request, CheckSmsVerifyCodeRequest)
            wire = request.to_map()
            checked.append(wire)
            self.assertLessEqual(len(wire['SchemeName']), 20)
            self.assertEqual(wire['CaseAuthPolicy'], 2)
            self.assertIs(runtime.autoretry, False)
            key = (wire['PhoneNumber'], wire['SchemeName'], wire['OutId'])
            result = 'PASS' if issued.get(key) == wire['VerifyCode'] else 'FAIL'
            return response({'VerifyResult': result})

        client = SimpleNamespace(send_sms_verify_code_with_options=send,
                                 check_sms_verify_code_with_options=check)
        with tempfile.TemporaryDirectory(prefix='navren-recovery-sdk-') as temp, \
                patch('socket.socket', side_effect=AssertionError('network forbidden')), \
                patch('socket.getaddrinfo', side_effect=AssertionError('network forbidden')), \
                patch('sms_aliyun._credential', side_effect=AssertionError('credential lookup forbidden')):
            config = {'enabled': True, 'mode': 'restricted', 'sign_name': sign,
                      'allowed_numbers': [phone],
                      'templates': {'register': '100001', 'login': '100001', 'bind': '100004',
                                    'operation': '100005', 'financial_recovery': '100005'},
                      'budget_database': str(Path(temp)/'budget.sqlite3'), 'max_send_attempts': 2}
            transport = AliyunSmsTransport(config, client=client, clock=lambda: 1000.0)
            self.assertTrue(transport.supports_purpose('financial_recovery'))
            self.assertEqual(SCHEMES, {'register': 'navren-register', 'login': 'navren-login',
                                      'bind': 'navren-bind', 'operation': 'navren-operation',
                                      'financial_recovery': 'navren-fin-recovery'})
            self.assertEqual(len(SCHEMES['financial_recovery']), 19)
            returned = []
            for purpose, reference in [('operation', 'a'*32), ('financial_recovery', 'b'*32)]:
                returned.append(transport.send(phone, purpose, reference, expires_at=1120, deadline=1010))
            for purpose, reference, correct, other in [
                    ('financial_recovery', 'b'*32, codes['navren-fin-recovery'], codes['navren-operation']),
                    ('operation', 'a'*32, codes['navren-operation'], codes['navren-fin-recovery'])]:
                self.assertIsNone(transport.verify(phone, purpose, reference, other, deadline=1010))
                verified = transport.verify(phone, purpose, reference, correct, deadline=1010)
                self.assertEqual(verified, hashlib.sha256(
                    ('local-sdk-contract:'+purpose+':'+reference).encode()).hexdigest())
                returned.append(verified)
            self.assertEqual(len(sent), 2)
            self.assertEqual(len(checked), 4)
            self.assertEqual(sent[1]['SchemeName'], 'navren-fin-recovery')
            self.assertTrue(all(c['SchemeName'] == sent[1]['SchemeName'] for c in checked[:2]))
            self.assertTrue(all(c['SchemeName'] == sent[0]['SchemeName'] for c in checked[2:]))
            self.assertTrue(all(len(value) == 64 for value in returned))
            for value in (phone, *codes.values()):
                self.assertNotIn(value, json.dumps(returned))


if __name__ == '__main__':
    unittest.main()
