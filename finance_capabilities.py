"""面向对话 Agent 的可发现、只读模拟银行查询能力目录。"""
from dataclasses import dataclass
import re

from simulated_bank import BankError


@dataclass(frozen=True)
class ReadCapability:
    capability_id: str
    description: str
    data_scope: str
    parameters: tuple[dict, ...]
    validate: object
    execute: object


# 六类业务使用同一目录管理入口、只读依据与可准备的草案类型。
# 这里只描述能力与权限；所有执行仍由已有单独确认/执行 API 完成。
WORKFLOW_CAPABILITIES = (
    {
        "id": "transfer",
        "name": "智能转账",
        "selector": r"转账|转给|汇给|给.{0,8}转|定时转账|每月.*转|按月.*转|AA收款|发起AA|登记.*收款",
        "read_capabilities": ("simulated_bank.account_snapshot",),
        "draft_actions": ("transfer", "schedule_transfer", "request_split", "collect_split_payment"),
    },
    {
        "id": "statements",
        "name": "账单分析",
        "selector": r"账单|流水|消费|支出|收入|月报|年报|年度|异常",
        "read_capabilities": ("statements.monthly_analysis", "statements.annual_analysis",
                              "statements.details", "statements.anomalies", "subscriptions.periodic_charges"),
        "draft_actions": (),
    },
    {
        "id": "wealth",
        "name": "理财操作与资金规划",
        "selector": r"理财|配置|申购|购买|买入|赎回|持仓|余额|资产|风险等级|风险画像|投资期限|应急金|保留资金|可投资|SIM-|模拟.*产品|现金流|资金时间轴|未来\s*\d{1,3}\s*(?:天|日).{0,10}(?:资金|余额)|(?:\d{1,3}\s*(?:天|日)(?:后|内)).{0,10}(?:资金|余额)",
        "read_capabilities": ("simulated_bank.account_snapshot", "budgets.investable",
                              "products.catalog", "cashflow.timeline"),
        "draft_actions": ("purchase", "redeem"),
    },
    {
        "id": "cards",
        "name": "卡片管理",
        "selector": r"挂失|提额|提高.*额度|调整.*额度|额度|开启.*交易|关闭.*交易|卡片|银行卡|信用卡|借记卡|申请补卡|办一张.*卡",
        "read_capabilities": ("simulated_bank.account_snapshot",),
        "draft_actions": ("report_card_lost", "request_card_limit", "card_control", "apply_card"),
    },
    {
        "id": "subscriptions",
        "name": "订阅与代扣",
        "selector": r"代扣|订阅|会员|周期扣费|退订",
        "read_capabilities": ("subscriptions.periodic_charges",),
        "draft_actions": ("close_mandate", "cancel_subscription"),
    },
    {
        "id": "cross_scene",
        "name": "跨场景联动",
        "selector": r"生日|礼物|鲜花|蛋糕|预约服务|预留.*(?:任务|预算|资金)|跨场景",
        "read_capabilities": ("simulated_bank.account_snapshot", "cashflow.timeline"),
        "draft_actions": ("reserve_funds", "book_services"),
    },
    {
        "id": "service_negotiation",
        "name": "用户利益代理与模拟银行服务协商",
        "selector": r"协商|服务优惠|费用减免|工资迁入.{0,12}(?:服务|优惠|费用)|(?:服务|优惠|费用).{0,12}工资迁入|工资.{0,36}(?:优惠|费用|服务)|(?:优惠|费用|服务).{0,36}工资",
        "read_capabilities": ("simulated_bank.account_snapshot",),
        "draft_actions": ("submit_simulated_service_application",),
        "kind": "differentiator",
    },
)


def is_account_balance_query(text):
    """识别口语化的个人账户余额问题，避免落入旧版理财推荐兜底。"""
    if not isinstance(text, str):
        return False
    patterns = (
        r'我.{0,8}(?:还有|有|还剩|剩下|能用|可用|能动用|可以动用).{0,8}(?:多少钱|多少(?:现金|资金|存款|余额|可用金额)?)',
        r'(?:账户|银行卡|卡里|账上|手头|手里).{0,8}(?:还有|有|还剩|剩下|可用|能用)?.{0,6}(?:多少钱|多少(?:现金|资金|存款|余额|可用金额)?)',
        r'我有多少(?:现金|资金|存款|余额|可用金额)',
        r'(?:现金|资金|存款|可用金额|余额)(?:还剩|剩下|还有|是|有)?多少',
        r'(?:多少钱|多少(?:现金|资金|存款|余额|可用金额)?).{0,6}(?:可以|能|可)投资',
    )
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def select_workflow_capabilities(text):
    if not isinstance(text, str):
        return ()
    selected = [item["id"] for item in WORKFLOW_CAPABILITIES
                if re.search(item["selector"], text, re.I)]
    if is_account_balance_query(text) and "wealth" not in selected:
        selected.append("wealth")
    return tuple(selected)


def workflow_capability_catalog():
    return [{"id": item["id"], "name": item["name"],
             "kind": item.get("kind", "required_business_capability"),
             "read_capabilities": list(item["read_capabilities"]),
             "draft_actions": list(item["draft_actions"]),
             "execution_requires_user_confirmation": True}
            for item in WORKFLOW_CAPABILITIES]


class ReadCapabilityRegistry:
    """只注册读取操作；目录执行器不接受任意方法名或函数参数。"""

    def __init__(self):
        self._capabilities = {}

    def register(self, capability):
        if not isinstance(capability, ReadCapability) or not capability.capability_id:
            raise ValueError('只读能力注册项无效。')
        if capability.capability_id in self._capabilities:
            raise ValueError('只读能力标识重复。')
        self._capabilities[capability.capability_id] = capability

    def catalog(self):
        return [
            {'id': item.capability_id, 'description': item.description,
             'data_scope': item.data_scope, 'parameters': list(item.parameters),
             'read_only': True}
            for item in self._capabilities.values()
        ]

    def invoke(self, workspace_id, capability_id, parameters=None):
        capability = self._capabilities.get(capability_id)
        if capability is None:
            raise BankError('CAPABILITY_NOT_FOUND', '没有登记该银行查询能力。', 404)
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise BankError('INVALID_CAPABILITY_ARGUMENTS', '查询参数必须为对象。')
        try:
            checked = capability.validate(parameters)
        except BankError:
            raise
        except (TypeError, ValueError):
            raise BankError('INVALID_CAPABILITY_ARGUMENTS', '查询参数无效。')
        return capability.execute(workspace_id, checked)


def finance_read_capabilities(gateway):
    """把现有 FinanceGateway 查询端口登记为统一、严格校验的只读工具。"""
    registry = ReadCapabilityRegistry()
    from base_runtime.statement_details import parameters as detail_parameters

    def no_parameters(values):
        if values:
            raise ValueError('该能力不接受参数。')
        return {}

    def required_parameters(required, optional=()):
        allowed = set(required) | set(optional)

        def validate(values):
            if set(values) - allowed or not set(required).issubset(values):
                raise ValueError('参数字段不匹配。')
            return values
        return validate

    def statement_month(values):
        checked = required_parameters(('month',))(values)
        if not isinstance(checked['month'], str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', checked['month']):
            raise ValueError('月份格式无效。')
        return checked

    def statement_year(values):
        checked = required_parameters(('year',))(values)
        if type(checked['year']) is not int or not 2016 <= checked['year'] <= 2100:
            raise ValueError('年份无效。')
        return checked

    def budget_parameters(values):
        checked = required_parameters(('account_id',), ('reserve_floor_minor',))(values)
        if not isinstance(checked['account_id'], str) or not checked['account_id']:
            raise ValueError('账户标识无效。')
        floor = checked.get('reserve_floor_minor', 0)
        if type(floor) is not int or floor < 0:
            raise ValueError('预留金额无效。')
        return {'account_id': checked['account_id'], 'reserve_floor_minor': floor}

    def timeline_parameters(values):
        checked = required_parameters((), ('days',))(values)
        days = checked.get('days', 90)
        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError('时间范围无效。')
        return {'days': days}

    specs = (
        ReadCapability('simulated_bank.account_snapshot', '读取当前模拟账户摘要及其只读资金时间轴。',
                       'simulated_bank.account_summary', (), no_parameters,
                       lambda workspace, _: gateway.overview(workspace)),
        ReadCapability('simulated_bank.statement_source', '读取当前账单分析数据来源及覆盖信息。',
                       'statement.source_metadata', (), no_parameters,
                       lambda workspace, _: gateway.source(workspace)),
        ReadCapability('statements.monthly_analysis', '读取指定月份的账单汇总。',
                       'statements.monthly_summary', ({'name': 'month', 'type': 'YYYY-MM', 'required': True},),
                       statement_month,
                       lambda workspace, params: gateway.analysis(workspace, params['month'])),
        ReadCapability('statements.annual_analysis', '读取指定年度的账单汇总及覆盖月份。',
                       'statements.annual_summary', ({'name': 'year', 'type': 'integer', 'required': True},),
                       statement_year,
                       lambda workspace, params: gateway.annual_analysis(workspace, params['year'])),
        ReadCapability('statements.details', '按月及类别读取同源流水明细、完整笔数和合计。',
                       'statements.filtered_details',
                       ({'name': 'month', 'type': 'YYYY-MM', 'required': True},
                        {'name': 'category', 'type': 'string', 'required': False},
                        {'name': 'kind', 'type': 'string', 'required': False},
                        {'name': 'currency', 'type': 'string', 'required': False},
                        {'name': 'offset', 'type': 'integer', 'required': False},
                        {'name': 'limit', 'type': 'integer', 'required': False}),
                       detail_parameters,
                       lambda workspace, params: gateway.statement_details(workspace, **params)),
        ReadCapability('statements.anomalies', '读取流水规则标记的异常候选。',
                       'statements.anomaly_candidates', (), no_parameters,
                       lambda workspace, _: gateway.anomalies(workspace)),
        ReadCapability('subscriptions.periodic_charges', '读取周期扣费识别结果。',
                       'statements.periodic_charge_candidates', (), no_parameters,
                       lambda workspace, _: gateway.periodic_charges(workspace)),
        ReadCapability('products.catalog', '读取模拟理财产品目录和确定性适配结果。',
                       'simulated_bank.products', (), no_parameters,
                       lambda workspace, _: gateway.bank.products(workspace)),
        ReadCapability('budgets.investable', '按账户读取已有规则计算的可投资金额。',
                       'simulated_bank.account_summary',
                       ({'name': 'account_id', 'type': 'string', 'required': True},
                        {'name': 'reserve_floor_minor', 'type': 'integer', 'required': False, 'default': 0}),
                       budget_parameters,
                       lambda workspace, params: gateway.budget(workspace, params['account_id'],
                                                                 params['reserve_floor_minor'])),
        ReadCapability('cashflow.timeline', '读取指定天数的分币种现金流推演。',
                       'simulated_bank.cashflow_timeline',
                       ({'name': 'days', 'type': 'integer', 'required': False, 'default': 90},),
                       timeline_parameters,
                       lambda workspace, params: gateway.cashflow_timeline(workspace, params['days'])),
        ReadCapability('financial_metrics.snapshot', '读取按版本绑定、可独立重算的资金指标；预测和确认分支分别列出。',
                       'financial_metrics.versioned_snapshot',
                       ({'name':'days','type':'integer','required':False,'default':60},),
                       timeline_parameters,
                       lambda workspace,params:gateway.financial_metrics.snapshot(workspace,params['days'])),
    )
    for spec in specs:
        registry.register(spec)
    return registry
