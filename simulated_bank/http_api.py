"""仅本机的模拟银行HTTP服务，提供human/agent权限分离。"""
import ipaddress
import json
import re
import sys
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .service import BankError, strict_fields

PREFIX = '/api/sim-bank'


def unique_object(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            raise ValueError('重复字段')
        result[key]=value
    return result


def invalid_constant(value):
    raise ValueError('非标准JSON数值')


def create_server(bank, port=18766):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, fmt, *args):
            # 不记录请求体、验证码、Authorization或Cookie。
            sys.stderr.write(f'模拟银行 {self.command} {urlsplit(self.path).path}\n')

        def _respond(self, status, data, cookie=None):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Connection', 'close')
            if cookie:
                self.send_header('Set-Cookie', f'qz_sim_session={cookie}; HttpOnly; SameSite=Strict; Path=/api/sim-bank')
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _origin_check(self):
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                raise BankError('LOCAL_ONLY', '模拟银行仅供本机访问。', 403)
            authority = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in authority:
                raise BankError('INVALID_HOST', '请求主机不匹配。', 403)
            origin = self.headers.get('Origin')
            if origin and origin not in {'http://'+host for host in authority}:
                raise BankError('CROSS_ORIGIN_DENIED', '不接受跨站模拟业务请求，请使用同源后端适配。', 403)
            if self.headers.get('Sec-Fetch-Site') in ('cross-site', 'same-site'):
                raise BankError('CROSS_ORIGIN_DENIED', '不接受跨站模拟业务请求。', 403)

        def _payload(self):
            if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                raise BankError('JSON_REQUIRED', '请求体必须为JSON。', 415)
            length = self.headers.get('Content-Length', '')
            if not re.fullmatch(r'\d+',length) or not 1 <= int(length) <= 65536 or self.headers.get('Transfer-Encoding'):
                raise BankError('INVALID_LENGTH', '请求体大小必须为1至65536字节。', 413)
            try:
                data = json.loads(self.rfile.read(int(length)).decode('utf-8'),
                                  object_pairs_hook=unique_object,parse_constant=invalid_constant)
            except (ValueError, UnicodeError):
                raise BankError('INVALID_JSON', 'JSON请求体无法解析。')
            if not isinstance(data,dict):
                raise BankError('INVALID_JSON', 'JSON请求体必须为对象。')
            return data

        def _session(self):
            authorization = self.headers.get('Authorization', '')
            if authorization.startswith('Bearer '):
                token = authorization[7:]
            else:
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get('Cookie', ''))
                except Exception:
                    raise BankError('UNAUTHENTICATED','模拟会话无效。',401)
                token = cookie['qz_sim_session'].value if 'qz_sim_session' in cookie else ''
            return bank.session(token)

        def _dispatch(self):
            self._origin_check()
            parsed = urlsplit(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if self.command=='GET' and path=='/health':
                return 200, {'status':'ok', 'environment':'simulation', 'api_version':'2'}, None
            if self.command=='GET' and path=='/openapi.json':
                return 200, json.loads((Path(__file__).parent/'openapi.json').read_text()), None
            if self.command=='POST' and path==PREFIX+'/workspaces':
                strict_fields(self._payload(), [])
                result = bank.create_workspace()
                return 201, result, result['session_token']
            workspace, role = self._session()
            operation_match = re.fullmatch(PREFIX+r'/operations/([a-f0-9]{32})(?:/(verify|confirm|cancel))?',path)
            if self.command=='GET':
                allowed_query = {'year'} if path==PREFIX+'/annual-analysis' else {'month'} if path in (PREFIX+'/snapshot',PREFIX+'/transactions',PREFIX+'/analysis') else set()
                if set(query)-allowed_query or any(len(v)!=1 for v in query.values()):
                    raise BankError('INVALID_QUERY','查询参数无效。')
                month = query.get('month',['2026-09'])[0]
                get_routes = {
                    PREFIX+'/capabilities': lambda: bank.capabilities(),
                    PREFIX+'/snapshot': lambda: bank.snapshot(workspace,month),
                    PREFIX+'/transactions': lambda: bank.transactions(workspace,query.get('month',[None])[0]),
                    PREFIX+'/analysis': lambda: bank.analysis(workspace,month),
                    PREFIX+'/anomalies': lambda: bank.anomalies(workspace),
                    PREFIX+'/periodic-charges': lambda: bank.periodic_charges(workspace),
                    PREFIX+'/mandates': lambda: bank.mandates(workspace),
                    PREFIX+'/products': lambda: bank.products(workspace),
                    PREFIX+'/profile': lambda: bank.profile(workspace),
                    PREFIX+'/holdings': lambda: bank.holdings(workspace),
                    PREFIX+'/orders': lambda: bank.orders(workspace),
                    PREFIX+'/audit': lambda: bank.audit(workspace),
                    PREFIX+'/customer-archive': lambda: bank.customer_archive(workspace),
                    PREFIX+'/data-contract': lambda: bank.data_contract(),
                    PREFIX+'/annual-analysis': lambda: bank.annual_analysis(workspace,int(query.get('year',['2025'])[0])) if re.fullmatch(r'\d{4}',query.get('year',['2025'])[0]) else (_ for _ in ()).throw(BankError('INVALID_YEAR','年度格式必须为四位数字。')),
                }
                if path in get_routes:
                    return 200, get_routes[path](), None
                if operation_match and not operation_match[2]:
                    return 200, bank.operation(workspace,operation_match[1]), None
            elif self.command=='POST':
                data = self._payload()
                if role=='agent' and path not in (PREFIX+'/operations/prepare',PREFIX+'/products/compare') and not (operation_match and operation_match[2]=='cancel'):
                    raise BankError('HUMAN_REQUIRED','此接口仅供用户确认或模拟验证，Agent凭证无权调用。',403)
                if path==PREFIX+'/operations/prepare':
                    return 200,bank.prepare(workspace,data),None
                if path==PREFIX+'/products/compare':
                    strict_fields(data,['product_ids'],['product_ids'])
                    return 200,bank.products(workspace,data['product_ids']),None
                if path==PREFIX+'/profile':
                    return 200,bank.profile(workspace,data),None
                if operation_match:
                    op, action = operation_match[1],operation_match[2]
                    if action=='verify': return 200,bank.verify(workspace,op,data),None
                    if action=='confirm': return 200,bank.confirm(workspace,op,data),None
                    if action=='cancel':
                        strict_fields(data,[])
                        return 200,bank.cancel(workspace,op),None
            raise BankError('NOT_FOUND','接口不存在。',404)

        def _handle(self):
            try:
                status,data,cookie = self._dispatch()
                self._respond(status,data,cookie)
            except BankError as error:
                self._respond(error.status,{'error':error.as_dict()})
            except (BrokenPipeError,ConnectionResetError):
                pass
            except Exception:
                self._respond(500,{'error':{'code':'INTERNAL_ERROR','message':'模拟服务发生错误，本次未确认成功。','environment':'simulation'}})

        do_GET = _handle
        do_POST = _handle

    server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads = True
    return server
