"""持仓本金的账本币种；历史头寸缺产品条款时使用其绑定账户。"""
from simulated_bank import BankError


def position_currency(workspace, position, accounts, products):
    product = products.get(position.get('product_id'))
    if not product or product.get('workspace_id', workspace) != workspace:
        raise BankError('POSITION_CURRENCY_UNAVAILABLE', '持仓的产品来源暂时无法核对。', 503)
    currency = product.get('currency')
    if isinstance(currency, str) and len(currency) == 3 and currency.isascii() and currency.isalpha() and currency.isupper():
        return currency
    if currency in (None, '') and product.get('historical_synthetic_position') is True and product.get('execution_enabled') is False:
        account = accounts.get(position.get('account_id'))
        if account and account.get('workspace_id', workspace) == workspace:
            currency = account.get('currency')
            if isinstance(currency, str) and len(currency) == 3 and currency.isascii() and currency.isalpha() and currency.isupper():
                return currency
    raise BankError('POSITION_CURRENCY_UNAVAILABLE', '持仓本金的账本币种暂时无法核对。', 503)
