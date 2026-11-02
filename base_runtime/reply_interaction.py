"""受审对话中的确认和单选组件；选择本身不授予金融操作权限。

模型仅提供问题和可见选项。响应身份、有效期和固定确认/拒绝选项由
本地服务补齐。银行操作、日历保存和记忆授权继续使用各自的确认合同。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import unicodedata

from simulated_bank import BankError


SCHEMA = 'navren.reply-interaction.v1'
INTERACTION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'kind': {'type': 'string', 'enum': ['confirmation', 'single_choice']},
        'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 240},
        'options': {'type': 'array', 'minItems': 2, 'maxItems': 4,
            'items': {'type': 'object', 'additionalProperties': False,
                'properties': {
                    'id': {'type': 'string', 'minLength': 1, 'maxLength': 32,
                           'pattern': r'[a-z][a-z0-9_-]{0,31}'},
                    'label': {'type': 'string', 'minLength': 1, 'maxLength': 48}},
                'required': ['id', 'label']}}},
    'required': ['kind', 'prompt']}


def _invalid():
    return BankError('INVALID_REPLY_INTERACTION', '交互问题或选项格式不完整，请重新准备。', 422)


def _plain(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise _invalid()
    value = unicodedata.normalize('NFKC', value)
    if len(value) > limit:
        raise _invalid()
    # 单行纯文本；不容许隐藏控制字符、链接或模型提供的页面标记。
    if any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value):
        raise _invalid()
    if re.search(r'https?://|javascript:|data:|</?[a-z][^>]*>', value, re.I):
        raise _invalid()
    return value.strip()


def validate_interaction(spec):
    """在独立审核之前调用；返回规范化的同一份审核输入。"""
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise _invalid()
    kind = spec.get('kind')
    if kind not in ('confirmation', 'single_choice'):
        raise _invalid()
    required = {'kind', 'prompt'} if kind == 'confirmation' else {'kind', 'prompt', 'options'}
    if set(spec) != required:
        raise _invalid()
    normalized = {'kind': kind, 'prompt': _plain(spec['prompt'], 240)}
    if kind == 'single_choice':
        options = spec['options']
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            raise _invalid()
        ids, labels, cleaned = set(), set(), []
        for option in options:
            if not isinstance(option, dict) or set(option) != {'id', 'label'}:
                raise _invalid()
            oid = option['id']
            if not isinstance(oid, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', oid):
                raise _invalid()
            label = _plain(option['label'], 48)
            unique = unicodedata.normalize('NFKC', label).casefold()
            if oid in ids or unique in labels:
                raise _invalid()
            ids.add(oid); labels.add(unique)
            cleaned.append({'id': oid, 'label': label})
        normalized['options'] = cleaned
    return normalized


def build_reply_interaction(workspace_id, response_id, privacy_revision, spec,
                            *, operation_ids=(), now=None):
    """仅供审核通过分支调用，构建本轮对话选择的受控展示包装。"""
    normalized = validate_interaction(spec)
    if normalized is None:
        return None
    if operation_ids:
        raise BankError('INTERACTION_DRAFT_CONFLICT', '操作草案需使用原确认卡，不能由对话选择代替。', 422)
    for value in (workspace_id, response_id):
        if not isinstance(value, str) or not value or len(value) > 200:
            raise _invalid()
    if type(privacy_revision) is not int or privacy_revision < 0:
        raise _invalid()
    timestamp = datetime.now(timezone.utc) if now is None else now
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise _invalid()
    timestamp = timestamp.astimezone(timezone.utc)
    options = normalized.get('options') or [
        {'id': 'confirm', 'label': '确认'}, {'id': 'reject', 'label': '拒绝'}]
    binding = json.dumps([SCHEMA, workspace_id, response_id, privacy_revision,
                          normalized], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {
        'schema_version': SCHEMA, 'status': 'awaiting_input',
        'workspace_id': workspace_id, 'response_id': response_id,
        'privacy_revision': privacy_revision,
        'interaction_id': 'choice-' + hashlib.sha256(binding.encode()).hexdigest()[:24],
        'expires_at': (timestamp + timedelta(minutes=10)).isoformat(),
        'kind': normalized['kind'], 'prompt': normalized['prompt'], 'options': options,
        'scope': 'conversation_only'}


def legacy_followups_interaction(followups, *, language):
    """Map one complete ordinary-question list; never filter or truncate it.

    This is the existing interaction display canonicalization (NFKC), not a
    choice of intent, financial parameters, user consent or an execution action.
    Ineligible legacy lists remain legacy at the caller.
    """
    prompts={'zh':'接下来想了解哪一项？','en':'What would you like to explore next?'}
    if type(language)is not str or language not in prompts:return None
    if (type(followups)is not list or not 2<=len(followups)<=3
            or any(type(item)is not str for item in followups)):
        return None
    try:
        return validate_interaction({'kind':'single_choice',
            'prompt':prompts[language],
            'options':[{'id':'followup_'+str(index+1),'label':text}
                for index,text in enumerate(followups)]})
    except BankError:
        return None
