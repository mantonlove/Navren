"""注册聊天媒体原件。原始bytes只加密、不转码；公开上传永远属于用户。"""
from __future__ import annotations
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import io
import json
import math
import re
import secrets
import struct
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from urllib.parse import urlsplit

from simulated_bank import BankError

CONTRACT='navren.chat-media/1'
MAX_IMAGE=10*1024*1024
MAX_AUDIO=25*1024*1024
MAX_PIXELS=40_000_000
MAX_DURATION=600_000
MAX_PER_TURN=8
MAX_PENDING=32
MAX_PENDING_BYTES=100*1024*1024
MAX_TOTAL_BYTES=512*1024*1024
PENDING_TTL=86400
MAX_SEND_SECONDS=120
MIMES=('image/png','image/jpeg','image/webp','image/gif','audio/wav','audio/webm','audio/ogg','audio/mpeg','audio/mp4')
DESCRIPTOR_FIELDS={'contract','media_id','kind','mime_type','bytes','sha256','duration_ms','audio_format',
                   'storage_status','content_url','historical_readonly','execution_authority'}
MEDIA_SCHEMA='''
CREATE TABLE IF NOT EXISTS chat_media_objects(
 media_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,registered_user_id TEXT NOT NULL,
 archive_epoch INTEGER NOT NULL CHECK(archive_epoch>=0),media_role TEXT NOT NULL CHECK(media_role IN ('user','assistant')),
 byte_count INTEGER NOT NULL CHECK(byte_count>0),upload_key TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL,
 metadata_cipher TEXT NOT NULL,payload_cipher BLOB NOT NULL,
 UNIQUE(workspace_id,archive_epoch,media_role,upload_key),CHECK(substr(metadata_cipher,1,4)='pd1:'));
CREATE TABLE IF NOT EXISTS chat_media_bindings(
 media_id TEXT PRIMARY KEY REFERENCES chat_media_objects(media_id) ON DELETE CASCADE,
 workspace_id TEXT NOT NULL,request_id TEXT NOT NULL,archive_epoch INTEGER NOT NULL,
 media_role TEXT NOT NULL CHECK(media_role IN ('user','assistant')),ordinal INTEGER NOT NULL CHECK(ordinal>=0),
 origin TEXT NOT NULL CHECK(origin IN ('user_upload','owner_browser_migration','assistant_tts')),bound_at REAL NOT NULL,
 UNIQUE(workspace_id,request_id,media_role,ordinal),
 FOREIGN KEY(workspace_id,request_id) REFERENCES agent_user_turn_archive(workspace_id,request_id)
 ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED);
CREATE INDEX IF NOT EXISTS chat_media_workspace ON chat_media_objects(workspace_id,archive_epoch);
CREATE TABLE IF NOT EXISTS chat_media_object_versions(
 media_id TEXT PRIMARY KEY REFERENCES chat_media_objects(media_id) ON DELETE CASCADE,revision INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO chat_media_object_versions(media_id) SELECT media_id FROM chat_media_objects;
CREATE TRIGGER IF NOT EXISTS chat_media_version_insert AFTER INSERT ON chat_media_objects
 BEGIN INSERT OR IGNORE INTO chat_media_object_versions(media_id) VALUES(NEW.media_id); END;
CREATE TRIGGER IF NOT EXISTS chat_media_version_update AFTER UPDATE ON chat_media_objects
 BEGIN UPDATE chat_media_object_versions SET revision=revision+1 WHERE media_id=NEW.media_id; END;
'''


def fail(code='CHAT_MEDIA_NOT_FOUND',status=404):
    messages={'CHAT_MEDIA_NOT_FOUND':'媒体不存在、尚未绑定或无权读取。','CHAT_MEDIA_CORRUPT':'媒体原件无法完整核验，请保留问题记录。',
              'CHAT_MEDIA_TYPE_INVALID':'文件真实类型不受支持或与声明不符。','CHAT_MEDIA_TOO_LARGE':'媒体超过允许的大小或时长。',
              'CHAT_MEDIA_QUOTA':'媒体存储或上传并发已到上限。','CHAT_MEDIA_STALE':'聊天授权或归档代次已变化，请重新读取。'}
    raise BankError(code,messages.get(code,'聊天媒体校验未通过。'),status)

def packed(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def ident(value):return type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None
def key(value):return type(value) is str and re.fullmatch('[0-9a-f]{32}',value) is not None

def validate_descriptor(value):
    if (type(value) is not dict or set(value)!=DESCRIPTOR_FIELDS or value['contract']!=CONTRACT
            or not ident(value['media_id']) or not ident(value['sha256']) or value['kind'] not in ('image','audio')
            or value['mime_type'] not in (*MIMES,'audio/pcm') or type(value['bytes']) is not int
            or (value['kind']=='image')!=value['mime_type'].startswith('image/')
            or not 0<value['bytes']<=(MAX_IMAGE if value['kind']=='image' else MAX_AUDIO)
            or value['storage_status'] not in ('stored','bound')
            or value['content_url']!='/api/finance/chat-media/'+value['media_id']
            or value['historical_readonly'] is not True or value['execution_authority'] is not False):
        fail('CHAT_MEDIA_DESCRIPTOR_INVALID',422)
    duration=value['duration_ms'];fmt=value['audio_format']
    if duration is not None and (type(duration) is not int or not 0<duration<=MAX_DURATION):fail('CHAT_MEDIA_DESCRIPTOR_INVALID',422)
    if fmt is not None and (type(fmt) is not dict or set(fmt)!={'encoding','sample_rate','channels'}
            or fmt['encoding']!='pcm_s16le' or type(fmt['sample_rate']) is not int
            or fmt['sample_rate'] not in (8000,16000,22050,24000,44100,48000)
            or type(fmt['channels']) is not int or fmt['channels'] not in (1,2)):
        fail('CHAT_MEDIA_DESCRIPTOR_INVALID',422)
    if (value['mime_type']=='audio/pcm')!=(fmt is not None):fail('CHAT_MEDIA_DESCRIPTOR_INVALID',422)
    return json.loads(packed(value))

def _ebml(data,start=0,end=None):
    end=len(data) if end is None else end;count=0
    while start<end:
        count+=1
        if count>20000:raise ValueError
        first=data[start];length=next((n for n in range(1,5) if first & (1<<(8-n))),None)
        if length is None or start+length>=end:raise ValueError
        tag=int.from_bytes(data[start:start+length],'big');start+=length;first=data[start]
        size_len=next((n for n in range(1,9) if first & (1<<(8-n))),None)
        if size_len is None or start+size_len>end:raise ValueError
        size=int.from_bytes(data[start:start+size_len],'big') & ((1<<(7*size_len))-1);start+=size_len
        stop=end if size==(1<<(7*size_len))-1 else start+size
        if stop>end:raise ValueError
        yield tag,start,stop
        start=stop

def _webm_duration(data):
    roots=list(_ebml(data))
    if not roots or roots[0][0]!=0x1A45DFA3:raise ValueError
    header={tag:data[a:b] for tag,a,b in _ebml(data,roots[0][1],roots[0][2])}
    if header.get(0x4282)!=b'webm':raise ValueError
    segments=[r for r in roots if r[0]==0x18538067]
    if len(segments)!=1:raise ValueError
    tracks=[];scale=1_000_000;duration=None;blocks=False;last_tick=0
    for tag,a,b in _ebml(data,segments[0][1],segments[0][2]):
        if tag==0x1549A966:
            for item,x,y in _ebml(data,a,b):
                if item==0x2AD7B1:scale=int.from_bytes(data[x:y],'big')
                elif item==0x4489:
                    if y-x not in (4,8):raise ValueError
                    duration=struct.unpack('>f' if y-x==4 else '>d',data[x:y])[0]
        elif tag==0x1654AE6B:
            for item,x,y in _ebml(data,a,b):
                if item==0xAE:
                    fields={k:data[s:e] for k,s,e in _ebml(data,x,y)}
                    if int.from_bytes(fields.get(0x83,b'\0'),'big')!=2 or fields.get(0x86) not in (b'A_OPUS',b'A_VORBIS',b'A_AAC'):raise ValueError
                    tracks.append(fields)
        elif tag==0x1F43B675:
            tick=0
            for item,x,y in _ebml(data,a,b):
                if item==0xE7:tick=int.from_bytes(data[x:y],'big')
                elif item==0xA3:
                    if y-x<5:raise ValueError
                    track_size=next((n for n in range(1,9) if data[x] & (1<<(8-n))),None)
                    if track_size is None or y-x<=track_size+3:raise ValueError
                    relative=int.from_bytes(data[x+track_size:x+track_size+2],'big',signed=True)
                    blocks=True;last_tick=max(last_tick,tick+relative)
    if not tracks or not blocks or not 0<scale<=1_000_000_000:raise ValueError
    if duration is not None:
        if not math.isfinite(duration) or duration<=0:raise ValueError
        return math.ceil(duration*scale/1_000_000)
    # MediaRecorder通常不写Duration；最后块时间可用于拒绝超长，不能当作精确时长。
    if last_tick*scale/1_000_000>MAX_DURATION:fail('CHAT_MEDIA_TOO_LARGE',413)
    return None

def _ogg_duration(data):
    at=0;serial=None;last=0;head=None;finished=False
    while at<len(data):
        if data[at:at+4]!=b'OggS' or at+27>len(data) or data[at+4]!=0:raise ValueError
        page_serial=int.from_bytes(data[at+14:at+18],'little')
        if serial is not None and serial!=page_serial:raise ValueError
        serial=page_serial;n=data[at+26];body=at+27+n
        if body>len(data):raise ValueError
        stop=body+sum(data[at+27:body])
        if stop>len(data):raise ValueError
        if head is None:head=data[body:stop]
        granule=int.from_bytes(data[at+6:at+14],'little')
        if granule!=(1<<64)-1:last=max(last,granule)
        finished=bool(data[at+5]&4);at=stop
    if not head or len(head)<19 or not head.startswith(b'OpusHead') or not finished or head[9] not in (1,2):raise ValueError
    duration=(last-int.from_bytes(head[10:12],'little'))/48
    if duration<=0:raise ValueError
    return math.ceil(duration)

def _mp3_duration(data):
    at=0;samples=0;seconds=0;frames=0
    if data.startswith(b'ID3'):
        if len(data)<10 or any(x&128 for x in data[6:10]):raise ValueError
        tag=sum(v<<(7*(3-i)) for i,v in enumerate(data[6:10]));at=10+tag+(10 if data[5]&16 else 0)
    while at+4<=len(data):
        if data[at:at+3]==b'TAG' and len(data)-at==128:at=len(data);break
        h=int.from_bytes(data[at:at+4],'big');version=(h>>19)&3;layer=(h>>17)&3
        if h>>21!=0x7ff or version==1 or layer!=1:raise ValueError
        bit=(h>>12)&15;rate=(h>>10)&3
        if bit in (0,15) or rate==3:raise ValueError
        bitrate=([0,32,40,48,56,64,80,96,112,128,160,192,224,256,320] if version==3 else [0,8,16,24,32,40,48,56,64,80,96,112,128,144,160])[bit]
        hz=(44100,48000,32000)[rate]//(1 if version==3 else 2 if version==2 else 4)
        length=(144000 if version==3 else 72000)*bitrate//hz+((h>>9)&1)
        if at+length>len(data):raise ValueError
        seconds+=(1152 if version==3 else 576)/hz;frames+=1;at+=length
        if seconds*1000>MAX_DURATION:fail('CHAT_MEDIA_TOO_LARGE',413)
    if at!=len(data) or not frames:raise ValueError
    return max(1,math.ceil(seconds*1000))

def _mp4_duration(data):
    def boxes(start,end):
        while start<end:
            if start+8>end:raise ValueError
            size=int.from_bytes(data[start:start+4],'big');tag=data[start+4:start+8];head=8
            if size==1:
                if start+16>end:raise ValueError
                size=int.from_bytes(data[start+8:start+16],'big');head=16
            if size==0:size=end-start
            if size<head or start+size>end:raise ValueError
            yield tag,start+head,start+size;start+=size
    roots=list(boxes(0,len(data)))
    if not roots or roots[0][0]!=b'ftyp' or not any(t==b'mdat' and b>a for t,a,b in roots):raise ValueError
    duration=[]
    for tag,a,b in roots:
        if tag!=b'moov':continue
        for child,x,y in boxes(a,b):
            if child!=b'trak':continue
            for sub,u,v in boxes(x,y):
                if sub!=b'mdia':continue
                fields={k:(s,e) for k,s,e in boxes(u,v)}
                if b'hdlr' not in fields or b'mdhd' not in fields:raise ValueError
                s,e=fields[b'hdlr']
                if data[s+8:s+12]!=b'soun':raise ValueError
                s,e=fields[b'mdhd'];offset=20 if data[s]==1 else 12 if data[s]==0 else None
                if offset is None or s+offset+12>e and data[s]==1 or s+offset+8>e:raise ValueError
                hz=int.from_bytes(data[s+offset:s+offset+4],'big');ticks=int.from_bytes(data[s+offset+4:s+offset+(12 if data[s]==1 else 8)],'big')
                if hz<=0 or ticks<=0:raise ValueError
                duration.append(math.ceil(ticks*1000/hz))
    if not duration:raise ValueError
    return max(duration)

def _decode_audio(raw,mime):
    """有界实际样本解码，输出丢弃；原始bytes不改写。临时校验文件权限0600。"""
    binary=shutil.which('ffmpeg')
    if not binary:fail('CHAT_MEDIA_CODEC_UNAVAILABLE',503)
    demux={'audio/webm':'matroska','audio/ogg':'ogg','audio/mpeg':'mp3','audio/mp4':'mov'}[mime]
    with tempfile.TemporaryDirectory(prefix='navren-media-verify-') as folder:
        file=Path(folder)/'original.bin';file.write_bytes(raw);file.chmod(0o600)
        command=[binary,'-nostdin','-hide_banner','-v','error','-xerror','-err_detect','explode',
            '-threads','1','-max_alloc','67108864','-protocol_whitelist','file,pipe','-f',demux]
        if demux=='mov':command+=['-enable_drefs','0','-use_absolute_path','0']
        command+=['-i',str(file),'-map','0:a:0','-vn','-sn','-dn','-af','asetpts=N/SR/TB',
            '-f','null','-','-progress','pipe:1','-nostats']
        try:result=subprocess.run(command,capture_output=True,timeout=10)
        except subprocess.TimeoutExpired:fail('CHAT_MEDIA_TYPE_INVALID',415)
        if result.returncode!=0:fail('CHAT_MEDIA_TYPE_INVALID',415)
        times=[int(x) for x in re.findall(rb'(?m)^out_time_us=(\d+)$',result.stdout)]
        if not times or max(times)<=0:fail('CHAT_MEDIA_TYPE_INVALID',415)
        duration=math.ceil(max(times)/1000)
        if not 0<duration<=MAX_DURATION:fail('CHAT_MEDIA_TOO_LARGE',413)
        return duration

def inspect_bytes(raw,mime_type,*,audio_format=None,server_pcm=False):
    if type(raw) is not bytes or not raw:fail('CHAT_MEDIA_TYPE_INVALID',415)
    mime=mime_type.split(';',1)[0].strip().lower() if type(mime_type) is str else ''
    mime={'audio/x-wav':'audio/wav','audio/wave':'audio/wav','audio/x-m4a':'audio/mp4'}.get(mime,mime)
    if mime not in MIMES and not (server_pcm and mime=='audio/pcm'):fail('CHAT_MEDIA_TYPE_INVALID',415)
    kind='image' if mime.startswith('image/') else 'audio'
    if len(raw)>(MAX_IMAGE if kind=='image' else MAX_AUDIO):fail('CHAT_MEDIA_TOO_LARGE',413)
    duration=None
    try:
        if kind=='image':
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as picture:
                if {'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp','GIF':'image/gif'}.get(picture.format)!=mime:raise ValueError
                if not 0<picture.width*picture.height<=MAX_PIXELS:fail('CHAT_MEDIA_TOO_LARGE',413)
                picture.verify()  # 只校验，不save、不转码、不去除原元数据。
            with Image.open(io.BytesIO(raw)) as picture:
                pixels=0
                for frame in range(getattr(picture,'n_frames',1)):
                    if frame>=256:fail('CHAT_MEDIA_TOO_LARGE',413)
                    picture.seek(frame);pixels+=picture.width*picture.height
                    if pixels>2*MAX_PIXELS:fail('CHAT_MEDIA_TOO_LARGE',413)
                    picture.load()  # 验证能真实解码；不保存这个解码对象。
        elif mime=='audio/wav':
            with wave.open(io.BytesIO(raw),'rb') as audio:
                if not 1<=audio.getnchannels()<=8 or not 1<=audio.getsampwidth()<=4 or not 0<audio.getframerate()<=192000:raise ValueError
                frames=audio.getnframes()
                if not frames or len(audio.readframes(frames))!=frames*audio.getnchannels()*audio.getsampwidth():raise ValueError
                duration=math.ceil(frames*1000/audio.getframerate())
        elif mime=='audio/webm':_webm_duration(raw);duration=_decode_audio(raw,mime)
        elif mime=='audio/ogg':_ogg_duration(raw);duration=_decode_audio(raw,mime)
        elif mime=='audio/mpeg':_mp3_duration(raw);duration=_decode_audio(raw,mime)
        elif mime=='audio/mp4':_mp4_duration(raw);duration=_decode_audio(raw,mime)
        else:
            if (type(audio_format) is not dict or set(audio_format)!={'encoding','sample_rate','channels'}
                    or audio_format['encoding']!='pcm_s16le' or type(audio_format['sample_rate']) is not int
                    or audio_format['sample_rate'] not in (8000,16000,22050,24000,44100,48000)
                    or type(audio_format['channels']) is not int or audio_format['channels'] not in (1,2)
                    or len(raw)%(2*audio_format['channels'])):raise ValueError
            duration=math.ceil(len(raw)*1000/(2*audio_format['channels']*audio_format['sample_rate']))
    except BankError:raise
    except Exception:fail('CHAT_MEDIA_TYPE_INVALID',415)
    if duration is not None and not 0<duration<=MAX_DURATION:fail('CHAT_MEDIA_TOO_LARGE',413)
    return {'kind':kind,'mime_type':mime,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
            'duration_ms':duration,'audio_format':audio_format if mime=='audio/pcm' else None}


@dataclass(frozen=True,repr=False)
class AssistantPermit:
    nonce:str
    workspace:str
    token:str
    speech_receipt:str
    request_id:str
    registered_user_id:str
    archive_epoch:int
    metadata:dict
    cipher:bytes
    expires:float


class ChatMediaService:
    def __init__(self,gateway):
        self.g,self.bank,self.store=gateway,gateway.bank,gateway.personal_data
        self._slots=threading.BoundedSemaphore(2);self._permit_lock=threading.Lock();self._permits={}
        self._assistant_guard=ContextVar('navren_chat_media_assistant_guard',default=None)
        with closing(self.store.connect()) as db:db.executescript(MEDIA_SCHEMA)
        self.store.chat_media=self

    def _scope(self,workspace,bank_db,private=None):
        if not self.store._guarded_bank_owner(workspace,bank_db):fail('CHAT_MEDIA_STALE',409)
        request=self.g.demo_sessions._request.get()
        if request is None or request[1]!=workspace:fail('CHAT_MEDIA_STALE',409)
        reader=getattr(getattr(self.g,'dialogue_archive',None),'binding_reader',None)
        if not callable(reader):reader=getattr(self.g,'current_registered_binding',None)
        if not callable(reader):fail('CHAT_MEDIA_BINDING_UNAVAILABLE',503)
        bound=reader(workspace,bank_db)
        if (type(bound) is not dict or set(bound)!={'registered_user_id','workspace_id','persona_id'}
                or bound['workspace_id']!=workspace):fail('CHAT_MEDIA_BINDING_UNAVAILABLE',503)
        from user_memory import privacy_revision
        def read(db):
            revision=db.execute('SELECT revision,pending_invalidation FROM personal_versions WHERE workspace_id=?',(workspace,)).fetchone()
            if revision and revision[1]:fail('CHAT_MEDIA_STALE',409)
            return {**bound,'privacy_revision':privacy_revision(bank_db,workspace),
                    'personal_revision':revision[0] if revision else 0,'archive_epoch':self.store.archive_epoch_from_db(db,workspace)}
        if private is not None:return read(private)
        with closing(self.store.connect()) as db:return read(db)

    def _envelope(self,raw,metadata,scope,role):
        header=packed({'contract':CONTRACT,**metadata,'registered_user_id':scope['registered_user_id'],
            'workspace_id':scope['workspace_id'],'archive_epoch':scope['archive_epoch'],'media_role':role}).encode()
        return b'pm1:'+self.store._cipher.encrypt(len(header).to_bytes(4,'big')+header+raw)

    def _open(self,row):
        try:
            cipher=bytes(row['payload_cipher'])
            if not cipher.startswith(b'pm1:'):raise ValueError
            body=self.store._cipher.decrypt(cipher[4:]);length=int.from_bytes(body[:4],'big')
            if not 0<length<=4096:raise ValueError
            header=json.loads(body[4:4+length]);raw=body[4+length:]
            metadata=json.loads(self.store._decode(row['metadata_cipher']))
            expected={'contract':CONTRACT,**metadata,'registered_user_id':row['registered_user_id'],
                'workspace_id':row['workspace_id'],'archive_epoch':row['archive_epoch'],'media_role':row['media_role']}
            if header!=expected or type(metadata['bytes']) is not int or len(raw)!=metadata['bytes'] or row['byte_count']!=len(raw) or hashlib.sha256(raw).hexdigest()!=metadata['sha256']:raise ValueError
            return raw,metadata
        except Exception:fail('CHAT_MEDIA_CORRUPT',503)

    @staticmethod
    def _descriptor(row,metadata,bound):
        result={'contract':CONTRACT,'media_id':row['media_id'],**metadata,'storage_status':'bound' if bound else 'stored',
                'content_url':'/api/finance/chat-media/'+row['media_id'],'historical_readonly':True,'execution_authority':False}
        return validate_descriptor(result)

    def _row(self,db,scope,media_id,*,bound_only=False,role=None):
        if not ident(media_id):fail()
        row=db.execute('SELECT * FROM chat_media_objects WHERE media_id=? AND workspace_id=? AND registered_user_id=? AND archive_epoch=?',
            (media_id,scope['workspace_id'],scope['registered_user_id'],scope['archive_epoch'])).fetchone()
        if row is None or role is not None and row['media_role']!=role:fail()
        binding=db.execute('SELECT * FROM chat_media_bindings WHERE media_id=?',(media_id,)).fetchone()
        if binding and (binding['workspace_id']!=scope['workspace_id'] or binding['archive_epoch']!=scope['archive_epoch'] or binding['media_role']!=row['media_role']):fail('CHAT_MEDIA_CORRUPT',503)
        if bound_only and not binding:fail()
        if not binding and row['expires_at'] is not None and row['expires_at']<=self.bank.clock():fail()
        return row,binding

    def _put(self,db,scope,raw,metadata,upload_key,*,role='user',cipher=None):
        workspace=scope['workspace_id'];now=float(self.bank.clock())
        if not math.isfinite(now) or not key(upload_key):fail('CHAT_MEDIA_UPLOAD_KEY_INVALID',422)
        db.execute('INSERT OR IGNORE INTO dialogue_archive_epochs(workspace_id,archive_epoch) VALUES(?,0)',(workspace,))
        db.execute('DELETE FROM chat_media_objects WHERE workspace_id=? AND expires_at<=? AND media_id NOT IN (SELECT media_id FROM chat_media_bindings)',(workspace,now))
        existing=db.execute('''SELECT * FROM chat_media_objects WHERE workspace_id=? AND archive_epoch=? AND media_role=? AND upload_key=?''',
            (workspace,scope['archive_epoch'],role,upload_key)).fetchone()
        if existing:
            _,previous=self._open(existing)
            if previous!=metadata:fail('CHAT_MEDIA_UPLOAD_KEY_CONFLICT',409)
            binding=db.execute('SELECT 1 FROM chat_media_bindings WHERE media_id=?',(existing['media_id'],)).fetchone()
            return self._descriptor(existing,metadata,bool(binding))
        quota=db.execute('''SELECT coalesce(sum(byte_count),0),coalesce(sum(CASE WHEN b.media_id IS NULL THEN byte_count ELSE 0 END),0),
            coalesce(sum(CASE WHEN b.media_id IS NULL THEN 1 ELSE 0 END),0) FROM chat_media_objects o
            LEFT JOIN chat_media_bindings b ON b.media_id=o.media_id WHERE o.workspace_id=?''',(workspace,)).fetchone()
        if quota[0]+len(raw)>MAX_TOTAL_BYTES or role=='user' and (quota[1]+len(raw)>MAX_PENDING_BYTES or quota[2]>=MAX_PENDING):fail('CHAT_MEDIA_QUOTA',429)
        media_id=secrets.token_hex(32)
        db.execute('INSERT INTO chat_media_objects VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (media_id,workspace,scope['registered_user_id'],scope['archive_epoch'],role,len(raw),upload_key,now,
             now+PENDING_TTL if role=='user' else None,self.store.protect(packed(metadata)),cipher or self._envelope(raw,metadata,scope,role)))
        row=db.execute('SELECT * FROM chat_media_objects WHERE media_id=?',(media_id,)).fetchone()
        return self._descriptor(row,metadata,False)

    def put_user(self,workspace,raw,mime_type,upload_key,*,bank_db,expected_privacy,expected_epoch):
        metadata=inspect_bytes(raw,mime_type)
        with closing(self.store.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                scope=self._scope(workspace,bank_db,db)
                if type(expected_privacy) is not int or type(expected_epoch) is not int or (scope['privacy_revision'],scope['archive_epoch'])!=(expected_privacy,expected_epoch):fail('CHAT_MEDIA_STALE',409)
                descriptor=self._put(db,scope,raw,metadata,upload_key)
                db.commit()
            except Exception:db.rollback();raise
        self.store._permissions()
        return {'contract':'navren.chat-media-upload/1','media':descriptor,'workspace_binding':workspace,
                'privacy_revision':scope['privacy_revision'],'archive_epoch':scope['archive_epoch']}

    def bind_user_in_db(self,db,workspace,request_id,media_ids,*,bank_db,registered_user_id,origin='user_upload'):
        if type(media_ids) is not list or len(media_ids)>MAX_PER_TURN or any(not ident(x) for x in media_ids) or len(set(media_ids))!=len(media_ids):fail('CHAT_MEDIA_IDS_INVALID',422)
        scope=self._scope(workspace,bank_db,db)
        if scope['registered_user_id']!=registered_user_id:fail('CHAT_MEDIA_STALE',409)
        existing=list(db.execute("SELECT media_id FROM chat_media_bindings WHERE workspace_id=? AND request_id=? AND media_role='user' ORDER BY ordinal",(workspace,request_id)))
        if existing and [r[0] for r in existing]!=media_ids:fail('CHAT_MEDIA_BINDING_CONFLICT',409)
        result=[]
        for ordinal,media_id in enumerate(media_ids):
            row,binding=self._row(db,scope,media_id,role='user');_,metadata=self._open(row)
            if binding and binding['request_id']!=request_id:fail('CHAT_MEDIA_BINDING_CONFLICT',409)
            if not binding:db.execute('INSERT INTO chat_media_bindings VALUES(?,?,?,?,?,?,?,?)',
                (media_id,workspace,request_id,scope['archive_epoch'],'user',ordinal,origin,float(self.bank.clock())))
            result.append(self._descriptor(row,metadata,True))
        return result

    def bind_existing_user(self,workspace,request_id,media_ids,*,bank_db,expected_privacy,expected_epoch):
        """当前用户主动迁移旧浏览器原件；不重封或覆盖原用户归档包。"""
        with closing(self.store.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                scope=self._scope(workspace,bank_db,db)
                if (type(expected_privacy) is not int or type(expected_epoch) is not int
                        or (scope['privacy_revision'],scope['archive_epoch'])!=(expected_privacy,expected_epoch)):
                    fail('CHAT_MEDIA_STALE',409)
                parent=db.execute('SELECT * FROM agent_user_turn_archive WHERE workspace_id=? AND request_id=?',(workspace,request_id)).fetchone()
                if not parent or parent['archive_epoch']!=scope['archive_epoch']:fail()
                try:
                    packet=json.loads(self.store._decode(parent['user_text_cipher']))
                    if (packet['workspace_id'],packet['registered_user_id'],packet['request_id'],packet['archive_epoch'])!=(workspace,scope['registered_user_id'],request_id,scope['archive_epoch']):fail()
                except (KeyError,TypeError,ValueError):fail('CHAT_MEDIA_CORRUPT',503)
                descriptors=self.bind_user_in_db(db,workspace,request_id,media_ids,bank_db=bank_db,
                    registered_user_id=scope['registered_user_id'],origin='owner_browser_migration')
                db.commit()
            except Exception:db.rollback();raise
        return {'contract':'navren.chat-media-bind/1','request_id':request_id,'attachments':descriptors,
                'workspace_binding':workspace,'archive_epoch':scope['archive_epoch']}

    def descriptors_for_turn(self,workspace,request_id,role,*,bank_db):
        if role not in ('user','assistant'):fail('CHAT_MEDIA_ROLE_INVALID',422)
        with closing(self.store.connect()) as db:
            db.execute('BEGIN');scope=self._scope(workspace,bank_db,db);result=[]
            for binding in db.execute('SELECT * FROM chat_media_bindings WHERE workspace_id=? AND request_id=? AND media_role=? ORDER BY ordinal',(workspace,request_id,role)):
                row,_=self._row(db,scope,binding['media_id'],bound_only=True,role=role);_,metadata=self._open(row)
                result.append(self._descriptor(row,metadata,True))
            return result

    def read_blob(self,workspace,media_id,*,bank_db):
        with closing(self.store.connect()) as db:
            db.execute('BEGIN');scope=self._scope(workspace,bank_db,db)
            row,binding=self._row(db,scope,media_id,bound_only=True)
            parent=db.execute('SELECT archive_epoch FROM agent_user_turn_archive WHERE workspace_id=? AND request_id=?',(workspace,binding['request_id'])).fetchone()
            if parent is None or parent[0]!=scope['archive_epoch']:fail()
            raw,metadata=self._open(row)
            return raw,self._descriptor(row,metadata,True)

    def _read_fingerprint(self,db,scope,media_id):
        row=db.execute('''SELECT o.media_id,o.workspace_id,o.registered_user_id,o.archive_epoch,o.media_role,
            o.byte_count,o.metadata_cipher,v.revision,b.request_id,b.media_role AS binding_role,b.origin,b.ordinal,
            b.archive_epoch AS binding_epoch FROM chat_media_objects o JOIN chat_media_object_versions v ON v.media_id=o.media_id
            JOIN chat_media_bindings b ON b.media_id=o.media_id WHERE o.media_id=? AND o.workspace_id=? AND o.registered_user_id=?''',
            (media_id,scope['workspace_id'],scope['registered_user_id'])).fetchone()
        if row is None or row['archive_epoch']!=scope['archive_epoch'] or row['binding_epoch']!=scope['archive_epoch']:fail()
        parent=db.execute('SELECT archive_epoch FROM agent_user_turn_archive WHERE workspace_id=? AND request_id=?',
            (scope['workspace_id'],row['request_id'])).fetchone()
        if parent is None or parent[0]!=scope['archive_epoch']:fail()
        return hashlib.sha256(packed(dict(row)).encode()).hexdigest()

    @contextmanager
    def assistant_speech_guard(self,workspace,speech_receipt,session_token):
        """原speech_guard的窄包装，证明本次采集确实属于同一语音授权。"""
        with self.g.speech_guard(workspace,speech_receipt,session_token) as approved_text:
            token=self._assistant_guard.set((workspace,speech_receipt,session_token))
            try:yield approved_text
            finally:self._assistant_guard.reset(token)

    def prepare_assistant_audio(self,workspace,raw_bytes,mime_type,*,speech_receipt,session_token,audio_format=None):
        if self._assistant_guard.get()!=(workspace,speech_receipt,session_token) or not self.g.assert_publication_current():fail('CHAT_MEDIA_SPEECH_GUARD_REQUIRED',403)
        speech=self.g.assert_speech_receipt(workspace,speech_receipt,session_token)
        request_id=speech.get('request_id')
        if type(request_id) is not str or not 1<=len(request_id)<=160:fail('CHAT_MEDIA_ASSISTANT_BINDING_REQUIRED',409)
        metadata=inspect_bytes(raw_bytes,mime_type,audio_format=audio_format,server_pcm=True)
        if metadata['kind']!='audio':fail('CHAT_MEDIA_TYPE_INVALID',415)
        with self.g.demo_sessions.request(session_token,workspace),closing(self.bank.connect()) as db:
            db.execute('BEGIN');scope=self._scope(workspace,db)
            with closing(self.store.connect()) as private:self._assistant_parent(private,scope,request_id)
        self.g.assert_publication_current()
        with self._permit_lock:
            self._permits={k:v for k,v in self._permits.items() if v.expires>time.monotonic()}
            if len(self._permits)>=4:fail('CHAT_MEDIA_QUOTA',429)
            permit=AssistantPermit(secrets.token_hex(32),workspace,session_token,speech_receipt,request_id,scope['registered_user_id'],scope['archive_epoch'],
                metadata,self._envelope(raw_bytes,metadata,scope,'assistant'),time.monotonic()+30)
            self._permits[permit.nonce]=permit
        return permit

    @staticmethod
    def _assistant_parent(db,scope,request_id):
        parent=db.execute('SELECT archive_epoch FROM agent_user_turn_archive WHERE workspace_id=? AND request_id=?',(scope['workspace_id'],request_id)).fetchone()
        saved=db.execute("SELECT status FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?",(scope['workspace_id'],request_id)).fetchone()
        published=db.execute('SELECT 1 FROM guarded_turn_publications WHERE workspace_id=? AND response_id=?',(scope['workspace_id'],request_id)).fetchone()
        if not parent or parent[0]!=scope['archive_epoch'] or not saved or saved[0]!='saved' or not published:fail('CHAT_MEDIA_ASSISTANT_BINDING_REQUIRED',409)

    def commit_assistant_audio(self,permit):
        with self._permit_lock:
            if type(permit) is not AssistantPermit or self._permits.pop(permit.nonce,None) is not permit or permit.expires<=time.monotonic():fail('CHAT_MEDIA_ASSISTANT_PERMIT_INVALID',403)
        with self.g.demo_sessions.request(permit.token,permit.workspace),self.bank.transaction() as bank_db:
            self.g.assert_speech_evidence(permit.workspace,permit.speech_receipt,permit.token,db=bank_db)
            with closing(self.store.connect()) as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    scope=self._scope(permit.workspace,bank_db,db)
                    if (scope['registered_user_id'],scope['archive_epoch'])!=(permit.registered_user_id,permit.archive_epoch):fail('CHAT_MEDIA_STALE',409)
                    self._assistant_parent(db,scope,permit.request_id)
                    # 解密只为校验准备阶段密文未被替换，原音频不会重新生成或变码。
                    plain=self.store._cipher.decrypt(permit.cipher[4:]);n=int.from_bytes(plain[:4],'big');raw=plain[4+n:]
                    expected={'contract':CONTRACT,**permit.metadata,'registered_user_id':scope['registered_user_id'],
                        'workspace_id':permit.workspace,'archive_epoch':scope['archive_epoch'],'media_role':'assistant'}
                    if json.loads(plain[4:4+n])!=expected or len(raw)!=permit.metadata['bytes'] or hashlib.sha256(raw).hexdigest()!=permit.metadata['sha256']:fail('CHAT_MEDIA_CORRUPT',503)
                    upload_key=hashlib.sha256(packed([permit.request_id,permit.metadata]).encode()).hexdigest()[:32]
                    descriptor=self._put(db,scope,raw,permit.metadata,upload_key,role='assistant',cipher=permit.cipher)
                    binding=db.execute('SELECT * FROM chat_media_bindings WHERE media_id=?',(descriptor['media_id'],)).fetchone()
                    if binding and binding['request_id']!=permit.request_id:fail('CHAT_MEDIA_BINDING_CONFLICT',409)
                    if not binding:
                        ordinal=db.execute("SELECT coalesce(max(ordinal),-1)+1 FROM chat_media_bindings WHERE workspace_id=? AND request_id=? AND media_role='assistant'",(permit.workspace,permit.request_id)).fetchone()[0]
                        db.execute('INSERT INTO chat_media_bindings VALUES(?,?,?,?,?,?,?,?)',(descriptor['media_id'],permit.workspace,permit.request_id,scope['archive_epoch'],'assistant',ordinal,'assistant_tts',float(self.bank.clock())))
                    db.commit()
                except Exception:db.rollback();raise
        self.store._permissions();return {**descriptor,'storage_status':'bound'}

    @contextmanager
    def _http_scope(self,handler):
        if urlsplit(handler.path).query:fail('INVALID_QUERY',400)
        self.g.origin_check(handler);workspace,token=self.g.identity(handler)
        with self.g.demo_sessions.request(token,workspace),self.bank.transaction() as db:
            scope=self._scope(workspace,db)
            handler._navren_workspace=workspace;handler._navren_session_token=token
            handler._navren_start_revision=scope['privacy_revision']
            yield workspace,token,db,scope

    def context_http(self,handler):
        with self._http_scope(handler) as (workspace,token,db,scope):
            handler._finance_json(200,{'contract':'navren.chat-media-context/1','workspace_binding':workspace,
                'registered_user_id':scope['registered_user_id'],'privacy_revision':scope['privacy_revision'],'archive_epoch':scope['archive_epoch'],
                'limits':{'image_bytes':MAX_IMAGE,'audio_bytes':MAX_AUDIO,'media_per_user_turn':MAX_PER_TURN,'mime_types':list(MIMES)}})

    def upload_http(self,handler):
        self.g.origin_check(handler);workspace,token=self.g.identity(handler);self.g.check_csrf(handler,token)
        if not self._slots.acquire(blocking=False):fail('CHAT_MEDIA_QUOTA',429)
        try:
            if handler.headers.get('Transfer-Encoding') or len(handler.headers.get_all('Content-Length',[]))!=1:fail('CHAT_MEDIA_LENGTH_REQUIRED',411)
            length=handler.headers.get('Content-Length','')
            if not re.fullmatch('[0-9]{1,9}',length):fail('CHAT_MEDIA_LENGTH_REQUIRED',411)
            length=int(length);mime=handler.headers.get('Content-Type','').split(';',1)[0].strip().lower()
            if mime not in MIMES and mime not in ('audio/x-wav','audio/wave','audio/x-m4a'):fail('CHAT_MEDIA_TYPE_INVALID',415)
            if not 0<length<=(MAX_IMAGE if mime.startswith('image/') else MAX_AUDIO):fail('CHAT_MEDIA_TOO_LARGE',413)
            expected_workspace=handler.headers.get('X-Navren-Expected-Workspace');privacy=handler.headers.get('X-Navren-Privacy-Revision','');epoch=handler.headers.get('X-Navren-Archive-Epoch','')
            if expected_workspace!=workspace:fail('WORKSPACE_CHANGED',409)
            if not re.fullmatch('[0-9]{1,12}',privacy) or not re.fullmatch('[0-9]{1,12}',epoch):fail('CHAT_MEDIA_STALE',409)
            with self._http_scope(handler) as (_,_,db,scope):
                if (scope['privacy_revision'],scope['archive_epoch'])!=(int(privacy),int(epoch)):fail('CHAT_MEDIA_STALE',409)
            raw=handler.rfile.read(length)
            if len(raw)!=length:fail('CHAT_MEDIA_LENGTH_MISMATCH',400)
            with self._http_scope(handler) as (_,_,db,scope):
                result=self.put_user(workspace,raw,handler.headers.get('Content-Type',''),handler.headers.get('X-Navren-Upload-Key',''),
                    bank_db=db,expected_privacy=int(privacy),expected_epoch=int(epoch))
                handler._finance_json(201,result)
        except BankError:
            handler.close_connection=True
            raise
        finally:self._slots.release()

    def read_http(self,handler,media_id):
        with self._http_scope(handler) as (workspace,token,db,scope):
            raw,descriptor=self.read_blob(workspace,media_id,bank_db=db)
            with closing(self.store.connect()) as private:fingerprint=self._read_fingerprint(private,scope,media_id)
        # 网络发送不持银行写锁；每块及底层socket发送前重查当前会话、隐私、epoch和原件版本。
        deadline=time.monotonic()+MAX_SEND_SECONDS
        def remaining():return deadline-time.monotonic()
        def checkpoint():
            if remaining()<=0:fail('CHAT_MEDIA_SEND_TIMEOUT',408)
            with self.g.demo_sessions.request(token,workspace),closing(self.bank.connect()) as current:
                current.execute('PRAGMA query_only=ON');current.execute('BEGIN')
                now=self._scope(workspace,current)
                if any(now[k]!=scope[k] for k in ('registered_user_id','workspace_id','persona_id','privacy_revision','archive_epoch')):fail('CHAT_MEDIA_STALE',409)
                with closing(self.store.connect()) as private:
                    private.execute('PRAGMA query_only=ON');private.execute('BEGIN')
                    if self._read_fingerprint(private,now,media_id)!=fingerprint:fail('CHAT_MEDIA_CHANGED',409)
        from finance_gateway import _publication_checkpoint,_publication_remaining
        checkpoint_token=_publication_checkpoint.set(checkpoint);remaining_token=_publication_remaining.set(remaining)
        connection=getattr(handler,'connection',None);old_timeout=None;started=False
        try:
            if connection is not None:old_timeout=connection.gettimeout();connection.settimeout(2.0)
            checkpoint()
            handler._navren_chat_media_response_started=True
            started=True
            handler.send_response(200)
            for name,value in (('Content-Type',descriptor['mime_type']),('Content-Length',str(len(raw))),('X-Content-Type-Options','nosniff'),
                ('Cache-Control','no-store'),('Content-Disposition','inline'),('Accept-Ranges','none'),('X-Navren-Workspace',workspace),
                ('X-Navren-Privacy-Revision',str(scope['privacy_revision']))):handler.send_header(name,value)
            handler.end_headers()
            for at in range(0,len(raw),65536):
                checkpoint();handler.wfile.write(raw[at:at+65536]);checkpoint()
        except Exception:
            handler.close_connection=True
            if started and connection is not None:
                try:
                    import socket
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:pass
            raise
        finally:
            _publication_remaining.reset(remaining_token);_publication_checkpoint.reset(checkpoint_token)
            if connection is not None:
                try:connection.settimeout(old_timeout)
                except OSError:pass
