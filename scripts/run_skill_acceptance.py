#!/usr/bin/env python3
"""冻结题集的真实模型采集；独立临时银行，金融语义由另行复核裁决。"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_manifest(dataset_path=None, dependencies=()):
    """冻结当前生产代码、静态页面、实际技能和合成数据；支持独立候选快照根。"""
    paths = set()
    excluded = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'outputs',
                'evaluation', 'tests', 'scripts', 'skills', 'upstream', 'archive', 'archives', 'backups'}
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in excluded]
        paths.update(Path(directory)/name for name in files if name.endswith('.py'))
    paths.update(ROOT.glob('*.html'))
    for name in ('js', 'css', 'assets'):
        directory = ROOT/name
        if directory.is_dir():
            paths.update(path for path in directory.rglob('*') if path.is_file())
    paths.update(path for path in (ROOT/'skills').glob('*/SKILL.md') if 'upstream' not in path.relative_to(ROOT).parts)
    paths.update(path for path in (ROOT/'skills').glob('*/references/*.md') if 'upstream' not in path.relative_to(ROOT).parts)
    # customer_seed 也读取 reference/*.json；它们属于执行输入，而非只读来源说明。
    for suffix in ('*.json', '*.csv', '*.yaml', '*.yml', '*.sql'):
        paths.update((ROOT/'simulated_bank').rglob(suffix))
        for directory in ('knowledge', 'local_knowledge'):
            paths.update(path for path in (ROOT/directory).rglob(suffix)
                         if not any(part in excluded for part in path.relative_to(ROOT).parts))
    paths.update((ROOT/'scripts/run_skill_acceptance.py', ROOT/'tests/test_skill_acceptance.py'))
    if dataset_path is not None:
        paths.add(Path(dataset_path).resolve())
    for relative in dependencies:
        path = (ROOT/relative).resolve()
        if not path.is_relative_to(ROOT.resolve()):
            raise ValueError('dataset_dependency_outside_root')
        if not any(part in {'outputs', 'upstream'} for part in path.relative_to(ROOT.resolve()).parts):
            paths.add(path)
    manifest = {}
    for path in sorted(paths):
        if path.is_file():
            path = path.resolve()
            key = str(path.relative_to(ROOT.resolve())) if path.is_relative_to(ROOT.resolve()) else str(path)
            manifest[key] = digest(path)
    return manifest


# 先冻结再导入，避免执行已导入旧模块却绑定稍后变化的磁盘源码。
_IMPORT_ROOT = ROOT
_IMPORT_SOURCE_MANIFEST = {}
_PRODUCTION_IMPORT_ERROR = None
try:
    _IMPORT_SOURCE_MANIFEST = source_manifest()
    from agent_runtime.bank_tools import BankTools, safe_data
    from agent_runtime.conversation import ConversationAgent, GLMConversationReviewer
    from agent_runtime.native_client import AIPingToolClient
    from server import create_server
except Exception as error:
    # 并行生产编辑造成语法/导入失败时，CLI 仍能保存 startup 失败产物。
    _PRODUCTION_IMPORT_ERROR = error
    AIPingToolClient = globals().get('AIPingToolClient', object)
    GLMConversationReviewer = globals().get('GLMConversationReviewer', object)


def write_json(path, value):
    """严格 JSON 原子保存；每次运行须使用未占用的独立目录。"""
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.acceptance-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _reject_constant(value):
    raise ValueError('non_finite_constant')


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def trace_json(raw):
    """观测层不得抛解析错误或改写原消息；不保存无法安全解析的原始参数。"""
    try:
        value = safe_data(json.loads(raw, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs))
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        return value, None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        text = raw if isinstance(raw, str) else repr(type(raw))
        return {'unparsed': True, 'length': len(text),
                'sha256': hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()}, 'invalid_trace_json'


def record_envelopes(records):
    """仅供应商身份和回执号；不另存正文、隐藏推理、请求头或密钥。"""
    class Response:
        def __init__(self, raw):
            self.raw = raw
            self.status = getattr(raw, 'status', 200)
        def __enter__(self):
            self.raw.__enter__()
            return self
        def __exit__(self, *args):
            return self.raw.__exit__(*args)
        def read(self, limit):
            data = self.raw.read(limit)
            try:
                envelope = json.loads(data)
                if isinstance(envelope, dict):
                    records.append({key: envelope[key] for key in ('model', 'id')
                                    if isinstance(envelope.get(key), str) and len(envelope[key]) <= 256})
            except (ValueError, TypeError):
                pass
            return data
    return lambda *args, **kwargs: Response(urllib.request.urlopen(*args, **kwargs))


class TracePrimary(AIPingToolClient):
    def __init__(self, log_path):
        super().__init__(log_path=str(log_path))
        self.calls, self.tool_outputs, self.seen = [], [], set()
        self.response_metadata = []
        self.urlopen = record_envelopes(self.response_metadata)

    def complete(self, messages, tools, **kwargs):
        for index, message in enumerate(messages):
            marker = (index, message.get('tool_call_id'))
            if message.get('role') == 'tool' and marker not in self.seen:
                self.seen.add(marker)
                output, error = trace_json(message.get('content'))
                row = {'call_id': message.get('tool_call_id'), 'output': output}
                if error:
                    row['trace_parse_error'] = error
                self.tool_outputs.append(row)
        message, usage = super().complete(messages, tools, **kwargs)
        for item in message.get('tool_calls') or []:
            function = item.get('function', {})
            arguments, error = trace_json(function.get('arguments'))
            row = {'id': item.get('id'), 'name': function.get('name'), 'arguments': arguments}
            if error:
                row['arguments_parse_error'] = error
            self.calls.append(row)
        return message, usage


class TraceReviewer(GLMConversationReviewer):
    def __init__(self, log_path):
        super().__init__()
        self.client.log_path = str(log_path)
        self.records = []
        self.response_metadata = []
        self.client.urlopen = record_envelopes(self.response_metadata)

    def review(self, packet, *args):
        result = super().review(packet, *args)
        self.records.append({'packet': deepcopy(safe_data(packet)), 'decision': deepcopy(safe_data(result))})
        return result


class EvalCatalog:
    def __init__(self, fixture): self.fixture = fixture
    def read(self, query='', limit=12): return deepcopy(self.fixture['public_catalog_result'])


class EvalPublicProvider:
    def __init__(self, fixture): self.fixture = fixture
    def status(self): return deepcopy(self.fixture['provider_status'])
    def public_catalog(self):
        # 仅冻结合成公开原文标识/主体，供新的别名边界登记；不注入个人资料。
        evidence = self.fixture['canonical_evidence']
        terms = [evidence[key] for key in ('title', 'issuer', 'product_name', 'share_class', 'channel_name')
                 if isinstance(evidence.get(key), str) and evidence[key]]
        identifiers = [evidence[key] for key in ('citation_id', 'release_id', 'document_id', 'product_id', 'share_class_id', 'channel_id')
                       if isinstance(evidence.get(key), str) and evidence[key]]
        return {'terms': terms, 'identifiers': identifiers}
    def search_public(self, request, release_id):
        if request['scope'] != 'public': raise ValueError('not_public')
        if release_id != self.fixture['provider_status']['public_release_id']:
            return {'schema_version': 1, 'release_id': release_id, 'citation_ids': [], 'coverage_gaps': []}
        return deepcopy(self.fixture['search_result'])
    def resolve_public_evidence(self, citation_id, release_id, as_of):
        row = self.fixture['canonical_evidence']
        if (citation_id != row['citation_id'] or release_id != row['release_id']
                or not row['effective_from'] <= as_of < row['effective_to']): return None
        return deepcopy(row)


class EvalUnavailableProvider:
    """冻结的“资料不可用”条件适配新接口；不冒充生产 RAG 状态。"""
    def status(self):
        return {'schema_version': 1, 'provider_id': 'eval.unavailable.v1', 'ready': False, 'public_release_id': None}
    def public_catalog(self):
        return {'terms': [], 'identifiers': []}
    def search_public(self, request, release_id):
        return {'schema_version': 1, 'release_id': release_id, 'citation_ids': [], 'coverage_gaps': []}
    def resolve_public_evidence(self, citation_id, release_id, as_of):
        return None


PREFLIGHT_TOOLS = ('load_skill', 'account_summary', 'personal_graph', 'knowledge_status', 'budget', 'cashflow')


class ProcessPreflightPrimary:
    """只证明当前HTTP/工具/边界/保存链能执行，不模拟模型金融分析质量。"""
    model_id = 'offline-scripted-process-preflight'
    def __init__(self, log_path=None):
        self.calls, self.tool_outputs, self.seen, self.response_metadata = [], [], set(), []
        self.rounds = 0
    def complete(self, messages, tools, **kwargs):
        for index, item in enumerate(messages):
            marker = (index, item.get('tool_call_id'))
            if item.get('role') == 'tool' and marker not in self.seen:
                self.seen.add(marker)
                output, error = trace_json(item.get('content'))
                record = {'call_id': item.get('tool_call_id'), 'output': output}
                if error: record['trace_parse_error'] = error
                self.tool_outputs.append(record)
        self.rounds += 1
        if self.rounds == 1:
            planned = [('load_skill', {'name': 'financial-diagnosis'}), ('account_summary', {}),
                       ('personal_graph', {'view': 'overview', 'question': '仅核对离线集成所需的财务图谱，不创建操作。'}),
                       ('knowledge_status', {})]
        elif self.rounds == 2:
            account_output = next(record['output'] for record in self.tool_outputs if record['call_id'] == 'preflight-1-account_summary')
            main = next(account['id'] for account in account_output['data']['accounts'] if account['id'].endswith('-main'))
            planned = [('budget', {'account_id': main}), ('cashflow', {'days': 60})]
        elif self.rounds == 3:
            planned = [('answer_reviewed', {'text': '离线集成预检完成，仅核对程序链路。',
                        'claims': [], 'draft_ids': [], 'followups': []})]
        else:
            raise ValueError('process_preflight_unexpected_round')
        calls = []
        for name, arguments in planned:
            call_id = f'preflight-{self.rounds}-{name}'
            self.calls.append({'id': call_id, 'name': name, 'arguments': deepcopy(arguments)})
            calls.append({'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}})
        return {'role': 'assistant', 'content': None, 'tool_calls': calls}, {'total_tokens': 0}


class ProcessPreflightReviewer:
    model_id = 'offline-scripted-reviewer-preflight'
    def __init__(self, log_path=None):
        self.records, self.response_metadata = [], []
    def review(self, packet, *args):
        result = {'verdict': 'approve', 'reason': '仅用于离线链路预检，不裁决金融语义。', 'issues': []}
        self.records.append({'packet': deepcopy(safe_data(packet)), 'decision': result})
        return result


class ProcessPreflightAgent(ConversationAgent if _PRODUCTION_IMPORT_ERROR is None else object):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.integration_failures = []
    def process(self, *args, **kwargs):
        try:
            return super().process(*args, **kwargs)
        except Exception as error:
            self.integration_failures.append(_failure(error))
            raise


def configure(g, w, fixture, original_catalog):
    g.knowledge_provider = EvalUnavailableProvider()
    g.public_products = original_catalog
    kind = fixture['type']
    if kind == 'risk_r4':
        with g.bank.transaction() as db:
            db.execute('UPDATE profiles SET risk_level=? WHERE workspace_id=?', (4, w))
    elif kind == 'memory_enabled': g.memories.set_enabled(w, {'enabled': True})
    elif kind == 'legacy_catalog_no_rag': g.public_products = EvalCatalog(fixture)
    elif kind == 'public_policy': g.knowledge_provider = EvalPublicProvider(fixture)
    elif kind == 'imported_january':
        preview = g.statements.preview(w, fixture['csv_document'])
        if preview['valid'] is not True: raise ValueError('invalid_fixture_csv')
        g.statements.commit(w, {'batch_id': preview['batch_id'], 'confirmed': True})
        g.statements.set_source(w, {'source': 'imported'})
    elif kind != 'default': raise ValueError('unknown_fixture_type')
    g.wealth.provision(w)


def snapshot(g, w):
    overview = g.overview(w)
    stores = {'bank': g.bank}
    personal = getattr(g, 'personal_data', None) or getattr(g.memories, 'store', None)
    if personal is not None:
        stores['personal'] = personal
    storage_state = {}
    for name, store in stores.items():
        with closing(store.connect()) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            storage_state[name] = {table: db.execute('SELECT count(*) FROM '+table+' WHERE workspace_id=?', (w,)).fetchone()[0]
                for table in ('user_memories', 'user_memory_relations', 'user_memory_settings', 'profile_observations', 'agent_dialogues', 'agent_widget_history')
                if table in tables}
    observations = storage_state.get('personal', storage_state['bank']).get('profile_observations', 0)
    return {'total_cash_minor': overview['total_cash_minor'], 'profile': overview['profile'],
            'orders': overview['orders'], 'holdings': overview['holdings'], 'fund_holds': overview['fund_holds'],
            'memories': g.memories.state(w)['items'], 'observations': observations, 'storage_state': storage_state}


@contextmanager
def temporary_personal_key():
    """合成私人库使用临时密钥，不查询或更改用户钥匙串。"""
    from cryptography.fernet import Fernet
    prior = os.environ.get('NAVREN_PERSONAL_DATA_KEY')
    os.environ['NAVREN_PERSONAL_DATA_KEY'] = Fernet.generate_key().decode('ascii')
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop('NAVREN_PERSONAL_DATA_KEY', None)
        else:
            os.environ['NAVREN_PERSONAL_DATA_KEY'] = prior


def assert_isolated_storage(g, temporary_root):
    """在任何 session/fixture 前检查后端实际持有的全部数据库路径。"""
    root = Path(temporary_root).resolve()
    paths = {}
    bank = getattr(g, 'bank', None)
    if bank is None:
        # 离线生命周期 mock 无存储；真实 FinanceGateway 一定有 bank。
        return paths
    candidates = {'bank': getattr(bank, 'database', None)}
    personal = getattr(g, 'personal_data', None) or getattr(getattr(g, 'memories', None), 'store', None)
    if personal is not None:
        candidates['personal'] = getattr(personal, 'database', None)
    knowledge = getattr(g, 'knowledge_provider', None)
    if knowledge is not None:
        candidates['knowledge'] = getattr(knowledge, 'path', None)
    market = getattr(g, 'market', None)
    if market is not None:
        candidates['market'] = getattr(market, 'path', None)
    for name, value in candidates.items():
        if value is None or not Path(value).resolve().is_relative_to(root):
            raise ValueError('storage_outside_temporary_root')
        paths[name] = str(Path(value).resolve())
    return paths


def replace_variables(value, variables):
    if isinstance(value, str) and value.startswith('$'): return variables[value[1:]]
    if isinstance(value, list): return [replace_variables(x, variables) for x in value]
    if isinstance(value, dict): return {k: replace_variables(v, variables) for k, v in value.items()}
    return value


def select_path(value, path, selector):
    for part in path.split('.'):
        if isinstance(value, list):
            matches = [x for x in value if all(
                str(x.get('id', '')).endswith(expected) if key == 'id_suffix' else x.get(key) == expected
                for key, expected in selector.items())]
            if len(matches) != 1: raise ValueError('ambiguous_or_missing_oracle_selector')
            value = matches[0]
        value = value[part]
    return value


def oracle(g, w, case):
    """冻结预期独立读取；答案、标准及此工具历史不进入被测 Agent。"""
    tools = BankTools(g, w, 'oracle-'+case['id'])
    accounts = tools.invoke('account_summary', {})['data']['accounts']
    main = next(x['id'] for x in accounts if x['id'].endswith('-main'))
    budget = tools.invoke('budget', {'account_id': main})['data']
    credit = next(x['evidence_id'] for x in budget['commitments'] if x['kind'] == 'credit_card')
    variables = {'main_account_id': main, 'credit_commitment_id': credit, 'workspace_id': w}
    checked = []
    for fact in case['expected']['facts']:
        if 'tool' not in fact: continue
        args = replace_variables(fact.get('arguments', {}), variables)
        output = tools.invoke(fact['tool'], args)
        actual = select_path(output, fact['path'], replace_variables(fact.get('select', {}), variables))
        passed = type(actual) is type(fact['expected']) and actual == fact['expected']
        checked.append({'label': fact['label'], 'tool': fact['tool'], 'arguments': args,
                        'path': fact['path'], 'expected': fact['expected'], 'actual': actual, 'passed': passed})
    if not all(x['passed'] for x in checked):
        error = ValueError('fixture_oracle_mismatch')
        error.checked_facts = checked
        raise error
    return checked


def request(opener, base, path, payload, csrf=None):
    headers = {'Content-Type': 'application/json', 'Origin': base}
    if csrf is not None: headers['X-QZ-CSRF'] = csrf
    req = Request(base+path, data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
    try:
        with opener.open(req, timeout=130) as response: return response.status, json.load(response)
    except HTTPError as error: return error.code, json.load(error)


def select_cases(cases, requested):
    ids = [case['id'] for case in cases]
    if len(ids) != len(set(ids)): raise ValueError('duplicate_dataset_case_id')
    if not requested: return cases
    selected = [value.strip() for value in requested.split(',')]
    if not all(selected) or len(selected) != len(set(selected)): raise ValueError('duplicate_or_empty_case_selection')
    if not set(selected) <= set(ids): raise ValueError('unknown_case')
    return [case for case in cases if case['id'] in selected]


def _failure(error):
    # 不保存任意异常正文，它可能包含请求、密钥或私有内容。
    frames = []
    for frame, line in traceback.walk_tb(error.__traceback__):
        path = Path(frame.f_code.co_filename).resolve()
        name = str(path.relative_to(ROOT.resolve())) if path.is_relative_to(ROOT.resolve()) else path.name
        frames.append({'file': name, 'line': line, 'function': frame.f_code.co_name})
    result = {'code': 'runner_exception', 'exception_type': type(error).__name__, 'frames': frames}
    if isinstance(error, SyntaxError) and error.filename:
        path = Path(error.filename).resolve()
        result['syntax_location'] = {'file': str(path.relative_to(ROOT.resolve())) if path.is_relative_to(ROOT.resolve()) else path.name,
                                     'line': error.lineno, 'offset': error.offset}
    return result


def _empty_row(case):
    return {'id': case['id'], 'question': case['prompt'], 'fixture_type': case['fixture_type'],
            'oracle': [], 'http_status': None, 'result': {}, 'model_tool_calls': [], 'tool_outputs': [],
            'independent_reviews': [], 'before': None, 'after': None,
            'response_metadata': {'primary': [], 'reviewer': []}, 'no_financial_execution': None,
            'no_action_cards': None, 'profile_unchanged': None, 'observation_delta': None,
            'memory_delta': None, 'technical_completed': None}


def _close_server(server, thread):
    if server is not None:
        try:
            if thread is not None: server.shutdown()
        finally:
            server.server_close()
            if thread is not None: thread.join(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=ROOT/'evaluation/navren_skill_semantic_cases.json')
    parser.add_argument('--out-dir', type=Path, required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--case-ids', dest='case_ids', default='')
    selection.add_argument('--cases', dest='case_ids')
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument('--offline', action='store_true')
    execution.add_argument('--process-preflight', action='store_true', help='真实HTTP主循环使用脚本化假模型，不调用云模型、不裁决金融语义')
    parser.add_argument('--browser-hold-seconds', type=int, default=0)
    args = parser.parse_args()
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any((out/name).exists() for name in ('semantic-results.json', 'server-ready.json', 'model-calls.jsonl', 'browser-ready.json', 'browser-session.json')):
        raise ValueError('refuse_overwrite_run')
    dataset_path = args.dataset.resolve()
    log = out/'model-calls.jsonl'
    report = {'started_at': now(), 'tested_source_root': str(ROOT.resolve()),
              'python_version': sys.version, 'interpreter': sys.executable,
              'scope': 'offline_fixture_oracle_preflight' if args.offline else 'real_cloud_models_on_frozen_synthetic_cases_via_current_http_handler',
              'models': {'primary': 'AiPing / DeepSeek-V4-Flash', 'reviewer': 'AiPing / GLM-5.3-Flash'},
              'offline': args.offline, 'source_sha256': {}, 'records': [], 'selected_case_ids': [],
              'semantic_grading': 'pending_independent_annotation', 'complete': False,
              'collection_complete': False, 'run_status': 'collecting',
              'source_freeze_scope': 'recursive production Python; static UI; active skill main/references; synthetic bank and knowledge/local_knowledge execution data; runner and its regression tests; frozen dataset and author dependencies',
              'dataset_source_policy': 'author source is historical provenance; planned candidate differences are recorded; within-run changes invalidate collection'}
    report['fixture_adapter'] = {'version': 'current_public_catalog_compatibility_v1',
        'unavailable_provider': 'explicit empty public catalog, ready=false, no evidence; frozen fixture data and oracle expectations unchanged',
        'public_policy_catalog': 'only identifiers and public subjects from frozen synthetic canonical evidence'}
    if args.process_preflight:
        report['scope'] = 'offline_real_http_process_integration_preflight_with_scripted_fake_models'
        report['models'] = {'primary': ProcessPreflightPrimary.model_id, 'reviewer': ProcessPreflightReviewer.model_id}
        report['process_preflight'] = True
        report['required_completed_tools'] = list(PREFLIGHT_TOOLS)
        report['semantic_grading'] = 'not_evaluated_in_scripted_process_preflight'
    write_json(out/'semantic-results.json', report)
    before_source, dependencies, cases = None, (), []
    phase = 'startup'
    try:
        if _PRODUCTION_IMPORT_ERROR is not None:
            report['stop_reason'] = 'startup_import_failed'
            raise _PRODUCTION_IMPORT_ERROR
        phase = 'dataset'
        dataset_bytes = dataset_path.read_bytes()
        report['dataset_sha256'] = hashlib.sha256(dataset_bytes).hexdigest()
        dataset = json.loads(dataset_bytes, parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)
        report['dataset_id'] = dataset['dataset_id']
        cases = select_cases(dataset['cases'], args.case_ids)
        if not cases: raise ValueError('empty_cases')
        report['selected_case_ids'] = [case['id'] for case in cases]
        report['expected_case_count'] = len(cases)
        phase = 'environment'
        # 构造 LocalKnowledgeProvider 会建库，故必须先拒绝共享路径覆盖。
        if os.environ.get('NAVREN_KNOWLEDGE_DB'):
            raise ValueError('knowledge_database_environment_override')
        if args.process_preflight and args.browser_hold_seconds:
            raise ValueError('process_preflight_browser_hold_forbidden')
        phase = 'source_manifest'
        dependencies = tuple(dataset.get('source_sha256', {}))
        before_source = source_manifest(dataset_path, dependencies)
        report['source_sha256'] = before_source
        report['author_baseline_differences'] = [
            {'path': path, 'author_sha256': expected, 'candidate_sha256': before_source.get(path)}
            for path, expected in sorted(dataset.get('source_sha256', {}).items())
            if before_source.get(path) != expected and not any(part in {'outputs', 'upstream'} for part in Path(path).parts)]
        if ROOT == _IMPORT_ROOT:
            report['import_source_sha256'] = _IMPORT_SOURCE_MANIFEST
            if any(before_source.get(path) != expected for path, expected in _IMPORT_SOURCE_MANIFEST.items()):
                report['stop_reason'] = 'source_changed_during_import'
                raise ValueError('source_changed_during_import')
        if digest(dataset_path) != report['dataset_sha256']:
            report['stop_reason'] = 'dataset_changed_before_start'
            raise ValueError('dataset_changed_before_start')
        consecutive_failures = 0
        for case in cases:
            row = _empty_row(case)
            started = time.monotonic()
            server = thread = primary = reviewer = tested_agent = None
            row_phase = 'create_server'
            with tempfile.TemporaryDirectory(prefix='navren-skill-acceptance-case-') as temp, temporary_personal_key():
                try:
                    server = create_server(port=0, database=str(Path(temp)/'bank.sqlite3'))
                    row_phase = 'storage_isolation'
                    row['database_paths'] = assert_isolated_storage(server.finance, temp)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    base = 'http://127.0.0.1:'+str(server.server_port)
                    write_json(out/'server-ready.json', {'case_id': case['id'], 'url': base,
                               'environment': 'independent_temporary_synthetic_bank_per_case', 'started_at': now()})
                    opener = build_opener(HTTPCookieProcessor(CookieJar()))
                    row_phase = 'session'
                    status, session = request(opener, base, '/api/finance/session', {})
                    if status != 200: raise ValueError('fixture_session_failed')
                    w = session['workspace_id']
                    row['workspace_id'] = w
                    fixture = dataset['fixtures'][case['fixture']]
                    row_phase = 'configure'
                    configure(server.finance, w, fixture, server.finance.public_products)
                    row_phase = 'oracle'
                    row['oracle'] = oracle(server.finance, w, case)
                    row_phase = 'before_snapshot'
                    row['before'] = snapshot(server.finance, w)
                    if not args.offline:
                        primary, reviewer = (ProcessPreflightPrimary(log), ProcessPreflightReviewer(log)) if args.process_preflight else (TracePrimary(log), TraceReviewer(log))
                        tested_agent = (ProcessPreflightAgent if args.process_preflight else ConversationAgent)(primary=primary, reviewer=reviewer)
                        server.conversation_agent = tested_agent
                        row_phase = 'agent_http'
                        row['http_status'], row['result'] = request(opener, base, '/api/agent',
                            {'text': case['prompt'], 'mode': 'classic'}, session['csrf'])
                        row['technical_completed'] = row['http_status'] == 200 and not row['result'].get('isFallback', False)
                        if args.process_preflight:
                            row['integration_failures'] = tested_agent.integration_failures
                            completed = {item['tool'] for item in row['result'].get('agent_runtime', {}).get('trace', []) if item.get('status') == 'completed'}
                            row['completed_preflight_tools'] = sorted(completed)
                            row['process_preflight_completed'] = row['technical_completed'] and set(PREFLIGHT_TOOLS) <= completed
                            if not row['process_preflight_completed']:
                                row['failure_stage'] = 'agent_process'
                                row['runner_failure'] = tested_agent.integration_failures[0] if tested_agent.integration_failures else {'code': 'process_preflight_incomplete', 'exception_type': None}
                                report['stop_reason'] = 'process_preflight_failed'
                    row_phase = 'after_snapshot'
                    row['after'] = snapshot(server.finance, w)
                    before, after = row['before'], row['after']
                    row['no_financial_execution'] = all(before[key] == after[key] for key in ('total_cash_minor', 'orders', 'holdings', 'fund_holds'))
                    row['no_action_cards'] = not row['result'].get('bank_actions')
                    row['profile_unchanged'] = before['profile'] == after['profile']
                    row['observation_delta'] = after['observations']-before['observations']
                    row['memory_delta'] = len(after['memories'])-len(before['memories'])
                except Exception as error:
                    if hasattr(error, 'checked_facts'):
                        row['oracle'] = error.checked_facts
                    row['failure_stage'] = row_phase
                    row['runner_failure'] = _failure(error)
                    row['technical_completed'] = False
                    report['stop_reason'] = 'runner_exception'
                finally:
                    if primary is not None:
                        row['model_tool_calls'], row['tool_outputs'] = primary.calls, primary.tool_outputs
                        row['response_metadata']['primary'] = primary.response_metadata
                    if reviewer is not None:
                        row['independent_reviews'] = reviewer.records
                        row['response_metadata']['reviewer'] = reviewer.response_metadata
                    try:
                        _close_server(server, thread)
                    except Exception as error:
                        row['cleanup_failure'] = _failure(error)
                        row.setdefault('failure_stage', 'server_cleanup')
                        row.setdefault('runner_failure', _failure(error))
                        report['stop_reason'] = 'runner_exception'
                    row['duration_seconds'] = round(time.monotonic()-started, 3)
                    report['records'].append(row)
                    write_json(out/'semantic-results.json', report)
            print(json.dumps({'case': case['id'], 'http': row['http_status'],
                              'technical_completed': row['technical_completed'],
                              'no_financial_execution': row['no_financial_execution'],
                              'runner_failed': bool(row.get('runner_failure'))}, ensure_ascii=False), flush=True)
            if row.get('runner_failure'): break
            failure = row['result'].get('agent_runtime', {}).get('failure', row['result'].get('error', {}).get('code', ''))
            consecutive_failures = consecutive_failures+1 if failure in ('provider_http_error', 'provider_unavailable', 'missing_api_key', 'AGENT_UNAVAILABLE') else 0
            if row['no_financial_execution'] is not True or consecutive_failures >= 2:
                report['stop_reason'] = 'funds_changed' if row['no_financial_execution'] is not True else 'consecutive_provider_failures'
                break
            if source_manifest(dataset_path, dependencies) != before_source:
                report['stop_reason'] = 'source_changed_during_run'
                break
        if args.browser_hold_seconds > 0 and not args.offline and not report.get('stop_reason'):
            # 浏览器验收从新默认库开始，不复用任何题目的风险/记忆/导入 fixture。
            phase = 'browser_hold'
            with tempfile.TemporaryDirectory(prefix='navren-skill-acceptance-browser-') as temp, temporary_personal_key():
                server = thread = browser_primary = browser_reviewer = None
                try:
                    server = create_server(port=0, database=str(Path(temp)/'bank.sqlite3'))
                    browser_paths = assert_isolated_storage(server.finance, temp)
                    browser_primary, browser_reviewer = TracePrimary(log), TraceReviewer(log)
                    server.conversation_agent = ConversationAgent(primary=browser_primary, reviewer=browser_reviewer)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    base = 'http://127.0.0.1:'+str(server.server_port)
                    write_json(out/'browser-ready.json', {'url': base+'/chat.html', 'environment': 'separate_default_temporary_synthetic_browser_bank',
                        'database_paths': browser_paths, 'started_at': now(), 'expires_after_seconds': args.browser_hold_seconds})
                    deadline = time.monotonic()+args.browser_hold_seconds
                    while time.monotonic() < deadline and not (out/'browser-done.json').exists(): time.sleep(1)
                finally:
                    try:
                        _close_server(server, thread)
                    finally:
                        write_json(out/'browser-session.json', {'completed_at': now(),
                            'environment': 'separate_default_temporary_synthetic_browser_bank',
                            'model_tool_calls': browser_primary.calls if browser_primary is not None else [],
                            'tool_outputs': browser_primary.tool_outputs if browser_primary is not None else [],
                            'independent_reviews': browser_reviewer.records if browser_reviewer is not None else [],
                            'response_metadata': {
                                'primary': browser_primary.response_metadata if browser_primary is not None else [],
                                'reviewer': browser_reviewer.response_metadata if browser_reviewer is not None else []}})
    except Exception as error:
        report['failure_stage'] = phase
        report['runner_failure'] = _failure(error)
        report.setdefault('stop_reason', 'runner_exception')
    finally:
        try:
            report['final_source_sha256'] = source_manifest(dataset_path, dependencies)
            report['source_unchanged'] = before_source is not None and before_source == report['final_source_sha256']
            report['source_changes'] = sorted(path for path in set(before_source or {}) | set(report['final_source_sha256'])
                if (before_source or {}).get(path) != report['final_source_sha256'].get(path))
        except Exception as error:
            report['source_unchanged'] = False
            report['source_manifest_failure'] = _failure(error)
        try:
            report['final_dataset_sha256'] = digest(dataset_path)
            report['dataset_unchanged'] = report.get('dataset_sha256') == report['final_dataset_sha256']
        except OSError:
            report['dataset_unchanged'] = False
        report['collection_complete'] = bool(cases) and len(report['records']) == len(cases) and not report.get('runner_failure') and not any(row.get('runner_failure') for row in report['records'])
        report['complete'] = report['collection_complete']
        report['invalidated'] = not report['source_unchanged'] or not report['dataset_unchanged'] or report.get('stop_reason') == 'source_changed_during_import'
        report['run_status'] = 'invalidated' if report['invalidated'] else ('collection_complete' if report['collection_complete'] else 'incomplete')
        report['completed_at'] = now()
        report['summary'] = {'case_count': len(report['records']),
            'technical_completed': sum(row['technical_completed'] is True for row in report['records']),
            'runner_failed': sum(bool(row.get('runner_failure')) for row in report['records']),
            'no_financial_execution': bool(report['records']) and all(row['no_financial_execution'] is True for row in report['records']),
            'oracle_fact_count': sum(len(row['oracle']) for row in report['records'])}
        report['planned_exit_code'] = 0 if report['collection_complete'] and not report['invalidated'] and report['summary']['no_financial_execution'] else 1
        write_json(out/'semantic-results.json', report)
    # 退出码只证明采集完整且来源稳定；模型回退照实收集，金融语义仍待独立标注。
    return report['planned_exit_code']


if __name__ == '__main__': raise SystemExit(main())
