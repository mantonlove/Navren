#!/usr/bin/env python3
"""从运行时白名单抽查具体原文；不写入市场缓存或银行数据。"""
import argparse, concurrent.futures, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from market_sources import SOURCES, REGISTRY, discover, read_article, readable_article, now_iso
from market_pipeline import failure

def probe(source,limit):
    result={'id':source.id,'publisher':source.publisher,'endpoint':source.url,'articles':[],'rejected':[]}
    try:
        entries,state=discover(source,limit)
        result.update(discovery=state)
        for entry in entries:
            try:
                a=read_article(source,entry)
                if not readable_article(a):raise ValueError('not_free_full_text')
                result['articles'].append({k:a[k] for k in ('id','title','url','published_at','published_precision','publisher','origin_publisher','page_author','identity','read_scope','link_status','access_status','checked_at')})
                result['articles'][-1].update(body_characters=len(a['evidence_text']),evidence_truncated=a['evidence_truncated'])
            except Exception as error:result['rejected'].append({'url':entry['url'],**failure(error)})
        result['status']='qualified' if len(result['articles'])>=min(2,limit) else 'insufficient_sample'
    except Exception as error:result.update(status='unavailable',**failure(error))
    return result

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--sources',nargs='+',choices=list(REGISTRY),default=[s.id for s in SOURCES if s.enabled])
    parser.add_argument('--limit',type=int,choices=range(1,6),default=2)
    parser.add_argument('--out',default='outputs/market-acceptance/source-probes.json')
    args=parser.parse_args();selected=[REGISTRY[i] for i in args.sources]
    if any(not s.enabled for s in selected):parser.error('只能抽查当前启用的来源')
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(lambda s:probe(s,args.limit),selected))
    result={'checked_at':now_iso(),'method':'未登录、无Cookie、默认TLS校验，核对具体原文正文/日期/访问门槛；仅代表本次样本。','sources':rows}
    dest=Path(args.out);dest.parent.mkdir(parents=True,exist_ok=True);fd,tmp=tempfile.mkstemp(dir=dest.parent,prefix='.source-probe-')
    with os.fdopen(fd,'w') as f:json.dump(result,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(tmp,dest)
    folder=os.open(dest.parent,os.O_RDONLY)
    try:os.fsync(folder)
    finally:os.close(folder)
    print(json.dumps([{'id':r['id'],'status':r['status'],'accepted':len(r['articles']),'rejected':len(r['rejected'])} for r in rows],ensure_ascii=False))
    return int(any(r['status']!='qualified' for r in rows))

if __name__=='__main__':sys.exit(main())
