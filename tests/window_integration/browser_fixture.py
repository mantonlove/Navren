"""四合成账户与冻结公共数据的隔离实屏服务；模型、冷抓取和云语音关闭。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
FINANCIAL_SOURCE = ROOT / 'outputs/financial-data/navren-financial-v2.sqlite3'
FINANCIAL_SOURCE_SHA256 = 'cf0a8c109e8301f3d5382aea7065d25d9b02ebbfaca00543aad6d17d70969d65'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare_server(out):
    from agent_runtime.conversation import ConversationAgent
    from local_knowledge import LocalKnowledgeProvider
    from local_knowledge.runtime import PreparedEvidence, RoundProvider
    from server import create_server
    from voice_gateway import VoiceGateway

    package = ROOT / 'outputs/window-progress/rag'
    manifest = json.loads((package / 'integration-data-manifest.json').read_text())
    expected = {}
    for key, target in (('knowledge_database', out / 'public.sqlite3'),
                        ('market_database', out / 'bank.market.sqlite3')):
        row = manifest[key]
        source = package / Path(row['path']).name
        if source.is_symlink() or sha(source) != row['sha256']:
            raise ValueError('frozen_browser_public_data_changed:' + key)
        shutil.copy2(source, target)
        expected[key] = {'source_sha256': row['sha256'], 'copy_sha256': sha(target)}
    if manifest.get('public_only') is not True:
        raise ValueError('browser_public_only_attestation_missing')

    # 浏览器金融数据读取须绑定临时副本；绝不让HTTP进程读取项目原始库。
    financial_database = out / 'financial.sqlite3'
    if FINANCIAL_SOURCE.is_symlink() or sha(FINANCIAL_SOURCE) != FINANCIAL_SOURCE_SHA256:
        raise ValueError('frozen_browser_financial_data_changed')
    shutil.copy2(FINANCIAL_SOURCE, financial_database)
    try:
        import sqlite3
        with sqlite3.connect(f'file:{financial_database}?mode=ro', uri=True) as db:
            financial_subjects = [dict(zip(('subject_id', 'workspace_id'), row)) for row in db.execute(
                'SELECT subject_id,workspace_id FROM subjects ORDER BY subject_id')]
    except sqlite3.Error as error:
        raise ValueError('browser_financial_fixture_unreadable') from error
    if (sha(financial_database) != FINANCIAL_SOURCE_SHA256 or len(financial_subjects) != 4
            or {row['subject_id'] for row in financial_subjects} != {
                'grain_farmer', 'retired_entrepreneur', 'tycoon', 'working_landlady'}):
        raise ValueError('browser_financial_fixture_subjects_invalid')
    expected['financial_database'] = {'source_sha256': FINANCIAL_SOURCE_SHA256,
        'copy_sha256': sha(financial_database), 'dataset_version': 'navren-financial-c49c00383e9501ce',
        'subjects': financial_subjects}

    class NoCloud:
        model_id = 'integration-browser-cloud-disabled'
        def complete(self, *args, **kwargs):
            raise ValueError('browser_cloud_calls_disabled')

    class FrozenRuntime:
        def __init__(self, provider):
            self.provider = RoundProvider(provider, release_id=manifest['knowledge_database']['release_id'])
        def prepare_round(self, text):
            return PreparedEvidence(self.provider, {'mode': 'frozen_browser_no_external_fetch',
                'cold_fetch_enabled': False, 'semantic_validation': False})
        def close(self):
            pass

    with patch.dict(os.environ, {'NAVREN_KNOWLEDGE_DB': str(out / 'bootstrap-public.sqlite3'),
                                 'NAVREN_FINANCIAL_DB': str(financial_database)}):
        server = create_server(port=0, database=str(out / 'bank.sqlite3'),
            voice=VoiceGateway(key='', allow_cloud_asr=False),
            conversation_agent=ConversationAgent(primary=NoCloud()))
    try:
        # create_server的自动市场发布仅作用于新建bootstrap库；选定公共发布另行固定。
        server.evidence_runtime.close()
        provider = LocalKnowledgeProvider(out / 'public.sqlite3',
            release_id=manifest['knowledge_database']['release_id'])
        server.finance.knowledge_provider = provider
        server.evidence_runtime = FrozenRuntime(provider)
        server.finance.demo_accounts.source_dir = ROOT / 'outputs/contest-finances'
        initialized = server.finance.demo_accounts.initialize_offline(allow_offline=True)
        catalog = server.finance.demo_accounts.catalog()
        if initialized['initialized_count'] != 4 or catalog['initialized_count'] != 4:
            raise ValueError('browser_requires_all_four_frozen_profiles')
        expected_workspaces = {row['workspace_id'] for row in financial_subjects}
        actual_workspaces = {row['workspace_id'] for row in catalog['accounts']}
        if (actual_workspaces != expected_workspaces
                or server.finance.financial_data is None
                or sha(server.finance.financial_data.database_path) != FINANCIAL_SOURCE_SHA256):
            raise ValueError('browser_financial_database_not_bound_to_four_profiles')
        ready = {'url': f'http://127.0.0.1:{server.server_port}/chat.html',
            'release': server.app_release, 'cloud_models_enabled': False,
            'cloud_audio_enabled': False, 'cold_public_fetch_enabled': False,
            'database': str(out / 'bank.sqlite3'), 'private_key_persisted': False,
            'profile_catalog': catalog, 'public_release_id': provider.status()['public_release_id'],
            'public_inputs': expected, 'financial_data_configured': True,
            'financial_dataset_version': server.finance.financial_data.dataset_version,
            'scope': 'isolated_frozen_synthetic_browser_technical_checks',
            'semantic_verified': False}
        return server, ready
    except Exception:
        server.server_close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--hold-seconds', type=int, required=True)
    args = parser.parse_args()
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    original_dns, original_connect = socket.getaddrinfo, socket.socket.connect
    def local_dns(host, *a, **kw):
        if host not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('browser_fixture_external_network_disabled')
        return original_dns(host, *a, **kw)
    def local_connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('browser_fixture_external_network_disabled')
        return original_connect(sock, address)
    with patch('socket.getaddrinfo', side_effect=local_dns), \
            patch.object(socket.socket, 'connect', local_connect):
        server, ready = prepare_server(out)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (out / 'browser-ready.json').write_text(json.dumps(ready, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(ready, ensure_ascii=False), flush=True)
        completion = None
        try:
            deadline = time.monotonic() + args.hold_seconds
            while time.monotonic() < deadline:
                marker = out / 'browser-done.json'
                if marker.exists():
                    completion = json.loads(marker.read_text())
                    break
                time.sleep(.5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            finished = bool(completion and completion.get('technical_checks_finished') is True)
            (out / 'browser-session.json').write_text(json.dumps({'service_closed': True,
                'technical_checks_finished': finished, 'release': ready['release'],
                'scope': ready['scope'], 'semantic_verified': False,
                'cloud_models_enabled': False, 'private_key_persisted': False}, ensure_ascii=False, indent=2) + '\n')
        return 0 if finished else 2


if __name__ == '__main__':
    raise SystemExit(main())
