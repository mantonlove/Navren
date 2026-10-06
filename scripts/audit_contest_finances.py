#!/usr/bin/env python3
"""独立、只读的个人财务合成账本审计；不导入生成器或以其汇总函数作判据。

金额为整数分；期间边界为闭区间；分录从零开始，opening 分录只计入存量。
收入排除 income:unrealized*，净费用排除 expense:depreciation* 并扣退款。
现金流是全部非 opening 现金腿的毛额，包含内部转账两腿。
退出码：0 检查通过（允许明确未覆盖），1 数据失败，2 数据库/结构错误。
"""
from __future__ import annotations

import argparse
import calendar
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3

SOURCE = "navren_synthetic"
START_DATE, END_DATE = "2016-10-04", "2026-10-03"
CATEGORIES = ("identity", "cross_total", "spec", "entity", "ranges", "duplicates",
              "external_total", "value_coverage", "time_semantics")
FIELDS = {
    "metadata": "key value",
    "users": "user_id display_name risk_level horizon_days reserve_minor source start_date end_date",
    "ledger_accounts": "account_id user_id code name account_class scope opening_balance_minor source",
    "journal_events": "event_id user_id occurred_at event_type category scope counterparty source reference_id",
    "journal_lines": "line_id event_id user_id account_id debit_minor credit_minor",
    "cash_transactions": "transaction_id user_id occurred_at account_id merchant amount_minor signed_minor kind category scope event_id transfer_group_id source",
    "daily_balances": "user_id date cash_minor investment_minor real_assets_minor liabilities_minor total_assets_minor net_worth_minor",
    "asset_values": "user_id date asset_id book_value_minor cost_basis_minor unrealized_pnl_minor",
    "loan_balances": "user_id date loan_id principal_minor",
    "monthly_summaries": "user_id period_start period_end partial_period income_minor expense_minor refund_minor net_saving_minor cash_inflow_minor cash_outflow_minor opening_cash_minor closing_cash_minor realized_return_minor unrealized_change_minor",
    "annual_summaries": "user_id period_start period_end partial_period income_minor expense_minor refund_minor net_saving_minor cash_inflow_minor cash_outflow_minor opening_cash_minor closing_cash_minor realized_return_minor unrealized_change_minor",
}
KEYS = {
    "metadata": ("key",), "users": ("user_id",), "ledger_accounts": ("account_id",),
    "journal_events": ("event_id",), "journal_lines": ("line_id",),
    "cash_transactions": ("transaction_id",), "daily_balances": ("user_id", "date"),
    "asset_values": ("user_id", "date", "asset_id"),
    "loan_balances": ("user_id", "date", "loan_id"),
    "monthly_summaries": ("user_id", "period_start"),
    "annual_summaries": ("user_id", "period_start"),
}
OPTIONAL = {("journal_events", "reference_id"), ("cash_transactions", "transfer_group_id")}
CLASSES = {"cash", "investment", "real_asset", "liability", "income", "expense", "equity"}
SUMMARY_FIELDS = tuple(FIELDS["monthly_summaries"].split()[4:])


class Check:
    def __init__(self, category, name, sample_limit=20):
        self.category, self.name = category, name
        self.failures, self.samples, self.sample_limit = 0, [], sample_limit
        self.details = {}

    def fail(self, message, **where):
        self.failures += 1
        if len(self.samples) < self.sample_limit:
            self.samples.append({"message": message, **where})

    def equal(self, actual, expected, message, **where):
        if actual != expected:
            self.fail(message, actual=actual, expected=expected, **where)

    def as_dict(self):
        return {"category": self.category, "name": self.name,
                "passed": self.failures == 0, "status": "failed" if self.failures else "passed",
                "failure_count": self.failures, "samples": self.samples, **self.details}


def iso_day(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("日期必须是 YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("日期格式不规范")
    return parsed


def timestamp(value):
    if not isinstance(value, str) or not value.endswith("+08:00") or "T" not in value:
        raise ValueError("时间戳必须显式带 +08:00")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(hours=8):
        raise ValueError("时区必须是 UTC+8")
    return parsed


def periods(start, end, annual=False):
    first = date(start.year, 1 if annual else start.month, 1)
    result = []
    while first <= end:
        last = date(first.year, 12, 31) if annual else date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
        result.append((first, last, max(first, start), min(last, end)))
        first = date(first.year + 1, 1, 1) if annual or first.month == 12 else date(first.year, first.month + 1, 1)
    return result


def _report(db, checks, rows=None, config_error=False):
    data = [c.as_dict() for c in checks]
    covered = {c["category"] for c in data}
    uncovered = [c for c in CATEGORIES if c not in covered]
    return {
        "audit_version": "contest-finances-v1", "database": str(Path(db).resolve()),
        "passed": bool(data) and not config_error and all(c["passed"] for c in data),
        "config_error": config_error, "checks": data,
        "uncovered_categories": uncovered,
        "uncovered_details": {"external_total": "合成数据无独立外部真实总量基准；不证明真实客户或人口代表性。"},
        "row_counts": {t: len(v) for t, v in (rows or {}).items()},
        "conventions": {"source": SOURCE, "money": "integer_minor_units", "timezone": "+08:00",
                        "period_end": "inclusive_clipped_to_sample", "opening_balances": "debit_positive_credit_negative",
                        "cash_flows": "all_nonopening_cash_legs_including_internal_transfers",
                        "income": "income_except_income:unrealized*",
                        "expense": "net_debit_expense_except_expense:depreciation*",
                        "refund": "net_credit_expense_in_refund_events",
                        "daily_replay": "zero_balances_plus_all_journal_lines_including_opening"},
    }


def audit_database(db, *, start_date=START_DATE, end_date=END_DATE, expected_users=4):
    """返回可序列化审计报告。日期参数供独立的小型故障夹具使用。"""
    checks = []
    def check(category, name):
        c = Check(category, name); checks.append(c); return c
    schema = check("spec", "database_schema")
    rows = {}
    try:
        start, end = iso_day(start_date), iso_day(end_date)
        if start > end or end > iso_day(END_DATE):
            raise ValueError("样本日期无效或晚于约定截止日")
        with closing(sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            known = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, names in FIELDS.items():
                if table not in known:
                    schema.fail("缺少必需表", table=table); continue
                columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                absent = sorted(set(names.split()) - columns)
                if absent:
                    schema.fail("缺少必需列", table=table, missing=absent); continue
                rows[table] = [dict(r) for r in conn.execute(f'SELECT {", ".join(names.split())} FROM "{table}"')]
    except (sqlite3.Error, OSError, ValueError) as exc:
        schema.fail("无法只读加载数据库", error=str(exc))
    if schema.failures:
        return _report(db, checks, rows, config_error=True)

    values = check("value_coverage", "required_values_and_minor_unit_types")
    coverage = {}
    for table, records in rows.items():
        for field in FIELDS[table].split():
            if (table, field) in OPTIONAL:
                continue
            present = sum(r[field] is not None for r in records)
            coverage[f"{table}.{field}"] = {"present": present, "expected": len(records)}
            for idx, r in enumerate(records):
                value = r[field]
                if value is None:
                    values.fail("必需值为空", table=table, row=idx, field=field); continue
                numeric = field.endswith("_minor") or field in {"risk_level", "horizon_days", "partial_period"}
                if numeric and type(value) is not int:
                    values.fail("金额/数值必须为整数，不接受浮点或文本", table=table, row=idx, field=field, actual=value)
                elif not numeric and not isinstance(value, str):
                    values.fail("文本字段类型无效", table=table, row=idx, field=field)
                elif not numeric and field not in {"counterparty", "category"} and not value.strip():
                    values.fail("必需文本为空串", table=table, row=idx, field=field)
    values.details["value_coverage"] = coverage

    dup = check("duplicates", "unique_keys_and_account_codes")
    for table, fields in KEYS.items():
        for key, count in Counter(tuple(r[f] for f in fields) for r in rows[table]).items():
            if count > 1:
                dup.fail("重复主键", table=table, key=list(key), count=count)
    for key, count in Counter((r["user_id"], r["code"]) for r in rows["ledger_accounts"]).items():
        if count > 1:
            dup.fail("同一用户科目代码重复", key=list(key), count=count)

    users = {r["user_id"]: r for r in rows["users"]}
    accounts = {r["account_id"]: r for r in rows["ledger_accounts"]}
    events = {r["event_id"]: r for r in rows["journal_events"]}
    entity = check("entity", "foreign_keys_and_user_ownership")
    entity.equal(len(rows["users"]), expected_users, "用户数量与固定样本规格不一致")
    for table, records in rows.items():
        if table == "metadata": continue
        for idx, r in enumerate(records):
            user = r["user_id"]
            if user not in users:
                entity.fail("未知用户", table=table, row=idx, user_id=user)
            if table in {"journal_lines", "cash_transactions"}:
                account, event = accounts.get(r["account_id"]), events.get(r["event_id"])
                if not account or not event:
                    entity.fail("现金/分录关联缺失", table=table, row=idx)
                elif account["user_id"] != user or event["user_id"] != user:
                    entity.fail("跨用户账户或事件归属错误", table=table, row=idx)
                if table == "cash_transactions" and account and account["account_class"] != "cash":
                    entity.fail("现金流水指向非现金科目", row=idx, account_id=r["account_id"])
            if table in {"asset_values", "loan_balances"}:
                ident = r["asset_id" if table == "asset_values" else "loan_id"]
                account = accounts.get(ident)
                allowed = {"investment", "real_asset"} if table == "asset_values" else {"liability"}
                if not account or account["user_id"] != user or account["account_class"] not in allowed:
                    entity.fail("资产/负债明细未绑定同用户对应科目", table=table, row=idx, object_id=ident)

    spec = check("spec", "source_and_dataset_contract")
    metadata = {r["key"]: r["value"] for r in rows["metadata"]}
    for key, expected in (("source", SOURCE), ("start_date", start_date), ("end_date", end_date),
                          ("amount_unit", "integer_cent"), ("is_real_bank_data", "false"),
                          ("is_real_product_return", "false")):
        spec.equal(metadata.get(key), expected, "元数据口径缺失或不一致", key=key)
    for table in ("users", "ledger_accounts", "journal_events", "cash_transactions"):
        for idx, r in enumerate(rows[table]):
            spec.equal(r["source"], SOURCE, "合成数据来源不可冒充真实来源", table=table, row=idx)
    for r in rows["users"]:
        spec.equal(r["start_date"], start_date, "用户起始日期不一致", user_id=r["user_id"])
        spec.equal(r["end_date"], end_date, "用户截止日期不一致", user_id=r["user_id"])

    ranges = check("ranges", "amount_domains_and_account_types")
    if not values.failures:
        for r in rows["users"]:
            if not 1 <= r["risk_level"] <= 5 or r["horizon_days"] < 1 or r["reserve_minor"] < 0:
                ranges.fail("风险、期限或预留资金超出值域", user_id=r["user_id"])
        for r in rows["ledger_accounts"]:
            kind, opening_value = r["account_class"], r["opening_balance_minor"]
            invalid_opening = (kind in {"cash", "investment", "real_asset"} and opening_value < 0) or (kind == "liability" and opening_value > 0)
            if kind not in CLASSES or invalid_opening:
                ranges.fail("科目类型/期初借方正贷方负余额无效", account_id=r["account_id"])
        for r in rows["journal_lines"]:
            if r["debit_minor"] < 0 or r["credit_minor"] < 0 or (r["debit_minor"] and r["credit_minor"]):
                ranges.fail("借贷金额须非负且不能同笔双向", line_id=r["line_id"])
        for r in rows["cash_transactions"]:
            if r["amount_minor"] < 0 or abs(r["signed_minor"]) != r["amount_minor"]:
                ranges.fail("流水绝对额与方向金额不符", transaction_id=r["transaction_id"])
        nonnegative = {
            "daily_balances": ("cash_minor", "investment_minor", "real_assets_minor", "liabilities_minor", "total_assets_minor"),
            "asset_values": ("book_value_minor", "cost_basis_minor"), "loan_balances": ("principal_minor",),
            "monthly_summaries": ("refund_minor", "cash_inflow_minor", "cash_outflow_minor", "opening_cash_minor", "closing_cash_minor"),
            "annual_summaries": ("refund_minor", "cash_inflow_minor", "cash_outflow_minor", "opening_cash_minor", "closing_cash_minor"),
        }
        for table, fields in nonnegative.items():
            for idx, r in enumerate(rows[table]):
                for f in fields:
                    if r[f] < 0: ranges.fail("正常方向存量/毛流量不可负", table=table, row=idx, field=f)
        for table in ("monthly_summaries", "annual_summaries"):
            for idx, r in enumerate(rows[table]):
                if r["partial_period"] not in (0, 1): ranges.fail("非完整期间标志须为0/1", table=table, row=idx)
    else:
        ranges.fail("必需值错误，不能可靠执行值域运算")

    times = check("time_semantics", "timezone_range_and_complete_calendar")
    event_dates = {}
    for table in ("journal_events", "cash_transactions"):
        for idx, r in enumerate(rows[table]):
            try:
                dt = timestamp(r["occurred_at"])
                if not start <= dt.date() <= end: raise ValueError("事件日期越界/落到未来")
                if table == "journal_events": event_dates[r["event_id"]] = dt
            except (ValueError, TypeError) as exc:
                times.fail(str(exc), table=table, row=idx, occurred_at=r["occurred_at"])
    for table in ("daily_balances", "asset_values", "loan_balances"):
        for idx, r in enumerate(rows[table]):
            try:
                if not start <= iso_day(r["date"]) <= end: raise ValueError("存量日期越界")
            except (ValueError, TypeError) as exc: times.fail(str(exc), table=table, row=idx)
    calendar_days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    expected_days = {(u, d) for u in users for d in calendar_days}
    actual_days = {(r["user_id"], r["date"]) for r in rows["daily_balances"]}
    for u, d in sorted(expected_days - actual_days): times.fail("日历缺日", user_id=u, date=d)
    for u, d in sorted(actual_days - expected_days): times.fail("日历有额外用户/日期", user_id=u, date=d)
    times.details["expected_daily_rows"] = len(expected_days)
    for table in ("monthly_summaries", "annual_summaries"):
        ps = periods(start, end, annual=table == "annual_summaries")
        expected = {(u, lo.isoformat()): (a, b, lo, hi) for u in users for a, b, lo, hi in ps}
        actual = {(r["user_id"], r["period_start"]): r for r in rows[table]}
        for key in expected.keys() - actual.keys(): times.fail("期间汇总缺期", table=table, key=list(key))
        for key in actual.keys() - expected.keys(): times.fail("期间汇总边界/用户多余", table=table, key=list(key))
        for key in expected.keys() & actual.keys():
            a, b, lo, hi = expected[key]; r = actual[key]
            times.equal(r["period_end"], hi.isoformat(), "汇总必须标注实际样本闭区间", table=table, key=list(key))
            times.equal(r["partial_period"], int(lo != a or hi != b), "首尾非完整期间未正确标注", table=table, key=list(key))

    identity = check("identity", "balanced_events_and_daily_replay")
    totals = check("cross_total", "cash_legs_assets_and_period_summaries")
    if values.failures or entity.failures or times.failures or dup.failures:
        identity.fail("值、归属、日期或唯一键检查失败，停止依赖其的回放")
        totals.fail("基础检查失败，停止依赖其的汇总核对")
        return _report(db, checks, rows)

    lines = defaultdict(list)
    for r in rows["journal_lines"]: lines[r["event_id"]].append(r)
    for event_id in events:
        ls = lines[event_id]
        if not ls: identity.fail("事件没有分录", event_id=event_id)
        identity.equal(sum(r["debit_minor"] for r in ls), sum(r["credit_minor"] for r in ls), "事件借贷不平", event_id=event_id)
    opening_counts = Counter(e["user_id"] for e in events.values() if e["event_type"] == "opening")
    opening = defaultdict(int)
    for e in events.values():
        if e["event_type"] == "opening":
            times.equal(e["occurred_at"], start_date + "T00:00:00+08:00", "opening必须在样本首日零时", event_id=e["event_id"])
            for line in lines[e["event_id"]]: opening[line["account_id"]] += line["debit_minor"] - line["credit_minor"]
    for user in users: identity.equal(opening_counts[user], 1, "每用户必须有且只有一个opening事件", user_id=user)
    for ident, account in accounts.items():
        expected = account["opening_balance_minor"]
        identity.equal(opening[ident], expected, "科目期初值与opening分录不符", account_id=ident)

    cash_expected = Counter()
    for e in events.values():
        if e["event_type"] == "opening": continue
        for line in lines[e["event_id"]]:
            delta = line["debit_minor"] - line["credit_minor"]
            if accounts[line["account_id"]]["account_class"] == "cash" and delta:
                cash_expected[(e["event_id"], e["user_id"], line["account_id"], delta)] += 1
    cash_actual = Counter()
    for row in rows["cash_transactions"]:
        e = events[row["event_id"]]
        if e["event_type"] == "opening": totals.fail("opening不应成为期间现金流水", transaction_id=row["transaction_id"])
        totals.equal(timestamp(row["occurred_at"]), event_dates[e["event_id"]], "流水和事件时间不一致", transaction_id=row["transaction_id"])
        totals.equal(row["scope"], e["scope"], "流水与事件scope不一致", transaction_id=row["transaction_id"])
        cash_actual[(row["event_id"], row["user_id"], row["account_id"], row["signed_minor"])] += 1
    for key, count in (cash_expected - cash_actual).items(): totals.fail("非opening现金分录缺少/错配流水", key=list(key), count=count)
    for key, count in (cash_actual - cash_expected).items(): totals.fail("流水没有对应现金分录或重复现金腿", key=list(key), count=count)

    days = defaultdict(list)
    for e in events.values(): days[event_dates[e["event_id"]].date().isoformat()].append(e)
    daily = {(r["user_id"], r["date"]): r for r in rows["daily_balances"]}
    assets = {(r["user_id"], r["date"], r["asset_id"]): r for r in rows["asset_values"]}
    loans = {(r["user_id"], r["date"], r["loan_id"]): r for r in rows["loan_balances"]}
    balances = dict.fromkeys(accounts, 0)
    costs = {ident: 0 for ident, a in accounts.items() if a["account_class"] in {"investment", "real_asset"}}
    history = {}
    for on in calendar_days:
        for e in sorted(days[on], key=lambda x: (event_dates[x["event_id"]], x["event_id"])):
            movements = defaultdict(int)
            for line in lines[e["event_id"]]: movements[line["account_id"]] += line["debit_minor"] - line["credit_minor"]
            if e["event_type"] == "opening":
                for ident, change in movements.items():
                    if ident in costs: costs[ident] += change
            elif e["event_type"] == "investment_purchase":
                for ident, change in movements.items():
                    if accounts[ident]["account_class"] == "investment":
                        if change <= 0: identity.fail("申购须增加投资资产成本", event_id=e["event_id"], account_id=ident)
                        costs[ident] += change
            elif e["event_type"] == "investment_redemption":
                realized = 0
                redeemed = False
                for ident, change in movements.items():
                    if accounts[ident]["account_class"] != "investment": continue
                    redeemed = True
                    amount, before = -change, balances[ident]
                    if amount <= 0 or before <= 0 or amount > before:
                        identity.fail("赎回额必须在事件前持仓账面值内", event_id=e["event_id"], account_id=ident)
                        continue
                    removed = costs[ident] if amount == before else costs[ident] * amount // before
                    costs[ident] -= removed
                    realized += amount - removed
                if not redeemed: identity.fail("赎回事件缺少投资资产腿", event_id=e["event_id"])
                actual_realized = sum(r["credit_minor"] - r["debit_minor"] for r in lines[e["event_id"]]
                                      if accounts[r["account_id"]]["code"].startswith("income:investment_realized"))
                actual_reclassified = sum(r["credit_minor"] - r["debit_minor"] for r in lines[e["event_id"]]
                                          if accounts[r["account_id"]]["code"].startswith("income:unrealized_fund"))
                identity.equal(actual_realized, realized, "赎回已实现损益与独立成本剔除不符", event_id=e["event_id"])
                identity.equal(actual_reclassified, -realized, "赎回未实现重分类与已实现损益不相反", event_id=e["event_id"])
            for ident, change in movements.items(): balances[ident] += change
            for ident in movements:
                kind = accounts[ident]["account_class"]
                if kind in {"cash", "investment", "real_asset"} and balances[ident] < 0:
                    ranges.fail("事件后现金/资产科目为负", event_id=e["event_id"], account_id=ident, balance=balances[ident])
                if kind == "liability" and balances[ident] > 0:
                    ranges.fail("负债科目出现借方剩余", event_id=e["event_id"], account_id=ident, balance=balances[ident])
        expected = {u: {"cash_minor": 0, "investment_minor": 0, "real_assets_minor": 0, "liabilities_minor": 0} for u in users}
        for ident, account in accounts.items():
            u, kind, balance = account["user_id"], account["account_class"], balances[ident]
            target = {"cash": "cash_minor", "investment": "investment_minor", "real_asset": "real_assets_minor", "liability": "liabilities_minor"}.get(kind)
            if target: expected[u][target] += -balance if kind == "liability" else balance
            if kind in {"investment", "real_asset"}:
                point = assets.get((u, on, ident))
                if point is None: totals.fail("资产缺少当日估值明细（零持仓也应保留）", user_id=u, date=on, asset_id=ident)
                if point:
                    totals.equal(point["book_value_minor"], balance, "资产明细与科目回放不符", user_id=u, date=on, asset_id=ident)
                    identity.equal(point["cost_basis_minor"], costs[ident], "资产成本与独立事件回放不符", user_id=u, date=on, asset_id=ident)
                    identity.equal(point["book_value_minor"], point["cost_basis_minor"] + point["unrealized_pnl_minor"], "账面值不等于成本加未实现损益", user_id=u, date=on, asset_id=ident)
            elif kind == "liability":
                point = loans.get((u, on, ident))
                if point is None: totals.fail("负债缺少当日明细（已结清也应保留）", user_id=u, date=on, loan_id=ident)
                if point: totals.equal(point["principal_minor"], -balance, "负债明细与科目回放不符", user_id=u, date=on, loan_id=ident)
        for u, expected_row in expected.items():
            row = daily[u, on]
            expected_row["total_assets_minor"] = expected_row["cash_minor"] + expected_row["investment_minor"] + expected_row["real_assets_minor"]
            expected_row["net_worth_minor"] = expected_row["total_assets_minor"] - expected_row["liabilities_minor"]
            for field, value in expected_row.items(): identity.equal(row[field], value, "日余额与独立分录回放不符", user_id=u, date=on, field=field)
            identity.equal(row["total_assets_minor"], row["cash_minor"] + row["investment_minor"] + row["real_assets_minor"], "总资产定义式失败", user_id=u, date=on)
            identity.equal(row["net_worth_minor"], row["total_assets_minor"] - row["liabilities_minor"], "净资产定义式失败", user_id=u, date=on)
            history[u, on] = expected_row

    metrics = defaultdict(lambda: dict.fromkeys(SUMMARY_FIELDS, 0))
    for e in events.values():
        if e["event_type"] == "opening": continue
        key = (e["user_id"], event_dates[e["event_id"]].date().isoformat())
        for line in lines[e["event_id"]]:
            account = accounts[line["account_id"]]; code = account["code"]
            net_credit = line["credit_minor"] - line["debit_minor"]
            if account["account_class"] == "income" and not code.startswith("income:unrealized"):
                metrics[key]["income_minor"] += net_credit
            if account["account_class"] == "expense" and not code.startswith("expense:depreciation"):
                metrics[key]["expense_minor"] -= net_credit
                if e["event_type"] == "refund": metrics[key]["refund_minor"] += net_credit
            if code.startswith("income:investment_realized"): metrics[key]["realized_return_minor"] += net_credit
            if code.startswith("income:unrealized"): metrics[key]["unrealized_change_minor"] += net_credit
    for row in rows["cash_transactions"]:
        key = row["user_id"], timestamp(row["occurred_at"]).date().isoformat()
        metrics[key]["cash_inflow_minor"] += max(row["signed_minor"], 0)
        metrics[key]["cash_outflow_minor"] += max(-row["signed_minor"], 0)
    initial_cash = {u: sum(v for ident, v in opening.items() if accounts[ident]["user_id"] == u and accounts[ident]["account_class"] == "cash") for u in users}
    for table in ("monthly_summaries", "annual_summaries"):
        for r in rows[table]:
            lo, hi = max(iso_day(r["period_start"]), start), min(iso_day(r["period_end"]), end)
            expected = dict.fromkeys(SUMMARY_FIELDS, 0)
            for i in range((hi - lo).days + 1):
                for f, value in metrics[r["user_id"], (lo + timedelta(days=i)).isoformat()].items(): expected[f] += value
            expected["net_saving_minor"] = expected["income_minor"] - expected["expense_minor"]
            expected["opening_cash_minor"] = initial_cash[r["user_id"]] if lo == start else history[r["user_id"], (lo - timedelta(days=1)).isoformat()]["cash_minor"]
            expected["closing_cash_minor"] = history[r["user_id"], hi.isoformat()]["cash_minor"]
            for field, value in expected.items(): totals.equal(r[field], value, "期间汇总与独立科目/现金加总不符", table=table, user_id=r["user_id"], period_start=r["period_start"], field=field)
            identity.equal(r["net_saving_minor"], r["income_minor"] - r["expense_minor"], "结余定义式失败", table=table, user_id=r["user_id"], period_start=r["period_start"])
            identity.equal(r["closing_cash_minor"] - r["opening_cash_minor"], r["cash_inflow_minor"] - r["cash_outflow_minor"], "期间现金存量差与净流量不符", table=table, user_id=r["user_id"], period_start=r["period_start"])
    return _report(db, checks, rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = audit_database(args.db)
    result = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result + "\n", encoding="utf-8")
    print(result)
    return 2 if report["config_error"] else (0 if report["passed"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
