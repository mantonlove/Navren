"""临时SQLite语义夹具适配回归；不导入旧TestCase，不跑云或HTTP。

python3 -B -m unittest discover -s tests/window_integration -p test_semantic_runner_adapter.py -v
"""
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("independent_semantic_runner_adapter", ROOT / "tests/window_integration/semantic_runner_adapter.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def full_types(value):
    if isinstance(value, dict):
        return (type(value), {key: full_types(item) for key, item in value.items()})
    if isinstance(value, list):
        return (type(value), [full_types(item) for item in value])
    return (type(value), value)


class SemanticRunnerSQLiteAdapterChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.originals = {adapter.RUNNER_PATH: adapter.digest(adapter.RUNNER_PATH),
                         adapter.GOLDEN_PATH: adapter.digest(adapter.GOLDEN_PATH),
                         ROOT / "evaluation/window_integration/semantic_cases.json": adapter.digest(ROOT / "evaluation/window_integration/semantic_cases.json")}
        # 导入/测试均禁止网络；实际final public_guard是本地上下文调用。
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
                       "socket.getaddrinfo", "urllib.request.urlopen", "urllib.request.OpenerDirector.open"):
            guard = patch(target, side_effect=AssertionError("network_forbidden_in_adapter_unit_test"))
            guard.start()
            cls.addClassCleanup(guard.stop)
        cls.runner = adapter.load_runner()
        cls.fixture = next(fixture for fixture in json.loads(adapter.GOLDEN_PATH.read_text(encoding="utf-8"))["fixtures"].values() if fixture["type"] == "public_policy")
        from base_runtime.evidence import OperationEvidence, public_digest
        from simulated_bank import BankError
        cls.OperationEvidence, cls.public_digest, cls.BankError = OperationEvidence, staticmethod(public_digest), BankError

    @classmethod
    def tearDownClass(cls):
        if cls.originals != {path: adapter.digest(path) for path in cls.originals}:
            raise RuntimeError("legacy_source_or_golden_modified: 本轮结果无效")
        if cls.runner.source_manifest() != cls.runner._IMPORT_SOURCE_MANIFEST:
            raise RuntimeError("production_changed_during_adapter_test: 本轮结果无效")
        print(json.dumps({"suite": "semantic-runner-sqlite-adapter", "adapter": cls.runner.semantic_runner_adapter,
                          "originals_unchanged": True, "cloud_model_calls": 0,
                          "network_calls_allowed": False, "formal_semantic_acceptance": "not_run"}, ensure_ascii=False, sort_keys=True))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="navren-semantic-adapter-test-")
        self.addCleanup(self.temp.cleanup)
        self.bank_path = Path(self.temp.name) / "bank.sqlite3"
        self.legacy = self.runner.EvalPublicProvider(deepcopy(self.fixture))

    def provider(self):
        return adapter.SQLiteEvalPublicProvider(self.legacy, self.bank_path)

    def canonical_args(self):
        row = self.fixture["canonical_evidence"]
        return row["citation_id"], row["release_id"], "2026-10-06"

    def snapshot(self, provider):
        citation, release, as_of = self.canonical_args()
        canonical = provider.resolve_public_evidence(citation, release, as_of)
        return {"knowledge": {"release_id": release, "citations": [{"citation_id": citation, "as_of": as_of}],
                              "canonical_digests": [{"citation_id": citation, "as_of": as_of, "sha256": self.public_digest(canonical)}]}}

    def final_guard(self, provider):
        # 调用真实最终公开证据门；无需构造银行/私人库或启动HTTP。
        guard = object.__new__(self.OperationEvidence)
        guard.g = SimpleNamespace(knowledge_provider=provider)
        return guard

    def test_01_all_public_fields_and_types_are_exact_copies(self):
        provider = self.provider()
        operations = [(provider.status(), self.legacy.status()),
                      (provider.public_catalog(), self.legacy.public_catalog()),
                      (provider.search_public({"scope": "public"}, "eval-public-1"), self.legacy.search_public({"scope": "public"}, "eval-public-1")),
                      (provider.resolve_public_evidence(*self.canonical_args()), self.legacy.resolve_public_evidence(*self.canonical_args()))]
        for actual, expected in operations:
            self.assertEqual(actual, expected)
            self.assertEqual(full_types(actual), full_types(expected))
        canonical = provider.resolve_public_evidence(*self.canonical_args())
        canonical["locator"]["page"] = 999
        canonical["conditions"].append("caller-only-mutation")
        self.assertEqual(provider.resolve_public_evidence(*self.canonical_args()), self.fixture["canonical_evidence"])
        status = provider.status()
        status["ready"] = False
        self.assertIs(provider.status()["ready"], True)

    def test_02_scope_release_citation_and_date_match_old_provider(self):
        provider = self.provider()
        row = self.fixture["canonical_evidence"]
        for args in (("wrong-citation", row["release_id"], "2026-10-06"),
                     (row["citation_id"], "wrong-release", "2026-10-06"),
                     (row["citation_id"], row["release_id"], "2025-12-31"),
                     (row["citation_id"], row["release_id"], row["effective_to"]),
                     (row["citation_id"], row["release_id"], row["effective_from"])):
            with self.subTest(args=args):
                self.assertEqual(full_types(provider.resolve_public_evidence(*args)), full_types(self.legacy.resolve_public_evidence(*args)))
        self.assertEqual(provider.search_public({"scope": "public"}, "wrong-release"), self.legacy.search_public({"scope": "public"}, "wrong-release"))
        with self.assertRaisesRegex(ValueError, "not_public"):
            provider.search_public({"scope": "private"}, row["release_id"])

    def test_03_actual_final_guard_uses_same_locked_sqlite_connection(self):
        provider = self.provider()
        snapshot = self.snapshot(provider)
        connections, traces, resolutions = [], [], []
        original_db, original_resolve = provider.db, provider.resolve_public_evidence

        @contextmanager
        def traced_db():
            with original_db() as db:
                db.set_trace_callback(traces.append)
                connections.append(db)
                yield db

        def traced_resolve(*args):
            active = getattr(provider._local, "connection", None)
            resolutions.append((active, active.in_transaction if active else False))
            return original_resolve(*args)

        with patch.object(provider, "db", traced_db), patch.object(provider, "resolve_public_evidence", traced_resolve):
            with self.final_guard(provider).public_guard(snapshot):
                self.assertTrue(connections[0].in_transaction)
                self.assertTrue(resolutions)
                self.assertIs(resolutions[0][0], connections[0])
                self.assertIs(resolutions[0][1], True)
                writer = sqlite3.connect(provider.path, timeout=0)
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        writer.execute("UPDATE eval_public_payload SET payload=payload WHERE kind='canonical'")
                finally:
                    writer.close()
        self.assertTrue(any(sql.upper() == "BEGIN IMMEDIATE" for sql in traces))
        self.assertGreaterEqual(len(connections), 2)
        self.assertTrue(all(connection is connections[0] for connection in connections))
        self.assertIsNone(getattr(provider._local, "connection", None))
        # 负对照：旧夹具没有db，真实最终门确实会在旧接口失败。
        with self.assertRaises(AttributeError):
            with self.final_guard(self.legacy).public_guard(snapshot):
                self.fail("old_provider_must_not_pass_sqlite_guard")

    def test_04_revised_withdrawn_or_outbound_revoked_old_snapshots_rejected(self):
        for mutation in ({"document_version_id": "eval-policy-v2"}, {"withdrawn": True}, {"outbound_allowed": False}):
            with self.subTest(mutation=mutation):
                provider = self.provider()
                snapshot = self.snapshot(provider)
                with provider.db() as db:
                    canonical = json.loads(db.execute("SELECT payload FROM eval_public_payload WHERE kind='canonical'").fetchone()[0])
                    canonical.update(mutation)
                    db.execute("UPDATE eval_public_payload SET payload=? WHERE kind='canonical'", (json.dumps(canonical, ensure_ascii=False),))
                with self.assertRaises(self.BankError) as error:
                    with self.final_guard(provider).public_guard(snapshot):
                        self.fail("stale_public_snapshot_must_not_be_published")
                self.assertEqual(error.exception.code, "STALE_KNOWLEDGE_EVIDENCE")

    def test_05_only_temporary_public_json_database_no_private_reads(self):
        with patch.object(adapter.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            with self.assertRaisesRegex(ValueError, "requires_case_temporary_directory"):
                adapter.SQLiteEvalPublicProvider(self.legacy, ROOT / "must-not-open-bank.sqlite3")
            connect.assert_not_called()
        link = Path(self.temp.name) / "outside-bank.sqlite3"
        link.symlink_to(ROOT / "must-not-open-bank.sqlite3")
        with self.assertRaisesRegex(ValueError, "requires_case_temporary_directory"):
            adapter.SQLiteEvalPublicProvider(self.legacy, link)
        provider = self.provider()
        self.assertEqual(provider.path.parent, self.bank_path.parent.resolve())
        self.assertFalse(self.bank_path.exists())  # 只使用银行路径作隔离位置，不打开银行。
        with provider.db() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")], ["eval_public_payload"])
            self.assertEqual({row[0] for row in db.execute("SELECT kind FROM eval_public_payload")}, {"status", "search", "canonical", "public_catalog"})
        with self.assertRaises(AttributeError):
            provider.path = ROOT / "must-not-open-bank.sqlite3"

    def test_06_configure_only_public_fixture_original_configure_once(self):
        fixture = deepcopy(self.fixture)
        before = deepcopy(fixture)
        catalog = object()
        wealth = SimpleNamespace(provision=Mock())
        gateway = SimpleNamespace(bank=SimpleNamespace(database=str(self.bank_path)), wealth=wealth)
        self.assertIsNone(self.runner.configure(gateway, "synthetic-workspace", fixture, catalog))
        wealth.provision.assert_called_once_with("synthetic-workspace")
        self.assertIs(gateway.public_products, catalog)
        self.assertIsInstance(gateway.knowledge_provider, adapter.SQLiteEvalPublicProvider)
        self.assertEqual(fixture, before)
        self.assertEqual(full_types(gateway.knowledge_provider.resolve_public_evidence(*self.canonical_args())), full_types(self.fixture["canonical_evidence"]))
        wealth.provision.reset_mock()
        self.runner.configure(gateway, "synthetic-workspace", {"type": "default"}, catalog)
        wealth.provision.assert_called_once_with("synthetic-workspace")
        self.assertIsInstance(gateway.knowledge_provider, self.runner.EvalUnavailableProvider)
        self.assertFalse(hasattr(gateway.knowledge_provider, "db"))

    def test_07_cli_arguments_are_unchanged_and_no_model_is_invoked(self):
        for arguments in (["--dataset", "frozen.json", "--out-dir", "result", "--offline", "--case-ids", "S07"],
                          ["--dataset", "frozen.json", "--out-dir", "result", "--process-preflight", "--cases", "S07"],
                          ["--dataset", "frozen.json", "--out-dir", "result", "--browser-hold-seconds", "30"]):
            argv = ["semantic_runner_adapter.py", *arguments]
            received = []
            fake = SimpleNamespace(main=lambda: received.append(list(sys.argv)) or 17)
            with patch.object(adapter, "load_runner", return_value=fake), patch.object(sys, "argv", argv):
                self.assertEqual(adapter.main(), 17)
            self.assertEqual(received, [argv])

    def test_08_report_records_preserved_adapter_and_original_hashes_recorded(self):
        value = {"records": [{"id": "S07", "technical_completed": True, "approved": True}],
                 "semantic_grading": "pending_independent_annotation", "fixture_adapter": {"legacy": "unchanged"}}
        before = deepcopy(value)
        path = Path(self.temp.name) / "semantic-results.json"
        self.runner.write_json(path, value)
        actual = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(full_types(actual["records"]), full_types(before["records"]))
        self.assertEqual(actual["semantic_grading"], before["semantic_grading"])
        self.assertEqual(value, before)
        self.assertEqual(actual["fixture_adapter"]["legacy"], "unchanged")
        metadata = actual["fixture_adapter"]["sqlite_public_fixture"]
        self.assertTrue(metadata["synthetic_fixture_only"])
        self.assertEqual(metadata["original_runner_sha256"], adapter.RUNNER_SHA256)
        self.assertEqual(metadata["original_golden_sha256"], adapter.GOLDEN_SHA256)
        self.assertEqual(metadata["adapter_sha256"], adapter._helper_inputs())
        self.assertEqual(adapter.digest(adapter.RUNNER_PATH), adapter.RUNNER_SHA256)
        self.assertEqual(adapter.digest(adapter.GOLDEN_PATH), adapter.GOLDEN_SHA256)

    def test_10_current_runner_binding_and_original_gold_copy_are_exact(self):
        derived = json.loads((ROOT / "evaluation/window_integration/semantic_cases.json").read_text(encoding="utf-8"))
        original = json.loads(adapter.GOLDEN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(adapter.digest(adapter.RUNNER_PATH), adapter.RUNNER_SHA256)
        self.assertEqual(derived["integration_origin"]["runner_sha256"], adapter.RUNNER_SHA256)
        self.assertEqual(derived["integration_origin"]["sha256"], adapter.GOLDEN_SHA256)
        for section in ("fixtures", "fixture_policy", "cases"):
            with self.subTest(section=section):
                self.assertEqual(derived[section], original[section])

    def test_11_legacy_catalog_stop_forwards_to_the_captured_real_lifecycle(self):
        fixture_catalog = self.runner.EvalCatalog({'public_catalog_result': {'items': ['fixed']}})
        lifecycle = Mock()
        lifecycle.stop.return_value = True
        adapted = adapter.EvalCatalogLifecycleAdapter(fixture_catalog, lifecycle)
        self.assertEqual(adapted.read(), {'items': ['fixed']})
        self.assertTrue(adapted.stop(timeout=1.25))
        lifecycle.stop.assert_called_once_with(timeout=1.25)
        with self.assertRaisesRegex(AttributeError, 'lifecycle_stop_unavailable'):
            adapter.EvalCatalogLifecycleAdapter(fixture_catalog, object()).stop()

    def test_12_process_preflight_uses_the_real_glm_request_builder_and_local_wire(self):
        reviewer = self.runner.ProcessPreflightReviewer()
        result = reviewer.review({}, 'adapter-preflight-wire', 30)
        self.assertEqual(result['verdict'], 'approve')
        self.assertEqual(result['reason'], '仅用于离线链路预检，不裁决金融语义。')
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['_model_usage'], {
            'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0})
        self.assertIsInstance(result['_agent_review_wire'], bytes)
        self.assertGreater(len(result['_agent_review_wire']), 0)
        self.assertEqual(reviewer._offline_request_metadata[0]['transport'], 'in_process_fixture_no_network')
        self.assertEqual(reviewer._offline_request_metadata[0]['billing_mode'], 'unpaid_fixture')
        self.assertEqual(len(reviewer.records), 1)

    def test_13_runner_is_registered_with_a_package_import_context(self):
        self.assertIs(sys.modules[self.runner.__name__], self.runner)
        self.assertEqual(list(self.runner.__spec__.submodule_search_locations),
                         [str(adapter.RUNNER_PATH.parent)])


class TraceReviewerCompatibilityChecks(unittest.TestCase):
    def test_09_real_original_trace_preserves_constructor_metadata_and_forwards_all_review_kwargs_once(self):
        with tempfile.TemporaryDirectory(prefix="navren-trace-kwargs-unit-") as directory:
            for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
                           "socket.getaddrinfo", "urllib.request.urlopen", "urllib.request.OpenerDirector.open"):
                guard = patch(target, side_effect=AssertionError("network_forbidden_in_trace_kwargs_unit"))
                guard.start()
                self.addCleanup(guard.stop)
            originals = {adapter.RUNNER_PATH: adapter.digest(adapter.RUNNER_PATH),
                         adapter.GOLDEN_PATH: adapter.digest(adapter.GOLDEN_PATH)}
            module = adapter.load_runner()
            original_class = module._semantic_original_trace_reviewer
            key_guard = patch("agent_runtime.model_adapters._key_from_sources",
                              side_effect=AssertionError("secrets_forbidden_in_trace_kwargs_unit"))
            key_lookup = key_guard.start()
            self.addCleanup(key_guard.stop)
            helper_path = ROOT / "tests/window_integration/public_probe_runner.py"
            helper_spec = importlib.util.spec_from_file_location("kwargs_public_helper_under_test", helper_path)
            public = importlib.util.module_from_spec(helper_spec)
            helper_spec.loader.exec_module(public)
            compatible = public.compatible_trace_reviewer(original_class, module.safe_data)
            self.assertTrue(issubclass(compatible, original_class))
            self.assertIs(compatible.__init__, original_class.__init__)
            log_path = Path(directory) / "never-requested-model-log.jsonl"
            reviewer = compatible(log_path)
            client, metadata, urlopen = reviewer.client, reviewer.response_metadata, reviewer.client.urlopen
            metadata.append({"model": reviewer.model_id, "id": "synthetic-envelope-metadata-only"})
            self.assertEqual(reviewer.client.log_path, str(log_path))
            self.assertEqual(reviewer.billing_mode, "paid")
            self.assertEqual(reviewer.output_token_limit, 1400)
            packet = {"unit": {"integer": 7, "boolean": False, "missing": None}}
            usage = {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13}
            decision = {"verdict": "approve", "reason": "协议单测；没有模型请求", "issues": [], "_model_usage": usage}
            with patch.object(module.GLMConversationReviewer, "review", return_value=decision) as native_review:
                actual = reviewer.review(packet, "synthetic-turn-no-request", 17.5, max_input_bytes=3456)
                native_review.assert_called_once_with(reviewer, packet, "synthetic-turn-no-request", 17.5, max_input_bytes=3456)
            self.assertIs(actual, decision)
            self.assertEqual(reviewer.records, [{"packet": deepcopy(module.safe_data(packet)),
                                                 "decision": deepcopy(module.safe_data(decision))}])
            self.assertEqual(len(reviewer.records), 1)
            self.assertEqual(reviewer.records[0]["decision"]["_model_usage"], usage)
            self.assertIs(reviewer.client, client)
            self.assertIs(reviewer.response_metadata, metadata)
            self.assertIs(reviewer.client.urlopen, urlopen)
            self.assertEqual(reviewer.client.log_path, str(log_path))
            self.assertEqual(reviewer.output_token_limit, 1400)
            packet["unit"]["integer"] = 999
            decision["_model_usage"]["total_tokens"] = 999
            self.assertEqual(reviewer.records[0]["packet"]["unit"]["integer"], 7)
            self.assertEqual(reviewer.records[0]["decision"]["_model_usage"]["total_tokens"], 13)
            self.assertFalse(log_path.exists())
            key_lookup.assert_not_called()
            self.assertEqual(originals, {path: adapter.digest(path) for path in originals})
            self.assertEqual(module.source_manifest(), module._IMPORT_SOURCE_MANIFEST)


if __name__ == "__main__":
    unittest.main(verbosity=2)
