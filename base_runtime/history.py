"""封存历史投影到现有只读报告；不重演操作，不改运行现金。"""
from collections import defaultdict
from contextlib import closing
from simulated_bank import BankError


def rows(demos, workspace, table, period=None):
    output, offset = [], 0
    while True:
        page=demos.history(workspace,table,period,limit=500,offset=offset)
        output.extend(page['rows'])
        if not page['has_more']:return output
        offset+=len(page['rows'])
        if offset>200000 or not page['rows']:
            raise BankError('HISTORY_INCOMPLETE','历史分页未能完整读取。',503)


def cash_rows(demos, workspace, period=None):
    mapping=demos.current(workspace)['bank_state']['account_mapping']
    records=rows(demos,workspace,'cash_transactions',period)
    realized={}
    if any(r['kind']=='investment_redemption' for r in records):
        record=demos._record(workspace)
        with closing(demos._history_connection(record)) as db:
            realized={r[0]:r[1] for r in db.execute('''SELECT l.event_id,SUM(l.credit_minor-l.debit_minor)
                FROM journal_lines l JOIN ledger_accounts a ON a.user_id=l.user_id AND a.account_id=l.account_id
                WHERE l.user_id=? AND a.code='income:investment_realized' GROUP BY l.event_id''',(record['user_id'],))}
    return [{**r,'id':r['transaction_id'],'workspace_id':workspace,
             'account_id':mapping[r['account_id']],
             'source_account_id':r['account_id'],'delta_minor':r['signed_minor'],
             'note':'合成历史原件；不是本次银行操作。','operation_id':None,'mandate_id':None,
             'historical_import':True,
             'investment_principal_minor':r['amount_minor']-realized.get(r['event_id'],0) if r['kind']=='investment_redemption' else
                                          r['amount_minor'] if r['kind']=='investment_purchase' else None,
             'realized_return_minor':realized.get(r['event_id'],0) if r['kind']=='investment_redemption' else None,
             'principal_basis':'source_journal_cost_recovery' if r['kind']=='investment_redemption' else None} for r in records]


def analysis(bank, workspace, period):
    demos=bank.demo_accounts
    annual=len(period)==4
    summaries=rows(demos,workspace,'annual_summaries' if annual else 'monthly_summaries',period)
    historical=cash_rows(demos,workspace,period)
    with closing(bank.connect()) as db:
        ws=bank._workspace(db,workspace)
        live=[dict(r) for r in db.execute('SELECT * FROM transactions WHERE workspace_id=? AND substr(occurred_at,1,?)=? ORDER BY occurred_at,id',
                                        (workspace,len(period),period))]
    totals=defaultdict(int)
    category=defaultdict(int);category_ids=defaultdict(list)
    for row in historical+live:
        totals[row['kind']]+=row['amount_minor']
        if row['kind']=='expense':
            category[row['category']]+=row['amount_minor'];category_ids[row['category']].append(row['id'])
    income=sum(r['income_minor'] for r in summaries)+sum(r['amount_minor'] for r in live if r['kind']=='income')
    # 源expense为已扣退款且排除折旧的分录消费；退款只抵扣一次。
    source_net_expense=sum(r['expense_minor'] for r in summaries)
    expenses=source_net_expense+sum(r['refund_minor'] for r in summaries)+sum(r['amount_minor'] for r in live if r['kind']=='expense')
    refunds=sum(r['refund_minor'] for r in summaries)+sum(r['amount_minor'] for r in live if r['kind']=='refund')
    totals.update(income=income,expense=expenses,refund=refunds)
    for key in ('transfer','investment_purchase','investment_redemption'):totals.setdefault(key,0)
    result={'environment':'simulation','source':'simulation','data_scope':'navren_synthetic',
            'source_version':demos.current(workspace)['source_version'],'workspace_id':workspace,'as_of':ws['as_of'],
            'totals_minor':dict(totals),'income_expense_surplus_minor':income-expenses+refunds,
            'expense_basis':'排除折旧的分录毛消费；退款在refund单列且只抵扣一次。',
            'journal_net_expense_minor':expenses-refunds,
            'cash_expense_minor':sum(r['amount_minor'] for r in historical+live if r['kind']=='expense'),
            'category_expense_basis':'仅现金expense流水；不包含贷款本金及折旧，与分录毛消费分别列示。',
            'category_expense_minor':dict(category),'category_transaction_ids':dict(category_ids),
            'transaction_count':len(historical)+len(live),'archived_count':len(historical),'live_count':len(live),
            'transaction_ids':[r['id'] for r in historical+live],
            'cash_delta_minor':sum(r['delta_minor'] for r in historical+live),
            'coverage_start':summaries[0]['period_start'] if summaries else None,
            'coverage_end':summaries[-1]['period_end'] if summaries else None,
            'partial_period':not summaries or any(r['partial_period'] for r in summaries),
            'source_summaries':summaries,
            'notice':'合成历史与运行新流水分别列出；借贷/投资本金及自转账不是收入消费；封存历史导入不改变运行余额。'}
    if annual:
        result.update(year=int(period),complete_calendar_year=bool(summaries and not result['partial_period']),
                      covered_months=sorted({r['occurred_at'][:7] for r in historical+live}))
    else:
        result.update(month=period,recorded_range=[(historical+live)[0]['occurred_at'],(historical+live)[-1]['occurred_at']] if historical or live else None)
    return result
