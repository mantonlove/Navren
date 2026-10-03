#!/usr/bin/env python3
"""Navren — 本地开发服务器 + GLM-5 API 代理 + 元景语音交互3.0 TTS"""
import http.server, json, urllib.request, urllib.error, sys, os, time, subprocess, tempfile, base64, struct
from pathlib import Path
from urllib.parse import urlsplit, unquote, parse_qsl, urlencode
import hashlib, io, re
from finance_gateway import FinanceGateway, request_json
from simulated_bank import BankError
import finance_agent
PORT = int(os.environ.get('NAVREN_PORT', os.environ.get('QINGZHOU_PORT', '8765')))
APP_PAGES = {'index.html', 'chat.html', 'recommend.html', 'account.html', 'mine.html', 'copilot.html'}

def app_release():
    """内容版本同时覆盖页面、展示脚本和样式，避免旧链接拼出混合界面。"""
    root = Path(__file__).resolve().parent
    files = [root / name for name in sorted(APP_PAGES)]
    files += sorted((root / 'css').glob('*.css')) + sorted((root / 'js').glob('*.js'))
    fingerprint = hashlib.sha256()
    for path in files:
        fingerprint.update(path.name.encode())
        fingerprint.update(path.read_bytes())
    return 'navren-' + fingerprint.hexdigest()[:12]
API_URL = 'https://maas-api.ai-yuanjing.com/openapi/compatible-mode/v1/chat/completions'
API_KEY = os.environ.get('YUANJING_API_KEY', '')
TTS_WS_URL = 'wss://maas-api.ai-yuanjing.com/openapi/v1/voice/voice_interaction'
RETRY_DELAY = 15
MAX_RETRIES = 3
TTS_INTERVAL = 3  # TTS API 调用最小间隔（秒），防 QPS 限流
_last_tts_call = 0

# 元景语音交互3.0 音色映射（高自然度）
VOICE_SPEAKER = {
    'female_sweet':  ('female_sweet',  0.95, 1.0),   # 甜美女生
    'male_warm':     ('male_warm',     0.90, 1.0),   # 温暖男生
    'male_podcast':  ('male_podcast',  0.92, 1.0),   # 播客男生
}

class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):
        kwargs['directory']=str(Path(__file__).resolve().parent)
        super().__init__(*args,**kwargs)

    def end_headers(self):
        # HTML、JS、CSS 都必须重新取得；只在 HTML 中放 meta 无法修复旧缓存。
        self.send_header('Cache-Control', 'no-store, max-age=0, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()
    def _finance_json(self, status, data, cookie=None):
        body = json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('X-Content-Type-Options','nosniff')
        if cookie: self.send_header('Set-Cookie',cookie)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path == '/api/app-version':
            self._finance_json(200, {'name': 'Navren', 'release': self.server.app_release})
            return
        if self.server.finance.dispatch(self): return
        super().do_GET()

    def send_head(self):
        # 静态页面不暴露源码、数据库、日志、备份或目录列表；HEAD同样受限。
        root = Path(__file__).resolve().parent
        path = (root/unquote(urlsplit(self.path).path).lstrip('/')).resolve()
        if path==root:
            path=root/'index.html'
            self.path='/index.html' + ('?' + urlsplit(self.path).query if urlsplit(self.path).query else '')
        try: rel=path.relative_to(root)
        except ValueError: self.send_error(404); return None
        allowed = (len(rel.parts)==1 and rel.name in APP_PAGES) or (
            len(rel.parts)>1 and rel.parts[0] in ('js','css','assets') and path.suffix.lower() in
            ('.js','.json','.css','.png','.jpg','.jpeg','.webp','.svg','.ico','.woff','.woff2','.ttf'))
        if not allowed or not path.is_file(): self.send_error(404); return None
        # 不允许 If-Modified-Since 将旧资源继续判为 304。
        for header in ('If-Modified-Since', 'If-None-Match'):
            if header in self.headers: del self.headers[header]
        if rel.name in APP_PAGES and len(rel.parts)==1:
            release = self.server.app_release
            params = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
            if params.get('ui') != release:
                params['ui'] = release
                self.send_response(307)
                self.send_header('Location', '/' + rel.name + '?' + urlencode(params))
                self.send_header('Content-Length', '0')
                self.end_headers()
                return None
            html = path.read_text(encoding='utf-8')
            def version_asset(match):
                return match.group(1) + match.group(2) + '?v=' + release + match.group(3)
            html = re.sub(r'(?:((?:src|href)=[\"\']))((?:js|css)/[^\"\'?]+)(?:\?[^\"\']*)?([\"\'])', version_asset, html)
            bootstrap = ('<meta name="navren-release" content="' + release + '">'
                         '<script src="/js/app_boot.js?v=' + release + '"></script>')
            html = html.replace('<head>', '<head>' + bootstrap, 1)
            body = html.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Navren-Release', release)
            self.end_headers()
            return io.BytesIO(body)
        return super().send_head()

    def do_POST(self):
        if self.server.finance.dispatch(self): return
        if self.path == '/api/chat':
            self._handle_chat()
        elif self.path == '/api/agent':
            self._handle_agent()
        elif self.path == '/api/tts':
            self._handle_tts()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_agent(self):
        """智能体编排端点：意图识别→槽位提取→产品检索→合规审查→LLM生成→日志"""
        try:
            data = request_json(self)

            text = data.get('text', '').strip()
            mode = data.get('mode', 'classic')
            profile = data.get('profile', {})
            history = data.get('history', [])
            image = data.get('image', None)
            followup_memory = data.get('followup_memory', None)
            holdings = data.get('holdings', None)

            if not text:
                self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'text is required'}).encode())
                return

            if finance_agent.supports(text):
                gateway=self.server.finance
                gateway.origin_check(self)
                workspace,token=gateway.identity(self)
                gateway.check_csrf(self,token)
                result=finance_agent.process(gateway,workspace,text)
                self._finance_json(200,result)
                return

            from agent_core import get_agent
            agent = get_agent()
            result = agent.process(text, mode=mode, profile=profile, history=history,
                                   image=image, followup_memory=followup_memory, holdings=holdings)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))

        except BankError as e:
            self._finance_json(e.status,{'error':e.as_dict(),'bank':True,'reply':e.message+' 本次没有确认执行任何操作。','isFallback':True})
        except Exception:
            self._finance_json(500,{'error':{'code':'AGENT_UNAVAILABLE','message':'服务暂不可用。'},
                'reply':'服务暂不可用。请在账户页查询订单核实状态，本次对话没有确认执行操作。','isFallback':True})

    def _handle_chat(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            data = json.loads(body)

            for attempt in range(MAX_RETRIES):
                try:
                    req = urllib.request.Request(API_URL,
                        data=json.dumps(data).encode('utf-8'),
                        headers={
                            'Content-Type': 'application/json',
                            'Authorization': f'Bearer {API_KEY}'
                        })
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        result = json.loads(resp.read().decode('utf-8'))

                    if result.get('code') == 5001:
                        if attempt < MAX_RETRIES - 1:
                            print(f'  QPM限流，{RETRY_DELAY}s后重试({attempt+1}/{MAX_RETRIES})...')
                            time.sleep(RETRY_DELAY)
                            continue
                        else:
                            self.send_response(429)
                            self.send_header('Access-Control-Allow-Origin', '*')
                            self.end_headers()
                            self.wfile.write(json.dumps({'error': 'QPM限流，请稍后重试'}).encode())
                            return

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
                    return
                except urllib.error.HTTPError as e:
                    err_body = e.read().decode('utf-8', errors='replace')
                    if attempt < MAX_RETRIES - 1:
                        print(f'  HTTP {e.code}, retrying ({attempt+1}/{MAX_RETRIES})...')
                        time.sleep(RETRY_DELAY)
                        continue
                    self.send_response(500)
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({'error': f'HTTP {e.code}: {err_body[:200]}'}).encode())
                    return
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        print(f'  Error: {e}, retrying ({attempt+1}/{MAX_RETRIES})...')
                        time.sleep(RETRY_DELAY)
                        continue
                    raise

        except Exception as e:
            self.send_response(500)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8'))

    def _handle_tts(self):
        try:
            # 语音依赖按需加载，缺失时不影响页面和文字智能体启动。
            try:
                from websocket import create_connection
            except ImportError as e:
                raise RuntimeError('语音服务缺少 websocket-client，请安装 requirements.txt 中的依赖') from e

            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            text = body.get('text', '').strip()
            voice_id = body.get('voice', 'gentle-female')
            rate = float(body.get('rate', 0.9))

            if not text:
                self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'text is required'}).encode())
                return

            text = text.replace('\n', ' ')[:500]
            speaker_id, speed, energy = VOICE_SPEAKER.get(voice_id, ('female_sweet', 0.95, 1.0))
            final_rate = rate * speed / 0.95

            # QPS 限流控制
            global _last_tts_call
            elapsed = time.time() - _last_tts_call
            if elapsed < TTS_INTERVAL:
                time.sleep(TTS_INTERVAL - elapsed)
            _last_tts_call = time.time()

            # Step 1: 用 say 生成 prompt 音频（"请朗读：{text}"）
            prompt = f'请朗读以下文字，只朗读不回复：{text}'
            with tempfile.NamedTemporaryFile(suffix='.aiff', delete=False) as tmp:
                aiff_path = tmp.name
            in_wav = aiff_path + '.wav'

            try:
                subprocess.run(
                    ['say', '-v', 'Tingting', '-r', '200', prompt, '-o', aiff_path],
                    capture_output=True, timeout=10
                )
                subprocess.run(
                    ['afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', aiff_path, in_wav],
                    capture_output=True, timeout=5
                )

                # Step 2: 通过 WebSocket 发送给元景语音 API
                ws = None
                for tts_attempt in range(3):
                    try:
                        ws = create_connection(TTS_WS_URL,
                            header={'Authorization': f'Bearer {API_KEY}'}, timeout=15)
                        break
                    except Exception as e:
                        if '429' in str(e) or '5001' in str(e):
                            if tts_attempt < 2:
                                time.sleep(5)
                                continue
                        raise
                if ws is None:
                    raise RuntimeError('TTS API connection failed after retries')

                ws.send(json.dumps({
                    'session_id': f'tts-{int(time.time()*1000)}',
                    'sample_rate': 16000, 'audio_format': 'pcm',
                    'kbps': 128, 'speed': final_rate, 'energy': energy,
                    'speaker_id': speaker_id, 'output_sample_rate': 24000
                }))

                with open(in_wav, 'rb') as f:
                    f.read(44)  # skip WAV header
                    while True:
                        chunk = f.read(4096)
                        if not chunk: break
                        ws.send_binary(chunk)
                        time.sleep(0.03)

                for _ in range(30):
                    ws.send_binary(bytes(4096))
                    time.sleep(0.03)
                ws.send(json.dumps({'eof': 1}))

                # Step 3: 接收音频输出
                audio_chunks = []
                deadline = time.time() + 25
                while time.time() < deadline:
                    try:
                        ws.settimeout(3)
                        msg = ws.recv()
                        if msg:
                            data = json.loads(msg)
                            b64 = data.get('audio', '')
                            if b64:
                                audio_chunks.append(base64.b64decode(b64))
                            if data.get('status') in (2, 3):
                                break
                    except Exception:
                        break
                ws.close()

                if not audio_chunks:
                    raise RuntimeError('TTS API returned no audio')

                # Step 4: PCM → WAV
                pcm_data = b''.join(audio_chunks)
                wav_buf = bytearray()
                wav_buf += b'RIFF'
                wav_buf += struct.pack('<I', 36 + len(pcm_data))
                wav_buf += b'WAVEfmt '
                wav_buf += struct.pack('<IHHIIHH', 16, 1, 1, 24000, 24000 * 2, 2, 16)
                wav_buf += b'data'
                wav_buf += struct.pack('<I', len(pcm_data))
                wav_buf += pcm_data

                self.send_response(200)
                self.send_header('Content-Type', 'audio/wav')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(wav_buf)))
                self.end_headers()
                self.wfile.write(bytes(wav_buf))

            finally:
                for p in [aiff_path, in_wav]:
                    if os.path.exists(p):
                        os.unlink(p)

        except Exception as e:
            self.send_response(500)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}, ensure_ascii=False).encode('utf-8'))

    def do_OPTIONS(self):
        if urlsplit(self.path).path.startswith('/api/finance'):
            self._finance_json(403,{'error':{'code':'CROSS_ORIGIN_DENIED','message':'银行接口不开放跨站访问。'}})
            return
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()

def create_server(port=PORT, database=None):
    server=http.server.ThreadingHTTPServer(('127.0.0.1',port),ProxyHandler)
    server.app_release=app_release()
    server.finance=FinanceGateway(database)
    return server


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f'Navren服务器 → http://localhost:{PORT}')
    print(f'Chat API 代理 → {API_URL}')
    print(f'Agent 智能体 → /api/agent (产品检索+合规审查+LLM编排)')
    print(f'TTS 语音合成 → /api/tts')
    if API_KEY:
        print('模型服务密钥已加载')
    else:
        print('⚠️  YUANJING_API_KEY 未设置，LLM 调用将失败（确定性回复仍可用）')
    create_server().serve_forever()
