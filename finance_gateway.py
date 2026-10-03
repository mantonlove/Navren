"""Navren同源银行接入：HttpOnly会话、限定Agent工具、用户确认与资金约束。"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
from contextlib import closing
from datetime import date, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from simulated_bank import BankError, SimulatedBank
from simulated_bank.service import digest, integer, strict_fields

COOKIE = 'qz_bank_user'
TERMINAL = {'succeeded','pending_settlement','settled'}


def json_unique(pairs):
    result = {}
    for key,value in pairs:
        if key in result: raise ValueError('重复字段')
        result[key] = value
    return result


def request_json(handler):
    if handler.headers.get('Content-Type','').split(';')[0].strip().lower() != 'application/json':
        raise BankError('JSON_REQUIRED','请求体必须为JSON。',415)
    raw = handler.headers.get('Content-Length','')
    if not re.fullmatch(r'\d+',raw) or not 1<=int(raw)<=1048576 or handler.headers.get('Transfer-Encoding'):
        raise BankError('INVALID_LENGTH','请求体大小无效。',413)
    try:
        data = json.loads(handler.rfile.read(int(raw)),object_pairs_hook=json_unique,
                          parse_constant=lambda _:(_ for _ in ()).throw(ValueError('无效数值')))
    except (ValueError,UnicodeError): raise BankError('INVALID_JSON','请求体无法解析。')
    if not isinstance(data,dict): raise BankError('INVALID_JSON','请求体必须是对象。')
    return data


class FinanceGateway:
    def __init__(self, database=None):
        path = database or os.environ.get('NAVREN_SIM_DB') or os.environ.get('QINGZHOU_SIM_DB') or str(Path.home()/'Library/Application Support/Qingzhou/simulated-bank.sqlite3')
        self.bank = SimulatedBank(path)

    def origin_check(self, handler):
        authority = {f'127.0.0.1:{handler.server.server_port}',f'localhost:{handler.server.server_port}'}
        if not ipaddress.ip_address(handler.client_address[0]).is_loopback or handler.headers.get('Host') not in authority:
            raise BankError('LOCAL_ONLY','银行演示仅供本机同源访问。',403)
        origin = handler.headers.get('Origin')
        if origin and origin not in {'http://'+x for x in authority}:
            raise BankError('CROSS_ORIGIN_DENIED','拒绝跨站银行请求。',403)
        if handler.headers.get('Sec-Fetch-Site') in ('cross-site','same-site'):
            raise BankError('CROSS_ORIGIN_DENIED','拒绝跨站银行请求。',403)

    def identity(self, handler):
        cookies = SimpleCookie()
        try: cookies.load(handler.headers.get('Cookie',''))
        except Exception: raise BankError('UNAUTHENTICATED','银行会话无效。',401)
        token = cookies[COOKIE].value if COOKIE in cookies else ''
        workspace,role = self.bank.session(token)
        if role!='human': raise BankError('HUMAN_REQUIRED','当前会话不能确认操作。',403)
        return workspace,token

    @staticmethod
    def csrf(token):
        return digest(token+':qingzhou-csrf-v1')

    @staticmethod
    def demo_code(token):
        # 仅本机模拟第二因子；独立用户界面读取，绝不送入Agent上下文。
        return f'{int(hashlib.sha256((token+":demo-code").encode()).hexdigest()[:12],16)%1000000:06d}'

    def check_csrf(self,handler,token):
        value = handler.headers.get('X-QZ-CSRF','')
        if not hmac.compare_digest(value,self.csrf(token)):
            raise BankError('CSRF_DENIED','页面会话已变化，请刷新后重新查看操作。',403)

    def create_session(self):
        credentials = self.bank.create_workspace()
        token,workspace = credentials['session_token'],credentials['workspace_id']
        with self.bank.transaction() as db:
            db.execute('UPDATE workspaces SET pin_hash=? WHERE id=?',(digest(workspace+':'+self.demo_code(token)),workspace))
            self.bank._audit(db,workspace,'application_session_created',{'environment':'simulation'})
        return workspace,token

    def session_data(self,workspace,token):
        return {'environment':'simulation','workspace_id':workspace,'csrf':self.csrf(token)}

    def overview(self,workspace):
        archive = self.bank.customer_archive(workspace)
        accounts,profile,records = archive['accounts'],archive['profile'],archive['records']
        today = date.fromisoformat(archive['as_of'])
        end = (today+timedelta(days=30)).isoformat()
        commitments = []
        loans = {r['id']:r for r in records['loans']}
        for r in records['loan_installments']:
            if r['status'] in ('scheduled','overdue') and r['due_on']<=end:
                due = max(0,r['principal_minor']+r['interest_minor']+r['fees_minor']-r['paid_minor'])
                commitments.append({'kind':'loan','name':loans[r['loan_id']]['loan_type'],'account_id':loans[r['loan_id']]['repayment_account_id'],
                                    'amount_minor':due,'due_on':r['due_on'],'evidence_id':r['id']})
        cards = {r['id']:r for r in records['cards']}
        for r in records['credit_statements']:
            if r['status']=='open' and r['due_on']<=end:
                commitments.append({'kind':'credit_card','name':'信用卡本期应还（全额口径）','account_id':cards[r['card_id']]['account_id'],
                                    'amount_minor':max(0,r['total_due_minor']-r['paid_minor']),'due_on':r['due_on'],'evidence_id':r['id']})
        mandates = {r['id']:r for r in records['mandates']}
        linked = set()
        for r in records['subscriptions']:
            if r['merchant_status']=='active' and r['next_renewal_on']<=end:
                mandate=mandates.get(r['mandate_id'])
                linked.add(r['mandate_id'])
                name=r['plan_name']+('（商户合同仍有效）' if mandate and mandate['status']=='closed' else '')
                commitments.append({'kind':'subscription','name':name,'account_id':r['account_id'],
                                    'amount_minor':r['amount_minor'],'due_on':r['next_renewal_on'],'evidence_id':r['id']})
        for r in records['mandates']:
            if r['id'] not in linked and r['status']=='active' and r['next_charge_date']<=end:
                commitments.append({'kind':'mandate','name':r['merchant'],'account_id':r['account_id'],
                                    'amount_minor':r['amount_minor'],'due_on':r['next_charge_date'],'evidence_id':r['id']})
        total_due = sum(r['amount_minor'] for r in commitments)
        cash = sum(r['balance_minor'] for r in accounts)
        assets = archive['financial_summary']
        return {'environment':'simulation','workspace_id':workspace,'as_of':archive['as_of'],'version':archive['version'],
                'accounts':accounts,'profile':profile,'holdings':records['holdings'],'lots':records['investment_lots'],
                'mandates':records['mandates'],'orders':records['orders'],'operations':records['operations'],
                'commitments':commitments,'commitment_window_end':end,'upcoming_due_minor':total_due,
                'total_cash_minor':cash,'holding_principal_minor':assets['holding_principal_minor'],
                'pending_redemption_minor':assets['pending_redemption_minor'],'liabilities_minor':assets['liabilities_minor'],
                'net_assets_principal_minor':assets['net_assets_principal_minor'],
                'total_assets_principal_minor':cash+assets['holding_principal_minor']+assets['pending_redemption_minor'],
                'investable_minor':max(0,cash-profile['reserve_minor']-total_due),
                'integrity':archive['integrity'],'notice':'本金口径；仅计已结算现金。30日已知到期款按全额保留，含仍有效的商户订阅；关闭银行代扣不释放该合同的预算。不预支未来工资，未预测其他生活支出。'}

    def budget(self,workspace,account_id=None,floor=0):
        state = self.overview(workspace)
        account = next((a for a in state['accounts'] if a['id']==account_id),None)
        if account_id and not account: raise BankError('NOT_FOUND','当前客户没有该账户。',404)
        reserve = max(integer(floor,'保留资金'),state['profile']['reserve_minor'])
        # 全户应急金保留于付款账户，避免余额在备用账户时错误透支。
        account_cash = account['balance_minor'] if account else state['total_cash_minor']
        return {'cash_minor':account_cash,'reserve_minor':reserve,'upcoming_due_minor':state['upcoming_due_minor'],
                'investable_minor':max(0,account_cash-reserve-state['upcoming_due_minor']),
                'commitments':state['commitments'],'as_of':state['as_of'],'version':state['version']}

    def prepare(self,workspace,payload):
        strict_fields(payload,['action','account_id','product_id','amount_minor','mandate_id','idempotency_key','liquidity_floor_minor'],
                      ['action','account_id','idempotency_key'])
        core = {k:v for k,v in payload.items() if k!='liquidity_floor_minor'}
        for key in ('action','account_id','idempotency_key'):
            if not isinstance(core[key],str) or not 1<=len(core[key])<=128:
                raise BankError('INVALID_FIELDS','操作、账户和幂等键必须为字符串。')
        floor = integer(payload.get('liquidity_floor_minor',0),'保留资金')
        with closing(self.bank.connect()) as db:
            existing = db.execute('SELECT id,payload_hash,state FROM operations WHERE workspace_id=? AND idempotency_key=?',(workspace,core['idempotency_key'])).fetchone()
        if not existing or existing['state'] not in TERMINAL:
            if core['action']=='purchase':
                budget = self.budget(workspace,core['account_id'],floor)
                if integer(core.get('amount_minor'),'申购金额',1)>budget['investable_minor']:
                    raise BankError('LIQUIDITY_GUARD','申购金额超过扣除保留资金及近期到期款后的可投资额度。',422)
        op = self.bank.prepare(workspace,core)
        with self.bank.transaction() as db:
            policies = db.execute("SELECT details FROM audit WHERE workspace_id=? AND operation_id=? AND event='liquidity_policy' ORDER BY id LIMIT 1",(workspace,op['id'])).fetchone()
            if policies:
                if json.loads(policies['details'])['floor_minor']!=floor:
                    raise BankError('IDEMPOTENCY_CONFLICT','同一操作不能更改保留资金约束。',409)
            else: self.bank._audit(db,workspace,'liquidity_policy',{'floor_minor':floor},op['id'])
        return self.operation_view(workspace,op['id'])

    def operation_view(self,workspace,operation_id):
        op = self.bank.operation(workspace,operation_id)
        state = self.overview(workspace)
        payload = op['payload']
        product = next((p for p in self.bank.products(workspace) if p['id']==payload.get('product_id')),None)
        mandate = next((m for m in state['mandates'] if m['id']==payload.get('mandate_id')),None)
        with closing(self.bank.connect()) as db:
            policy = db.execute("SELECT details FROM audit WHERE workspace_id=? AND operation_id=? AND event='liquidity_policy' ORDER BY id LIMIT 1",(workspace,operation_id)).fetchone()
        floor = json.loads(policy['details'])['floor_minor'] if policy else 0
        return {'operation':op,'product':product,'mandate':mandate,
                'account':next(a for a in state['accounts'] if a['id']==payload['account_id']),
                'budget':self.budget(workspace,payload['account_id'],floor),
                'liquidity_floor_minor':floor,
                'environment':'simulation','verification_required':op['permission']=='red'}

    def confirm(self,workspace,operation_id,payload):
        strict_fields(payload,['confirmed','confirmation_digest','verification_code'],['confirmed','confirmation_digest'])
        op = self.bank.operation(workspace,operation_id)
        if op['state'] in TERMINAL:
            result = self.bank.confirm(workspace,operation_id,{k:v for k,v in payload.items() if k!='verification_code'})
            return {'operation':result,'overview':self.overview(workspace)}
        if op['action']=='purchase':
            view = self.operation_view(workspace,operation_id)
            if op['payload']['amount_minor']>view['budget']['investable_minor']:
                raise BankError('LIQUIDITY_GUARD','当前资金不足以满足已确认的保留与还款约束，请重新准备。',422)
        body = {k:v for k,v in payload.items() if k!='verification_code'}
        if body['confirmed'] is not True: raise BankError('CONFIRMATION_REQUIRED','请明确确认此操作。')
        if not isinstance(body['confirmation_digest'],str) or body['confirmation_digest']!=op['confirmation_digest']:
            raise BankError('CONFIRMATION_MISMATCH','确认摘要不一致。',409)
        if op['permission']=='red':
            grant = self.bank.verify(workspace,operation_id,{'verification_code':payload.get('verification_code')})
            body['verification_token'] = grant['verification_token']
        result = self.bank.confirm(workspace,operation_id,body)
        return {'operation':result,'overview':self.overview(workspace)}

    def dispatch(self,handler):
        parsed = urlsplit(handler.path)
        if not parsed.path.startswith('/api/finance'): return False
        cookie = None
        try:
            self.origin_check(handler)
            path,query = parsed.path,parse_qs(parsed.query)
            if any(len(v)!=1 for v in query.values()): raise BankError('INVALID_QUERY','查询参数重复。')
            if handler.command=='POST' and path=='/api/finance/session':
                strict_fields(request_json(handler),[])
                try: workspace,token = self.identity(handler)
                except BankError as error:
                    if error.code!='UNAUTHENTICATED': raise
                    workspace,token = self.create_session()
                    cookie = f'{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000'
                handler._finance_json(200,self.session_data(workspace,token),cookie)
                return True
            workspace,token = self.identity(handler)
            if handler.command=='GET':
                allowed = {'month'} if path in ('/api/finance/analysis','/api/finance/transactions') else {'year'} if path=='/api/finance/annual-analysis' else set()
                if set(query)-allowed: raise BankError('INVALID_QUERY','查询参数无效。')
                routes = {
                    '/api/finance/session':lambda:self.session_data(workspace,token),
                    '/api/finance/overview':lambda:self.overview(workspace),
                    '/api/finance/products':lambda:self.bank.products(workspace),
                    '/api/finance/analysis':lambda:self.bank.analysis(workspace,query.get('month',['2026-09'])[0]),
                    '/api/finance/transactions':lambda:self.bank.transactions(workspace,query.get('month',[None])[0]),
                    '/api/finance/anomalies':lambda:self.bank.anomalies(workspace),
                    '/api/finance/periodic-charges':lambda:self.bank.periodic_charges(workspace),
                    '/api/finance/audit':lambda:self.bank.audit(workspace),
                    '/api/finance/demo-code':lambda:{'code':self.demo_code(token),'notice':'仅用于模拟额外验证；不要发送到对话。'},
                }
                match = re.fullmatch(r'/api/finance/operations/([a-f0-9]{32})',path)
                if path in routes: result = routes[path]()
                elif path=='/api/finance/annual-analysis':
                    year = query.get('year',['2025'])[0]
                    if not re.fullmatch(r'\d{4}',year): raise BankError('INVALID_YEAR','年度格式无效。')
                    result = self.bank.annual_analysis(workspace,int(year))
                elif match: result = self.operation_view(workspace,match[1])
                else: raise BankError('NOT_FOUND','银行接口不存在。',404)
            elif handler.command=='POST':
                self.check_csrf(handler,token)
                payload = request_json(handler)
                match = re.fullmatch(r'/api/finance/operations/([a-f0-9]{32})/(confirm|cancel)',path)
                if path=='/api/finance/operations/prepare': result = self.prepare(workspace,payload)
                elif path=='/api/finance/profile': result = self.bank.profile(workspace,payload)
                elif match and match[2]=='confirm': result = self.confirm(workspace,match[1],payload)
                elif match:
                    strict_fields(payload,[])
                    result = self.bank.cancel(workspace,match[1])
                else: raise BankError('NOT_FOUND','银行接口不存在。',404)
            else: raise BankError('METHOD_NOT_ALLOWED','此接口不支持该方法。',405)
            handler._finance_json(200,result)
        except BankError as error: handler._finance_json(error.status,{'error':error.as_dict()})
        except Exception: handler._finance_json(500,{'error':{'code':'BANK_UNAVAILABLE','message':'银行服务暂不可用；请查询订单核实状态，勿重复提交。'}})
        return True
