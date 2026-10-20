from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CASEBOOK_PATH = ROOT / "evaluation/product_insurance/casebook.json"
SECURITY_PATH = ROOT / "evaluation/product_insurance/security-preservation.json"
API_MAP_PATH = ROOT / "evaluation/product_insurance/api-map-v1.json"
ACCEPTANCE_OUT = ROOT / "outputs/product-insurance-implementation-20261018/acceptance"
BASE_API_PATH = ROOT / "outputs/product-insurance-implementation-20261018/base/api-contract.json"
PENSION_PUBLICATION_PATH = ROOT / "outputs/product-insurance-implementation-20261018/rag/content-completeness.json"
TASK_ID = "navren-product-insurance-implementation-20261018"
EXPECTED_CASE_IDS = {
    *(f"PI-P{i:02d}" for i in range(1, 9)),
    *(f"PI-I{i:02d}" for i in range(1, 9)),
    *(f"PI-X{i:02d}" for i in range(1, 9)),
}
EXPECTED_SECURITY_IDS = ["G01", "Y01", "Y02", "Y03", "R01", "R02", "R03", "R04", "R05", "S01", "S02", "S03", "S04", "S05"]


class AcceptanceError(ValueError):
    pass


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json_value(path: Path) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AcceptanceError(f"missing_json:{_display_path(path)}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"invalid_json:{_display_path(path)}:{exc}") from exc
    return value


def read_json(path: Path) -> dict[str, Any]:
    value = read_json_value(path)
    if not isinstance(value, dict):
        raise AcceptanceError(f"json_root_must_be_object:{_display_path(path)}")
    return value


def _path_value(value: Any, path: str) -> Any:
    if not path.startswith("$."):
        raise AcceptanceError(f"unsupported_json_path:{path}")
    current = value
    for segment in path[2:].split("."):
        match = re.fullmatch(r"([^\[]+)(?:\[(\d+)\])?", segment)
        if not match:
            raise AcceptanceError(f"unsupported_json_path_segment:{segment}")
        key, index = match.groups()
        if isinstance(current, dict):
            if key not in current:
                raise AcceptanceError(f"json_path_missing:{path}")
            current = current[key]
        else:
            raise AcceptanceError(f"json_path_not_object:{path}")
        if index is not None:
            if not isinstance(current, list) or int(index) >= len(current):
                raise AcceptanceError(f"json_path_index_missing:{path}")
            current = current[int(index)]
    return current


def validate_two_level_mapping(
    entity_bindings: dict[str, dict[str, Any]],
    documents_by_entity: dict[str, dict[str, Any]],
    math_facts: list[dict[str, Any]],
    *,
    declared_complete: bool,
    required_facts: set[tuple[str, str]] | None = None,
) -> list[str]:
    """Enforce entity_id -> evidence_version and math source_id -> source_version + locator."""
    errors: list[str] = []
    for entity_id, binding in entity_bindings.items():
        if not isinstance(binding.get("evidence_version"), str) or not binding["evidence_version"].strip():
            errors.append(f"entity_missing_evidence_version:{entity_id}")
        bundle = documents_by_entity.get(entity_id)
        if not isinstance(bundle, dict) or bundle.get("evidence_version") != binding.get("evidence_version"):
            errors.append(f"entity_documents_evidence_version_mismatch:{entity_id}")
    verified_fields: set[tuple[str, str]] = set()
    for index, fact in enumerate(math_facts):
        state = fact.get("state")
        if state == "verified":
            entity_id = fact.get("entity_id")
            field = fact.get("field")
            source_id = fact.get("source_id")
            source_version = fact.get("source_version")
            locator = fact.get("locator")
            binding = entity_bindings.get(entity_id)
            if not isinstance(binding, dict):
                errors.append(f"fact_entity_unbound:{index}")
                continue
            if not isinstance(field, str) or not field.strip():
                errors.append(f"verified_fact_missing_field:{index}")
            else:
                verified_fields.add((entity_id, field))
            if not source_id or not source_version or not locator:
                errors.append(f"verified_fact_missing_source_binding:{index}")
                continue
            evidence_version = binding.get("evidence_version")
            bundle = documents_by_entity.get(entity_id, {})
            docs = bundle.get("items", []) if isinstance(bundle, dict) else []
            if not isinstance(docs, list):
                errors.append(f"entity_documents_items_invalid:{entity_id}")
                docs = []
            matches = [d for d in docs if d.get("source_id") == source_id and d.get("source_version") == source_version]
            if fact.get("document_id"):
                matches = [d for d in matches if d.get("document_id") == fact["document_id"]]
            if not matches:
                errors.append(f"fact_source_not_in_entity_evidence:{index}")
            elif bundle.get("evidence_version") != evidence_version:
                errors.append(f"fact_evidence_version_mismatch:{index}")
            if fact.get("value") is None:
                errors.append(f"verified_fact_has_no_value:{index}")
        elif state == "conflict":
            if not fact.get("missing_reason"):
                errors.append(f"conflict_fact_missing_reason:{index}")
            if fact.get("value") is None:
                errors.append(f"conflict_fact_missing_competing_evidence:{index}")
            entity_id = fact.get("entity_id")
            source_id = fact.get("source_id")
            source_version = fact.get("source_version")
            locator = fact.get("locator")
            bundle = documents_by_entity.get(entity_id, {})
            docs = bundle.get("items", []) if isinstance(bundle, dict) else []
            if not source_id or not source_version or not locator:
                errors.append(f"conflict_fact_missing_source_binding:{index}")
            else:
                matches = [d for d in docs if d.get("source_id") == source_id and d.get("source_version") == source_version]
                if fact.get("document_id"):
                    matches = [d for d in matches if d.get("document_id") == fact["document_id"]]
                if not matches:
                    errors.append(f"fact_source_not_in_entity_evidence:{index}")
        elif state == "superseded":
            if not fact.get("missing_reason"):
                errors.append(f"nonverified_fact_missing_reason:{index}")
            if fact.get("value") is None:
                errors.append(f"superseded_fact_missing_historical_value:{index}")
            entity_id = fact.get("entity_id")
            source_id = fact.get("source_id")
            source_version = fact.get("source_version")
            locator = fact.get("locator")
            bundle = documents_by_entity.get(entity_id, {})
            docs = bundle.get("items", []) if isinstance(bundle, dict) else []
            if not source_id or not source_version or not locator:
                errors.append(f"superseded_fact_missing_source_binding:{index}")
            else:
                matches = [d for d in docs if d.get("source_id") == source_id and d.get("source_version") == source_version]
                if fact.get("document_id"):
                    matches = [d for d in matches if d.get("document_id") == fact["document_id"]]
                if not matches:
                    errors.append(f"fact_source_not_in_entity_evidence:{index}")
        elif state in {"missing", "stale", "restricted"}:
            if not fact.get("missing_reason"):
                errors.append(f"nonverified_fact_missing_reason:{index}")
            if fact.get("value") is not None:
                errors.append(f"nonverified_fact_has_value:{index}")
        else:
            errors.append(f"unknown_fact_state:{index}:{state}")
    if declared_complete and (not math_facts or not verified_fields):
        errors.append("all_missing_or_unverified_facts_cannot_be_declared_complete")
    if declared_complete and required_facts:
        for entity_id, field in sorted(required_facts - verified_fields):
            errors.append(f"required_fact_not_verified:{entity_id}:{field}")
    return errors


def summarize_pension_research_scope(
    publication: dict[str, Any],
    *,
    current_provider_revision: str | None,
    smoke_provider_revision: str | None,
    smoke_entity_ids: list[str] | None = None,
    smoke_entities: list[dict[str, Any]] | None = None,
    smoke_errors: list[str] | None = None,
    original_document_bytes_verified: bool = False,
) -> dict[str, Any]:
    """Separate a published, bounded research sample from executable quote completeness."""
    requirement = publication.get("commercial_pension_requirement", {})
    entity_id = requirement.get("entity_id")
    entity_ids = set(smoke_entity_ids or [])
    entity = next((x for x in (smoke_entities or []) if x.get("entity_id") == entity_id), {})
    revision_matches = bool(current_provider_revision) and current_provider_revision == smoke_provider_revision == publication.get("provider_revision")
    sample_published = (
        publication.get("published_ready_for_integration") is True
        and requirement.get("status") == "APPROVED_READ_ONLY_RESEARCH_SAMPLE_PUBLISHED"
        and requirement.get("primary_contract_obtained") is True
        and requirement.get("dated_amendment_chain_obtained") is True
    )
    source_readback = (
        entity_id in entity_ids
        and entity.get("document_count", 0) > 0
        and entity.get("verified_fact_count", 0) > 0
        and original_document_bytes_verified
        and not (smoke_errors or [])
    )
    available = sample_published and source_readback and revision_matches
    current_complete = entity.get("current_complete") is True
    return {
        "status": "READ_ONLY_RESEARCH_SAMPLE_AVAILABLE" if available else "RESEARCH_SAMPLE_NOT_READY",
        "read_only_research_sample_available": available,
        "publication_status": requirement.get("status"),
        "entity_id": entity_id,
        "provider_revision": publication.get("provider_revision"),
        "provider_revision_matches_current": revision_matches,
        "documents": entity.get("document_count", 0),
        "verified_facts": entity.get("verified_fact_count", 0),
        "all_original_bytes_read_back": original_document_bytes_verified,
        "current_publication_ready": publication.get("published_ready_for_integration") is True,
        "source_entity_read_back": entity_id in entity_ids,
        "current_complete": current_complete,
        "individual_net_payment_quote_supported": False,
        "personal_cash_value_supported": False,
        "future_irr_supported": False,
        "public_rule_dependencies_missing": requirement.get("public_rule_dependencies_missing"),
        "current_required_gaps": ["complete public business rules", "individual inputs and future cashflows"] if not current_complete else [],
    }


def validate_frozen_contracts() -> dict[str, Any]:
    from evaluation.product_insurance.adoption import current_adoption
    adoption = current_adoption()
    casebook = read_json(CASEBOOK_PATH)
    preserve = read_json(SECURITY_PATH)
    api_map = read_json(API_MAP_PATH)
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    if casebook.get("schema") != "navren.product-insurance.acceptance-casebook/1":
        errors.append("casebook_schema_mismatch")
    expected_plan_sha = "860d0a1fda9aad470fcebce8ed81ca068cd3cfbbd1dd0f3da36b3406c5b4432a"
    plan = casebook.get("plan", {})
    plan_path = ROOT / str(plan.get("path", ""))
    plan_ok = plan_path.is_file() and sha256_file(plan_path) == expected_plan_sha == plan.get("sha256")
    checks.append({"check": "approved_plan_hash", "passed": plan_ok, "expected_sha256": expected_plan_sha})
    if not plan_ok:
        errors.append("approved_plan_hash_mismatch")

    input_hash_errors = []
    input_artifacts = casebook.get("input_artifacts", {})
    flat_inputs: list[dict[str, Any]] = []
    for key in ("approved_plan", "implementation_contract", "original_security_contract", "execution_identity_boundary", "api_contract"):
        item = input_artifacts.get(key)
        if item:
            flat_inputs.append(item)
    flat_inputs.extend(input_artifacts.get("research_artifacts", {}).values())
    for item in flat_inputs:
        path = ROOT / item.get("path", "")
        expected = item.get("sha256")
        if adoption and item == input_artifacts.get("api_contract"):
            expected = adoption['api_contract']['sha256']
        if not path.is_file() or sha256_file(path) != expected:
            input_hash_errors.append(item.get("path", "unknown"))
    checks.append({"check": "frozen_input_hashes", "passed": not input_hash_errors, "count": len(flat_inputs), "mismatches": input_hash_errors})
    if input_hash_errors:
        errors.append("frozen_input_hash_mismatch")

    cases = casebook.get("cases", [])
    ids = [c.get("id") for c in cases]
    counts = {d: sum(c.get("domain") == d for c in cases) for d in ("product", "insurance", "cross")}
    case_ok = len(cases) == 24 and len(set(ids)) == 24 and set(ids) == EXPECTED_CASE_IDS and counts == {"product": 8, "insurance": 8, "cross": 8}
    checks.append({"check": "frozen_24_case_matrix", "passed": case_ok, "counts": counts, "total": len(cases)})
    if not case_ok:
        errors.append("case_matrix_count_or_identity_mismatch")

    required_scenarios = {"PI-P02", "PI-P03", "PI-P04", "PI-P05", "PI-P06", "PI-P07", "PI-I01", "PI-I02", "PI-I04", "PI-I06", "PI-I07", "PI-X02", "PI-X03", "PI-X05", "PI-X06", "PI-X07", "PI-X08"}
    if not required_scenarios.issubset(set(ids)):
        errors.append("required_domain_scenarios_missing")
    task_ok = len(casebook.get("task_journeys", [])) == 3 and all(t.get("original_prompt") and t.get("unseen_wording") and t.get("negative_control") and t.get("completion_oracle") for t in casebook.get("task_journeys", []))
    checks.append({"check": "three_original_unseen_negative_journeys", "passed": task_ok, "count": len(casebook.get("task_journeys", []))})
    if not task_ok:
        errors.append("task_journey_contract_incomplete")

    source_security = read_json(ROOT / "outputs/security-requirements-20261008/acceptance-contract.json")
    source_ids = [x.get("id") for x in source_security.get("requirements", [])]
    copied = preserve.get("requirements", [])
    copied_ids = [x.get("id") for x in copied]
    same_security = (
        source_ids == EXPECTED_SECURITY_IDS == copied_ids
        and preserve.get("mandatory") is True
        and preserve.get("source_status") == "REQUIRED_NOT_ACCEPTED"
        and preserve.get("all_requirements_complete") is False
        and all(x.get("status") == "NOT_ACCEPTED" and x.get("pi_acceptance_credit") == "NONE_UNLESS_ORIGINAL_SECURITY_ACCEPTANCE_RUNS_AND_PASSES" for x in copied)
        and preserve.get("source", {}).get("sha256") == sha256_file(ROOT / "outputs/security-requirements-20261008/acceptance-contract.json")
    )
    checks.append({"check": "original_14_security_controls_preserved", "passed": same_security, "count": len(copied), "source_status": source_security.get("status")})
    if not same_security:
        errors.append("original_security_contract_changed_or_credited")

    api = read_json(BASE_API_PATH)
    api_sha = sha256_file(BASE_API_PATH)
    api_source = api_map.get("source_contract", {})
    if adoption:
        api_binding_ok = (api.get('contract') == 'navren.research-workspace/1'
            and api.get('status') == adoption['api_contract']['status']
            and api_sha == adoption['api_contract']['sha256']
            and api_source.get('sha256') == adoption['original_api_source']['sha256'])
    else:
        api_binding_ok = api.get("contract") == "navren.research-workspace/1" and api.get("status") == "FROZEN_V1_IMPLEMENTING" and api_source.get("sha256") == api_sha == casebook.get("input_artifacts", {}).get("api_contract", {}).get("sha256")
    checks.append({"check": "base_api_v1_hash_and_status", "passed": api_binding_ok, "sha256": api_sha, "status": api.get("status")})
    if not api_binding_ok:
        errors.append("base_api_contract_hash_or_status_mismatch")
    if api_map.get("evidence_mapping", {}).get("level_1") is None or api_map.get("evidence_mapping", {}).get("level_2") is None:
        errors.append("two_level_evidence_mapping_not_frozen")
    if api_map.get("write_safety", {}).get("default_enabled") is not False or api_map.get("write_safety", {}).get("production_cookie_or_browser_state_forbidden") is not True:
        errors.append("isolated_write_guard_not_frozen")
    api_map_sha = sha256_file(API_MAP_PATH)
    freeze = casebook.get("api_interface_freeze", {})
    api_map_hash_ok = freeze.get("path") == str(API_MAP_PATH.relative_to(ROOT)) and freeze.get("sha256") == api_map_sha
    checks.append({"check": "acceptance_api_map_hash", "passed": api_map_hash_ok, "sha256": api_map_sha})
    if not api_map_hash_ok:
        errors.append("acceptance_api_map_hash_mismatch")

    receipts_path = ROOT / "outputs/product-insurance-design-research-20261018/document-source-receipts.json"
    receipts = read_json_value(receipts_path)
    if not isinstance(receipts, list):
        raise AcceptanceError("document_source_receipts_must_be_array")
    doc_errors = []
    doc_root = ROOT / "outputs/product-insurance-design-research-20261018"
    for item in receipts:
        path = doc_root / item.get("file", "")
        if not path.is_file() or sha256_file(path) != item.get("sha256"):
            doc_errors.append(item.get("file", "unknown"))
    checks.append({"check": "archived_document_source_receipts", "passed": not doc_errors, "count": len(receipts), "mismatches": doc_errors})
    if doc_errors:
        errors.append("research_document_receipt_hash_mismatch")

    try:
        from evaluation.product_insurance.source_facts import verify_source_facts

        source_snapshot = verify_source_facts()
        source_facts_ok = not source_snapshot.get("errors")
    except Exception as exc:
        source_snapshot = {"errors": [f"source_snapshot_exception:{type(exc).__name__}"]}
        source_facts_ok = False
    checks.append({"check": "official_source_snapshot_facts", "passed": source_facts_ok, "fact_count": len(source_snapshot.get("facts", {})), "errors": source_snapshot.get("errors", [])})
    if not source_facts_ok:
        errors.append("official_source_snapshot_facts_invalid")

    report = {"status": "PASS_SPEC_INTEGRITY" if not errors else "FAIL_SPEC_INTEGRITY", "spec_integrity_only": True, "product_acceptance": False, "model_acceptance": False, "checks": checks, "errors": errors}
    if errors:
        raise AcceptanceError(json.dumps(report, ensure_ascii=False))
    return report


def isolated_entry_vertical_report(
    *,
    root: Path = ROOT,
    acceptance_dir: Path = ACCEPTANCE_OUT,
    api_contract_sha256: str | None = None,
    provider_revision: str | None = None,
) -> dict[str, Any]:
    """Verify the latest isolated HTTP+browser vertical without granting product acceptance."""
    errors: list[str] = []
    run_path = acceptance_dir / "isolated-entry-run.json"
    if not run_path.is_file():
        return {
            "status": "NOT_RUN_OR_FAILED",
            "http_status": "NOT_RUN_OR_FAILED",
            "ui_status": "NOT_RUN_OR_FAILED",
            "receipt": _display_path(run_path),
            "errors": ["isolated_entry_receipt_missing"],
            "product_acceptance": False,
        }
    try:
        run = read_json(run_path)
    except AcceptanceError as exc:
        return {
            "status": "NOT_RUN_OR_FAILED",
            "http_status": "NOT_RUN_OR_FAILED",
            "ui_status": "NOT_RUN_OR_FAILED",
            "receipt": _display_path(run_path),
            "errors": [str(exc)],
            "product_acceptance": False,
        }

    expected_api_sha = api_contract_sha256 or sha256_file(BASE_API_PATH)
    if run.get("schema") != "navren.product-insurance.isolated-entry-run/1":
        errors.append("isolated_entry_schema_mismatch")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"\d{8}T\d{12}Z", run_id):
        errors.append("isolated_entry_run_id_invalid")
    if run.get("status") != "PASS_ISOLATED_ENTRY_VERTICAL":
        errors.append("isolated_entry_run_not_passed")
    if run.get("api_contract_sha256") != expected_api_sha:
        errors.append("isolated_entry_api_contract_stale")
    if provider_revision and run.get("provider_revision") != provider_revision:
        errors.append("isolated_entry_provider_revision_stale")
    if any(run.get(key) != 0 for key in ("production_writes", "model_calls", "real_sms")):
        errors.append("isolated_entry_side_effect_boundary_mismatch")
    if run.get("private_state_persisted_to_acceptance") is not False:
        errors.append("isolated_entry_private_state_persistence_mismatch")

    evidence = run.get("evidence", {})
    report_paths: dict[str, Path] = {}
    reports: dict[str, dict[str, Any]] = {}
    for kind, expected_schema, expected_status in (
        ("http", "navren.product-insurance.http-run/1", "PASS_ISOLATED_HTTP"),
        ("ui", "navren.product-insurance.ui-run/1", "PASS_ISOLATED_UI"),
    ):
        key = f"{kind}_report"
        raw_path = evidence.get(key)
        if not isinstance(raw_path, str):
            errors.append(f"isolated_entry_{kind}_report_path_missing")
            continue
        report_path = (root / raw_path).resolve()
        try:
            report_path.relative_to(acceptance_dir.resolve())
        except ValueError:
            errors.append(f"isolated_entry_{kind}_report_outside_acceptance")
            continue
        if run_id and not report_path.stem.endswith(run_id):
            errors.append(f"isolated_entry_{kind}_report_run_id_mismatch")
        if not report_path.is_file():
            errors.append(f"isolated_entry_{kind}_report_missing")
            continue
        try:
            report = read_json(report_path)
        except AcceptanceError:
            errors.append(f"isolated_entry_{kind}_report_invalid")
            continue
        report_paths[kind] = report_path
        reports[kind] = report
        if report.get("schema") != expected_schema or report.get("status") != expected_status:
            errors.append(f"isolated_entry_{kind}_report_not_passed")
        if report.get("api_contract_sha256") != expected_api_sha:
            errors.append(f"isolated_entry_{kind}_api_contract_stale")
        if report.get("isolated_instance") is not True:
            errors.append(f"isolated_entry_{kind}_instance_not_isolated")
        if report.get("base_origin") != run.get("base_url"):
            errors.append(f"isolated_entry_{kind}_origin_mismatch")
        if not report.get("flows") or not all(flow.get("passed") is True for flow in report.get("flows", [])):
            errors.append(f"isolated_entry_{kind}_flow_failed")
        if report.get("real_model_calls") != 0 or report.get("paid_api_calls") != 0:
            errors.append(f"isolated_entry_{kind}_model_boundary_mismatch")

    screenshots: list[dict[str, Any]] = []
    ui_report = reports.get("ui", {})
    for flow in ui_report.get("flows", []):
        for step in flow.get("steps", []):
            if step.get("action") != "screenshot":
                continue
            raw_path = step.get("path")
            if not isinstance(raw_path, str) or not step.get("passed"):
                errors.append("isolated_entry_screenshot_step_invalid")
                continue
            screenshot_path = (acceptance_dir / raw_path).resolve()
            try:
                screenshot_path.relative_to(acceptance_dir.resolve())
            except ValueError:
                errors.append("isolated_entry_screenshot_outside_acceptance")
                continue
            if not screenshot_path.is_file() or sha256_file(screenshot_path) != step.get("sha256") or screenshot_path.stat().st_size != step.get("bytes"):
                errors.append("isolated_entry_screenshot_readback_mismatch")
            screenshots.append({"case_id": flow.get("case_id"), "sha256": step.get("sha256"), "bytes": step.get("bytes")})
    if not screenshots:
        errors.append("isolated_entry_screenshot_missing")
    if screenshots != evidence.get("screenshots"):
        errors.append("isolated_entry_screenshot_manifest_mismatch")

    passed = not errors
    return {
        "status": "PASS_ISOLATED_ENTRY_VERTICAL_NOT_PRODUCT_ACCEPTANCE" if passed else "NOT_RUN_OR_FAILED",
        "http_status": "PASS_ISOLATED_HTTP_VERTICAL_NOT_FULL_MATRIX" if passed else "NOT_RUN_OR_FAILED",
        "ui_status": "PASS_ISOLATED_UI_VERTICAL_NOT_PRODUCT_ACCEPTANCE" if passed else "NOT_RUN_OR_FAILED",
        "receipt": _display_path(run_path),
        "http_report": _display_path(report_paths["http"]) if "http" in report_paths else None,
        "ui_report": _display_path(report_paths["ui"]) if "ui" in report_paths else None,
        "run_id": run_id,
        "api_contract_sha256": run.get("api_contract_sha256"),
        "provider_revision": run.get("provider_revision"),
        "http_requests": sum(len(flow.get("steps", [])) for flow in reports.get("http", {}).get("flows", [])),
        "browser_sessions": 1 if passed and reports.get("ui", {}).get("flows") else 0,
        "screenshot_count": len(screenshots),
        "errors": errors,
        "product_acceptance": False,
    }


def readiness_report() -> dict[str, Any]:
    frozen_integrity: dict[str, Any]
    try:
        frozen_integrity = validate_frozen_contracts()
    except AcceptanceError as exc:
        try:
            frozen_integrity = json.loads(str(exc))
        except json.JSONDecodeError:
            frozen_integrity = {"status": "FAIL_SPEC_INTEGRITY", "errors": [str(exc)]}
    required = [
        "research_evidence.py",
        "data/research_evidence/manifest.json",
        "research_methods.py",
        "research_workspace.py",
        "agent_runtime/research_tools.py",
        "js/research_workspace.js",
        "css/research_workspace.css",
        "outputs/product-insurance-implementation-20261018/rag/provider-contract.json",
        "outputs/product-insurance-implementation-20261018/rag/delivery.json",
        "outputs/product-insurance-implementation-20261018/methods/contract.json",
        "outputs/product-insurance-implementation-20261018/agent/task-contract.json",
        "outputs/product-insurance-implementation-20261018/ui/consumer-contract.json",
        "outputs/product-insurance-implementation-20261018/acceptance/http-adapter.json",
        "outputs/product-insurance-implementation-20261018/acceptance/ui-adapter.json",
    ]
    missing = [p for p in required if not (ROOT / p).is_file()]
    api_state = read_json(BASE_API_PATH).get("status")
    blockers = []
    if frozen_integrity.get("status") != "PASS_SPEC_INTEGRITY":
        blockers.append({"id": "FROZEN_INPUT_INTEGRITY_FAILED", "status": frozen_integrity.get("status"), "errors": frozen_integrity.get("errors", [])})
    if missing:
        blockers.append({"id": "IMPLEMENTATION_OR_ADAPTERS_MISSING", "paths": missing})
    if not isinstance(api_state, str) or not api_state.startswith("FROZEN_V1_"):
        blockers.append({"id": "BASE_API_NOT_FROZEN_V1", "observed": api_state})
    rag_delivery_path = ROOT / "outputs/product-insurance-implementation-20261018/rag/delivery.json"
    rag_contract_path = ROOT / "outputs/product-insurance-implementation-20261018/rag/provider-contract.json"
    rag_owner_smoke_path = ROOT / "outputs/product-insurance-implementation-20261018/rag/provider-smoke.json"
    rag_acceptance_smoke_path = ACCEPTANCE_OUT / "source-provider-smoke.json"
    methods_contract_path = ROOT / "outputs/product-insurance-implementation-20261018/methods/contract.json"
    ui_contract_path = ROOT / "outputs/product-insurance-implementation-20261018/ui/consumer-contract.json"
    rag_delivery = read_json(rag_delivery_path) if rag_delivery_path.is_file() else {}
    rag_contract = read_json(rag_contract_path) if rag_contract_path.is_file() else {}
    rag_owner_smoke = read_json(rag_owner_smoke_path) if rag_owner_smoke_path.is_file() else {}
    rag_smoke = read_json(rag_acceptance_smoke_path) if rag_acceptance_smoke_path.is_file() else rag_owner_smoke
    pension_publication = read_json(PENSION_PUBLICATION_PATH) if PENSION_PUBLICATION_PATH.is_file() else {}
    content = rag_delivery.get("content", rag_delivery.get("content_actual", {}))
    smoke_ids_value = rag_smoke.get("entity_ids", [])
    smoke_ids = smoke_ids_value if isinstance(smoke_ids_value, list) else []
    expected_ids_value = rag_smoke.get("expected_entity_ids", smoke_ids)
    expected_deep_ids = set(expected_ids_value) if isinstance(expected_ids_value, list) else set()
    ids_valid = rag_smoke.get("status") in {"MODULE_PROVIDER_SMOKE_PASS_NOT_PRODUCT_ACCEPTANCE", "PUBLISHED_ARTIFACT_READBACK_PASS_NOT_END_TO_END"} and bool(expected_deep_ids) and set(smoke_ids) == expected_deep_ids and len(smoke_ids) == len(expected_deep_ids)
    revision_aligned = bool(rag_delivery.get("provider_revision")) and rag_delivery.get("provider_revision") == rag_smoke.get("provider_revision")
    smoke_counts = rag_smoke.get("entity_counts", {})
    deep_content = {
        "channel_rows": content.get("channel_rows_preserved", content.get("channel_rows", 0)),
        "fund_deep_entities": smoke_counts.get("fund", 0),
        "insurance_deep_entities": smoke_counts.get("protection_insurance", smoke_counts.get("insurance", 0)),
        "pension_samples": smoke_counts.get("pension_sample", smoke_counts.get("pension", 0)),
        "commercial_pension_product_research_packages": smoke_counts.get("commercial_pension_research", content.get("commercial_pension_product_research_packages", content.get("commercial_pension_product_packages", 0))),
        "deep_entities_reported": content.get("deep_entities"),
        "current_complete_commercial_pension_packages": content.get("current_complete_commercial_pension_packages", 0),
    }
    content_minimums = {"channel_rows": 13680, "fund_deep_entities": 4, "insurance_deep_entities": 2, "pension_samples": 1, "commercial_pension_product_research_packages": 1}
    content_gaps = {key: {"actual": deep_content.get(key, 0), "minimum": minimum} for key, minimum in content_minimums.items() if not isinstance(deep_content.get(key), int) or deep_content.get(key, 0) < minimum}
    admission = rag_contract.get("admission", {})
    pension_scope = summarize_pension_research_scope(
        pension_publication,
        current_provider_revision=rag_delivery.get("provider_revision"),
        smoke_provider_revision=rag_smoke.get("provider_revision"),
        smoke_entity_ids=smoke_ids,
        smoke_entities=rag_smoke.get("entities", []),
        smoke_errors=rag_smoke.get("errors", []),
        original_document_bytes_verified=rag_smoke.get("original_document_bytes_verified") is True,
    )
    coverage_ready = not content_gaps and ids_valid and revision_aligned and deep_content.get("deep_entities_reported") == len(expected_deep_ids)
    pension_research_sample_ready = coverage_ready and pension_scope["read_only_research_sample_available"]
    rag_complete = pension_research_sample_ready and admission.get("full_framework_complete") is True and not rag_delivery.get("pending")
    if not rag_complete:
        blockers.append({"id": "CURRENT_RAG_DELIVERY_INCOMPLETE", "status": rag_delivery.get("status"), "content_actual": deep_content, "content_gaps": content_gaps, "smoke_ids_match_expected": ids_valid, "provider_revision_matches_smoke": revision_aligned, "research_only_pension_sample_available": pension_research_sample_ready, "admission_full_framework_complete": admission.get("full_framework_complete"), "pending": rag_delivery.get("pending", [])})
    methods_contract = read_json(methods_contract_path) if methods_contract_path.is_file() else {}
    methods_sha_ok = bool(methods_contract) and methods_contract.get("module_SHA256") == sha256_file(ROOT / "research_methods.py")
    if methods_contract and not methods_sha_ok:
        blockers.append({"id": "METHODS_CONTRACT_SOURCE_HASH_MISMATCH", "contract_sha256": methods_contract.get("module_SHA256"), "source_sha256": sha256_file(ROOT / "research_methods.py") if (ROOT / "research_methods.py").is_file() else None})
    ui_contract = read_json(ui_contract_path) if ui_contract_path.is_file() else {}
    ui_bound = ui_contract.get("status") in {"FROZEN_CONSUMER_CONTRACT", "FROZEN_CONSUMER_CONTRACT_IMPLEMENTING", "READY_FOR_ISOLATED_UI_ACCEPTANCE"} and ui_contract.get("http_paths")
    if ui_contract and not ui_bound:
        blockers.append({"id": "UI_CONSUMER_CONTRACT_NOT_BOUND_TO_FROZEN_HTTP_API", "status": ui_contract.get("status"), "http_paths_present": bool(ui_contract.get("http_paths"))})
    base_delivery_path = ROOT / "outputs/product-insurance-implementation-20261018/base/delivery.json"
    base_delivery = read_json(base_delivery_path) if base_delivery_path.is_file() else {}
    base_complete = base_delivery.get("overall_accepted") is True and not base_delivery.get("in_progress") and not base_delivery.get("blocked_dependencies")
    if not base_complete:
        blockers.append({"id": "BASE_FULL_FRAMEWORK_NOT_ACCEPTED", "status": base_delivery.get("status"), "overall_accepted": base_delivery.get("overall_accepted"), "in_progress": base_delivery.get("in_progress", []), "blocked_dependencies": base_delivery.get("blocked_dependencies", [])})
    security = read_json(SECURITY_PATH)
    if security.get("source_status") != "ACCEPTED" or security.get("all_requirements_complete") is not True:
        blockers.append({"id": "ORIGINAL_14_SECURITY_CONTROLS_NOT_ACCEPTED", "source_status": security.get("source_status"), "accepted_count": sum(x.get("status") == "ACCEPTED" for x in security.get("requirements", [])), "required_count": len(security.get("requirements", []))})
    transaction_protocol = read_json(ROOT / "evaluation/product_insurance/transaction-fault-protocol.json")
    if transaction_protocol.get("operation_receipt_subassertion", {}).get("status") != "ACCEPTED":
        blockers.append({"id": "PI_X06_OPERATION_RECEIPT_MAPPING_PENDING", "status": transaction_protocol.get("operation_receipt_subassertion", {}).get("status")})
    task_journeys = {x.get("id"): x for x in read_json(CASEBOOK_PATH).get("task_journeys", [])}
    t01 = task_journeys.get("PI-T01", {})
    if any("reserve_funds" in str(x) or "reserve operation" in str(x) for x in t01.get("current_blockers", [])):
        blockers.append({"id": "PI_T01_CONFIRMED_RESERVE_FLOW_PENDING", "current_blockers": t01.get("current_blockers", [])})
    test_receipt_path = ACCEPTANCE_OUT / "test-run.json"
    source_smoke_receipt_path = ACCEPTANCE_OUT / "source-provider-smoke.json"
    test_receipt = read_json(test_receipt_path) if test_receipt_path.is_file() else {}
    source_smoke_receipt = read_json(source_smoke_receipt_path) if source_smoke_receipt_path.is_file() else {}
    isolated_entry = isolated_entry_vertical_report(
        api_contract_sha256=sha256_file(BASE_API_PATH),
        provider_revision=source_smoke_receipt.get("provider_revision"),
    )
    test_subset_pass = test_receipt.get("status") == "PASS_TESTS" and test_receipt.get("inputs_unchanged") is True
    http_vertical_pass = test_subset_pass and test_receipt.get("isolated_http_vertical") == "PASS_SYNTHETIC_ROUTE_TEST_NOT_FULL_MATRIX"
    provider_subset_pass = source_smoke_receipt.get("status") == "MODULE_PROVIDER_SMOKE_PASS_NOT_PRODUCT_ACCEPTANCE" and source_smoke_receipt.get("inputs_unchanged") is True and not source_smoke_receipt.get("errors")
    missing_adapters = {"http": "outputs/product-insurance-implementation-20261018/acceptance/http-adapter.json", "ui": "outputs/product-insurance-implementation-20261018/acceptance/ui-adapter.json"}
    engineering_subtests = {
        "status": "PASS_PARTIAL_ENGINEERING_EVIDENCE" if test_subset_pass and provider_subset_pass else "INCOMPLETE_ENGINEERING_EVIDENCE",
        "unit_and_fault_isolation": {"status": "PASS_NOT_PRODUCT_ACCEPTANCE" if test_subset_pass else "NOT_RUN_OR_FAILED", "receipt": str(test_receipt_path.relative_to(ROOT)) if test_receipt else None, "test_count": test_receipt.get("test_count")},
        "current_public_provider_binding": {"status": "PASS_MODULE_SMOKE_NOT_PRODUCT_ACCEPTANCE" if provider_subset_pass else "NOT_RUN_OR_FAILED", "receipt": str(source_smoke_receipt_path.relative_to(ROOT)) if source_smoke_receipt else None, "provider_revision": source_smoke_receipt.get("provider_revision"), "entities": source_smoke_receipt.get("entity_counts")},
        "isolated_proxy_route_tests": {"status": "PASS_SYNTHETIC_ROUTE_VERTICAL_NOT_FULL_MATRIX" if http_vertical_pass else "NOT_RUN_OR_FAILED", "receipt": str(test_receipt_path.relative_to(ROOT)) if test_receipt else None},
        "isolated_entry_vertical": isolated_entry,
        "isolated_http_flows": {"status": isolated_entry["http_status"], "receipt": isolated_entry.get("receipt"), "report": isolated_entry.get("http_report")},
        "isolated_ui_flows": {"status": isolated_entry["ui_status"], "receipt": isolated_entry.get("receipt"), "report": isolated_entry.get("ui_report")},
    }
    historical_source_audit = read_json(ROOT / "outputs/product-insurance-design-research-20261018/catalog-audit-report.json")
    historical_statuses = {str(c.get("status")) for c in historical_source_audit.get("checks", [])}
    return {
        "schema": "navren.product-insurance.acceptance-readiness/1",
        "task_id": TASK_ID,
        "status": "BLOCKED_WAITING_FOR_OWNER_DELIVERIES" if blockers else "READY_FOR_ISOLATED_JOINT_TEST_ONLY",
        "spec_integrity": frozen_integrity.get("status", "FAIL_SPEC_INTEGRITY"),
        "product_acceptance": False,
        "current_api_contract_status": api_state,
        "engineering_subtests": engineering_subtests,
        "final_acceptance_gates": {
            "real_model_and_agent_integration": "BLOCKED_AGENT_ORIGINAL_REVIEW_INTEGRATION_MISSING" if "agent_runtime/research_tools.py" in missing else "NOT_ACCEPTED",
            "original_14_security_controls": "NOT_ACCEPTED",
            "full_24_case_matrix_and_three_journeys": "NOT_RUN_ON_ONE_SAME_VERSION_HTTP_UI_ENTRY",
            "current_entry_and_deployment": "NOT_ACCEPTED",
            "product_acceptance": False,
        },
        "current_deliveries": {
            "rag": {"delivery_sha256": sha256_file(rag_delivery_path) if rag_delivery_path.is_file() else None, "provider_contract_sha256": sha256_file(rag_contract_path) if rag_contract_path.is_file() else None, "acceptance_smoke_sha256": sha256_file(rag_acceptance_smoke_path) if rag_acceptance_smoke_path.is_file() else None, "previous_owner_smoke_sha256": sha256_file(rag_owner_smoke_path) if rag_owner_smoke_path.is_file() else None, "previous_owner_smoke_revision": rag_owner_smoke.get("provider_revision"), "previous_owner_smoke_matches_delivery": rag_owner_smoke.get("provider_revision") == rag_delivery.get("provider_revision"), "provider_revision": rag_delivery.get("provider_revision"), "smoke_revision": rag_smoke.get("provider_revision"), "content_actual": deep_content, "entity_ids_match_expected": ids_valid, "research_only_pension_sample_ready": pension_research_sample_ready, "full_framework_complete": rag_complete, "complete_for_full_matrix": rag_complete},
            "base": {"delivery_sha256": sha256_file(base_delivery_path) if base_delivery_path.is_file() else None, "status": base_delivery.get("status"), "overall_accepted": base_delivery.get("overall_accepted"), "complete_for_full_matrix": base_complete},
            "methods": {"contract_sha256": sha256_file(methods_contract_path) if methods_contract_path.is_file() else None, "source_sha256": sha256_file(ROOT / "research_methods.py") if (ROOT / "research_methods.py").is_file() else None, "hash_match": methods_sha_ok},
            "ui": {"consumer_contract_sha256": sha256_file(ui_contract_path) if ui_contract_path.is_file() else None, "status": ui_contract.get("status"), "bound_to_http": bool(ui_bound)},
        },
        "historical_research_audit": {"path": "outputs/product-insurance-design-research-20261018/catalog-audit-report.json", "role": "baseline_expectations_and_negative_cases_only; current readiness is determined from versioned RAG delivery and implementation contracts", "observed_check_statuses": sorted(historical_statuses), "blocks_current_readiness": False},
        "scope_limits": {"commercial_pension": pension_scope, "interpretation": "A source-bound read-only pension research sample can be tested while current contract, eligibility, personal cash value, net-payment quote, or future IRR remain unavailable. These missing facts block only outputs that depend on them; they do not erase the published research sample."},
        "blockers": blockers,
        "execution": {"http_requests": test_receipt.get("http_requests", {}).get("total_loopback_requests", 0), "http_request_scope": "isolated loopback route tests only; not live product traffic", "isolated_entry_http_requests": isolated_entry.get("http_requests", 0), "browser_sessions": isolated_entry.get("browser_sessions", 0), "model_calls": 0, "paid_calls": 0, "production_writes": 0, "service_restarts": 0},
        "next_acceptance_gate": "Keep running executable free engineering subtests without waiting for model or global-security acceptance. For full product acceptance, complete the missing Agent integration and operation-receipt mapping, obtain remaining source-backed positive cases, then run all 24 cases and three journeys on one same-version temporary identity and database set.",
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def status_report() -> dict[str, Any]:
    try:
        spec = validate_frozen_contracts()
    except AcceptanceError as exc:
        try:
            spec = json.loads(str(exc))
        except json.JSONDecodeError:
            spec = {"status": "FAIL_SPEC_INTEGRITY", "errors": [str(exc)]}
    readiness = readiness_report()
    test_receipt_path = ACCEPTANCE_OUT / "test-run.json"
    source_smoke_receipt_path = ACCEPTANCE_OUT / "source-provider-smoke.json"
    test_receipt = read_json(test_receipt_path) if test_receipt_path.is_file() else {}
    source_smoke_receipt = read_json(source_smoke_receipt_path) if source_smoke_receipt_path.is_file() else {}
    tests_pass = test_receipt.get("status") == "PASS_TESTS" and test_receipt.get("inputs_unchanged") is True
    source_smoke_pass = source_smoke_receipt.get("status") == "MODULE_PROVIDER_SMOKE_PASS_NOT_PRODUCT_ACCEPTANCE" and source_smoke_receipt.get("inputs_unchanged") is True and not source_smoke_receipt.get("errors")
    partial_engineering_pass = tests_pass and source_smoke_pass
    return {
        "schema": "navren.product-insurance.acceptance-status/1",
        "task_id": TASK_ID,
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "status": "PARTIAL_ENGINEERING_EVIDENCE_PASS_FULL_ACCEPTANCE_BLOCKED" if partial_engineering_pass and readiness["blockers"] else "READY_FOR_ISOLATED_JOINT_TEST_ONLY" if not readiness["blockers"] else "ENGINEERING_EVIDENCE_INCOMPLETE",
        "spec_integrity": spec.get("status"),
        "partial_engineering_evidence": {
            "status": "PASS" if partial_engineering_pass else "INCOMPLETE",
            "unit_and_fault_isolation": {"status": test_receipt.get("status", "NOT_RUN"), "test_count": test_receipt.get("test_count"), "inputs_unchanged": test_receipt.get("inputs_unchanged"), "receipt": "outputs/product-insurance-implementation-20261018/acceptance/test-run.json" if test_receipt else None},
            "current_public_provider_binding": {"status": source_smoke_receipt.get("status", "NOT_RUN"), "provider_revision": source_smoke_receipt.get("provider_revision"), "entity_counts": source_smoke_receipt.get("entity_counts"), "two_level_mapping_errors": source_smoke_receipt.get("two_level_mapping_errors"), "inputs_unchanged": source_smoke_receipt.get("inputs_unchanged"), "receipt": "outputs/product-insurance-implementation-20261018/acceptance/source-provider-smoke.json" if source_smoke_receipt else None},
            "isolated_entry_vertical": readiness["engineering_subtests"].get("isolated_entry_vertical"),
        },
        "product_acceptance": False,
        "real_model_acceptance": False,
        "deployment": False,
        "current_entry_acceptance": False,
        "original_security_controls": {"accepted": 0, "required": len(EXPECTED_SECURITY_IDS), "status": "REQUIRED_NOT_ACCEPTED"},
        "integrated_matrix": {"domain_cases": 24, "task_journeys": 3, "status": "NOT_RUN_ON_ONE_SAME_VERSION_HTTP_UI_ENTRY"},
        "blockers": readiness.get("blockers", []),
        "readiness": readiness,
        "evidence": [
            {"path": "outputs/product-insurance-implementation-20261018/acceptance/readiness.json", "sha256": sha256_file(ACCEPTANCE_OUT / "readiness.json") if (ACCEPTANCE_OUT / "readiness.json").is_file() else None},
            {"path": "outputs/product-insurance-implementation-20261018/acceptance/test-run.json", "sha256": sha256_file(test_receipt_path) if test_receipt_path.is_file() else None},
            {"path": "outputs/product-insurance-implementation-20261018/acceptance/source-provider-smoke.json", "sha256": sha256_file(source_smoke_receipt_path) if source_smoke_receipt_path.is_file() else None},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Independent, fail-closed PI-ACCEPT harness.")
    parser.add_argument("command", choices=("validate", "readiness", "status"))
    args = parser.parse_args()
    try:
        if args.command == "validate":
            report = validate_frozen_contracts()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        report = readiness_report()
        write_json(ACCEPTANCE_OUT / "readiness.json", report)
        if args.command == "status":
            report = status_report()
            write_json(ACCEPTANCE_OUT / "status.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if report["blockers"] else 0
    except AcceptanceError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
