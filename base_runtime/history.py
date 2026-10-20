"""封存历史投影到现有只读报告；不重演操作，不改运行现金。"""
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
import calendar
import json
import sqlite3
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
    service = demos.active_history(workspace)
    if service is not None:
        return _connected_rows(demos, workspace, service, period)
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
             'note':'历史入账记录。','operation_id':None,'mandate_id':None,
             'historical_import':True,
             'investment_principal_minor':r['amount_minor']-realized.get(r['event_id'],0) if r['kind']=='investment_redemption' else
                                          r['amount_minor'] if r['kind']=='investment_purchase' else None,
             'realized_return_minor':realized.get(r['event_id'],0) if r['kind']=='investment_redemption' else None,
             'principal_basis':'source_journal_cost_recovery' if r['kind']=='investment_redemption' else None} for r in records]


def _connected_rows(demos, workspace, service, period):
    """同一财务原件投影到查账合同；资金本金与消费分开。"""
    subject = demos.subject_for(workspace)
    with closing(demos.bank.connect()) as db:
        mapping = {r['source_account_id']: r['account_id'] for r in db.execute(
            'SELECT source_account_id,account_id FROM connected_account_links WHERE workspace_id=?', (workspace,))}
    with closing(service._connect()) as db:
        values, where = [subject], 't.subject_id=?'
        if period is not None:
            from demo_accounts import _period
            start, end = _period(period)
            where += ' AND t.booked_on BETWEEN ? AND ?'
            values += [start, end]
        records = [dict(r) for r in db.execute(
            'SELECT t.*,a.account_type FROM transactions t JOIN accounts a USING(subject_id,account_id) '
            'WHERE ' + where + ' ORDER BY t.occurred_at,t.transaction_id LIMIT 200001', values)]
    if len(records) > 200000:
        raise BankError('HISTORY_INCOMPLETE', '历史读取超过处理上限，请缩小期间。', 503)
    realized = {}
    if any(r['kind'] == 'investment_redemption' for r in records):
        record = demos._record(workspace)
        with closing(demos._history_connection(record)) as db:
            realized = {r[0]: r[1] for r in db.execute('''SELECT l.event_id,SUM(l.credit_minor-l.debit_minor)
                FROM journal_lines l JOIN ledger_accounts a ON a.user_id=l.user_id AND a.account_id=l.account_id
                WHERE l.user_id=? AND a.code='income:investment_realized' GROUP BY l.event_id''', (subject,))}
    result = []
    for raw in records:
        account = mapping.get(raw['account_id'])
        if account is None:
            raise BankError('CONNECTED_ACCOUNT_INCOMPLETE', '历史账户缺少对应的当前账户。', 503)
        details = json.loads(raw['details'])
        kind = {'internal_transfer': 'transfer', 'insurance_premium': 'expense'}.get(raw['kind'], raw['kind'])
        gain = None
        if kind == 'investment_redemption':
            gain = details.get('realized_return_minor', realized.get(raw['source_event_id']))
            if type(gain) is not int:
                raise BankError('STATEMENT_GAIN_UNAVAILABLE', '投资赎回缺少已实现损益依据，未把本金计为收入。', 503)
        result.append({**raw, 'id': raw['transaction_id'], 'workspace_id': workspace,
                       'account_id': account, 'source_account_id': raw['account_id'],
                       'kind': kind, 'original_kind': raw['kind'], 'merchant': raw['counterparty'],
                       'delta_minor': raw['signed_minor'] if raw['account_type'] != 'credit_card' else 0,
                       'note': '历史入账记录。', 'operation_id': None, 'historical_import': True,
                       'investment_principal_minor': raw['amount_minor'] - gain if gain is not None else
                            raw['amount_minor'] if kind == 'investment_purchase' else None,
                       'realized_return_minor': gain,
                       'principal_basis': 'source_journal_cost_recovery' if gain is not None else None})
    return result


def verified_runtime_redemption(bank, workspace, row):
    """仅依据已结算订单及批次本金确认运行赎回，不推算收益。"""
    if row.get('historical_import') or row.get('kind') != 'investment_redemption':
        return row
    with closing(bank.connect()) as db:
        order = db.execute('''SELECT o.*,coalesce(SUM(r.amount_minor),0) AS redeemed_principal
            FROM orders o LEFT JOIN lot_redemptions r ON r.workspace_id=o.workspace_id AND r.order_id=o.id
            WHERE o.workspace_id=? AND o.operation_id=? GROUP BY o.id''',
            (workspace, row.get('operation_id'))).fetchone()
    if (not order or order['side'] != 'redeem' or order['status'] not in ('succeeded', 'settled')
            or order['account_id'] != row['account_id'] or order['amount_minor'] != row['amount_minor']
            or order['redeemed_principal'] != row['amount_minor'] or row['delta_minor'] != row['amount_minor']):
        raise BankError('STATEMENT_GAIN_UNAVAILABLE', '赎回记录缺少已结算本金依据，未将其计为收入。', 503)
    return {**row, 'investment_principal_minor': order['redeemed_principal'],
            'realized_return_minor': 0, 'principal_basis': 'settled_order_and_redeemed_lots'}


def _connected_analysis(bank, workspace, period, service):
    demos = bank.demo_accounts
    historical = _connected_rows(demos, workspace, service, period)
    with closing(bank.connect()) as db:
        ws = bank._workspace(db, workspace)
        live = [dict(r) for r in db.execute('''SELECT t.*,a.currency FROM transactions t
            JOIN accounts a ON a.workspace_id=t.workspace_id AND a.id=t.account_id
            WHERE t.workspace_id=? AND substr(t.occurred_at,1,?)=? ORDER BY t.occurred_at,t.id''',
            (workspace, len(period), period))]
    live = [verified_runtime_redemption(bank, workspace, r) for r in live]
    records = sorted(historical + live, key=lambda r: (r['occurred_at'], r['id']))
    totals = defaultdict(int)
    expense_categories, category_ids = defaultdict(int), defaultdict(list)
    incomes, codes = defaultdict(int), defaultdict(list)
    for row in records:
        kind, amount = row['kind'], row['amount_minor']
        expense = row.get('expense_minor', amount if kind == 'expense' else -amount if kind == 'refund' else 0)
        if expense > 0:
            totals['expense'] += expense
            expense_categories[row['category']] += expense
            category_ids[row['category']].append(row['id'])
        elif expense < 0:
            totals['refund'] -= expense
        if kind == 'income':
            totals['income'] += amount
            incomes[row['category']] += amount
            codes[row['category']] = ['income:transaction']
        elif kind == 'investment_redemption':
            gain = row.get('realized_return_minor')
            if type(gain) is not int:
                raise BankError('STATEMENT_GAIN_UNAVAILABLE', '赎回记录缺少已实现损益依据。', 503)
            totals['income'] += gain
            incomes['已实现投资损益'] += gain
            codes['已实现投资损益'] = ['income:investment_realized']
            totals[kind] += amount
        elif kind == 'transfer':
            # 同一本人调拨的借贷两侧只累计转出金额一次。
            if row.get('signed_minor', row['delta_minor']) < 0:
                totals['transfer'] += amount
        elif kind not in ('expense', 'refund'):
            totals[kind] += amount
    for key in ('income', 'expense', 'refund', 'transfer', 'investment_purchase', 'investment_redemption'):
        totals.setdefault(key, 0)
    annual = len(period) == 4
    requested_start = period + ('-01-01' if annual else '-01')
    requested_end = period + '-12-31' if annual else period + '-' + str(calendar.monthrange(*map(int, period.split('-')))[1])
    start = max(requested_start, service.metadata['start_date'])
    end = min(requested_end, service.metadata['end_date'])
    covered = start <= end
    partial = not covered or start != requested_start or end != requested_end
    historical_income = sum(r['amount_minor'] if r['kind'] == 'income' else
                            r['realized_return_minor'] if r['kind'] == 'investment_redemption' else 0
                            for r in historical)
    historical_expense = sum(max(0, r['expense_minor']) for r in historical)
    historical_refund = sum(max(0, -r['expense_minor']) for r in historical)
    summary = {'period_start': start, 'period_end': end, 'partial_period': partial,
               'income_minor': historical_income, 'expense_minor': historical_expense - historical_refund,
               'refund_minor': historical_refund} if covered else None
    runtime_range = [live[0]['occurred_at'], live[-1]['occurred_at']] if live else None
    coverage_start = min(([start] if covered else []) + [r['occurred_at'][:10] for r in live]) if covered or live else None
    coverage_end = max(([end] if covered else []) + [r['occurred_at'][:10] for r in live]) if covered or live else None
    result = {'environment': 'simulation', 'source': 'simulation', 'data_scope': 'navren_synthetic',
              'source_version': service.dataset_version, 'workspace_id': workspace, 'as_of': ws['as_of'],
              'totals_minor': dict(totals), 'income_expense_surplus_minor': totals['income'] - totals['expense'] + totals['refund'],
              'expense_basis': '已入账消费与利息费用；贷款本金、投资本金及本人调拨分别列示。',
              'journal_net_expense_minor': totals['expense'] - totals['refund'],
              'cash_expense_minor': sum(r['amount_minor'] for r in records if r['kind'] == 'expense' and r.get('account_type') != 'credit_card'),
              'category_expense_basis': '已入账费用，含信用卡消费与贷款利息；信用卡还款不重复计消费。',
              'category_expense_minor': dict(expense_categories), 'category_transaction_ids': dict(category_ids),
              'category_income_minor': dict(incomes), 'income_category_codes': dict(codes),
              'income_category_basis': '入账收入加已实现投资损益；投资赎回本金、未实现估值及本人调拨不计收入。',
              'income_categories_complete': True, 'currency': 'CNY',
              'transaction_count': len(records), 'archived_count': len(historical), 'live_count': len(live),
              'transaction_ids': [r['id'] for r in records], 'cash_delta_minor': sum(r['delta_minor'] for r in records),
              'coverage_start': coverage_start, 'coverage_end': coverage_end,
              'historical_coverage_start': start if covered else None, 'historical_coverage_end': end if covered else None,
              'runtime_recorded_range': runtime_range,
              'partial_period': partial, 'source_summaries': [summary] if summary else [],
              'notice': '历史入账和当前操作分别保留；贷款及投资本金、本人调拨不计收入消费。'}
    if annual:
        months = set()
        if covered:
            cursor = date.fromisoformat(start)
            while cursor.isoformat() <= end:
                months.add(cursor.strftime('%Y-%m'))
                cursor += timedelta(days=1)
        result.update(year=int(period), complete_calendar_year=covered and not partial, covered_months=sorted(months))
    else:
        result.update(month=period, recorded_range=[records[0]['occurred_at'], records[-1]['occurred_at']] if records else None)
    return result


def income_categories(demos, workspace, period, live, expected_income):
    """同主体、同期间的已确认收入分录；与月/年报收入逐分对账。"""
    record = demos._record(workspace)
    categories, codes = defaultdict(int), defaultdict(list)
    try:
        with closing(demos._history_connection(record)) as db:
            metadata = dict(db.execute('SELECT key,value FROM metadata'))
            if metadata.get('currency') != 'CNY' or metadata.get('amount_unit') != 'integer_cent':
                raise BankError('STATEMENT_CURRENCY_UNSUPPORTED', '历史收入币种或金额单位无法核对，未合并分类。', 503)
            grouped = db.execute('''SELECT a.code,a.name,SUM(l.credit_minor-l.debit_minor) AS income_minor
                FROM journal_lines l
                JOIN ledger_accounts a ON a.user_id=l.user_id AND a.account_id=l.account_id
                JOIN journal_events e ON e.user_id=l.user_id AND e.event_id=l.event_id
                WHERE l.user_id=? AND substr(e.occurred_at,1,?)=?
                  AND e.event_type<>'opening' AND a.account_class='income'
                  AND a.code NOT LIKE 'income:unrealized%'
                GROUP BY a.code,a.name ORDER BY a.code''', (record['user_id'], len(period), period))
            for row in grouped:
                if not row['name'] or not row['code'] or type(row['income_minor']) is not int:
                    raise BankError('STATEMENT_INCOME_CATEGORY_INVALID', '收入分类原件字段无法核对。', 503)
                categories[row['name']] += row['income_minor']
                codes[row['name']].append(row['code'])
    except sqlite3.Error as error:
        raise BankError('STATEMENT_INCOME_CATEGORY_UNAVAILABLE', '同源收入分录读取失败，未生成分类。', 503) from error
    for row in live:
        if row.get('currency') != 'CNY':
            raise BankError('STATEMENT_CURRENCY_UNSUPPORTED', '运行流水币种不明或与历史不同，未合并账单。', 503)
        if row['kind'] != 'income':
            continue
        if not row.get('category') or type(row.get('amount_minor')) is not int:
            raise BankError('STATEMENT_INCOME_CATEGORY_INVALID', '运行收入分类字段无法核对。', 503)
        name = row['category']
        categories[name] += row['amount_minor']
        if 'runtime:income' not in codes[name]:
            codes[name].append('runtime:income')
    if type(expected_income) is not int or sum(categories.values()) != expected_income:
        raise BankError('STATEMENT_INCOME_CATEGORY_MISMATCH', '收入分类与同期间账单收入总计不一致，已停止展示分类。', 503)
    return {'category_income_minor': dict(categories), 'income_category_codes': dict(codes),
            'income_category_basis': '同主体同期间已确认收入科目净贷方，加运行收入流水；含已实现投资损益，不含投资本金及未实现估值。',
            'income_categories_complete': True, 'currency': 'CNY'}


def analysis(bank, workspace, period):
    demos=bank.demo_accounts
    service = demos.active_history(workspace)
    if service is not None:
        return _connected_analysis(bank, workspace, period, service)
    annual=len(period)==4
    summaries=rows(demos,workspace,'annual_summaries' if annual else 'monthly_summaries',period)
    historical=cash_rows(demos,workspace,period)
    with closing(bank.connect()) as db:
        ws=bank._workspace(db,workspace)
        live=[dict(r) for r in db.execute('''SELECT t.*,a.currency AS currency FROM transactions t
            LEFT JOIN accounts a ON a.workspace_id=t.workspace_id AND a.id=t.account_id
            WHERE t.workspace_id=? AND substr(t.occurred_at,1,?)=? ORDER BY t.occurred_at,t.id''',
                                        (workspace,len(period),period))]
    totals=defaultdict(int)
    category=defaultdict(int);category_ids=defaultdict(list)
    for row in historical+live:
        totals[row['kind']]+=row['amount_minor']
        if row['kind']=='expense':
            category[row['category']]+=row['amount_minor'];category_ids[row['category']].append(row['id'])
    income=sum(r['income_minor'] for r in summaries)+sum(r['amount_minor'] for r in live if r['kind']=='income')
    income_classification=income_categories(demos,workspace,period,live,income)
    # 源expense为已扣退款且排除折旧的分录消费；退款只抵扣一次。
    source_net_expense=sum(r['expense_minor'] for r in summaries)
    expenses=source_net_expense+sum(r['refund_minor'] for r in summaries)+sum(r['amount_minor'] for r in live if r['kind']=='expense')
    refunds=sum(r['refund_minor'] for r in summaries)+sum(r['amount_minor'] for r in live if r['kind']=='refund')
    totals.update(income=income,expense=expenses,refund=refunds)
    for key in ('transfer','investment_purchase','investment_redemption'):totals.setdefault(key,0)
    result={**income_classification,'environment':'simulation','source':'simulation','data_scope':'navren_synthetic',
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
