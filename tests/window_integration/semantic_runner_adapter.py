"""冻结语义执行器的临时SQLite公共夹具适配；不是生产RAG资料。

CLI原样转交旧runner全部参数。本文件不修改旧脚本、题意、金融预期或裁判。
public_policy夹具使用真实db()锁；原Trace仅内存兼容原生审核kwargs，保留实际用量。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
if str(Path(__file__).resolve().parent) not in sys.path:sys.path.insert(0,str(Path(__file__).resolve().parent))
from registered_entry_context import wrap_semantic_server,wrap_semantic_request,request_context


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "scripts/run_skill_acceptance.py"
GOLDEN_PATH = ROOT / "evaluation/navren_skill_semantic_cases.json"
RUNNER_SHA256 = "a4bc9a7631e89cc68f9a1a0d506563daa848de31874aeb6bf20890cb1a34e0d8"
GOLDEN_SHA256 = "f92627f6dcd96a979d17d805dc5a219a47fcacc0a9e647c6f6e1a421f62ec0e6"
ADAPTER_VERSION = "frozen-synthetic-public-sqlite/2"
_RUNNER = None
_ADAPTER_INPUTS = None


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json_text(value):
    # JSON本身就是冻结夹具合同：保留null/bool/int/str/list/dict类型，禁止NaN。
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if _types(json.loads(result)) != _types(value):
        raise ValueError("fixture_not_lossless_json")
    return result


def _types(value):
    if isinstance(value, dict):
        return ("dict", tuple(sorted((key, _types(item)) for key, item in value.items())))
    if isinstance(value, list):
        return ("list", tuple(_types(item) for item in value))
    return (type(value).__name__, value)


def _temporary_parent(bank_path):
    bank_path = Path(bank_path).resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    parent = bank_path.parent
    if parent == temporary_root or not parent.is_relative_to(temporary_root) or not parent.is_dir():
        raise ValueError("synthetic_public_provider_requires_case_temporary_directory")
    return parent


class SQLiteEvalPublicProvider:
    """仅四类冻结公共JSON；所有方法从同库回查，锁内复用同一连接。

    初始输出与旧EvalPublicProvider逐字段/逐类型相同。信任方在临时库修订或
    撤回后，最终public_guard回查会拒绝原摘要；不锁私人库、不复制题目正文。
    """
    def __init__(self, legacy_provider, bank_path):
        parent = _temporary_parent(bank_path)
        fd, name = tempfile.mkstemp(prefix="semantic-public-", suffix=".sqlite3", dir=parent)
        os.close(fd)
        self._path = Path(name)
        self._local = threading.local()
        self.fixture_kind = "frozen_synthetic_public_policy_only"
        payloads = {
            "status": legacy_provider.status(),
            "search": deepcopy(legacy_provider.fixture["search_result"]),
            "canonical": deepcopy(legacy_provider.fixture["canonical_evidence"]),
            "public_catalog": legacy_provider.public_catalog(),
        }
        try:
            with self.db() as db:
                db.execute("CREATE TABLE eval_public_payload(kind TEXT PRIMARY KEY CHECK(kind IN ('status','search','canonical','public_catalog')), payload TEXT NOT NULL CHECK(json_valid(payload)))")
                db.executemany("INSERT INTO eval_public_payload VALUES(?,?)", [(kind, _json_text(value)) for kind, value in payloads.items()])
            self.path.chmod(0o600)
        except Exception:
            self.path.unlink(missing_ok=True)
            raise

    @property
    def path(self):
        return self._path

    @contextmanager
    def db(self):
        _temporary_parent(self.path)  # 也拒绝创建后被换到非临时目录的符号链接。
        # BEGIN IMMEDIATE由实际public_guard执行；这里绝不提前提交其锁。
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        self._local.connection = db
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            self._local.connection = None
            db.close()

    @staticmethod
    def _read(db, kind):
        row = db.execute("SELECT payload FROM eval_public_payload WHERE kind=?", (kind,)).fetchone()
        if row is None:
            raise ValueError("synthetic_public_payload_missing")
        return json.loads(row[0])

    def status(self):
        with self.db() as db:
            return self._read(db, "status")

    def public_catalog(self):
        with self.db() as db:
            return self._read(db, "public_catalog")

    def search_public(self, request, release_id):
        if request["scope"] != "public":
            raise ValueError("not_public")
        with self.db() as db:
            status = self._read(db, "status")
            if release_id != status["public_release_id"]:
                return {"schema_version": 1, "release_id": release_id, "citation_ids": [], "coverage_gaps": []}
            return self._read(db, "search")

    def resolve_public_evidence(self, citation_id, release_id, as_of):
        with self.db() as db:
            row = self._read(db, "canonical")
            if (citation_id != row["citation_id"] or release_id != row["release_id"]
                    or not row["effective_from"] <= as_of < row["effective_to"]):
                return None
            # 初始原值不变；撤回/出站取消不能仅靠固定发布绕过。
            if (row.get("withdrawn") is not False or row.get("outbound_allowed") is not True
                    or row.get("visibility") != "public" or row.get("content_status") != "full_text"):
                return None
            return row


class EvalCatalogLifecycleAdapter:
    """保留冻结目录内容，并将停机交给原真实目录实例。"""
    def __init__(self, fixture_catalog, lifecycle_owner):
        self._fixture_catalog = fixture_catalog
        self._lifecycle_owner = lifecycle_owner

    def read(self, query="", limit=12):
        return self._fixture_catalog.read(query=query, limit=limit)

    def stop(self, timeout=2):
        stop = getattr(self._lifecycle_owner, "stop", None)
        if callable(stop):
            return stop(timeout=timeout)
        if getattr(self._lifecycle_owner, "_navren_no_worker_fixture", False) is True:
            return True
        raise AttributeError("legacy_catalog_lifecycle_stop_unavailable")


class FrozenSemanticEvidenceRuntime:
    """将原题提供器接入真实HTTP轮快照；不抓取网络或借用现役公共库。"""
    def __init__(self, provider):
        self.provider = provider

    def prepare_round(self, text):
        from local_knowledge.runtime import PreparedEvidence, RoundProvider
        class FrozenRound(RoundProvider):
            def public_catalog(self, *, limit=5000):
                # 原eval协议没有limit参数；不查询RoundProvider的生产图表。
                return self.provider.public_catalog()
        status = self.provider.status()
        release = status['public_release_id']
        return PreparedEvidence(FrozenRound(self.provider, release_id=release), {
            'mode': 'original97_frozen_semantic_fixture',
            'base_release_id': release, 'release_id': release,
            'cold_fetch_enabled': False, 'temporary': False,
            'semantic_validation': False})

    def close(self):
        pass


class _LocalJsonResponse:
    def __init__(self, value):
        self.status = 200
        self._body = json.dumps(value, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self._body if limit is None or limit < 0 else self._body[:limit]


def _install_preflight_glm_wire(module):
    """在本地执行真实GLM审核请求适配器；传输只到确定性内存响应。"""
    from agent_runtime.conversation import GLMConversationReviewer
    from agent_runtime.model_adapters import OpenAICompatibleJsonClient, GLM_REVIEW_MODEL

    reviewer_class = module.ProcessPreflightReviewer
    original_init = reviewer_class.__init__
    original_review = reviewer_class.review
    local_url = "https://offline-preflight.invalid/v1/chat/completions"

    def preflight_init(self, log_path=None):
        original_init(self, log_path)
        self._offline_decision = threading.local()
        self._offline_request_metadata = []

        def local_open(request, *, timeout=None, **kwargs):
            if (getattr(request, "full_url", None) != local_url
                    or request.get_method() != "POST"
                    or not isinstance(getattr(request, "data", None), bytes)):
                raise ValueError("offline_preflight_request_contract_invalid")
            payload = json.loads(request.data.decode("utf-8"))
            if payload.get("model") != GLM_REVIEW_MODEL:
                raise ValueError("offline_preflight_model_binding_invalid")
            decision = getattr(self._offline_decision, "value", None)
            if type(decision) is not dict:
                raise ValueError("offline_preflight_decision_unbound")
            self._offline_request_metadata.append({
                "method": request.get_method(), "url": local_url,
                "request_bytes": len(request.data),
                "request_sha256": hashlib.sha256(request.data).hexdigest(),
                "transport": "in_process_fixture_no_network",
                "billing_mode": "unpaid_fixture"})
            response = {"id": "offline-preflight-review", "model": GLM_REVIEW_MODEL,
                "choices": [{"finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(decision, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
            return _LocalJsonResponse(response)

        client = OpenAICompatibleJsonClient(provider="ai_ping", model_id=GLM_REVIEW_MODEL,
            url=local_url, env_name="NAVREN_OFFLINE_FIXTURE_KEY",
            keychain_service="CC/NAVREN_OFFLINE_FIXTURE_KEY", timeout=35, max_retries=0,
            urlopen=local_open, key_loader=lambda: "offline-fixture-only", log_path=os.devnull)
        self._wire_reviewer = GLMConversationReviewer(client=client)
        self._wire_reviewer.billing_mode = "unpaid_fixture"

    def preflight_review(self, packet, *args, **kwargs):
        fixed = original_review(self, packet, *args, **kwargs)
        expected = {key: fixed[key] for key in ("verdict", "reason", "issues")}
        self._offline_decision.value = expected
        try:
            result = self._wire_reviewer.review(packet, *args, **kwargs)
        finally:
            self._offline_decision.value = None
        if any(result.get(key) != value for key, value in expected.items()):
            raise ValueError("offline_preflight_review_decision_changed")
        if (not isinstance(result.get("_agent_review_wire"), bytes)
                or not result["_agent_review_wire"]
                or not self._offline_request_metadata):
            raise ValueError("offline_preflight_real_request_wire_missing")
        return result

    reviewer_class.__init__ = preflight_init
    reviewer_class.review = preflight_review
    reviewer_class.model_id = GLM_REVIEW_MODEL
    reviewer_class.billing_mode = "unpaid_fixture"

def _verify_originals():
    if digest(RUNNER_PATH) != RUNNER_SHA256:
        raise ValueError("frozen_legacy_runner_source_changed")
    if digest(GOLDEN_PATH) != GOLDEN_SHA256:
        raise ValueError("frozen_original_golden_dataset_changed")


def _runner_assignment_signature(node):
    value = node.value
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return value.func.id
    return None


def _rewrite_actual_agent_assignments(source):
    """只在内存将冻结runner的两个Agent重绑点路由到本适配器。"""
    tree = ast.parse(source, filename=str(RUNNER_PATH))
    replacements = []

    class Rewriter(ast.NodeTransformer):
        def visit_Assign(self, node):
            targets = [target for target in node.targets
                       if isinstance(target, ast.Attribute)
                       and target.attr == 'conversation_agent'
                       and isinstance(target.value, ast.Name)
                       and target.value.id == 'server']
            if not targets:
                return self.generic_visit(node)
            if len(node.targets) != 1 or len(targets) != 1:
                raise ValueError('frozen_runner_agent_assignment_shape_changed')
            signature = _runner_assignment_signature(node)
            if signature not in {'tested_agent', 'ConversationAgent'}:
                raise ValueError('frozen_runner_agent_assignment_rhs_changed')
            replacements.append(signature)
            call = ast.Call(func=ast.Name(id='_bind_actual_agent', ctx=ast.Load()),
                args=[ast.Name(id='server', ctx=ast.Load()), node.value], keywords=[])
            return ast.copy_location(ast.Expr(value=call), node)

    tree = Rewriter().visit(tree)
    if (replacements.count('tested_agent') != 1
            or replacements.count('ConversationAgent') != 1
            or len(replacements) != 2):
        raise ValueError('frozen_runner_agent_assignment_count_changed')
    ast.fix_missing_locations(tree)
    return tree, tuple(replacements)


def _bind_actual_agent(server, agent):
    """同实例绑定对话Agent与发布validator，所有原校验仍执行。"""
    validator = getattr(agent, 'validate_final_response', None)
    validator_function = getattr(validator, '__func__', None)
    approved = getattr(getattr(server, 'finance', None), 'approved_responses', None)
    if (approved is None or not callable(validator) or validator_function is None
            or getattr(validator, '__self__', None) is not agent):
        raise RuntimeError('actual_agent_validator_unavailable_or_unbound')
    server.conversation_agent = agent
    approved.validator = validator
    bound = getattr(approved, 'validator', None)
    if (getattr(bound, '__self__', None) is not agent
            or getattr(bound, '__func__', None) is not validator_function):
        raise RuntimeError('actual_agent_validator_binding_mismatch')
    return agent


def _helper_inputs():
    paths = (Path(__file__).resolve(), ROOT / "tests/window_integration/test_semantic_runner_adapter.py",
             ROOT / "tests/window_integration/public_probe_runner.py",
             ROOT / "tests/window_integration/paid_campaign.py",
             ROOT / "tests/window_integration/registered_entry_context.py",
             ROOT / "tests/window_integration/registered_fixture_template.py",
             ROOT / "tests/window_integration/registered_profile_templates.py")
    return {str(path.relative_to(ROOT)): digest(path) for path in paths}


def compatible_trace_reviewer(original_class, safe_data):
    """保留原Trace构造，只将真实原生review的kwargs与记录补齐。"""
    from agent_runtime.conversation import GLMConversationReviewer
    if not isinstance(original_class, type) or not issubclass(original_class, GLMConversationReviewer) or not callable(safe_data):
        raise ValueError("native_trace_reviewer_contract_invalid")

    class CompatibleTraceReviewer(original_class):
        def review(self, packet, *args, **kwargs):
            result = GLMConversationReviewer.review(self, packet, *args, **kwargs)
            decision = deepcopy(result)
            wire = decision.get("_agent_review_wire")
            artifact = None
            if "_agent_review_wire" in decision:
                if not isinstance(wire, bytes) or not wire:
                    raise ValueError("native_review_journal_wire_invalid")
                log_path = getattr(self.client, "log_path", None)
                if not log_path:
                    raise ValueError("native_review_journal_log_path_required")
                directory = Path(log_path).resolve().parent
                artifact = directory / ("native-review-wire-" + hashlib.sha256(wire).hexdigest() + ".json")
                decision["_agent_review_wire"] = {"path": str(artifact), "bytes": len(wire),
                    "sha256": hashlib.sha256(wire).hexdigest(), "storage": "original_native_request_bytes"}
            record = {"packet": deepcopy(safe_data(packet)), "decision": deepcopy(safe_data(decision))}
            # Only the known wire has a binary artifact contract. Any other bytes
            # or non-JSON value fails closed rather than silently stringifying it.
            json.dumps(record, ensure_ascii=False, allow_nan=False)
            if artifact is not None:
                if artifact.exists() or artifact.is_symlink():
                    if artifact.is_symlink() or not artifact.is_file() or artifact.read_bytes() != wire:
                        raise ValueError("native_review_journal_artifact_changed")
                else:
                    fd, temporary = tempfile.mkstemp(prefix=".native-review-wire-", dir=artifact.parent)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(wire); stream.flush(); os.fsync(stream.fileno())
                        os.replace(temporary, artifact)
                        directory_fd = os.open(artifact.parent, os.O_RDONLY)
                        try: os.fsync(directory_fd)
                        finally: os.close(directory_fd)
                    finally:
                        if os.path.exists(temporary): os.unlink(temporary)
            self.records.append(record)
            return result

    return CompatibleTraceReviewer


def _install_current_baseline_agent(module):
    """仅改受信runner的装配符号；冻结原方法、题目与裁判保持原样。"""
    from agent_runtime.complex_agent import ComplexConversationAgent
    original_agent, original_preflight = module.ConversationAgent, module.ProcessPreflightAgent
    if (not isinstance(original_agent, type) or not isinstance(original_preflight, type)
            or not issubclass(original_preflight, original_agent)):
        raise ValueError('frozen_runner_agent_class_contract_changed')

    class CurrentBaselineAgent(ComplexConversationAgent):
        def __init__(self, *args, **kwargs):
            if kwargs.pop('route_provider', 'off') != 'off':
                raise ValueError('baseline_route_must_be_off')
            if kwargs.pop('tool_routing', 'progressive') != 'progressive':
                raise ValueError('baseline_tools_must_be_progressive')
            super().__init__(*args, route_provider='off', tool_routing='progressive', **kwargs)

    class CurrentProcessPreflightAgent(original_preflight, CurrentBaselineAgent):
        pass

    module._semantic_original_conversation_agent = original_agent
    module._semantic_original_preflight_agent = original_preflight
    module.ConversationAgent = CurrentBaselineAgent
    module.ProcessPreflightAgent = CurrentProcessPreflightAgent


def load_runner():
    """返回原冻结runner模块；仅在内存包装configure及报告适配说明。"""
    global _RUNNER, _ADAPTER_INPUTS
    _verify_originals()
    if _RUNNER is not None:
        if _helper_inputs() != _ADAPTER_INPUTS:
            raise ValueError("semantic_adapter_source_changed_since_import")
        if _RUNNER.source_manifest() != _RUNNER._IMPORT_SOURCE_MANIFEST:
            raise ValueError("production_source_changed_since_legacy_import")
        return _RUNNER
    helper_inputs = _helper_inputs()
    package_name = "window_sqlite_adapted_skill_runner"
    spec = importlib.util.spec_from_file_location(package_name, RUNNER_PATH,
        submodule_search_locations=[str(RUNNER_PATH.parent)])
    module = importlib.util.module_from_spec(spec)
    original_source = RUNNER_PATH.read_text(encoding="utf-8")
    patched_tree, assignment_signatures = _rewrite_actual_agent_assignments(original_source)
    code = compile(patched_tree, str(RUNNER_PATH), "exec")
    module._bind_actual_agent = _bind_actual_agent
    module.semantic_runner_agent_binding_patch = assignment_signatures
    # 注册后以包上下文执行，支持未来runner中的相对导入与dataclass模块查询。
    sys.modules[package_name] = module
    try:
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(package_name, None)
        raise
    if module._PRODUCTION_IMPORT_ERROR is not None:
        raise RuntimeError("production_import_failed") from module._PRODUCTION_IMPORT_ERROR
    _verify_originals()
    if module.source_manifest() != module._IMPORT_SOURCE_MANIFEST:
        raise ValueError("production_source_changed_during_legacy_import")
    if helper_inputs != _helper_inputs():
        raise ValueError("semantic_adapter_changed_during_legacy_import")
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    public_fixtures = [fixture for fixture in golden["fixtures"].values() if fixture["type"] == "public_policy"]
    allowed_payloads = {_json_text({key: fixture[key] for key in ("provider_status", "search_result", "canonical_evidence")}) for fixture in public_fixtures}
    original_configure, original_write_json = module.configure, module.write_json
    module.create_server=wrap_semantic_server(module.create_server,ROOT)
    module.request=wrap_semantic_request(module.request)
    original_source_manifest, original_trace_reviewer = module.source_manifest, module.TraceReviewer

    def source_manifest(*args, **kwargs):
        return {**original_source_manifest(*args, **kwargs), **_helper_inputs()}

    def configure(gateway, workspace, fixture, original_catalog):
        with request_context(gateway,workspace):
            result = original_configure(gateway, workspace, fixture, original_catalog)
        if fixture["type"] == "legacy_catalog_no_rag":
            gateway.public_products = EvalCatalogLifecycleAdapter(gateway.public_products, original_catalog)
        if fixture["type"] == "public_policy":
            public = {key: fixture[key] for key in ("provider_status", "search_result", "canonical_evidence")}
            if _json_text(public) not in allowed_payloads:
                raise ValueError("public_fixture_differs_from_frozen_golden")
            gateway.knowledge_provider = SQLiteEvalPublicProvider(gateway.knowledge_provider, gateway.bank.database)
        host = getattr(gateway, '_original97_registered_host', None)
        if host is not None:
            # server在启动时捕获的EvidenceRuntime不会随gateway字段替换而更新。
            # 只对这个受控注册测试宿主安装原题的轮提供器，保留生产HTTP的固定、
            # 原件回查、GLM和发布门；不从请求或模型接收提供器。
            if host.g is not gateway or host.server.finance is not gateway:
                raise ValueError('semantic_fixture_registered_host_mismatch')
            if fixture['type'] == 'legacy_catalog_no_rag':
                from local_knowledge.catalog_information import CatalogInformationSource
                package = gateway.public_products.read(query='', limit=12)
                if _json_text(package) != _json_text(fixture['public_catalog_result']):
                    raise ValueError('legacy_catalog_differs_from_frozen_golden')
                with request_context(gateway, workspace):
                    gateway.catalog_information_host.install(workspace,
                        CatalogInformationSource(package, source_kind='synthetic'))
            host.server.evidence_runtime.close()
            host.server.evidence_runtime = FrozenSemanticEvidenceRuntime(gateway.knowledge_provider)
        return result

    metadata = {
        "version": ADAPTER_VERSION, "synthetic_fixture_only": True,
        "original_runner_sha256": RUNNER_SHA256, "original_golden_sha256": GOLDEN_SHA256,
        "adapter_sha256": helper_inputs,
        "actual_agent_binding": "原runner两处Agent赋值仅在内存改写为受信绑定器；校验approved_responses.validator的__self__与__func__属于同一实际Agent。",
        "baseline_agent_assembly": "受信模块符号装配ComplexConversationAgent(off/progressive)；预检继承原方法，保留integration_failures；未改冻结脚本、题目、金额、裁判或单题预算。",
        "change": "原configure之后仅public_policy换为同值临时SQLite；status/search/canonical/public_catalog均原JSON；db与resolve锁内同连接。",
        "storage": "公共库仅建于本case的系统临时银行父目录；不读银行/私人库；不连接生产公共资料。",
        "judge": "未改变题意、固定金额、人工判据；技术流程和审核approved均不是金融语义通过。",
        "process_preflight_billing": "仅受信本地脚本预检使用真实GLM请求构造与wire捕获、确定性内存响应及零用量；不访问网络、不计费，真实Trace的paid计量保持。",
        "trace_reviewer_compatibility": "仅内存继承原Trace ctor；原生review完整转发kwargs（含max_input_bytes），原safe_data/deepcopy记录一次；1400与_model_usage不改。",
    }

    def write_json(path, value):
        if Path(path).name == "semantic-results.json" and isinstance(value, dict):
            # 附加记录；不改旧records、fixture或金融预期，也不改旧冻结清单。
            value = {**value, "fixture_adapter": {**value.get("fixture_adapter", {}), "sqlite_public_fixture": deepcopy(metadata)}}
        return original_write_json(path, value)

    module.configure = configure
    module.write_json = write_json
    module.source_manifest = source_manifest
    module._IMPORT_SOURCE_MANIFEST = {**module._IMPORT_SOURCE_MANIFEST, **helper_inputs}
    module.TraceReviewer = compatible_trace_reviewer(original_trace_reviewer, module.safe_data)
    module._semantic_original_trace_reviewer = original_trace_reviewer
    module.semantic_runner_adapter = deepcopy(metadata)
    module._semantic_original_configure = original_configure
    # 仅这个受信runner配置的脚本预检替身免费；真实Trace客户端保持paid。
    module.ProcessPreflightPrimary.billing_mode = 'unpaid_fixture'
    module.ProcessPreflightReviewer.billing_mode = 'unpaid_fixture'
    original_preflight_complete = module.ProcessPreflightPrimary.complete

    def preflight_complete(self, *args, **kwargs):
        message, _ = original_preflight_complete(self, *args, **kwargs)
        return message, {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}

    module.ProcessPreflightPrimary.complete = preflight_complete
    _install_preflight_glm_wire(module)
    _install_current_baseline_agent(module)
    _ADAPTER_INPUTS, _RUNNER = helper_inputs, module
    return module


def main():
    # 专属参数只在此真实适配入口消费，原冻结runner参数与题集保持。
    import argparse
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--paid-campaign-manifest',type=Path)
    parser.add_argument('--paid-campaign-round',type=int)
    parser.add_argument('--paid-campaign-delegated-attempt')
    context,remaining=parser.parse_known_args()
    free=any(flag in remaining for flag in ('--offline','--process-preflight'))
    if free:
        if any(v is not None for v in vars(context).values()):raise ValueError('free_semantic_cannot_take_paid_context')
        return load_runner().main()
    if context.paid_campaign_manifest is None:raise ValueError('semantic_paid_campaign_manifest_required')
    from paid_campaign import load_campaign,audit_total
    campaign=load_campaign(ROOT,context.paid_campaign_manifest)
    if remaining.count('--out-dir')!=1:raise ValueError('semantic_exact_original_output_required')
    output=Path(remaining[remaining.index('--out-dir')+1]).resolve()
    sys.argv=[sys.argv[0],*remaining]
    module=load_runner()
    if context.paid_campaign_delegated_attempt:
        if remaining.count('--case-ids')!=1:raise ValueError('semantic_delegated_exact_one_case_required')
        case_id=remaining[remaining.index('--case-ids')+1]
        identity=campaign.delegated(context.paid_campaign_delegated_attempt,case_id=case_id,output=output)
        with campaign.transport(identity):return module.main()
    if context.paid_campaign_round not in (1,2,3):raise ValueError('semantic_original_round_required')
    original_primary,original_write=module.TracePrimary,module.write_json
    dataset_path=Path(remaining[remaining.index('--dataset')+1]) if '--dataset' in remaining else GOLDEN_PATH
    dataset=json.loads(dataset_path.read_text())
    original=json.loads(GOLDEN_PATH.read_text())
    if dataset['cases']!=original['cases'] or dataset['fixtures']!=original['fixtures']:
        raise ValueError('semantic_campaign_original_cases_or_fixtures_changed')
    selected_arg=remaining[remaining.index('--case-ids')+1] if '--case-ids' in remaining else ''
    selected=[row['id'] for row in module.select_cases(dataset['cases'],selected_arg)]
    active=None;scope=None;next_case=0
    class CampaignPrimary(original_primary):
        def __init__(self,*args,**kwargs):
            nonlocal active,scope,next_case
            if active is not None or next_case>=len(selected):raise ValueError('semantic_campaign_case_state')
            case_id=selected[next_case];next_case+=1
            active=campaign.begin(suite_id='semantic-original10-three-rounds',case_id=case_id,
                round_index=context.paid_campaign_round,variant=None,output=output/case_id)
            scope=campaign.transport(active);scope.__enter__()
            super().__init__(*args,**kwargs)
    def write(path,value):
        nonlocal active,scope,selected
        result=original_write(path,value)
        if Path(path).name=='semantic-results.json' and isinstance(value,dict):
            if value.get('selected_case_ids'):selected=value['selected_case_ids']
            rows=value.get('records',[])
            if active is not None and rows and rows[-1].get('id')==selected[next_case-1]:
                try:
                    receipt=campaign.finish(active,audit_total=audit_total(rows[-1]))
                    receipt_path=output/rows[-1]['id']/'paid-campaign-attempt.json'
                    campaign.p.write(receipt_path,receipt)
                    rows[-1]['paid_campaign']={'attempt_id':active,'ledger':campaign.manifest['ledger'],
                        'receipt':str(receipt_path),'receipt_sha256':campaign.p.sha(receipt_path)}
                    result=original_write(path,value)
                finally:scope.__exit__(None,None,None);active=scope=None
        return result
    module.TracePrimary=CampaignPrimary;module.write_json=write
    try:return module.main()
    finally:
        module.TracePrimary=original_primary;module.write_json=original_write
        if active is not None:
            try:campaign.finish(active,failed=True)
            finally:scope.__exit__(None,None,None)


if __name__ == "__main__":
    raise SystemExit(main())
