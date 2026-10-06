"""仅使用预先批准的合成费用/服务规则的双角色协商服务。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
import os
import fcntl
import tempfile
import threading
from contextlib import closing, contextmanager, ExitStack
from datetime import date
from pathlib import Path

from simulated_bank.service import BankError, encoded, strict_fields


RULE_VERSION = "navren.synthetic-service-benefits.v1"
SHARED_MEMORY_CATEGORIES = {"goal", "liquidity_preference", "product_interest"}
CONDITION_POLICY = "salary-consent/2"
_RULE_LOCKS = {}
_RULE_LOCKS_MUTEX = threading.Lock()


class ServiceNegotiationService:
    def __init__(self, gateway, rules_path=None):
        self.gateway = gateway
        self.rules_path = Path(rules_path or Path(__file__).with_name("simulated_bank") /
                               "service_benefit_rules.json")
        with self.gateway.bank.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS base_service_negotiation_terms(
                draft_id TEXT PRIMARY KEY REFERENCES service_negotiation_drafts(id),
                rules_sha256 TEXT NOT NULL, rules_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS base_service_condition_policies(
                draft_id TEXT PRIMARY KEY REFERENCES service_negotiation_drafts(id),
                policy TEXT NOT NULL)""")

    @contextmanager
    def rules_guard(self, *, protect=False):
        """同一路径跨实例/同用户跨进程发布互斥；银行/公共锁必须先取得。"""
        path = str(self.rules_path.resolve())
        with _RULE_LOCKS_MUTEX:
            lock, local = _RULE_LOCKS.setdefault(path, (threading.RLock(), threading.local()))
        with lock:
            depth = getattr(local, "depth", 0)
            if not depth:
                # 与规则同目录的稳定inode；不依赖各进程的TMPDIR或临时命名空间。
                canonical = Path(path)
                lockpath = canonical.with_name("." + canonical.name + ".publish.lock")
                fd = os.open(lockpath, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                local.fd = fd
                local.path = path
            local.depth = depth + 1
            if protect:
                local.protected = getattr(local, "protected", 0) + 1
            try:
                yield local
            finally:
                if protect:
                    local.protected -= 1
                local.depth -= 1
                if not local.depth:
                    fcntl.flock(local.fd, fcntl.LOCK_UN)
                    os.close(local.fd)
                    del local.fd

    def publish_rules(self, rules, *, expected_sha256):
        """受信维护入口，非模型/HTTP工具；CAS原子发布，不能穿过响应或提交保护。"""
        # 深复制并拒绝NaN；不允许调用方在等待锁时修改将发布的对象。
        try:
            rules = json.loads(json.dumps(rules, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError):
            raise BankError("SERVICE_RULES_INVALID", "规则不是有效的合成资料。", 503) from None
        self._validate_rules(rules)
        with self.rules_guard() as local:
            if getattr(local, "protected", 0):
                raise BankError("SERVICE_RULES_BUSY", "本轮发布或提交正在保护服务规则，请稍后更新。", 409)
            if not isinstance(expected_sha256, str) or self._rules_digest(self.rules()) != expected_sha256:
                raise BankError("SERVICE_RULES_VERSION_CONFLICT", "当前规则版本与发布预期不一致。", 409)
            target = Path(local.path)
            fd, name = tempfile.mkstemp(prefix="." + target.name + "-", dir=target.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(rules, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, target)
                parent = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            return {"rules_sha256": self._rules_digest(rules), "environment": "synthetic"}

    @classmethod
    def _validate_rules(cls, data):
        if (not isinstance(data, dict) or data.get("schema_version") != RULE_VERSION
                or data.get("environment") != "synthetic" or not isinstance(data.get("offers"), list)
                or not isinstance(data.get("notice"), str)):
            raise BankError("SERVICE_RULES_INVALID", "模拟服务优惠规则未通过版本检查。", 503)
        ids = set()
        for rule in data["offers"]:
            cls._validate_rule(rule)
            if (not isinstance(rule.get("offer_id"), str) or not rule["offer_id"] or rule["offer_id"] in ids
                    or any(type(rule.get(k)) is not bool for k in ("requires_salary_deposit", "requires_investment_purchase"))
                    or not isinstance(rule.get("eligibility"), dict)
                    or set(rule["eligibility"]) - {"active_account_required", "salary_intent_required"}
                    or any(type(v) is not bool for v in rule["eligibility"].values())
                    or not isinstance(rule.get("conditions"), list)
                    or not all(isinstance(c, str) for c in rule["conditions"])
                    or rule.get("application_status_on_accept") != "submitted_simulation_pending_eligibility"):
                raise BankError("SERVICE_RULES_INVALID", "服务条件或模拟申请状态无效。", 503)
            ids.add(rule["offer_id"])

    def assert_evidence(self, workspace, snapshot, *, db):
        """在调用方已持有银行事务和规则锁时重查，不另建银行事务。"""
        fail = lambda: BankError("STALE_SERVICE_EVIDENCE", "服务规则、资格或预览已变化，请重新查看。", 409)
        if (not isinstance(snapshot, dict) or set(snapshot) != {"schema_version", "workspace_id",
                "rules_sha256", "applications_sha256", "preview"}
                or type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != 1
                or snapshot["workspace_id"] != workspace):
            raise fail()
        try:
            rules = self.rules()
        except BankError:
            raise fail() from None
        if self._rules_digest(rules) != snapshot["rules_sha256"]:
            raise fail()
        if snapshot["applications_sha256"] is not None:
            rows = db.execute("SELECT * FROM service_negotiation_applications WHERE workspace_id=? "
                "ORDER BY created_at DESC,id DESC LIMIT 20", (workspace,)).fetchall()
            view = {"environment": "simulation", "applications": [self._application_view(r, idempotent=False) for r in rows],
                    "notice": "全部为 Navren 演示用合成服务申请。"}
            if self._rules_digest(view) != snapshot["applications_sha256"]:
                raise fail()
        preview = snapshot["preview"]
        if preview is None:
            return
        if not isinstance(preview, dict) or set(preview) != {"draft_id", "sha256"}:
            raise fail()
        row = db.execute("SELECT * FROM service_negotiation_drafts WHERE id=? AND workspace_id=?",
                         (preview["draft_id"], workspace)).fetchone()
        if (not row or row["status"] != "previewed" or self.gateway.bank.clock() >= row["expires_at"]
                or self._rules_digest(dict(row)) != preview["sha256"]):
            raise fail()
        policy = db.execute("SELECT policy FROM base_service_condition_policies WHERE draft_id=?", (row["id"],)).fetchone()
        if not policy or policy["policy"] != CONDITION_POLICY:
            raise fail()
        binding = db.execute("SELECT rules_sha256 FROM base_service_negotiation_terms WHERE draft_id=?", (row["id"],)).fetchone()
        if not binding or binding["rules_sha256"] != snapshot["rules_sha256"]:
            raise fail()
        if row["share_memory"] and self._memory_revision(db, workspace) != row["memory_revision"]:
            raise fail()
        conditions = json.loads(row["conditions_json"])
        as_of = db.execute("SELECT as_of FROM workspaces WHERE id=?", (workspace,)).fetchone()[0]
        for offer_id in json.loads(row["offer_ids_json"]):
            rule = next((r for r in rules["offers"] if r["offer_id"] == offer_id), None)
            if (not rule or not rule["valid_from"] <= as_of <= rule["valid_through"]
                    or (rule["requires_salary_deposit"] and
                        (conditions.get("salary_deposit_intent") is not True or conditions.get("salary_deposit_refused") is not False))
                    or (rule["requires_investment_purchase"] and conditions.get("investment_purchase_refused"))
                    or (rule["eligibility"].get("active_account_required") and not db.execute(
                        "SELECT 1 FROM accounts WHERE workspace_id=? AND id LIKE '%-main'", (workspace,)).fetchone())):
                raise fail()

    @contextmanager
    def evidence_guard(self, workspace, snapshot, *, db):
        """先取得所有公开锁，再进入本门；由外层ExitStack持有到银行commit及发送完成。"""
        with self.rules_guard(protect=True):
            self.assert_evidence(workspace, snapshot, db=db)
            yield

    @staticmethod
    def _rules_digest(rules):
        return hashlib.sha256(json.dumps(rules, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    @staticmethod
    def _validate_rule(rule):
        try:
            valid = (all(type(rule[key]) is int and rule[key] >= 0 for key in
                ("monthly_value_minor", "max_cost_minor", "minimum_monthly_salary_minor", "salary_months_required"))
                and type(rule["duration_months"]) is int and rule["duration_months"] > 0
                and rule["monthly_value_minor"] * rule["duration_months"] == rule["max_cost_minor"]
                and rule["currency"] == "CNY"
                and date.fromisoformat(rule["valid_from"]) <= date.fromisoformat(rule["valid_through"]))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise BankError("SERVICE_RULES_INVALID", "模拟优惠的金额、成本或有效期未通过校验。", 503)

    def rules(self):
        with self.rules_guard() as local:
            try:
                data = json.loads(Path(local.path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise BankError("SERVICE_RULES_UNAVAILABLE", "模拟服务优惠规则暂不可用。", 503) from None
            self._validate_rules(data)
            return data

    @staticmethod
    def _user_conditions(text):
        clauses = re.split(r"[，,；;。\n]|但是|但|不过|然而|而且|且", text)
        salary_clauses = [clause for clause in clauses if
            re.search(r"工资|薪资", clause) and re.search(r"迁入|转入|代发|入账", clause)]
        refusal = r"不愿意|不同意|不愿|不想|拒绝|不要|不能|不会|不接受|不考虑|暂不|尚未同意|没同意|没有同意|并非同意|不是同意|不\s*(?:迁入|转入|代发|入账)"
        uncertain = r"(?:没|没有|未|尚未|还没).{0,6}(?:说|表示|同意|决定|确定|想好|表态|承诺)|不是说|并非同意|不确定|不一定|是否|能否|愿不愿|同不同意|可不可以|能不能|怎么办|什么|如何|吗|[？?]|如果|假如|要是|若|只要|除非|前提|考虑|了解|询问|问问|咨询|设想|假设"
        reported = r"(?:银行|客服|朋友|同事|队友|他|她|有人|你|您).{0,6}(?:说|问|建议|表示)|听说|转述|据说|[“”\"‘’]"
        eligible_clauses = [c for c in salary_clauses if not re.search(reported, c)]
        salary_refused = any(re.search(refusal, clause) and not re.search(uncertain, clause)
                             for clause in eligible_clauses)
        affirmative = (
            r"(?:愿意|可以|同意)(?:(?!不|电子账单|理财).){0,20}(?:迁入|转入|代发|入账).{0,12}(?:工资|薪资)"
            r"|(?:愿意|可以|同意)(?:(?!不|电子账单|理财).){0,12}(?:工资|薪资).{0,12}(?:迁入|转入|代发|入账)"
            r"|(?:工资|薪资).{0,12}(?:愿意|可以|同意).{0,8}(?:迁入|转入|代发|入账)"
            r"|(?:工资|薪资).{0,12}(?:迁入|转入|代发|入账).{0,8}(?:我愿意|我可以|我同意)")
        salary = any(re.search(affirmative, re.sub(refusal, "拒绝", clause))
            and not re.search(uncertain, clause) for clause in eligible_clauses)
        # 条件的前件、尚未决定或矛盾可以跨分句；不能把后件单独升格为授权。
        unresolved = (any(re.search(uncertain, c) for c in eligible_clauses)
            or bool(re.search(r"如果|假如|要是|只要|除非|前提|只是问问|(?:没|没有|未|尚未|还没).{0,7}(?:决定|确定|想好|承诺)|尚未作最终决定", text)))
        if unresolved or (salary and salary_refused):
            salary, salary_refused = False, False
        refuses_investment = bool(re.search(
            r"(?:不接受|不愿意|拒绝|不买|不想买|不要|不能).{0,18}(?:购买|买入|申购)?"
            r".{0,12}理财|(?:购买|申购)理财.{0,12}(?:不接受|不愿意|拒绝)", text, re.I,
        ))
        mentions_service_fee = bool(re.search(r"费用|费率|减免|优惠|福利|协商|服务方案", text, re.I))
        return {"salary_deposit_intent": salary,
                "salary_deposit_refused": salary_refused,
                "investment_purchase_refused": refuses_investment,
                "service_benefit_requested": mentions_service_fee}

    def _memory_revision(self, db, workspace):
        # 调用方持有银行事务写锁，个人变更不能穿过接受核验。
        return self.gateway.memories.state(workspace)['revision']

    def preview(self, workspace, payload):
        strict_fields(payload, {"text", "share_memory"}, {"text", "share_memory"})
        text, share_memory = payload["text"], payload["share_memory"]
        if not isinstance(text, str) or not text.strip() or len(text) > 1200:
            raise BankError("INVALID_NEGOTIATION_REQUEST", "請用不超過1200字描述服務需求。")
        if type(share_memory) is not bool:
            raise BankError("INVALID_MEMORY_SHARING", "請明確選擇是否向模拟银行代理披露获准记忆。")
        rules = self.rules()
        overview = self.gateway.overview(workspace)
        as_of = overview["as_of"]
        conditions = self._user_conditions(text)
        if not conditions["service_benefit_requested"]:
            return {"status": "clarification", "offers": [], "draft_id": None,
                    "message": "请说明希望协商哪类费用或服务，例如账户服务费。",
                    "notice": rules["notice"]}
        main = next((account for account in overview["accounts"]
                     if account["id"].endswith("-main")), None)
        active_account = bool(main and main.get("status", "active") == "active")

        memory_revision = None
        memory_nodes = ()
        if share_memory:
            state = self.gateway.memories.state(workspace)
            memory_revision = state["revision"]
            if state["enabled"]:
                context = self.gateway.memories.read_relevant(workspace, text, workflow="wealth")
                memory_nodes = tuple(node for node in context.nodes
                                     if node.category in SHARED_MEMORY_CATEGORIES and node.user_confirmed)

        offers = []
        for rule in rules["offers"]:
            self._validate_rule(rule)
            if not (rule["valid_from"] <= as_of <= rule["valid_through"]):
                continue
            if rule["eligibility"].get("active_account_required") and not active_account:
                continue
            if rule.get("requires_salary_deposit") and not conditions["salary_deposit_intent"]:
                continue
            if conditions["investment_purchase_refused"] and rule.get("requires_investment_purchase"):
                continue
            offers.append({
                **rule,
                "eligibility_status": "conditional_application",
                "review_status": "rules_checked",
                "status_label": "可提交条件申请，资格待模拟规则核验",
                "investment_purchase_required": bool(rule.get("requires_investment_purchase")),
            })

        shared_memory = [{"node_id": node.node_id, "category": node.category, "value": node.value}
                         for node in memory_nodes]
        if not offers:
            reason = ("没有发现符合期限与资格条件的预批准服务优惠。"
                      if conditions["salary_deposit_intent"] else
                      "未识别到可供模拟规则核验的工资入账条件；可先查看不要求迁入工资的方案。")
            return {"status": "no_offer", "offers": [], "draft_id": None,
                    "conditions": conditions, "shared_memory": shared_memory,
                    "message": reason, "notice": rules["notice"],
                    "participants": ["user_interest_agent-v1", "simulated_bank_benefit_agent-v1",
                                     "service_rules_reviewer-v1"]}

        draft_id, now = uuid.uuid4().hex, self.gateway.bank.clock()
        memory_ids = [item["node_id"] for item in shared_memory]
        offer_ids = [item["offer_id"] for item in offers]
        with ExitStack() as protections, self.gateway.bank.transaction() as db:
            protections.enter_context(self.rules_guard(protect=True))
            db.execute("UPDATE service_negotiation_drafts SET status='superseded',updated_at=? "
                       "WHERE workspace_id=? AND status='previewed'", (now, workspace))
            db.execute(
                "INSERT INTO service_negotiation_drafts "
                "(id,workspace_id,conditions_json,offer_ids_json,memory_node_ids_json,memory_revision,"
                "share_memory,rule_version,status,expires_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'previewed',?,?,?)",
                (draft_id, workspace, encoded(conditions), encoded(offer_ids), encoded(memory_ids),
                 memory_revision, int(share_memory), RULE_VERSION, now + 900, now, now),
            )
            if self._rules_digest(self.rules()) != self._rules_digest(rules):
                raise BankError("SERVICE_DRAFT_STALE", "服务规则在生成方案时变化，请重新协商。", 409)
            db.execute("INSERT INTO base_service_negotiation_terms VALUES(?,?,?)",
                (draft_id, self._rules_digest(rules), encoded(rules)))
            db.execute("INSERT INTO base_service_condition_policies VALUES(?,?)", (draft_id, CONDITION_POLICY))
            self.gateway.bank._audit(db, workspace, "service_negotiation_previewed", {
                "draft_id": draft_id, "offer_ids": offer_ids,
                "share_memory": bool(share_memory), "shared_memory_categories": sorted(
                    {item["category"] for item in shared_memory}),
            })
        return {"status": "previewed", "draft_id": draft_id, "conditions": conditions,
                "offers": offers, "shared_memory": shared_memory,
                "memory_revision": memory_revision, "rule_version": RULE_VERSION,
                "expires_in_seconds": 900, "notice": rules["notice"],
                "message": ("工资迁入意愿尚未明确，请本人明确确认是否愿意；电子账单可独立申请。"
                            if not conditions["salary_deposit_intent"] and not conditions["salary_deposit_refused"] else
                            "已按本人明确的工资意愿筛选；申请不代表工资已迁入或优惠已兑现。"),
                "participants": ["user_interest_agent-v1", "simulated_bank_benefit_agent-v1",
                                 "service_rules_reviewer-v1"]}

    def accept(self, workspace, draft_id, payload):
        strict_fields(payload, {"offer_id", "confirmed"}, {"offer_id", "confirmed"})
        offer_id, confirmed = payload["offer_id"], payload["confirmed"]
        if not isinstance(offer_id, str) or type(confirmed) is not bool or confirmed is not True:
            raise BankError("CONFIRMATION_REQUIRED", "请在核对条件后明确确认提交模拟申请。")
        now = None
        memory_changed = False
        try:
            with ExitStack() as protections, self.gateway.bank.transaction() as db:
                protections.enter_context(self.rules_guard(protect=True))
                now = self.gateway.bank.clock()
                draft = db.execute(
                    "SELECT * FROM service_negotiation_drafts WHERE id=? AND workspace_id=?",
                    (draft_id, workspace),
                ).fetchone()
                if not draft:
                    raise BankError("SERVICE_DRAFT_NOT_FOUND", "模拟协商草案不存在或已过期。", 404)
                existing = db.execute(
                    "SELECT * FROM service_negotiation_applications WHERE draft_id=? AND offer_id=? AND workspace_id=?",
                    (draft_id, offer_id, workspace),
                ).fetchone()
                if existing:
                    return self._application_view(existing, idempotent=True)
                if draft["status"] != "previewed" or draft["expires_at"] <= now:
                    raise BankError("SERVICE_DRAFT_STALE", "这份服务方案已过期或被新方案替代，请重新协商。", 409)
                if draft["rule_version"] != RULE_VERSION:
                    raise BankError("SERVICE_DRAFT_STALE", "服务优惠规则已更新，请重新查看方案。", 409)
                policy = db.execute("SELECT policy FROM base_service_condition_policies WHERE draft_id=?", (draft_id,)).fetchone()
                if not policy or policy["policy"] != CONDITION_POLICY:
                    raise BankError("SERVICE_DRAFT_STALE", "工资条件校验方式已更新，请重新预览并由本人确认。", 409)
                binding = db.execute("SELECT * FROM base_service_negotiation_terms WHERE draft_id=?", (draft_id,)).fetchone()
                try:
                    rules = self.rules()
                except BankError:
                    raise BankError("SERVICE_DRAFT_STALE", "当前服务规则已失效，请重新查看方案。", 409) from None
                if not binding or binding["rules_sha256"] != self._rules_digest(rules):
                    raise BankError("SERVICE_DRAFT_STALE", "服务优惠条款或资格已变化，请重新查看并确认新方案。", 409)
                rule = next((item for item in rules["offers"] if item["offer_id"] == offer_id), None)
                if rule is None:
                    raise BankError("SERVICE_DRAFT_STALE", "已展示的服务优惠已撤回，请重新协商。", 409)
                self._validate_rule(rule)
                as_of = db.execute("SELECT as_of FROM workspaces WHERE id=?", (workspace,)).fetchone()[0]
                if not rule["valid_from"] <= as_of <= rule["valid_through"]:
                    raise BankError("SERVICE_DRAFT_STALE", "服务优惠不在当前模拟业务日期的有效期内。", 409)
                selected = json.loads(draft["offer_ids_json"])
                if offer_id not in selected:
                    raise BankError("SERVICE_OFFER_NOT_ELIGIBLE", "该优惠不在这次已审核的服务方案中。", 409)
                conditions = json.loads(draft["conditions_json"])
                if rule.get("requires_salary_deposit") and (conditions.get("salary_deposit_intent") is not True
                        or conditions.get("salary_deposit_refused") is not False):
                    raise BankError("SERVICE_OFFER_NOT_ELIGIBLE", "工资入账条件未通过规则核验。", 409)
                if conditions.get("investment_purchase_refused") and rule.get("requires_investment_purchase"):
                    raise BankError("SERVICE_OFFER_CONFLICT", "方案与用户明确拒绝的购买条件冲突。", 409)
                if rule["eligibility"].get("active_account_required"):
                    account = db.execute(
                        "SELECT id FROM accounts WHERE workspace_id=? AND id LIKE '%-main'",
                        (workspace,),
                    ).fetchone()
                    if not account:
                        raise BankError("SERVICE_OFFER_NOT_ELIGIBLE", "没有符合条件的模拟活跃账户。", 409)
                if draft["share_memory"]:
                    current_revision = self._memory_revision(db, workspace)
                    if current_revision != draft["memory_revision"]:
                        db.execute("UPDATE service_negotiation_drafts SET status='superseded',updated_at=? WHERE id=?",
                                   (now, draft_id))
                        memory_changed = True
                        raise BankError("SERVICE_DRAFT_STALE", "获准披露的财务记忆已变化，请重新查看服务方案。", 409)
                now = self.gateway.bank.clock()
                if now >= draft["expires_at"]:
                    raise BankError("SERVICE_DRAFT_STALE", "接受核验期间方案已过期，请重新预览。", 409)
                application_id = uuid.uuid4().hex
                terms = {key: rule[key] for key in (
                    "offer_id", "title", "benefit_type", "monthly_value_minor", "duration_months",
                    "max_cost_minor", "currency", "conditions", "requires_salary_deposit",
                    "minimum_monthly_salary_minor", "salary_months_required",
                    "requires_investment_purchase", "valid_from", "valid_through")}
                terms["eligibility"] = rule["eligibility"]
                terms["rules_sha256"] = binding["rules_sha256"]
                terms["notice"] = rules["notice"]
                db.execute(
                    "INSERT INTO service_negotiation_applications "
                    "(id,workspace_id,draft_id,offer_id,status,terms_json,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (application_id, workspace, draft_id, offer_id,
                     rule["application_status_on_accept"], encoded(terms), now, now),
                )
                self.gateway.bank._audit(db, workspace, "service_negotiation_application_submitted", {
                    "application_id": application_id, "draft_id": draft_id,
                    "offer_id": offer_id, "status": rule["application_status_on_accept"],
                    "max_cost_minor": rule["max_cost_minor"],
                })
                row = db.execute("SELECT * FROM service_negotiation_applications WHERE id=?",
                                 (application_id,)).fetchone()
                if self.gateway.bank.clock() >= draft["expires_at"]:
                    raise BankError("SERVICE_DRAFT_STALE", "提交前方案已过期，模拟申请未提交。", 409)
                return self._application_view(row, idempotent=False)
        except BankError:
            if memory_changed:
                # The stale-status update was rolled back with the rejected acceptance.
                # Commit invalidation separately so this draft can never be retried.
                with self.gateway.bank.transaction() as db:
                    db.execute("UPDATE service_negotiation_drafts SET status='superseded',updated_at=? "
                               "WHERE id=? AND workspace_id=? AND status='previewed'",
                               (now, draft_id, workspace))
                    self.gateway.bank._audit(db, workspace, "service_negotiation_draft_invalidated", {
                        "draft_id": draft_id, "reason": "shared_memory_changed",
                    })
            raise

    @staticmethod
    def _application_view(row, *, idempotent):
        return {"application_id": row["id"], "draft_id": row["draft_id"],
                "offer_id": row["offer_id"], "status": row["status"],
                "terms": json.loads(row["terms_json"]), "created_at": row["created_at"],
                "idempotent_replay": idempotent,
                "notice": "已记录模拟申请；资格仍待模拟规则核验，不代表真实优惠或合同。"}

    def applications(self, workspace):
        with closing(self.gateway.bank.connect()) as db:
            rows = db.execute(
                "SELECT * FROM service_negotiation_applications WHERE workspace_id=? "
                "ORDER BY created_at DESC,id DESC LIMIT 20", (workspace,),
            ).fetchall()
        return {"environment": "simulation", "applications": [
            self._application_view(row, idempotent=False) for row in rows
        ], "notice": "全部为 Navren 演示用合成服务申请。"}
