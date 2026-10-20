"""真实注册实例/私库的媒体验证；不发短信、不调用模型，不代表实屏恢复已过。"""
from contextlib import closing,contextmanager
from email.message import Message
import hashlib
import io
import json
import math
from pathlib import Path
import struct
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import wave

from PIL import Image,PngImagePlugin
from base_runtime.chat_media import ChatMediaService,AssistantPermit,inspect_bytes,MAX_IMAGE
from simulated_bank import BankError
import test_registered_instances as registered_fixture
from base_runtime.service_assembly import AssembledGateway
from user_memory import privacy_revision


def image_bytes(color='red'):
    out=io.BytesIO();info=PngImagePlugin.PngInfo();info.add_text('original','private-byte-sentinel')
    Image.new('RGB',(16,12),color).save(out,format='PNG',pnginfo=info)
    return out.getvalue()

def audio_bytes(frames=12000):
    out=io.BytesIO()
    with wave.open(out,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000)
        audio.writeframes(b''.join(struct.pack('<h',int(4000*math.sin(i*2*math.pi*440/24000))) for i in range(frames)))
    return out.getvalue()


class ChatMediaTests(unittest.TestCase):
    def setUp(self):
        self.fixture=registered_fixture.RegisteredInstancesTests()
        self.fixture.gateway=lambda registered=True:AssembledGateway(self.fixture.path,financial_database=registered_fixture.FINANCIAL,registered_mode=registered)
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.g=self.fixture.g;self.media=ChatMediaService(self.g);self.first=self.fixture.select()
        self.w=self.first['workspace_id'];self.token=self.fixture.token(self.first)
        self.image=image_bytes();self.audio=audio_bytes()

    @contextmanager
    def scope(self,lease=None):
        lease=lease or self.first
        with self.g.demo_sessions.request(self.fixture.token(lease),lease['workspace_id']),self.g.bank.transaction() as db:
            yield db

    def deny(self,code,fn,status=None):
        with self.assertRaises(BankError) as error:fn()
        if code:self.assertEqual(error.exception.code,code)
        if status:self.assertEqual(error.exception.status,status)

    def upload(self,raw=None,mime='image/png',upload_key=None):
        with self.scope() as db:
            return self.media.put_user(self.w,self.image if raw is None else raw,mime,upload_key or uuid.uuid4().hex,
                bank_db=db,expected_privacy=privacy_revision(db,self.w),expected_epoch=self.g.personal_data.archive_epoch(self.w))['media']

    def archive(self,ids,text='',rid=None):
        rid=rid or uuid.uuid4().hex
        with self.scope() as db:
            result=self.g.personal_data.archive_user_turn_guarded(self.w,rid,text,self.fixture.owner,bank_db=db,
                expected_revision=self.g.personal_data.revision(self.w),expected_privacy=privacy_revision(db,self.w),media_ids=ids)
        return rid,result

    def read(self,media_id,lease=None):
        lease=lease or self.first
        with self.scope(lease) as db:return self.media.read_blob(lease['workspace_id'],media_id,bank_db=db)

    def test_original_image_and_audio_media_only_atomic_archive_encrypted_and_restart(self):
        picture=self.upload();audio=self.upload(self.audio,'audio/wav')
        self.deny('CHAT_MEDIA_NOT_FOUND',lambda:self.read(picture['media_id']),404)
        rid,result=self.archive([picture['media_id'],audio['media_id']])
        self.assertEqual(len(result['attachments']),2)
        for descriptor,raw in zip(result['attachments'],(self.image,self.audio)):
            content,metadata=self.read(descriptor['media_id'])
            self.assertEqual(content,raw);self.assertEqual(metadata['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual(metadata['storage_status'],'bound')
        with closing(self.g.personal_data.connect()) as db:
            rows=list(db.execute('SELECT payload_cipher,metadata_cipher FROM chat_media_objects'))
            self.assertTrue(all(bytes(r[0]).startswith(b'pm1:') and r[1].startswith('pd1:') for r in rows))
            self.assertTrue(all(b'private-byte-sentinel' not in bytes(r[0]) for r in rows))
        # 同一数据及密钥重新构造网关，模拟缓存被清除后的账号恢复。
        fresh=self.fixture.gateway();fresh.bank.clock=lambda:self.fixture.now[0];service=ChatMediaService(fresh)
        resumed=fresh.experience_provider.resume(self.fixture.owner,'welcome-'+uuid.uuid4().hex,auth_expires_at=self.fixture.expiry)
        with fresh.demo_sessions.request(self.fixture.token(resumed),self.w),fresh.bank.transaction() as db:
            restored,metadata=service.read_blob(self.w,audio['media_id'],bank_db=db)
        self.assertEqual(restored,self.audio);self.assertEqual(metadata['duration_ms'],500)

    def test_cross_owner_persona_unknown_and_revoked_never_read(self):
        item=self.upload();self.archive([item['media_id']])
        persona=self.fixture.select('grain_farmer',subject='welcome-'+uuid.uuid4().hex)
        other=self.fixture.select(owner=uuid.uuid4().hex,subject='welcome-'+uuid.uuid4().hex)
        for lease in (persona,other):self.deny('CHAT_MEDIA_NOT_FOUND',lambda lease=lease:self.read(item['media_id'],lease),404)
        self.deny('CHAT_MEDIA_NOT_FOUND',lambda:self.read('0'*64),404)
        self.g.experience_provider.revoke_subject(self.fixture.owner,self.fixture.subject)
        self.deny('SESSION_CHANGED',lambda:self.read(item['media_id']))

    def test_idempotent_upload_and_disguised_or_oversized_rejected(self):
        nonce=uuid.uuid4().hex;first=self.upload(upload_key=nonce);second=self.upload(upload_key=nonce)
        self.assertEqual(first,second)
        self.deny('CHAT_MEDIA_UPLOAD_KEY_CONFLICT',lambda:self.upload(image_bytes('blue'),upload_key=nonce),409)
        self.deny('CHAT_MEDIA_TYPE_INVALID',lambda:self.upload(self.image,'image/jpeg'),415)
        self.deny('CHAT_MEDIA_TYPE_INVALID',lambda:self.upload(b'not an audio file','audio/webm'),415)
        self.deny('CHAT_MEDIA_TOO_LARGE',lambda:inspect_bytes(b'x'*(MAX_IMAGE+1),'image/png'),413)
        self.deny('CHAT_MEDIA_TYPE_INVALID',lambda:inspect_bytes(b'\0\0'*24000,'audio/pcm'),415)
        self.deny('CHAT_MEDIA_IDS_INVALID',lambda:self.archive([{}]),422)

    def test_profile_add_retains_original_but_explicit_history_delete_revokes_all(self):
        item=self.upload();self.archive([item['media_id']])
        with self.g.demo_sessions.request(self.token,self.w):
            self.g.memories.create(self.w,{'category':'goal','value':'毕业前攒一笔学费'})
        self.assertEqual(self.read(item['media_id'])[0],self.image)
        with self.g.demo_sessions.request(self.token,self.w):self.g.personal_data.delete_history(self.w,expected_revision=self.g.personal_data.revision(self.w))
        self.deny('CHAT_MEDIA_NOT_FOUND',lambda:self.read(item['media_id']),404)
        with closing(self.g.personal_data.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM chat_media_objects WHERE workspace_id=?',(self.w,)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM chat_media_bindings WHERE workspace_id=?',(self.w,)).fetchone()[0],0)

    def test_archive_failure_rolls_back_binding_but_preserves_uploaded_user_original(self):
        item=self.upload();rid=uuid.uuid4().hex
        with patch.object(self.g.personal_data,'protect',side_effect=RuntimeError('archive_commit_fixture')):
            with self.assertRaisesRegex(RuntimeError,'archive_commit_fixture'):self.archive([item['media_id']],rid=rid)
        with closing(self.g.personal_data.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM chat_media_bindings').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM chat_media_objects').fetchone()[0],1)
        self.archive([item['media_id']],rid=rid)
        self.assertEqual(self.read(item['media_id'])[0],self.image)
        # 助手失败/未发布并不撤销已经受理的用户原件。
        with closing(self.g.personal_data.connect()) as db:self.assertIsNone(db.execute('SELECT 1 FROM guarded_turn_publications WHERE response_id=?',(rid,)).fetchone())
        self.assertEqual(self.read(item['media_id'])[0],self.image)

    def test_cipher_corruption_expired_pending_and_fake_descriptor_fail_closed(self):
        item=self.upload();self.archive([item['media_id']])
        with closing(self.g.personal_data.connect()) as db,db:db.execute('UPDATE chat_media_objects SET payload_cipher=? WHERE media_id=?',(b'pm1:broken',item['media_id']))
        self.deny('CHAT_MEDIA_CORRUPT',lambda:self.read(item['media_id']),503)
        pending=self.upload()
        with closing(self.g.personal_data.connect()) as db,db:db.execute('UPDATE chat_media_objects SET expires_at=0 WHERE media_id=?',(pending['media_id'],))
        self.deny('CHAT_MEDIA_NOT_FOUND',lambda:self.archive([pending['media_id']]),404)
        with self.scope() as bank:
            self.deny('CHAT_MEDIA_IDS_REQUIRED',lambda:self.g.personal_data.archive_user_turn_guarded(self.w,uuid.uuid4().hex,'伪造附件',self.fixture.owner,
                bank_db=bank,expected_revision=0,expected_privacy=privacy_revision(bank,self.w),attachments=[{**item,'storage_status':'bound'}]),422)

    def test_existing_owner_browser_migration_does_not_reseal_old_user_packet(self):
        rid=uuid.uuid4().hex
        with self.scope() as bank:
            self.g.personal_data.archive_user_turn_guarded(self.w,rid,'之前的图片',self.fixture.owner,bank_db=bank,
                expected_revision=0,expected_privacy=privacy_revision(bank,self.w),attachments=[{'kind':'image','storage_status':'not_archived'}])
        with closing(self.g.personal_data.connect()) as db:before=db.execute('SELECT user_text_cipher FROM agent_user_turn_archive WHERE request_id=?',(rid,)).fetchone()[0]
        item=self.upload()
        with self.scope() as bank:
            result=self.media.bind_existing_user(self.w,rid,[item['media_id']],bank_db=bank,expected_privacy=privacy_revision(bank,self.w),expected_epoch=0)
        self.assertEqual(result['attachments'][0]['storage_status'],'bound')
        self.assertEqual(self.read(item['media_id'])[0],self.image)
        with closing(self.g.personal_data.connect()) as db:
            self.assertEqual(db.execute('SELECT user_text_cipher FROM agent_user_turn_archive WHERE request_id=?',(rid,)).fetchone()[0],before)

    def test_assistant_role_requires_real_active_guard_and_opaque_permit(self):
        self.deny('CHAT_MEDIA_SPEECH_GUARD_REQUIRED',lambda:self.media.prepare_assistant_audio(self.w,self.audio,'audio/wav',
            speech_receipt='made-up',session_token=self.token),403)
        self.deny('CHAT_MEDIA_ASSISTANT_PERMIT_INVALID',lambda:self.media.commit_assistant_audio({'role':'assistant'}),403)
        with patch.object(self.g,'assert_publication_current',return_value=True):
            self.deny('CHAT_MEDIA_SPEECH_GUARD_REQUIRED',lambda:self.media.prepare_assistant_audio(self.w,self.audio,'audio/wav',
                speech_receipt='ordinary-publication-is-not-speech',session_token=self.token),403)
        user_audio=self.upload(self.audio,'audio/wav');rid,_=self.archive([user_audio['media_id']])
        with self.scope() as db:
            self.assertEqual(self.media.descriptors_for_turn(self.w,rid,'assistant',bank_db=db),[])

    def test_binary_upload_http_preconditions_and_full_original_read(self):
        responses=[];headers=Message()
        for name,value in {'Host':'127.0.0.1:8765','Cookie':'qz_bank_user='+self.token,'X-QZ-CSRF':self.g.csrf(self.token),
            'Content-Type':'image/png','Content-Length':str(len(self.image)),'X-Navren-Expected-Workspace':self.w,
            'X-Navren-Privacy-Revision':'0','X-Navren-Archive-Epoch':'0','X-Navren-Upload-Key':uuid.uuid4().hex}.items():headers[name]=value
        handler=SimpleNamespace(path='/api/finance/chat-media',headers=headers,server=SimpleNamespace(server_port=8765),
            client_address=('127.0.0.1',0),rfile=io.BytesIO(self.image),_finance_json=lambda *args:responses.append(args))
        self.media.upload_http(handler)
        self.assertEqual(responses[0][0],201);item=responses[0][1]['media'];self.archive([item['media_id']])
        captured=[];handler.path+='/'+item['media_id'];handler.wfile=io.BytesIO()
        handler.send_response=lambda status:captured.append(('status',status));handler.send_header=lambda *args:captured.append(args);handler.end_headers=lambda:None
        self.media.read_http(handler,item['media_id'])
        self.assertEqual(handler.wfile.getvalue(),self.image)
        self.assertIn(('X-Content-Type-Options','nosniff'),captured)
        self.assertIn(('Cache-Control','no-store'),captured)
        headers.replace_header('X-Navren-Expected-Workspace','0'*32);handler.path='/api/finance/chat-media';handler.rfile=io.BytesIO(self.image)
        self.deny('WORKSPACE_CHANGED',lambda:self.media.upload_http(handler),409)

    def test_read_rechecks_after_chunk_and_does_not_hold_bank_write_lock(self):
        raw=audio_bytes(48000);item=self.upload(raw,'audio/wav');self.archive([item['media_id']])
        headers=Message();headers['Host']='127.0.0.1:8765';headers['Cookie']='qz_bank_user='+self.token
        output=io.BytesIO();timeouts=[]
        class Sink:
            def write(_,chunk):
                output.write(chunk)
                # 若网络阶段仍持银行BEGIN IMMEDIATE，这个真实撤销会在另连接卡住。
                if output.tell()==65536:self.g.experience_provider.revoke_subject(self.fixture.owner,self.fixture.subject)
        connection=SimpleNamespace(gettimeout=lambda:None,settimeout=lambda value:timeouts.append(value),shutdown=lambda *args:None)
        handler=SimpleNamespace(path='/api/finance/chat-media/'+item['media_id'],headers=headers,server=SimpleNamespace(server_port=8765),
            client_address=('127.0.0.1',0),wfile=Sink(),connection=connection,
            send_response=lambda *args:None,send_header=lambda *args:None,end_headers=lambda:None)
        self.deny('SESSION_CHANGED',lambda:self.media.read_http(handler,item['media_id']))
        self.assertEqual(len(output.getvalue()),65536)
        self.assertTrue(handler._navren_chat_media_response_started)
        self.assertTrue(handler.close_connection);self.assertIn(2.0,timeouts)


class CodecTests(unittest.TestCase):
    def test_root_minimal_fake_mp4_without_real_samples_is_rejected(self):
        def box(tag,body):return (8+len(body)).to_bytes(4,'big')+tag+body
        hdlr=box(b'hdlr',b'\0'*8+b'soun'+b'\0'*12)
        mdhd=box(b'mdhd',b'\0'*12+(48000).to_bytes(4,'big')+(480000).to_bytes(4,'big')+b'\0'*4)
        raw=box(b'ftyp',b'M4A '+b'\0'*4+b'M4A ')+box(b'mdat',b'this is not audio')+box(b'moov',box(b'trak',box(b'mdia',mdhd+hdlr)))
        self.assertEqual(len(raw),133)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'8803fdb703c424af96849bcab9e34fd07cdcd3854f2202bd111810e862788523')
        with self.assertRaises(BankError) as error:inspect_bytes(raw,'audio/mp4')
        self.assertEqual(error.exception.status,415)

    def test_encoded_real_audio_has_decodable_samples_and_original_hash(self):
        binary=shutil.which('ffmpeg')
        if binary is None:self.skipTest('本机缺少实际样本解码依赖')
        with tempfile.TemporaryDirectory(prefix='media-codec-fixture-') as folder:
            for name,mime,codec in (('sample.m4a','audio/mp4','aac'),('sample.webm','audio/webm','libopus'),
                                    ('sample.ogg','audio/ogg','libopus'),('sample.mp3','audio/mpeg','libmp3lame')):
                with self.subTest(mime=mime):
                    file=Path(folder)/name
                    process=subprocess.run([binary,'-nostdin','-v','error','-f','wav','-i','pipe:0','-c:a',codec,str(file)],
                        input=audio_bytes(),capture_output=True,timeout=10)
                    self.assertEqual(process.returncode,0,process.stderr.decode(errors='replace'))
                    raw=file.read_bytes();metadata=inspect_bytes(raw,mime)
                    self.assertGreater(metadata['duration_ms'],0)
                    self.assertEqual(metadata['sha256'],hashlib.sha256(raw).hexdigest())


if __name__=='__main__':unittest.main(verbosity=2)
