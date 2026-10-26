#!/usr/bin/env python3
"""真实公开资讯采集；不重写旧样例日期。"""
import argparse,hashlib,json,os,sys,tempfile
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
    p.add_argument('--controlled-recovery',action='store_true',help='明确受控修复恢复，默认原生真实采集，不计为自然重试；须由原owner/预算准入入口调用')
    p.add_argument('--source-run-id',help='真实、最新、同日来源运行ID')
    p.add_argument('--recovery-bindings-file',help='原运行真实generation/article/permission冻结绑定JSON')
    p.add_argument('--recovery-bindings-sha256',help='经授权控制清单固定的绑定文件SHA256')
    p.add_argument('--report',default='outputs/market-acceptance/latest-run.json');a=p.parse_args()
    recovery_bindings=None
    if a.controlled_recovery:
        if a.scheduled or a.force_check or a.ingest_only or a.reconstruct_history or not a.hotspots_only or a.sources:
            p.error('CONTROLLED_RECOVERY_REQUIRES_HOTSPOTS_ONLY_NO_SCHEDULE_FORCE_INGEST_RECONSTRUCT_OR_SOURCES')
        if a.source_run_id or a.recovery_bindings_file or a.recovery_bindings_sha256:
            if not a.source_run_id or not a.recovery_bindings_file or not a.recovery_bindings_sha256:
                p.error('CONTROLLED_RECOVERY_SOURCE_RUN_AND_FROZEN_BINDINGS_REQUIRED_FOR_REUSE')
            raw=Path(a.recovery_bindings_file).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=a.recovery_bindings_sha256:p.error('CONTROLLED_RECOVERY_BINDINGS_SHA256_MISMATCH')
            recovery_bindings=json.loads(raw)
    elif a.source_run_id or a.recovery_bindings_file or a.recovery_bindings_sha256:
        p.error('RECOVERY_ARGUMENTS_REQUIRE_CONTROLLED_RECOVERY')
    store=MarketStore(a.database)
    if a.reconstruct_history:
        import fcntl,uuid
        with open(str(store.path)+'.lock','a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:print('资讯任务正在运行，请稍后补录。');return 1
            result={'status':'complete_with_limits','history':store.reconstruct_daily(run_id='reconstruct-'+uuid.uuid4().hex)}
    else:
        if a.scheduled and not due(store):return 0
        result=refresh(store,source_limit=max(1,min(a.source_limit,40)),backfill_weeks=a.backfill_weeks,ingest_only=a.ingest_only,force_check=a.force_check,scheduled=a.scheduled,sources=[REGISTRY[i] for i in a.sources] if a.sources else None,hotspots_only=a.hotspots_only,run_kind='controlled_recovery' if a.controlled_recovery else None,source_run_id=a.source_run_id,recovery_bindings=recovery_bindings)
    dest=Path(a.report);dest.parent.mkdir(parents=True,exist_ok=True);fd,tmp=tempfile.mkstemp(dir=dest.parent,prefix='.market-run-')
    with os.fdopen(fd,'w') as f:json.dump(result,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(tmp,dest)
    folder=os.open(dest.parent,os.O_RDONLY)
    try:os.fsync(folder)
    finally:os.close(folder)
    print(json.dumps({k:result.get(k) for k in ['status','new_or_revised','published_events','published_weeklies','elapsed_seconds','daily_edition','history']},ensure_ascii=False))
    return 1 if result['status']=='failed' else 0
if __name__=='__main__':sys.exit(main())
