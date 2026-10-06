"""输出脱敏核算案例，所有分为整数；不读取任何客户账本。"""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from wealth_portfolio import state,convert
from wealth_returns import calculate,COMPONENTS,COMPLETENESS,value_minor

def point(a,b,contribution=0,withdrawal=0):return {'accounting_basis':'total_equity_v2','opening':a,'closing':b,'owner_contribution_minor':contribution,'owner_withdrawal_minor':withdrawal,**{k:True for k in COMPLETENESS},'remaining_cost_minor':None,'cost_rule':'教育案例，未设置累计成本'}
a=state(100,1);cases=[]
def add(name,p,expected):
 r=calculate(p);assert r['daily_pnl_minor']==expected;cases.append({'name':name,'input':p,'expected_daily_pnl_minor':expected,'output':r})
# 分红权益先计提，到账/再投不重复确认为第二次收益。
b={**state(100,.98),'dividend_receivable_minor':400};add('跨净值日计提现金分红',point(a,b),200)
c={**state(100,.98),'investment_cash_minor':400};add('分红应收到投资现金',point(b,c),0)
d=state('104.0816326530612245',.98);add('分红内部再投资',point(b,d),0)
exdiv={**state(100,'.9'),'dividend_receivable_minor':1000};add('合同样例：除息加应收保持100元权益',point(a,exdiv),0)
add('合同样例：同日红利再投保持100元权益',point(exdiv,state('111.111111','.9')),0)
add('合同样例：次日净值0.92且10元待再投',point(exdiv,{**state(100,'.92'),'pending_subscription_minor':1000}),200)
e={**state(0,None),'redemption_receivable_minor':10500};add('赎回应收仍为权益',point(a,e),500);add('赎回结算提取',point(e,state(0,None),withdrawal=10500),0)
f={**a,'pending_subscription_minor':1000};add('外部投入待确认申购',point(a,f,contribution=1000),0);add('申购确认内部转换',point(f,state(110,1)),0)
add('费用减少权益',point(a,state(99,1)),-100)
g={**a,'payable_minor':100};add('外部投入代付应付费用',point(g,a,contribution=100),0)
qty,cash=convert(10000,'3.6834');conversion={'old_equity_minor':10000,'quantity':qty,'nav':'3.6834','new_value_minor':value_minor(qty,'3.6834'),'residual_cash_minor':cash};assert conversion['new_value_minor']+cash==10000
household={'opening_cash_minor':1000000,'income_minor':600000,'consumption_minor':200000,'subscription_principal_minor':100000,'loan_inflow_minor':300000,'daily_surplus_minor':400000,'closing_cash_minor':1600000,'holding_minor':100000,'liability_minor':300000,'net_assets_minor':1400000}
assert household['closing_cash_minor']==household['opening_cash_minor']+household['income_minor']-household['consumption_minor']-household['subscription_principal_minor']+household['loan_inflow_minor']
assert household['net_assets_minor']==household['closing_cash_minor']+household['holding_minor']-household['liability_minor']
Path('output/equity/calculation_examples.json').write_text(json.dumps({'cases':cases,'conversion':conversion,'household':household},ensure_ascii=False,indent=2));print('通过',len(cases),'项权益复算，以及转换和家庭口径对账')
