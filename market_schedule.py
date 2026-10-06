"""热点按日内时段、周报按完整自然周调度；失败重试不冒充成功。"""
from __future__ import annotations
import json
from datetime import datetime, timedelta
from market_content import CN, week_periods

HOT_HOURS = (8, 12, 18)
RETRY_MINUTES = (15, 30)
MAX_ATTEMPTS = 3

def hot_slot(now):
    slots = [now.replace(hour=h, minute=30, second=0, microsecond=0) for h in HOT_HOURS]
    return max(s for s in slots if s <= now) if now >= slots[0] else slots[-1] - timedelta(days=1)

def next_hot_slot(now):
    slots = [now.replace(hour=h, minute=30, second=0, microsecond=0) for h in HOT_HOURS]
    return next((s for s in slots if s > now), slots[0] + timedelta(days=1))

def plan(store, now=None):
    now = (now or datetime.now(CN)).astimezone(CN)
    with store.db() as db:
        runs = [json.loads(r[0]) for r in db.execute('SELECT body FROM market_ingest_runs ORDER BY started_at DESC LIMIT 128')]
    jobs = []; state = {}
    slot = hot_slot(now)
    start, end = week_periods(1, now)[0]
    weekly_release = end.replace(hour=8, minute=30)
    # 周一凌晨尚未到出版时刻时，只补上一个已经到期的期号。
    if now < weekly_release:
        start -= timedelta(days=7); end -= timedelta(days=7); weekly_release -= timedelta(days=7)
    targets = {'hotspots': slot.isoformat(), 'weekly': start.date().isoformat()}
    published = {w['period']: w for w in store.versions('market_weeklies') if w['status'] == 'published'}
    for kind in ('hotspots', 'weekly'):
        records = [(r, r.get('streams', {}).get(kind, {})) for r in runs if kind in r.get('streams', {})]
        recent = [(r, s) for r, s in records if r.get('targets', {}).get(kind) == targets[kind]]
        attempts = [(r, s) for r, s in recent if r.get('status') != 'running']
        # 周报失败每天重新给予有限次数；热点到下一个时段重置次数。
        if kind == 'weekly':
            attempts = [(r, s) for r, s in attempts if datetime.fromisoformat(r['started_at']).astimezone(CN).date() == now.date()]
        success = next((r for r, s in records if s.get('status') == 'ok'), None)
        latest = records[0] if records else None
        complete = (bool(recent and recent[0][1].get('status') == 'ok') if kind == 'hotspots' else targets[kind] in published)
        normal_next = next_hot_slot(now) if kind == 'hotspots' else weekly_release + timedelta(days=7)
        available = slot if kind == 'hotspots' else weekly_release
        if kind == 'weekly' and attempts:
            available = max(available, now.replace(hour=8, minute=30, second=0, microsecond=0))
        if complete:
            next_at = normal_next
        elif len(attempts) >= MAX_ATTEMPTS:
            next_at = normal_next if kind == 'hotspots' else (now + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)
        elif attempts:
            ended = datetime.fromisoformat(attempts[0][0].get('ended_at') or attempts[0][0]['started_at']).astimezone(CN)
            next_at = max(available, ended + timedelta(minutes=RETRY_MINUTES[len(attempts)-1]))
        else:
            # 新一天的周报补跑在08:30，避免夜间连续调用模型。
            earlier = bool(recent)
            next_at = max(available, now.replace(hour=8, minute=30, second=0, microsecond=0)) if kind == 'weekly' and earlier else available
        running = next((r for r, _ in recent if r.get('status') == 'running' and now - datetime.fromisoformat(r['started_at']).astimezone(CN) < timedelta(minutes=20)), None)
        is_due = not complete and not running and now >= next_at
        if is_due: jobs.append(kind)
        state[kind] = {'target': targets[kind], 'due': is_due, 'next_check_at': next_at.isoformat(),
            'attempts': len(attempts), 'max_attempts': MAX_ATTEMPTS, 'updating': bool(running),
            'last_attempt_at': latest[0]['started_at'] if latest else None,
            'last_result': latest[1].get('status') if latest else 'not_checked',
            'last_success_at': success.get('ended_at') if success else None}
    return {'jobs': jobs, 'targets': targets, 'streams': state}

def snapshot(store, now=None):
    p = plan(store, now)
    return {'hotspots': '每日08:30、12:30、18:30（北京时间）', 'weekly': '每周一08:30，汇总上一完整周（北京时间）',
        'retry': '失败后15分钟、30分钟重试；每个时段最多3次，周报未发布则次日08:30继续补跑。',
        'runtime': '本机登录后定时检查，应用运行时也会检查；休眠或离线期间无法采集，恢复后补跑。',
        'timezone': 'Asia/Shanghai', 'streams': p['streams']}
