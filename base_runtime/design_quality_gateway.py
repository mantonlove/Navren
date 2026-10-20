"""隔离候选的真实HTTP接线；主FinanceGateway/server须由原owner选择采用。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from finance_gateway import FinanceGateway, COOKIE, request_json
from simulated_bank import BankError
from base_runtime.funding_breakdown import FundingPresentations
from base_runtime.account_profile import AccountProfile
from base_runtime.presentation_provider import PresentationProvider
from base_runtime.view_followups import ViewFollowups
from base_runtime.response_wire import ResponseWire


class DesignQualityGateway(FinanceGateway):
    def __init__(self, database=None, **gateway_options):
        super().__init__(database, **gateway_options)
        self.presentations = FundingPresentations(self)
        self.presentation_provider = PresentationProvider(self, followups=ViewFollowups(self))
        self.account_profile = AccountProfile(self)
        self.response_wire = ResponseWire(self, self.presentation_provider)

    def query_read_capability(self, workspace, capability_id, parameters=None):
        from base_runtime.money_range import ensure_money_ranges
        result = super().query_read_capability(workspace, capability_id, parameters)
        # BankTools 在登记本轮 evidence 之前取得这个原服务结果，不能仅在签名处拒绝。
        ensure_money_ranges(result)
        return result

    def dispatch(self, handler):
        from base_runtime.money_range import ensure_money_ranges
        original = handler._finance_json
        def checked(status, result, *args, **kwargs):
            if 200 <= status < 300:
                ensure_money_ranges(result)
            return original(status, result, *args, **kwargs)
        handler._finance_json = checked
        try:
            return self._dispatch_with_identity(handler)
        finally:
            handler._finance_json = original

    def _dispatch_with_identity(self, handler):
        parsed = urlsplit(handler.path); path = parsed.path
        routes = {'/api/finance/account-profile', '/api/finance/account-profile/display', '/api/finance/profile',
                  '/api/finance/session/logout', '/api/finance/presentations/query', '/api/finance/presentations/detail',
                  '/api/finance/presentations/item-page', '/api/finance/presentations/validate'}
        history = path.startswith('/api/finance/presentation-history/')
        if path not in routes and not history: return super().dispatch(handler)
        try:
            self.origin_check(handler)
            w, token = self.identity(handler)
            with self.demo_sessions.request(token, w):
                cookie = None
                if handler.command == 'GET':
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if history:
                        if set(query) - {'language'} or any(len(v) != 1 for v in query.values()):
                            raise BankError('INVALID_QUERY', '历史读取参数无效。', 400)
                        response = unquote(path[len('/api/finance/presentation-history/'):])
                        result = self.response_wire.restore(w, response, language=query.get('language', ['zh'])[0])
                    elif path == '/api/finance/account-profile' and not query:
                        result = self.account_profile.read(w)
                    else: raise BankError('METHOD_NOT_ALLOWED', '此接口不支持该读取方式。', 405)
                elif handler.command == 'POST':
                    if parsed.query: raise BankError('INVALID_QUERY', '保存接口不接受URL参数。', 400)
                    self.check_csrf(handler, token)
                    payload = request_json(handler)
                    if path == '/api/finance/account-profile/display':
                        result = self.account_profile.update_display(w, payload)
                    elif path == '/api/finance/profile':
                        # 保留既有同源、本人确认的银行profile协议；新CAS协议仅以明确版本字段选择。
                        result = (self.account_profile.update_conditions(w, payload) if 'expected_version' in payload
                                  else self.bank.profile(w, payload))
                    elif path == '/api/finance/session/logout' and not payload:
                        result = self.account_profile.logout(w)
                        cookie = COOKIE + '=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'
                    elif path == '/api/finance/presentations/validate' and set(payload) == {'wire_ref'}:
                        result = self.response_wire.validate(w, payload['wire_ref'])
                    elif path == '/api/finance/presentations/query' and set(payload) <= {'parameters', 'language'} and 'parameters' in payload:
                        request = uuid.uuid4().hex; language = payload.get('language', 'zh')
                        obj = self.presentation_provider.dispatch(w, 'query', payload['parameters'],
                            trusted_current_request=request, trusted_current_message=request, language=language)
                        result = self.response_wire.build(w, obj, trusted_current_request=request,
                            trusted_current_message=request, language=language)
                    elif path in {'/api/finance/presentations/detail', '/api/finance/presentations/item-page'}:
                        required = {'wire_ref', 'selector'} if path.endswith('/detail') else {'wire_ref', 'point_ref', 'offset', 'limit'}
                        if set(payload) != required: raise BankError('INVALID_REQUEST', '明细参数无效。', 422)
                        current = self.response_wire.validate(w, payload['wire_ref'])['wire']
                        if current['presentation'] is None: raise BankError('PRESENTATION_REQUIRED', '原回复没有图表。', 422)
                        args = {'view_id': current['presentation']['view_id']}
                        if path.endswith('/detail'): args['selector'] = payload['selector']; operation = 'detail'
                        else: args.update({k:payload[k] for k in ('point_ref', 'offset', 'limit')}); operation = 'item_page'
                        obj = self.presentation_provider.dispatch(w, operation, args,
                            trusted_current_request=current['request_id'], trusted_current_message=current['message_id'], language=current['language'])
                        result = self.response_wire.build(w, obj, trusted_current_request=current['request_id'],
                            trusted_current_message=current['message_id'], language=current['language'])
                    else: raise BankError('INVALID_REQUEST', '接口或参数无效。', 422)
                else: raise BankError('METHOD_NOT_ALLOWED', '接口不支持此请求方式。', 405)
                handler._finance_json(200, result, cookie)
        except BankError as error:
            handler._finance_json(error.status, {'error': error.as_dict()})
        return True


def create_isolated_server(database):
    """显式离线库、loopback随机端口，不自动建现役会话或模型配置。"""
    path = Path(database).resolve()
    if 'Library/Application Support' in str(path):
        raise ValueError('live_database_forbidden')
    gateway = DesignQualityGateway(path)
    class Handler(BaseHTTPRequestHandler):
        def _finance_json(self, status, result, cookie=None):
            body = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(status); self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body))); self.send_header('Cache-Control', 'no-store')
            if cookie: self.send_header('Set-Cookie', cookie)
            self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if not gateway.dispatch(self): self._finance_json(404, {'error': 'NOT_FOUND'})
        def do_POST(self):
            if not gateway.dispatch(self): self._finance_json(404, {'error': 'NOT_FOUND'})
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.gateway = gateway
    return server
