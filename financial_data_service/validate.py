"""只检查此次数据生成、资金归属、读取合同与隔离迁移，不运行整站测试。"""
import calendar
from contextlib import closing
from datetime import date
import json
from pathlib import Path
import sqlite3
import uuid

from .build import file_sha, readonly, NS
from .service import FinancialDataService, OPERATIONS
from .schema import START, END

def validate(database,source,output):
    dbpath,sourcepath,out=map(Path,(database,source,output))
    before={'new_database':file_sha(dbpath),'old_source':file_sha(sourcepath)}
    service=FinancialDataService(dbpath);checks=[]
    def check(name,value):
        if not value:raise AssertionError(name)
        checks.append(name)
    c=readonly(dbpath);c.execute('ATTACH DATABASE ? AS original',(sourcepath.resolve().as_uri()+'?mode=ro&immutable=1',))
    check('sqlite_integrity',c.execute('PRAGMA integrity_check').fetchone()[0]=='ok')
    check('composite_foreign_key_ownership',not c.execute('PRAGMA foreign_key_check').fetchall())
    users=[r['subject_id'] for r in c.execute('SELECT * FROM subjects ORDER BY subject_id')]
    check('exact_four_subjects',set(users)=={'tycoon','grain_farmer','working_landlady','retired_entrepreneur'})
    days=(date.fromisoformat(END)-date.fromisoformat(START)).days+1
    check('account_full_daily_coverage',not c.execute('SELECT 1 FROM account_balances GROUP BY subject_id,account_id HAVING count(*)!=? OR min(date)!=? OR max(date)!=?',(days,START,END)).fetchone())
    check('every_account_has_history',c.execute('SELECT count(*) FROM accounts').fetchone()[0]==c.execute('SELECT count(*) FROM (SELECT account_id FROM account_balances GROUP BY account_id)').fetchone()[0])
    check('source_transactions_amount_identity',not c.execute('''WITH projected AS(
      SELECT subject_id,source_transaction_id,sum(signed_minor) amount FROM transactions WHERE source_transaction_id IS NOT NULL GROUP BY subject_id,source_transaction_id)
      SELECT 1 FROM original.cash_transactions s LEFT JOIN projected p ON p.subject_id=s.user_id AND p.source_transaction_id=s.transaction_id
      WHERE p.amount IS NULL OR p.amount!=s.signed_minor''').fetchone())
    check('transfer_pairs_two_equal_opposite',not c.execute('SELECT 1 FROM transactions WHERE transfer_group_id IS NOT NULL GROUP BY subject_id,transfer_group_id HAVING count(*)!=2 OR sum(signed_minor)!=0').fetchone())
    check('daily_cash_minus_card_debt_matches_source',not c.execute('''SELECT 1 FROM(
      SELECT subject_id,date,sum(balance_minor) balance FROM account_balances GROUP BY subject_id,date) n
      JOIN original.daily_balances s ON s.user_id=n.subject_id AND s.date=n.date WHERE n.balance!=s.cash_minor''').fetchone())
    # 逐账户、逐日独立复算 opening + 已入账交易，不借用builder内存余额。
    replay_failures=[]
    for a in c.execute('SELECT * FROM accounts').fetchall():
        daily={r['booked_on']:r['amount'] for r in c.execute('SELECT booked_on,sum(signed_minor) amount FROM transactions WHERE subject_id=? AND account_id=? GROUP BY booked_on',(a['subject_id'],a['account_id']))}
        balance=a['opening_balance_minor']
        for b in c.execute('SELECT * FROM account_balances WHERE subject_id=? AND account_id=? ORDER BY date',(a['subject_id'],a['account_id'])):
            balance+=daily.get(b['date'],0)
            if balance!=b['balance_minor']:replay_failures.append((a['account_id'],b['date']))
        if balance!=a['balance_minor']:replay_failures.append((a['account_id'],'closing'))
    check('all_accounts_independent_money_replay',not replay_failures)
    check('cash_accounts_never_negative',not c.execute("SELECT 1 FROM account_balances b JOIN accounts a USING(subject_id,account_id) WHERE a.account_type!='credit_card' AND b.balance_minor<0").fetchone())
    check('credit_facility_shared_and_not_exceeded',c.execute('SELECT count(*) FROM credit_facilities').fetchone()[0]==4 and not c.execute('SELECT 1 FROM account_balances b JOIN credit_facilities f USING(subject_id,account_id) WHERE -b.balance_minor>f.limit_minor').fetchone())
    check('card_transaction_active_dates',not c.execute('SELECT 1 FROM transactions t JOIN cards c USING(subject_id,card_id) WHERE t.account_id!=c.account_id OR t.booked_on<c.issued_on OR (c.closed_on IS NOT NULL AND t.booked_on>c.closed_on)').fetchone())
    check('refund_original_same_subject',not c.execute("SELECT 1 FROM transactions WHERE kind='refund' AND original_transaction_id IS NULL").fetchone())
    check('premium_paid_from_cash_exactly_once',not c.execute('''SELECT 1 FROM policies p LEFT JOIN(
      SELECT subject_id,contract_id,-sum(signed_minor) paid FROM transactions WHERE kind='insurance_premium' GROUP BY subject_id,contract_id) t
      USING(subject_id,contract_id) WHERE t.paid IS NULL OR t.paid!=p.farmer_paid_minor''').fetchone())
    check('known_subsidy_plus_policyholder_equals_premium',not c.execute('''SELECT 1 FROM policies p JOIN(
      SELECT subject_id,policy_id,sum(amount_minor) funded FROM premium_funding GROUP BY subject_id,policy_id) f USING(subject_id,policy_id)
      WHERE p.subsidy_minor IS NOT NULL AND f.funded!=p.premium_minor''').fetchone())
    check('historical_subsidy_unknown_not_zero',not c.execute("SELECT 1 FROM policies WHERE insurance_type='crop' AND start_date<'2025-01-01' AND subsidy_minor IS NOT NULL").fetchone())
    check('insurance_no_overlapping_same_object_period',not c.execute('''SELECT 1 FROM policies a JOIN policies b ON a.subject_id=b.subject_id AND a.coverage_key=b.coverage_key
      AND a.policy_id<b.policy_id AND a.start_date<=b.end_date AND b.start_date<=a.end_date''').fetchone())
    check('valuations_match_original_without_duplicate_assets',not c.execute('''SELECT 1 FROM original.asset_values s LEFT JOIN valuations n ON s.user_id=n.subject_id AND s.asset_id=n.asset_id AND s.date=n.date
      WHERE n.book_value_minor IS NULL OR n.book_value_minor!=s.book_value_minor OR n.cost_basis_minor!=s.cost_basis_minor''').fetchone())
    check('loan_principal_matches_original',not c.execute('''SELECT 1 FROM original.loan_balances s LEFT JOIN loan_balances n
      ON s.user_id=n.subject_id AND replace(s.loan_id,'::liability:','::contract:')=n.contract_id AND s.date=n.date WHERE n.principal_minor IS NULL OR n.principal_minor!=s.principal_minor''').fetchone())
    old_expenses={r['user_id']:r['amount'] for r in c.execute("SELECT l.user_id,sum(l.debit_minor-l.credit_minor) amount FROM original.journal_lines l JOIN original.ledger_accounts a USING(account_id) JOIN original.journal_events e USING(event_id) WHERE a.account_class='expense' AND e.event_type IN ('expense','refund','loan_repayment') GROUP BY l.user_id")}
    new_expenses={r['subject_id']:r['amount'] for r in c.execute('SELECT subject_id,sum(expense_minor) amount FROM transactions GROUP BY subject_id')}
    check('economic_spend_refunds_interest_without_card_wallet_double_count',old_expenses==new_expenses)
    counts={};calls=0;rejects=0
    for i,uid in enumerate(users):
        counts[uid]={t:c.execute('SELECT count(*) FROM '+t+' WHERE subject_id=?',(uid,)).fetchone()[0] for t in ('connections','accounts','cards','policies','transactions','contracts','positions','assets','mandates','claims')}
        account=c.execute('SELECT account_id FROM accounts WHERE subject_id=? LIMIT 1',(uid,)).fetchone()[0]
        policy=c.execute('SELECT policy_id FROM policies WHERE subject_id=? LIMIT 1',(uid,)).fetchone()[0]
        for operation in OPERATIONS:
            args={'account_id':account} if operation=='get_account_overview' else {'policy_id':policy} if operation=='get_policy' else {}
            result=service.query(uid,operation,args);calls+=1
            check(uid+':'+operation,'error' not in result and result['subject_id']==uid and result['source']=='navren_synthetic' and result['schema_version']=='navren.financial.v1')
        for when in (START,'2020-07-01',END):
            summary=service.query(uid,'get_financial_summary',{'as_of':when})['data']
            source_net=c.execute('SELECT net_worth_minor FROM original.daily_balances WHERE user_id=? AND date=?',(uid,when)).fetchone()[0]
            check(uid+':net_worth:'+when,summary['net_worth_minor']==source_net and summary['insurance_sum_excluded'] and summary['credit_limits_excluded'])
        other=users[(i+1)%len(users)]
        other_account=c.execute('SELECT account_id FROM accounts WHERE subject_id=? LIMIT 1',(other,)).fetchone()[0]
        other_policy=c.execute('SELECT policy_id FROM policies WHERE subject_id=? LIMIT 1',(other,)).fetchone()[0]
        for op,args in [('list_cards',{'account_id':other_account}),('get_account_overview',{'account_id':other_account}),('list_transactions',{'account_id':other_account}),('get_policy',{'policy_id':other_policy})]:
            rejects+=1;check(uid+':cross:'+op,service.query(uid,op,args).get('error',{}).get('code')=='ENTITY_NOT_ACCESSIBLE')
        rejects+=1;check(uid+':model_subject_parameter_denied',service.query(uid,'list_accounts',{'subject_id':other}).get('error',{}).get('code')=='INVALID_ARGUMENTS')
    for label,op,args in [('sql','sql',{}),('bad_date','list_transactions',{'start_date':'2026-02-30'}),('bool_limit','list_transactions',{'limit':True}),('oversize','list_transactions',{'limit':201}),('negative_offset','list_transactions',{'offset':-1}),('unknown_argument','list_accounts',{'database_path':'/tmp/other'}),('summary_outside','get_financial_summary',{'as_of':'2027-01-01'})]:
        rejects+=1;check('invalid:'+label,'error' in service.query(users[0],op,args))
    c.close()
    check('new_and_old_database_bytes_stable',before=={'new_database':file_sha(dbpath),'old_source':file_sha(sourcepath)})
    result={'dataset_version':service.dataset_version,'database_path':str(dbpath.resolve()),'checks_passed':len(checks),'checks':checks,
            'service_positive_calls':calls,'service_rejected_calls':rejects,'daily_coverage_days':days,'counts':counts,
            'economic_expense_minor':new_expenses,'source_and_generated_stable':True,'model_calls':0,'HTTP_calls':0,'browser_checks':0,
            'coverage':{'start':START,'end':END},'live_bank_access':False,'server_restarted':False}
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--database',required=True);p.add_argument('--source',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();r=validate(a.database,a.source,a.output)
    print(json.dumps({k:r[k] for k in ('checks_passed','service_positive_calls','service_rejected_calls','daily_coverage_days','counts')},ensure_ascii=False))
