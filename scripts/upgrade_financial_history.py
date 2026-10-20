#!/usr/bin/env python3
"""专用历史升级：默认只读；输出隔离快照可验收，最终采用须显式apply和计划。"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from simulated_bank.connected_assets import upgrade_financial_history
from simulated_bank import BankError


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bank-db',required=True);p.add_argument('--parent-db',required=True);p.add_argument('--financial-db',required=True)
    p.add_argument('--output-bank',help='从源银行库建立新只读快照，随后只对隔离副本执行')
    p.add_argument('--apply',action='store_true');p.add_argument('--expected-plan');p.add_argument('--quality-receipt');p.add_argument('--receipt',required=True)
    a=p.parse_args();bank=Path(a.bank_db).resolve();receipt=Path(a.receipt).resolve()
    protected={bank,Path(a.parent_db).resolve(),Path(a.financial_db).resolve()}
    if receipt in protected or receipt.exists():p.error('receipt必须为新文件且不能覆盖库')
    if a.output_bank:
        copied=Path(a.output_bank).resolve()
        if copied in protected or copied==receipt or copied.exists():p.error('银行快照必须为新路径')
        copied.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(bank.as_uri()+'?mode=ro',uri=True)) as source, closing(sqlite3.connect(copied)) as target:source.backup(target)
        copied.chmod(0o600);bank=copied
    expected=json.loads(Path(a.expected_plan).read_text()) if a.expected_plan else None
    try:
        result=upgrade_financial_history(bank,a.parent_db,a.financial_db,apply=a.apply,expected=expected,quality_receipt=a.quality_receipt)
    except (BankError,ValueError,OSError,sqlite3.Error) as error:
        print(json.dumps({'applied':False,'error':error.as_dict() if isinstance(error,BankError) else str(error)},ensure_ascii=False));return 1
    result['isolated_copy']=bool(a.output_bank)
    receipt.parent.mkdir(parents=True,exist_ok=True)
    temp=receipt.with_suffix(receipt.suffix+'.tmp')
    with temp.open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(temp,receipt)
    print(json.dumps({'applied':result['applied'],'changed':result['changed'],'isolated_copy':bool(a.output_bank),'receipt':str(receipt)},ensure_ascii=False));return 0

if __name__=='__main__':raise SystemExit(main())
