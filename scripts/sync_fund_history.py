"""有界采集公开源，结果必须以实际行数与覆盖校验为准。"""
import sys,json,argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fund_history import collect,FUNDS,atomic
p=argparse.ArgumentParser();p.add_argument('--from',dest='start',default='2025-09-01');p.add_argument('--to',dest='end',default='2026-10-04');p.add_argument('--out',default='output/equity');a=p.parse_args()
reports={};errors={}
for code in FUNDS:
 try:
  d=collect(code,a.start,a.end);reports[code]=d['coverage'];print(code,len(d['navs']),d['coverage']['actual_from'],d['coverage']['actual_to'],flush=True)
 except Exception as e:errors[code]=type(e).__name__+':'+str(e)[:120]
atomic(Path(a.out)/'history_coverage.json',{'funds':reports,'errors':errors})
from fund_history import cached
atomic(Path(a.out)/'source_probe.json',{'sources':[s for code in reports for s in cached(code)['probes']],'errors':errors})
if errors:sys.exit(1)
