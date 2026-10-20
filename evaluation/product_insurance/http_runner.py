from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from evaluation.product_insurance.harness import ACCEPTANCE_OUT, ROOT, AcceptanceError, read_json, sha256_file, write_json


API_CONTRACT = ROOT / "outputs/product-insurance-implementation-20261018/base/api-contract.json"
CONSUMABLE_API_STATES = {
    "FROZEN_V1_IMPLEMENTING",
    "FROZEN_V1_RULE_BINDING_IMPLEMENTED_ACTUAL_CONTENT_NORMALIZATION_PENDING",
    "FROZEN_V1_FINAL_CONSUMER_ENGINEERING_PASSED_PENDING_JOINT_ADOPTION",
}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ALLOWED_MIME = {"application/pdf", "image/png", "image/jpeg", "image/webp"}


def is_loopback_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port is not None
    except ValueError:
        return False


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def load_isolation_manifest(env_name: str, *, required: bool) -> dict[str, Any] | None:
    raw = os.environ.get(env_name, "")
    if not raw:
        if required:
            raise AcceptanceError("isolated_instance_manifest_required")
        return None
    p = Path(raw).expanduser().resolve()
    if not p.is_file():
        raise AcceptanceError("isolated_instance_manifest_missing")
    manifest = read_json(p)
    if manifest.get("schema") != "navren.pi.isolated-instance/1" or manifest.get("task_id") != "navren-product-insurance-implementation-20261018" or manifest.get("is_isolated") is not True:
        raise AcceptanceError("isolated_instance_manifest_not_authorized")
    storage_root = Path(manifest.get("storage_root", "")).expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    accepted_output_tmp = (ACCEPTANCE_OUT / "tmp").resolve()
    if not (_under(storage_root, temp_root) or _under(storage_root, accepted_output_tmp)):
        raise AcceptanceError("isolated_storage_root_outside_temp_scope")
    if not storage_root.exists() or not storage_root.is_dir():
        raise AcceptanceError("isolated_storage_root_missing")
    database_path = Path(manifest.get("database_path", "")).expanduser().resolve()
    if not _under(database_path, storage_root):
        raise AcceptanceError("database_path_outside_isolated_storage")
    if not manifest.get("workspace_id") or not isinstance(manifest.get("privacy_revision"), str):
        raise AcceptanceError("isolated_test_identity_incomplete")
    manifest["_manifest_path"] = str(p)
    return manifest


def _ref(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            key = value["$ref"]
            if key not in context:
                raise AcceptanceError(f"flow_reference_missing:{key}")
            return context[key]
        return {k: _ref(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_ref(x, context) for x in value]
    if isinstance(value, str):
        return re.sub(r"\{\{ref:([A-Za-z0-9_.-]+)\}\}", lambda m: str(context[m.group(1)]), value)
    return value


def _value_at(obj: Any, path: str) -> Any:
    if path == "$":
        return obj
    if not path.startswith("$." ):
        raise AcceptanceError(f"unsupported_response_path:{path}")
    current = obj
    for part in path[2:].split("."):
        match = re.fullmatch(r"([^\[]+)(?:\[(\d+)\])?", part)
        if not match or not isinstance(current, dict) or match.group(1) not in current:
            raise AcceptanceError(f"response_path_missing:{path}")
        current = current[match.group(1)]
        if match.group(2) is not None:
            index = int(match.group(2))
            if not isinstance(current, list) or index >= len(current):
                raise AcceptanceError(f"response_index_missing:{path}")
            current = current[index]
    return current


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _contract_check(adapter: dict[str, Any]) -> dict[str, Any]:
    if adapter.get("schema") != "navren.pi.http-adapter/1":
        raise AcceptanceError("http_adapter_schema_mismatch")
    api_path = ROOT / adapter.get("api_contract_path", "")
    if not api_path.is_file() or sha256_file(api_path) != adapter.get("api_contract_sha256"):
        raise AcceptanceError("http_adapter_api_contract_hash_mismatch")
    actual_api = read_json(API_CONTRACT)
    if api_path.resolve() != API_CONTRACT.resolve() or actual_api.get("status") not in CONSUMABLE_API_STATES:
        raise AcceptanceError("http_adapter_not_bound_to_current_frozen_api_contract")
    return actual_api


def _request_headers(step: dict[str, Any], adapter: dict[str, Any], manifest: dict[str, Any] | None) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json, application/octet-stream"}
    if step.get("private"):
        if manifest is None:
            raise AcceptanceError("private_route_requires_test_identity_manifest")
        cookie = os.environ.get(adapter.get("test_cookie_env", ""), "")
        if not cookie:
            raise AcceptanceError("isolated_test_cookie_missing")
        headers["Cookie"] = cookie
        headers["X-Navren-Workspace"] = str(manifest["workspace_id"])
        headers["X-Navren-Privacy-Revision"] = str(manifest["privacy_revision"])
    method = str(step.get("method", "GET")).upper()
    if method not in {"GET", "HEAD", "OPTIONS"}:
        if manifest is None:
            raise AcceptanceError("write_requires_test_identity_manifest")
        csrf = os.environ.get(adapter.get("test_csrf_env", ""), "")
        if not csrf:
            raise AcceptanceError("isolated_test_csrf_missing")
        headers["X-QZ-CSRF"] = csrf
    return headers


def run_adapter(adapter_path: Path, *, base_url_override: str | None = None, allow_isolated_writes: bool = False, allow_prepare: bool = False) -> dict[str, Any]:
    adapter = read_json(adapter_path)
    api = _contract_check(adapter)
    base_url = base_url_override or os.environ.get(adapter.get("base_url_env", "NAVREN_PI_BASE_URL"), "")
    if not is_loopback_url(base_url):
        raise AcceptanceError("base_url_must_be_loopback_with_explicit_port")
    flows = adapter.get("flows")
    if not isinstance(flows, list) or not flows:
        raise AcceptanceError("http_adapter_has_no_flows")
    has_private = any(s.get("private") for f in flows for s in f.get("steps", []))
    has_writes = any(str(s.get("method", "GET")).upper() not in {"GET", "HEAD", "OPTIONS"} for f in flows for s in f.get("steps", []))
    has_prepare = any(s.get("operation") == "private.prepare" or s.get("financial_side_effect_possible") for f in flows for s in f.get("steps", []))
    manifest = load_isolation_manifest(adapter.get("isolation_manifest_env", "NAVREN_PI_ISOLATED_MANIFEST"), required=has_private or has_writes)
    if has_writes and not allow_isolated_writes:
        raise AcceptanceError("writes_disabled_use_explicit_isolated_write_flag")
    if has_writes and os.environ.get("NAVREN_PI_ALLOW_ISOLATED_WRITES") != "1":
        raise AcceptanceError("isolated_write_environment_gate_missing")
    if has_prepare and (not allow_prepare or os.environ.get("NAVREN_PI_ALLOW_PREPARE") != "1" or not manifest or manifest.get("operation_gates_enabled") is not True):
        raise AcceptanceError("financial_prepare_gate_disabled")

    results: list[dict[str, Any]] = []
    opener = urllib.request.build_opener(_NoRedirect)
    for flow in flows:
        context: dict[str, Any] = {}
        step_results: list[dict[str, Any]] = []
        flow_passed = True
        for step in flow.get("steps", []):
            method = str(step.get("method", "GET")).upper()
            path = str(_ref(step.get("path", ""), context))
            if not path.startswith("/") or path.startswith("//"):
                raise AcceptanceError("http_path_must_be_origin_relative")
            query = _ref(step.get("query", {}), context)
            url = base_url.rstrip("/") + path
            if query:
                url += "?" + urllib.parse.urlencode(query, doseq=True)
            if not is_loopback_url(url):
                raise AcceptanceError("request_target_left_loopback_origin")
            if method not in {"GET", "HEAD", "OPTIONS"}:
                if not allow_isolated_writes or os.environ.get("NAVREN_PI_ALLOW_ISOLATED_WRITES") != "1":
                    raise AcceptanceError("mutating_step_blocked")
                if step.get("operation") == "private.prepare" and not allow_prepare:
                    raise AcceptanceError("prepare_step_blocked")
            headers = _request_headers(step, adapter, manifest)
            body_bytes = None
            if step.get("upload_file"):
                if manifest is None:
                    raise AcceptanceError("binary_upload_requires_isolated_manifest")
                upload = Path(step["upload_file"]).expanduser().resolve()
                storage_root = Path(manifest["storage_root"]).resolve()
                if not _under(upload, storage_root) or not upload.is_file():
                    raise AcceptanceError("binary_upload_file_outside_isolated_storage")
                if upload.stat().st_size > 10 * 1024 * 1024:
                    raise AcceptanceError("binary_upload_exceeds_10_mib")
                if sha256_file(upload) != step.get("upload_sha256"):
                    raise AcceptanceError("binary_upload_sha_mismatch")
                mime = step.get("content_type")
                if mime not in ALLOWED_MIME:
                    raise AcceptanceError("binary_upload_mime_not_allowed")
                headers["Content-Type"] = mime
                headers["X-Navren-Upload-Key"] = os.environ.get(adapter.get("upload_key_env", "NAVREN_PI_TEST_UPLOAD_KEY"), "")
                if not headers["X-Navren-Upload-Key"]:
                    raise AcceptanceError("isolated_upload_key_missing")
                body_bytes = upload.read_bytes()
            elif "json" in step:
                headers["Content-Type"] = "application/json"
                body_bytes = json.dumps(_ref(step["json"], context), ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
            status = None
            response_bytes = b""
            response_headers = {}
            try:
                with opener.open(request, timeout=float(step.get("timeout_seconds", 5))) as response:
                    status = response.status
                    response_headers = dict(response.headers.items())
                    response_bytes = response.read(MAX_RESPONSE_BYTES + 1)
            except urllib.error.HTTPError as error:
                status = error.code
                response_headers = dict(error.headers.items())
                response_bytes = error.read(MAX_RESPONSE_BYTES + 1)
            if len(response_bytes) > MAX_RESPONSE_BYTES:
                raise AcceptanceError("http_response_exceeds_2_mib")
            expected = step.get("expected", {})
            passed = status == expected.get("status")
            content_type = response_headers.get("Content-Type", "")
            record: dict[str, Any] = {"step_id": step.get("id"), "method": method, "path": path, "status": status, "response_bytes": len(response_bytes), "content_type": content_type, "response_body_persisted": False, "passed": passed}
            response_json = None
            is_json = "json" in content_type.lower()
            if is_json and response_bytes:
                try:
                    response_json = json.loads(response_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    passed = False
                    record["parse_error"] = "invalid_json"
            if expected.get("json_fields"):
                if response_json is None:
                    passed = False
                else:
                    json_assertions = []
                    for key, wanted in expected["json_fields"].items():
                        try:
                            actual = _value_at(response_json, key)
                        except AcceptanceError:
                            passed = False
                            json_assertions.append({"path": key, "passed": False})
                            continue
                        assertion_passed = actual == _ref(wanted, context)
                        json_assertions.append({"path": key, "passed": assertion_passed})
                        if not assertion_passed:
                            passed = False
            if expected.get("binary_sha256"):
                actual_sha = hashlib.sha256(response_bytes).hexdigest()
                record["response_sha256"] = actual_sha
                passed = passed and actual_sha == expected["binary_sha256"]
                passed = passed and int(response_headers.get("Content-Length", len(response_bytes))) == len(response_bytes)
            record["passed"] = passed
            if expected.get("json_fields") and response_json is not None:
                record["json_assertions"] = json_assertions
            if step.get("capture") and response_json is not None:
                captured_names = []
                for name, json_path in step["capture"].items():
                    context[f"{step.get('id')}.{name}"] = _value_at(response_json, json_path)
                    captured_names.append(name)
                record["captured_names"] = captured_names
            step_results.append(record)
            flow_passed = flow_passed and passed
            if not passed:
                break
        results.append({"case_id": flow.get("case_id"), "passed": flow_passed, "steps": step_results})

    report = {"schema": "navren.product-insurance.http-run/1", "status": "PASS_ISOLATED_HTTP" if all(x["passed"] for x in results) else "FAIL_ISOLATED_HTTP", "api_contract_sha256": sha256_file(API_CONTRACT), "base_origin": base_url, "isolated_instance": bool(manifest), "model_acceptance": False, "real_model_calls": 0, "paid_api_calls": 0, "flows": results}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="PI-ACCEPT isolated loopback HTTP runner; writes are denied by default.")
    parser.add_argument("--adapter", type=Path, default=ACCEPTANCE_OUT / "http-adapter.json")
    parser.add_argument("--base-url")
    parser.add_argument("--allow-isolated-writes", action="store_true")
    parser.add_argument("--allow-prepare", action="store_true")
    args = parser.parse_args()
    try:
        report = run_adapter(args.adapter, base_url_override=args.base_url, allow_isolated_writes=args.allow_isolated_writes, allow_prepare=args.allow_prepare)
        write_json(ACCEPTANCE_OUT / "http-last-run.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "PASS_ISOLATED_HTTP" else 1
    except AcceptanceError as exc:
        blocked = {"schema": "navren.product-insurance.http-run/1", "status": "BLOCKED", "reason": str(exc), "model_acceptance": False, "real_model_calls": 0, "paid_api_calls": 0}
        write_json(ACCEPTANCE_OUT / "http-last-run.json", blocked)
        print(json.dumps(blocked, ensure_ascii=False, indent=2))
        return 2
    except Exception as exc:
        blocked = {"schema": "navren.product-insurance.http-run/1", "status": "BLOCKED", "reason": f"runner_error:{type(exc).__name__}", "model_acceptance": False, "real_model_calls": 0, "paid_api_calls": 0}
        write_json(ACCEPTANCE_OUT / "http-last-run.json", blocked)
        print(json.dumps(blocked, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
