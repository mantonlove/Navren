"""独立的wire字节身份与未认证token预算估计；不加载密钥、不授予权限。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
from types import SimpleNamespace


MODEL = 'DeepSeek-V4-Flash'
TOKENIZER_SHA = '8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf'
TOKENIZERS_VERSION = '0.23.3'
PROFILES = (
    ('base', 'deepseek_v4_base.py', 'bdbd57c132a1b3725042323d02b98b9d1df28e5f388f134399555d041f5055e0',
     '60d8d70770c6776ff598c94bb586a859a38244f1'),
    ('0731', 'deepseek_v4_0731.py', 'abc0d26120250dda0ae077dc64aa28836026e61e970854aaeb792445e6a0dde6',
     '7872f01b1d1fe23eabc4c98b48bffcef5a386062'),
)
_CACHE = None


OFFICIAL_MODEL = 'deepseek-flash'
OFFICIAL_VERSION = 'DeepSeek-V4.1-Flash-1017'
OFFICIAL_REVISION = '1bae0e1088993b9b82de08f59de5be5a34e42170'
OFFICIAL_TOKENIZER_SHA = 'c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b'
OFFICIAL_ENCODER_SHA = '13231c592e958fb39e2af6001dae69d90e7a36b265702b9b390236fb387599e2'
_OFFICIAL_CACHE = None


class RequestBudgetError(ValueError):
    """只返回固定错误码；不把请求正文、隐藏内容或用户资料写入错误。"""
    pass


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RequestBudgetError('request_budget_duplicate_json_key')
        result[key] = value
    return result


def _json(value):
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json(item)
        return
    raise RequestBudgetError('request_budget_unsupported_json')


def _payload(wire):
    if type(wire) is not bytes:
        raise RequestBudgetError('request_budget_wire_bytes_required')
    try:
        value = json.loads(wire.decode('utf-8'), object_pairs_hook=_unique)
        _json(value)
    except (ValueError, UnicodeError, RecursionError):
        raise RequestBudgetError('request_budget_invalid_json') from None
    if type(value) is not dict or type(value.get('model')) is not str:
        raise RequestBudgetError('request_budget_model_required')
    return value


def _validate_official_payload(payload):
    allowed = {'model', 'messages', 'tools', 'tool_choice', 'temperature', 'max_tokens',
               'thinking', 'enable_thinking', 'reasoning_effort', 'response_format', 'provider', 'extra_body'}
    if set(payload) - allowed or type(payload.get('messages')) is not list or not payload['messages']:
        raise RequestBudgetError('request_budget_unsupported_payload')
    provider = payload.get('provider')
    fixed_primary_policy = (payload.get('model') == 'DeepSeek-V4-Flash'
        and type(provider) is dict and set(provider) == {'only', 'allow_fallbacks', 'effective_multiplier_range'}
        and type(provider.get('only')) is list and provider['only'] == ['硅基流动']
        and provider.get('allow_fallbacks') is False
        and type(provider.get('effective_multiplier_range')) is list
        and len(provider['effective_multiplier_range']) == 2
        and all(type(value) is int and value == 1 for value in provider['effective_multiplier_range'])
        and payload.get('enable_thinking') is False and 'thinking' not in payload
        and 'extra_body' not in payload and 'reasoning_effort' not in payload)
    if ('provider' in payload or 'extra_body' in payload) and not fixed_primary_policy:
        if payload.get('model') != 'DeepSeek-V4-Flash':
            raise RequestBudgetError('request_budget_unsupported_transport_model')
        provider = payload.get('provider')
        extra = payload.get('extra_body')
        legacy_provider = (type(provider) is dict
            and set(provider) == {'sort', 'effective_multiplier_range', 'allow_fallbacks'}
            and provider.get('sort') == ['latency', 'throughput']
            and type(provider.get('effective_multiplier_range')) is list
            and len(provider['effective_multiplier_range']) == 2
            and provider['effective_multiplier_range'][0] is None
            and type(provider['effective_multiplier_range'][1]) is int
            and provider['effective_multiplier_range'][1] == 1
            and provider.get('allow_fallbacks') is False)
        fixed_provider = (type(provider) is dict and set(provider) == {'only', 'allow_fallbacks'}
            and type(provider.get('only')) is list and provider['only'] == ['SophNet']
            and provider.get('allow_fallbacks') is False)
        if (not (legacy_provider or fixed_provider)
                or type(extra) is not dict or set(extra) != {'enable_thinking'}
                or extra.get('enable_thinking') is not False):
            raise RequestBudgetError('request_budget_unsupported_transport_policy')
    first_system = False
    for message in payload['messages']:
        if (type(message) is not dict or message.get('role') not in
                ('system', 'user', 'assistant', 'tool', 'latest_reminder')
                or set(message) - {'role', 'content', 'tool_calls', 'tool_call_id', 'reasoning_content', 'name'}
                or 'content' not in message or message['content'] is not None and type(message['content']) is not str):
            raise RequestBudgetError('request_budget_unsupported_message')
        role = message['role']
        if role != 'assistant' and type(message['content']) is not str:
            raise RequestBudgetError('request_budget_unsupported_message_content')
        if role == 'system':
            first_system = True
        if 'reasoning_content' in message and (role != 'assistant' or
                message['reasoning_content'] is not None and type(message['reasoning_content']) is not str):
            raise RequestBudgetError('request_budget_unsupported_reasoning')
        if 'tool_call_id' in message and (role != 'tool' or type(message['tool_call_id']) is not str):
            raise RequestBudgetError('request_budget_unsupported_tool_result')
        if 'tool_calls' in message:
            if role != 'assistant' or type(message['tool_calls']) is not list:
                raise RequestBudgetError('request_budget_unsupported_tool_calls')
            for call in message['tool_calls']:
                if (type(call) is not dict or not {'id', 'type', 'function'} <= set(call)
                        or set(call) - {'id', 'type', 'function', 'index'}
                        or 'index' in call and (type(call['index']) is not int or call['index'] < 0)
                        or type(call['id']) is not str or call['type'] != 'function'
                        or type(call['function']) is not dict or set(call['function']) != {'name', 'arguments'}
                        or type(call['function']['name']) is not str or type(call['function']['arguments']) is not str):
                    raise RequestBudgetError('request_budget_unsupported_tool_call')
                try:
                    arguments = json.loads(call['function']['arguments'], object_pairs_hook=_unique)
                    _json(arguments)
                    if type(arguments) is not dict:
                        raise ValueError()
                except (ValueError, RecursionError):
                    raise RequestBudgetError('request_budget_invalid_tool_arguments') from None
    if not first_system:
        raise RequestBudgetError('request_budget_explicit_system_required')
    tools = payload.get('tools', [])
    if type(tools) is not list:
        raise RequestBudgetError('request_budget_unsupported_tools')
    for tool in tools:
        if (type(tool) is not dict or set(tool) != {'type', 'function'} or tool['type'] != 'function'
                or type(tool['function']) is not dict or set(tool['function']) - {'name', 'description', 'parameters', 'strict'}
                or type(tool['function'].get('name')) is not str
                or type(tool['function'].get('parameters')) is not dict):
            raise RequestBudgetError('request_budget_unsupported_tool_schema')
    if 'thinking' in payload and (type(payload['thinking']) is not dict or
            set(payload['thinking']) != {'type'} or payload['thinking']['type'] not in ('enabled', 'disabled')):
        raise RequestBudgetError('request_budget_unsupported_thinking')
    if 'enable_thinking' in payload and type(payload['enable_thinking']) is not bool:
        raise RequestBudgetError('request_budget_unsupported_thinking')
    if 'reasoning_effort' in payload and payload['reasoning_effort'] not in ('low', 'medium', 'high', 'max'):
        raise RequestBudgetError('request_budget_unsupported_effort')
    if 'max_tokens' in payload and (type(payload['max_tokens']) is not int or payload['max_tokens'] < 1):
        raise RequestBudgetError('request_budget_unsupported_output_cap')


def _fixed_file(path, expected_sha):
    if path.is_symlink() or not path.is_file() or path.resolve() != path.absolute():
        raise RequestBudgetError('request_budget_dependency_missing_or_redirected')
    raw = path.read_bytes()
    if _sha(raw) != expected_sha:
        raise RequestBudgetError('request_budget_dependency_sha_mismatch')
    return raw


def _profiles():
    global _CACHE
    root = Path(__file__).resolve().parent
    tokenizer_path = root.parent / 'dependency-resources' / 'deepseek-v4' / 'tokenizer.json'
    # Every estimate checks file bytes; a cached parser cannot hide later drift.
    tokenizer_raw = _fixed_file(tokenizer_path, TOKENIZER_SHA)
    paths = [(name, root / 'vendor' / filename, digest, revision)
             for name, filename, digest, revision in PROFILES]
    encoder_bytes = {name: _fixed_file(path, digest) for name, path, digest, _ in paths}
    try:
        if importlib.metadata.version('tokenizers') != TOKENIZERS_VERSION:
            raise RequestBudgetError('request_budget_tokenizers_version_mismatch')
        from tokenizers import Tokenizer
    except (ImportError, importlib.metadata.PackageNotFoundError):
        raise RequestBudgetError('request_budget_tokenizers_unavailable') from None
    if _CACHE is None:
        try:
            tokenizer = Tokenizer.from_str(tokenizer_raw.decode('utf-8'))
            if tokenizer.token_to_id('<｜begin▁of▁sentence｜>') != 0:
                raise RequestBudgetError('request_budget_tokenizer_identity_mismatch')
            modules = []
            for name, path, digest, revision in paths:
                namespace = {'__name__': 'navren_fixed_dsv4_' + name}
                exec(compile(encoder_bytes[name], str(path), 'exec'), namespace)
                module = SimpleNamespace(**namespace)
                modules.append((name, digest, revision, module))
            _CACHE = (tokenizer, tuple(modules))
        except (ValueError, OSError, UnicodeError, AttributeError, TypeError):
            raise RequestBudgetError('request_budget_dependency_load_failed') from None
    return _CACHE


def _official_profile():
    global _OFFICIAL_CACHE
    root = Path(__file__).resolve().parent
    tokenizer_path = root.parent / 'dependency-resources' / 'deepseek-v41-1017' / 'tokenizer.json'
    encoder_path = root / 'vendor' / 'deepseek_v41_1017.py'
    # 每次均复核固定资产；缓存不能掩盖后续改动。
    tokenizer_raw = _fixed_file(tokenizer_path, OFFICIAL_TOKENIZER_SHA)
    encoder_raw = _fixed_file(encoder_path, OFFICIAL_ENCODER_SHA)
    try:
        if importlib.metadata.version('tokenizers') != TOKENIZERS_VERSION:
            raise RequestBudgetError('request_budget_tokenizers_version_mismatch')
        from tokenizers import Tokenizer
        if _OFFICIAL_CACHE is None:
            tokenizer = Tokenizer.from_str(tokenizer_raw.decode('utf-8'))
            if tokenizer.token_to_id('<｜begin▁of▁sentence｜>') != 0:
                raise RequestBudgetError('request_budget_tokenizer_identity_mismatch')
            namespace = {'__name__': 'navren_fixed_dsv41_1017'}
            exec(compile(encoder_raw, str(encoder_path), 'exec'), namespace)
            _OFFICIAL_CACHE = (tokenizer, SimpleNamespace(**namespace))
    except RequestBudgetError:
        raise
    except (ImportError, importlib.metadata.PackageNotFoundError, ValueError,
            OSError, UnicodeError, AttributeError, TypeError):
        raise RequestBudgetError('request_budget_dependency_load_failed') from None
    return _OFFICIAL_CACHE

def _estimate_official(payload, wire, result):
    # 不支持的完整API形态保持原byte回退，不猜测媒体或未知协议的token数。
    try:
        _validate_official_payload(payload)
    except RequestBudgetError as error:
        result.update(input_token_estimate=len(wire), raw_token_estimate=None,
            margin_tokens=0, bound_method='full_wire_bytes_conservative_token_fallback',
            profiles=[], profile_fallback_reason=error.args[0])
        return result
    tokenizer, encoder = _official_profile()
    special = tuple(t.content for t in tokenizer.get_added_tokens_decoder().values() if t.content)
    def contains_special(value):
        if type(value) is str: return any(s in value for s in special)
        if type(value) is dict: return any(contains_special(k) or contains_special(v) for k,v in value.items())
        if type(value) is list: return any(contains_special(v) for v in value)
        return False
    if contains_special(payload):
        result.update(input_token_estimate=len(wire), raw_token_estimate=None,
            margin_tokens=0, bound_method='full_wire_bytes_conservative_token_fallback',
            profiles=[], profile_fallback_reason='request_budget_literal_special_token_unsupported')
        return result
    requested = payload.get('reasoning_effort')
    # 官方API medium是high兼容别名；wire原样保留，只改参考模板参数。
    effort = 'high' if requested in (None, 'medium') else requested
    counts = []
    try:
        for mode in ('chat', 'thinking'):
            messages = deepcopy(payload['messages'])
            first = next(m for m in messages if m['role'] == 'system')
            if payload.get('tools'): first['tools'] = deepcopy(payload['tools'])
            if payload.get('response_format'): first['response_format'] = deepcopy(payload['response_format'])
            prompt, media = encoder.encode_messages(messages, thinking_mode=mode,
                drop_thinking=False, add_default_bos_token=True, reasoning_effort=effort,
                return_multi_modal_data=True)
            if media['images']: raise ValueError('unsupported_media')
            counts.append({'profile':'official_v41_1017', 'model_version':OFFICIAL_VERSION,
                'repository_revision':OFFICIAL_REVISION, 'mode':mode,
                'encoder_sha256':OFFICIAL_ENCODER_SHA, 'tokenizer_sha256':OFFICIAL_TOKENIZER_SHA,
                'input_tokens':len(tokenizer.encode(prompt, add_special_tokens=False).ids),
                'rendered_prompt_sha256':_sha(prompt.encode('utf-8')),
                'reasoning_effort_requested':requested, 'reasoning_effort_template':effort,
                'provider_bound_certified':False})
    except (ValueError, TypeError, KeyError, AttributeError, AssertionError, RecursionError):
        result.update(input_token_estimate=len(wire), raw_token_estimate=None,
            margin_tokens=0, bound_method='full_wire_bytes_conservative_token_fallback',
            profiles=[], profile_fallback_reason='request_budget_template_unsupported')
        return result
    raw = max(row['input_tokens'] for row in counts)
    # 模板未使用的API控制字段以完整wire字节另行预留，不从样本比值拟合。
    controls = {k:v for k,v in payload.items() if k not in ('messages','tools','response_format')}
    extension = len(json.dumps(controls, ensure_ascii=False, separators=(',', ':')).encode())
    estimate = (raw * 6 + 4) // 5 + 1024 + extension
    result.update(input_token_estimate=estimate, raw_token_estimate=raw,
        margin_tokens=estimate-raw, transport_extension_byte_reserve=extension,
        bound_method='official_dsv41_1017_two_modes_max_20pct_plus1024',
        profiles=counts, tokenizer_version=TOKENIZERS_VERSION,
        gateway_template_certified=False, model_version=OFFICIAL_VERSION)
    return result


def estimate_request(wire):
    """完整JSON wire身份独立于完整messages/tools模板token数，绝不认证网关上界。"""
    payload = _payload(wire)
    if ('provider' in payload or 'extra_body' in payload) and payload['model'] != MODEL:
        raise RequestBudgetError('request_budget_unsupported_transport_model')
    result = {'input_bytes': len(wire), 'request_sha256': _sha(wire),
              'model_id': payload['model'], 'provider_bound_certified': False}
    if payload['model'] == OFFICIAL_MODEL:
        return _estimate_official(payload, wire, result)
    if payload['model'] != MODEL:
        result.update(input_token_estimate=len(wire), raw_token_estimate=None,
            margin_tokens=0, bound_method='full_wire_bytes_conservative_token_fallback', profiles=[])
        return result
    _validate_official_payload(payload)
    tokenizer, profiles = _profiles()
    # The official dictionary also marks structural tokens such as <think>
    # as special=False. All added-token lexemes are reserved here: raw user
    # text cannot be mistaken for a template's structural token during count.
    special_literals = tuple(token.content for token in tokenizer.get_added_tokens_decoder().values()
                             if token.content)
    def scan_literals(value):
        if isinstance(value, str):
            if any(token in value for token in special_literals):
                raise RequestBudgetError('request_budget_literal_special_token_unsupported')
        elif isinstance(value, dict):
            for key, item in value.items():
                scan_literals(key)
                scan_literals(item)
        elif isinstance(value, list):
            for item in value:
                scan_literals(item)
    scan_literals(payload)
    counts = []
    requested_effort = payload.get('reasoning_effort')
    try:
        for name, digest, revision, encoder in profiles:
            # Local rendering compatibility only; preserve the outbound wire.
            # The older base has no low prefix, while 0731 calls it low.
            # These reference projections do not certify an AiPing/provider bound.
            template_effort = 'high' if requested_effort == 'medium' else requested_effort
            if name == 'base' and template_effort == 'low':
                template_effort = None
            compatibility = ('medium_alias_projected_to_high' if requested_effort == 'medium'
                else 'base_low_projected_to_no_prefix' if name == 'base' and requested_effort == 'low'
                else 'unchanged_profile_argument')
            for mode in ('chat', 'thinking'):
                messages = deepcopy(payload['messages'])
                first = next(message for message in messages if message['role'] == 'system')
                if payload.get('tools'):
                    first['tools'] = deepcopy(payload['tools'])
                if payload.get('response_format'):
                    first['response_format'] = deepcopy(payload['response_format'])
                text = encoder.encode_messages(messages, thinking_mode=mode, drop_thinking=False,
                    add_default_bos_token=True, reasoning_effort=template_effort)
                count = len(tokenizer.encode(text, add_special_tokens=False).ids)
                counts.append({'profile': name, 'repository_revision': revision, 'mode': mode,
                    'encoder_sha256': digest, 'tokenizer_sha256': TOKENIZER_SHA,
                    'input_tokens': count, 'rendered_prompt_sha256': _sha(text.encode('utf-8')),
                    'reasoning_effort_requested': requested_effort, 'reasoning_effort_template': template_effort,
                    'estimation_compatibility': compatibility, 'provider_bound_certified': False})
    except (ValueError, TypeError, KeyError, AttributeError, AssertionError, RecursionError):
        raise RequestBudgetError('request_budget_template_unsupported') from None
    raw = max(row['input_tokens'] for row in counts)
    extension = {k: payload[k] for k in ('provider', 'extra_body') if k in payload}
    extension_bytes = len(json.dumps(extension, ensure_ascii=False, separators=(',', ':')).encode()) if extension else 0
    estimated = (raw * 6 + 4) // 5 + 1024 + extension_bytes
    result.update(input_token_estimate=estimated, raw_token_estimate=raw,
        transport_extension_byte_reserve=extension_bytes, margin_tokens=estimated - raw, bound_method='official_dsv4_two_profiles_two_modes_max_20pct_plus1024',
        profiles=counts, tokenizer_version=TOKENIZERS_VERSION,
        gateway_template_certified=False)
    return result


def validate_estimate(value, *, wire=None):
    if (type(value) is not dict or any(type(value.get(key)) is not int or value[key] < 0
            for key in ('input_bytes', 'input_token_estimate')) or
            type(value.get('request_sha256')) is not str or len(value['request_sha256']) != 64
            or value.get('provider_bound_certified') is not False):
        raise RequestBudgetError('request_budget_invalid_estimate')
    if wire is not None and (value['input_bytes'] != len(wire) or value['request_sha256'] != _sha(wire)):
        raise RequestBudgetError('request_budget_wire_identity_mismatch')
    return value
