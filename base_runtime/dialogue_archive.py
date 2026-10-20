"""注册实例的只读聊天分页候选；未正式采用，不发行金融授权或第二份助手正文。"""
from collections import OrderedDict
from contextlib import contextmanager, ExitStack, closing
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import secrets
import threading

from financial_metrics import fingerprint
from simulated_bank import BankError
from user_memory import privacy_revision
from agent_runtime.final_proof import CONTRACT as FINAL_PROOF, strict_equal
from agent_runtime import social_reply
from base_runtime.approved_response_store import CONTRACT as APPROVED, COMMIT
from base_runtime.historical_source_attestation import HistoricalSourceAttestor
from registered_instances import registered_id, persona_id


CONTRACT = 'navren.registered-dialogue-archive/2'
USER_CONTRACT = 'navren.archived-user-turn/2'
CURSOR_CONTRACT = 'navren.dialogue-archive-cursor/2'
ATTESTATION = 'navren.dialogue-publication-attestation/1'
ORIGINAL_CODEC = 'navren.approved-original-seal/1'
_ATTESTATION_FIELDS = {'contract', 'codec', 'binding', 'archive_epoch', 'response_id',
    'original_contract', 'issuer_code', 'original_envelope_hash', 'publication_marker_hash',
    'saved_widget_hash', 'save_generation', 'original_response_hash', 'source_attestation',
    'issued_at', 'rule_validation'}
LOCAL_ORIGINAL = 'navren.local-rule-response/1'
CODE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MODEL_CONTEXT_LIMIT = 16
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
_BINDING_FIELDS = {'registered_user_id', 'workspace_id', 'persona_id'}


def fail(code='DIALOGUE_ARCHIVE_INVALID', status=409):
    raise BankError(code, '聊天归档、当前实例或资料权限已变化，请重新读取。', status)


@dataclass(frozen=True)
class _Capture:
    reader: object
    nonce: str


class DialogueArchiveReader:
    """binding_reader(w, bank_db) 只能由 Base 可信装配注入，不来自 HTTP 或模型。

    它必须查当前欢迎认证会话及注册实例映射，严格返回 registered_user_id、
    workspace_id、persona_id；本类另外调用原 _session_binding 与隐私守卫。
    w 也是服务端 identity(handler) 的结果，HTTP 读参只有 cursor/limit。
    """

    def __init__(self, gateway, *, binding_reader):
        if not callable(binding_reader):
            raise TypeError('trusted_registered_binding_reader_required')
        self.g, self.store, self.p = gateway, gateway.personal_data, gateway.presentations
        self.approved = gateway.approved_responses
        self.binding_reader = binding_reader
        self.sources = HistoricalSourceAttestor(gateway)
        self._captures = OrderedDict()
        self._capture_lock = threading.RLock()

    def _binding(self, w, db):
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != CODE:
            fail('DIALOGUE_ARCHIVE_RESTART_REQUIRED', 503)
        session = self.p._session_binding(w, db, required=True)
        bound = self.binding_reader(w, db)
        if (type(bound) is not dict or set(bound) != _BINDING_FIELDS
                or bound['workspace_id'] != w
                or any(type(v) is not str or not 1 <= len(v) <= 160 for v in bound.values())):
            fail('REGISTERED_DIALOGUE_BINDING_REQUIRED', 403)
        # Base 的实际 canonical UUID4 与四画像白名单；不建立另一份名称映射。
        registered_id(bound['registered_user_id'])
        persona_id(bound['persona_id'])
        return {**bound, 'privacy_revision': privacy_revision(db, w),
                'personal_revision': self.store.revision(w), 'archive_epoch': self.store.archive_epoch(w),
                'session': deepcopy(session)}

    def archive_user_turn(self, w, request_id, text, *, expected_personal_revision, expected_privacy_revision,
                          references=None, attachments=None, media_ids=None):
        """原银行锁内复验可信注册绑定，再交给私库唯一 writer；不写助手正文。"""
        writer = getattr(self.store, 'archive_user_turn_guarded', None)
        if not callable(writer):
            fail('DIALOGUE_ARCHIVE_STORAGE_REQUIRED', 503)
        with self.p._publication() as db:
            bound = self._binding(w, db)
            if (type(expected_personal_revision) is not int or expected_personal_revision < 0
                    or type(expected_privacy_revision) is not int or expected_privacy_revision < 0
                    or bound['personal_revision'] != expected_personal_revision
                    or bound['privacy_revision'] != expected_privacy_revision):
                fail('DIALOGUE_ARCHIVE_USER_VERSION_CHANGED')
            result = writer(w, request_id, text, bound['registered_user_id'], bank_db=db,
                            expected_revision=expected_personal_revision,
                            expected_privacy=expected_privacy_revision,
                            references=references, attachments=attachments, media_ids=media_ids)
            if result is False or result is None:
                fail('DIALOGUE_ARCHIVE_USER_SAVE_FAILED')
            return result

    def _cursor(self, scope, *, upper, before):
        body = {'contract': CURSOR_CONTRACT, 'code': CODE,
                'binding': {k: scope[k] for k in (*sorted(_BINDING_FIELDS), 'archive_epoch', 'privacy_revision', 'session')},
                'upper_sequence': upper, 'before_sequence': before}
        return self.store.protect(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                             separators=(',', ':'), allow_nan=False))

    def _open_cursor(self, cursor, scope):
        if type(cursor) is not str or not 1 <= len(cursor) <= 4096 or not cursor.startswith('pd1:'):
            fail('DIALOGUE_ARCHIVE_CURSOR_INVALID', 422)
        try:
            value = json.loads(self.store._decode(cursor))
        except (BankError, TypeError, ValueError):
            fail('DIALOGUE_ARCHIVE_CURSOR_INVALID', 422)
        binding = {k: scope[k] for k in (*sorted(_BINDING_FIELDS), 'archive_epoch', 'privacy_revision', 'session')}
        if (type(value) is not dict or set(value) != {'contract', 'code', 'binding', 'upper_sequence', 'before_sequence'}
                or value['contract'] != CURSOR_CONTRACT or value['code'] != CODE
                or not strict_equal(value['binding'], binding)
                or type(value['upper_sequence']) is not int or value['upper_sequence'] < 0
                or type(value['before_sequence']) is not int or not 0 < value['before_sequence'] <= value['upper_sequence'] + 1):
            fail('DIALOGUE_ARCHIVE_CURSOR_STALE', 409)
        return value['upper_sequence'], value['before_sequence']

    @staticmethod
    def _table_ready(private):
        if private.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_user_turn_archive'").fetchone() is None:
            fail('DIALOGUE_ARCHIVE_STORAGE_REQUIRED', 503)

    def _user(self, row, scope, bank_db=None):
        """加密包内也绑定主体/工作区/请求/序号/版本，不能移动密文行认领原话。"""
        required = {'contract', 'registered_user_id', 'workspace_id', 'request_id', 'archive_sequence',
                    'content', 'personal_revision', 'privacy_revision', 'received_at',
                    'archive_epoch', 'references', 'attachments'}
        cipher = row['user_text_cipher']
        if type(cipher) is not str or not cipher.startswith('pd1:'):
            fail('DIALOGUE_ARCHIVE_USER_CHANGED')
        try:
            packet = json.loads(self.store._decode(cipher))
        except (BankError, TypeError, ValueError):
            fail('DIALOGUE_ARCHIVE_USER_CHANGED')
        if (type(packet) is not dict or set(packet) != required or packet['contract'] != USER_CONTRACT
                or packet['registered_user_id'] != scope['registered_user_id']
                or packet['workspace_id'] != scope['workspace_id']
                or any(not strict_equal(packet[k], row[k]) for k in
                       ('workspace_id', 'request_id', 'archive_sequence', 'personal_revision', 'privacy_revision', 'received_at', 'archive_epoch'))
                or type(packet['content']) is not str or len(packet['content']) > 8000
                or not (packet['content'] or packet['references'] or packet['attachments'])
                or type(packet['archive_epoch']) is not int or packet['archive_epoch'] != scope['archive_epoch']
                or type(packet['privacy_revision']) is not int
                or not 0 <= packet['privacy_revision'] <= scope['privacy_revision']
                or type(packet['personal_revision']) is not int
                or not 0 <= packet['personal_revision'] <= scope['personal_revision']):
            fail('DIALOGUE_ARCHIVE_USER_CHANGED')
        refs, _ = self.store._archive_reference_metadata(packet['references'], [])
        if type(packet['attachments']) is not list:
            fail('DIALOGUE_ARCHIVE_USER_CHANGED')
        legacy, originals = [], []
        for attachment in packet['attachments']:
            if attachment == {'kind': 'image', 'storage_status': 'not_archived'}:
                legacy.append(attachment)
            elif type(attachment) is dict and attachment.get('contract') == 'navren.chat-media/1':
                originals.append(attachment)
            else:
                fail('DIALOGUE_ARCHIVE_USER_CHANGED')
        trusted = self._media_for_turn(scope['workspace_id'], row['request_id'], 'user', bank_db)
        original_ids = {x.get('media_id') for x in originals}
        if not strict_equal(originals, [x for x in trusted if x.get('media_id') in original_ids]):
            fail('DIALOGUE_ARCHIVE_MEDIA_CHANGED')
        # 原用户包保持不变；当前主体主动迁移的旧浏览器原件由独立受信绑定投影。
        attachments = ([] if any(x.get('kind') == 'image' for x in trusted) else legacy) + trusted
        return {'message_id': row['request_id'] + ':user', 'role': 'user',
                'status': 'archived', 'content': packet['content'],
                'references': refs, 'attachments': attachments}

    def _media_for_turn(self, workspace, request_id, role, bank_db):
        service = getattr(self.g, 'chat_media', None)
        if service is None:
            return []
        if bank_db is None:
            fail('DIALOGUE_ARCHIVE_MEDIA_GUARD_REQUIRED', 503)
        return service.descriptors_for_turn(workspace, request_id, role, bank_db=bank_db)

    def _proof(self, packet, publication, original, envelope, saved, scope, rid):
        proof = packet.get('agent_final_proof')
        if (type(original) is not dict or type(proof) is not dict
                or original.get('isFallback') or original.get('workspace_id') != scope['workspace_id']
                or original.get('bank_response_id') != rid or packet.get('response_id') != rid
                or type(original.get('reply')) is not str
                or not 1 <= len(original['reply']) <= 6000
                or packet.get('language') not in ('zh', 'en')
                or original.get('privacy_revision') != packet.get('privacy_revision')
                or original.get('personal_revision') != packet.get('personal_revision')
                or saved['privacy_revision'] != packet.get('privacy_revision')
                or saved['personal_revision'] != packet.get('personal_revision')
                or type(packet.get('privacy_revision')) is not int
                or not 0 <= packet['privacy_revision'] <= scope['privacy_revision']
                or type(packet.get('personal_revision')) is not int
                or not 0 <= packet['personal_revision'] <= scope['personal_revision']
                or type(packet.get('source_versions')) is not dict
                or fingerprint(original) != packet.get('original_response_hash')
                or proof.get('contract') != FINAL_PROOF or proof.get('request_id') != rid
                or proof.get('message_id') != rid or proof.get('workspace_id') != scope['workspace_id']
                or proof.get('language') != packet['language']
                or proof.get('approved_content') != original['reply']
                or proof.get('approved_response_hash') != packet['original_response_hash']
                or type(proof.get('approved_review_projection')) is not dict
                or fingerprint(proof['approved_review_projection']) != proof.get('approved_projection_hash')
                or type(proof.get('verdict')) is not dict or proof['verdict'].get('verdict') != 'approve'
                or type(original.get('agent_review')) is not dict
                or original['agent_review'].get('status') != 'approved'
                or proof.get('reviewer_model') != original['agent_review'].get('model_id')
                or proof.get('review_id') != original['agent_review'].get('review_id')
                or publication.get('response_id') != rid
                or publication.get('original_response_hash') != packet['original_response_hash']
                or publication.get('prepared_envelope_hash') != fingerprint(envelope)
                or publication.get('publication_state') != 'server_send_completed'
                or publication.get('original_guarded_writer_committed') is not True
                or publication.get('bank_binding_committed_before_publication') is not True):
            fail('DIALOGUE_ARCHIVE_ORIGINAL_CHANGED')
        a, b = proof.get('body_amount_occurrence_count'), proof.get('total_amount_occurrence_count')
        if type(a) is not int or type(b) is not int or not 0 <= a <= b <= 24:
            fail('DIALOGUE_ARCHIVE_ORIGINAL_CHANGED')
        return proof

    @contextmanager
    def _public_sources(self, packet, db):
        # 独立历史证明只检查原来源及显式撤回，不复用105秒/256条当前pin。
        grant = packet.get('_historical_attestation')
        if type(grant) is not dict:
            fail('DIALOGUE_ARCHIVE_ATTESTATION_REQUIRED')
        with self.sources.guard(packet['actor']['workspace_id'], grant['source_attestation'],
                                packet['original_response'], bank_db=db):
            yield

    @staticmethod
    def _attestation_table(private):
        if private.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_dialogue_attestations'").fetchone() is None:
            fail('DIALOGUE_ARCHIVE_ATTESTATION_STORAGE_REQUIRED', 503)

    def _open_attestation(self, private, scope, rid, envelope, marker, saved, raw):
        self._attestation_table(private)
        row = private.execute('SELECT * FROM agent_dialogue_attestations WHERE workspace_id=? AND response_id=?',
                              (scope['workspace_id'], rid)).fetchone()
        if row is None:
            fail('DIALOGUE_ARCHIVE_ATTESTATION_REQUIRED')
        cipher = row['attestation_cipher']
        if type(cipher) is not str or not cipher.startswith('pd1:'):
            fail('DIALOGUE_ARCHIVE_ATTESTATION_INVALID')
        try:
            sealed = json.loads(self.store._decode(cipher))
        except (BankError, ValueError, TypeError):
            fail('DIALOGUE_ARCHIVE_ATTESTATION_INVALID')
        if (type(sealed) is not dict or set(sealed) != {'body', 'body_hash'}
                or type(sealed['body']) is not dict or fingerprint(sealed['body']) != sealed['body_hash']):
            fail('DIALOGUE_ARCHIVE_ATTESTATION_INVALID')
        grant = sealed['body']
        binding = {k: scope[k] for k in sorted(_BINDING_FIELDS)}
        if (set(grant) != _ATTESTATION_FIELDS or grant['contract'] != ATTESTATION
                or grant['codec'] != ORIGINAL_CODEC or not strict_equal(grant['binding'], binding)
                or type(grant['archive_epoch']) is not int or grant['archive_epoch'] != scope['archive_epoch']
                or grant['response_id'] != rid or grant['original_contract'] != envelope['contract']
                or grant['original_contract'] not in (APPROVED, LOCAL_ORIGINAL)
                or type(grant['issuer_code']) is not str or len(grant['issuer_code']) != 64
                or any(c not in '0123456789abcdef' for c in grant['issuer_code'])
                or grant['original_envelope_hash'] != fingerprint(envelope)
                or grant['publication_marker_hash'] != fingerprint(marker)
                or grant['saved_widget_hash'] != saved['widget_hash']
                or grant['save_generation'] != saved['save_generation']
                or grant['saved_widget_hash'] != self.store._guarded_row_hash(raw, 'widgets_cipher', 'created_at')
                or type(grant['issued_at']) not in (int, float) or not math.isfinite(grant['issued_at'])
                or not strict_equal(grant['issued_at'], row['created_at'])
                or grant['rule_validation'] != ('agent_final_proof' if envelope['contract'] == APPROVED else 'local_fixed_rule')):
            fail('DIALOGUE_ARCHIVE_ATTESTATION_INVALID')
        return grant

    def attest_published(self, w, response_id, *, bank_db):
        """仅供server成功published后调用；新发行证明不得为旧历史补签。"""
        with self.p._publication(bank_db) as db:
            scope = self._binding(w, db)
            # 进程内原pending证明它是本请求刚完成的原published；重启不补签。
            with self.approved._lock:
                entry = deepcopy(self.approved._pending.get((w, response_id)))
            if not entry or entry.get('publication') is None:
                fail('DIALOGUE_ARCHIVE_CURRENT_PUBLICATION_REQUIRED')
            if (fingerprint(self.approved._open(entry['envelope'], entry['envelope']['contract'])) != fingerprint(entry['packet'])
                    or self.approved._open(entry['publication'], COMMIT).get('publication_state') != 'server_send_completed'):
                fail('DIALOGUE_ARCHIVE_CURRENT_PUBLICATION_REQUIRED')
            # source prepare可能写同一私库registry；不得持私库写锁再开第二写连接。
            source = self.sources.prepare(w, entry['packet']['original_response'], bank_db=db)
            with closing(self.store.connect()) as private:
                private.execute('BEGIN IMMEDIATE')
                try:
                    self._table_ready(private)
                    self._attestation_table(private)
                    if self.store.archive_epoch_from_db(private, w) != scope['archive_epoch']:
                        fail('DIALOGUE_ARCHIVE_EPOCH_CHANGED')
                    row = private.execute('SELECT * FROM agent_user_turn_archive WHERE workspace_id=? AND request_id=?',
                                          (w, response_id)).fetchone()
                    if row is None:
                        fail('DIALOGUE_ARCHIVE_USER_REQUIRED')
                    self._user(row, scope, db)
                    _, packet = self._assistant(private, scope, response_id, db, _issuing=True)
                    widgets = self.store.published_turn_widgets(private, w, response_id)
                    envelope = next(x for x in widgets if type(x) is dict and x.get('contract') in (APPROVED, LOCAL_ORIGINAL))
                    marker = next(x for x in widgets if type(x) is dict and x.get('contract') == COMMIT)
                    if (not strict_equal(envelope, entry['envelope']) or not strict_equal(marker, entry['publication'])
                            or fingerprint(packet) != fingerprint(entry['packet'])):
                        fail('DIALOGUE_ARCHIVE_CURRENT_PUBLICATION_REQUIRED')
                    # 原_current _open只在发行时使用；durable reader不忽略其CODE。
                    saved = private.execute('SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?',
                                            (w, response_id)).fetchone()
                    from base_runtime.approved_response_store import CODE as issuer_code
                    stamp = float(self.g.bank.clock())
                    grant = {'contract': ATTESTATION, 'codec': ORIGINAL_CODEC,
                             'binding': {k: scope[k] for k in sorted(_BINDING_FIELDS)},
                             'archive_epoch': scope['archive_epoch'], 'response_id': response_id,
                             'original_contract': envelope['contract'], 'issuer_code': issuer_code,
                             'original_envelope_hash': fingerprint(envelope),
                             'publication_marker_hash': fingerprint(marker),
                             'saved_widget_hash': saved['widget_hash'], 'save_generation': saved['save_generation'],
                             'original_response_hash': packet['original_response_hash'],
                             'source_attestation': source, 'issued_at': stamp,
                             'rule_validation': 'agent_final_proof' if envelope['contract'] == APPROVED else 'local_fixed_rule'}
                    existing = private.execute('SELECT * FROM agent_dialogue_attestations WHERE workspace_id=? AND response_id=?',
                                               (w, response_id)).fetchone()
                    if existing is not None:
                        old = self._open_attestation(private, scope, response_id, envelope, marker, saved,
                                                    self.store._raw_widget(private, w, response_id))
                        if old['original_response_hash'] != grant['original_response_hash']:
                            fail('DIALOGUE_ARCHIVE_ATTESTATION_CONFLICT')
                        private.rollback()
                        return {'response_id': response_id, 'archive_epoch': old['archive_epoch'], 'attested': True}
                    cipher = self.store.protect(json.dumps({'body': grant, 'body_hash': fingerprint(grant)},
                        ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))
                    private.execute('INSERT INTO agent_dialogue_attestations VALUES(?,?,?,?)',
                                    (w, response_id, cipher, stamp))
                    private.commit()
                    return {'response_id': response_id, 'archive_epoch': scope['archive_epoch'], 'attested': True}
                except Exception:
                    private.rollback()
                    raise

    @staticmethod
    def _readonly_components(original):
        """仅静态组件数据，不交给现有带resolver/action的widget挂载路径。"""
        blocked = {'actions', 'action', 'bank_actions', 'followups', 'follow_up_questions', 'readonly_actions',
                   'confirmation', 'confirmation_token', 'confirm_token', 'retry', 'retry_token', 'pay', 'payment',
                   'token', 'service_signature', 'wire_ref', 'message_state', 'callback', 'endpoint', 'href', 'url',
                   'intent', 'context_ref', 'availability', 'operation_id', 'draft_id',
                   'service_nonce', 'confirmation_context'}
        compact_blocked = {k.replace('_', '').replace('-', '').lower() for k in blocked}
        def allowed_key(key):
            if type(key) is not str or key in {'__proto__', 'constructor', 'prototype'}:
                return False
            normalized = key.replace('_', '').replace('-', '').lower()
            return (normalized not in compact_blocked and not normalized.endswith('token')
                    and not (key.lower().startswith('on') and len(key) > 2))
        def inert(value, depth=0):
            if depth > 32:
                fail('DIALOGUE_ARCHIVE_COMPONENT_INVALID')
            if value is None or type(value) in (str, bool, int):
                return value
            if type(value) is float and math.isfinite(value):
                return value
            if type(value) is list:
                return [inert(v, depth + 1) for v in value]
            if type(value) is dict:
                return {k: inert(v, depth + 1) for k, v in value.items()
                        if allowed_key(k)}
            fail('DIALOGUE_ARCHIVE_COMPONENT_INVALID')
        pres = original.get('presentation')
        if pres is not None and (type(pres) is not dict or pres.get('contract') != 'navren.presentation/1'):
            fail('DIALOGUE_ARCHIVE_COMPONENT_INVALID')
        reply = original.get('reply_presentation')
        if reply is not None and (type(reply) is not dict or reply.get('schema_version') != 'navren.reply-presentation.v1'):
            fail('DIALOGUE_ARCHIVE_COMPONENT_INVALID')
        widgets = []
        for widget in original.get('widgets', []):
            if type(widget) is dict and widget.get('type') == 'financial_presentation':
                view = widget.get('presentation')
                if type(view) is not dict or view.get('contract') != 'navren.presentation/1':
                    fail('DIALOGUE_ARCHIVE_COMPONENT_INVALID')
                widgets.append({'type': 'financial_presentation_snapshot', 'presentation': inert(view)})
        batch = original.get('metric_batch')
        if batch is not None and (type(batch) is not dict or type(batch.get('metrics')) is not list):
            fail('DIALOGUE_ARCHIVE_COMPONENT_INVALID')
        return {'contract': 'navren.archived-display/1', 'presentation': inert(pres),
                'reply_presentation': inert(reply), 'metric_records': inert(batch['metrics']) if batch else [],
                'widgets': widgets, 'historical_readonly': True, 'current_fact_eligible': False,
                'execution_authority': False}

    def _assistant(self, private, scope, rid, db, *, _issuing=False):
        w = scope['workspace_id']
        widgets = self.store.published_turn_widgets(private, w, rid)
        if widgets is None:
            return {'message_id': rid, 'role': 'assistant', 'status': 'not_published', 'content': None,
                    'historical_readonly': True, 'current_fact_eligible': False, 'execution_authority': False}, None
        originals = [x for x in widgets if type(x) is dict and x.get('contract') in (APPROVED, LOCAL_ORIGINAL)]
        markers = [x for x in widgets if type(x) is dict and x.get('contract') == COMMIT]
        if len(originals) != 1 or len(markers) != 1:
            fail('DIALOGUE_ARCHIVE_NOT_PUBLISHED')
        actor = self.approved._actor(w, db)
        original_contract = originals[0]['contract']
        saved = private.execute("SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=? AND status='saved'", (w, rid)).fetchone()
        raw = self.store._raw_widget(private, w, rid)
        if (not saved or not raw
                or saved['widget_hash'] != self.store._guarded_row_hash(raw, 'widgets_cipher', 'created_at')):
            fail('DIALOGUE_ARCHIVE_SAVE_CHANGED')
        grant = None
        if _issuing:
            packet = self.approved._open(originals[0], original_contract)
            publication = self.approved._open(markers[0], COMMIT)
        else:
            grant = self._open_attestation(private, scope, rid, originals[0], markers[0], saved, raw)
            packet = self.approved._open_historical_codec(originals[0], original_contract,
                issuer_code=grant['issuer_code'], codec=grant['codec'])
            publication = self.approved._open_historical_codec(markers[0], COMMIT,
                issuer_code=grant['issuer_code'], codec=grant['codec'])
        if (packet.get('contract') != original_contract or packet.get('actor') != actor
                or grant is not None and grant['original_response_hash'] != packet.get('original_response_hash')):
            fail('DIALOGUE_ARCHIVE_SAVE_CHANGED')
        stamp = publication.get('published_at')
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            fail('DIALOGUE_ARCHIVE_NOT_PUBLISHED')
        original = packet.get('original_response')
        if original_contract == APPROVED:
            proof = self._proof(packet, publication, original, originals[0], saved, scope, rid)
            original_review = {'source': 'agent_review', 'status': 'approved_at_publication',
                               'reviewer_model': proof['reviewer_model'], 'review_id': proof['review_id']}
        else:
            # Base 尚须在原固定规则发布点生成此同表封套；缺失时绝不从短历史重造。
            if _issuing:
                try:
                    social_reply.validate_social_reply_result(original)
                except (ValueError, TypeError):
                    fail('DIALOGUE_ARCHIVE_LOCAL_CHANGED')
            local_code = hashlib.sha256(Path(social_reply.__file__).read_bytes()).hexdigest()
            if (packet.get('response_id') != rid or original['bank_response_id'] != rid
                    or original['workspace_id'] != w or type(packet.get('privacy_revision')) is not int
                    or not 0 <= packet['privacy_revision'] <= scope['privacy_revision']
                    or original.get('privacy_revision') != packet.get('privacy_revision')
                    or original.get('personal_revision') != packet.get('personal_revision')
                    or type(packet.get('personal_revision')) is not int
                    or not 0 <= packet['personal_revision'] <= scope['personal_revision']
                    or saved['privacy_revision'] != packet['privacy_revision']
                    or saved['personal_revision'] != packet['personal_revision']
                    or packet.get('language') != original['social_reply']['language']
                    or (_issuing and packet.get('local_rule_code') != local_code)
                    or type(packet.get('source_versions')) is not dict
                    or packet.get('local_rule_id') != original['social_reply']['rule_id']
                    or fingerprint(original) != packet.get('original_response_hash')
                    or publication.get('response_id') != rid
                    or publication.get('original_response_hash') != packet['original_response_hash']
                    or publication.get('prepared_envelope_hash') != fingerprint(originals[0])
                    or publication.get('publication_state') != 'server_send_completed'
                    or publication.get('original_guarded_writer_committed') is not True
                    or publication.get('bank_binding_committed_before_publication') is not True):
                fail('DIALOGUE_ARCHIVE_LOCAL_CHANGED')
            original_review = {'source': 'local_rule', 'status': 'not_required',
                               'rule_id': packet['local_rule_id'], 'reviewer_model': None, 'review_id': None}
        if not _issuing:
            packet['_historical_attestation'] = grant
            with self._public_sources(packet, db):
                pass
        current_versions = self.p.source_versions(w, db)
        dto = {'message_id': rid, 'role': 'assistant',
               'status': 'published_original' if original_contract == APPROVED else 'published_local_rule',
               'content': original['reply'], 'language': packet['language'],
               'historical_readonly': True, 'current_fact_eligible': False, 'execution_authority': False,
               'source_versions_state': 'unchanged' if strict_equal(packet['source_versions'], current_versions) else 'changed',
               'original_personal_revision': packet['personal_revision'],
               'original_privacy_revision': packet['privacy_revision'],
               'original_response_hash': packet['original_response_hash'],
               'original_review': original_review,
               'readonly_components': self._readonly_components(original),
               'attachments': self._media_for_turn(w, rid, 'assistant', db),
               'publication': {'state': 'server_send_completed', 'published_at': publication.get('published_at'),
                               'client_acknowledged': publication.get('client_acknowledged') is True}}
        return dto, packet

    def read_page(self, w, *, cursor=None, limit=DEFAULT_LIMIT):
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            fail('DIALOGUE_ARCHIVE_LIMIT', 422)
        packets, pins, items = [], {}, []
        with self.p._publication() as db:
            scope = self._binding(w, db)
            with self.store.connection(w) as private:
                self._table_ready(private)
                if self.store.archive_epoch_from_db(private, w) != scope['archive_epoch']:
                    fail('DIALOGUE_ARCHIVE_EPOCH_CHANGED')
                if cursor is None:
                    upper = private.execute('SELECT COALESCE(MAX(archive_sequence),0) FROM agent_user_turn_archive WHERE workspace_id=?', (w,)).fetchone()[0]
                    before = upper + 1
                else:
                    upper, before = self._open_cursor(cursor, scope)
                rows = private.execute('SELECT * FROM agent_user_turn_archive WHERE workspace_id=? AND archive_sequence<=? AND archive_sequence<? ORDER BY archive_sequence DESC LIMIT ?',
                                       (w, upper, before, limit + 1)).fetchall()
                has_more = len(rows) > limit
                selected = rows[:limit]
                for row in reversed(selected):
                    rid = row['request_id']
                    if (type(rid) is not str or not 1 <= len(rid) <= 160
                            or type(row['archive_sequence']) is not int or row['archive_sequence'] < 1
                            or type(row['received_at']) not in (int, float) or not math.isfinite(row['received_at'])):
                        fail('DIALOGUE_ARCHIVE_ROW_CHANGED')
                    user = self._user(row, scope, db)
                    try:
                        assistant, packet = self._assistant(private, scope, rid, db)
                    except BankError:
                        assistant = {'message_id': rid, 'role': 'assistant', 'status': 'withheld', 'content': None,
                                     'historical_readonly': True, 'current_fact_eligible': False, 'execution_authority': False}
                        packet = None
                    if packet is not None:
                        packets.append(packet)
                    items.append({'request_id': rid, 'sequence': row['archive_sequence'], 'received_at': row['received_at'],
                                  'user': user, 'assistant': assistant})
                    pins[rid] = self._row_pin(private, w, rid)
            page = {k: scope[k] for k in sorted(_BINDING_FIELDS)}
            page.update(contract=CONTRACT, archive_epoch=scope['archive_epoch'], privacy_revision=scope['privacy_revision'], personal_revision=scope['personal_revision'],
                        snapshot_id=fingerprint({'contract': CURSOR_CONTRACT, 'code': CODE,
                            'binding': {k: scope[k] for k in (*sorted(_BINDING_FIELDS), 'archive_epoch', 'privacy_revision', 'session')},
                            'upper_sequence': upper}),
                        items=items, next_cursor=self._cursor(scope, upper=upper, before=selected[-1]['archive_sequence']) if has_more else None,
                        has_more=has_more, model_context_limit=MODEL_CONTEXT_LIMIT, execution_authority=False,
                        archive_coverage={'source': 'registered_user_archive', 'legacy_public_history_imported': False})
            nonce = secrets.token_hex(16)
            with self._capture_lock:
                self._captures[nonce] = {'scope': scope, 'pins': pins, 'packets': packets,
                                         'response_hash': fingerprint(page), 'source_versions': self.p.source_versions(w, db)}
                while len(self._captures) > 64:
                    self._captures.popitem(last=False)
            return page, _Capture(self, nonce)

    def _row_pin(self, private, w, rid):
        user = private.execute('SELECT * FROM agent_user_turn_archive WHERE workspace_id=? AND request_id=?', (w, rid)).fetchone()
        saved = private.execute('SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=?', (w, rid)).fetchone()
        widget = self.store._raw_widget(private, w, rid)
        publication = private.execute('SELECT * FROM guarded_turn_publications WHERE workspace_id=? AND response_id=?', (w, rid)).fetchone()
        self._attestation_table(private)
        attestation = private.execute('SELECT * FROM agent_dialogue_attestations WHERE workspace_id=? AND response_id=?', (w, rid)).fetchone()
        return fingerprint({'attestation': dict(attestation) if attestation else None,
                            'user': dict(user) if user else None, 'saved': dict(saved) if saved else None,
                            'widget': dict(widget) if widget else None, 'publication': dict(publication) if publication else None})

    def _check_send(self, w, record, page, db, source_checkpoint):
        if (not strict_equal(record['scope'], self._binding(w, db))
                or fingerprint(page) != record['response_hash']
                or not strict_equal(record['source_versions'], self.p.source_versions(w, db))):
            fail('DIALOGUE_ARCHIVE_SEND_CHANGED')
        with self.store.connection(w) as private:
            if self.store.archive_epoch_from_db(private, w) != record['scope']['archive_epoch']:
                fail('DIALOGUE_ARCHIVE_EPOCH_CHANGED')
            if any(self._row_pin(private, w, rid) != digest for rid, digest in record['pins'].items()):
                fail('DIALOGUE_ARCHIVE_SEND_CHANGED')
        source_checkpoint()

    @contextmanager
    def send_guard(self, w, capture, page):
        if type(capture) is not _Capture or capture.reader is not self:
            fail('DIALOGUE_ARCHIVE_CAPTURE_REQUIRED', 503)
        with self._capture_lock:
            record = self._captures.get(capture.nonce)
        if record is None or not callable(getattr(self.g._restore_send_check, 'set', None)):
            fail('DIALOGUE_ARCHIVE_CAPTURE_REQUIRED', 503)
        try:
            # 当前会话/资料发送门不变；一页只取一次历史market/RAG锁，socket
            # checkpoint复用同连接，不为每turn再开BEGIN IMMEDIATE。
            entries = [(p['_historical_attestation']['source_attestation'], p['original_response'])
                       for p in record['packets']]
            with self.g.publication_guard(w, record['scope']['privacy_revision'], record['scope']['personal_revision']) as db:
                with self.sources.guard_many(w, entries, bank_db=db) as source_checkpoint:
                    token = self.g._restore_send_check.set(
                        lambda current_db: self._check_send(w, record, page, current_db, source_checkpoint))
                    try:
                        self._check_send(w, record, page, db, source_checkpoint)
                        yield
                    finally:
                        self.g._restore_send_check.reset(token)
        finally:
            with self._capture_lock:
                self._captures.pop(capture.nonce, None)
