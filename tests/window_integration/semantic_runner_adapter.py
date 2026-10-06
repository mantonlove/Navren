"""冻结语义执行器的临时SQLite公共夹具适配；不是生产RAG资料。

CLI原样转交旧runner全部参数。本文件不修改旧脚本、题意、金融预期或裁判。
public_policy夹具使用真实db()锁；原Trace仅内存兼容原生审核kwargs，保留实际用量。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "scripts/run_skill_acceptance.py"
GOLDEN_PATH = ROOT / "evaluation/navren_skill_semantic_cases.json"
RUNNER_SHA256 = "c3cf75416eb5306e0a2cf8e09b9222e68cf3deed4630cac6a7bad1de80ab215b"
GOLDEN_SHA256 = "f92627f6dcd96a979d17d805dc5a219a47fcacc0a9e647c6f6e1a421f62ec0e6"
ADAPTER_VERSION = "frozen-synthetic-public-sqlite/1"
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


def _verify_originals():
    if digest(RUNNER_PATH) != RUNNER_SHA256:
        raise ValueError("frozen_legacy_runner_source_changed")
    if digest(GOLDEN_PATH) != GOLDEN_SHA256:
        raise ValueError("frozen_original_golden_dataset_changed")


def _helper_inputs():
    paths = (Path(__file__).resolve(), ROOT / "tests/window_integration/test_semantic_runner_adapter.py",
             ROOT / "tests/window_integration/public_probe_runner.py")
    return {str(path.relative_to(ROOT)): digest(path) for path in paths}


def compatible_trace_reviewer(original_class, safe_data):
    """保留原Trace构造，只将真实原生review的kwargs与记录补齐。"""
    from agent_runtime.conversation import GLMConversationReviewer
    if not isinstance(original_class, type) or not issubclass(original_class, GLMConversationReviewer) or not callable(safe_data):
        raise ValueError("native_trace_reviewer_contract_invalid")

    class CompatibleTraceReviewer(original_class):
        def review(self, packet, *args, **kwargs):
            result = GLMConversationReviewer.review(self, packet, *args, **kwargs)
            self.records.append({"packet": deepcopy(safe_data(packet)), "decision": deepcopy(safe_data(result))})
            return result

    return CompatibleTraceReviewer


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
    spec = importlib.util.spec_from_file_location("window_sqlite_adapted_skill_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
    original_source_manifest, original_trace_reviewer = module.source_manifest, module.TraceReviewer

    def source_manifest(*args, **kwargs):
        return {**original_source_manifest(*args, **kwargs), **_helper_inputs()}

    def configure(gateway, workspace, fixture, original_catalog):
        result = original_configure(gateway, workspace, fixture, original_catalog)
        if fixture["type"] == "public_policy":
            public = {key: fixture[key] for key in ("provider_status", "search_result", "canonical_evidence")}
            if _json_text(public) not in allowed_payloads:
                raise ValueError("public_fixture_differs_from_frozen_golden")
            gateway.knowledge_provider = SQLiteEvalPublicProvider(gateway.knowledge_provider, gateway.bank.database)
        return result

    metadata = {
        "version": ADAPTER_VERSION, "synthetic_fixture_only": True,
        "original_runner_sha256": RUNNER_SHA256, "original_golden_sha256": GOLDEN_SHA256,
        "adapter_sha256": helper_inputs,
        "change": "原configure之后仅public_policy换为同值临时SQLite；status/search/canonical/public_catalog均原JSON；db与resolve锁内同连接。",
        "storage": "公共库仅建于本case的系统临时银行父目录；不读银行/私人库；不连接生产公共资料。",
        "judge": "未改变题意、固定金额、人工判据；技术流程和审核approved均不是金融语义通过。",
        "process_preflight_billing": "仅受信本地脚本替身明确unpaid_fixture及实际零云usage；真实Trace的paid计量保持。",
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
    original_preflight_review = module.ProcessPreflightReviewer.review

    def preflight_complete(self, *args, **kwargs):
        message, _ = original_preflight_complete(self, *args, **kwargs)
        return message, {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}

    def preflight_review(self, *args, **kwargs):
        decision = original_preflight_review(self, *args, **kwargs)
        return {**decision, '_model_usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}

    module.ProcessPreflightPrimary.complete = preflight_complete
    module.ProcessPreflightReviewer.review = preflight_review
    _ADAPTER_INPUTS, _RUNNER = helper_inputs, module
    return module


def main():
    return load_runner().main()


if __name__ == "__main__":
    raise SystemExit(main())
