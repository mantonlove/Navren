"""一个财务日历服务同时供账户和原生 Agent 使用，投影来源而非复制账本。"""
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import closing
from simulated_bank import BankError
from simulated_bank.service import encoded
from simulated_bank.agent_guard import context_hash
from calendar_events import CalendarEvents, day, occurrences
from wealth_returns import calculate

SCHEMA='''
CREATE TABLE IF NOT EXISTS calendar_views(
 id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,parameters TEXT NOT NULL,snapshot TEXT NOT NULL,
 source_versions TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS calendar_items(
 id TEXT NOT NULL,workspace_id TEXT NOT NULL,reference TEXT NOT NULL,
 PRIMARY KEY(id,workspace_id));
CREATE TABLE IF NOT EXISTS agent_widget_history(
 workspace_id TEXT NOT NULL,response_id TEXT NOT NULL,widgets TEXT NOT NULL,created_at REAL NOT NULL,
 PRIMARY KEY(workspace_id,response_id));
CREATE TABLE IF NOT EXISTS wealth_valuation_points(
 workspace_id TEXT NOT NULL,dataset_id TEXT NOT NULL,product_id TEXT NOT NULL,valuation_on TEXT NOT NULL,
 currency TEXT NOT NULL,source TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(workspace_id,dataset_id,product_id,valuation_on));
'''
TOTALS=('recorded_inflow_minor','recorded_outflow_minor','recorded_net_minor','income_minor','expense_minor',
        'refund_minor','transfer_in_minor','transfer_out_minor','investment_principal_in_minor',
        'investment_principal_out_minor','confirmed_due_minor','candidate_due_minor','user_budget_minor',
        'investment_pnl_minor','settled_dividend_minor')


def summation(items):
    totals={}
    for item in items:
        c=item.get('currency')
        if not c: continue
        t=totals.setdefault(c,{'currency':c,**{k:0 for k in TOTALS},'investment_redemption_cash_in_minor':0,
                             'realized_return_minor':0,'unknown_direction_count':0,'pnl_available_count':0,'pnl_missing_count':0,'recorded_count':0})
        delta=item.get('cash_delta_minor');amount=item.get('amount_minor');state=item['state']
        if state=='recorded' and item['kind']=='transaction':
            t['recorded_count']+=1
            if delta is None: t['unknown_direction_count']+=1;continue
            if not item.get('internal_transfer'):
                t['recorded_inflow_minor']+=max(delta,0);t['recorded_outflow_minor']+=max(-delta,0);t['recorded_net_minor']+=delta
            category=item.get('transaction_kind')
            mapping={'income':'income_minor','expense':'expense_minor','refund':'refund_minor'}
            if category in mapping:t[mapping[category]]+=amount
            elif category=='transfer' and not item.get('internal_transfer'):t['transfer_in_minor' if delta>0 else 'transfer_out_minor']+=amount
            elif category in {'purchase','redeem','investment','investment_purchase','investment_redemption'}:
                principal=item.get('investment_principal_minor')
                t['investment_principal_in_minor' if delta>0 else 'investment_principal_out_minor']+=principal if type(principal) is int else amount
                if delta>0:t['investment_redemption_cash_in_minor']+=amount
                realized=item.get('realized_return_minor')
                if type(realized) is int:t['realized_return_minor']+=realized;t['income_minor']+=realized
        elif state=='planned' and type(delta) is int and delta<0:t['confirmed_due_minor']+=-delta
        elif state=='candidate' and type(delta) is int and delta<0:t['candidate_due_minor']+=-delta
        if item['source_type']=='user_calendar' and state=='planned' and type(item.get('budget_minor')) is int:t['user_budget_minor']+=item['budget_minor']
        if item['kind']=='investment_pnl' and item.get('returns',{}).get('status')=='available':
            t['investment_pnl_minor']+=item['returns']['daily_pnl_minor'];t['pnl_available_count']+=1
        if item['kind']=='investment_pnl' and item.get('returns',{}).get('status')!='available':t['pnl_missing_count']+=1
        if item['kind']=='distribution' and state=='recorded' and type(amount) is int:t['settled_dividend_minor']+=amount
    for t in totals.values():
        t['known_investment_pnl_minor']=t['investment_pnl_minor'] if t['pnl_available_count'] else None
        t['pnl_coverage']='partial' if t['pnl_missing_count'] else 'complete' if t['pnl_available_count'] else 'unknown'
        if not t['pnl_available_count'] or t['pnl_missing_count']:t['investment_pnl_minor']=None
        if not t['recorded_count']:
            for key in TOTALS[:10]:t[key]=None
    return list(totals.values())


class FinancialCalendar:
    def __init__(self,g):
        self.g=g;self.bank=g.bank;self.events=CalendarEvents(g)
        with closing(self.bank.connect()) as db: db.executescript(SCHEMA)

    def clock(self,w,source=None):
        source=source or self.g.source(w)['active_source']
        if source=='simulation':return self.bank.customer_archive(w)['as_of'],'simulation'
        return datetime.fromtimestamp(self.bank.clock(),ZoneInfo('Asia/Shanghai')).date().isoformat(),'actual_records'

    def versions(self,w):
        with self.bank.transaction() as db:return {'context':context_hash(db,w),'rules':'calendar-equity-v2'}

    def _project(self,w,begin,end,source,include_life=True,overlay=False,kinds=None,portfolio='current'):
        archive=self.bank.customer_archive(w);records=archive['records'];as_of,clock_mode=self.clock(w,source)
        accounts={a['id']:a['currency'] for a in archive['accounts']};items=[]
        if source=='imported':rows=self.g.statements.transactions(w)
        elif self.g.demo_accounts.is_demo(w):rows=self.bank.transactions(w)
        else:rows=list({r['id']:r for r in records.get('archived_transactions',[])+records.get('transactions',[])}.values())
        op_groups={}
        for r in rows:
            if r.get('operation_id'):op_groups.setdefault(r['operation_id'],[]).append(r)
        for row in ([] if portfolio!='current' else rows):
            occurred=row['occurred_at'];precision=row.get('time_precision','datetime')
            if precision!='date':
                parsed=datetime.fromisoformat(occurred)
                if parsed.tzinfo:occurred=parsed.astimezone(ZoneInfo('Asia/Shanghai')).isoformat()
                else:precision='date'
            d=occurred[:10]
            if not begin<=day(d)<=end:continue
            kind=row['kind'];delta=row.get('delta_minor');internal=False
            group=op_groups.get(row.get('operation_id'),[])
            if row.get('transfer_group_id'):internal=True
            elif kind=='transfer' and source=='simulation':
                internal=len({r['account_id'] for r in group if r['account_id'] in accounts})>1 and sum(r['delta_minor'] for r in group)==0
            if source=='imported' and kind=='transfer':delta=None
            items.append({'id':source+':tx:'+row['id'],'kind':'transaction','date':d,'title':row.get('merchant') or row.get('category') or '银行流水',
                'currency':row.get('currency') or accounts.get(row['account_id']),'amount_minor':row['amount_minor'],'cash_delta_minor':delta,
                'transaction_kind':kind,'category':row.get('category'),'internal_transfer':internal,
                'investment_principal_minor':row.get('investment_principal_minor'),
                'realized_return_minor':row.get('realized_return_minor'),'principal_basis':row.get('principal_basis'),
                'account_id':row['account_id'],'state':'recorded','source_type':source,'source_ref':row['id'],
                'time_precision':precision,'occurred_at':occurred,
                'linked_operation_id':row.get('operation_id'),'allowed_actions':['ask'],'editable_fields':[]})
        if (source=='simulation' or overlay) and portfolio=='current':
            horizon=min(365,max(1,(end-day(archive['as_of'])).days))
            timeline=self.g.cashflow_timeline(w,horizon)
            for slot in timeline['timeline']:
                for e in slot['events']:
                    d=e['scheduled_date']
                    if not begin<=day(d)<=end:continue
                    if e['kind']=='reservation_release':continue
                    if e['kind']=='redemption_settlement':kind='settlement'
                    elif 'subscription' in e['kind'] or 'mandate' in e['kind']:kind='subscription'
                    else:kind='bill'
                    items.append({'id':'simulation:future:'+e['id'],'kind':kind,'date':d,'scheduled_date':d,
                        'title':e['title'],'currency':e['currency'],'amount_minor':e['amount_minor'],'cash_delta_minor':e['cash_delta_minor'],
                        'state':'candidate' if e['certainty']=='candidate' else 'planned','source_type':'simulation',
                        'source_ref':e['evidence_ref'],'evidence_ids':[e['evidence_id']],'linked_commitment_ids':[e['evidence_id']],
                        'overdue':day(d)<day(archive['as_of']),'allowed_actions':['ask'],'editable_fields':[]})
            for h in records.get('holdings',[]):
                d=h.get('locked_until')
                if d and begin<=day(d)<=end:items.append({'id':'simulation:maturity:'+h['product_id']+':'+h['account_id']+':'+d,'kind':'maturity','date':d,
                    'title':h['product_id']+' 解锁日期','currency':accounts[h['account_id']],'amount_minor':h['amount_minor'],'cash_delta_minor':None,
                    'state':'planned','source_type':'simulation','source_ref':h['product_id'],'allowed_actions':['ask'],'editable_fields':[],
                    'notice':'解锁不等于自动赎回或现金到账。'})
            with self.bank.transaction() as db:
                points=[dict(r) for r in db.execute('SELECT * FROM wealth_valuation_points WHERE workspace_id=? AND (valuation_on BETWEEN ? AND ? OR json_extract(payload,\'$.settled_dividend_on\') BETWEEN ? AND ?)',(w,begin.isoformat(),end.isoformat(),begin.isoformat(),end.isoformat()))]
            active_points={}
            for p in points:
                a=json.loads(p['payload']);identity=(a.get('account_id'),p['product_id'],p['valuation_on'])
                previous=active_points.get(identity)
                if previous is None or p['revision']>previous['revision']:active_points[identity]=p
                elif p['revision']==previous['revision'] and a!=json.loads(previous['payload']):
                    conflicted=json.loads(previous['payload']);conflicted['movements_complete']=False;previous['payload']=json.dumps(conflicted)
            for p in active_points.values():
                payload=json.loads(p['payload']);r=calculate(payload)
                if payload.get('account_id') not in accounts:r={'status':'unavailable','daily_pnl_minor':None,'cumulative_pnl_minor':None,'realized_pnl_minor':None,'missing':['估值账户不属于当前工作区']}
                base={'currency':p['currency'],'date':p['valuation_on'],'source_type':'simulation','source_ref':p['dataset_id']+':'+p['product_id'],
                      'source_revision':p['revision'],'account_id':payload.get('account_id'),'valuation_dataset_id':p['dataset_id'],'allowed_actions':['ask'],'editable_fields':[]}
                if begin<=day(p['valuation_on'])<=end:
                    items.append({**base,'id':'simulation:valuation:'+str(payload.get('account_id'))+':'+p['dataset_id']+':'+p['product_id']+':'+p['valuation_on'],
                        'title':p['product_id']+' 当日损益（模拟估值）','kind':'investment_pnl','state':'recorded' if r['status']=='available' else 'unknown',
                        'returns':r,'amount_minor':r['daily_pnl_minor'],'cash_delta_minor':None})
                settled=payload.get('settled_dividend_on')
                if settled and begin<=day(settled)<=end and type(payload.get('settled_dividend_minor')) is int and payload['settled_dividend_minor']>0:
                    items.append({**base,'date':settled,'id':'simulation:dividend:'+str(payload.get('account_id'))+':'+p['dataset_id']+':'+p['product_id']+':'+settled,
                        'title':p['product_id']+' 分红到账（模拟估值样本）','kind':'distribution','state':'recorded',
                        'amount_minor':payload['settled_dividend_minor'],'cash_delta_minor':None})
        if source=='simulation' and portfolio!='current':
            points,meta=self.g.wealth.points(w,begin.isoformat(),end.isoformat(),portfolio)
            with self.bank.transaction() as db:expected={(r[0],r[1]) for r in db.execute('SELECT account_id,fund_id FROM portfolio_positions WHERE workspace_id=? AND portfolio_version=?',(w,meta['id']))} if meta else set()
            byday={}
            for point in points:byday.setdefault(point['valuation_on'],set()).add((point['account_id'],point['fund_id']))
            for on in meta.get('valuation_dates',[]) if meta else []:
                if begin.isoformat()<=on<=end.isoformat():byday.setdefault(on,set())
            for on,present in byday.items():
                for aid,fid in expected-present:items.append({'id':'missing:'+meta['id']+':'+aid+':'+fid+':'+on,'kind':'investment_pnl','date':on,'title':fid,'currency':'CNY','amount_minor':None,'cash_delta_minor':None,'state':'unknown','source_type':'simulation','source_ref':fid,'returns':{'status':'unavailable','daily_pnl_minor':None,'missing':['此产品当期净值缺失']},'allowed_actions':['ask']})
            for point in points:
                r=calculate(point);iid='equity:'+point['portfolio_version']+':'+point['account_id']+':'+point['fund_id']+':'+point['valuation_on']
                items.append({'id':iid,'kind':'investment_pnl','date':point['valuation_on'],'title':point['product_name'],'currency':point['currency'],'amount_minor':r['daily_pnl_minor'],'cash_delta_minor':None,'state':'recorded' if r['status']=='available' else 'unknown','source_type':'simulation','source_ref':point['nav_evidence']['url'],'source_revision':point['portfolio_version'],'portfolio_version':point['portfolio_version'],'fund_id':point['fund_id'],'nav_asof':point['nav_asof'],'interval_days':point['interval_days'],'returns':r,'valuation':point,'allowed_actions':['ask','product_detail'],'editable_fields':[]})
        if include_life and portfolio=='current':
            items+=self.events.list(w,begin,end)
            # 银行亲属资料只在记忆启用时供 Agent/日历使用；手工事件不依赖这个开关。
            if self.g.memories.state(w).get('enabled'):
                for relation in records.get('relationships',[]):
                    if not relation.get('birthday'):continue
                    p={'start_on':relation['birthday'],'recurrence':'yearly','month_policy':'skip'}
                    for d in occurrences(p,begin,end):
                        items.append({'id':'simulation:birthday:'+relation['id']+':'+d.isoformat(),'kind':'birthday','date':d.isoformat(),
                            'title':relation['display_name']+' 生日','state':'planned','source_type':'bank_profile','source_ref':relation['id'],
                            'amount_minor':None,'cash_delta_minor':None,'budget_minor':None,'allowed_actions':['ask'],'editable_fields':[]})
        if source=='simulation' and portfolio=='current' and include_life:
            from goal_service import GoalService
            for goal in records.get('wealth_goals',[]):
                if goal['status']=='active' and goal.get('due_on') and begin<=day(goal['due_on'])<=end:
                    items.append({'id':'simulation:goal:'+goal['id']+':'+goal['due_on'],'kind':'life_event',
                        'date':goal['due_on'],'title':goal['name']+' · 目标截止日','goal_id':goal['id'],
                        'linked_role':'goal_deadline','source_revision':GoalService._item(goal)['object_version'],
                        'state':'planned','source_type':'simulation','source_ref':goal['id'],'currency':'CNY',
                        'amount_minor':None,'cash_delta_minor':None,'budget_minor':None,
                        'allowed_actions':['ask','goal_detail'],'editable_fields':[]})
        if kinds:items=[i for i in items if i['kind'] in kinds]
        for i in items:i.update(workspace_binding=w,timezone='Asia/Shanghai',generated_at=self.bank.clock())
        return items,as_of,clock_mode

    def query(self,w,params):
        allowed={'from','to','view','source','include_life_events','simulation_overlay','kinds','period','portfolio','accounting_basis'}
        if set(params)-allowed:raise BankError('INVALID_QUERY','日历查询参数无效。')
        source=params.get('source') or self.g.source(w)['active_source']
        if source not in {'simulation','imported'}:raise BankError('CALENDAR_SOURCE','日历来源无效。')
        as_of,_=self.clock(w,source)
        if params.get('period'):
            import calendar
            anchor=day(as_of);period=params['period']
            if period in {'today','tomorrow'}:begin=anchor+timedelta(days=period=='tomorrow');end=begin
            elif period in {'last_week','this_week','next_week'}:
                begin=anchor-timedelta(days=anchor.weekday())+timedelta(days={'last_week':-7,'this_week':0,'next_week':7}[period]);end=begin+timedelta(days=6)
            elif period in {'last_month','this_month','next_month'}:
                y,m=divmod(anchor.year*12+anchor.month-1+{'last_month':-1,'this_month':0,'next_month':1}[period],12);m+=1
                begin=anchor.replace(year=y,month=m,day=1);end=begin.replace(day=calendar.monthrange(y,m)[1])
            else:raise BankError('CALENDAR_PERIOD','相对期间无效。')
            if 'from' in params or 'to' in params:raise BankError('CALENDAR_RANGE','相对期间和绝对日期不能混用。')
        else:begin=day(params.get('from',as_of));end=day(params.get('to',as_of))
        if not 1<=(end-begin).days+1<=93:raise BankError('CALENDAR_RANGE','范围最多93天，请分段查询。')
        view=params.get('view','month')
        if view not in {'month','week','day','agenda'}:raise BankError('CALENDAR_VIEW','日历视图无效。')
        kinds=params.get('kinds',[])
        if not isinstance(kinds,list) or set(kinds)-{'transaction','investment_pnl','distribution','maturity','settlement','bill','subscription','birthday','life_event'}:raise BankError('CALENDAR_KIND','类型筛选无效。')
        for key in ('include_life_events','simulation_overlay'):
            if key in params and type(params[key]) is not bool:raise BankError('INVALID_QUERY','覆盖层开关无效。')
        portfolio=params.get('portfolio','current')
        if source=='imported' and portfolio!='current':raise BankError('CALENDAR_SOURCE_PORTFOLIO','导入账单不含历史组合估值，请选择当前流水视图或切换到账户来源。',409)
        if portfolio!='current' and params.get('accounting_basis','total_equity_v2')!='total_equity_v2':raise BankError('CALENDAR_BASIS','此版本组合仅支持 total_equity_v2。')
        if not isinstance(portfolio,str) or len(portfolio)>100:raise BankError('CALENDAR_PORTFOLIO','组合标识无效。')
        basis=params.get('accounting_basis','holdings_only_v1' if portfolio=='current' else 'total_equity_v2')
        if portfolio=='current' and basis!='holdings_only_v1':raise BankError('CALENDAR_BASIS','旧账户组合仅支持 holdings_only_v1；权益法请指定历史组合。')
        if basis not in {'total_equity_v2','holdings_only_v1'}:raise BankError('CALENDAR_BASIS','核算口径无效。')
        self.g.wealth.provision(w)
        normalized={'from':begin.isoformat(),'to':end.isoformat(),'view':view,'source':source,
                    'include_life_events':params.get('include_life_events',True),'simulation_overlay':params.get('simulation_overlay',False),'kinds':kinds,'portfolio':portfolio,'accounting_basis':basis}
        before=self.versions(w)
        items,as_of,mode=self._project(w,begin,end,source,normalized['include_life_events'],normalized['simulation_overlay'],kinds,portfolio)
        if before!=self.versions(w):raise BankError('CALENDAR_STALE','来源在读取时变化，请重试。',409)
        archive=self.bank.customer_archive(w)
        account_ids={a['id'] for a in archive['accounts']}
        periods=archive['records'].get('statement_periods',[]) if source=='simulation' else []
        def coverage_on(on,entries):
            if source=='simulation' and self.g.demo_accounts.is_demo(w):
                demo=self.g.demo_accounts.current(w)
                if demo['start_date']<=on<=demo['end_date']:return 'complete'
            proven={p['account_id'] for p in periods if p['status']=='complete' and p['period_start']<=on<=p['period_end']}
            if account_ids and proven>=account_ids:return 'complete'
            return 'partial' if any(i['kind']=='transaction' for i in entries) else 'unknown'
        def totals_on(entries,coverage):
            rows=summation([i for i in entries if source=='simulation' or i['source_type']!='simulation'])
            if coverage=='complete':
                for currency in {a['currency'] for a in archive['accounts']}:
                    row=next((r for r in rows if r['currency']==currency),None)
                    if row is None:row={'currency':currency};rows.append(row)
                    for key in TOTALS[:10]:
                        if row.get(key) is None:row[key]=0
            return rows
        vid=uuid.uuid4().hex;days=[]
        for offset in range((end-begin).days+1):
            d=(begin+timedelta(days=offset)).isoformat();entries=[i for i in items if i['date']==d]
            primary=sorted(entries,key=lambda i:(i['kind']=='transaction',i['title']))
            dc=coverage_on(d,entries)
            days.append({'date':d,'coverage':dc,
                'summary_by_currency':totals_on(entries,dc),
                'wealth_status':('missing' if any(i['kind']=='investment_pnl' and i['state']=='unknown' for i in entries) else 'available' if any(i['kind']=='investment_pnl' and i['state']=='recorded' for i in entries) else 'not_disclosed') if source=='simulation' else 'not_in_scope',
                'item_count':len(entries),'preview':primary[:2]})
        wealth_points,portfolio_meta=self.g.wealth.points(w,begin.isoformat(),end.isoformat(),portfolio) if source=='simulation' and portfolio!='current' else ([],None)
        holding_rows=list({(x['account_id'],x['fund_id']):x for x in wealth_points}.values())
        expected={(x['account_id'],x['fund_id'])for x in self.g.wealth.positions(w,portfolio_meta['id'])} if portfolio_meta else set()
        holding_returns=self.g.wealth.aggregate(holding_rows,expected)
        # 模拟覆盖层与导入流水总额不叠加；分别给出。
        financial=[i for i in items if i['source_type']!='simulation' or source=='simulation']
        coverage='complete' if all(d['coverage']=='complete' for d in days) else 'partial' if any(d['coverage']!='unknown' for d in days) else 'unknown'
        result={'schema_version':1,'view_id':vid,'workspace_binding':w,
            'generated_at':datetime.fromtimestamp(self.bank.clock(),ZoneInfo('Asia/Shanghai')).isoformat(),
            'as_of':as_of,'clock_mode':mode,'timezone':'Asia/Shanghai','source_versions':before,
            'source_scope':{'statement_source':source,'include_life_events':normalized['include_life_events'],'simulation_overlay':normalized['simulation_overlay'],'kinds':kinds,'portfolio':portfolio,'accounting_basis':normalized['accounting_basis']},
            'range':{'from':begin.isoformat(),'to':end.isoformat()},'view':view,'summary_by_currency':totals_on(financial,coverage),
            'coverage_basis':'银行已封存完整期间，逐账户核对' if coverage=='complete' else '仅已记录流水，未证明完整覆盖',
            'simulation_overlay_summary':summation([i for i in items if i['source_type']=='simulation']) if source=='imported' and normalized['simulation_overlay'] else [],
            'portfolio':portfolio_meta,'accounting_basis':normalized['accounting_basis'],'nav_asof':max((x['nav_asof'] for x in wealth_points),default=None),'holdings_pnl_minor':holding_returns.get('unrealized_pnl_minor'),'coverage':coverage,'days':days,'limitations':['仅汇总已记录流水；暂无记录不代表零支出。','理财本金口径缺少净值/数量时，每日收益不可计算。','用户预算仅展示计划，不扣款；提醒仅在应用打开时检查。']}
        with self.bank.transaction() as db:
            db.execute('INSERT INTO calendar_views VALUES(?,?,?,?,?,?,?)',(vid,w,encoded(normalized),encoded(result),encoded(before),self.bank.clock(),self.bank.clock()+30*86400))
            for i in items:
                db.execute('INSERT INTO calendar_items VALUES(?,?,?) ON CONFLICT(id,workspace_id) DO UPDATE SET reference=excluded.reference',(i['id'],w,encoded({'date':i['date'],'source':source,'simulation_overlay':normalized['simulation_overlay'],'item':i,'portfolio':portfolio})))
        return result

    def widget(self,result):
        return {'type':'financial_calendar','schema_version':1,'widget_id':result['view_id'],'view_id':result['view_id'],
            'workspace_binding':result['workspace_binding'],'view':result['view'],'range':result['range'],
            'source_scope':result['source_scope'],'generated_at':result['generated_at'],'snapshot':result}

    def _view(self,w,vid):
        with self.bank.transaction() as db:row=db.execute('SELECT * FROM calendar_views WHERE workspace_id=? AND id=?',(w,vid)).fetchone()
        if not row:raise BankError('CALENDAR_NOT_FOUND','日历视图不属于当前账户。',404)
        if self.bank.clock()>=row['expires_at']:raise BankError('CALENDAR_EXPIRED','日历引用已过期，可重新查询。',410)
        return row

    def detail(self,w,vid,on,offset=0,limit=100):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:raise BankError('CALENDAR_PAGE','单页最多100条。')
        row=self._view(w,vid);p=json.loads(row['parameters']);d=day(on)
        if not day(p['from'])<=d<=day(p['to']):raise BankError('CALENDAR_RANGE','日期不在此卡片范围。')
        before=self.versions(w)
        items,as_of,mode=self._project(w,d,d,p['source'],p['include_life_events'],p['simulation_overlay'],p['kinds'],p.get('portfolio','current'))
        if before!=self.versions(w):raise BankError('CALENDAR_STALE','来源变化，请重试。',409)
        with self.bank.transaction() as db:
            for item in items:
                db.execute('INSERT INTO calendar_items VALUES(?,?,?) ON CONFLICT(id,workspace_id) DO UPDATE SET reference=excluded.reference',
                    (item['id'],w,encoded({'date':item['date'],'source':p['source'],'simulation_overlay':p['simulation_overlay'],'item':item,'portfolio':p.get('portfolio','current')})))
        return {'date':on,'as_of':as_of,'clock_mode':mode,'items':items[offset:offset+limit],
                'total':len(items),'offset':offset,'limit':limit,'has_more':offset+limit<len(items),
                'changed_since_snapshot':before!=json.loads(row['source_versions']),'source_versions':before,
                'summary_by_currency':summation([i for i in items if p['source']=='simulation' or i['source_type']!='simulation']),
                'generated_at':self.bank.clock(),'notice':'这是最新详情，原卡片仍保留生成时快照。'}

    def item(self,w,iid):
        with self.bank.transaction() as db:row=db.execute('SELECT * FROM calendar_items WHERE workspace_id=? AND id=?',(w,iid)).fetchone()
        if not row:raise BankError('CALENDAR_NOT_FOUND','事项不存在或未在当前账户读取。',404)
        ref=json.loads(row['reference']);d=day(ref['date']);items,_,_=self._project(w,d,d,ref['source'],True,ref['simulation_overlay'],portfolio=ref.get('portfolio','current'))
        entry=next((i for i in items if i['id']==iid),None)
        if entry is None:
            return {**ref['item'],'state':'cancelled','allowed_actions':[],'notice':'原来源已变化或事项已取消，请重新查询。'}
        return entry

    def budget_scenario(self,w,account_id,start,end):
        version=self.versions(w)
        begin,stop=day(start),day(end)
        if not 1<=(stop-begin).days+1<=93:raise BankError('CALENDAR_RANGE','计划情景最多93天。')
        base=self.g.budget(w,account_id)
        archive=self.bank.customer_archive(w)
        account=next(a for a in archive['accounts'] if a['id']==account_id)
        credit={r['evidence_id']:r['amount_minor'] for r in base['commitments'] if r['account_id']==account_id}
        credit.update({h['id']:h['amount_minor'] for h in archive['records']['fund_holds'] if h['status']=='held' and h['account_id']==account_id})
        rows=[];extra=0;ignored=[]
        for event in sorted(self.events.list(w,begin,stop),key=lambda i:i['id']):
            if event['state']!='planned' or event.get('budget_minor') is None:continue
            if event['currency']!=account['currency']:ignored.append(event['id']);continue
            remaining=event['budget_minor'];offset=0
            for link in event['linked_commitment_ids']:
                consumed=min(remaining,credit.get(link,0));remaining-=consumed;offset+=consumed
                if link in credit:credit[link]-=consumed
            extra+=remaining
            rows.append({'item_id':event['id'],'title':event['title'],'currency':account['currency'],
                'budget_minor':event['budget_minor'],'already_reserved_minor':offset,'additional_reserve_minor':remaining})
        if version!=self.versions(w):raise BankError('CALENDAR_STALE','情景读取期间资料已变化，请重试。',409)
        return {'as_of':archive['as_of'],'source':'user_calendar_scenario','currency':account['currency'],
            'account_id':account_id,'range':{'from':start,'to':end},'items':rows,
            'base_investable_minor':base['investable_minor'],'additional_reserve_minor':extra,
            'scenario_investable_minor':max(0,base['investable_minor']-extra),'excluded_currency_item_ids':ignored,
            'notice':'这是用户计划情景，没有扣款或建立预留；现有承诺/生效预留按有效关联逐项扣除，单个预留不能抵扣多次。'}
