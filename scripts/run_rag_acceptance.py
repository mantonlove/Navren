#!/usr/bin/env python3
"""RAG真实联合验收。仅驱动现有HTTP/ConversationAgent，不另建模型链。"""
from __future__ import annotations
import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http import cookiejar
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import unicodedata
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT/'outputs/window-progress/rag'
OUTPUT = ARTIFACTS/'acceptance-20261006'


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()
def save(path, value):
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    os.replace(temp, path)


def protocol_checks(report, requested):
    """完整采集和原件/末端协议判据；不把它当金融语义裁决。"""
    rows = report['records']
    return {'complete_collection':len(rows)==requested and requested>0,
        'approved_replies':bool(rows) and all(r['http_reply_approved'] for r in rows),
        'expected_evidence':bool(rows) and all(r['all_expected_evidence_retrieved'] for r in rows),
        'fixed_release':bool(rows) and all(r['fixed_release_preserved'] for r in rows),
        'speech_guard':bool(rows) and all(r['speech_receipt_validated'] for r in rows),
        'speech_content':bool(rows) and all(r.get('speech_text')==unicodedata.normalize('NFKC',r['response'].get('reply','')).strip() for r in rows),
        'no_financial_execution':all(r['no_financial_side_effect'] for r in rows),
        'candidate_unchanged':report['candidate_unchanged'],
        'public_input_unchanged':report['public_input_unchanged'],
        'question_input_unchanged':report['question_input_unchanged']}


def freeze(target):
    source_files = list(ROOT.glob('*.py')) + list(ROOT.glob('*.html'))
    for directory, suffix in (('css','.css'),('js','.js')):
        source_files += list((ROOT/directory).glob('*'+suffix))
    for directory in ('agent_runtime','base_runtime','simulated_bank','local_knowledge','financial_methods','skills','knowledge','scripts'):
        source_files += [p for p in (ROOT/directory).rglob('*') if p.is_file() and p.suffix in ('.py','.json','.md','.txt')
                         and '__pycache__' not in p.parts and 'acceptance' not in p.parts]
    manifest = {}
    for src in sorted(set(source_files)):
        rel = src.relative_to(ROOT); dst = target/rel
        dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dst)
        digest = sha(src)
        if sha(dst) != digest: raise ValueError('source_copy_changed:'+str(rel))
        manifest[str(rel)] = digest
    # 同一个本机代码快照，实际答复不会读取工作中途的新代码。
    for rel, digest in manifest.items():
        if sha(ROOT/rel) != digest: raise ValueError('source_changed_during_freeze:'+rel)
    return manifest


def native_call(name, arguments):
    return {'role':'assistant','content':None,'tool_calls':[{'id':'protocol-'+name,'type':'function',
            'function':{'name':name,'arguments':json.dumps(arguments,ensure_ascii=False)}}]}


class ProtocolPrimary:
    model_id = 'offline-rag-protocol-only'
    def __init__(self, case): self.case, self.step, self.calls, self.tool_outputs = case, 0, [], []
    def complete(self, messages, tools, **kwargs):
        for message in messages:
            if message.get('role') == 'tool' and not any(r['call_id'] == message['tool_call_id'] for r in self.tool_outputs):
                self.tool_outputs.append({'call_id':message['tool_call_id'],'output':json.loads(message['content'])})
        self.step += 1
        if self.step == 1:
            request = deepcopy(self.case['request']); request['query'] = '费用 条件' if 'fees' in request['facets'] else '赎回 到账 条件' if 'liquidity' in request['facets'] else '风险提示 原文'
            answer = native_call('knowledge_search', request)
        elif self.step == 2:
            evidence = self.tool_outputs[-1]['output']
            quotes = '\n'.join('“'+hit['excerpt']+'”' for hit in evidence['data']['hits'])
            text = '隔离协议预检：原件有效区间尚未核实，当前是否在售或适合购买未知。\n'+quotes
            answer = native_call('answer_reviewed', {'text':text,'claims':[{'text':'“'+hit['excerpt']+'”','evidence_ids':[evidence['evidence_id']]} for hit in evidence['data']['hits']],
                                                   'draft_ids':[],'followups':[]})
        else: raise ValueError('protocol_unexpected_extra_round')
        for call in answer['tool_calls']:
            self.calls.append({'id':call['id'],'name':call['function']['name'],'arguments':json.loads(call['function']['arguments'])})
        return answer, {'total_tokens':1}


class ProtocolReviewer:
    model_id = 'offline-rag-review-protocol-only'
    def __init__(self): self.records = []
    def review(self, packet, *args):
        decision = {'verdict':'approve','reason':'仅离线协议回归，不判金融语义','issues':[]}
        self.records.append({'packet':deepcopy(packet),'decision':decision})
        return decision


def run(args):
    out = OUTPUT/args.run_id
    out.mkdir(parents=True, exist_ok=False)
    candidate = out/'candidate'; candidate.mkdir()
    manifest = freeze(candidate)
    save(out/'source-manifest.json', manifest)
    # 导入后全部业务模块来自本次候选，runner没有提前加载业务代码。
    sys.path.insert(0, str(candidate))
    from cryptography.fernet import Fernet
    temporary = tempfile.TemporaryDirectory(prefix='navren-rag-joint-')
    directory = Path(temporary.name)
    # fund_history.CACHE 等默认参数在导入时确定，必须先建立隔离环境。
    isolated_env = {'NAVREN_PERSONAL_DATA_KEY':Fernet.generate_key().decode(),
        'NAVREN_SIM_DB':str(directory/'bank.sqlite3'),'NAVREN_KNOWLEDGE_DB':str(directory/'knowledge.sqlite3'),
        'NAVREN_MARKET_DB':str(directory/'market.sqlite3'),'NAVREN_FUND_CACHE':str(directory/'funds'),
        'NAVREN_MODEL_CALL_LOG':str(out/'model-calls.jsonl'),'NAVREN_ALLOW_CLOUD_ASR':'0'}
    os.environ.update(isolated_env)
    real_open = urllib.request.urlopen
    def registered_requests_only(request, *a, **kw):
        from urllib.parse import urlsplit
        url = request.full_url if hasattr(request,'full_url') else str(request)
        p = urlsplit(url)
        permitted = p.hostname in ('127.0.0.1','localhost') or args.mode == 'cloud' and url in (
            'https://aiping.cn/api/v1/chat/completions')
        if not permitted: raise AssertionError('unregistered_network_request')
        return real_open(request,*a,**kw)
    # 捕获 from urllib.request import urlopen 的模块也必须在受限环境中导入。
    urllib.request.urlopen = registered_requests_only
    from agent_runtime.conversation import ConversationAgent
    from server import create_server, ProxyHandler
    from voice_gateway import VoiceGateway
    from local_knowledge import LocalKnowledgeProvider
    from scripts.run_skill_acceptance import TracePrimary, TraceReviewer
    import fund_history
    if fund_history.CACHE != directory/'funds': raise ValueError('fund_cache_not_isolated')
    release_manifest = json.loads((ARTIFACTS/'integration-data-manifest.json').read_text())
    questions = json.loads((ARTIFACTS/'manual-question-set.json').read_text())
    cases = [c for c in questions['cases'] if not args.cases or c['id'] in args.cases.split(',')]
    if not cases: raise ValueError('empty_cases')
    source = Path(release_manifest['knowledge_database']['path'])
    if sha(source) != release_manifest['knowledge_database']['sha256']: raise ValueError('public_database_changed')
    report = {'schema_version':1,'started_at':now(),'mode':args.mode,'source_manifest':str(out/'source-manifest.json'),
        'source_sha256':manifest,'question_set_sha256':sha(ARTIFACTS/'manual-question-set.json'),
        'public_database_sha256':sha(source),'release_id':release_manifest['knowledge_database']['release_id'],
        'no_live_database_access':True,'live_service_restarted':False,'cloud_authorization':'用户明确要求完成验收',
        'model_chain':'existing_http_ConversationAgent','financial_execution_authorized':False,'records':[],
        'isolated_paths':{k:v for k,v in isolated_env.items() if k.endswith(('_DB','_CACHE'))},
        'budget':{'max_case_seconds':args.case_seconds,'max_case_tokens':80000,'maximum_aggregate_tokens':600000}}
    aggregate_tokens = 0
    with temporary:
        with patch('urllib.request.urlopen', registered_requests_only), patch.object(ProxyHandler,'log_message',lambda *a:None):
            shutil.copy2(source,directory/'knowledge.sqlite3')
            server = create_server(0,directory/'bank.sqlite3',voice=VoiceGateway(key='isolated-no-tts-cloud'))
            server.finance.knowledge_provider = LocalKnowledgeProvider(directory/'knowledge.sqlite3',release_id=report['release_id'])
            status = server.finance.knowledge_provider.status()
            if status['public_release_id'] != report['release_id'] or status['published_chunk_count'] != 22:
                raise ValueError('unexpected_public_release')
            thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            base = 'http://127.0.0.1:'+str(server.server_port)
            try:
                for case in cases:
                    if aggregate_tokens >= 600000: report['stop_reason']='aggregate_token_budget';break
                    primary = TracePrimary(out/'model-calls.jsonl') if args.mode=='cloud' else ProtocolPrimary(case)
                    reviewer = TraceReviewer(out/'model-calls.jsonl') if args.mode=='cloud' else ProtocolReviewer()
                    # 全请求/双模型共享同一应用实例与既有工具循环。
                    server.conversation_agent=ConversationAgent(primary,reviewer,max_rounds=8,max_tools=12,
                        timeout_seconds=args.case_seconds,max_tokens=80000)
                    cookies=cookiejar.CookieJar()
                    client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
                    def request(path, body=None, csrf=None):
                        headers={'Content-Type':'application/json','Origin':base}
                        if csrf: headers['X-QZ-CSRF']=csrf
                        req=urllib.request.Request(base+path,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers=headers)
                        try: response=client.open(req,timeout=args.case_seconds+15)
                        except urllib.error.HTTPError as error: response=error
                        with response: return response.status,json.loads(response.read()),dict(response.headers)
                    http,session,_=request('/api/finance/session',{})
                    if http!=200: raise ValueError('session_setup_failed')
                    workspace=session['workspace_id'];server.finance.wealth.provision(workspace)
                    def bank_state():
                        with closing(server.finance.bank.connect()) as db:
                            return {table:[dict(row)for row in db.execute('SELECT * FROM '+table+' WHERE workspace_id=? ORDER BY rowid',(workspace,))]
                                    for table in ('accounts','holdings','orders','transactions','fund_holds','operations')}
                    before=bank_state(); started=time.monotonic()
                    status_http,status_body,_=request('/api/finance/knowledge/status')
                    http,result,headers=request('/api/agent',{'text':case['question']},session['csrf'])
                    runtime=result.get('agent_runtime',{})
                    aggregate_tokens+=runtime.get('budget_tokens',0)
                    knowledge_outputs=[r['output']['data']for r in primary.tool_outputs
                        if isinstance(r.get('output'),dict) and isinstance(r['output'].get('data'),dict)
                        and r['output']['data'].get('scope')=='public' and 'hits' in r['output']['data']]
                    expected=set(case['expected_review_keys'])
                    hit_keys={h['locator']['paragraph'].split('/',1)[-1]for d in knowledge_outputs for h in d.get('hits',[])}
                    public_snapshot=result.get('knowledge_snapshot') or {}
                    accepted=bool(http==200 and not result.get('isFallback') and result.get('agent_review',{}).get('status')=='approved')
                    receipt=result.get('speech_receipt');speech_pass=False; speech_text=None
                    if receipt:
                        token=next(c.value for c in cookies if c.name=='qz_bank_user')
                        speech_text=server.finance.resolve_speech(workspace,receipt,token)
                        speech_pass=bool(isinstance(speech_text,str) and speech_text.strip())
                    no_side_effect=before==bank_state()
                    record={'id':case['id'],'question':case['question'],'expected_answer':case['expected_answer'],
                        'http_status':http,'release_header':headers.get('X-Navren-Release'),
                        'status_http':status_http,'provider_status':status_body,'response':result,
                        'model_tool_calls':primary.calls,'tool_outputs':primary.tool_outputs,'reviewer_records':reviewer.records,
                        'duration_seconds':time.monotonic()-started,'http_reply_approved':accepted,
                        'expected_review_keys':sorted(expected),'retrieved_review_keys':sorted(hit_keys),
                        'all_expected_evidence_retrieved':expected.issubset(hit_keys),
                        'fixed_release_preserved':public_snapshot.get('release_id')==report['release_id'],
                        'speech_receipt_validated':speech_pass,'speech_text':speech_text,
                        'speech_receipt_same_text':speech_text==result.get('reply'),
                        'no_financial_side_effect':no_side_effect,
                        'semantic_judgment':'pending_independent_source_review' if args.mode=='cloud' else 'not_evaluated'}
                    report['records'].append(record); save(out/'results.json',report)
                    print(json.dumps({k:record[k]for k in ('id','http_status','http_reply_approved','all_expected_evidence_retrieved','speech_receipt_validated','no_financial_side_effect')},ensure_ascii=False),flush=True)
                    if args.stop_on_failure and not accepted:
                        report['stop_reason']='pilot_http_or_model_failure';break
            finally:
                server.shutdown();server.server_close();thread.join(5)
    report['completed_at']=now()
    report['candidate_unchanged']=all(sha(candidate/rel)==digest for rel,digest in manifest.items())
    report['current_source_changes']=[rel for rel,digest in manifest.items() if not (ROOT/rel).exists() or sha(ROOT/rel)!=digest]
    report['public_input_unchanged']=sha(source)==report['public_database_sha256']
    report['question_input_unchanged']=sha(ARTIFACTS/'manual-question-set.json')==report['question_set_sha256']
    report['summary']={'requested_cases':len(cases),'collected_cases':len(report['records']),
        'approved_replies':sum(r['http_reply_approved']for r in report['records']),
        'full_expected_retrieval':sum(r['all_expected_evidence_retrieved']for r in report['records']),
        'speech_receipts':sum(r['speech_receipt_validated']for r in report['records']),
        'no_financial_execution':all(r['no_financial_side_effect']for r in report['records']),
        'budget_tokens':aggregate_tokens,'semantic_passes':'not_scored_by_runner'}
    report['protocol_checks']=protocol_checks(report,len(cases))
    report['protocol_pass']=all(report['protocol_checks'].values())
    report['financial_semantic_verdict']='requires_independent_review'
    report['exit_code']=0 if report['protocol_pass'] else 1
    save(out/'results.json',report)
    urllib.request.urlopen=real_open
    return report['exit_code']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('offline','cloud'),default='offline')
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--cases',default='')
    parser.add_argument('--case-seconds',type=int,default=120)
    parser.add_argument('--stop-on-failure',action='store_true')
    args=parser.parse_args()
    if not 1<=args.case_seconds<=180 or not args.run_id or Path(args.run_id).name!=args.run_id:
        parser.error('invalid bounded run configuration')
    return run(args)


if __name__=='__main__': raise SystemExit(main())
