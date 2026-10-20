#!/usr/bin/env python3
"""真实公开资讯采集；不重写旧样例日期。"""
import argparse,json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from market_content import MarketStore
from market_pipeline import refresh,due
from market_sources import REGISTRY
def main():
    p=argparse.ArgumentParser();p.add_argument('--ingest-only',action='store_true');p.add_argument('--scheduled',action='store_true');p.add_argument('--force-check',action='store_true')
    p.add_argument('--reconstruct-history',action='store_true',help='仅按已审核真实材料报道日期补录近30天，不冒充历史实际选稿')
    p.add_argument('--hotspots-only',action='store_true',help='只核验热点并建立每日版次，不改市场周报')
    p.add_argument('--backfill-weeks',type=int,choices=(1,2),default=2);p.add_argument('--source-limit',type=int,default=30)
    p.add_argument('--sources',nargs='+',choices=[i for i,s in REGISTRY.items() if s.enabled],help='仅抽样指定来源；日常刷新省略此参数')
    p.add_argument('--database',help='指定公开资讯数据库；不使用个人银行数据库')
    p.add_argument('--report',default='outputs/market-acceptance/latest-run.json');a=p.parse_args();store=MarketStore(a.database)
    if a.reconstruct_history:
        import fcntl,uuid
        with open(str(store.path)+'.lock','a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:print('资讯任务正在运行，请稍后补录。');return 1
            result={'status':'complete_with_limits','history':store.reconstruct_daily(run_id='reconstruct-'+uuid.uuid4().hex)}
    else:
        if a.scheduled and not due(store):return 0
        result=refresh(store,source_limit=max(1,min(a.source_limit,40)),backfill_weeks=a.backfill_weeks,ingest_only=a.ingest_only,force_check=a.force_check,scheduled=a.scheduled,sources=[REGISTRY[i] for i in a.sources] if a.sources else None,hotspots_only=a.hotspots_only)
    dest=Path(a.report);dest.parent.mkdir(parents=True,exist_ok=True);fd,tmp=tempfile.mkstemp(dir=dest.parent,prefix='.market-run-')
    with os.fdopen(fd,'w') as f:json.dump(result,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(tmp,dest)
    folder=os.open(dest.parent,os.O_RDONLY)
    try:os.fsync(folder)
    finally:os.close(folder)
    print(json.dumps({k:result.get(k) for k in ['status','new_or_revised','published_events','published_weeklies','elapsed_seconds','daily_edition','history']},ensure_ascii=False))
    return 1 if result['status']=='failed' else 0
if __name__=='__main__':sys.exit(main())
