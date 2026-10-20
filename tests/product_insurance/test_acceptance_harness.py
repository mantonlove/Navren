from __future__ import annotations

import hashlib
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evaluation.product_insurance.harness import (  # noqa: E402
    AcceptanceError,
    API_MAP_PATH,
    BASE_API_PATH,
    ACCEPTANCE_OUT,
    isolated_entry_vertical_report,
    read_json,
    readiness_report,
    sha256_file,
    summarize_pension_research_scope,
    status_report,
    validate_frozen_contracts,
    validate_two_level_mapping,
)
from evaluation.product_insurance.http_runner import run_adapter  # noqa: E402
from evaluation.product_insurance import http_runner  # noqa: E402


class AcceptanceHarnessTests(unittest.TestCase):
    def test_isolated_entry_receipts_require_same_run_api_identity_and_screenshot_readback(self):
        run_id = "20261018T174232393738Z"
        api_sha = "api-contract-sha"
        provider_revision = "provider-revision-v1"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            acceptance = root / "outputs/product-insurance-implementation-20261018/acceptance"
            (acceptance / "ui-evidence").mkdir(parents=True)
            screenshot = acceptance / "ui-evidence/home.png"
            screenshot.write_bytes(b"synthetic screenshot receipt")
            screenshot_sha = sha256_file(screenshot)
            http_path = acceptance / f"http-run-{run_id}.json"
            ui_path = acceptance / f"ui-run-{run_id}.json"
            http = {
                "schema": "navren.product-insurance.http-run/1",
                "status": "PASS_ISOLATED_HTTP",
                "api_contract_sha256": api_sha,
                "base_origin": "http://127.0.0.1:12345",
                "isolated_instance": True,
                "real_model_calls": 0,
                "paid_api_calls": 0,
                "flows": [{"passed": True, "steps": [{"passed": True}]}],
            }
            ui = {
                "schema": "navren.product-insurance.ui-run/1",
                "status": "PASS_ISOLATED_UI",
                "api_contract_sha256": api_sha,
                "base_origin": "http://127.0.0.1:12345",
                "isolated_instance": True,
                "real_model_calls": 0,
                "paid_api_calls": 0,
                "flows": [{"case_id": "PI-ENTRY-UI", "passed": True, "steps": [{"action": "screenshot", "passed": True, "path": "ui-evidence/home.png", "sha256": screenshot_sha, "bytes": screenshot.stat().st_size}]}],
            }
            http_path.write_text(json.dumps(http), encoding="utf-8")
            ui_path.write_text(json.dumps(ui), encoding="utf-8")
            relative_http = str(http_path.relative_to(root))
            relative_ui = str(ui_path.relative_to(root))
            run = {
                "schema": "navren.product-insurance.isolated-entry-run/1",
                "run_id": run_id,
                "status": "PASS_ISOLATED_ENTRY_VERTICAL",
                "base_url": "http://127.0.0.1:12345",
                "api_contract_sha256": api_sha,
                "provider_revision": provider_revision,
                "production_writes": 0,
                "model_calls": 0,
                "real_sms": 0,
                "private_state_persisted_to_acceptance": False,
                "evidence": {
                    "http_report": relative_http,
                    "ui_report": relative_ui,
                    "screenshots": [{"case_id": "PI-ENTRY-UI", "sha256": screenshot_sha, "bytes": screenshot.stat().st_size}],
                },
            }
            (acceptance / "isolated-entry-run.json").write_text(json.dumps(run), encoding="utf-8")
            report = isolated_entry_vertical_report(root=root, acceptance_dir=acceptance, api_contract_sha256=api_sha, provider_revision=provider_revision)
            self.assertEqual(report["status"], "PASS_ISOLATED_ENTRY_VERTICAL_NOT_PRODUCT_ACCEPTANCE")
            self.assertEqual(report["http_status"], "PASS_ISOLATED_HTTP_VERTICAL_NOT_FULL_MATRIX")
            self.assertEqual(report["ui_status"], "PASS_ISOLATED_UI_VERTICAL_NOT_PRODUCT_ACCEPTANCE")
            self.assertFalse(report["product_acceptance"])

            ui["api_contract_sha256"] = "stale-api-sha"
            ui_path.write_text(json.dumps(ui), encoding="utf-8")
            stale = isolated_entry_vertical_report(root=root, acceptance_dir=acceptance, api_contract_sha256=api_sha, provider_revision=provider_revision)
            self.assertEqual(stale["status"], "NOT_RUN_OR_FAILED")
            self.assertIn("isolated_entry_ui_api_contract_stale", stale["errors"])

    def test_frozen_spec_hash_validation_is_fail_closed_without_product_credit(self):
        try:
            report = validate_frozen_contracts()
        except AcceptanceError as exc:
            report = json.loads(str(exc))
        self.assertFalse(report["product_acceptance"])
        self.assertFalse(report["model_acceptance"])
        self.assertEqual(next(x for x in report["checks"] if x["check"] == "acceptance_api_map_hash")["sha256"], sha256_file(API_MAP_PATH))
        self.assertTrue(next(x for x in report["checks"] if x["check"] == "archived_document_source_receipts")["passed"])
        frozen_inputs = next(x for x in report["checks"] if x["check"] == "frozen_input_hashes")
        self.assertIn("设计文档/产品保险实施任务-20261018/01-执行身份与演示正例.md", {x["path"] for x in read_json(ROOT / "evaluation/product_insurance/casebook.json")["input_artifacts"].values() if isinstance(x, dict) and x.get("path")})
        if frozen_inputs["passed"]:
            self.assertEqual(report["status"], "PASS_SPEC_INTEGRITY")
        else:
            self.assertEqual(report["status"], "FAIL_SPEC_INTEGRITY")
            self.assertIn("outputs/product-insurance-implementation-20261018/base/api-contract.json", frozen_inputs["mismatches"])

    def test_limited_pension_publication_is_readable_without_quote_or_irr_capability(self):
        publication = {
            "published_ready_for_integration": True,
            "provider_revision": "revision-v2",
            "commercial_pension_requirement": {
                "status": "APPROVED_READ_ONLY_RESEARCH_SAMPLE_PUBLISHED",
                "entity_id": "insurance:CN:CLPC:2026-001:A",
                "primary_contract_obtained": True,
                "dated_amendment_chain_obtained": True,
                "public_rule_dependencies_missing": 4,
            },
        }
        smoke_entities = [{"entity_id": "insurance:CN:CLPC:2026-001:A", "document_count": 4, "verified_fact_count": 59, "current_complete": False}]
        scope = summarize_pension_research_scope(
            publication,
            current_provider_revision="revision-v2",
            smoke_provider_revision="revision-v2",
            smoke_entity_ids=["insurance:CN:CLPC:2026-001:A"],
            smoke_entities=smoke_entities,
            smoke_errors=[],
            original_document_bytes_verified=True,
        )
        self.assertTrue(scope["read_only_research_sample_available"])
        self.assertEqual(scope["entity_id"], "insurance:CN:CLPC:2026-001:A")
        self.assertEqual(scope["documents"], 4)
        self.assertFalse(scope["current_complete"])
        self.assertFalse(scope["individual_net_payment_quote_supported"])
        self.assertFalse(scope["personal_cash_value_supported"])
        self.assertFalse(scope["future_irr_supported"])
        self.assertIn("future cashflows", " ".join(scope["current_required_gaps"]))
        mismatched = summarize_pension_research_scope(
            publication,
            current_provider_revision="revision-v3",
            smoke_provider_revision="revision-v2",
            smoke_entity_ids=["insurance:CN:CLPC:2026-001:A"],
            smoke_entities=smoke_entities,
            smoke_errors=[],
            original_document_bytes_verified=True,
        )
        self.assertFalse(mismatched["read_only_research_sample_available"])
        no_byte_readback = summarize_pension_research_scope(
            publication,
            current_provider_revision="revision-v2",
            smoke_provider_revision="revision-v2",
            smoke_entity_ids=["insurance:CN:CLPC:2026-001:A"],
            smoke_entities=smoke_entities,
            smoke_errors=[],
            original_document_bytes_verified=False,
        )
        self.assertFalse(no_byte_readback["read_only_research_sample_available"])

    def test_entity_version_and_math_fact_source_binding_are_both_required(self):
        entities = {"fund:CN:002604": {"evidence_version": "entity-v1"}}
        documents = {
            "fund:CN:002604": {
                "evidence_version": "entity-v1",
                "items": [{"document_id": "doc-fee", "source_id": "manager:002604:fee", "source_version": "source-v1"}],
            }
        }
        facts = [{
            "entity_id": "fund:CN:002604",
            "field": "subscription_fee",
            "value": "0.001",
            "state": "verified",
            "source_id": "manager:002604:fee",
            "source_version": "source-v1",
            "document_id": "doc-fee",
            "locator": "招募说明书第5页，申购费率表",
        }]
        self.assertEqual(validate_two_level_mapping(entities, documents, facts, declared_complete=True, required_facts={("fund:CN:002604", "subscription_fee")}), [])

        all_missing = [{"entity_id": "fund:CN:002604", "field": "subscription_fee", "state": "missing", "missing_reason": "official_channel_rate_not_present", "value": None}]
        errors = validate_two_level_mapping(entities, documents, all_missing, declared_complete=True, required_facts={("fund:CN:002604", "subscription_fee")})
        self.assertIn("all_missing_or_unverified_facts_cannot_be_declared_complete", errors)
        self.assertIn("required_fact_not_verified:fund:CN:002604:subscription_fee", errors)

        no_locator = [{**facts[0], "locator": ""}]
        self.assertTrue(any("verified_fact_missing_source_binding" in error for error in validate_two_level_mapping(entities, documents, no_locator, declared_complete=True)))
        changed_entity_version = {"fund:CN:002604": {"evidence_version": "entity-v2", "items": documents["fund:CN:002604"]["items"]}}
        self.assertTrue(any("entity_documents_evidence_version_mismatch" in error for error in validate_two_level_mapping(entities, changed_entity_version, facts, declared_complete=True)))

        conflict = [{**facts[0], "state": "conflict", "value": {"current_notice": "R2", "historical_contract": "R1"}, "missing_reason": "Different source versions are unresolved."}]
        self.assertEqual(validate_two_level_mapping(entities, documents, conflict, declared_complete=False), [])
        conflict_complete = validate_two_level_mapping(entities, documents, conflict, declared_complete=True, required_facts={("fund:CN:002604", "subscription_fee")})
        self.assertIn("all_missing_or_unverified_facts_cannot_be_declared_complete", conflict_complete)
        self.assertNotIn("nonverified_fact_has_value:0", conflict_complete)

        superseded = [{**facts[0], "state": "superseded", "value": "0.0007", "missing_reason": "Retained as the source-backed historical value after a later effective revision."}]
        self.assertEqual(validate_two_level_mapping(entities, documents, superseded, declared_complete=False), [])
        superseded_without_source = [{**superseded[0], "source_version": "unbound-version"}]
        self.assertIn("fact_source_not_in_entity_evidence:0", validate_two_level_mapping(entities, documents, superseded_without_source, declared_complete=False))

    def test_old_research_audit_remains_baseline_and_current_delivery_gaps_block_readiness(self):
        # Exercise current delivery classification separately from a failed frozen-contract gate.
        with patch("evaluation.product_insurance.harness.validate_frozen_contracts", return_value={"status": "PASS_SPEC_INTEGRITY"}):
            report = readiness_report()
        self.assertEqual(report["historical_research_audit"]["blocks_current_readiness"], False)
        self.assertNotIn("PUBLIC_SOURCE_IDENTITY_OR_COVERAGE_GAPS", {x["id"] for x in report["blockers"]})
        ids = {x["id"] for x in report["blockers"]}
        self.assertIn("CURRENT_RAG_DELIVERY_INCOMPLETE", ids)
        self.assertNotIn("CURRENT_COMMERCIAL_PENSION_CONTRACT_INCOMPLETE", ids)
        self.assertIn("BASE_FULL_FRAMEWORK_NOT_ACCEPTED", ids)
        self.assertIn("ORIGINAL_14_SECURITY_CONTROLS_NOT_ACCEPTED", ids)
        self.assertIn("PI_X06_OPERATION_RECEIPT_MAPPING_PENDING", ids)
        self.assertIn("PI_T01_CONFIRMED_RESERVE_FLOW_PENDING", ids)
        self.assertEqual(report["current_deliveries"]["rag"]["content_actual"]["fund_deep_entities"], 4)
        self.assertEqual(report["current_deliveries"]["rag"]["content_actual"]["insurance_deep_entities"], 2)
        self.assertEqual(report["current_deliveries"]["rag"]["content_actual"]["pension_samples"], 1)
        self.assertTrue(report["current_deliveries"]["rag"]["research_only_pension_sample_ready"])
        self.assertTrue(report["current_deliveries"]["rag"]["entity_ids_match_expected"])
        self.assertEqual(report["current_deliveries"]["rag"]["provider_revision"], report["current_deliveries"]["rag"]["smoke_revision"])
        pension_scope = report["scope_limits"]["commercial_pension"]
        self.assertFalse(pension_scope["current_complete"])
        self.assertFalse(pension_scope["individual_net_payment_quote_supported"])
        self.assertFalse(pension_scope["personal_cash_value_supported"])
        self.assertFalse(pension_scope["future_irr_supported"])
        self.assertFalse(report["product_acceptance"])

    def test_readiness_reports_frozen_drift_without_discarding_other_gate_results(self):
        failure = {"status": "FAIL_SPEC_INTEGRITY", "errors": ["frozen_input_hash_mismatch"]}
        with patch("evaluation.product_insurance.harness.validate_frozen_contracts", side_effect=AcceptanceError(json.dumps(failure))):
            report = readiness_report()
        self.assertEqual(report["spec_integrity"], "FAIL_SPEC_INTEGRITY")
        blocker_ids = {x["id"] for x in report["blockers"]}
        self.assertIn("FROZEN_INPUT_INTEGRITY_FAILED", blocker_ids)
        self.assertIn("CURRENT_RAG_DELIVERY_INCOMPLETE", blocker_ids)
        self.assertFalse(report["product_acceptance"])

    def test_status_keeps_partial_engineering_evidence_separate_from_frozen_failure(self):
        failure = {"status": "FAIL_SPEC_INTEGRITY", "errors": ["frozen_input_hash_mismatch"]}
        with patch("evaluation.product_insurance.harness.validate_frozen_contracts", side_effect=AcceptanceError(json.dumps(failure))):
            report = status_report()
        self.assertEqual(report["spec_integrity"], "FAIL_SPEC_INTEGRITY")
        self.assertEqual(report["partial_engineering_evidence"]["status"], "PASS")
        self.assertEqual(report["status"], "PARTIAL_ENGINEERING_EVIDENCE_PASS_FULL_ACCEPTANCE_BLOCKED")
        self.assertFalse(report["product_acceptance"])

    def test_http_runner_never_persists_json_response_body(self):
        sentinel = "SYNTHETIC_PRIVATE_RESPONSE_SENTINEL"
        request_count = {"value": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                request_count["value"] += 1
                body = json.dumps({"value": sentinel}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            adapter = {
                "schema": "navren.pi.http-adapter/1",
                "api_contract_path": str(BASE_API_PATH.relative_to(ROOT)),
                "api_contract_sha256": sha256_file(BASE_API_PATH),
                "base_url_env": "NAVREN_PI_BASE_URL",
                "isolation_manifest_env": "NAVREN_PI_TEST_UNUSED_MANIFEST",
                "flows": [{"case_id": "PI-HTTP-REPORT-PRIVACY", "steps": [{"id": "synthetic", "method": "GET", "path": "/synthetic", "expected": {"status": 200}}]}],
            }
            with tempfile.TemporaryDirectory() as temp:
                adapter_path = Path(temp) / "adapter.json"
                adapter_path.write_text(json.dumps(adapter), encoding="utf-8")
                # This test isolates response-body persistence; the separate frozen-contract
                # checks cover API state and hash binding, which currently fail closed on drift.
                with patch("evaluation.product_insurance.http_runner._contract_check", return_value=read_json(BASE_API_PATH)):
                    report = run_adapter(adapter_path, base_url_override=f"http://127.0.0.1:{server.server_port}")
            encoded = json.dumps(report, ensure_ascii=False)
            self.assertEqual(report["status"], "PASS_ISOLATED_HTTP")
            self.assertNotIn(sentinel, encoded)
            self.assertNotIn("response_json", encoded)
            self.assertFalse(report["flows"][0]["steps"][0]["response_body_persisted"])
            self.assertEqual(request_count["value"], 1)
            print("PI_ACCEPTANCE_HTTP_HARNESS_REQUESTS=1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_runner_binds_known_frozen_candidate_by_exact_hash(self):
        adapter = {
            "schema": "navren.pi.http-adapter/1",
            "api_contract_path": str(BASE_API_PATH.relative_to(ROOT)),
            "api_contract_sha256": sha256_file(BASE_API_PATH),
        }
        contract = http_runner._contract_check(adapter)
        self.assertEqual(contract["status"], read_json(BASE_API_PATH)["status"])
        stale = {**adapter, "api_contract_sha256": "0" * 64}
        with self.assertRaisesRegex(AcceptanceError, "http_adapter_api_contract_hash_mismatch"):
            http_runner._contract_check(stale)
        with patch("evaluation.product_insurance.http_runner.read_json", return_value={"status": "DRAFT_NOT_FROZEN"}):
            with self.assertRaisesRegex(AcceptanceError, "http_adapter_not_bound_to_current_frozen_api_contract"):
                http_runner._contract_check(adapter)

    def test_real_proxy_research_routes_on_isolated_registered_identity(self):
        sys.path.insert(0, str(ROOT / "tests"))
        import test_research_workspace_http

        class CountedResearchHTTPCase(test_research_workspace_http.ResearchWorkspaceHTTPTests):
            def setUp(self):
                super().setUp()
                self.proxy_request_count = 0
                original = self.h.request

                def counted_request(*args, **kwargs):
                    self.proxy_request_count += 1
                    return original(*args, **kwargs)

                self.h.request = counted_request

        case = CountedResearchHTTPCase(methodName="test_actual_http_public_and_private_save_read_cas_and_identity_rejection")
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual(result.errors, [], "isolated real HTTP vertical raised an error")
        self.assertEqual(result.failures, [], "isolated real HTTP vertical failed an assertion")
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(case.proxy_request_count, 10)
        print(f"PI_ACCEPTANCE_PROXY_HTTP_REQUESTS={case.proxy_request_count}")


if __name__ == "__main__":
    unittest.main()
