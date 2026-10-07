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
    def __init__(self,*args,**kwargs):
        kwargs['directory']=str(Path(__file__).resolve().parent)
        super().__init__(*args,**kwargs)

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
    def _finance_json(self, status, data, cookie=None):
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

    def do_GET(self):
        path = urlsplit(self.path).path
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
        try:
            data = request_json(self)

            text = data.get('text', '')
            mode = data.get('mode', 'classic')

            if not isinstance(text,str) or not text.strip():
                self.send_response(400)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'text is required'}).encode())
                return

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
            options={'mode':mode,'has_attachment':bool(data.get('image')),'request_id':request_id}
            response_language=data.get('language','zh')
            if response_language not in ('zh','en'):raise BankError('INVALID_LANGUAGE','答复语言须为zh或en。',422)
            request_personal=gateway.personal_data.revision(workspace)
            from agent_runtime.social_reply import resolve_social_reply
            social_match=resolve_social_reply(text, language=response_language,
                has_attachment=bool(data.get('image')),
                has_selected_context=any(data.get(key) is not None for key in
                    ('page_context_id','market_context','graph_context')))
            if social_match is not None:
                self._publish_local_social_reply(gateway,workspace,token,text,social_match,
                    request_id,request_privacy,request_personal,stream)
                return
            if data.get('page_context_id') is not None:
                page_context=gateway.page_contexts.consume(workspace,token,data['page_context_id'],text)
            # 同一现有研究组合服务先完成只读投影缓存，再固定输入版本。
            # BankTools同样会调用provision；不能把首次缓存创建误判成用户资料变更。
            gateway.wealth.provision(workspace)
            request_metrics=gateway.financial_metrics.snapshot(workspace,60)
            memory_enabled=gateway.memories.state(workspace)['enabled']
            if memory_enabled:
                gateway.personal_data.register_conversation_source(workspace,request_id,text,expected_revision=request_personal)
            if page_context:
                options['page_context']=page_context
                if page_context['kind'] in ('event','article','weekly'):
                    options['market_context']=page_context
                elif page_context['kind']=='graph':options['graph_context']=page_context['parameters']
            if data.get('market_context') is not None:
                context=data['market_context']
                if not isinstance(context,dict) or set(context)!={'kind','id'} or context['kind'] not in ('event','article','weekly') or not isinstance(context['id'],str) or len(context['id'])>128:
                    raise BankError('MARKET_CONTEXT_INVALID','市场提问对象不合法。')
                try:
                    obj={'event':self.server.market.event,'article':self.server.market.article,'weekly':self.server.market.weekly}[context['kind']](context['id'])
                    if context['kind']=='weekly' and obj['report'] is None:raise KeyError('weekly_not_found')
                except KeyError:raise BankError('MARKET_NOT_FOUND','市场提问对象不存在或已撤下。',404)
                if page_context and page_context['kind'] in ('event','article','weekly'):
                    if (context['kind'],context['id'])!=(page_context['kind'],page_context['id']):
                        raise BankError('MARKET_CONTEXT_CONFLICT','市场对象与当前页面引用不一致。',409)
                else:options['market_context']=gateway.page_contexts.resolve(workspace,context)
            if stream:options.update(on_event=stream.progress,request_id=request_id)
            if data.get('graph_context') is not None:
                gc=data['graph_context']
                if not isinstance(gc,dict) or set(gc)-{'node_id','insight_id','edge_id','view','from','to','source'}:
                    raise BankError('GRAPH_CONTEXT_INVALID','图谱对象不合法。')
                gateway.financial_graph.query(workspace,gc,for_agent=True)
                options['graph_context']=gc
            from copy import copy
            from base_runtime.evidence import PublicEvidenceObserver, MetricEvidenceObserver, EvidenceGuardedClient
            from base_runtime.tool_scope import ScopedEvidenceClient, statement_tool_scope
            request_gateway=copy(gateway)
            # 冷补充在首个云请求前完成；每轮固定快照，不改Agent检索中的版本。
            from local_knowledge.runtime import RoundProductCatalog
            prepared=self.server.evidence_runtime.prepare_round(text)
            public_observer=PublicEvidenceObserver(prepared.provider)
            request_gateway.knowledge_provider=public_observer
            request_gateway.public_products=RoundProductCatalog(public_observer,prepared.provider.records)
            metric_observer=MetricEvidenceObserver(gateway.financial_metrics)
            request_gateway.financial_metrics=metric_observer
            request_agent=copy(self.server.conversation_agent)
            tool_scope=statement_tool_scope(text,has_attachment=bool(data.get('image')),
                has_context=any(value is not None for value in
                    (page_context,data.get('market_context'),data.get('graph_context'))))
            for client_name in ('primary','reviewer'):
                client=getattr(request_agent,client_name,None)
                if client is not None:
                    guarded=(ScopedEvidenceClient(client,public_observer,prepared.report,
                        allowed_tools=tool_scope) if client_name=='primary' else
                        EvidenceGuardedClient(client,public_observer,prepared.report))
                    if client_name=='primary':
                        # 后端已接通的读取能力属于服务端事实；不能把尚未调用说成没有数据。
                        guarded.notice=(guarded.notice or '')+'\n本机已接通当前登录主体的财务读取工具。'+(
                            '询问某月钱花在哪、收入支出或消费分类，先调用monthly_statement(month=YYYY-MM)，'
                            '按本轮as_of解析未指定的年份，再根据返回的分类、facts和metrics回答。'
                            '没有调用对应工具前，不得声称没有账单、要求用户上传已有账单，或让用户换一种问法再查。'
                            '只有工具明确返回无记录或不可用时才说明缺项。'
                            '独立历史库的银行账户、银行卡、保险、持仓和资产可用financial_*工具读取；'
                            '历史合成资料与现行模拟操作分开，不把历史估值当可执行余额。')
                    setattr(request_agent,client_name,guarded)
            def finalize_result(result,commit_history=None):
                history_attempted=False
                failure_stage='finalization'
                try:
                    result['financial_metric_batches']=metric_observer.batches
                    result['knowledge_snapshot']=public_observer.snapshot(result.get('knowledge_snapshot'))
                    result['knowledge_preparation']=prepared.report
                    gateway.demo_sessions.validate(token,workspace)
                    gateway.financial_metrics.validate(workspace,request_metrics)
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
                        expected_revision=request_personal) if memory_enabled and not result.get('isFallback') else []
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
                                                metric_batches=metric_observer.batches) as guarded_db:
                        gateway.demo_sessions.validate(token,workspace)
                        gateway.financial_metrics.validate(workspace,request_metrics)
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
                        commit_history(writer=guarded_writer)
                    failure_stage='publication'
                    with gateway.publication_guard(workspace,request_privacy,request_personal,result.get('knowledge_snapshot'),expected_context,
                            market_snapshot=result.get('market_snapshot'),metric_snapshot=request_metrics,
                            service_snapshot=result.get('service_snapshot'),metric_batches=metric_observer.batches,
                            before_send=save_history if commit_history is not None else None,
                            budget_check=commit_history.assert_budget if commit_history is not None else None,
                            budget_remaining=commit_history.remaining_seconds if commit_history is not None else None):
                        gateway.demo_sessions.validate(token,workspace)
                        if stream:
                            stream.finish(result)
                            if not stream.connected:raise PublicationInterrupted('publication_client_disconnected')
                        else:self._finance_json(200,result)
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
                options['finalize_result']=finalize_result
                request_agent.process(request_gateway,workspace,text,**options)
            else:
                # 冻结旧关键词Agent仅供本地行为对照，其接口没有历史提交回调。
                finalize_result(request_agent.process(request_gateway,workspace,text,**options))

        except BankError as e:
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

    def _publish_local_social_reply(self, gateway, workspace, token, text, match,
                                    request_id, request_privacy, request_personal, stream):
        """本地固定社交文本：保留身份、版本与历史发布守卫，不准备金融证据。"""
        from agent_runtime.social_reply import build_social_reply_result, validate_social_reply_result
        agent = self.server.conversation_agent
        lock = agent._locks[int(hashlib.sha256(workspace.encode()).hexdigest(),16) % len(agent._locks)]
        if not lock.acquire(blocking=False):
            raise BankError('AGENT_BUSY','这段会话正在处理上一项请求，请稍后再发。',409)
        result = None
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
            def save_history(bank_db):
                nonlocal history_attempted
                gateway.demo_sessions.validate(token, workspace)
                history_attempted = True
                if gateway.personal_data.save_turn_guarded(workspace, messages, request_id, [],
                        bank_db=bank_db, expected_revision=request_personal,
                        expected_privacy=request_privacy) is not True:
                    raise BankError('STALE_EVIDENCE','资料刚刚发生变化，本轮对话未保存。',409)
            with gateway.publication_guard(workspace, request_privacy, request_personal,
                                            before_send=save_history):
                gateway.demo_sessions.validate(token, workspace)
                gateway.assert_publication_current()
                if stream:
                    stream.progress({'kind':'activity', 'call_id':'local-social-reply',
                        'activity_kind':'reply', 'name':'social_reply', 'label':'本地问候回复',
                        'status':'completed', 'summary':{'source':'local_rule'}})
                    stream.finish(result)
                    if not stream.connected:
                        raise PublicationInterrupted('publication_client_disconnected')
                else:
                    self._finance_json(200, result)
            return result
        except Exception as error:
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
        runtime=getattr(self,'evidence_runtime',None)
        if runtime is not None:runtime.close()
        super().server_close()


def create_server(port=PORT, database=None, voice=None, model_agents=None, conversation_agent=None,
                  evidence_runtime=None, market_database=None):
    from agent_runtime.conversation import ConversationAgent
    server=NavrenHTTPServer(('127.0.0.1',port),ProxyHandler)
    server.app_release=app_release()
    server.finance=FinanceGateway(database)
    # 显式数据库用于隔离验收；不得把开发验收资料写入现役市场缓存。
    server.market=MarketStore(market_database if market_database is not None else
        (Path(database).with_name(Path(database).stem+'.market.sqlite3') if database else None))
    server.finance.market=server.market
    server.knowledge_import=server.finance.knowledge_provider.import_market_store(server.market,publish=True)
    from local_knowledge.runtime import EvidenceRuntime
    server.evidence_runtime=evidence_runtime or EvidenceRuntime(server.finance.knowledge_provider)
    server.voice=voice or VoiceGateway()
    server.agent_models=model_agents  # 仅旧行为对照测试可注入；生产对话不使用。
    server.conversation_agent=conversation_agent if conversation_agent is not None else ConversationAgent()
    return server


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f'Navren服务器 → http://localhost:{PORT}')
    print(f'Chat API 代理 → {API_URL}')
    print('Agent 对话 → /api/agent (DeepSeek 原生工具调用 + GLM 独立审核)')
    print('云端 TTS 与语音接入接口 → /api/speech/ws（原始录音出库默认关闭）')
    if API_KEY:
        print('模型服务密钥已加载')
    server=create_server()
    from market_pipeline import start_background
    stop=start_background(server.market)
    try:server.serve_forever()
    finally:stop.set();server.server_close()
