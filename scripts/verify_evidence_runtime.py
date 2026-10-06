#!/usr/bin/env python3
"""冻结候选、隔离数据库的公共证据运行时验收。默认离线；联网须显式选择。

真实云端只测一个合成公共目录问题，不证明真实产品分析或金融语义质量。
报告不记录密钥、账户标识、问题私人条件或模型隐藏思考。
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import warnings

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def save(path,value):
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    os.replace(temp,path)


class Results(unittest.TextTestResult):
    def __init__(self,*a,**k):super().__init__(*a,**k);self.records=[]
    def addSuccess(self,test):
        super().addSuccess(test);self.records.append({'test':test.id(),'status':'passed'})
    def addFailure(self,test,error):
        super().addFailure(test,error);self.records.append({'test':test.id(),'status':'failed'})
    def addError(self,test,error):
        super().addError(test,error);self.records.append({'test':test.id(),'status':'error'})


def offline(out):
    names=['test_evidence_runtime','test_evidence_runtime_http','test_knowledge_resolver_performance',
           'test_local_knowledge','test_knowledge_retrieval','test_base_evidence','test_base_guards',
           'test_current_catalog','test_rag_integration']
    log=io.StringIO();suite=unittest.defaultTestLoader.loadTestsFromNames(names)
    # Python3.14会报告既有银行服务连接关闭告警；只略去ResourceWarning，失败仍完整保存。
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',ResourceWarning)
        result=unittest.TextTestRunner(stream=log,verbosity=2,resultclass=Results).run(suite)
    (out/'tests.log').write_text(log.getvalue())
    return {'passed':result.wasSuccessful(),'tests_run':result.testsRun,
            'failures':len(result.failures),'errors':len(result.errors),'tests':result.records,
            'scope':'isolated_sqlite_http_scripted_models_no_financial_semantic_certification'}


def official(out,directory):
    from local_knowledge import LocalKnowledgeProvider,KnowledgeError
    from local_knowledge.webank_runtime import ensure_webank_source,read_webank_catalog,decode_webank_catalog
    from webank_catalog import URL
    p=LocalKnowledgeProvider(directory/'official.sqlite3');ensure_webank_source(p)
    began=time.monotonic()
    try:
        catalog=read_webank_catalog(p,timeout=8)
        ids=['WEBANK:'+r['code'] for r in catalog['records'][:12]]
        admitted=decode_webank_catalog(p,catalog['raw_bytes'],product_ids=ids,
                                      facets=['general','risk','fees','sale_status']) if ids else None
        return {'transport_passed':True,'fixed_url':URL,'raw_sha256':catalog['raw_sha256'],
                'source_date':catalog['source_date'],'records':len(catalog['records']),
                'current_field_versions':len(admitted['version_ids']) if admitted else 0,
                'coverage_gaps':admitted['coverage_gaps'] if admitted else [],
                'elapsed_ms':round((time.monotonic()-began)*1000,3),
                'financial_semantic_sufficiency_verified':False,'published':False}
    except KnowledgeError as error:
        return {'transport_passed':False,'fixed_url':URL,'error_code':error.code,
                'elapsed_ms':round((time.monotonic()-began)*1000,3),'published':False}


def cloud(out,directory):
    from server import create_server,ProxyHandler
    from agent_runtime.conversation import ConversationAgent,GLMConversationReviewer
    from agent_runtime.native_client import AIPingToolClient
    from local_knowledge.runtime import EvidenceRuntime
    from test_evidence_runtime import catalog_body
    from test_bank_integration import Browser
    from unittest.mock import patch
    records=[]
    class Primary(AIPingToolClient):
        def complete(self,*a,**k):
            result,usage=super().complete(*a,**k)
            records.append({'stage':'primary','usage':usage,
                            'tools':[c['function']['name'] for c in result.get('tool_calls',[])]})
            return result,usage
    class Reviewer(GLMConversationReviewer):
        def review(self,*a,**k):
            result=super().review(*a,**k)
            records.append({'stage':'reviewer','usage':result.get('_model_usage',{}),
                            'verdict':result['verdict']})
            return result
    primary=Primary(log_path=out/'model-calls.jsonl');reviewer=Reviewer()
    # 与当前正式Agent相同的上限；旧48k/80k失败目录保留，不改金融验收题或预算。
    agent=ConversationAgent(primary,reviewer,max_rounds=8,max_tools=12,max_tokens=100000,timeout_seconds=105)
    server=create_server(0,directory/'cloud-bank.sqlite3',conversation_agent=agent)
    server.evidence_runtime.close()
    fetches=[]
    def reader(provider,source_id,url,**kwargs):
        fetches.append({'source_id':source_id,'url':url});return catalog_body()
    server.evidence_runtime=EvidenceRuntime(server.finance.knowledge_provider,reader=reader,background=False)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    began=time.monotonic()
    try:
        browser=Browser(f'http://127.0.0.1:{server.server_port}');session,_=browser.start()
        question='请查询 WEBANK:000001 的风险披露。只引用本轮资料；当前可买和完整费率未核实。'
        req=urllib.request.Request(browser.base+'/api/agent',method='POST',
            data=json.dumps({'text':question},ensure_ascii=False).encode(),
            headers={'Content-Type':'application/json','X-QZ-CSRF':browser.csrf})
        try:response=browser.client.open(req,timeout=105)
        except urllib.error.HTTPError as error:response=error
        with response:status=response.status;result=json.loads(response.read(2_000_000))
        receipt_ok=False
        if result.get('speech_receipt'):
            token=next(c.value for c in browser.cookies if c.name=='qz_bank_user')
            receipt_ok=bool(server.finance.resolve_speech(session['workspace_id'],result['speech_receipt'],token))
        snapshot=result.get('knowledge_snapshot') or {}
        passed=(status==200 and result.get('compliance_pass') is True and not result.get('isFallback')
                and snapshot.get('release_id','').startswith('tmp-') and bool(snapshot.get('canonical_digests'))
                and receipt_ok and len(fetches)==1 and any(r['stage']=='reviewer' for r in records))
        return {'passed':passed,'http_status':status,'fallback':result.get('isFallback'),
                'error_code':result.get('error',{}).get('code') if isinstance(result.get('error'),dict) else None,
                'reply':result.get('reply'),'review':result.get('agent_review'),'runtime':result.get('agent_runtime'),
                'knowledge_preparation':result.get('knowledge_preparation'),
                'fixed_temporary_snapshot':snapshot.get('release_id','').startswith('tmp-'),
                'canonical_digest_count':len(snapshot.get('canonical_digests',[])),
                'speech_receipt_valid':receipt_ok,'cloud_calls':records,'source_fetches':fetches,
                'elapsed_ms':round((time.monotonic()-began)*1000,3),
                'fixture':'synthetic_public_catalog_not_real_bank_product',
                'financial_semantic_quality':'not_certified','tts_audio_synthesis':'not_executed',
                'configured_agent_token_budget':100000,'configured_agent_timeout_seconds':105,
                'primary_max_output_tokens':4000,'maximum_cases':1,'live_service_restarted':False}
    finally:server.shutdown();server.server_close();thread.join(5)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['offline','official','cloud'],default='offline')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    from scripts.run_rag_acceptance import freeze
    candidate=out/'candidate';candidate.mkdir();manifest=freeze(candidate)
    for src in sorted((ROOT/'tests').glob('*.py')):
        target=candidate/'tests'/src.name;target.parent.mkdir(exist_ok=True);shutil.copy2(src,target)
        if digest(src)!=digest(target):raise RuntimeError('test_source_changed')
        manifest['tests/'+src.name]=digest(target)
    if args.mode=='offline':
        # 基座回归依赖已封存的合成四画像来源；冻结输入，不读现役客户库。
        fixture=ROOT/'outputs/contest-finances'
        for src in sorted(fixture.rglob('*')):
            if not src.is_file() or src.name.endswith(('-wal','-shm')):continue
            rel=src.relative_to(ROOT);target=candidate/rel;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(src,target)
            if digest(src)!=digest(target):raise RuntimeError('synthetic_fixture_changed')
            manifest[str(rel)]=digest(target)
    save(out/'source-manifest.json',manifest)
    sys.path[:0]=[str(candidate),str(candidate/'tests')]
    with tempfile.TemporaryDirectory(prefix='navren-evidence-verification-') as temp:
        directory=Path(temp)
        from cryptography.fernet import Fernet
        os.environ.update(NAVREN_PERSONAL_DATA_KEY=Fernet.generate_key().decode(),
            NAVREN_KNOWLEDGE_DB=str(directory/'public.sqlite3'),NAVREN_MARKET_DB=str(directory/'market.sqlite3'),
            NAVREN_MODEL_CALL_LOG=str(out/'model-calls.jsonl'),NAVREN_FUND_CACHE=str(directory/'funds'),
            NAVREN_ALLOW_CLOUD_ASR='0')
        if args.mode=='offline':
            # 每项测试传入独立银行文件；不要让一个全局覆盖路径把它们串成同一个公共库。
            os.environ.pop('NAVREN_KNOWLEDGE_DB',None)
        from server import ProxyHandler
        ProxyHandler.log_message=lambda *a:None
        began=time.monotonic()
        report={'mode':args.mode,'started_at':datetime.now(timezone.utc).isoformat(),
                'candidate_file_count':len(manifest),'live_service_restarted':False}
        report['result']={'offline':offline,'official':official,'cloud':cloud}[args.mode](out,*(() if args.mode=='offline' else (directory,)))
        report['candidate_unchanged']=all(digest(candidate/rel)==sha for rel,sha in manifest.items())
        report['working_tree_changed_after_freeze']=[rel for rel,sha in manifest.items() if not (ROOT/rel).exists() or digest(ROOT/rel)!=sha]
        report['elapsed_ms']=round((time.monotonic()-began)*1000,3)
        save(out/'results.json',report)
        print(json.dumps({'mode':args.mode,'result_path':str(out/'results.json'),
                          'passed':report['result'].get('passed'),'elapsed_ms':report['elapsed_ms']},ensure_ascii=False))
        if args.mode!='official' and not report['result']['passed']:return 1
        return 0 if report['candidate_unchanged'] else 1


if __name__=='__main__':raise SystemExit(main())
