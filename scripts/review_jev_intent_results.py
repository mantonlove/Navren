#!/usr/bin/env python3
"""离线独立判卷旁路：遮蔽显式组别，保存原记录，逐条绑定原金标。

只读冻结实验和原答案；不加载密钥，不调用模型，不修改 campaign/record/gold。
轨迹可透露架构，因此本工具不声称双盲，也不作 Jev 自动采用决定。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
from unittest.mock import patch
import urllib.request
import uuid

VERSION = 'navren-independent-result-review/1'
ROOT = Path(__file__).resolve().parents[1]


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def digest(path):
    return sha(Path(path).read_bytes())


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key:' + key)
        result[key] = value
    return result


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite_json:' + value)))


def write_new(path, value, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o644)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def inside(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('path_outside_frozen_directory')
    path = root / relative
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root):
        raise ValueError('symlink_or_path_escape')
    return path


@contextmanager
def offline_guard():
    def blocked(*args, **kwargs):
        raise RuntimeError('offline_sidecar_external_execution_forbidden')
    with patch.object(socket.socket, 'connect', blocked), patch.object(socket.socket, 'connect_ex', blocked), \
            patch.object(urllib.request, 'urlopen', blocked), patch.object(urllib.request.OpenerDirector, 'open', blocked), \
            patch.object(subprocess, 'run', blocked), patch.object(subprocess, 'Popen', blocked), \
            patch.object(os, 'system', blocked), patch.object(os, 'popen', blocked):
        yield


def load(path, prefix):
    name = prefix + '_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def frozen_input(out, expected):
    """先核字节身份，再导入已冻结的只读必要条件检查器。"""
    out = Path(out).resolve()
    protocol = read(out / 'protocol.json')
    if not expected or digest(out / 'protocol.json') != expected:
        raise ValueError('external_protocol_sha_required_or_mismatch')
    if read(out / 'freeze-manifest.json').get('protocol_sha256') != expected:
        raise ValueError('freeze_manifest_protocol_mismatch')
    for key, filename in [('case_sha256', 'cases.json'), ('split_sha256', 'split.json')]:
        if digest(out / filename) != protocol[key]:
            raise ValueError('frozen_input_changed:' + filename)
    for filename, pin in protocol['source_sha256'].items():
        if digest(inside(out / 'source', filename)) != pin:
            raise ValueError('frozen_source_changed:' + filename)
    if protocol.get('pricing_snapshot_sha256') and digest(out / 'pricing.json') != protocol['pricing_snapshot_sha256']:
        raise ValueError('frozen_pricing_changed')
    own_path = 'scripts/review_jev_intent_results.py'
    if protocol['source_sha256'].get(own_path) != digest(__file__):
        raise ValueError('sidecar_must_be_in_reviewed_frozen_source')
    with offline_guard():
        harness = load(inside(out / 'source', 'scripts/run_jev_intent_ab.py'), 'independent_harness')
        harness.verify(out, expected)
    cases = read(out / 'cases.json')['cases']
    if len({case['id'] for case in cases}) != len(cases):
        raise ValueError('duplicate_case_identity')
    report = read(out / 'results.json')
    if report.get('protocol_sha256') != expected or not isinstance(report.get('records'), list):
        raise ValueError('results_protocol_or_records_invalid')
    if len(report['records']) > len(protocol['plan']):
        raise ValueError('more_starts_than_frozen_plan')
    # run先落盘start+campaign，再追加results。崩溃或截断report绝不能把
    # 已启动任务静默变成未启动；不伪造丢失答案，须保留失败现场再由owner修复。
    physical = out / 'trials'
    if physical.exists():
        for trial in physical.iterdir():
            if trial.is_symlink() or not trial.is_dir():
                raise ValueError('invalid_actual_trial_inventory')
            observed = any((trial / name).is_file() for name in ('start.json', 'record.json', 'provider-calls.json',
                'process.json', 'exit.json', 'stdout.log', 'stderr.log', 'review-packet.json'))
            if not observed:
                continue
            try:
                index = int(trial.name.split('-', 1)[0]) - 1
            except ValueError:
                raise ValueError('actual_started_inventory_name_invalid') from None
            if not 0 <= index < len(report['records']):
                raise ValueError('actual_started_inventory_missing_from_results')
            planned = protocol['plan'][index]
            expected_name = f'{index + 1:03}-{planned["variant"]}-{planned["case_id"]}'
            if trial.name != expected_name:
                raise ValueError('actual_started_inventory_plan_mismatch')
    campaign_binding = None
    if report.get('campaign_path'):
        path = Path(report['campaign_path'])
        if not path.is_absolute() or path.resolve() != path or path.suffix != '.json':
            raise ValueError('campaign_receipt_path_invalid')
        ledger = read(path)
        attempt = report.get('configuration_attempt')
        if type(attempt) is not int or not isinstance(ledger.get('starts'), list):
            raise ValueError('campaign_attempt_identity_required')
        matched = [start for start in ledger['starts'] if isinstance(start, dict)
                   and start.get('protocol_sha256') == expected and start.get('configuration_attempt') == attempt]
        if (len(matched) != len(report['records']) or sorted(start.get('index', -1) for start in matched) != list(range(len(report['records'])))):
            raise ValueError('campaign_started_inventory_missing_from_results')
        for start in matched:
            index = start['index']; planned = protocol['plan'][index]
            if type(index) is not int or any(start.get(key) != value for key, value in planned.items()):
                raise ValueError('campaign_start_plan_mismatch')
        campaign_binding = {'path': str(path), 'matching_attempt_starts_sha256': sha(packed(sorted(matched, key=lambda start: start['index'])).encode()),
                            'matching_starts': len(matched), 'configuration_attempt': attempt,
                            'scope': 'only this protocol/attempt; other campaign attempts are not discarded or rewritten'}
    records = []
    for index, row in enumerate(report['records']):
        planned = protocol['plan'][index]
        if any(row.get(key) != value for key, value in planned.items()):
            raise ValueError('started_record_plan_identity_mismatch')
        if row.get('protocol_sha256') not in (None, expected):
            raise ValueError('record_protocol_mismatch')
        path = f'trials/{index + 1:03}-{planned["variant"]}-{planned["case_id"]}/record.json'
        raw_path = inside(out, path)
        raw = read(raw_path) if raw_path.is_file() else None
        artifacts = {}
        trial = Path(path).parent
        for filename in ('start.json', 'provider-calls.json', 'exit.json'):
            candidate = inside(out, trial / filename)
            artifacts[str(trial / filename)] = digest(candidate) if candidate.is_file() else None
        provider_file = inside(out, trial / 'provider-calls.json')
        if provider_file.is_file() and read(provider_file) != row.get('provider_calls', []):
            raise ValueError('native_provider_ledger_differs_from_result')
        start_file = inside(out, trial / 'start.json')
        if start_file.is_file():
            start = read(start_file)
            if start.get('protocol_sha256') != expected or start.get('index') != index or any(start.get(k) != v for k, v in planned.items()):
                raise ValueError('start_receipt_plan_identity_mismatch')
        exit_file = inside(out, trial / 'exit.json')
        if exit_file.is_file() and row.get('exit') is not None and read(exit_file) != row['exit']:
            raise ValueError('supervisor_exit_receipt_differs_from_result')
        request_inputs = []; requests_complete = True
        calls = row.get('provider_calls', [])
        request_dir = inside(out, trial / 'requests')
        if request_dir.exists():
            for candidate in request_dir.iterdir():
                if candidate.is_symlink() or not candidate.is_file() or not candidate.name.endswith('.json'):
                    raise ValueError('request_body_inventory_invalid')
                try:
                    wire_index = int(candidate.stem)
                except ValueError:
                    raise ValueError('request_body_inventory_index_invalid') from None
                if not 0 <= wire_index < len(calls) or candidate.name != f'{wire_index:03}.json':
                    raise ValueError('request_body_without_matching_native_call')
        for wire_index, call in enumerate(calls):
            relative = str(trial / 'requests' / f'{wire_index:03}.json')
            body_file = inside(out, relative)
            present = body_file.is_file()
            wire_sha = call.get('request_sha256')
            stored_sha = call.get('stored_request_json_sha256')
            actual_stored_sha = digest(body_file) if present else None
            if stored_sha is not None and present and stored_sha != actual_stored_sha:
                raise ValueError('stored_request_body_differs_from_native_ledger')
            shape = (type(call.get('index')) is int and call['index'] == wire_index
                     and type(call.get('request_sent')) is bool
                     and isinstance(wire_sha, str) and len(wire_sha) == 64
                     and all(char in '0123456789abcdef' for char in wire_sha)
                     and isinstance(stored_sha, str) and len(stored_sha) == 64
                     and all(char in '0123456789abcdef' for char in stored_sha)
                     and stored_sha == actual_stored_sha)
            if type(call.get('request_sent')) is not bool or (call.get('request_sent') and (not present or not shape)):
                requests_complete = False
            body = read(body_file) if present else None
            if body is not None and (not isinstance(body, dict) or any(key.casefold() in
                    ('authorization', 'headers', 'api_key', 'apikey') for key in body)):
                raise ValueError('request_body_contains_headers_or_credential_fields')
            request_inputs.append({'wire_index': wire_index, 'path': relative, 'stored_json_sha256': actual_stored_sha,
                'native_stored_json_sha256': stored_sha,
                'native_actual_wire_sha256': wire_sha, 'native_index_verified': bool(shape), 'request_sent': call.get('request_sent') is True,
                'body': body, 'body_missing': not present})
        records.append({'index': index, 'row': row, 'path': path if raw is not None else None,
                        'raw': raw, 'record_sha256': digest(raw_path) if raw is not None else None,
                        'results_row_sha256': sha(packed(row).encode()), 'artifact_sha256': artifacts,
                        'request_inputs': request_inputs, 'request_inputs_complete': requests_complete})
    return out, protocol, cases, report, records, harness, campaign_binding


def criteria_for(case, context):
    """保留原文/结构；没有必要关键词、路由标签或业务 GLM 判卷。"""
    result = []
    metadata = {'id', 'case_id', 'question', 'prompt', 'input_text', 'title', 'origin', 'source', 'kind',
                'fixture', 'fixture_type', 'fixture_case', 'fixture_configuration', 'skills_in_scope', 'severity',
                'gold_status', 'expected_skill', 'expected_skill_id', 'expected_route', 'route', 'mode', 'original'}
    # original 保存完整来源、旧标签和夹具代码，仍全文出现在公开包。现代冻结
    # 金标的 must_convey/must_not_convey 已逐项复制其业务标准；源码字符串不是
    # 每一个都必须出现在用户答复中的结论，不能将来源元数据偷换成判卷标准。

    def add(pointer, value, kind='required'):
        text = value if isinstance(value, str) else packed(value)
        item = {'pointer': pointer, 'kind': kind, 'criterion': text}
        item['criterion_sha256'] = sha(packed(item).encode())
        item['id'] = '/'.join(str(part) for part in pointer)
        result.append(item)

    def walk(value, pointer):
        if isinstance(value, dict):
            for key, child in value.items():
                if key not in metadata:
                    walk(child, pointer + [key])
        elif isinstance(value, list):
            for index, child in enumerate(value):
                # 一项结构化原事实作为完整标准，不能拆掉 tool/参数/数值/单位。
                if pointer[-1] == 'facts':
                    add(pointer + [index], child, 'fact_review')
                elif isinstance(child, (dict, list)):
                    walk(child, pointer + [index])
                else:
                    add(pointer + [index], child,
                        'forbidden' if pointer[-1] in ('forbidden_conclusions', 'must_not_convey', 'forbidden_behaviors')
                        else 'conditional_evidence' if pointer[-1] == 'minimum_evidence' else 'required')
        else:
            add(pointer, value)
    walk(case['gold'], ['gold'])
    if case.get('oracle') == 'original_semantic':
        for key, value in context.get('common_expected', {}).items():
            add(['shared_context', 'common_expected', key], value)
    if not result:
        raise ValueError('original_semantic_criteria_missing:' + case['id'])
    if len({item['id'] for item in result}) != len(result):
        raise ValueError('duplicate_criterion_pointer')
    return result


def blind_record(record):
    """只遮蔽基础设施身份/数字，财务契约、签名、真实工具和失败留存。"""
    result = deepcopy(record)
    root_mask = {'variant', 'trial', 'trial_index', 'index', 'configuration', 'configuration_attempt',
                 'protocol_sha256', 'same_configuration_repair_of', 'source', 'started_at', 'cost',
                 'http_elapsed_ms', 'worker_wall_ms', 'api_latency_ms', 'penalized_api_latency_ms',
                 'latency_scope', 'provider_calls', 'request_tool_views', 'reservation'}
    for key in root_mask:
        if key in result:
            result[key] = '[身份或性能字段已遮蔽；原文保存在私有映射所绑定的原记录]'
    hidden = {'provider', 'route_provider', 'model', 'model_id', 'model_actual', 'model_requested',
              'url', 'endpoint', 'env_name', 'keychain_service', 'duration_ms', 'elapsed_ms', 'remaining_ms',
              'usage', 'budget_tokens', 'total_tokens', 'prompt_tokens', 'completion_tokens', 'input_tokens',
              'output_tokens', 'cost', 'estimated_upper_cny', 'estimated_upper_cny_lower_bound', 'budget_source',
              'primary_model', 'reviewer_model', 'primary_tokens', 'reviewer_tokens', 'conversation_total_tokens',
              'known_tokens_lower_bound', 'primary_tokens_lower_bound', 'reviewer_tokens_lower_bound',
              'token_budget_reserved', 'input_estimate', 'output_reserve', 'input_bytes', 'remaining_tokens',
              'primary_tokens_known', 'reviewer_tokens_known', 'input_tokens_estimate', 'output_tokens_reserve'}
    business_children = {'arguments', 'output', 'raw_output', 'data', 'trace',
                         'money_signature', 'money_signatures', 'money_contract', 'financial_contract', 'snapshot', 'source_scope'}

    def infrastructure(value):
        if isinstance(value, dict):
            return {key: deepcopy(child) if key in business_children else
                    '[身份或性能字段已遮蔽]' if key in hidden or (key == 'stage' and child in ('native_route', 'jev_route')) else infrastructure(child)
                    for key, child in value.items()}
        if isinstance(value, list):
            return [infrastructure(child) for child in value]
        return value
    final = result.get('result')
    if isinstance(final, dict):
        for key in ('agent_runtime', 'agent_review'):
            if key in final:
                final[key] = infrastructure(final[key])
    if 'actual_events' in result:
        result['actual_events'] = infrastructure(result['actual_events'])
    if isinstance(result.get('exit'), dict):
        result['exit'] = infrastructure(result['exit'])
        for key in ('pid', 'process_group_id', 'command', 'cwd', 'started_at', 'ended_at'):
            if key in result['exit']:
                result['exit'][key] = '[进程身份已遮蔽]'
    # 可见答复/工具名称/原错误正文不替换字符串；模型自述身份可能透露组别。
    return result


def blind_model_inputs(values):
    """正文与server_context全文保留；HTTP认证不在Recorder正文，隐藏推理不导出。"""
    result = []
    for item in values:
        body = deepcopy(item['body']); removed = 0
        if isinstance(body, dict):
            for key in ('model', 'model_id', 'provider', 'route_provider', 'endpoint', 'url'):
                if key in body:
                    body[key] = '[基础设施身份已遮蔽]'
            messages = body.get('messages', [])
            if isinstance(messages, list):
                for message in messages:
                    if isinstance(message, dict):
                        for key in ('reasoning_content', 'reasoning_details'):
                            if key in message:
                                message.pop(key); removed += 1
        result.append({'wire_order': item['wire_index'], 'request_sent': item['request_sent'],
                       'body_missing': item['body_missing'], 'body': body, 'hidden_reasoning_fields_omitted': removed})
    return result


def request_pins(values):
    return [{key: value for key, value in item.items() if key != 'body'} for item in values]


def export(out, destination, expected):
    out, protocol, cases, report, rows, _, campaign_binding = frozen_input(out, expected)
    destination = Path(destination)
    if destination.exists():
        raise ValueError('refuse_overwrite_blind_export')
    by_id = {case['id']: case for case in cases}
    gold_source = inside(out / 'source', 'evaluation/window_integration/semantic_cases.json')
    context = read(gold_source)
    items, mapping = [], []
    for value in rows:
        row = value['row']; case = by_id[row['case_id']]; opaque = uuid.uuid4().hex
        item = {'blind_id': opaque, 'prompt': case['prompt'], 'gold': deepcopy(case['gold']),
                'criteria': criteria_for(case, context), 'record': blind_record(row),
                'model_inputs': blind_model_inputs(value['request_inputs']),
                'model_inputs_complete': value['request_inputs_complete'],
                'record_file_missing': value['raw'] is None,
                'supervisor_and_worker_record_equal': value['raw'] == row,
                'worker_record': blind_record(value['raw']) if value['raw'] is not None and value['raw'] != row else None}
        item['item_sha256'] = sha(packed(item).encode())
        items.append(item)
        mapping.append({'blind_id': opaque, 'index': value['index'], 'case_id': case['id'],
                        'variant': row['variant'], 'repetition': row['repetition'], 'family': case['family'],
                        'record_path': value['path'], 'record_sha256': value['record_sha256'],
                        'results_row_sha256': value['results_row_sha256'], 'artifact_sha256': value['artifact_sha256'],
                        'request_body_pins': request_pins(value['request_inputs']),
                        'item_sha256': item['item_sha256']})
    random.SystemRandom().shuffle(items)
    packet = {'version': VERSION, 'protocol_sha256': expected, 'case_sha256': protocol['case_sha256'],
              'all_started': len(rows), 'scope': protocol.get('scope'), 'shared_context': context,
              'blindness': '遮蔽显式组别/基础设施身份及性能数字；业务轨迹可能透露架构，不声称双盲。',
              'grading': '完整原文标准逐条语义复核＋冻结独立必要条件。agent_review/GLM approved不是判卷结果。',
              'items': items}
    destination.mkdir(parents=True)
    write_new(destination / 'blind-packet.json', packet)
    manifest = {'version': VERSION, 'protocol_sha256': expected, 'case_sha256': protocol['case_sha256'],
                'results_sha256': digest(out / 'results.json'), 'blind_packet_sha256': digest(destination / 'blind-packet.json'),
                'all_started': len(rows), 'mapping': mapping, 'campaign_start_binding': campaign_binding}
    write_new(destination / 'private-mapping.json', manifest, private=True)
    template = {'version': VERSION, 'protocol_sha256': expected, 'case_sha256': protocol['case_sha256'],
                'blind_packet_sha256': manifest['blind_packet_sha256'], 'reviewer': '', 'reviewed_at': '',
                'source_kind': 'human', 'independent_of_generation': None, 'performance_numbers_seen': None,
                'reviewer_source_reference': '', 'identity_authentication': '本工具只验证来源声明和证据绑定，身份真实性须由根任务核验。',
                'items': [{'blind_id': item['blind_id'], 'item_sha256': item['item_sha256'], 'verdict': None,
                    'rationale': '', 'criteria': [{**criterion, 'verdict': None, 'evidence': '', 'citations': []}
                                                for criterion in item['criteria']]} for item in items]}
    write_new(destination / 'review-template.json', template)
    return {'all_started': len(rows), 'packet': str(destination / 'blind-packet.json'),
            'template': str(destination / 'review-template.json'), 'private_mapping': str(destination / 'private-mapping.json'),
            'business_pass': None, 'cloud_calls': 0, 'campaign_modified': False}


def binding(out, directory, expected):
    values = frozen_input(out, expected)
    _, protocol, cases, report, rows, harness, campaign_binding = values
    directory = Path(directory)
    manifest = read(directory / 'private-mapping.json'); packet = read(directory / 'blind-packet.json')
    if (manifest.get('version') != VERSION or packet.get('version') != VERSION
            or manifest.get('protocol_sha256') != expected or packet.get('protocol_sha256') != expected
            or manifest.get('case_sha256') != protocol['case_sha256'] or packet.get('case_sha256') != protocol['case_sha256']
            or manifest.get('results_sha256') != digest(Path(out) / 'results.json')
            or manifest.get('blind_packet_sha256') != digest(directory / 'blind-packet.json')
            or manifest.get('campaign_start_binding') != campaign_binding):
        raise ValueError('blind_export_binding_changed')
    mapping = manifest.get('mapping', []); items = packet.get('items', [])
    if (len(mapping) != len(rows) or len(items) != len(rows) or packet.get('all_started') != len(rows)
            or manifest.get('all_started') != len(rows) or len({v['blind_id'] for v in mapping}) != len(rows)
            or len({v['blind_id'] for v in items}) != len(rows)):
        raise ValueError('all_started_blind_inventory_incomplete')
    by_case = {case['id']: case for case in cases}; by_blind = {item['blind_id']: item for item in items}
    current_context = read(inside(Path(out) / 'source', 'evaluation/window_integration/semantic_cases.json'))
    if packet.get('shared_context') != current_context:
        raise ValueError('complete_original_gold_context_changed')
    for index, value in enumerate(rows):
        m = mapping[index]; row = value['row']; case = by_case[row['case_id']]
        for key, expected_value in {'index': index, 'case_id': case['id'], 'variant': row['variant'],
                'repetition': row['repetition'], 'family': case['family'], 'record_path': value['path'],
                'record_sha256': value['record_sha256'], 'results_row_sha256': value['results_row_sha256'],
                'artifact_sha256': value['artifact_sha256'], 'request_body_pins': request_pins(value['request_inputs'])}.items():
            if m.get(key) != expected_value:
                raise ValueError('original_record_or_mapping_changed:' + key)
        item = deepcopy(by_blind.get(m['blind_id']))
        if not item:
            raise ValueError('blind_item_missing')
        declared = item.pop('item_sha256', None)
        if declared != sha(packed(item).encode()) or declared != m.get('item_sha256'):
            raise ValueError('blind_item_sha_mismatch')
        if (item.get('prompt') != case['prompt'] or item.get('gold') != case['gold']
                or item.get('criteria') != criteria_for(case, current_context)
                or item.get('record') != blind_record(row)
                or item.get('model_inputs') != blind_model_inputs(value['request_inputs'])
                or item.get('model_inputs_complete') != value['request_inputs_complete']
                or item.get('record_file_missing') != (value['raw'] is None)
                or item.get('supervisor_and_worker_record_equal') != (value['raw'] == row)
                or item.get('worker_record') != (blind_record(value['raw']) if value['raw'] is not None and value['raw'] != row else None)):
            raise ValueError('blind_business_evidence_changed')
    return values, manifest, packet


def select_pointer(value, pointer):
    for part in pointer:
        if isinstance(value, list) and type(part) is int and 0 <= part < len(value):
            value = value[part]
        elif isinstance(value, dict) and isinstance(part, str) and part in value:
            value = value[part]
        else:
            raise ValueError('evidence_pointer_does_not_exist')
    return value


def validate_review(review, manifest, packet):
    for key in ('version', 'protocol_sha256', 'case_sha256', 'blind_packet_sha256'):
        if review.get(key) != manifest[key]:
            raise ValueError('review_binding_mismatch:' + key)
    for key in ('reviewer', 'reviewed_at', 'reviewer_source_reference'):
        if not isinstance(review.get(key), str) or not review[key].strip():
            raise ValueError('independent_reviewer_source_required:' + key)
    try:
        parsed = datetime.fromisoformat(review['reviewed_at'].replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError('review_timestamp_timezone_required') from None
    if (review.get('source_kind') not in ('human', 'independent_external')
            or review.get('independent_of_generation') is not True
            or review.get('performance_numbers_seen') is not False):
        raise ValueError('independent_blind_source_attestation_required')
    source = (review['reviewer'] + ' ' + review['reviewer_source_reference']).casefold()
    if any(value in source for value in ('operational_glm', 'agent_review', 'business_glm_approved')):
        raise ValueError('operational_glm_approval_is_not_independent_judging')
    items = review.get('items')
    original = {item['blind_id']: item for item in packet['items']}
    if (not isinstance(items, list) or len(items) != len(original)
            or {row.get('blind_id') for row in items if isinstance(row, dict)} != set(original)):
        raise ValueError('all_started_review_missing_or_duplicate')
    for row in items:
        item = original[row['blind_id']]
        if row.get('item_sha256') != item['item_sha256']:
            raise ValueError('review_item_sha_mismatch')
        if row.get('verdict') not in ('pass', 'fail', 'incomplete') or not str(row.get('rationale', '')).strip():
            raise ValueError('complete_overall_semantic_verdict_required')
        judgments = row.get('criteria')
        if (not isinstance(judgments, list) or len(judgments) != len(item['criteria'])
                or {criterion.get('id') for criterion in judgments if isinstance(criterion, dict)}
                != {criterion['id'] for criterion in item['criteria']}):
            raise ValueError('original_criteria_review_missing_or_duplicate')
        expected = {criterion['id']: criterion for criterion in item['criteria']}
        for criterion in judgments:
            fixed = expected[criterion['id']]
            if any(criterion.get(key) != value for key, value in fixed.items()):
                raise ValueError('original_criterion_text_or_sha_changed')
            allowed = ('absent', 'present', 'not_applicable') if fixed['kind'] == 'forbidden' else ('met', 'not_met', 'not_applicable')
            if criterion.get('verdict') not in allowed or not isinstance(criterion.get('evidence'), str) or not criterion['evidence'].strip():
                raise ValueError('criterion_semantic_judgment_and_evidence_required')
            citations = criterion.get('citations')
            if not isinstance(citations, list) or not citations:
                raise ValueError('bound_actual_evidence_citation_required')
            actual_cited = False
            for citation in citations:
                if not isinstance(citation, dict) or not isinstance(citation.get('pointer'), list) or not citation['pointer']:
                    raise ValueError('invalid_evidence_citation')
                pointer = citation['pointer']
                if pointer[0] not in ('record', 'worker_record', 'prompt', 'model_inputs'):
                    raise ValueError('gold_or_performance_is_not_actual_answer_evidence')
                if len(pointer) > 1 and pointer[0] in ('record', 'worker_record'):
                    actual_fields = {'result', 'before', 'after', 'tool_invocations', 'model_tool_calls', 'tool_outputs',
                                     'model_projected_tool_outputs', 'actual_events', 'runner_failure', 'exit',
                                     'http_status', 'technical_completed', 'status', 'preflight'}
                    if pointer[1] not in actual_fields:
                        raise ValueError('infrastructure_or_prior_judgment_is_not_actual_answer_evidence')
                    if pointer[1] in ('oracle', 'independent_checks', 'human_review', 'independent_reviews'):
                        raise ValueError('prior_judgment_is_not_actual_answer_evidence')
                    if pointer[1] == 'preflight' and fixed['kind'] != 'fact_review':
                        raise ValueError('preflight_is_not_model_answer_evidence')
                    if pointer[1:3] == ['result', 'agent_review']:
                        raise ValueError('operational_glm_approval_is_not_answer_evidence')
                    if pointer[1] == 'result' and (len(pointer) < 3 or (pointer[2] == 'agent_runtime'
                            and (len(pointer) < 4 or pointer[3] not in ('trace', 'intent_route', 'failure', 'contract_errors')))):
                        raise ValueError('precise_business_answer_or_trace_pointer_required')
                    actual_cited = True
                elif pointer[0] == 'model_inputs':
                    if len(pointer) < 4 or type(pointer[1]) is not int or pointer[2] != 'body':
                        raise ValueError('precise_actual_model_message_pointer_required')
                    actual_cited = True
                elif pointer[0] != 'prompt':
                    raise ValueError('precise_actual_evidence_pointer_required')
                actual = select_pointer(item, pointer)
                if citation.get('value_sha256') != sha(packed(actual).encode()):
                    raise ValueError('actual_evidence_value_sha_mismatch')
                excerpt = citation.get('excerpt')
                if not isinstance(excerpt, str) or not excerpt or excerpt not in (actual if isinstance(actual, str) else packed(actual)):
                    raise ValueError('evidence_excerpt_not_in_actual_artifact')
                if isinstance(actual, str) and actual.startswith(('[身份或性能字段已遮蔽', '[进程身份已遮蔽', '[基础设施身份已遮蔽')):
                    raise ValueError('masked_infrastructure_is_not_business_evidence')
            if not actual_cited:
                raise ValueError('prompt_alone_does_not_prove_answer_completion')
            if row['verdict'] == 'pass':
                desired = 'absent' if fixed['kind'] == 'forbidden' else 'met'
                conditional = fixed['kind'] in ('conditional_evidence', 'fact_review')
                if criterion['verdict'] != desired and not (conditional and criterion['verdict'] == 'not_applicable'):
                    raise ValueError('criterion_contradicts_complete_semantic_pass')
    return {row['blind_id']: row for row in items}


def import_reviews(out, directory, reviews, destination, expected):
    values, manifest, packet = binding(out, directory, expected)
    _, _, cases, report, records, harness, _ = values
    if not reviews:
        raise ValueError('independent_review_file_required')
    validated, inputs, identities = [], [], set()
    for path in reviews:
        review = read(path)
        identity = review.get('reviewer')
        if not isinstance(identity, str):
            raise ValueError('independent_reviewer_identity_required')
        identity_key = identity.strip().casefold()
        if identity_key in identities:
            raise ValueError('duplicate_reviewer_identity')
        identities.add(identity_key)
        validated.append(validate_review(review, manifest, packet))
        inputs.append({'path': str(Path(path).resolve()), 'sha256': digest(path), 'reviewer': identity,
                       'source_kind': review['source_kind'], 'source_reference': review['reviewer_source_reference'],
                       'reviewed_at': review['reviewed_at']})
    by_case = {case['id']: case for case in cases}
    results = []
    # 原 oracle 完整 classify；不调用 build_fixture/_self_test，不重生成答案。
    with offline_guard():
        oracle = harness.oracle_module(Path(out))
        for m, value in zip(manifest['mapping'], records):
            case = by_case[m['case_id']]; row = deepcopy(value['row'])
            opinions = [review[m['blind_id']] for review in validated]
            verdicts = {opinion['verdict'] for opinion in opinions}
            base = harness.automatic_grade(row, case, oracle)
            checks = base.get('checks', [])
            necessary = (bool(checks) and all(check.get('passed') is not False for check in checks)
                         and base.get('status') not in ('fixture_or_runner_error', 'fixture_error', 'technical_failure', 'necessary_condition_failure'))
            terminal = row.get('status') in ('FINISHED', 'FAILED')
            exit_status = row.get('exit', {})
            supervision = (isinstance(exit_status, dict) and exit_status.get('exit_code') == 0
                           and exit_status.get('reaped') is True and exit_status.get('process_group_still_exists') is False
                           and not any(exit_status.get(key) for key in ('timed_out', 'deadline_exceeded', 'supervisor_failure')))
            billing = harness.account_calls(row.get('provider_calls', []))
            provider_pin = next((pin for path, pin in value['artifact_sha256'].items() if path.endswith('/provider-calls.json')), None)
            exit_pin = next((pin for path, pin in value['artifact_sha256'].items() if path.endswith('/exit.json')), None)
            start_pin = next((pin for path, pin in value['artifact_sha256'].items() if path.endswith('/start.json')), None)
            identity = all(call.get('identity_verified') is True for call in row.get('provider_calls', []) if call.get('request_sent'))
            technical = (row.get('technical_completed') is True and row.get('http_status') == 200
                         and terminal and value['raw'] is not None and value['raw'] == value['row']
                         and supervision and billing['known'] and row.get('cost', {}).get('known') is True and identity
                         and provider_pin is not None and exit_pin is not None and start_pin is not None
                         and value['request_inputs_complete'])
            reason = 'semantic_pass' if verdicts == {'pass'} else 'semantic_fail' if verdicts == {'fail'} else 'semantic_incomplete' if verdicts == {'incomplete'} else 'independent_reviewer_disagreement'
            passed = technical and necessary and verdicts == {'pass'}
            if case.get('oracle') == 'original_semantic':
                # 保持原人工接口每个 index/原文，强加真实原记录 SHA绑定。
                first = opinions[0]; judged = {c['id']: c for c in first['criteria']}
                human = {'reviewer': inputs[0]['reviewer'], 'reviewed_at': inputs[0]['reviewed_at'],
                         'artifact_sha256': m['record_sha256'] or '0' * 64,
                         'rationale': first['rationale'], 'verdict': first['verdict']}
                for name, criteria in oracle._manual_template(case['gold']).items():
                    prefix = ['gold', 'expected', name] if name != 'human_judgment' else ['gold', name]
                    human[name] = [{'index': criterion['index'], 'verdict': judged['/'.join(map(str, prefix + [criterion['index']]))]['verdict'],
                                    'evidence': judged['/'.join(map(str, prefix + [criterion['index']]))]['evidence']} for criterion in criteria]
                row['human_review'] = human; row['record_artifact_sha256'] = m['record_sha256']
                combined = oracle.classify(row, case['gold'])
                passed = passed and combined.get('semantic_pass') is True
            else:
                combined = base
            if not technical:
                reason = 'original_technical_or_missing_result_failure'
            elif not necessary:
                reason = 'original_necessary_condition_failure'
            results.append({**m, 'pass': bool(passed), 'status': reason, 'technical_complete': bool(technical),
                            'necessary_conditions_passed': bool(necessary), 'original_necessary_checks': base,
                            'original_oracle_with_bound_review': combined, 'reviews': opinions,
                            'disagreement': len(verdicts) > 1})
    result = {'version': VERSION, 'protocol_sha256': expected, 'case_sha256': manifest['case_sha256'],
              'results_sha256': manifest['results_sha256'], 'blind_packet_sha256': manifest['blind_packet_sha256'],
              'private_mapping_sha256': digest(Path(directory) / 'private-mapping.json'), 'review_inputs': inputs,
              'all_started': len(records), 'all_started_denominator': True, 'pass_count': sum(row['pass'] for row in results),
              'records': results, 'reviewer_identity_authentication': '来源声明与文件绑定已核；身份真实性由根任务核验，非本工具认证。',
              'adoption_verdict': 'NOT_ASSESSED', 'cloud_calls': 0, 'campaign_modified': False,
              'original_records_modified': False, 'operational_glm_approval_used_as_truth': False}
    write_new(destination, result)
    return result


def bootstrap(families, seed=20261009, samples=5000):
    if len(families) < 2:
        return None
    rng = random.Random(seed); keys = sorted(families); values = []
    for _ in range(samples):
        draw = [value for _ in keys for value in families[rng.choice(keys)]]
        values.append(sum(draw) / len(draw))
    values.sort()
    return [values[math.floor((samples - 1) * .025)], values[math.ceil((samples - 1) * .975)]]


def summarize(out, directory, imported, destination, expected):
    values, manifest, packet = binding(out, directory, expected)
    _, protocol, cases, report, rows, harness, _ = values
    result = read(imported)
    if (result.get('version') != VERSION or result.get('protocol_sha256') != expected
            or result.get('case_sha256') != manifest['case_sha256'] or result.get('results_sha256') != manifest['results_sha256']
            or result.get('blind_packet_sha256') != manifest['blind_packet_sha256']
            or result.get('private_mapping_sha256') != digest(Path(directory) / 'private-mapping.json')):
        raise ValueError('imported_judging_binding_changed')
    # 重新核输入并重算判卷，不能接受被手改的 import 成绩。
    scratch = Path(destination).with_name('.verify-' + uuid.uuid4().hex + '.json')
    try:
        recomputed = import_reviews(out, directory, [v['path'] for v in result['review_inputs']], scratch, expected)
    finally:
        if scratch.exists():
            scratch.unlink()
    if result != recomputed:
        raise ValueError('imported_judging_result_changed')
    by_blind = {row['blind_id']: row for row in result['records']}; by_case = {c['id']: c for c in cases}
    measures, arms = {}, {}
    maximum = protocol['limits']['seconds'] * 1000
    for mapping, value in zip(manifest['mapping'], rows):
        row = value['row']; judged = by_blind[mapping['blind_id']]
        billing = harness.account_calls(row.get('provider_calls', []))
        # 缺 record/未结算启动没有完整计费凭据；空调用不能推断成本0。
        provider_pin = next((pin for path, pin in value['artifact_sha256'].items() if path.endswith('/provider-calls.json')), None)
        start_pin = next((pin for path, pin in value['artifact_sha256'].items() if path.endswith('/start.json')), None)
        if value['raw'] is None or row.get('status') == 'STARTED' or row.get('cost', {}).get('known') is not True or provider_pin is None or start_pin is None:
            billing['known'] = False; billing['estimated_upper_cny'] = None
        latency = row.get('http_elapsed_ms') if judged['technical_complete'] else maximum
        if not isinstance(latency, (int, float)) or isinstance(latency, bool) or not math.isfinite(latency) or latency < 0:
            latency = maximum
        wall = row.get('worker_wall_ms')
        if not isinstance(wall, (int, float)) or isinstance(wall, bool) or not math.isfinite(wall) or wall < 0:
            wall = maximum
        metric = {'pass': int(judged['pass']), 'latency_ms': latency, 'worker_wall_ms': wall,
                  'cost_upper_cny': billing['estimated_upper_cny'] if billing['known'] else None,
                  'cost_lower_bound_cny': billing['estimated_upper_cny_lower_bound'], 'billing_known': billing['known'],
                  'family': mapping['family'], 'case_id': mapping['case_id'], 'repetition': mapping['repetition']}
        measures[(mapping['case_id'], mapping['repetition'], mapping['variant'])] = metric
    for arm in protocol['arms']:
        selected = [metric for key, metric in measures.items() if key[2] == arm]; n = len(selected)
        known = all(m['billing_known'] for m in selected)
        arms[arm] = {'all_started': n, 'pass_count': sum(m['pass'] for m in selected),
                     'completion_rate_all_started': sum(m['pass'] for m in selected) / n if n else None,
                     'penalized_mean_api_latency_ms': sum(m['latency_ms'] for m in selected) / n if n else None,
                     'mean_worker_wall_ms': sum(m['worker_wall_ms'] for m in selected) / n if n else None,
                     'accounting_complete': known,
                     'estimated_upper_cny': sum(m['cost_upper_cny'] for m in selected) if known else None,
                     'estimated_upper_cny_lower_bound': sum(m['cost_lower_bound_cny'] for m in selected),
                     'unknown_cost_starts': sum(not m['billing_known'] for m in selected)}
    comparisons = {}
    for baseline in ('off', 'native'):
        pairs = []
        for key, candidate in measures.items():
            if key[2] == 'jev' and (key[0], key[1], baseline) in measures:
                pairs.append((candidate, measures[(key[0], key[1], baseline)]))
        metrics = {}
        for field in ('pass', 'latency_ms', 'worker_wall_ms', 'cost_upper_cny'):
            families = {}; missing = 0
            for candidate, anchor in pairs:
                if candidate[field] is None or anchor[field] is None:
                    missing += 1; continue
                families.setdefault(candidate['family'], []).append(candidate[field] - anchor[field])
            diffs = [value for group in families.values() for value in group]
            complete = missing == 0
            metrics[field] = {'delta_jev_minus_baseline': sum(diffs) / len(diffs) if diffs and complete else None,
                              'family_cluster_bootstrap_95_percent': bootstrap(families) if diffs and complete else None,
                              'paired_starts': len(pairs), 'known_metric_pairs': len(diffs), 'unknown_metric_pairs': missing,
                              'distinct_families': len(families), 'repetitions_are_not_independent_tasks': True}
        comparisons['jev_vs_' + baseline] = {'metrics': metrics, 'paired_case_repetitions': len(pairs),
            'unpaired_jev_starts': sum(key[2] == 'jev' for key in measures) - len(pairs),
            'unpaired_baseline_starts': sum(key[2] == baseline for key in measures) - len(pairs)}
    summary = {'version': VERSION, 'protocol_sha256': expected, 'results_sha256': manifest['results_sha256'],
               'independent_result_sha256': digest(imported), 'all_started': len(rows), 'planned_starts': len(protocol['plan']),
               'unstarted_plan_rows': len(protocol['plan']) - len(rows), 'arms': arms, 'comparisons': comparisons,
               'scope': protocol.get('scope'), 'campaign_configuration': report.get('campaign_configuration'),
               'configuration_attempt': report.get('configuration_attempt'), 'same_configuration_repair_of': report.get('same_configuration_repair_of'),
               'all_attempts_campaign_summary': False, 'adoption_verdict': 'NOT_ASSESSED', 'real_customer_holdout': 0,
               'current_question_count': len(cases), 'distinct_families': len({c['family'] for c in cases}),
               'selection_accuracy': None, 'eligible_route_coverage': None,
               'limitation': 'DEV原题已暴露，完整能力/顺序/禁动作路由金标及独立任务holdout不足；工程均值和簇bootstrap区间不证明采用。',
               'cost_status': '冻结价格上界估计，非实际账单；未知费用保留null，失败及fallback调用全部纳入。',
               'bootstrap': {'method': 'paired differences, family clusters sampled with replacement; retain every repetition within selected family',
                             'seed': 20261009, 'samples': 5000, 'less_than_two_families_interval': None},
               'cloud_calls': 0, 'campaign_modified': False, 'jev_default_changed': False}
    write_new(destination, summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('export', 'import', 'summary'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--protocol-sha', required=True)
    parser.add_argument('--review-dir', type=Path, required=True)
    parser.add_argument('--review', type=Path, action='append', default=[])
    parser.add_argument('--imported', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.action != 'export' and args.output is None:
        parser.error('import/summary require --output; original result directories remain unchanged')
    if args.action == 'summary' and args.imported is None:
        parser.error('summary requires --imported')
    try:
        with offline_guard():
            if args.action == 'export':
                result = export(args.out, args.review_dir, args.protocol_sha)
            elif args.action == 'import':
                result = import_reviews(args.out, args.review_dir, args.review, args.output, args.protocol_sha)
            else:
                result = summarize(args.out, args.review_dir, args.imported, args.output, args.protocol_sha)
        print(json.dumps({key: value for key, value in result.items() if key in ('all_started', 'pass_count', 'cloud_calls', 'packet', 'template', 'adoption_verdict')}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as error:
        print(json.dumps({'status': 'REJECTED', 'error': str(error), 'cloud_calls': 0}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
