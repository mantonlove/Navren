"""将本轮选中的受信事实投影为消息组件；不计算或推测新的金融指标。

source_path 是 BankTools 在本地登记的 JSON Pointer。所有展示数值须
逐项回查同一 evidence.data，禁止凭相同金额寻找其他来源或对象。
这个模块不接受模型布局、自由 HTML、交易动作或浏览器缓存。
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import re


SCHEMA = 'navren.reply-presentation.v1'
_TOOLS = {'statement_details', 'monthly_statement', 'annual_statement',
          'budget', 'budget_aggregate', 'account_summary', 'financial_list_transfers', 'cashflow'}
_KINDS = {'expense': '支出', 'income': '收入', 'refund': '退款',
          'transfer': '转账', 'investment_purchase': '投资买入',
          'investment_redemption': '投资赎回', 'loan_repayment': '还款'}


def _pointer(data, pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('invalid_fact_source_path')
    current, parent = data, None
    for raw in pointer[1:].split('/'):
        key = raw.replace('~1', '/').replace('~0', '~')
        parent = current
        if isinstance(current, dict):
            current = current[key]
        elif isinstance(current, list) and re.fullmatch(r'0|[1-9]\d*', key):
            current = current[int(key)]
        else:
            raise ValueError('invalid_fact_source_path')
    return current, parent, key


def _money(value, currency='CNY'):
    number = format(Decimal(value) / Decimal(100), ',.2f')
    return number + '元' if currency == 'CNY' else currency + ' ' + number


def _period(data):
    if isinstance(data.get('month'), str):
        return data['month']
    if type(data.get('year')) is int:
        return str(data['year'])
    return data.get('as_of') if isinstance(data.get('as_of'), str) else ''


def _period_label(period):
    if re.fullmatch(r'\d{4}-\d{2}', period):
        return period[:4] + '年' + str(int(period[5:])) + '月'
    return period + '年' if re.fullmatch(r'\d{4}', period) else period


def _value(fact, label=None):
    row = {'id': fact['id'], 'fact_id': fact['id'],
           'label': label or fact['label'], 'currency': fact.get('currency')}
    row['value_minor' if fact['unit'] == 'minor_currency' else 'value_scalar'] = fact['value']
    return row


def _source(evidence, data):
    raw = data.get('source', evidence.get('source', 'simulation'))
    return 'imported_local_csv' if raw in ('imported', 'imported_local_csv') else 'simulation'


def _block(evidence, data, kind, title, facts):
    currencies = {f['currency'] for f in facts if f['unit'] == 'minor_currency'}
    return {'id': evidence['id'] + ':' + kind, 'type': kind, 'title': title,
            'period': _period(data), 'currency': next(iter(currencies)) if len(currencies) == 1 else None,
            'source': _source(evidence, data), 'evidence_id': evidence['id'],
            'source_version': str(data.get('source_version', data.get('dataset_version', data.get('version', '')))),
            'coverage_start': data.get('coverage_start'), 'coverage_end': data.get('coverage_end'),
            'recorded_range': deepcopy(data.get('recorded_range')),
            'metrics': [], 'rows': []}


def _summary(evidence, data, facts, title='已核验记录'):
    block = _block(evidence, data, 'fact_summary', title, facts)
    for fact in facts:
        row = _value(fact)
        if fact['name'] and fact['name'] != fact['label']:
            row['label'] = fact['name'] + ' · ' + fact['label']
        block['rows'].append(row)
    return block


def _details(evidence, data, facts):
    by_path = {f['source_path']: f for f in facts}
    label = data.get('category') or _KINDS.get(data.get('kind'), '流水')
    block = _block(evidence, data, 'transaction_list', label, facts)
    for path, title in (('/transaction_count', '笔数'), ('/total_minor', '合计')):
        if path in by_path:
            block['metrics'].append(_value(by_path[path], title))
    for index, item in enumerate(data.get('items', [])):
        fact = by_path.get('/items/' + str(index) + '/amount_minor')
        if not fact:
            continue
        row = _value(fact, item.get('category') or _KINDS.get(item.get('kind'), '流水'))
        row.update(record_id=item.get('id'), account_id=item.get('account_id'),
                   date=item.get('occurred_at'), kind=item.get('kind'))
        block['rows'].append(row)
    if type(data.get('transaction_count')) is int and '/transaction_count' in by_path:
        block['total_count'] = data['transaction_count']
    block['has_more'] = data.get('has_more') is True
    block['offset'] = data.get('offset', 0)
    block['kind'] = data.get('kind')
    block['category'] = data.get('category')
    block['rows_complete'] = (data.get('has_more') is False and data.get('offset', 0) == 0
                              and len(block['rows']) == data.get('transaction_count'))
    if not block['rows_complete']:
        block['note'] = '笔数与合计统计全部筛选记录；下方仅展示本次已核验明细。'
    if not block['rows'] and data.get('items'):
        block['type'] = 'fact_summary'
        block['title'] = label + ' · 筛选记录汇总'
        block['note'] = '本次只展示已核验的筛选统计，未展示逐笔明细。'
    # Exactly one selected row can stand for its identical selected aggregate.
    # Keep all IDs in fact_ids, but do not print the same amount twice.
    if block.get('total_count') == 1 and block['rows_complete']:
        total = by_path.get('/total_minor')
        if total and len(block['rows']) == 1 and total['value'] == block['rows'][0]['value_minor']:
            block['metrics'] = [m for m in block['metrics'] if m['fact_id'] != total['id']]
            block['rows'][0]['equivalent_fact_ids'] = [total['id']]
    return block


def _expense_statement(evidence, data, facts):
    category = [f for f in facts if f['source_path'].startswith('/category_expense_minor/')]
    is_month = evidence['tool'] == 'monthly_statement'
    kind = 'category_bars' if category else 'fact_summary'
    title = ('现金消费分类' if category else '收支记录') if is_month else '年度收支记录'
    block = _block(evidence, data, kind, title, facts)
    category_ids = {f['id'] for f in category}
    for fact in sorted(category, key=lambda f: f['value'], reverse=True):
        _, _, name = _pointer(data, fact['source_path'])
        row = _value(fact, name)
        row['category'] = name
        block['rows'].append(row)
    for fact in facts:
        if fact['id'] in category_ids:
            continue
        row = _value(fact)
        if fact['source_path'] == '/cash_expense_minor':
            row['label'] = '现金消费合计'
            row['role'] = 'secondary'
            block['metrics'].append(row)
        elif fact['source_path'].startswith('/totals_minor/') or fact['field'] in (
                'income_expense_surplus_minor', 'transaction_count'):
            row['role'] = 'primary' if fact['field'] == 'income_expense_surplus_minor' else 'secondary'
            block['metrics'].append(row)
        else:
            block['rows'].append(row)
    block['note'] = ('分类仅含现金消费；账单收支结余不等于账户余额。' if category
                     else '账单收支结余不等于账户余额。')
    if (data.get('partial_period') is True or data.get('source') in ('imported', 'imported_local_csv')
            or (not is_month and data.get('complete_calendar_year') is not True)):
        block['note'] += '统计限于已记录范围。'
    if not is_month:
        block['covered_months'] = deepcopy(data.get('covered_months', []))
    block['category_basis'] = data.get('category_expense_basis', '现金消费流水')
    block['expense_basis'] = data.get('expense_basis', '已记录消费支出')
    block['rows_complete'] = len(category) == len(data.get('category_expense_minor', {}))
    if category and not block['rows_complete']:
        block['note'] += '这里只展示本次选中的分类。'
    if data.get('source') == 'simulation':
        block['note'] += '来源为合成模拟流水，不代表完整真实银行账单。'
        full = data.get('category_expense_minor')
        total = data.get('totals_minor', {}).get('expense')
        if (category and isinstance(full, dict) and full and type(total) is int
                and all(type(v) is int and v >= 0 for v in full.values())
                and sum(full.values()) == total):
            largest = [name for name, value in full.items() if value == max(full.values())]
            selected = {f['source_path'] for f in category}
            paths = {'/category_expense_minor/' + name.replace('~', '~0').replace('/', '~1') for name in largest}
            if paths <= selected:
                block['note'] += '本期最大支出分类：' + '、'.join(largest) + '。'
    return block


def _statement(evidence, data, facts):
    income = [f for f in facts if f['source_path'].startswith('/category_income_minor/')]
    if not income:
        return _expense_statement(evidence, data, facts)
    income_ids = {f['id'] for f in income}
    total = [f for f in facts if f['source_path'] == '/totals_minor/income']
    income_ids.update(f['id'] for f in total)
    selected = [f for f in facts if f['id'] in income_ids]
    # Negative realized results are signed income components, not bar lengths.
    kind = 'fact_summary' if any(f['value'] < 0 for f in income) else 'category_bars'
    block = _block(evidence, data, kind, '收入来源', selected)
    block['id'] += ':income'
    block['statement_kind'] = 'income'
    block['kind'] = 'income'
    block['category_basis'] = data.get('income_category_basis', '已记录收入分类')
    for fact in total:
        row = _value(fact, '已确认收入合计')
        row['role'] = 'primary'
        block['metrics'].append(row)
    for fact in sorted(income, key=lambda f: (-f['value'], f['source_path'])):
        _, _, name = _pointer(data, fact['source_path'])
        row = _value(fact, name)
        row['category'] = name
        block['rows'].append(row)
    block['rows_complete'] = (data.get('income_categories_complete') is True
                              and len(income) == len(data.get('category_income_minor', {})))
    block['note'] = block['category_basis'] + '账单收入不等于账户余额。'
    if any(f['value'] < 0 for f in income):
        block['note'] += '负数表示该收入科目的净损失或冲减。'
    is_month = evidence['tool'] == 'monthly_statement'
    if (data.get('partial_period') is True or data.get('source') in ('imported', 'imported_local_csv')
            or (not is_month and data.get('complete_calendar_year') is not True)):
        block['note'] += '统计限于已记录范围。'
    if not is_month:
        block['covered_months'] = deepcopy(data.get('covered_months', []))
    if not block['rows_complete']:
        block['note'] += '这里只展示本次选中的收入分类。'
    others = [f for f in facts if f['id'] not in income_ids]
    # Income and cash expense never share one comparison chart.
    return [block, _expense_statement(evidence, data, others)] if others else block


def _transfers(evidence, envelope, facts):
    data = envelope['data']
    block = _block(evidence, envelope, 'transaction_list', '历史转账', facts)
    first, last = data.get('actual_coverage_start'), data.get('actual_coverage_end')
    block.update(period=(first + ' — ' + last) if first and last else '',
                 coverage_start=first, coverage_end=last, kind='transfer',
                 has_more=data['has_more'], offset=data['offset'])
    by_path = {f['source_path']: f for f in facts}
    count = by_path.get('/data/transfer_count')
    used = set()
    if count:
        block['metrics'].append(_value(count, '笔数'))
        block['total_count'] = count['value']
        used.add(count['id'])
    directions = {'internal': '账户间调拨', 'incoming': '转入', 'outgoing': '转出'}
    for index, total in enumerate(data.get('totals_by_currency_and_direction', [])):
        fact = by_path.get('/data/totals_by_currency_and_direction/' + str(index) + '/amount_minor')
        if fact:
            block['metrics'].append(_value(fact, directions.get(total['direction'], '转账') + '合计'))
            used.add(fact['id'])
    for index, item in enumerate(data.get('items', [])):
        fact = by_path.get('/data/items/' + str(index) + '/amount_minor')
        if not fact:
            continue
        row = _value(fact, directions.get(item['direction'], '转账'))
        row.update(record_id=item['transfer_id'], date=item['occurred_at'], kind='transfer',
                   direction=directions.get(item['direction'], '转账'))
        if item.get('counterparty'):
            row['counterparty'] = item['counterparty']
        block['rows'].append(row)
        used.add(fact['id'])
    for fact in facts:
        if fact['id'] not in used:
            block['metrics'].append(_value(fact))
    block['rows_complete'] = bool(count and data['item_page_complete']
                                 and len(block['rows']) == data['transfer_count'])
    block['note'] = '本人账户间调拨按一次事件计数，不含当前账户的新操作。'
    if not block['rows_complete']:
        block['note'] += '笔数与合计覆盖全部筛选记录，下方仅展示本次已核验明细。'
    if data.get('missing_fields'):
        block['note'] += '部分记录字段缺失，未知金额未按零计算。'
    return block


def _budget(evidence, data, facts):
    by_path = {f['source_path']: f for f in facts}
    scenario = data.get('budget_scope') == 'planning_scenario' or evidence.get('parameters', {}).get('floor_minor', 0) > 0
    account_name = data.get('name') or data.get('account_name') or '本次账户'
    title = account_name + (' · 资金情景测算' if scenario else ' · 资金安排')
    block = _block(evidence, data, 'funding_breakdown', title, facts)
    block['account_id'] = data.get('account_id')
    block['account_name'] = account_name
    block['scenario'] = scenario
    used = set()
    entries = (('cash_minor', '已结算现金', 'cash'), ('reserve_minor', '应急保留', 'deduction'),
               ('upcoming_due_minor', '30天已知到期款', 'deduction'), ('held_minor', '生效资金预留', 'deduction'),
               ('additional_reserve_minor', '额外规划留款', 'deduction'),
               ('investable_minor', '情景可安排资金' if scenario else '可安排资金', 'available'))
    for field, title, role in entries:
        fact = by_path.get('/' + field)
        if not fact:
            continue
        used.add(fact['id'])
        row = _value(fact, title)
        row['role'] = role
        (block['metrics'] if role == 'available' else block['rows']).append(row)
    for fact in facts:
        if fact['id'] not in used:
            block['rows'].append(_value(fact))
    needed = ['cash_minor', 'reserve_minor', 'upcoming_due_minor', 'held_minor', 'investable_minor']
    complete = all('/' + field in by_path for field in needed)
    extra = data.get('additional_reserve_minor', 0)
    if type(extra) is not int or (extra and '/additional_reserve_minor' not in by_path):
        complete = False
    # This only checks whether an explanatory diagram is complete. Its endpoint
    # always remains the selected backend fact, never this expression.
    if complete:
        complete = max(0, data['cash_minor'] - data['reserve_minor'] - data['upcoming_due_minor']
                       - data['held_minor'] - extra) == data['investable_minor']
    block['breakdown_complete'] = complete
    block['note'] = ('已扣保留资金、预留和30天已知到期款；未预支未来收入或估算其他生活开支。'
                     if complete else '仅列本次已核验资金项，不能据此还原完整扣减过程。')
    if scenario:
        block['note'] += '这是条件测算，不产生扣款或资金预留。'
    return block


def _cashflow(evidence, data, facts):
    # Use the existing static fact_summary view. No model layout, new UI type,
    # new money calculation, or boolean-as-money fact registration is involved.
    from datetime import date, timedelta
    days = data.get('horizon_days')
    excluded = data.get('not_included')
    if (data.get('source') != 'simulated_bank_archive' or data.get('date_granularity') != 'day'
            or not isinstance(excluded, dict) or any(not isinstance(excluded.get(key), str) or not excluded[key]
                for key in ('future_salary_and_other_unconfirmed_income', 'unrecorded_daily_spending'))):
        raise ValueError('invalid_cashflow_coverage')
    if (type(days) is not int or not 1 <= days <= 365
            or evidence.get('parameters') != {'days': days}
            or date.fromisoformat(data['horizon_end']) != date.fromisoformat(data['as_of']) + timedelta(days=days)):
        raise ValueError('invalid_cashflow_period')
    fields = {'minimum_available_after_confirmed_minor': 'confirmed_floor_breached',
              'minimum_available_if_candidates_occur_minor': 'candidate_floor_breached'}
    matches = [re.fullmatch(r'/accounts/(0|[1-9]\d*)/(' + '|'.join(fields) + ')', f['source_path']) for f in facts]
    if len(facts) != 2 or any(m is None for m in matches) or len({m[1] for m in matches}) != 1:
        raise ValueError('incomplete_cashflow_facts')
    account = data['accounts'][int(matches[0][1])]
    if ({m[2] for m in matches} != set(fields) or not isinstance(account.get('account_id'), str)
            or not account['account_id'] or not isinstance(account.get('account_name'), str)
            or not account['account_name'] or not re.fullmatch(r'[A-Z]{3}', account.get('currency', ''))):
        raise ValueError('invalid_cashflow_account')
    by_field = {f['field']: f for f in facts}
    for field, flag in fields.items():
        if (type(account.get(field)) is not int or type(account.get(flag)) is not bool
                or account[flag] != (account[field] < 0)
                or by_field[field]['value'] != account[field]
                or by_field[field]['currency'] != account['currency']):
            raise ValueError('invalid_cashflow_floor_state')
    block = _block(evidence, data, 'fact_summary', account['account_name'] + ' · 已记录事项情景', facts)
    block['period'] = data['as_of'] + '至' + data['horizon_end']
    block['rows'] = [_value(by_field[field]) for field in fields]
    confirmed = '会跌破' if account['confirmed_floor_breached'] else '不跌破'
    candidate = '会跌破' if account['candidate_floor_breached'] else '不跌破'
    block['note'] = ('已确认事项情景' + confirmed + '已设置的应急底线；候选也发生情景' + candidate + '该底线。'
        '上述最低金额是已扣应急底线与生效预留后的底线外余量，与零比较，不再扣一次底线。'
        '仅覆盖本期已记录事项；续费和代扣候选不等于已确认支付承诺。'
        '不预测未录入的生活支出或未确认未来收入，不能据此判断缓冲充分性。'
        '最低金额按日终净额计算，不证明日内支付顺序可行。')
    return block


def _income_window_blocks(query, groups, evidence):
    from datetime import date, timedelta, datetime
    if not isinstance(query, dict) or set(query) != {'month', 'days'} or len(groups) != 2:
        raise ValueError('invalid_income_window_query')
    monthly = [(evidence[eid], selected) for eid, selected in groups.items()
               if evidence[eid]['tool'] == 'monthly_statement']
    flows = [(evidence[eid], selected) for eid, selected in groups.items()
             if evidence[eid]['tool'] == 'cashflow']
    if len(monthly) != 1 or len(flows) != 1:
        raise ValueError('incomplete_income_window_sources')
    report, income = monthly[0]; flow, inflow = flows[0]
    m, c = report['data'], flow['data']
    if (len(income) != 1 or len(inflow) != 1 or income[0]['source_path'] != '/totals_minor/income'
            or inflow[0]['source_path'] != '/totals/confirmed_inflow_minor'
            or income[0]['currency'] != inflow[0]['currency'] or c.get('currency') != inflow[0]['currency']
            or m.get('source') != 'simulation' or m.get('month') != query['month']
            or report.get('parameters') != {'month': query['month']}
            or flow.get('parameters') != {'days': query['days']}
            or type(query['days']) is not int or not 1 <= query['days'] <= 365
            or c.get('horizon_days') != query['days'] or c.get('source') != 'simulated_bank_archive'
            or date.fromisoformat(c['horizon_end']) != date.fromisoformat(c['as_of']) + timedelta(days=query['days'])):
        raise ValueError('invalid_income_window_binding')
    recorded = m.get('recorded_range')
    if (not isinstance(recorded, list) or len(recorded) != 2
            or any(not isinstance(v, str) or datetime.fromisoformat(v).date().strftime('%Y-%m') != query['month'] for v in recorded)
            or datetime.fromisoformat(recorded[0]) > datetime.fromisoformat(recorded[1])):
        raise ValueError('unknown_monthly_recorded_range')
    history = _block(report, m, 'fact_summary', '已记录月收入', income)
    history['rows'] = [_value(income[0], '月报已记录收入合计')]
    history['note'] = ('来源为模拟账单；实际记录范围' + recorded[0][:10] + '至' + recorded[1][:10] + '。'
        '仅统计已记录范围，该范围不证明整月完整覆盖；账单收入不等于当前余额，也不自动加入未来资金。')
    future = _block(flow, c, 'fact_summary', '未来窗口已确认入账', inflow)
    future['period'] = c['as_of'] + '至' + c['horizon_end']
    future['rows'] = [_value(inflow[0], '该窗口已确认未来入账')]
    future['note'] = ('这是该未来窗口内已记录、已确认的入账事项；即使为零，也不能据此断言没有收入或收入档案缺失。'
        '未确认的未来工资等收入未被预支，未录入事项仍未知；历史月收入与未来已确认入账分属不同期间和口径。')
    return [history, future]


def _accounts(evidence, data, facts):
    block = _block(evidence, data, 'account_balances', '账户与已结算现金', facts)
    for fact in facts:
        match = re.fullmatch(r'/accounts/(\d+)/balance_minor', fact['source_path'])
        if match:
            account = data['accounts'][int(match[1])]
            row = _value(fact, account.get('name') or fact['name'])
            row['account_id'] = account.get('id')
            block['rows'].append(row)
        else:
            row = _value(fact)
            row['label'] = fact['name'] + ' · ' + fact['label']
            block['metrics'].append(row)
    block['note'] = '已结算现金、持仓本金与待到账赎回分别列示；信用额度不计入现金。'
    return block


def _budget_aggregate(evidence, data, facts):
    block = _block(evidence, data, 'fact_summary', data['name'], facts)
    block['budget_aggregate'] = True
    block['metrics'] = [_value(fact, '可安排资金合计') for fact in facts]
    block['note'] = data['amount_basis']
    return block


def _display(row):
    if 'value_minor' in row:
        return _money(row['value_minor'], row.get('currency') or 'CNY')
    return str(row['value_scalar'])


def _readable(block):
    period = _period_label(block['period'])
    if block.get('statement_kind') == 'expense' and block.get('selection') == 'largest_category':
        rows = block['rows']
        if not rows or rows[0]['value_minor'] == 0:
            lead = period + '没有已记录的现金消费支出。'
        else:
            largest = [r for r in rows if r['value_minor'] == rows[0]['value_minor']]
            lead = period + '已记录的现金消费中，最大分类是' + '、'.join(r['label'] for r in largest) + '，'
            lead += ('各' if len(largest) > 1 else '') + _display(largest[0]) + '。'
        text = lead
    elif block.get('statement_kind') == 'income':
        total = next((m for m in block['metrics'] if m.get('role') == 'primary'), None)
        # The component presents the amounts and categories; the lead only
        # states the main finding without repeating the whole ledger.
        rows = block['rows']
        if (block.get('rows_complete') is True and total and total['value_minor'] > 0
                and rows and all(row['value_minor'] >= 0 for row in rows)
                and sum(row['value_minor'] for row in rows) == total['value_minor']
                and (len(rows) == 1 or rows[0]['value_minor'] > rows[1]['value_minor'])):
            lead = period + '的收入主要来自' + rows[0]['label'] + '。'
        else:
            lead = period + '的已确认收入分项如下。'
        text = lead
    elif block.get('budget_aggregate'):
        lead = ('截至' + period + '，' if period else '') + block['title'] + '为'
        lead += '、'.join(_display(row) for row in block['metrics']) + '。'
        text = lead
    elif block['type'] == 'transaction_list':
        count = next((m['value_scalar'] for m in block['metrics'] if m['label'] == '笔数'), None)
        lead = period + ('这项' if block.get('category') else '') + _KINDS.get(block.get('kind'), '流水')
        lead += '共' + str(count) + '笔。' if count is not None else '明细已查到。'
        if count == 0:
            lead = period + '没有符合这次筛选条件的记录。'
        text = period + ' · ' + block['title']
        if count is not None:
            text += '：共' + str(count) + '笔。'
    elif block['type'] == 'funding_breakdown':
        lead = (block['account_name'] + '的条件测算如下。' if block['scenario']
                else block['account_name'] + '的资金安排如下。')
        text = (period + ' · ' if period else '') + block['title']
    elif block['type'] == 'category_bars':
        surplus = next((m for m in block['metrics'] if m.get('role') == 'primary'), None)
        lead = ('算上退款，这份账单的消费支出比收入多。' if surplus and surplus['value_minor'] < 0
                else '这份账单有结余。' if surplus and surplus['value_minor'] > 0
                else '这份账单的消费收支持平。' if surplus else period + '的收支与现金消费分类如下。')
        text = period + ' · ' + lead
    else:
        lead = (period + '的' if period else '') + block['title'] + '已查到。'
        text = lead
    parts = [text]
    for row in block['metrics']:
        if block['type'] == 'transaction_list' and row['label'] == '笔数':
            continue
        parts.append(row['label'] + '：' + _display(row))
    for row in block['rows']:
        date = row.get('date', '')
        parts.append((date[:10] + ' · ' if date else '') + row['label'] + '：' + _display(row))
    if block.get('note'):
        parts.append(block['note'])
    return lead, '\n'.join(parts)


def build_reply_presentation(workspace_id, response_id, facts, evidence, fact_ids, *, reviewed=False, statement_query=None, income_window_query=None):
    """Return a local verified-facts view, or None to retain the original reply.

    Reviewed prose is deliberately not transformed after approval. It can only
    adopt this view when the existing reviewer/final publication supports the
    complete visible contract; that integration is a separate explicit change.
    """
    if reviewed or not isinstance(workspace_id, str) or not workspace_id or not isinstance(response_id, str) or not response_id:
        return None
    if not isinstance(facts, dict) or not isinstance(evidence, dict) or not isinstance(fact_ids, (list, tuple)):
        return None
    if not 1 <= len(fact_ids) <= 24 or any(not isinstance(fid, str) for fid in fact_ids) or len(set(fact_ids)) != len(fact_ids):
        return None
    groups = OrderedDict()
    try:
        for fid in fact_ids:
            fact = facts[fid]
            if not isinstance(fact, dict):
                return None
            source = evidence[fact['evidence_id']]
            if not isinstance(source, dict) or not isinstance(source.get('data'), dict):
                return None
            if source.get('tool') not in _TOOLS or source.get('id') != fact['evidence_id']:
                return None
            if source['data'].get('workspace_id') not in (None, workspace_id):
                return None
            if fact.get('id') != fid or fact.get('unit') not in ('minor_currency', 'scalar') or type(fact.get('value')) is not int:
                return None
            value, parent, field = _pointer(source['data'], fact['source_path'])
            if type(value) is not int or value != fact['value'] or field != fact['field']:
                return None
            if not all(isinstance(fact.get(k), str) and fact[k] for k in ('label', 'name', 'path')):
                return None
            expected = 'f' + hashlib.sha256((fact['evidence_id'] + fact['path']).encode()).hexdigest()[:20]
            if fid != expected:
                return None
            currency = (parent.get('currency') if isinstance(parent, dict) else None) or source['data'].get('currency')
            if fact['unit'] == 'minor_currency' and (fact.get('currency') not in ('CNY', 'USD', 'EUR', 'GBP', 'JPY')
                    or (currency is not None and currency != fact['currency'])):
                return None
            groups.setdefault(fact['evidence_id'], []).append(fact)
        blocks, leads, texts = [], [], []
        if income_window_query is not None:
            blocks = _income_window_blocks(income_window_query, groups, evidence)
            for block in blocks:
                lead, text = _readable(block)
                leads.append(lead); texts.append(text)
        for eid, selected in ([] if income_window_query is not None else sorted(groups.items(), key=lambda pair: evidence[pair[0]]['tool'] != 'budget_aggregate')):
            source, data = evidence[eid], evidence[eid]['data']
            producer = {'statement_details': _details, 'monthly_statement': _statement,
                        'annual_statement': _statement, 'budget': _budget, 'budget_aggregate': _budget_aggregate,
                        'account_summary': _accounts, 'financial_list_transfers': _transfers,
                        'cashflow': _cashflow}[source['tool']]
            produced = producer(source, data, selected)
            for block in produced if isinstance(produced, list) else [produced]:
                if (statement_query and statement_query.get('kind') == 'expense'
                        and statement_query.get('selection') == 'largest_category'
                        and source['tool'] == 'monthly_statement'
                        and data.get('month') == statement_query.get('month')):
                    categories = data.get('category_expense_minor', {})
                    total = data.get('cash_expense_minor')
                    expected_paths = {'/cash_expense_minor', *('/category_expense_minor/' +
                        k.replace('~', '~0').replace('/', '~1') for k in categories)}
                    if (block['rows_complete'] and type(total) is int and total >= 0
                            and all(type(v) is int and v >= 0 for v in categories.values())
                            and sum(categories.values()) == total
                            and {f['source_path'] for f in selected} == expected_paths):
                        block['statement_kind'] = 'expense'
                        block['selection'] = 'largest_category'
                lead, text = _readable(block)
                blocks.append(block)
                leads.append(lead)
                texts.append(text)
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        return None
    result = {'schema_version': SCHEMA, 'status': 'verified', 'workspace_id': workspace_id,
              'response_id': response_id, 'environment': 'simulation', 'fact_ids': list(fact_ids),
              'blocks': blocks, 'lead': leads[0], 'text': '\n\n'.join(texts)}
    result['content_hash'] = hashlib.sha256(json.dumps(result, ensure_ascii=False,
                                          sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return deepcopy(result)
