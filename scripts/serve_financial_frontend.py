#!/usr/bin/env python3
"""Navren 注册入口：验证登录后连接本人的私有体验实例。"""
from __future__ import annotations

import argparse
from contextlib import closing
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


def active_financial_database(state_dir):
    """默认重启沿用已激活的历史绑定，避免退回旧财务版本。"""
    bank = state_dir.expanduser().resolve() / 'ordinary-bank.sqlite3'
    if bank.is_file():
        with closing(sqlite3.connect(bank.as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='registered_template_sources'").fetchone():
                sealed=db.execute('''SELECT DISTINCT t.financial_file FROM registered_template_sources t
                    JOIN registered_user_instances i ON i.template_sha256=t.template_sha256 WHERE i.status='ready' ''').fetchall()
                if len(sealed)>1:
                    raise RuntimeError('已有多个封存模板版本，请明确指定本次新实例使用的财务库。')
                if sealed:
                    return bank.parent/sealed[0][0]
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='connected_asset_migrations'").fetchone():
                sources = db.execute('SELECT DISTINCT source_path FROM connected_asset_migrations').fetchall()
                if len(sources) > 1:
                    raise RuntimeError('已激活的历史数据来源不一致，请明确指定财务库后处理绑定。')
                if sources:
                    return Path(sources[0][0])
    return ROOT / 'outputs/financial-data/navren-financial-v3.sqlite3'


def ensure_runtime():
    """本机优先复用项目依赖环境，监听端口前确认流式事件组件可用。"""
    environment = ROOT / '.venv'
    interpreter = environment / 'bin/python'
    if interpreter.is_file() and Path(sys.prefix).resolve() != environment.resolve():
        os.execv(str(interpreter), [str(interpreter), str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.path.insert(0, str(ROOT))
    from agent_runtime.events import RuntimeEvents


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--financial-database', type=Path)
    parser.add_argument('--market-database', type=Path,
        default=Path.home() / 'Library/Application Support/Navren/market.sqlite3')
    parser.add_argument('--port', type=int,
        default=os.environ.get('NAVREN_PORT', os.environ.get('QINGZHOU_PORT', '60291')))
    parser.add_argument('--upstream-port', type=int, default=60262)
    parser.add_argument('--runtime-dir', type=Path, default=OUTPUT,
        help='运行收据与调用记录目录；独立实例应使用独立目录。')
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--agent-route-provider', choices=('off','native','jev'), default='off')
    args = parser.parse_args()
    if args.financial_database is None:
        args.financial_database = active_financial_database(args.state_dir)
    return args


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
        has_legacy=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='navren_demo_accounts'").fetchone()
        bindings = dict(db.execute("SELECT user_id,workspace_id FROM navren_demo_accounts WHERE status='ready'")) if has_legacy else {}
        has_instances=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='registered_user_instances'").fetchone()
        registered_count=db.execute("SELECT COUNT(*) FROM registered_user_instances WHERE status='ready'").fetchone()[0] if has_instances else 0
    with connect(paths['financial']) as db:
        subjects = dict(db.execute('SELECT subject_id,workspace_id FROM subjects'))
        metadata = dict(db.execute('SELECT key,value FROM metadata'))
    expected_personas={'tycoon','grain_farmer','working_landlady','retired_entrepreneur'}
    if set(subjects)!=expected_personas or (bindings and bindings!=subjects):
        raise RuntimeError('SEALED_FOUR_TEMPLATES_MISMATCH')
    if metadata.get('schema_version') != 'navren.financial.v1':
        raise RuntimeError('FINANCIAL_SCHEMA_UNSUPPORTED')
    if metadata.get('history_profile') != 'realistic_three_year':
        raise RuntimeError('REGISTERED_THREE_YEAR_TEMPLATE_REQUIRED')
    metadata['existing_registered_instance_count']=registered_count
    metadata['legacy_workspace_count']=len(bindings)
    sdk = ROOT / 'outputs/financial-mcp/venv/bin/python'
    if not sdk.is_file() or not os.access(sdk, os.X_OK):
        raise RuntimeError('ISOLATED_MCP_SDK_UNAVAILABLE')
    if not 1024 <= args.port <= 65535 or not 1024 <= args.upstream_port <= 65535 or args.port == args.upstream_port:
        raise RuntimeError('INVALID_LOCAL_PORTS')
    return paths, metadata


def save_receipt(value, output_dir=None):
    output_dir = OUTPUT if output_dir is None else output_dir
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = output_dir / 'runtime.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def main():
    ensure_runtime()
    args = arguments()
    paths, metadata = prepare(args)
    runtime_dir = args.runtime_dir.expanduser().resolve()
    base = {
        'source_root': str(ROOT), 'data_source': 'navren_synthetic',
        'dataset_version': metadata['dataset_version'],
        'financial_database': str(paths['financial']),
        'reused_databases': {name: str(paths[name]) for name in ('bank', 'personal', 'auth', 'knowledge')},
        'welcome_url': f'http://127.0.0.1:{args.port}/',
        'upstream_port': args.upstream_port, 'four_sealed_templates_verified': True,
        'registered_experience_mode': True,
        'existing_registered_instance_count':metadata['existing_registered_instance_count'],
        'legacy_workspace_count':metadata['legacy_workspace_count'],
        'existing_personal_key_verified': True, 'model_provider': 'aiping',
        'model_checks': 0, 'source_static_files': True,
        'runtime_python': sys.executable, 'runtime_prefix': sys.prefix,
        'stream_events_ready': True,
        'agent_route_provider': args.agent_route_provider, 'tool_routing': 'progressive',
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
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ['NAVREN_MODEL_CALL_LOG'] = str(runtime_dir / 'model-calls.jsonl')
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    from local_knowledge.store import LocalKnowledgeProvider
    from local_knowledge.runtime import EvidenceRuntime
    from voice_gateway import VoiceGateway
    from server import create_server
    from welcome_auth import create_welcome_server
    from sms_aliyun import load_sms_transport
    from simulated_bank.aliyun_sms import AliyunOperationSmsProvider

    sms_transport = load_sms_transport()
    operation_sms = AliyunOperationSmsProvider()
    from functools import partial
    from base_runtime.service_assembly import AssembledGateway
    gateway_options = {'finance_factory':partial(AssembledGateway,registered_mode=True,
        **({'sms_provider':operation_sms} if sms_transport is not None else {}))}

    provider = LocalKnowledgeProvider(paths['knowledge'])
    evidence = EvidenceRuntime(provider, background=False)
    voice = VoiceGateway(allow_cloud_asr=False)
    stopped = threading.Event()
    instances, threads = [], []
    try:
        business = create_server(port=args.upstream_port, database=paths['bank'],
            voice=voice, evidence_runtime=evidence, market_database=paths['market'],
            agent_route_provider=args.agent_route_provider, **gateway_options)
        instances.append(business)
        experience_provider=business.finance.experience_provider
        if experience_provider is None:
            raise RuntimeError('REGISTERED_EXPERIENCE_PROVIDER_REQUIRED')
        welcome = create_welcome_server(port=args.port, database=paths['auth'],
            root=ROOT, upstream_port=args.upstream_port, sms_transport=sms_transport,
            clock=business.finance.bank.clock,experience_provider=experience_provider)
        instances.append(welcome)
        operation_sms.attach(welcome.auth,business.finance.bank,experience_provider=experience_provider)
        for instance in instances:
            thread = threading.Thread(target=instance.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopped.set())
        receipt = {**base, 'pid': os.getpid(), 'runtime_enabled': True,
            'server_deployed': False, 'app_release': business.app_release,
            'speech_configured': voice.status().get('configured',False),
            'sms_configured': welcome.auth.sms.configured,
            'message_worker': business.finance.messages.status(),
            'started_at': datetime.now(timezone.utc).isoformat()}
        save_receipt(receipt, runtime_dir)
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
