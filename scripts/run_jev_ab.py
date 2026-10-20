#!/usr/bin/env python3
"""冻结源码和题集后，交替执行基线/Jev；保留所有失败和实际用量。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]

def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)

def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as stream:
        stream.write(packed(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def source_files(root):
    excluded = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'outputs', 'logs',
                'upstream', 'archive', 'archives', 'backups', '.pytest_cache'}
    suffixes = {'.py', '.json', '.csv', '.md', '.sql', '.yaml', '.yml', '.html', '.js', '.css', '.png', '.svg'}
    return [p for p in root.rglob('*') if p.is_file() and not p.is_symlink()
            and not excluded.intersection(p.relative_to(root).parts)
            and p.suffix in suffixes and not p.name.startswith('.env')]

def selected_cases(data, case_ids=None):
    """Select original cases by exact ID or stable S01-style short ID."""
    cases = data['cases']
    if case_ids is None:
        return list(cases)
    if isinstance(case_ids, str):
        case_ids = [case_ids]
    requested = [part.strip() for value in case_ids for part in value.split(',') if part.strip()]
    if not requested:
        raise ValueError('case_subset_empty')
    aliases = {case['id']: case['id'] for case in cases}
    for case in cases:
        short = case['id'].split('_', 1)[0]
        if short in aliases and aliases[short] != case['id']:
            raise ValueError('case_short_id_ambiguous:' + short)
        aliases[short] = case['id']
    normalized = []
    for value in requested:
        if value not in aliases:
            raise ValueError('unknown_case_id:' + value)
        normalized.append(aliases[value])
    if len(set(normalized)) != len(normalized):
        raise ValueError('duplicate_case_id')
    selected = set(normalized)
    return [case for case in cases if case['id'] in selected]

def paired_plan(cases, repetitions=2, seed=20261006):
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError('repetitions_must_be_positive_integer')
    rng = random.Random(seed)
    plan, first_order = [], {}
    for repetition in range(repetitions):
        ordered = list(cases)
        rng.shuffle(ordered)
        for index, case in enumerate(ordered):
            if repetition == 0:
                first_order[case['id']] = ['baseline', 'jev'] if index % 2 == 0 else ['jev', 'baseline']
            variants = first_order[case['id']] if repetition % 2 == 0 else list(reversed(first_order[case['id']]))
            for variant in variants:
                plan.append({'case_id': case['id'], 'repetition': repetition + 1, 'variant': variant})
    return plan

def freeze(out, repetitions=2, case_ids=None):
    out = Path(out)
    if out.exists():
        raise ValueError('refuse_overwrite_experiment')
    # Validate before creating a partial experiment directory.
    original = json.loads((ROOT / 'evaluation/window_integration/semantic_cases.json').read_text())
    selected_cases(original, case_ids)
    paired_plan([], repetitions)
    out.mkdir(parents=True)
    files = source_files(ROOT)
    before = {str(p.relative_to(ROOT)): digest(p) for p in files}
    for p in files:
        target = out / 'source' / p.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    after = {str(p.relative_to(ROOT)): digest(p) for p in files}
    copied = {name: digest(out / 'source' / name) for name in before}
    if before != after or before != copied:
        raise ValueError('source_changed_during_freeze')
    data = json.loads((out / 'source/evaluation/window_integration/semantic_cases.json').read_text())
    cases = selected_cases(data, case_ids)
    plan = paired_plan(cases, repetitions)
    protocol = {'version': 2, 'frozen_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'real_cloud_models_on_selected_existing_frozen_synthetic_regression_cases_not_unseen_customers',
        'source_sha256': before, 'dataset_sha256': digest(out / 'source/evaluation/window_integration/semantic_cases.json'),
        'plan': plan, 'random_seed': 20261006,
        'case_ids': [case['id'] for case in cases], 'repetitions': repetitions,
        'original_case_count': len(data['cases']),
        'order_control': 'case order shuffled per repetition; each adjacent two repetitions reverse the AB/BA order for each case',
        'public_evidence': 'frozen synthetic fixture; cold external fetch disabled in both arms; no production RAG claim',
        'intervention': 'explicit Jev preparation before tool-view construction and budget admission; both arms use progressive tool routing; preload at p>=0.75, confidence>=0.60',
        'architecture': {'tool_routing': 'progressive', 'jev_preparation': 'explicit_before_tool_view_and_budget_admission',
            'shared_budget_policy': 'usage-stop-v2', 'failure_denominator': 'all_started_trials'},
        'profiles': {
            'baseline': {'agent_class': 'ConversationAgent', 'tool_routing': 'progressive', 'jev_mode': 'off'},
            'jev': {'agent_class': 'JevConversationAgent', 'tool_routing': 'progressive', 'jev_mode': 'active',
                    'preparation_phase': 'before_tool_view_and_budget_admission'}},
        'limits': {'seconds_per_trial': 105, 'tokens_per_trial_including_jev': 100000,
                   'rounds': 8, 'tools': 12, 'total_tokens': 4000000, 'estimated_cost_upper_cny': 20},
        'models': {'primary': 'DeepSeek-V4-Flash', 'primary_provider': 'ai_ping',
                   'reviewer': 'GLM-5.3-Flash', 'reviewer_provider': 'ai_ping',
                   'judge': 'jev-1.13.0', 'judge_provider': 'typesafe_official',
                   'judge_endpoint': 'https://api.typesafe.ai/v1/systemone'},
        'accuracy': 'frozen independent necessary checks AND post-run full-answer annotation; GLM approve is not truth',
        'latency': 'actual /api/agent HTTP round trip including Jev, main model, review, final guard and publication',
        'cost': {'method': 'actual provider usage multiplied by frozen price ranges; not an invoice',
            'aiping_primary_cny_per_million': {'input': [1, 1], 'output': [2, 2]},
            'glm_cny_per_million': {'input': [0.8, 0.8], 'output': [1.4, 2.8]},
            'jev_usd_per_million': {'input': 0.042, 'output': 0},
            'comparison_fx_cny_per_usd': 7.2,
            'fx_status': 'fixed_comparison_assumption_not_market_quote',
            'sources': ['https://aiping.cn/api/v1/models',
                        'https://typesafe.ai/blog/introducing-system-one-models-and-jev']},
        'success_rule': 'all failures in denominator; factual/constraint correctness separately from technical completion',
        'enable_rule': 'no critical safety regression; quality not worse; both end-to-end time and cost improve; small sample only',
        'source_unchanged_during_freeze': True}
    write(out / 'protocol.json', protocol)
    print(packed({'status': 'frozen', 'source_files': len(before), 'trials': len(plan), 'out': str(out)}))

class Recorder:
    def __init__(self, out):
        self.out, self.calls = out, []

    def opener(self, stage):
        def opened(request, **kwargs):
            started = time.monotonic()
            record = {'stage': stage, 'url': request.full_url, 'request_sent': True,
                'request_sha256': hashlib.sha256(request.data).hexdigest(), 'input_bytes': len(request.data),
                'started_at': datetime.now(timezone.utc).isoformat(), 'usage': None, 'outcome': 'pending'}
            index = len(self.calls)
            self.calls.append(record)
            write(self.out / 'requests' / f'{index:03}.json', json.loads(request.data))
            # Persist dispatch intent before waiting on the provider. If the
            # collector is interrupted after dispatch, billing must remain
            # visibly unknown instead of disappearing from the ledger.
            write(self.out / 'provider-calls.json', self.calls)
            try:
                response = urllib.request.urlopen(request, **kwargs)
            except Exception as error:
                record.update(outcome='transport_error', error_type=type(error).__name__,
                              http_status=getattr(error, 'code', None), duration_ms=round((time.monotonic()-started)*1000))
                write(self.out / 'provider-calls.json', self.calls)
                raise
            class Response:
                def __enter__(inner): response.__enter__(); return inner
                def __exit__(inner, *args): return response.__exit__(*args)
                def __getattr__(inner, name): return getattr(response, name)
                def read(inner, *args):
                    raw = response.read(*args)
                    record['duration_ms'] = round((time.monotonic()-started)*1000)
                    try:
                        envelope = json.loads(raw)
                        if not isinstance(envelope, dict):
                            raise ValueError('invalid_envelope')
                        record.update(model_actual=envelope.get('model'),
                                      provider_actual=envelope.get('provider'),
                                      usage=envelope.get('usage'), outcome='response')
                        # 不保存隐藏思考；只保留实际模型、计量和可见输出/工具参数。
                        visible = deepcopy(envelope)
                        for choice in visible.get('choices', []):
                            choice.get('message', {}).pop('reasoning_content', None)
                        write(self.out / 'responses' / f'{index:03}.json', visible)
                    except ValueError:
                        record['outcome'] = 'invalid_json'
                    write(self.out / 'provider-calls.json', self.calls)
                    return raw
            return Response()
        return opened

class FrozenEvidenceRuntime:
    """旧合成夹具的只读接口适配；绝不更改黄金事实或发起冷抓取。"""
    def __init__(self, provider):
        from local_knowledge.runtime import RoundProvider
        class Adapter:
            def __getattr__(self, name): return getattr(provider, name)
            def public_catalog(self, *, limit=5000):
                catalog = provider.public_catalog()
                return {k: catalog[k][:limit] for k in ('terms', 'identifiers')}
        self.provider = RoundProvider(Adapter(), records=(),
            release_id=provider.status().get('public_release_id'))
    def prepare_round(self, text):
        from local_knowledge.runtime import PreparedEvidence
        return PreparedEvidence(self.provider, {'mode': 'jev_ab_frozen_synthetic',
            'cold_fetch_enabled': False, 'semantic_validation': False})
    def close(self): pass

def cost(calls, protocol):
    policy = protocol['cost']
    lo, hi, jev_usd, total_tokens, known = 0.0, 0.0, 0.0, 0, True
    for call in calls:
        usage = call.get('usage')
        if not isinstance(usage, dict):
            known = False
            continue
        if call['stage'] == 'jev':
            i, o = usage.get('input_tokens'), usage.get('output_tokens')
            if type(i) is not int or type(o) is not int or i < 0 or o < 0:
                known = False; continue
            # Historical frozen protocols may contain separately billed Jev.
            # New AiPing-only protocols cannot price or dispatch such a call.
            if protocol.get('models', {}).get('judge_provider') == 'unavailable_via_aiping':
                known = False
                continue
            total_tokens += i + o
            legacy_rate = policy.get('jev_usd_per_million', {}).get('input')
            if legacy_rate is None:
                known = False
            else:
                jev_usd += i * legacy_rate / 1000000
        else:
            i, o = usage.get('prompt_tokens'), usage.get('completion_tokens')
            if type(i) is not int or type(o) is not int or i < 0 or o < 0:
                known = False; continue
            total_tokens += i + o
            if call['stage'] == 'primary':
                rates = policy.get('aiping_primary_cny_per_million')
                if rates is not None:
                    lo += (i*rates['input'][0] + o*rates['output'][0])/1000000
                    hi += (i*rates['input'][1] + o*rates['output'][1])/1000000
                else:
                    # Historical frozen protocols retain the old direct-DeepSeek
                    # price calculation for read-only report reproduction.
                    rates = policy['deepseek_cny_per_million']
                    details = usage.get('prompt_tokens_details')
                    hit = usage.get('prompt_cache_hit_tokens', details.get('cached_tokens') if isinstance(details, dict) else None)
                    if type(hit) is not int or not 0 <= hit <= i:
                        lo += i * rates['cached_input'][0] / 1000000
                        hi += i * rates['uncached_input'][1] / 1000000
                    else:
                        miss = i - hit
                        lo += (hit*rates['cached_input'][0] + miss*rates['uncached_input'][0])/1000000
                        hi += (hit*rates['cached_input'][1] + miss*rates['uncached_input'][1])/1000000
                    lo += o*rates['output'][0]/1000000
                    hi += o*rates['output'][1]/1000000
            else:
                rates = policy['glm_cny_per_million']
                lo += (i*rates['input'][0] + o*rates['output'][0])/1000000
                hi += (i*rates['input'][1] + o*rates['output'][1])/1000000
    converted = jev_usd * policy.get('comparison_fx_cny_per_usd', 0)
    return {'known': known, 'tokens': total_tokens, 'main_cny_range': [lo, hi], 'jev_usd': jev_usd,
            'comparison_total_cny_range': [lo+converted, hi+converted] if known else None}

def enforce_provider_routes(protocol):
    models = protocol.get('models', {})
    if models.get('primary_provider') != 'ai_ping' or models.get('reviewer_provider') != 'ai_ping':
        raise ValueError('experiment_requires_aiping_primary_and_reviewer')
    has_jev = any(item.get('variant') == 'jev' for item in protocol.get('plan', []))
    if has_jev and models.get('judge_provider') != 'typesafe_official':
        raise ValueError('experiment_requires_official_typesafe_jev')
    if has_jev and models.get('judge_endpoint') != 'https://api.typesafe.ai/v1/systemone':
        raise ValueError('experiment_requires_official_typesafe_endpoint')

def shared_agent_options(protocol):
    """Keep all main-agent controls identical across the two trial arms."""
    limits = protocol['limits']
    options = {'timeout_seconds': limits['seconds_per_trial'],
        'max_tokens': limits['tokens_per_trial_including_jev'],
        'max_rounds': limits['rounds'], 'max_tools': limits['tools']}
    if protocol.get('version', 1) >= 2:
        architecture = protocol.get('architecture', {})
        profiles = protocol.get('profiles', {})
        if architecture.get('tool_routing') != 'progressive' or any(
                profiles.get(variant, {}).get('tool_routing') != 'progressive'
                for variant in ('baseline', 'jev')):
            raise ValueError('v2_requires_identical_progressive_tool_routing')
        options['tool_routing'] = 'progressive'
    return options

def instantiate_trial_agent(primary, reviewer, protocol, variant, *, judge,
                            baseline_class, jev_class):
    options = shared_agent_options(protocol)
    if variant == 'baseline':
        return baseline_class(primary=primary, reviewer=reviewer, **options)
    if variant == 'jev':
        return jev_class(primary=primary, reviewer=reviewer, jev_mode='active', judge=judge, **options)
    raise ValueError('unknown_trial_variant:' + variant)

def execution_observation(row, request_tool_views=()):
    """Capture auditable actual paths; intended architecture is stored separately."""
    runtime = row.get('result', {}).get('agent_runtime', {})
    calls = row.get('provider_calls', [])
    first_admission = next((item for item in runtime.get('budget_admissions', [])
        if item.get('stage') != 'reviewer'), None)
    first_primary = next((item for item in calls if item['stage'] == 'primary'), None)
    first_jev = next((item for item in calls if item['stage'] == 'jev'), None)
    before_primary = None
    if first_primary is not None and first_jev is not None:
        before_primary = datetime.fromisoformat(first_jev['started_at']) < datetime.fromisoformat(first_primary['started_at'])
    stages = {stage: {'requests_sent': sum(item['stage'] == stage for item in calls),
        'provider_duration_sum_ms': sum(item.get('duration_ms', 0) for item in calls if item['stage'] == stage),
        'usage_unknown_calls': sum(item['stage'] == stage and not isinstance(item.get('usage'), dict) for item in calls)}
        for stage in ('jev', 'primary', 'reviewer')}
    return {'first_primary_admission_stage': first_admission.get('stage') if first_admission else None,
        'first_primary_tool_view': deepcopy(first_admission.get('tool_view')) if first_admission else None,
        'runtime_tool_routing': deepcopy(runtime.get('tool_routing')),
        'first_primary_requested_tools': deepcopy(request_tool_views[0]) if request_tool_views else None,
        'primary_request_tool_views': deepcopy(list(request_tool_views)),
        'jev_preparation': {'provider_called_before_first_primary': before_primary,
            'runtime_receipt': deepcopy(runtime.get('jev'))},
        'stages': stages, 'runtime_failure': runtime.get('failure'),
        'technical_completed': row.get('technical_completed', False)}

def collect(out, offline=False):
    protocol = json.loads((out / 'protocol.json').read_text())
    if not offline:
        enforce_provider_routes(protocol)
        shared_agent_options(protocol)
    source = out / 'source'
    for name, expected in protocol['source_sha256'].items():
        if digest(source/name) != expected:
            raise ValueError('frozen_source_modified:' + name)
    if (out/'results.json').exists():
        raise ValueError('refuse_repeat_paid_collection')
    sys.path.insert(0, str(source))
    oracle = load(source/'tests/window_integration/semantic_oracle.py', 'jev_independent_oracle')
    data = oracle.load_dataset()
    from agent_runtime.conversation import ConversationAgent, GLMConversationReviewer
    from agent_runtime.jev_agent import JevConversationAgent
    from agent_runtime.jev_client import JevClient
    from agent_runtime.model_adapters import OpenAICompatibleJsonClient, AIPING_URL, GLM_REVIEW_MODEL
    from agent_runtime.native_client import AIPingToolClient
    from server import create_server
    class Primary(AIPingToolClient):
        def __init__(self, recorder):
            super().__init__(urlopen=recorder.opener('primary'), log_path=str(recorder.out/'native-calls.jsonl'))
            self.calls, self.tool_outputs, self.seen = [], [], set()
            self.request_tool_views = []
        def complete(self, messages, tools, **kwargs):
            for index, m in enumerate(messages):
                marker = (index, m.get('tool_call_id'))
                if m.get('role') == 'tool' and marker not in self.seen:
                    self.seen.add(marker)
                    self.tool_outputs.append({'call_id': m.get('tool_call_id'), 'output': json.loads(m['content'])})
            self.request_tool_views.append({'tool_count': len(tools),
                'tool_names': [tool.get('function', {}).get('name') for tool in tools]})
            message, usage = super().complete(messages, tools, **kwargs)
            for c in message.get('tool_calls') or []:
                self.calls.append({'id': c['id'], 'name': c['function']['name'],
                                   'arguments': json.loads(c['function']['arguments'])})
            return message, usage
    report = {'protocol_sha256': digest(out/'protocol.json'), 'complete': False,
              'scope': protocol['scope'], 'records': [], 'offline': offline,
              'architecture': deepcopy(protocol.get('architecture')),
              'profiles': deepcopy(protocol.get('profiles')), 'planned_trials': len(protocol['plan'])}
    if offline:
        report['scope'] = 'fixture_preflight_only_no_model_quality_result'
        for case in selected_cases(data, protocol.get('case_ids')):
            with tempfile.TemporaryDirectory(prefix='navren-jev-preflight-') as tmp:
                g,w,_ = oracle.build_fixture(case, tmp)
                pre = oracle.preflight(g,w,case)
                report['records'].append({'id': case['id'], 'preflight': pre})
                if pre['status'] != 'passed':
                    write(out/'preflight.json', report)
                    raise ValueError('fixture_failed:' + case['id'])
        report['complete'] = True
        write(out/'preflight.json', report)
        return
    if not json.loads((out/'preflight.json').read_text()).get('complete'):
        raise ValueError('preflight_required')
    write(out/'results.json', report)
    grand_upper, grand_tokens = 0.0, 0
    for index, planned in enumerate(protocol['plan']):
        case = next(c for c in data['cases'] if c['id'] == planned['case_id'])
        trial_out = out / 'trials' / f'{index+1:02}-{planned["variant"]}-{case["id"]}'
        recorder = Recorder(trial_out)
        primary = Primary(recorder)
        review_client = OpenAICompatibleJsonClient(provider='ai_ping', model_id=GLM_REVIEW_MODEL,
            url=AIPING_URL, env_name='AIPING_API_KEY', keychain_service='CC/AIPING_GLM_API_KEY',
            timeout=35, max_retries=0, urlopen=recorder.opener('reviewer'), log_path=str(trial_out/'native-calls.jsonl'))
        reviewer = GLMConversationReviewer(client=review_client)
        judge = JevClient(urlopen=recorder.opener('jev')) if planned['variant'] == 'jev' else None
        agent = instantiate_trial_agent(primary, reviewer, protocol, planned['variant'], judge=judge,
            baseline_class=ConversationAgent, jev_class=JevConversationAgent)
        row = {**planned, 'id': case['id'], 'question': case['prompt'], 'trial_index': index+1,
            'execution_profile': {**deepcopy(protocol.get('profiles', {}).get(planned['variant'], {})),
                'shared_agent_options': shared_agent_options(protocol)}}
        server, thread = None, None
        try:
            with tempfile.TemporaryDirectory(prefix='navren-jev-ab-') as tmp:
                g,w,token = oracle.build_fixture(case, tmp)
                row['preflight'] = oracle.preflight(g,w,case)
                if row['preflight']['status'] != 'passed':
                    raise ValueError('fixture_drift')
                row['before'] = oracle.snapshot(g,w)
                # Bootstrap only constructs the existing HTTP handler. Requests use the exact oracle gateway.
                server = create_server(port=0, database=str(Path(tmp)/'bootstrap.sqlite3'), conversation_agent=agent)
                server.evidence_runtime.close()
                server.finance, server.market = g, g.market
                server.evidence_runtime = FrozenEvidenceRuntime(g.knowledge_provider)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = 'http://127.0.0.1:' + str(server.server_port)
                request = urllib.request.Request(base+'/api/agent', data=json.dumps(
                    {'text': case['prompt'], 'mode': 'classic'}, ensure_ascii=False).encode(), headers={
                    'Content-Type': 'application/json', 'Origin': base,
                    'Cookie': 'qz_bank_user='+token, 'X-QZ-CSRF': g.csrf(token)})
                started = time.monotonic()
                try:
                    with urllib.request.urlopen(request, timeout=130) as response:
                        row['http_status'], row['result'] = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    row['http_status'], row['result'] = error.code, json.load(error)
                except KeyboardInterrupt:
                    row['collector_interruption'] = {
                        'stage': 'waiting_for_local_agent_http',
                        'usage_may_be_unknown': True,
                    }
                    row['technical_completed'] = False
                row['elapsed_ms'] = round((time.monotonic()-started)*1000)
                if not row.get('collector_interruption'):
                    row['technical_completed'] = row['http_status'] == 200 and not row['result'].get('isFallback', False)
                    row['after'] = oracle.snapshot(g,w)
        except Exception as error:
            row['runner_failure'] = {'type': type(error).__name__, 'code': str(error)[:160]}
            row['technical_completed'] = False
        finally:
            if server:
                if thread: server.shutdown()
                server.server_close()
                if thread: thread.join(timeout=3)
        row['model_tool_calls'], row['tool_outputs'] = primary.calls, primary.tool_outputs
        row['provider_calls'] = recorder.calls
        row['execution_observation'] = execution_observation(row, primary.request_tool_views)
        row['cost'] = cost(recorder.calls, protocol)
        row['independent_checks'] = oracle.classify(row, case)
        grand_tokens += row['cost']['tokens']
        if row['cost']['known']:
            grand_upper += row['cost']['comparison_total_cny_range'][1]
        report['records'].append(row)
        report['tokens'], report['estimated_cost_upper_cny'] = grand_tokens, grand_upper
        write(trial_out/'record.json', row)
        write(out/'results.json', report)
        print(json.dumps({'trial': index+1, 'variant': planned['variant'], 'case': case['id'],
            'elapsed_ms': row.get('elapsed_ms'), 'status': row['independent_checks']['status'],
            'primary_calls': sum(c['stage']=='primary' for c in recorder.calls),
            'jev': row.get('result',{}).get('agent_runtime',{}).get('jev',{}).get('selection'),
            'runner_failure': row.get('runner_failure')}, ensure_ascii=False), flush=True)
        if row.get('collector_interruption'):
            report['stop_reason'] = 'collector_interrupted_usage_unknown'
            write(out/'results.json', report)
            break
        if row.get('runner_failure') or not row['cost']['known']:
            report['stop_reason'] = 'runner_or_billing_unverified'
            break
        if grand_upper >= protocol['limits']['estimated_cost_upper_cny'] or grand_tokens >= protocol['limits']['total_tokens']:
            report['stop_reason'] = 'global_budget'
            break
        if any(digest(source/name) != expected for name,expected in protocol['source_sha256'].items()):
            report['stop_reason'] = 'source_drift'
            break
    report['complete'] = len(report['records']) == len(protocol['plan'])
    report['recorded_trials'] = len(report['records'])
    report['accounting_complete'] = all(row['cost']['known'] for row in report['records'])
    report['source_unchanged_after_run'] = all(digest(source/name)==expected for name,expected in protocol['source_sha256'].items())
    write(out/'results.json', report)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['freeze', 'preflight', 'run'])
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--repetitions', type=int, default=2, help='每题每组重复次数；仅用于新 freeze')
    p.add_argument('--case-ids', nargs='+', help='新 freeze 的原题 ID，例如 S01 S04 S06 S08；也支持逗号分隔')
    args = p.parse_args()
    if args.action != 'freeze' and (args.case_ids is not None or args.repetitions != 2):
        p.error('--case-ids 和 --repetitions 仅用于新 freeze；preflight/run 必须遵循已冻结协议')
    out = args.out.resolve()
    if args.action == 'freeze': freeze(out, repetitions=args.repetitions, case_ids=args.case_ids)
    else: collect(out, offline=args.action=='preflight')

if __name__ == '__main__': main()
