"""正式 HTTP/WS 媒体接线；隔离注册身份、库和合成 PCM，无公网/模型/真实短信。"""
from contextlib import closing
from functools import partial
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
import wave
from unittest.mock import patch

from cryptography.fernet import Fernet
from PIL import Image
from websocket import create_connection

from server import create_server, ProxyHandler
from base_runtime.service_assembly import AssembledGateway
from local_knowledge.runtime import EvidenceRuntime
from local_knowledge.store import LocalKnowledgeProvider
from voice_gateway import VoiceGateway

ROOT = Path(__file__).resolve().parents[1]
FINANCIAL = ROOT / 'outputs/realistic-history/navren-financial.sqlite3'
PCM = b''.join(struct.pack('<h', int(8000 * __import__('math').sin(i / 8))) for i in range(24000))


class NoModel:
    def __init__(self): self._locks = [threading.Lock() for _ in range(32)]; self.calls = 0
    def process(self, *args, **kwargs): self.calls += 1; raise AssertionError('ISOLATED_NO_MODEL')


class SpeechFixture:
    seen = []
    def __init__(self, kind, options, key, url):
        assert kind == 'tts'
        self.id = 'media-wire-fixture'; self.frames = queue.Queue(); self.closed = False
        self.__class__.seen.append(dict(options))
        for offset in range(0, len(PCM), 12000): self.frames.put(PCM[offset:offset+12000])
        self.frames.put({'header': {'event': 'task-finished'}})
    def receive(self):
        try: return self.frames.get(timeout=.2)
        except queue.Empty: raise TimeoutError()
    def close(self): self.closed = True


def image_bytes(large=False):
    value = io.BytesIO()
    if large:
        # 真 PNG 大于1MiB，证明独立原始上传未扩大普通 Agent JSON。
        picture = Image.frombytes('RGB', (720, 720), os.urandom(720 * 720 * 3))
    else: picture = Image.new('RGB', (8, 8), '#326ab1')
    picture.save(value, format='PNG'); return value.getvalue()


def audio_bytes():
    value = io.BytesIO()
    with wave.open(value, 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(24000); output.writeframes(PCM)
    return value.getvalue()


class ChatMediaWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='navren-media-http-')
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {
            'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
            'NAVREN_KNOWLEDGE_DB': str(self.state / 'knowledge.sqlite3'),
            'NAVREN_PUBLIC_SYNC_ENABLED': 'false', 'NAVREN_MESSAGE_WORKER_ENABLED': 'false',
            'NAVREN_LIVING_WORKER_ENABLED': 'false'})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.quiet = patch.object(ProxyHandler, 'log_message', lambda *_: None)
        self.quiet.start(); self.addCleanup(self.quiet.stop)
        self.owner = uuid.uuid4().hex
        self.start_server(); self.addCleanup(self.stop_server)
        self.login(self.owner)

    def start_server(self):
        self.agent = NoModel()
        self.evidence = EvidenceRuntime(LocalKnowledgeProvider(self.state / 'knowledge.sqlite3'), background=False)
        voice = VoiceGateway(key='fixture-no-provider', url='ws://127.0.0.1:1', task_factory=SpeechFixture)
        voice.local_asr = voice.local_tts = None
        self.server = create_server(0, self.state / 'bank.sqlite3', voice=voice, conversation_agent=self.agent,
            evidence_runtime=self.evidence, market_database=self.state / 'market.sqlite3', public_sync_enabled=False,
            finance_factory=partial(AssembledGateway, registered_mode=True, financial_database=FINANCIAL))
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def stop_server(self):
        if self.server:
            self.server.shutdown(); self.server.server_close(); self.thread.join(5); self.server = None

    def login(self, owner, persona='working_landlady'):
        lease = self.server.finance.experience_provider.select_registered(owner, persona, 'media-' + uuid.uuid4().hex,
            auth_expires_at=time.time()+1800)
        self.workspace = lease['workspace_id']; self.token = lease['upstream_cookies']['qz_bank_user']
        self.csrf = self.server.finance.csrf(self.token)
        return self.get_json('/api/finance/chat-media/context')

    def request(self, path, body=None, *, headers=None, anonymous=False, method=None):
        values = {'Origin': self.base}
        if not anonymous: values.update(Cookie='qz_bank_user=' + self.token, **{'X-QZ-CSRF': self.csrf})
        values.update(headers or {})
        req = urllib.request.Request(self.base+path, data=body, headers=values, method=method)
        return self.http.open(req, timeout=12)

    def get_json(self, path):
        with self.request(path) as response: return json.load(response)

    def post_json(self, path, data):
        with self.request(path, json.dumps(data).encode(), headers={'Content-Type': 'application/json'}) as response:
            return json.load(response)

    def upload(self, raw, mime):
        context = self.get_json('/api/finance/chat-media/context')
        headers = {'Content-Type': mime, 'X-Navren-Upload-Key': uuid.uuid4().hex,
            'X-Navren-Expected-Workspace': context['workspace_binding'],
            'X-Navren-Privacy-Revision': str(context['privacy_revision']),
            'X-Navren-Archive-Epoch': str(context['archive_epoch'])}
        with self.request('/api/finance/chat-media', raw, headers=headers) as response:
            self.assertEqual(response.status, 201); value = json.load(response)
        self.assertEqual(value['media']['sha256'], hashlib.sha256(raw).hexdigest())
        return value['media']

    def read(self, media):
        with self.request(media['content_url']) as response:
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(response.headers['Accept-Ranges'], 'none')
            return response.read()

    def assert_http(self, status, callback):
        with self.assertRaises(urllib.error.HTTPError) as caught: callback()
        self.assertEqual(caught.exception.code, status)
        with caught.exception as response: return json.load(response)

    def test_raw_large_image_and_blank_audio_archive_without_model(self):
        raw = image_bytes(True); self.assertGreater(len(raw), 1024*1024)
        image = self.upload(raw, 'image/png'); audio = self.upload(audio_bytes(), 'audio/wav')
        self.assert_http(404, lambda: self.read(image))  # pending原件不能读取。
        value = self.post_json('/api/agent', {'text': '', 'media_ids': [image['media_id'], audio['media_id']]})
        self.assertEqual(value['attachment_receipt']['status'], 'archived')
        self.assertEqual(self.agent.calls, 0)
        page = self.get_json('/api/finance/dialogue/history')
        self.assertEqual(len(page['items']), 1)
        turn = page['items'][0]
        self.assertEqual(turn['user']['content'], '')
        self.assertEqual([x['media_id'] for x in turn['user']['attachments']], [image['media_id'], audio['media_id']])
        self.assertEqual(turn['assistant']['status'], 'archive_unavailable')
        self.assertEqual(self.read(image), raw); self.assertEqual(self.read(audio), audio_bytes())
        self.assert_http(413, lambda: self.request('/api/agent', json.dumps({'text': 'a'*1050000}).encode(), headers={'Content-Type':'application/json'}))

    def test_failed_assistant_keeps_user_original_and_text(self):
        image = self.upload(image_bytes(), 'image/png')
        self.assert_http(500, lambda: self.post_json('/api/agent', {'text':'请看这张图片', 'media_ids':[image['media_id']]}))
        page = self.get_json('/api/finance/dialogue/history')
        self.assertEqual(page['items'][0]['user']['content'], '请看这张图片')
        self.assertEqual(page['items'][0]['user']['attachments'][0]['media_id'], image['media_id'])
        self.assertEqual(self.read(image), image_bytes())

    def test_fixed_published_reply_real_websocket_saves_identical_pcm(self):
        result = self.post_json('/api/agent', {'text':'你好'})
        self.assertIn('speech_receipt', result)
        ticket = self.post_json('/api/speech/session', {'kind':'tts','speech_receipt':result['speech_receipt']})
        ws = create_connection(self.base.replace('http:', 'ws:') + ticket['path'], origin=self.base,
            cookie='qz_bank_user='+self.token, timeout=10, http_proxy_host=None)
        frames = bytearray(); media = None
        try:
            ws.send(json.dumps({'type':'start','text':'客户端伪造正文'}))
            for _ in range(30):
                event = ws.recv()
                if isinstance(event, bytes): frames.extend(event); continue
                event = json.loads(event)
                self.assertNotEqual(event['type'], 'error', event)
                if event['type']=='done': media=event['media']; break
        finally: ws.close()
        self.assertIsNotNone(media); self.assertEqual(bytes(frames), PCM)
        self.assertEqual(self.read(media), PCM)
        self.assertEqual(media['mime_type'], 'audio/pcm')
        self.assertEqual(media['sha256'], hashlib.sha256(PCM).hexdigest())
        approved_text = self.server.finance.resolve_speech(self.workspace, result['speech_receipt'], self.token)
        self.assertEqual(SpeechFixture.seen[-1]['text'], approved_text)
        self.assertNotIn('客户端伪造正文', SpeechFixture.seen[-1]['text'])
        archived = self.get_json('/api/finance/dialogue/history')['items'][0]['assistant']
        self.assertEqual(archived['status'], 'published_local_rule')
        self.assertEqual(archived['attachments'], [media]); self.assertFalse(archived['execution_authority'])
        self.assertNotIn('speech_receipt', archived)

    def test_anonymous_csrf_origin_and_client_authority_denied(self):
        self.assert_http(401, lambda:self.request('/api/finance/chat-media/context', anonymous=True))
        self.assert_http(403, lambda:self.request('/api/finance/chat-media', image_bytes(), headers={'Content-Type':'image/png','X-QZ-CSRF':''}))
        self.assert_http(403, lambda:self.request('/api/finance/chat-media/context',headers={'Origin':'https://example.com'}))
        for body in ({'text':'你好','attachments':[{'media_id':'a'*64}]}, {'text':'你好','media_ids':['a'*64],'role':'assistant'},
                     {'text':'你好','media_ids':['a'*64,'a'*64]}):
            self.assert_http(422, lambda:self.post_json('/api/agent',body))
        self.assertEqual(self.get_json('/api/finance/dialogue/history')['items'], [])

    def test_wrong_owner_persona_and_revoked_session_cannot_read(self):
        image = self.upload(image_bytes(), 'image/png')
        self.post_json('/api/agent',{'text':'','media_ids':[image['media_id']]})
        old_token, old_csrf = self.token, self.csrf
        self.login(uuid.uuid4().hex); self.assert_http(404,lambda:self.read(image))
        self.login(self.owner, 'grain_farmer'); self.assert_http(404,lambda:self.read(image))
        self.login(self.owner); self.assertEqual(self.read(image),image_bytes())
        self.server.finance.experience_provider.revoke_subject(self.owner,
            self.server.finance.experience_provider.validate(self.token)['auth_subject'])
        with self.assertRaises(urllib.error.HTTPError) as caught: self.read(image)
        self.assertIn(caught.exception.code,(401,409)); caught.exception.close()
        self.token,self.csrf=old_token,old_csrf;self.assertEqual(self.read(image),image_bytes())

    def test_same_database_and_key_restart_restore_original(self):
        image = self.upload(image_bytes(), 'image/png'); audio = self.upload(audio_bytes(), 'audio/wav')
        result=self.post_json('/api/agent', {'text':'','media_ids':[image['media_id'],audio['media_id']]})
        rid=result['bank_response_id'];workspace=self.workspace
        self.stop_server();self.start_server();self.login(self.owner)
        self.assertEqual(self.workspace,workspace)
        page=self.get_json('/api/finance/dialogue/history')
        self.assertEqual(page['items'][0]['request_id'],rid)
        self.assertEqual(self.read(image),image_bytes());self.assertEqual(self.read(audio),audio_bytes())

    def test_voice_preview_keeps_original_non_chat_purpose(self):
        ticket=self.post_json('/api/speech/session',{'kind':'tts','purpose':'voice_preview'})
        ws=create_connection(self.base.replace('http:', 'ws:')+ticket['path'],origin=self.base,
            cookie='qz_bank_user='+self.token,timeout=10,http_proxy_host=None)
        frames=bytearray();done=None
        try:
            ws.send(json.dumps({'type':'start'}))
            for _ in range(30):
                event=ws.recv()
                if isinstance(event,bytes):frames.extend(event);continue
                event=json.loads(event)
                self.assertNotEqual(event['type'],'error',event)
                if event['type']=='done':done=event;break
        finally:ws.close()
        self.assertEqual(bytes(frames),PCM);self.assertEqual(done,{'type':'done'})
        self.assertEqual(self.get_json('/api/finance/dialogue/history')['items'],[])

    def test_legacy_private_turn_migration_binds_without_rewriting_original(self):
        reply=self.post_json('/api/agent',{'text':'你好'})
        rid=reply['bank_response_id']
        with closing(self.server.finance.personal_data.connect()) as db:
            before=db.execute('SELECT user_text_cipher FROM agent_user_turn_archive WHERE workspace_id=? AND request_id=?',
                (self.workspace,rid)).fetchone()[0]
        image=self.upload(image_bytes(),'image/png')
        context=self.get_json('/api/finance/chat-media/context')
        headers={'Content-Type':'application/json','X-Navren-Expected-Workspace':self.workspace,
            'X-Navren-Privacy-Revision':str(context['privacy_revision']),
            'X-Navren-Archive-Epoch':str(context['archive_epoch'])}
        body=json.dumps({'request_id':rid,'media_ids':[image['media_id']]}).encode()
        self.assert_http(409,lambda:self.request('/api/finance/chat-media/bind',body,
            headers={**headers,'X-Navren-Expected-Workspace':'other'}))
        with self.request('/api/finance/chat-media/bind',body,headers=headers) as response:
            self.assertEqual(json.load(response)['contract'],'navren.chat-media-bind/1')
        with closing(self.server.finance.personal_data.connect()) as db:
            after=db.execute('SELECT user_text_cipher FROM agent_user_turn_archive WHERE workspace_id=? AND request_id=?',
                (self.workspace,rid)).fetchone()[0]
        self.assertEqual(before,after)
        original=self.get_json('/api/finance/dialogue/history')['items'][0]
        self.assertEqual(original['user']['content'],'你好')
        self.assertEqual(original['user']['attachments'][0]['media_id'],image['media_id'])
        self.assertEqual(original['assistant']['status'],'published_local_rule')
        self.assertEqual(self.read(image),image_bytes())

    def test_bad_media_list_rolls_back_user_archive_and_all_bindings(self):
        image=self.upload(image_bytes(),'image/png')
        self.assert_http(404,lambda:self.post_json('/api/agent',{'text':'','media_ids':[image['media_id'],'0'*64]}))
        self.assertEqual(self.get_json('/api/finance/dialogue/history')['items'],[])
        self.assert_http(404,lambda:self.read(image))

    def test_failed_publication_revokes_fixed_reply_speech_receipt(self):
        from server import PublicationInterrupted
        original=ProxyHandler._finance_json
        def interrupt(handler,status,data,cookie=None):
            if status==200 and isinstance(data,dict) and data.get('social_reply'):
                raise PublicationInterrupted('fixture_send_failed')
            return original(handler,status,data,cookie)
        with patch.object(ProxyHandler,'_finance_json',interrupt):
            with self.assertRaises(Exception):self.post_json('/api/agent',{'text':'你好'})
        self.assertEqual(self.server.finance._speech_receipts,{})
        page=self.get_json('/api/finance/dialogue/history')
        self.assertEqual(page['items'][0]['assistant']['status'],'archive_unavailable')

    def test_partial_binary_expiry_closes_without_json_append(self):
        from http.client import IncompleteRead
        from simulated_bank import BankError
        prefix=b'\x89PNG-original-prefix'
        def interrupted(handler,media_id):
            handler._navren_chat_media_response_started=True
            handler.send_response(200)
            handler.send_header('Content-Type','image/png')
            handler.send_header('Content-Length',str(len(prefix)+100))
            handler.end_headers();handler.wfile.write(prefix)
            raise BankError('SESSION_CHANGED','会话已变化。',409)
        with patch.object(self.server.finance.chat_media,'read_http',interrupted):
            with self.request('/api/finance/chat-media/'+'a'*64) as response:
                self.assertEqual(response.status,200)
                with self.assertRaises(IncompleteRead) as caught:response.read()
                self.assertEqual(caught.exception.partial,prefix)

    def test_disguised_and_oversized_upload_rejected(self):
        self.assert_http(415,lambda:self.upload(b'<svg onload="alert(1)"></svg>','image/png'))
        context=self.get_json('/api/finance/chat-media/context')
        self.assert_http(413,lambda:self.request('/api/finance/chat-media',b'',headers={
            'Content-Type':'image/png','Content-Length':str(10*1024*1024+1)}))
        self.assertEqual(self.get_json('/api/finance/dialogue/history')['items'],[])


if __name__ == '__main__': unittest.main()
