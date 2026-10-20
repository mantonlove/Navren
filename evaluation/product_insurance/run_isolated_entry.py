from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.request import HTTPCookieProcessor, ProxyHandler, build_opener, Request

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from evaluation.product_insurance.harness import ACCEPTANCE_OUT, read_json, sha256_file, write_json
from evaluation.product_insurance.http_runner import API_CONTRACT as HTTP_API_CONTRACT, run_adapter as run_http_adapter
from evaluation.product_insurance.ui_runner import API_CONTRACT as UI_API_CONTRACT, run_adapter as run_ui_adapter
from research_evidence import ResearchEvidenceStore
from test_chat_media_wiring import ChatMediaWiringTests
from test_sms_auth import PASSWORD, PHONE, TestTransport
from welcome_auth import create_welcome_server

TASK_ROOT = ROOT / "outputs/product-insurance-implementation-20261018"
RUNTIME_RECEIPT = ACCEPTANCE_OUT / "isolated-entry-run.json"
HTTP_ADAPTER = ACCEPTANCE_OUT / "http-adapter.json"
UI_ADAPTER = ACCEPTANCE_OUT / "ui-adapter.json"


def _load_retention_client():
    path = ROOT / "output/playwright/login-retention-20261018/retention_check.py"
    spec = importlib.util.spec_from_file_location("pi_accept_retention_client", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("retention_test_client_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Client


def _client_call(client, path: str, payload: dict | None = None):
    headers = {"Origin": client.base, "Accept": "application/json"}
    if client.csrf:
        headers["X-Welcome-CSRF"] = client.csrf
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    else:
        data = None
    request = Request(client.base + path, data=data, headers=headers)
    with client.http.open(request, timeout=20) as response:
        result = json.loads(response.read())
    if path.startswith("/api/welcome/auth/") and result.get("csrf"):
        client.csrf = result["csrf"]
    return result


def _browser_storage_state(cookie_jar: CookieJar) -> dict:
    cookies = []
    for item in cookie_jar:
        cookies.append({
            "name": item.name,
            "value": item.value,
            "domain": "127.0.0.1",
            "path": item.path or "/",
            "expires": item.expires if item.expires is not None else -1,
            "httpOnly": item.has_nonstandard_attr("HttpOnly"),
            "secure": bool(item.secure),
            "sameSite": "Lax",
        })
    return {"cookies": cookies, "origins": []}


def _adapters(api_sha256: str, provider_revision: str) -> tuple[dict, dict]:
    http = {
        "schema": "navren.pi.http-adapter/1",
        "api_contract_path": str(HTTP_API_CONTRACT.relative_to(ROOT)),
        "api_contract_sha256": api_sha256,
        "base_url_env": "NAVREN_PI_BASE_URL",
        "isolation_manifest_env": "NAVREN_PI_ISOLATED_MANIFEST",
        "test_cookie_env": "NAVREN_PI_TEST_COOKIE",
        "test_csrf_env": "NAVREN_PI_TEST_CSRF",
        "flows": [
            {
                "case_id": "PI-ENTRY-HTTP-PUBLIC-CATALOG-DETAIL",
                "steps": [
                    {
                        "id": "catalog",
                        "method": "GET",
                        "path": "/api/finance/research/catalog",
                        "private": True,
                        "query": {"limit": 1, "offset": 0},
                        "timeout_seconds": 20,
                        "expected": {"status": 200, "json_fields": {"$.contract": "navren.research-workspace/1", "$.data_scope": "public", "$.provider_revision": provider_revision}},
                    },
                    {
                        "id": "clpc-detail",
                        "method": "GET",
                        "path": "/api/finance/research/entities/insurance:CN:CLPC:2026-001:A",
                        "private": True,
                        "timeout_seconds": 20,
                        "expected": {"status": 200, "json_fields": {"$.contract": "navren.research-workspace/1", "$.data_scope": "public", "$.entity_id": "insurance:CN:CLPC:2026-001:A", "$.provider_revision": provider_revision}},
                    },
                ],
            },
            {
                "case_id": "PI-ENTRY-HTTP-PRIVATE-IDENTITY-GATE",
                "steps": [
                    {"id": "anonymous-workspace", "method": "GET", "path": "/api/finance/research/workspace", "timeout_seconds": 10, "expected": {"status": 401}},
                    {"id": "registered-workspace", "method": "GET", "path": "/api/finance/research/workspace", "private": True, "timeout_seconds": 20, "expected": {"status": 200}},
                ],
            },
        ],
    }
    ui = {
        "schema": "navren.pi.ui-adapter/1",
        "api_contract_path": str(UI_API_CONTRACT.relative_to(ROOT)),
        "api_contract_sha256": api_sha256,
        "base_url_env": "NAVREN_PI_BASE_URL",
        "isolation_manifest_env": "NAVREN_PI_ISOLATED_MANIFEST",
        "browser": "chromium",
        "channel": "chrome",
        "headless": True,
        "flows": [
            {
                "case_id": "PI-ENTRY-HOME-CAPABILITY-ACTIVATION",
                "uses_private_identity": True,
                "steps": [
                    {"action": "goto", "path": "/recommend.html", "expected_status": 200, "timeout_ms": 20000},
                    {"action": "expect_text", "selector": "text", "name": "现金安排", "timeout_ms": 20000},
                    {"action": "expect_text", "selector": "text", "name": "中长期投资", "timeout_ms": 10000},
                    {"action": "expect_text", "selector": "text", "name": "保障研究", "timeout_ms": 10000},
                    {"action": "screenshot", "path": "home-capability-activation.png", "full_page": True},
                    {"action": "click", "selector": "text", "name": "现金安排", "timeout_ms": 10000},
                    {"action": "expect_text", "selector": "role", "role": "tab", "name": "产品库", "timeout_ms": 20000},
                    {"action": "expect_text", "selector": "role", "role": "tab", "name": "我的研究", "timeout_ms": 10000},
                    {"action": "screenshot", "path": "research-route.png", "full_page": True},
                ],
            }
        ],
    }
    return http, ui


def run() -> dict:
    ACCEPTANCE_OUT.mkdir(parents=True, exist_ok=True)
    (ACCEPTANCE_OUT / "tmp").mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    api_sha = sha256_file(HTTP_API_CONTRACT)
    if UI_API_CONTRACT.resolve() != HTTP_API_CONTRACT.resolve() or sha256_file(UI_API_CONTRACT) != api_sha:
        raise RuntimeError("http_ui_api_contract_binding_mismatch")

    fixture = ChatMediaWiringTests()
    welcome = None
    welcome_thread = None
    transport = TestTransport()
    try:
        fixture.setUp()
        public_root = fixture.state / "public-evidence"
        import shutil
        shutil.copytree(ROOT / "data/research_evidence", public_root)
        fixture.server.finance.research_workspace._provider = ResearchEvidenceStore(root=public_root)
        welcome = create_welcome_server(
            0,
            fixture.state / "welcome-auth.sqlite3",
            root=ROOT,
            upstream_port=fixture.server.server_port,
            sms_transport=transport,
            allow_test_sms=True,
            experience_provider=fixture.server.finance.experience_provider,
            clock=fixture.server.finance.bank.clock,
        )
        welcome_thread = threading.Thread(target=welcome.serve_forever, daemon=True)
        welcome_thread.start()

        client_type = _load_retention_client()
        client = client_type(welcome.server_port)
        registered = client.register(transport)
        _client_call(client, "/api/welcome/auth/experience", {"user_id": "working_landlady"})
        finance = _client_call(client, "/api/finance/session")
        provider_revision = fixture.server.finance.research_workspace._provider.revision()

        storage_path = fixture.state / "browser-storage-state.json"
        storage_value = _browser_storage_state(client.jar)
        storage_path.write_text(json.dumps(storage_value, ensure_ascii=False), encoding="utf-8")
        storage_sha = sha256_file(storage_path)
        manifest = {
            "schema": "navren.pi.isolated-instance/1",
            "task_id": "navren-product-insurance-implementation-20261018",
            "is_isolated": True,
            "storage_root": str(fixture.state),
            "database_path": str(fixture.state / "bank.sqlite3"),
            "workspace_id": finance["workspace_id"],
            "privacy_revision": str(finance["privacy_revision"]),
            "registered_user_id": registered["registered_user_id"],
            "test_storage_state_isolated": True,
            "test_storage_state_path": str(storage_path),
            "test_storage_state_sha256": storage_sha,
            "operation_gates_enabled": False,
            "production_writes": False,
            "real_sms": 0,
            "model_calls": 0,
        }
        manifest_path = ACCEPTANCE_OUT / "tmp" / "isolated-instance-manifest.json"
        write_json(manifest_path, manifest)

        http_adapter, ui_adapter = _adapters(api_sha, provider_revision)
        write_json(HTTP_ADAPTER, http_adapter)
        write_json(UI_ADAPTER, ui_adapter)
        os.environ.update({
            "NAVREN_PI_BASE_URL": client.base,
            "NAVREN_PI_ISOLATED_MANIFEST": str(manifest_path),
            "NAVREN_PI_TEST_COOKIE": "; ".join(f"{cookie.name}={cookie.value}" for cookie in client.jar),
            "NAVREN_PI_TEST_CSRF": finance.get("csrf", ""),
        })
        http_report = run_http_adapter(HTTP_ADAPTER, base_url_override=client.base)
        ui_report = run_ui_adapter(UI_ADAPTER, base_url_override=client.base)
        write_json(ACCEPTANCE_OUT / "http-last-run.json", http_report)
        write_json(ACCEPTANCE_OUT / "ui-last-run.json", ui_report)
        http_run_path = ACCEPTANCE_OUT / f"http-run-{run_id}.json"
        ui_run_path = ACCEPTANCE_OUT / f"ui-run-{run_id}.json"
        write_json(http_run_path, http_report)
        write_json(ui_run_path, ui_report)

        return {
            "schema": "navren.product-insurance.isolated-entry-run/1",
            "run_id": run_id,
            "status": "PASS_ISOLATED_ENTRY_VERTICAL" if http_report.get("status") == "PASS_ISOLATED_HTTP" and ui_report.get("status") == "PASS_ISOLATED_UI" else "FAIL_ISOLATED_ENTRY_VERTICAL",
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "base_url": client.base,
            "api_contract_sha256": api_sha,
            "provider_revision": provider_revision,
            "test_account_scope": "temporary registered synthetic working_landlady identity; state removed after run",
            "http": {"status": http_report.get("status"), "flows": http_report.get("flows", [])},
            "ui": {"status": ui_report.get("status"), "flows": ui_report.get("flows", [])},
            "evidence": {
                "http_adapter": str(HTTP_ADAPTER.relative_to(ROOT)),
                "http_report": str(http_run_path.relative_to(ROOT)),
                "ui_adapter": str(UI_ADAPTER.relative_to(ROOT)),
                "ui_report": str(ui_run_path.relative_to(ROOT)),
                "screenshots": [
                    {"case_id": flow.get("case_id"), "sha256": step.get("sha256"), "bytes": step.get("bytes")}
                    for flow in ui_report.get("flows", [])
                    for step in flow.get("steps", [])
                    if step.get("action") == "screenshot"
                ],
            },
            "production_writes": 0,
            "model_calls": fixture.agent.calls,
            "real_sms": 0,
            "private_state_persisted_to_acceptance": False,
            "product_acceptance": False,
            "limitation": "Current-entry, public research routes and one isolated read-only UI flow only; not the 24-case matrix, three full journeys, real-model, global security or deployment acceptance.",
        }
    finally:
        if welcome is not None:
            welcome.shutdown()
            welcome.server_close()
        if welcome_thread is not None:
            welcome_thread.join(5)
        fixture.doCleanups()


if __name__ == "__main__":
    result = run()
    write_json(ACCEPTANCE_OUT / f"isolated-entry-run-{result['run_id']}.json", result)
    write_json(RUNTIME_RECEIPT, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
