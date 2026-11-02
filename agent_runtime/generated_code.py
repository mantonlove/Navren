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
import unicodedata
import time
import uuid

from simulated_bank import BankError
from .request_privacy import RequestPrivacyPolicy
from .sandbox_runtime.sandbox import (Limits, SandboxExecutor, Scope,
                                      TrustedDatasetRegistry)

TOOL_NAME = 'run_generated_code'
_IDENTITY_MINOR_ABI_EXAMPLE = '(module\n  (memory (export "memory") 1)\n  (func (export "run") (param $p i32) (param $n i32) (result i64)\n    (i64.store (local.get $p) (i64.load (local.get $p)))\n    (i64.or\n      (i64.shl (i64.const 8) (i64.const 32))\n      (i64.extend_i32_u (local.get $p)))))'
_SUCCESS_EXPLANATION = ('工具成功只表示隔离执行结果与既有指标一致。'
                        '该代码回执不提供金融操作权限。')
TOOL_SPEC = (
    '在受限 WAT worker 中复现本轮已读取、完整且可计算的既有金额指标。'
    '先读取正式指标，只传原 metric_id 和 WAT；不得提交身份、路径、数据、配方或权限。'
    '只支持 identity_minor/1：宿主已把一个小端 i64 输入值放在参数 $p 指向的内存中，'
    '$n 是值的个数，本工具为 1，不是字节数。复现必须读取该输入，不写死金额或地址。'
    '仅导出 memory 和 run(i32输入指针,i32个数)->i64。run 结束时栈只能留下一个 i64；'
    '不要留下额外的独立 const/shl/or 结果。返回值高32位为输出字节数8，低32位为输出地址，'
    '即 (8<<32)|输出指针；若原位写回，输出地址用 $p，而不是常数8。'
    '完整合法的最小 ABI 结构示例，不包含任何业务金额：\n'
    + _IDENTITY_MINOR_ABI_EXAMPLE
    + '\n示例只说明 ABI；模型仍须显式提交本轮 metric_id 和实际 WAT。'
    '成功返回中的 explanation 只在真实成功回执成立时可引用；失败只能说明未形成代码复现结果。'
    '不要用代码回执推断金融状态或复制账户字段清单。'
    '禁止 imports/WASI；原代码大小、内存、fuel、时间与工具额度不变。'
    '结果只核对原指标，不形成新金额事实或授权交易；失败不自动重试。',
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


def _worker_rejected(receipt):
    """仅把受信 worker 的固定阶段/原因映射成安全提示，不回传原异常。"""
    reason = receipt.get('reason') if type(receipt) is dict else None
    phases = receipt.get('worker_phases') if type(receipt) is dict else None
    phase = phases[-1] if type(phases) is list and phases else None
    if reason == 'wasm_validation_compile_or_resource_error' and phase == 'compile':
        message = ('受限 WAT 在编译/验证阶段未通过，本次未形成可用的代码复现结果。'
                   '请按工具中的完整 ABI 检查指令类型和栈：run 结束只能留下一个 i64，'
                   '输出长度为8字节，地址由输入参数 $p 取得，不写死金额或地址。')
    elif reason == 'wasm_validation_compile_or_resource_error' and phase == 'parse_wat':
        message = ('受限 WAT 文本解析未通过，本次未形成可用的代码复现结果。'
                   '请按工具中的完整 module/函数 ABI 检查括号和指令结构。')
    else:
        message = ('受限 WAT 未通过既有沙箱校验，本次未形成可用的代码复现结果。'
                   '请核对工具规定的导出、输入/输出 ABI 和资源限制。')
    raise BankError('GENERATED_CODE_WORKER_REJECTED', message +
                    'imports/WASI及原资源、调用额度保持，不自动重试。', 409)


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


def reproduction_only_intent(text):
    """有限产品意图：复核已签发指标、说明限制且明确无业务操作。"""
    import re
    import unicodedata
    if type(text) is not str or not 1 <= len(text) <= 600:
        return False
    value = unicodedata.normalize('NFKC', text)
    if re.search(r'顺便|另外|再帮|同时帮|并帮|建议|推荐|配置|转账|汇款|买入|卖出|购买|申购|赎回|保本|稳赚|保证.{0,8}收益|收益.{0,8}保证|(?:不要|无需|不必|不想|不需要).{0,8}(?:复现|重现|复核|核验|核对|受限代码|沙箱代码|WAT)', value):
        return False
    return bool(re.search(r'受限代码|沙箱代码|WAT|WebAssembly', value, re.I)
        and re.search(r'复现|重现|复核|核验|核对', value)
        and re.search(r'(?:已签发|已经签发|既有|本轮已读取).{0,8}指标', value)
        and re.search(r'(?:解释|说明|讲清|说清|介绍).{0,10}(?:限制|边界|能力|资源)', value)
        and re.search(r'(?:不|不要|无需|禁止).{0,4}(?:执行|办理|进行|做).{0,4}(?:操作|交易)', value))


_REPRODUCTION_LIMITS_NOTICE = (
    '运行边界为 WAT 单个既有指标的纯计算复现；禁止 imports、WASI、文件、网络、进程、环境变量与金融执行。'
    '代码大小、线性内存、fuel、worker截止和输出大小沿用已登记沙箱限额。'
    'Python、shell、任意新算法和新金融事实不在当前支持范围。')


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
                if receipt['status'] != 'COMPLETED_CANDIDATE':
                    _worker_rejected(receipt)
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
                    'explanation': _SUCCESS_EXPLANATION,
                    'financial_execution_authorized': False, 'new_financial_metric_registered': False,
                    'formal_fresh_and_publication_guard_required': True}
                self._jobs.append({'metric_id': metric_id, 'snapshot': snapshot,
                    'receipt': deepcopy(receipt), 'result': deepcopy(result)})
                return deepcopy(result)
            except Exception as e:
                self._event('GENERATED_CODE_REJECTED',
                            error_code=getattr(e, 'code', type(e).__name__))
                raise

    def canonical_reproduction_answer(self):
        """Only a verified unique model-selected job supplies this host reply.

        This is not a model tool, a new metric or approval. The normal terminal
        tool, full OP/amount/GLM/FinalProof and final freshness remain required.
        """
        tools = self.tools
        if (not reproduction_only_intent(tools.user_text)
                or len(self._jobs) != 1 or not tools.metric_selection_mode
                or tools.changed or tools.drafts or tools.calendar_drafts
                or getattr(tools, '_history_reference', None) is not None):
            return None
        _need(tools.generated_code_session is self and self.g is tools.g and self.w == tools.w,
              'GENERATED_CODE_RESULT_SESSION_MISMATCH')
        self.verify_fresh()
        job = self._jobs[0]
        _need(job['result']['metric_id'] == job['metric_id'],
              'GENERATED_CODE_RESULT_SELECTION_MISMATCH')
        code_evidence = [eid for eid, evidence in tools.evidence.items()
            if evidence.get('source') == 'restricted_generated_code'
            and evidence.get('tool') == 'run_generated_code'
            and evidence.get('context_hash') == tools.start_context
            and evidence.get('data') == job['result']]
        skills = [eid for eid, evidence in tools.evidence.items()
            if evidence.get('tool') == 'load_skill'
            and evidence.get('context_hash') == tools.start_context
            and evidence.get('data', {}).get('skill') == 'code-sandbox']
        _need(len(code_evidence) == 1 and bool(skills),
              'GENERATED_CODE_RESULT_EVIDENCE_REQUIRED')
        parts = [{'text': _SUCCESS_EXPLANATION + '\n本轮所选既有指标的复现金额为：'},
                 {'metric_id': job['metric_id']},
                 {'text': '。\n范围仅为所选单个既有指标的数值复现；原始数据真实性仍依赖原来源与指标签发合同。'
                     '\n' + _REPRODUCTION_LIMITS_NOTICE + '\n本轮未执行任何金融操作。'}]
        # Original signed amount renderer, then the unchanged legacy envelope:
        # no new monetary fact and no model-authored state sentence survives.
        rendered = tools.render_metric_parts({'reply_parts': parts,
            'claims': [], 'draft_ids': [], 'followups': []})
        # These fixed host claims enter the same NFKC typed review as the body.
        # Keep their evidence bindings; normalize before any GLM wire is built.
        rendered['claims'] = [
            {'text': unicodedata.normalize('NFKC', _SUCCESS_EXPLANATION), 'evidence_ids': code_evidence},
            {'text': unicodedata.normalize('NFKC', _REPRODUCTION_LIMITS_NOTICE), 'evidence_ids': skills}]
        return rendered

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
