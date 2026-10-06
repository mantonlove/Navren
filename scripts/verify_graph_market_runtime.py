#!/usr/bin/env python3
"""隔离账户 HTTP → 真模型 → 工具 → 独立审核；证据不包含 Cookie/CSRF。"""
import concurrent.futures,http.cookiejar,json,urllib.request,time
from pathlib import Path
BASE='http://127.0.0.1:18767'
def probe(kind):
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(path,payload=None,headers=None):
        req=urllib.request.Request(BASE+path,data=json.dumps(payload).encode() if payload is not None else None,headers={'Content-Type':'application/json',**(headers or {})})
        with opener.open(req,timeout=170) as r:return json.loads(r.read())
    session=request('/api/finance/session',{})
    if kind=='market':
        event=request('/api/market/hotspots')['events'][0]
        payload={'text':'解释这项事件的事实、条件与资金传导，附真实来源。不读取我的账户。','market_context':{'kind':'event','id':event['id']}}
    elif kind=='graph':
        graph=request('/api/finance/personal-graph?view=funds');insight=graph['insights'][0]
        payload={'text':'解释这项本期支出洞察，给出金额、比较口径和证据。','graph_context':{'view':'funds','insight_id':insight['id']}}
    elif kind=='graph_relation':
        graph=request('/api/finance/personal-graph?view=cost');edge=next(e for e in graph['links'] if e['predicate']=='续费候选')
        payload={'text':'解释指定的这条续费候选关系：对应对象、金额、时间及证据。不要把候选说成已扣款。','graph_context':{'view':'cost','edge_id':edge['id']}}
    else:payload={'text':'我们的市场数据从哪里来，哪些来源还没有接入？只查询来源状态，不读我的账户。'}
    started=time.monotonic();result=request('/api/agent',payload,{'X-QZ-CSRF':session['csrf']})
    safe={k:result.get(k) for k in ('reply','isFallback','agent_runtime','agent_review','answer_contract','market_sources')}
    assert result.get('reply') and not result.get('isFallback'),result.get('error')
    assert result['agent_runtime']['failure'] is None
    assert result.get('agent_review',{}).get('status')=='approved'
    safe.update(kind=kind,elapsed_seconds=round(time.monotonic()-started,2))
    return safe
if __name__=='__main__':
    path=Path('outputs/graph-acceptance/real-runtime.json')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(probe,['market','graph','graph_relation','sources']))
    path.write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    for r in rows:print(r['kind'],r.get('isFallback'),r.get('agent_review'),r['elapsed_seconds'])
