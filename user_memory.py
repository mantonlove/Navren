"""用户主动维护的本地财务记忆；默认关闭，不保存流水原文或身份凭证。"""

from __future__ import annotations

import re
import uuid
import hashlib
import json
from contextlib import closing
from datetime import date

from agent_runtime.contracts import MemoryGraphContext, MemoryNode, MemoryOrigin, MemoryRelation
from simulated_bank.service import BankError, strict_fields
from personal_data import personal_store, explicit_self_statement


CATEGORIES = {
    "goal": "财务目标",
    "planned_commitment": "已确认的大额支出",
    "liquidity_preference": "流动性偏好",
    "product_interest": "关注的产品类型",
    "knowledge_preference": "信息说明偏好",
}

_CATEGORY_RELEVANCE = {
    "cashflow": {"goal", "planned_commitment", "liquidity_preference"},
    "wealth": {"goal", "liquidity_preference", "product_interest", "knowledge_preference"},
}


def privacy_revision(db, workspace):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_privacy_versions'").fetchone():return 0
    row=db.execute('SELECT revision FROM conversation_privacy_versions WHERE workspace_id=?',(workspace,)).fetchone()
    return row[0] if row else 0


def invalidate_graph(db, workspace):
    """撤回/修正立即清除派生投影，不等下一次浏览器读取才失效。"""
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('graph_evidence_links','graph_assertions','graph_events','graph_entities','graph_calculations','graph_scenarios'):
        if table in tables:db.execute('DELETE FROM '+table+' WHERE workspace_id=?',(workspace,))
    if 'personal_decisions' in tables:
        for r in db.execute("SELECT id,payload FROM personal_decisions WHERE workspace_id=? AND status='confirmed'",(workspace,)).fetchall():
            if any(x.get('source_table')in {'user_memories','user_memory_relations','profile_observations'}for x in json.loads(r['payload']).get('evidence_refs',[])):
                db.execute("UPDATE personal_decisions SET status='invalidated' WHERE workspace_id=? AND id=?",(workspace,r['id']))
    # 删除或修正资料后，旧答复不能再次作为模型上下文或组件依据。
    for table in ('agent_dialogues','agent_widget_history'):
        if table in tables:db.execute('DELETE FROM '+table+' WHERE workspace_id=?',(workspace,))
    db.execute('CREATE TABLE IF NOT EXISTS conversation_privacy_versions(workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id), revision INTEGER NOT NULL)')
    db.execute('INSERT INTO conversation_privacy_versions VALUES(?,1) ON CONFLICT(workspace_id) DO UPDATE SET revision=revision+1',(workspace,))


class UserMemoryStore:
    """以模拟工作区隔离用户明确填写的记忆，并强制检查读取开关。"""

    def __init__(self, bank):
        self.bank = bank
        self.store = personal_store(bank)

    def _as_of(self, workspace):
        self.store.validate_owner(workspace)
        with closing(self.bank.connect()) as db:
            row = db.execute("SELECT as_of FROM workspaces WHERE id=?", (workspace,)).fetchone()
        return row[0]

    @staticmethod
    def _row(row):
        return {
            "node_id": row["id"],
            "category": row["category"],
            "category_label": CATEGORIES.get(row["category"], "待核实类别"),
            "value": row["value"],
            "origin": row["origin"],
            "user_confirmed": bool(row["user_confirmed"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_on": row["expires_on"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "confidence": row["confidence"],
            "confirmed_at": row["confirmed_at"],
        }

    def state(self, workspace):
        with self.store.connection(workspace) as db:
            setting = db.execute(
                "SELECT enabled FROM user_memory_settings WHERE workspace_id=?", (workspace,),
            ).fetchone()
            rows = db.execute(
                "SELECT * FROM user_memories WHERE workspace_id=? ORDER BY updated_at DESC,id",
                (workspace,),
            ).fetchall()
            relation_rows = db.execute(
                "SELECT r.*,s.value AS source_value,t.value AS target_value "
                "FROM user_memory_relations r "
                "JOIN user_memories s ON s.id=r.source_node_id AND s.workspace_id=r.workspace_id "
                "JOIN user_memories t ON t.id=r.target_node_id AND t.workspace_id=r.workspace_id "
                "WHERE r.workspace_id=? ORDER BY r.created_at,r.id", (workspace,),
            ).fetchall()
            revision = self.store.check_revision(db, workspace, None)
        as_of = self._as_of(workspace)
        items = [self._row(row) for row in rows]
        for item in items:
            item["expired"] = bool(item["expires_on"] and item["expires_on"] < as_of)
            item["relations"] = [
                {"relation_id": row["id"], "relation": row["relation"],
                 "node_id": row["target_node_id"], "value": row["target_value"]}
                for row in relation_rows if row["source_node_id"] == item["node_id"]
            ] + [
                {"relation_id": row["id"], "relation": row["relation"],
                 "node_id": row["source_node_id"], "value": row["source_value"]}
                for row in relation_rows if row["target_node_id"] == item["node_id"]
            ]
        return {"enabled": bool(setting["enabled"]) if setting else False,
                "as_of": as_of, "items": items, "revision": str(revision),
                "personal_revision": revision,
                "notice": "保存在独立加密个人资料库。默认关闭；未启用时不会读取。"}

    def set_enabled(self, workspace, payload):
        strict_fields(payload, {"enabled", "expected_revision"}, {"enabled"})
        enabled = payload["enabled"]
        if type(enabled) is not bool:
            raise BankError("INVALID_MEMORY_SETTING", "记忆开关必须为明确的开或关。")
        now = self.bank.clock()
        with self.store.transaction(workspace, expected_revision=payload.get("expected_revision")) as db:
            db.execute(
                "INSERT INTO user_memory_settings(workspace_id,enabled,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",
                (workspace, int(enabled), now),
            )
        return self.state(workspace)

    @staticmethod
    def _value(value):
        if not isinstance(value, str):
            raise BankError("INVALID_MEMORY", "记忆内容必须是文字。")
        value = " ".join(value.split())
        if not value or len(value) > 240:
            raise BankError("INVALID_MEMORY", "记忆内容须为1至240个字符。")
        if (re.search(r"(?:密码|验证码|银行卡号|卡号|身份证|护照|手机号|手机号码|token|password)", value, re.I)
                or re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", value)
                or re.search(r"(?<!\d)\d(?:[ -]?\d){11,18}(?!\d)", value)):
            raise BankError("SENSITIVE_MEMORY", "不要在财务记忆中填写密码、验证码、证件号、手机号或银行卡号。")
        return value

    @staticmethod
    def _expiry(value, as_of):
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise BankError("INVALID_MEMORY_EXPIRY", "有效期须使用 YYYY-MM-DD 日期。")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise BankError("INVALID_MEMORY_EXPIRY", "有效期须使用 YYYY-MM-DD 日期。") from error
        if parsed.isoformat() != value:
            raise BankError("INVALID_MEMORY_EXPIRY", "有效期须使用 YYYY-MM-DD 日期。")
        if value < as_of:
            raise BankError("MEMORY_EXPIRY_IN_PAST", "有效期不能早于当前模拟日期。")
        return value

    def create(self, workspace, payload):
        strict_fields(payload, {"category", "value", "expires_on", "source_type", "source_ref",
                               "proposal_id", "confirmation_token", "expected_revision"})
        if payload.get("proposal_id") or payload.get("source_type") == "conversation_user_statement":
            if any(key in payload for key in ("category", "value", "expires_on")):
                raise BankError("INVALID_MEMORY_CONFIRMATION", "确认候选不能由客户端修改内容或有效期。")
            required = {"proposal_id", "source_ref", "confirmation_token", "expected_revision"}
            if required - set(payload) or type(payload["expected_revision"]) is not int:
                raise BankError("INVALID_MEMORY_CONFIRMATION", "请使用服务器候选及其确认凭据。")
            with self.store.transaction(workspace, expected_revision=payload["expected_revision"], event="memory_candidate_confirmed") as db:
                setting = db.execute("SELECT enabled FROM user_memory_settings WHERE workspace_id=?", (workspace,)).fetchone()
                if not setting or not setting[0]:
                    raise BankError("MEMORY_DISABLED", "记忆已关闭，候选不能确认。", 409)
                candidate = db.execute("SELECT * FROM memory_candidates WHERE workspace_id=? AND proposal_id=?", (workspace, payload["proposal_id"])).fetchone()
                import hmac
                if (not candidate or candidate["source_ref"] != payload["source_ref"]
                        or not isinstance(payload["confirmation_token"], str)
                        or not hmac.compare_digest(candidate["confirmation_token"], payload["confirmation_token"])
                        or candidate["expected_revision"] != payload["expected_revision"]
                        or candidate["status"] != "candidate" or candidate["expires_at"] <= self.bank.clock()):
                    raise BankError("INVALID_MEMORY_CONFIRMATION", "候选不属于本账户、已经失效或确认凭据不符。", 409)
                source = db.execute("SELECT text FROM conversation_sources WHERE workspace_id=? AND source_ref=?", (workspace,candidate["source_ref"])).fetchone()
                if not source or candidate["value"] not in source[0] or not explicit_self_statement(candidate["value"]):
                    raise BankError("INVALID_MEMORY_SOURCE", "候选缺少可靠的本人原话依据。", 409)
                value = self._value(candidate["value"])
                node_id, now = uuid.uuid4().hex, self.bank.clock()
                db.execute("INSERT INTO user_memories(id,workspace_id,category,value,origin,user_confirmed,created_at,updated_at,expires_on,source_type,source_ref,confidence,confirmed_at,record_revision) VALUES(?,?,?,?, 'user_declared',1,?,?,?,'conversation_user_statement',?,1,?,?)",
                           (node_id, workspace, candidate["category"], self.store.protect(value), now, now, candidate["expires_on"],candidate["source_ref"],now,payload["expected_revision"]+1))
                db.execute("UPDATE memory_candidates SET status='confirmed' WHERE workspace_id=? AND proposal_id=?", (workspace,payload["proposal_id"]))
            return self.state(workspace)
        if not {"category","value"} <= set(payload):
            raise BankError("INVALID_FIELDS", "请提供记忆类别和内容。")
        category = payload["category"]
        if not isinstance(category,str) or category not in CATEGORIES:
            raise BankError("INVALID_MEMORY_CATEGORY", "请选择允许保存的财务记忆类别。")
        if payload.get("source_type", "user_entered") != "user_entered" or payload.get("source_ref") is not None:
            raise BankError("INVALID_MEMORY_SOURCE", "手动填写的记忆不接受外部来源标识。")
        value = self._value(payload["value"])
        expires_on = self._expiry(payload.get("expires_on"), self._as_of(workspace))
        node_id, now = uuid.uuid4().hex, self.bank.clock()
        with self.store.transaction(workspace, expected_revision=payload.get("expected_revision"), event="memory_created",object_id=node_id) as db:
            revision = self.store.check_revision(db,workspace,None)+1
            db.execute("INSERT INTO user_memories(id,workspace_id,category,value,origin,user_confirmed,created_at,updated_at,expires_on,source_type,source_ref,confidence,confirmed_at,record_revision) VALUES(?,?,?,?, 'user_declared',1,?,?,?,'user_entered',NULL,1,?,?)",
                       (node_id,workspace,category,self.store.protect(value),now,now,expires_on,now,revision))
        return self.state(workspace)

    def correct(self, workspace, node_id, payload):
        strict_fields(payload, {"value", "expires_on", "expected_revision"}, {"value"})
        value, now = self._value(payload["value"]), self.bank.clock()
        expires_on = self._expiry(payload["expires_on"], self._as_of(workspace)) if "expires_on" in payload else None
        with self.store.transaction(workspace, expected_revision=payload.get("expected_revision")) as db:
            row = db.execute(
                "SELECT category,source_ref FROM user_memories WHERE id=? AND workspace_id=?", (node_id, workspace),
            ).fetchone()
            if not row:
                raise BankError("NOT_FOUND", "这条财务记忆不存在。", 404)
            if "expires_on" in payload:
                db.execute("UPDATE user_memories SET value=?,updated_at=?,expires_on=? WHERE id=? AND workspace_id=?",
                           (self.store.protect(value), now, expires_on, node_id, workspace))
            else:
                db.execute("UPDATE user_memories SET value=?,updated_at=? WHERE id=? AND workspace_id=?",
                           (self.store.protect(value), now, node_id, workspace))
            details = {"node_id": node_id, "category": row["category"]}
            if "expires_on" in payload:
                details["expires_on"] = expires_on
            db.execute("UPDATE user_memories SET user_confirmed=1,source_type='user_entered',source_ref=NULL,confirmed_at=?,record_revision=? WHERE id=? AND workspace_id=?", (now,self.store.check_revision(db,workspace,None)+1,node_id,workspace))
            self.store.discard_unused_source(db, workspace, row['source_ref'])
        return self.state(workspace)

    def delete(self, workspace, node_id, payload):
        strict_fields(payload, {"expected_revision"})
        with self.store.transaction(workspace, expected_revision=payload.get("expected_revision")) as db:
            row = db.execute(
                "SELECT category,source_ref FROM user_memories WHERE id=? AND workspace_id=?", (node_id, workspace),
            ).fetchone()
            if not row:
                raise BankError("NOT_FOUND", "这条财务记忆不存在。", 404)
            db.execute("DELETE FROM user_memories WHERE id=? AND workspace_id=?", (node_id, workspace))
            self.store.discard_unused_source(db, workspace, row['source_ref'])

        return self.state(workspace)

    def read_relevant(self, workspace, question, *, workflow):
        categories = _CATEGORY_RELEVANCE.get(workflow, set())
        if not categories:
            return MemoryGraphContext()
        with self.store.connection(workspace) as db:
            setting = db.execute(
                "SELECT enabled FROM user_memory_settings WHERE workspace_id=?", (workspace,),
            ).fetchone()
            if not setting or not setting["enabled"]:
                return MemoryGraphContext()
            as_of = self._as_of(workspace)
            rows = db.execute(
                "SELECT * FROM user_memories WHERE workspace_id=? AND user_confirmed=1 "
                "AND (expires_on IS NULL OR expires_on>=?) ORDER BY updated_at DESC,id",
                (workspace, as_of),
            ).fetchall()
            relation_rows = db.execute('SELECT * FROM user_memory_relations WHERE workspace_id=? ORDER BY created_at,id',(workspace,)).fetchall()
        # 问题先定种子，再沿本人确认关系展开两跳，限制上下文而不是全量灌入。
        def terms(s):
            return set(re.findall(r'[a-z0-9_]{2,}',s.lower()))|{s[i:i+2] for i in range(len(s)-1) if re.fullmatch(r'[\u4e00-\u9fff]{2}',s[i:i+2])}
        query_terms=terms(question or '')
        relevant=[r for r in rows if r['category'] in categories]
        ranked=sorted(relevant,key=lambda r:len(query_terms & terms(r['value'])),reverse=True)
        matched=[r for r in ranked if query_terms & terms(r['value'])]
        selected={r['id'] for r in (matched or ranked)[:4]};known={r['id'] for r in rows}
        frontier=set(selected)
        for _ in range(2):
            following=set()
            for r in relation_rows:
                if len(selected|following)>=12:break
                a,b=r['source_node_id'],r['target_node_id']
                if a in frontier and b in known and b not in selected:following.add(b)
                if b in frontier and a in known and a not in selected:following.add(a)
            selected.update(following);frontier=following
        rows=[r for r in rows if r['id'] in selected]
        nodes = tuple(MemoryNode(
            node_id=row["id"], category=row["category"], value=row["value"],
            origin=MemoryOrigin.USER_DECLARED, evidence_ids=(row["id"],),
            confidence=float(row["confidence"]), observed_at=str(row["updated_at"]), user_confirmed=True,
            expires_at=row["expires_on"], source_type=row["source_type"], source_ref=row["source_ref"],
        ) for row in rows)
        node_ids = {node.node_id for node in nodes}
        with self.store.connection(workspace) as db:
            relation_rows = db.execute(
                "SELECT id,source_node_id,target_node_id,relation FROM user_memory_relations "
                "WHERE workspace_id=? ORDER BY created_at,id", (workspace,),
            ).fetchall()
        relations = tuple(MemoryRelation(
            source_node_id=row["source_node_id"], relation=row["relation"],
            target_node_id=row["target_node_id"],
            evidence_ids=(row["source_node_id"], row["target_node_id"]),
        ) for row in relation_rows
            if row["source_node_id"] in node_ids and row["target_node_id"] in node_ids)
        return MemoryGraphContext(nodes=nodes, relations=relations)

    def add_relation(self, workspace, node_id, payload):
        strict_fields(payload, {"target_node_id", "relation", "expected_revision"}, {"target_node_id", "relation"})
        target_id, relation = payload["target_node_id"], payload["relation"]
        allowed = {"supports", "conflicts_with", "depends_on", "related_to"}
        if (not isinstance(target_id, str) or not re.fullmatch(r"[a-f0-9]{32}", target_id)
                or not isinstance(relation, str) or relation not in allowed or target_id == node_id):
            raise BankError("INVALID_MEMORY_RELATION", "记忆关联类型或目标无效。")
        relation_id, now = uuid.uuid4().hex, self.bank.clock()
        with self.store.transaction(workspace, expected_revision=payload.get("expected_revision")) as db:
            ids = {row[0] for row in db.execute(
                "SELECT id FROM user_memories WHERE workspace_id=? AND id IN (?,?)",
                (workspace, node_id, target_id),
            ).fetchall()}
            if ids != {node_id, target_id}:
                raise BankError("NOT_FOUND", "关联的财务记忆不存在。", 404)
            try:
                db.execute(
                    "INSERT INTO user_memory_relations(id,workspace_id,source_node_id,target_node_id,relation,created_at) "
                    "VALUES(?,?,?,?,?,?)", (relation_id, workspace, node_id, target_id, relation, now),
                )
            except Exception as error:
                if "UNIQUE" in str(error).upper():
                    raise BankError("MEMORY_RELATION_EXISTS", "这条记忆关联已经存在。", 409) from None
                raise
        return self.state(workspace)

    def delete_relation(self, workspace, relation_id, expected_revision=None):
        with self.store.transaction(workspace, expected_revision=expected_revision) as db:
            row = db.execute(
                "SELECT source_node_id,target_node_id,relation FROM user_memory_relations "
                "WHERE id=? AND workspace_id=?", (relation_id, workspace),
            ).fetchone()
            if not row:
                raise BankError("NOT_FOUND", "这条记忆关联不存在。", 404)
            db.execute("DELETE FROM user_memory_relations WHERE id=? AND workspace_id=?",
                       (relation_id, workspace))
        return self.state(workspace)


def propose_conversation_memories(text, source_ref):
    """从明确表达的长期偏好中提出候选；只返回候选，不自动写入存储。"""
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        return []
    if not explicit_self_statement(text):
        return []
    patterns = (
        ("planned_commitment", r"((?:我(?:计划|准备|打算)?\s*)?(?:明年|今年|下月|下个月|未来\d{1,2}个月|\d{1,2}月).{0,50}(?:学费|房租|医疗费|旅行|缴费|支出).{0,40})"),
        ("goal", r"((?:我的长期目标是|长期目标是|我的储蓄目标是|财务目标是).{2,100})"),
        ("liquidity_preference", r"((?:长期偏好|请长期保留|以后请帮我保留|我希望始终留有).{2,100})"),
        ("product_interest", r"((?:我长期关注|以后优先给我介绍|我长期偏好).{2,100})"),
        ("knowledge_preference", r"((?:以后请始终|以后请|今后请).{2,100}(?:解释|说明|对比|回答).{0,40})"),
    )
    found = []
    for category, pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        value = " ".join(match.group(1).strip(" ，,。；;\n\t").split())
        if not explicit_self_statement(value):
            continue
        try:
            value = UserMemoryStore._value(value)
        except BankError:
            continue
        proposal_id = hashlib.sha256(f"{source_ref}:{category}:{value}".encode()).hexdigest()[:24]
        found.append({"proposal_id": proposal_id, "category": category, "value": value,
                      "source_type": "conversation_user_statement", "source_ref": source_ref,
                      "notice": "这是未保存的候选。只有你点击保存后，才会加入本机财务记忆。"})
    return found
