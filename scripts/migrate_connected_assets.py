#!/usr/bin/env python3
"""显式采用模拟银行分账户；默认 dry-run，不启动服务，不签发会话或操作。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from simulated_bank.connected_assets import migrate_connected_assets
from simulated_bank import BankError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bank-db',required=True)
    parser.add_argument('--financial-db',required=True)
    choices = parser.add_mutually_exclusive_group()
    choices.add_argument('--apply',action='store_true',help='原子采用；已有操作的未迁移工作区拒绝迁移')
    choices.add_argument('--dry-run',action='store_true',help='仅生成计划（默认）')
    parser.add_argument('--expected-versions',help='先前 dry-run 记录的 workspace_id -> version JSON 文件')
    parser.add_argument('--receipt',help='保存无凭据迁移记录的路径')
    args = parser.parse_args()
    path = Path(args.receipt).expanduser().resolve() if args.receipt else None
    protected = {Path(args.bank_db).expanduser().resolve(),Path(args.financial_db).expanduser().resolve()}
    if path in protected:
        parser.error('迁移记录不能覆盖数据库')
    expected = json.loads(Path(args.expected_versions).read_text()) if args.expected_versions else None
    try:
        result = migrate_connected_assets(args.bank_db,args.financial_db,apply=args.apply,expected_versions=expected)
    except BankError as error:
        print(json.dumps({'applied':False,'error':error.as_dict()},ensure_ascii=False))
        return 1
    if path:
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
