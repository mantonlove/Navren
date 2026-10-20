"""封存的同主体、同用途资金安排；没有按余额换来源或新增收入。"""
from collections import defaultdict
from contextlib import closing
from datetime import date,timedelta
import hashlib
import json

VERSION='navren.living-funding/1'
MERCHANT='模拟本人用途资金调拨'
SCHEMA=(
    '''CREATE TABLE IF NOT EXISTS living_funding_policies(
       workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),registered_user_id TEXT NOT NULL,
       persona_id TEXT NOT NULL,initialization_id TEXT NOT NULL,template_sha256 TEXT NOT NULL,
       policy_version TEXT NOT NULL,configuration TEXT NOT NULL,created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS living_funding_transfers(
       id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspaces(id),
       event_id TEXT NOT NULL REFERENCES living_events(event_id),rule_id TEXT NOT NULL,
       scheduled_month TEXT NOT NULL,source_account_id TEXT NOT NULL,target_account_id TEXT NOT NULL,
       amount_minor INTEGER NOT NULL CHECK(amount_minor>0),out_transaction_id TEXT NOT NULL UNIQUE REFERENCES transactions(id),
       in_transaction_id TEXT NOT NULL UNIQUE REFERENCES transactions(id),posted_at TEXT NOT NULL,
       UNIQUE(workspace_id,event_id,rule_id))''')

def packed(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def derive_policy(service,persona,end_on):
    end=date.fromisoformat(end_on).replace(day=1)-timedelta(days=1)
    month=end.year*12+end.month-12;start=date(month//12,month%12+1,1)
    with closing(service._connect()) as db:
        accounts={r['account_id']:dict(r) for r in db.execute('SELECT * FROM accounts WHERE subject_id=?',(persona,))}
        groups=defaultdict(list)
        for row in db.execute('''SELECT * FROM transactions WHERE subject_id=? AND kind='internal_transfer'
            AND booked_on BETWEEN ? AND ? ORDER BY booked_on,transaction_id''',(persona,start.isoformat(),end.isoformat())):
            groups[row['transfer_group_id']].append(dict(row))
    observed=defaultdict(lambda:{'count':0,'months':defaultdict(int),'sample_groups':[]})
    excluded=defaultdict(int)
    for group,legs in groups.items():
        if not group or len(legs)!=2:excluded['unpaired']+=1;continue
        out=next((r for r in legs if r['signed_minor']<0),None)
        inc=next((r for r in legs if r['signed_minor']>0),None)
        if out is None or inc is None or out['signed_minor']+inc['signed_minor']!=0 or out['amount_minor']!=inc['amount_minor']:
            excluded['unbalanced']+=1;continue
        a,b=accounts.get(out['account_id']),accounts.get(inc['account_id'])
        if not a or not b or a['currency']!='CNY' or b['currency']!='CNY' or a['scope']!=b['scope'] or 'credit_card' in (a['account_type'],b['account_type']):
            excluded['different_purpose_or_currency']+=1;continue
        details=json.loads(out['details'])
        if details.get('temporary_advance') or details.get('bridge_direction') or '备用金' in out['category']:
            excluded['temporary_bridge_requires_repayment']+=1;continue
        if b['account_id'].rsplit(':',1)[-1] not in ('daily','wallet','operating','rent','rent1','rent2','rent3'):
            continue
        edge=observed[a['account_id'],b['account_id']]
        edge['count']+=1;edge['months'][out['booked_on'][:7]]+=out['amount_minor']
        if len(edge['sample_groups'])<3:edge['sample_groups'].append(group)
    incoming=defaultdict(list)
    for (source,target),receipt in observed.items():incoming[target].append((source,receipt))
    routes={};unavailable={}
    for target,choices in incoming.items():
        choices.sort(key=lambda item:(-item[1]['count'],item[0]))
        source,evidence=choices[0]
        # 相同用途内须有唯一或至少两倍于次路径的明确主安排；歧义不挑余额解。
        if evidence['count']<12 or len(evidence['months'])<3 or len(choices)>1 and evidence['count']<2*choices[1][1]['count']:
            unavailable[target]={'reason':'ambiguous_or_sparse_source','source_count':len(choices)};continue
        routes[target]={'source':source,'target':target,'scope':accounts[target]['scope'],
            'rule_id':'funding.topup.'+target.rsplit(':',1)[-1],
            'monthly_cap_minor':max(evidence['months'].values()),'observed_count':evidence['count'],
            'observed_months':len(evidence['months']),'sample_groups':evidence['sample_groups'],
            'selection':'unique_same_purpose' if len(choices)==1 else 'dominant_same_purpose_at_least_two_to_one'}
    return {'version':VERSION,'source_dataset_version':service.dataset_version,'start_on':start.isoformat(),
        'end_on':end.isoformat(),'routes':routes,'unavailable':unavailable,'excluded':dict(excluded)}


class FundingService:
    def __init__(self,bank,template_service_for,adapter):
        self.bank,self.template_service_for,self.adapter=bank,template_service_for,adapter

    def initialize(self,db):
        for statement in SCHEMA:db.execute(statement)

    def ensure(self,workspace,now):
        with closing(self.bank.connect()) as db:
            binding=dict(db.execute('SELECT * FROM registered_user_instances WHERE workspace_id=?',(workspace,)).fetchone())
            existing=db.execute('SELECT * FROM living_funding_policies WHERE workspace_id=?',(workspace,)).fetchone()
        if existing:return
        policy=derive_policy(self.template_service_for(workspace),binding['persona_id'],binding['template_end_on'])
        with self.bank.transaction() as db:
            current=dict(db.execute('SELECT * FROM registered_user_instances WHERE workspace_id=?',(workspace,)).fetchone())
            if current!=binding:raise RuntimeError('LIVING_FUNDING_BINDING_CHANGED')
            db.execute('INSERT OR IGNORE INTO living_funding_policies VALUES(?,?,?,?,?,?,?,?)',
                (*[binding[k] for k in ('workspace_id','registered_user_id','persona_id','initialization_id','template_sha256')],
                 VERSION,packed(policy),now.isoformat()))

    def cover(self,db,binding,event,target,now,event_id):
        workspace=binding['workspace_id']
        row=db.execute('SELECT * FROM living_funding_policies WHERE workspace_id=?',(workspace,)).fetchone()
        if not row or row['policy_version']!=VERSION or any(row[k]!=binding[k] for k in ('registered_user_id','persona_id','initialization_id','template_sha256')):
            raise RuntimeError('LIVING_FUNDING_BINDING_CHANGED')
        policy=json.loads(row['configuration'])
        if policy.get('version')!=VERSION:raise RuntimeError('LIVING_FUNDING_VERSION_CHANGED')
        links={r['source_account_id']:dict(r) for r in db.execute('''SELECT l.*,a.currency FROM connected_account_links l
            JOIN accounts a ON a.workspace_id=l.workspace_id AND a.id=l.account_id WHERE l.workspace_id=?''',(workspace,))}
        balances={r['id']:r['balance_minor'] for r in db.execute('SELECT * FROM accounts WHERE workspace_id=?',(workspace,))}
        planned=[];month=event['scheduled_at'][:7]
        def available(link):return balances[link['account_id']]-self.adapter.held_minor(db,workspace,link['account_id'])
        def fund(link,needed,visited):
            shortage=needed-available(link)
            if shortage<=0:return None
            key=link['source_account_id']
            if key in visited or len(visited)>=4:return 'funding_cycle_or_depth'
            route=policy['routes'].get(key)
            if route is None:return 'insufficient_available_funds'
            source=links.get(route['source'])
            if source is None or route['target']!=key:return 'funding_source_unavailable'
            control=db.execute('SELECT enabled FROM living_rule_controls WHERE workspace_id=? AND rule_id=?',(workspace,route['rule_id'])).fetchone()
            if control and not control[0]:return 'user_disabled_funding_rule'
            transfer_event={**event,'rule_id':route['rule_id'],'kind':'expense','merchant':MERCHANT}
            denied=self.adapter.denial(db,workspace,transfer_event,source)
            if denied:return denied
            used=db.execute('''SELECT coalesce(sum(amount_minor),0) FROM living_funding_transfers
                WHERE workspace_id=? AND rule_id=? AND scheduled_month=?''',(workspace,route['rule_id'],month)).fetchone()[0]
            used+=sum(amount for rule,_,_,amount in planned if rule==route['rule_id'])
            if shortage+used>route['monthly_cap_minor']:return 'funding_monthly_cap_exceeded'
            reason=fund(source,shortage,visited|{key})
            if reason:return reason
            balances[source['account_id']]-=shortage;balances[link['account_id']]+=shortage
            planned.append((route['rule_id'],source,link,shortage))
            return None
        reason=fund(target,event['amount_minor'],set())
        if reason:return reason
        for rule,source,destination,amount in planned:
            group='living-funding-'+hashlib.sha256(packed([workspace,event_id,rule]).encode()).hexdigest()
            if db.execute('SELECT 1 FROM living_funding_transfers WHERE id=?',(group,)).fetchone():
                raise RuntimeError('LIVING_FUNDING_EVENT_ALREADY_MOVED')
            note=packed({'origin':'living_simulation_funding','transfer_group_id':group,'expense_counted':False,
                'event_id':event_id,'scheduled_at':event['scheduled_at'],'posted_at':now.isoformat(),'policy_version':VERSION,
                'source_account_id':source['source_account_id'],'target_account_id':destination['source_account_id']})
            for link,delta,suffix in ((source,-amount,':out'),(destination,amount,':in')):
                db.execute('UPDATE accounts SET balance_minor=balance_minor+? WHERE workspace_id=? AND id=?',(delta,workspace,link['account_id']))
                db.execute('''INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)''',
                    (group+suffix,workspace,link['account_id'],now.isoformat(),MERCHANT,amount,delta,'internal_transfer','本人用途资金调拨',note))
            db.execute('INSERT INTO living_funding_transfers VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (group,workspace,event_id,rule,month,source['account_id'],destination['account_id'],amount,group+':out',group+':in',now.isoformat()))
        return None
