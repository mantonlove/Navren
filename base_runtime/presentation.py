"""受控展示候选：复用日历原件和 FinancialMetrics，不接管模型/UI发布。"""
from calendar import monthrange
from contextlib import closing, contextmanager
from copy import copy, deepcopy
from datetime import date, datetime, timedelta
import hashlib
import hmac
import json
import re
from pathlib import Path
import sqlite3
import uuid

from simulated_bank import BankError
from simulated_bank.service import digest
from financial_calendar import day, summation
from financial_methods import MetricContext, money_metric
from financial_metrics import fingerprint
from wealth_returns import calculate
from user_memory import privacy_revision
from simulated_bank.customer_schema import SPECS
from base_runtime.finance_sources import synthetic_cash_snapshot
from base_runtime.finance_methods import accounting_report

CONTRACT='navren.presentation/1'
CODE=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
DEPENDENCIES={name:hashlib.sha256((Path(__file__).parent.parent/name).read_bytes()).hexdigest() for name in
              ('financial_calendar.py','wealth_returns.py','wealth_portfolio.py','cashflow_timeline.py','base_runtime/history.py',
               'base_runtime/finance_sources.py','base_runtime/finance_methods.py','financial_methods/accounting.py',
               'financial_methods/contracts.py','simulated_bank/customer_schema.py')}
TABLES=('accounts','profiles','transactions','archived_transactions','statement_periods',
        'finance_data_sources','statement_datasets','imported_statement_transactions',
        'calendar_series','calendar_occurrence_overrides','wealth_valuation_points',
        'portfolio_versions','portfolio_positions','portfolio_valuations','navren_demo_accounts',
        'loans','loan_installments','cards','credit_statements','subscriptions','mandates',
        'scheduled_payments','orders','fund_holds','wealth_goals','user_memory_settings','holdings')
TABLES=tuple(dict.fromkeys((*TABLES,*(row[0] for row in SPECS))))
MEASURES={'recorded_net_minor','income_minor','expense_minor','refund_minor',
          'net_expense_minor','operating_surplus_minor','confirmed_due_minor','candidate_due_minor','user_budget_minor'}
HISTORICAL_MEASURES={'recorded_net_minor','income_minor','expense_minor','refund_minor','net_expense_minor','operating_surplus_minor'}
LIMITS={'day':7,'week':6,'month':12,'year':12,'category':4,'valuation':12}


def reject(code,message,status=422):raise BankError(code,message,status)


class _BorrowedConnection:
    def __init__(self,db):self.db=db
    def __getattr__(self,key):return getattr(self.db,key)
    def close(self):pass
    def commit(self):reject('PRESENTATION_READ_ONLY','来源读取不能提交业务变更。',409)
    def rollback(self):reject('PRESENTATION_READ_ONLY','来源读取不能改变银行事务。',409)
    def __enter__(self):return self
    def __exit__(self,*args):pass


@contextmanager
def read_gateway(g,db):
    """复用原服务方法，但把银行读取固定到同一已有快照；来源区禁止写入。"""
    scoped=copy(g);bank=copy(g.bank);borrowed=_BorrowedConnection(db)
    bank.connect=lambda:borrowed
    @contextmanager
    def transaction():yield borrowed
    bank.transaction=transaction;scoped.bank=bank
    for name in ('statements','demo_accounts','wealth','calendar','memories'):
        obj=copy(getattr(g,name));obj.bank=bank
        if hasattr(obj,'g'):obj.g=scoped
        setattr(scoped,name,obj)
    scoped.calendar.events=copy(g.calendar.events)
    scoped.calendar.events.bank=bank;scoped.calendar.events.g=scoped
    scoped.memories.store=copy(g.memories.store);scoped.memories.store.bank=bank
    bank.demo_accounts=scoped.demo_accounts
    # 首版仅消费已有已封存组合；不能在聊天查询中暗建/重新激活组合。
    scoped.wealth.provision=lambda w:None
    # 保留发布区已经设置的 authorizer；sqlite 没有可供安全恢复的 get_authorizer。
    prior_query_only=db.execute('PRAGMA query_only').fetchone()[0]
    db.execute('PRAGMA query_only=ON')
    try:yield scoped
    finally:
        if not prior_query_only:db.execute('PRAGMA query_only=OFF')


def buckets(begin,end,granularity):
    cursor=begin
    while cursor<=end:
        if granularity=='year':stop=date(cursor.year,12,31)
        elif granularity=='month':stop=date(cursor.year,cursor.month,monthrange(cursor.year,cursor.month)[1])
        elif granularity=='week':stop=cursor+timedelta(days=6-cursor.weekday())
        else:stop=cursor
        stop=min(stop,end);yield cursor,stop;cursor=stop+timedelta(days=1)


class FinancialPresentations:
    def __init__(self,g):
        self.g,self.bank=g,g.bank
        g.presentations=self

    @contextmanager
    def _publication(self,bank_db=None):
        if bank_db is None:
            with self.bank.transaction() as db:yield db
            return
        if not isinstance(bank_db,sqlite3.Connection):
            reject('PRESENTATION_DATABASE','展示连接必须来自原银行。',403)
        try:paths=[r[2] for r in bank_db.execute('PRAGMA database_list') if r[1]=='main']
        except sqlite3.Error:reject('PRESENTATION_DATABASE','展示银行连接已不可用。',403)
        if (not bank_db.in_transaction or len(paths)!=1
                or Path(paths[0]).resolve()!=Path(self.bank.database).resolve()):
            reject('PRESENTATION_DATABASE','展示必须使用原银行的已开启事务。',403)
        guard=getattr(self.bank,'request_identity_guard',None)
        if callable(guard):guard(bank_db)
        yield bank_db

    def _session_binding(self,w,db,*,required=False):
        sessions=getattr(self.g,'demo_sessions',None)
        context=getattr(sessions,'_request',None)
        request=context.get() if context is not None else None
        if request is None:
            if required:reject('PRESENTATION_IDENTITY_REQUIRED','跨轮详情需要当前服务身份。',403)
            return None
        if (not isinstance(request,tuple) or len(request)!=2 or request[1]!=w
                or not isinstance(request[0],str)):
            reject('PRESENTATION_IDENTITY_REQUIRED','展示身份不属于当前工作区。',403)
        guard=getattr(self.bank,'request_identity_guard',None)
        if not callable(guard):reject('PRESENTATION_IDENTITY_REQUIRED','展示缺少服务身份守卫。',403)
        guard(db)
        token_hash=digest(request[0])
        row=db.execute('SELECT * FROM demo_sessions WHERE token_hash=?',(token_hash,)).fetchone()
        if row:
            if row['workspace_id']!=w or row['revoked'] or row['expires_at']<=self.bank.clock():
                reject('SESSION_CHANGED','展示会话已切换或失效。',409)
            return {'workspace_binding':w,'token_hash':token_hash,'generation':row['generation'],'mode':'demo'}
        row=db.execute('SELECT token_hash FROM workspaces WHERE id=?',(w,)).fetchone()
        if not row or not hmac.compare_digest(row['token_hash'],token_hash):
            reject('SESSION_CHANGED','展示会话已失效。',409)
        return {'workspace_binding':w,'token_hash':token_hash,'generation':0,'mode':'legacy'}

    def _trusted_context(self,w,versions,db,*,required=False):
        identity=self._session_binding(w,db,required=required)
        if identity is None:return None
        return {'identity':identity,'personal_revision':int(versions['personal']),
                'privacy_revision':privacy_revision(db,w),'private_context':versions['private_context']}

    @contextmanager
    def _reissue_atomic(self,bank_db=None):
        with self._publication(bank_db) as db:
            if db.execute('PRAGMA query_only').fetchone()[0]:
                reject('PRESENTATION_DATABASE','只读银行连接不能发行新展示对象。',403)
            savepoint='presentation_reissue_'+uuid.uuid4().hex
            db.execute('SAVEPOINT '+savepoint)
            try:
                yield db
                db.execute('RELEASE '+savepoint)
            except BaseException:
                db.execute('ROLLBACK TO '+savepoint)
                db.execute('RELEASE '+savepoint)
                raise

    def source_versions(self,w,db):
        result=self.g.financial_metrics.versions(w,db)
        result['presentation_workspace/'+w]=fingerprint(dict(self.bank._workspace(db,w)))
        result.update(presentation_contract=CONTRACT,presentation_code=CODE)
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=CODE:
            reject('PRESENTATION_RESTART_REQUIRED','展示代码已变化，请重新加载服务。',503)
        for name,digest in DEPENDENCIES.items():
            if hashlib.sha256((Path(__file__).parent.parent/name).read_bytes()).hexdigest()!=digest:
                reject('PRESENTATION_RESTART_REQUIRED','展示依赖代码已变化，请重新加载服务。',503)
            result['presentation_code/'+name]=digest
        for table in TABLES:
            columns={r['name'] for r in db.execute('PRAGMA table_info('+table+')')}
            values=[dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE workspace_id=? ORDER BY rowid',(w,))] if columns else None
            result['presentation_table/'+table+'/'+w]=fingerprint(values)
        demo=db.execute("SELECT * FROM navren_demo_accounts WHERE workspace_id=? AND status='ready'",(w,)).fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE name='navren_demo_accounts'").fetchone() else None
        if demo:
            record=dict(demo)
            # 原服务会核验文件hash/只读封存；不只相信数据库声明的版本。
            with closing(self.g.demo_accounts._history_connection(record)) as history:
                history.execute('SELECT 1').fetchone()
            result['presentation_history/'+w]=record['history_sha256']
        return result

    @staticmethod
    def _identity(request_id,message_id):
        if (not isinstance(request_id,str) or not 1<=len(request_id)<=160
                or message_id!=request_id):
            reject('PRESENTATION_BINDING','展示必须属于当前服务响应和消息。',403)

    def _params(self,w,p,g):
        allowed={'intent','from','to','period','source','currency','portfolio','accounting_basis','granularity',
                 'measure','offset','limit','account_id','kinds','include_life_events','layout'}
        if not isinstance(p,dict) or set(p)-allowed:reject('PRESENTATION_QUERY','展示参数包含未支持字段。')
        p=deepcopy(p);intent=p.get('intent','calendar')
        if intent not in {'calendar','statement','cashflow','wealth'}:reject('PRESENTATION_INTENT','展示意图无效。')
        source=p.get('source') or g.source(w)['active_source']
        if source not in {'simulation','imported'}:reject('PRESENTATION_SOURCE','来源无效。')
        if source=='imported' and intent in {'cashflow','wealth'}:reject('PRESENTATION_SOURCE','该来源没有组合份额或未来确认事项。')
        as_of,clock=g.calendar.clock(w,source)
        if intent=='wealth':
            portfolio=p.get('portfolio','historical')
            if not isinstance(portfolio,str) or len(portfolio)>100:reject('PRESENTATION_PORTFOLIO','组合标识无效。')
            _,meta=g.wealth.points(w,'1900-01-01','2100-12-31',portfolio)
            if meta and meta.get('to'):as_of,clock=meta['to'],'historical_nav'
            else:clock='simulation_without_valuation_clock'
        if p.get('period'):
            if 'from' in p or 'to' in p:reject('PRESENTATION_RANGE','相对期间与绝对日期不能同时使用。')
            anchor=day(as_of);period=p['period']
            if period in {'today','tomorrow'}:begin=anchor+timedelta(days=period=='tomorrow');end=begin
            elif period in {'last_week','this_week','next_week'}:
                begin=anchor-timedelta(days=anchor.weekday())+timedelta(days={'last_week':-7,'this_week':0,'next_week':7}[period]);end=begin+timedelta(days=6)
            elif period in {'last_month','this_month','next_month'}:
                y,m=divmod(anchor.year*12+anchor.month-1+{'last_month':-1,'this_month':0,'next_month':1}[period],12);m+=1
                begin=date(y,m,1);end=date(y,m,monthrange(y,m)[1])
            elif period in {'this_year','last_year'}:
                y=anchor.year-(period=='last_year');begin=date(y,1,1);end=date(y,12,31)
            else:reject('PRESENTATION_PERIOD','相对期间无效。')
        else:begin=day(p.get('from',as_of));end=day(p.get('to',as_of))
        span=(end-begin).days+1
        if not 1<=span<=3660:reject('PRESENTATION_RANGE','展示范围最多十年。')
        if intent=='calendar' and span>93:reject('PRESENTATION_RANGE','日程展示最多93天；历史流量请使用账单意图。')
        if intent=='cashflow' and (begin<day(as_of) or end>day(as_of)+timedelta(days=365)):
            reject('PRESENTATION_RANGE','未来推演仅限当前业务日至未来365天。')
        gran=p.get('granularity') or ('day' if span<=7 else 'week' if span<=31 else 'month' if span<=366 else 'year')
        if intent=='wealth':gran='valuation'
        if gran not in LIMITS:reject('PRESENTATION_GRANULARITY','时间粒度无效。')
        if gran=='valuation' and intent!='wealth':reject('PRESENTATION_GRANULARITY','净值点只属于财富视图。')
        if gran=='category' and intent!='statement':reject('PRESENTATION_GRANULARITY','分类构成只属于流水视图。')
        currency=p.get('currency','CNY')
        if currency!='CNY':reject('PRESENTATION_CURRENCY','当前来源仅支持人民币，不做跨币种合计。')
        portfolio=p.get('portfolio','historical' if intent=='wealth' else 'current')
        if not isinstance(portfolio,str) or len(portfolio)>100:reject('PRESENTATION_PORTFOLIO','组合标识无效。')
        if intent!='wealth' and portfolio!='current':reject('PRESENTATION_PORTFOLIO','历史组合不能与账户现金流水合并。')
        basis=p.get('accounting_basis','total_equity_v2' if intent=='wealth' else 'cash_records_v1')
        if basis!=('total_equity_v2' if intent=='wealth' else 'cash_records_v1'):reject('PRESENTATION_BASIS','该来源不支持所选口径。')
        measure=p.get('measure','closing_equity_minor' if intent=='wealth' else 'confirmed_balance_minor' if intent=='cashflow' else 'recorded_net_minor')
        if measure not in (MEASURES if intent in {'calendar','statement'} else {'closing_equity_minor','interval_pnl_minor'} if intent=='wealth' else {'confirmed_balance_minor','candidate_balance_minor'}):
            reject('PRESENTATION_MEASURE','指标不属于该意图。')
        if intent=='statement' and measure not in HISTORICAL_MEASURES:
            reject('PRESENTATION_MEASURE','历史流水不能包含未来义务或手工预算。')
        if gran=='category' and measure not in {'expense_minor','net_expense_minor'}:
            reject('PRESENTATION_MEASURE','分类视图仅支持独立的毛支出或已核验退款净支出。')
        aid=p.get('account_id')
        if intent=='wealth' and aid is not None:
            reject('PRESENTATION_SUBJECT','历史研究组合不属于银行现金账户；首版仅提供组合整体。',403)
        if aid is not None and aid not in {a['id'] for a in g.bank.customer_archive(w)['accounts']}:
            reject('PRESENTATION_SUBJECT','账户不属于当前工作区。',403)
        kinds=p.get('kinds',[])
        if (not isinstance(kinds,list) or any(not isinstance(k,str) for k in kinds)
                or set(kinds)-{'transaction','investment_pnl','distribution','maturity','settlement','bill','subscription','birthday','life_event'}):
            reject('PRESENTATION_KIND','事项筛选无效。')
        if 'include_life_events' in p and type(p['include_life_events']) is not bool:reject('PRESENTATION_QUERY','生活事项开关无效。')
        offset=p.get('offset',0);limit=p.get('limit',LIMITS[gran])
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=LIMITS[gran]:
            reject('PRESENTATION_PAGE','超出受控展示分页数量。')
        return {'intent':intent,'from':begin.isoformat(),'to':end.isoformat(),'source':source,'currency':currency,
                'portfolio':portfolio,'accounting_basis':basis,'granularity':gran,'measure':measure,
                'offset':offset,'limit':limit,'account_id':aid,'kinds':kinds,
                'include_life_events':p.get('include_life_events',intent=='calendar'),
                'layout':p.get('layout'),'as_of':as_of,'clock_basis':clock}

    def _cash_evidence(self,g,w,start,end,p):
        """消费既有封存适配与pure，独立日记账目录证明现金行；不从min/max推覆盖。"""
        if p['source']!='simulation' or not g.demo_accounts.is_demo(w):return None
        snapshot=synthetic_cash_snapshot(g.demo_accounts,w,{'source':'navren_synthetic','scope':w,
            'as_of':None,'period_start':start,'period_end':end})
        if not snapshot or not snapshot['coverage_complete']:return {'coverage':False,'snapshot':snapshot}
        record=g.demo_accounts._record(w);mapping=json.loads(record['account_mapping'])
        categories={};movement_items={};raw=[]
        with closing(g.demo_accounts._history_connection(record)) as history:
            raw=[dict(r) for r in history.execute('SELECT * FROM cash_transactions WHERE user_id=? AND substr(occurred_at,1,10) BETWEEN ? AND ? ORDER BY transaction_id',(record['user_id'],start,end))]
            # 类别直接来自同一封存原行；原退款类别取原expense，不相信退款自填分类。
            for r in history.execute('''SELECT l.line_id,c.category,c.kind,c.signed_minor,c.transaction_id FROM journal_lines l
                JOIN cash_transactions c ON c.user_id=l.user_id AND c.event_id=l.event_id AND c.account_id=l.account_id
                AND c.signed_minor=l.debit_minor-l.credit_minor WHERE l.user_id=?''',(record['user_id'],)):
                categories[r['line_id']]=r['category'] or '未分类'
                movement_items[r['line_id']]='simulation:tx:'+r['transaction_id']
        context=MetricContext(w,'CNY','navren_synthetic',period_start=start,period_end=end,
            input_refs=tuple(snapshot['input_refs']),source_versions=tuple(snapshot['source_versions'].items()),coverage_complete=True)
        report=accounting_report(context,snapshot['records'],snapshot['catalog'],snapshot['provenance'])
        expected={'simulation:tx:'+r['transaction_id'] for r in raw if not p['account_id'] or mapping.get(r['account_id'])==p['account_id']}
        return {'coverage':report['calculation_status']=='calculated','snapshot':snapshot,'report':report,
                'expected_ids':expected,'categories':categories,'account_mapping':mapping,'movement_items':movement_items}

    def _classified(self,evidence,p,selected_items=None):
        """退款proof与pure报告已先核验；仅分桶/分类，不重建金额来源。"""
        if not evidence or not evidence.get('coverage'):return None
        rows=[];originals={x['movement_id']:x for x in evidence['snapshot']['records']['refund_originals']}
        for m in evidence['snapshot']['records']['movements']:
            if p['account_id'] and evidence['account_mapping'].get(m['account_id'])!=p['account_id']:continue
            key=m['movement_id'].removesuffix('/interest').removesuffix('/principal')
            if selected_items is not None and evidence['movement_items'].get(key) not in selected_items:continue
            category=evidence['categories'].get(m['related_id'] if m['kind']=='refund' else key)
            if m['kind'] in {'expense','loan_interest','refund'} and category is None:return None
            rows.append({**m,'category':category or '未分类','item_ref':evidence['movement_items'].get(key),
                         'original_ref':m['related_id'] if m['kind']=='refund' else None})
        if any(not x['item_ref'] for x in rows):return None
        # accounting_report已核验原费用账户/主体/币种/时间与累计未超额；原分类另绑定同sealed source。
        proofs=[{'refund_ref':x['movement_id'],'original_expense_ref':x['original_ref'],'category':x['category'],
                 'subject_id':x['subject_id'],'account_id':x['account_id'],'currency':x['currency'],
                 'settled_on':x['on'],'original_on':originals[x['original_ref']]['on'],
                 'cumulative_ref':evidence['snapshot']['provenance']['refund_cumulative'][0],
                 'source_version':x['source_version'],'cumulative_not_excessive':True}
                for x in rows if x['kind']=='refund']
        return rows,proofs

    def _observed(self,pres,point,metrics,kinds,value,name,part,*,basis=None,refs=(),missing=()):
        """给定记录小计和请求整期是两个声明，实际记录日期不证明连续覆盖。"""
        if not part:return
        dates=[x['date'] for x in part]
        point['observed_scope']={'type':'provided_record_set','from':min(dates),'to':max(dates),
            'record_count':len(part),'complete_period_proven':False}
        point['observed_value']=self._metric(pres,metrics,kinds,value,'observed_'+name,min(dates),max(dates),
            refs or tuple('calendar_item/'+x['id'] for x in part),coverage='partial',
            basis='provided_record_set/'+(basis or name),missing=missing or ('requested_period_not_fully_covered',),
            data_scope=pres['data_scope']+'/provided_record_set')

    def _coverage(self,g,w,start,end,items,p,evidence=None):
        if end>p['as_of']:return 'partial' if items else 'unknown'
        if p['measure'] in HISTORICAL_MEASURES and p['kinds'] and 'transaction' not in p['kinds']:return 'unknown'
        if p['source']=='imported':return 'partial' if items else 'unknown'
        if g.demo_accounts.is_demo(w):
            evidence=evidence or self._cash_evidence(g,w,start,end,p)
            return 'complete' if evidence and evidence['coverage'] and evidence['expected_ids']=={i['id'] for i in items} else 'partial' if items else 'unknown'
        archive=g.bank.customer_archive(w);accounts=[a for a in archive['accounts'] if not p['account_id'] or a['id']==p['account_id']]
        rows=archive['records'].get('statement_periods',[])
        complete=bool(accounts)
        for a in accounts:
            cursor=day(start)
            for r in sorted((r for r in rows if r['account_id']==a['id'] and r['status']=='complete'),key=lambda r:r['period_start']):
                left,right=day(r['period_start']),day(r['period_end'])
                if left>cursor:break
                if right>=cursor:cursor=right+timedelta(days=1)
            if cursor<=day(end):complete=False
        return 'complete' if complete else 'partial' if items else 'unknown'

    def _metric(self,pres,metrics,kinds,value,name,start,end,refs,*,kind='recorded_fact',coverage='complete',basis=None,missing=(),on=None,data_scope=None):
        if value is not None and type(value) is not int:reject('PRESENTATION_AMOUNT','原件金额必须为整数分。',503)
        refmap={**pres['source_versions']}
        for ref in refs:refmap[ref]=fingerprint({'view_id':pres['view_id'],'ref':ref})
        ctx=MetricContext(pres['scope']['account_id'] or pres['workspace_binding'],pres['scope']['currency'],
                          data_scope or pres['data_scope'],as_of=on,period_start=None if on else start,period_end=None if on else end,
                          input_refs=tuple(refs),source_versions=tuple(refmap.items()),coverage_complete=coverage=='complete')
        metric=money_metric(ctx,name,basis or name,'presentation.'+name+'/1',value,
                            status='unavailable' if value is None else 'partial' if coverage!='complete' else 'calculated',
                            gaps=tuple(missing) or (('source_value_not_available',) if value is None else () if coverage=='complete' else ('requested_period_not_fully_covered',)),
                            excluded=('not_real_bank_data','no_execution_authority',),
                            assumptions=({'type':'recorded_candidate_scenario'},) if kind=='candidate_scenario' else ()).to_dict()
        metrics.append(metric);kinds[metric['metric_id']]=kind
        return {'metric_id':metric['metric_id'],'value_minor':value,'currency':ctx.currency,'calculation_status':metric['calculation_status']}

    def _build(self,g,w,p,versions,request_id,message_id,parent=None,selected_items=None,details=False):
        vid=uuid.uuid4().hex;begin,end=day(p['from']),day(p['to']);metrics=[];kinds={}
        scope={k:p[k] for k in ('source','currency','portfolio','accounting_basis','account_id','kinds','include_life_events')}
        pres={'schema_version':1,'contract':CONTRACT,'view_id':vid,'parent_view_id':parent,
              'request_id':request_id,'message_id':message_id,'workspace_binding':w,
              'authorization_revision':{'personal':int(versions['personal']),'privacy':None},
              'source_versions':versions,'scope':scope,'range':{'from':p['from'],'to':p['to']},
              'granularity':p['granularity'],'timezone':'Asia/Shanghai','clock_basis':p['clock_basis'],'as_of':p['as_of'],
              'series_kind':'interval_pnl' if p['intent']=='wealth' and p['measure']=='interval_pnl_minor' else
                            'stock' if p['intent']=='wealth' else 'future_projection' if p['intent']=='cashflow' else 'recorded_flow',
              'data_scope':'historical_research_portfolio' if p['intent']=='wealth' else
                           'imported_statement_records' if p['source']=='imported' else
                           'navren_synthetic' if g.demo_accounts.is_demo(w) else 'simulation_bank_ledger',
              'measure':p['measure'],'title':p['from']+' — '+p['to'],'points':[],'missing':[],
              'coverage':'unknown','readonly_actions':['select_point','filter_range','ask'],
              'important_states':['只读视图，不会执行资金操作。'],'authority_reports':[]}
        pres['measure_basis']={'expense_minor':'cash_expense_rows_gross_legacy',
            'net_expense_minor':'classified_cash_expense_including_loan_interest_minus_verified_settled_refunds',
            'operating_surplus_minor':'classified_cash_income_minus_net_expenses_excludes_owner_principal_realized_returns'}.get(p['measure'],p['measure'])
        items=[]
        if p['intent'] in {'calendar','statement'}:
            items,_,_=g.calendar._project(w,begin,end,p['source'],p['include_life_events'],False,p['kinds'],'current')
            items=[i for i in items if (not p['account_id'] or i.get('account_id')==p['account_id']) and (not i.get('currency') or i['currency']==p['currency'])]
            items=[i for i in items if i['kind']!='transaction' or i['date']<=p['as_of']]
            if p['intent']=='statement':
                items=[i for i in items if i['kind']=='transaction']
                # 复用权威年度报告原入口，明示现金流水口径与原分录报告的区别。
                for year in range(begin.year,end.year+1):
                    try:r=(g.bank if p['source']=='simulation' else g.statements).annual_analysis(w,year)
                    except BankError as e:
                        if e.code!='INVALID_YEAR':raise
                        pres['missing'].append('annual_statement_year_not_supported:'+str(year));continue
                    pres['authority_reports'].append({'year':year,'sha256':fingerprint(r),'basis':'source_annual_statement',
                        'projection_basis':'Asia/Shanghai_cash_rows_not_journal_gross_consumption'})
            if selected_items is not None:items=[i for i in items if i['id'] in selected_items]
            full=buckets(begin,end,p['granularity']) if p['granularity']!='category' else []
            if details:
                total=len(items)
                pres['allowed_layouts']=['day_cards'];pres['granularity']='item'
                for i in sorted(items,key=lambda x:(x['date'],x['id']))[p['offset']:p['offset']+8]:
                    point={'point_ref':vid+':item:'+i['id'],'item_ref':i['id'],'title':i['title'],'date':i['date'],
                           'start':i['date'],'end':i['date'],'state':i['state'],'kind':i['kind'],
                           'source_ref':i['source_ref'],'coverage':'recorded_items_only','item_count':1,'preview':[]}
                    kind='candidate_scenario' if i['state']=='candidate' else 'planning_scenario' if i['source_type']=='user_calendar' else 'recorded_projection' if i['state']=='planned' else 'recorded_fact'
                    for source_field,out in (('amount_minor','value'),('cash_delta_minor','cash_delta'),('budget_minor','budget')):
                        if source_field in i and i.get('currency'):
                            point[out]=self._metric(pres,metrics,kinds,i[source_field],'item_'+source_field,i['date'],i['date'],
                                ('calendar_item/'+i['id']+'/'+source_field,),kind=kind,coverage='partial',
                                basis='recorded_item_'+source_field+'_not_current_balance',missing=('item_source_not_complete_period',))
                    pres['points'].append(point)
                pres['coverage']='recorded_items_only'
            elif p['granularity']=='category':
                evidence=self._cash_evidence(g,w,p['from'],p['to'],p)
                coverage=self._coverage(g,w,p['from'],p['to'],items,p,evidence)
                classified=self._classified(evidence,p,selected_items) if p['measure']=='net_expense_minor' else None
                categories={}
                if p['measure']=='net_expense_minor':
                    if classified:
                        rows,proofs=classified;pres['refund_attributions']=proofs
                        for row in rows:
                            if row['kind'] in {'expense','loan_interest','refund'}:
                                categories.setdefault(row['category'],[]).append(row)
                    else:pres['missing'].append('verified_refund_or_classification_source_unavailable')
                    amount=lambda row:-row['delta_minor']
                    item_ref=lambda row:row['item_ref']
                    source_ref=lambda row:'cash_movement/'+row['movement_id']
                else:
                    for i in items:
                        if i.get('transaction_kind')=='expense':categories.setdefault(i.get('category') or '未分类',[]).append(i)
                    amount=lambda row:row['amount_minor']
                    item_ref=lambda row:row['id']
                    source_ref=lambda row:'calendar_item/'+row['id']
                groups=sorted(categories.items(),key=lambda pair:(-sum(amount(i) for i in pair[1]),pair[0]))
                top=groups[:3]
                if len(groups)>3:top.append(('其余',[i for _,part in groups[3:] for i in part]))
                if not top:pres['missing'].append('no_category_expense_records')
                for label,part in top:
                    point={'point_ref':vid+':category:'+fingerprint(label)[:16],'label':label,'start':p['from'],'end':p['to'],
                           'coverage':coverage,'category_values':[c for c,rows in groups if any(i in part for i in rows)],
                           'selected_item_refs':list(dict.fromkeys(item_ref(i) for i in part)),
                           'item_count':len(part),'state':'recorded','value_scope':'requested_complete_period'}
                    val=sum(amount(i) for i in part);name='category_'+p['measure'];refs=tuple(source_ref(i) for i in part)
                    point['value']=self._metric(pres,metrics,kinds,val if coverage=='complete' else None,name,p['from'],p['to'],
                        refs,coverage=coverage,basis=pres['measure_basis'])
                    if coverage!='complete':
                        observed=[i for i in items if i['id'] in point['selected_item_refs']]
                        self._observed(pres,point,metrics,kinds,val,name,observed,basis=pres['measure_basis'],refs=refs)
                    pres['points'].append(point)
                refund=any(i.get('transaction_kind')=='refund' for i in items)
                denominator=sum(amount(i) for _,part in groups for i in part)
                pres['allowed_layouts']=['category_bar']+(['donut'] if coverage=='complete' and top and denominator>0 and len(groups)<=5 and not refund and all(sum(amount(i) for i in part)>=0 for _,part in groups) else [])
                pres['coverage']=coverage
                pres['denominator_basis']=pres['measure_basis'] if coverage=='complete' and top else None
                pres['ratios_available']=False
                pres['missing']+=['donut_denominator_not_qualified'] if 'donut' not in pres['allowed_layouts'] else []
                total=len(top)
            else:
                periods=list(full);total=len(periods)
                layout=p.get('layout')
                if layout=='month_grid':
                    if p['intent']!='calendar' or begin.year!=end.year or begin.month!=end.month:reject('PRESENTATION_LAYOUT','月历只显示同一月份的日程。')
                    periods=list(buckets(begin,end,'day'));chosen=periods
                    pres['granularity']='day';pres['allowed_layouts']=['month_grid']
                else:
                    chosen=periods[p['offset']:p['offset']+p['limit']]
                    pres['allowed_layouts']=['day_cards'] if p['granularity']=='day' else ['grouped_bar']
                for start,stop in chosen:
                    part=[i for i in items if start.isoformat()<=i['date']<=stop.isoformat()]
                    cash=[i for i in part if i['kind']=='transaction']
                    evidence=self._cash_evidence(g,w,start.isoformat(),stop.isoformat(),p) if layout!='month_grid' else None
                    coverage=self._coverage(g,w,start.isoformat(),stop.isoformat(),cash,p,evidence)
                    point={'point_ref':vid+':range:'+start.isoformat(),'date':start.isoformat(),'start':start.isoformat(),'end':stop.isoformat(),
                           'coverage':coverage,'item_count':len(part),'state':'future' if start>day(p['as_of']) else 'recorded',
                           'preview':[{'item_ref':i['id'],'title':i['title'],'kind':i['kind'],'state':i['state']} for i in sorted(part,key=lambda i:i['id'])[:2]]}
                    if layout!='month_grid':
                        totals=next((t for t in summation(part) if t['currency']==p['currency']),{})
                        val=totals.get(p['measure']);ref=('calendar_range/'+w+'/'+start.isoformat()+'/'+stop.isoformat(),)
                        gaps=[]
                        if p['measure'] in {'net_expense_minor','operating_surplus_minor'}:
                            classified=self._classified(evidence,p,selected_items)
                            if not classified and cash:
                                observed_evidence=self._cash_evidence(g,w,min(i['date'] for i in cash),max(i['date'] for i in cash),p)
                                classified=self._classified(observed_evidence,p,selected_items)
                            if classified:
                                rows,proofs=classified;point['refund_attributions']=proofs
                                gross=-sum(i['delta_minor'] for i in rows if i['kind'] in {'expense','loan_interest'})
                                refund=sum(i['delta_minor'] for i in rows if i['kind']=='refund')
                                net=gross-refund
                                val=net if p['measure']=='net_expense_minor' else sum(i['delta_minor'] for i in rows if i['kind']=='income')-net
                                ref=tuple('cash_movement/'+i['movement_id'] for i in rows) or ('sealed_empty_cash_catalog/'+w+'/'+start.isoformat(),)
                            else:val=None;gaps=['verified_refund_or_classification_source_unavailable']
                        elif not cash and coverage=='complete' and p['measure'] in HISTORICAL_MEASURES:val=0
                        kind='candidate_scenario' if p['measure']=='candidate_due_minor' else 'planning_scenario' if p['measure']=='user_budget_minor' else 'recorded_projection' if p['measure']=='confirmed_due_minor' else 'recorded_fact'
                        historical=p['measure'] in HISTORICAL_MEASURES
                        point['value_scope']='requested_complete_period' if historical else 'recorded_items_only'
                        point['value']=self._metric(pres,metrics,kinds,val if not historical or coverage=='complete' else None,p['measure'],
                            start.isoformat(),stop.isoformat(),ref,kind=kind,coverage=coverage,basis=pres['measure_basis'],missing=gaps)
                        if historical and coverage!='complete':
                            self._observed(pres,point,metrics,kinds,val,p['measure'],cash,basis=pres['measure_basis'],refs=ref,missing=gaps)
                    pres['points'].append(point)
                pres['coverage']='complete' if pres['points'] and all(x['coverage']=='complete' for x in pres['points']) else 'partial' if any(x['coverage']!='unknown' for x in pres['points']) else 'unknown'
        elif p['intent']=='cashflow':
            timeline=g.cashflow_timeline(w,max(1,(end-day(p['as_of'])).days))
            accounts=[a for a in timeline['accounts'] if not p['account_id'] or a['account_id']==p['account_id']]
            state={a['account_id']:a['opening_balance_minor'] for a in accounts}
            events={slot['date']:slot['events'] for slot in timeline['timeline']}
            kind='candidate_scenario' if p['measure']=='candidate_balance_minor' else 'recorded_projection'
            points=[]
            for offset in range((end-day(p['as_of'])).days+1):
                on=(day(p['as_of'])+timedelta(days=offset)).isoformat();part=events.get(on,[])
                for e in part:
                    if e['account_id'] in state and (e['certainty']=='confirmed' or kind=='candidate_scenario'):state[e['account_id']]+=e['cash_delta_minor']
                if on<p['from']:continue
                point={'point_ref':vid+':range:'+on,'date':on,'start':on,'end':on,'coverage':'recorded_items_only',
                       'state':'candidate' if kind=='candidate_scenario' else 'confirmed_projection','item_count':len(part),'preview':[]}
                points.append((point,sum(state.values())))
            total=len(points)
            # 长跨度仍是日终值；按真实日期受控分页，禁止月柱冒充期内收益。
            points=points[p['offset']:p['offset']+min(12,p['limit'])]
            for point,value in points:
                point['value']=self._metric(pres,metrics,kinds,value,p['measure'],point['date'],point['date'],
                    tuple('accounts/'+a['account_id'] for a in accounts),kind=kind,coverage='partial',basis='future_end_of_day_cash_not_history',
                    missing=('unrecorded_living_expenses','adequacy_not_established'),on=point['date'])
                pres['points'].append(point)
            pres['coverage']='recorded_items_only';pres['allowed_layouts']=['step_line']
            pres['important_states']+=['仅已记录事项的未来日终推演；候选分支不等于已确认。']
        else:
            points,meta=g.wealth.points(w,p['from'],p['to'],p['portfolio'])
            if meta and meta.get('currency')!='CNY':reject('PRESENTATION_CURRENCY','组合币种与所选展示不一致。')
            if any(x.get('currency')!=p['currency'] for x in points):reject('PRESENTATION_CURRENCY','估值点包含不同币种，不能合计。')
            pres['portfolio_metadata']=meta
            expected={(x['account_id'],x['fund_id']) for x in g.wealth.positions(w,meta['id'])} if meta else set()
            byday={}
            for point in points:byday.setdefault(point['valuation_on'],[]).append(point)
            for on in meta.get('valuation_dates',[]) if meta else []:
                if p['from']<=on<=p['to']:byday.setdefault(on,[])
            total=len(byday)
            for on in sorted(byday)[p['offset']:p['offset']+p['limit']]:
                part=byday[on];present={(x['account_id'],x['fund_id']) for x in part}
                returns=[calculate(x) for x in part];field='closing_equity_minor' if p['measure']=='closing_equity_minor' else 'daily_pnl_minor'
                missing=sorted({s for r in returns for s in r.get('missing',[])})
                if p['measure']=='closing_equity_minor' and all(type(r.get('closing_equity_minor')) is int for r in returns):
                    missing=[s for s in missing if s!='nav_complete']
                if not expected or present!=expected:missing.append('portfolio_product_account_coverage_missing')
                openings={x.get('opening_on') for x in part}
                if p['measure']=='interval_pnl_minor' and any(not isinstance(x.get('opening_on'),str) or
                        type(x.get('interval_days')) is not int or x['interval_days']<=0 or
                        (day(x['valuation_on'])-day(x['opening_on'])).days!=x['interval_days'] for x in part):
                    missing.append('nav_interval_dates_not_verified')
                if p['measure']=='interval_pnl_minor' and (len(openings)!=1 or None in openings or any(x<p['from'] for x in openings if x)):
                    missing.append('nav_interval_crosses_requested_boundary_or_misaligned')
                vals=[r.get(field) for r in returns]
                value=sum(vals) if part and not missing and all(type(x) is int for x in vals) else None
                point={'point_ref':vid+':range:'+on,'date':on,'start':on,'end':on,'coverage':'complete' if not missing else 'unknown',
                       'state':'historical_research','opening_on':next(iter(openings)) if len(openings)==1 else None,
                       'interval_days':sorted({x.get('interval_days') for x in part},key=lambda x:-1 if x is None else x),
                       'item_count':len(part),'preview':[],'missing':missing}
                point['value']=self._metric(pres,metrics,kinds,value,p['measure'],min((x for x in openings if x),default=on),on,
                    ('portfolio/'+(meta['id'] if meta else p['portfolio'])+'/'+on,),coverage=point['coverage'],missing=missing,
                    on=on if p['measure']=='closing_equity_minor' else None)
                pres['points'].append(point)
            pres['coverage']='complete' if pres['points'] and all(x['coverage']=='complete' for x in pres['points']) else 'unknown'
            pres['allowed_layouts']=['time_line'] if p['measure']=='closing_equity_minor' else ['grouped_bar']
            if not meta:pres['missing'].append('portfolio_not_provisioned_or_not_owned')
            pres['important_states']+=['独立历史研究组合；不是账户现金或现售事实。','损益按原净值区间，不平均摊日。']
        pres['layout']=p.get('layout') or pres['allowed_layouts'][0]
        if pres['layout'] not in pres['allowed_layouts']:reject('PRESENTATION_LAYOUT','数据条件不满足所选图型。')
        pres['page']={'offset':p['offset'],'limit':8 if details else p['limit'],'total':total,'has_more':p['offset']+len(pres['points'])<total}
        pres['followups']=[{'schema_version':1,'id':vid+':detail','label':'查看明细','text':'查看这份视图相同来源和期间的明细',
            'type':'open_view','intent':'open_view','context_ref':{'view_id':vid},'availability':'available'},
            {'schema_version':1,'id':vid+':ask','label':'解释口径','text':'解释这份视图的数据范围、计算口径和缺失资料',
            'type':'send_message','intent':'ask','context_ref':{'view_id':vid},'availability':'available'}]
        if len(metrics)>24:reject('PRESENTATION_AMOUNT_LIMIT','展示金额声明超过24条，请缩小窗口。')
        return pres,metrics,kinds,items

    def query(self,w,parameters,*,request_id,message_id,parent=None,selected_items=None,details=False,parent_projection_hash=None,detail_page_meta=None,bank_db=None):
        return self._query(w,parameters,request_id=request_id,message_id=message_id,parent=parent,
            selected_items=selected_items,details=details,parent_projection_hash=parent_projection_hash,
            detail_page_meta=detail_page_meta,bank_db=bank_db)

    def _query(self,w,parameters,*,request_id,message_id,parent=None,selected_items=None,details=False,parent_projection_hash=None,detail_page_meta=None,bank_db=None,_lineage=None):
        self._identity(request_id,message_id)
        with self._publication(bank_db) as db:
            if db.execute('PRAGMA query_only').fetchone()[0]:
                reject('PRESENTATION_DATABASE','只读银行连接不能发行新展示对象。',403)
            versions=self.source_versions(w,db)
            context=self._trusted_context(w,versions,db)
            with read_gateway(self.g,db) as g:
                p=self._params(w,parameters,g)
                pres,metrics,kinds,items=self._build(g,w,p,versions,request_id,message_id,parent,selected_items,details)
            if versions!=self.source_versions(w,db):reject('PRESENTATION_STALE','来源在读取中变化。',409)
            pres['authorization_revision']['privacy']=privacy_revision(db,w)
            pres['parent_projection_hash']=parent_projection_hash
            pres['review_status']='owner_wiring_required';pres['publication_status']='owner_wiring_required'
            if selected_items is not None:
                pres['scope']['selection_ref']=fingerprint(sorted(selected_items))
            if detail_page_meta is not None:pres['detail_page']=deepcopy(detail_page_meta)
            if _lineage is not None:pres['lineage']=deepcopy(_lineage)
            projection={k:deepcopy(v) for k,v in pres.items()}
            pres['projection_hash']=fingerprint(projection)
            stored={'presentation':pres,'metrics':metrics,'metric_kinds':kinds,'items':items,'parameters':p,'selected_item_ids':selected_items,
                    'trusted_context':context}
            stored['service_signature']=self.g.financial_metrics._tool_signature({'contract':CONTRACT,'stored':stored})
            db.execute('INSERT INTO calendar_views VALUES(?,?,?,?,?,?,?)',(pres['view_id'],w,json.dumps(p),json.dumps(stored),
                       json.dumps(versions),self.bank.clock(),self.bank.clock()+86400))
            result=self._output(stored)
            result['metric_batch']=self.g.financial_metrics.metrics_for_tool(w,'presentation_view',
                {'view_id':pres['view_id'],'request_id':request_id,'message_id':message_id},result,db=db)
        return result

    @staticmethod
    def _output(stored):return {'presentation':deepcopy(stored['presentation'])}

    def _stored(self,w,vid,db,*,current=True):
        row=db.execute('SELECT * FROM calendar_views WHERE workspace_id=? AND id=?',(w,vid)).fetchone()
        if not row:reject('PRESENTATION_NOT_FOUND','视图不属于当前账户。',404)
        try:
            stored=json.loads(row['snapshot']);sig=stored.pop('service_signature')
            expected=self.g.financial_metrics._tool_signature({'contract':CONTRACT,'stored':stored})
            if not hmac.compare_digest(sig,expected):raise ValueError('signature')
            stored['service_signature']=sig
        except (ValueError,KeyError,TypeError):reject('PRESENTATION_TAMPERED','视图不是本服务发行原件。',409)
        pres=stored['presentation']
        if pres['view_id']!=vid or pres['workspace_binding']!=w:reject('PRESENTATION_TAMPERED','视图对象绑定不一致。',409)
        if self.bank.clock()>=row['expires_at']:reject('PRESENTATION_EXPIRED','视图引用已过期。',410)
        if current and stored.get('trusted_context') is not None:
            identity=self._session_binding(w,db,required=True)
            if identity!=stored['trusted_context']['identity']:
                reject('PRESENTATION_IDENTITY_CHANGED','旧展示授权不能在切换账户后继续使用。',409)
        if current and pres['source_versions']!=self.source_versions(w,db):reject('PRESENTATION_STALE','视图来源已变化，请重新查询。',409)
        if current and pres['authorization_revision']!={'personal':int(pres['source_versions']['personal']),'privacy':privacy_revision(db,w)}:
            reject('PRESENTATION_STALE','展示授权或个人资料已变化。',409)
        if current and stored.get('trusted_context') is not None:
            if stored['trusted_context']!=self._trusted_context(w,pres['source_versions'],db,required=True):
                reject('PRESENTATION_STALE','展示受信上下文已变化。',409)
        return stored

    def binding(self,w,args):
        if not isinstance(args,dict) or set(args)!={'view_id','request_id','message_id'}:reject('PRESENTATION_BINDING','视图绑定无效。',403)
        self._identity(args['request_id'],args['message_id'])
        return deepcopy(args)

    def describe(self,w,args,binding,versions,db=None):
        if db is None:
            with closing(self.bank.connect()) as connection:return self.describe(w,args,binding,versions,connection)
        stored=self._stored(w,binding['view_id'],db)
        if args!=binding or any(stored['presentation'][k]!=binding[k] for k in ('request_id','message_id')):
            reject('PRESENTATION_BINDING','不能把另一消息的视图当作当前回复。',403)
        return self._output(stored),stored['metrics'],stored['metric_kinds'],{'projection_hash':stored['presentation']['projection_hash'],
            'view_id':binding['view_id'],'visible_amount_occurrences':len(stored['metrics']),'final_review_wired':False}

    def resolve(self,w,vid,*,request_id,message_id,bank_db=None):
        self._identity(request_id,message_id)
        with self._publication(bank_db) as db:
            stored=self._stored(w,vid,db)
            if stored['presentation']['request_id']!=request_id:reject('PRESENTATION_BINDING','引用不属于该消息。',403)
            result=self._output(stored)
            result['metric_batch']=self.g.financial_metrics.metrics_for_tool(w,'presentation_view',
                {'view_id':vid,'request_id':request_id,'message_id':message_id},result,db=db)
        return result

    def reissue_selected_detail(self,w,old_view_id,selection_ref,expected_parent_projection_hash,*,
                                trusted_current_request,trusted_current_message,bank_db=None):
        """当前身份重新查询服务发行的旧选择；旧批准和旧请求不迁移。"""
        self._identity(trusted_current_request,trusted_current_message)
        if (type(old_view_id) is not str or re.fullmatch(r'[a-f0-9]{32}',old_view_id) is None
                or type(expected_parent_projection_hash) is not str
                or re.fullmatch(r'[a-f0-9]{64}',expected_parent_projection_hash) is None):
            reject('PRESENTATION_SELECTOR','父引用或摘要格式无效。')
        if (type(selection_ref) is not dict or set(selection_ref) not in ({'point_ref'},{'item_ref'})
                or any(type(v) is not str or not 1<=len(v)<=512 for v in selection_ref.values())):
            reject('PRESENTATION_SELECTOR','跨轮详情只能选择服务发行的图点或事项。')
        with self._reissue_atomic(bank_db) as db:
            self._session_binding(w,db,required=True)
            stored=self._stored(w,old_view_id,db);original=stored['presentation']
            if stored.get('trusted_context') is None:
                reject('PRESENTATION_IDENTITY_REQUIRED','旧原件缺少可信会话绑定，请重新查询。',403)
            if trusted_current_request==original['request_id']:
                reject('PRESENTATION_BINDING','跨轮详情必须使用当前新请求。',403)
            if not hmac.compare_digest(original['projection_hash'],expected_parent_projection_hash):
                reject('PRESENTATION_PARENT_MISMATCH','父视图投影已变化。',409)
            p=deepcopy(stored['parameters']);selected=deepcopy(stored['selected_item_ids']);details=False
            if 'point_ref' in selection_ref:
                point=next((x for x in original['points'] if x['point_ref']==selection_ref['point_ref']),None)
                if point is None:reject('PRESENTATION_SELECTOR','图点不属于原视图。',403)
                p.update({'from':point['start'],'to':point['end'],'granularity':'day','layout':None,'offset':0,'limit':7})
                if original['series_kind']=='interval_pnl' and point.get('opening_on'):
                    p['from']=point['opening_on']
                if point.get('category_values') is not None:selected=deepcopy(point['selected_item_refs'])
            else:
                item=next((i for i in stored['items'] if i['id']==selection_ref['item_ref']),None)
                visible={x.get('item_ref') for x in original['points']}
                visible.update(x.get('item_ref') for point in original['points'] for x in point.get('preview',[]))
                if item is None or item['id'] not in visible:
                    reject('PRESENTATION_SELECTOR','事项引用没有在原视图实际发行。',403)
                p.update({'from':item['date'],'to':item['date'],'granularity':'day','layout':None,'offset':0,'limit':1})
                selected=[item['id']];details=True
            if not original['range']['from']<=p['from']<=p['to']<=original['range']['to']:
                reject('PRESENTATION_RANGE','新详情不得扩大原查询期间。',403)
            p={k:v for k,v in p.items() if k not in {'as_of','clock_basis'} and v is not None}
            lineage={'old_view_id':old_view_id,'parent_projection_hash':original['projection_hash'],
                     'selection_ref':deepcopy(selection_ref)}
            result=self._query(w,p,request_id=trusted_current_request,message_id=trusted_current_message,
                parent=old_view_id,selected_items=selected,details=details,
                parent_projection_hash=original['projection_hash'],bank_db=db,_lineage=lineage)
            # 最后仍回查旧原件，私库/代码/权限漂移不能留下已签发子对象。
            self._stored(w,old_view_id,db)
            self.g.financial_metrics.validate(w,result['metric_batch'],db)
            return result

    def detail(self,w,vid,selector,*,request_id,message_id,bank_db=None):
        with self._reissue_atomic(bank_db) as db:
            return self._detail(w,vid,selector,request_id=request_id,message_id=message_id,db=db)

    def _detail(self,w,vid,selector,*,request_id,message_id,db):
        original=self.resolve(w,vid,request_id=request_id,message_id=message_id,bank_db=db)['presentation']
        if not isinstance(selector,dict) or set(selector)-{'point_ref','from','to','item_ref','offset','limit'}:
            reject('PRESENTATION_SELECTOR','子视图只能使用受信图点或所选范围。')
        stored=self._stored(w,vid,db)
        p=deepcopy(stored['parameters']);selected=deepcopy(stored['selected_item_ids'])
        if 'point_ref' in selector:
            point=next((x for x in original['points'] if x['point_ref']==selector['point_ref']),None)
            if not point:reject('PRESENTATION_SELECTOR','图点不属于这份视图。',403)
            if 'from' in selector or 'to' in selector:reject('PRESENTATION_SELECTOR','图点与范围不能混用。')
            p.update({'from':point['start'],'to':point['end'],'granularity':'day','layout':None,'offset':0,'limit':7})
            if original['series_kind']=='interval_pnl' and point.get('opening_on'):
                p['from']=point['opening_on']
            if point.get('category_values') is not None:
                selected=deepcopy(point['selected_item_refs'])
        elif 'from' in selector or 'to' in selector:
            if set(selector)&{'item_ref'} or not ('from' in selector and 'to' in selector):reject('PRESENTATION_SELECTOR','区间选择需要完整起止日期。')
            day(selector['from']);day(selector['to'])
            if not original['range']['from']<=selector['from']<=selector['to']<=original['range']['to']:
                reject('PRESENTATION_RANGE','所选范围超出原视图。')
            p.update({'from':selector['from'],'to':selector['to'],'granularity':None,'layout':None,'offset':0})
        elif 'item_ref' in selector:
            item=next((i for i in stored['items'] if i['id']==selector['item_ref']),None)
            if item is None:reject('PRESENTATION_SELECTOR','事项不属于原筛选。',403)
            p.update({'from':item['date'],'to':item['date'],'granularity':'day','layout':None,'offset':0,'limit':1})
            selected=[item['id']]
        else:reject('PRESENTATION_SELECTOR','需要图点、事项或所选区间。')
        if not original['range']['from']<=p['from']<=p['to']<=original['range']['to']:
            reject('PRESENTATION_RANGE','新详情不得扩大原查询期间。',403)
        p.update({k:selector[k] for k in ('offset','limit') if k in selector})
        p={k:v for k,v in p.items() if k not in {'as_of','clock_basis'} and v is not None}
        # 旧范围文本不移植；本轮受控结果作为独立审核对象。
        child=self.query(w,p,request_id=request_id,message_id=message_id,parent=vid,selected_items=selected,
                         parent_projection_hash=original['projection_hash'],bank_db=db)
        self.resolve(w,vid,request_id=request_id,message_id=message_id,bank_db=db)
        self.g.financial_metrics.validate(w,child['metric_batch'],db)
        return child

    def item_page(self,w,vid,point_ref,*,request_id,message_id,offset=0,limit=8,bank_db=None):
        with self._reissue_atomic(bank_db) as db:
            return self._item_page(w,vid,point_ref,request_id=request_id,message_id=message_id,offset=offset,limit=limit,db=db)

    def _item_page(self,w,vid,point_ref,*,request_id,message_id,offset,limit,db):
        pres=self.resolve(w,vid,request_id=request_id,message_id=message_id,bank_db=db)['presentation']
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=8:reject('PRESENTATION_PAGE','事项详情单页最多8条。')
        point=next((p for p in pres['points'] if p['point_ref']==point_ref),None)
        if not point:reject('PRESENTATION_SELECTOR','图点不属于原视图。',403)
        stored=self._stored(w,vid,db)
        items=[i for i in stored['items'] if point['start']<=i['date']<=point['end']]
        if point.get('category_values') is not None:items=[i for i in items if i['id'] in point['selected_item_refs']]
        selected=[i['id'] for i in sorted(items,key=lambda x:(x['date'],x['id']))[offset:offset+limit]]
        p=deepcopy(stored['parameters']);p.update({'from':point['start'],'to':point['end'],'layout':None,
            'offset':0,'limit':1,'granularity':'day'})
        p={k:v for k,v in p.items() if k not in {'as_of','clock_basis'} and v is not None}
        result=self.query(w,p,request_id=request_id,message_id=message_id,parent=vid,selected_items=selected,
                          details=True,parent_projection_hash=pres['projection_hash'],
                          detail_page_meta={'offset':offset,'limit':limit,'total':len(items),'has_more':offset+limit<len(items)},bank_db=db)
        self.resolve(w,vid,request_id=request_id,message_id=message_id,bank_db=db)
        self.g.financial_metrics.validate(w,result['metric_batch'],db)
        return result

    def review_projection(self,w,result,*,bank_db=None):
        batch=result.get('metric_batch');pres=result.get('presentation')
        if not isinstance(batch,dict) or not isinstance(pres,dict):reject('PRESENTATION_REVIEW','展示缺少原发行批次。',409)
        self.g.financial_metrics.validate(w,batch,bank_db)
        if bank_db is None:
            with closing(self.bank.connect()) as db:stored=self._stored(w,pres['view_id'],db)
        else:stored=self._stored(w,pres['view_id'],bank_db)
        if pres!=stored['presentation']:reject('PRESENTATION_TAMPERED','审核投影与实际发行展示不一致。',409)
        if batch['coverage']['projection_hash']!=pres['projection_hash']:reject('PRESENTATION_REVIEW','金额与展示不是同一批准对象。',409)
        amount_occurrences=[{'path':'points/'+str(i)+'/'+field,'metric_id':p[field]['metric_id'],
                            'value_minor':p[field]['value_minor'],'currency':p[field]['currency']}
                           for i,p in enumerate(pres['points']) for field in ('value','observed_value','cash_delta','budget') if field in p]
        if len(amount_occurrences)>24 or len(amount_occurrences)!=len(batch['metrics']):reject('PRESENTATION_AMOUNT_LIMIT','展示值与金额声明没有逐项匹配。',409)
        byid={m['metric_id']:m for m in batch['metrics']}
        for occurrence in amount_occurrences:
            m=byid.get(occurrence['metric_id'])
            if not m or any(m[k]!=occurrence[k] for k in ('value_minor','currency')):reject('PRESENTATION_TAMPERED','图点金额与指标不一致。',409)
        return {'presentation':deepcopy(pres),'amount_occurrences':amount_occurrences,'projection_hash':pres['projection_hash'],
                'metric_snapshot_id':batch['snapshot_id'],'deterministically_validated':True,
                'semantic_review_status':'owner_wiring_required','final_publication_status':'owner_wiring_required'}

    def complete_review_projection(self,w,result,text,amount_claims=(),*,surface_bindings=(),language='zh',bank_db=None):
        """同一审核对象包含正文和全部展示；复用现逐occ金额门，不增加模型。"""
        from agent_runtime.metric_claims import MetricRegistry, MetricClaimError, merge_source_versions, monetary_occurrences
        if type(language) is not str or language not in {'zh','en'}:
            reject('PRESENTATION_LANGUAGE','审核投影语言必须明确为zh或en。')
        review=self.review_projection(w,result,bank_db=bank_db);batch=result['metric_batch'];pres=result['presentation']
        registry=MetricRegistry()
        try:
            for metric in batch['metrics']:
                registry.register(metric,kind=batch['metric_kinds'][metric['metric_id']],evidence_id=batch['snapshot_id'])
            body=registry.validate_answer(text,list(amount_claims),current_source_versions=merge_source_versions(batch['metrics']))
            # 来源名称/标签中的金额不能绕开正文门；待原件引用gate接线前保守拒绝。
            visible=[pres['title'],*pres['important_states'],*pres['missing']]
            visible += [x[k] for x in pres['followups'] for k in ('label','text')]
            visible += [p[k] for p in pres['points'] for k in ('title','label') if k in p]
            visible += [x['title'] for p in pres['points'] for x in p.get('preview',[])]
            if any(monetary_occurrences(value) for value in visible):
                reject('PRESENTATION_LABEL_AMOUNT_UNBOUND','显示标题/标签含尚未绑定的金额。',409)
        except MetricClaimError as e:reject(e.code,'正文金额与已发行指标绑定不一致。',409)
        if not isinstance(surface_bindings,(list,tuple)) or len(surface_bindings)>24:reject('PRESENTATION_AMOUNT_LIMIT','附加显示金额数量无效。',409)
        extra=[];paths={x['path'] for x in review['amount_occurrences']}
        for surface in surface_bindings:
            if (not isinstance(surface,dict) or set(surface)!={'path','metric_id'}
                    or not isinstance(surface['path'],str) or not 1<=len(surface['path'])<=512
                    or not isinstance(surface['metric_id'],str) or not 1<=len(surface['metric_id'])<=160 or surface['path'] in paths):
                reject('PRESENTATION_SURFACE','每一个tooltip/汇总/表格显示位置须独立声明。',409)
            try:m=registry.lookup(surface['metric_id'])['metric']
            except MetricClaimError as e:reject(e.code,'附加显示引用没有实际发行。',409)
            paths.add(surface['path']);extra.append({**surface,'value_minor':m['value_minor'],'currency':m['currency']})
        count=len(review['amount_occurrences'])+body['amount_occurrence_count']+len(extra)
        if count>24:reject('PRESENTATION_AMOUNT_LIMIT','正文和所有展示位置合计超过24条。',409)
        packet={'schema_version':1,'language':language,'text':text,'presentation':deepcopy(pres),'followups':deepcopy(pres['followups']),
                'body_amount_contract':body,'display_amount_occurrences':review['amount_occurrences']+extra,
                'visible_amount_occurrence_count':count,'metric_snapshot_id':batch['snapshot_id'],
                'semantic_review_status':'owner_wiring_required','final_publication_status':'owner_wiring_required'}
        packet['projection_hash']=fingerprint(packet)
        return packet

    def message_state(self,w,result,approved_content,language='zh',amount_claims=(),*,surface_bindings=(),bank_db=None):
        review=self.complete_review_projection(w,result,approved_content,amount_claims,surface_bindings=surface_bindings,language=language,bank_db=bank_db);p=result['presentation']
        return {'schema_version':1,'message_id':p['message_id'],'request_id':p['request_id'],'workspace_binding':w,
                'language':language,'approved_content':approved_content,'content_amount_claims':deepcopy(list(amount_claims)),
                'surface_bindings':deepcopy(list(surface_bindings)),'projection_hash':review['projection_hash'],
                'view_refs':[{'view_id':p['view_id'],'projection_hash':p['projection_hash'],'metric_snapshot_id':review['metric_snapshot_id']}],
                'source_refs':[{'view_id':p['view_id'],'source_scope':p['scope'],'source_versions':p['source_versions'],'range':p['range'],
                                'currency':p['scope']['currency'],'portfolio':p['scope']['portfolio'],'accounting_basis':p['scope']['accounting_basis']}],
                'followups':deepcopy(p['followups']),'personal_revision':p['authorization_revision']['personal'],
                'privacy_revision':p['authorization_revision']['privacy'],'answer_context':p['source_versions']['private_context']}

    def validate_message_state(self,w,state,*,bank_db):
        if not isinstance(state,dict) or len(state.get('view_refs',[]))!=1:reject('PRESENTATION_HISTORY','消息缺少唯一发行视图。',409)
        vid=state['view_refs'][0]['view_id'];stored=self._stored(w,vid,bank_db)
        result=self._output(stored);p=result['presentation']
        args={'view_id':vid,'request_id':p['request_id'],'message_id':p['message_id']}
        # 固定原批次的纯构造，避免在发布锁中开另一个写事务。
        result['metric_batch']=self.g.financial_metrics.metrics_for_tool(w,'presentation_view',args,result,db=bank_db)
        canonical=self.message_state(w,result,state.get('approved_content'),state.get('language'),state.get('content_amount_claims',()),
            surface_bindings=state.get('surface_bindings',()),bank_db=bank_db)
        if canonical!=state:reject('PRESENTATION_HISTORY','消息元数据不是原发行对象。',409)
        return canonical

    def refresh_state(self,w,state):
        try:
            p=state['view_refs'][0];result=self.resolve(w,p['view_id'],request_id=state['request_id'],message_id=state['message_id'])
            canonical=self.message_state(w,result,state['approved_content'],state['language'],state.get('content_amount_claims',()),surface_bindings=state.get('surface_bindings',()))
            if canonical!=state:reject('PRESENTATION_HISTORY','历史消息绑定已变化。',409)
            return {'status':'current','message_id':state['message_id'],'followups':deepcopy(state['followups'])}
        except (BankError,KeyError,TypeError):
            return {'status':'historical_stale','message_id':state.get('message_id'),'followups':[]}
