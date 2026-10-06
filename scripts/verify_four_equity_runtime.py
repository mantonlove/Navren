"""独立浏览器会话的 HTTP → 主模型 → 银行工具 → GLM 实测，不执行资金动作。"""
import http.cookiejar,json,time,urllib.request
from pathlib import Path
BASE='http://127.0.0.1:18767'
def probe(question):
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def call(path,payload=None,csrf=None):
        req=urllib.request.Request(BASE+path,data=json.dumps(payload).encode()if payload is not None else None,headers={'Content-Type':'application/json',**({'X-QZ-CSRF':csrf}if csrf else {})})
        with opener.open(req,timeout=170)as r:return json.loads(r.read())
    session=call('/api/finance/session',{});before=call('/api/finance/overview');started=time.monotonic()
    r=call('/api/agent',{'text':question,'mode':'youth'},session['csrf']);after=call('/api/finance/overview')
    safe={k:r.get(k)for k in ['reply','isFallback','agent_runtime','agent_review','answer_contract','capabilities_used','widgets']}
    # Widgets contain workspace bindings; retained only as deidentified amounts and view identifiers.
    safe['widgets']=[{'type':v['type'],'range':v.get('range'),'portfolio':v.get('snapshot',{}).get('portfolio'),'summary_by_currency':v.get('snapshot',{}).get('summary_by_currency')}for v in safe.get('widgets')or[]]
    safe.update(question=question,elapsed_seconds=round(time.monotonic()-started,2),no_financial_execution=before['version']==after['version']and before['total_cash_minor']==after['total_cash_minor'])
    assert not r.get('isFallback') and r.get('agent_review',{}).get('status')=='approved',safe
    assert safe['no_financial_execution'];return safe
if __name__=='__main__':
    out=Path('output/equity/real-runtime.json');rows=[]
    for question in ['查独立历史模拟组合，东方臻宝纯债A ORIENT:006210在2026-09-30的份额、净值、当次损益及来源。只查询，不推荐、不申购。','查看独立历史模拟组合2026年9月的收益日历，告诉我完整月份总损益，不能与当前账户相加。']:
        rows.append(probe(question));out.write_text(json.dumps({'scope':'live_smoke_not_heldout_benchmark','records':rows},ensure_ascii=False,indent=2));print(rows[-1]['capabilities_used'],rows[-1]['agent_review']['status'],flush=True)
