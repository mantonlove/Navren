"""本轮金融方法消费状态；不计算金额，不签发或授权任何操作。"""
from copy import deepcopy
import json

from simulated_bank import BankError

METHOD_TOOLS = frozenset({'accounting_report', 'returns_report', 'allocation_preview'})
REPORT_TOOLS = METHOD_TOOLS | {'funding_report'}
REPORT_LIMIT = 64
VERSION_LIMIT = 128

# Maintained reference branches describe only their own method workflow. These
# are descriptor subsets of the parent skill, never permissions or query routes.
REFERENCE_TOOLS = {
    ('financial-diagnosis','formal-accounting.md'): {'load_skill','account_summary','accounting_report','answer_reviewed'},
    ('portfolio-review','formal-returns.md'): {'load_skill','account_summary','wealth_versions','returns_report','answer_reviewed'},
    ('goal-planning','formal-allocation.md'): {'load_skill','funding_report','allocation_preview','answer_reviewed'},
}


def reference_tools(data):
    selected=REFERENCE_TOOLS.get((data['skill'],data.get('reference')))
    return sorted(selected.intersection(data['tools'])) if selected else data['tools']


def query_key(name, arguments):
    return name + ':' + json.dumps({k: v for k, v in arguments.items() if k != 'expected_versions'},
                                  sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def funding_kind(metric):
    """固定正式公式分支；kind不接受模型选择，保留原件全部假设。"""
    formula = metric.get('formula_id')
    if formula == 'source.terminal_snapshot/1':
        return 'recorded_fact'
    if formula == 'funds.planning_budget/1':
        return 'planning_scenario'
    if formula in ('funds.daily_minimum_candidates/1', 'funds.closing_cash_candidates/1'):
        return 'candidate_scenario'
    if formula not in ('funds.balance/1', 'funds.available/1', 'funds.current_budget/1',
                       'funds.daily_minimum/1', 'funds.closing_cash/1'):
        raise BankError('METRIC_BINDING_REQUIRED', '资金报告含未知指标方法。', 409)
    return 'recorded_fact' if formula in ('funds.balance/1','funds.available/1','funds.current_budget/1') else 'recorded_projection'


def bounded_report(value):
    """整份有界交付；不截取列表或将剩余记录声称完整。"""
    def valid(item):
        if isinstance(item, list):
            return len(item) <= REPORT_LIMIT and all(valid(v) for v in item)
        if isinstance(item, dict):
            return len(item) <= VERSION_LIMIT and all(isinstance(k, str) and valid(v) for k, v in item.items())
        if isinstance(item, str):
            return len(item) <= 4000
        return item is None or type(item) in (bool, int)
    return valid(value) and len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= 524288


class FinanceReportState:
    def __init__(self):
        self.scopes, self.portfolios, self.constraints, self.budgets, self.product_refs = set(), set(), set(), set(), set()
        self.pins = {}

    def observe(self, gateway, workspace, name, data):
        context = {}
        if name in ('account_summary', 'funding_report'):
            accounts = data.get('accounts', []) if name == 'account_summary' else gateway.overview(workspace)['accounts']
            self.scopes.update([workspace, *(a['id'] for a in accounts)])
            context['scopes'] = sorted(self.scopes)
            context['workspace_scope'] = workspace
        if name in ('wealth_versions', 'funding_report'):
            versions = data if name == 'wealth_versions' else gateway.wealth.versions(workspace)
            self.portfolios.update(row['id'] for row in versions.get('versions', []) if isinstance(row.get('id'), str))
            context['portfolio_refs'] = sorted(self.portfolios)
        if name == 'funding_report' or (name == 'customer_records' and data.get('domain') == 'goals'):
            actual = gateway.goals.constraints(workspace)
            self.constraints.add('goals/current')
            self.constraints.update('goals/' + row['goal_id'] for row in actual['goals'])
            context['constraints_refs'] = sorted(self.constraints)
        if name == 'funding_report':
            self.budgets.update(m['metric_id'] for m in data['metrics']
                if m['formula_id'] == 'funds.current_budget/1' and m['basis'] == 'floor_surplus')
            context['budget_metric_ids'] = sorted(self.budgets)
        return context

    def check(self, name, args):
        if args['scope'] not in self.scopes:
            raise BankError('OBJECT_NOT_OBSERVED', '请先读取当前账户或正式资金报告中的范围。', 422)
        if name == 'returns_report' and args['portfolio_ref'] not in self.portfolios:
            raise BankError('OBJECT_NOT_OBSERVED', '请先读取本人的组合版本。', 422)
        if name == 'allocation_preview':
            if args['budget_metric_id'] not in self.budgets or args['constraints_ref'] not in self.constraints:
                raise BankError('METRIC_BUDGET_NOT_OBSERVED', '请先读取正式资金预算和本人目标目录。', 409)
            if not set(args.get('product_evidence_refs', [])) <= self.product_refs:
                raise BankError('OBJECT_NOT_OBSERVED', '产品来源必须来自本轮已读取证据。', 422)
        pin = self.pins.get(query_key(name, args))
        if args['expected_versions'] != (pin if pin is not None else {}):
            raise BankError('METRIC_VERSION_PIN_REQUIRED', '复读必须复制本轮完整来源版本；不能清空或只传部分版本。', 409)

    def pin(self, name, args, batch):
        self.pins.setdefault(query_key(name, args), deepcopy(batch['source_versions']))

    def identifiers(self):
        return {'scope': self.scopes, 'portfolio_ref': self.portfolios,
                'constraints_ref': self.constraints, 'budget_metric_id': self.budgets,
                'product_evidence_ref': self.product_refs}
