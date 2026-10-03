"""供Agent接入的确定性工具，故意不暴露用户确认及验证码接口。"""
from .service import BankError


class BankTools:
    def __init__(self, bank, agent_token):
        self.bank = bank
        self.workspace, role = bank.session(agent_token)
        if role!='agent':
            raise BankError('AGENT_TOKEN_REQUIRED','Agent工具必须绑定专用Agent凭证。',403)

    def query_snapshot(self, month='2026-09'):
        return self.bank.snapshot(self.workspace,month)

    def query_mandates(self):
        return self.bank.mandates(self.workspace)

    def query_transactions(self, month=None):
        return self.bank.transactions(self.workspace,month)

    def query_holdings(self):
        return self.bank.holdings(self.workspace)

    def query_customer_archive(self):
        return self.bank.customer_archive(self.workspace)

    def query_annual_analysis(self, year):
        return self.bank.annual_analysis(self.workspace,year)

    def compare_products(self, product_ids):
        return self.bank.products(self.workspace,product_ids)

    def prepare_operation(self, payload):
        return self.bank.prepare(self.workspace,payload)

    def query_operation(self, operation_id):
        return self.bank.operation(self.workspace,operation_id)

    def cancel_operation(self, operation_id):
        return self.bank.cancel(self.workspace,operation_id)
