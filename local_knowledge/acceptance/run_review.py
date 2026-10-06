#!/usr/bin/env python3
"""读取冻结人工预期；只在源库的临时 backup 克隆上运行公共证据验收。"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agent_runtime.knowledge_retrieval import KnowledgeRetrieval
from local_knowledge import LocalKnowledgeProvider


def clone_database(source,target):
    source,target=Path(source).resolve(),Path(target).resolve()
    if source==target or target.exists():raise ValueError('clone_target_must_be_new')
    read=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    write=sqlite3.connect(target)
    try:read.backup(write)
    finally:write.close();read.close()
    target.chmod(0o600)


def verify_original(provider,suite):
    results=[]
    with provider.db() as db:
        stored=db.execute('SELECT payload FROM raw_artifacts WHERE sha256=?',(suite['raw_sha256'],)).fetchone()
        if stored is None:return [{'passed':False,'reason':'reviewed_raw_missing'}]
        raw=bytes(stored[0]);body=raw.decode('utf-8').strip()
        original=json.loads(body[body.index('(')+1:body.rindex(')')])
        for product in suite['products']:
            record=original['productPresentation'][product['record_index']]
            version=db.execute('''SELECT v.*,d.source_id,d.source_url FROM document_versions v JOIN documents d
                ON d.id=v.document_id WHERE v.id=?''',(product['version_id'],)).fetchone()
            passed=bool(version and hashlib.sha256(raw).hexdigest()==suite['raw_sha256']
                and original['extractDate']==suite['source_date']
                and all(record.get(k)==value for k,value in product['expected_fields'].items())
                and json.loads(version['parsed_text'])==record
                and version['source_id']==suite['source_id'] and version['source_url']==suite['source_url'])
            results.append({'product_id':product['product_id'],'passed':passed,
                'original_locator':'productPresentation['+str(product['record_index'])+']',
                'reviewed_fields':product['expected_fields']})
    return results


def evaluate(provider,suite,case):
    selected=next(p for p in suite['products'] if p['product_id']==case['product_id'])
    if case.get('mutation')=='withdraw_version':provider.withdraw(selected['version_id'])
    elif case.get('mutation')=='conflict_fees':
        provider.block_conflict(product_id=case['product_id'],facets=['fees'],from_on=suite['source_date'],reason='isolated_acceptance_conflict')
    elif case.get('mutation')=='revoke_source':provider.set_source_state(suite['source_id'],outbound_allowed=False)
    request={'query':case['question'],'scope':'public','product_id':case['product_id'],
        'share_class_id':case.get('share_class_id'),'channel_id':case.get('channel_id','WEBANK'),
        'as_of':case['as_of'],'facets':case['facets'],'limit':4}
    pinned=provider.for_release(suite['reviewed_release_id'])
    result=KnowledgeRetrieval(pinned).search(**{k:v for k,v in request.items() if k!='scope'})
    hits=result['hits'];codes={g['code'] for g in result['coverage_gaps']}
    expect=case['expect'];passed=not hits if expect['mode']=='no_hits' else bool(hits)
    if expect['mode']=='no_hits':passed=passed and expect['gap'] in codes
    else:passed=passed and all(expect['contains'] in hit['excerpt'] for hit in hits)
    audits=[]
    with provider.db() as db:
        for hit in hits:
            row=db.execute('''SELECT v.parsed_text,v.published_on,c.data_on FROM document_versions v JOIN chunks c
                ON c.version_id=v.id WHERE v.id=? AND c.id=?''',(hit['document_version_id'],hit['citation_id'])).fetchone()
            locator=hit['locator']
            same_original=bool(row and row[0][locator['start_char']:locator['end_char']]==hit['excerpt'])
            identity=(hit['product_id']==case['product_id'] and hit['share_class_id']==case.get('share_class_id')
                and hit['channel_id']==request['channel_id'])
            frozen_version=(hit['document_version_id']==selected['version_id'] and hit['release_id']==suite['reviewed_release_id']
                and hit['source_url']==suite['source_url'] and hit['published_on']==suite['source_date']
                and row is not None and row[1]==suite['source_date'] and row[2]==suite['source_date'])
            trace=pinned.graph_trace(hit['citation_id'],suite['reviewed_release_id'],case['as_of'],request=request)
            passed=passed and same_original and identity and frozen_version and bool(trace['paths'])
            audits.append({'citation_id':hit['citation_id'],'version_id':hit['document_version_id'],
                'excerpt':hit['excerpt'],'locator':locator,'source_url':hit['source_url'],
                'published_on':hit['published_on'],'effective_from':hit['effective_from'],'effective_to':hit['effective_to'],
                'same_original':same_original,'identity_matches':identity,'frozen_version_matches':frozen_version,
                'data_on':row[2] if row else None,'graph_trace':trace})
    return {'id':case['id'],'passed':passed,'question':case['question'],'request':request,
        'expected':expect,'status':result['status'],'coverage_gaps':result['coverage_gaps'],'hits':audits,
        'mutation_is_test_only':bool(case.get('mutation')),'agent_final_answer_verified':False}


def run(source,suite):
    with tempfile.TemporaryDirectory(prefix='navren-public-review-') as temporary:
        base=Path(temporary)/'base.sqlite3';clone_database(source,base)
        provider=LocalKnowledgeProvider(base)
        status=provider.status();original=verify_original(provider,suite)
        results=[]
        if all(r['passed'] for r in original):
            for case in suite['cases']:
                target=Path(temporary)/(case['id']+'.sqlite3');clone_database(base,target)
                results.append(evaluate(LocalKnowledgeProvider(target),suite,case))
        return {'schema_version':1,'source_opened_read_only':True,'mutations_on_temporary_clones_only':True,
            'reviewed_release_id':suite['reviewed_release_id'],'current_provider_status':status,
            'reviewed_raw_sha256':suite['raw_sha256'],'source_date':suite['source_date'],
            'source_checks':original,'cases':results,'case_count':len(suite['cases']),
            'passed_count':sum(r['passed'] for r in results),'failed_count':sum(not r['passed'] for r in results),
            'original_checks_passed':all(r['passed'] for r in original),
            'agent_final_answer_verified':False,
            'code_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [ROOT/'local_knowledge/store.py',ROOT/'local_knowledge/graph.py',ROOT/'local_knowledge/importers.py',
                          ROOT/'agent_runtime/knowledge_retrieval.py',Path(__file__)]}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-database',required=True)
    parser.add_argument('--suite',default=str(Path(__file__).with_name('reviewed_cases.json')))
    parser.add_argument('--output',required=True)
    args=parser.parse_args();suite=json.loads(Path(args.suite).read_text(encoding='utf-8'))
    with (patch('socket.create_connection',side_effect=AssertionError('network_forbidden')),
          patch('socket.socket.connect',side_effect=AssertionError('network_forbidden'))):
        report=run(args.source_database,suite)
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('original_checks_passed','case_count','passed_count','failed_count','agent_final_answer_verified')},ensure_ascii=False))
    return 0 if report['original_checks_passed'] and report['passed_count']==report['case_count'] else 1


if __name__=='__main__':raise SystemExit(main())
