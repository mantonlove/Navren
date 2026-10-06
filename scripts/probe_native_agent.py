#!/usr/bin/env python3
"""隔离合成账户上的真实模型冒烟；不触碰用户当前模拟账户。"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_runtime.conversation import ConversationAgent
from agent_runtime.native_client import AIPingToolClient
from agent_runtime.bank_tools import safe_data
from finance_gateway import FinanceGateway
from simulated_bank import BankError


class TracingClient(AIPingToolClient):
    def __init__(self):super().__init__();self.calls=[]
    def complete(self,*args,**kwargs):
        message,usage=super().complete(*args,**kwargs)
        self.calls.extend({'name':c['function']['name'],
                           'arguments':safe_data(json.loads(c['function']['arguments']))}
                          for c in message.get('tool_calls',[]))
        return message,usage


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--limit',type=int,default=12)
    parser.add_argument('--cases',default='')
    args=parser.parse_args()
    cases = [
        ('balance','我还有多少钱'),
        ('cash_synonym','手上还能拿出多少'),
        ('month','9月钱都花哪了，哪项最高？'),
        ('cashflow','房租交完以后还够吗？看一下未来30天'),
        ('product','比较模拟活钱和30天产品，提前取出来有什么影响？'),
        ('transfer','转给妈妈1000元'),
        ('edit','把刚刚那个金额改成两千'),
        ('withdraw','这笔先不要转了，撤回刚才的草案'),
        ('negation','不要买任何产品，只告诉我余额'),
        ('card','帮我关闭借记卡尾号1001的线上交易'),
        ('subscription','取消视频会员订阅，银行代扣也会一起关闭吗？'),
        ('composite','给妈妈转1000元，剩下的钱再买模拟活钱产品'),
    ]
    if args.cases:
        selected=set(args.cases.split(','));cases=[case for case in cases if case[0] in selected]
    cases=cases[:args.limit]
    records=[]
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='navren-agent-probe-') as p:
        g=FinanceGateway(Path(p)/'bank.sqlite3');w,token=g.create_session()
        client=TracingClient();agent=ConversationAgent(primary=client)
        for name,question in cases:
            before=g.overview(w)
            client.calls=[]
            r=agent.process(g,w,question)
            after=g.overview(w)
            row={'case':name,'question':question,'reply':r['reply'],
                 'answer_contract':r.get('answer_contract'),'review':r.get('agent_review'),
                 'runtime':r.get('agent_runtime'),'actions':r.get('bank_actions'),
                 'model_tool_calls':client.calls,
                 'cash_before_minor':before['total_cash_minor'],'cash_after_minor':after['total_cash_minor'],
                 'no_financial_execution':before['version']==after['version'] and before['total_cash_minor']==after['total_cash_minor'],
                 'functional_success':not r.get('isFallback',False)}
            # 读取已失效旧草案的执行阻断，不执行任何有效资金草案。
            if name=='edit' and len(records)>0 and records[-1].get('actions') and row['actions']:
                old_id=records[-1]['actions'][0]['operation_id']
                view=g.operation_view(w,old_id)
                try:
                    g.confirm(w,old_id,{'confirmed':True,'confirmation_digest':view['operation']['confirmation_digest'],
                                      'verification_code':g.demo_code(token)})
                    row['old_draft_blocked']=False
                except BankError as e:
                    row['old_draft_blocked']=True;row['old_draft_error']=e.code
            if name in ('transfer','edit','card','composite'):
                row['functional_success'] = row['functional_success'] and len(row['actions'] or [])==1
            if name=='edit' and row.get('old_draft_blocked') is not True:
                row['functional_success']=False
            records.append(row)
            args.out.write_text(json.dumps({'scope':'live_smoke_not_heldout_benchmark','records':records},ensure_ascii=False,indent=2))
            print(json.dumps({'case':name,'success':row['functional_success'],
                              'tools':r.get('capabilities_used'),'failure':r.get('agent_runtime',{}).get('failure')},ensure_ascii=False),flush=True)
    summary={'complete':len(records)==len(cases),'passed':sum(r['functional_success'] for r in records),
             'total':len(records),'all_no_financial_execution':all(r['no_financial_execution'] for r in records)}
    args.out.write_text(json.dumps({'scope':'live_smoke_not_heldout_benchmark','summary':summary,'records':records},ensure_ascii=False,indent=2))
    if not records or not summary['all_no_financial_execution']:return 2
    return 0 if summary['passed']==summary['total'] else 1

if __name__=='__main__':sys.exit(main())
