from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from typing import Any

from evaluation.product_insurance.http_runner import is_loopback_url, load_isolation_manifest
from evaluation.product_insurance.harness import ACCEPTANCE_OUT, ROOT, AcceptanceError, read_json, sha256_file, write_json


API_CONTRACT = ROOT / "outputs/product-insurance-implementation-20261018/base/api-contract.json"


def _check_ui_adapter(adapter: dict[str, Any]) -> None:
    if adapter.get("schema") != "navren.pi.ui-adapter/1":
        raise AcceptanceError("ui_adapter_schema_mismatch")
    contract = ROOT / adapter.get("api_contract_path", "")
    if not contract.is_file() or contract.resolve() != API_CONTRACT.resolve() or sha256_file(contract) != adapter.get("api_contract_sha256"):
        raise AcceptanceError("ui_adapter_api_contract_hash_mismatch")
    if not isinstance(adapter.get("flows"), list) or not adapter["flows"]:
        raise AcceptanceError("ui_adapter_has_no_flows")


def _locator(page: Any, step: dict[str, Any]) -> Any:
    kind = step.get("selector")
    if kind == "role":
        return page.get_by_role(step["role"], name=step["name"], exact=step.get("exact", True))
    if kind == "label":
        return page.get_by_label(step["name"], exact=step.get("exact", True))
    if kind == "text":
        return page.get_by_text(step["name"], exact=step.get("exact", True))
    if kind == "test_id":
        return page.get_by_test_id(step["name"])
    raise AcceptanceError(f"unsupported_ui_selector:{kind}")


def _safe_location(value: str) -> str:
    parsed = urlsplit(value)
    return parsed.path or "/"


def run_adapter(adapter_path: Path, *, base_url_override: str | None = None, allow_isolated_writes: bool = False) -> dict[str, Any]:
    adapter = read_json(adapter_path)
    _check_ui_adapter(adapter)
    base_url = base_url_override or os.environ.get(adapter.get("base_url_env", "NAVREN_PI_BASE_URL"), "")
    if not is_loopback_url(base_url):
        raise AcceptanceError("ui_base_url_must_be_loopback_with_explicit_port")
    mutates = any(flow.get("mutates") or any(step.get("mutates") for step in flow.get("steps", [])) for flow in adapter["flows"])
    manifest = load_isolation_manifest(adapter.get("isolation_manifest_env", "NAVREN_PI_ISOLATED_MANIFEST"), required=mutates)
    if mutates and (not allow_isolated_writes or os.environ.get("NAVREN_PI_ALLOW_ISOLATED_WRITES") != "1"):
        raise AcceptanceError("ui_mutations_disabled")
    if mutates and (manifest is None or manifest.get("operation_gates_enabled") is not True):
        raise AcceptanceError("ui_isolated_operation_gates_missing")

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise AcceptanceError(f"playwright_python_unavailable:{type(exc).__name__}") from exc

    results: list[dict[str, Any]] = []
    api_sha = sha256_file(API_CONTRACT)
    with sync_playwright() as playwright:
        browser_name = adapter.get("browser", "chromium")
        browser_type = getattr(playwright, browser_name, None)
        if browser_type is None:
            raise AcceptanceError(f"unsupported_browser:{browser_name}")
        launch_options: dict[str, Any] = {"headless": adapter.get("headless", True)}
        if adapter.get("channel"):
            launch_options["channel"] = adapter["channel"]
        browser = browser_type.launch(**launch_options)
        try:
            for flow in adapter["flows"]:
                storage_state = None
                if flow.get("uses_private_identity"):
                    if manifest is None or manifest.get("test_storage_state_isolated") is not True:
                        raise AcceptanceError("private_ui_flow_requires_isolated_test_storage_state")
                    state_path = Path(manifest.get("test_storage_state_path", "")).expanduser().resolve()
                    temp_root = Path(tempfile.gettempdir()).resolve()
                    if not state_path.is_file() or not _under(state_path, temp_root):
                        raise AcceptanceError("private_ui_storage_state_outside_temp_scope")
                    expected = manifest.get("test_storage_state_sha256")
                    if not expected or sha256_file(state_path) != expected:
                        raise AcceptanceError("private_ui_storage_state_hash_mismatch")
                    storage_state = str(state_path)
                context = browser.new_context(locale="zh-CN", storage_state=storage_state, service_workers="block")
                page = context.new_page()
                blocked_external: list[str] = []

                def guard_route(route: Any) -> None:
                    request_url = route.request.url
                    parsed = urlsplit(request_url)
                    if parsed.scheme in {"http", "https"} and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                        blocked_external.append(parsed.hostname or "unknown")
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", guard_route)
                step_results: list[dict[str, Any]] = []
                flow_passed = True
                for step in flow.get("steps", []):
                    action = step.get("action")
                    record: dict[str, Any] = {"action": action, "passed": False}
                    try:
                        if action == "goto":
                            target = urljoin(base_url.rstrip("/") + "/", step["path"].lstrip("/"))
                            if not is_loopback_url(target) or urlsplit(target).netloc != urlsplit(base_url).netloc:
                                raise AcceptanceError("ui_navigation_left_loopback_origin")
                            response = page.goto(target, wait_until="domcontentloaded", timeout=step.get("timeout_ms", 10000))
                            record.update({"path": _safe_location(page.url), "status": response.status if response else None})
                            record["passed"] = response is not None and response.status == step.get("expected_status", 200)
                        elif action == "click":
                            _locator(page, step).click(timeout=step.get("timeout_ms", 5000))
                            record["passed"] = True
                        elif action == "fill":
                            _locator(page, step).fill(str(step.get("value", "")), timeout=step.get("timeout_ms", 5000))
                            record["passed"] = True
                        elif action == "select":
                            _locator(page, step).select_option(value=str(step["value"]), timeout=step.get("timeout_ms", 5000))
                            record["passed"] = True
                        elif action == "press":
                            _locator(page, step).press(step["key"], timeout=step.get("timeout_ms", 5000))
                            record["passed"] = True
                        elif action == "expect_text":
                            _locator(page, step).wait_for(state="visible", timeout=step.get("timeout_ms", 5000))
                            record["passed"] = True
                        elif action == "expect_absent_text":
                            record["passed"] = _locator(page, step).count() == 0
                        elif action == "expect_value":
                            record["passed"] = _locator(page, step).input_value() == step["value"]
                        elif action == "expect_url":
                            record["passed"] = bool(re.search(step["pattern"], page.url))
                            record["path"] = _safe_location(page.url)
                        elif action == "screenshot":
                            relative = Path(step["path"])
                            target = (ACCEPTANCE_OUT / "ui-evidence" / relative).resolve()
                            if not _under(target, (ACCEPTANCE_OUT / "ui-evidence").resolve()):
                                raise AcceptanceError("ui_screenshot_path_outside_acceptance_scope")
                            target.parent.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(target), full_page=step.get("full_page", True))
                            record.update({"passed": True, "path": str(target.relative_to(ACCEPTANCE_OUT)), "sha256": sha256_file(target), "bytes": target.stat().st_size})
                        else:
                            raise AcceptanceError(f"unsupported_ui_action:{action}")
                    except Exception as exc:
                        record["error_type"] = type(exc).__name__
                        record["passed"] = False
                    step_results.append(record)
                    flow_passed = flow_passed and record["passed"]
                    if not record["passed"]:
                        break
                if blocked_external:
                    flow_passed = False
                results.append({"case_id": flow.get("case_id"), "passed": flow_passed, "blocked_external_hosts": sorted(set(blocked_external)), "steps": step_results})
                context.close()
        finally:
            browser.close()
    return {"schema": "navren.product-insurance.ui-run/1", "status": "PASS_ISOLATED_UI" if all(x["passed"] for x in results) else "FAIL_ISOLATED_UI", "api_contract_sha256": api_sha, "base_origin": base_url, "isolated_instance": bool(manifest), "model_acceptance": False, "real_model_calls": 0, "paid_api_calls": 0, "flows": results}


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="PI-ACCEPT semantic-role UI runner; loopback and isolated identity only.")
    parser.add_argument("--adapter", type=Path, default=ACCEPTANCE_OUT / "ui-adapter.json")
    parser.add_argument("--base-url")
    parser.add_argument("--allow-isolated-writes", action="store_true")
    args = parser.parse_args()
    try:
        report = run_adapter(args.adapter, base_url_override=args.base_url, allow_isolated_writes=args.allow_isolated_writes)
        write_json(ACCEPTANCE_OUT / "ui-last-run.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "PASS_ISOLATED_UI" else 1
    except AcceptanceError as exc:
        blocked = {"schema": "navren.product-insurance.ui-run/1", "status": "BLOCKED", "reason": str(exc), "model_acceptance": False, "real_model_calls": 0, "paid_api_calls": 0}
        write_json(ACCEPTANCE_OUT / "ui-last-run.json", blocked)
        print(json.dumps(blocked, ensure_ascii=False, indent=2))
        return 2
    except Exception as exc:
        blocked = {"schema": "navren.product-insurance.ui-run/1", "status": "BLOCKED", "reason": f"runner_error:{type(exc).__name__}", "model_acceptance": False, "real_model_calls": 0, "paid_api_calls": 0}
        write_json(ACCEPTANCE_OUT / "ui-last-run.json", blocked)
        print(json.dumps(blocked, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
