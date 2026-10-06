"""可复算的两套投资核算合同；权益内部转换不重复计收益。"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

COMPONENTS=('dividend_receivable_minor','redemption_receivable_minor','pending_subscription_minor','investment_cash_minor','payable_minor')
COMPLETENESS=('nav_complete','entitlements_complete','transactions_complete','corporate_actions_complete','fees_complete')


def value_minor(quantity,nav):
    q=Decimal(str(quantity))
    if not q.is_finite() or q<0:raise ValueError('数量无效')
    if q==0:return 0
    n=Decimal(str(nav))
    if not n.is_finite() or n<=0:raise ValueError('非零持仓必须有正净值')
    return int((q*n*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))


def equity(state):
    value=value_minor(state['quantity'],state.get('nav'))
    for k in COMPONENTS:
        if type(state.get(k)) is not int or state[k]<0:raise ValueError(k)
    return value+sum(state[k]for k in COMPONENTS[:-1])-state['payable_minor'],value


def unavailable(missing,basis=None):
    return {'status':'unavailable','accounting_basis':basis,'daily_pnl_minor':None,'cumulative_pnl_minor':None,'realized_pnl_minor':None,'missing':sorted(set(missing))}


def calculate(point):
    basis=point.get('accounting_basis')
    try:
        if basis=='total_equity_v2':
            missing=[k for k in COMPLETENESS if point.get(k) is not True]
            if missing:
                result=unavailable(missing,basis)
                # 时点权益与区间损益分别核验；首个净值日缺期初值不抹掉期末资产。
                if point.get('closing_nav_complete',point.get('nav_complete')) is True and all(point.get(k)is True for k in COMPLETENESS[1:]):
                    closing,cv=equity(point['closing'])
                    result.update(closing_equity_minor=closing,closing_value_minor=cv,
                        unrealized_pnl_minor=cv-point['remaining_cost_minor'] if type(point.get('remaining_cost_minor'))is int else None)
                return result
            for k in ('owner_contribution_minor','owner_withdrawal_minor'):
                if type(point.get(k)) is not int or point[k]<0:raise ValueError(k)
            opening,ov=equity(point['opening']);closing,cv=equity(point['closing'])
            flow=point['owner_contribution_minor']-point['owner_withdrawal_minor'];pnl=closing-opening-flow
            cumulative=point.get('cumulative_pnl_minor')
            return {'status':'available','accounting_basis':basis,'method_version':'equity/2','opening_value_minor':ov,'closing_value_minor':cv,'opening_equity_minor':opening,'closing_equity_minor':closing,'owner_net_flow_minor':flow,'daily_pnl_minor':pnl,'cumulative_pnl_minor':cumulative if type(cumulative)is int else None,'realized_pnl_minor':point.get('realized_net_pnl_minor'),'unrealized_pnl_minor':cv-point['remaining_cost_minor'] if type(point.get('remaining_cost_minor'))is int else None,'equation':'期末权益 − 期初权益 − 所有者净投入','cash_settlement_notice':'应收、待确认权益与投资现金只发生内部转换；费用减少权益，不列本金提取。','cost_rule':point.get('cost_rule','成本分配未知'),'missing':[]}
        if basis!='holdings_only_v1':return unavailable(['必须显式选择 accounting_basis'],basis)
        required=('opening_quantity','opening_nav','closing_quantity','closing_nav','buy_minor','sale_minor','dividend_minor','fee_minor','movements_complete')
        missing=[k for k in required if point.get(k)is None]
        if point.get('movements_complete')is not True:missing.append('完整交易/分红/费用记录')
        if missing:return unavailable(missing,basis)
        opening=value_minor(point['opening_quantity'],point['opening_nav']);closing=value_minor(point['closing_quantity'],point['closing_nav'])
        for k in ('buy_minor','sale_minor','dividend_minor','fee_minor'):
            if type(point[k])is not int or point[k]<0:raise ValueError(k)
        realized=point['sale_minor']-point['allocated_sale_cost_minor'] if type(point.get('allocated_sale_cost_minor'))is int else None
        fields=('remaining_cost_minor','cumulative_realized_minor','cumulative_dividend_minor','cumulative_fee_minor')
        cumulative=closing-point['remaining_cost_minor']+point['cumulative_realized_minor']+point['cumulative_dividend_minor']-point['cumulative_fee_minor'] if all(type(point.get(k))is int for k in fields)else None
        return {'status':'available','accounting_basis':basis,'daily_pnl_minor':closing-opening-point['buy_minor']+point['sale_minor']+point['dividend_minor']-point['fee_minor'],'opening_value_minor':opening,'closing_value_minor':closing,'cumulative_pnl_minor':cumulative,'realized_pnl_minor':None,'realized_gross_pnl_minor':realized,'confirmed_dividend_minor':point['dividend_minor'],'settled_dividend_minor':point.get('settled_dividend_minor'),'cash_settlement_notice':'交易与分红按确认日，现金按到账日。已实现毛损益不含费用。','cost_rule':point.get('cost_rule','成本分配未知'),'missing':[]}
    except(KeyError,InvalidOperation,ValueError,TypeError,OverflowError):return unavailable(['估值、权益或交易字段无效'],basis)
