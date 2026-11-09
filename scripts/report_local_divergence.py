#!/usr/bin/env python3
"""对比当前工作树与指定远端分支，分出「本地旧版」「本地独有」「未跟踪」三类文件。

用途：本地长期未同步的仓库，直接 `git merge` 或整树覆盖都会把远端新改动冲掉。
先看清哪些文件其实是历史旧版本、哪些是远端从未有过的本地工作，再决定合并顺序。

用法：
    python3 scripts/report_local_divergence.py            # 默认对比 origin/main
    python3 scripts/report_local_divergence.py --ref main
    python3 scripts/report_local_divergence.py --json

只读：不写文件、不改索引、不联网（需要本地已有该 ref 的对象）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> str:
    return subprocess.run(['git', '-c', 'core.quotepath=false', *args], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout


def historical_blobs(ref: str) -> set[str]:
    """ref 可达的全部 blob，用于判断本地内容是否只是历史某一版。"""
    objects = git('rev-list', '--objects', ref).splitlines()
    names = [line.split(' ', 1)[0] for line in objects if line.strip()]
    if not names:
        return set()
    check = subprocess.run(
        ['git', 'cat-file', '--batch-check=%(objectname) %(objecttype)'],
        cwd=ROOT, input='\n'.join(names) + '\n', capture_output=True, text=True, check=True,
    ).stdout
    return {line.split(' ', 1)[0] for line in check.splitlines() if line.endswith(' blob')}


def changed_paths(ref: str) -> list[str]:
    out = git('diff', '--name-only', ref, '--diff-filter=AM')
    return [line for line in out.splitlines() if line.strip()]


def classify(ref: str) -> dict[str, list[str]]:
    blobs = historical_blobs(ref)
    stale, local_new = [], []
    for path in changed_paths(ref):
        target = ROOT / path
        if not target.is_file():
            continue
        digest = subprocess.run(['git', 'hash-object', str(target)], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout.strip()
        (stale if digest in blobs else local_new).append(path)
    untracked = []
    for line in git('status', '--porcelain').splitlines():
        if line.startswith('?? '):
            untracked.append(line[3:].strip().strip('"'))
    return {'stale': sorted(stale), 'local_new': sorted(local_new), 'untracked': sorted(untracked)}


def by_top(paths: list[str]) -> list[tuple[str, int]]:
    return Counter(p.split('/', 1)[0] if '/' in p else '(根目录)' for p in paths).most_common()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--ref', default='origin/main')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--repo', type=Path, default=None, help='要分析的工作树（默认本脚本所在仓库）')
    args = parser.parse_args()
    if args.repo is not None:
        global ROOT
        ROOT = args.repo.expanduser().resolve()

    result = classify(args.ref)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f'对比基线：{args.ref}')
    print(f'  本地旧版（内容出现在 {args.ref} 历史中）：{len(result["stale"])}')
    for name, count in by_top(result['stale']):
        print(f'    {name}: {count}')
    print(f'  本地独有（{args.ref} 历史中从未出现）：{len(result["local_new"])}')
    for name, count in by_top(result['local_new']):
        print(f'    {name}: {count}')
    print(f'  未跟踪：{len(result["untracked"])}')
    for name, count in by_top(result['untracked']):
        print(f'    {name}: {count}')
    print('\n完整清单用 --json 输出；合并顺序建议见 docs/local-divergence.md。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
