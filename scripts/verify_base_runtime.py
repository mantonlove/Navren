#!/usr/bin/env python3
"""Offline real-HTTP verification; all bank/private/market files are temporary."""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import date, timedelta
import http.cookiejar
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet
from demo_accounts import USER_IDS
from financial_methods import MetricContext
from financial_methods.funding import CashSnapshot, FundingEvent, Hold, evaluate_funding
from server import create_server, ProxyHandler
from simulated_bank import BankError
from voice_gateway import VoiceGateway


class Browser:
    def __init__(self, base):
        self.base, self.csrf = base, None
        self.jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def cookie(self):
        return "; ".join(f"{item.name}={item.value}" for item in self.jar)

    def request(self, path, method="GET", body=None, headers=None, cookie=None):
        hdr = {"Content-Type": "application/json", "Origin": self.base}
        if self.csrf:
            hdr["X-QZ-CSRF"] = self.csrf
        hdr.update(headers or {})
        if cookie is not None:
            hdr["Cookie"] = cookie
        request = urllib.request.Request(self.base + path,
            data=json.dumps(body or {}).encode() if method == "POST" else None,
            headers=hdr, method=method)
        client = self.client if cookie is None else urllib.request.build_opener()
        try:
            response = client.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read().decode()
            return response.status, json.loads(raw), dict(response.headers)

    def start(self):
        status, result, headers = self.request("/api/finance/session", "POST", {})
        if status != 200:
            raise AssertionError((status, result))
        self.csrf = result["csrf"]
        return result, headers

    def select(self, user, workspace):
        status, result, headers = self.request("/api/finance/demo/select", "POST",
            {"user_id": user, "expected_workspace_id": workspace})
        if status != 200:
            raise AssertionError((status, result))
        self.csrf = result["csrf"]
        return result, headers


class _NoCloudAgent:
    def process(self, *args, **kwargs):
        raise AssertionError("Verification must not invoke a model")


@contextmanager
def temporary_runtime(initialize=True):
    with tempfile.TemporaryDirectory(prefix="navren-base-runtime-") as folder:
        path = Path(folder)
        environment = {"NAVREN_PERSONAL_DATA_KEY": Fernet.generate_key().decode(),
                       "NAVREN_KNOWLEDGE_DB": str(path / "knowledge.sqlite3")}
        with patch.dict(os.environ, environment), patch.object(ProxyHandler, "log_message", lambda *args: None):
            voice = VoiceGateway(key="offline-verification-fixture", allow_cloud_asr=False,
                task_factory=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("No paid speech task")))
            server = create_server(port=0, database=path / "bank.sqlite3", voice=voice,
                                   conversation_agent=_NoCloudAgent())
            thread = None
            try:
                if initialize:
                    imported = server.finance.demo_accounts.initialize_offline(allow_offline=True)
                else:
                    imported = None
                thread = threading.Thread(target=server.serve_forever, name="base-verification-http", daemon=True)
                thread.start()
                yield server, f"http://127.0.0.1:{server.server_port}", imported
            finally:
                if thread:
                    server.shutdown()
                server.server_close()
                if thread:
                    thread.join(5)
                    if thread.is_alive():
                        raise AssertionError("Temporary HTTP server did not stop")


def recompute_metrics(gateway, workspace, days=60):
    """Read raw archive/timeline and independently compute integer checkpoints.

    Also reconstruct pure financial_methods inputs without calling snapshot().
    This adapter has no implicit hold-to-payment links or planning allocations.
    """
    archive = gateway.bank.customer_archive(workspace)
    timeline = gateway.cashflow_timeline(workspace, days)
    begin, end = archive["as_of"], timeline["horizon_end"]
    balance = sum(row["balance_minor"] for row in archive["accounts"])
    floor = archive["profile"]["reserve_minor"]
    holds = [row for row in archive["records"]["fund_holds"] if row["status"] == "held"]
    held_at = lambda on: sum(h["amount_minor"] for h in holds if h["expires_on"] >= on)
    events = [event for slot in timeline["timeline"] for event in slot["events"] if event["cash_delta_minor"]]
    deltas = defaultdict(lambda: {"confirmed": 0, "candidate": 0})
    due = 0
    for event in events:
        if begin <= event["date"] <= end and event["certainty"] in ("confirmed", "candidate"):
            deltas[event["date"]][event["certainty"]] += event["cash_delta_minor"]
            if event["certainty"] == "confirmed" and event["cash_delta_minor"] < 0:
                due -= event["cash_delta_minor"]
    initial = balance - floor - held_at(begin)
    confirmed_cash = candidate_cash = balance
    confirmed_minimum = candidate_minimum = initial
    on = date.fromisoformat(begin)
    while on.isoformat() <= end:
        value = deltas[on.isoformat()]
        confirmed_cash += value["confirmed"]
        candidate_cash += value["confirmed"] + value["candidate"]
        confirmed_minimum = min(confirmed_minimum, confirmed_cash - floor - held_at(on.isoformat()))
        candidate_minimum = min(candidate_minimum, candidate_cash - floor - held_at(on.isoformat()))
        on += timedelta(days=1)
    manual = {"cash_balance": balance, "current_available": initial,
              "current_budget": initial - due, "scenario_budget": initial - due,
              "confirmed_minimum": confirmed_minimum, "candidate_minimum": candidate_minimum,
              "confirmed_closing_cash": confirmed_cash, "candidate_closing_cash": candidate_cash}
    versions = gateway.financial_metrics.versions(workspace)
    context = MetricContext(workspace, "CNY", "recorded_items_projection", period_start=begin,
                            period_end=end, input_refs=("independent-bank-archive/" + workspace,),
                            source_versions=tuple(versions.items()))
    cash = CashSnapshot(workspace, "CNY", begin, balance, 0, floor, begin, end,
                        "independent-bank-snapshot/" + workspace, versions["bank"])
    reconstructed_events = tuple(FundingEvent(e["id"], workspace, e["currency"], e["date"],
        abs(e["cash_delta_minor"]), "inflow" if e["cash_delta_minor"] > 0 else "outflow",
        e["certainty"], e["evidence_ref"], versions["bank"], e["kind"]) for e in events)
    reconstructed_holds = tuple(Hold(h["id"], workspace, "CNY", h["amount_minor"],
        "independent-fund-hold/" + h["id"], versions["bank"],
        release_on=(date.fromisoformat(h["expires_on"]) + timedelta(days=1)).isoformat()) for h in holds)
    methods = evaluate_funding(context, cash, reconstructed_events, reconstructed_holds)
    if methods.status != "calculated":
        raise AssertionError(methods.coverage_gaps)
    return {"manual": manual, "financial_methods": {k: m.value_minor for k, m in methods.metrics.items()},
            "forecasts": [e for e in events if e["kind"] == "recorded_forecast"]}


def _assert(ok, detail):
    if not ok:
        raise AssertionError(detail)


def verify_runtime():
    started = time.monotonic()
    closed_cleanly = False
    report = {"passed": False, "checks": [], "accounts": {}, "source_counts": {}, "metrics": {},
              "uncovered": ["Real cloud model and paid speech upstream are deliberately not invoked.",
                            "This verifies loopback HTTP sessions, not deployment/TLS or real-bank login.",
                            "Synthetic holdings have no verified real fund units/NAV mapping."]}
    def check(name, callback):
        try:
            detail = callback()
            report["checks"].append({"id": name, "passed": True, "detail": detail})
        except Exception as error:
            report["checks"].append({"id": name, "passed": False,
                "detail": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc(limit=4)})
    try:
        with temporary_runtime() as (server, base, imported):
            gateway = server.finance
            browser = Browser(base)
            state, headers = browser.start()
            workspace = state["workspace_id"]
            workspaces = {uid: gateway.demo_accounts.stable_workspace(uid) for uid in USER_IDS}
            report["accounts"] = workspaces
            def offline_catalog():
                _assert(imported["initialized_count"] == 4 and imported["history_replayed"] is False, imported)
                _assert(len(set(workspaces.values())) == 4, workspaces)
                _assert(state["account_id"] == USER_IDS[0] and workspace == workspaces[USER_IDS[0]], state)
                _assert("HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"], "session cookie flags")
                again = gateway.demo_accounts.initialize_offline(allow_offline=True)
                _assert(again["already_initialized"], "offline reimport duplicated accounts")
                return "four stable workspaces; default first demo; offline import idempotent; cookie flags"
            check("offline_catalog_and_default_identity", offline_catalog)

            source = ROOT / "outputs/contest-finances/financial_data.sqlite3"
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                for uid in USER_IDS:
                    report["source_counts"][uid] = {table: db.execute("SELECT count(*) FROM " + table + " WHERE user_id=?", (uid,)).fetchone()[0]
                        for table in ("users", "ledger_accounts", "cash_transactions", "daily_balances",
                                      "journal_events", "journal_lines", "asset_values", "loan_balances",
                                      "monthly_summaries", "annual_summaries", "recurring_obligations", "financial_targets")}
            initial_cash = {uid: gateway.overview(w)["total_cash_minor"] for uid, w in workspaces.items()}
            def all_account_reads():
                nonlocal state, workspace
                for uid in USER_IDS:
                    state, _ = browser.select(uid, workspace)
                    workspace = state["workspace_id"]
                    for table, expected in report["source_counts"][uid].items():
                        status, result, h = browser.request("/api/finance/demo/history?table=" + table + "&limit=1")
                        _assert(status == 200 and result["total"] == expected and result["user_id"] == uid, (table, status, result))
                        _assert(result["workspace_id"] == workspace and h.get("X-Navren-Workspace") == workspace, "history response binding")
                    status, current, _ = browser.request("/api/finance/demo/current")
                    _assert(status == 200 and current["bank_state"]["cash_minor"] == current["source_snapshot"]["latest_balances"]["cash_minor"], (status, current))
                    source_snapshot = current["source_snapshot"]
                    expected_cost = sum(a["cost_basis_minor"] for a in source_snapshot["assets"] if a["account_class"] == "investment")
                    _assert(current["bank_state"]["holding_principal_minor"] == expected_cost, "runtime holdings must import cost, not market value or duplicate history")
                    _assert(current["bank_state"]["liabilities_minor"] == source_snapshot["latest_balances"]["liabilities_minor"], "source liability import mismatch")
                    _assert(all(h["quantity"] is None and h["historical_synthetic_position"] and not h["execution_enabled"] for h in current["bank_state"]["holdings"]), "synthetic positions acquired invented real units or execution permission")
                    status, metrics, _ = browser.request("/api/finance/metrics?days=60")
                    _assert(status == 200, metrics)
                    gateway.financial_metrics.validate(workspace, metrics)
                    calculated = recompute_metrics(gateway, workspace)
                    actual = {key: value["value_minor"] for key, value in metrics["funding"]["metrics"].items()}
                    _assert(actual == calculated["manual"] == calculated["financial_methods"], (uid, actual, calculated))
                    terminal = {m["metric_name"]: m["value_minor"] for m in metrics["metrics"] if m["data_scope"] == "navren_synthetic"}
                    _assert(all(terminal.get(key) == source_snapshot["latest_balances"][key] for key in ("investment_minor", "real_assets_minor", "net_worth_minor")), "historical book values must remain separately sourced metrics")
                    _assert(all(e["certainty"] == "candidate" for e in calculated["forecasts"]), "forecast became commitment")
                    _assert(actual["confirmed_closing_cash"] == initial_cash[uid], "unconfirmed future forecasts changed confirmed cash")
                    _assert(gateway.bank.customer_archive(workspace)["records"]["fund_holds"] == [], "targets became frozen funds")
                    overview = gateway.overview(workspace)
                    accounts = overview["accounts"]
                    reserve_total = overview["profile"]["reserve_minor"]
                    remaining = reserve_total
                    independent_floors = {a["id"]: 0 for a in accounts}
                    allocation_order = sorted(accounts, key=lambda a: (
                        not a["id"].endswith("-reserve"), not a["id"].endswith("-main"), a["id"]))
                    for account in allocation_order:
                        allocation = min(remaining, account["balance_minor"])
                        independent_floors[account["id"]] = allocation
                        remaining -= allocation
                    if remaining:
                        independent_floors[allocation_order[-1]["id"]] += remaining
                    timeline_floors = {a["account_id"]: a["reserve_floor_minor"]
                        for a in overview["cashflow_timeline"]["accounts"]}
                    budgets = {a["id"]: gateway.budget(workspace, a["id"]) for a in accounts}
                    with gateway.bank.transaction() as db:
                        execution_floors = {a["id"]: gateway.bank._reserve_floor(db, workspace, a["id"])
                            for a in accounts}
                    _assert(independent_floors == timeline_floors == execution_floors ==
                        {key: value["reserve_minor"] for key, value in budgets.items()},
                        "per-account budget/timeline/execution use inconsistent emergency floors")
                    _assert(sum(independent_floors.values()) == reserve_total,
                        "household reserve counted more than once")
                    _assert(sum(b["investable_minor"] for b in budgets.values()) ==
                        gateway.budget(workspace)["investable_minor"],
                        "account budgets do not reconcile to household budget")
                    report["metrics"][uid] = {"workspace_id": workspace,
                        "snapshot_id": metrics["snapshot_id"], "source_versions": metrics["source_versions"],
                        "actual_minor": actual, "manual_recomputed_minor": calculated["manual"],
                        "methods_recomputed_minor": calculated["financial_methods"],
                        "candidate_forecast_count": len(calculated["forecasts"]),
                        "historical_terminal_metrics_minor": terminal,
                        "runtime_holding_cost_minor": expected_cost,
                        "emergency_floor_minor": reserve_total,
                        "emergency_floor_by_account_minor": independent_floors,
                        "budget_by_account_minor": budgets}
                    for path in ("/api/finance/memories", "/api/finance/personal-graph?view=wealth", "/api/finance/calendar?from=2026-10-03&to=2026-10-03"):
                        status, result, h = browser.request(path)
                        _assert(status == 200 and result["workspace_id"] == workspace and h.get("X-Navren-Workspace") == workspace, (path, status, result))
                    _assert(gateway.overview(workspace)["total_cash_minor"] == initial_cash[uid], "read-only activity moved cash")
                return "all 12 source-table row counts; source costs/loans preserved without invented units; metrics manual/methods recomputation; household reserve allocated once across budget/timeline/execution; read bindings; forecasts candidates; goals not holds; balances unchanged"
            check("four_account_reads_and_metrics_recompute", all_account_reads)

            state, _ = browser.select(USER_IDS[0], workspace); workspace = state["workspace_id"]
            def payload(amount=10000):
                overview = gateway.overview(workspace)
                account = max(overview["accounts"], key=lambda row: row["balance_minor"])
                return {"action": "transfer", "account_id": account["id"], "beneficiary_id": overview["beneficiaries"][0]["id"],
                        "amount_minor": amount, "idempotency_key": "verify-" + uuid.uuid4().hex}
            def prepare(agent_origin=False):
                data = payload()
                if agent_origin:
                    return gateway.prepare(workspace, data, agent_origin=True), data
                status, view, _ = browser.request("/api/finance/operations/prepare", "POST", data)
                _assert(status == 200, (status, view))
                return view, data
            def confirmation(view):
                return {"confirmed": True, "confirmation_digest": view["operation"]["confirmation_digest"],
                        "verification_code": browser.request("/api/finance/demo-code")[1]["code"]}
            def confirm(view, body=None):
                return browser.request("/api/finance/operations/" + view["operation"]["id"] + "/confirm", "POST", body or confirmation(view))

            def switching_and_confirmation():
                nonlocal state, workspace
                view, data = prepare()
                before = gateway.overview(workspace)["total_cash_minor"]
                old_cookie, old_csrf, old_workspace = browser.cookie(), browser.csrf, workspace
                state, _ = browser.select(USER_IDS[1], workspace); workspace = state["workspace_id"]
                _assert(browser.request("/api/finance/overview", cookie=old_cookie)[0] == 409, "old cookie accepted")
                _assert(browser.request("/api/finance/demo/select", "POST", {"user_id": USER_IDS[0], "expected_workspace_id": workspace}, headers={"X-QZ-CSRF": old_csrf})[0] == 403, "old CSRF accepted")
                _assert(browser.request("/api/finance/overview?workspace_id=" + old_workspace)[0] == 400, "query overrides identity")
                _assert(confirm(view)[0] == 404, "foreign operation confirmed")
                state, _ = browser.select(USER_IDS[0], workspace); workspace = state["workspace_id"]
                status, first, _ = confirm(view)
                _assert(status == 200 and first["operation"]["state"] == "succeeded", (status, first))
                status, second, _ = confirm(view)
                _assert(status == 200 and second["operation"] == first["operation"], (status, second))
                _assert(gateway.overview(workspace)["total_cash_minor"] == before - data["amount_minor"], "duplicate balance change")
                with closing(gateway.bank.connect()) as db:
                    count = db.execute("SELECT count(*) FROM transactions WHERE operation_id=?", (view["operation"]["id"],)).fetchone()[0]
                _assert(count == 1, ("duplicate ledger", count))
                return "old cookie/CSRF rejected; identity query denied; foreign confirm 404; return and explicit confirm succeeds once"
            check("switching_confirm_and_idempotency", switching_and_confirmation)

            def rejected_case(kind):
                before = gateway.overview(workspace)["total_cash_minor"]
                if kind == "limit":
                    status, result, _ = browser.request("/api/finance/operations/prepare", "POST", payload(100000001))
                    _assert(status == 400 and result["error"]["code"] == "INVALID_INTEGER", (status, result))
                else:
                    view, data = prepare(agent_origin=kind == "agent_review")
                    body = confirmation(view)
                    if kind == "digest":
                        body["confirmation_digest"] = "0" * 64
                    elif kind == "expired":
                        with gateway.bank.transaction() as db:
                            db.execute("UPDATE operations SET expires_at=? WHERE id=?", (gateway.bank.clock() - 1, view["operation"]["id"]))
                    elif kind == "personal":
                        status, memory, _ = browser.request("/api/finance/memories", "POST", {"category": "goal", "value": "测试账户目标", "expected_revision": gateway.personal_data.revision(workspace)})
                        _assert(status == 200, memory)
                    elif kind == "metrics":
                        with gateway.bank.transaction() as db:
                            row = db.execute("SELECT snapshot FROM base_operation_metrics WHERE operation_id=?", (view["operation"]["id"],)).fetchone()
                            value = json.loads(row[0]); value["funding"]["metrics"]["current_budget"]["value_minor"] += 1
                            db.execute("UPDATE base_operation_metrics SET snapshot=? WHERE operation_id=?", (json.dumps(value), view["operation"]["id"]))
                    status, result, _ = confirm(view, body)
                    allowed = {"digest": {"CONFIRMATION_MISMATCH"}, "expired": {"EXPIRED"},
                               "personal": {"METRIC_VERSION_CONFLICT", "STALE_CONFIRMATION", "INVALID_STATE"},
                               "metrics": {"METRIC_TAMPERED"}, "agent_review": {"AGENT_REVIEW_REQUIRED"}}
                    _assert(status == 409 and result["error"]["code"] in allowed[kind], (status, result))
                _assert(gateway.overview(workspace)["total_cash_minor"] == before, "rejection moved cash")
                return "rejected with no balance change"
            for kind in ("limit", "digest", "expired", "personal", "metrics", "agent_review"):
                check("reject_" + kind, lambda kind=kind: rejected_case(kind))

            def private_and_calendar_isolation():
                nonlocal state, workspace
                revision = gateway.personal_data.revision(workspace)
                _assert(browser.request("/api/finance/memories/settings", "POST", {"enabled": True, "expected_revision": revision})[0] == 200, "enable memory")
                unique = "独立测试私人目标" + uuid.uuid4().hex
                status, memory, _ = browser.request("/api/finance/memories", "POST", {"category": "goal", "value": unique, "expected_revision": gateway.personal_data.revision(workspace)})
                _assert(status == 200, memory)
                node = next(item["node_id"] for item in memory["items"] if item["value"] == unique)
                status, graph, _ = browser.request("/api/finance/personal-graph?view=preferences")
                _assert(status == 200 and unique in json.dumps(graph, ensure_ascii=False), "current memory missing from preferences graph")
                status, calendar, _ = browser.request("/api/finance/calendar?from=2026-10-03&to=2026-10-03")
                _assert(status == 200, calendar)
                old_workspace = workspace
                state, _ = browser.select(USER_IDS[1], workspace); workspace = state["workspace_id"]
                _assert(unique not in json.dumps(browser.request("/api/finance/memories")[1], ensure_ascii=False), "foreign memory leaked")
                _assert(unique not in json.dumps(browser.request("/api/finance/personal-graph?view=preferences")[1], ensure_ascii=False), "foreign graph leaked")
                path = "/api/finance/graph/evidence?source=user_confirmed&table=user_memories&id=" + node
                _assert(browser.request(path)[0] == 404, "foreign graph evidence leaked")
                path = "/api/finance/calendar/views/" + calendar["view_id"] + "/days/2026-10-03"
                _assert(browser.request(path)[0] == 404, "foreign calendar view leaked")
                _assert(browser.request("/api/finance/memories/" + node + "/correct", "POST", {"value": "越权更新", "expected_revision": gateway.personal_data.revision(workspace)})[0] == 404, "foreign memory updated")
                _assert(gateway.overview(old_workspace)["total_cash_minor"] == initial_cash[USER_IDS[0]] - 10000, "memory actions moved cash")
                return "memories, graph/evidence, calendar views and corrections isolated by server workspace"
            check("private_graph_calendar_isolation", private_and_calendar_isolation)

            def revoked_voice_credentials():
                nonlocal state, workspace
                status, ticket, _ = browser.request("/api/speech/session", "POST", {"kind": "tts", "purpose": "voice_preview"})
                _assert(status == 200, ticket)
                old_cookie, old_csrf = browser.cookie(), browser.csrf
                state, _ = browser.select(USER_IDS[2], workspace); workspace = state["workspace_id"]
                _assert(browser.request(ticket["path"], cookie=old_cookie)[0] == 409, "voice old token accepted")
                _assert(browser.request(ticket["path"])[0] == 403, "voice old ticket accepted for new token")
                _assert(browser.request("/api/speech/session", "POST", {"kind": "tts", "purpose": "voice_preview"}, headers={"X-QZ-CSRF": old_csrf})[0] == 403, "voice old CSRF accepted")
                return "old session, ticket and CSRF denied before paid speech task creation"
            check("revoked_voice_ticket_and_token", revoked_voice_credentials)
        closed_cleanly = True
    except Exception as error:
        report["checks"].append({"id": "runtime_setup_or_cleanup", "passed": False,
            "detail": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc(limit=5)})
    report["passed"] = bool(report["checks"]) and all(row["passed"] for row in report["checks"])
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["financial_operations_are_simulation"] = True
    report["temporary_server_closed"] = closed_cleanly
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/window-progress/base/runtime-verification.json")
    args = parser.parse_args()
    result = verify_runtime()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "checks": len(result["checks"]), "elapsed_seconds": result["elapsed_seconds"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
