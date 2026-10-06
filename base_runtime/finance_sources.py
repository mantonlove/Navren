"""封存合成日记账的现金期间来源；运行现金和历史原件从不拼接。"""
from contextlib import closing
from datetime import date, timedelta


def synthetic_cash_snapshot(demos, workspace, args):
    if (args.get('source') != 'navren_synthetic' or args.get('scope') != workspace
            or args.get('as_of') is not None or not demos.is_demo(workspace)):
        return None
    start, end = args['period_start'], args['period_end']
    record = demos._record(workspace)
    uid = record['user_id']
    refs = {key:'synthetic_history/'+uid+'/'+key for key in
            ('journal_cash_catalog','cash_transactions','opening_cash','closing_cash','refund_originals','refund_cumulative')}
    version = record['history_sha256']
    value = {'subject_id':workspace,'currency':'CNY','data_scope':'navren_synthetic',
             'as_of':None,'period_start':start,'period_end':end,'coverage_complete':False,
             'input_refs':list(refs.values()),'source_versions':{r:version for r in refs.values()},
             'provenance':{'coverage':[refs['journal_cash_catalog']], 'movements':[refs['cash_transactions']],
                 'opening_cash':[refs['opening_cash']], 'closing_cash':[refs['closing_cash']],
                 'refund_originals':[refs['refund_originals']], 'refund_cumulative':[refs['refund_cumulative']]},
             'catalog':{'complete':False,'coverage_ref':refs['journal_cash_catalog'],
                        'expected_account_ids':[], 'expected_movement_ids':[],
                        'refunds_complete':False,'expected_refund_ids':[]},
             'records':{'movements':[],'refund_originals':[],'refund_cumulative_minor':{},
                        'opening_cash_minor':None,'closing_cash_minor':None}}
    gaps = []
    if not '2016-10-04' <= start <= end <= '2026-10-03':
        value['records']['source_gaps']=['period_outside_sealed_source']
        return value
    with closing(demos._history_connection(record)) as db:
        db.execute('BEGIN')
        accounts = [dict(r) for r in db.execute("SELECT * FROM ledger_accounts WHERE user_id=? AND account_class='cash' ORDER BY account_id",(uid,))]
        account_ids = [r['account_id'] for r in accounts]
        value['catalog']['expected_account_ids']=account_ids
        prior = (date.fromisoformat(start)-timedelta(days=1)).isoformat()
        opening = db.execute('SELECT cash_minor FROM daily_balances WHERE user_id=? AND date=?',(uid,prior)).fetchone()
        closing_cash = db.execute('SELECT cash_minor FROM daily_balances WHERE user_id=? AND date=?',(uid,end)).fetchone()
        value['records']['opening_cash_minor'] = (sum(a['opening_balance_minor'] for a in accounts)
            if start == '2016-10-04' else opening['cash_minor'] if opening else None)
        value['records']['closing_cash_minor'] = closing_cash['cash_minor'] if closing_cash else None
        if value['records']['opening_cash_minor'] is None or closing_cash is None:
            gaps.append('cash_boundary_snapshot_missing')
        # 独立覆盖目录读取原日记账现金腿。opening分录只证明期初存量。
        legs = [dict(r) for r in db.execute('''SELECT l.*,e.occurred_at,e.event_type,e.reference_id
            FROM journal_lines l JOIN ledger_accounts a ON a.user_id=l.user_id AND a.account_id=l.account_id
            JOIN journal_events e ON e.user_id=l.user_id AND e.event_id=l.event_id
            WHERE l.user_id=? AND a.account_class='cash' AND e.event_type!='opening'
            AND substr(e.occurred_at,1,10) BETWEEN ? AND ? ORDER BY e.occurred_at,l.line_id''',(uid,start,end))]
        raw = [dict(r) for r in db.execute('''SELECT * FROM cash_transactions WHERE user_id=?
            AND substr(occurred_at,1,10) BETWEEN ? AND ? ORDER BY occurred_at,transaction_id''',(uid,start,end))]
        index = {}
        for row in raw:
            key=(row['event_id'],row['account_id'],row['signed_minor'])
            index.setdefault(key,[]).append(row)
        matched, expected, movements = set(), [], []

        def movement(leg, kind, amount, suffix='', related=None):
            return {'movement_id':leg['line_id']+suffix,'subject_id':workspace,'account_id':leg['account_id'],
                    'currency':'CNY','on':leg['occurred_at'][:10],'kind':kind,'delta_minor':amount,
                    'evidence_ref':refs['cash_transactions'],'source_version':version,'related_id':related}

        for leg in legs:
            delta=leg['debit_minor']-leg['credit_minor']
            expected.extend([leg['line_id']+'/principal',leg['line_id']+'/interest'] if leg['event_type']=='loan_repayment' else [leg['line_id']])
            rows=index.get((leg['event_id'],leg['account_id'],delta),[])
            if len(rows)!=1 or rows[0]['transaction_id'] in matched:
                gaps.append('journal_cash_leg_not_uniquely_matched:'+leg['line_id']);continue
            row=rows[0];matched.add(row['transaction_id'])
            if row['occurred_at'] != leg['occurred_at'] or row['account_id'] not in account_ids:
                gaps.append('cash_leg_identity_or_time_mismatch:'+leg['line_id']);continue
            if row['kind']=='loan_repayment':
                counterparts=[dict(r) for r in db.execute('''SELECT l.*,a.code FROM journal_lines l
                    JOIN ledger_accounts a ON a.user_id=l.user_id AND a.account_id=l.account_id
                    WHERE l.user_id=? AND l.event_id=?''',(uid,leg['event_id']))]
                principal=sum(r['debit_minor']-r['credit_minor'] for r in counterparts if r['code'].startswith('liability:'))
                interest=sum(r['debit_minor']-r['credit_minor'] for r in counterparts if r['code'].startswith('expense:贷款利息:'))
                if leg['event_type']!='loan_repayment' or principal<0 or interest<0 or delta!=-principal-interest:
                    gaps.append('loan_principal_interest_reconciliation_failed:'+leg['line_id']);continue
                movements += [movement(leg,'loan_principal_repayment',-principal,'/principal'),
                              movement(leg,'loan_interest',-interest,'/interest')]
            else:
                related=row['transfer_group_id'] if row['kind']=='internal_transfer' else None
                if row['kind']=='refund':
                    original_rows=[dict(r) for r in db.execute('''SELECT c.*,l.line_id
                        FROM cash_transactions c JOIN journal_lines l
                        ON l.user_id=c.user_id AND l.event_id=c.event_id AND l.account_id=c.account_id
                        AND l.debit_minor-l.credit_minor=c.signed_minor
                        WHERE c.user_id=? AND c.event_id=? AND c.kind='expense' ''',(uid,leg['reference_id']))]
                    if len(original_rows)!=1:
                        gaps.append('refund_original_not_unique:'+leg['line_id'])
                    else:
                        original=original_rows[0];related=original['line_id']
                        value['records']['refund_originals'].append({'movement_id':related,'subject_id':workspace,
                            'account_id':original['account_id'],'currency':'CNY','on':original['occurred_at'][:10],
                            'kind':'expense','delta_minor':original['signed_minor'],
                            'evidence_ref':refs['refund_originals'],'source_version':version})
                        total=db.execute('''SELECT COALESCE(SUM(c.signed_minor),0) FROM cash_transactions c
                            JOIN journal_events e ON e.user_id=c.user_id AND e.event_id=c.event_id
                            WHERE c.user_id=? AND c.kind='refund' AND e.reference_id=?
                            AND substr(c.occurred_at,1,10)<=?''',(uid,leg['reference_id'],end)).fetchone()[0]
                        value['records']['refund_cumulative_minor'][related]=total
                movements.append(movement(leg,row['kind'],delta,related=related))
        if len(matched)!=len(raw):
            gaps.append('cash_transaction_without_independent_journal_leg')
        # 同一原单可在期内有多笔退款，原件只保留一次；摘要固定整份封存来源。
        originals=value['records']['refund_originals']
        value['records']['refund_originals']=list({r['movement_id']:r for r in originals}.values())
        value['records']['movements']=movements
        value['records']['source_gaps']=gaps
        value['catalog']['expected_movement_ids']=expected
        value['catalog']['expected_refund_ids']=[l['line_id'] for l in legs if l['event_type']=='refund']
        value['catalog']['refunds_complete']=not gaps
        value['catalog']['complete']=not gaps
        value['coverage_complete']=not gaps
    return value
