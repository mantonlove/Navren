"""有限离线验收：独立逐分回放、来源勾稽、归属、生活频次和截止日不变。"""
from __future__ import annotations
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, timedelta
import argparse
import csv
import json
from pathlib import Path
import sqlite3
from .realistic_history import START, END, SUBJECTS, CAPS, DRINK_MENUS, ro, sha, js


def endpoint(db):
    result={}
    for table, omit in [('subjects',set()),('accounts',{'name','opening_balance_minor','details'}),
                        ('cards',{'masked_number'}),('positions',set()),('credit_facilities',set()),
                        ('policies',{'details'}),('contracts',{'name','details'}),('assets',{'name','details'}),
                        ('connections',{'institution_name','details'}),('premium_funding',set()),('obligations',set())]:
        fields=[r[1] for r in db.execute('PRAGMA table_info('+table+')') if r[1] not in omit]
        result[table]=sorted(tuple(r) for r in db.execute('SELECT '+','.join(fields)+' FROM '+table))
    for table in ('valuations','loan_balances','account_balances'):
        result[table]=sorted(tuple(r) for r in db.execute('SELECT * FROM '+table+' WHERE date=?',(END,)))
    return result


def validate(source_path, candidate_path, output_path=None):
    checks=[]
    def check(name,ok,**detail):
        checks.append({'name':name,'passed':bool(ok),**detail})
    with closing(ro(source_path)) as source, closing(ro(candidate_path)) as db:
        metadata=dict(db.execute('SELECT key,value FROM metadata')); parent=dict(source.execute('SELECT key,value FROM metadata'))
        check('metadata_and_parent',metadata.get('history_profile')=='realistic_three_year' and metadata.get('start_date')==START and metadata.get('end_date')==END and metadata.get('source')=='navren_synthetic' and metadata.get('historical_parent_sha256')==sha(source_path) and metadata.get('historical_parent_dataset_version')==parent['dataset_version'])
        check('sqlite_integrity',db.execute('PRAGMA integrity_check').fetchone()[0]=='ok' and not db.execute('PRAGMA foreign_key_check').fetchall())
        check('stable_entities_and_endpoints',endpoint(source)==endpoint(db))
        accounts={r['account_id']:dict(r) for r in db.execute('SELECT * FROM accounts')}
        check('four_subjects',set(r[0] for r in db.execute('SELECT subject_id FROM subjects'))==SUBJECTS)
        expected_days=(date.fromisoformat(END)-date.fromisoformat(START)).days+1
        coverage=[dict(r) for r in db.execute('SELECT account_id,min(date) a,max(date) b,count(*) n FROM account_balances GROUP BY account_id')]
        check('1096_daily_snapshots',len(coverage)==len(accounts) and all(r['a']==START and r['b']==END and r['n']==expected_days for r in coverage),accounts=len(coverage),days=expected_days)
        rows=[]
        for r in db.execute('SELECT * FROM transactions ORDER BY occurred_at,transaction_id'):
            row=dict(r);row['details']=json.loads(row['details']);rows.append(row)
        original={r['transaction_id']:dict(r) for r in source.execute('SELECT * FROM transactions WHERE booked_on BETWEEN ? AND ?',(START,END))}
        budgets={r['budget_id']:dict(r) for r in db.execute('SELECT * FROM history_budget_controls')}
        budget_sums=defaultdict(lambda:[0,0,0]);invalid=[];groups=defaultdict(list);refunded=Counter();byid={r['transaction_id']:r for r in rows}
        source_events=defaultdict(list);financial_types={'investment_purchase','investment_redemption','loan_repayment','insurance_premium'}
        amount_fields=('amount_minor','signed_minor','expense_minor')
        for r in rows:
            a=accounts.get(r['account_id']);d=r['details'];b=budgets.get(d.get('budget_id'))
            if not a or a['subject_id']!=r['subject_id'] or not START<=r['booked_on']<=END or r['occurred_at'][:10]!=r['booked_on'] or any(type(r[k]) is not int for k in amount_fields) or r['amount_minor']<=0 or abs(r['signed_minor'])!=r['amount_minor']:invalid.append(r['transaction_id'])
            if r['kind']=='internal_transfer':groups[r['transfer_group_id']].append(r)
            else:
                if not b or (r['subject_id'],r['booked_on'][:7],r['account_id'],r['scope'],d.get('original_category'),r['kind'])!=(b['subject_id'],b['month'],b['account_id'],b['scope'],b['original_category'],b['kind']):invalid.append(r['transaction_id'])
                if b:
                    for i,k in enumerate(amount_fields):budget_sums[b['budget_id']][i]+=r[k]
            if r['kind'] in financial_types:source_events[tuple(r['details']['legacy_source_transaction_ids'])].append(r)
            if r['kind']=='refund':
                purchase=byid.get(r['original_transaction_id']);refunded[r['original_transaction_id']]+=r['amount_minor']
                if not purchase or purchase['kind']!='expense' or purchase['occurred_at']>r['occurred_at'] or (purchase['subject_id'],purchase['account_id'],purchase['scope'],purchase['details']['original_category'])!=(r['subject_id'],r['account_id'],r['scope'],d['original_category']) or refunded[r['original_transaction_id']]>purchase['amount_minor']:invalid.append(r['transaction_id'])
        check('integer_money_ownership_and_refunds',not invalid,violations=invalid[:20])
        mismatches=[]
        for ident,b in budgets.items():
            olds=[original[i] for i in json.loads(b['source_transaction_ids'])]
            exp=[sum(r[k] for r in olds) for k in amount_fields]
            if exp!=[b[k] for k in amount_fields] or exp!=budget_sums[ident]:mismatches.append(ident)
        assigned=[i for b in budgets.values() for i in json.loads(b['source_transaction_ids'])]
        economic_old={i for i,r in original.items() if r['kind']!='internal_transfer'}
        check('monthly_source_budget_exact',not mismatches and len(assigned)==len(set(assigned)) and set(assigned)==economic_old,budgets=len(budgets),mismatches=mismatches[:20])
        event_errors=[]
        for r in original.values():
            if r['kind'] in financial_types:
                matches=source_events[(r['transaction_id'],)]
                if len(matches)!=1 or any(matches[0][k]!=r[k] for k in (*amount_fields,'kind','account_id','contract_id','source_event_id','booked_on')):event_errors.append(r['transaction_id'])
        check('financial_source_events_preserved',not event_errors,violations=event_errors[:20])
        transfer_errors=[];old_transfer_sums=Counter();new_transfer_sums=Counter();bridges=defaultdict(list)
        for r in original.values():
            if r['kind']=='internal_transfer' and r['signed_minor']<0:old_transfer_sums[r['transfer_group_id']]+=r['amount_minor']
        for ident,legs in groups.items():
            if len(legs)!=2 or sum(r['signed_minor'] for r in legs) or sum(r['expense_minor'] for r in legs) or len({r['subject_id'] for r in legs})!=1 or len({r['amount_minor'] for r in legs})!=1 or len({r['economic_event_id'] for r in legs})!=1:transfer_errors.append(ident)
            if legs:
                if legs[0]['details'].get('temporary_advance'):bridges[legs[0]['details']['history_bridge_id']].extend(legs)
                else:new_transfer_sums[legs[0]['details']['original_transfer_group_id']]+=legs[0]['amount_minor']
        check('paired_transfers_and_original_amounts',not transfer_errors and old_transfer_sums==new_transfer_sums,groups=len(groups),violations=transfer_errors[:10])
        bridge_errors=[]
        for ident,legs in bridges.items():
            totals=Counter();directions=Counter();months={r['booked_on'][:7] for r in legs};linked=[accounts[r['account_id']] for r in legs]
            for r in legs:totals[r['account_id']]+=r['signed_minor'];directions[r['details']['bridge_direction']]+=1
            advance_days=[r['booked_on'] for r in legs if r['details']['bridge_direction']=='advance']
            repayment_days=[r['booked_on'] for r in legs if r['details']['bridge_direction']=='repayment']
            first=max(START,next(iter(months))+'-01') if len(months)==1 else ''
            last=max(r['date'] for r in db.execute('SELECT date FROM account_balances WHERE date LIKE ?',(next(iter(months))+'%',))) if len(months)==1 else ''
            if (len(legs)!=4 or any(totals.values()) or directions!={'advance':2,'repayment':2} or len(months)!=1
                or any(d!=first for d in advance_days) or any(d!=last for d in repayment_days)
                or len({a['subject_id'] for a in linked})!=1 or len({a['scope'] for a in linked})!=1
                or any(a['account_type']=='credit_card' for a in linked)
                or any(r['details'].get('original_transfer_group_id') is not None or r['details'].get('legacy_source_transaction_ids') for r in legs)):
                bridge_errors.append(ident)
        check('monthly_cash_bridges_close_without_income',not bridge_errors,bridges=len(bridges),violations=bridge_errors[:10])
        balances={i:a['opening_balance_minor'] for i,a in accounts.items()};daily={(r['account_id'],r['date']):r['balance_minor'] for r in db.execute('SELECT * FROM account_balances')}
        limits={r['account_id']:r['limit_minor'] for r in db.execute('SELECT * FROM credit_facilities')};intraday=[];snapshot_errors=[];byday=defaultdict(list)
        for r in rows:byday[r['booked_on']].append(r)
        for n in range(expected_days):
            day=(date.fromisoformat(START)+timedelta(days=n)).isoformat()
            for r in byday[day]:
                account=r['account_id'];balances[account]+=r['signed_minor']
                value=balances[account]
                if value!=r['details'].get('balance_after_minor') or (value<0 if accounts[account]['account_type']!='credit_card' else not -limits[account]<=value<=0):intraday.append({'transaction_id':r['transaction_id'],'balance_minor':value})
            for account,value in balances.items():
                if daily.get((account,day))!=value:snapshot_errors.append((account,day))
        check('chronological_intraday_replay',not intraday,violations=intraday[:20])
        check('daily_end_replay',not snapshot_errors,violations=snapshot_errors[:10])
        months=[r[0] for r in db.execute('SELECT DISTINCT substr(date,1,7) FROM account_balances')];endpoint_errors=[]
        for month in months:
            day=db.execute('SELECT max(date) FROM account_balances WHERE date LIKE ?',(month+'%',)).fetchone()[0]
            a=dict(db.execute('SELECT account_id,balance_minor FROM account_balances WHERE date=?',(day,)))
            b=dict(source.execute('SELECT account_id,balance_minor FROM account_balances WHERE date=?',(day,)))
            if a!=b:endpoint_errors.append(day)
        check('original_month_end_balances',not endpoint_errors,violations=endpoint_errors)
        events=[r['economic_event_id'] for r in rows if r['kind']!='internal_transfer']
        check('independent_economic_events',len(events)==len(set(events)))
        visible_errors=[];categories=defaultdict(Counter);frequency=Counter();price=defaultdict(list);monthly=defaultdict(Counter);hours=defaultdict(Counter);deferred=Counter();late=Counter();expense_count=Counter()
        for r in rows:
            uid,cat=r['subject_id'],r['category'];categories[uid][cat]+=1;hours[uid][r['occurred_at'][11:13]]+=1
            if any(x in r['counterparty'] for x in ('合成','模拟','演示','SIM')):visible_errors.append(r['transaction_id'])
            if r['kind']=='expense':
                expense_count[uid]+=1;frequency[(uid,r['booked_on'],cat)]+=1;price[cat].append(r['amount_minor']);monthly[(uid,r['booked_on'][:7])][cat]+=1
                if r['details'].get('planned_occurred_at'):deferred[uid]+=1
                if r['occurred_at'][11:16]>='23:40':late[uid]+=1
                if cat in CAPS and r['amount_minor']>CAPS[cat]:visible_errors.append(r['transaction_id'])
                if cat=='公共交通' and date.fromisoformat(r['booked_on']).weekday()>=5:visible_errors.append(r['transaction_id'])
        check('ordinary_labels_and_price_caps',not visible_errors,violations=visible_errors[:20])
        transport=[r for r in rows if r['kind']=='expense' and r['category']=='公共交通']
        check('public_transit_fare_bands',bool(transport) and all(
            (r['counterparty']=='杭州公交' and r['amount_minor']==200) or
            (r['counterparty']=='杭州地铁' and r['amount_minor'] in range(200,801,100)) for r in transport))
        drinks=[r for r in rows if r['kind']=='expense' and r['category']=='饮品']
        check('merchant_drink_prices_and_cups',bool(drinks) and all(
            r['details'].get('cups') in (1,2) and r['details'].get('unit_price_minor') in DRINK_MENUS.get(r['counterparty'],())
            and r['amount_minor']==r['details']['unit_price_minor']*r['details']['cups'] for r in drinks))
        meals=[r for r in rows if r['kind']=='expense' and r['category']=='餐饮堂食']
        check('meal_party_and_merchant',bool(meals) and all(
            type(r['details'].get('meal_party_size')) is int and r['details']['meal_party_size']>0 and r['details'].get('meal_occasion')
            and (r['counterparty'] not in ('兰州拉面','沙县小吃','社区快餐店') or r['amount_minor']<=5000)
            and (r['amount_minor']<=60000 or r['counterparty'] in ('西湖宴请餐厅','酒店餐饮服务')) for r in meals))
        check('public_transit_daily_cap',all(v<=2 for (u,d,c),v in frequency.items() if c=='公共交通'))
        ordinary_frequency=Counter((r['subject_id'],r['booked_on'],r['category']) for r in rows if r['kind']=='expense' and r['scope']!='self_employed')
        check('all_ordinary_daily_category_caps',all(n<=(3 if c in ('生活采购','购物','餐饮堂食','赡养与家庭往来','私人出行服务') else 2) for (u,d,c),n in ordinary_frequency.items()))
        density=Counter((u,d) for u,d,c in ordinary_frequency for _ in range(ordinary_frequency[(u,d,c)]))
        check('daily_life_channels_present',all({'饮品','外卖','餐饮堂食','购物','药店购药'}.issubset(categories[u]) for u in SUBJECTS))
        restaurant=[r for r in rows if r['category']=='餐饮经营收款']
        weekend=[r['amount_minor'] for r in restaurant if date.fromisoformat(r['booked_on']).weekday()>=5]
        weekday=[r['amount_minor'] for r in restaurant if date.fromisoformat(r['booked_on']).weekday()<5]
        weekend_mean=sum(weekend)/max(1,len(weekend));weekday_mean=sum(weekday)/max(1,len(weekday))
        check('restaurant_weekend_weighted',bool(weekend) and weekend_mean>weekday_mean*1.15,weekend_mean_minor=round(weekend_mean),weekday_mean_minor=round(weekday_mean))
        scheduling={u:{'expenses':expense_count[u],'deferred':deferred[u],'deferred_ratio':round(deferred[u]/max(1,expense_count[u]),4),'late':late[u],'late_ratio':round(late[u]/max(1,expense_count[u]),4)} for u in sorted(SUBJECTS)}
        check('life_timing_preserved',all(x['late_ratio']<.02 and x['deferred_ratio']<.15 for x in scheduling.values()),scheduling=scheduling)
        # 有界接口：四主体三视图、边界和身份拒绝，不启动运行实例。
        from .service import FinancialDataService
        from .presentation import FinancialPresentationService
        service=FinancialDataService(candidate_path);presentation=FinancialPresentationService(service);interface=[]
        for u in sorted(SUBJECTS):
            for args in ({'view':'overview'},{'view':'month_report','month':'2026-09'},{'view':'calendar','start_date':'2026-09-01','end_date':'2026-09-30'}):
                result=presentation.query(u,args)
                interface.append(not result.get('error') and result['evidence']['coverage_start']==START and result['evidence']['coverage_end']==END)
        interface.append(service.query('tycoon','get_financial_summary',{'as_of':'2023-10-03'}).get('error',{}).get('code')=='OUTSIDE_COVERAGE')
        foreign=next(a['account_id'] for a in accounts.values() if a['subject_id']=='grain_farmer')
        interface.append(service.query('tycoon','get_account_overview',{'account_id':foreign}).get('error',{}).get('code')=='ENTITY_NOT_ACCESSIBLE')
        for args in ({'subject_id':'grain_farmer'},{'sql':'SELECT * FROM accounts'},{'database_path':str(source_path)}):
            interface.append(service.query('tycoon','list_accounts',args).get('error',{}).get('code')=='INVALID_ARGUMENTS')
        check('dynamic_coverage_and_bounded_identity_interfaces',all(interface),checks=len(interface))
        result={'status':'passed' if all(c['passed'] for c in checks) else 'failed','candidate_path':str(Path(candidate_path).resolve()),'candidate_sha256':sha(candidate_path),'dataset_version':metadata['dataset_version'],'source':'navren_synthetic','coverage':{'start':START,'end':END,'days':expected_days},'checks':checks,'rows':len(rows),'scheduling':scheduling,'personas':{u:dict(categories[u]) for u in sorted(SUBJECTS)},'price_ranges_minor':{c:{'count':len(v),'min':min(v),'max':max(v)} for c,v in sorted(price.items())},'ordinary_daily_max':{u:max((n for (uid,day),n in density.items() if uid==u),default=0) for u in sorted(SUBJECTS)},'monthly_frequency':[{'subject_id':u,'month':m,'categories':dict(v)} for (u,m),v in sorted(monthly.items())],'hourly_frequency':{u:dict(v) for u,v in hours.items()},'runtime_changed':False,'paid_model_calls':0}
        if output_path:
            path=Path(output_path);path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            with path.with_suffix('.sample.csv').open('w',newline='') as f:
                writer=csv.writer(f);writer.writerow(['subject_id','account_id','occurred_at','signed_minor','balance_after_minor','counterparty','category','kind','scope','economic_event_id'])
                for r in rows:
                    if '2026-09-01'<=r['booked_on']<='2026-09-07':writer.writerow([r[k] if k!='balance_after_minor' else r['details'][k] for k in ['subject_id','account_id','occurred_at','signed_minor','balance_after_minor','counterparty','category','kind','scope','economic_event_id']])
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True);p.add_argument('--candidate',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    output=Path(a.output).resolve()
    if output.exists() or output in (Path(a.source).resolve(),Path(a.candidate).resolve()):p.error('质量报告必须为新文件')
    result=validate(a.source,a.candidate,a.output)
    print(json.dumps({'status':result['status'],'checks':len(result['checks']),'failed':[c for c in result['checks'] if not c['passed']],'rows':result['rows']},ensure_ascii=False))
    return 0 if result['status']=='passed' else 1

if __name__=='__main__':raise SystemExit(main())
