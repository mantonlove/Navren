"""版本化组合：保留旧账户账本；行情缓存公开，组合/份额/迁移记录私有。"""
from contextlib import closing
from datetime import date,timedelta
from decimal import Decimal,ROUND_DOWN
import json,hashlib
from pathlib import Path
from fund_history import FUNDS,cached
from wealth_returns import value_minor,calculate,COMPONENTS
from simulated_bank import BankError

SCHEMA='''
CREATE TABLE IF NOT EXISTS portfolio_versions(workspace_id TEXT NOT NULL,id TEXT NOT NULL,kind TEXT NOT NULL,active INTEGER NOT NULL,effective_on TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(workspace_id,id));
CREATE UNIQUE INDEX IF NOT EXISTS portfolio_current ON portfolio_versions(workspace_id,kind) WHERE active=1;
CREATE TABLE IF NOT EXISTS portfolio_positions(workspace_id TEXT NOT NULL,portfolio_version TEXT NOT NULL,account_id TEXT NOT NULL,fund_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(workspace_id,portfolio_version,account_id,fund_id));
CREATE TABLE IF NOT EXISTS portfolio_valuations(workspace_id TEXT NOT NULL,portfolio_version TEXT NOT NULL,account_id TEXT NOT NULL,fund_id TEXT NOT NULL,valuation_on TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(workspace_id,portfolio_version,account_id,fund_id,valuation_on));
CREATE TABLE IF NOT EXISTS portfolio_migrations(workspace_id TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(workspace_id,id));
'''

def pack(d):return json.dumps(d,ensure_ascii=False,sort_keys=True)
def state(q,n,cash=0):return {'quantity':str(q),'nav':n,**{k:0 for k in COMPONENTS},'investment_cash_minor':cash}
def convert(amount,nav):
    qty=(Decimal(amount)/100/Decimal(nav)).quantize(Decimal('.000001'),rounding=ROUND_DOWN)
    mv=value_minor(qty,nav)
    return str(qty),amount-mv


class WealthPortfolio:
    def __init__(self,g):
        self.g=g;self.bank=g.bank
        with closing(self.bank.connect())as db:db.executescript(SCHEMA)

    def products(self):
        return [d['product']for c in FUNDS if(d:=cached(c))]

    def source_revision(self):
        """只读公开来源指纹，不创建、刷新或激活任何组合。"""
        data=[d for c in FUNDS if(d:=cached(c))]
        return hashlib.sha256(pack({'expected':list(FUNDS),'sources':data}).encode()).hexdigest()

    def provision(self,w,*,db=None):
        """只构建独立历史回放；绝不把回放份额写回真实/现有账户。幂等、源修订生成新版。"""
        # Explicit lifecycle writes may share their caller's bank transaction.
        if db is None:
            with self.bank.transaction() as connection:
                return self.provision(w,db=connection)
        main=next((r[2] for r in db.execute('PRAGMA database_list') if r[1]=='main'),None)
        if not main or Path(main).resolve()!=self.bank.database.resolve() or not db.in_transaction:
            raise BankError('PORTFOLIO_BANK_TRANSACTION_REQUIRED','历史组合需同一银行活动写事务。',403)
        self.bank._workspace(db,w)
        data=[d for c in FUNDS if(d:=cached(c))]
        if not data:
            prior=db.execute("SELECT payload FROM portfolio_versions WHERE workspace_id=? AND kind='historical' AND active=1",(w,)).fetchone()
            if not prior:return
            prior=json.loads(prior['payload'])
        revision=hashlib.sha256(pack({'expected':list(FUNDS),'sources':data}).encode()).hexdigest()[:16]
        pid='history-'+revision
        self.bank._workspace(db,w)
        if db.execute('SELECT 1 FROM portfolio_versions WHERE workspace_id=? AND id=?',(w,pid)).fetchone():
            db.execute("UPDATE portfolio_versions SET active=0 WHERE workspace_id=? AND kind='historical' AND id!=?",(w,pid))
            db.execute('UPDATE portfolio_versions SET active=1 WHERE workspace_id=? AND id=?',(w,pid))
            return
        db.execute("UPDATE portfolio_versions SET active=0 WHERE workspace_id=? AND kind='historical'",(w,))
        begin=max(d['coverage']['actual_from']for d in data) if data else prior['from'];end=min(d['coverage']['actual_to']for d in data) if data else prior['to']
        payload={'identity':'基于真实行情的历史模拟组合','quantity_origin':'独立示例，每只基金1000份，不代表用户历史持仓或推荐','from':begin,'to':end,'currency':'CNY','accounting_basis':'total_equity_v2','source_revision':revision,'expected_funds':['ORIENT:'+c for c in FUNDS],'source_gaps':['ORIENT:'+c for c in FUNDS if not any(d['product']['share_code']==c for d in data)],'valuation_dates':sorted({r['nav_on']for d in data for r in d['navs']if begin<=r['nav_on']<=end}) if data else prior['valuation_dates']}
        db.execute('INSERT INTO portfolio_versions VALUES(?,?,?,?,?,?)',(w,pid,'historical',1,begin,pack(payload)))
        for d in data:
            product=d['product'];fid=product['fund_id'];navs=[r for r in d['navs']if begin<=r['nav_on']<=end];quantity='1000';cost=value_minor(quantity,navs[0]['unit_nav']);acc='historical-example'
            pos={'fund_id':fid,'product':product,'quantity':quantity,'initial_cost_minor':cost,'remaining_cost_minor':cost,'rounding_cash_minor':0,'capital_origin':'模拟期初资金，无期间买卖，开户前费用不纳入期间收益','trades':[],'acquired_on':navs[0]['nav_on'],'source_revision':revision}
            db.execute('INSERT INTO portfolio_positions VALUES(?,?,?,?,?)',(w,pid,acc,fid,pack(pos)))
            previous=None
            for nav in navs:
                curr=state(quantity,nav['unit_nav']);point={'accounting_basis':'total_equity_v2','opening':state(quantity,previous['unit_nav'])if previous else curr,'closing':curr,'owner_contribution_minor':0,'owner_withdrawal_minor':0,'remaining_cost_minor':cost,'cumulative_pnl_minor':value_minor(quantity,nav['unit_nav'])-cost,'realized_net_pnl_minor':0,'cost_rule':'独立期初模拟持仓，期间无买卖；移动平均成本','nav_complete':previous is not None,'closing_nav_complete':True,'entitlements_complete':True,'transactions_complete':True,'corporate_actions_complete':d['coverage']['corporate_actions_complete'],'fees_complete':True,'valuation_on':nav['nav_on'],'opening_on':previous['nav_on']if previous else None,'nav_asof':nav['nav_on'],'interval_days':(date.fromisoformat(nav['nav_on'])-date.fromisoformat(previous['nav_on'])).days if previous else None,'nav_evidence':nav['source'],'portfolio_version':pid,'product_name':product['name'],'account_id':acc,'quantity':quantity,'unit_nav':nav['unit_nav'],'currency':'CNY','fee_assumptions':'期初持仓，无期间申赎/额外费用；运作费用已含净值，不重复扣。'}
                db.execute('INSERT INTO portfolio_valuations VALUES(?,?,?,?,?,?)',(w,pid,acc,fid,nav['nav_on'],pack(point)));previous=nav
        for code in FUNDS:
            if any(d['product']['share_code']==code for d in data):continue
            fid='ORIENT:'+code;acc='historical-example'
            pos={'fund_id':fid,'product':{'fund_id':fid,'name':FUNDS[code][0],'share_code':code,'currency':'CNY','current_channel_status':'unknown','source_status':'unavailable'},'quantity':'1000','initial_cost_minor':None,'remaining_cost_minor':None,'source_revision':revision,'source_status':'unavailable'}
            db.execute('INSERT INTO portfolio_positions VALUES(?,?,?,?,?)',(w,pid,acc,fid,pack(pos)))

    def versions(self,w):
        with self.bank.transaction()as db:
            self.bank._workspace(db,w)
            rows=[{**dict(r),'metadata':json.loads(r['payload'])}for r in db.execute('SELECT * FROM portfolio_versions WHERE workspace_id=? ORDER BY effective_on DESC',(w,))]
        for r in rows:r.pop('payload')
        return {'versions':rows,'products':self.products(),'accounting_basis':'total_equity_v2'}

    def points(self,w,start,end,portfolio='historical'):
        with self.bank.transaction()as db:
            self.bank._workspace(db,w)
            version=db.execute('SELECT * FROM portfolio_versions WHERE workspace_id=? AND '+('kind=? AND active=1'if portfolio in ['historical','current']else 'id=?'),(w,portfolio)).fetchone()
            if not version:return [],None
            values=[{**json.loads(r['payload']),'fund_id':r['fund_id'],'account_id':r['account_id'],'valuation_on':r['valuation_on']}for r in db.execute('SELECT * FROM portfolio_valuations WHERE workspace_id=? AND portfolio_version=? AND valuation_on BETWEEN ? AND ? ORDER BY valuation_on,account_id,fund_id',(w,version['id'],start,end))]
            meta={**json.loads(version['payload']),'id':version['id'],'kind':version['kind'],'effective_on':version['effective_on'],'active':bool(version['active']),'archived':not bool(version['active']),'archive_notice':'归档快照，仅供回看，不作为当前规划或执行依据。' if not version['active'] else None}
            return values,meta

    def positions(self,w,pid):
        with self.bank.transaction()as db:
            self.bank._workspace(db,w)
            return [{**json.loads(r['payload']),'account_id':r['account_id'],'fund_id':r['fund_id']} for r in db.execute('SELECT * FROM portfolio_positions WHERE workspace_id=? AND portfolio_version=?',(w,pid))]

    def aggregate(self,rows,expected):
        """按账户+产品汇总；缺一账户不得把已知部分伪装为完整产品。"""
        present={(p['account_id'],p['fund_id'])for p in rows};complete=bool(rows) and present==expected
        returns=[calculate(p)for p in rows];r={'status':'available' if complete and all(x['status']=='available'for x in returns)else 'unavailable','accounting_basis':'total_equity_v2','missing':sorted({k for x in returns for k in x.get('missing',[])}|({'账户估值缺失'}if not complete else set()))}
        for field in ['daily_pnl_minor','opening_equity_minor','closing_equity_minor','closing_value_minor','cumulative_pnl_minor','realized_pnl_minor','unrealized_pnl_minor','owner_net_flow_minor']:
            known=[x.get(field)for x in returns]
            r[field]=sum(known) if complete and all(type(v)is int for v in known)else None
        r['known_closing_equity_minor']=sum(x['closing_equity_minor']for x in returns if type(x.get('closing_equity_minor'))is int)
        r['equation']='期末权益 − 期初权益 − 所有者净投入';r['cost_rule']='逐账户计算后合计，完整性按账户与产品核对'
        return r

    def detail(self,w,fid,on,portfolio='historical'):
        points,meta=self.points(w,'1900-01-01',on,portfolio)
        if not meta:raise BankError('PORTFOLIO_NOT_FOUND','组合不存在或不属于当前账户。',404)
        positions=[p for p in self.positions(w,meta['id'])if p['fund_id']==fid]
        if not positions:raise BankError('PRODUCT_NOT_FOUND','此组合没有该产品。',404)
        rows=[p for p in points if p['fund_id']==fid]
        if not rows:raise BankError('VALUATION_NOT_FOUND','该产品来源缺失，没有可读取的估值。',404)
        expected={(p['account_id'],fid)for p in positions};byday={}
        for row in rows:byday.setdefault(row['valuation_on'],[]).append(row)
        latest=byday[max(byday)];point={**latest[0]};point['quantity']=str(sum(Decimal(p['quantity'])for p in latest));point['accounts']=[p['account_id']for p in latest]
        for end in ['opening','closing']:
            point[end]={**latest[0][end],'quantity':str(sum(Decimal(p[end]['quantity'])for p in latest)),**{k:sum(p[end][k]for p in latest)for k in COMPONENTS}}
        r=self.aggregate(latest,expected)
        complete={(p['account_id'],p['fund_id'])for p in latest}==expected
        for field in ['remaining_cost_minor','owner_contribution_minor','owner_withdrawal_minor']:
            values=[p.get(field)for p in latest]
            point[field]=sum(values)if complete and all(type(v)is int for v in values)else None
        point['complete']=complete;point['missing_accounts']=sorted(a for a,f in expected-{(p['account_id'],p['fund_id'])for p in latest})
        if not complete:
            point['known_quantity']=point['quantity'];point['quantity']=None
            for name in ['opening','closing']:
                point['known_'+name]=point[name]
                point[name]={k:None for k in point[name]}
        history=[]
        for history_on,part in sorted(byday.items()):
            present={(p['account_id'],p['fund_id'])for p in part};complete_day=present==expected;known=str(sum(Decimal(p['quantity'])for p in part))
            history.append({'date':history_on,'nav':part[0]['unit_nav'],'quantity':known if complete_day else None,'known_quantity':known,'complete':complete_day,'missing_accounts':sorted(a for a,f in expected-present),'returns':self.aggregate(part,expected),'interval_days':part[0]['interval_days'],'accounts':[{'account_id':p['account_id'],'quantity':p['quantity'],'returns':calculate(p)}for p in part]})
        return {'portfolio':meta,'product':positions[0]['product'],'positions':positions,'as_of':on,'nav_asof':point['nav_asof'],'point':point,'returns':r,'history':history,'source_identity':meta['identity']}

    def migrate(self,w):
        """风险或期限资料不匹配则保留旧组合，记录缺口；不改旧记录产品ID。"""
        archive=self.bank.customer_archive(w);holdings=archive['records']['holdings'];products={p['id']:p for p in self.bank.products(w)};candidate=cached('006210');gaps=[];mapped=[]
        for h in holdings:
            old=products[h['product_id']]
            if not candidate or not candidate['coverage']['corporate_actions_complete']:gaps.append({'product_id':h['product_id'],'reason':'行情或企业行为核验不完整'});continue
            if old['risk_level']<candidate['product'].get('risk_level',5):gaps.append({'product_id':h['product_id'],'reason':'原R1活钱用途不能升级到管理人R2债券；待补微众货币产品每日收益证据'});continue
            mapped.append(h)
        report={'as_of':archive['as_of'],'before_principal_minor':sum(h['amount_minor']for h in holdings),'mapped_candidates':len(mapped),'remaining':gaps,'status':'blocked'if gaps else 'ready','active_current':'legacy-v1','cash_changed_minor':0,'notice':'当前组合保持原账本。部分候选匹配不足以批准整组转换；独立历史模拟收益已另建版本。旧订单继续旧版本，不重用确认。'}
        with self.bank.transaction()as db:db.execute('INSERT INTO portfolio_migrations VALUES(?,?,?) ON CONFLICT(workspace_id,id) DO UPDATE SET payload=excluded.payload',(w,'four-board-v2',pack(report)))
        return report
