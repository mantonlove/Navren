#!/usr/bin/env python3
"""离线三年历史构建器；新输出、来源保留，不改现役。"""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from financial_data_service.realistic_history import build_realistic_history

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True);p.add_argument('--output',required=True);p.add_argument('--receipt',required=True)
    a=p.parse_args();receipt=Path(a.receipt).resolve()
    if receipt.exists() or receipt in {Path(a.source).resolve(),Path(a.output).resolve()}:p.error('receipt必须为新文件且不能覆盖库')
    result=build_realistic_history(a.source,a.output)
    receipt.parent.mkdir(parents=True,exist_ok=True);receipt.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False));return 0

if __name__=='__main__':raise SystemExit(main())
