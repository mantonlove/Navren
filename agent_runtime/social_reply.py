"""确定性的独立寒暄回复；不读取历史、金融资料或模型。"""
from __future__ import annotations

import re
import unicodedata


_TRAILING_PUNCTUATION = re.compile(r"[!！?？。.,，、…~～;；:：]+$")
_RULES = {
    "zh": {
        "greeting": frozenset(("你好", "您好", "嗨", "哈喽", "早上好", "上午好", "中午好", "下午好", "晚上好")),
        "thanks": frozenset(("谢谢", "谢谢你", "多谢", "感谢", "感谢你", "辛苦了")),
        "farewell": frozenset(("再见", "拜拜", "回头见", "晚安")),
    },
    "en": {
        "greeting": frozenset(("hi", "hello", "hey", "good morning", "good afternoon", "good evening")),
        "thanks": frozenset(("thanks", "thank you", "thx")),
        "farewell": frozenset(("bye", "goodbye", "see you", "good night")),
    },
}
_REPLIES = {
    ("zh", "greeting"): "你好！有什么我可以帮你？",
    ("zh", "thanks"): "不客气，有需要随时告诉我。",
    ("zh", "farewell"): "再见，欢迎随时回来。",
    ("en", "greeting"): "Hello! What can I help you with?",
    ("en", "thanks"): "You’re welcome. I’m here if you need anything else.",
    ("en", "farewell"): "Goodbye. You’re welcome back anytime.",
}


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold().strip()
    value = _TRAILING_PUNCTUATION.sub("", value).strip()
    return value


def resolve_social_reply(text, *, language="zh", has_attachment=False,
                         has_selected_context=False):
    """只匹配整条输入中的固定寒暄；上下文或附件会关闭此路由。"""
    if (not isinstance(text, str) or has_attachment or has_selected_context
            or not isinstance(language,str) or language not in _RULES):
        return None
    normalized = _normalize(text)
    if not normalized:
        return None
    for catalog in _RULES.values():
        for rule_id, phrases in catalog.items():
            if normalized in phrases:
                return {"rule_id": rule_id, "language": language,
                        "reply": _REPLIES[(language, rule_id)]}
    return None


def build_social_reply_result(workspace_id, request_id, match):
    """构造固定的常规结果形状，不签发任何金融或模型审核凭证。"""
    if (not isinstance(workspace_id, str) or not 1 <= len(workspace_id) <= 160
            or not isinstance(request_id, str) or not 1 <= len(request_id) <= 160
            or not isinstance(match, dict)):
        raise ValueError("invalid_social_reply_binding")
    rule_id, language = match.get("rule_id"), match.get("language")
    if (not isinstance(language,str) or language not in _RULES
            or not isinstance(rule_id,str) or rule_id not in _RULES[language]
            or match.get("reply") != _REPLIES[(language, rule_id)]):
        raise ValueError("invalid_social_reply_rule")
    return {
        "bank": True,
        "environment": "simulation",
        "workspace_id": workspace_id,
        "bank_response_id": request_id,
        "reply": _REPLIES[(language, rule_id)],
        "products": [],
        "bank_actions": [],
        "follow_up_questions": [],
        "widgets": [],
        "market_sources": [],
        "capabilities_used": [],
        "answer_contract": {
            "kind": "social_reply", "status": "not_required",
            "source": "local_rule", "rule_id": rule_id,
        },
        "agent_review": {"status": "not_required", "reason": "local_social_reply"},
        "agent_runtime": {
            "architecture": "local_rule", "model_calls": 0,
            "tool_calls": 0, "review_calls": 0,
        },
        "social_reply": {
            "status": "not_required", "rule_id": rule_id, "language": language,
        },
    }


def validate_social_reply_result(result):
    """只接受匹配本地固定模板的纯寒暄结果及服务器版本号。"""
    required = {
        "bank", "environment", "workspace_id", "bank_response_id", "reply",
        "products", "bank_actions", "follow_up_questions", "widgets",
        "market_sources", "capabilities_used", "answer_contract", "agent_review",
        "agent_runtime", "social_reply",
    }
    safe_server_metadata = {"personal_revision", "privacy_revision"}
    if not isinstance(result, dict) or not required.issubset(result):
        raise ValueError("invalid_social_reply_result")
    if set(result) - required - safe_server_metadata:
        raise ValueError("unexpected_social_reply_fields")
    for key in safe_server_metadata & set(result):
        if type(result[key]) is not int or result[key] < 0:
            raise ValueError("invalid_social_reply_revision")
    rule = result.get("social_reply")
    if not isinstance(rule, dict) or set(rule) != {"status", "rule_id", "language"}:
        raise ValueError("invalid_social_reply_status")
    language, rule_id = rule.get("language"), rule.get("rule_id")
    if (not isinstance(language,str) or language not in _RULES
            or not isinstance(rule_id,str) or rule_id not in _RULES[language]):
        raise ValueError("invalid_social_reply_rule")
    expected = {
        "bank": True,
        "environment": "simulation",
        "reply": _REPLIES[(language, rule_id)],
        "products": [],
        "bank_actions": [],
        "follow_up_questions": [],
        "widgets": [],
        "market_sources": [],
        "capabilities_used": [],
        "answer_contract": {
            "kind": "social_reply", "status": "not_required",
            "source": "local_rule", "rule_id": rule_id,
        },
        "agent_review": {"status": "not_required", "reason": "local_social_reply"},
        "agent_runtime": {
            "architecture": "local_rule", "model_calls": 0,
            "tool_calls": 0, "review_calls": 0,
        },
        "social_reply": {"status": "not_required", "rule_id": rule_id,
                         "language": language},
    }
    if any(result.get(key) != value for key, value in expected.items()):
        raise ValueError("social_reply_contract_mismatch")
    if (not isinstance(result.get("workspace_id"), str)
            or not 1 <= len(result["workspace_id"]) <= 160
            or not isinstance(result.get("bank_response_id"), str)
            or not 1 <= len(result["bank_response_id"]) <= 160):
        raise ValueError("invalid_social_reply_binding")
    return True
