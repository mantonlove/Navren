"""GLM 官方语音 HTTP 传输；不包含身份、业务回答或自动云识别授权。"""
import http.client
import io
import json
import math
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid
import wave

from simulated_bank import BankError

ASR_MODEL, TTS_MODEL = 'glm-asr-2512', 'glm-tts'
ASR_RATE, TTS_RATE = 16000, 24000
OFFICIAL_HOST = 'open.bigmodel.cn'
OFFICIAL_BASE = 'https://open.bigmodel.cn/api/paas/v4/audio'
# 仅映射已有页面选项，不改写用户偏好，也不保证与旧供应商音色相同。
VOICES = {'female_sweet': 'tongtong', 'male_warm': 'xiaochen', 'male_podcast': 'chuichui'}
APPROVED_SPEECH = object()
CLOUD_ASR_OPT_IN = object()


def endpoint():
    return OFFICIAL_BASE


def load_key():
    key = os.environ.get('GLM_API_KEY', '').strip()
    if not key and sys.platform == 'darwin':
        try:
            result = subprocess.run(['security', 'find-generic-password', '-a', os.environ.get('USER', ''),
                                     '-s', 'CC/GLM_API_KEY', '-w'], capture_output=True, timeout=5)
            if result.returncode == 0:
                key = result.stdout.decode().strip()
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            pass
    return key


def pcm_wav(raw):
    out = io.BytesIO()
    with wave.open(out, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(ASR_RATE)
        stream.writeframes(raw)
    return out.getvalue()


def checked_pcm(body, maximum):
    try:
        with wave.open(io.BytesIO(body), 'rb') as stream:
            if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype()) != (1, 2, TTS_RATE, 'NONE'):
                raise ValueError('unexpected WAV format')
            expected = stream.getnframes() * 2
            if expected <= 0 or expected > maximum:
                raise ValueError('unexpected WAV length')
            raw = stream.readframes(stream.getnframes() + 1)
            if len(raw) != expected or len(raw) % 2:
                raise ValueError('truncated WAV')
            return raw
    except (wave.Error, EOFError, ValueError, TypeError):
        raise BankError('SPEECH_AUDIO_FORMAT', '官方朗读返回的音频格式无效，请使用文字查看。', 502)


class SpeechTask:
    """匹配 VoiceGateway 的 audio/finish/receive/close 接口；只支持官方 ASR/TTS。"""
    normalized_sentence = True
    MAX_AUDIO_BYTES = ASR_RATE * 2 * 120
    SEGMENT_BYTES = ASR_RATE * 2 * 30
    MAX_PCM_BYTES = TTS_RATE * 2 * 120
    # 文件接口可能在返回响应头前完成生成；不得套用旧WS的1秒轮询超时。
    IO_TIMEOUT = 5.0

    def __init__(self, kind, options, key, url, connector=None, *, transport=None):
        if kind not in ('asr', 'tts') or url != OFFICIAL_BASE:
            raise BankError('SPEECH_PROTOCOL_ERROR', '语音服务地址或任务类型无效。', 503)
        if not key:
            raise BankError('SPEECH_NOT_CONFIGURED', 'GLM 官方语音密钥尚未配置。', 503)
        if connector is not None:
            raise BankError('SPEECH_PROTOCOL_ERROR', '官方语音使用 HTTP 传输。', 503)
        if kind == 'asr' and options.get('_cloud_asr_opt_in') is not CLOUD_ASR_OPT_IN:
            raise BankError('LOCAL_ASR_REQUIRED', '云端语音识别需要明确授权。', 403)
        if kind == 'tts' and options.get('_speech_approval') is not APPROVED_SPEECH:
            raise BankError('SPEECH_NOT_APPROVED', '朗读内容尚未通过后端核准。', 403)
        self.kind, self.id, self.done = kind, str(uuid.uuid4()), False
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._events = queue.Queue(maxsize=16)
        self._audio = bytearray()
        self._runner = None
        self._connection = self._response = None
        self._key = key
        self._transport = transport
        self._text = None
        if kind == 'tts':
            text = options.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                raise BankError('INVALID_SPEECH_TEXT', '朗读内容为空或超过八千字，请分段朗读。')
            self._voice = VOICES.get(options.get('voice', 'female_sweet'))
            if not self._voice:
                raise BankError('INVALID_VOICE', '请选择页面提供的音色。')
            try:
                self._rate = float(options.get('rate', 1.0))
            except (TypeError, ValueError):
                self._rate = float('nan')
            if not math.isfinite(self._rate) or not 0.5 <= self._rate <= 2:
                raise BankError('INVALID_RATE', '语速应在0.5至2倍之间。')
            self._text = text
            self.done = True
            self._start()

    def _start(self):
        self._runner = threading.Thread(target=self._run, name='navren-glm-' + self.kind, daemon=True)
        try:
            self._runner.start()
        except Exception:
            self._key = self._text = None
            raise BankError('SPEECH_START_FAILED', '官方语音任务无法启动。', 503)

    def audio(self, data):
        with self._lock:
            if self.kind != 'asr' or self.done or self._closed.is_set() or not isinstance(data, bytes) or not data or len(data) > 65536 or len(data) % 2:
                raise BankError('INVALID_AUDIO', '录音数据无效。')
            if len(self._audio) + len(data) > self.MAX_AUDIO_BYTES:
                raise BankError('SPEECH_TIMEOUT', '录音达到两分钟上限，请分段说。', 408)
            self._audio.extend(data)

    def finish(self):
        with self._lock:
            if self.done:
                return
            if self._closed.is_set() or not self._audio:
                raise BankError('MICROPHONE_NO_AUDIO', '没有收到麦克风音频，请重试。')
            self.done = True
            self._start()

    def _emit(self, event):
        while not self._closed.is_set():
            try:
                self._events.put(event, timeout=0.1)
                return
            except queue.Full:
                continue

    def _check_open(self):
        if self._closed.is_set():
            raise InterruptedError('speech cancelled')

    def _post(self, suffix, body, content_type, maximum, deadline):
        self._check_open()
        if suffix not in ('/transcriptions', '/speech'):
            raise ValueError('unsupported official speech path')
        if self._transport is not None:
            # 仅供无网络桩测试注入；正式调用始终固定 HTTPS 主机，不读取代理或重定向。
            status, headers, result = self._transport(OFFICIAL_BASE + suffix, body, content_type, self._closed)
            self._check_open()
            if len(result) > maximum:
                raise ValueError('response too large')
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            connection = http.client.HTTPSConnection(OFFICIAL_HOST, timeout=min(self.IO_TIMEOUT, remaining))
            with self._lock:
                self._check_open()
                self._connection = connection
            try:
                self._check_open()
                connection.request('POST', '/api/paas/v4/audio' + suffix, body=body,
                                   headers={'Authorization': 'Bearer ' + self._key, 'Content-Type': content_type,
                                            'Accept': 'audio/wav' if suffix == '/speech' else 'application/json'})
                self._check_open()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                if connection.sock:
                    connection.sock.settimeout(min(self.IO_TIMEOUT, remaining))
                response = connection.getresponse()
                with self._lock:
                    self._response = response
                status, headers = response.status, {'content-type': response.getheader('Content-Type', '')}
                chunks, length = [], 0
                while True:
                    self._check_open()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    if connection.sock:
                        connection.sock.settimeout(min(self.IO_TIMEOUT, remaining))
                    block = response.read1(min(65536, maximum + 1 - length))
                    if not block:
                        break
                    length += len(block)
                    if length > maximum:
                        raise ValueError('response too large')
                    chunks.append(block)
                result = b''.join(chunks)
            finally:
                connection.close()
                with self._lock:
                    self._connection = self._response = None
        self._check_open()
        if not 200 <= status < 300:
            code = 'SPEECH_RATE_LIMIT' if status == 429 else 'SPEECH_MODEL_ERROR'
            raise BankError(code, 'GLM 官方语音请求失败，请核对权限、余额或稍后重试。', 429 if status == 429 else 502)
        return headers, result

    def _run(self):
        try:
            if self.kind == 'asr':
                # 总预算小于网关 finish 后原有15秒等待；无自动重试。
                deadline = time.monotonic() + 12
                index = 0
                while True:
                    self._check_open()
                    with self._lock:
                        if not self._audio:
                            break
                        raw = bytes(self._audio[:self.SEGMENT_BYTES])
                        del self._audio[:self.SEGMENT_BYTES]
                    if time.monotonic() > deadline:
                        raise TimeoutError()
                    boundary = 'navren-' + uuid.uuid4().hex
                    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{ASR_MODEL}\r\n'
                            f'--{boundary}\r\nContent-Disposition: form-data; name="stream"\r\n\r\nfalse\r\n'
                            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
                            'Content-Type: audio/wav\r\n\r\n').encode() + pcm_wav(raw) + f'\r\n--{boundary}--\r\n'.encode()
                    raw = None
                    _, result = self._post('/transcriptions', body, 'multipart/form-data; boundary=' + boundary, 65536, deadline)
                    body = None
                    decoded = json.loads(result)
                    text = decoded.get('text') if isinstance(decoded, dict) and not decoded.get('error') else None
                    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                        raise BankError('SPEECH_NO_TEXT', '没有识别到有效文字，请重试或使用文字输入。', 502)
                    self._emit({'header': {'event': 'result-generated', 'task_id': self.id}, 'payload': {'output': {
                        'sentence': {'sentence_id': index, 'text': text, 'sentence_end': True}}}})
                    index += 1
            else:
                text, self._text = self._text, None
                total = 0
                for offset in range(0, len(text), 1024):
                    self._check_open()
                    body = json.dumps({'model': TTS_MODEL, 'input': text[offset:offset+1024], 'voice': self._voice,
                                       'speed': self._rate, 'stream': False, 'response_format': 'wav',
                                       'watermark_enabled': True}, ensure_ascii=False).encode()
                    headers, result = self._post('/speech', body, 'application/json', self.MAX_PCM_BYTES + 65536, time.monotonic() + 12)
                    if 'json' in headers.get('content-type', '').lower() or result[:1] in (b'{', b'['):
                        raise BankError('SPEECH_MODEL_ERROR', '官方朗读未返回音频，请稍后重试。', 502)
                    pcm = checked_pcm(result, self.MAX_PCM_BYTES - total)
                    total += len(pcm)
                    for pos in range(0, len(pcm), 4800):
                        self._emit(pcm[pos:pos+4800])
                text = None
            self._emit({'header': {'event': 'task-finished', 'task_id': self.id}})
        except Exception as error:
            if not self._closed.is_set():
                self._emit(error if isinstance(error, BankError) else BankError(
                    'SPEECH_TIMEOUT' if isinstance(error, (TimeoutError, socket.timeout)) else 'SPEECH_UNAVAILABLE',
                    '官方语音暂不可用，请重试或使用文字输入。', 502))
        finally:
            with self._lock:
                self._audio.clear()
            self._text = self._key = None

    def receive(self):
        if self._closed.is_set():
            raise BankError('SPEECH_DISCONNECTED', '语音会话已结束。', 502)
        try:
            event = self._events.get(timeout=1)
        except queue.Empty:
            raise TimeoutError()
        if isinstance(event, Exception):
            raise event
        return event

    def close(self):
        self._closed.set()
        with self._lock:
            connection, response = self._connection, self._response
            self._audio.clear()
            self._text = None
        if connection and connection.sock:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if response:
            response.close()
        if connection:
            connection.close()
        if self._runner and self._runner is not threading.current_thread():
            self._runner.join(1.5)
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                break
