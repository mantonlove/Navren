#!/usr/bin/env python3
"""汇总离线 UI 模块检查；不启动服务、不调用模型、不写业务数据库。"""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
REGRESSIONS = [
    'tests/test_api_response.js', 'tests/test_voice_frontend.js',
    'tests/test_i18n.js', 'tests/test_account_workspace.js',
    'tests/test_account_workspace_calendar.js', 'tests/test_market_deck.js',
    'tests/test_market_history.js', 'tests/test_discover_workspace.js',
    'tests/test_market_weekly_ui.js',
]
PAGES = ['index.html', 'chat.html', 'recommend.html', 'account.html', 'mine.html', 'copilot.html']


def fingerprint():
    paths = sorted(ROOT / p for p in PAGES)
    paths += sorted((ROOT / 'css').glob('*.css')) + sorted((ROOT / 'js').glob('*.js'))
    digest = hashlib.sha256()
    files = {}
    for path in paths:
        raw = path.read_bytes()
        digest.update(path.name.encode())
        digest.update(raw)
        files[str(path.relative_to(ROOT))] = hashlib.sha256(raw).hexdigest()
    return {'release': 'navren-' + digest.hexdigest()[:12], 'files': files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node-path', default=os.environ.get('NODE_PATH', '/tmp/navren-market-ui-tests/node_modules'))
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/window-progress/ui/module-checks')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    tests = sorted(str(p.relative_to(ROOT)) for p in (ROOT / 'tests/ui_window').glob('test_*.js')) + REGRESSIONS
    environment = dict(os.environ, NODE_PATH=args.node_path)
    before = fingerprint()

    def run(test):
        started = time.monotonic()
        log = args.output / (Path(test).stem + '.log')
        command = ['node', test]
        with log.open('w') as target:
            try:
                completed = subprocess.run(command, cwd=ROOT, env=environment, stdout=target,
                                           stderr=subprocess.STDOUT, timeout=60, check=False)
                code, timeout = completed.returncode, False
            except subprocess.TimeoutExpired:
                target.write('\nUI 模块检查达到 60 秒硬超时。\n')
                code, timeout = 124, True
        return {'test': test, 'command': command, 'exit_code': code, 'timeout': timeout,
                'duration_seconds': round(time.monotonic() - started, 3),
                'log': str(log.relative_to(ROOT)),
                'test_sha256': hashlib.sha256((ROOT / test).read_bytes()).hexdigest()}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, tests))
    after = fingerprint()
    stable = before == after
    report = {'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'scope': 'offline-ui-modules-and-fixed-contracts',
              'real_browser': False, 'real_model': False, 'persistent_service_restart': False,
              'frontend_before': before, 'frontend_after': after,
              'frontend_stable_during_checks': stable,
              'passed': sum(r['exit_code'] == 0 for r in results), 'total': len(results),
              'results': results}
    (args.output / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'total': report['total'], 'release': after['release'],
                      'frontend_stable': stable,
                      'failed': [r['test'] for r in results if r['exit_code']],
                      'report': str((args.output / 'results.json').relative_to(ROOT))}, ensure_ascii=False))
    return 0 if stable and report['passed'] == report['total'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
