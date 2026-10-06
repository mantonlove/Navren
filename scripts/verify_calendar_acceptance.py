"""在临时工作区生成可复算估值/金额证据，不修改应用当前账户。"""
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from finance_gateway import FinanceGateway
from wealth_returns import calculate


def run():
    out=Path('outputs/calendar-acceptance');out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='navren-calendar-') as tmp:
        g=FinanceGateway(Path(tmp)/'isolated.sqlite3');w,_=g.create_session();archive=g.bank.customer_archive(w)
        holds=archive['records']['holdings'];account=holds[0]['account_id']
        a={'accounting_basis':'holdings_only_v1','opening_quantity':'100','opening_nav':'10','closing_quantity':'110','closing_nav':'10.1',
           'buy_minor':20000,'sale_minor':10100,'dividend_minor':300,'fee_minor':50,'movements_complete':True,
           'allocated_sale_cost_minor':10000,'remaining_cost_minor':110000,'cumulative_realized_minor':100,
           'cumulative_dividend_minor':300,'cumulative_fee_minor':50,'settled_dividend_minor':300,
           'settled_dividend_on':'2026-10-03','cost_rule':'移动平均成本，交易金额不含费用','account_id':account}
        b={**a,'account_id':holds[1]['account_id'],'closing_nav':'10','dividend_minor':0,'sale_minor':9900,'fee_minor':250,
           'cumulative_realized_minor':-100,'cumulative_dividend_minor':0,'cumulative_fee_minor':250}
        incomplete={**a,'opening_nav':None,'settled_dividend_minor':0,'settled_dividend_on':None}
        samples=[{'dataset_id':'sample-a','product_id':holds[0]['product_id'],'expected_daily_pnl_minor':1450,'point':a},
                 {'dataset_id':'sample-b','product_id':holds[1]['product_id'],'expected_daily_pnl_minor':-350,'point':b},
                 {'dataset_id':'sample-incomplete','product_id':holds[0]['product_id'],'expected_daily_pnl_minor':None,'point':incomplete}]
        for sample in samples:
            actual=calculate(sample['point']);assert actual['daily_pnl_minor']==sample['expected_daily_pnl_minor']
            sample['calculation']=actual
        with g.bank.transaction() as db:
            for sample in samples:
                valuation_on='2026-10-04' if sample['dataset_id']=='sample-incomplete' else '2026-10-02'
                db.execute('INSERT INTO wealth_valuation_points VALUES(?,?,?,?,?,?,?,?)',
                    (w,sample['dataset_id'],sample['product_id'],valuation_on,'CNY','holdings_only_v1',1,json.dumps(sample['point'])))
        q=g.calendar.query(w,{'from':'2026-10-02','to':'2026-10-04','view':'day'})
        assert q['summary_by_currency'][0]['investment_pnl_minor'] is None
        assert q['summary_by_currency'][0]['known_investment_pnl_minor']==1100
        tx=g.bank.transactions(w,'2026-09');view=g.calendar.query(w,{'from':'2026-09-01','to':'2026-09-30'})
        expected={'recorded_inflow_minor':sum(max(r['delta_minor'],0) for r in tx),
                  'recorded_outflow_minor':sum(max(-r['delta_minor'],0) for r in tx),
                  'recorded_net_minor':sum(r['delta_minor'] for r in tx)}
        actual=view['summary_by_currency'][0];assert all(actual[k]==v for k,v in expected.items())
        out.joinpath('valuation-samples.json').write_text(json.dumps({'environment':'isolated_simulation','notice':'仅验证确定性算法和账户/持仓绑定，不是真实客户收益。','samples':samples,'view':q},ensure_ascii=False,indent=2))
        out.joinpath('independent-reconciliation.json').write_text(json.dumps({'month':'2026-09','record_count':len(tx),'expected':expected,'actual':{k:actual[k] for k in expected},'pass':True},ensure_ascii=False,indent=2))
        print('隔离样本与独立金额对账通过；未保存账户密钥或修改应用账户。')


if __name__=='__main__':run()
