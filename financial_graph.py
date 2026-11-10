"""可重算的个人财务图投影。SQLite 保存类型与证据，NetworkX 只做子图检索。"""
from __future__ import annotations
from collections import OrderedDict, defaultdict
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import time
import logging
import re
import threading
import uuid
from pathlib import Path
from contextlib import nullcontext
from simulated_bank.agent_guard import context_hash

from simulated_bank import BankError
from simulated_bank.service import strict_fields

ONTOLOGY = 'navren-finance-graph/3'
METHOD = 'integer-ledger/2'
VIEWS = {'overview','cashflow','wealth','plans','preferences'}
ALIASES={'funds':'cashflow','cost':'cashflow','goals':'plans','liquidity':'plans','risk':'wealth','records':'overview'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS graph_entities(
 workspace_id TEXT NOT NULL REFERENCES workspaces(id),id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('account','category','merchant','contract','goal','memory','event','concept','holding','profile','person')),
 source_scope TEXT NOT NULL,label TEXT NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(workspace_id,id));
CREATE TABLE IF NOT EXISTS graph_events(
 workspace_id TEXT NOT NULL,id TEXT NOT NULL,entity_id TEXT NOT NULL,
 occurred_at TEXT NOT NULL,amount_minor INTEGER CHECK(amount_minor IS NULL OR typeof(amount_minor)='integer'),
 currency TEXT CHECK(currency IS NULL OR length(currency)=3),state TEXT NOT NULL,
 PRIMARY KEY(workspace_id,id),FOREIGN KEY(workspace_id,entity_id) REFERENCES graph_entities(workspace_id,id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS graph_assertions(
 workspace_id TEXT NOT NULL,id TEXT NOT NULL,subject_id TEXT NOT NULL,object_id TEXT NOT NULL,
 predicate TEXT NOT NULL,amount_minor INTEGER CHECK(amount_minor IS NULL OR typeof(amount_minor)='integer'),
 currency TEXT CHECK(currency IS NULL OR length(currency)=3),valid_from TEXT,valid_to TEXT,
 derivation_type TEXT NOT NULL CHECK(derivation_type IN ('observed','confirmed','calculated','candidate','scenario','concept')),
 record_version TEXT NOT NULL,ontology_version TEXT NOT NULL,recorded_at REAL NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(workspace_id,id),FOREIGN KEY(workspace_id,subject_id) REFERENCES graph_entities(workspace_id,id) ON DELETE CASCADE,
 FOREIGN KEY(workspace_id,object_id) REFERENCES graph_entities(workspace_id,id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS graph_evidence_links(
 workspace_id TEXT NOT NULL,assertion_id TEXT NOT NULL,source_scope TEXT NOT NULL,source_table TEXT NOT NULL,
 source_id TEXT NOT NULL,source_version TEXT NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(workspace_id,assertion_id,source_scope,source_table,source_id),
 FOREIGN KEY(workspace_id,assertion_id) REFERENCES graph_assertions(workspace_id,id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS graph_calculations(
 workspace_id TEXT NOT NULL,id TEXT NOT NULL,method_version TEXT NOT NULL,metric_definition TEXT NOT NULL,
 period_start TEXT NOT NULL,period_end_exclusive TEXT NOT NULL,currency TEXT NOT NULL,
 output_minor INTEGER CHECK(output_minor IS NULL OR typeof(output_minor)='integer'),input_refs TEXT NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(workspace_id,id));
CREATE TABLE IF NOT EXISTS goal_allocations(
 workspace_id TEXT NOT NULL REFERENCES workspaces(id),id TEXT NOT NULL,goal_id TEXT NOT NULL,account_id TEXT NOT NULL,
 amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor)='integer' AND amount_minor>0),currency TEXT NOT NULL CHECK(length(currency)=3),
 confirmed_at REAL NOT NULL,source_version TEXT NOT NULL,PRIMARY KEY(workspace_id,id),UNIQUE(workspace_id,goal_id,account_id));
CREATE TABLE IF NOT EXISTS personal_decisions(
 workspace_id TEXT NOT NULL,id TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(workspace_id,id));
CREATE TABLE IF NOT EXISTS graph_scenarios(
 workspace_id TEXT NOT NULL REFERENCES workspaces(id),id TEXT NOT NULL,input_version TEXT NOT NULL,
 parameters TEXT NOT NULL,outputs TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(workspace_id,id));
'''


def packed(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def key(*parts):
    return hashlib.sha256(packed(parts).encode()).hexdigest()[:24]


def minor(v, name='金额', zero=True):
    if type(v) is not int or v < (0 if zero else 1) or v > 10**12:
        raise BankError('GRAPH_AMOUNT', name+'必须是有效的最小货币单位整数。')
    return v


def local_day(v):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    if len(v)==10:return date.fromisoformat(v)
    d=datetime.fromisoformat(v)
    return (d.astimezone(ZoneInfo('Asia/Shanghai')) if d.tzinfo else d).date()


def classify(row):
    """依显式账本类型判断，不从商户名称猜测用途或人的动机。"""
    kind=row.get('transaction_kind',row.get('kind'))
    if kind=='fee' and row.get('investment_link'):return 'investment_fee'
    if row.get('internal_transfer') or row.get('transfer_group_id') or kind=='internal_transfer':return 'internal_transfer'
    if kind in {'card_repayment','credit_repayment','repayment','loan_principal','loan_repayment'}:return 'principal'
    if kind in {'purchase','redeem','investment','investment_purchase','investment_redemption'}:return 'investment_principal'
    return kind if kind in {'income','expense','refund','transfer','fee','interest'} else 'unknown'


def liquidity_scenario(timeline, account_id, plans, floor_minor=None):
    """在原时间轴的日终余额上叠加明确的用户计划；从不把候选工资/解锁当成到账。"""
    a=next((a for a in timeline['accounts'] if a['account_id']==account_id),None)
    if a is None:raise BankError('GRAPH_ACCOUNT','账户不存在。',404)
    floor=a['reserve_floor_minor'] if floor_minor is None else minor(floor_minor, '应急底线')
    start=date.fromisoformat(timeline['as_of']);stop=date.fromisoformat(timeline['horizon_end'])
    by_day=defaultdict(list)
    for p in plans:
        try:d=date.fromisoformat(p['date'])
        except (ValueError,TypeError):raise BankError('GRAPH_RANGE','计划日期应为YYYY-MM-DD。')
        minor(p['amount_minor'])
        if not start<=d<=stop:raise BankError('GRAPH_RANGE','计划日期必须位于推演期间。')
        if p['currency']!=a['currency']:raise BankError('GRAPH_CURRENCY','不同币种的计划不能相加。')
        by_day[d.isoformat()].append(p)
    slots={s['date']:next((x for x in s['accounts'] if x['account_id']==account_id),None) for s in timeline['timeline']}
    cash=a['opening_balance_minor'];expected=cash;held=a['held_minor'];deduct=0
    rows=[];min_available=cash-held-floor
    for d in sorted(set(slots)|set(by_day)|{timeline['as_of']}):
        slot=slots.get(d)
        if slot:
            cash=slot['balance_after_confirmed_minor'];expected=slot['balance_if_candidates_occur_minor'];held=slot['held_minor']
        deduct+=sum(p['amount_minor'] for p in by_day[d])
        available=cash-held-floor-deduct;min_available=min(min_available,available)
        rows.append({'date':d,'cash_after_confirmed_minor':cash,'cash_after_plans_minor':cash-deduct,
                     'available_after_plans_minor':available,'available_if_candidates_occur_minor':expected-held-floor-deduct,
                     'floor_minor':floor,'held_minor':held,'plans':by_day[d]})
    return {'account_id':account_id,'currency':a['currency'],'rows':rows,'minimum_available_minor':min_available,
            'planning_floor_shortfall_minor':max(0,-min_available),'floor_minor':floor,
            'notice':'计划底线缺口不等于银行余额为负；候选代扣另列，不预支未来工资，不把产品解锁当成到账。没有扣款、预留或赎回。'}


class FinancialGraph:
    def __init__(self, gateway):
        self.g=gateway
        self._cache=OrderedDict();self._cache_lock=threading.Lock()
        with closing(self.g.bank.connect()) as db:db.executescript(SCHEMA)

    def _cached(self,k):
        with self._cache_lock:
            if k not in self._cache:return None
            self._cache.move_to_end(k)
            return self._cache[k]

    def _remember(self,k,v):
        with self._cache_lock:
            self._cache[k]=v;self._cache.move_to_end(k)
            while len(self._cache)>8:self._cache.popitem(last=False)

    def _version(self,w,db=None):
        with (nullcontext(db) if db is not None else closing(self.g.bank.connect())) as db:
            alloc=[tuple(r) for r in db.execute('SELECT id,goal_id,account_id,amount_minor FROM goal_allocations WHERE workspace_id=? ORDER BY id',(w,))]
            bank_version={'context':context_hash(db,w),'rules':'calendar-v1'}
        return key(bank_version,self.g.source(w),self.g.memories.state(w)['revision'],alloc,ONTOLOGY)

    def _parameters(self,w,p):
        strict_fields(p,{'view','from','to','source','question','node_id','insight_id','edge_id','portfolio','accounting_basis','expected_version'},set())
        source=p.get('source') or self.g.source(w)['active_source']
        if source not in {'simulation','imported'}:raise BankError('GRAPH_SOURCE','请选择独立的模拟银行或导入账单来源。')
        view=ALIASES.get(p.get('view'),p.get('view','overview'))
        if view not in VIEWS:raise BankError('GRAPH_VIEW','图谱视图无效。')
        asof=self.g.calendar.clock(w,source)[0]
        try:
            end=date.fromisoformat(p.get('to',(date.fromisoformat(asof).replace(day=1)-timedelta(days=1)).isoformat()));start=date.fromisoformat(p.get('from',end.replace(day=1).isoformat()))
        except (ValueError,TypeError):raise BankError('GRAPH_RANGE','日期应为YYYY-MM-DD。')
        if not 1<=(end-start).days+1<=366:raise BankError('GRAPH_RANGE','图谱期间最多366天。')
        for f in ('question','node_id','insight_id','edge_id'):
            if f in p and (not isinstance(p[f],str) or len(p[f])>500):raise BankError('GRAPH_QUERY','查询内容无效。')
        return view,source,start,end,asof

    def query(self,w,parameters=None,for_agent=False,_stale_retries=1):
        p=parameters or {};view,source,start,end,asof=self._parameters(w,p)
        portfolio=p.get('portfolio','current')
        if not isinstance(portfolio,str) or len(portfolio)>100:raise BankError('GRAPH_PORTFOLIO','组合标识无效。')
        if portfolio!='current' and p.get('accounting_basis','total_equity_v2')!='total_equity_v2':raise BankError('GRAPH_BASIS','此版本组合仅支持 total_equity_v2。')
        basis=p.get('accounting_basis','holdings_only_v1' if portfolio=='current' else 'total_equity_v2')
        if portfolio=='current' and basis!='holdings_only_v1':raise BankError('GRAPH_BASIS','旧账户组合仅支持 holdings_only_v1；权益法请指定历史组合。')
        if basis not in {'total_equity_v2','holdings_only_v1'}:raise BankError('GRAPH_BASIS','核算口径无效。')
        version=self._version(w)
        if p.get('expected_version') and p['expected_version']!=version:raise BankError('GRAPH_STALE','对象来源或组合版本已变化，请重新读取。',409)
        cache_key=(w,version,view,source,start.isoformat(),end.isoformat(),portfolio,basis)
        cached=self._cached(cache_key)
        if cached is not None:return self._subgraph(cached,p,view,for_agent)
        started=time.monotonic()
        archive=self.g.bank.customer_archive(w);records=archive['records']
        nodes={};edges=[];calculations=[];insights=[];gaps=[];metrics=[];portfolio_meta=None
        period={'from':start.isoformat(),'to_exclusive':(end+timedelta(days=1)).isoformat()}
        def ref(table,id,scope=source,**fields):return {'source_scope':scope,'source_table':table,'source_id':str(id),'source_version':version,**fields}
        def node(id,label,kind,scope=source,**fields):
            nodes[id]={'id':id,'label':str(label)[:180],'kind':kind,'group':kind,'source_scope':scope,**fields};return id
        def edge(a,b,verb,refs,amount=None,currency=None,derivation='observed',**fields):
            if a not in nodes or b not in nodes:return
            relation_period=period
            if fields.get('occurred_at'):
                on=local_day(fields['occurred_at']);relation_period={'from':on.isoformat(),'to_exclusive':(on+timedelta(days=1)).isoformat()}
            edges.append({'id':key(w,a,b,verb,relation_period),'source':a,'target':b,'relation':verb,'predicate':verb,
                          'source_scope':source,'evidence_refs':refs,'amount_minor':amount,'currency':currency,'period':relation_period,
                          'derivation_type':derivation,'status':'candidate' if derivation=='candidate' else 'active',
                          'record_version':version,'ontology_version':ONTOLOGY,'method_version':METHOD,**fields})
        accounts={a['id']:a for a in archive['accounts']}
        merchant_names={r['id']:r['name'] for r in records.get('merchants',[])}
        if source=='simulation':
            txs=list({r['id']:r for r in records.get('archived_transactions',[])+records.get('transactions',[])}.values())
        else:txs=self.g.statements.transactions(w)
        projected,_,_=self.g.calendar._project(w,start-timedelta(days=(end-start).days+1),end,source,False,False,{'transaction'})
        if portfolio!='current':projected=[]
        if source=='simulation' and portfolio=='current':
            bills={r['id']:r for r in records.get('credit_statements',[])};cards={r['id']:r for r in records.get('cards',[])}
            bank_credit_ids={r.get('credit_line_id')for r in txs if r.get('credit_line_id')}
            for line in records.get('credit_statement_lines',[]):
                if line['id']in bank_credit_ids or line['kind'] not in {'purchase','refund'}:continue
                bill=bills.get(line['statement_id'],{});card=cards.get(bill.get('card_id'),{});currency=accounts.get(card.get('account_id'),{}).get('currency')
                if not currency:continue
                item={'source_ref':line['id'],'currency':currency,'amount_minor':line['amount_minor'],'transaction_kind':'expense' if line['kind']=='purchase' else 'refund','occurred_at':line['occurred_on'],'title':'信用卡消费'}
                if start-timedelta(days=(end-start).days+1)<=local_day(item['occurred_at'])<=end:projected.append(item);txs.append({**line,'category':'信用卡消费','source_table':'credit_statement_lines'})
        previous_start=start-timedelta(days=(end-start).days+1)
        tx_lookup={r['id']:r for r in txs}
        buckets=defaultdict(lambda:{'rows':[],'previous':[]})
        for item in projected:
            row=tx_lookup.get(item['source_ref'],{})
            currency=item.get('currency');kind=classify({**item,'investment_link':row.get('product_id')or row.get('investment_lot_id')})
            if kind not in {'expense','refund','income','fee','interest'}:continue
            category=row.get('category') or {'expense':'支出','refund':'退款','income':'收入','fee':'费用','interest':'利息'}[kind]
            direction='收入' if kind=='income' else '支出'
            b=buckets[(currency,direction,category)]
            b['rows' if start<=local_day(item['occurred_at'])<=end else 'previous'].append((item,row,kind))
        for (currency,direction,category),b in sorted(buckets.items(),key=lambda x:str(x[0])):
            rows=b['rows'];past=b['previous']
            if not rows:continue
            amount=sum((-i['amount_minor'] if k=='refund' else i['amount_minor']) for i,r,k in rows)
            prev=sum((-i['amount_minor'] if k=='refund' else i['amount_minor']) for i,r,k in past)
            refs=[ref(r.get('source_table','transactions'),i['source_ref'],occurred_at=i['occurred_at'],amount_minor=i['amount_minor'],kind=k) for i,r,k in rows]
            cid=node('category:'+key(source,currency,direction,category),category,'category',amount_minor=amount,currency=currency,
                     metric_family='period_'+direction,transaction_count=len(rows),refund_policy='期间净消费（包括未付信用卡消费）：本期退款抵减本期，不回填原消费期间',evidence_refs=refs)
            mid=node('metric:'+key(source,currency,direction),direction+'去向' if direction=='支出' else '收入来源','concept',metric_family='period_'+direction,currency=currency)
            edge(mid,cid,'经济净'+direction,refs,amount,currency,'calculated',metric_definition='支出类型金额减本期退款；收入仅收入类型；不把本金、内部转账、还款本金计为消费。')
            merchants=defaultdict(list)
            for i,r,k in rows:
                identity=r.get('merchant_id') or key(r.get('merchant',i['title']))
                merchants[identity].append((i,r,k))
            for identity,mrows in merchants.items():
                r=mrows[0][1];label=r.get('merchant') or merchant_names.get(r.get('merchant_id')) or '未标注商户'
                mid=node('merchant:'+key(source,currency,category,identity),label,'merchant',amount_minor=sum(-i['amount_minor'] if k=='refund' else i['amount_minor'] for i,r,k in mrows),currency=currency,metric_family='period_'+direction,transaction_count=len(mrows),evidence_refs=[ref(r.get('source_table','transactions'),i['source_ref']) for i,r,k in mrows])
                edge(cid,mid,'发生于',nodes[mid]['evidence_refs'],nodes[mid]['amount_minor'],currency,'calculated')
            allrefs=refs+[ref(r.get('source_table','transactions'),i['source_ref'],occurred_at=i['occurred_at'],amount_minor=i['amount_minor'],kind=k,comparison_period=True) for i,r,k in past]
            calc={'id':key(cid,period),'metric_definition':'期间经济净'+direction,'amount_minor':amount,'currency':currency,'period':period,'input_refs':allrefs,'method_version':METHOD,
                  'previous_period':{'from':previous_start.isoformat(),'to_exclusive':start.isoformat()},
                  'count':len(rows),'average_minor':int((Decimal(amount)/len(rows)).quantize(Decimal('1'),rounding=ROUND_HALF_UP)),
                  'previous_amount_minor':prev if past else None,'change_minor':amount-prev if past else None,
                  'comparison_coverage':'recorded_rows_only' if past else 'unavailable','comparison_notice':'等长相邻期间的已记录流水比较；未证明完整覆盖，不代表真实总消费变化。'}
            calculations.append(calc)
            if direction in {'支出','收入'}:insights.append({'id':key(cid,period,'insight'),'view':'funds','title':category+('本期净消费' if direction=='支出' else '本期已记录收入'),'amount_minor':amount,'currency':currency,'node_ids':[cid], 'calculation_id':calc['id'],'evidence_refs':allrefs,'change_minor':calc['change_minor'],'coverage':calc['comparison_coverage'],'notice':calc['comparison_notice']})
        if source=='simulation' and portfolio=='current':
            for a in accounts.values():node('account:'+a['id'],a['name'],'account',balance_minor=a['balance_minor'],currency=a['currency'],metric_family='cash_stock',amount_minor=a['balance_minor'],evidence_refs=[ref('accounts',a['id'])])
            products={p['id']:p for p in self.g.bank.products(w)}
            for lot in records.get('investment_lots',[]):
                if lot['remaining_minor']<=0:continue
                product=products.get(lot['product_id'],{})
                lid=node('holding:'+lot['id'],product.get('name',lot['product_id'])+'持仓批次','holding',amount_minor=lot['remaining_minor'],currency=product.get('currency'),metric_family='holding_principal',date=lot['locked_until'],
                         evidence_refs=[ref('investment_lots',lot['id'])],notice='持仓本金不是已结算现金；解锁后仍需申请赎回并核对结算，未计入未来到账。')
                edge('account:'+lot['account_id'],lid,'持有本金',[ref('investment_lots',lot['id'])],lot['remaining_minor'],product.get('currency'),'observed',valid_from=lot['acquired_on'],valid_to=lot['locked_until'])
            for s in records.get('subscriptions',[]):
                sid=node('contract:'+s['id'],s['plan_name'],'contract',amount_minor=s['amount_minor'],currency=accounts[s['account_id']]['currency'],metric_family='contract_cycle',status=s['merchant_status'],cycle=s['cycle'],next_on=s['next_renewal_on'],evidence_refs=[ref('subscriptions',s['id'])])
                edge('account:'+s['account_id'],sid,'续费候选',[ref('subscriptions',s['id'])],s['amount_minor'],nodes[sid]['currency'],'candidate',occurred_at=s['next_renewal_on'])
                # 实际扣费只能通过明确 mandate_id 对应；同名商户不会自动匹配。
                paid=[r for r in txs if r.get('mandate_id')==s.get('mandate_id') and s.get('mandate_id') and start<=local_day(r['occurred_at'])<=end and r.get('kind')=='expense']
                annual=s['amount_minor']*(12 if s['cycle']=='monthly' else 1)
                insights.append({'id':key(sid,period,'cost'),'view':'cost','title':s['plan_name']+'合同与实际扣费','node_ids':[sid], 'amount_minor':sum(r['amount_minor'] for r in paid) if paid else None,'currency':nodes[sid]['currency'],
                                 'annual_hypothesis_minor':annual,'coverage':'contract_and_linked_records','evidence_refs':[ref('subscriptions',s['id'])]+[ref('transactions',r['id']) for r in paid],
                                 'notice':'年度化是假设全年按现合同续费，不是已发生费用或可保证节省金额。记录频率不等于偏好；价值由你确认。'})
            goals=records.get('wealth_goals',[])
            with closing(self.g.bank.connect()) as db:alloc=[dict(r) for r in db.execute('SELECT * FROM goal_allocations WHERE workspace_id=?',(w,))]
            for goal in goals:
                gid=node('goal:'+goal['id'],goal['name'],'goal',target_minor=goal['target_minor'],due_on=goal['due_on'],priority=goal['priority'],saved_minor=goal['saved_minor'],currency='CNY',metric_family='goal_target',amount_minor=goal['target_minor'],evidence_refs=[ref('wealth_goals',goal['id'])])
                assigned=[a for a in alloc if a['goal_id']==goal['id'] and a['account_id'] in accounts]
                for a in assigned:edge('account:'+a['account_id'],gid,'本人确认规划分配',[ref('goal_allocations',a['id'],'user_confirmed')],a['amount_minor'],a['currency'],'confirmed')
                insights.append({'id':key(gid,'goal'),'view':'goals','title':goal['name']+'的资金约束','node_ids':[gid]+['account:'+a['account_id'] for a in assigned],
                    'amount_minor':sum(a['amount_minor'] for a in assigned),'target_minor':goal['target_minor'],'currency':'CNY','due_on':goal['due_on'], 'coverage':'confirmed_allocations_only',
                    'evidence_refs':[ref('wealth_goals',goal['id'])]+[ref('goal_allocations',a['id'],'user_confirmed') for a in assigned],
                    'notice':'仅列本人确认的规划分配，不是银行预留；saved_minor 不自动代表某账户已分配给此目标。'})
            timeline=self.g.cashflow_timeline(w,90)
            for slot in timeline['timeline']:
                for e in slot['events']:
                    derivation='confirmed' if e['certainty']=='confirmed' else 'candidate'
                    eid=node('event:'+e['id'],e['title'],'event',amount_minor=e['amount_minor'],currency=e['currency'],metric_family='future_obligation',date=e['date'],certainty=e['certainty'],
                             derivation_type=derivation,status='active' if derivation=='confirmed' else 'candidate',
                             account_id=e['account_id'],cash_delta_minor=e['cash_delta_minor'],event_type=e['kind'],
                             source_table=e['source_table'],source_id=e['evidence_id'],
                             evidence_refs=[ref(e['source_table'],e['evidence_id'],occurred_at=e['date'])])
                    edge('account:'+e['account_id'],eid,'预计到账' if e['cash_delta_minor']>0 else '释放预留' if e['cash_delta_minor']==0 else '应付' if e['certainty']=='confirmed' else '可能扣费',nodes[eid]['evidence_refs'],abs(e['cash_delta_minor']),e['currency'],'confirmed' if e['certainty']=='confirmed' else 'candidate',occurred_at=e['date'])
            for a in timeline['accounts']:
                insights.append({'id':key('liquidity',a['account_id']),'view':'liquidity','title':a['account_name']+'未来90天可用资金','node_ids':['account:'+a['account_id']], 'amount_minor':a['minimum_available_after_confirmed_minor'],'candidate_minimum_minor':a['minimum_available_if_candidates_occur_minor'],'currency':a['currency'], 'coverage':'bank_timeline', 'evidence_refs':[ref('accounts',a['account_id'])]+[ref(e['source_table'],e['evidence_id']) for s in timeline['timeline'] for e in s['events'] if e['account_id']==a['account_id']], 'notice':timeline['notice']})
        else:
            timeline=None;gaps.append('导入流水没有可核验的当前余额、合同、持仓和未来承诺，目标与流动性视图未混用模拟银行。')
        # 四板块均有明确对象范围；缺失证据不制造资产或家庭责任。
        if source=='simulation' and portfolio=='current':
            liability_by_currency=defaultdict(int)
            for loan in records.get('loans',[]):
                if loan['status']!='active':continue
                c=accounts.get(loan['repayment_account_id'],{}).get('currency','CNY');amount=loan['outstanding_minor'];liability_by_currency[c]+=amount
                lid=node('liability:'+loan['id'],loan['loan_type'],'contract',role='liability',amount_minor=amount,currency=c,metric_family='liability_stock',date=loan['matures_on'],evidence_refs=[ref('loans',loan['id'])])
                edge(lid,'account:'+loan['repayment_account_id'],'偿还责任来源',nodes[lid]['evidence_refs'],amount,c,'observed')
            for card in records.get('cards',[]):
                if card['type']!='credit':continue
                c=accounts.get(card['account_id'],{}).get('currency','CNY');amount=card['outstanding_minor'];liability_by_currency[c]+=amount
                lid=node('liability:'+card['id'],'信用卡未偿余额','contract',role='liability',amount_minor=amount,currency=c,metric_family='liability_stock',evidence_refs=[ref('cards',card['id'])]);edge(lid,'account:'+card['account_id'],'偿还责任来源',nodes[lid]['evidence_refs'],amount,c)
            receivable_by_currency=defaultdict(int)
            for order in records.get('orders',[]):
                if order['status']!='pending_settlement':continue
                c=accounts.get(order['account_id'],{}).get('currency','CNY');amount=order['amount_minor'];receivable_by_currency[c]+=amount
                rid=node('receivable:'+order['id'],'赎回待到账','holding',role='receivable',amount_minor=amount,currency=c,metric_family='redemption_receivable',evidence_refs=[ref('orders',order['id'])],notice='已离开持仓、尚未进入可用现金，仍是本人资产。');edge(rid,'account:'+order['account_id'],'结算后到账',nodes[rid]['evidence_refs'],amount,c,'confirmed')
            for c in sorted({a['currency']for a in accounts.values()}):
                cash=sum(a['balance_minor']for a in accounts.values()if a['currency']==c);principal=sum(h['amount_minor']for h in records.get('holdings',[])if accounts.get(h['account_id'],{}).get('currency')==c)
                debt=liability_by_currency[c];nid=node('net-assets:'+w+':'+c,'已覆盖财富结构','concept',role='net_assets',amount_minor=cash+principal+receivable_by_currency[c]-debt,currency=c,metric_family='net_assets_principal',notice='旧组合只有本金口径；市场价值缺项，净资产完整市值未知。',evidence_refs=[ref('accounts',a['id'])for a in accounts.values()if a['currency']==c])
                for n in list(nodes.values()):
                    if n['id']==nid or n.get('currency')!=c:continue
                    if n['kind']in {'account','holding'}:edge(n['id'],nid,'构成资产',n.get('evidence_refs',[]),n.get('amount_minor'),c,'calculated')
                    elif n.get('role')=='liability':edge(n['id'],nid,'抵减净资产',n.get('evidence_refs',[]),n.get('amount_minor'),c,'calculated')
                m={'currency':c,'cash_minor':cash,'holding_principal_minor':principal,'pending_redemption_minor':receivable_by_currency[c],'liabilities_minor':debt,'net_assets_minor':None,'known_principal_net_assets_minor':cash+principal+receivable_by_currency[c]-debt,'coverage':'principal_only','as_of':asof};metrics.append(m)
                insights.append({'id':key(nid,version),'view':'wealth','title':'资产与负债','amount_minor':None,'known_principal_net_assets_minor':cash+principal+receivable_by_currency[c]-debt,'currency':c,'node_ids':[nid],'evidence_refs':nodes[nid]['evidence_refs'],'notice':'已结算现金 '+str(cash/100)+'；负债 '+str(debt/100)+'。持仓仍为本金口径，完整市场净资产待补。'})
            for person in records.get('relationships',[]):
                node('person:'+person['id'],person['display_name'],'person',role='family',relationship=person['relationship'],evidence_refs=[ref('relationships',person['id'])],notice='仅表示已记录关系，不自动推断赡养义务、责任份额或读取对方账户。')
            gaps.append('家庭责任、保障合同及其本人承担份额尚需明确确认；目标规划不是资产或扣款。')
        if source=='simulation' and portfolio!='current':
            points,portfolio_meta=self.g.wealth.points(w,'1900-01-01',end.isoformat(),portfolio)
            latest={}
            for point in points:latest[(point['account_id'],point['fund_id'])]=point
            from wealth_returns import calculate
            positions=self.g.wealth.positions(w,portfolio_meta['id']) if portfolio_meta else []
            expected={(x['account_id'],x['fund_id'])for x in positions};available=set(latest)
            for identity in expected-available:gaps.append('估值缺失：'+identity[0]+' / '+identity[1])
            for pos in positions:
                identity=(pos['account_id'],pos['fund_id']);point=latest.get(identity);r=calculate(point) if point else {'status':'unavailable','missing':['来源估值缺失']}
                if r.get('closing_equity_minor') is None:gaps.append('权益缺项：'+pos['fund_id'])
                nid=node('equity:'+pos['account_id']+':'+pos['fund_id'],pos['product']['name'],'holding',amount_minor=r.get('closing_equity_minor'),currency='CNY',metric_family='investment_equity_stock',nav_asof=point['nav_asof'] if point else None,fund_id=pos['fund_id'],portfolio_version=portfolio_meta['id'],valuation=point,returns=r,evidence_refs=[ref('portfolio_valuations',pos['fund_id'],'simulation')],notice='独立历史模拟组合，不与当前银行现金/旧持仓相加。')
                insights.append({'id':key(nid,version),'view':'wealth','title':pos['product']['name']+'权益','amount_minor':r.get('closing_equity_minor'),'currency':'CNY','node_ids':[nid],'evidence_refs':nodes[nid]['evidence_refs'],'notice':('净值日期 '+point['nav_asof']+'；权益法，净值已含运作费用。') if point else '来源估值缺失，金额未知。'})
            totals=self.g.wealth.aggregate(list(latest.values()),expected)
            metrics.append({'currency':'CNY','investment_equity_minor':totals.get('closing_equity_minor'),'known_investment_equity_minor':totals.get('known_closing_equity_minor'),'as_of':end.isoformat(),'nav_asof':max((p['nav_asof']for p in latest.values()),default=None),'coverage':'historical_simulation_only','equity_coverage':'complete' if totals.get('closing_equity_minor') is not None else 'partial'})
        # 日常结余只由经济收入/净消费组成；投资、还款本金及借款另列。
        for c in sorted({n.get('currency')for n in nodes.values()if n.get('currency')}):
            incomes=[n for n in nodes.values()if n.get('metric_family')=='period_收入'and n['kind']=='category'];costs=[n for n in nodes.values()if n.get('metric_family')=='period_支出'and n['kind']=='category']
            incomes=[n for n in incomes if n['currency']==c];costs=[n for n in costs if n['currency']==c]
            if not incomes and not costs:continue
            inc=sum(n['amount_minor']for n in incomes);expense=sum(n['amount_minor']for n in costs)
            nid=node('surplus:'+w+':'+c,'本期日常结余','concept',role='surplus',amount_minor=inc-expense if incomes else None,currency=c,metric_family='period_surplus',notice='已记录收入减净消费；没有收入证据时保持未知。借款、投资本金、内部转账及还款本金不计。',evidence_refs=[r for n in incomes+costs for r in n['evidence_refs']])
            for n in incomes:edge(n['id'],nid,'形成收入',n['evidence_refs'],n['amount_minor'],c,'calculated')
            for n in costs:edge(nid,n['id'],'用于消费',n['evidence_refs'],n['amount_minor'],c,'calculated')
            metrics.append({'currency':c,'income_minor':inc if incomes else None,'net_consumption_minor':expense if costs else None,'daily_surplus_minor':inc-expense if incomes else None,'coverage':'recorded_evidence_only','period':period})
            insights.insert(0,{'id':key(nid,period),'view':'cashflow','title':'日常收支与结余','amount_minor':nodes[nid]['amount_minor'],'currency':c,'node_ids':[nid]+[n['id']for n in incomes+costs],'evidence_refs':nodes[nid]['evidence_refs'],'notice':nodes[nid]['notice']})
        memory=self.g.memories.state(w)
        show_memory=portfolio=='current' and (memory['enabled'] or not for_agent)
        if show_memory:
            for m in memory['items']:
                if m['expired'] or (for_agent and not m['user_confirmed']):continue
                scope='user_confirmed' if m['user_confirmed'] else 'unverified_personal'
                node('memory:'+m['node_id'],m['value'],'memory',scope,category=m['category'],role='preference',
                     status='confirmed' if m['user_confirmed'] else 'unverified',user_confirmed=m['user_confirmed'],
                     evidence_refs=[ref('user_memories',m['node_id'],scope)])
            with self.g.memories.store.connection(w) as db:
                relations=[dict(r) for r in db.execute('SELECT * FROM user_memory_relations WHERE workspace_id=?',(w,))]
                observations=[dict(r) for r in db.execute('SELECT * FROM profile_observations WHERE workspace_id=?',(w,))]
            for r in relations:edge('memory:'+r['source_node_id'],'memory:'+r['target_node_id'],r['relation'],[ref('user_memory_relations',r['id'],'user_confirmed')],derivation='confirmed')
            for o in observations:
                if (o.get('expires_on') and o['expires_on']<asof) or (for_agent and (o['status']!='confirmed' or not o['source_verified'])):continue
                node('observation:'+o['id'],o['value'],'memory','conversation',status=o['status'],observation_id=o['id'],quote=o['quote'],confirmation_token=o['confirmation_token'],expected_revision=o['expected_revision'],evidence_refs=[ref('profile_observations',o['id'],'conversation')])
        if show_memory:
            for n in list(nodes.values()):
                if n['kind']!='memory':continue
                category=n.get('category','unknown');topic={'goal':'目标条件','risk':'风险边界','preference':'选择条件','life':'生活安排','finance':'资金条件','knowledge':'理解记录'}.get(category,'本人陈述')
                cid=node('condition:'+w+':'+category,topic,'concept','user_confirmed',role='condition',evidence_refs=n.get('evidence_refs',[]))
                edge(n['id'],cid,'提出条件',n.get('evidence_refs',[]),derivation='confirmed' if n.get('status')=='confirmed' else 'candidate')
                insights.append({'id':key(n['id'],'preference'),'view':'preferences','title':n['label'],'amount_minor':None,'node_ids':[n['id'],cid],'evidence_refs':n.get('evidence_refs',[]),'notice':'本人确认条件与待核实陈述分开；阅读或购买记录不证明理解、偏好或适当性。'})
            if not any(n['kind']=='memory'for n in nodes.values()):gaps.append('偏好与认知缺少本人确认条件、选择理由和执行反馈；可通过对话补充，不生成认知评分。')
        if show_memory and portfolio=='current':
            with closing(self.g.bank.connect())as db:decisions=[json.loads(r['payload']) for r in db.execute("SELECT payload FROM personal_decisions WHERE workspace_id=? AND status='confirmed'",(w,))]
            for decision in decisions:
                if decision.get('expires_on') and decision['expires_on']<asof:continue
                did=node('decision:'+decision['id'],decision['choice'],'memory','user_confirmed',role='decision',status='confirmed',decision=decision,evidence_refs=[ref('personal_decisions',decision['id'],'user_confirmed')],notice='本人确认选择；执行只由当前账户回执核验，未执行仍未知。')
                for index,option in enumerate(decision['alternatives']):
                    oid=node('alternative:'+decision['id']+':'+str(index),option,'concept','user_confirmed',role='alternative',evidence_refs=decision['evidence_refs'])
                    edge(oid,did,'选择并说明原因' if option==decision['choice'] else '已考虑的替代项',nodes[did]['evidence_refs'],derivation='confirmed',reason=decision['reason'])
                for r in decision['evidence_refs']:
                    targets=[n['id'] for n in nodes.values() if n['id']!=did and any(x['source_id']==r['source_id']and x['source_table']==r['source_table']for x in n.get('evidence_refs',[]))]
                    for target in targets:edge(target,did,'作为选择依据',[ref(r['source_table'],r['source_id'],r['source_scope'],declared_source_version=r.get('source_version'))],derivation='confirmed')
                insights.append({'id':key(w,did),'view':'preferences','title':decision['choice'],'amount_minor':None,'node_ids':[did],'evidence_refs':nodes[did]['evidence_refs'],'notice':decision['reason']+'；执行证据：'+('已有当前账户回执' if decision['execution_ids'] else '尚无')})
        for item in (self.g.calendar.events.list(w,start,max(end,date.fromisoformat(asof)+timedelta(days=90))) if portfolio=='current' else []):
            eid=node('calendar:'+item['id'],item['title'],'event','user_confirmed',amount_minor=item['budget_minor'],currency=item['currency'],metric_family='calendar_budget',date=item['date'],status=item['state'],
                     evidence_refs=[ref('calendar_series',item['series_id'],'user_confirmed')],notice='本人日历预算，不是银行已支付金额。')
            if source=='simulation':
                for commitment_id in item['linked_commitment_ids']:
                    linked=[n['id'] for n in nodes.values() if n['id']!=eid and any(r['source_id']==commitment_id for r in n.get('evidence_refs',[]))]
                    for target in linked:edge(eid,target,'本人关联承诺',nodes[eid]['evidence_refs'],derivation='confirmed',occurred_at=item['date'])
            if show_memory:
                for memory_id in item['linked_memory_ids']:edge(eid,'memory:'+memory_id,'本人关联计划',nodes[eid]['evidence_refs']+[ref('user_memories',memory_id,'user_confirmed')],derivation='confirmed',occurred_at=item['date'])
        gaps+=['缺少已确认的雇主行业、产品底层资产权重和贷款重定价条款，不能计算共同暴露、相关性或损失。',
               '未确认跨期退款原单、AA款项和转账对手的唯一关联；只按原记录展示，不自动合并人物或重述消费。']
        insights.append({'id':key('risk',source),'view':'risk','title':'共同风险需要哪些依据','node_ids':[], 'coverage':'unavailable','amount_minor':None,'evidence_refs':[], 'notice':gaps[-2]})
        public={'workspace':w,'source_scope':source,'ontology_version':ONTOLOGY,'method_version':METHOD,'version':version,'as_of':asof,'source':source,'period':period,
                'memory_enabled':memory['enabled'],'personal_revision':memory['personal_revision'],'nodes':list(nodes.values()),'links':edges,'calculations':calculations,
                'insights':insights,'metrics':metrics,'portfolio':portfolio_meta,'portfolio_version':portfolio_meta['id'] if portfolio_meta else 'legacy-v1','accounting_basis':basis,'nav_asof':portfolio_meta.get('to') if portfolio_meta else None,'data_version':version,'coverage':{'status':'partial','gaps':gaps},'timeline':timeline if view=='plans' else None,
                'related_knowledge':[c for c in json.loads((Path(__file__).parent/'knowledge/financial-concepts.json').read_text())['concepts'] if view in c['views'] or any(ALIASES.get(v)==view for v in c['views'])],
                'notice':'金额来自确定性账本计算；观测、本人确认、候选与情景分开。图谱不替代授权，也不把消费频率当作偏好。'}
        for i in insights:i['view']=ALIASES.get(i['view'],i['view'])
        try:
            if version!=self._version(w):raise BankError('GRAPH_STALE','读取期间资料已变化，请重试。',409)
            self._persist(w,public)
        except BankError as late:
            # 并发写入在读期间推进版本时重建一次，避免把可重试的竞态直接抛给页面。
            if late.code=='GRAPH_STALE' and _stale_retries>0:return self.query(w,parameters,for_agent,_stale_retries-1)
            raise
        self._remember(cache_key,public)
        elapsed=time.monotonic()-started
        if elapsed>=1.0:logging.getLogger('navren.graph').info('graph_build workspace=%s seconds=%.2f nodes=%d edges=%d',w,elapsed,len(public['nodes']),len(public['links']))
        return self._subgraph(public,p,view,for_agent)

    def _persist(self,w,data):
        """当前投影原子替换；删除记忆后不保留可复活其内容的旧实体/证据快照。"""
        # 投影版本未变时跳过整表重写：读取路径不再因为重复落库长期占用写锁。
        with closing(self.g.bank.connect()) as probe:
            stored=probe.execute('SELECT record_version FROM graph_assertions WHERE workspace_id=? LIMIT 1',(w,)).fetchone()
        if stored is not None and stored[0]==data['version']:
            return
        with self.g.bank.transaction() as db:
            if self._version(w,db)!=data['version']:raise BankError('GRAPH_STALE','图谱投影依据已变化。',409)
            for table in ('graph_evidence_links','graph_assertions','graph_events','graph_entities','graph_calculations'):
                db.execute('DELETE FROM '+table+' WHERE workspace_id=?',(w,))
            for n in data['nodes']:
                db.execute('INSERT INTO graph_entities VALUES(?,?,?,?,?,?)',(w,n['id'],n['kind'],n['source_scope'],n['label'],packed(n)))
                if n['kind']=='event':db.execute('INSERT INTO graph_events VALUES(?,?,?,?,?,?,?)',(w,n['id'],n['id'],n['date'],n.get('amount_minor'),n.get('currency'),n.get('certainty','unknown')))
            for e in data['links']:
                db.execute('INSERT INTO graph_assertions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(w,e['id'],e['source'],e['target'],e['predicate'],e['amount_minor'],e['currency'],e.get('valid_from',e['period']['from']),e.get('valid_to',e['period']['to_exclusive']),e['derivation_type'],data['version'],ONTOLOGY,self.g.bank.clock(),packed(e)))
                for r in e['evidence_refs']:
                    db.execute('INSERT OR IGNORE INTO graph_evidence_links VALUES(?,?,?,?,?,?,?)',(w,e['id'],r['source_scope'],r['source_table'],r['source_id'],r['source_version'],packed(r)))
            for c in data['calculations']:
                db.execute('INSERT INTO graph_calculations VALUES(?,?,?,?,?,?,?,?,?,?)',(w,c['id'],METHOD,c['metric_definition'],data['period']['from'],data['period']['to_exclusive'],c['currency'],c['amount_minor'],packed(c['input_refs']),packed(c)))
            db.execute('DELETE FROM graph_scenarios WHERE workspace_id=? AND input_version<>?',(w,data['version']))

    def _subgraph(self,data,p,view,for_agent):
        import networkx as nx
        graph=nx.MultiDiGraph();graph.add_nodes_from(n['id'] for n in data['nodes'])
        for e in data['links']:graph.add_edge(e['source'],e['target'])
        chosen=[i for i in data['insights'] if view=='overview' or i['view']==view]
        seeds=[];selected_edge=None
        if p.get('edge_id'):
            selected_edge=next((e for e in data['links']if e['id']==p['edge_id']),None)
            if selected_edge is None:raise BankError('GRAPH_NOT_FOUND','关系已失效或不属于当前账户。',404)
            seeds=[selected_edge['source'],selected_edge['target']]
        if p.get('insight_id'):
            match=next((i for i in data['insights']if i['id']==p['insight_id']),None)
            if not match:raise BankError('GRAPH_NOT_FOUND','洞察已失效。',404)
            chosen=[match];seeds+=match['node_ids']
        if p.get('node_id'):
            if p['node_id']not in graph:raise BankError('GRAPH_NOT_FOUND','节点不属于当前账户。',404)
            seeds.append(p['node_id'])
        question=p.get('question','').strip()
        if question:seeds += [n['id']for n in data['nodes']if n['label']in question or any(t in n['label']for t in re.findall(r'[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{2,}',question))]
        if not seeds:
            def relevant(n):
                if view=='overview':return True
                if view=='cashflow':return n['kind']in {'category','merchant'} or n.get('role')=='surplus' or n.get('metric_family')in {'period_收入','period_支出','contract_cycle'}
                if view=='wealth':return n['kind']in {'account','holding'} or n.get('role')in {'liability','net_assets'}
                if view=='plans':return n['kind']in {'goal','event','person','account'} or n.get('role')=='family'
                return n['kind']=='memory' or n.get('role')in {'condition','alternative'}
            keep={n['id']for n in data['nodes']if relevant(n)}
            if view=='preferences':
                keep|={e['source']for e in data['links']if e['target']in keep}|{e['target']for e in data['links']if e['source']in keep}
        else:
            keep=set(seeds);front=set(keep)
            for _ in range(2):
                nxt={o for n in front for o in set(graph.predecessors(n))|set(graph.successors(n))}-keep
                keep|=nxt;front=nxt
        selected=[n for n in data['nodes'] if n['id'] in keep]
        links=[e for e in data['links'] if e['source'] in keep and e['target'] in keep]
        selected_insights=chosen
        calcids={i.get('calculation_id') for i in selected_insights}
        refs=[r for i in selected_insights for r in i['evidence_refs']]+(selected_edge['evidence_refs'] if selected_edge else [])
        return {**data,'view':view,'nodes':selected,'links':links,'insights':selected_insights,
                'selected_relation':selected_edge,
                'calculations':[c for c in data['calculations'] if c['id'] in calcids],
                'subgraph':{'nodes':selected,'links':links},'evidence_refs':refs,'insight_id':selected_insights[0]['id'] if selected_insights else None,
                'metric_definitions':['同币种、同期间的现金净支出；退款按到账期间抵减','账户余额为存量，不与期间支出共用大小比例','年度化费用是合同延续假设','规划分配不是银行预留'],
                'assumptions':['流水覆盖尚未证明完整；缺失值不按零处理','未确认资料不作事实；未知币种不折算'],
                'available_actions':['ask','inspect_evidence']+(['plan_allocation','compare_scenario'] if data['source']=='simulation' else []),
                'answer':'；'.join(i['title']+'：'+i.get('notice','') for i in selected_insights),
                'node_count_total':len(data['nodes']),'edge_count_total':len(data['links'])}

    def allocate(self,w,p):
        strict_fields(p,{'goal_id','account_id','amount_minor','expected_version','confirmed'}, {'goal_id','account_id','amount_minor','expected_version','confirmed'})
        if p['confirmed'] is not True:raise BankError('GRAPH_CONFIRM','需要本人确认规划分配。')
        amount=minor(p['amount_minor'],zero=False)
        archive=self.g.bank.customer_archive(w)
        budget=self.g.budget(w,p['account_id'])
        # 银行 transaction 使用 BEGIN IMMEDIATE，防止两个规划并发超额。
        with self.g.bank.transaction() as db:
            if self._version(w,db)!=p['expected_version']:raise BankError('GRAPH_STALE','资料已变化，请重新核对分配。',409)
            goal=next((r for r in archive['records'].get('wealth_goals',[]) if r['id']==p['goal_id'] and r['status']=='active'),None)
            account=next((a for a in archive['accounts'] if a['id']==p['account_id']),None)
            if not goal or not account:raise BankError('GRAPH_NOT_FOUND','目标或账户不属于当前工作区。',404)
            assigned=db.execute('SELECT COALESCE(SUM(amount_minor),0) FROM goal_allocations WHERE workspace_id=? AND account_id=? AND goal_id<>?',(w,account['id'],goal['id'])).fetchone()[0]
            if assigned+amount>budget['investable_minor']:raise BankError('GRAPH_OVERALLOCATED','分配合计超过扣除现有承诺后的可规划金额。')
            db.execute('INSERT INTO goal_allocations VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(workspace_id,goal_id,account_id) DO UPDATE SET amount_minor=excluded.amount_minor,confirmed_at=excluded.confirmed_at,source_version=excluded.source_version',(w,uuid.uuid4().hex,goal['id'],account['id'],amount,account['currency'],self.g.bank.clock(),p['expected_version']))
        return self.query(w,{'view':'goals','source':'simulation'})

    def delete_allocation(self,w,p):
        strict_fields(p,{'allocation_id'},{'allocation_id'})
        with self.g.bank.transaction() as db:
            r=db.execute('DELETE FROM goal_allocations WHERE workspace_id=? AND id=?',(w,p['allocation_id']))
            if not r.rowcount:raise BankError('GRAPH_NOT_FOUND','规划分配不存在。',404)
        return self.query(w,{'view':'goals','source':'simulation'})

    def evidence(self,w,p):
        strict_fields(p,{'source','table','id'},{'source','table','id'})
        if p['source']=='imported' and p['table']=='transactions':
            rows=self.g.statements.transactions(w)
        elif p['source'] in {'user_confirmed','conversation','unverified_personal'}:
            if p['table']=='user_memories':rows=[{**r,'id':r['node_id']} for r in self.g.memories.state(w)['items']]
            elif p['table'] in {'user_memory_relations','profile_observations'}:
                rows=self.g.memories.store.rows(w,p['table'])
            elif p['table'] in {'goal_allocations','calendar_series','personal_decisions'}:
                with closing(self.g.bank.connect()) as db:rows=[dict(r) for r in db.execute('SELECT * FROM '+p['table']+' WHERE workspace_id=?',(w,))]
                if p['table']=='personal_decisions':rows=[json.loads(r['payload'])for r in rows if r['status']=='confirmed']
                if p['table']=='calendar_series':rows=[{**json.loads(r['payload']),'id':r['id'],'status':r['status']} for r in rows if r['status']=='active']
            else:raise BankError('GRAPH_EVIDENCE','证据类型无效。')
        elif p['source']=='simulation':
            archive=self.g.bank.customer_archive(w)
            allowed={'accounts','transactions','subscriptions','wealth_goals','loans','relationships','loan_installments','credit_statements','credit_statement_lines','cards','mandates','scheduled_payments','orders','fund_holds','investment_lots','recurring_obligations'}
            if p['table'] not in allowed:raise BankError('GRAPH_EVIDENCE','证据类型无效。')
            rows=archive['accounts'] if p['table']=='accounts' else archive['records'].get(p['table'],[])
            if p['table']=='transactions':rows=rows+archive['records'].get('archived_transactions',[])
            if p['table']=='recurring_obligations':
                # 预测来自当前工作区登记的演示快照；历史原件必须仍存在且属于同一主体。
                # 不从旧图投影或审计正文恢复已经移除、已付或失效的候选。
                rows=[{**r,'id':r.get('obligation_id',r.get('id'))} for r in rows
                      if r.get('status')=='forecast_not_paid']
                if 'recurring_obligations' not in archive['records'] and self.g.demo_accounts.is_demo(w):
                    from demo_accounts import SOURCE
                    demos=self.g.demo_accounts
                    current=demos.current(w)
                    forecasts=current.get('recurring_obligations',current.get('forecasts',[]))
                    candidate=next((r for r in forecasts if r.get('obligation_id')==p['id']
                                    and r.get('status')=='forecast_not_paid' and r.get('source')==SOURCE),None)
                    if candidate:
                        record=demos._record(w)
                        with closing(demos._history_connection(record)) as db:
                            rows=[dict(r) for r in db.execute('''SELECT obligation_id AS id,category,
                                amount_minor,frequency,next_due_on,status FROM recurring_obligations
                                WHERE user_id=? AND obligation_id=? AND status='forecast_not_paid'
                                  AND source=?''',(record['user_id'],p['id'],SOURCE))]
        else:raise BankError('GRAPH_EVIDENCE','证据来源无效。')
        row=next((r for r in rows if r['id']==p['id']),None)
        if row is None:raise BankError('GRAPH_NOT_FOUND','原记录已删除或不属于当前账户。',404)
        if p['table']=='user_memories':
            actual_scope='user_confirmed' if row['user_confirmed'] else 'unverified_personal'
            if p['source']!=actual_scope:raise BankError('GRAPH_EVIDENCE_UNVERIFIED','来源标签与个人资料的实际核实状态不符。',409)
        elif p['source']=='unverified_personal':
            raise BankError('GRAPH_EVIDENCE','该来源仅用于未经核实的迁移记忆。')
        elif p['table']=='profile_observations' and p['source']=='user_confirmed':
            if row['status']!='confirmed' or not row['source_verified']:
                raise BankError('GRAPH_EVIDENCE_UNVERIFIED','待核实画像不能当作本人确认事实。',409)
        allowed={'id','choice','reason','alternatives','feedback','execution_ids','evidence_refs','name','title','display_name','relationship','outstanding_minor','loan_type','start_on','budget_minor','plan_name','merchant','category','value','quote','status','occurred_at','amount_minor','delta_minor','kind','currency','balance_minor','target_minor','saved_minor','due_on','next_renewal_on','cycle','principal_minor','interest_minor','fees_minor','paid_minor','total_due_minor','source_node_id','target_node_id','relation','goal_id','account_id','confirmed_at','remaining_minor','original_minor','acquired_on','locked_until','product_id','frequency','next_due_on'}
        return {'record':{k:v for k,v in row.items() if k in allowed},'source':p['source'],'table':p['table'],'version':self._version(w),'notice':'原记录只读；不读取删除资料的审计正文。'}

    def decision(self,w,p):
        strict_fields(p,{'choice','alternatives','reason','evidence_refs','execution_ids','feedback','expires_on','confirmed'}, {'choice','alternatives','reason','evidence_refs','confirmed'})
        if p['confirmed'] is not True:raise BankError('GRAPH_CONFIRM','需要本人确认选择记录。')
        alternatives=p['alternatives']
        if not isinstance(alternatives,list) or not 1<=len(alternatives)<=8 or any(not isinstance(x,str)or not 1<=len(x)<=180 for x in alternatives) or p['choice']not in alternatives:raise BankError('GRAPH_DECISION','选择必须来自明确的替代项。')
        if not isinstance(p['reason'],str) or not 2<=len(p['reason'])<=800:raise BankError('GRAPH_DECISION','请说明本人选择理由。')
        refs=p['evidence_refs']
        if not isinstance(refs,list)or len(refs)>20:raise BankError('GRAPH_DECISION','依据数量无效。')
        for r in refs:
            strict_fields(r,{'source_scope','source_table','source_id'},{'source_scope','source_table','source_id'})
            evidence=self.evidence(w,{'source':r['source_scope'],'table':r['source_table'],'id':r['source_id']})
            r['source_version']=evidence['version']
        executions=p.get('execution_ids',[])
        if not isinstance(executions,list)or len(executions)>20 or any(not isinstance(x,str)or len(x)>100 for x in executions):raise BankError('GRAPH_DECISION','执行回执数量无效。')
        with self.g.bank.transaction()as db:
            self.g.bank._workspace(db,w)
            for eid in executions:
                if not db.execute("SELECT 1 FROM operations WHERE workspace_id=? AND id=? AND state IN ('succeeded','settled','pending_settlement')",(w,eid)).fetchone():raise BankError('GRAPH_DECISION','执行必须有当前账户成功回执。')
            expiry=p.get('expires_on')
            if expiry:
                try:expired=date.fromisoformat(expiry)<date.fromisoformat(self.g.calendar.clock(w)[0])
                except (ValueError,TypeError):raise BankError('GRAPH_DECISION','有效期应为YYYY-MM-DD。')
                if expired:raise BankError('GRAPH_DECISION','选择有效期不能早于当前日期。')
            feedback=p.get('feedback','')
            if not isinstance(feedback,str)or len(feedback)>800:raise BankError('GRAPH_DECISION','反馈内容无效。')
            value={**p,'id':uuid.uuid4().hex,'evidence_refs':refs,'execution_ids':executions,'confirmed_at':self.g.bank.clock(),'feedback':feedback}
            db.execute('INSERT INTO personal_decisions VALUES(?,?,?,?)',(w,value['id'],'confirmed',packed(value)))
        return value

    def scenario(self,w,p):
        strict_fields(p,{'account_id','floor_minor','goals','horizon_days','expected_version'}, {'account_id','expected_version'})
        version=self._version(w)
        if p['expected_version']!=version:raise BankError('GRAPH_STALE','情景依据已变化，请重新读取。',409)
        days=p.get('horizon_days',90)
        if type(days) is not int or not 1<=days<=93:raise BankError('GRAPH_RANGE','情景最多推演93天。')
        timeline=self.g.cashflow_timeline(w,days);account=next((a for a in timeline['accounts'] if a['account_id']==p['account_id']),None)
        if not account:raise BankError('GRAPH_NOT_FOUND','账户不属于当前工作区。',404)
        goals=p.get('goals',[])
        if not isinstance(goals,list) or len(goals)>10:raise BankError('GRAPH_SCENARIO','情景最多10个目标。')
        archive=self.g.bank.customer_archive(w);known={g['id']:g for g in archive['records'].get('wealth_goals',[])}
        plans=[];seen=set()
        for g in goals:
            strict_fields(g,{'goal_id','amount_minor','date','monthly_contribution_minor'},{'goal_id','amount_minor','date'})
            if g['goal_id'] not in known or g['goal_id'] in seen:raise BankError('GRAPH_SCENARIO','目标不存在或重复。')
            seen.add(g['goal_id']);plans.append({**g,'title':known[g['goal_id']]['name'],'currency':account['currency'],'kind':'goal_scenario'})
        start=timeline['as_of'];end=timeline['horizon_end']
        projections=[]
        with closing(self.g.bank.connect()) as db:allocations=[dict(r) for r in db.execute('SELECT * FROM goal_allocations WHERE workspace_id=?',(w,))]
        for goal in goals:
            if 'monthly_contribution_minor' not in goal:continue
            contribution=minor(goal['monthly_contribution_minor'],'每月规划投入')
            try:due=date.fromisoformat(goal['date'])
            except (ValueError,TypeError):raise BankError('GRAPH_RANGE','目标日期无效。')
            begin=date.fromisoformat(start);periods=max(0,(due.year-begin.year)*12+due.month-begin.month)
            allocated=sum(a['amount_minor'] for a in allocations if a['goal_id']==goal['goal_id'] and a['account_id']==p['account_id'])
            projected=allocated+contribution*periods
            projections.append({'goal_id':goal['goal_id'],'allocated_minor':allocated,'monthly_contribution_minor':contribution,'period_count':periods,'projected_planning_amount_minor':projected,'scenario_target_minor':goal['amount_minor'],'planning_gap_minor':max(0,goal['amount_minor']-projected),'notice':'每月投入是单独的规划假设，没有计入现金或预支未来工资；不预测实际完成概率或提前月份。'})
        calendar=self.g.calendar.budget_scenario(w,p['account_id'],start,end) if days<=93 else None
        if calendar:
            events={e['id']:e for e in self.g.calendar.events.list(w,date.fromisoformat(start),date.fromisoformat(end))}
            for c in calendar['items']:
                if c['additional_reserve_minor'] and c['item_id'] in events:plans.append({'date':events[c['item_id']]['date'],'amount_minor':c['additional_reserve_minor'],'currency':account['currency'],'title':c['title'],'kind':'calendar_plan'})
        baseline=liquidity_scenario(timeline,p['account_id'],[])
        result=liquidity_scenario(timeline,p['account_id'],plans,p.get('floor_minor'))
        if version!=self._version(w):raise BankError('GRAPH_STALE','情景计算期间资料已变化。',409)
        id=uuid.uuid4().hex
        result.update(goal_projections=projections,scenario_id=id,input_version=version,source='user_scenario',baseline=baseline,calendar_scenario=calendar,parameters=p)
        with self.g.bank.transaction() as db:db.execute('INSERT INTO graph_scenarios VALUES(?,?,?,?,?,?)',(w,id,version,packed(p),packed(result),self.g.bank.clock()))
        return result
