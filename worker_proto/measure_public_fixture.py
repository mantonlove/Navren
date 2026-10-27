"""Read-only memory/time probe for a separately deployed worker.

This script never starts/stops a service, opens a business DB, or reads a
private query. Run separately for E5 and aligned on a fresh controlled worker.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import threading
import time

from .client import EncoderProxy

PUBLIC_QUERY = '公开测试：基金赎回到账时间'
PUBLIC_DOCUMENTS = ['公开测试材料：赎回申请后两个工作日到账。',
                    'Public test material: a redemption request may settle later.']


def _integer(path: Path):
    try:return int(path.read_text().strip())
    except (FileNotFoundError, ValueError, PermissionError):return None


def _events(path: Path):
    try:return {line.split()[0]:int(line.split()[1]) for line in path.read_text().splitlines()}
    except (FileNotFoundError, ValueError, PermissionError):return {}


def _host():
    try:
        data={line.split(':',1)[0]:int(line.split(':',1)[1].split()[0])*1024
              for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line}
        return {key:data.get(key) for key in ('MemAvailable','SwapFree')}
    except (FileNotFoundError, ValueError):return {}


def measure(socket_path: Path, cgroup: Path, profile: str, timeout: float):
    assert (cgroup/'memory.current').is_file(), 'cgroup_memory_current_missing'
    before_events=_events(cgroup/'memory.events')
    before_host=_host()
    samples=[]
    stop=threading.Event()

    def sample():
        while not stop.wait(0.02):
            value=_integer(cgroup/'memory.current')
            if value is not None:samples.append(value)

    samples.append(_integer(cgroup/'memory.current'))
    thread=threading.Thread(target=sample,daemon=True)
    thread.start()
    try:
        proxy=EncoderProxy(socket_path,profile,timeout=timeout)
        profile_id=proxy.profile_id
        started=time.monotonic()
        query=proxy.encode_query(PUBLIC_QUERY)
        cold_query_seconds=time.monotonic()-started
        started=time.monotonic()
        warm_query=proxy.encode_query(PUBLIC_QUERY)
        warm_query_seconds=time.monotonic()-started
        started=time.monotonic()
        docs=proxy.encode_documents(PUBLIC_DOCUMENTS)
        documents_seconds=time.monotonic()-started
        if (query.shape != (384,) or warm_query.shape != (384,)
                or len(docs) != 2 or any(x.shape[-1] != 384 for x in docs)):
            raise RuntimeError('vector_shape_invalid')
    finally:
        stop.set()
        thread.join(timeout=1)
    after_events=_events(cgroup/'memory.events')
    return {
        'schema':'navren.public-single-profile-memory-probe/1',
        'profile':profile,
        'profile_id':profile_id,
        'fixture':'fixed-public-text-only',
        'cold_query_seconds':round(cold_query_seconds,3),
        'warm_query_seconds':round(warm_query_seconds,3),
        'two_documents_seconds':round(documents_seconds,3),
        'sampled_cgroup_current_peak_bytes':max(x for x in samples if x is not None),
        'cgroup_memory_peak_cumulative_bytes':_integer(cgroup/'memory.peak'),
        'cgroup_oom_kill_delta':after_events.get('oom_kill',0)-before_events.get('oom_kill',0),
        'host_before_bytes':before_host,
        'host_after_bytes':_host(),
        'samples':len(samples),
        'model_quality_accepted':False,
        'production_memory_accepted':False,
    }


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--socket',type=Path,required=True)
    parser.add_argument('--cgroup',type=Path,required=True)
    parser.add_argument('--profile',choices=('e5','aligned'),required=True)
    parser.add_argument('--timeout',type=float,default=90.0)
    parser.add_argument('--public-fixture-only',action='store_true',required=True)
    args=parser.parse_args()
    if args.timeout <= 0:parser.error('timeout must be positive')
    print(json.dumps(measure(args.socket,args.cgroup,args.profile,args.timeout),ensure_ascii=False))


if __name__=='__main__':
    main()
