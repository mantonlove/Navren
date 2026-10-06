#!/usr/bin/env python3
"""Independent, read-only audit of the four-account synthetic base dataset.

No generator or previous audit code is imported. A pass establishes internal
reconciliation, not real customer data, actual fund units, or external truth.
"""
from __future__ import annotations

import argparse
import calendar
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3


USERS = ("tycoon", "grain_farmer", "working_landlady", "retired_entrepreneur")
START, END = date(2016, 10, 4), date(2026, 10, 3)
SOURCE = "navren_synthetic"
BALANCE_FIELDS = ("cash_minor", "investment_minor", "real_assets_minor",
                  "liabilities_minor", "total_assets_minor", "net_worth_minor")
METRICS = ("income_minor", "expense_minor", "refund_minor", "cash_inflow_minor",
           "cash_outflow_minor", "realized_return_minor", "unrealized_change_minor")
TABLE_KEYS = {
    "users": ("user_id",), "metadata": ("key",),
    "ledger_accounts": ("account_id",), "journal_events": ("event_id",),
    "journal_lines": ("line_id",), "cash_transactions": ("transaction_id",),
    "daily_balances": ("user_id", "date"),
    "asset_values": ("user_id", "date", "asset_id"),
    "loan_balances": ("user_id", "date", "loan_id"),
    "monthly_summaries": ("user_id", "period_start"),
    "annual_summaries": ("user_id", "period_start"),
    "recurring_obligations": ("obligation_id",), "financial_targets": ("target_id",),
}
CLASSES = {"cash", "investment", "real_asset", "liability", "income", "expense", "equity"}
EVENT_TYPES = {"opening", "income", "expense", "refund", "internal_transfer",
               "investment_purchase", "investment_redemption", "loan_repayment", "valuation"}


class _Checks:
    def __init__(self):
        self.items = {}

    def test(self, name, ok, detail):
        item = self.items.setdefault(name, {"id": name, "passed": True,
                                           "checked": 0, "failure_count": 0, "failures": []})
        item["checked"] += 1
        if not ok:
            item["passed"] = False
            item["failure_count"] += 1
            if len(item["failures"]) < 8:
                item["failures"].append(detail)


def _hash(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _days():
    return [(START + timedelta(days=n)).isoformat() for n in range((END - START).days + 1)]


def _periods(annual):
    result = []
    cursor = START
    while cursor <= END:
        natural_start = date(cursor.year, 1, 1) if annual else date(cursor.year, cursor.month, 1)
        natural_end = (date(cursor.year, 12, 31) if annual else
                       date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1]))
        last = min(natural_end, END)
        result.append((cursor.isoformat(), last.isoformat(), int(cursor != natural_start or last != natural_end)))
        cursor = last + timedelta(days=1)
    return result


def _totals(balances, accounts):
    by_class = defaultdict(int)
    for aid, value in balances.items():
        by_class[accounts[aid]["account_class"]] += value
    cash, investments, physical = (by_class[x] for x in ("cash", "investment", "real_asset"))
    liability = -by_class["liability"]
    assets = cash + investments + physical
    return dict(zip(BALANCE_FIELDS, (cash, investments, physical, liability, assets, assets - liability)))


def _load_json(path, checks):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        checks.test("source_files", isinstance(value, dict), str(path))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError) as error:
        checks.test("source_files", False, f"{path}: {error}")
        return {}


def _audit_account(db, uid, checks, current, summary_users):
    accounts = {r["account_id"]: dict(r) for r in db.execute(
        "SELECT * FROM ledger_accounts WHERE user_id=?", (uid,))}
    user = dict(db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone())
    days = _days()
    daily = {r["date"]: dict(r) for r in db.execute("SELECT * FROM daily_balances WHERE user_id=?", (uid,))}
    assets = {(r["date"], r["asset_id"]): dict(r) for r in db.execute(
        "SELECT * FROM asset_values WHERE user_id=?", (uid,))}
    loans = {(r["date"], r["loan_id"]): r["principal_minor"] for r in db.execute(
        "SELECT * FROM loan_balances WHERE user_id=?", (uid,))}
    asset_ids = {aid for aid, a in accounts.items() if a["account_class"] in ("investment", "real_asset")}
    loan_ids = {aid for aid, a in accounts.items() if a["account_class"] == "liability"}
    checks.test("calendar_coverage", set(daily) == set(days), f"{uid}: daily dates")
    checks.test("calendar_coverage", set(assets) == set(itertools.product(days, asset_ids)), f"{uid}: asset dates/IDs")
    checks.test("calendar_coverage", set(loans) == set(itertools.product(days, loan_ids)), f"{uid}: loan dates/IDs")
    checks.test("calendar_coverage", (user["start_date"], user["end_date"]) == (START.isoformat(), END.isoformat()), f"{uid}: user coverage")

    cash_by_event = defaultdict(list)
    kind_counts, kind_totals = Counter(), Counter()
    for row in db.execute("SELECT * FROM cash_transactions WHERE user_id=?", (uid,)):
        row = dict(row)
        cash_by_event[row["event_id"]].append(row)
        kind_counts[row["kind"]] += 1
        kind_totals[row["kind"]] += row["signed_minor"]
    events = {r["event_id"]: dict(r) for r in db.execute("SELECT * FROM journal_events WHERE user_id=?", (uid,))}
    lines = defaultdict(list)
    for row in db.execute("SELECT * FROM journal_lines WHERE user_id=?", (uid,)):
        lines[row["event_id"]].append(dict(row))
    by_day = defaultdict(list)
    for event in events.values():
        try:
            stamp = datetime.fromisoformat(event["occurred_at"])
            ok = (stamp.utcoffset() == timedelta(hours=8) and START <= stamp.date() <= END
                  and event["event_type"] in EVENT_TYPES)
        except (ValueError, TypeError):
            ok = False
        checks.test("event_time_and_type", ok, event["event_id"])
        by_day[event["occurred_at"][:10]].append(event)
    opening = [e for e in events.values() if e["event_type"] == "opening"]
    checks.test("opening_register", len(opening) == 1 and opening[0]["occurred_at"] == START.isoformat() + "T00:00:00+08:00", uid)
    balances, costs = defaultdict(int), defaultdict(int)
    daily_metrics, opening_cash, replay = {}, {}, {}
    refunded = Counter()
    principal = Counter()
    cost_refs = defaultdict(list)
    for day in days:
        opening_cash[day] = sum(balances[a] for a in accounts if accounts[a]["account_class"] == "cash")
        metric = dict.fromkeys(METRICS, 0)
        for event in sorted(by_day[day], key=lambda e: (e["occurred_at"], e["event_id"])):
            eid, kind = event["event_id"], event["event_type"]
            event_lines = lines[eid]
            changes = defaultdict(int)
            for line in event_lines:
                changes[line["account_id"]] += line["debit_minor"] - line["credit_minor"]
            checks.test("balanced_events", len(event_lines) >= 2 and sum(changes.values()) == 0, eid)
            checks.test("event_account_links", all(a in accounts for a in changes), eid)
            if not all(a in accounts for a in changes):
                continue
            grouped = defaultdict(int)
            for aid, delta in changes.items():
                grouped[accounts[aid]["account_class"]] += delta
            cash_changes = {a: d for a, d in changes.items() if accounts[a]["account_class"] == "cash"}
            income_lines = {accounts[a]["code"]: d for a, d in changes.items() if accounts[a]["account_class"] == "income"}
            recognized = -sum(d for code, d in income_lines.items() if not code.startswith("income:unrealized"))
            realized = -income_lines.get("income:investment_realized", 0)
            net_expense = sum(d for a, d in changes.items() if accounts[a]["account_class"] == "expense" and not accounts[a]["code"].startswith("expense:depreciation"))
            if kind == "opening":
                expected = {aid: a["opening_balance_minor"] for aid, a in accounts.items() if a["opening_balance_minor"]}
                checks.test("opening_register", dict(changes) == expected, eid)
                for aid in asset_ids:
                    costs[aid] = changes.get(aid, 0)
                    cost_refs[aid].append(eid)
                opening_cash[day] = sum(cash_changes.values())
            else:
                metric["income_minor"] += recognized
                metric["expense_minor"] += net_expense
                metric["realized_return_minor"] += realized
                metric["unrealized_change_minor"] -= sum(d for code, d in income_lines.items() if code.startswith("income:unrealized"))
                metric["cash_inflow_minor"] += sum(d for d in cash_changes.values() if d > 0)
                metric["cash_outflow_minor"] -= sum(d for d in cash_changes.values() if d < 0)
                if kind == "refund":
                    metric["refund_minor"] += sum(cash_changes.values())
                actual_legs = Counter((r["account_id"], r["signed_minor"]) for r in cash_by_event[eid])
                expected_legs = Counter(cash_changes.items())
                checks.test("cash_legs", actual_legs == expected_legs, eid)
                for row in cash_by_event[eid]:
                    checks.test("cash_legs", row["amount_minor"] == abs(row["signed_minor"]) > 0 and all(row[k] == event[e] for k, e in (
                        ("occurred_at", "occurred_at"), ("kind", "event_type"), ("category", "category"), ("scope", "scope"), ("merchant", "counterparty"))), row["transaction_id"])
                    checks.test("transfer_pairing", row["transfer_group_id"] == (eid if kind == "internal_transfer" else None), row["transaction_id"])
            if kind == "internal_transfer":
                principal["internal_transfer_gross_each_direction_minor"] += sum(d for d in cash_changes.values() if d > 0)
                checks.test("principal_and_return_semantics", len(changes) == 2 and len(cash_changes) == 2 and grouped["cash"] == 0 and recognized == net_expense == 0, eid)
            elif kind == "investment_purchase":
                principal["investment_purchase_principal_minor"] += grouped["investment"]
                checks.test("principal_and_return_semantics", set(grouped) <= {"cash", "investment"} and grouped["cash"] < 0 and grouped["investment"] == -grouped["cash"] and recognized == net_expense == 0, eid)
                for aid, delta in changes.items():
                    if aid in asset_ids:
                        costs[aid] += delta
                        cost_refs[aid].append(eid)
            elif kind == "investment_redemption":
                realized_expected = 0
                for aid, delta in changes.items():
                    if accounts[aid]["account_class"] != "investment":
                        continue
                    before, redeemed = balances[aid], -delta
                    valid = before > 0 and 0 < redeemed <= before
                    checks.test("cost_and_return_replay", valid, eid)
                    if valid:
                        cost_removed = costs[aid] if redeemed == before else costs[aid] * redeemed // before
                        costs[aid] -= cost_removed
                        cost_refs[aid].append(eid)
                        principal["redemption_removed_cost_minor"] += cost_removed
                        realized_expected += redeemed - cost_removed
                principal["redemption_cash_proceeds_minor"] += grouped["cash"]
                principal["redemption_realized_return_minor"] += realized_expected
                checks.test("cost_and_return_replay", realized == realized_expected and income_lines.get("income:unrealized_fund", 0) == realized_expected, eid)
                checks.test("principal_and_return_semantics", grouped["cash"] > 0 and grouped["cash"] == -grouped["investment"] and recognized == realized and net_expense == 0 and set(grouped) <= {"cash", "investment", "income"}, eid)
            elif kind == "loan_repayment":
                principal["loan_repaid_principal_minor"] += grouped["liability"]
                principal["loan_interest_expense_minor"] += net_expense
                checks.test("principal_and_return_semantics", grouped["cash"] < 0 and grouped["liability"] > 0 and recognized == 0 and net_expense >= 0 and grouped["liability"] + net_expense == -grouped["cash"] and set(grouped) <= {"cash", "liability", "expense"}, eid)
            elif kind == "refund":
                reference = events.get(event["reference_id"])
                eligible = bool(reference and reference["event_type"] == "expense" and reference["occurred_at"] <= event["occurred_at"] and reference["category"] == event["category"] and reference["scope"] == event["scope"])
                reference_lines = lines.get(event["reference_id"], [])
                original = defaultdict(int)
                for line in reference_lines:
                    original[line["account_id"]] += line["debit_minor"] - line["credit_minor"]
                expense_ids = {a for a in changes if accounts[a]["account_class"] == "expense"}
                for aid in expense_ids:
                    refunded[(event["reference_id"], aid)] -= changes[aid]
                    eligible &= original.get(aid, 0) >= refunded[(event["reference_id"], aid)] > 0
                checks.test("refund_reference_and_netting", eligible and bool(expense_ids) and grouped["cash"] > 0 and net_expense == -grouped["cash"] and recognized == 0 and set(grouped) <= {"cash", "expense"}, eid)
            elif kind == "valuation":
                principal["noncash_depreciation_minor"] += sum(d for a, d in changes.items() if accounts[a]["code"].startswith("expense:depreciation"))
                checks.test("principal_and_return_semantics", not cash_changes and recognized == 0 and net_expense == 0 and set(grouped) <= {"investment", "real_asset", "income", "expense"}, eid)
            elif kind in ("income", "expense"):
                checks.test("principal_and_return_semantics", set(grouped) <= {"cash", kind} and (grouped["cash"] > 0 if kind == "income" else grouped["cash"] < 0), eid)
            if kind == "opening":
                checks.test("cash_legs", not cash_by_event[eid], eid)
            for aid, delta in changes.items():
                balances[aid] += delta
                klass = accounts[aid]["account_class"]
                checks.test("event_balance_ranges", balances[aid] >= 0 if klass in ("cash", "investment", "real_asset") else balances[aid] <= 0 if klass == "liability" else True, f"{eid}/{aid}")
        expected = _totals(balances, accounts)
        replay[day] = expected
        checks.test("daily_balance_replay", all(daily.get(day, {}).get(k) == v for k, v in expected.items()), f"{uid}/{day}")
        for aid in asset_ids:
            row = assets.get((day, aid), {})
            checks.test("asset_value_and_cost_replay", (row.get("book_value_minor"), row.get("cost_basis_minor"), row.get("unrealized_pnl_minor")) == (balances[aid], costs[aid], balances[aid] - costs[aid]), f"{uid}/{day}/{aid}")
        for aid in loan_ids:
            checks.test("loan_principal_replay", loans.get((day, aid)) == -balances[aid], f"{uid}/{day}/{aid}")
        daily_metrics[day] = metric
    checks.test("cash_legs", set(cash_by_event) <= set(events), f"{uid}: orphan cash events")

    partial = {}
    for table, annual in (("monthly_summaries", False), ("annual_summaries", True)):
        rows = list(map(dict, db.execute(f"SELECT * FROM {table} WHERE user_id=? ORDER BY period_start", (uid,))))
        expected_periods = _periods(annual)
        checks.test("period_calendar_semantics", [(r["period_start"], r["period_end"], r["partial_period"]) for r in rows] == expected_periods, f"{uid}/{table}")
        partial[table] = [{"period_start": first, "period_end": last} for first, last, flag in expected_periods if flag]
        for row in rows:
            period_days = [d for d in days if row["period_start"] <= d <= row["period_end"]]
            expected = {k: sum(daily_metrics[d][k] for d in period_days) for k in METRICS}
            expected["net_saving_minor"] = expected["income_minor"] - expected["expense_minor"]
            expected["opening_cash_minor"] = opening_cash.get(row["period_start"])
            expected["closing_cash_minor"] = replay.get(row["period_end"], {}).get("cash_minor")
            checks.test("independent_period_summaries", all(row[k] == v for k, v in expected.items()), f"{uid}/{table}/{row['period_start']}")
            checks.test("independent_period_summaries", row["closing_cash_minor"] - row["opening_cash_minor"] == row["cash_inflow_minor"] - row["cash_outflow_minor"], f"{uid}/{table}/{row['period_start']}: cash bridge")
    user_counts = {"cash_transaction_count": sum(kind_counts.values()), "journal_event_count": len(events), "daily_snapshot_count": len(daily)}
    expected_user = {**user, **user_counts, "latest_balances": {"user_id": uid, "date": END.isoformat(), **replay[END.isoformat()]}}
    checks.test("current_json_and_global_summary", all(current.get(k) == v for k, v in expected_user.items()), f"{uid}: current metadata/totals")
    checks.test("current_json_and_global_summary", summary_users.get(uid) == expected_user, f"{uid}: summary user")
    expected_collections = {"accounts": [], "assets": [], "loans": []}
    for aid, account in accounts.items():
        klass = account["account_class"]
        if klass not in ("cash", "investment", "real_asset", "liability"):
            continue
        record = {**account, "balance_minor": balances[aid]}
        if klass == "liability":
            record["outstanding_principal_minor"] = -balances[aid]
        elif aid in asset_ids:
            record.update(book_value_minor=balances[aid], cost_basis_minor=costs[aid], unrealized_pnl_minor=balances[aid] - costs[aid])
        key = "accounts" if klass == "cash" else "loans" if klass == "liability" else "assets"
        expected_collections[key].append(record)
    for key, expected in expected_collections.items():
        actual = current.get(key, [])
        valid = isinstance(actual, list) and all(isinstance(r, dict) for r in actual)
        checks.test("current_holdings_and_provenance", valid and sorted(actual, key=lambda r: r.get("account_id", "")) == sorted(expected, key=lambda r: r["account_id"]), f"{uid}/{key}")
    for table, key in (("recurring_obligations", "obligation_id"), ("financial_targets", "target_id")):
        expected = list(map(dict, db.execute(f"SELECT * FROM {table} WHERE user_id=? ORDER BY {key}", (uid,))))
        actual = current.get(table, [])
        checks.test("forecast_and_target_boundary", isinstance(actual, list) and all(isinstance(r, dict) for r in actual) and sorted(actual, key=lambda r: r.get(key, "")) == expected, f"{uid}/{table}: JSON records")
        for row in expected:
            if table == "recurring_obligations":
                valid = row["status"] == "forecast_not_paid" and row["next_due_on"] > END.isoformat() and row["amount_minor"] > 0 and row[key] not in events
            else:
                valid = row["as_of"] == END.isoformat() and 0 <= row["allocated_minor"] <= row["target_minor"] and row["target_minor"] == user["reserve_minor"] and row[key] not in events
            checks.test("forecast_and_target_boundary", valid, row[key])
    annual_totals = {k: sum(day[k] for day in daily_metrics.values()) for k in METRICS}
    return {"user_id": uid, "display_name": user["display_name"], "counts": user_counts,
            "coverage_start": START.isoformat(), "coverage_end": END.isoformat(), "days": len(daily),
            "partial_periods": partial, "closing_balances_minor": replay[END.isoformat()],
            "ledger_totals_minor": annual_totals, "cash_kind_counts": dict(kind_counts),
            "cash_kind_signed_minor": dict(kind_totals),
            "principal_and_noncash_breakdown_minor": dict(principal),
            "investment_cost_minor": sum(costs[a] for a in asset_ids if accounts[a]["account_class"] == "investment"),
            "current_asset_cost_provenance": [{"account_id": aid, "source": accounts[aid]["source"],
                "account_class": accounts[aid]["account_class"], "book_value_minor": balances[aid],
                "cost_basis_minor": costs[aid], "cost_input_event_ids": cost_refs[aid]}
                for aid in sorted(asset_ids)],
            "holding_basis": "synthetic_book_value_and_cost; no units or real NAV linkage"}


def audit_dataset(source_dir: Path) -> dict:
    """Read source files without mutation; return explicit failures and limits."""
    source_dir = Path(source_dir).expanduser().resolve()
    checks = _Checks()
    report = {"schema_version": 1, "passed": False, "source_dir": str(source_dir),
              "source": SOURCE, "amount_unit": "integer_CNY_cent", "counts": {},
              "source_sha256": {}, "accounts": {}, "checks": [],
              "uncovered": [
                  {"id": "external_real_world_validation", "reason": "All four accounts are synthetic; no real customer or industry benchmark is supplied."},
                  {"id": "fund_units_and_real_nav_linkage", "reason": "Source holdings have book values/costs, but no units, product codes, NAV dates, or manager NAV input references. No units are inferred."},
                  {"id": "csv_and_zip_exports", "reason": "This audit covers SQLite, four current JSON files, summary, contract and selected manifest digests; CSV/ZIP contents are not replayed."},
                  {"id": "simulation_parameter_realism", "reason": "Arithmetic reconciliation does not validate return assumptions, customer representativeness, risk suitability, or forecast reliability."}],
              "generated_at": datetime.now(timezone.utc).isoformat()}
    names = ["financial_data.sqlite3", "summary.json", "data_contract.json", "manifest.json"] + [f"{uid}/current_finances.json" for uid in USERS]
    for name in names:
        path = source_dir / name
        checks.test("source_files", path.is_file(), name)
        if path.is_file():
            report["source_sha256"][name] = _hash(path)
    for suffix in ("-wal", "-journal"):
        path = source_dir / ("financial_data.sqlite3" + suffix)
        checks.test("immutable_source_snapshot", not path.exists() or path.stat().st_size == 0, str(path))
    summary = _load_json(source_dir / "summary.json", checks)
    contract = _load_json(source_dir / "data_contract.json", checks)
    manifest = _load_json(source_dir / "manifest.json", checks)
    currents = {uid: _load_json(source_dir / uid / "current_finances.json", checks) for uid in USERS}
    manifest_hashes = {r.get("path"): r.get("sha256") for r in manifest.get("files", []) if isinstance(r, dict)}
    for name, digest in report["source_sha256"].items():
        if name != "manifest.json":
            checks.test("manifest_hashes", manifest_hashes.get(name) == digest, name)
    database = source_dir / "financial_data.sqlite3"
    try:
        # immutable avoids even SQLite's incidental WAL/SHM sidecar writes.
        # Any pre-existing WAL/journal is separately rejected above.
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            checks.test("sqlite_integrity", [r[0] for r in db.execute("PRAGMA integrity_check")] == ["ok"], "integrity_check")
            checks.test("sqlite_integrity", not list(db.execute("PRAGMA foreign_key_check")), "foreign_key_check")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            checks.test("schema_contract", tables == set(TABLE_KEYS), "expected source tables")
            if not set(TABLE_KEYS) <= tables:
                raise ValueError("Missing source tables; cannot replay this snapshot")
            users = {r[0] for r in db.execute("SELECT user_id FROM users")}
            checks.test("stable_user_ids", users == set(USERS), sorted(users))
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
            for key, expected in {"source": SOURCE, "currency": "CNY", "amount_unit": "integer_cent", "start_date": START.isoformat(), "end_date": END.isoformat(), "is_real_bank_data": "false", "is_real_product_return": "false"}.items():
                checks.test("source_provenance", metadata.get(key) == expected, key)
            checks.test("source_provenance", contract.get("source") == manifest.get("source") == SOURCE, "contract/manifest source")
            checks.test("source_provenance", (manifest.get("coverage_start"), manifest.get("coverage_end"), manifest.get("days_per_user")) == (START.isoformat(), END.isoformat(), len(_days())), "manifest coverage")
            nullable = {"reference_id", "transfer_group_id"}
            for table, keys in TABLE_KEYS.items():
                report["counts"][table] = db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                schema = list(map(dict, db.execute(f'PRAGMA table_info("{table}")')))
                expected_schema = contract.get("tables", {}).get(table, [])
                checks.test("schema_contract", [{"cid": r["cid"], "name": r["name"], "type": r["type"], "notnull": r["notnull"], "default": r["dflt_value"], "pk": r["pk"]} for r in schema] == expected_schema, table)
                key_expr = ",".join(f'"{k}"' for k in keys)
                duplicates = db.execute(f'SELECT count(*) FROM (SELECT {key_expr} FROM "{table}" GROUP BY {key_expr} HAVING count(*)>1)').fetchone()[0]
                checks.test("unique_keys", duplicates == 0, table)
                for column in schema:
                    name = column["name"]
                    if name not in nullable:
                        invalid = db.execute(f'SELECT count(*) FROM "{table}" WHERE "{name}" IS NULL OR (typeof("{name}")=\'text\' AND trim("{name}")=\'\')').fetchone()[0]
                        checks.test("required_value_coverage", invalid == 0, f"{table}.{name}")
                    if "INT" in column["type"].upper():
                        invalid = db.execute(f'SELECT count(*) FROM "{table}" WHERE typeof("{name}")<>\'integer\'').fetchone()[0]
                        checks.test("integer_amounts_and_fields", invalid == 0, f"{table}.{name}")
                columns = {r["name"] for r in schema}
                if "user_id" in columns:
                    invalid = db.execute(f'SELECT count(*) FROM "{table}" WHERE user_id NOT IN (SELECT user_id FROM users)').fetchone()[0]
                    checks.test("user_ownership", invalid == 0, table)
                if "source" in columns:
                    invalid = db.execute(f'SELECT count(*) FROM "{table}" WHERE source<>?', (SOURCE,)).fetchone()[0]
                    checks.test("source_provenance", invalid == 0, table)
                for key, target, target_key in (("account_id", "ledger_accounts", "account_id"), ("asset_id", "ledger_accounts", "account_id"), ("loan_id", "ledger_accounts", "account_id"), ("event_id", "journal_events", "event_id")):
                    if key in columns and table != target:
                        invalid = db.execute(f'SELECT count(*) FROM "{table}" x LEFT JOIN "{target}" y ON x."{key}"=y."{target_key}" WHERE y."{target_key}" IS NULL OR x.user_id<>y.user_id').fetchone()[0]
                        checks.test("user_ownership", invalid == 0, f"{table}.{key}")
            checks.test("schema_contract", not list(db.execute("SELECT 1 FROM ledger_accounts WHERE account_class NOT IN ('cash','investment','real_asset','liability','income','expense','equity')")), "account classes")
            checks.test("journal_leg_ranges", db.execute("SELECT count(*) FROM journal_lines WHERE NOT ((debit_minor>0 AND credit_minor=0) OR (credit_minor>0 AND debit_minor=0))").fetchone()[0] == 0, "one sided positive journal leg")
            checks.test("user_ownership", db.execute("SELECT count(*) FROM ledger_accounts WHERE account_id<>user_id||'::'||code").fetchone()[0] == 0, "namespaced account IDs")
            checks.test("user_parameter_ranges", db.execute("SELECT count(*) FROM users WHERE risk_level NOT BETWEEN 1 AND 5 OR horizon_days<=0 OR reserve_minor<0").fetchone()[0] == 0, "synthetic risk/horizon/reserve ranges")
            summary_users = {u.get("user_id"): u for u in summary.get("users", []) if isinstance(u, dict)}
            checks.test("current_json_and_global_summary", len(summary.get("users", [])) == len(USERS) and set(summary_users) == set(USERS), "summary IDs")
            checks.test("current_json_and_global_summary", summary.get("table_counts") == report["counts"] and summary.get("metadata") == metadata, "summary counts and metadata")
            checks.test("manifest_hashes", manifest.get("cash_transactions") == report["counts"]["cash_transactions"], "manifest cash count")
            for uid in USERS:
                if uid in users:
                    try:
                        report["accounts"][uid] = _audit_account(db, uid, checks, currents[uid], summary_users)
                    except (KeyError, TypeError, ValueError, sqlite3.DatabaseError) as error:
                        checks.test("audit_execution", False, f"{uid}: {type(error).__name__}: {error}")
            db.rollback()
    except (OSError, ValueError, TypeError, sqlite3.DatabaseError) as error:
        checks.test("audit_execution", False, f"{type(error).__name__}: {error}")
    for name, digest in report["source_sha256"].items():
        path = source_dir / name
        checks.test("immutable_source_snapshot", path.is_file() and _hash(path) == digest, name)
    report["checks"] = list(checks.items.values())
    report["passed"] = bool(report["checks"]) and all(c["passed"] for c in report["checks"]) and set(report["accounts"]) == set(USERS)
    report["failure_count"] = sum(c["failure_count"] for c in report["checks"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).resolve().parents[1] / "outputs/contest-finances")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output.expanduser().resolve() if args.output else None
    source = args.source_dir.expanduser().resolve()
    if output == source or (output and source in output.parents):
        parser.error("Audit output must be outside the read-only source directory")
    report = audit_dataset(args.source_dir)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "failure_count": report["failure_count"], "output": str(output)}, ensure_ascii=False))
    else:
        print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
