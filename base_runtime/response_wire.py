"""唯一外部回包边界与只读历史提供端；Base当前性不冒充Agent语义批准。"""
from collections import OrderedDict
from contextlib import closing
from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time

from financial_metrics import fingerprint
from simulated_bank import BankError
from agent_runtime.metric_claims import monetary_occurrences, MetricClaimError
from user_memory import privacy_revision

CONTRACT = 'navren.response-wire/1'
HISTORY = 'navren.readonly-history/1'
CODE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def reject(code='RESPONSE_WIRE_INVALID', status=422):
    raise BankError(code, '回复原件、账户或版本无效，请重新读取。', status)


class ResponseWire:
    def __init__(self, gateway, provider, *, ttl=300, maximum=128):
        if type(ttl) not in (int, float) or not 0 < ttl <= 3600 or type(maximum) is not int or not 1 <= maximum <= 1024:
            raise ValueError('response_wire_configuration_invalid')
        self.g, self.provider, self.p = gateway, provider, gateway.presentations
        self.ttl, self.maximum = ttl, maximum
        self._pool, self._lock = OrderedDict(), threading.RLock()

    def _identity(self, w, db):
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != CODE:
            reject('RESPONSE_WIRE_RESTART_REQUIRED', 503)
        return self.p._session_binding(w, db, required=True)

    @staticmethod
    def _suggestions(texts):
        if type(texts) is not list or len(texts) > 3:
            reject()
        out = []
        for text in texts:
            if (type(text) is not str or not text.strip() or len(text) > 180 or '<' in text or '>' in text
                    or re.search(r'https?://', text)):
                reject('READONLY_SUGGESTION_INVALID')
            try:
                if monetary_occurrences(text): reject('READONLY_SUGGESTION_INVALID')
            except MetricClaimError:
                reject('READONLY_SUGGESTION_INVALID')
            out.append({'label': text, 'text': text, 'intent': 'ask', 'type': 'send_message', 'execution_authority': False})
        return out

    def build(self, w, obj=None, receipt=None, *, trusted_current_request, trusted_current_message,
              language='zh', readonly_questions=None, readonly_text='', bank_db=None):
        self.provider._current(trusted_current_request, trusted_current_message, language)
        questions = self._suggestions([] if readonly_questions is None else readonly_questions)
        if type(readonly_text) is not str or len(readonly_text) > 6000 or re.search(r'<\s*/?\s*[A-Za-z!]', readonly_text):
            reject('READONLY_BODY_INVALID')
        if readonly_text:
            if obj is not None or receipt is not None: reject('RESPONSE_WIRE_BODY_NOT_ISSUED')
            try:
                if monetary_occurrences(readonly_text): reject('RESPONSE_WIRE_BODY_NOT_ISSUED')
            except MetricClaimError:
                reject('RESPONSE_WIRE_BODY_NOT_ISSUED')
        with self.p._publication(bank_db) as db:
            identity = self._identity(w, db)
            result = {'presentation': None, 'metric_batch': None}
            proof, reply = None, readonly_text
            if obj is not None:
                self.provider._guard_object(w, obj, trusted_current_request, trusted_current_message, language, db)
                result = deepcopy(obj['result'])
                if set(result) != {'presentation', 'metric_batch'}:
                    reject()
            if receipt is not None:
                if obj is None: reject()
                proof = self.provider.verify(w, receipt, trusted_current_request=trusted_current_request,
                                             trusted_current_message=trusted_current_message, language=language, bank_db=db)
                projection = receipt['projection']
                if projection['object_ref'] != obj['object_ref']:
                    reject('RESPONSE_WIRE_RECEIPT_MISMATCH', 409)
                # 正文沿服务缓存原投影；不接客户另写的reply。
                reply = projection['complete_review']['text']
            wire_id = 'response-wire-' + secrets.token_hex(16)
            packet = {'contract': CONTRACT, 'wire_id': wire_id, 'request_id': trusted_current_request,
                      'message_id': trusted_current_message, 'workspace_binding': w, 'language': language,
                      'reply': reply, **result, 'presentation_receipt': deepcopy(receipt), 'service_proof': proof,
                      'readonly_suggestions': questions, 'source_versions': self.p.source_versions(w, db),
                      'privacy_revision': privacy_revision(db, w), 'personal_revision': self.g.personal_data.revision(w),
                      'semantic_review_status': 'owner_wiring_required', 'publication_status': 'owner_wiring_required',
                      'readonly': True, 'execution_authority': False}
            signature = self.g.financial_metrics._tool_signature({'packet': packet, 'identity': identity, 'code': CODE})
            packet['wire_ref'] = {'wire_id': wire_id, 'service_signature': signature}
            with self._lock:
                self._pool[wire_id] = {'packet': deepcopy(packet), 'identity': identity, 'object': deepcopy(obj),
                                      'receipt': deepcopy(receipt), 'expires_at': time.monotonic() + self.ttl}
                while len(self._pool) > self.maximum: self._pool.popitem(last=False)
            return packet

    def validate(self, w, ref, *, bank_db=None):
        if (type(ref) is not dict or set(ref) != {'wire_id', 'service_signature'}
                or type(ref['wire_id']) is not str or re.fullmatch(r'response-wire-[a-f0-9]{32}', ref['wire_id']) is None
                or type(ref['service_signature']) is not str or re.fullmatch(r'[a-f0-9]{64}', ref['service_signature']) is None):
            reject()
        with self._lock: entry = deepcopy(self._pool.get(ref['wire_id']))
        if not entry: reject('RESPONSE_WIRE_NOT_ISSUED', 404)
        if time.monotonic() >= entry['expires_at']: reject('RESPONSE_WIRE_EXPIRED', 410)
        packet = entry['packet']; bare = {k:v for k,v in packet.items() if k != 'wire_ref'}
        expected = self.g.financial_metrics._tool_signature({'packet': bare, 'identity': entry['identity'], 'code': CODE})
        if not hmac.compare_digest(expected, ref['service_signature']): reject('RESPONSE_WIRE_TAMPERED', 409)
        with self.p._publication(bank_db) as db:
            if self._identity(w, db) != entry['identity'] or packet['workspace_binding'] != w:
                reject('RESPONSE_WIRE_IDENTITY_CHANGED', 409)
            if (packet['source_versions'] != self.p.source_versions(w, db)
                    or packet['privacy_revision'] != privacy_revision(db, w)
                    or packet['personal_revision'] != self.g.personal_data.revision(w)):
                reject('RESPONSE_WIRE_STALE', 409)
            if entry['object'] is not None:
                self.provider._guard_object(w, entry['object'], packet['request_id'], packet['message_id'], packet['language'], db)
            if entry['receipt'] is not None:
                self.provider.verify(w, entry['receipt'], trusted_current_request=packet['request_id'],
                                     trusted_current_message=packet['message_id'], language=packet['language'], bank_db=db)
            return {'ok': True, 'service_currentness': True, 'wire': deepcopy(packet),
                    'semantic_review_status': 'owner_wiring_required', 'approved_for_publication': False}

    def archive_descriptor(self, w, ref, *, bank_db=None):
        """交给原owner与完整turn一起guarded保存；不另保存聊天或批准。"""
        checked = self.validate(w, ref, bank_db=bank_db)
        packet = checked['wire']
        body = {'contract': HISTORY, 'response_id': packet['message_id'], 'workspace_binding': w,
                'language': packet['language'], 'created_at': self.g.bank.clock(),
                'source_versions': deepcopy(packet['source_versions']), 'privacy_revision': packet['privacy_revision'],
                'personal_revision': packet['personal_revision'],
                'original_wire_hash': fingerprint(packet), 'original_receipt_hash': packet['service_proof']['original_receipt_hash'] if packet['service_proof'] else None,
                'reply': packet['reply'], 'presentation': deepcopy(packet['presentation']), 'metric_batch': deepcopy(packet['metric_batch']),
                'readonly_suggestions': deepcopy(packet['readonly_suggestions']),
                'semantic_review_status': 'owner_wiring_required', 'readonly': True, 'execution_authority': False}
        body['service_signature'] = self.g.financial_metrics._tool_signature({'history': body, 'code': CODE})
        # 原个人库的持久Fernet校验封套；不新增密钥、不延长执行授权。
        body['sealed_attestation'] = self.g.personal_data.protect(json.dumps(
            {'contract': HISTORY, 'issuer_code': CODE, 'descriptor_hash': fingerprint(body)},
            ensure_ascii=False, sort_keys=True, separators=(',', ':')))
        return body

    def restore(self, w, response_id, *, language='zh', bank_db=None):
        if type(response_id) is not str or not 1 <= len(response_id) <= 160 or language not in {'zh', 'en'}:
            reject()
        with self.p._publication(bank_db) as db:
            self._identity(w, db)
            with self.g.personal_data.connection(w) as private:
                row = private.execute('SELECT widgets FROM agent_widget_history WHERE workspace_id=? AND response_id=?', (w, response_id)).fetchone()
                if not row: reject('READONLY_HISTORY_NOT_FOUND', 404)
                widgets = json.loads(self.g.personal_data._decode(row['widgets']))
            originals = [x for x in widgets if type(x) is dict and x.get('contract') == HISTORY]
            if len(originals) != 1: reject('READONLY_HISTORY_INVALID', 409)
            original = originals[0]; sig = original.get('service_signature')
            seal = original.get('sealed_attestation')
            if type(sig) is not str or re.fullmatch(r'[a-f0-9]{64}', sig) is None or type(seal) is not str or not seal.startswith('pd1:'):
                reject('READONLY_HISTORY_TAMPERED', 409)
            try:
                attestation = json.loads(self.g.personal_data._decode(seal))
            except (BankError, ValueError, TypeError):
                reject('READONLY_HISTORY_TAMPERED', 409)
            signed = {k:v for k,v in original.items() if k != 'sealed_attestation'}
            if attestation != {'contract': HISTORY, 'issuer_code': CODE, 'descriptor_hash': fingerprint(signed)}:
                reject('READONLY_HISTORY_TAMPERED', 409)
            body = {k:v for k,v in signed.items() if k != 'service_signature'}
            if (body['workspace_binding'] != w or body['response_id'] != response_id or body['language'] != language):
                reject('READONLY_HISTORY_BINDING', 403)
            if (body['source_versions'] != self.p.source_versions(w, db) or body['privacy_revision'] != privacy_revision(db, w)
                    or body['personal_revision'] != self.g.personal_data.revision(w)):
                reject('READONLY_HISTORY_STALE', 409)
            with self.g.personal_data.connection(w) as private:
                saved = private.execute("SELECT * FROM guarded_turn_saves WHERE workspace_id=? AND response_id=? AND status='saved'",
                                        (w,response_id)).fetchone()
                latest = private.execute("SELECT response_id FROM guarded_turn_saves WHERE workspace_id=? AND status='saved' ORDER BY rowid DESC LIMIT 1",
                                         (w,)).fetchone()
                store = self.g.personal_data
                dialogue = private.execute('SELECT messages FROM agent_dialogues WHERE workspace_id=?',(w,)).fetchone()
                messages = json.loads(dialogue['messages']) if dialogue else []
                save_verified = bool(saved and saved['personal_revision']==body['personal_revision']
                    and saved['privacy_revision']==body['privacy_revision']
                    and saved['widget_hash']==store._guarded_row_hash(store._raw_widget(private,w,response_id),'widgets_cipher','created_at'))
                latest_verified = bool(save_verified and latest and latest['response_id']==response_id
                    and messages and messages[-1].get('role')=='assistant' and messages[-1].get('content')==body['reply']
                    and saved['history_hash']==store._guarded_row_hash(store._raw_dialogue(private,w),'messages_cipher','updated_at'))
            # 保留原projection原件；只读交互策略在外层，不改原hash对应的p。
            output = deepcopy(body)
            output['status'] = 'current' if latest_verified else ('historical_readonly' if save_verified else 'owner_wiring_required')
            output['restored_as'] = 'current_readonly' if latest_verified else output['status']
            output['original_save_verified'] = save_verified
            output['latest_saved_reply_verified'] = latest_verified
            output['allowed_readonly_actions'] = ['ask'] if latest_verified else []
            output['original_presentation_preserved'] = True
            output['can_execute'] = False
            output['can_reissue_old_approval'] = False
            output['approved_for_publication'] = False
            return output
