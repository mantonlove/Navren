"""受信宿主批准的已发行指标复现；唯一 guest 输入为 WAT。

不发行指标、不决定交易或权限、不执行 Python/shell。计算结果仍须由原
金额声明、GLM、FinalProof 和实际发送守卫消费。本模块不打开正式库路径。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from simulated_bank import BankError
from .request_privacy import RequestPrivacyPolicy
from .sandbox_runtime.sandbox import (Limits, SandboxExecutor, Scope,
                                      TrustedDatasetRegistry)

TOOL_NAME = 'run_generated_code'
TOOL_SPEC = (
    '在受限 WAT worker 中复现本轮已读取、完整且可计算的既有金额指标。'
    '先读取正式指标，只传原 metric_id 和 WAT；不得提交身份、路径、数据、配方或权限。'
    'WAT 必须仅导出 memory 和 run(i32输入指针,i32个数)->i64，'
    '将单个输入 i64 原样写入内存，返回 (输出字节数<<32)|输出地址。'
    '禁止 imports/WASI；结果只核对原指标，不形成新金额事实或授权交易。',
    {'type': 'object', 'properties': {
        'metric_id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
        'wat': {'type': 'string', 'minLength': 1, 'maxLength': 65536}},
     'required': ['metric_id', 'wat'], 'additionalProperties': False})


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _need(condition, code):
    if not condition:
        raise BankError(code, '受限代码或原指标的权限、来源、计算条件未满足。', 409)


def _knowledge(tools):
    reads = [e['data'] for e in tools.evidence.values()
             if e.get('tool') == 'knowledge_search' and e.get('data', {}).get('hits')]
    if not reads:
        return None
    _need(len({r['public_release_id'] for r in reads}) == 1,
          'GENERATED_CODE_KNOWLEDGE_CONFLICT')
    return {'release_id': reads[0]['public_release_id'], 'citations': [
        {'citation_id': h['citation_id'], 'as_of': r['as_of']}
        for r in reads for h in r['hits']]}


class GeneratedCodeSession:
    """由本轮 BankTools 和受信请求能力构造；模型不能构造本对象。

    execute 只做 identity_minor/1，原 worker 一次执行/一次消费。
    verify_for_publication 使用调用方已持有的银行 db，不开启嵌套事务。
    close 应在实际交付完成或失败后由宿主调用，不能提早代替发布复验。
    """
    def __init__(self, tools, *, audit_dir, deadline, cancel=None):
        from .bank_tools import BankTools
        _need(isinstance(tools, BankTools), 'GENERATED_CODE_HOST_TOOLS_REQUIRED')
        _need(type(tools.request_privacy) is RequestPrivacyPolicy,
              'GENERATED_CODE_PRIVACY_REQUIRED')
        _need(type(deadline) in (int, float) and math.isfinite(deadline),
              'GENERATED_CODE_DEADLINE_REQUIRED')
        _need(cancel is None or isinstance(cancel, threading.Event),
              'GENERATED_CODE_HOST_CANCEL_REQUIRED')
        capture = getattr(tools.g, 'capture_request_binding', None)
        validate = getattr(tools.g, 'revalidate_request_binding', None)
        _need(callable(capture) and callable(validate),
              'GENERATED_CODE_REQUEST_BINDING_UNAVAILABLE')
        self.tools, self.g, self.w = tools, tools.g, tools.w
        self.policy = tools.request_privacy
        self.binding = capture(self.w)
        _need(self.binding is not None and type(self.binding) is not bool,
              'GENERATED_CODE_REQUEST_BINDING_REQUIRED')
        self._validate_binding = validate
        self.deadline, self.cancel = deadline, cancel
        self._thread = threading.get_ident()
        self._active = True
        self._lock = threading.RLock()
        self._jobs = []
        self._datasets = TrustedDatasetRegistry()
        self._audit_dir = Path(audit_dir)
        self._audit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._personal = self.g.personal_data.revision(self.w)
        self._pins = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (
            Path(__file__), Path(__file__).parent/'sandbox_runtime/sandbox.py',
            Path(__file__).parent/'sandbox_runtime/worker.py',
            Path(__file__).parent/'sandbox_runtime/limits.json')}
        self._live()

    def _live(self):
        _need(self._active and threading.get_ident() == self._thread,
              'GENERATED_CODE_REQUEST_NOT_ACTIVE')
        _need(time.monotonic() < self.deadline,
              'GENERATED_CODE_REQUEST_EXPIRED')
        _need(self.cancel is None or not self.cancel.is_set(),
              'GENERATED_CODE_CANCELLED')
        _need(self.tools.g is self.g and self.tools.w == self.w
              and self.tools.request_privacy == self.policy
              and self.tools.private_context_disabled == self.policy.private_context_disabled,
              'GENERATED_CODE_HOST_CONTEXT_CHANGED')
        self.policy.require_tool('account_summary')
        _need(importlib.metadata.version('wasmtime') == '49.0.0',
              'GENERATED_CODE_RUNTIME_CHANGED')
        _need(all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha
                  for p, sha in self._pins.items()), 'GENERATED_CODE_SOURCE_CHANGED')

    def _event(self, event, **details):
        row = {'schema': 'navren.generated-code-audit/1', 'event_id': uuid.uuid4().hex,
               'request_id': self.tools.request_id, 'event': event,
               'financial_execution_authorized': False,
               'new_financial_metric_registered': False, **details}
        fd = os.open(self._audit_dir/(row['event_id']+'.json'),
                     os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(_canonical(row)+b'\n'); f.flush(); os.fsync(f.fileno())
        fd = os.open(self._audit_dir, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def _guard_args(self):
        return {'market_snapshot': self.tools.market_snapshot(),
                'service_snapshot': self.tools.service_snapshot(),
                'metric_batches': deepcopy(self.tools.metric_batches)}

    def _verify_binding(self, db):
        self._live()
        checked = self._validate_binding(self.binding, bank_db=db)
        _need(isinstance(checked, dict) and checked.get('workspace_id') == self.w
              and checked.get('privacy_revision') == self.tools.privacy_revision
              and checked.get('same_bank_transaction') is True
              and checked.get('financial_execution_authority') is False,
              'GENERATED_CODE_REQUEST_BINDING_CHANGED')
        self.g.assert_response_evidence(self.w, self.tools.privacy_revision,
            self._personal, _knowledge(self.tools), self.tools.start_context,
            db=db, **self._guard_args())
        self._live()  # 来源核验可能耗时；原截止不能被迟到的 validator 放行。

    @contextmanager
    def _guard(self):
        self._live()
        with self.g.response_guard(self.w, self.tools.privacy_revision, self._personal,
                _knowledge(self.tools), self.tools.start_context, **self._guard_args()) as db:
            self._verify_binding(db)
            yield db
            self._live()

    def _snapshot(self, metric_id, db):
        self._verify_binding(db)
        record = self.tools.metrics.lookup(metric_id)
        m = record['metric']
        batches = [b for b in self.tools.metric_batches if any(
            isinstance(v, dict) and v.get('metric_id') == metric_id
            for v in b.get('metrics', []))]
        _need(len(batches) == 1, 'GENERATED_CODE_ORIGINAL_ISSUANCE_REQUIRED')
        batch = batches[0]
        _need(batch.get('tool_name') not in (
            'accounting_report', 'returns_report', 'allocation_preview'),
            'GENERATED_CODE_EXTERNAL_SOURCE_GUARD_REQUIRED')
        observer = getattr(self.g.financial_metrics, 'batches', None)
        _need(isinstance(observer, list) and batch in observer,
              'GENERATED_CODE_ORIGINAL_OBSERVER_REQUIRED')
        self.g.financial_metrics.validate(self.w, batch, db=db)
        issued = [v for v in batch['metrics'] if v['metric_id'] == metric_id]
        _need(len(issued) == 1 and issued[0] == m
              and batch['metric_kinds'].get(metric_id) == record['kind'],
              'GENERATED_CODE_ISSUANCE_MISMATCH')
        owner = db.execute('SELECT workspace_id FROM accounts WHERE id=?',
                           (m['subject_id'],)).fetchone()
        _need(m['subject_id'] == self.w or (owner and owner['workspace_id'] == self.w),
              'GENERATED_CODE_SUBJECT_DENIED')
        _need(m['unit'] == 'minor_currency' and m['calculation_status'] == 'calculated'
              and not m['coverage_gaps'] and not m['assumed_inputs']
              and type(m['value_minor']) is int
              and -(1<<63) <= m['value_minor'] < (1<<63)
              and set(m['input_refs']) <= set(m['source_versions']),
              'GENERATED_CODE_METRIC_REPRODUCTION_GAP')
        value = m['value_minor']
        amount = ('-' if value < 0 else '')+f'{abs(value)//100}.{abs(value)%100:02d} '+m['currency']
        claims = self.tools.metrics.validate_claims([
            {**self.tools.metrics.binding(metric_id), 'text': amount}],
            current_source_versions=m['source_versions'])
        _need(claims[0]['metric'] == m, 'GENERATED_CODE_REGISTRY_CHANGED')
        self._live()
        return {'record': deepcopy(record), 'batch': deepcopy(batch),
                'policy': self.policy.private_context_disabled,
                'request_id': self.tools.request_id, 'operation': 'identity_minor/1'}

    def execute(self, args):
        with self._lock:
            try:
                self._live()
                _need(type(args) is dict and set(args) == {'metric_id', 'wat'}
                      and isinstance(args['metric_id'], str) and 1 <= len(args['metric_id']) <= 128
                      and isinstance(args['wat'], str) and 1 <= len(args['wat']) <= 65536
                      and len(args['wat'].encode('utf-8')) <= 65536,
                      'GENERATED_CODE_ARGUMENTS_INVALID')
                metric_id, wat = args['metric_id'], args['wat']
                with self._guard() as db:
                    snapshot = self._snapshot(metric_id, db)
                scope = Scope(self.w, _sha({'request': self.tools.request_id,
                    'binding': id(self.binding), 'job': uuid.uuid4().hex}))
                binding_sha = _sha(snapshot)
                handle = self._datasets.admit_copy(scope, binding_sha,
                    [snapshot['record']['metric']['value_minor']], unit='minor')
                # 原默认所有限制保持；请求所剩时限只能缩短 5 秒 worker 截止。
                left = min(5.0, self.deadline-time.monotonic())
                _need(left > 0, 'GENERATED_CODE_REQUEST_EXPIRED')
                executor = SandboxExecutor(self._datasets, self._audit_dir/'worker-receipts',
                    limits=Limits(total_seconds=left))
                receipt = executor.execute(scope, handle, wat, cancel=self.cancel)
                self._live()
                _need(receipt['status'] == 'COMPLETED_CANDIDATE',
                      'GENERATED_CODE_WORKER_REJECTED')
                with self._guard() as db:
                    current = self._snapshot(metric_id, db)
                    _need(current == snapshot, 'GENERATED_CODE_AUTHORIZED_SNAPSHOT_CHANGED')
                    candidate = executor.consume_candidate(receipt, scope, handle, wat)
                    _need(candidate['unit'] == 'minor' and candidate['values'] == [
                        snapshot['record']['metric']['value_minor']],
                        'GENERATED_CODE_RESULT_MISMATCH')
                    self._event('REGISTERED_METRIC_REPRODUCED',
                        execution_id=receipt['execution_id'], code_sha256=receipt['code_sha256'],
                        binding_sha256=binding_sha, receipt_sha256=_sha(receipt))
                result = {'schema': 'navren.generated-code-result/1',
                    'kind': 'registered_metric_reproduction', 'operation': 'identity_minor/1',
                    'metric_id': metric_id, 'execution_id': receipt['execution_id'],
                    'code_sha256': receipt['code_sha256'], 'binding_sha256': binding_sha,
                    'receipt_sha256': _sha(receipt), 'independent_review_required': True,
                    'financial_execution_authorized': False, 'new_financial_metric_registered': False,
                    'formal_fresh_and_publication_guard_required': True}
                self._jobs.append({'metric_id': metric_id, 'snapshot': snapshot,
                    'receipt': deepcopy(receipt), 'result': deepcopy(result)})
                return deepcopy(result)
            except Exception as e:
                self._event('GENERATED_CODE_REJECTED',
                            error_code=getattr(e, 'code', type(e).__name__))
                raise

    def verify_for_publication(self, bank_db):
        """同 DB 的完整末端复验；没有事务/写入/新的指标或权限。"""
        with self._lock:
            self._verify_binding(bank_db)
            for job in self._jobs:
                _need(self._snapshot(job['metric_id'], bank_db) == job['snapshot'],
                      'GENERATED_CODE_AUTHORIZED_SNAPSHOT_CHANGED')
                _need(_sha(job['receipt']) == job['result']['receipt_sha256'],
                      'GENERATED_CODE_RECEIPT_CHANGED')
                r = job['receipt']
                _need(r['status'] == 'COMPLETED_CANDIDATE' and r['worker_reaped']
                      and r['worker_group_absent'] and r['temporary_directory_removed'],
                      'GENERATED_CODE_WORKER_NOT_CLOSED')
            self._live()
            return True

    def verify_fresh(self):
        with self._guard() as db:
            return self.verify_for_publication(db)

    def receipt_refs(self):
        return [deepcopy(job['result']) for job in self._jobs]

    def close(self):
        with self._lock:
            self._active = False
            self.binding = None
            self._datasets = None
