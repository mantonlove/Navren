#!/usr/bin/env python3
"""构建新版本文件，不覆盖旧源或已存在的版本。"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from financial_data_service.build import build

if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    p=argparse.ArgumentParser()
    p.add_argument('--source',default=str(root/'outputs/contest-finances/financial_data.sqlite3'))
    p.add_argument('--profiles',default=str(root/'outputs/contest-finances/contest_profiles.json'))
    p.add_argument('--output',default=str(root/'outputs/financial-data/navren-financial-v1.sqlite3'))
    args=p.parse_args()
    result=build(args.source,args.profiles,args.output)
    Path(args.output).resolve().with_suffix('.build.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))
