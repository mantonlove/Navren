#!/usr/bin/env python3
"""
市场热点新闻池同步脚本。
用法：python3 scripts/sync_market_news.py
在 server.py 启动时自动检查是否为今日数据，过期则刷新。
生产环境建议 cron：0 8 * * * cd /path/to/Navren && python3 scripts/sync_market_news.py
"""

import json, os, time, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NEWS_PATH = 'js/market_news.json'
NEWS_JS_PATH = 'js/market_news.js'


def needs_update():
    """检查新闻池是否需要更新（非今日数据）"""
    if not os.path.exists(NEWS_PATH):
        return True
    try:
        with open(NEWS_PATH) as f:
            data = json.load(f)
        update_time = data.get('update_time', '')
        today = time.strftime('%Y-%m-%d')
        return not update_time.startswith(today)
    except Exception:
        return True


def refresh_news():
    """刷新新闻池：当前为本地静态池（demo阶段）。
    生产环境可接入新闻API：新浪财经/财联社/EastMoney/同花顺等。"""
    print(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] 刷新市场热点新闻池...')

    # 读取现有新闻数据
    if os.path.exists(NEWS_PATH):
        with open(NEWS_PATH) as f:
            data = json.load(f)
    else:
        print('  新闻池文件不存在，将使用本地静态数据')
        return False

    # 更新 fetch 时间
    now = time.strftime('%Y-%m-%dT%H:%M:%S+08:00')
    data['update_time'] = now
    for item in data.get('news', []):
        item['fetched_at'] = now

    # 写入 JSON
    with open(NEWS_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # 写入 JS（前端可直接 <script> 引入）
    with open(NEWS_JS_PATH, 'w', encoding='utf-8') as f:
        f.write('// Navren — 市场热点新闻池\n')
        f.write(f'// 自动生成于 {now}\n')
        f.write(f'// 来源：{data.get("source", "")}\n')
        f.write(f'const MARKET_NEWS = ')
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write(';\n')

    print(f'  完成：{len(data.get("news", []))} 条新闻')
    print(f'  更新时间：{now}')
    print(f'  文件：{NEWS_PATH}, {NEWS_JS_PATH}')
    return True


if __name__ == '__main__':
    if needs_update():
        ok = refresh_news()
        sys.exit(0 if ok else 1)
    else:
        print('新闻池已是最新（今日数据），无需更新')
