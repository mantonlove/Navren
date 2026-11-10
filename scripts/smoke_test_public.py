#!/usr/bin/env python3
"""线上实例冒烟检查：确认一个已部署的 Navren 对外可用。

只发匿名 GET，不登录、不写数据；用于部署后与日常巡检。
检查项：首页、首页引用的静态资源、欢迎会话接口、公开市场接口、
未登录访问需鉴权页面时的跳转。任一失败退出 1。

用法：
    python3 scripts/smoke_test_public.py
    python3 scripts/smoke_test_public.py --base-url http://127.0.0.1:60291
    python3 scripts/smoke_test_public.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = 'https://navren.cn'
ASSET_RE = re.compile(r'(?:src|href)="(/[^"?#]+\.(?:js|css|png|jpg|jpeg|svg|webp|woff2?))', re.IGNORECASE)
REDIRECT_CODES = (301, 302, 303, 307, 308)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, timeout, follow=True):
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, method='GET', headers={'User-Agent': 'navren-smoke/1.0'})
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(2_000_000)
            return response.status, dict(response.headers), body
    except urllib.error.HTTPError as error:
        body = error.read(200_000) if error.fp else b''
        return error.code, dict(error.headers or {}), body


def decode(body):
    return body.decode('utf-8', 'replace')


def check_home(base, timeout):
    status, _headers, body = fetch(base + '/', timeout)
    text = decode(body)
    if status != 200:
        return False, f'HTTP {status}'
    if 'Navren' not in text:
        return False, '页面未包含 Navren 字样'
    title = re.search(r'<title[^>]*>(.*?)</title>', text, re.S | re.I)
    return True, f'200，title={title.group(1).strip() if title else "?"}'


def collect_assets(base, timeout):
    status, _headers, body = fetch(base + '/', timeout)
    if status != 200:
        return None, f'首页 HTTP {status}，无法收集资源'
    assets = []
    for path in ASSET_RE.findall(decode(body)):
        if path.startswith('//') or path in assets:
            continue
        assets.append(path)
    return assets[:40], f'首页引用 {len(assets)} 个本地静态资源'


def check_assets(base, timeout, assets):
    failures = []
    for path in assets:
        status, _headers, _body = fetch(base + path, timeout)
        if status != 200:
            failures.append(f'{path} -> {status}')
    if failures:
        return False, '; '.join(failures[:5])
    return True, f'{len(assets)}/{len(assets)} 均 200'


def check_json(base, path, timeout, required_keys=()):
    status, headers, body = fetch(base + path, timeout)
    if status != 200:
        return False, f'HTTP {status}'
    if 'json' not in (headers.get('Content-Type') or '').lower():
        return False, f'Content-Type={headers.get("Content-Type")}'
    try:
        payload = json.loads(decode(body))
    except json.JSONDecodeError as error:
        return False, f'JSON 解析失败：{error}'
    missing = [key for key in required_keys if key not in payload]
    if missing:
        return False, f'缺少字段 {missing}'
    if isinstance(payload, dict) and not payload:
        return False, '返回空对象'
    return True, '200 JSON'


def check_redirect(base, path, timeout):
    status, headers, _body = fetch(base + path, timeout, follow=False)
    if status not in REDIRECT_CODES:
        return False, f'未登录访问返回 {status}，预期重定向'
    location = headers.get('Location') or ''
    return True, f'{status} -> {location}'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base-url', default=DEFAULT_BASE)
    parser.add_argument('--timeout', type=float, default=15.0)
    parser.add_argument('--json', action='store_true', help='输出机器可读结果')
    args = parser.parse_args()

    base = args.base_url.rstrip('/')
    rows = []

    def run(seq, name, func):
        try:
            ok, detail = func()
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as error:
            ok, detail = False, f'{type(error).__name__}: {error}'
        rows.append({'seq': seq, 'check': name, 'ok': bool(ok), 'detail': detail})

    run(10, '首页', lambda: check_home(base, args.timeout))
    try:
        assets, detail = collect_assets(base, args.timeout)
    except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as error:
        assets, detail = None, f'{type(error).__name__}: {error}'
    if assets is None:
        rows.append({'seq': 20, 'check': '静态资源', 'ok': False, 'detail': detail})
    else:
        run(20, '静态资源', lambda: check_assets(base, args.timeout, assets))
    run(30, '欢迎会话接口', lambda: check_json(base, '/api/welcome/auth/session', args.timeout))
    run(40, '公开市场接口', lambda: check_json(base, '/api/public-market/v1/hotspots', args.timeout))
    run(50, '未登录跳转', lambda: check_redirect(base, '/chat.html', args.timeout))

    failed = [row for row in rows if not row['ok']]
    if args.json:
        print(json.dumps({'base_url': base, 'ok': not failed, 'checks': rows}, ensure_ascii=False, indent=2))
    else:
        width = max(len(row['check']) for row in rows) + 2
        print(f'目标 {base}')
        for row in rows:
            mark = 'OK  ' if row['ok'] else 'FAIL'
            print(f'{mark} {row["check"]:<{width}} {row["detail"]}')
        print('结果：' + ('全部通过' if not failed else f'{len(failed)} 项失败'))
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
