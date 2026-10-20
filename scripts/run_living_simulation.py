#!/usr/bin/env python3
"""对明确配置的注册实例库补跑一次；无HTTP、无模型、无短信、无任意时间入口。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description='Navren持续模拟单次补跑')
    parser.add_argument('--bank-db',type=Path,required=True)
    parser.add_argument('--financial-db',type=Path,required=True)
    parser.add_argument('--limit',type=int,default=20)
    parser.add_argument('--max-days',type=int,default=7)
    parser.add_argument('--max-events',type=int,default=200)
    args=parser.parse_args()
    for value,maximum,label in ((args.limit,100,'limit'),(args.max_days,31,'max-days'),(args.max_events,2000,'max-events')):
        if not 1<=value<=maximum:parser.error(label+'超出允许批量范围。')
    for path in (args.bank_db,args.financial_db):
        if not path.is_file() or path.is_symlink():parser.error('数据库须为显式配置的既有普通文件。')
    from finance_gateway import FinanceGateway
    gateway=FinanceGateway(args.bank_db,financial_database=args.financial_db,registered_mode=True)
    results=gateway.living_simulation.run_ready_instances(limit=args.limit,max_days=args.max_days,max_events=args.max_events)
    failed=any(row['status']=='error' for row in results)
    print(json.dumps({'status':'partial_failure' if failed else 'ok','outcomes':results},ensure_ascii=False))
    return 1 if failed else 0


if __name__=='__main__':raise SystemExit(main())
