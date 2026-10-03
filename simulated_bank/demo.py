"""通过实际HTTP接口执行三场景验收，保存脱敏证据；凭证只在内存。"""
import argparse
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


def atomic_json(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


def run(base_url, output):
    parsed = urlsplit(base_url)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.path not in ('','/'):
        raise ValueError('验收仅使用本机模拟服务地址')
    base_url = base_url.rstrip('/')
    prefix = '/api/sim-bank'
    trace = []

    def request(path, data=None, token=None, expected=200):
        headers={'Content-Type':'application/json'}
        if token:
            headers['Authorization']='Bearer '+token
        req=urllib.request.Request(base_url+path,data=json.dumps(data).encode() if data is not None else None,headers=headers)
        try:
            response=urllib.request.urlopen(req,timeout=10)
        except urllib.error.HTTPError as error:
            response=error
        with response:
            status,result=response.status,json.loads(response.read())
        trace.append({'method':'POST' if data is not None else 'GET','path':path,'http_status':status})
        assert status==expected,(path,status,result)
        return result

    assert request('/health')['environment']=='simulation'
    contract=request('/openapi.json')
    credentials=request(prefix+'/workspaces',{},expected=201)
    human,agent,pin=credentials['session_token'],credentials['agent_token'],credentials['human_verification_code']
    reads={name:request(prefix+'/'+name,token=agent) for name in (
        'capabilities','snapshot','transactions','analysis','anomalies','periodic-charges',
        'mandates','products','profile','holdings','orders','audit')}
    archive=request(prefix+'/customer-archive',token=agent)
    data_contract=request(prefix+'/data-contract',token=agent)
    annual=request(prefix+'/annual-analysis?year=2025',token=agent)
    assert archive['integrity']['ok'] and len(archive['coverage']['scenes'])==6
    assert len(data_contract['entities'])==55 and len(data_contract['database_tables'])==72
    assert annual['complete_calendar_year'] and annual['archived_count']==96
    atomic_json(output.parent/'客户全档案初始样本-v2.json',archive)
    atomic_json(output.parent/'完整数据库字段字典-v2.json',data_contract)
    initial=reads['snapshot']
    assert initial['total_cash_minor']==2439000 and initial['holding_principal_minor']==800000
    assert len(reads['transactions'])==101 and len(reads['products'])==6
    assert len(reads['anomalies']['duplicates'])==1 and len(reads['anomalies']['high_amounts'])==1
    transaction_ids={row['id'] for row in reads['transactions']}
    evidence_items=reads['anomalies']['duplicates']+reads['anomalies']['high_amounts']+reads['periodic-charges']['candidates']
    assert all(set(item['transaction_ids'])<=transaction_ids for item in evidence_items)
    assert len([item for item in reads['periodic-charges']['candidates'] if item['mandate_id']])==3
    account=next(a['id'] for a in initial['accounts'] if a['name']=='模拟日常账户')
    comparison=request(prefix+'/products/compare',{'product_ids':['SIM-CASH-01','SIM-EQUITY-04']},agent)
    assert comparison[0]['profile_eligible'] and not comparison[1]['profile_eligible']

    mandate=next(m for m in reads['mandates'] if m['merchant']=='模拟视频会员')
    close=request(prefix+'/operations/prepare',dict(action='close_mandate',account_id=account,
                  mandate_id=mandate['id'],idempotency_key='demo-close-video'),agent)
    close_body=dict(confirmed=True,confirmation_digest=close['confirmation_digest'])
    close_result=request(prefix+f'/operations/{close["id"]}/confirm',close_body,human)
    rechecked_mandate=next(m for m in request(prefix+'/mandates',token=agent) if m['id']==mandate['id'])
    assert rechecked_mandate['status']=='closed' and rechecked_mandate['merchant_contract_status']=='unchanged'

    purchase=request(prefix+'/operations/prepare',dict(action='purchase',account_id=account,
                     product_id='SIM-CASH-01',amount_minor=500000,idempotency_key='demo-purchase'),agent)
    bare=dict(confirmed=True,confirmation_digest=purchase['confirmation_digest'])
    agent_denial=request(prefix+f'/operations/{purchase["id"]}/confirm',bare,agent,expected=403)
    assert agent_denial['error']['code']=='HUMAN_REQUIRED'
    no_verification=request(prefix+f'/operations/{purchase["id"]}/confirm',bare,human,expected=409)
    assert no_verification['error']['code']=='VERIFICATION_REQUIRED'
    grant=request(prefix+f'/operations/{purchase["id"]}/verify',dict(verification_code=pin),human)
    confirmed={**bare,'verification_token':grant['verification_token']}
    executed=request(prefix+f'/operations/{purchase["id"]}/confirm',confirmed,human)
    replay=request(prefix+f'/operations/{purchase["id"]}/confirm',confirmed,human)
    assert executed['result']['order_id']==replay['result']['order_id']
    orders=request(prefix+'/orders',token=agent)
    assert len(orders)==1
    final=request(prefix+'/snapshot',token=agent)
    assert final['total_cash_minor']==1939000 and final['holding_principal_minor']==1300000
    assert final['total_cash_minor']+final['holding_principal_minor']==3239000

    stale=request(prefix+'/operations/prepare',dict(action='purchase',account_id=account,
                  product_id='SIM-CASH-01',amount_minor=100000,idempotency_key='demo-stale'),agent)
    request(prefix+'/profile',dict(risk_level=2,horizon_days=90,reserve_minor=1100000,confirmed=True),human)
    stale_result=request(prefix+f'/operations/{stale["id"]}/confirm',dict(confirmed=True,confirmation_digest=stale['confirmation_digest']),human,expected=409)
    assert stale_result['error']['code']=='STALE_CONFIRMATION'
    assert request(prefix+f'/operations/{stale["id"]}',token=agent)['state']=='invalidated'
    query=request(prefix+f'/operations/{purchase["id"]}',token=agent)
    assert query['state']=='succeeded' and query['result']['order_id']==executed['result']['order_id']
    audit=request(prefix+'/audit',token=agent)
    evidence={
        'generated_at':datetime.now().astimezone().isoformat(timespec='seconds'),
        'environment':'simulation','base_url':base_url,'workspace_id':credentials['workspace_id'],
        'seed_hash':reads['capabilities']['seed_hash'],'http_contract_operation_count':sum(len(v) for v in contract['paths'].values()),
        'sample_counts':{'transactions':101,'products':6,'mandates':3,'initial_holdings':2},
        'initial':{'cash_minor':2439000,'holding_principal_minor':800000},
        'archive_counts':archive['coverage']['counts'],
        'annual_2025':annual,
        'schema_version':archive['schema_version'],
        'after_purchase':{'cash_minor':1939000,'holding_principal_minor':1300000},
        'mandate_close':close_result['result'],'purchase':executed['result'],
        'checks':{'all_12_read_routes_work':True,'bill_evidence_traceable':True,
                  'all_six_scene_data_ready':True,'archive_integrity_verified':True,'2025_full_year_verified':True,
                  'mandate_requeried_closed':True,'merchant_contract_unchanged':True,
                  'agent_self_confirmation_denied':True,'unverified_purchase_denied':True,
                  'duplicate_confirmation_one_order':True,'cash_plus_holdings_conserved':True,
                  'stale_confirmation_invalidated':True,'order_requeried_success':True},
        'requests':trace,'audit':audit,
        'limits':['全为虚构数据和模拟业务','未模拟行情、收益和费用','尚未接入轻舟页面和对话','额外验证为模拟验证'],
    }
    text=json.dumps(evidence,ensure_ascii=False)
    assert human not in text and agent not in text and grant['verification_token'] not in text
    atomic_json(output,evidence)
    actual=json.loads(output.read_text())
    assert len(actual['requests'])>=30 and all(actual['checks'].values())
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    print(f'HTTP三场景验收通过：{len(trace)}次请求，证据保存到{output}；SHA256={digest}')


def main():
    parser=argparse.ArgumentParser(description='模拟银行HTTP验收')
    parser.add_argument('--base-url',default='http://127.0.0.1:18766')
    parser.add_argument('--out',type=Path,default=Path('outputs/模拟银行验收/HTTP运行证据.json'))
    args=parser.parse_args()
    run(args.base_url,args.out.resolve())


if __name__=='__main__':
    main()
