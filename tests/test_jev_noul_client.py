"""多标签协议新增边界：不把坏概率、未知用量或错模型当可执行路由。"""
import io
import json
import unittest

from agent_runtime.jev_client import JevClient, JevError


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *unused): self.close()


class NoulProtocolTests(unittest.TestCase):
    questions = {
        'research': {'type': 'noul', 'instructions': '需要公开产品研究吗？'},
        'read_only': {'type': 'noul', 'instructions': '本轮仅查询吗？',
                      'criteria': {'true': '只读', 'false': '含办理请求'}},
        'object': {'type': 'choice', 'instructions': '选择已有对象或未知',
                   'criteria': {'known': '已登记', 'none': '未绑定'}},
    }

    def payload(self):
        return {'model': 'jev-1.13.0', 'usage': {'input_tokens': 9, 'output_tokens': 3},
                'answers': {'research': {'type': 'noul', 'noul': .92},
                            'read_only': {'type': 'noul', 'noul': .97},
                            'object': {'type': 'choice', 'choice': 'none', 'confidence': 1.,
                                       'probabilities': {'known': 0., 'none': 1.}}}}

    def evaluate(self, payload):
        sent = []
        def opener(request, **kwargs):
            sent.append(request.data)
            return Response(json.dumps(payload, allow_nan=True).encode())
        client = JevClient(urlopen=opener, key_loader=lambda: 'in-memory-fixture')
        return client.evaluate({'current_request': '比较条款，不购买'}, self.questions), sent

    def test_independent_flags_and_choice_are_one_metered_request(self):
        (answers, receipt), sent = self.evaluate(self.payload())
        self.assertEqual(len(sent), 1)
        self.assertEqual(set(json.loads(sent[0])['questions']), set(self.questions))
        self.assertEqual(answers['research']['noul'], .92)
        self.assertNotIn('confidence', answers['research'])
        self.assertEqual(receipt['usage'], {'input_tokens': 9, 'output_tokens': 3})
        self.assertNotIn('in-memory-fixture', json.dumps(receipt))

    def test_bad_noul_keeps_real_usage_but_never_returns_decisions(self):
        for value in (True, None, '0.9', -1, 1.01, float('nan'), float('inf')):
            with self.subTest(value=value):
                data = self.payload(); data['answers']['research']['noul'] = value
                with self.assertRaises(JevError) as caught:
                    self.evaluate(data)
                self.assertTrue(caught.exception.receipt['request_sent'])
                self.assertEqual(caught.exception.receipt['usage']['input_tokens'], 9)
                self.assertEqual(caught.exception.receipt['outcome'], 'failed')

    def test_extra_confidence_is_not_a_valid_noul(self):
        data = self.payload(); data['answers']['research']['confidence'] = .99
        with self.assertRaisesRegex(JevError, 'jev_invalid_noul_probability'):
            self.evaluate(data)

    def test_missing_answer_or_wrong_model_not_accepted(self):
        for change in ('missing', 'model'):
            data = self.payload()
            if change == 'missing': del data['answers']['read_only']
            else: data['model'] = 'jev-latest'
            with self.subTest(change=change), self.assertRaises(JevError):
                self.evaluate(data)

    def test_unknown_usage_stays_unknown(self):
        data = self.payload(); del data['usage']
        with self.assertRaises(JevError) as caught:
            self.evaluate(data)
        self.assertIsNone(caught.exception.receipt['usage'])

    def test_invalid_question_or_oversize_never_reads_key_or_sends(self):
        def forbidden(*args, **kwargs): raise AssertionError('no key or HTTP permitted')
        client = JevClient(urlopen=forbidden, key_loader=forbidden)
        bad = {'x': {'type': 'noul', 'instructions': '判断', 'criteria': {'maybe': '未知'}}}
        with self.assertRaises(JevError) as caught: client.evaluate({}, bad)
        self.assertFalse(caught.exception.receipt['request_sent'])
        with self.assertRaises(JevError) as caught:
            client.evaluate('中' * 6000, self.questions)
        self.assertEqual(caught.exception.code, 'jev_input_limit')
        self.assertFalse(caught.exception.receipt['request_sent'])


if __name__ == '__main__': unittest.main()
