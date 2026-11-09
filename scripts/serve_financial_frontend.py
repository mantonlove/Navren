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


def probe_prerequisites(args):
    """逐项探测启动前置条件，供 prepare() 与 scripts/check_environment.py 共用。

    返回 (paths, metadata, findings)。findings 按固定顺序记录每一项：

        ok=True   通过
        ok=False  失败，prepare() 取序号最小的一项抛错（因此启动报错顺序不变）
        ok=None   前序项未通过，本次未执行

    诊断脚本据此一次列出全部缺口；启动路径仍只报第一个问题。
    """
    findings = []
    paths = {}
    metadata = {}
    state = None

    def record(seq, name, code, ok, detail=''):
        findings.append({'seq': seq, 'check': name, 'code': code, 'ok': ok, 'detail': detail})

    try:
        state = args.state_dir.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        record(10, 'state-dir', 'STATE_DIR_UNAVAILABLE', False, f'{type(exc).__name__}: {exc}')
    else:
        record(10, 'state-dir', 'STATE_DIR_UNAVAILABLE', True, str(state))

    if state is None:
        record(20, 'state-databases', 'REQUIRED_DATABASE_UNAVAILABLE', None, '状态目录不存在，未检查')
    else:
        missing = []
        for name, filename in {
            'bank': 'ordinary-bank.sqlite3', 'personal': 'ordinary-bank.personal.sqlite3',
            'auth': 'ordinary-auth.sqlite3', 'knowledge': 'ordinary-knowledge.sqlite3',
        }.items():
            try:
                paths[name] = readable(state / filename)
            except (OSError, RuntimeError) as exc:
                missing.append(f'{filename}（{type(exc).__name__}）')
        if missing:
            record(20, 'state-databases', 'REQUIRED_DATABASE_UNAVAILABLE', False, '、'.join(missing))
        else:
            record(20, 'state-databases', 'REQUIRED_DATABASE_UNAVAILABLE', True, '四库齐全')

    for seq, name, attribute in ((30, 'financial-database', 'financial_database'),
                                 (40, 'market-database', 'market_database')):
        try:
            paths[name.split('-')[0]] = readable(getattr(args, attribute))
        except (OSError, RuntimeError) as exc:
            record(seq, name, 'REQUIRED_DATABASE_UNAVAILABLE', False, f'{type(exc).__name__}: {exc}')
        else:
            record(seq, name, 'REQUIRED_DATABASE_UNAVAILABLE', True, str(paths[name.split('-')[0]]))

    if not all(os.environ.get(name) for name in ('DEEPSEEK_API_KEY', 'GLM_API_KEY')):
        record(50, 'model-keys', 'OFFICIAL_MODEL_KEYS_REQUIRED_USE_CC_WITH_SECRETS', False,
               'DEEPSEEK_API_KEY 或 GLM_API_KEY 未设置')
    else:
        record(50, 'model-keys', 'OFFICIAL_MODEL_KEYS_REQUIRED_USE_CC_WITH_SECRETS', True,
               '两把模型密钥均在环境变量中')

    key = os.environ.get('NAVREN_PERSONAL_DATA_KEY')
    if not key:
        record(60, 'personal-key', 'EXISTING_PERSONAL_KEY_REQUIRED_USE_CC_WITH_SECRETS', False,
               'NAVREN_PERSONAL_DATA_KEY 未设置')
    else:
        record(60, 'personal-key', 'EXISTING_PERSONAL_KEY_REQUIRED_USE_CC_WITH_SECRETS', True, '密钥已设置')

    if not key or 'personal' not in paths:
        record(70, 'personal-key-check', 'PERSONAL_KEY_CHECK_UNAVAILABLE', None, '缺少密钥或私库，未检查')
    else:
        from cryptography.fernet import Fernet
        with connect(paths['personal']) as db:
            check = db.execute("SELECT value FROM private_crypto WHERE key='key_check'").fetchone()
        if not check or not check[0].startswith('pd1:'):
            record(70, 'personal-key-check', 'PERSONAL_KEY_CHECK_UNAVAILABLE', False, '私库缺少 pd1: 密钥校验值')
        elif Fernet(key.encode('ascii')).decrypt(check[0][4:].encode()).decode() != 'navren-personal-v1':
            record(70, 'personal-key-check', 'EXISTING_PERSONAL_KEY_MISMATCH', False, '密钥与私库校验值不匹配')
        else:
            record(70, 'personal-key-check', 'EXISTING_PERSONAL_KEY_MISMATCH', True, '密钥与私库一致')

    if 'bank' not in paths or 'financial' not in paths:
        record(80, 'sealed-templates', 'SEALED_FOUR_TEMPLATES_MISMATCH', None, '缺少启动库或财务库，未检查')
    else:
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
            record(80, 'sealed-templates', 'SEALED_FOUR_TEMPLATES_MISMATCH', False,
                   f'财务库角色={sorted(subjects)}，绑定={sorted(bindings)}')
        else:
            record(80, 'sealed-templates', 'SEALED_FOUR_TEMPLATES_MISMATCH', True, '四个封存模板一致')
        if metadata.get('schema_version') != 'navren.financial.v1':
            record(90, 'financial-schema', 'FINANCIAL_SCHEMA_UNSUPPORTED', False,
                   f"schema_version={metadata.get('schema_version')!r}")
        else:
            record(90, 'financial-schema', 'FINANCIAL_SCHEMA_UNSUPPORTED', True, 'navren.financial.v1')
        if metadata.get('history_profile') != 'realistic_three_year':
            record(100, 'history-profile', 'REGISTERED_THREE_YEAR_TEMPLATE_REQUIRED', False,
                   f"history_profile={metadata.get('history_profile')!r}")
        else:
            record(100, 'history-profile', 'REGISTERED_THREE_YEAR_TEMPLATE_REQUIRED', True, 'realistic_three_year')
        metadata['existing_registered_instance_count']=registered_count
        metadata['legacy_workspace_count']=len(bindings)

    sdk = ROOT / 'outputs/financial-mcp/venv/bin/python'
    if not sdk.is_file() or not os.access(sdk, os.X_OK):
        record(110, 'isolated-mcp-sdk', 'ISOLATED_MCP_SDK_UNAVAILABLE', False, f'缺少可执行文件 {sdk}')
    else:
        record(110, 'isolated-mcp-sdk', 'ISOLATED_MCP_SDK_UNAVAILABLE', True, str(sdk))

    if not 1024 <= args.port <= 65535 or not 1024 <= args.upstream_port <= 65535 or args.port == args.upstream_port:
        record(120, 'ports', 'INVALID_LOCAL_PORTS', False, f'port={args.port} upstream_port={args.upstream_port}')
    else:
        record(120, 'ports', 'INVALID_LOCAL_PORTS', True, f'port={args.port} upstream_port={args.upstream_port}')

    findings.sort(key=lambda item: item['seq'])
    return paths, metadata, findings

def prepare(args):
    paths, metadata, findings = probe_prerequisites(args)
    failed = next((item for item in findings if item['ok'] is False), None)
    if failed is None:
        return paths, metadata
    if failed['code'] == 'STATE_DIR_UNAVAILABLE':
        # 保持原有行为：状态目录不存在时由 resolve(strict=True) 抛 FileNotFoundError。
        raise FileNotFoundError(failed['detail'])
    raise RuntimeError(failed['code'])


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
        'existing_personal_key_verified': True, 'model_provider': 'unverified',
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
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ['NAVREN_MODEL_CALL_LOG'] = str(runtime_dir / 'model-calls.jsonl')
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    from local_knowledge.store import LocalKnowledgeProvider
    from local_knowledge.runtime import EvidenceRuntime
    from voice_gateway import VoiceGateway, load_key
    from local_speech_task import LocalTTSConfig
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
    voice = VoiceGateway(key=load_key() if LocalTTSConfig.from_environment() is None else '', allow_cloud_asr=False)
    stopped = threading.Event()
    instances, threads = [], []
    try:
        business = create_server(port=args.upstream_port, database=paths['bank'],
            voice=voice, evidence_runtime=evidence, market_database=paths['market'],
            agent_route_provider=args.agent_route_provider, **gateway_options)
        instances.append(business)
        # Metadata is bound to the same actual Agent used by this business server.
        binding = business.finance.capabilities()['conversation_runtime']
        base['model_route'] = ('official' if binding.get('binding_status') == 'official_transport_bound'
                               else 'unverified')
        base['model_provider'] = base['model_route']
        base['configured_models'] = {
            'primary': {'provider': binding.get('primary_provider'), 'model_id': binding.get('primary')},
            'reviewer': {'provider': binding.get('reviewer_provider'), 'model_id': binding.get('reviewer')}}
        base['runtime_model_binding'] = binding.get('binding_status', 'unbound')
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
        speech_status = voice.status()
        receipt = {**base, 'pid': os.getpid(), 'runtime_enabled': True,
            'server_deployed': False, 'app_release': business.app_release,
            'speech_configured': speech_status.get('configured',False),
            'speech_status': {key:speech_status.get(key) for key in (
                'asr_model','asr_available','tts_model','tts_available','cloud_provider',
                'asr_privacy_mode','asr_sample_rate','tts_sample_rate')},
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
