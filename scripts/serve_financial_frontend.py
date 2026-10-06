#!/usr/bin/env python3
"""原地址启用独立财务库；复用既有登录、业务、个人库，不运行模型验收。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'outputs/financial-frontend'
DEFAULT_STATE = ROOT / 'outputs/window-progress/integration/execution-20261006/ordinary-runtime-run-3/state'


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--financial-database', type=Path,
        default=ROOT / 'outputs/financial-data/navren-financial-v2.sqlite3')
    parser.add_argument('--market-database', type=Path,
        default=Path.home() / 'Library/Application Support/Navren/market.sqlite3')
    parser.add_argument('--port', type=int, default=60291)
    parser.add_argument('--upstream-port', type=int, default=60262)
    parser.add_argument('--prepare', action='store_true')
    return parser.parse_args()


def readable(path):
    path = path.expanduser().resolve(strict=True)
    if not path.is_file():
        raise RuntimeError('REQUIRED_DATABASE_UNAVAILABLE')
    return path


def connect(path):
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)


def prepare(args):
    state = args.state_dir.expanduser().resolve(strict=True)
    paths = {name: readable(state / filename) for name, filename in {
        'bank': 'ordinary-bank.sqlite3', 'personal': 'ordinary-bank.personal.sqlite3',
        'auth': 'ordinary-auth.sqlite3', 'knowledge': 'ordinary-knowledge.sqlite3',
    }.items()}
    paths['financial'] = readable(args.financial_database)
    paths['market'] = readable(args.market_database)
    if not os.environ.get('AIPING_API_KEY'):
        raise RuntimeError('AIPING_KEY_REQUIRED_USE_CC_WITH_SECRETS')
    key = os.environ.get('NAVREN_PERSONAL_DATA_KEY')
    if not key:
        raise RuntimeError('EXISTING_PERSONAL_KEY_REQUIRED_USE_CC_WITH_SECRETS')
    from cryptography.fernet import Fernet
    with connect(paths['personal']) as db:
        check = db.execute("SELECT value FROM private_crypto WHERE key='key_check'").fetchone()
    if not check or not check[0].startswith('pd1:'):
        raise RuntimeError('PERSONAL_KEY_CHECK_UNAVAILABLE')
    if Fernet(key.encode('ascii')).decrypt(check[0][4:].encode()).decode() != 'navren-personal-v1':
        raise RuntimeError('EXISTING_PERSONAL_KEY_MISMATCH')
    with connect(paths['bank']) as db:
        bindings = dict(db.execute("SELECT user_id,workspace_id FROM navren_demo_accounts WHERE status='ready'"))
    with connect(paths['financial']) as db:
        subjects = dict(db.execute('SELECT subject_id,workspace_id FROM subjects'))
        metadata = dict(db.execute('SELECT key,value FROM metadata'))
    if bindings != subjects or len(subjects) != 4:
        raise RuntimeError('EXISTING_FOUR_WORKSPACES_MISMATCH')
    if metadata.get('schema_version') != 'navren.financial.v1':
        raise RuntimeError('FINANCIAL_SCHEMA_UNSUPPORTED')
    sdk = ROOT / 'outputs/financial-mcp/venv/bin/python'
    if not sdk.is_file() or not os.access(sdk, os.X_OK):
        raise RuntimeError('ISOLATED_MCP_SDK_UNAVAILABLE')
    if not 1024 <= args.port <= 65535 or not 1024 <= args.upstream_port <= 65535 or args.port == args.upstream_port:
        raise RuntimeError('INVALID_LOCAL_PORTS')
    return paths, metadata


def save_receipt(value):
    OUTPUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = OUTPUT / 'runtime.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def main():
    args = arguments()
    paths, metadata = prepare(args)
    base = {
        'source_root': str(ROOT), 'data_source': 'navren_synthetic',
        'dataset_version': metadata['dataset_version'],
        'financial_database': str(paths['financial']),
        'reused_databases': {name: str(paths[name]) for name in ('bank', 'personal', 'auth', 'knowledge')},
        'welcome_url': f'http://127.0.0.1:{args.port}/',
        'upstream_port': args.upstream_port, 'existing_four_workspaces_verified': True,
        'existing_personal_key_verified': True, 'model_provider': 'aiping',
        'model_checks': 0, 'source_static_files': True,
    }
    if args.prepare:
        print(json.dumps({**base, 'prepared': True, 'runtime_enabled': False}, ensure_ascii=False))
        return
    # 这些路径均来自可信启动配置，模型不能覆盖；保留原个人库文件名与加密密钥。
    os.environ.update(NAVREN_FINANCIAL_DB=str(paths['financial']),
        NAVREN_SIM_DB=str(paths['bank']), NAVREN_KNOWLEDGE_DB=str(paths['knowledge']),
        NAVREN_MARKET_DB=str(paths['market']),
        NAVREN_FUND_CACHE=str(args.state_dir.resolve() / 'fund-cache'),
        NAVREN_ALLOW_CLOUD_ASR='0')
    os.environ.pop('DEEPSEEK_API_KEY', None)
    OUTPUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ['NAVREN_MODEL_CALL_LOG'] = str(OUTPUT / 'model-calls.jsonl')
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    from local_knowledge.store import LocalKnowledgeProvider
    from local_knowledge.runtime import EvidenceRuntime
    from voice_gateway import VoiceGateway
    from simulated_bank import BankError
    from server import create_server
    from welcome_auth import create_welcome_server

    def unavailable_voice(*_args, **_kwargs):
        raise BankError('VOICE_NOT_CONFIGURED', '当前本地服务未配置语音。', 503)

    provider = LocalKnowledgeProvider(paths['knowledge'])
    evidence = EvidenceRuntime(provider, background=False)
    voice = VoiceGateway(key='', url='', allow_cloud_asr=False, task_factory=unavailable_voice)
    stopped = threading.Event()
    instances, threads = [], []
    try:
        business = create_server(port=args.upstream_port, database=paths['bank'],
            voice=voice, evidence_runtime=evidence, market_database=paths['market'])
        instances.append(business)
        welcome = create_welcome_server(port=args.port, database=paths['auth'],
            root=ROOT, upstream_port=args.upstream_port)
        instances.append(welcome)
        for instance in instances:
            thread = threading.Thread(target=instance.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopped.set())
        receipt = {**base, 'pid': os.getpid(), 'runtime_enabled': True,
            'server_deployed': False, 'app_release': business.app_release,
            'started_at': datetime.now(timezone.utc).isoformat()}
        save_receipt(receipt)
        print(json.dumps({'ready': True, 'pid': os.getpid(), 'welcome_url': base['welcome_url'],
            'dataset_version': metadata['dataset_version']}, ensure_ascii=False), flush=True)
        stopped.wait()
    finally:
        for instance, thread in zip(instances, threads):
            if thread.is_alive():
                instance.shutdown()
        for instance in instances:
            instance.server_close()
        evidence.close()


if __name__ == '__main__':
    main()
