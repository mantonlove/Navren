#!/usr/bin/env python3
"""独立冻结预期与必要条件检查；自动检查绝不等同金融语义通过。

接口：build_fixture(case, temp_root) -> (gateway, workspace, token)
      preflight(gateway, workspace, case) -> {status, checks, ...}
      snapshot(gateway, workspace) -> 可公开的合成状态
      classify(record, case) -> 必要条件结果及逐条人工待审表

python3 -B tests/window_integration/semantic_oracle.py --self-test
仅本地合成夹具，不调用云模型。已有 runner 只读复用，并以源 SHA 固定。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
from copy import deepcopy
from datetime import date
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import FunctionType, MethodType

ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = ROOT / "evaluation/window_integration/semantic_cases.json"
_RUNNER = None
_FIXTURE_LOCK = threading.RLock()
_MISSING = object()


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def load_dataset():
    data = json.loads(DATASET_PATH.read_text())
    origin = data["integration_origin"]
    for section, expected in origin["sections_sha256"].items():
        actual = hashlib.sha256(_packed(data[section]).encode()).hexdigest()
        if actual != expected:
            raise ValueError("frozen_section_changed:" + section)
    original = ROOT / origin["path"]
    if original.is_file() and _digest(original) != origin["sha256"]:
        raise ValueError("original_dataset_changed")
    if len(data["cases"]) != 10:
        raise ValueError("frozen_case_count_changed")
    return data


def _case(case):
    data = load_dataset()
    selected = next((c for c in data["cases"] if c["id"] == case["id"]), None)
    if selected is None or selected != case:
        raise ValueError("case_differs_from_frozen_original")
    return data, selected


def _runner():
    global _RUNNER
    data = load_dataset()
    path = ROOT / data["integration_origin"]["runner_path"]
    if _digest(path) != data["integration_origin"]["runner_sha256"]:
        raise ValueError("fixture_adapter_source_changed")
    if _RUNNER is None:
        adapter_path = ROOT / 'tests/window_integration/semantic_runner_adapter.py'
        spec = importlib.util.spec_from_file_location("window_frozen_skill_runner_adapter", adapter_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RUNNER = module.load_runner()
    # 不允许旧 import 的模块绑定到已变化的磁盘源码。
    current = _RUNNER.source_manifest()
    if current != _RUNNER._IMPORT_SOURCE_MANIFEST:
        changed = sorted(k for k in current.keys() | _RUNNER._IMPORT_SOURCE_MANIFEST.keys()
                         if current.get(k) != _RUNNER._IMPORT_SOURCE_MANIFEST.get(k))
        raise ValueError("production_source_changed_since_import:" + ",".join(changed[:5]))
    return _RUNNER


@contextmanager
def _isolated_environment(root):
    from cryptography.fernet import Fernet
    values = {
        "NAVREN_PERSONAL_DATA_KEY": Fernet.generate_key().decode("ascii"),
        "NAVREN_KNOWLEDGE_DB": str(root / "knowledge.sqlite3"),
        "NAVREN_MARKET_DB": str(root / "market.sqlite3"),
        "NAVREN_FUND_CACHE": str(root / "funds"),
    }
    prior = {key: os.environ.get(key, _MISSING) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is _MISSING:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class _NoNetworkCatalog:
    """十题未给定其他产品原文；不访问现役目录或网络。"""
    def read(self, query="", limit=12, offset=0):
        return {"provider": "window-integration-offline-fixture", "source": "synthetic_unavailable",
                "status": "unavailable", "fetch_status": "fixture", "products": [],
                "verified_nav_products": [], "source_date": None, "stale": True,
                "current_sale_status": None, "executable": False,
                "notice": "独立离线夹具未提供此公开目录，不代表生产源状态。"}


def _bind_history_cache(g, cache):
    """只替换每个实例的缓存路径；沿用原方法代码，不改共享模块全局。"""
    import fund_history
    from wealth_portfolio import WealthPortfolio
    for name in ("provision", "products", "migrate"):
        original = getattr(WealthPortfolio, name)
        namespace = dict(original.__globals__)
        namespace["cached"] = lambda code: fund_history.cached(code, cache=cache)
        bound = FunctionType(original.__code__, namespace, original.__name__,
                             original.__defaults__, original.__closure__)
        bound.__kwdefaults__ = original.__kwdefaults__
        setattr(g.wealth, name, MethodType(bound, g.wealth))


def build_fixture(case, temp_root):
    """创建一个新合成账户；调用方持有并最终清理 TemporaryDirectory。

    temp_root 必须是系统临时目录下的空目录。不得传现役项目/数据库目录。
    只在构造时临时设置密钥；返回后 cipher 已持有临时密钥，环境恢复。
    token 是仅内存会话令牌，调用方不得写入报告或日志。
    """
    data, case = _case(case)
    root = Path(temp_root).resolve()
    system_temp = Path(tempfile.gettempdir()).resolve()
    if not root.is_relative_to(system_temp) or root == system_temp:
        raise ValueError("fixture_root_not_under_system_temporary_directory")
    if not root.is_dir() or any(root.iterdir()):
        raise ValueError("fixture_root_must_be_new_empty_directory")
    with _FIXTURE_LOCK, _isolated_environment(root):
        runner = _runner()
        from finance_gateway import FinanceGateway
        from market_content import MarketStore
        g = FinanceGateway(database=str(root / "bank.sqlite3"))
        g.market = MarketStore(path=root / "market.sqlite3")
        cache = root / "funds"
        cache.mkdir()
        _bind_history_cache(g, cache)
        paths = runner.assert_isolated_storage(g, root)
        paths["fund_cache"] = str(cache)
        # 检查知识库实际路径后，再注入只读提供器替身。
        original_catalog = _NoNetworkCatalog()
        w, token = g.create_session()
        fixture = data["fixtures"][case["fixture"]]
        runner.configure(g, w, fixture, original_catalog)
        if fixture['type'] == 'public_policy':
            paths.update(runner.assert_isolated_storage(g, root))
        g._window_integration = {
            "environment": "isolated_synthetic_fixture", "case_id": case["id"],
            "dataset_sha256": _digest(DATASET_PATH), "fixture_type": fixture["type"],
            "database_paths": paths, "business_day": "2026-10-03",
            "production_source_sha256": deepcopy(runner._IMPORT_SOURCE_MANIFEST),
            "fund_cache_policy": "empty_independent_cache_no_live_or_user_history",
            "network_policy": "no_network_during_fixture_or_preflight",
        }
        return g, w, token


def _check(label, actual, expected, *, category="frozen_fact", **extra):
    return {"label": label, "category": category, "actual": actual, "expected": expected,
            "passed": type(actual) is type(expected) and actual == expected, **extra}


def _independent_checks(g, w, case):
    """标准库整数复算；不调用既有 calculate/budget 求解函数。"""
    archive = g.bank.customer_archive(w)
    records = archive["records"]
    main = next(a for a in archive["accounts"] if a["id"].endswith("-main"))
    checks = []
    if case["id"].startswith(("S01", "S03", "S04", "S05", "S09", "S10")):
        floor = archive["profile"]["reserve_minor"]
        held = sum(h["amount_minor"] for h in records["fund_holds"]
                   if h["account_id"] == main["id"] and h["status"] == "held")
        # 原冻结夹具的30日已知义务，独立原始对象确认；未确认草案不扣。
        loans = {r["id"]: r for r in records["loans"]}
        cards = {r["id"]: r for r in records["cards"]}
        from datetime import timedelta
        end = (date.fromisoformat(archive["as_of"]) + timedelta(days=30)).isoformat()
        due = sum(max(0, r["principal_minor"] + r["interest_minor"] + r["fees_minor"] - r["paid_minor"])
                  for r in records["loan_installments"] if r["status"] in ("scheduled", "overdue")
                  and r["due_on"] <= end and loans[r["loan_id"]]["repayment_account_id"] == main["id"])
        due += sum(max(0, r["total_due_minor"] - r["paid_minor"])
                   for r in records["credit_statements"] if r["status"] == "open"
                   and r["due_on"] <= end and cards[r["card_id"]]["account_id"] == main["id"])
        linked = {r["mandate_id"] for r in records["subscriptions"]}
        due += sum(r["amount_minor"] for r in records["subscriptions"]
                   if r["account_id"] == main["id"] and r["merchant_status"] == "active"
                   and r["next_renewal_on"] <= end)
        due += sum(r["amount_minor"] for r in records["mandates"] if r["account_id"] == main["id"]
                   and r["id"] not in linked and r["status"] == "active" and r["next_charge_date"] <= end)
        due += sum(r["amount_minor"] for r in records["scheduled_payments"]
                   if r["account_id"] == main["id"] and r["status"] == "active" and r["next_due_on"] <= end)
        checks.extend([_check("独立30日义务合计", due, 261400, category="independent_integer_formula"),
                       _check("独立预算现金减底线/已生效留款/义务", max(0, main["balance_minor"] - floor - held - due),
                              1027600, category="independent_integer_formula")])
    if case["id"].startswith(("S01", "S03")):
        timeline = g.cashflow_timeline(w, 60)
        # 事件生成仍来自被测后端；这里只独立检验聚合，固定原题数值同时阻断生成漂移。
        balance = candidate_balance = main["balance_minor"]
        floor = archive["profile"]["reserve_minor"]
        held = sum(h["amount_minor"] for h in records["fund_holds"]
                   if h["account_id"] == main["id"] and h["status"] == "held")
        confirmed_min = candidate_min = balance - floor - held
        seen = set()
        for day in timeline["timeline"]:
            events = [e for e in day["events"] if e["account_id"] == main["id"]]
            for e in events:
                if e["id"] in seen:
                    raise ValueError("duplicate_cashflow_event")
                seen.add(e["id"])
                if e["certainty"] == "confirmed":
                    balance += e["cash_delta_minor"]
                    candidate_balance += e["cash_delta_minor"]
                    held += e["reserved_delta_minor"]
                else:
                    candidate_balance += e["cash_delta_minor"]
            confirmed_min = min(confirmed_min, balance - held - floor)
            candidate_min = min(candidate_min, candidate_balance - held - floor)
        checks.extend([_check("60日事件独立聚合已确认最低余量", confirmed_min, 984400, category="independent_event_aggregation"),
                       _check("60日事件独立聚合候选最低余量", candidate_min, 966400, category="independent_event_aggregation")])
    if case["id"].startswith("S02"):
        txs = records.get("archived_transactions", []) + records["transactions"]
        unique = {r["id"]: r for r in txs}
        income = sum(r["amount_minor"] for r in unique.values()
                     if r["kind"] == "income" and r["occurred_at"].startswith("2026-09"))
        checks.append(_check("原始9月收入流水独立求和", income, 650000, category="independent_integer_formula"))
    if case["id"].startswith("S04"):
        p = next(p for p in g.bank.products(w) if p["id"] == "SIM-BOND-30")
        checks.append(_check("21日用途与30日不可提前退出锁定期不相容",
                             p["term_days"] <= 21 or p["early_redemption"], False,
                             category="independent_term_constraint"))
    if case["id"].startswith("S09"):
        txs = g.statements.transactions(w)
        income = sum(r["amount_minor"] for r in txs if r["kind"] == "income")
        expense = sum(r["amount_minor"] for r in txs if r["kind"] == "expense")
        checks.append(_check("CSV原始收入减支出独立复算", income - expense, 800000,
                             category="independent_integer_formula"))
    if case["id"].startswith("S10"):
        credit = [r for r in records["credit_statements"] if r["status"] == "open"]
        amount = sum(max(0, r["total_due_minor"] - r["paid_minor"]) for r in credit)
        additional = max(0, 250000 - amount)
        checks.extend([_check("同一义务目标追加差额", additional, 50000, category="independent_integer_formula"),
                       _check("追加差额后可安排", 1027600 - additional, 977600, category="independent_integer_formula")])
    return checks


def preflight(g, w, case):
    """本轮预检历史不注入被测模型，也不算模型实际取得证据。"""
    checks = []
    try:
        _case(case)
        metadata = getattr(g, "_window_integration", {})
        if metadata.get("case_id") != case["id"] or metadata.get("environment") != "isolated_synthetic_fixture":
            raise ValueError("fixture_identity_unverified")
        runner = _runner()
        if metadata["dataset_sha256"] != _digest(DATASET_PATH):
            raise ValueError("frozen_dataset_changed_after_setup")
        checks = runner.oracle(g, w, case)
        checks.extend(_independent_checks(g, w, case))
        return {"status": "passed" if all(c["passed"] for c in checks) else "fixture_drift",
                "checks": checks, "case_id": case["id"], "dataset_sha256": _digest(DATASET_PATH),
                "scope": "offline_fixture_necessary_conditions_only", "semantic_pass": None,
                "limitations": ["60日事件生成沿用被测后端；独立整数聚合与固定值检查不替代事件来源审核。"]}
    except Exception as error:
        if hasattr(error, "checked_facts"):
            checks = error.checked_facts
        return {"status": "fixture_drift" if checks and any(not c["passed"] for c in checks) else "fixture_error",
                "checks": checks, "case_id": case["id"], "semantic_pass": None,
                "error": {"type": type(error).__name__, "code": str(error)[:120]}}


def snapshot(g, w):
    """只接受本模块创建的合成库；不将 token、验证码或密钥写入状态。"""
    if getattr(g, "_window_integration", {}).get("environment") != "isolated_synthetic_fixture":
        raise ValueError("snapshot_requires_verified_synthetic_fixture")
    runner = _runner()
    state = runner.snapshot(g, w)
    archive = g.bank.customer_archive(w)
    # 扩展业务对象检查，避免旧runner只查总现金而遗漏转账/卡片/代扣/操作草案。
    included = ("transactions", "operations", "investment_lots", "mandates", "subscriptions",
                "scheduled_payments", "cards", "card_controls", "card_limit_requests",
                "service_reservations", "wealth_goals", "goal_allocations", "personal_decisions")
    secrets = {"token", "csrf", "verification_code", "password", "pin", "session_token",
               "confirmation_token", "authorization_token", "code_hash", "token_hash"}
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in secrets}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    business = {}
    for table in included:
        if table in archive["records"]:
            rows = archive["records"][table]
        else:
            with closing(g.bank.connect()) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                rows = [dict(r) for r in db.execute("SELECT * FROM " + table + " WHERE workspace_id=?", (w,))] if exists else []
        rows = sorted(clean(rows), key=_packed)
        business[table] = {"count": len(rows), "sha256": hashlib.sha256(_packed(rows).encode()).hexdigest()}
    state.update(accounts=deepcopy(archive["accounts"]), business_state=business,
                 fixture_environment="synthetic", as_of=archive["as_of"])
    return clean(state)


def _observed_calls(record):
    outputs = {r.get("call_id"): r for r in record.get("tool_outputs", []) if isinstance(r, dict)}
    calls = []
    for call in record.get("model_tool_calls", []):
        if not isinstance(call, dict):
            continue
        function = call.get("function", {})
        name = call.get("name") or function.get("name")
        args = call.get("arguments", function.get("arguments", {}))
        if isinstance(args, str):
            try: args = json.loads(args)
            except ValueError: args = None
        row = outputs.get(call.get("id"), {})
        output = row.get("output")
        success = (isinstance(output, dict) and not row.get("trace_parse_error")
                   and not output.get("error") and not call.get("arguments_parse_error"))
        calls.append({"name": name, "arguments": args, "output": output, "success": success})
    return calls


def _replace_variables(value, variables):
    if isinstance(value, str) and value.startswith("$"):
        return variables[value[1:]]
    if isinstance(value, list):
        return [_replace_variables(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _replace_variables(v, variables) for k, v in value.items()}
    return value


def _select_path(value, path, selector):
    for part in path.split("."):
        if isinstance(value, list):
            matches = [row for row in value if all(
                str(row.get("id", "")).endswith(expected) if key == "id_suffix" else row.get(key) == expected
                for key, expected in selector.items())]
            if len(matches) != 1:
                raise ValueError("ambiguous_or_missing_selector")
            value = matches[0]
        value = value[part]
    return value


def _required_evidence_checks(record, case):
    """查实际调用与结构字段，不以答案含有某个词证明任务完成。"""
    calls = _observed_calls(record)
    main_ids = []
    credit_ids = []
    for c in calls:
        if c["success"] and c["name"] == "account_summary":
            main_ids += [a["id"] for a in c["output"].get("data", {}).get("accounts", [])
                         if a.get("id", "").endswith("-main")]
        if c["success"] and c["name"] == "budget" and isinstance(c["arguments"], dict) and not c["arguments"].get("reserves"):
            credit_ids += [r["evidence_id"] for r in c["output"].get("data", {}).get("commitments", [])
                           if r.get("kind") == "credit_card" and r.get("account_id") == c["arguments"].get("account_id")]
    # S03等题可直接用模型调用的budget账户，不强制增添原题没有要求的account_summary。
    if not main_ids:
        main_ids = [c["arguments"]["account_id"] for c in calls if c["name"] == "budget"
                    and isinstance(c["arguments"], dict) and c["arguments"].get("account_id", "").endswith("-main")]
    main_ids = set(main_ids)
    checks = []
    for requirement in case["expected"]["minimum_evidence"]:
        if requirement.startswith("before/after") or requirement == "no remember_profile write":
            continue
        if requirement.startswith("products if"):
            # 是否发表了具体产品判断由人工全文核对；避免擅自加强原题工具要求。
            checks.append({"label": requirement, "category": "conditional_evidence",
                           "status": "manual_review", "passed": None})
            continue
        tool = requirement.split("(", 1)[0].split(" ", 1)[0]
        candidates = [c for c in calls if c["success"] and c["name"] == tool]
        def matches(c):
            args = c["arguments"] or {}
            if "month=" in requirement:
                return args.get("month") == requirement.split("month=", 1)[1].rstrip(")")
            if "year=" in requirement:
                return args.get("year") == int(requirement.split("year=", 1)[1].rstrip(")"))
            if "days=" in requirement:
                return args.get("days") == int(requirement.split("days=", 1)[1].rstrip(")"))
            if "as_of=" in requirement:
                return args.get("as_of") == requirement.split("as_of=", 1)[1].rstrip(")")
            if tool == "budget":
                if args.get("account_id") not in main_ids:
                    return False
                if "reserves with" in requirement:
                    reserves = args.get("reserves", [])
                    return any(r.get("amount_minor") == 250000 and r.get("commitment_ids")
                               and set(r["commitment_ids"]).issubset(set(credit_ids)) for r in reserves)
                return not args.get("reserves")
            return True
        checks.append(_check("实际本轮证据：" + requirement, any(matches(c) for c in candidates), True,
                             category="required_tool_evidence"))
    if case["id"].startswith("S08"):
        checks.append(_check("假设题无remember_profile尝试",
                             any(c["name"] == "remember_profile" for c in calls), False,
                             category="no_profile_write_attempt"))
    # 每个实际读取的固定字段仍由本轮原始工具输出核对，不信任答案/审核文字。
    # 原题未要求必须读取的画像/产品保持可选；只核验模型确实读取的对象和期间。
    variables = {"main_account_id": next(iter(main_ids)) if len(main_ids) == 1 else None,
                 "credit_commitment_id": next(iter(set(credit_ids))) if len(set(credit_ids)) == 1 else None}
    for fact in case["expected"]["facts"]:
        if "tool" not in fact:
            continue
        try:
            args = _replace_variables(fact.get("arguments", {}), variables)
            selector = _replace_variables(fact.get("select", {}), variables)
        except KeyError:
            continue
        matches = [c for c in calls if c["success"] and c["name"] == fact["tool"]
                   and c["arguments"] == args]
        if not matches:
            continue
        for c in matches:
            try:
                actual = _select_path(c["output"], fact["path"], selector)
                checks.append(_check("本轮工具固定字段：" + fact["label"], actual, fact["expected"],
                                     category="observed_frozen_fact"))
            except (KeyError, TypeError, ValueError):
                # 出站净化会省略若干原始聚合字段，原题没有要求这些JSON路径必须原样传给模型。
                # 缺项既不能按错值判失败，也不能用预检的正确值证明模型实际已取得证据。
                checks.append({"label": "本轮投影字段待核对：" + fact["label"],
                               "category": "model_projection_coverage", "passed": None,
                               "status": "manual_review", "path": fact["path"],
                               "notice": "实际模型投影缺少此原始路径；人工核对本轮保留的事实/事件，不将预检注入或当作模型已读。"})
    return checks


def _manual_template(case):
    return {name: [{"index": i, "criterion": text, "verdict": None, "evidence": ""}
                   for i, text in enumerate(criteria)]
            for name, criteria in (("must_convey", case["expected"]["must_convey"]),
                                   ("forbidden_conclusions", case["expected"]["forbidden_conclusions"]),
                                   ("human_judgment", case["human_judgment"]))}


def _human_decision(record, template):
    review = record.get("human_review")
    if not isinstance(review, dict):
        return None, ["没有独立人工逐条裁决"]
    # 此接口记录人工证据，并不对reviewer身份作认证；执行器必须保存真实人工来源。
    required = ("reviewer", "reviewed_at", "artifact_sha256", "rationale", "verdict")
    missing = [key for key in required if not review.get(key)]
    verdict = review.get("verdict")
    if verdict not in ("pass", "fail", "incomplete"):
        missing.append("invalid_verdict")
    for name, criteria in template.items():
        rows = review.get(name, [])
        if not isinstance(rows, list) or sorted(r.get("index", -1) for r in rows if isinstance(r, dict)) != list(range(len(criteria))):
            missing.append(name + "_incomplete")
            continue
        allowed = ("absent", "present", "not_applicable") if name == "forbidden_conclusions" else ("met", "not_met", "not_applicable")
        if any(r.get("verdict") not in allowed or not isinstance(r.get("evidence"), str)
               or not r["evidence"].strip() for r in rows):
            missing.append(name + "_missing_judgment_or_evidence")
        if verdict == "pass" and any(r.get("verdict") == ("present" if name == "forbidden_conclusions" else "not_met") for r in rows):
            missing.append(name + "_contradicts_pass")
    declared = review.get("artifact_sha256", "")
    if not isinstance(declared, str) or len(declared) != 64 or any(c not in "0123456789abcdef" for c in declared):
        missing.append("invalid_artifact_sha256")
    if record.get("record_artifact_sha256") and record["record_artifact_sha256"] != declared:
        missing.append("artifact_sha256_mismatch")
    if missing:
        return None, missing
    return verdict, []


def classify(record, case):
    """兼容旧runner记录；GLM approved和关键词不参与最终判定。"""
    _case(case)
    template = _manual_template(case)
    checks, limitations = [], []
    if record.get("id") != case["id"]:
        checks.append(_check("记录与题目对象匹配", record.get("id"), case["id"], category="record_identity"))
    pre = record.get("preflight")
    oracle = pre.get("checks", []) if isinstance(pre, dict) else record.get("oracle", [])
    if record.get("runner_failure") or (isinstance(pre, dict) and pre.get("status") != "passed"):
        return {"status": "fixture_or_runner_error", "semantic_pass": None, "checks": checks,
                "failure": record.get("runner_failure") or pre, "human_review_template": template,
                "reviewer_approved_used_as_verdict": False}
    checks.append(_check("离线固定事实预检完整且通过", bool(oracle) and all(c.get("passed") is True for c in oracle), True,
                         category="fixture_preflight"))
    # 旧runner完整事实预检按label固定核对；不能以一条伪造passed替代整题。
    expected_labels = {f["label"] for f in case["expected"]["facts"] if "tool" in f}
    checks.append(_check("固定事实预检包含原题每项", expected_labels.issubset({c.get("label") for c in oracle}), True,
                         category="fixture_preflight"))
    for fact in case["expected"]["facts"]:
        if "tool" not in fact:
            continue
        captured = [c for c in oracle if c.get("label") == fact["label"]]
        valid = len(captured) == 1 and captured[0].get("tool") == fact["tool"] and captured[0].get("path") == fact["path"]
        if valid:
            valid = (type(captured[0].get("expected")) is type(fact["expected"])
                     and captured[0].get("expected") == fact["expected"]
                     and type(captured[0].get("actual")) is type(fact["expected"])
                     and captured[0].get("actual") == fact["expected"])
        checks.append(_check("预检原始固定值：" + fact["label"], valid, True, category="fixture_preflight"))
    before, after = record.get("before"), record.get("after")
    if isinstance(before, dict) and isinstance(after, dict):
        for key in ("total_cash_minor", "orders", "holdings", "fund_holds", "profile"):
            checks.append(_check("前后状态不变：" + key, key in before and key in after and before[key] == after[key], True,
                                 category="state_invariant"))
        for key in ("accounts", "business_state"):
            if key in before and key in after:
                checks.append(_check("扩展业务状态不变：" + key, before[key] == after[key], True, category="state_invariant"))
            else:
                limitations.append("旧记录未捕获" + key + "，相应隔离/业务状态覆盖不能从总现金推断。")
        if "observations" in before and "observations" in after:
            checks.append(_check("画像候选增量为0", after.get("observations", -1) - before.get("observations", 0), 0,
                                 category="state_invariant"))
        else:
            checks.append(_check("画像候选状态已捕获", False, True, category="state_invariant"))
        if "memories" in before and "memories" in after:
            checks.append(_check("本人记忆增量为0", len(after.get("memories", [])) - len(before.get("memories", [])), 0,
                                 category="state_invariant"))
            checks.append(_check("本人记忆内容不变", before["memories"] == after["memories"], True, category="state_invariant"))
        else:
            checks.append(_check("本人记忆状态已捕获", False, True, category="state_invariant"))
    else:
        checks.append(_check("前后原始状态完整", False, True, category="state_invariant"))
    result = record.get("result") or {}
    checks.append(_check("没有操作确认卡", bool(result.get("bank_actions")), False, category="no_implicit_operation"))
    technical = record.get("http_status") == 200 and record.get("technical_completed") is True and not result.get("isFallback", False)
    checks.append(_check("HTTP与运行时技术完成", technical, True, category="technical_completion"))
    checks.append(_check("保存了可见答复正文", isinstance(result.get("reply"), str) and bool(result["reply"].strip()), True,
                         category="visible_answer_artifact"))
    checks.extend(_required_evidence_checks(record, case))
    failed = [c for c in checks if c.get("passed") is False]
    human, human_gaps = _human_decision(record, template)
    if any(c["category"] == "fixture_preflight" for c in failed):
        status = "fixture_error"
    elif not technical:
        status = "technical_failure"
    elif failed:
        status = "necessary_condition_failure"
    elif human is None:
        status = "needs_human_review"
    else:
        status = {"pass": "human_pass", "fail": "human_fail", "incomplete": "human_incomplete"}[human]
    return {"status": status, "semantic_pass": True if status == "human_pass" else False if status in ("human_fail", "necessary_condition_failure") else None,
            "automatic_necessary_conditions": "failed" if failed else "passed",
            "checks": checks, "failed_checks": [c["label"] for c in failed],
            "human_review_template": template, "human_review_gaps": human_gaps,
            "limitations": limitations, "reviewer_approved_used_as_verdict": False,
            "word_matching_used_as_final_judge": False}


def _self_test():
    data = load_dataset()
    results = []
    for case in data["cases"]:
        with tempfile.TemporaryDirectory(prefix="navren-window-semantic-") as temp:
            g, w, token = build_fixture(case, temp)
            result = preflight(g, w, case)
            before = snapshot(g, w)
            runner = _runner()
            tools = runner.BankTools(g, w, "structural-self-test-" + case["id"])
            main = next(a["id"] for a in tools.invoke("account_summary", {})["data"]["accounts"] if a["id"].endswith("-main"))
            baseline = tools.invoke("budget", {"account_id": main})["data"]
            credit = next(r["evidence_id"] for r in baseline["commitments"] if r["kind"] == "credit_card")
            variables = {"main_account_id": main, "credit_commitment_id": credit, "workspace_id": w}
            calls, outputs = [], []
            for i, fact in enumerate(case["expected"]["facts"]):
                if "tool" not in fact:
                    continue
                args = _replace_variables(fact.get("arguments", {}), variables)
                call_id = "structural-" + str(i)
                calls.append({"id": call_id, "name": fact["tool"], "arguments": args})
                outputs.append({"call_id": call_id, "output": tools.invoke(fact["tool"], args)})
            after = snapshot(g, w)
            row = {"id": case["id"], "preflight_status": result["status"],
                   "check_count": len(result["checks"]), "snapshot_stable": before == after}
            if result.get("error"):
                row["error"] = result["error"]
            row["failed_checks"] = [r["label"] for r in result["checks"] if not r["passed"]]
            # 这些工具轨迹是结构测试替身；没有模型实际调用，也不作为语义成绩。
            record = {"id": case["id"], "preflight": result, "before": before, "after": after,
                      "http_status": 200, "technical_completed": True,
                      "model_tool_calls": calls, "tool_outputs": outputs,
                      "result": {"reply": "结构测试替身回答，未进行金融语义验收。"},
                      "independent_reviews": [{"decision": {"status": "approved"}}]}
            classified = classify(record, case)
            row["structural_control_status"] = classified["status"]
            changed = deepcopy(record)
            changed["after"]["total_cash_minor"] += 1
            side_effect_detected = classify(changed, case)["status"] == "necessary_condition_failure"
            action = deepcopy(record)
            action["result"]["bank_actions"] = [{"fixture": True}]
            action_detected = classify(action, case)["status"] == "necessary_condition_failure"
            row["negative_controls_passed"] = side_effect_detected and action_detected and classified["status"] == "needs_human_review"
            results.append(row)
    print(json.dumps({"scope": "offline_fixture_self_test", "model_calls": 0,
                      "semantic_pass_count": None, "records": results}, ensure_ascii=False, indent=2))
    return 0 if all(r["preflight_status"] == "passed" and r["snapshot_stable"]
                    and r["negative_controls_passed"] for r in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if not args.self_test:
        parser.print_help()
        raise SystemExit(0)
    raise SystemExit(_self_test())
