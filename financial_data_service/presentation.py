"""同一只读快照中的前端聚合；不读取或合并现行执行账本。"""
from __future__ import annotations

import calendar
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
import json
import re
import sqlite3
from urllib.parse import urlencode

from .schema import START, END
from .service import FinancialDataError, _rows

PRESENTATION_ARGUMENTS = {'view', 'month', 'account_id', 'start_date', 'end_date'}
VIEWS = {'overview': {'view'}, 'month_report': {'view', 'month', 'account_id'},
         'calendar': {'view', 'start_date', 'end_date'}}
MAX_CALENDAR_DAYS = 92
MAX_EVENTS = 200
MAX_RESPONSE_BYTES = 256 * 1024
TOTAL_KEYS = ('income_minor', 'expense_minor', 'refund_minor', 'net_expense_minor',
              'net_cash_change_minor', 'net_account_change_minor',
              'investment_purchase_minor', 'investment_redemption_minor',
              'loan_repayment_minor', 'loan_principal_repayment_minor',
              'internal_transfer_in_minor', 'internal_transfer_out_minor')


def _days(start, end):
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    while day <= last:
        yield day.isoformat()
        if day == last:
            break
        day += timedelta(days=1)


def _empty_totals():
    return dict.fromkeys(TOTAL_KEYS, 0)


def _accumulate(total, row):
    signed, expense, kind = row['signed_minor'], row['expense_minor'], row['kind']
    total['expense_minor'] += max(0, expense)
    total['refund_minor'] += -min(0, expense)
    total['net_expense_minor'] += expense
    total['net_account_change_minor'] += signed
    if row['account_type'] != 'credit_card':
        total['net_cash_change_minor'] += signed
    if kind == 'income':
        total['income_minor'] += signed
    elif kind == 'investment_purchase':
        total['investment_purchase_minor'] += row['amount_minor']
    elif kind == 'investment_redemption':
        total['investment_redemption_minor'] += row['amount_minor']
    elif kind == 'loan_repayment':
        total['loan_repayment_minor'] += row['amount_minor']
        total['loan_principal_repayment_minor'] += row['amount_minor'] - max(0, expense)
    elif kind == 'internal_transfer':
        total['internal_transfer_in_minor' if signed > 0 else 'internal_transfer_out_minor'] += abs(signed)


class FinancialPresentationService:
    def __init__(self, service):
        self.service = service
        self.start_date, self.end_date = service.start_date, service.end_date

    def query(self, subject_id, arguments):
        service, as_of = self.service, self.end_date
        try:
            if not isinstance(subject_id, str):
                raise FinancialDataError('SUBJECT_INVALID', '读取主体无效。')
            if type(arguments) is not dict or set(arguments) - PRESENTATION_ARGUMENTS:
                raise FinancialDataError('INVALID_ARGUMENTS', '展示参数不符合合同。')
            view = arguments.get('view')
            if not isinstance(view, str) or view not in VIEWS or set(arguments) - VIEWS[view]:
                raise FinancialDataError('INVALID_VIEW', '展示视图或其参数无效。')
            args = dict(arguments)
            if any(not isinstance(v, str) or not v for v in args.values()):
                raise FinancialDataError('INVALID_ARGUMENTS', '展示参数必须为非空字符串。')
            with closing(service._connect()) as db:
                db.execute('BEGIN')
                if not db.execute('SELECT 1 FROM subjects WHERE subject_id=?', (subject_id,)).fetchone():
                    raise FinancialDataError('SUBJECT_NOT_FOUND', '读取主体未登记。')
                if 'account_id' in args:
                    service._owned(db, subject_id, 'accounts', 'account_id', args['account_id'])
                if view == 'overview':
                    data = self._overview(db, subject_id)
                elif view == 'month_report':
                    data = self._month_report(db, subject_id, args)
                    as_of = data['period']['end_date']
                else:
                    data = self._calendar(db, subject_id, args)
                if service._signature() != service._stat:
                    raise FinancialDataError('DATASET_CHANGED', '读取期间财务库已更新。')
                result = {**service._envelope(subject_id, as_of, data),
                          'presentation_version': 'navren.financial.presentation.v1'}
                if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESPONSE_BYTES:
                    raise FinancialDataError('RESULT_TOO_LARGE', '展示响应超过有界读取上限。')
                return result
        except FinancialDataError as error:
            return {**service._envelope(subject_id, as_of, None),
                    'error': {'code': error.code, 'message': error.message}}
        except (sqlite3.Error, OSError):
            return {**service._envelope(subject_id, as_of, None),
                    'error': {'code': 'DATABASE_READ_FAILED', 'message': '独立财务展示读取失败。'}}

    def _list(self, db, subject, table):
        # table只来自本模块固定调用，不能由请求指定。
        items = _rows(db.execute('SELECT * FROM ' + table + ' WHERE subject_id=? ORDER BY rowid LIMIT 201', (subject,)))
        if len(items) > 200:
            raise FinancialDataError('RESULT_TOO_LARGE', '实体索引超过读取上限。')
        return items

    def _overview(self, db, subject):
        service = self.service
        data = {'view': 'overview', 'summary': service._query(db, subject, 'get_financial_summary', {}, self.end_date)}
        for key, op in [('connections', 'list_connections'), ('accounts', 'list_accounts'),
                        ('cards', 'list_cards'), ('positions', 'list_positions'),
                        ('assets', 'list_assets'), ('obligations', 'list_obligations')]:
            data[key] = service._query(db, subject, op, {}, self.end_date)['items']
        data['policies'] = service._query(db, subject, 'list_insurance', {'status': 'active'}, self.end_date)['items']
        data['contracts'] = [r for r in self._list(db, subject, 'contracts')
                             if r['start_date'] <= self.end_date and (r['end_date'] is None or r['end_date'] >= self.end_date)
                             and r['status'] not in ('cancelled', 'expired', 'closed')]
        data['credit_facilities'] = self._list(db, subject, 'credit_facilities')
        for account in data['accounts']:
            ident = account['account_id']
            account['card_ids'] = [r['card_id'] for r in data['cards'] if r['account_id'] == ident]
            account['contract_ids'] = [r['contract_id'] for r in data['contracts'] if r['account_id'] == ident]
            account['credit_facility_ids'] = [r['facility_id'] for r in data['credit_facilities'] if r['account_id'] == ident]
        values = {r['asset_id']: dict(r) for r in db.execute('SELECT asset_id,book_value_minor,cost_basis_minor FROM valuations WHERE subject_id=? AND date=?', (subject, self.end_date))}
        for asset in data['assets']:
            value = values.get(asset['asset_id'], {})
            asset.update(book_value_minor=value.get('book_value_minor'), cost_basis_minor=value.get('cost_basis_minor'),
                         valuation_date=self.end_date if value else None, valuation_source='synthetic_not_real_NAV')
        data['entity_counts'] = {key: len(data[key]) for key in ('connections', 'accounts', 'cards', 'contracts', 'credit_facilities', 'positions', 'policies', 'assets', 'obligations')}
        data['policy_scope'] = 'active_as_of_dataset_end'
        data['cards_include_historical'] = True
        return data

    def _transactions(self, db, subject, start, end, account_id=None):
        clause, values = 't.subject_id=? AND t.booked_on>=? AND t.booked_on<=?', [subject, start, end]
        if account_id is not None:
            clause += ' AND t.account_id=?'
            values.append(account_id)
        # 最多92天，仅用于服务内聚合；逐笔原件仍走独立分页接口。
        return _rows(db.execute('SELECT t.*,a.account_type FROM transactions t JOIN accounts a USING(subject_id,account_id) WHERE ' + clause + ' ORDER BY t.booked_on,t.occurred_at,t.transaction_id', values))

    def _cash_series(self, db, subject, start, end, account_id=None):
        first = date.fromisoformat(start)
        previous = (first - timedelta(days=1)).isoformat() if first > date.min else start
        lower, upper = max(self.start_date, previous), min(self.end_date, end)
        accounts = self._list(db, subject, 'accounts')
        if account_id is not None:
            accounts = [r for r in accounts if r['account_id'] == account_id]
        types = {r['account_id']: r['account_type'] for r in accounts}
        balances = defaultdict(dict)
        if lower <= upper:
            clause, values = 'subject_id=? AND date>=? AND date<=?', [subject, lower, upper]
            if account_id is not None:
                clause += ' AND account_id=?'
                values.append(account_id)
            for row in db.execute('SELECT account_id,date,balance_minor FROM account_balances WHERE ' + clause, values):
                balances[row['date']][row['account_id']] = row['balance_minor']
        if start <= self.start_date <= end:
            balances[(date.fromisoformat(self.start_date) - timedelta(days=1)).isoformat()] = {r['account_id']: r['opening_balance_minor'] for r in accounts}
        cash = {}
        for day, values in balances.items():
            if set(values) != set(types):
                raise FinancialDataError('INCOMPLETE_COVERAGE', '账户日余额覆盖不完整。')
            cash[day] = sum(amount for ident, amount in values.items() if types[ident] != 'credit_card')
        return cash, balances

    def _daily(self, db, subject, start, end, transactions, account_id=None):
        cash, balances = self._cash_series(db, subject, start, end, account_id)
        grouped = defaultdict(list)
        for row in transactions:
            grouped[row['booked_on']].append(row)
        result = []
        for day in _days(start, end):
            covered = self.start_date <= day <= self.end_date
            rows = grouped[day]
            total = _empty_totals()
            for row in rows:
                _accumulate(total, row)
            if covered:
                prior = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
                if day not in cash or prior not in cash:
                    raise FinancialDataError('INCOMPLETE_COVERAGE', '日现金变化缺少期初或期末余额。')
                total['net_cash_change_minor'] = cash[day] - cash[prior]
            else:
                total = dict.fromkeys(TOTAL_KEYS, None)
            result.append({'date': day, **total, 'cash_minor': cash.get(day),
                           'account_balance_minor': balances.get(day, {}).get(account_id) if account_id else None,
                           'covered': covered, 'transaction_count': len(rows) if covered else None,
                           'realized_gain_minor': None,
                           'account_ids': sorted({r['account_id'] for r in rows}),
                           'card_ids': sorted({r['card_id'] for r in rows if r['card_id']}),
                           'contract_ids': sorted({r['contract_id'] for r in rows if r['contract_id']}),
                           'policy_ids': sorted({r['details']['policy_id'] for r in rows if r['details'].get('policy_id')})})
        return result

    def _snapshot(self, db, subject, day):
        if day < self.start_date:
            accounts = self._list(db, subject, 'accounts')
            result = {'cash_minor': sum(r['opening_balance_minor'] for r in accounts if r['account_type'] != 'credit_card'),
                      'credit_card_outstanding_minor': -sum(r['opening_balance_minor'] for r in accounts if r['account_type'] == 'credit_card')}
            result.update(dict.fromkeys(('investment_minor', 'real_assets_minor', 'loan_principal_minor',
                                         'total_assets_minor', 'liabilities_minor', 'net_worth_minor'), None))
            result.update(insurance_cash_value_minor=None, insurance_sum_excluded=True, credit_limits_excluded=True,
                          runtime_operations_included=False, snapshot_complete=False,
                          notice='覆盖首日前只有账户期初余额；其他期初估值未提供，不填成零。')
        else:
            result = self.service._query(db, subject, 'get_financial_summary', {}, day)
            result['snapshot_complete'] = True
        return {'date': day, 'scope': 'subject', 'timing': 'end_of_day', **result}

    def _month_report(self, db, subject, args):
        month = args.get('month')
        if not isinstance(month, str) or not re.fullmatch(r'\d{4}-\d{2}', month):
            raise FinancialDataError('INVALID_MONTH', 'month必须为真实的YYYY-MM。')
        try:
            first = date.fromisoformat(month + '-01')
        except ValueError:
            raise FinancialDataError('INVALID_MONTH', 'month必须为真实的YYYY-MM。') from None
        requested_start, requested_end = first.isoformat(), first.replace(day=calendar.monthrange(first.year, first.month)[1]).isoformat()
        start, end = max(self.start_date, requested_start), min(self.end_date, requested_end)
        if start > end:
            raise FinancialDataError('OUTSIDE_COVERAGE', '月份不在账户历史覆盖范围内。')
        account_id = args.get('account_id')
        rows = self._transactions(db, subject, start, end, account_id)
        daily = self._daily(db, subject, start, end, rows, account_id)
        total = _empty_totals()
        categories = {}
        for row in rows:
            _accumulate(total, row)
            if row['kind'] != 'internal_transfer':
                category = row['category']
                bucket = categories.setdefault(category, {'category': category, **_empty_totals(), 'transaction_count': 0})
                _accumulate(bucket, row)
                bucket['transaction_count'] += 1
        total['net_cash_change_minor'] = sum(row['net_cash_change_minor'] for row in daily)
        prior = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
        selected = None
        if account_id:
            account = self.service._query(db, subject, 'get_account_overview', {'account_id': account_id}, self.end_date)['account']
            _, balances = self._cash_series(db, subject, start, end, account_id)
            selected = {'account': account, 'opening_balance_minor': balances[prior][account_id],
                        'closing_balance_minor': balances[end][account_id],
                        'balance_change_minor': balances[end][account_id] - balances[prior][account_id]}
        query = {'start_date': start, 'end_date': end, 'limit': 50, 'offset': 0}
        if account_id:
            query['account_id'] = account_id
        return {'view': 'month_report', 'month': month, 'account_id': account_id,
                'period': {'requested_start_date': requested_start, 'requested_end_date': requested_end,
                           'start_date': start, 'end_date': end, 'coverage_partial': start != requested_start or end != requested_end},
                'snapshots_scope': 'subject', 'totals_scope': 'selected_account' if account_id else 'subject',
                'opening': self._snapshot(db, subject, prior), 'closing': self._snapshot(db, subject, end),
                'selected_account': selected, 'totals': total,
                'categories': sorted(categories.values(), key=lambda r: (-r['net_expense_minor'], r['category'])),
                'daily': daily, 'transaction_count': len(rows),
                'transaction_endpoint': '/api/finance/data/list_transactions?' + urlencode(query),
                'realized_gain_minor': None, 'currency': 'CNY',
                'accounting': {'expense_basis': 'expense_minor', 'refunds_deducted_once': True,
                               'internal_transfers_are_not_income_or_expense': True,
                               'loan_interest_in_expense_principal_separate': True,
                               'redemption_cash_is_not_income_or_verified_gain': True,
                               'credit_payment_cash_effect_is_not_second_expense': True,
                               'runtime_operations_included': False}}

    def _calendar(self, db, subject, args):
        if not {'start_date', 'end_date'} <= args.keys():
            raise FinancialDataError('INVALID_ARGUMENTS', '日历需要start_date与end_date。')
        start, end = self.service._day(args['start_date']), self.service._day(args['end_date'])
        if start > end or (date.fromisoformat(end) - date.fromisoformat(start)).days + 1 > MAX_CALENDAR_DAYS:
            raise FinancialDataError('INVALID_RANGE', '日历开始不得晚于结束，最多读取92天。')
        rows = self._transactions(db, subject, start, end)
        daily = self._daily(db, subject, start, end, rows)
        events = []
        for row in rows:
            if row['kind'] == 'insurance_premium':
                events.append({'event_id': row['transaction_id'], 'event_type': 'premium_paid', 'date': row['booked_on'],
                               'status': 'booked', 'amount_minor': row['amount_minor'], 'expense_minor': row['expense_minor'],
                               'account_id': row['account_id'], 'card_id': row['card_id'], 'contract_id': row['contract_id'],
                               'policy_id': row['details'].get('policy_id'), 'transaction_id': row['transaction_id'],
                               'label': '保险保费已入账', 'cash_effect_included_in_daily': True})
        obligations = _rows(db.execute('SELECT * FROM obligations WHERE subject_id=? AND due_on>=? AND due_on<=? ORDER BY due_on,obligation_id LIMIT 201', (subject, start, end)))
        statements = _rows(db.execute('SELECT * FROM card_statements WHERE subject_id=? AND due_on>=? AND due_on<=? ORDER BY due_on,statement_id LIMIT 201', (subject, start, end)))
        if len(obligations) > 200 or len(statements) > 200:
            raise FinancialDataError('RESULT_TOO_LARGE', '日历事件超过读取上限，请缩小日期范围。')
        for row in obligations:
            events.append({'event_id': row['obligation_id'], 'event_type': 'obligation', 'date': row['due_on'],
                           'status': row['status'], 'amount_minor': row['amount_minor'], 'expense_minor': None,
                           'account_id': row['account_id'], 'card_id': None, 'contract_id': row['contract_id'],
                           'policy_id': None, 'mandate_id': row['mandate_id'], 'transaction_id': None,
                           'label': row['category'], 'cash_effect_included_in_daily': False, 'details': row['details']})
        for row in statements:
            # 原obligation已表示同一未来账单，不能重复列为两次应付。
            if any(e['event_type'] == 'obligation' and e['date'] == row['due_on'] and e['label'] == '信用卡账单还款' for e in events):
                continue
            events.append({'event_id': row['statement_id'], 'event_type': 'credit_statement_due', 'date': row['due_on'],
                           'status': row['status'], 'amount_minor': row['statement_minor'], 'paid_minor': row['paid_minor'],
                           'expense_minor': 0, 'account_id': row['account_id'], 'card_id': None, 'contract_id': None,
                           'policy_id': None, 'transaction_id': None, 'label': '信用卡账单还款（不重复消费）',
                           'cash_effect_included_in_daily': False})
        policies = self._list(db, subject, 'policies')
        for policy in policies:
            for key, kind, label in [('start_date', 'coverage_start', '保障期间开始'), ('end_date', 'coverage_end', '保障期间结束')]:
                if start <= policy[key] <= end:
                    events.append({'event_id': policy['policy_id'] + ':' + kind, 'event_type': kind, 'date': policy[key],
                                   'status': 'contract_term', 'amount_minor': None, 'expense_minor': None,
                                   'account_id': policy['payment_account_id'], 'card_id': None,
                                   'contract_id': policy['contract_id'], 'policy_id': policy['policy_id'],
                                   'transaction_id': None, 'label': label, 'cash_effect_included_in_daily': False})
        events.sort(key=lambda r: (r['date'], r['event_type'], r['event_id']))
        total = len(events)
        events = events[:MAX_EVENTS]
        policy_ids = {e['policy_id'] for e in events if e['policy_id']}
        contract_ids = {e['contract_id'] for e in events if e['contract_id']}
        return {'view': 'calendar', 'period': {'start_date': start, 'end_date': end,
                'coverage_start': self.start_date, 'coverage_end': self.end_date, 'coverage_partial': start < self.start_date or end > self.end_date},
                'daily': daily, 'events': events, 'event_count': total,
                'events_truncated': total > MAX_EVENTS, 'event_limit': MAX_EVENTS,
                'accounts': self._list(db, subject, 'accounts'), 'cards': self._list(db, subject, 'cards'),
                'contracts': [r for r in self._list(db, subject, 'contracts') if r['contract_id'] in contract_ids],
                'policies': [r for r in policies if r['policy_id'] in policy_ids],
                'currency': 'CNY', 'realized_gain_minor': None, 'runtime_operations_included': False,
                'forecast_cash_applied': False, 'notice': '历史入账与待付预测分开；覆盖外日金额未知，保额/授信不计资产，未生成未来收益或续保扣款。'}
