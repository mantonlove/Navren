#!/usr/bin/env python3
"""显式离线初始化四画像；默认写隔离目录，不指向正在运行的银行库。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from demo_accounts import DemoAccountService
from finance_gateway import FinanceGateway
from simulated_bank.service import BankError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=ROOT / 'outputs/window-progress/base/isolated/bank.sqlite3')
    parser.add_argument('--source', type=Path, default=ROOT / 'outputs/contest-finances')
    parser.add_argument('--allow-offline', action='store_true', help='明确允许本次离线导入；缺省拒绝')
    args = parser.parse_args(argv)
    if not args.allow_offline:
        parser.error('必须明确传入 --allow-offline；不会隐式初始化账号。')
    live_default=Path.home() / 'Library/Application Support/Qingzhou/simulated-bank.sqlite3'
    if args.database.expanduser().resolve()==live_default.resolve():
        parser.error('禁止对现役默认库初始化；请使用停服后的独立副本。')
    try:
        gateway = FinanceGateway(args.database)
        result = DemoAccountService(gateway, args.source).initialize_offline(allow_offline=True)
    except BankError as error:
        print(json.dumps(error.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({'database': str(args.database.expanduser().resolve()), **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
