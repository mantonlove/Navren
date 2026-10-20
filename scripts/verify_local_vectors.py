#!/usr/bin/env python3
"""真实本地 embedding 的小样本检索验证（合成语料，不是产品事实验收）。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import resource
import socket
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from local_knowledge import LocalKnowledgeProvider
from local_knowledge.embeddings import get_encoder,MODEL_ARTIFACTS,MODEL_REVISION
from local_knowledge import vectors
from agent_runtime.knowledge_retrieval import KnowledgeRetrieval


CORPUS = {
 'arrival':('流动性条款','赎回申请经确认后两个工作日到账，遇非交易日顺延。','liquidity'),
 'shortfee':('短期退出费用','持有期限不足七日，赎回费率为1.5%，从赎回款项中扣除。','fees'),
 'lock':('封闭期限','产品成立后有180天封闭期，期间不接受赎回申请。','terms'),
 'risk':('本金风险','本产品不保证本金安全，投资可能亏损，极端情况下本金可能发生较大损失。','risk'),
 'dividend':('分红方式','现金分红支付至账户；红利再投资将分配金额转换为新增基金份额。','terms'),
 'equity':('资产投资比例','股票投资占基金资产的比例不得超过百分之八十。','strategy'),
 'annualfee':('持续费用','管理费和托管费按前一日资产净值每日计提，按月从基金资产中支付。','fees'),
 'shares':('份额收费差异','A类份额收取申购费；C类份额按日计提销售服务费。','fees'),
 'duration':('债券利率风险','久期衡量债券价格对利率变化的敏感程度；市场利率上升时债券价格通常下降。','risk'),
 'nav':('净值口径','单位净值为每份基金份额对应的资产净值；累计净值在单位净值基础上计入历次现金分红。','returns'),
 'fx':('币种与汇率','本产品以美元计价和结算，换算成人民币的金额受汇率波动影响。','risk'),
 'guarantee':('存款保障范围','存款保险与投资理财产品不同，理财产品不属于存款保险保障范围。','risk'),
}
# 人工预先指定目标，dev只选阈值；test单独报告，绝不追调已冻结test结果。
CASES = [
 ('dev','急用钱想拿出来要等多久','arrival'),
 ('dev','刚买了没几天就卖掉要扣多少钱','shortfee'),
 ('dev','买进去后半年里能提前退出吗','lock'),
 ('dev','存进去的一万会不会变少','risk'),
 ('dev','分到的钱能不能自动继续买成份额','dividend'),
 ('dev','这种产品把钱投进股市最多能占几成','equity'),
 ('dev','如何烧红烧肉',None),
 ('dev','杭州明天会不会下雨',None),
 ('dev','帮我订一张北京的机票',None),
 ('test','赎回后什么时候能收到钱','arrival'),
 ('test','只放三天就取走需要付什么费用','shortfee'),
 ('test','处于锁定期的钱还能拿出来吗','lock'),
 ('test','这款投资能保证我一分钱都不赔吗','risk'),
 ('test','分红后选择继续投资是什么意思','dividend'),
 ('test','组合里权益资产最多可以占多少','equity'),
 ('test','为什么没卖出基金也会扣管理托管成本','annualfee'),
 ('test','A和C是买的时候扣费还是每天扣费','shares'),
 ('test','利息水平涨了为什么手里的债券会跌','duration'),
 ('test','为什么基金显示两个不同的净值数字','nav'),
 ('test','买美元产品换回人民币还会受什么影响','fx'),
 ('test','买银行理财也能享受存款保险吗','guarantee'),
 ('test','Python列表如何排序',None),
 ('test','给我写一首中秋诗',None),
 ('test','附近哪里修自行车',None),
]


def atomic_json(path,data):
    import os
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(data,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(temp,path)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',required=True);args=p.parse_args()
    network_attempts=[]
    def forbidden(*a,**k):
        network_attempts.append('socket_connect');raise AssertionError('network_forbidden_during_local_rag')
    started=time.monotonic()
    with patch.object(socket.socket,'connect',forbidden),patch.object(socket.socket,'connect_ex',forbidden):
        encoder=get_encoder();cold=time.monotonic()-started
        with tempfile.TemporaryDirectory(prefix='navren-real-local-vector-') as directory:
            provider=LocalKnowledgeProvider(Path(directory)/'public.sqlite3',encoder=encoder)
            provider.register_source('synthetic',publisher='人工合成检索语料',endpoints=['https://synthetic.invalid/'],
                freshness={f:None for f in ['liquidity','fees','terms','risk','strategy','returns']},
                shape='plain-text-v1',permission_basis='synthetic_retrieval_benchmark_not_financial_evidence')
            versions=[]; labels={}
            for label,(title,text,facet) in CORPUS.items():
                version=provider.stage_document(source_id='synthetic',source_url='https://synthetic.invalid/'+label,
                    title=title,text=text,published_on='2026-10-01',retrieved_on='2026-10-10',
                    effective_from='2026-10-01',facets=[facet])
                provider.approve_version(version,verification_basis='synthetic_expected_label_frozen')
                versions.append(version)
                with provider.db() as db:
                    labels.update({r[0]:label for r in db.execute('SELECT id FROM chunks WHERE version_id=?',(version,))})
            release=provider.publish(version_ids=versions)
            built=provider.build_vector_index(release)
            baseline=LocalKnowledgeProvider(provider.path,release_id=release,encoder=False)
            examples=[];timings=[]
            for split,query,target in CASES:
                request={'scope':'public','query':query,'as_of':'2026-10-10','facets':[],'limit':3}
                lexical=baseline.search_public(request,release)
                t=time.monotonic();hybrid=provider.search_public(request,release);timings.append(time.monotonic()-t)
                with provider.db() as db:
                    raw=vectors.VectorIndex(provider,encoder).search(db,release,query,set(labels))
                ranked=sorted(raw['scores'],key=lambda x:(-raw['scores'][x],x))
                examples.append({'split':split,'query':query,'target':target,
                    'fts':[labels[x] for x in lexical['citation_ids']],
                    'vector':[labels[x] for x in ranked[:3]],
                    'hybrid':[labels[x] for x in hybrid['citation_ids']],
                    'vector_scores':{labels[k]:round(v,5) for k,v in raw['scores'].items()},
                    'retrieval':hybrid['retrieval']})
            consumer=KnowledgeRetrieval(provider)
            consumed=consumer.search(query='赎回后什么时候能收到钱',as_of='2026-10-10',facets=[],limit=3)
            consumer.assert_fresh()
            first=consumed['hits'][0] if consumed['hits'] else None
            consumer_proof={'hit_count':len(consumed['hits']),'original_quote':first.get('excerpt') if first else None,
                            'resolved_citation':bool(first and first['citation_id'] in labels)}
            # 长片段尾窗真实推理：验证不仅给第一窗向量。
            tail=encoder.encode_documents(['债券价格利率风险。'*100+'赎回申请经确认后两个工作日到账。'])[0]
    summary={}
    for split in ['dev','test']:
        cases=[x for x in examples if x['split']==split]; positives=[x for x in cases if x['target']];negatives=[x for x in cases if x['target'] is None]
        summary[split]={method:{'positive_count':len(positives),'hit_at_1':sum(x[method][:1]==[x['target']] for x in positives),
            'hit_at_3':sum(x['target'] in x[method] for x in positives),'negative_count':len(negatives),
            'negative_with_hits':sum(bool(x[method]) for x in negatives)} for method in ['fts','vector','hybrid']}
    checks={'zero_query_network':not network_attempts,'consumer_original_resolved':bool(consumer_proof['original_quote'] and consumer_proof['resolved_citation']),
            'long_passage_has_tail_windows':len(tail)>=2,
            'dev_recall_improves':summary['dev']['hybrid']['hit_at_3']>summary['dev']['fts']['hit_at_3'],
            'heldout_recall_does_not_regress':summary['test']['hybrid']['hit_at_3']>=summary['test']['fts']['hit_at_3'],
            'daily_irrelevant_questions_rejected':all(summary[s]['hybrid']['negative_with_hits']==0 for s in ['dev','test']),
            'actual_vector_branch_used':all(c['retrieval']['mode']=='hybrid' and c['retrieval']['vector_reason'] is None for c in examples)}
    result={'schema_version':1,'data_mode':'synthetic_manual_financial_retrieval_corpus',
        'model_mode':'real_local_onnx_cpu_quantized_embedding','model_revision':MODEL_REVISION,
        'profile_id':encoder.profile_id,'minimum_similarity':vectors.MIN_SIMILARITY,
        'maximum_score_distance':vectors.MAX_SCORE_DISTANCE,'semantic_rrf_weight':vectors.SEMANTIC_WEIGHT,
        'model_bytes':sum(x['size'] for x in MODEL_ARTIFACTS.values()),'cold_load_seconds':round(cold,4),
        'hot_query_median_ms':round(sorted(timings)[len(timings)//2]*1000,3),
        'hot_query_max_ms':round(max(timings)*1000,3),'process_max_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'network_attempt_count':len(network_attempts),'cloud_model_calls':0,'build':built,'summary':summary,
        'consumer':consumer_proof,'long_passage_windows':len(tail),'cases':examples,'checks':checks,
        'limits':['Small synthetic corpus; not an official product evidence or allocation-quality acceptance.',
                  'Socket guards apply to this Python process; no claim of OS-wide network monitoring.',
                  'No cloud conversation, production restart or real personal-data retrieval tested.']}
    atomic_json(args.out,result)
    print(json.dumps({k:result[k] for k in ['summary','model_bytes','cold_load_seconds','hot_query_median_ms','network_attempt_count']},ensure_ascii=False))
    return 0 if all(checks.values()) else 1


if __name__=='__main__':sys.exit(main())
