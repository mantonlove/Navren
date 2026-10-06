"""百炼语音代理：同源会话、一次性票据、PCM 流和取消；不保存录音。"""
import json
import math
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from finance_gateway import request_json
from simulated_bank import BankError

ASR_MODEL = 'qwen-audio-3.1-asr-flash-streaming'
TTS_MODEL = 'qwen-audio-3.1-tts-flash'
ASR_RATE, TTS_RATE = 16000, 24000
VOICES = {
    'female_sweet': 'longanwen_v3.1',
    'male_warm': 'longanyang_v3.1',
    'male_podcast': 'longsanshu_v3.1',
}


def load_key():
    key = os.environ.get('DASHSCOPE_API_KEY', '').strip()
    if not key and sys_platform_mac():
        try:
            result = subprocess.run(['security', 'find-generic-password', '-a', os.environ.get('USER', ''),
                                     '-s', 'CC/DASHSCOPE_API_KEY', '-w'], capture_output=True, timeout=5)
            if result.returncode == 0:
                key = result.stdout.decode().strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return key


def sys_platform_mac():
    import sys
    return sys.platform == 'darwin'


def endpoint():
    config_path = Path(__file__).with_name('voice.local.json')
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    host = os.environ.get('DASHSCOPE_API_HOST', '') or config.get('api_host', '')
    workspace = os.environ.get('DASHSCOPE_WORKSPACE_ID', '') or config.get('workspace_id', '')
    if not host and workspace:
        host = f'{workspace}.cn-beijing.maas.aliyuncs.com'
    if host:
        host = host.strip().removeprefix('https://').removeprefix('wss://').rstrip('/')
        if not re.fullmatch(r'[a-zA-Z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com', host):
            raise ValueError('百炼语音 API Host 应为北京地域的业务空间专属域名。')
    else:
        # 官方保留的存量域名；配置业务空间后自动使用专属域名。
        host = 'dashscope.aliyuncs.com'
    return f'wss://{host}/api-ws/v1/inference'


def speech_text(text):
    """去掉展示标记并规范符号，保留全部风险披露和金额数值。"""
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        raise BankError('INVALID_SPEECH_TEXT', '朗读内容为空或超过八千字，请分段朗读。')
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'(?m)^\s{0,3}(?:#{1,6}|>|[-*])\s+', '', text)
    text = text.replace('**', '').replace('`', '').replace('⚠️', '注意：')
    text = re.sub(r'(\d+(?:\.\d+)?)\s*%', r'百分之\1', text)
    text = re.sub(r'[¥￥]\s*([\d,]+(?:\.\d+)?)', lambda m: m[1].replace(',', '') + '元', text)
    return re.sub(r'\n+', '。', text).strip()


def packet(action, task_id, payload=None):
    return {'header': {'action': action, 'task_id': task_id, 'streaming': 'duplex'},
            'payload': payload or {'input': {}}}


class SpeechTask:
    """只暴露选定的两种语音任务；协议可独立用模拟服务验收。"""
    def __init__(self, kind, options, key, url, connector=None):
        self.kind, self.id = kind, str(uuid.uuid4())
        self.ws = None
        self.lock = threading.Lock()
        self.done = False
        if kind == 'tts':
            text = speech_text(options.get('text'))
            voice = VOICES.get(options.get('voice', 'female_sweet'))
            if not voice:
                raise BankError('INVALID_VOICE', '请选择页面提供的音色。')
            try:
                rate = float(options.get('rate', 1.0))
            except (ValueError, TypeError):
                rate = float('nan')
            if not math.isfinite(rate) or not 0.5 <= rate <= 2:
                raise BankError('INVALID_RATE', '语速应在 0.5 至 2 倍之间。')
            parameters = {'text_type': 'PlainText', 'voice': voice, 'format': 'pcm',
                          'sample_rate': TTS_RATE, 'rate': rate, 'volume': 50,
                          'instruction': '使用普通话，吐字清晰，语气平和，准确朗读金额和风险提示。'}
        else:
            text = None
            parameters = {'format': 'pcm', 'sample_rate': ASR_RATE,
                          'semantic_punctuation_enabled': False, 'max_sentence_silence': 1000,
                          'keep_dialect': False,
                          'vocabulary': {'Navren': 3, '申购': 3, '赎回': 3, '年化收益率': 3,
                                         '风险等级': 3, '代扣': 3, '应急金': 3}}
        if not key:
            raise BankError('SPEECH_NOT_CONFIGURED', '百炼语音密钥尚未配置。', 503)
        if connector is None:
            from websocket import create_connection
            connector = create_connection
        try:
            self.ws = connector(url, header={'Authorization': f'Bearer {key}'}, timeout=10,
                                enable_multithread=True)
            payload = {'task_group': 'audio', 'task': kind,
                       'function': 'recognition' if kind == 'asr' else 'SpeechSynthesizer',
                       'model': ASR_MODEL if kind == 'asr' else TTS_MODEL,
                       'parameters': parameters, 'input': {}}
            self.send(packet('run-task', self.id, payload))
            message = self.receive()
            if message.get('header', {}).get('event') != 'task-started':
                raise BankError('SPEECH_START_FAILED', '百炼未能启动语音任务，请核对模型权限和地域。', 502)
            if text:
                # 所有文本顺序送达，不再截断五百字或删除风险提示。
                for offset in range(0, len(text), 1000):
                    self.send(packet('continue-task', self.id, {'input': {'text': text[offset:offset+1000]}}))
                self.finish()
            self.ws.settimeout(1)
        except Exception:
            self.close()
            raise

    def send(self, message):
        with self.lock:
            self.ws.send(json.dumps(message, ensure_ascii=False))

    def audio(self, data):
        if self.done or not data or len(data) > 65536 or len(data) % 2:
            raise BankError('INVALID_AUDIO', '录音数据无效。')
        with self.lock:
            self.ws.send_binary(data)

    def finish(self):
        if not self.done:
            self.done = True
            self.send(packet('finish-task', self.id))

    def receive(self):
        data = self.ws.recv()
        if isinstance(data, bytes):
            return data
        if not data:
            raise BankError('SPEECH_DISCONNECTED', '语音连接已断开，请重试。', 502)
        message = json.loads(data)
        header = message.get('header', {})
        if header.get('task_id') != self.id:
            raise BankError('SPEECH_PROTOCOL_ERROR', '语音任务返回无效。', 502)
        if header.get('event') == 'task-failed':
            code = str(header.get('error_code', ''))
            if 'limit' in code.lower() or 'throttl' in code.lower():
                raise BankError('SPEECH_RATE_LIMIT', '语音服务繁忙，请稍后重试。', 429)
            raise BankError('SPEECH_MODEL_ERROR', '百炼语音任务失败，请核对模型权限、音色及账户余额。', 502)
        return message

    def close(self):
        if self.ws:
            try:
                self.ws.close(timeout=1)
            except Exception:
                pass


class BrowserSocket:
    """wsproto 处理升级和帧，不自行实现 WebSocket 编解码。"""
    def __init__(self, handler):
        from wsproto import WSConnection, ConnectionType
        from wsproto.events import AcceptConnection, Request
        self.connection = WSConnection(ConnectionType.SERVER)
        self.socket = handler.connection
        self.lock = threading.Lock()
        self.closed = False
        self.connection.initiate_upgrade_connection(
            [(k.lower().encode('ascii'), v.encode('latin1')) for k, v in handler.headers.items()], handler.path)
        if not any(isinstance(event, Request) for event in self.connection.events()):
            raise ValueError('无效 WebSocket 握手')
        self.socket.sendall(self.connection.send(AcceptConnection()))
        handler.close_connection = True
        self.socket.settimeout(1)

    def send(self, value):
        from wsproto.events import BytesMessage, TextMessage
        with self.lock:
            if self.closed:
                raise ConnectionError('连接已关闭')
            data = self.connection.send(BytesMessage(data=value) if isinstance(value, bytes)
                                        else TextMessage(data=json.dumps(value, ensure_ascii=False)))
            self.socket.sendall(data)

    def receive(self):
        from wsproto.events import BytesMessage, TextMessage, Ping, CloseConnection
        pending_bytes, pending_text = bytearray(), ''
        while not self.closed:
            try:
                data = self.socket.recv(8192)
            except socket.timeout:
                yield None
                continue
            if not data:
                return
            with self.lock:
                self.connection.receive_data(data)
                events = list(self.connection.events())
            for event in events:
                if isinstance(event, Ping):
                    with self.lock:
                        self.socket.sendall(self.connection.send(event.response()))
                elif isinstance(event, CloseConnection):
                    return
                elif isinstance(event, BytesMessage):
                    pending_bytes.extend(event.data)
                    if len(pending_bytes) > 65536:
                        raise BankError('INVALID_AUDIO', '单次录音数据过大。')
                    if event.message_finished:
                        yield bytes(pending_bytes)
                        pending_bytes.clear()
                elif isinstance(event, TextMessage):
                    pending_text += event.data
                    if len(pending_text) > 20000:
                        raise BankError('INVALID_SPEECH_TEXT', '语音请求过大。')
                    if event.message_finished:
                        value = json.loads(pending_text)
                        if not isinstance(value, dict):
                            raise ValueError('请求必须为对象')
                        yield value
                        pending_text = ''

    def close(self):
        from wsproto.events import CloseConnection
        with self.lock:
            if not self.closed:
                try:
                    self.socket.sendall(self.connection.send(CloseConnection(code=1000, reason='任务结束')))
                except Exception:
                    pass
                self.closed = True


class VoiceGateway:
    def __init__(self, key=None, url=None, task_factory=SpeechTask, *, allow_cloud_asr=None):
        self.key = load_key() if key is None else key
        self.url = endpoint() if url is None else url
        self.task_factory = task_factory
        self.allow_cloud_asr = (os.environ.get('NAVREN_ALLOW_CLOUD_ASR')=='1') if allow_cloud_asr is None else bool(allow_cloud_asr)
        self.tickets = {}
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(3)

    def status(self):
        import importlib.util
        dependencies = all(importlib.util.find_spec(name) for name in ('websocket', 'wsproto'))
        return {'configured': bool(self.key) and dependencies, 'asr_model': ASR_MODEL, 'tts_model': TTS_MODEL,
                'asr_available':bool(self.key) and dependencies and self.allow_cloud_asr,
                'asr_privacy_mode':'explicit_cloud_audio_opt_in' if self.allow_cloud_asr else 'local_asr_required',
                'voices': VOICES, 'asr_sample_rate': ASR_RATE, 'tts_sample_rate': TTS_RATE,
                'message': '' if self.key and dependencies else '语音服务尚未配置密钥或安装依赖。'}

    def authorize(self, handler):
        finance = handler.server.finance
        finance.origin_check(handler)
        _, token = finance.identity(handler)
        return token

    def session(self, handler):
        token = self.authorize(handler)
        handler.server.finance.check_csrf(handler, token)
        data = request_json(handler)
        kind = data.get('kind')
        if set(data)-{'kind','speech_receipt','purpose'}:
            raise BankError('INVALID_SPEECH_REQUEST','语音请求包含未支持字段。')
        if kind not in ('asr', 'tts'):
            raise BankError('INVALID_SPEECH_KIND', '语音任务类型无效。')
        if kind=='asr' and not self.allow_cloud_asr:
            raise BankError('LOCAL_ASR_REQUIRED','语音输入需要本地识别服务，目前请使用文字输入。',403)
        workspace,_=handler.server.finance.identity(handler)
        receipt=data.get('speech_receipt') if kind=='tts' else None
        if kind=='tts' and data.get('purpose')=='voice_preview':
            finance=handler.server.finance
            fixed='您好，我是 Navren，可以帮您看懂账单、比较产品和安排资金。理财有风险，请仔细阅读产品说明。'
            receipt=finance.approve_speech(workspace,{'reply':fixed,'compliance_pass':True,
                'answer_contract':{'kind':'fixed_public_voice_preview'},**finance.session_data(workspace,token)},token)
        elif data.get('purpose') is not None:
            raise BankError('INVALID_SPEECH_REQUEST','语音用途无效。')
        if kind=='tts':handler.server.finance.resolve_speech(workspace,receipt,token)
        if not self.status()['configured']:
            raise BankError('SPEECH_NOT_CONFIGURED', self.status()['message'], 503)
        ticket = secrets.token_urlsafe(32)
        with self.lock:
            now = time.monotonic()
            self.tickets = {k: v for k, v in self.tickets.items() if v[2] > now}
            if len(self.tickets) >= 64:
                raise BankError('SPEECH_BUSY', '语音服务繁忙，请稍后重试。', 429)
            self.tickets[ticket] = (token, kind, now + 30, workspace, receipt)
        return {'path': '/api/speech/ws?ticket=' + ticket, 'kind': kind}

    def socket(self, handler):
        token = self.authorize(handler)
        # 浏览器升级也必须明确携带同源 Origin，防跨站消耗模型额度。
        if handler.headers.get('Origin') != 'http://' + handler.headers.get('Host', ''):
            raise BankError('CROSS_ORIGIN_DENIED', '拒绝跨站语音请求。', 403)
        ticket = parse_qs(urlsplit(handler.path).query).get('ticket', [''])[0]
        with self.lock:
            entry = self.tickets.pop(ticket, None)
        if not entry or entry[0] != token or entry[2] < time.monotonic():
            raise BankError('SPEECH_TICKET_EXPIRED', '语音会话已过期，请重试。', 403)
        if not self.slots.acquire(blocking=False):
            raise BankError('SPEECH_BUSY', '语音服务繁忙，请稍后重试。', 429)
        browser, task, receiver = None, None, None
        ended = threading.Event()
        deadline, audio_bytes = time.monotonic() + 120, 0
        try:
            browser = BrowserSocket(handler)
            for value in browser.receive():
                handler.server.finance.demo_sessions.validate(token,entry[3])
                if ended.is_set():
                    break
                if time.monotonic() > deadline:
                    raise BankError('SPEECH_TIMEOUT', '语音会话已超时，请重新开始。', 408)
                if value is None:
                    continue
                if isinstance(value, dict) and value.get('type') == 'cancel':
                    break
                if task is None:
                    if not isinstance(value, dict) or value.get('type') != 'start':
                        raise BankError('SPEECH_PROTOCOL_ERROR', '语音启动请求无效。')
                    if entry[1]=='tts':
                        with handler.server.finance.speech_guard(entry[3],entry[4],token) as approved_text:
                            value={**value,'text':approved_text}
                            task = self.task_factory(entry[1], value, self.key, self.url)
                    else:task = self.task_factory(entry[1], value, self.key, self.url)
                    browser.send({'type': 'ready', 'kind': entry[1], 'sample_rate':
                                  ASR_RATE if entry[1] == 'asr' else TTS_RATE})

                    def pump():
                        try:
                            while not ended.is_set():
                                handler.server.finance.demo_sessions.validate(token,entry[3])
                                try:
                                    message = task.receive()
                                except (socket.timeout, TimeoutError):
                                    continue
                                except Exception as error:
                                    from websocket import WebSocketTimeoutException
                                    if isinstance(error, WebSocketTimeoutException):
                                        continue
                                    raise
                                # receive可能跨过账户切换；每个输出在银行身份锁内发布。
                                def publish(value):
                                    finance=handler.server.finance
                                    guard=finance.speech_guard(entry[3],entry[4],token) if entry[1]=='tts' else finance.bank.transaction()
                                    with finance.demo_sessions.request(token,entry[3]), guard:
                                        finance.demo_sessions.validate(token,entry[3])
                                        browser.send(value)
                                if isinstance(message, bytes):
                                    publish(message)
                                    continue
                                event = message.get('header', {}).get('event')
                                if event == 'result-generated' and entry[1] == 'asr':
                                    # 使用阿里官方SDK结果适配器，兼容供应商识别事件。
                                    from dashscope.audio.asr import RecognitionResult
                                    from dashscope.api_entities.dashscope_response import RecognitionResponse
                                    output=message.get('payload', {}).get('output', {})
                                    response=RecognitionResponse(status_code=200,request_id=task.id,output=output)
                                    sentence = RecognitionResult(response).get_sentence() or {}
                                    if not sentence.get('heartbeat'):
                                        publish({'type': 'transcript', 'text': sentence.get('text', ''),
                                                      'final': bool(sentence.get('sentence_end')),
                                                      'sentence_id': sentence.get('sentence_id', sentence.get('begin_time', 0))})
                                elif event == 'task-finished':
                                    publish({'type': 'done'})
                                    break
                        except Exception as error:
                            try:
                                browser.send(safe_error(error))
                            except Exception:
                                pass
                        finally:
                            ended.set()
                    receiver = threading.Thread(target=pump, daemon=True)
                    receiver.start()
                elif isinstance(value, bytes) and entry[1] == 'asr':
                    audio_bytes += len(value)
                    if audio_bytes > ASR_RATE * 2 * 120:
                        raise BankError('SPEECH_TIMEOUT', '录音达到两分钟上限，请分段说。')
                    task.audio(value)
                elif isinstance(value, dict) and value.get('type') == 'finish' and entry[1] == 'asr':
                    if not audio_bytes:
                        raise BankError('MICROPHONE_NO_AUDIO','没有收到麦克风音频，请检查输入设备与系统录音权限。')
                    task.finish()
                    deadline = time.monotonic() + 15
                else:
                    raise BankError('SPEECH_PROTOCOL_ERROR', '语音请求无效。')
        except Exception as error:
            if browser:
                try:
                    browser.send(safe_error(error))
                except Exception:
                    pass
            else:
                raise
        finally:
            ended.set()
            if task:
                task.close()
            if receiver:
                receiver.join(2)
            if browser:
                browser.close()
            self.slots.release()


def safe_error(error):
    if isinstance(error, BankError):
        return {'type': 'error', 'code': error.code, 'message': error.message}
    return {'type': 'error', 'code': 'SPEECH_UNAVAILABLE',
            'message': '语音连接暂不可用，请检查网络、百炼地域与模型权限后重试。'}
