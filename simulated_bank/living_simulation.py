"""独立持续模拟候选；显式构造后才写所给 bank，模块导入无数据库副作用。

默认适配 Navren 现有运行表；只追加生活流水，不消费 HTTP 身份或运行模型。
须由 Base 采用后接统一队列/报表入口，不能据本候选宣称已运行或部署。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
import hashlib
import json
import math
import re
import logging
import threading
from statistics import median_low
from zoneinfo import ZoneInfo

from .simulation_rules import ACCOUNT_ROLES, EVENT_FIELDS, RULES_VERSION, plan_day, rule_catalog


WORKER_VERSION = "navren.living-worker.candidate.v2"
CN = ZoneInfo("Asia/Shanghai")
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS living_states(
       workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),owner_id TEXT NOT NULL,
       persona_id TEXT NOT NULL,initialization_id TEXT NOT NULL,template_sha256 TEXT NOT NULL,
       template_end_on TEXT NOT NULL,closed_through TEXT NOT NULL,rules_version TEXT NOT NULL,
       observations TEXT NOT NULL,calibration TEXT NOT NULL,activated_at TEXT NOT NULL,
       last_run_order INTEGER NOT NULL DEFAULT 0,last_checked_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS living_events(
       event_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspaces(id),
       rule_id TEXT NOT NULL,scheduled_on TEXT NOT NULL,occurrence_index INTEGER NOT NULL,
       scheduled_at TEXT NOT NULL,plan TEXT NOT NULL,rules_version TEXT NOT NULL,
       status TEXT NOT NULL CHECK(status IN ('planned','posted','failed','cancelled')),
       reason TEXT,transaction_id TEXT UNIQUE REFERENCES transactions(id),posted_at TEXT,
       balance_before_minor INTEGER,balance_after_minor INTEGER,
       UNIQUE(workspace_id,rule_id,scheduled_on,occurrence_index))""",
    """CREATE TABLE IF NOT EXISTS living_snapshots(
       workspace_id TEXT NOT NULL REFERENCES workspaces(id),day TEXT NOT NULL,
       timing TEXT NOT NULL CHECK(timing IN ('end_of_day','intraday')),
       ledger_version INTEGER NOT NULL,account_balances TEXT NOT NULL,
       cash_minor INTEGER NOT NULL,observed_at TEXT NOT NULL,
       PRIMARY KEY(workspace_id,day))""",
    """CREATE TABLE IF NOT EXISTS living_rule_controls(
       workspace_id TEXT NOT NULL REFERENCES workspaces(id),rule_id TEXT NOT NULL,
       enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),updated_at TEXT NOT NULL,
       PRIMARY KEY(workspace_id,rule_id))""",
    """CREATE TABLE IF NOT EXISTS living_schedule(
       workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),last_attempt_order INTEGER NOT NULL,
       attempted_at TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('running','ok','error')),
       error_code TEXT)""",
    "CREATE INDEX IF NOT EXISTS living_due ON living_events(workspace_id,status,scheduled_at)",
)


class LivingError(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _now(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now 必须有时区。")
        return value.astimezone(CN)
    if type(value) in (int, float) and math.isfinite(value):
        return datetime.fromtimestamp(value, CN)
    raise ValueError("now 必须是可信时间戳或含时区 datetime。")


def _binding(db, workspace):
    row = db.execute("SELECT * FROM registered_user_instances WHERE workspace_id=? AND status='ready'",
                     (workspace,)).fetchone()
    if row is None:
        raise LivingError("REGISTERED_READY_INSTANCE_REQUIRED")
    return dict(row)


def _checked_stamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        # 旧未连接演示流水的naive时间在原合同中使用北京时间。
        return parsed.replace(tzinfo=CN) if parsed.tzinfo is None else parsed.astimezone(CN)
    except (ValueError, TypeError):
        raise LivingError("LIVING_PERSISTED_TIME_INVALID") from None


def _assert_monotonic_time(db, workspace, now, state=None):
    """当前余额不能被贴到早于任一已记账/已观察时刻的快照上。无写入。"""
    if state is not None:
        for field in ("activated_at", "last_checked_at"):
            if field in state.keys() and state[field] and _checked_stamp(state[field]) > now:
                raise LivingError("LIVING_TRUSTED_CLOCK_ROLLBACK")
    for row in db.execute("SELECT observed_at FROM living_snapshots WHERE workspace_id=?", (workspace,)):
        if _checked_stamp(row[0]) > now:
            raise LivingError("LIVING_TRUSTED_CLOCK_ROLLBACK")
    # 遍历并解析，不用字符串MAX比较不同时区/旧naive格式而漏掉未来入账。
    for row in db.execute("SELECT occurred_at FROM transactions WHERE workspace_id=?", (workspace,)):
        if _checked_stamp(row[0]) > now:
            raise LivingError("LIVING_LEDGER_AFTER_TRUSTED_TIME")


def _check_binding(state, binding):
    fields = (("owner_id", "registered_user_id"), ("persona_id", "persona_id"),
              ("initialization_id", "initialization_id"), ("template_sha256", "template_sha256"),
              ("template_end_on", "template_end_on"))
    if any(state[a] != binding[b] for a, b in fields):
        raise LivingError("LIVING_INSTANCE_BINDING_CHANGED")


def calibrate_template(service, persona, template_end_on):
    """最近12个完整自然月的封存流水校准；不读取现役余额或用户操作。

    同类别对应多条规则时保留默认值并明确未覆盖，避免将同笔预算重复分配。
    末月不完整时舍弃末月；样本少于12个月则保留默认值。
    """
    end = date.fromisoformat(template_end_on).replace(day=1) - timedelta(days=1)
    month_index = end.year * 12 + end.month - 12
    start = date(month_index // 12, month_index % 12 + 1, 1)
    catalog = rule_catalog(persona)
    observations, details = {"rules": {}}, []
    coverage_start = date.fromisoformat(service.metadata["start_date"])
    if coverage_start > start:
        return observations, {"status": "default_insufficient_12_complete_months", "start_on": start.isoformat(),
                              "end_on": end.isoformat(), "dataset_version": service.dataset_version, "rules": []}
    with closing(service._connect()) as db:
        rows = [dict(r) for r in db.execute(
            "SELECT category,kind,amount_minor,booked_on FROM transactions "
            "WHERE subject_id=? AND booked_on BETWEEN ? AND ? AND kind IN ('income','expense') ORDER BY booked_on,transaction_id",
            (persona, start.isoformat(), end.isoformat()))]
    category_rules = Counter((r["category"], r["kind"]) for r in catalog)
    for rule in catalog:
        matched = [r for r in rows if r["category"] == rule["category"] and r["kind"] == rule["kind"]]
        reason = ("ambiguous_category_kept_default" if category_rules[(rule["category"], rule["kind"])] > 1
                  else "category_not_present_kept_default" if not matched else "calibrated")
        info = {"rule_id": rule["rule_id"], "source_rows": len(matched), "status": reason}
        if reason != "calibrated":
            details.append(info)
            continue
        if any(type(r["amount_minor"]) is not int or r["amount_minor"] <= 0 for r in matched):
            raise LivingError("TEMPLATE_CALIBRATION_AMOUNT_INVALID")
        if rule["rule_id"] in ("income.restaurant_sales", "business.ingredients"):
            totals = defaultdict(int)
            for row in matched:
                totals[row["booked_on"][:7]] += row["amount_minor"]
            per_open_day = []
            for month, total in totals.items():
                first = date.fromisoformat(month + "-01")
                following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
                open_days = sum((first + timedelta(days=i)).weekday() != 0 for i in range((following - first).days))
                per_open_day.append(max(1, total // open_days))
            observations["rules"][rule["rule_id"]] = {"amount_range_minor": [min(per_open_day), max(per_open_day)]}
            info["cadence_basis"] = "source_month_total_distributed_over_tuesday_to_sunday_business_days"
        elif rule["cadence"] == "monthly":
            totals = defaultdict(int)
            for row in matched:
                totals[row["booked_on"][:7]] += row["amount_minor"]
            # 只取有发生的月份；农季不可把全年合计平均成每月收入。
            values = sorted(totals.values())
            override = ({"amount_range_minor": [min(values), max(values)]} if len(values) <= 4 else
                        {"amount_minor": int(median_low(values))})
            seen_months = sorted({int(r["booked_on"][5:7]) for r in matched})
            if len(rule["months"]) < 12 or len(values) <= 4:
                override["months"] = seen_months
            if rule["kind"] == "income":
                override["day_of_month"] = int(Counter(r["booked_on"][8:10] for r in matched).most_common(1)[0][0])
            observations["rules"][rule["rule_id"]] = override
            info.update(observed_months=seen_months, amount_basis="active_month_min_max" if len(values) <= 4 else "active_month_median")
        else:
            amounts = sorted(r["amount_minor"] for r in matched)
            counts = Counter(r["booked_on"][:7] for r in matched)
            values = [counts[f"{(month_index+n)//12:04}-{(month_index+n)%12+1:02}"] for n in range(12)]
            target = int(median_low(sorted(values)))
            # 28天的最短月与最多每周一天休息的保守容量；规则仍作精确容量检查。
            cap = 20 * rule["occurrences_per_day"] if rule["weekend_probability_bps"] == 0 else 24 * rule["occurrences_per_day"]
            applied_count = min(target, cap)
            frequency = ({"monthly_occurrences": applied_count} if target else
                         {"probability_bps": min(10000, max(1, len(matched) * 10000 // 365 // rule["occurrences_per_day"]))})
            observations["rules"][rule["rule_id"]] = {
                "amount_range_minor": [amounts[len(amounts) // 4], amounts[(len(amounts) * 3) // 4]], **frequency}
            info.update(observed_monthly_median_count=target, applied_monthly_count=applied_count,
                        capacity_clamped=applied_count != target, sparse_frequency_mode="probability" if not target else "fixed_monthly")
        details.append(info)
    mapped = {(r["category"], r["kind"]) for r in catalog}
    uncovered = defaultdict(lambda: {"count": 0, "sum_minor": 0})
    for row in rows:
        key = (row["category"], row["kind"])
        if key not in mapped:
            uncovered[key]["count"] += 1
            uncovered[key]["sum_minor"] += row["amount_minor"]
    return observations, {"status": "calibrated_with_explicit_gaps", "start_on": start.isoformat(),
                          "end_on": end.isoformat(), "dataset_version": service.dataset_version, "rules": details,
                          "source_totals_minor": {kind: sum(r["amount_minor"] for r in rows if r["kind"] == kind)
                                                  for kind in ("income", "expense")},
                          "unmapped_source_categories": [{"category": k[0], "kind": k[1], **v} for k, v in sorted(uncovered.items())]}


class BankAdapter:
    """只操作调用方 bank 的当前事务；Base 可替换账户映射和策略，不能另开写连接。"""

    def resolve_account(self, db, workspace, event):
        role = event["account_role"]
        suffix = {"personal_daily": "daily", "salary": "daily", "business": "operating", "wallet": "wallet"}.get(role)
        suffix = {"income.business_distribution":"broker", "income.machinery_services":"machinery",
                  "income.restaurant_sales":"shop_wallet"}.get(event["rule_id"],suffix)
        if role == "rent":
            suffix = {"income.rent_one": "rent1", "income.rent_two": "rent2", "income.rent_three": "rent3"}.get(event["rule_id"], "rent")
        links = [dict(r) for r in db.execute(
            "SELECT l.*,a.currency FROM connected_account_links l JOIN accounts a ON a.workspace_id=l.workspace_id AND a.id=l.account_id "
            "WHERE l.workspace_id=?", (workspace,))]
        matches = [r for r in links if r["source_account_id"].rsplit(":", 1)[-1] == suffix
                   and r["currency"] == "CNY" and r["account_type"] != "credit_card"]
        if role == "rent" and suffix == "rent" and not matches:
            # 多套物业的共同费用路由固定到rent1；不因余额变化换账户，不搬动资金。
            matches = [r for r in links if r["source_account_id"].endswith(":rent1") and r["currency"] == "CNY"]
        if len(matches) != 1:
            return None
        return matches[0]

    def process_authorized(self, bank, db, workspace, now):
        # 原授权队列先于新增生活支出；冻结资金不因补记收入而自动挪作别用。
        locked=db.execute('SELECT locked FROM permission_security WHERE workspace_id=?',(workspace,)).fetchone()
        def pending_due():
            if any(db.execute(sql,(workspace,now.date().isoformat())).fetchone() for sql in (
                "SELECT 1 FROM orders WHERE workspace_id=? AND status='pending_settlement' AND settlement_date<=? LIMIT 1",
                "SELECT 1 FROM card_applications WHERE workspace_id=? AND status='submitted' AND requested_on<? LIMIT 1",
                "SELECT 1 FROM card_limit_requests WHERE workspace_id=? AND status='submitted' AND requested_on<? LIMIT 1",
                "SELECT 1 FROM fund_holds WHERE workspace_id=? AND status='held' AND expires_on<? LIMIT 1")):return True
            return bool(db.execute('''SELECT 1 FROM scheduled_payments s LEFT JOIN navren_remittances r
                ON r.workspace_id=s.workspace_id AND r.scheduled_payment_id=s.id
                WHERE s.workspace_id=? AND s.status='active'
                AND ((r.operation_id IS NULL AND s.next_due_on<=?) OR (r.status='scheduled' AND r.execute_at<=?)) LIMIT 1''',
                (workspace,now.date().isoformat(),now.timestamp())).fetchone())
        if locked and locked[0]:
            return {'_pending':pending_due()}
        eligible=[r[0] for r in db.execute('''SELECT s.id FROM scheduled_payments s
            LEFT JOIN navren_remittances r ON r.workspace_id=s.workspace_id AND r.scheduled_payment_id=s.id
            WHERE s.workspace_id=? AND s.status='active'
            AND ((r.operation_id IS NULL AND s.next_due_on<=?) OR (r.status='scheduled' AND r.execute_at<=?))
            ORDER BY s.next_due_on,s.id LIMIT 200''',
            (workspace,now.date().isoformat(),now.timestamp()))]
        result=bank._process_authorized_day(db,workspace,now.date(),schedule_ids=eligible,limit=200)
        result['_pending']=pending_due()
        return result

    def denial(self, db, workspace, event, link):
        for required in ("permission_security", "account_details", "fund_holds", "navren_remittances"):
            if not _table(db, required):
                raise LivingError("LIVING_POLICY_SCHEMA_MISSING:" + required)
        security = db.execute("SELECT locked FROM permission_security WHERE workspace_id=?", (workspace,)).fetchone()
        if security and security["locked"]:
            return "financial_write_locked"
        control = db.execute("SELECT enabled FROM living_rule_controls WHERE workspace_id=? AND rule_id=?",
                             (workspace, event["rule_id"])).fetchone()
        if control and not control["enabled"]:
            return "user_disabled_rule"
        if link is None:
            return "purpose_account_unavailable"
        detail = db.execute("SELECT status FROM account_details WHERE workspace_id=? AND account_id=?",
                            (workspace, link["account_id"])).fetchall()
        if not detail or any(r["status"] != "active" for r in detail):
            return "account_not_active"
        if _table(db, "navren_account_connections"):
            connection = db.execute("SELECT status FROM navren_account_connections WHERE workspace_id=? AND connection_id=?",
                                    (workspace, link["connection_id"])).fetchone()
            if connection and connection["status"] != "bound":
                return "account_connection_inactive"
        if event["kind"] == "expense":
            if _table(db, "subscriptions") and not _table(db, "merchants"):
                raise LivingError("LIVING_POLICY_SCHEMA_MISSING:merchants")
            if _table(db, "mandates") and db.execute(
                "SELECT 1 FROM mandates WHERE workspace_id=? AND account_id=? AND merchant=? AND status='closed'",
                (workspace, link["account_id"], event["merchant"])).fetchone():
                return "user_closed_merchant_mandate"
            # 契约不允许planner重演订阅。若模板适配将生活规则绑定已有订阅，取消态也拒扣。
            if _table(db, "subscriptions") and _table(db, "merchants") and db.execute(
                "SELECT 1 FROM subscriptions s JOIN merchants m ON m.workspace_id=s.workspace_id AND m.id=s.merchant_id "
                "WHERE s.workspace_id=? AND s.account_id=? AND m.name=? AND s.merchant_status<>'active'",
                (workspace, link["account_id"], event["merchant"])).fetchone():
                return "user_cancelled_merchant_subscription"
        return None

    def held_minor(self, db, workspace, account):
        return db.execute("SELECT coalesce(sum(amount_minor),0) FROM fund_holds WHERE workspace_id=? AND account_id=? AND status='held'",
                          (workspace, account)).fetchone()[0] + db.execute(
            "SELECT coalesce(sum(amount_minor),0) FROM navren_remittances WHERE workspace_id=? AND account_id=? AND status='scheduled'",
            (workspace, account)).fetchone()[0]

    def balances(self, db, workspace, *, day=None):
        result = {}
        for account in db.execute("SELECT * FROM accounts WHERE workspace_id=? ORDER BY id", (workspace,)):
            query = "SELECT coalesce(sum(delta_minor),0) FROM transactions WHERE workspace_id=? AND account_id=?"
            values = [workspace, account["id"]]
            if day is not None:
                query += " AND substr(occurred_at,1,10)<=?"
                values.append(day)
            value = account["opening_balance_minor"] + db.execute(query, values).fetchone()[0]
            if day is None and value != account["balance_minor"]:
                raise LivingError("LIVING_CASH_REPLAY_MISMATCH")
            if value < 0:
                raise LivingError("LIVING_HISTORICAL_NEGATIVE_CASH")
            result[account["id"]] = value
        return result


class LivingWorker:
    def __init__(self, bank, *, template_service_for, adapter=None, planner=plan_day,
                 rules_version=RULES_VERSION, failpoint=None, funding_enabled=False):
        self.bank = bank
        self.template_service_for = template_service_for
        self.adapter = adapter or BankAdapter()
        self.planner, self.rules_version, self.failpoint = planner, rules_version, failpoint
        from .living_funding import FundingService
        self.funding=FundingService(bank,template_service_for,self.adapter) if funding_enabled else None

    def initialize(self):
        """必须由owner显式迁移；构造及导入不创建表。"""
        with self.bank.transaction() as db:
            for statement in SCHEMA:
                db.execute(statement)
            columns = {r[1] for r in db.execute("PRAGMA table_info(living_states)")}
            if "last_checked_at" not in columns:
                db.execute("ALTER TABLE living_states ADD COLUMN last_checked_at TEXT")
            if self.funding:self.funding.initialize(db)

    def _ensure(self, workspace, now):
        with closing(self.bank.connect()) as db:
            binding = _binding(db, workspace)
            state = db.execute("SELECT * FROM living_states WHERE workspace_id=?", (workspace,)).fetchone()
            _assert_monotonic_time(db, workspace, now, state)
        if state:
            _check_binding(state, binding)
            if state["rules_version"] != self.rules_version:
                raise LivingError("LIVING_RULE_UPGRADE_REQUIRES_EXPLICIT_MIGRATION")
            return
        service = self.template_service_for(workspace)
        if service.metadata.get("end_date") != binding["template_end_on"]:
            raise LivingError("LIVING_TEMPLATE_END_MISMATCH")
        observations, receipt = calibrate_template(service, binding["persona_id"], binding["template_end_on"])
        # 在任何状态落盘前验证配置能被规则接受。
        self.planner(binding["persona_id"], binding["template_end_on"], seed_namespace=workspace, observations=observations)
        with self.bank.transaction() as db:
            current = _binding(db, workspace)
            if current != binding:
                raise LivingError("LIVING_BINDING_CHANGED_DURING_CALIBRATION")
            existing = db.execute("SELECT * FROM living_states WHERE workspace_id=?", (workspace,)).fetchone()
            _assert_monotonic_time(db, workspace, now, existing)
            if existing:
                _check_binding(existing, binding)
                return
            db.execute("INSERT INTO living_states(workspace_id,owner_id,persona_id,initialization_id,template_sha256,template_end_on,"
                       "closed_through,rules_version,observations,calibration,activated_at,last_run_order,last_checked_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)", (
                workspace, binding["registered_user_id"], binding["persona_id"], binding["initialization_id"],
                binding["template_sha256"], binding["template_end_on"], binding["template_end_on"],
                self.rules_version, _json(observations), _json(receipt), now.isoformat(), now.isoformat()))

    def _snapshot(self, db, workspace, day, today, now, version):
        balances = self.adapter.balances(db, workspace, day=day if day < today else None)
        timing = "end_of_day" if day < today else "intraday"
        db.execute("INSERT INTO living_snapshots VALUES(?,?,?,?,?,?,?) ON CONFLICT(workspace_id,day) DO UPDATE SET "
                   "timing=excluded.timing,ledger_version=excluded.ledger_version,account_balances=excluded.account_balances,"
                   "cash_minor=excluded.cash_minor,observed_at=excluded.observed_at",
                   (workspace, day, timing, version, _json(balances), sum(balances.values()), now.isoformat()))

    def run_batch(self, workspace, *, now=None, max_days=7, max_events=200):
        if type(max_days) is not int or not 1 <= max_days <= 31:
            raise ValueError("max_days 需要1至31。")
        if type(max_events) is not int or not 1 <= max_events <= 2000:
            raise ValueError("max_events 需要1至2000。")
        now = _now(self.bank.clock() if now is None else now)
        today = now.date().isoformat()
        self._ensure(workspace, now)
        if self.funding:self.funding.ensure(workspace,now)
        result = {"workspace_id": workspace, "processed_days": 0, "posted": 0, "failed": 0,
                  "cancelled": 0, "events_processed": 0, "worker_version": WORKER_VERSION, "caught_up": False}
        for _ in range(max_days):
            with self.bank.transaction() as db:
                binding = _binding(db, workspace)
                state = db.execute("SELECT * FROM living_states WHERE workspace_id=?", (workspace,)).fetchone()
                _check_binding(state, binding)
                _assert_monotonic_time(db, workspace, now, state)
                if state["rules_version"] != self.rules_version:
                    raise LivingError("LIVING_RULE_UPGRADE_REQUIRES_EXPLICIT_MIGRATION")
                on = (date.fromisoformat(state["closed_through"]) + timedelta(days=1)).isoformat()
                if on > today:
                    db.execute("UPDATE living_states SET last_checked_at=? WHERE workspace_id=?", (now.isoformat(), workspace))
                    result.update(closed_through=state["closed_through"], caught_up=True)
                    return result
                authorized=self.adapter.process_authorized(self.bank,db,workspace,now)
                authorized_pending=authorized.pop('_pending',False)
                old_as_of=db.execute('SELECT as_of FROM workspaces WHERE id=?',(workspace,)).fetchone()[0]
                if old_as_of>today:
                    raise LivingError('LIVING_WORKSPACE_AFTER_TRUSTED_TIME')
                db.execute('UPDATE workspaces SET as_of=? WHERE id=?',(today,workspace))
                events = self.planner(binding["persona_id"], on, seed_namespace=workspace,
                                      observations=json.loads(state["observations"]))
                for event in events:
                    if (type(event) is not dict or set(event) != EVENT_FIELDS
                            or type(event.get("amount_minor")) is not int or not 0 < event["amount_minor"] <= 2**53-1
                            or type(event.get("occurrence_index")) is not int or event["occurrence_index"] < 0
                            or event.get("account_role") not in ACCOUNT_ROLES
                            or not isinstance(event.get("rule_id"), str)
                            or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,96}", event["rule_id"])
                            or any(not isinstance(event.get(k), str) or not 1 <= len(event[k]) <= 120 for k in ("category", "merchant"))
                            or not isinstance(event.get("scheduled_at"), str)
                            or event["scheduled_at"][:10] != on or not event["scheduled_at"].endswith("+08:00")
                            or event["kind"] not in ("income", "expense")):
                        raise LivingError("LIVING_PLAN_CONTRACT_INVALID")
                    try:
                        if datetime.fromisoformat(event["scheduled_at"]).date().isoformat() != on:
                            raise ValueError
                    except ValueError:
                        raise LivingError("LIVING_PLAN_CONTRACT_INVALID") from None
                    material = [workspace, event["rule_id"], on, event["occurrence_index"]]
                    ident = "living-" + hashlib.sha256(_json(material).encode()).hexdigest()
                    db.execute("INSERT OR IGNORE INTO living_events(event_id,workspace_id,rule_id,scheduled_on,occurrence_index,"
                               "scheduled_at,plan,rules_version,status) VALUES(?,?,?,?,?,?,?,?,'planned')", (
                                   ident, workspace, event["rule_id"], on, event["occurrence_index"],
                                   event["scheduled_at"], _json(event), self.rules_version))
                    registered = db.execute("SELECT plan FROM living_events WHERE event_id=?", (ident,)).fetchone()
                    if registered["plan"] != _json(event):
                        raise LivingError("LIVING_PLANNED_EVENT_CHANGED")
                self.adapter.balances(db, workspace)  # 先证明已有用户账本完整，不能把旧错埋进新快照。
                capacity = max_events - result["events_processed"]
                pending = db.execute("SELECT * FROM living_events WHERE workspace_id=? AND scheduled_on=? AND status='planned' "
                                     "AND scheduled_at<=? ORDER BY scheduled_at,event_id LIMIT ?",
                                     (workspace, on, now.isoformat(), capacity)).fetchall()
                day_counts = Counter()
                for registered in pending:
                    event = json.loads(registered["plan"])
                    link = self.adapter.resolve_account(db, workspace, event)
                    reason = self.adapter.denial(db, workspace, event, link)
                    before = after = None
                    transaction = None
                    if not reason:
                        account = db.execute("SELECT * FROM accounts WHERE workspace_id=? AND id=?",
                                             (workspace, link["account_id"])).fetchone()
                        before = after = account["balance_minor"]
                        if event["kind"] == "expense" and before - self.adapter.held_minor(db, workspace, account["id"]) < event["amount_minor"]:
                            reason = self.funding.cover(db,binding,event,link,now,registered['event_id']) if self.funding else "insufficient_available_funds"
                            if not reason:
                                account=db.execute('SELECT * FROM accounts WHERE workspace_id=? AND id=?',(workspace,link['account_id'])).fetchone()
                                before=after=account['balance_minor']
                                if before-self.adapter.held_minor(db,workspace,account['id'])<event['amount_minor']:
                                    raise LivingError('LIVING_FUNDING_REPLAY_MISMATCH')
                    if not reason:
                        delta = event["amount_minor"] * (1 if event["kind"] == "income" else -1)
                        after = before + delta
                        transaction = registered["event_id"]
                        db.execute("UPDATE accounts SET balance_minor=? WHERE workspace_id=? AND id=?", (after, workspace, account["id"]))
                        db.execute("INSERT INTO transactions(id,workspace_id,account_id,occurred_at,merchant,amount_minor,delta_minor,"
                                   "kind,category,note,mandate_id,operation_id) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)", (
                                       transaction, workspace, account["id"], now.isoformat(), event["merchant"],
                                       event["amount_minor"], delta, event["kind"], event["category"],
                                       _json({"origin": "living_simulation", "scheduled_at": event["scheduled_at"],
                                              "posted_at": now.isoformat(), "rules_version": registered["rules_version"]})))
                    status = "posted" if not reason else "cancelled" if reason.startswith("user_") else "failed"
                    db.execute("UPDATE living_events SET status=?,reason=?,transaction_id=?,posted_at=?,balance_before_minor=?,"
                               "balance_after_minor=? WHERE event_id=? AND status='planned'", (
                                   status, reason, transaction, now.isoformat() if transaction else None, before, after, registered["event_id"]))
                    day_counts[status] += 1
                if self.failpoint:
                    self.failpoint("after_postings_before_snapshots", db)
                unfinished = db.execute("SELECT count(*) FROM living_events WHERE workspace_id=? AND scheduled_on=? AND status='planned'",
                                        (workspace, on)).fetchone()[0]
                closed = on < today and not unfinished
                if closed:
                    db.execute("UPDATE living_states SET closed_through=? WHERE workspace_id=?", (on, workspace))
                changed = bool(pending) or closed or any(authorized.values()) or old_as_of!=today
                if changed:
                    self.bank._bump(db, workspace)
                version = db.execute("SELECT version FROM workspaces WHERE id=?", (workspace,)).fetchone()[0]
                db.execute("UPDATE living_states SET last_run_order=(SELECT coalesce(max(last_run_order),0)+1 FROM living_states),"
                           "last_checked_at=? WHERE workspace_id=?", (now.isoformat(), workspace))
                self.adapter.balances(db, workspace)
                self._snapshot(db, workspace, on, today, now, version)
                if on != today:
                    self._snapshot(db, workspace, today, today, now, version)
                if changed:
                    self.bank._audit(db, workspace, "living_batch", {"scheduled_on": on, "closed": closed,
                                     "counts": dict(day_counts), "ledger_version": version})
                result["processed_days"] += 1
                for key in ("posted", "failed", "cancelled"):
                    result[key] += day_counts[key]
                result["events_processed"] += len(pending)
                result.update(closed_through=on if closed else state["closed_through"],
                              authorized_pending=authorized_pending,
                              caught_up=on == today and not authorized_pending and not db.execute(
                                  "SELECT 1 FROM living_events WHERE workspace_id=? AND status='planned' AND scheduled_at<=? LIMIT 1",
                                  (workspace, now.isoformat())).fetchone())
                if self.failpoint:
                    self.failpoint("before_commit", db)
                if not closed or result["events_processed"] >= max_events:
                    return result
        return result

    def read_snapshots(self, workspace, *, start_on=None, end_on=None):
        """只刷新同账本版本投影，不在GET中生成事件或推进授权队列。"""
        now=_now(self.bank.clock());today=now.date().isoformat()
        end_on=today if end_on is None else end_on
        start_on=end_on if start_on is None else start_on
        try:
            start,end=date.fromisoformat(start_on),date.fromisoformat(end_on)
            if start.isoformat()!=start_on or end.isoformat()!=end_on or not 0<=(end-start).days<=365 or end_on>today:
                raise ValueError
        except (TypeError,ValueError):
            from .service import BankError
            raise BankError('INVALID_SNAPSHOT_RANGE','快照范围须为当前日期以内、最多366日。',400) from None
        with self.bank.transaction() as db:
            binding=_binding(db,workspace)
            state=db.execute('SELECT * FROM living_states WHERE workspace_id=?',(workspace,)).fetchone()
            if state is None:
                from .service import BankError
                raise BankError('LIVING_NOT_STARTED','本人的持续模拟尚待后台初始化。',409)
            _check_binding(state,binding);_assert_monotonic_time(db,workspace,now,state)
            version=db.execute('SELECT version FROM workspaces WHERE id=?',(workspace,)).fetchone()[0]
            self.adapter.balances(db,workspace)
            rows=db.execute('SELECT * FROM living_snapshots WHERE workspace_id=? AND day BETWEEN ? AND ? ORDER BY day',
                            (workspace,start_on,end_on)).fetchall()
            for row in rows:
                if row['ledger_version']!=version:
                    self._snapshot(db,workspace,row['day'],today,now,version)
            rows=db.execute('SELECT * FROM living_snapshots WHERE workspace_id=? AND day BETWEEN ? AND ? ORDER BY day',
                            (workspace,start_on,end_on)).fetchall()
            return {'contract':'navren.living-snapshots/1','environment':'simulation','data_scope':'navren_synthetic',
                'workspace_binding':workspace,'ledger_version':version,'closed_through':state['closed_through'],
                'template_end_on':state['template_end_on'],'start_on':start_on,'end_on':end_on,
                'catch_up_pending':state['closed_through']<(now.date()-timedelta(days=1)).isoformat(),
                'cash_basis':'actual_posting_day',
                'snapshots':[{**dict(r),'account_balances':json.loads(r['account_balances'])} for r in rows]}

    @contextmanager
    def snapshot_send_guard(self, workspace, version):
        with self.bank.transaction() as db:
            _binding(db,workspace)
            current=db.execute('SELECT version FROM workspaces WHERE id=?',(workspace,)).fetchone()[0]
            if type(version) is not int or current!=version:
                from .service import BankError
                raise BankError('LIVING_LEDGER_CHANGED','账本已变化，请重新读取同版本快照。',409)
            self.adapter.balances(db,workspace)
            yield

    def run_ready_instances(self, *, now=None, limit=20, max_days=7, max_events=200):
        """返回list[{workspace_id,status,result,error}]；每实例异常明确记录且继续。

        status=ok只是该实例有限批次成功，是否追平仍看result.caught_up。
        error时result为None，调用方应让任务失败/告警，不能把部分成功当全成功。
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit 需要1至100。")
        if type(max_days) is not int or not 1 <= max_days <= 31:
            raise ValueError("max_days 需要1至31。")
        if type(max_events) is not int or not 1 <= max_events <= 2000:
            raise ValueError("max_events 需要1至2000。")
        now = _now(self.bank.clock() if now is None else now)
        with closing(self.bank.connect()) as db:
            workspaces = [r[0] for r in db.execute("SELECT i.workspace_id FROM registered_user_instances i "
                "LEFT JOIN living_states s ON s.workspace_id=i.workspace_id "
                "LEFT JOIN living_schedule q ON q.workspace_id=i.workspace_id WHERE i.status='ready' "
                "ORDER BY coalesce(q.last_attempt_order,0),coalesce(s.closed_through,i.template_end_on),i.workspace_id LIMIT ?", (limit,))]
        outcomes = []
        for workspace in workspaces:
            # 独立调度元数据先保留轮转位置；坏模板还没有living_state也可让出下一轮。
            with self.bank.transaction() as db:
                order = db.execute("SELECT coalesce(max(last_attempt_order),0)+1 FROM living_schedule").fetchone()[0]
                db.execute("INSERT INTO living_schedule VALUES(?,?,?,'running',NULL) ON CONFLICT(workspace_id) DO UPDATE SET "
                           "last_attempt_order=excluded.last_attempt_order,attempted_at=excluded.attempted_at,status='running',error_code=NULL",
                           (workspace, order, now.isoformat()))
            try:
                result = self.run_batch(workspace, now=now, max_days=max_days, max_events=max_events)
                outcome = {"workspace_id": workspace, "status": "ok", "result": result, "error": None}
            except Exception as error:
                code = str(error) if isinstance(error, LivingError) and re.fullmatch(r"[A-Z][A-Z0-9_:]*", str(error)) else "LIVING_INSTANCE_EXECUTION_FAILED"
                outcome = {"workspace_id": workspace, "status": "error", "result": None,
                           "error": {"code": code, "type": type(error).__name__}}
            with self.bank.transaction() as db:
                db.execute("UPDATE living_schedule SET status=?,error_code=? WHERE workspace_id=? AND last_attempt_order=?", (
                    outcome["status"], outcome["error"]["code"] if outcome["error"] else None, workspace, order))
            outcomes.append(outcome)
        return outcomes


class LivingScheduler:
    """应用生命周期内后台补跑；构造不启动线程，部分失败单独计数。"""
    def __init__(self, worker, *, interval=30):
        if type(interval) not in (int,float) or not math.isfinite(interval) or not 1<=interval<=3600:
            raise ValueError('invalid_living_interval')
        self.worker,self.interval=worker,interval
        self._stop=threading.Event();self._lock=threading.Lock();self._thread=None
        self.last_status={'status':'not_started'}

    def tick(self):
        outcomes=self.worker.run_ready_instances(limit=20,max_days=7,max_events=200)
        errors=sum(r['status']=='error' for r in outcomes)
        value={'status':'partial_failure' if errors else 'ok','instances':len(outcomes),'errors':errors,
               'caught_up':sum(r['status']=='ok' and r['result']['caught_up'] for r in outcomes)}
        with self._lock:self.last_status=value
        if errors:logging.getLogger(__name__).error('living_batch_partial_failure instances=%s errors=%s',len(outcomes),errors)
        return outcomes

    def _run(self):
        while not self._stop.is_set():
            try:self.tick()
            except Exception as error:
                with self._lock:self.last_status={'status':'error','error_type':type(error).__name__}
                logging.getLogger(__name__).error('living_scheduler_failed type=%s',type(error).__name__)
            self._stop.wait(self.interval)

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():return False
            self._stop.clear()
            self._thread=threading.Thread(target=self._run,name='navren-living-simulation',daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout=2):
        self._stop.set()
        with self._lock:thread=self._thread
        if thread is not None:thread.join(timeout)
        return thread is None or not thread.is_alive()
