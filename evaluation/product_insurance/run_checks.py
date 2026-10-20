from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.product_insurance.harness import (
    ACCEPTANCE_OUT,
    ROOT,
    read_json,
    sha256_file,
    validate_two_level_mapping,
    write_json,
)


TASK_ROOT = ROOT / "outputs/product-insurance-implementation-20261018"
TEST_LOG = ACCEPTANCE_OUT / "unittest.log"
TEST_RECEIPT = ACCEPTANCE_OUT / "test-run.json"
SOURCE_SMOKE_RECEIPT = ACCEPTANCE_OUT / "source-provider-smoke.json"
TEST_INPUTS = [
    "evaluation/product_insurance/harness.py",
    "evaluation/product_insurance/run_checks.py",
    "evaluation/product_insurance/casebook.json",
    "evaluation/product_insurance/transaction-fault-protocol.json",
    "tests/product_insurance/test_acceptance_harness.py",
    "tests/product_insurance/test_transaction_fault_injection.py",
    "research_workspace.py",
    "finance_gateway.py",
    "simulated_bank/service.py",
    "simulated_bank/http_api.py",
    "base_runtime/identity.py",
    "base_runtime/account_state.py",
    "server.py",
    "tests/test_registered_instances.py",
    "tests/test_research_workspace.py",
    "tests/test_research_workspace_http.py",
    "tests/test_account_state_http.py",
    "tests/test_account_state.py",
    "tests/test_chat_media.py",
    "outputs/product-insurance-implementation-20261018/base/api-contract.json",
]


def input_manifest(paths: list[Path]) -> list[dict[str, Any]]:
    result = []
    for path in sorted(set(paths)):
        result.append({
            "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else "external",
            "sha256": sha256_file(path) if path.is_file() else None,
            "exists": path.is_file(),
        })
    return result


def run_tests(timeout_seconds: int = 240) -> dict[str, Any]:
    ACCEPTANCE_OUT.mkdir(parents=True, exist_ok=True)
    paths = [ROOT / item for item in TEST_INPUTS]
    before = input_manifest(paths)
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests/product_insurance", "-p", "test_*.py", "-v"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        returncode = completed.returncode
        output = completed.stdout + completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        output = stdout + stderr + f"\nTimed out after {timeout_seconds} seconds.\n"
    elapsed = round(time.monotonic() - started, 3)
    TEST_LOG.write_text(output, encoding="utf-8")
    after = input_manifest(paths)
    count_match = re.search(r"Ran (\d+) tests? in ", output)
    test_names = re.findall(r"^(test_[A-Za-z0-9_]+) \(", output, flags=re.MULTILINE)
    proxy_requests = re.search(r"PI_ACCEPTANCE_PROXY_HTTP_REQUESTS=(\d+)", output)
    harness_requests = re.search(r"PI_ACCEPTANCE_HTTP_HARNESS_REQUESTS=(\d+)", output)
    inputs_unchanged = before == after
    passed = returncode == 0 and not timed_out and "OK" in output and inputs_unchanged
    receipt = {
        "schema": "navren.product-insurance.test-run/1",
        "task_id": "navren-product-insurance-implementation-20261018",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS_TESTS" if passed else "INPUTS_CHANGED_DURING_RUN" if returncode == 0 and not inputs_unchanged else "TIMEOUT" if timed_out else "FAIL_TESTS",
        "product_acceptance": False,
        "command": [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests/product_insurance", "-p", "test_*.py", "-v"],
        "timeout_seconds": timeout_seconds,
        "elapsed_seconds": elapsed,
        "returncode": returncode,
        "test_count": int(count_match.group(1)) if count_match else None,
        "test_names": test_names,
        "isolated_http_vertical": "PASS_SYNTHETIC_ROUTE_TEST_NOT_FULL_MATRIX" if "test_real_proxy_research_routes_on_isolated_registered_identity" in test_names and returncode == 0 else "NOT_RUN_OR_FAILED",
        "http_requests": {
            "isolated_proxy_routes": int(proxy_requests.group(1)) if proxy_requests else 0,
            "sanitization_harness": int(harness_requests.group(1)) if harness_requests else 0,
            "total_loopback_requests": (int(proxy_requests.group(1)) if proxy_requests else 0) + (int(harness_requests.group(1)) if harness_requests else 0),
        },
        "log_path": str(TEST_LOG.relative_to(ROOT)),
        "input_manifest_before": before,
        "input_manifest_after": after,
        "inputs_unchanged": inputs_unchanged,
        "isolated_data": "transaction fault tests use fresh temporary private and bank SQLite databases and synthetic registered identity",
    }
    write_json(TEST_RECEIPT, receipt)
    return receipt


def run_source_smoke() -> dict[str, Any]:
    from research_evidence import ResearchEvidenceStore

    ACCEPTANCE_OUT.mkdir(parents=True, exist_ok=True)
    delivery_path = TASK_ROOT / "rag/delivery.json"
    delivery = read_json(delivery_path)
    content = delivery.get("content", {})
    expected_revision = delivery.get("provider_revision")
    smoke_path = TASK_ROOT / "rag/provider-smoke.json"
    completeness_path = TASK_ROOT / "rag/content-completeness.json"
    completeness = read_json(completeness_path)
    old_owner_smoke = read_json(smoke_path)
    data_root = ROOT / "data/research_evidence"
    manifest_path = data_root / "manifest.json"
    source_manifest = read_json(manifest_path)
    source_bundle_entries = source_manifest.get("bundles", [])
    source_bundles = [read_json(data_root / item["path"]) for item in source_bundle_entries]
    expected_entity_ids = sorted(
        entity["entity_id"]
        for bundle in source_bundles
        for entity in bundle.get("entities", [])
        if isinstance(entity, dict) and isinstance(entity.get("entity_id"), str)
    )
    delivery_bundle_hashes = {(x.get("path"), x.get("sha256")) for x in delivery.get("source_bundles", []) if isinstance(x, dict)}
    manifest_bundle_hashes = {(x.get("path"), x.get("sha256")) for x in source_bundle_entries if isinstance(x, dict)}
    source_bundle_manifest_match = delivery_bundle_hashes == manifest_bundle_hashes
    source_documents = [source_manifest.get("catalog"), *source_manifest.get("documents", [])]
    for bundle in source_bundles:
        source_documents.extend(bundle.get("documents", []))
    document_paths = []
    for document in source_documents:
        if isinstance(document, dict):
            if document.get("path"):
                document_paths.append(data_root / document["path"])
            if document.get("sha256"):
                document_paths.append(data_root / ".objects" / document["sha256"])
    source_paths = [
        ROOT / "research_evidence.py",
        manifest_path,
        delivery_path,
        smoke_path,
        completeness_path,
        *[data_root / x["path"] for x in delivery.get("source_bundles", []) if isinstance(x, dict) and x.get("path")],
        *[data_root / item["path"] for item in source_manifest.get("bundles", []) if isinstance(item, dict) and item.get("path")],
        *document_paths,
    ]
    before = input_manifest(source_paths)
    errors: list[str] = []
    entity_rows: list[dict[str, Any]] = []
    entity_bindings: dict[str, dict[str, Any]] = {}
    documents_by_entity: dict[str, dict[str, Any]] = {}
    math_facts: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="pi-accept-provider-") as temporary_root:
        store = ResearchEvidenceStore(root=data_root, db_path=Path(temporary_root) / "isolated-provider.sqlite3")
        actual_revision = store.revision()
        if actual_revision != expected_revision:
            errors.append("provider_revision_does_not_match_current_owner_delivery")
        if completeness.get("provider_revision") != expected_revision:
            errors.append("content_completeness_revision_does_not_match_current_owner_delivery")
        if completeness.get("published_ready_for_integration") is not True:
            errors.append("current_content_completeness_not_published_ready_for_integration")
        if not source_bundle_manifest_match:
            errors.append("delivery_source_bundle_hashes_do_not_match_current_data_manifest")
        with store.guard(actual_revision):
            for entity_id in expected_entity_ids:
                try:
                    detail = store.detail(entity_id)
                except Exception as exc:
                    errors.append(f"provider_read_failed:{entity_id}:{type(exc).__name__}")
                    continue
                documents = detail.get("documents", [])
                if detail.get("provider_revision") != actual_revision:
                    errors.append(f"provider_revision_changed_during_read:{entity_id}")
                if not detail.get("evidence_version") or not detail.get("facts") or not documents:
                    errors.append(f"provider_entity_missing_evidence:{entity_id}")
                if detail.get("execution_authority") is not False or detail.get("executable") is not False:
                    errors.append(f"provider_entity_execution_authority_not_false:{entity_id}")
                entity_bindings[entity_id] = {"evidence_version": detail.get("evidence_version")}
                documents_by_entity[entity_id] = {
                    "evidence_version": detail.get("evidence_version"),
                    "items": documents,
                }
                math_facts.extend(detail.get("facts", []))
                for document in documents:
                    try:
                        payload = store.document_bytes(document["document_id"], revision=actual_revision)
                    except Exception as exc:
                        errors.append(f"provider_document_read_failed:{entity_id}:{type(exc).__name__}")
                        continue
                    if hashlib.sha256(payload).hexdigest() != document.get("sha256"):
                        errors.append(f"provider_document_sha_mismatch:{entity_id}:{document.get('document_id')}")
                entity_rows.append({
                    "entity_id": entity_id,
                    "evidence_version": detail.get("evidence_version"),
                    "fact_count": len(detail.get("facts", [])),
                    "document_count": len(documents),
                    "verified_fact_count": sum(x.get("state") == "verified" for x in detail.get("facts", [])),
                    "missing_fact_count": sum(x.get("state") == "missing" for x in detail.get("facts", [])),
                    "current_complete": detail.get("current_complete"),
                })
        if set(entity_bindings) != set(expected_entity_ids) or not expected_entity_ids:
            errors.append("expected_deep_entity_set_not_read")
        errors.extend(validate_two_level_mapping(entity_bindings, documents_by_entity, math_facts, declared_complete=False))
    entity_counts = {
        "fund": sum(x.startswith("fund:") for x in expected_entity_ids),
        "protection_insurance": sum(x.startswith("insurance:CN:ZHONGAN:") for x in expected_entity_ids),
        "pension_sample": sum(x.startswith("pension:") for x in expected_entity_ids),
        "commercial_pension_research": sum(x.startswith(("insurance:CN:GUOMIN:", "insurance:CN:CLPC:")) for x in expected_entity_ids),
        "approved_independent_pension_research_sample": sum(
            entity_id == completeness.get("commercial_pension_requirement", {}).get("entity_id")
            for entity_id in expected_entity_ids
        ) if completeness.get("commercial_pension_requirement", {}).get("status") == "APPROVED_READ_ONLY_RESEARCH_SAMPLE_PUBLISHED" else 0,
    }
    if len(expected_entity_ids) != content.get("deep_entities"):
        errors.append("current_deep_entity_count_does_not_match_delivery")
    published_deep = completeness.get("published_deep_entities", {})
    if (
        entity_counts["fund"] != published_deep.get("fund_shares")
        or entity_counts["protection_insurance"] != published_deep.get("protection_insurance_plans")
        or entity_counts["pension_sample"] != published_deep.get("personal_pension_policy_mechanism_samples")
        or entity_counts["commercial_pension_research"] != published_deep.get("commercial_pension_product_research_packages")
        or entity_counts["approved_independent_pension_research_sample"] != published_deep.get("approved_independent_pension_research_sample")
    ):
        errors.append("current_deep_entity_types_do_not_match_delivery_content")
    after = input_manifest(source_paths)
    if before != after:
        errors.append("source_inputs_changed_during_isolated_provider_smoke")
    status = "MODULE_PROVIDER_SMOKE_PASS_NOT_PRODUCT_ACCEPTANCE" if not errors else "MODULE_PROVIDER_SMOKE_FAILED"
    receipt = {
        "schema": "navren.product-insurance.source-provider-smoke/1",
        "task_id": "navren-product-insurance-implementation-20261018",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "product_acceptance": False,
        "provider_revision": actual_revision,
        "expected_revision": expected_revision,
        "previous_owner_smoke_revision": old_owner_smoke.get("provider_revision"),
        "previous_owner_smoke_matches_current_revision": old_owner_smoke.get("provider_revision") == actual_revision,
        "source_bundle_manifest_match": source_bundle_manifest_match,
        "expected_entity_ids": expected_entity_ids,
        "entity_ids": sorted(entity_bindings),
        "entity_counts": entity_counts,
        "commercial_pension_current_complete": any(x.get("current_complete") is True for x in entity_rows if x["entity_id"].startswith(("insurance:CN:GUOMIN:", "insurance:CN:CLPC:"))),
        "approved_independent_pension_research_sample_entity_id": completeness.get("commercial_pension_requirement", {}).get("entity_id"),
        "approved_independent_pension_research_sample_current_complete": any(
            x.get("current_complete") is True and x["entity_id"] == completeness.get("commercial_pension_requirement", {}).get("entity_id")
            for x in entity_rows
        ),
        "original_document_bytes_verified": not any(x.startswith(("provider_document_read_failed:", "provider_document_sha_mismatch:")) for x in errors),
        "facts_read": len(math_facts),
        "two_level_mapping_errors": [x for x in errors if x.startswith(("entity_", "fact_", "verified_", "conflict_", "nonverified_", "unknown_fact", "all_missing", "required_fact"))],
        "documents_readback_count": sum(x.get("document_count", 0) for x in entity_rows),
        "entities": entity_rows,
        "temporary_provider_database": True,
        "private_or_production_writes": False,
        "input_manifest_before": before,
        "input_manifest_after": after,
        "inputs_unchanged": before == after,
        "errors": errors,
        "limitation": "Provider module, revision and evidence mapping smoke only; not HTTP, UI, Agent, commercial completeness, real-model, current-entry or product acceptance.",
    }
    write_json(SOURCE_SMOKE_RECEIPT, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated, reproducible PI acceptance evidence checks.")
    parser.add_argument("command", choices=("tests", "source-smoke", "all"))
    args = parser.parse_args()
    reports = []
    if args.command in {"tests", "all"}:
        reports.append(run_tests())
    if args.command in {"source-smoke", "all"}:
        reports.append(run_source_smoke())
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0 if all(x.get("status") in {"PASS_TESTS", "MODULE_PROVIDER_SMOKE_PASS_NOT_PRODUCT_ACCEPTANCE"} for x in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
