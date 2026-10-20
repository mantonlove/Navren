"""同源账单的本地分类筛选与有界明细；笔数和合计在分页前计算。"""
import re

from simulated_bank import BankError


KINDS = ('expense', 'income', 'refund', 'transfer', 'investment_purchase',
         'investment_redemption', 'loan_repayment')


def parameters(values):
    allowed = {'month', 'category', 'kind', 'currency', 'offset', 'limit'}
    if not isinstance(values, dict) or set(values) - allowed or 'month' not in values:
        raise BankError('INVALID_CAPABILITY_ARGUMENTS', '账单明细查询参数无效。')
    month = values['month']
    if not isinstance(month, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
        raise BankError('INVALID_MONTH', '账单明细需要有效的YYYY-MM月份。')
    category = values.get('category')
    if category is not None and (not isinstance(category, str) or not category.strip()
                                 or len(category) > 80):
        raise BankError('INVALID_CATEGORY', '账单类别无效。')
    kind, currency = values.get('kind', 'expense'), values.get('currency', 'CNY')
    if kind not in KINDS or currency not in ('CNY', 'USD', 'EUR', 'GBP', 'JPY'):
        raise BankError('INVALID_CAPABILITY_ARGUMENTS', '账单类型或币种无效。')
    offset, limit = values.get('offset', 0), values.get('limit', 6)
    if type(offset) is not int or not 0 <= offset <= 200000 or type(limit) is not int or not 1 <= limit <= 12:
        raise BankError('INVALID_PAGINATION', '账单明细每页为1至12条。')
    return {'month': month, 'category': category.strip() if category else None,
            'kind': kind, 'currency': currency, 'offset': offset, 'limit': limit}


def query(gateway, workspace, values):
    args = parameters(values)
    source = gateway.source(workspace)
    rows = gateway.transactions(workspace, args['month'])
    selected = []
    for row in rows:
        item = row
        # 三年账本费用以expense_minor为准；贷款利息可下钻，本金不计消费。
        if row.get('historical_import') and type(row.get('expense_minor')) is int:
            expense = row['expense_minor']
            if args['kind'] == 'expense' and expense > 0:
                item = {**row, 'kind': 'expense', 'amount_minor': expense}
            elif args['kind'] == 'refund' and expense < 0:
                item = {**row, 'kind': 'refund', 'amount_minor': -expense}
        if args['kind'] == 'income' and row.get('kind') == 'investment_redemption':
            from .history import verified_runtime_redemption
            redemption = verified_runtime_redemption(gateway.bank, workspace, row)
            gain = redemption.get('realized_return_minor')
            if type(gain) is not int:
                raise BankError('STATEMENT_GAIN_UNAVAILABLE', '已实现投资损益缺少可核对依据。', 503)
            if gain == 0:
                continue
            item = {**redemption, 'kind': 'income', 'category': '已实现投资损益', 'amount_minor': gain}
        if (item.get('kind') == args['kind'] and item.get('currency', 'CNY') == args['currency']
                and (args['category'] is None or item.get('category') == args['category'])):
            selected.append(item)
    selected.sort(key=lambda row: (row['occurred_at'], row['id']))
    if any(type(row.get('amount_minor')) is not int for row in selected):
        raise BankError('STATEMENT_AMOUNT_INVALID', '账单明细金额无法核对。', 503)
    end = args['offset'] + args['limit']
    items = [{'id': row['id'], 'account_id': row.get('account_id'),
              'occurred_at': row['occurred_at'], 'name': row['occurred_at'][:10],
              'category': row.get('category'), 'kind': row['kind'],
              'counterparty': row.get('counterparty') or row.get('merchant'),
              'amount_minor': row['amount_minor'], 'currency': args['currency']}
             for row in selected[args['offset']:end]]
    return {**args, 'source': source['active_source'], 'month': args['month'],
            'name': args['month'] + ' · ' + (args['category'] or args['kind']),
            'transaction_count': len(selected),
            'total_minor': sum(row['amount_minor'] for row in selected), 'items': items,
            'has_more': end < len(selected), 'next_offset': end if end < len(selected) else None,
            'source_version': source['sources'][source['active_source']]['source_version'],
            'notice': '笔数及合计包含筛选后的全部记录；明细仅当前页，同币种计算。'}
