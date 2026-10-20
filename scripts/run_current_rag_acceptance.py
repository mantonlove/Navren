#!/usr/bin/env python3
"""冻结当前 factory + HTTP RAG 链；默认只预检，云调用须同时 --execute --cloud。

技术采集出口不裁决金融语义，不调用云 TTS。每题 fresh 子进程、独立临时库。
"""
from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack, closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.cookiejar import CookieJar
import importlib.util
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / 'outputs/window-progress/rag'
OUTPUT_ROOT = ROOT / 'outputs/rag-overall-acceptance-20261012'
MODEL_URL = 'https://aiping.cn/api/v1/chat/completions'
LIMITS = {'max_tokens': 100000, 'timeout_seconds': 105, 'max_rounds': 8,
          'max_tools': 12, 'primary_output_tokens': 4000, 'reviewer_output_tokens': 1400}
PRIVATE_KEYS = frozenset({'csrf', 'speech_receipt', 'session_token', 'access_token', 'refresh_token',
                         'token', 'cookie', 'authorization', 'api_key', 'workspace_id',
                         'account_id', 'personal_data_key', 'reasoning_content', 'thinking'})
BUSINESS = ('server', 'finance_gateway', 'voice_gateway', 'fund_history', 'market_content',
            'market_daily', 'financial_metrics', 'financial_graph', 'financial_methods',
            'financial_data_service', 'financial_mcp', 'user_memory', 'personal_data',
            'agent_runtime', 'base_runtime', 'simulated_bank', 'local_knowledge',
            'registered_instances', 'demo_sessions')
REGISTERED_ENTRY_HELPERS = ('registered_entry_context', 'registered_fixture_template',
                            'registered_profile_templates')
MODEL_FILES = {
    'multilingual-e5-small-onnx-q8': ('NAVREN_MULTILINGUAL_EMBEDDING_DIR',
        ('config.json', 'manifest.json', 'tokenizer.json', 'onnx/model_quantized.onnx')),
    'paraphrase-multilingual-minilm-l12-v2-onnx-q8': ('NAVREN_ALIGNED_EMBEDDING_DIR',
        ('config.json', 'manifest.json', 'tokenizer.json', 'onnx/model_qint8_arm64.onnx')),
    'opus-mt-zh-en-onnx-q8': ('NAVREN_QUERY_TRANSLATION_DIR',
        ('config.json', 'generation_config.json', 'manifest.json', 'source.spm',
         'special_tokens_map.json', 'tokenizer.json', 'tokenizer_config.json',
         'onnx/encoder_model_quantized.onnx', 'onnx/decoder_model_merged_quantized.onnx')),
    'mmarco-mminilmv2-reranker-q8': ('NAVREN_RERANK_DIR',
        ('config.json', 'manifest.json', 'tokenizer.json', 'tokenizer_config.json',
         'special_tokens_map.json', 'README.md', 'onnx/model_qint8_arm64.onnx'))}
PROFILE_NAMES = ('multilingual-e5-small-q8', 'paraphrase-multilingual-minilm-l12-v2-q8')
PREPARED_CHECKS = ('two_vector_profiles_ready', 'product_structure_ready_but_not_applicable',
    'original_22_chunks_and_all_original_tables_unchanged', 'all_12_original_retrieval_cases_pass',
    'source_and_assets_unchanged', 'frozen_original_public_database_unchanged',
    'prepared_db_hash_stable_across_12_reads', 'sqlite_integrity_ok',
    'all_background_jobs_completed', 'zero_background_jobs_scheduled',
    'zero_observed_socket_attempts', 'only_public_sqlite_opens', 'rss_peak_below_cap')
IDENTITY_TABLES = ('chunks', 'release_chunks', 'document_versions', 'raw_artifacts',
                   'documents', 'products', 'share_classes', 'graph_entities',
                   'graph_relations', 'graph_provenance', 'releases', 'current_release')
REQUEST_BUDGET_FILES = {
    'agent_runtime/request_budget.py':
        'c02034ace959cb14c600436d1cb20a5a52a50d01af33c7ad259e79ab6ad83b3d',
    'dependency-resources/deepseek-v4/tokenizer.json':
        '8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf',
    'agent_runtime/vendor/deepseek_v4_base.py':
        'bdbd57c132a1b3725042323d02b98b9d1df28e5f388f134399555d041f5055e0',
    'agent_runtime/vendor/deepseek_v4_0731.py':
        'abc0d26120250dda0ae077dc64aa28836026e61e970854aaeb792445e6a0dde6'}


REQUEST_BUDGET_SOURCE22_FILES = {
    'agent_runtime/request_budget.py':
        '75a5f7d3a130903ee7c679176013a6502faa1d8872330b8460051322e3c41ffb',
    'dependency-resources/deepseek-v4/tokenizer.json':
        '8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf',
    'agent_runtime/vendor/deepseek_v4_base.py':
        'bdbd57c132a1b3725042323d02b98b9d1df28e5f388f134399555d041f5055e0',
    'agent_runtime/vendor/deepseek_v4_0731.py':
        'abc0d26120250dda0ae077dc64aa28836026e61e970854aaeb792445e6a0dde6'}

# 正式Agent已采用的source46完整组合；这里只登记身份，不加载估计器、
# 放宽原100K/105秒门或授权云调用。MIT原件与代码/分词资源一起冻结。
REQUEST_BUDGET_SOURCE46_FILES = {
    **REQUEST_BUDGET_SOURCE22_FILES,
    'agent_runtime/request_budget.py':
        '1be42e62b1d2eaba876493aafe30ebfb46a69b1c244c901f92a6926142d7117a',
    'agent_runtime/vendor/DEEPSEEK-MIT-LICENSE.md':
        'f2c6c602815669d292889e5be8c802f2ed950653b77999b1584e8e6aed25d040',
    'dependency-resources/deepseek-v4/LICENSE':
        'f2c6c602815669d292889e5be8c802f2ed950653b77999b1584e8e6aed25d040'}


# 正式Agent于2026-10-19已采用的固定transport预算身份；分词器、官方
# formatter和MIT原件与source46一致。仅登记完整六文件身份，原预算/审核门不变。
REQUEST_BUDGET_SOURCE20261019_FILES = {
    **REQUEST_BUDGET_SOURCE46_FILES,
    'agent_runtime/request_budget.py':
        '05213471c33af3098e2b204c060b9d926098557a584499a91de478ca63892c7c'}


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def reviewer_response_diagnostic(raw):
    """只观测原响应身份及最终JSON根结构；不保存正文、字段值或隐藏推理。"""
    if type(raw) is not bytes:
        return {'parse_status': 'response_not_bytes'}
    result = {'response_bytes': len(raw), 'response_sha256': hashlib.sha256(raw).hexdigest()}
    try:
        envelope = json.loads(raw)
        content = envelope['choices'][0]['message']['content']
        if type(content) is not str:
            result['parse_status'] = 'content_not_text'
            return result
        value = json.loads(content)
    except (ValueError, TypeError, KeyError, IndexError, UnicodeError, RecursionError):
        result['parse_status'] = 'unparseable_envelope_or_content'
        return result
    kind = lambda item: ('null' if item is None else 'boolean' if type(item) is bool else
        'object' if type(item) is dict else 'array' if type(item) is list else
        'string' if type(item) is str else 'number' if type(item) in (int, float) else 'unknown')
    result.update(parse_status='parsed', root_type=kind(value))
    if type(value) is dict:
        required = {'verdict', 'reason', 'issues'}
        # 普通字段名可读；任意长文本或特殊字符键只保留身份摘要。
        label = lambda key: key if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', key) else (
            'key_sha256:' + hashlib.sha256(key.encode('utf-8', errors='surrogatepass')).hexdigest())
        result.update(root_keys=sorted(label(key) for key in value),
            missing_required=sorted(required - set(value)),
            unknown_fields=sorted(label(key) for key in set(value) - required),
            field_types={label(key): kind(value[key]) for key in sorted(value)})
    return result


class ReviewerDiagnosticResponse:
    """读后观测，原read参数、返回对象和异常均原样透传。"""
    def __init__(self, response, diagnostics):
        self.response, self.diagnostics = response, diagnostics
    def __enter__(self):
        self.response.__enter__()
        return self
    def __exit__(self, *args):
        return self.response.__exit__(*args)
    def __getattr__(self, name):
        return getattr(self.response, name)
    def read(self, *args, **kwargs):
        raw = self.response.read(*args, **kwargs)
        self.diagnostics.append(reviewer_response_diagnostic(raw))
        return raw


def trace_reviewer_call(reviewer, review_method, safe_data, packet, *args, **kwargs):
    """失败也留原安全packet及结构诊断；不修补判决、不重试、不记录异常正文。"""
    row = {'packet': deepcopy(safe_data(packet)), 'response_diagnostics': []}
    original_open = reviewer.client.urlopen
    def observed_open(*open_args, **open_kwargs):
        return ReviewerDiagnosticResponse(original_open(*open_args, **open_kwargs),
                                          row['response_diagnostics'])
    try:
        with patch.object(reviewer.client, 'urlopen', observed_open):
            result = review_method(reviewer, packet, *args, **kwargs)
    except Exception as error:
        code = getattr(error, 'code', None)
        known = {'INVALID_TOOL_ARGUMENTS', 'request_budget_exceeded',
            'request_budget_protocol_mismatch', 'review_request_not_sent', 'missing_api_key',
            'provider_http_error', 'provider_timeout', 'provider_unavailable', 'provider_error',
            'invalid_model_contract', 'response_truncated'}
        row['error_code'] = code if type(code) is str and code in known else 'unexpected_review_error'
        raise
    else:
        decision = {k: v for k, v in result.items() if k != '_agent_review_wire'}
        wire = result.get('_agent_review_wire')
        if type(wire) is bytes:
            decision['review_wire_identity'] = {'bytes': len(wire),
                'sha256': hashlib.sha256(wire).hexdigest()}
        row['decision'] = deepcopy(safe_data(decision))
        return result
    finally:
        reviewer.records.append(row)


def public_app_release_assets(root):
    """静态登记app_release的页面及顶层JS/CSS；不导入server或扩大读目录。"""
    root = Path(root).absolute()
    if root.resolve() != root or root.is_symlink():
        raise ValueError('public_app_asset_redirected')
    source = root / 'server.py'
    if not source.is_file() or source.is_symlink() or source.resolve() != source.absolute():
        raise ValueError('public_app_asset_missing_or_redirected')
    tree = ast.parse(source.read_text())
    entries = [node.value for node in tree.body if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == 'APP_PAGES' for target in node.targets)]
    if len(entries) != 1:
        raise ValueError('public_app_pages_contract_missing')
    pages = ast.literal_eval(entries[0])
    if (not isinstance(pages, (set, list, tuple)) or not pages
            or any(not isinstance(name, str) or Path(name).name != name
                   or not re.fullmatch(r'[A-Za-z0-9_-]+\.html', name) for name in pages)):
        raise ValueError('public_app_pages_contract_invalid')
    paths = [root / name for name in sorted(pages)]
    for directory, suffix in (('css', '*.css'), ('js', '*.js')):
        folder = root / directory
        if folder.is_symlink() or folder.resolve() != folder.absolute():
            raise ValueError('public_app_asset_redirected')
        paths.extend(sorted(folder.glob(suffix)))
    result = {}
    for path in paths:
        if not path.is_file() or path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError('public_app_asset_missing_or_redirected')
        result[str(path.relative_to(root))] = sha(path)
    return result


def request_budget_runtime():
    """只查分词依赖身份，不导入 tokenizer、候选或模型。"""
    try:
        distribution = importlib.metadata.distribution('tokenizers')
    except importlib.metadata.PackageNotFoundError:
        raise ValueError('request_budget_runtime_missing') from None
    if distribution.version != '0.23.3':
        raise ValueError('request_budget_runtime_version_changed')
    files = [Path(distribution.locate_file(item)).resolve() for item in distribution.files or ()
             if Path(str(item)).name.startswith('tokenizers') and Path(str(item)).suffix in ('.so', '.pyd')]
    if len(files) != 1 or not files[0].is_file():
        raise ValueError('request_budget_native_identity_missing')
    return {'python': str(Path(sys.executable).resolve()), 'python_version': platform.python_version(),
            'tokenizers_version': distribution.version,
            'extension': {'path': str(files[0]), 'sha256': sha(files[0])},
            'tokenizer_loaded': False, 'model_sessions_created': 0}


def fixed_budget_file(root, relative, expected):
    path = Path(root) / relative
    if (not path.is_file() or path.is_symlink() or path.resolve() != path.absolute()
            or sha(path) != expected):
        raise ValueError('request_budget_resource_missing_redirected_or_changed')
    return path


def registered_request_budget_files(root):
    """从实际文件选完整可信登记组合；不信候选或manifest自报版本。"""
    module = Path(root) / 'agent_runtime/request_budget.py'
    if (not module.is_file() or module.is_symlink()
            or module.resolve() != module.absolute()):
        raise ValueError('request_budget_resource_missing_redirected_or_changed')
    module_sha256 = sha(module)
    for files in (REQUEST_BUDGET_FILES, REQUEST_BUDGET_SOURCE22_FILES,
                  REQUEST_BUDGET_SOURCE46_FILES, REQUEST_BUDGET_SOURCE20261019_FILES):
        if module_sha256 != files['agent_runtime/request_budget.py']:
            continue
        for relative, expected in files.items():
            fixed_budget_file(root, relative, expected)
        return dict(files)
    raise ValueError('request_budget_resource_missing_redirected_or_changed')


def freeze_request_budget_resources(target, manifest, source_root=None):
    """仅完整已登记组合；资源变更不能隐式换估计器。"""
    source_root = ROOT if source_root is None else Path(source_root)
    module = source_root / 'agent_runtime/request_budget.py'
    if not module.exists() and not module.is_symlink():
        return
    files = registered_request_budget_files(source_root)
    for relative, expected in files.items():
        source = fixed_budget_file(source_root, relative, expected)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        fixed_budget_file(target, relative, expected)
        manifest[relative] = expected
    if registered_request_budget_files(source_root) != files:
        raise ValueError('request_budget_resource_missing_redirected_or_changed')


def request_budget_contract(candidate, manifest):
    module = Path(candidate) / 'agent_runtime/request_budget.py'
    present = module.exists() or module.is_symlink()
    registered = 'agent_runtime/request_budget.py' in manifest
    if not present and not registered:
        return None
    if present != registered:
        raise ValueError('request_budget_resource_manifest_mismatch')
    files = registered_request_budget_files(candidate)
    if any(manifest.get(relative) != expected for relative, expected in files.items()):
        raise ValueError('request_budget_resource_manifest_mismatch')
    return {'files': files, 'native_runtime': request_budget_runtime()}


def request_budget_unchanged(config):
    module = Path(config['candidate']) / 'agent_runtime/request_budget.py'
    present = module.exists() or module.is_symlink()
    registered = 'agent_runtime/request_budget.py' in config['source_sha256']
    contract = config.get('request_budget_resources')
    if not present and not registered:
        return contract is None
    if not present or not registered:
        return False
    try:
        files = registered_request_budget_files(config['candidate'])
        if not isinstance(contract, dict) or contract.get('files') != files:
            return False
        if any(config['source_sha256'].get(relative) != expected for relative, expected in files.items()):
            return False
        return contract.get('native_runtime') == request_budget_runtime()
    except (ValueError, OSError, TypeError, KeyError):
        return False


def native_runtime(source_root=None):
    """轻量依赖检查，不创建ONNX session，不导入业务或下载。"""
    import numpy
    import onnxruntime
    import tokenizers
    del numpy, tokenizers
    packages = {name: importlib.metadata.version(name) for name in ('numpy', 'onnxruntime', 'tokenizers')}
    source_root = ROOT if source_root is None else Path(source_root)
    expected = dict(line.strip().split('==', 1) for line in
                    (source_root / 'local_knowledge/requirements-vector.txt').read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith('#'))
    if packages != expected or 'CPUExecutionProvider' not in onnxruntime.get_available_providers():
        raise ValueError('native_runtime_contract_changed')
    extensions = {}
    for name, needle in (('numpy', '_multiarray_umath'), ('onnxruntime', 'onnxruntime_pybind11_state'),
                         ('tokenizers', 'tokenizers')):
        distribution = importlib.metadata.distribution(name)
        files = [Path(distribution.locate_file(item)).resolve() for item in distribution.files or ()
                 if Path(str(item)).name.startswith(needle) and Path(str(item)).suffix in ('.so', '.pyd')]
        if len(files) != 1:
            raise ValueError('native_extension_identity_missing')
        extensions[name] = {'path': str(files[0]), 'sha256': sha(files[0])}
    return {'python': str(Path(sys.executable).absolute()), 'python_version': platform.python_version(),
            'architecture': platform.machine(), 'platform': sys.platform,
            'packages': packages, 'extensions': extensions,
            'cpu_provider_available': True, 'model_sessions_created': 0}


def public_table_identity(db, names):
    result = {}
    for name in names:
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name):
            raise ValueError('prepared_identity_invalid')
        quoted = '"' + name + '"'
        columns = list(db.execute('PRAGMA table_info(' + quoted + ')'))
        if not columns:
            raise ValueError('prepared_identity_invalid')
        digest, count = hashlib.sha256(), 0
        for row in db.execute('SELECT * FROM ' + quoted + ' ORDER BY ' +
                              ','.join(str(i + 1) for i in range(len(columns)))):
            values = [{'bytes': len(v), 'sha256': hashlib.sha256(v).hexdigest()}
                      if isinstance(v, bytes) else v for v in row]
            digest.update(json.dumps(values, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode())
            digest.update(b'\n'); count += 1
        result[name] = {'rows': count, 'content_sha256': digest.hexdigest()}
    return result


def validate_prepared_database(path, contract, *, all_original=False):
    """只读验证已准备公共DB；worker只访问其隔离副本。"""
    import sqlite3
    path = Path(path)
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('prepared_database_not_quiescent')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON'); db.row_factory = sqlite3.Row
        names = contract['original_identity'] if all_original else IDENTITY_TABLES
        expected = {name: contract['original_identity'][name] for name in names}
        if public_table_identity(db, names) != expected:
            raise ValueError('prepared_original_identity_changed')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('prepared_database_integrity_failed')
        current = db.execute("SELECT release_id FROM current_release WHERE scope='public'").fetchone()
        release = db.execute('SELECT sealed FROM releases WHERE id=?', (contract['release_id'],)).fetchone()
        if not current or current[0] != contract['release_id'] or not release or release[0] != 1:
            raise ValueError('prepared_release_changed')
        manifests = [dict(row) for row in db.execute('SELECT * FROM vector_releases WHERE release_id=? ORDER BY profile_id',
                                                   (contract['release_id'],))]
        if manifests != contract['vector_manifests']:
            raise ValueError('prepared_vector_manifest_changed')
        for item in manifests:
            count = db.execute('SELECT count(*) FROM chunk_vectors WHERE release_id=? AND profile_id=?',
                               (contract['release_id'], item['profile_id'])).fetchone()[0]
            if count != item['vector_count']:
                raise ValueError('prepared_vector_manifest_changed')
    return {'original_identity_verified': True, 'release_id': contract['release_id'],
            'profile_ids': [row['profile_id'] for row in manifests], 'chunk_count': 22}


def prepared_environment(receipt):
    """只采纳模型目录与固定能力选择；不采纳DB/密钥/网络地址。"""
    supplied = receipt.get('fixed_environment', {})
    if (supplied.get('NAVREN_VECTOR_ENCODER') != 'dual'
            or supplied.get('NAVREN_QUERY_TRANSLATION_ENABLED') != '0'):
        raise ValueError('prepared_environment_invalid')
    environment = {'NAVREN_VECTOR_ENCODER': 'dual', 'NAVREN_QUERY_TRANSLATION_ENABLED': '0',
                   'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}
    for variable, _ in MODEL_FILES.values():
        value = supplied.get(variable)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('prepared_environment_invalid')
        environment[variable] = str(Path(value).resolve())
    return environment


def shared_assets_unchanged(assets):
    return all(Path(item['path']).is_file() and not Path(item['path']).is_symlink()
               and sha(item['path']) == item['sha256'] for item in assets.values())


def prepared_contract(args, database, expected_sha, release, reference, source_root=None):
    path = getattr(args, 'prepared_public_manifest', None)
    supplied_sha = getattr(args, 'prepared_public_manifest_sha256', None)
    enabled = any(getattr(args, name) for name in
                  ('vector_enabled', 'structure_enabled', 'rerank_enabled', 'answer_form_enabled'))
    if not path and not supplied_sha and not enabled:
        return None
    if not path or not supplied_sha:
        raise ValueError('prepared_public_manifest_required')
    path = Path(path).resolve()
    if not path.is_relative_to(OUTPUT_ROOT.resolve()) or sha(path) != supplied_sha:
        raise ValueError('prepared_public_manifest_changed')
    receipt = json.loads(path.read_text())
    checks = receipt.get('checks', {})
    if (receipt.get('schema') != 'navren_persistent_public_vector_preparation/v1'
            or receipt.get('passed') is not True or not isinstance(checks, dict)
            or any(checks.get(name) is not True for name in PREPARED_CHECKS)
            or any(value is not True for value in checks.values())):
        raise ValueError('prepared_public_checks_failed')
    source = Path(receipt['source_database_path']).resolve()
    if (not database.is_relative_to(OUTPUT_ROOT.resolve())
            or not (source.is_relative_to(OUTPUT_ROOT.resolve()) or source.is_relative_to(PUBLIC.resolve()))
            or Path(receipt['actual_database_path']).resolve() != database
            or receipt.get('database_sha256') != expected_sha
            or receipt.get('database_sha256_before_queries') != expected_sha
            or receipt.get('release_id') != release
            or receipt.get('source_database_sha256') != reference['knowledge_database']['sha256']
            or release != reference['knowledge_database']['release_id']
            or sha(source) != receipt['source_database_sha256']):
        raise ValueError('prepared_database_binding_invalid')
    identity = receipt.get('original_table_identity_before')
    if (not isinstance(identity, dict) or not set(IDENTITY_TABLES) <= set(identity)
            or identity != receipt.get('original_table_identity_after')
            or any(identity[name].get('rows') != count for name, count in
                   (('chunks', 22), ('release_chunks', 22), ('document_versions', 6), ('raw_artifacts', 6)))):
        raise ValueError('prepared_identity_invalid')
    import sqlite3
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if public_table_identity(db, identity) != identity:
            raise ValueError('prepared_source_identity_changed')
    vector = receipt.get('vector_preparation', {})
    profiles, manifests = vector.get('profiles', []), vector.get('manifest_rows', [])
    if (vector.get('ready') is not True or len(profiles) != 2 or len(manifests) != 2
            or {p.get('name') for p in profiles} != set(PROFILE_NAMES)
            or any(p.get('dimension') != 384 or p.get('providers') != ['CPUExecutionProvider']
                   or not re.fullmatch(re.escape(p['name']) + r':[0-9a-f]{64}', p.get('profile_id', '')) for p in profiles)
            or {p['profile_id'] for p in profiles} != {m.get('profile_id') for m in manifests}
            or any(m.get('release_id') != release or m.get('dimension') != 384
                   or m.get('chunk_count') != 22 or type(m.get('vector_count')) is not int
                   or not 22 <= m['vector_count'] <= 22 * 16 for m in manifests)
            or receipt.get('derived_manifests', {}).get('vector_releases') != manifests):
        raise ValueError('prepared_profiles_invalid')
    structure = receipt.get('structure_preparation', {})
    if (structure.get('ready') is not True or structure.get('applicable') is not False
            or structure.get('parent_count') != 0 or structure.get('child_count') != 0
            or structure.get('manifest_rows') != []):
        raise ValueError('prepared_structure_contract_invalid')
    environment = prepared_environment(receipt)
    before, after = receipt.get('model_assets_before', {}), receipt.get('model_assets_after', {})
    asset_keys = {folder + '/' + filename for folder, (_, members) in MODEL_FILES.items() for filename in members}
    if not isinstance(before, dict) or set(before) != asset_keys or before != after:
        raise ValueError('prepared_assets_invalid')
    assets = {}
    for folder, (variable, members) in MODEL_FILES.items():
        for filename in members:
            key = folder + '/' + filename; item = before[key]
            digest = item.get('actual_sha256')
            if (item.get('matches_v11') is not True or digest != item.get('v11_sha256')
                    or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
                raise ValueError('prepared_assets_invalid')
            assets[key] = {'path': str(Path(environment[variable]) / filename), 'sha256': digest}
    if not shared_assets_unchanged(assets):
        raise ValueError('prepared_model_asset_changed')
    local_root = ROOT if source_root is None else Path(source_root)
    if source_root is not None:
        requirements = local_root / 'local_knowledge/requirements-vector.txt'
        if not requirements.is_file() or requirements.is_symlink() or requirements.resolve() != requirements.absolute():
            raise ValueError('consumer_vector_requirements_missing_or_redirected')
    runtime = native_runtime() if source_root is None else native_runtime(local_root)
    if (runtime['packages'] != receipt.get('runtime', {}).get('packages')
            or runtime['python'] != receipt.get('runtime', {}).get('python')):
        raise ValueError('prepared_native_runtime_changed')
    local_sources = {name: digest for name, digest in receipt.get('source_sha256_before', {}).items()
                     if name.startswith('local_knowledge/')}
    if (not local_sources or any(receipt.get('source_sha256_after', {}).get(name) != digest
            or not (local_root / name).is_file() or (local_root / name).is_symlink()
            or (local_root / name).resolve() != (local_root / name).absolute()
            or sha(local_root / name) != digest for name, digest in local_sources.items())):
        raise ValueError('prepared_retrieval_source_changed')
    contract = {'receipt_path': str(path), 'receipt_sha256': supplied_sha,
                'environment': environment, 'model_assets': assets, 'native_runtime': runtime,
                'source_database_path': str(source), 'source_database_sha256': receipt['source_database_sha256'],
                'release_id': release, 'original_identity': identity,
                'profiles': profiles, 'vector_manifests': sorted(manifests, key=lambda m: m['profile_id']),
                'structure_preparation': structure, 'source_sha256': local_sources}
    build = receipt.get('build_result', {})
    if (build.get('ready') is not True or build.get('release_id') != release
            or len(build.get('profiles', [])) != 2
            or {p.get('profile_id') for p in build['profiles']} != {p['profile_id'] for p in profiles}
            or any(p.get('ready') is not True or p.get('chunk_count') != 22 for p in build['profiles'])
            or receipt.get('runtime', {}).get('loaded_embedding_profiles') != profiles
            or receipt.get('runtime', {}).get('CPUExecutionProvider_only') is not True):
        raise ValueError('prepared_profiles_invalid')
    assert_prepared_provider(receipt.get('provider_status', {}), contract)
    validate_prepared_database(database, contract, all_original=True)
    return contract


def assert_prepared_provider(status, contract):
    vector = status.get('vector_index', {})
    profiles = vector.get('profiles', [])
    expected = {row['profile_id']: row for row in contract['vector_manifests']}
    if (status.get('public_release_id') != contract['release_id'] or status.get('published_chunk_count') != 22
            or vector.get('ready') is not True or len(profiles) != 2
            or {p.get('profile_id') for p in profiles} != set(expected)
            or any(p.get('ready') is not True or p.get('dimension') != 384
                   or p.get('vector_count') != expected[p['profile_id']]['vector_count'] for p in profiles)):
        raise ValueError('prepared_runtime_profiles_not_ready')


def suppress_vector_background(vectors, stack, record):
    """验收独有：整个case消费已准备索引，不更改真实查询实现。"""
    observation = {'policy': 'prepared_index_no_background_build/1', 'calls': 0, 'scheduled_jobs': 0}
    record['vector_background_suppression'] = observation
    def suppress(*args, **kwargs):
        observation['calls'] = min(5000, observation['calls'] + 1)
    stack.enter_context(patch.object(vectors, 'schedule', suppress))


def retrieval_diagnostic(raw):
    """有限固定字段，无原问/译文/原文/任意rank日志或raw logit。"""
    raw = raw if isinstance(raw, dict) else {}
    def count(value):return value if type(value) is int and 0 <= value <= 5000 else None
    def reason(value):
        if value is None:return None
        allowed = {'vector_disabled', 'vector_model_missing', 'vector_model_invalid', 'vector_dependency_missing',
            'vector_inference_failed', 'vector_index_missing', 'vector_index_invalid', 'vector_empty_eligible',
            'vector_partial', 'structure_disabled', 'structure_not_applicable', 'structure_index_missing',
            'rerank_disabled', 'rerank_timeout',
            'rerank_empty_candidates', 'answer_form_disabled', 'request_form_unknown', 'no_literal_match',
            'translation_disabled', 'query_translation_disabled', 'not_applicable'}
        return value if isinstance(value, str) and value in allowed else 'unrecognized_diagnostic_reason'
    def component(value):
        value = value if isinstance(value, dict) else {}
        return {'used': value.get('used') is True, 'ready': value.get('ready') is True,
                'reason': reason(value.get('reason')),
                **{key: count(value.get(key)) for key in ('candidate_count', 'pool_count', 'scored_count')}}
    branches = []
    for item in raw.get('vector_branches', [])[:8] if isinstance(raw.get('vector_branches'), list) else []:
        if not isinstance(item, dict):continue
        profile = item.get('profile_id')
        if not isinstance(profile, str) or not any(re.fullmatch(re.escape(name) + r':[0-9a-f]{64}', profile) for name in PROFILE_NAMES):
            profile = None
        branches.append({'profile_id': profile, 'mode': item.get('mode') if item.get('mode') in ('vector', 'fts5') else None,
                         'reason': reason(item.get('reason')), 'candidate_count': count(item.get('candidate_count'))})
    rerank = raw.get('reranking') if isinstance(raw.get('reranking'), dict) else {}
    return {'mode': raw.get('mode') if raw.get('mode') in ('hybrid', 'fts5') else None,
            'vector_used': raw.get('vector_used') is True, 'vector_reason': reason(raw.get('vector_reason')),
            'vector_candidate_count': count(raw.get('vector_candidate_count')), 'vector_branches': branches,
            'structure': component(raw.get('structure_index')), 'rerank': component(rerank),
            'answer_form': component(rerank.get('answer_form_preference')),
            'translation': component(raw.get('query_translation'))}


def vector_runtime_observed(diagnostics, contract):
    """两个索引须绑定；至少一次向量查询成功，其已尝试分支均成功。

    双空间的实际ready由assert_prepared_provider在请求前独立核验。
    本门只证明查询确实使用向量，不声称每题都使用aligned分支。
    """
    allowed = {profile['profile_id'] for profile in (contract or {}).get('profiles', [])}
    if len(allowed) != 2:
        return False
    return any(item.get('vector_used') is True and item.get('mode') == 'hybrid'
               and item.get('vector_reason') is None
               and type(item.get('vector_candidate_count')) is int and item['vector_candidate_count'] > 0
               and bool(item.get('vector_branches'))
               and all(branch.get('profile_id') in allowed and branch.get('mode') == 'vector'
                       and branch.get('reason') is None
                       for branch in item.get('vector_branches', [])) for item in diagnostics)


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    os.replace(temporary, path)


def safe_record(value, private_values=()):
    """凭据/私人身份字段不入文件；不读取环境中的密钥来做日志过滤。"""
    if isinstance(value, dict):
        return {k: safe_record(v, private_values) for k, v in value.items()
                if k.lower() not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [safe_record(v, private_values) for v in value]
    if isinstance(value, tuple):
        return [safe_record(v, private_values) for v in value]
    if isinstance(value, str):
        for private in private_values:
            if private:
                value = value.replace(private, '[local-session]')
    return value


def source_manifest_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('consumer_source_manifest_duplicate_key')
        result[name] = value
    return result


def consumer_source_map(path):
    value = json.loads(Path(path).read_text(), object_pairs_hook=source_manifest_object)
    sources = value.get('source_sha256') if isinstance(value, dict) else None
    if not isinstance(sources, dict) or not sources:
        raise ValueError('consumer_source_manifest_invalid')
    for relative, digest in sources.items():
        if (not isinstance(relative, str) or not relative or '\\' in relative
                or Path(relative).is_absolute() or Path(relative).as_posix() != relative
                or any(part in ('', '.', '..') for part in relative.split('/'))
                or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise ValueError('consumer_source_manifest_invalid')
    return sources


def canonical_source_path(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('consumer_source_path_redirected_or_not_absolute')
    return path


def registered_source_file(root, relative, expected):
    path = Path(root) / relative
    if (not path.is_file() or path.is_symlink() or path.resolve() != path.absolute()
            or sha(path) != expected):
        raise ValueError('consumer_source_file_missing_redirected_or_changed')
    return path


def source_tree_unchanged(root, manifest):
    """独立候选只允许清单内文件，新增文件和路径重定向同样拒绝。"""
    try:
        root = canonical_source_path(root)
        names = set()
        for path in root.rglob('*'):
            if path.is_symlink() or path.resolve() != path.absolute():
                return False
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                if relative not in manifest:
                    return False
                registered_source_file(root, relative, manifest[relative])
                names.add(relative)
        return root.is_dir() and names == set(manifest)
    except (ValueError, OSError, TypeError, KeyError):
        return False


def consumer_source_contract(args):
    """外部 owner 源码须有显式清单；只导入正式验收 helper/worker。"""
    values = [getattr(args, name, None) for name in
              ('consumer_source_root', 'consumer_source_manifest', 'consumer_source_manifest_sha256')]
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError('consumer_source_binding_required')
    root, manifest = (canonical_source_path(value) for value in values[:2])
    project = ROOT.resolve()
    if (not root.is_dir() or not root.is_relative_to(project / 'outputs')
            or not manifest.is_relative_to(project / 'outputs') or not manifest.is_file()
            or not isinstance(values[2], str) or not re.fullmatch('[0-9a-f]{64}', values[2])
            or sha(manifest) != values[2]):
        raise ValueError('consumer_source_binding_invalid')
    sources = consumer_source_map(manifest)
    if not source_tree_unchanged(root, sources):
        raise ValueError('consumer_source_tree_not_registered_or_changed')
    if {'scripts.py', 'scripts/__init__.py'} & set(sources):
        raise ValueError('consumer_control_package_conflict')
    helper = canonical_source_path(ROOT / 'scripts/run_rag_acceptance.py')
    worker = canonical_source_path(Path(__file__).resolve())
    trace = canonical_source_path(Path(__file__).resolve().parent / 'run_skill_acceptance.py')
    supports = {}
    for name, path in (('scripts/run_rag_acceptance.py', helper), ('scripts/run_skill_acceptance.py', trace)):
        supports[name] = {'path': str(path), 'sha256': sha(path), 'original_sha256': sources.get(name)}
    return {'source_root': str(root), 'manifest_path': str(manifest), 'manifest_sha256': values[2],
            'source_sha256': sources, 'project_root': str(project),
            'trusted_helper': {'path': str(helper), 'sha256': sha(helper)},
            'support_overlays': supports,
            'worker_overlay': {'relative_path': 'scripts/run_current_rag_acceptance.py',
                'path': str(worker), 'sha256': sha(worker),
                'original_sha256': sources.get('scripts/run_current_rag_acceptance.py')}}


def consumer_source_unchanged(config):
    inputs = Path(config['out_dir']) / 'inputs'
    name = 'consumer-source-manifest.json'
    contract = config.get('consumer_source_contract')
    signals = (name in config['input_sha256'], name in config['input_bindings'],
               (inputs / name).exists() or (inputs / name).is_symlink())
    if contract is None:
        return not any(signals)
    if not isinstance(contract, dict) or not all(signals):
        return False
    try:
        root = canonical_source_path(contract['source_root'])
        manifest = canonical_source_path(contract['manifest_path'])
        project = canonical_source_path(contract['project_root'])
        if (not root.is_dir() or not root.is_relative_to(project / 'outputs')
                or not manifest.is_relative_to(project / 'outputs')
                or config['input_sha256'][name] != contract['manifest_sha256']
                or config['input_bindings'][name] != {'source_path': str(manifest), 'sha256': contract['manifest_sha256']}
                or sha(manifest) != contract['manifest_sha256']
                or consumer_source_map(manifest) != contract['source_sha256']
                or consumer_source_map(inputs / name) != contract['source_sha256']):
            return False
        registered_source_file(inputs, name, contract['manifest_sha256'])
        if not source_tree_unchanged(root, contract['source_sha256']):
            return False
        overlay = contract['worker_overlay']
        if (overlay['relative_path'] != 'scripts/run_current_rag_acceptance.py'
                or overlay['original_sha256'] != contract['source_sha256'].get(overlay['relative_path'])
                or overlay['sha256'] != config['runner_sha256']
                or config['source_sha256'].get(overlay['relative_path']) != overlay['sha256']):
            return False
        supports = contract['support_overlays']
        if set(supports) != {'scripts/run_rag_acceptance.py', 'scripts/run_skill_acceptance.py'}:
            return False
        if {key: supports['scripts/run_rag_acceptance.py'][key] for key in ('path', 'sha256')} != contract['trusted_helper']:
            return False
        for relative, item in supports.items():
            if (item['original_sha256'] != contract['source_sha256'].get(relative)
                    or config['source_sha256'].get(relative) != item['sha256']):
                return False
        for item in (contract['trusted_helper'], overlay, *supports.values()):
            path = canonical_source_path(item['path'])
            registered_source_file(path.parent, path.name, item['sha256'])
        for relative, digest in config['source_sha256'].items():
            expected = (overlay['sha256'] if relative == overlay['relative_path'] else
                        supports[relative]['sha256'] if relative in supports else contract['source_sha256'].get(relative))
            if digest != expected:
                return False
        return True
    except (ValueError, OSError, TypeError, KeyError):
        return False


def worker_identity_unchanged(config):
    try:
        name = 'scripts/run_current_rag_acceptance.py'
        if config['source_sha256'].get(name) != config['runner_sha256']:
            return False
        registered_source_file(config['candidate'], name, config['runner_sha256'])
        return True
    except (ValueError, OSError, TypeError, KeyError):
        return False


def execution_manifest_unchanged(config):
    try:
        path = registered_source_file(config['out_dir'], 'source-manifest.json', config['execution_manifest_sha256'])
        return json.loads(path.read_text(), object_pairs_hook=source_manifest_object) == config['source_sha256']
    except (ValueError, OSError, TypeError, KeyError):
        return False


def freeze(target, consumer=None):
    # 旧冻结器只加载标准库；不用其业务 run()，也不污染 scripts 包的 import 路径。
    helper = ROOT / 'scripts/run_rag_acceptance.py'
    if consumer:
        item = consumer['trusted_helper']
        if str(helper) != item['path']:
            raise ValueError('consumer_trusted_helper_identity_changed')
        registered_source_file(helper.parent, helper.name, item['sha256'])
    spec = importlib.util.spec_from_file_location('_navren_rag_freeze_only', helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source_root = Path(consumer['source_root']) if consumer else ROOT
    module.ROOT = source_root
    manifest = module.freeze(target)
    # These current packages are imported by BankTools even with no financial DB.
    for directory in ('financial_data_service', 'financial_mcp'):
        for source in sorted((source_root / directory).rglob('*')):
            if (not source.is_file() or source.suffix not in ('.py', '.sql', '.json')
                    or '__pycache__' in source.parts):
                continue
            relative = source.relative_to(source_root)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            digest = sha(source)
            if sha(destination) != digest:
                raise ValueError('source_copy_changed')
            manifest[str(relative)] = digest
    # 当前 SQLite schema 是执行输入，旧冻结器未包含 .sql。
    for directory in ('local_knowledge', 'base_runtime', 'simulated_bank', 'financial_methods'):
        for source in sorted((source_root / directory).rglob('*.sql')):
            relative = source.relative_to(source_root)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            digest = sha(source)
            if sha(destination) != digest:
                raise ValueError('source_copy_changed')
            manifest[str(relative)] = digest
    freeze_request_budget_resources(target, manifest, source_root)
    # 复用已采用的注册验收入口；资料和题目仍为原RAG夹具。
    for name in REGISTERED_ENTRY_HELPERS:
        relative = 'tests/window_integration/' + name + '.py'
        source = source_root / relative
        if not source.exists():
            continue  # 标准库单测的最小source树不含业务入口，worker另行强制验证。
        if source.is_symlink() or source.resolve() != source.absolute():
            raise ValueError('registered_entry_helper_redirected')
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        manifest[relative] = sha(source)
        if sha(destination) != manifest[relative]:
            raise ValueError('registered_entry_helper_copy_changed')
    if any(sha(source_root / relative) != digest for relative, digest in manifest.items()):
        raise ValueError('source_changed_during_freeze')
    if consumer:
        registered_source_file(helper.parent, helper.name, consumer['trusted_helper']['sha256'])
        for name, digest in manifest.items():
            if consumer['source_sha256'].get(name) != digest:
                raise ValueError('consumer_runtime_file_not_registered')
            registered_source_file(source_root, name, digest)
        overlay = consumer['worker_overlay']
        source = registered_source_file(Path(overlay['path']).parent, Path(overlay['path']).name, overlay['sha256'])
        destination = target / overlay['relative_path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        registered_source_file(target, overlay['relative_path'], overlay['sha256'])
        manifest[overlay['relative_path']] = overlay['sha256']
        for name, item in consumer['support_overlays'].items():
            source = registered_source_file(Path(item['path']).parent, Path(item['path']).name, item['sha256'])
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            registered_source_file(target, name, item['sha256'])
            manifest[name] = item['sha256']
    return manifest


def selected_cases(question_set, selection):
    rows = question_set.get('cases')
    if not isinstance(rows, list) or not rows:
        raise ValueError('empty_question_set')
    ids = []
    for row in rows:
        ident = row.get('id') if isinstance(row, dict) else None
        if (not isinstance(ident, str) or not ident or len(ident) > 100
                or Path(ident).name != ident or ident in ('.', '..') or ident in ids):
            raise ValueError('invalid_or_duplicate_case_id')
        ids.append(ident)
        request = row.get('request') if isinstance(row.get('request'), dict) else {}
        question = row.get('question', request.get('query'))
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 6000:
            raise ValueError('invalid_case_question')
    wanted = selection.split(',') if selection else ids
    if not wanted or len(set(wanted)) != len(wanted) or set(wanted) - set(ids):
        raise ValueError('unknown_or_duplicate_selected_case')
    return [deepcopy(rows[ids.index(ident)]) for ident in wanted]


def approved_reply(http_status, result):
    """宿主正常响应省略 isFallback；审批、合规及回答合同仍须明确存在。"""
    return (http_status == 200 and isinstance(result, dict)
            and result.get('isFallback', False) is False
            and result.get('compliance_pass') is True and not result.get('error')
            and isinstance(result.get('answer_contract'), dict) and bool(result['answer_contract'])
            and isinstance(result.get('agent_review'), dict)
            and result['agent_review'].get('status') == 'approved')


def isolated_sqlite_target(name, state):
    """SQLite URI 与 ATTACH 使用相同隔离根；先解码再裁决真实路径。"""
    if name == ':memory:':
        return True
    if not isinstance(name, (str, bytes, os.PathLike)):
        return False
    try:
        name = os.fsdecode(name)
        if name.startswith('file:'):
            parsed = urlsplit(name)
            if parsed.netloc not in ('', 'localhost'):
                return False
            name = unquote(parsed.path)
        return bool(name) and Path(name).resolve().is_relative_to(Path(state).resolve())
    except (ValueError, OSError):
        return False


def isolated_connection_factory(state):
    """业务覆盖 authorizer 时合并其规则；ATTACH 路径门禁始终优先。"""
    import sqlite3
    class IsolatedConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.set_authorizer(None)

        def set_authorizer(self, callback):
            if callback is not None and not callable(callback):
                raise TypeError('authorizer_must_be_callable_or_none')
            def combined(action, first, second, database, trigger):
                if action == sqlite3.SQLITE_ATTACH and not isolated_sqlite_target(first, state):
                    return sqlite3.SQLITE_DENY
                return callback(action, first, second, database, trigger) if callback else sqlite3.SQLITE_OK
            return super().set_authorizer(combined)
    return IsolatedConnection


def direct_http_opener(*handlers):
    """清空代理环境仍可能回退 macOS 系统代理，故明确禁用代理与重定向。"""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, new_url):
            raise PermissionError('unregistered_redirect')
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), *handlers)


def prepare(args):
    out = args.out_dir.resolve()
    if out == OUTPUT_ROOT.resolve() or not out.is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError('output_outside_registered_scope')
    consumer = consumer_source_contract(args)
    if consumer and out.is_relative_to(Path(consumer['source_root'])):
        raise ValueError('output_inside_consumer_source')
    out.mkdir(parents=True, exist_ok=False)
    candidate = out / 'candidate'
    candidate.mkdir()
    manifest = freeze(candidate, consumer)
    save(out / 'source-manifest.json', manifest)
    budget_resources = request_budget_contract(candidate, manifest)
    question_path = (args.question_set or PUBLIC / 'manual-question-set.json').resolve()
    questions = json.loads(question_path.read_text())
    cases = selected_cases(questions, args.cases)
    if len(cases) * LIMITS['max_tokens'] > args.max_total_tokens:
        raise ValueError('aggregate_token_preflight_rejected')
    reference = json.loads((PUBLIC / 'integration-data-manifest.json').read_text())
    database = (args.knowledge_database or Path(reference['knowledge_database']['path'])).resolve()
    expected_sha = args.database_sha256 or reference['knowledge_database']['sha256']
    release = args.release_id or reference['knowledge_database']['release_id']
    if args.knowledge_database and (not args.database_sha256 or not args.release_id):
        raise ValueError('custom_public_database_requires_hash_and_release')
    if sha(database) != expected_sha:
        raise ValueError('public_database_changed')
    prepared = prepared_contract(args, database, expected_sha, release, reference,
                                 candidate if consumer else None)
    if prepared and any(manifest.get(name) != digest for name, digest in prepared['source_sha256'].items()):
        raise ValueError('prepared_retrieval_source_not_frozen')
    inputs = out / 'inputs'
    inputs.mkdir()
    shutil.copy2(database, inputs / 'knowledge.sqlite3')
    shutil.copy2(question_path, inputs / 'question-set.json')
    bindings = {'question_set': {'source_path': str(question_path), 'sha256': sha(question_path)},
                'public_database': {'source_path': str(database), 'sha256': expected_sha}}
    if consumer:
        source = Path(consumer['manifest_path'])
        shutil.copy2(source, inputs / 'consumer-source-manifest.json')
        bindings['consumer-source-manifest.json'] = {'source_path': str(source), 'sha256': consumer['manifest_sha256']}
    for filename in ('product-review-manifest.json', 'integration-data-manifest.json'):
        source = PUBLIC / filename
        shutil.copy2(source, inputs / filename)
        bindings[filename] = {'source_path': str(source), 'sha256': sha(source)}
    pdfs = sorted((PUBLIC / 'sources/products').glob('*.pdf'))
    if prepared:
        review = json.loads((PUBLIC / 'product-review-manifest.json').read_text())
        documents = [d for fund in review.get('funds', []) for d in fund.get('documents', [])]
        if (len(pdfs) != 6 or len(documents) != 6
                or {Path(d['file']).resolve() for d in documents} != {p.resolve() for p in pdfs}
                or any(sha(d['file']) != d.get('raw_sha256') for d in documents)):
            raise ValueError('prepared_original_pdf_binding_invalid')
        source = Path(prepared['receipt_path'])
        shutil.copy2(source, inputs / 'prepared-public-manifest.json')
        bindings['prepared-public-manifest.json'] = {'source_path': str(source), 'sha256': prepared['receipt_sha256']}
        bindings['prepared-source-database'] = {'source_path': prepared['source_database_path'],
                                               'sha256': prepared['source_database_sha256']}
    for source in pdfs:
        destination = inputs / 'products' / source.name
        destination.parent.mkdir(exist_ok=True)
        shutil.copy2(source, destination)
        bindings['products/' + source.name] = {'source_path': str(source), 'sha256': sha(source)}
    frozen_inputs = {str(p.relative_to(inputs)): sha(p) for p in inputs.rglob('*') if p.is_file()}
    if frozen_inputs['knowledge.sqlite3'] != expected_sha or frozen_inputs['question-set.json'] != sha(question_path):
        raise ValueError('frozen_input_changed')
    config = {'schema_version': 1, 'created_at': stamp(), 'candidate': str(candidate),
              'out_dir': str(out), 'source_sha256': manifest, 'input_sha256': frozen_inputs,
              'execution_manifest_sha256': sha(out / 'source-manifest.json'),
              'input_bindings': bindings, 'release_id': release, 'cases': cases,
              'cloud': bool(args.cloud), 'execute': bool(args.execute), 'limits': LIMITS,
              'aggregate_admission_tokens': len(cases) * LIMITS['max_tokens'],
              'max_total_tokens': args.max_total_tokens, 'case_hard_seconds': args.case_hard_seconds,
              'retrieval_flags': {'NAVREN_VECTOR_ENABLED': str(args.vector_enabled),
                                  'NAVREN_STRUCTURE_ENABLED': str(args.structure_enabled),
                                  'NAVREN_RERANK_ENABLED': str(args.rerank_enabled),
                                  'NAVREN_ANSWER_FORM_ENABLED': str(args.answer_form_enabled)},
              'retrieval_environment': (prepared['environment'] if prepared else
                  {'NAVREN_VECTOR_ENCODER': 'dual', 'NAVREN_QUERY_TRANSLATION_ENABLED': '0',
                   'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}),
              'runner_sha256': sha(Path(__file__)),
              'prepared_public': prepared,
              'consumer_source_contract': consumer,
              'request_budget_resources': budget_resources,
              'cold_public_network': False, 'cloud_tts': False,
              'entrypoint': 'create_agent(off,progressive)->registered_create_server->registered_HTTP_session->POST /api/agent',
              'personal_data_boundary': 'no_live_private_db; isolated_synthetic_private_metadata_may_be_read',
              'tts_scope': 'receipt_text_only; real_audio_not_requested',
              'semantic_verdict': 'not_evaluated_by_runner'}
    if not consumer_source_unchanged(config):
        raise ValueError('consumer_source_changed_during_prepare')
    save(out / 'configuration.json', config)
    return config


def hashes_unchanged(config):
    candidate, inputs = Path(config['candidate']), Path(config['out_dir']) / 'inputs'
    return {'candidate_unchanged': execution_manifest_unchanged(config) and source_tree_unchanged(candidate, config['source_sha256']),
            'frozen_inputs_unchanged': all((inputs / p).is_file() and sha(inputs / p) == v
                                         for p, v in config['input_sha256'].items()),
            'original_inputs_unchanged': all(Path(v['source_path']).is_file() and sha(v['source_path']) == v['sha256']
                                           for v in config['input_bindings'].values()) and consumer_source_unchanged(config),
            'shared_model_assets_unchanged': shared_assets_unchanged(
                (config.get('prepared_public') or {}).get('model_assets', {})),
            'native_extensions_unchanged': shared_assets_unchanged(
                (config.get('prepared_public') or {}).get('native_runtime', {}).get('extensions', {}))
                and request_budget_unchanged(config)}


def case_worker(config_path, case_id):
    """只由本脚本父进程派发；任何业务 import 之前完成隔离。"""
    config = json.loads(Path(config_path).read_text())
    candidate, out = Path(config['candidate']), Path(config['out_dir']) / case_id
    case = next(c for c in config['cases'] if c['id'] == case_id)
    record = {'id': case_id, 'started_at': stamp(), 'question': case.get('question', case.get('request', {}).get('query')),
              'original_case_sha256': canonical_sha(case), 'original_expectation': safe_record(case),
              'mode': 'cloud' if config['cloud'] else 'offline_protocol_only', 'limits': config['limits'],
              'semantic_judgment': 'pending_independent_source_review', 'tts_status': 'not_requested_receipt_only',
              'live_private_database_access': False, 'external_network_attempts': [], 'provider_search_calls': []}
    started = time.monotonic()
    server = thread = runtime = None
    private_values = []
    try:
        if (not worker_identity_unchanged(config) or sha(Path(__file__)) != config['runner_sha256']
                or Path(__file__).resolve() != Path(config['candidate']) / 'scripts/run_current_rag_acceptance.py'):
            raise ValueError('worker_identity_changed')
        if not all(hashes_unchanged(config).values()):
            raise ValueError('pre_execution_inputs_changed')
        if any(name == prefix or name.startswith(prefix + '.') for name in sys.modules for prefix in BUSINESS):
            raise ValueError('business_import_not_fresh')
        with tempfile.TemporaryDirectory(prefix='navren-current-rag-') as directory, ExitStack() as stack:
            state = Path(directory).resolve()
            from cryptography.fernet import Fernet
            for name in ('NAVREN_FINANCIAL_DB', 'QINGZHOU_FINANCIAL_DB'):
                os.environ.pop(name, None)
            environment = {'NAVREN_PERSONAL_DATA_KEY': Fernet.generate_key().decode(),
                           'NAVREN_SIM_DB': str(state / 'bank.sqlite3'), 'QINGZHOU_SIM_DB': str(state / 'bank.sqlite3'),
                           'NAVREN_KNOWLEDGE_DB': str(state / 'knowledge.sqlite3'),
                           'NAVREN_MARKET_DB': str(state / 'market.sqlite3'),
                           'NAVREN_PUBLIC_DATA_DIR': str(state / 'public-data'),
                           'NAVREN_FUND_CACHE': str(state / 'funds'),
                           'NAVREN_MODEL_CALL_LOG': str(out / 'model-calls.jsonl'),
                           'NAVREN_PUBLIC_SYNC_ENABLED': '0', 'NAVREN_MESSAGE_WORKER_ENABLED': '0',
                           'NAVREN_LIVING_WORKER_ENABLED': '0',
                           'NAVREN_ALLOW_CLOUD_ASR': '0', 'NAVREN_AGENT_TIMEOUT': '105',
                           'PYTHONDONTWRITEBYTECODE': '1'}
            # The only approved cloud transport is direct fixed AiPing HTTPS.
            # A inherited proxy must not turn the restricted request into an unregistered hop.
            environment.update({name: '' for name in ('http_proxy', 'https_proxy', 'all_proxy',
                                                       'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')})
            environment.update({'NAVREN_VECTOR_ENCODER': 'dual', 'NAVREN_QUERY_TRANSLATION_ENABLED': '0',
                                'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'})
            prepared = config.get('prepared_public')
            if prepared:
                environment.update(prepared['environment'])
            environment.update(config['retrieval_flags'])
            record['retrieval_flags'] = deepcopy(config['retrieval_flags'])
            record['retrieval_environment'] = deepcopy(config['retrieval_environment'])
            record['runner_sha256'] = sha(Path(__file__))
            stack.enter_context(patch.dict(os.environ, environment))
            record['isolation_paths'] = {k: v for k, v in environment.items() if k.endswith(('_DB', '_DIR', '_CACHE'))}
            shutil.copy2(Path(config['out_dir']) / 'inputs/knowledge.sqlite3', state / 'knowledge.sqlite3')
            if sha(state / 'knowledge.sqlite3') != config['input_sha256']['knowledge.sqlite3']:
                raise ValueError('isolated_public_copy_changed')
            # SQLite audit hook is process-wide, including server threads. No live path can connect.
            def database_guard(event, args):
                if event == 'sqlite3.connect':
                    if not isolated_sqlite_target(args[0], state):
                        raise PermissionError('non_isolated_database_access')
            sys.addaudithook(database_guard)
            import sqlite3
            real_connect = sqlite3.connect
            connection_factory = isolated_connection_factory(state)
            def guarded_connect(*args, **kwargs):
                if len(args) > 5 or kwargs.get('factory', sqlite3.Connection) is not sqlite3.Connection:
                    raise PermissionError('custom_sqlite_factory_not_admitted')
                return real_connect(*args, **{**kwargs, 'factory': connection_factory})
            stack.enter_context(patch.object(sqlite3, 'connect', guarded_connect))
            # Trace envelopes 包装器不再经 urllib 的系统默认 opener。
            direct_transport = direct_http_opener()
            real_opener = urllib.request.OpenerDirector.open
            loopback_ports = set()
            def permitted(request):
                url = request.full_url if hasattr(request, 'full_url') else str(request)
                parsed = urlsplit(url)
                return (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')
                        and parsed.port in loopback_ports) or (config['cloud'] and url == MODEL_URL)
            def guarded_open(request, *args, **kwargs):
                if not permitted(request):
                    record['external_network_attempts'].append({'outcome': 'denied', 'layer': 'urlopen'})
                    raise PermissionError('unregistered_network_request')
                return direct_transport.open(request, *args, **kwargs)
            def guarded_opener(opener, request, *args, **kwargs):
                if not permitted(request):
                    record['external_network_attempts'].append({'outcome': 'denied', 'layer': 'opener'})
                    raise PermissionError('unregistered_network_request')
                return real_opener(opener, request, *args, **kwargs)
            stack.enter_context(patch('urllib.request.urlopen', guarded_open))
            stack.enter_context(patch.object(urllib.request.OpenerDirector, 'open', guarded_opener))
            # Also cover libraries that do not use urllib, including websocket/requests direct connects.
            import socket
            real_dns = socket.getaddrinfo
            allowed_ips = set()
            def guarded_dns(host, *args, **kwargs):
                if host not in ('127.0.0.1', 'localhost', b'127.0.0.1', b'localhost') and not (
                        config['cloud'] and host in ('aiping.cn', b'aiping.cn')):
                    record['external_network_attempts'].append({'outcome': 'denied', 'layer': 'dns'})
                    raise PermissionError('unregistered_dns_request')
                results = real_dns(host, *args, **kwargs)
                if host in ('aiping.cn', b'aiping.cn'):
                    allowed_ips.update(row[4][0] for row in results)
                return results
            stack.enter_context(patch.object(socket, 'getaddrinfo', guarded_dns))
            def socket_guard(event, args):
                if event == 'socket.connect':
                    address = args[1]
                    if isinstance(address, tuple):
                        host, port = address[:2]
                        if (host in ('127.0.0.1', '::1') and port in loopback_ports or
                                config['cloud'] and host in allowed_ips and port == 443):
                            return
                    record['external_network_attempts'].append({'outcome': 'denied', 'layer': 'socket'})
                    raise PermissionError('unregistered_socket_request')
            sys.addaudithook(socket_guard)
            sys.path.insert(0, str(candidate))
            # 两个Provider以及for_release/publish均不能在验收期间偷偷建索引。
            from local_knowledge import vectors
            suppress_vector_background(vectors, stack, record)
            if prepared:
                record['native_runtime'] = native_runtime()
                if record['native_runtime'] != prepared['native_runtime']:
                    raise ValueError('prepared_native_runtime_changed')
                record['prepared_database_before'] = validate_prepared_database(state / 'knowledge.sqlite3', prepared)
            from agent_runtime.agent_factory import create_agent
            from agent_runtime.conversation import GLMConversationReviewer
            from local_knowledge import LocalKnowledgeProvider
            from local_knowledge.runtime import EvidenceRuntime
            from local_knowledge.store import KnowledgeError
            from server import create_server, ProxyHandler
            from voice_gateway import VoiceGateway
            from scripts.run_skill_acceptance import TracePrimary, TraceReviewer, safe_data
            from scripts.run_rag_acceptance import ProtocolPrimary, ProtocolReviewer
            for name in REGISTERED_ENTRY_HELPERS:
                relative = 'tests/window_integration/' + name + '.py'
                registered_source_file(candidate, relative, config['source_sha256'].get(relative))
            sys.path.insert(0, str(candidate / 'tests/window_integration'))
            from registered_entry_context import (build_template, template_factory, attach_server,
                browser_bootstrap, browser_consume, request_context)
            for name in REGISTERED_ENTRY_HELPERS:
                relative = 'tests/window_integration/' + name + '.py'
                module = __import__(name)
                if Path(module.__file__).resolve() != candidate / relative:
                    raise ValueError('registered_entry_helper_outside_frozen_identity')
            for name in ('scripts.run_skill_acceptance', 'scripts.run_rag_acceptance'):
                relative = name.replace('.', '/') + '.py'
                path = registered_source_file(candidate, relative, config['source_sha256'][relative])
                if Path(sys.modules[name].__file__).resolve() != path:
                    raise ValueError('harness_control_module_outside_frozen_identity')
            import fund_history
            if fund_history.CACHE.resolve() != state / 'funds':
                raise ValueError('fund_cache_not_isolated')
            provider = LocalKnowledgeProvider(state / 'knowledge.sqlite3', release_id=config['release_id'])
            def cold_disabled(*args, **kwargs):
                record['external_network_attempts'].append({'outcome': 'disabled', 'layer': 'runtime_reader'})
                raise KnowledgeError('cold_network_disabled')
            runtime = EvidenceRuntime(provider, reader=cold_disabled, background=False)
            original_search = LocalKnowledgeProvider.search_public
            def observed_search(instance, request, release_id):
                call = {'request': deepcopy(request), 'release_id': release_id, 'status': 'started'}
                record['provider_search_calls'].append(call)
                try:
                    result = original_search(instance, request, release_id)
                except Exception as error:
                    call.update(status='failed', error_code=getattr(error, 'code', type(error).__name__))
                    raise
                call.update(status='completed', citation_ids=deepcopy(result.get('citation_ids', [])),
                            coverage_gaps=deepcopy(result.get('coverage_gaps', [])),
                            retrieval=retrieval_diagnostic(result.get('retrieval')))
                return result
            stack.enter_context(patch.object(LocalKnowledgeProvider, 'search_public', observed_search))
            if config['cloud']:
                primary = TracePrimary(out / 'model-calls.jsonl')
                class CompatibleTraceReviewer(TraceReviewer):
                    def review(self, packet, *args, **kwargs):
                        return trace_reviewer_call(self, GLMConversationReviewer.review,
                                                   safe_data, packet, *args, **kwargs)
                reviewer = CompatibleTraceReviewer(out / 'model-calls.jsonl')
            else:
                class OfflinePrimary(ProtocolPrimary):
                    billing_mode = 'unpaid_fixture'
                    metric_selection_support = True
                    supports_compact_metric_sources = True
                    supports_financial_metric_catalog = True
                    # Progressive discovery must use the real exposed skill expansion first.
                    def complete(self, messages, tools, **kwargs):
                        names = {t['function']['name'] for t in tools}
                        if self.step == 0 and 'knowledge_search' not in names:
                            from scripts.run_rag_acceptance import native_call
                            answer = native_call('load_skill', {'name': 'knowledge-retrieval'})
                            # This is an explicit protocol fixture, never a cloud query rewrite.
                            return answer, {'prompt_tokens': 0, 'completion_tokens': 1, 'total_tokens': 1}
                        answer, _ = super().complete(messages, tools, **kwargs)
                        terminal = next((t['function']['parameters'] for t in tools
                            if t['function']['name'] == 'answer_reviewed'), {})
                        if (answer['tool_calls'][0]['function']['name'] == 'answer_reviewed'
                                and 'direct_answer' in terminal.get('properties', {})):
                            parts = terminal['properties']['direct_answer']['items']['oneOf']
                            quote_ids = next(p['properties']['public_quote_fact_id']['enum']
                                for p in parts if 'public_quote_fact_id' in p.get('properties', {}))
                            arguments = {'direct_answer': [{'text':
                                '隔离协议预检：请按以下原文核对条款；适用日期、份额及渠道仍须审核。'}]
                                + [{'public_quote_fact_id': item} for item in quote_ids[:6]],
                                'conditions': [], 'unknowns': [], 'claims': [],
                                'amount_claims': [], 'draft_ids': [], 'followups': []}
                            answer['tool_calls'][0]['function']['arguments'] = json.dumps(arguments, ensure_ascii=False)
                            self.calls[-1]['arguments'] = deepcopy(arguments)
                        return answer, {'prompt_tokens': 0, 'completion_tokens': 1, 'total_tokens': 1}
                class OfflineReviewer(ProtocolReviewer):
                    billing_mode = 'unpaid_fixture'
                    def __init__(self):
                        super().__init__()
                        from agent_runtime.model_adapters import OpenAICompatibleJsonClient, GLM_REVIEW_MODEL
                        self.model_id = GLM_REVIEW_MODEL
                        local_url = 'https://offline-final-review.invalid/v1/chat/completions'
                        def local_open(request, **kwargs):
                            if (request.full_url != local_url or request.get_method() != 'POST'
                                    or json.loads(request.data).get('model') != GLM_REVIEW_MODEL):
                                raise ValueError('offline_review_wire_contract_changed')
                            import io
                            return io.BytesIO(json.dumps({'model': GLM_REVIEW_MODEL,
                                'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant',
                                    'content': json.dumps({'verdict': 'approve',
                                        'reason': '仅离线协议回归，不判金融语义', 'issues': []}, ensure_ascii=False)}}],
                                'usage': {'prompt_tokens': 0, 'completion_tokens': 1, 'total_tokens': 1}
                            }, ensure_ascii=False).encode())
                        client = OpenAICompatibleJsonClient(provider='ai_ping', model_id=GLM_REVIEW_MODEL,
                            url=local_url, env_name='NAVREN_OFFLINE_FIXTURE_KEY',
                            keychain_service='CC/NAVREN_OFFLINE_FIXTURE_KEY', timeout=35, max_retries=0,
                            urlopen=local_open, key_loader=lambda: 'offline-fixture-only', log_path=os.devnull)
                        self.inner = GLMConversationReviewer(client=client)
                    def request_input_bound(self, packet):
                        return self.inner.request_input_bound(packet)
                    def review(self, packet, *args, **kwargs):
                        # 与已采用ProcessPreflightReviewer一致：实际标准GLM builder
                        # 经内存transport产生wire，保留完整出站/最终投影校验。
                        result = self.inner.review(packet, *args, **kwargs)
                        self.records.append({'packet': deepcopy(safe_data(packet)),
                            'decision': {k: deepcopy(v) for k, v in result.items() if k != '_agent_review_wire'}})
                        return result
                primary, reviewer = OfflinePrimary(deepcopy(case)), OfflineReviewer()
            agent = create_agent(route_provider='off', primary=primary, reviewer=reviewer,
                                 max_rounds=8, max_tools=12, max_tokens=100000, timeout_seconds=105)
            from agent_runtime.complex_agent import ComplexConversationAgent
            if (type(agent) is not ComplexConversationAgent or agent.route_provider != 'off'
                    or agent.tool_routing != 'progressive'):
                raise ValueError('factory_entrypoint_contract_changed')
            record['factory_class'] = type(agent).__name__
            record['factory_contract_asserted'] = True
            record['configured_models'] = {'primary': {'provider': getattr(primary, 'provider', 'unpaid_fixture'),
                'model_id': primary.model_id}, 'reviewer': {'provider': 'ai_ping' if config['cloud'] else 'unpaid_fixture',
                'model_id': reviewer.model_id}, 'route_provider': agent.route_provider, 'tool_routing': agent.tool_routing}
            stack.enter_context(patch.object(ProxyHandler, 'log_message', lambda *args: None))
            template = build_template(candidate, state)
            with template_factory(template) as registered_factory:
                server = create_server(0, state / 'bank.sqlite3', finance_factory=registered_factory,
                    voice=VoiceGateway(key='isolated-no-cloud-audio', url='wss://disabled.invalid/api-ws/v1/inference'),
                    conversation_agent=agent, evidence_runtime=runtime, market_database=state / 'market.sqlite3',
                    public_sync_enabled=False)
            registered_host = attach_server(server, template, 'tycoon')
            if server.finance.registered_mode is not True:
                raise ValueError('registered_consumer_required')
            server.daemon_threads = False
            loopback_ports.add(server.server_port)
            def cleanup():
                if thread is not None:
                    server.shutdown()
                server.server_close()
                if thread is not None:
                    thread.join(3)
            stack.callback(cleanup)
            server.finance.knowledge_provider = provider
            if server.evidence_runtime.provider is not server.finance.knowledge_provider:
                raise ValueError('provider_runtime_pointer_mismatch')
            if not server.finance.personal_data.database.resolve().is_relative_to(state):
                raise ValueError('personal_database_not_isolated')
            if server.finance.financial_data is not None:
                raise ValueError('financial_database_not_isolated')
            record['provider_status'] = provider.status()
            if prepared:
                assert_prepared_provider(record['provider_status'], prepared)
            record['network_guard_scope'] = 'own_http_port_and_exact_aiping_url; no_other_loopback_services'
            record['transport_scope'] = 'explicit_direct_opener; no_system_proxy_or_redirect'
            record['sqlite_guard_scope'] = 'connect_paths_and_composed_attach_authorizer_with_uri_decoding'
            if record['provider_status'].get('public_release_id') != config['release_id']:
                raise ValueError('fixed_release_changed')
            cookies = CookieJar()
            client = direct_http_opener(urllib.request.HTTPCookieProcessor(cookies))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_port)
            def request(path, body=None, csrf=None):
                headers = {'Content-Type': 'application/json', 'Origin': base}
                if csrf:
                    headers['X-QZ-CSRF'] = csrf
                req = urllib.request.Request(base + path, headers=headers,
                    data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
                try:
                    response = client.open(req, timeout=115)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    return response.status, json.loads(response.read()), dict(response.headers)
            browser_bootstrap(client, base)
            code, session, _ = request('/api/finance/session', {})
            if code != 200:
                raise ValueError('session_setup_failed')
            workspace = session['workspace_id']
            private_values.extend([workspace, session['csrf']])
            browser_consume(client, base, session)
            private_values.extend([session.get('registered_user_id'), session.get('persona_id')])
            record['registered_consumer'] = {'registered_mode': True,
                'tenant_binding_validated': True, 'original_fixture_contract': template.source_contract,
                'original_business_equivalence_validated': bool(registered_host.completed),
                'phone_registration_tested': False}
            with request_context(server.finance, workspace):
                server.finance.wealth.provision(workspace)
            def financial_state():
                with closing(server.finance.bank.connect()) as db:
                    tables = ('accounts', 'profiles', 'holdings', 'orders', 'transactions', 'fund_holds',
                              'operations', 'calendar_series', 'calendar_event_drafts', 'service_negotiation_applications')
                    return {table: [dict(row) for row in db.execute('SELECT * FROM ' + table + ' WHERE workspace_id=? ORDER BY rowid', (workspace,))]
                            for table in tables}
            before = financial_state()
            status_http, status_body, _ = request('/api/finance/knowledge/status')
            code, result, headers = request('/api/agent', {'text': record['question']}, session['csrf'])
            after = financial_state()
            record.update(http_status=code, response=result, status_http=status_http, status_body=status_body,
                          financial_before_sha256=canonical_sha(before), financial_after_sha256=canonical_sha(after),
                          financial_state_unchanged=before == after, model_tool_calls=primary.calls,
                          tool_outputs=primary.tool_outputs, reviewer_records=reviewer.records,
                          primary_response_metadata=getattr(primary, 'response_metadata', []),
                          reviewer_response_metadata=getattr(reviewer, 'response_metadata', []))
            receipt = result.get('speech_receipt')
            speech_text, speech_valid, speech_error = None, False, None
            if receipt:
                private_values.append(receipt)
                try:
                    token = next(cookie.value for cookie in cookies if cookie.name == 'qz_bank_user')
                    private_values.append(token)
                    with request_context(server.finance, workspace):
                        speech_text = server.finance.resolve_speech(workspace, receipt, token)
                    speech_valid = bool(isinstance(speech_text, str) and speech_text.strip())
                except Exception as error:
                    speech_error = getattr(error, 'code', type(error).__name__)
            hits = [h for item in primary.tool_outputs if isinstance(item.get('output'), dict)
                    and isinstance(item['output'].get('data'), dict)
                    for h in item['output']['data'].get('hits', []) if isinstance(h, dict)]
            expected = set(case.get('expected_review_keys', []))
            keys = {h.get('locator', {}).get('paragraph', '').split('/', 1)[-1] for h in hits}
            snapshot = result.get('knowledge_snapshot') or {}
            record.update(speech_receipt_issued=bool(receipt), speech_receipt_validated=speech_valid,
                          speech_text=speech_text, speech_error=speech_error,
                          speech_text_matches_reply=(isinstance(speech_text, str) and
                              unicodedata.normalize('NFKC', speech_text).strip() ==
                              unicodedata.normalize('NFKC', result.get('reply', '')).strip()),
                          retrieved_review_keys=sorted(keys), expected_review_keys=sorted(expected),
                          all_expected_evidence_retrieved=bool(expected) and expected <= keys,
                          fixed_release_preserved=snapshot.get('release_id') == config['release_id'],
                          http_reply_approved=approved_reply(code, result))
            runtime_result = result.get('agent_runtime', {})
            record['actual_primary_requests_sent'] = runtime_result.get('primary_requests_sent')
            record['actual_reviewer_requests_sent'] = runtime_result.get('reviewer_requests_sent')
            record['runtime_request_count_semantics'] = 'adapter_call_attempts; not_confirmation_of_provider_receipt'
            record['primary_provider_response_count'] = len(record['primary_response_metadata'])
            record['reviewer_provider_response_count'] = len(record['reviewer_response_metadata'])
            loaded = {name: str(Path(module.__file__).resolve()) for name, module in sys.modules.items()
                      if getattr(module, '__file__', None) and any(name == p or name.startswith(p + '.') for p in BUSINESS)}
            if any(not Path(path).is_relative_to(candidate) for path in loaded.values()):
                raise ValueError('business_module_outside_candidate')
            record['loaded_module_paths'] = loaded
            diagnostics = [item.get('retrieval', {}) for item in record['provider_search_calls']
                           if item.get('status') == 'completed']
            record['retrieval_actual_used'] = {
                'vector': any(item.get('vector_used') is True for item in diagnostics),
                **{key: any(item.get(key, {}).get('used') is True for item in diagnostics)
                   for key in ('structure', 'rerank', 'answer_form', 'translation')}}
            record['vector_runtime_observed'] = (config['retrieval_flags']['NAVREN_VECTOR_ENABLED'] != '1'
                                                 or vector_runtime_observed(diagnostics, prepared))
            if prepared:
                record['prepared_database_after'] = validate_prepared_database(state / 'knowledge.sqlite3', prepared)
            record['technical_collection_complete'] = True
    except Exception as error:
        record['error'] = {'type': type(error).__name__, 'code': getattr(error, 'code', type(error).__name__)}
        record['technical_collection_complete'] = False
    record.update(completed_at=stamp(), elapsed_seconds=time.monotonic() - started,
                  source_integrity=hashes_unchanged(config))
    # A captured fallback is a valid observation, never relabelled as an approved answer.
    checks = {'collection': record.get('technical_collection_complete') is True,
              'source_integrity': all(record['source_integrity'].values()),
              'no_financial_execution': record.get('financial_state_unchanged') is True}
    record['collection_checks'] = checks
    record['worker_exit_code'] = 0 if all(checks.values()) else 1
    save(out / 'record.json', safe_record(record, private_values))
    return record['worker_exit_code']


def run(args):
    existing_output = args.out_dir.exists()
    try:
        config = prepare(args)
    except Exception as error:
        # Do not rewrite an existing run, including when fail-on-existing triggered.
        safe_code = str(error) if type(error) is ValueError else type(error).__name__
        failure = {'status': 'preflight_failed', 'error_type': type(error).__name__,
                   'error_code': safe_code, 'exit_code': 2, 'model_calls': 0}
        if not existing_output and args.out_dir.is_dir() and args.out_dir.resolve().is_relative_to(OUTPUT_ROOT.resolve()):
            save(args.out_dir / 'results.json', failure)
        print(json.dumps(failure))
        return 2
    out = Path(config['out_dir'])
    report = {'schema_version': 1, 'started_at': stamp(), 'mode': 'cloud' if args.cloud else 'offline_protocol_only',
              'execute': bool(args.execute), 'requested_cases': len(config['cases']), 'records': [],
              'semantic_verdict': 'not_evaluated_by_runner', 'tts_status': 'real_audio_not_requested',
              'limits': LIMITS, 'aggregate_admission_tokens': config['aggregate_admission_tokens'],
              'entrypoint': config['entrypoint'], 'cloud_authorized_by_flag': bool(args.cloud and args.execute)}
    if not args.execute:
        integrity = hashes_unchanged(config)
        passed = all(value is True for value in integrity.values())
        report.update(status='PREFLIGHT_ONLY_NO_SERVER_NO_API' if passed else 'PREFLIGHT_INTEGRITY_FAILED',
                      exit_code=0 if passed else 2, source_integrity=integrity)
        save(out / 'results.json', report)
        return report['exit_code']
    for case in config['cases']:
        if not worker_identity_unchanged(config) or not all(hashes_unchanged(config).values()):
            report.update(status='PRE_EXECUTION_INTEGRITY_FAILED', exit_code=2,
                          source_integrity=hashes_unchanged(config), completed_at=stamp())
            save(out / 'results.json', report)
            return 2
        case_out = out / case['id']
        case_out.mkdir()
        command = [sys.executable, '-I', '-B', str(Path(config['candidate']) / 'scripts/run_current_rag_acceptance.py'),
                   '--case-worker', str(out / 'configuration.json'), '--case-id', case['id']]
        timed_out = False
        with (case_out / 'worker.log').open('wb') as log:
            child = subprocess.Popen(command, stdout=log, stderr=log, cwd=config['candidate'], start_new_session=True)
            try:
                actual_exit = child.wait(timeout=args.case_hard_seconds)
            except subprocess.TimeoutExpired:
                import signal
                timed_out = True
                os.killpg(child.pid, signal.SIGKILL)
                actual_exit = child.wait()
        path = case_out / 'record.json'
        record = json.loads(path.read_text()) if path.exists() else {'id': case['id'], 'technical_collection_complete': False}
        record['actual_child_exit_code'] = actual_exit
        record['hard_timeout'] = timed_out
        report['records'].append(record)
        save(out / 'results.json', report)
        print(json.dumps({'id': case['id'], 'actual_child_exit_code': actual_exit,
                          'http_reply_approved': record.get('http_reply_approved'),
                          'actual_primary_requests_sent': record.get('actual_primary_requests_sent'),
                          'actual_reviewer_requests_sent': record.get('actual_reviewer_requests_sent')}, ensure_ascii=False), flush=True)
        if args.stop_on_failure and (actual_exit != 0 or timed_out or record.get('http_reply_approved') is not True
                or record.get('all_expected_evidence_retrieved') is not True
                or record.get('speech_receipt_validated') is not True):
            report['stop_reason'] = 'pilot_technical_or_evidence_failure'
            break
    report['source_integrity'] = hashes_unchanged(config)
    consumer = config.get('consumer_source_contract')
    origin = Path(consumer['source_root']) if consumer else ROOT
    origin_manifest = consumer['source_sha256'] if consumer else config['source_sha256']
    report['working_tree_changes_since_freeze'] = [name for name, digest in origin_manifest.items()
        if not (origin / name).is_file() or sha(origin / name) != digest]
    complete = (len(report['records']) == len(config['cases']) and all(r['actual_child_exit_code'] == 0
                and not r['hard_timeout'] for r in report['records']) and all(report['source_integrity'].values()))
    gates = {'complete_collection': complete,
             'approved_replies': bool(report['records']) and all(r.get('http_reply_approved') is True for r in report['records']),
             'expected_evidence': bool(report['records']) and all(r.get('all_expected_evidence_retrieved') is True for r in report['records']),
             'fixed_release': bool(report['records']) and all(r.get('fixed_release_preserved') is True for r in report['records']),
             'speech_receipt': bool(report['records']) and all(r.get('speech_receipt_validated') is True
                                and r.get('speech_text_matches_reply') is True for r in report['records']),
             'factory_entrypoint': bool(report['records']) and all(r.get('factory_contract_asserted') is True for r in report['records']),
             'registered_consumer': bool(report['records']) and all(
                 r.get('registered_consumer', {}).get('registered_mode') is True
                 and r.get('registered_consumer', {}).get('tenant_binding_validated') is True
                 and r.get('registered_consumer', {}).get('original_business_equivalence_validated') is True
                 for r in report['records']),
             'no_financial_execution': bool(report['records']) and all(r.get('financial_state_unchanged') is True for r in report['records'])}
    if config['retrieval_flags']['NAVREN_VECTOR_ENABLED'] == '1':
        gates['vector_runtime_observed'] = bool(report['records']) and all(
            r.get('vector_runtime_observed') is True for r in report['records'])
    passed = all(gates.values())
    report.update(status='TECHNICAL_GATES_PASS_PENDING_INDEPENDENT_JUDGMENT' if passed else
                         'COLLECTED_TECHNICAL_FAILURE' if complete else 'COLLECTION_FAILED',
                  technical_gates=gates, completed_at=stamp(), exit_code=0 if passed else 1)
    report['summary'] = {'collected_cases': len(report['records']),
                         'approved_replies': sum(r.get('http_reply_approved') is True for r in report['records']),
                         'full_expected_retrieval': sum(r.get('all_expected_evidence_retrieved') is True for r in report['records']),
                         'speech_receipts': sum(r.get('speech_receipt_validated') is True for r in report['records']),
                         'actual_audio_cases': 0, 'semantic_passes': None}
    save(out / 'results.json', report)
    return report['exit_code']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path)
    parser.add_argument('--consumer-source-root', type=Path)
    parser.add_argument('--consumer-source-manifest', type=Path)
    parser.add_argument('--consumer-source-manifest-sha256')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--cloud', action='store_true')
    parser.add_argument('--stop-on-failure', action='store_true')
    parser.add_argument('--cases', default='')
    parser.add_argument('--question-set', type=Path)
    parser.add_argument('--knowledge-database', type=Path)
    parser.add_argument('--database-sha256')
    parser.add_argument('--release-id')
    parser.add_argument('--prepared-public-manifest', type=Path)
    parser.add_argument('--prepared-public-manifest-sha256')
    parser.add_argument('--max-total-tokens', type=int, default=200000)
    parser.add_argument('--case-hard-seconds', type=int, default=150)
    parser.add_argument('--vector-enabled', type=int, choices=(0, 1), default=0)
    parser.add_argument('--structure-enabled', type=int, choices=(0, 1), default=0)
    parser.add_argument('--rerank-enabled', type=int, choices=(0, 1), default=0)
    parser.add_argument('--answer-form-enabled', type=int, choices=(0, 1), default=0)
    parser.add_argument('--case-worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--case-id', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.case_worker:
        if not args.case_id:
            parser.error('worker_case_required')
        return case_worker(args.case_worker, args.case_id)
    if not args.out_dir or args.max_total_tokens < 1 or not 120 <= args.case_hard_seconds <= 1800:
        parser.error('fresh_output_and_bounded_configuration_required')
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
