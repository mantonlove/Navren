"""可复算的模拟账户现金流时间轴；只读银行档案，不预测未确认收入或生活支出。"""
from calendar import monthrange
from datetime import date, timedelta


MAX_HORIZON_DAYS = 365


def validate_horizon_days(value):
    if type(value) is not int or not 1 <= value <= MAX_HORIZON_DAYS:
        raise ValueError(f'时间范围必须为1至{MAX_HORIZON_DAYS}天的整数。')
    return value


def _date(value, field):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f'{field}不是有效日期。')
    if parsed.isoformat() != value:
        raise ValueError(f'{field}不是标准日期。')
    return parsed


def _minor(value, field):
    if type(value) is not int or value < 0:
        raise ValueError(f'{field}必须是非负整数分。')
    return value


def _cycle_date(anchor, months):
    month_index = anchor.year * 12 + anchor.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(anchor.day, monthrange(year, month)[1]))


def _recurring_occurrences(anchor, as_of, end_on, cycle_months):
    """Yield each in-range cycle once, plus the latest missed cycle as catch-up."""
    if anchor < as_of:
        month_delta = (as_of.year - anchor.year) * 12 + as_of.month - anchor.month
        index = max(0, month_delta // cycle_months)
        while index > 0 and _cycle_date(anchor, index * cycle_months) >= as_of:
            index -= 1
        while _cycle_date(anchor, (index + 1) * cycle_months) < as_of:
            index += 1
        missed = _cycle_date(anchor, index * cycle_months)
        yield missed, True
        index += 1
    else:
        index = 0
    while True:
        due = _cycle_date(anchor, index * cycle_months)
        if due > end_on:
            break
        yield due, False
        index += 1


def _event(*, event_id, due_on, as_of, end_on, account_id, amount_minor,
           cash_delta_minor, kind, certainty, title, source_table, evidence_id,
           currency, status='scheduled', reserved_delta_minor=0):
    due = _date(due_on, '到期日期')
    if due > end_on:
        return None
    effective = max(due, as_of)
    amount = _minor(amount_minor, '金额')
    if type(cash_delta_minor) is not int or type(reserved_delta_minor) is not int:
        raise ValueError('现金或预留变动必须是整数分。')
    overdue = due < as_of and status == 'scheduled'
    return {
        'id': event_id,
        'date': effective.isoformat(),
        'scheduled_date': due.isoformat(),
        'date_status': 'overdue' if overdue else status,
        'account_id': account_id,
        'currency': currency,
        'amount_minor': amount,
        'cash_delta_minor': cash_delta_minor,
        'reserved_delta_minor': reserved_delta_minor,
        'kind': kind,
        'certainty': certainty,
        'title': title,
        'source': 'simulated_bank.customer_archive',
        'source_table': source_table,
        'evidence_id': evidence_id,
        'evidence_ref': f'simulated_bank.customer_archive:{source_table}/{evidence_id}',
    }


def demo_reserve_floors(accounts, reserve_minor):
    """演示画像的全户应急底线只分配一次，优先保留于原储备账户。"""
    floors={a['id']:0 for a in accounts};remaining=reserve_minor
    ordered=sorted(accounts,key=lambda a:(not a['id'].endswith('-reserve'),not a['id'].endswith('-main'),a['id']))
    for account in ordered:
        floor=min(remaining,account['balance_minor']);remaining-=floor;floors[account['id']]=floor
    if remaining and ordered:floors[ordered[-1]['id']]+=remaining
    return floors


def build_cashflow_timeline(archive, horizon_days=90, workspace_id=None):
    """Build a day-granularity projection from one workspace-scoped bank archive.

    Confirmed amounts and periodic candidates are calculated separately. Daily
    aggregation avoids claiming an intraday ordering that the source data lacks.
    """
    validate_horizon_days(horizon_days)
    as_of = _date(archive.get('as_of'), '模拟业务日期')
    end_on = as_of + timedelta(days=horizon_days)
    records = archive['records']
    accounts = sorted(archive['accounts'], key=lambda row: row['id'])
    account_by_id = {row['id']: row for row in accounts}
    currencies = {account['currency'] for account in accounts}
    profile = archive['profile']
    if len(account_by_id) != len(accounts):
        raise ValueError('账户标识重复，无法构建时间轴。')

    cny_main_accounts = [account['id'] for account in accounts
                         if account['id'].endswith('-main') and account['currency'] == 'CNY']
    if len(cny_main_accounts) == 1:
        reserve_account_id = cny_main_accounts[0]
    elif len(accounts) == 1 and accounts[0]['currency'] == 'CNY':
        reserve_account_id = accounts[0]['id']
    else:
        reserve_account_id = None
    reserve_minor = _minor(profile['reserve_minor'], '应急保留金额')

    events = []

    def append_event(**values):
        if values['account_id'] not in account_by_id:
            raise ValueError('现金流记录引用了未知账户。')
        event = _event(as_of=as_of, end_on=end_on,
                       currency=account_by_id[values['account_id']]['currency'], **values)
        if event:
            events.append(event)

    loans = {row['id']: row for row in records['loans']}
    for row in records['loan_installments']:
        if row['status'] not in ('scheduled', 'overdue'):
            continue
        amount = max(0, row['principal_minor'] + row['interest_minor'] + row['fees_minor'] - row['paid_minor'])
        if not amount:
            continue
        loan = loans.get(row['loan_id'])
        if not loan:
            raise ValueError('贷款还款计划缺少对应贷款。')
        append_event(event_id=f'loan:{row["id"]}', due_on=row['due_on'],
                     account_id=loan['repayment_account_id'], amount_minor=amount,
                     cash_delta_minor=-amount, kind='loan_installment', certainty='confirmed',
                     title='贷款还款计划', source_table='loan_installments', evidence_id=row['id'])

    cards = {row['id']: row for row in records['cards']}
    for row in records['credit_statements']:
        if row['status'] != 'open':
            continue
        amount = max(0, row['total_due_minor'] - row['paid_minor'])
        if not amount:
            continue
        card = cards.get(row['card_id'])
        if not card:
            raise ValueError('信用卡账单缺少对应卡片。')
        append_event(event_id=f'credit:{row["id"]}', due_on=row['due_on'],
                     account_id=card['account_id'], amount_minor=amount,
                     cash_delta_minor=-amount, kind='credit_statement', certainty='confirmed',
                     title='信用卡已出账应还（全额口径）', source_table='credit_statements', evidence_id=row['id'])

    # Active merchant contracts project their stated renewal cadence as candidates.
    # A linked bank mandate is deliberately not emitted again for the same cycle.
    for row in records['subscriptions']:
        if row['merchant_status'] != 'active':
            continue
        next_due = _date(row['next_renewal_on'], '商户续费日期')
        cycle_months = {'monthly': 1, 'yearly': 12}.get(row['cycle'])
        if not cycle_months:
            raise ValueError('不支持的商户续费周期。')
        for due, catch_up in _recurring_occurrences(next_due, as_of, end_on, cycle_months):
            append_event(event_id=f'subscription:{row["id"]}:{due.isoformat()}',
                         due_on=due.isoformat(), account_id=row['account_id'],
                         amount_minor=row['amount_minor'], cash_delta_minor=-row['amount_minor'],
                         kind='subscription_renewal', certainty='candidate',
                         title=row['plan_name'] + '续费候选', source_table='subscriptions',
                         evidence_id=row['id'], status='candidate' if catch_up else 'scheduled')

    active_subscriptions_by_mandate = {
        row['mandate_id'] for row in records['subscriptions']
        if row['merchant_status'] == 'active' and row['mandate_id']
    }
    for row in records['mandates']:
        if row['status'] != 'active' or row['id'] in active_subscriptions_by_mandate:
            continue
        append_event(event_id=f'mandate:{row["id"]}', due_on=row['next_charge_date'],
                     account_id=row['account_id'], amount_minor=row['amount_minor'],
                     cash_delta_minor=-row['amount_minor'], kind='bank_mandate', certainty='candidate',
                     title=row['merchant'] + '银行代扣候选', source_table='mandates', evidence_id=row['id'])

    beneficiaries = {row['id']: row for row in records['beneficiaries']}
    for row in records['scheduled_payments']:
        if row['status'] != 'active':
            continue
        first_due = _date(row['next_due_on'], '定时转账日期')
        end_date = _date(row['end_on'], '定时转账结束日期') if row.get('end_on') else None
        if row['frequency'] not in ('once', 'monthly'):
            raise ValueError('不支持的模拟定时转账频率。')
        occurrences = ((first_due, False),) if row['frequency'] == 'once' else _recurring_occurrences(first_due, as_of, end_on, 1)
        for due, _catch_up in occurrences:
            if end_date and due > end_date:
                continue
            beneficiary = beneficiaries.get(row['beneficiary_id'])
            name = beneficiary['display_name'] if beneficiary else '模拟收款人'
            append_event(event_id=f'scheduled_transfer:{row["id"]}:{due.isoformat()}',
                         due_on=due.isoformat(), account_id=row['account_id'],
                         amount_minor=row['amount_minor'], cash_delta_minor=-row['amount_minor'],
                         kind='scheduled_transfer', certainty='confirmed',
                         title='定时转账 · ' + name, source_table='scheduled_payments', evidence_id=row['id'],
                         status='scheduled')

    for row in records['orders']:
        if row['status'] != 'pending_settlement':
            continue
        append_event(event_id=f'redemption:{row["id"]}', due_on=row['settlement_date'],
                     account_id=row['account_id'], amount_minor=row['amount_minor'],
                     cash_delta_minor=row['amount_minor'], kind='redemption_settlement', certainty='confirmed',
                     title='模拟赎回待结算', source_table='orders', evidence_id=row['id'])

    held_by_account = {account['id']: 0 for account in accounts}
    # 十年源库保留的预测进入候选分支，不冒充已确认支付承诺。
    for row in records.get('recorded_forecasts', []):
        if row.get('status')!='forecast_not_paid':continue
        account_id=next((a['id'] for a in accounts if a['id'].endswith('-operating') and row.get('scope')=='self_employed'),None)
        account_id=account_id or next((a['id'] for a in accounts if a['id'].endswith('-main')),None)
        if not account_id:raise ValueError('已记录预测缺少对应运行账户。')
        cycle={'monthly':1,'yearly':12}.get(row['frequency'])
        anchor=_date(row['next_due_on'],'已记录预测日期')
        occurrences=_recurring_occurrences(anchor,as_of,end_on,cycle) if cycle else ((anchor,False),)
        for due,_ in occurrences:
            append_event(event_id='forecast:'+row['obligation_id']+':'+due.isoformat(),due_on=due.isoformat(),
                         account_id=account_id,amount_minor=row['amount_minor'],cash_delta_minor=-row['amount_minor'],
                         kind='recorded_forecast',certainty='candidate',title=row['category']+'（未付预测）',
                         source_table='recurring_obligations',evidence_id=row['obligation_id'],status='forecast_not_paid')
    proposed_reservations = []
    for row in records['fund_holds']:
        if row['status'] == 'held':
            if row['account_id'] not in held_by_account:
                raise ValueError('资金预留引用了未知账户。')
            amount = _minor(row['amount_minor'], '资金预留金额')
            held_by_account[row['account_id']] += amount
            expires = _date(row['expires_on'], '资金预留失效日期')
            release_on = max(as_of + timedelta(days=1), expires + timedelta(days=1))
            append_event(event_id=f'hold_release:{row["id"]}', due_on=release_on.isoformat(),
                         account_id=row['account_id'], amount_minor=amount,
                         cash_delta_minor=0, reserved_delta_minor=-amount,
                         kind='reservation_release', certainty='confirmed',
                         title='模拟任务预留到期释放', source_table='fund_holds', evidence_id=row['id'])
        elif row['status'] == 'proposed':
            proposed_reservations.append({
                'id': row['id'], 'account_id': row['account_id'],
                'amount_minor': _minor(row['amount_minor'], '拟议预留金额'),
                'status': 'proposed', 'counted_in_projection': False,
                'reason': '用户尚未确认的资金预留草案，不扣减可用余额。',
                'source_table': 'fund_holds', 'evidence_id': row['id'],
            })

    pending_operations = []
    for row in records['operations']:
        if row['state'] != 'pending_confirmation':
            continue
        payload = row.get('payload') or {}
        amount = payload.get('amount_minor')
        if type(amount) is not int or amount < 0:
            amount = 0
        pending_operations.append({
            'id': row['id'], 'action': row['action'], 'account_id': payload.get('account_id'),
            'amount_minor': amount, 'status': row['state'], 'counted_in_projection': False,
            'reason': '操作尚待用户确认，不计入现金流。',
            'source_table': 'operations', 'evidence_id': row['id'],
        })

    unconfirmed_schedules = []
    for row in records['scheduled_payments']:
        if row['status'] == 'draft':
            unconfirmed_schedules.append({
                'id': row['id'], 'account_id': row['account_id'],
                'scheduled_date': row['next_due_on'], 'amount_minor': _minor(row['amount_minor'], '定时转账金额'),
                'status': row['status'], 'counted_in_projection': False,
                'reason': '定时转账草案未启用，不作为已确认的支付承诺。',
                'source_table': 'scheduled_payments', 'evidence_id': row['id'],
            })

    events.sort(key=lambda row: (row['date'], row['account_id'], row['kind'], row['id']))
    by_day = {}
    for event in events:
        by_day.setdefault(event['date'], []).append(event)

    account_state = {}
    reserve_floors={a['id']:reserve_minor if a['id']==reserve_account_id else 0 for a in accounts}
    if archive.get('data_scope')=='navren_synthetic':
        reserve_floors=demo_reserve_floors(accounts,reserve_minor)
        reserve_account_id=next((a['id'] for a in accounts if reserve_floors[a['id']]>0),None)
    for account in accounts:
        account_id = account['id']
        opening = _minor(account['balance_minor'], '账户余额')
        floor = reserve_floors[account_id]
        held = held_by_account[account_id]
        account_state[account_id] = {
            'balance_confirmed': opening, 'balance_expected': opening,
            'held_minor': held, 'reserve_floor_minor': floor,
            'minimum_confirmed_available_minor': opening - held - floor,
            'minimum_expected_available_minor': opening - held - floor,
        }

    timeline = []
    for day in sorted(by_day):
        day_events = by_day[day]
        by_account = {}
        for event in day_events:
            summary = by_account.setdefault(event['account_id'], {
                'account_id': event['account_id'], 'confirmed_inflow_minor': 0,
                'currency': event['currency'],
                'confirmed_outflow_minor': 0, 'candidate_inflow_minor': 0,
                'candidate_outflow_minor': 0, 'released_reservation_minor': 0,
                '_confirmed_delta_minor': 0, '_candidate_delta_minor': 0,
                '_reserved_delta_minor': 0,
            })
            delta = event['cash_delta_minor']
            if event['certainty'] == 'confirmed':
                summary['_confirmed_delta_minor'] += delta
                if delta >= 0:
                    summary['confirmed_inflow_minor'] += delta
                else:
                    summary['confirmed_outflow_minor'] -= delta
            else:
                summary['_candidate_delta_minor'] += delta
                if delta >= 0:
                    summary['candidate_inflow_minor'] += delta
                else:
                    summary['candidate_outflow_minor'] -= delta
            summary['_reserved_delta_minor'] += event['reserved_delta_minor']
            if event['reserved_delta_minor'] < 0:
                summary['released_reservation_minor'] -= event['reserved_delta_minor']
        account_summaries = []
        for account_id in sorted(by_account):
            state = account_state[account_id]
            summary = by_account[account_id]
            state['held_minor'] = max(0, state['held_minor'] + summary['_reserved_delta_minor'])
            state['balance_confirmed'] += summary['_confirmed_delta_minor']
            state['balance_expected'] += summary['_confirmed_delta_minor'] + summary['_candidate_delta_minor']
            state['minimum_confirmed_available_minor'] = min(
                state['minimum_confirmed_available_minor'],
                state['balance_confirmed'] - state['held_minor'] - state['reserve_floor_minor'])
            state['minimum_expected_available_minor'] = min(
                state['minimum_expected_available_minor'],
                state['balance_expected'] - state['held_minor'] - state['reserve_floor_minor'])
            public_summary = {key: value for key, value in summary.items() if not key.startswith('_')}
            account_summaries.append({
                **public_summary,
                'balance_after_confirmed_minor': state['balance_confirmed'],
                'available_after_confirmed_minor': state['balance_confirmed'] - state['held_minor'] - state['reserve_floor_minor'],
                'balance_if_candidates_occur_minor': state['balance_expected'],
                'available_if_candidates_occur_minor': state['balance_expected'] - state['held_minor'] - state['reserve_floor_minor'],
                'held_minor': state['held_minor'], 'reserve_floor_minor': state['reserve_floor_minor'],
            })
        timeline.append({'date': day, 'events': day_events, 'accounts': account_summaries})

    account_summaries = []
    for account in accounts:
        state = account_state[account['id']]
        account_summaries.append({
            'account_id': account['id'], 'account_name': account['name'],
            'currency': account['currency'], 'opening_balance_minor': account['balance_minor'],
            'reserve_floor_minor': state['reserve_floor_minor'], 'held_minor': held_by_account[account['id']],
            'current_available_minor': account['balance_minor'] - state['reserve_floor_minor'] - held_by_account[account['id']],
            'projected_balance_after_confirmed_minor': state['balance_confirmed'],
            'projected_available_after_confirmed_minor': state['balance_confirmed'] - state['reserve_floor_minor'] - state['held_minor'],
            'projected_balance_if_candidates_occur_minor': state['balance_expected'],
            'projected_available_if_candidates_occur_minor': state['balance_expected'] - state['reserve_floor_minor'] - state['held_minor'],
            'minimum_available_after_confirmed_minor': state['minimum_confirmed_available_minor'],
            'minimum_available_if_candidates_occur_minor': state['minimum_expected_available_minor'],
        })

    all_events = [event for day in timeline for event in day['events']]
    totals_by_currency = {currency: {
        'confirmed_inflow_minor': 0, 'confirmed_outflow_minor': 0,
        'candidate_inflow_minor': 0, 'candidate_outflow_minor': 0,
    } for currency in sorted(currencies)}
    for event in all_events:
        currency = event['currency']
        totals = totals_by_currency[currency]
        prefix = 'confirmed' if event['certainty'] == 'confirmed' else 'candidate'
        direction = 'inflow' if event['cash_delta_minor'] >= 0 else 'outflow'
        totals[f'{prefix}_{direction}_minor'] += abs(event['cash_delta_minor'])
    reserve_unallocated_minor = reserve_minor if reserve_account_id is None else 0
    return {
        'environment': 'simulation', 'source': 'simulated_bank_archive',
        'workspace_id': workspace_id or archive.get('workspace_id'), 'version': archive.get('version'),
        'as_of': as_of.isoformat(), 'horizon_end': end_on.isoformat(),
        'horizon_days': horizon_days, 'date_granularity': 'day',
        'currency': next(iter(currencies)) if len(currencies) == 1 else None,
        'amount_unit': '人民币分' if currencies == {'CNY'} else '对应账户币种的模拟账本最小单位',
        'accounts': account_summaries,
        'reserve_unallocated_minor': reserve_unallocated_minor,
        'totals': totals_by_currency[next(iter(currencies))] if len(currencies) == 1 else None,
        'totals_by_currency': totals_by_currency, 'timeline': timeline,
        'not_included': {
            'unconfirmed_operations': pending_operations,
            'unconfirmed_scheduled_transfers': unconfirmed_schedules,
            'proposed_fund_reservations': proposed_reservations,
            'future_salary_and_other_unconfirmed_income': '不预支或推测未来工资及其他未确认收入。',
            'unrecorded_daily_spending': '没有预测未录入的生活支出、未出账信用卡消费或未来市场价格。',
            'imported_statements': '本时间轴仅使用当前模拟银行账户档案；本地导入流水与银行现金余额分开，不参与推算。',
        },
        'formula': '已结算现金余额 + 已确认未来流入 - 已确认未来流出；另展示周期续费/代扣候选情景。应急保留额和生效资金预留只减少可用金额，不减少银行现金余额。',
        'notice': '推演不是银行余额、收益预测或支付承诺；周期续费/代扣属于候选情景。最低可用金额按日终净额计算，没有日内先后顺序。',
    }
