"""真实本机 HTTP/WS 验收；模拟百炼上游，覆盖权限和取消。"""
import json
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import patch

from websocket import create_connection, WebSocketBadStatusException
from server import create_server, ProxyHandler
from voice_gateway import VoiceGateway, SpeechTask, speech_text, packet, VOICES
from simulated_bank import BankError


class Provider:
    instances = []

    def __init__(self, *args, **kwargs):
        self.events, self.audio_frames = [], []
        self.responses = queue.Queue()
        self.closed = False
        self.__class__.instances.append(self)

    def settimeout(self, timeout):
        pass

    def send(self, raw):
        value = json.loads(raw)
        self.events.append(value)
        action = value['header']['action']
        self.id = value['header']['task_id']
        if action == 'run-task':
            self.kind = value['payload']['task']
            self.emit('task-started')
        elif action == 'finish-task':
            if self.kind == 'tts':
                self.responses.put(b'\x00\x00\x10\x00' * 100)
            else:
                self.emit('result-generated', {'output': {'sentence': {
                    'sentence_id': 1, 'text': '帮我查看账户余额。', 'sentence_end': True}}})
            self.emit('task-finished')

    def emit(self, event, payload=None):
        self.responses.put(json.dumps({'header': {'event': event, 'task_id': self.id}, 'payload': payload or {}}))

    def send_binary(self, value):
        self.audio_frames.append(value)
        self.emit('result-generated', {'output': {'sentence': {
            'sentence_id': 1, 'text': '帮我查看账户', 'sentence_end': False}}})

    def recv(self):
        try:
            return self.responses.get(timeout=1)
        except queue.Empty:
            raise TimeoutError()

    def close(self, timeout=1):
        self.closed = True
        self.responses.put('')


def factory(kind, options, key, url):
    return SpeechTask(kind, options, key, url, connector=Provider)


class VoiceGatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.quiet = patch.object(ProxyHandler, 'log_message', lambda *args: None)
        cls.quiet.start()
        cls.server = create_server(0, Path(cls.tmp.name)/'bank.sqlite3',
                                  VoiceGateway(key='test-secret-do-not-expose', task_factory=factory,allow_cloud_asr=True))
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls.quiet.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.jar = CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.session_state = self.post('/api/finance/session', {})
        self.csrf = self.session_state['csrf']

    def post(self, path, data, csrf=True, origin=None):
        headers = {'Content-Type': 'application/json', 'Origin': origin or self.base}
        if csrf and hasattr(self, 'csrf'):
            headers['X-QZ-CSRF'] = self.csrf
        with self.http.open(urllib.request.Request(self.base+path, data=json.dumps(data).encode(), headers=headers)) as result:
            return json.load(result)

    def connect(self, kind='asr', origin=None, approved_text=None):
        body={'kind':kind}
        if kind=='tts':
            # 测试直接注入已校验答复，客户端自由文字不能获取语音授权。
            g=self.server.finance;w=self.session_state['workspace_id']
            result={'reply':approved_text or '测试已核验答复。','answer_contract':{'kind':'verified_facts'},
                'compliance_pass':True,'personal_revision':g.personal_data.revision(w),
                'privacy_revision':self.session_state['privacy_revision']}
            body['speech_receipt']=g.approve_speech(w,result)
        session = self.post('/api/speech/session', body)
        cookie = '; '.join(f'{item.name}={item.value}' for item in self.jar)
        ws = create_connection(self.base.replace('http:', 'ws:')+session['path'], origin=origin or self.base,
                               cookie=cookie, timeout=3)
        return ws, session

    def test_status_does_not_expose_key(self):
        with self.http.open(self.base+'/api/speech/status') as result:
            raw = result.read().decode()
        self.assertNotIn('test-secret', raw)
        self.assertIn('qwen-audio-3.1-asr-flash-streaming', raw)
        self.assertTrue(json.loads(raw)['configured'])

    def test_streaming_asr_partial_final_and_finish(self):
        ws, _ = self.connect()
        try:
            ws.send(json.dumps({'type': 'start'}))
            self.assertEqual(json.loads(ws.recv())['type'], 'ready')
            ws.send_binary(b'\0\0'*1600)
            partial = json.loads(ws.recv())
            self.assertEqual(partial['type'], 'transcript')
            self.assertFalse(partial['final'])
            ws.send(json.dumps({'type': 'finish'}))
            final = json.loads(ws.recv())
            self.assertTrue(final['final'])
            self.assertEqual(final['text'], '帮我查看账户余额。')
            self.assertEqual(json.loads(ws.recv())['type'], 'done')
            upstream = Provider.instances[-1]
            self.assertEqual(upstream.events[0]['payload']['model'], 'qwen-audio-3.1-asr-flash-streaming')
            self.assertEqual(len(upstream.audio_frames[0]), 3200)
            self.assertEqual({event['header']['task_id'] for event in upstream.events}, {upstream.id})
        finally:
            ws.close()

    def test_tts_binary_audio_and_full_risk_text(self):
        text = '余额：￥12,345.67，收益率3.5%。'+('说明。'*300)+'⚠️ 理财非存款，可能损失本金。'
        ws, _ = self.connect('tts',approved_text=text)
        try:
            ws.send(json.dumps({'type': 'start', 'text': '未经校验的客户端替换正文', 'voice': 'male_warm'}))
            self.assertEqual(json.loads(ws.recv())['sample_rate'], 24000)
            self.assertIsInstance(ws.recv(), bytes)
            self.assertEqual(json.loads(ws.recv())['type'], 'done')
            upstream = Provider.instances[-1]
            combined = ''.join(event['payload']['input']['text'] for event in upstream.events
                               if event['header']['action']=='continue-task')
            self.assertIn('可能损失本金', combined)
            self.assertIn('百分之3.5', combined)
            self.assertIn('12345.67元', combined)
            self.assertNotIn('客户端替换正文',combined)
            self.assertGreater(len(combined), 500)
            self.assertEqual(upstream.events[0]['payload']['parameters']['voice'], VOICES['male_warm'])
        finally:
            ws.close()

    def test_csrf_and_foreign_origin_denied_before_model(self):
        for kwargs in ({'csrf': False}, {'origin': 'https://example.com'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(urllib.error.HTTPError) as error:
                self.post('/api/speech/session', {'kind': 'asr'}, **kwargs)
            self.assertEqual(error.exception.code, 403)
        with self.assertRaises(WebSocketBadStatusException) as error:
            self.connect(origin='https://example.com')
        self.assertEqual(error.exception.status_code, 403)

    def test_ticket_single_use_and_session_binding(self):
        ws, session = self.connect()
        ws.close()
        cookie = '; '.join(f'{item.name}={item.value}' for item in self.jar)
        with self.assertRaises(WebSocketBadStatusException) as error:
            create_connection(self.base.replace('http:', 'ws:')+session['path'], origin=self.base, cookie=cookie)
        self.assertEqual(error.exception.status_code, 403)
        other = self.post('/api/speech/session', {'kind': 'asr'})
        with self.assertRaises(WebSocketBadStatusException):
            create_connection(self.base.replace('http:', 'ws:')+other['path'], origin=self.base)

    def test_cancel_closes_upstream_and_releases_capacity(self):
        ws, _ = self.connect()
        ws.send(json.dumps({'type': 'start'}))
        self.assertEqual(json.loads(ws.recv())['type'], 'ready')
        upstream = Provider.instances[-1]
        ws.send(json.dumps({'type': 'cancel'}))
        ws.close()
        for _ in range(30):
            if upstream.closed: break
            time.sleep(0.02)
        self.assertTrue(upstream.closed)
        self.assertTrue(self.server.voice.slots.acquire(blocking=False))
        self.server.voice.slots.release()

    def test_invalid_voice_rejected_and_disclosure_preserved(self):
        with self.assertRaises(BankError):
            SpeechTask('tts', {'text': '测试', 'voice': 'arbitrary'}, 'test', 'wss://example', connector=Provider)
        self.assertEqual(speech_text('建议。\n⚠️ 请确认风险。'), '建议。。注意： 请确认风险。')


if __name__ == '__main__':
    unittest.main()
