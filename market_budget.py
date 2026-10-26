"""Market 官方请求独立预算。纯本地账，不代表服务商余额或人工授权。

请求 JSON 的 UTF-8 字节数加 4096 协议余量作为输入 token 保守估计；
按官方峰值未命中价格和完整 max_tokens 预留。账本不保存提示词或密钥。
"""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid

SCHEMA = 'navren-market-budget-v1'
CN = ZoneInfo('Asia/Shanghai')
NANO = 1_000_000_000
INPUT_FRAMING_RESERVE = 4096
OUTPUT_CEILING = 8192
PRICES_NANO = {'deepseek': (2000, 8000), 'zhipu': (800, 2800)}
PHASE_CAPS = {'controlled_recovery': {'deepseek': 18, 'zhipu': 18},
              'automatic_morning': {'deepseek': 24, 'zhipu': 22}}
ROUTES = {'deepseek': ('deepseek-flash', 'https://api.deepseek.com/chat/completions'),
          'zhipu': ('glm-5.3-flash', 'https://open.bigmodel.cn/api/paas/v4/chat/completions')}
_call = ContextVar('navren_market_official_budget_call', default=None)


class MarketBudgetError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _require(ok, code):
    if not ok:
        raise MarketBudgetError(code)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def payload_bytes(payload):
    # 与正式父客户端的 wire 编码一致，包含完整 messages/schema/source/review。
    return json.dumps(payload, ensure_ascii=False).encode('utf-8')


def _money(value):
    _require(isinstance(value, str), 'market_budget_explicit_decimal_limit_required')
    try:
        number = Decimal(value)
        nano = number * NANO
        _require(number.is_finite() and number > 0 and nano == nano.to_integral_value(),
                 'market_budget_invalid_limit')
        return int(nano)
    except (InvalidOperation, ValueError, OverflowError):
        raise MarketBudgetError('market_budget_invalid_limit') from None


def _cny(nano):
    return format(Decimal(nano) / NANO, 'f')


def _safe_label(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and all(ord(c) >= 32 for c in value)


def estimate_request(provider, input_bytes, max_tokens):
    _require(provider in PRICES_NANO, 'market_budget_official_provider_required')
    _require(type(input_bytes) is int and input_bytes > 0, 'market_budget_payload_bytes_required')
    _require(type(max_tokens) is int and 0 < max_tokens <= OUTPUT_CEILING,
             'market_budget_output_limit_out_of_bounds')
    tokens = input_bytes + INPUT_FRAMING_RESERVE
    inp, out = PRICES_NANO[provider]
    cost = tokens * inp + max_tokens * out
    return {'input_bytes': input_bytes, 'input_tokens_upper_estimate': tokens,
            'max_output_tokens': max_tokens, 'reserved_nano_cny': cost,
            'reserved_cny': _cny(cost)}


def plan_from_byte_caps(byte_caps, *, max_tokens=OUTPUT_CEILING):
    """实测完整请求最大字节数/明确上界 -> 最坏计划；不授予花费权限。"""
    estimates = {p: estimate_request(p, byte_caps[p], max_tokens) for p in PRICES_NANO}
    recovery = sum(PHASE_CAPS['controlled_recovery'][p] * estimates[p]['reserved_nano_cny']
                   for p in PRICES_NANO)
    morning = sum(PHASE_CAPS['automatic_morning'][p] * estimates[p]['reserved_nano_cny']
                  for p in PRICES_NANO)
    return {'status': 'cost_plan_only_not_authorization', 'payload_byte_caps': dict(byte_caps),
            'estimate_method': 'full_wire_json_utf8_bytes_plus_4096', 'per_request': estimates,
            'controlled_requests': 36, 'automatic_per_run_requests': 46,
            'automatic_max_runs': 3, 'full_worst_requests': 174,
            'controlled_worst_cny': _cny(recovery), 'automatic_per_run_worst_cny': _cny(morning),
            'full_worst_cny': _cny(recovery + 3 * morning),
            'provider_balance': 'unknown', 'authorization_limit': None}


def plan_from_payloads(payloads_by_provider, *, max_tokens=OUTPUT_CEILING):
    """仅内存预检，不落原文；缺任一角色真实 payload 则不生成伪完整计划。"""
    caps = {}
    for provider in PRICES_NANO:
        values = payloads_by_provider.get(provider)
        _require(isinstance(values, (list, tuple)) and bool(values), 'market_budget_missing_real_payloads')
        caps[provider] = max(len(payload_bytes(p)) for p in values)
    return plan_from_byte_caps(caps, max_tokens=max_tokens)


class MarketBudgetGuard:
    """独立 flock + 原子账；同一控制文件不能静默改额度或换账解释。

    control 必含 budget_id / authority_ref / limit_cny / payload_byte_caps /
    controlled_date / automatic_date；调用方负责核实 authority_ref 的真实授权。
    """
    def __init__(self, *, control, ledger_path, expected_control_sha256):
        _require(isinstance(control, dict) and control.get('schema') == SCHEMA,
                 'market_budget_control_required')
        _require(all(_safe_label(control.get(k)) for k in ('budget_id', 'authority_ref')),
                 'market_budget_authority_metadata_required')
        caps = control.get('payload_byte_caps')
        _require(isinstance(caps, dict) and set(caps) == set(PRICES_NANO)
                 and all(type(v) is int and v > 0 for v in caps.values()),
                 'market_budget_real_payload_caps_required')
        declared_limit = _money(control.get('limit_cny'))
        self.technical_ceiling = None
        self.authorized_monetary_limit = declared_limit
        self.ceiling_basis = 'explicit_caller_monetary_limit'
        if control.get('ceiling_basis') == 'calculated_full_finite_task_plan':
            _require('authorized_monetary_limit_cny' in control,
                     'market_budget_monetary_evidence_field_required')
            self.technical_ceiling = _money(plan_from_byte_caps(caps)['full_worst_cny'])
            _require(declared_limit == self.technical_ceiling,
                     'market_budget_technical_ceiling_must_equal_full_plan')
            supplied_money_cap = control['authorized_monetary_limit_cny']
            self.authorized_monetary_limit = (None if supplied_money_cap is None
                                              else _money(supplied_money_cap))
            self.ceiling_basis = 'technical_computed_ceiling'
        else:
            _require(control.get('ceiling_basis') in (None, 'explicit_caller_monetary_limit'),
                     'market_budget_unknown_ceiling_basis')
            if control.get('authorized_monetary_limit_cny') is not None:
                self.authorized_monetary_limit = min(
                    declared_limit, _money(control['authorized_monetary_limit_cny']))
        self.limit = min(declared_limit, self.authorized_monetary_limit
                         if self.authorized_monetary_limit is not None else declared_limit)
        for field in ('controlled_date', 'automatic_date'):
            try:
                _require(isinstance(control.get(field), str), 'market_budget_date_required')
                datetime.strptime(control[field], '%Y-%m-%d')
            except ValueError:
                raise MarketBudgetError('market_budget_invalid_date') from None
        self.control = json.loads(_dump(control))
        self.control_sha = hashlib.sha256(_dump(control)).hexdigest()
        _require(expected_control_sha256 == self.control_sha, 'market_budget_control_sha_mismatch')
        self.path = Path(ledger_path)
        _require(self.path.parent.is_dir(), 'market_budget_ledger_parent_missing')
        _require(not self.path.is_symlink(), 'market_budget_ledger_symlink_forbidden')

    @classmethod
    def from_control_file(cls, *, control_path, ledger_path, expected_file_sha256):
        raw = Path(control_path).read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == expected_file_sha256,
                 'market_budget_control_file_sha_mismatch')
        control = json.loads(raw)
        return cls(control=control, ledger_path=ledger_path,
                   expected_control_sha256=hashlib.sha256(_dump(control)).hexdigest())

    def _new(self):
        return {'schema': SCHEMA, 'budget_id': self.control['budget_id'],
                'authority_ref': self.control['authority_ref'], 'control_sha256': self.control_sha,
                'caller_control_limit_cny': (_cny(self.authorized_monetary_limit)
                                            if self.authorized_monetary_limit is not None else None),
                'ceiling_basis': self.ceiling_basis,
                'technical_computed_ceiling_cny': (_cny(self.technical_ceiling)
                                                  if self.technical_ceiling is not None else None),
                'authorized_monetary_limit_cny': (_cny(self.authorized_monetary_limit)
                                                if self.authorized_monetary_limit is not None else None),
                'effective_stop_ceiling_cny': _cny(self.limit), 'provider_balance': 'unknown',
                'legacy_aiping': {'request_count': 33, 'usage': 'unknown', 'cost_cny': None,
                                  'metadata_source': 'parent_reported_historical_unknown',
                                  'charged_to_official_budget': False},
                'halted': False, 'requests': []}

    def _atomic(self, data):
        fd, name = tempfile.mkstemp(prefix=self.path.name + '.', dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(_dump(data) + b'\n'); handle.flush(); os.fsync(handle.fileno())
            _require(not self.path.is_symlink(), 'market_budget_ledger_symlink_forbidden')
            os.replace(name, self.path)
            dfd = os.open(self.path.parent, os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        finally:
            if os.path.exists(name): os.unlink(name)

    def _transaction(self, action):
        lock_path = str(self.path) + '.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _require(not self.path.is_symlink(), 'market_budget_ledger_symlink_forbidden')
            data = json.loads(self.path.read_bytes()) if self.path.exists() else self._new()
            _require(data.get('schema') == SCHEMA and data.get('control_sha256') == self.control_sha,
                     'market_budget_existing_ledger_control_mismatch')
            result = action(data)
            self._atomic(data)
            return result
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)

    @staticmethod
    def _charge(row):
        if row['state'] == 'known_usage': return row['known_usage_peak_priced_nano_cny']
        if row['state'] == 'cancelled_not_sent': return 0
        return row['reserved_nano_cny']

    def reserve(self, *, provider, model_id, url, stage, request_id, phase, payload):
        _require(phase in PHASE_CAPS, 'market_budget_phase_required')
        expected_date = self.control['controlled_date' if phase == 'controlled_recovery' else 'automatic_date']
        _require(datetime.now(CN).date().isoformat() == expected_date, 'market_budget_wrong_execution_date')
        _require(re.fullmatch(r'[0-9a-f]{32}', request_id or '') is not None,
                 'market_budget_real_run_id_required')
        _require(ROUTES.get(provider) == (model_id, url), 'market_budget_wrong_official_route')
        _require(stage in (('market.events', 'market.weekly') if provider == 'deepseek' else ('reviewer.market',)),
                 'market_budget_market_stage_required')
        _require(not (phase == 'controlled_recovery' and stage == 'market.weekly'),
                 'market_budget_controlled_hotspots_only')
        wire = payload_bytes(payload)
        _require(payload.get('model') == model_id, 'market_budget_payload_model_mismatch')
        estimate = estimate_request(provider, len(wire), payload.get('max_tokens'))
        _require(len(wire) <= self.control['payload_byte_caps'][provider], 'market_budget_payload_cap_exceeded')
        rid = uuid.uuid4().hex
        def action(data):
            _require(not data['halted'], 'market_budget_halted')
            active = [r for r in data['requests'] if r['state'] != 'cancelled_not_sent']
            runs = {r['run_id'] for r in active if r['phase'] == phase}
            max_runs = 1 if phase == 'controlled_recovery' else 3
            _require(request_id in runs or len(runs) < max_runs, 'market_budget_phase_run_cap_reached')
            used = sum(r['phase'] == phase and r['run_id'] == request_id and r['provider'] == provider for r in active)
            _require(used < PHASE_CAPS[phase][provider], 'market_budget_request_cap_reached')
            committed = sum(self._charge(r) for r in data['requests'])
            _require(committed + estimate['reserved_nano_cny'] <= self.limit,
                     'market_budget_money_cap_reached')
            row = {'id': rid, 'run_id': request_id, 'phase': phase, 'stage': stage,
                   'provider': provider, 'model_id': model_id,
                   'created_at': datetime.now(CN).isoformat(), 'state': 'reserved_not_sent',
                   'payload_sha256': hashlib.sha256(wire).hexdigest(), **estimate,
                   'actual_usage': None, 'actual_cost_cny': None}
            data['requests'].append(row)
            return dict(row)
        return self._transaction(action)

    def mark_attempted(self, rid):
        def action(data):
            row = next(r for r in data['requests'] if r['id'] == rid)
            _require(row['state'] == 'reserved_not_sent', 'market_budget_duplicate_transport_forbidden')
            row.update(state='transport_attempted', attempted_at=datetime.now(CN).isoformat())
        self._transaction(action)

    def settle(self, rid, usage):
        def action(data):
            row = next(r for r in data['requests'] if r['id'] == rid)
            if row['state'] == 'reserved_not_sent':
                row.update(state='cancelled_not_sent', usage_unknown=False)
            elif row['state'] == 'transport_attempted':
                known = (isinstance(usage, dict)
                         and all(type(usage.get(k)) is int and usage[k] >= 0
                                 for k in ('prompt_tokens', 'completion_tokens'))
                         and ('total_tokens' not in usage or usage['total_tokens'] ==
                              usage['prompt_tokens'] + usage['completion_tokens']))
                if not known:
                    row.update(state='unknown_usage_reserved', usage_unknown=True)
                else:
                    inp, out = PRICES_NANO[row['provider']]
                    cost = usage['prompt_tokens'] * inp + usage['completion_tokens'] * out
                    row.update(state='known_usage', usage_unknown=False,
                               actual_usage={k: usage[k] for k in ('prompt_tokens', 'completion_tokens')},
                               known_usage_peak_priced_nano_cny=cost,
                               known_usage_peak_priced_cny=_cny(cost))
                    # 实际账单费用仍未知；峰值估价不能冒充发票/账户扣款。
                    if (usage['prompt_tokens'] > row['input_tokens_upper_estimate'] or
                            usage['completion_tokens'] > row['max_output_tokens']):
                        data['halted'] = True
                        row['bound_exceeded'] = True
                row['settled_at'] = datetime.now(CN).isoformat()
            else:
                raise MarketBudgetError('market_budget_already_settled')
        self._transaction(action)

    def status(self):
        def action(data):
            committed = sum(self._charge(r) for r in data['requests'])
            return {'budget_id': data['budget_id'],
                    'caller_control_limit_cny': data['caller_control_limit_cny'],
                    'ceiling_basis': data['ceiling_basis'],
                    'technical_computed_ceiling_cny': data['technical_computed_ceiling_cny'],
                    'authorized_monetary_limit_cny': data['authorized_monetary_limit_cny'],
                    'effective_stop_ceiling_cny': data['effective_stop_ceiling_cny'],
                    'committed_peak_or_reserved_cny': _cny(committed),
                    'accounting_remaining_cny': _cny(self.limit - committed),
                    'provider_balance': 'unknown', 'halted': data['halted'],
                    'counts': {state: sum(r['state'] == state for r in data['requests']) for state in
                               ('known_usage', 'unknown_usage_reserved', 'reserved_not_sent',
                                'transport_attempted', 'cancelled_not_sent')},
                    'legacy_aiping': data['legacy_aiping']}
        return self._transaction(action)


class _UsageResponse:
    def __init__(self, response, note): self.response, self.note = response, note
    def __getattr__(self, name): return getattr(self.response, name)
    def __enter__(self): self.response.__enter__(); return self
    def __exit__(self, *args): return self.response.__exit__(*args)
    def read(self, *args, **kwargs):
        raw = self.response.read(*args, **kwargs)
        try:
            envelope = json.loads(raw.decode('utf-8'))
            if isinstance(envelope, dict): self.note['usage'] = envelope.get('usage')
        except (ValueError, UnicodeError, AttributeError): pass
        return raw


class MarketBudgetMixin:
    """放在原 MarketJsonClient 之前；不复制生成/审核/重试/响应合同。"""
    def __init__(self, *, budget_guard=None, budget_phase=None, **kwargs):
        self.budget_guard, self.budget_phase = budget_guard, budget_phase
        super().__init__(**kwargs)
        _require(self.max_retries == 0, 'market_budget_retries_must_be_zero')
        transport = self.urlopen
        def guarded_transport(request, **transport_kwargs):
            note = _call.get()
            _require(note is not None and note['client'] is self, 'market_budget_transport_without_reservation')
            _require(request.full_url == self.url and request.get_method() == 'POST'
                     and hashlib.sha256(request.data).hexdigest() == note['reservation']['payload_sha256'],
                     'market_budget_wire_payload_changed')
            self.budget_guard.mark_attempted(note['reservation']['id'])
            return _UsageResponse(transport(request, **transport_kwargs), note)
        self.urlopen = guarded_transport

    def _request_payload(self, *, system, user, stage):
        payload = super()._request_payload(system=system, user=user, stage=stage)
        note = _call.get()
        if note is not None and note['client'] is self:
            _require(hashlib.sha256(payload_bytes(payload)).hexdigest() ==
                     note['reservation']['payload_sha256'], 'market_budget_payload_changed_after_reservation')
        return payload

    def complete_json(self, *, system, user, stage, request_id):
        _require(isinstance(self.budget_guard, MarketBudgetGuard), 'market_budget_control_required')
        # 先读取最终 Market payload，预算失败在原客户端/usage log 之前发生。
        payload = self._request_payload(system=system, user=user, stage=stage)
        reservation = self.budget_guard.reserve(provider=self.provider, model_id=self.model_id,
            url=self.url, stage=stage, request_id=request_id, phase=self.budget_phase, payload=payload)
        note = {'client': self, 'reservation': reservation, 'usage': None}
        token = _call.set(note)
        try:
            return super().complete_json(system=system, user=user, stage=stage, request_id=request_id)
        finally:
            _call.reset(token)
            self.budget_guard.settle(reservation['id'], note['usage'])
