#!/usr/bin/env python3
"""启动前置条件自检：一次列出 Navren 缺什么、怎么补。

启动脚本 `scripts/serve_financial_frontend.py` 只在第一个问题处报错退出，
排查多库、多密钥环境时容易来回试错。本脚本复用同一份探测逻辑
(`probe_prerequisites`)，把所有检查项一次列全，不修改任何文件、不启动服务。

用法：
    python3 scripts/check_environment.py
    python3 scripts/check_environment.py --state-dir <实例目录> --financial-database <库>
    python3 scripts/check_environment.py --json

退出码：全部通过 0；有失败项 1；参数错误 2。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from serve_financial_frontend import DEFAULT_STATE, probe_prerequisites  # noqa: E402

HINTS = {
    'STATE_DIR_UNAVAILABLE': '状态目录不存在。指向已有体验实例的状态目录；仓库不附带封存的数据库。',
    'REQUIRED_DATABASE_UNAVAILABLE': '启动库缺失。状态目录需含 ordinary-bank / ordinary-bank.personal / ordinary-auth / ordinary-knowledge 四个 sqlite，财务库与市场库单独可读。',
    'OFFICIAL_MODEL_KEYS_REQUIRED_USE_CC_WITH_SECRETS': '模型密钥缺失。用 services/cc-secrets/cc-with-secrets 包裹启动命令，不要写进 .env 或仓库。',
    'EXISTING_PERSONAL_KEY_REQUIRED_USE_CC_WITH_SECRETS': 'NAVREN_PERSONAL_DATA_KEY 缺失。该密钥须与实例私库配套，同样走 cc-with-secrets。',
    'PERSONAL_KEY_CHECK_UNAVAILABLE': '私库没有密钥校验值，说明不是本系统的注册实例库。',
    'EXISTING_PERSONAL_KEY_MISMATCH': '密钥与私库校验值不匹配。换回该实例登记的密钥，或改用对应密钥的实例目录。',
    'SEALED_FOUR_TEMPLATES_MISMATCH': '财务库角色不是封存的四位体验角色。用 --financial-database 指定正确的封存库。',
    'FINANCIAL_SCHEMA_UNSUPPORTED': '财务库 schema 版本不符（需要 navren.financial.v1）。',
    'REGISTERED_THREE_YEAR_TEMPLATE_REQUIRED': '财务库不是三年真实感历史版本（history_profile=realistic_three_year）。',
    'ISOLATED_MCP_SDK_UNAVAILABLE': '隔离 MCP SDK 缺失。需存在可执行的 outputs/financial-mcp/venv/bin/python。',
    'INVALID_LOCAL_PORTS': '端口需在 1024–65535 之间，且页面端口与上游端口不同。',
}


def arguments():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--financial-database', type=Path,
                        default=ROOT / 'outputs/financial-data/navren-financial-v3.sqlite3')
    parser.add_argument('--market-database', type=Path,
                        default=Path.home() / 'Library/Application Support/Navren/market.sqlite3')
    parser.add_argument('--port', type=int,
                        default=os.environ.get('NAVREN_PORT', os.environ.get('QINGZHOU_PORT', '60291')))
    parser.add_argument('--upstream-port', type=int, default=60262)
    parser.add_argument('--json', action='store_true', help='输出机器可读结果')
    return parser.parse_args()


def main() -> int:
    args = arguments()
    paths, _metadata, findings = probe_prerequisites(args)
    failed = [item for item in findings if item['ok'] is False]
    skipped = [item for item in findings if item['ok'] is None]
    if args.json:
        print(json.dumps({
            'ok': not failed,
            'state_dir': str(args.state_dir),
            'resolved': {name: str(path) for name, path in paths.items()},
            'findings': findings,
        }, ensure_ascii=False, indent=2))
        return 0 if not failed else 1

    width = max(len(item['check']) for item in findings)
    summary = f'Navren 启动前置条件自检（{len(findings)} 项，失败 {len(failed)} 项'
    summary += f'，跳过 {len(skipped)} 项）' if skipped else '）'
    print(summary)
    print(f'状态目录：{args.state_dir}')
    print()
    marks = {True: 'OK  ', False: 'FAIL', None: 'SKIP'}
    for item in findings:
        detail = f"  {item['detail']}" if item['detail'] else ''
        print(f'{marks[item["ok"]]}  {item["check"]:<{width}}  {item["code"]}{detail}')
    if failed:
        print('\n修复建议：')
        for item in failed:
            print(f'  · {item["code"]}：{HINTS.get(item["code"], "见 README「快速开始」。")}')
        return 1
    print('\n全部前置条件满足，可以启动 scripts/serve_financial_frontend.py。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
