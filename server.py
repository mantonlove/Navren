#!/usr/bin/env python3
"""Navren — 本地开发服务器、金融 Agent 与百炼流式语音代理。"""
import http.server, json, urllib.request, urllib.error, sys, os, time, uuid
from pathlib import Path
from urllib.parse import urlsplit, unquote, parse_qsl, urlencode
import hashlib, io, re
from finance_gateway import FinanceGateway, request_json
from simulated_bank import BankError
from voice_gateway import VoiceGateway
from market_content import MarketStore
PORT = int(os.environ.get('NAVREN_PORT', os.environ.get('QINGZHOU_PORT', '8765')))
APP_PAGES = {'index.html', 'chat.html', 'recommend.html', 'account.html', 'mine.html', 'copilot.html', 'messages.html'}

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
RETRY_DELAY = 15
MAX_RETRIES = 3

class PublicationInterrupted(RuntimeError):
    """证据在部分发送后失效；与可忽略的普通客户端断开分开处理。"""


class PublicationSocket:
    """编码和取发送锁之后、真实写socket之前，再检查当前发布门。"""
    def __init__(self,sock,checkpoint,send_timeout=None):
        self.sock,self.checkpoint=sock,checkpoint
        self.send_timeout=send_timeout or (lambda:2.0)
        self.published=False
    def __getattr__(self,name):return getattr(self.sock,name)
    def sendall(self,data,*args,**kwargs):
        # 固定字节；大帧分段时跨到期即停止剩余传输。
        body=memoryview(bytes(data))
        for offset in range(0,len(body),65536):
            try:
                active=self.checkpoint()
                limit=self.send_timeout() if active else None
            except Exception:
                if self.published:
                    import socket
                    try:self.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    raise PublicationInterrupted('publication_invalid_after_partial_send') from None
                raise
            if active:self.published=True
            previous_timeout=None;restore_timeout=False
            if active and callable(getattr(self.sock,'gettimeout',None)):
                previous_timeout=self.sock.gettimeout()
                if previous_timeout is None or previous_timeout>limit:
                    self.sock.settimeout(limit);restore_timeout=True
            try:
                self.sock.sendall(body[offset:offset+65536],*args,**kwargs)
                if active:self.checkpoint()
            except Exception:
                if self.published:
                    import socket
                    try:self.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    raise PublicationInterrupted('publication_delivery_interrupted') from None
                raise
            finally:
                if restore_timeout:self.sock.settimeout(previous_timeout)

class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    finance_send_guard_contract='navren.finance-send-guard/1'
    def __init__(self,*args,**kwargs):
        kwargs['directory']=str(Path(__file__).resolve().parent)
        super().__init__(*args,**kwargs)

    def log_message(self, format, *args):
        # A revoked or closed access-log stream must not interrupt HTTP replies.
        # Financial audit records retain their independent persistent path.
        try:
            super().log_message(format, *args)
        except (OSError, ValueError):
            pass

    def setup(self):
        # 未缓冲wfile及WebSocket均使用这个socket；不改语音提供器或另建模型链。
        self.request=PublicationSocket(self.request,self.server.finance.assert_publication_current,
                                       self.server.finance.publication_send_timeout)
        super().setup()

    def end_headers(self):
        # HTML、JS、CSS 都必须重新取得；只在 HTML 中放 meta 无法修复旧缓存。
        self.send_header('Cache-Control', 'no-store, max-age=0, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def flush_headers(self):
        if hasattr(self,'_headers_buffer'):
            headers=self._headers_buffer
            # 原件回查可能在真正send前拒绝；缓冲区先清空，避免拼接第二个状态行。
            self._headers_buffer=[]
            self.wfile.write(b''.join(headers))

    def _history_failure(self,error):
        self._headers_buffer=[]
        if getattr(self.connection,'published',False):
            import socket
            self.close_connection=True
            try:self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            return
        if isinstance(error,BankError):
            self._finance_json(error.status,{'error':error.as_dict()})
        else:
            self._finance_json(503,{'error':{'code':'MARKET_HISTORY_UNAVAILABLE','message':'历史资料暂时无法核对。'}})
    def _finance_json(self, status, data, cookie=None):
        guard=getattr(self,'_navren_finance_send_guard',None)
        if status==200 and guard is not None:
            self._navren_finance_send_guard=None
            with guard(data):return self._finance_json_body(status,data,cookie)
        return self._finance_json_body(status,data,cookie)

    def _finance_json_body(self, status, data, cookie=None):
        # 客户端超时后的FIN有时不会让第一次send立即报错；检查已读完请求的连接，
        # 不消费任何字节。只有实际EOF/连接错误才交给既有断连回执审计。
        import select, socket
        if select.select([self.connection],[],[],0)[0] and self.connection.recv(1,socket.MSG_PEEK)==b'':
            raise BrokenPipeError('response_peer_closed')
        binding=None
        if status==200 and getattr(self,'_navren_workspace',None):
            token=getattr(self,'_navren_session_token',None)
            if token:self.server.finance.demo_sessions.validate(token,self._navren_workspace)
            from contextlib import closing
            from user_memory import privacy_revision
            with closing(self.server.finance.bank.connect()) as db:
                revision=privacy_revision(db,self._navren_workspace)
            if getattr(self,'_navren_start_revision',revision)!=revision:
                raise BankError('PRIVACY_REVISION_CHANGED','资料授权已变化，旧响应已失效，请重新核对。',409)
            binding={'workspace_id':self._navren_workspace,'privacy_revision':revision}
            if isinstance(data,dict):data={**data,**binding}
        body = json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
        self.server.finance.assert_publication_current()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('X-Content-Type-Options','nosniff')
        if binding:
            self.send_header('X-Navren-Workspace',binding['workspace_id'])
            self.send_header('X-Navren-Privacy-Revision',str(binding['privacy_revision']))
        if cookie: self.send_header('Set-Cookie',cookie)
        self.end_headers()
        self.wfile.write(body)

    def _dispatch_chat_media(self):
        path = urlsplit(self.path).path
        if path != '/api/finance/chat-media' and not path.startswith('/api/finance/chat-media/'):
            return False
        try:
            service = getattr(self.server.finance, 'chat_media', None)
            if service is None:
                raise BankError('CHAT_MEDIA_NOT_CONFIGURED', '聊天原件保存暂不可用。', 503)
            if path == '/api/finance/chat-media/context' and self.command == 'GET':
                service.context_http(self)
            elif path == '/api/finance/chat-media' and self.command == 'POST':
                # 独立原始二进制入口，保持普通 request_json 的 1MiB 上限。
                service.upload_http(self)
            elif path == '/api/finance/chat-media/bind' and self.command == 'POST':
                # 仅当前已认证主体主动迁移自己已有回合的原件，不改写原归档包。
                # 先验证身份/CSRF并有界读取，避免慢请求体持有银行写锁。
                self.server.finance.origin_check(self)
                _, token = self.server.finance.identity(self)
                self.server.finance.check_csrf(self, token)
                data = request_json(self)
                if (type(data) is not dict or set(data) != {'request_id', 'media_ids'}
                        or type(data['request_id']) is not str
                        or re.fullmatch(r'[A-Za-z0-9_-]{1,160}', data['request_id']) is None):
                    raise BankError('CHAT_MEDIA_BIND_INVALID', '旧原件关联参数无效。', 422)
                with service._http_scope(self) as (workspace, token, db, scope):
                    self.server.finance.check_csrf(self, token)
                    privacy = self.headers.get('X-Navren-Privacy-Revision', '')
                    epoch = self.headers.get('X-Navren-Archive-Epoch', '')
                    if (self.headers.get('X-Navren-Expected-Workspace') != workspace
                            or re.fullmatch(r'[0-9]{1,12}', privacy) is None
                            or re.fullmatch(r'[0-9]{1,12}', epoch) is None
                            or (int(privacy), int(epoch)) != (scope['privacy_revision'], scope['archive_epoch'])):
                        raise BankError('CHAT_MEDIA_STALE', '当前账户或资料权限已变化，请重新读取。', 409)
                    result = service.bind_existing_user(workspace, data['request_id'], data['media_ids'],
                        bank_db=db, expected_privacy=int(privacy), expected_epoch=int(epoch))
                    self._finance_json(200, result)
            elif re.fullmatch(r'/api/finance/chat-media/[0-9a-f]{64}', path) and self.command == 'GET':
                service.read_http(self, path.rsplit('/', 1)[1])
            else:
                raise BankError('CHAT_MEDIA_ROUTE_INVALID', '聊天原件地址或请求方式无效。', 404)
        except BankError as error:
            if getattr(self, '_navren_chat_media_response_started', False):
                # 二进制原件已开始传输时，只中止连接，禁止追加JSON或第二状态行。
                import socket
                self._headers_buffer = []
                self.close_connection = True
                try: self.connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            else:
                self._finance_json(error.status, {'error': error.as_dict()})
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if self._dispatch_chat_media(): return
        if path.startswith('/api/market/history/'):
            try:
                from urllib.parse import parse_qs
                from agent_runtime.history_binding import parse_route, read_exact, public_view, make_local_binding, history_publication_guard
                requested=parse_route(path,parse_qs(urlsplit(self.path).query,keep_blank_values=True))
                with self.server.market.db() as history_db:
                    history_db.execute('BEGIN')
                    packet=read_exact(self.server.market,requested,db=history_db)
                    binding=make_local_binding(self.server.market,packet,db=history_db) if packet['status']=='historical_readable' else None
                    view=public_view(packet)
                with history_publication_guard(self.server.market,packet,binding):
                    self._finance_json(200 if view['status']=='historical_readable' else 409,view)
            except BankError as error:
                self._history_failure(error)
            except Exception as error:
                self._history_failure(error)
            return
        if path.startswith('/api/market/'):
            from market_daily import MarketDailyError
            try:
                market=self.server.market
                from urllib.parse import parse_qs
                query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                if path.startswith('/api/market/hotspots'):data=market.route(path,query)
                elif path=='/api/market/status':data=market.status()
                elif path.startswith('/api/market/events/'):data=market.event(unquote(path.rsplit('/',1)[1]))
                elif path.startswith('/api/market/articles/'):data=market.article(unquote(path.rsplit('/',1)[1]))
                elif path.startswith('/api/market/weeklies/'):data=market.weekly(unquote(path.rsplit('/',1)[1]))
                else:raise KeyError('unknown_market_endpoint')
                if any(path.startswith('/api/market/'+kind+'/') for kind in ('events','articles','weeklies')):
                    if set(query)-{'version','revision'}:
                        raise BankError('MARKET_QUERY_INVALID','市场请求包含不支持的筛选条件。',400)
                    requested=[]
                    for key,values in query.items():
                        if len(values)!=1 or not re.fullmatch(r'[1-9][0-9]{0,8}',values[0]):
                            raise BankError('MARKET_QUERY_INVALID','市场版次必须是唯一的正整数。',400)
                        requested.append(int(values[0]))
                    if len(set(requested))>1:
                        raise BankError('MARKET_QUERY_INVALID','市场版本参数互相冲突。',400)
                    obj=data.get('report') if '/weeklies/' in path else data
                    if requested and (not obj or obj.get('version')!=requested[0]):
                        raise BankError('STALE_MARKET_EVIDENCE','所选市场版本已变化；本次不会替换为最新资料。',409)
                self._finance_json(200,data)
            except KeyError:self._finance_json(404,{'error':{'code':'MARKET_NOT_FOUND','message':'这条市场资料不存在或已撤下。'}})
            except BankError as error:self._finance_json(error.status,{'error':error.as_dict()})
            except MarketDailyError as error:self._finance_json(error.status,{'error':{'code':error.code,'message':'历史热点日期、版次或事件版本无法核对，请重新选择。'}})
            except Exception:self._finance_json(503,{'error':{'code':'MARKET_UNAVAILABLE','message':'市场缓存暂不可用。'}})
            return
        if path == '/api/speech/status':
            try:
                self.server.finance.origin_check(self)
                self._finance_json(200, self.server.voice.status())
            except BankError as error:
                self._finance_json(error.status, {'error': error.as_dict()})
            return
        if path == '/api/speech/ws':
            try:
                self.server.voice.socket(self)
            except BankError as error:
                self._finance_json(error.status, {'error': error.as_dict()})
            except Exception:
                self._finance_json(400, {'error': {'code': 'SPEECH_UPGRADE_FAILED', 'message': '语音连接无法建立。'}})
            return
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
            html=html.replace('</head>','<link rel="stylesheet" href="/css/site_workspace.css?v='+release+'"></head>',1)
            html=html.replace('</body>','<script src="/js/base_account_adapter.js?v='+release+'"></script></body>',1)
            body = html.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Navren-Release', release)
            self.end_headers()
            return io.BytesIO(body)
        return super().send_head()

    def do_POST(self):
        if self._dispatch_chat_media(): return
        if urlsplit(self.path).path == '/api/speech/session':
            try:
                self._finance_json(200, self.server.voice.session(self))
            except BankError as error:
                self._finance_json(error.status, {'error': error.as_dict()})
            return
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
        self.request.published=False
        """统一入口；身份与权限由服务端校验，纯社交输入走本地固定回复。"""
        stream = None
        request_guard = None
        workspace = request_id = None
        self._navren_workspace = self._navren_request_id = None
        try:
            data = request_json(self)
            allowed_fields={'text','mode','profile','history','followup_memory','holdings','image',
                            'page_context_id','market_context','graph_context','language','media_ids'}
            if not isinstance(data,dict) or set(data)-allowed_fields:
                raise BankError('AGENT_REQUEST_FIELDS_INVALID','请求包含不受支持的Agent配置字段。',422)

            text = data.get('text', '')
            mode = data.get('mode', 'classic')
            media_ids = data.get('media_ids', [])
            if (type(media_ids) is not list or len(media_ids) > 8
                    or any(type(mid) is not str or re.fullmatch(r'[0-9a-f]{64}', mid) is None for mid in media_ids)
                    or len(set(media_ids)) != len(media_ids)):
                raise BankError('CHAT_MEDIA_IDS_INVALID', '请重新选择有效的聊天附件。', 422)
            has_attachment = bool(media_ids or data.get('image'))

            if not isinstance(text,str) or len(text.strip())>6000 or (not text.strip() and data.get('page_context_id') is None and not media_ids):
                self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': '请输入问题或关联有效资料，问题不能超过6000字。'},ensure_ascii=False).encode('utf-8'))
                return

            from agent_runtime.request_privacy import RequestPrivacyPolicy
            request_policy=RequestPrivacyPolicy.from_text(text)

            gateway=self.server.finance
            gateway.origin_check(self)
            workspace,token=gateway.identity(self)
            self._navren_workspace=workspace
            gateway.check_csrf(self,token)
            request_guard=gateway.demo_sessions.request(token,workspace)
            request_guard.__enter__()
            page_context=None
            from contextlib import closing
            from user_memory import privacy_revision
            with closing(gateway.bank.connect()) as db:request_privacy=privacy_revision(db,workspace)
            request_id=uuid.uuid4().hex
            self._navren_request_id=request_id
            if 'text/event-stream' in self.headers.get('Accept',''):
                from agent_runtime.events import RuntimeEvents
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream; charset=utf-8')
                self.send_header('X-Accel-Buffering','no')
                self.send_header('Connection','close')
                self.end_headers()
                self.close_connection=True
                def write_event(body):
                    self.wfile.write(body)
                    self.wfile.flush()
                stream=RuntimeEvents(write_event,hashlib.sha256(workspace.encode()).hexdigest()[:16],request_id)
                stream.start()
            # 客户端画像/历史不能覆盖银行正式状态或注入伪造工具消息。
            options={'mode':mode,'has_attachment':has_attachment,'request_id':request_id}
            response_language=data.get('language','zh')
            if response_language not in ('zh','en'):raise BankError('INVALID_LANGUAGE','答复语言须为zh或en。',422)
            if getattr(gateway,'approved_responses',None) is not None:
                import inspect
                parameters=inspect.signature(self.server.conversation_agent.process).parameters
                if 'language' in parameters or any(p.kind==inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                    options['language']=response_language
                elif response_language!='zh':
                    raise BankError('AGENT_LANGUAGE_WIRING_REQUIRED','当前Agent尚未接入可信英文终结合同。',503)
            request_personal=gateway.personal_data.revision(workspace)
            from agent_runtime.social_reply import resolve_social_reply
            social_match=resolve_social_reply(text, language=response_language,
                has_attachment=has_attachment,
                has_selected_context=any(data.get(key) is not None for key in
                    ('page_context_id','market_context','graph_context')))
            if social_match is not None:
                self._archive_user_request(gateway,workspace,request_id,text,request_personal,
                    request_privacy,[],has_image=bool(data.get('image')),media_ids=media_ids)
                self._publish_local_social_reply(gateway,workspace,token,text,social_match,
                    request_id,request_privacy,request_personal,stream)
                return
            if data.get('page_context_id') is not None:
                if request_policy.private_context_disabled:
                    # Tickets and their references are server-owned and immutable
                    # after creation. Inspect their kind before any resolver can
                    # read an account/graph/calendar object; consume still checks
                    # ownership, privacy version, expiry and single-use status.
                    with gateway.page_contexts.lock:
                        reference=gateway.page_contexts._entry(workspace,token,data['page_context_id']).get('reference')
                        if reference and reference.get('kind') not in ('event','article','weekly'):
                            raise BankError('REQUEST_PRIVATE_DATA_FORBIDDEN','本轮已禁止读取个人资料，请移除个人资料引用后重新提问。',403)
                page_context=gateway.page_contexts.consume(workspace,token,data['page_context_id'],text)
            historical_context=None
            if page_context is not None:
                from agent_runtime.history_binding import HostHistoryContext, host_binding, requested_history
                if type(page_context) is HostHistoryContext:
                    host_binding(page_context,owner=gateway.page_contexts,workspace=workspace,market=gateway.market)
                    historical_context=page_context
                elif requested_history(page_context) or page_context.get('reading_mode')=='historical_audit':
                    raise BankError('HISTORY_CONTEXT_REQUIRED','历史引用必须来自本次已验证的页面票据。',403)
            if not text.strip() and not media_ids and (not page_context or page_context.get('request_mode')!='reference_overview'):
                raise BankError('INVALID_TEXT','请先输入问题或选择有效的关联资料。')
            if page_context:
                options['page_context']=page_context
                if page_context['kind'] in ('event','article','weekly'):
                    options['market_context']=page_context
                elif page_context['kind']=='graph':options['graph_context']=page_context['parameters']
            if data.get('market_context') is not None:
                context=data['market_context']
                from agent_runtime.history_binding import requested_history
                if requested_history(context) or (isinstance(context,dict) and
                        (context.get('reading_mode')=='historical_audit' or context.get('historical_only') is True)):
                    raise BankError('HISTORY_CONTEXT_REQUIRED','调用方历史标记不能赋予历史资料权限。',403)
                if not isinstance(context,dict) or set(context)!={'kind','id'} or context['kind'] not in ('event','article','weekly') or not isinstance(context['id'],str) or len(context['id'])>128:
                    raise BankError('MARKET_CONTEXT_INVALID','市场提问对象不合法。')
                if historical_context is not None:
                    if (context['kind'],context['id'])!=(historical_context['kind'],historical_context['id']):
                        raise BankError('MARKET_CONTEXT_CONFLICT','市场对象与选定历史票据不一致。',409)
                else:
                    try:
                        obj={'event':self.server.market.event,'article':self.server.market.article,'weekly':self.server.market.weekly}[context['kind']](context['id'])
                        if context['kind']=='weekly' and obj['report'] is None:raise KeyError('weekly_not_found')
                    except KeyError:raise BankError('MARKET_NOT_FOUND','市场提问对象不存在或已撤下。',404)
                    if page_context and page_context['kind'] in ('event','article','weekly'):
                        if (context['kind'],context['id'])!=(page_context['kind'],page_context['id']):
                            raise BankError('MARKET_CONTEXT_CONFLICT','市场对象与当前页面引用不一致。',409)
                    else:options['market_context']=gateway.page_contexts.resolve(workspace,context)
            if stream:options.update(on_event=stream.progress,request_id=request_id)
            selected_graph=None
            if data.get('graph_context') is not None:
                if historical_context is not None:
                    raise BankError('HISTORY_PRIVATE_CONTEXT_CONFLICT','历史复核不能同时读取个人图谱，请另开本人的当前资料问题。',403)
                request_policy.require_tool('personal_graph')
                gc=data['graph_context']
                if not isinstance(gc,dict) or set(gc)-{'node_id','insight_id','edge_id','view','from','to','source'}:
                    raise BankError('GRAPH_CONTEXT_INVALID','图谱对象不合法。')
                graph_result=gateway.financial_graph.query(workspace,gc,for_agent=True)
                selected_graph={'kind':'graph','id':gc.get('node_id') or gc.get('edge_id') or gc.get('insight_id') or 'overview',
                    'source_scope':gc.get('source','simulation'),'version':graph_result['version'],'parameters':gc}
                options['graph_context']=gc
            # 范围由宿主在票据/图谱对象已核验后推导；前端不能提交权限。
            request_policy=RequestPrivacyPolicy.for_turn(text,page_context=page_context,
                graph_context=options.get('graph_context'))
            if historical_context is not None and not request_policy.private_context_disabled:
                request_policy=RequestPrivacyPolicy(True,version=request_policy.version,scope_reason='historical_audit')
            # 先核验并消费宿主引用，再归档。空原话的合法资料概览保留真实结构，
            # 不伪造用户问题；原件由受信媒体ID在同一私库事务内绑定。
            archived_user = self._archive_user_request(gateway,workspace,request_id,text,request_personal,
                request_privacy,[page_context,options.get('market_context'),selected_graph],
                has_image=bool(data.get('image')),media_ids=media_ids)
            if not text.strip() and media_ids and not any((page_context, options.get('market_context'), selected_graph)):
                # 接收原始图片/无转写录音不等于识别内容，也不触发模型或伪造助手审核。
                result = {'bank': True, 'bank_response_id': request_id, 'workspace_id': workspace,
                          'privacy_revision': request_privacy, 'personal_revision': request_personal,
                          'reply': '附件已保存。你可以继续补充希望了解的问题。',
                          'attachment_receipt': {'status': 'archived', 'request_id': request_id},
                          'user_attachments': archived_user.get('attachments', []),
                          'isFallback': True}
                with gateway.publication_guard(workspace, request_privacy, request_personal):
                    gateway.demo_sessions.validate(token, workspace)
                    if stream: stream.finish(result)
                    else: self._finance_json(200, result)
                return
            # 同一现有研究组合服务先完成只读投影缓存，再固定输入版本。
            # BankTools同样会调用provision；不能把首次缓存创建误判成用户资料变更。
            if request_policy.private_context_disabled:
                request_metrics=None
                memory_enabled=False
            else:
                gateway.wealth.provision(workspace)
                request_metrics=gateway.financial_metrics.snapshot(workspace,60)
                memory_enabled=gateway.memories.state(workspace)['enabled']
            if memory_enabled and text.strip():
                gateway.personal_data.register_conversation_source(workspace,request_id,text,expected_revision=request_personal)
            from copy import copy,deepcopy
            from base_runtime.evidence import PublicEvidenceObserver, MetricEvidenceObserver, EvidenceGuardedClient
            from base_runtime.tool_scope import ScopedEvidenceClient, statement_tool_scope, statement_month_context
            request_gateway=copy(gateway)
            # 冷补充在首个云请求前完成；每轮固定快照，不改Agent检索中的版本。
            from local_knowledge.runtime import RoundProductCatalog
            prepared=self.server.evidence_runtime.prepare_round(text)
            public_observer=PublicEvidenceObserver(prepared.provider)
            request_gateway.knowledge_provider=public_observer
            request_gateway.public_products=RoundProductCatalog(public_observer,prepared.provider.records)
            # 独立宿主选源；HTTP JSON、模型正文和工具参数不能安装或发行来源。
            catalog_information=gateway.catalog_information_host.pin(request_id=request_id,
                workspace=workspace,privacy_revision=request_privacy,personal_revision=request_personal,
                public_only=request_policy.private_context_disabled)
            if catalog_information is not None:
                from local_knowledge.catalog_information import ScopedProductCatalog, CatalogInformationEvidenceObserver
                prepared=type(prepared)(prepared.provider,{**deepcopy(prepared.report),
                    'catalog_information':catalog_information.snapshot()})
                request_gateway.catalog_information_observer=catalog_information
                request_gateway.public_products=ScopedProductCatalog(request_gateway.public_products,catalog_information)
                public_observer=CatalogInformationEvidenceObserver(public_observer,catalog_information)
                request_gateway.knowledge_provider=public_observer
            metric_observer=MetricEvidenceObserver(gateway.financial_metrics)
            request_gateway.financial_metrics=metric_observer
            request_agent=copy(self.server.conversation_agent)
            statement_history=[] if request_policy.private_context_disabled else gateway.personal_data.read_history(workspace)
            tool_scope=request_policy.allowed_tools if request_policy.private_context_disabled else statement_tool_scope(text,has_attachment=has_attachment,
                has_context=any(value is not None for value in
                    (page_context,data.get('market_context'),data.get('graph_context'))),
                history=statement_history)
            with closing(gateway.bank.connect()) as statement_db:
                statement_as_of=gateway.bank._workspace(statement_db,workspace)['as_of']
            statement_month=statement_month_context(text,statement_history,statement_as_of) if tool_scope and not request_policy.private_context_disabled else None
            for client_name in ('primary','reviewer'):
                client=getattr(request_agent,client_name,None)
                if client is not None:
                    guarded=(ScopedEvidenceClient(client,public_observer,prepared.report,
                        allowed_tools=tool_scope) if client_name=='primary' else
                        EvidenceGuardedClient(client,public_observer,prepared.report))
                    if client_name=='primary' and catalog_information is not None:
                        guarded.append_notice('\n本轮宿主选定了历史或合成目录，只用于资料研究。先调用knowledge_status，再调用public_products(view="historical_conditions")；查询参数留空以读取完整选定资料。工具返回的conditions_draft来自实际目录、检索状态和用款期限，可原样作为answer_reviewed正文并引用本轮证据，不据此声称当前费用、在售或执行资格。')
                    if client_name=='primary' and not request_policy.private_context_disabled:
                        # 后端已接通的读取能力属于服务端事实；不能把尚未调用说成没有数据。
                        guarded.append_notice('\n本机已接通当前登录主体的财务读取工具。'+(
                            '询问某月钱花在哪、收入支出或消费分类，先调用monthly_statement(month=YYYY-MM)，'
                            '按本轮as_of解析未指定的年份，再根据返回的分类、facts和metrics回答。'
                            '没有调用对应工具前，不得声称没有账单、要求用户上传已有账单，或让用户换一种问法再查。'
                            '只有工具明确返回无记录或不可用时才说明缺项。'
                            '追问某项分几笔、具体日期或明细，调用statement_details，category使用本人所问的分类，'
                            '笔数和合计用返回的transaction_count与total_minor，不以页长或总额猜测笔数。'
                            '独立历史库的银行账户、银行卡、保险、持仓和资产可用financial_*工具读取；'
                            '历史合成资料与现行模拟操作分开，不把历史估值当可执行余额。'))
                        if statement_month:
                            guarded.append_notice('\n本轮本人账单问句的月份上下文：'+statement_month+'。沿用月份只为读取；必须重新查工具取得本轮证据。')
                    setattr(request_agent,client_name,guarded)
            def finalize_result(result,commit_history=None):
                history_attempted=False
                failure_stage='finalization'
                try:
                    result['financial_metric_batches']=metric_observer.batches
                    result['knowledge_snapshot']=public_observer.snapshot(result.get('knowledge_snapshot'))
                    result['knowledge_preparation']=prepared.report
                    gateway.demo_sessions.validate(token,workspace)
                    if request_metrics is not None:gateway.financial_metrics.validate(workspace,request_metrics)
                    result['financial_metric_snapshot']=request_metrics
                    from contextlib import closing
                    from user_memory import privacy_revision
                    with closing(gateway.bank.connect()) as db:
                        revision=privacy_revision(db,workspace)
                        if revision!=request_privacy:raise BankError('PRIVACY_REVISION_CHANGED','资料授权已变化，旧答复已丢弃。',409)
                        if gateway.personal_data.revision(workspace)!=request_personal:
                            raise BankError('PERSONAL_VERSION_CONFLICT','个人资料已变化，旧答复已丢弃。',409)
                        result.update(workspace_id=workspace,privacy_revision=revision,personal_revision=request_personal)
                    result['memory_proposals']=gateway.personal_data.propose_candidates(workspace,text,request_id,
                        expected_revision=request_personal) if memory_enabled and text.strip() and not result.get('isFallback') else []
                    # 切断模型/工具/Observer持有的嵌套可变别名；Agent历史回调仍
                    # 指向这个同一dict，正文、组件和最终绑定保持一致。
                    from copy import deepcopy
                    frozen=deepcopy(result)
                    result.clear();result.update(frozen)
                    receipt=gateway.approve_speech(workspace,result,token)
                    if receipt:result['speech_receipt']=receipt
                    result.pop('_approved_tts_text',None)
                    expected_context=result.pop('_answer_context',None)
                    failure_stage='binding'
                    with gateway.response_guard(workspace,request_privacy,request_personal,result.get('knowledge_snapshot'),expected_context,
                                                market_snapshot=result.get('market_snapshot'),metric_snapshot=request_metrics,
                                                service_snapshot=result.get('service_snapshot'),
                                                metric_batches=metric_observer.batches,
                                                catalog_information=catalog_information.snapshot() if catalog_information is not None else None) as guarded_db:
                        gateway.demo_sessions.validate(token,workspace)
                        if request_metrics is not None:gateway.financial_metrics.validate(workspace,request_metrics)
                        gateway.operation_evidence.bind_answer(workspace,result,guarded_db)
                    # 绑定事务先成功提交。第二阶段重新按bank→market→knowledge→rules
                    # 取得只读发布锁并复验，避免commit失败前发送成功答复。
                    def save_history(bank_db):
                        writer=getattr(gateway.personal_data,'save_turn_guarded',None)
                        if not callable(writer):
                            raise BankError('HISTORY_FINALIZATION_UNAVAILABLE','对话保存的最终校验接口尚未接入。',503)
                        def guarded_writer(*args,**kwargs):
                            nonlocal history_attempted
                            # 私库可能commit后再抛异常；从实际调用前标记，撤回只做本轮CAS。
                            history_attempted=True
                            return writer(*args,bank_db=bank_db,**kwargs)
                        approved=getattr(gateway,'approved_responses',None)
                        selected_writer=guarded_writer
                        if approved is not None and result.get('agent_review',{}).get('status')=='approved' and not result.get('isFallback'):
                            sealer=getattr(commit_history,'seal_final_response',None)
                            if not callable(sealer):
                                raise BankError('AGENT_FINAL_TRANSPORT_WIRING_REQUIRED','Agent尚未接入原发布事务的最终封存。',503)
                            sealer(bank_db=bank_db,language=response_language,server_gateway=gateway,session_token=token)
                            selected_writer=approved.guarded_writer(workspace,result,guarded_writer,bank_db=bank_db,language=response_language)
                        if approved is not None:
                            commit_history(writer=selected_writer,bank_db=bank_db,server_gateway=gateway,session_token=token)
                        else:commit_history(writer=selected_writer)
                    failure_stage='publication'
                    with gateway.publication_guard(workspace,request_privacy,request_personal,result.get('knowledge_snapshot'),expected_context,
                            market_snapshot=result.get('market_snapshot'),metric_snapshot=request_metrics,
                            service_snapshot=result.get('service_snapshot'),metric_batches=metric_observer.batches,
                            before_send=save_history if commit_history is not None else None,
                            budget_check=commit_history.assert_budget if commit_history is not None else None,
                            budget_remaining=commit_history.remaining_seconds if commit_history is not None else None,
                            catalog_information=catalog_information.snapshot() if catalog_information is not None else None) as publication_db:
                        gateway.demo_sessions.validate(token,workspace)
                        if stream:
                            stream.finish(result)
                            if not stream.connected:raise PublicationInterrupted('publication_client_disconnected')
                        else:self._finance_json(200,result)
                        approved=getattr(gateway,'approved_responses',None)
                        if approved is not None and history_attempted and result.get('agent_review',{}).get('status')=='approved' and not result.get('isFallback'):
                            approved.published(workspace,request_id,bank_db=publication_db)
                            archive=getattr(gateway,'dialogue_archive',None)
                            if archive is not None:archive.attest_published(workspace,request_id,bank_db=publication_db)
                    return result
                except Exception as error:
                    try:
                        gateway.discard_unpublished_answer(workspace,request_id,result,
                            expected_privacy=request_privacy,expected_personal=request_personal,history_saved=history_attempted,
                            failure_stage=failure_stage,failure_code=error.code if isinstance(error,BankError) else 'PUBLICATION_FAILED')
                    except Exception as cleanup_error:
                        # 清理异常不能把部分发送改成第二个结果；只记固定类型，不记正文或异常原文。
                        import logging
                        logging.getLogger(__name__).error('未发布答复清理未完成，阶段=%s，类型=%s',failure_stage,type(cleanup_error).__name__)
                    raise
            from agent_runtime.conversation import ConversationAgent
            if isinstance(request_agent,ConversationAgent):
                if getattr(gateway,'approved_responses',None) is not None:
                    import inspect
                    parameters=inspect.signature(request_agent.process).parameters
                    if ('publication_sources' not in parameters
                            and not any(p.kind==inspect.Parameter.VAR_KEYWORD for p in parameters.values())):
                        raise BankError('AGENT_FINAL_TRANSPORT_WIRING_REQUIRED','Agent尚未接入本轮实际发布来源读取。',503)
                    options['publication_sources']=lambda:deepcopy({'knowledge_preparation':prepared.report,
                                                                   'financial_metric_snapshot':request_metrics})
                options['finalize_result']=finalize_result
                request_agent.process(request_gateway,workspace,text,**options)
            else:
                # 冻结旧关键词Agent仅供本地行为对照，其接口没有历史提交回调。
                finalize_result(request_agent.process(request_gateway,workspace,text,**options))

        except BankError as e:
            self._audit_agent_rejection(workspace,request_id,e)
            if self.request.published:
                self.close_connection=True
                return
            # 若拒绝发生在headers flush前，清掉尚未发送的旧200头。
            self._headers_buffer=[]
            result={'error':e.as_dict(),'bank':True,'reply':e.message+' 本次没有确认执行任何操作。','isFallback':True}
            if stream:stream.finish(result)
            else:self._finance_json(e.status,result)
        except (PublicationInterrupted,BrokenPipeError,ConnectionResetError):
            # 已发送部分帧后失效，关闭连接；不能追加另一份HTTP或续播旧内容。
            self.close_connection=True
        except Exception as error:
            self._audit_agent_rejection(workspace,request_id,error)
            # 保留可定位的服务端异常；不记录问题正文、密钥或异常消息。
            import traceback
            frames = [{'file':Path(frame.filename).name, 'line':frame.lineno,
                       'function':frame.name}
                      for frame in traceback.extract_tb(error.__traceback__)]
            self.log_error('agent failure %s', json.dumps({
                'request_id':locals().get('request_id'),
                'exception_type':type(error).__name__, 'frames':frames}, ensure_ascii=False))
            if self.request.published:
                self.close_connection=True
                return
            result={'bank':True,'error':{'code':'AGENT_UNAVAILABLE','message':'服务暂不可用。'},
                'reply':'服务暂不可用。请在账户页查询订单核实状态，本次对话没有确认执行操作。','isFallback':True}
            if stream:stream.finish(result)
            else:self._finance_json(500,result)
        finally:
            if request_guard:request_guard.__exit__(None,None,None)

    def _audit_agent_rejection(self,workspace,request_id,error):
        if workspace is None:return
        from agent_runtime.bank_tools import _host_security_rejection
        event_id=('agent:http' if request_id is not None else 'agent:http:'+uuid.uuid4().hex)
        _host_security_rejection(self.server.finance,workspace,error,
            request_id=request_id,event_id=event_id,phase='http')

    def _archive_user_request(self,gateway,workspace,request_id,text,personal,privacy,contexts,*,has_image,media_ids=None):
        archive=getattr(gateway,'dialogue_archive',None)
        if archive is None:
            if media_ids: raise BankError('DIALOGUE_ARCHIVE_STORAGE_REQUIRED','当前会话尚未接入原件归档。',503)
            return {}
        from copy import deepcopy
        from personal_data import ARCHIVE_REFERENCE_LABELS,ARCHIVE_REFERENCE_PARAMETERS
        references=[]
        for context in contexts:
            if context is None:continue
            kind=context['kind'];ident=context['id']
            if kind not in ARCHIVE_REFERENCE_LABELS:
                raise BankError('DIALOGUE_REFERENCE_UNSUPPORTED','资料引用暂不支持完整聊天归档。',422)
            parameters={k:deepcopy(v) for k,v in context.get('parameters',{}).items()
                        if k in ARCHIVE_REFERENCE_PARAMETERS.get(kind,())}
            reference={'contract':'navren.archived-reference/1','kind':kind,'id':ident,
                'source_scope':context['source_scope'],'version':context['version'],
                'label':ARCHIVE_REFERENCE_LABELS[kind]+' · '+ident,'parameters':parameters,
                'historical_readonly':True,'current_fact_eligible':False,'execution_authority':False}
            if reference not in references:references.append(reference)
        return archive.archive_user_turn(workspace,request_id,text,references=references,media_ids=media_ids,
            attachments=[{'kind':'image','storage_status':'not_archived'}] if has_image else [],
            expected_personal_revision=personal,expected_privacy_revision=privacy)

    def _publish_local_social_reply(self, gateway, workspace, token, text, match,
                                    request_id, request_privacy, request_personal, stream):
        """本地固定社交文本：保留身份、版本与历史发布守卫，不准备金融证据。"""
        from agent_runtime.social_reply import build_social_reply_result, validate_social_reply_result
        agent = self.server.conversation_agent
        lock = agent._locks[int(hashlib.sha256(workspace.encode()).hexdigest(),16) % len(agent._locks)]
        if not lock.acquire(blocking=False):
            raise BankError('AGENT_BUSY','这段会话正在处理上一项请求，请稍后再发。',409)
        result = None
        local_speech_receipt = None
        history_attempted = False
        try:
            result = build_social_reply_result(workspace, request_id, match)
            validate_social_reply_result(result)
            result.update(privacy_revision=request_privacy, personal_revision=request_personal)
            # 只为保存当前回合读取历史；历史不参与问候匹配或答复生成。
            history = gateway.personal_data.read_history(workspace)
            messages = (history + [{'role':'user','content':text.strip()},
                                   {'role':'assistant','content':result['reply']}])[-16:]
            with gateway.response_guard(workspace, request_privacy, request_personal) as bank_db:
                gateway.demo_sessions.validate(token, workspace)
                gateway.bank._audit(bank_db, workspace, 'agent_social_reply_prepared',
                    {'request_id':request_id, 'rule_id':match['rule_id'],
                     'model_calls':0, 'tool_calls':0, 'review_calls':0})
            # 独立申请只读朗读凭据必须在 publication_guard 的银行锁外，
            # 之后仍由原保存/发布门控制交付，失败路径撤回该凭据。
            local_speech_receipt = gateway.approve_speech(workspace,
                {**result, 'compliance_pass': True}, token)
            wire_result = {**result, **({'speech_receipt': local_speech_receipt} if local_speech_receipt else {})}
            def save_history(bank_db):
                nonlocal history_attempted
                gateway.demo_sessions.validate(token, workspace)
                history_attempted = True
                widgets=[]
                if getattr(gateway,'dialogue_archive',None) is not None:
                    approved=getattr(gateway,'approved_responses',None)
                    if approved is None:
                        raise BankError('LOCAL_PUBLICATION_UNAVAILABLE','本地回复归档尚未接入。',503)
                    widgets.append(approved.prepare_local_rule(workspace,result,bank_db=bank_db))
                if gateway.personal_data.save_turn_guarded(workspace, messages, request_id, widgets,
                        bank_db=bank_db, expected_revision=request_personal,
                        expected_privacy=request_privacy) is not True:
                    raise BankError('STALE_EVIDENCE','资料刚刚发生变化，本轮对话未保存。',409)
            with gateway.publication_guard(workspace, request_privacy, request_personal,
                                            before_send=save_history) as publication_db:
                gateway.demo_sessions.validate(token, workspace)
                gateway.assert_publication_current()
                if stream:
                    stream.progress({'kind':'activity', 'call_id':'local-social-reply',
                        'activity_kind':'reply', 'name':'social_reply', 'label':'本地问候回复',
                        'status':'completed', 'summary':{'source':'local_rule'}})
                    stream.finish(wire_result)
                    if not stream.connected:
                        raise PublicationInterrupted('publication_client_disconnected')
                else:
                    self._finance_json(200, wire_result)
                if getattr(gateway,'dialogue_archive',None) is not None:
                    gateway.approved_responses.published(workspace,request_id,bank_db=publication_db)
                    gateway.dialogue_archive.attest_published(workspace,request_id,bank_db=publication_db)
            return result
        except Exception as error:
            if local_speech_receipt:
                with gateway._speech_lock:
                    gateway._speech_receipts.pop(local_speech_receipt, None)
            if history_attempted and result is not None:
                gateway.discard_unpublished_answer(workspace, request_id, result,
                    expected_privacy=request_privacy, expected_personal=request_personal,
                    history_saved=True, failure_stage='publication',
                    failure_code=error.code if isinstance(error,BankError) else 'PUBLICATION_FAILED')
            raise
        finally:
            lock.release()

    def _handle_chat(self):
        self._finance_json(410, {'error': {'code':'LEGACY_CHAT_RETIRED',
            'message':'旧对话代理已停用，请刷新页面使用统一 Agent。'}})

    def _handle_tts(self):
        self._finance_json(410, {'error': {'code': 'SPEECH_ENDPOINT_MOVED',
            'message': '语音已升级为流式服务，请刷新页面后重试。'}})

    def do_OPTIONS(self):
        if urlsplit(self.path).path.startswith(('/api/finance', '/api/speech', '/api/tts')):
            self._finance_json(403,{'error':{'code':'CROSS_ORIGIN_DENIED','message':'银行接口不开放跨站访问。'}})
            return
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()

class NavrenHTTPServer(http.server.ThreadingHTTPServer):
    def server_close(self):
        finance=getattr(self,'finance',None)
        if finance is not None:finance.stop_public_sync(timeout=2)
        runtime=getattr(self,'evidence_runtime',None)
        if runtime is not None:runtime.close()
        super().server_close()


def create_server(port=PORT, database=None, voice=None, model_agents=None, conversation_agent=None,
                  evidence_runtime=None, market_database=None, public_catalog=None, public_sync_enabled=None,
                  agent_route_provider=None,finance_factory=None):
    if conversation_agent is not None:
        if agent_route_provider is not None:
            raise ValueError('agent_injection_and_route_provider_conflict')
        selected_agent=conversation_agent
    else:
        from agent_runtime.agent_factory import create_agent
        selected_agent=create_agent(route_provider='off' if agent_route_provider is None else agent_route_provider)
    server=NavrenHTTPServer(('127.0.0.1',port),ProxyHandler)
    try:
        server.app_release=app_release()
        if finance_factory is None:
            from base_runtime.service_assembly import AssembledGateway
            finance_factory=AssembledGateway
        server.finance=finance_factory(database,public_catalog=public_catalog)
        from base_runtime.chat_media import ChatMediaService
        server.finance.chat_media = ChatMediaService(server.finance)
        # 显式数据库用于隔离验收；不得把开发验收资料写入现役市场缓存。
        server.market=MarketStore(market_database if market_database is not None else
            (Path(database).with_name(Path(database).stem+'.market.sqlite3') if database else None))
        server.finance.market=server.market
        server.knowledge_import=server.finance.knowledge_provider.import_market_store(server.market,publish=True)
        from local_knowledge.runtime import EvidenceRuntime
        server.evidence_runtime=evidence_runtime or EvidenceRuntime(server.finance.knowledge_provider)
        server.voice=voice or VoiceGateway()
        server.agent_models=model_agents  # 仅旧行为对照测试可注入；生产对话不使用。
        server.conversation_agent=selected_agent
        approved=getattr(server.finance,'approved_responses',None)
        if approved is not None and callable(getattr(selected_agent,'validate_final_response',None)):
            approved.validator=selected_agent.validate_final_response
        server.finance.start_public_sync(enabled=public_sync_enabled)
    except BaseException:
        server.server_close()
        raise
    return server


if __name__ == '__main__':
    from scripts.serve_financial_frontend import main
    main()
