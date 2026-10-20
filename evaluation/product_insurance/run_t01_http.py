"""Independent fixed-money T01 through welcome HTTP and separate server PIDs.

Only the parent seeds its own stopped temporary bank fixture. No HTTP balance
override, real person, provider SMS, model, or production database is involved.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
import sqlite3
import sys
import traceback
import uuid

from evaluation.product_insurance.run_joint_matrix import Joint, Client, OUT, ROOT, digest
from test_sms_auth import TestTransport, OTHER_PHONE
from test_sms_permissions import CODE


def seed_stopped_fixture(j, workspace):
    assert j.proc is None and j.state.name.startswith('navren-pi-joint-')
    with sqlite3.connect(j.state / 'bank.sqlite3') as db:
        account = db.execute('SELECT id FROM accounts WHERE workspace_id=? AND id LIKE ?',
            (workspace, '%-main')).fetchone()[0]
        db.execute('UPDATE accounts SET opening_balance_minor=0,balance_minor=0 WHERE workspace_id=?', (workspace,))
        db.execute('UPDATE accounts SET opening_balance_minor=2500000,balance_minor=2500000 WHERE workspace_id=? AND id=?', (workspace, account))
        db.execute('UPDATE profiles SET reserve_minor=500000 WHERE workspace_id=?', (workspace,))
        for sql in (
            "UPDATE loan_installments SET status='paid',paid_minor=principal_minor+interest_minor+fees_minor WHERE workspace_id=?",
            "UPDATE credit_statements SET status='paid',paid_minor=total_due_minor WHERE workspace_id=?",
            "UPDATE subscriptions SET merchant_status='cancelled' WHERE workspace_id=?",
            "UPDATE mandates SET status='closed' WHERE workspace_id=?",
            "UPDATE scheduled_payments SET status='cancelled' WHERE workspace_id=?",
            "UPDATE fund_holds SET status='released' WHERE workspace_id=?"):
            db.execute(sql, (workspace,))
        assert db.execute("SELECT count(*) FROM navren_remittances WHERE workspace_id=? AND status='scheduled'", (workspace,)).fetchone()[0] == 0
        for label, amount, due in [('rent',350000,'2026-10-13'),('bill',50000,'2026-10-18')]:
            db.execute('INSERT INTO mandates VALUES(?,?,?,?,?,?,?,?)',
                ('root-t01-'+label,workspace,account,label,amount,'active',due,'active'))
        db.execute('UPDATE workspaces SET version=version+1 WHERE id=?', (workspace,))
    return account


def main():
    j=Joint()
    def pins():
        # This runner never fetches or executes the UI; its active owner may
        # change those files without changing this HTTP acceptance input.
        return {n:digest((ROOT/n).read_bytes()) for n in ('research_workspace.py','research_evidence.py',
            'research_methods.py','finance_gateway.py','welcome_auth.py','webank_catalog.py',
            'financial_methods/funding.py','simulated_bank/service.py','simulated_bank/permission_policy.py')}
    j.source_pins=pins;j.source_before=pins()
    report={'schema':'navren.pi.t01-http/1','at':datetime.now(timezone.utc).isoformat(),
        'scope':'fixed synthetic T01 trusted temporary fixture via real welcome HTTP',
        'checks':[], 'models':0,'real_sms':0,'production_touched':False,
        'full_model_journey_accepted':False, 'source_before':j.source_before}
    def check(ok,label,value=None):
        assert ok,label
        report['checks'].append({'check':label,'evidence':value})
    def calc(policy):
        return j.post('calculate',{'method':'reserve_obligations','account_id':account,
            'until':'2026-11-01','policy_record_ids':[policy]})['record']
    def fields(record):return record['value']['result']['calculations']
    try:
        j.setup();workspace=j.a.finance['workspace_id']
        owner=j.a.call('/api/welcome/auth/session')['registered_user_id']
        j.stop();account=seed_stopped_fixture(j,workspace)
        j.start();j.a=Client(j.port);j.a.login(persona='tycoon')
        check(j.a.finance['workspace_id']==workspace,'same registered private instance after fixture server replacement')
        policy=j.policy(); pid=policy['id']
        quote=calc(pid);before=fields(quote)
        check(before['required_reserve_minor']==1080000 and before['already_protected_minor']==900000
            and before['additional_reserve_minor']==180000 and before['uncommitted_cash_minor']==1420000,
            'total10800 already9000 new1800 available14200',before)
        task=j.task('本次只新增预留1800元保费','reserve',[quote['id']],{
            'amount':'1800.00','currency':'CNY','account_id':account,'obligation_ids':['policy:'+pid]})
        prepared=j.post('prepare',{'record_id':task['id'],'action':'reserve_funds','account_id':account,
            'amount':'1800.00','expires_on':'2026-11-01','idempotency_key':uuid.uuid4().hex,
            'execution_choice':'reserve_and_keep_cash'})
        op=prepared['operation'];path='/api/finance/operations/'+op['id']
        check(op['state']=='pending_confirmation' and op['payload']['amount_minor']==180000,
            'original financial operation awaits explicit confirmation and MFA',{'id':op['id'],'permission':op['permission']})
        budget=j.a.call('/api/finance/budget?account_id='+account)
        check(budget['held_minor']==0 and budget['investable_minor']==1600000,'prepare never freezes money',budget)
        j.a.call(path+'/confirm',{'confirmed':True,'confirmation_digest':op['confirmation_digest']},expected=403)
        challenge=j.a.call(path+'/sms',{})
        confirmed=j.a.call(path+'/confirm',{'confirmed':True,'confirmation_digest':op['confirmation_digest'],
            'challenge_id':challenge['challenge_id'],'verification_code':CODE})['operation']
        check(confirmed['state']=='succeeded','original confirmation succeeds with explicit fixture SMS',confirmed['result'])
        receipt=j.a.call('/api/finance/research/operations/'+op['id'])
        hold_id=confirmed['result']['hold_id'];after=fields(calc(pid))
        check(after['required_reserve_minor']==1080000 and after['already_protected_minor']==1080000
            and after['additional_reserve_minor']==0 and after['uncommitted_cash_minor']==1420000,
            'same total and available after actual hold; no double count',after)
        budget=j.a.call('/api/finance/budget?account_id='+account)
        check(budget['cash_minor']==2500000 and budget['held_minor']==180000 and budget['investable_minor']==1420000,
            'original bank budget agrees exactly; reservation does not debit balance',budget)
        replay=j.a.call(path+'/confirm',{'confirmed':True,'confirmation_digest':op['confirmation_digest']})['operation']
        check(replay['result']==confirmed['result'],'idempotent confirmation returns the original hold')
        record=j.a.call('/api/finance/research/records/'+task['id'])['record']
        check(any(x.get('funding_allocations') and x['funding_allocations'][0]['policy_record_id']==pid
            for x in record['operation_links']),'task readback contains explicit policy allocation',record['operation_links'])
        j.stop();j.start();fresh=Client(j.port);fresh.login(persona='tycoon');j.a=fresh
        check(fresh.finance['workspace_id']==workspace and fresh.call('/api/welcome/auth/session')['registered_user_id']==owner,
            'fresh browser/password login restores original owner and role after separate PID replacement')
        check(fresh.call('/api/finance/research/operations/'+op['id'])['operation']['result']==confirmed['result'],
            'same operation receipt and hold persist after process replacement')
        recovered=fields(calc(pid));check(recovered==after,'persisted allocation preserves exact reconciled amounts after restart',recovered)
        holds=[h for h in fresh.call('/api/finance/overview')['fund_holds'] if h['id']==hold_id]
        check(len(holds)==1 and holds[0]['amount_minor']==180000,'exactly one actual hold survives restart')
        other=Client(j.port);other.register(TestTransport(),phone=OTHER_PHONE)
        other.call('/api/welcome/auth/experience',{'user_id':'tycoon'});other.finance=other.call('/api/finance/session')
        check(other.finance['workspace_id']!=workspace,'other registered owner has separate role instance')
        other.call('/api/finance/research/records/'+task['id'],expected=404)
        other.call('/api/finance/research/operations/'+op['id'],expected=404)
        check(True,'other owner cannot read original task or operation')
        report['status']='PASS_SCOPED_FIXED_T01_HTTP_AND_RECOVERY'
    except Exception:
        report.update(status='FAIL',error=traceback.format_exc(limit=6))
    finally:
        j.stop();report['service_processes']=j.processes;report['source_after']=j.source_pins()
        report['source_stable']=report['source_before']==report['source_after']
        if not report['source_stable']:report['status']='FAIL_SOURCE_DRIFT'
        filename='t01-http-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.json'
        path=OUT/filename;path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        latest=OUT/'t01-http-latest.json';tmp=latest.with_suffix('.tmp')
        tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');tmp.replace(latest)
        print(json.dumps({'status':report['status'],'checks':len(report['checks']),'receipt':str(path),
            'source_stable':report['source_stable'],'error':report.get('error')},ensure_ascii=False))
        j.temp.cleanup()
    return 0 if report['status'].startswith('PASS_') else 1


if __name__=='__main__':sys.exit(main())
