"""原操作视图的只读脱敏投影；不认证、不发短信、不签发/消费证明。"""
import hashlib
import json
import math
import re

CONTRACT = 'navren.operation-mfa.evidence/1'
EVENTS = ('permission_prepared', 'sms_challenge_sent', 'sms_proof_verified',
          'sms_proof_consumed', 'permission_executed', 'operation_executed',
          'permission_rejected', 'permission_attempt_failed')


def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _digest(value):
    return value if isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) else None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def operation_mfa_evidence(db, workspace, operation, *, now, provider, status):
    """只可从既有已鉴权 operation_view 调用，workspace 不来自新增客户端参数。

    不读取 OTP、手机号、Cookie、证明 token、短信正文或任何密钥字段。
    complete 仅表示数据库中渠道/绑定/一次消费/执行链一致，真人见证单独签认。
    """
    from simulated_bank.service import BankError
    row = db.execute('''SELECT id,action,permission,state,confirmation_digest,version,expires_at,
        json_extract(payload,'$.account_id') AS account_id,
        json_extract(payload,'$.card_id') AS card_id,
        json_extract(payload,'$.beneficiary_id') AS beneficiary_id,
        json_extract(payload,'$.product_id') AS product_id
        FROM operations WHERE workspace_id=? AND id=?''', (workspace, operation)).fetchone()
    if row is None:
        raise BankError('NOT_FOUND', '当前工作区没有该操作。', 404)
    binding = db.execute('''SELECT policy,grade,business_day,summary_hash
        FROM permission_bindings WHERE workspace_id=? AND operation_id=?''',
        (workspace, operation)).fetchone()
    proof = db.execute('''SELECT summary_hash,challenge_id,provider_reference_hash,source,
        issued_at,expires_at,consumed_at FROM permission_sms_proofs
        WHERE workspace_id=? AND operation_id=?
        ORDER BY (consumed_at IS NOT NULL) DESC,issued_at DESC LIMIT 1''',
        (workspace, operation)).fetchone()
    counts = db.execute('''SELECT COUNT(*) AS issued,
        COALESCE(SUM(consumed_at IS NOT NULL),0) AS consumed
        FROM permission_sms_proofs WHERE workspace_id=? AND operation_id=?''',
        (workspace, operation)).fetchone()
    audit = [dict(a) for a in db.execute('''SELECT id,event,
        json_extract(details,'$.summary_hash') AS summary_hash,
        json_extract(details,'$.provider_reference_hash') AS provider_reference_hash,
        json_extract(details,'$.challenge_id') AS challenge_id,
        json_extract(details,'$.source') AS source,
        json_extract(details,'$.mfa_source') AS mfa_source,
        json_extract(details,'$.state') AS state
        FROM audit WHERE workspace_id=? AND operation_id=? AND event IN ('''
        + ','.join('?' for _ in EVENTS) + ') ORDER BY id', (workspace, operation, *EVENTS))]
    challenge = None
    if proof:
        challenge = db.execute('''SELECT id,state,expires_at,
            json_extract(binding,'$.summary_sha256') AS summary_sha256,
            json_extract(binding,'$.subject')=? AS subject_matches,
            json_extract(binding,'$.operation_id')=? AS operation_matches
            FROM permission_sms_challenges WHERE workspace_id=? AND operation_id=? AND id=?''',
            (workspace, operation, workspace, operation, proof['challenge_id'])).fetchone()
    summary = _digest(row['confirmation_digest'])
    binding_ok = bool(binding and summary and binding['summary_hash'] == summary
                      and binding['grade'] == row['permission']
                      and isinstance(status, dict) and binding['policy'] == status.get('policy'))
    source = getattr(provider, 'source', None)
    safe_source = source if source in ('provider', 'test_double') else 'unavailable'
    live_status = bool(isinstance(status, dict) and status.get('operation_id') == operation
                       and status.get('grade') == 'red' and status.get('channel') == 'sms'
                       and status.get('channel_configured') is True
                       and status.get('financial_write_locked') is False)
    linked = bool(proof and binding_ok and challenge and challenge['state'] == 'verified'
                  and challenge['subject_matches'] == 1 and challenge['operation_matches'] == 1
                  and challenge['summary_sha256'] == summary and proof['summary_hash'] == summary
                  and _digest(proof['provider_reference_hash'])
                  and _finite(proof['issued_at']) and _finite(proof['expires_at'])
                  and _finite(challenge['expires_at']) and _finite(row['expires_at'])
                  and proof['issued_at'] < proof['expires_at'] <= challenge['expires_at'] <= row['expires_at'])
    consumed = bool(linked and _finite(proof['consumed_at'])
                    and proof['issued_at'] <= proof['consumed_at'] < proof['expires_at']
                    and counts['consumed'] == 1)
    matching = {}
    if linked:
        predicates = {
            'permission_prepared': lambda a: a['summary_hash'] == summary,
            'sms_challenge_sent': lambda a: a['summary_hash'] == summary
                and a['challenge_id'] == proof['challenge_id'] and a['source'] == proof['source'],
            'sms_proof_verified': lambda a: a['provider_reference_hash'] == proof['provider_reference_hash']
                and a['challenge_id'] == proof['challenge_id'] and a['source'] == proof['source'],
            'sms_proof_consumed': lambda a: a['provider_reference_hash'] == proof['provider_reference_hash']
                and a['source'] == proof['source'],
            'permission_executed': lambda a: a['mfa_source'] == proof['source'],
            'operation_executed': lambda a: a['state'] == row['state'],
        }
        matching = {event: [a['id'] for a in audit if a['event'] == event and predicate(a)]
                    for event, predicate in predicates.items()}
    ordered = bool(matching and all(len(v) == 1 for v in matching.values())
                   and [v[0] for v in matching.values()] == sorted(v[0] for v in matching.values()))
    complete = bool(row['permission'] == 'red' and safe_source == 'provider' and live_status
                    and proof and proof['source'] == 'provider' and consumed and ordered
                    and row['state'] in ('succeeded', 'pending_settlement', 'settled'))
    target = {key: row[key] for key in ('action', 'account_id', 'card_id', 'beneficiary_id', 'product_id')
              if row[key] is not None}
    return {
        'contract': CONTRACT, 'operation_id': operation, 'action': row['action'],
        'state': row['state'], 'grade': row['permission'], 'operation_version': row['version'],
        'subject_sha256': _sha(workspace), 'target_sha256': _sha(target),
        'summary_sha256': summary, 'binding_matches': binding_ok,
        'policy_version': binding['policy'] if binding else None,
        'business_day': binding['business_day'] if binding else None,
        'provider': {'class': type(provider).__module__ + '.' + type(provider).__qualname__
                     if provider is not None else None, 'source': safe_source,
                     'channel_currently_available': live_status},
        'proof': {'source': proof['source'] if proof and proof['source'] in ('provider', 'test_double') else None,
                  'provider_reference_sha256': _digest(proof['provider_reference_hash']) if proof else None,
                  'issued_at': proof['issued_at'] if proof and _finite(proof['issued_at']) else None,
                  'expires_at': proof['expires_at'] if proof and _finite(proof['expires_at']) else None,
                  'consumed_at': proof['consumed_at'] if proof and _finite(proof['consumed_at']) else None,
                  'live_unconsumed': bool(linked and proof['consumed_at'] is None
                      and _finite(now) and proof['issued_at'] <= now < proof['expires_at']),
                  'issued_count': counts['issued'], 'consumed_count': counts['consumed'],
                  'binding_matches': linked},
        'audit_event_ids': {event: [a['id'] for a in audit if a['event'] == event] for event in EVENTS},
        'linked_audit_event_ids': matching, 'audit_chain_ordered': ordered,
        'provider_chain_complete': complete,
        'real_human_witnessed': False, 'overall_accepted': False,
        'notice': '只读渠道证据；真人短信见证和当前入口整体验收须另行签认。',
    }
