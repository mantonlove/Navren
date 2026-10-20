"""当前真实Cookie工作区的资料投影与CAS；复用原个人库和银行profile。"""
from contextlib import closing, contextmanager
from copy import copy, deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import re

from simulated_bank import BankError
from user_memory import privacy_revision
from financial_metrics import fingerprint

CONTRACT = 'navren.account-profile/1'
CODE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
DISPLAY_ID = 'account-display'
AVATARS = frozenset({'default', 'stone', 'wave', 'orbit', 'leaf'})


def reject(code='ACCOUNT_PROFILE_INVALID', status=422):
    raise BankError(code, '账户资料或编辑版本无效，请重新读取当前账户。', status)


@contextmanager
def borrowed_transaction(db):
    yield db


class AccountProfile:
    def __init__(self, gateway):
        self.g, self.bank, self.store, self.p = gateway, gateway.bank, gateway.personal_data, gateway.presentations

    def _snapshot(self, w, bank_db, private_db):
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != CODE:
            reject('ACCOUNT_PROFILE_RESTART_REQUIRED', 503)
        identity = self.p._session_binding(w, bank_db, required=True)
        ws = self.bank._workspace(bank_db, w)
        conditions = self.bank._profile(bank_db, w)
        # 原模拟银行资金政策声明币种；逐账户只核验同币种，绝不加总推断币种。
        policy_currency = self.bank.capabilities().get('currency')
        account_currencies = [{'account_id': a['id'], 'currency': a.get('currency')}
                              for a in self.bank._accounts(bank_db, w)]
        known_policy = isinstance(policy_currency, str) and re.fullmatch(r'[A-Z]{3}', policy_currency) is not None
        currency_available = (known_policy and bool(account_currencies)
                              and all(a['currency'] == policy_currency for a in account_currencies))
        reserve_currency = {'source': 'simulation_bank_capabilities', 'policy_currency': policy_currency,
                            'account_currencies': account_currencies,
                            'currency': policy_currency if currency_available else None,
                            'status': 'available' if currency_available else 'unavailable',
                            'reason': None if currency_available else 'POLICY_CURRENCY_UNAVAILABLE' if not known_policy
                                      else 'ACCOUNT_CURRENCY_UNVERIFIED'}
        row = private_db.execute('SELECT * FROM user_memories WHERE workspace_id=? AND id=?', (w, DISPLAY_ID)).fetchone()
        display = json.loads(self.store._decode(row['value'])) if row else {'display_name': None, 'avatar': 'default'}
        account_state = None
        if self.g.registered_mode:
            account_state = self.g.account_state.read(w, bank_db=bank_db, private_db=private_db)
            profile = account_state['sections']['profile']
            display = {'display_name': profile['nickname'], 'avatar': profile['avatar_preset'], 'avatar_image': profile['avatar']}
        version = private_db.execute('SELECT revision FROM personal_versions WHERE workspace_id=?', (w,)).fetchone()
        personal_revision = version[0] if version else 0
        setting = private_db.execute('SELECT enabled FROM user_memory_settings WHERE workspace_id=?', (w,)).fetchone()
        declared = self.g.demo_accounts._record(w) if self.g.demo_accounts.is_demo(w) else None
        history = self.g.demo_accounts.active_history(w) if declared else None
        private = {'contract': CONTRACT, 'workspace_binding': w, 'identity': identity,
                   'bank_version': ws['version'], 'conditions': conditions,
                   'display': ({'profile': account_state['sections']['profile'], 'revision': account_state['revision']}
                               if account_state is not None else dict(row) if row else None), 'personal_revision': personal_revision,
                   'privacy_revision': privacy_revision(bank_db, w), 'helper_code': CODE,
                   'reserve_currency_basis': reserve_currency}
        expected = 'account-profile-' + self.g.financial_metrics._tool_signature(private)
        public = {'contract': CONTRACT, 'workspace_binding': w,
                  'identity': {'mode': 'demo_account' if declared else 'legacy_simulation',
                               'user_id': declared['user_id'] if declared else None,
                               'stable_label': declared['display_name'] if declared else '演示账户',
                               'is_real_identity': False},
                  'display': {'display_name': display['display_name'] or (declared['display_name'] if declared else '演示账户'),
                              'avatar': display['avatar'], 'avatar_image': display.get('avatar_image', ''),
                              'source': ('registered_account' if account_state is not None else 'user_confirmed' if row else 'source_default'),
                              'record_revision': account_state['revision'] if account_state is not None else row['record_revision'] if row else 0,
                              'editable_fields': ['display_name', 'avatar']},
                  'source': declared.get('source', 'navren_synthetic') if declared else 'simulation_bank_ledger',
                  'coverage': {'from': history.metadata['start_date'] if history else declared.get('coverage_start') if declared else None,
                               'to': history.metadata['end_date'] if history else declared.get('coverage_end') if declared else None,
                               'source_version': history.dataset_version if history else declared['source_version'] if declared else None,
                               'basis': 'registered_archive_header', 'new_full_audit_performed': False},
                  'as_of': ws['as_of'], 'bank_version': ws['version'],
                  'conditions': {**conditions, 'source': 'simulation_profile', 'is_formal_assessment': False,
                                 'currency': reserve_currency['currency'], 'currency_state': reserve_currency['status'],
                                 'currency_source': reserve_currency['source'],
                                 'currency_basis': 'simulation_household_reserve_policy',
                                 'currency_unavailable_reason': reserve_currency['reason'],
                                 'editable_fields': ['risk_level', 'horizon_days'] + (['reserve_minor'] if currency_available else []),
                                 'confirmation_required': True},
                  'unknown_fields': ['birth_date', 'phone', 'verified_legal_name'],
                  'authorization': {'privacy_revision': private['privacy_revision'], 'personal_revision': personal_revision,
                                    'memory_read_enabled': bool(setting and setting[0]),
                                    'financial_confirmation_required': True},
                  'expected_version': expected, 'execution_authority': False}
        return public

    def read(self, w, *, bank_db=None):
        with self.p._publication(bank_db) as db:
            if self.g.registered_mode: self.g.account_state.read(w, bank_db=db)
            with self.store.connection(w) as private:
                return self._snapshot(w, db, private)

    @staticmethod
    def _cas(expected, actual):
        if type(expected) is not str or re.fullmatch(r'account-profile-[a-f0-9]{64}', expected) is None:
            reject('ACCOUNT_PROFILE_VERSION_REQUIRED')
        if not hmac.compare_digest(expected, actual):
            reject('ACCOUNT_PROFILE_VERSION_CONFLICT', 409)

    def update_conditions(self, w, payload):
        if type(payload) is not dict or set(payload) != {'risk_level', 'horizon_days', 'reserve_minor', 'confirmed', 'expected_version'}:
            reject()
        with self.bank.transaction() as db, self.store.connection(w) as private:
            current = self._snapshot(w, db, private)
            self._cas(payload['expected_version'], current['expected_version'])
            if payload['confirmed'] is not True:
                raise BankError('CONFIRMATION_REQUIRED','修改模拟风险画像需要明确确认。')
            for key in ('risk_level', 'horizon_days', 'reserve_minor'):
                if type(payload[key]) is not int:
                    raise BankError('INVALID_INTEGER','资料条件必须使用整数。')
            if current['conditions']['currency_state'] != 'available' and payload['reserve_minor'] != current['conditions']['reserve_minor']:
                reject('ACCOUNT_PROFILE_RESERVE_CURRENCY_UNAVAILABLE', 409)
            scoped = copy(self.bank)
            scoped.transaction = lambda: borrowed_transaction(db)
            # 原profile的本人确认、范围限制、风险来源、审计和版本递增均原样复用。
            changed = {k: payload[k] for k in ('risk_level', 'horizon_days', 'reserve_minor')
                       if payload[k] != current['conditions'][k]}
            if changed:
                # 正式银行profile要求完整三字段；未变化值仍沿本人当前确认原件，不用局部payload覆盖。
                scoped.profile(w, {**{k:payload[k] for k in ('risk_level','horizon_days','reserve_minor')}, 'confirmed': True})
        return self.read(w)

    def update_display(self, w, payload):
        if type(payload) is not dict or set(payload) != {'display_name', 'avatar', 'expected_version', 'confirmed'}:
            reject()
        name = payload['display_name']
        if (payload['confirmed'] is not True or type(name) is not str or not 1 <= len(name.strip()) <= 40
                or any(ord(c) < 32 for c in name) or any(c in name for c in '<>')
                or type(payload['avatar']) is not str or payload['avatar'] not in AVATARS):
            reject()
        if self.g.registered_mode:
            with self.bank.transaction() as db, closing(self.store.connect()) as private:
                private.execute('BEGIN IMMEDIATE')
                binding = self.g.account_state.binding(w, db)
                state, _ = self.g.account_state._load(private, db, binding, migrate=True)
                current = self._snapshot(w, db, private)
                self._cas(payload['expected_version'], current['expected_version'])
                previous = state['sections']['profile']
                profile = {**previous, 'nickname': name.strip(), 'avatar_preset': payload['avatar']}
                # 旧界面每次保存都会回传当前预设；原样回传不是删除本人上传头像的指令。
                # 仅明确改选另一预设才用该预设替换上传图。
                if payload['avatar'] != previous['avatar_preset']:
                    profile['avatar'] = ''
                self.g.account_state.save(w, {'section': 'profile', 'value': profile,
                    'expected_revision': state['revision']}, bank_db=db, private_db=private)
                result = self._snapshot(w, db, private)
                private.commit()
            self.store._permissions()
            return result
        now = self.bank.clock()
        before = attempted = audit_id = None
        try:
            with self.bank.transaction() as db:
                scoped = copy(self.store); scoped.bank = copy(self.bank)
                scoped.bank.transaction = lambda: borrowed_transaction(db)
                with scoped.transaction(w, change=False) as private:
                    current = self._snapshot(w, db, private)
                    self._cas(payload['expected_version'], current['expected_version'])
                    row = private.execute('SELECT * FROM user_memories WHERE workspace_id=? AND id=?', (w, DISPLAY_ID)).fetchone()
                    before = dict(row) if row else None
                    record_revision = (row['record_revision'] if row else 0) + 1
                    value = self.store.protect(json.dumps({'display_name': name.strip(), 'avatar': payload['avatar']}, ensure_ascii=False))
                    private.execute('''INSERT INTO user_memories(id,workspace_id,category,value,origin,user_confirmed,
                        created_at,updated_at,expires_on,source_type,source_ref,confidence,confirmed_at,record_revision)
                        VALUES(?,?,'account_display',?,'user_declared',1,?,?,NULL,'account_display_edit',NULL,1,?,?)
                        ON CONFLICT(workspace_id,id) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,
                        confirmed_at=excluded.confirmed_at,record_revision=excluded.record_revision''',
                        (DISPLAY_ID, w, value, now, now, now, record_revision))
                    audit_id = private.execute('INSERT INTO personal_audit(workspace_id,event,object_id,revision,created_at) VALUES(?,?,?,?,?)',
                                               (w, 'account_display_updated', DISPLAY_ID, current['authorization']['personal_revision'], now)).lastrowid
                    attempted = dict(private.execute('SELECT * FROM user_memories WHERE workspace_id=? AND id=?', (w, DISPLAY_ID)).fetchone())
        except Exception:
            # 复用私库提交后_permissions已知失败语义；精确CAS补偿，不覆盖后来编辑。
            if attempted is not None:
                with closing(self.store.connect()) as private:
                    private.execute('BEGIN IMMEDIATE')
                    row = private.execute('SELECT * FROM user_memories WHERE workspace_id=? AND id=?', (w, DISPLAY_ID)).fetchone()
                    if row and dict(row) == attempted:
                        private.execute('DELETE FROM user_memories WHERE workspace_id=? AND id=?', (w, DISPLAY_ID))
                        if before:
                            before['value'] = self.store.protect(before['value'])
                            keys = list(before)
                            private.execute('INSERT INTO user_memories(' + ','.join(keys) + ') VALUES(' + ','.join('?' for _ in keys) + ')', list(before.values()))
                        private.execute('DELETE FROM personal_audit WHERE id=? AND workspace_id=?', (audit_id, w))
                    private.commit()
            raise
        return self.read(w)

    def logout(self, w):
        with self.bank.transaction() as db:
            identity = self.p._session_binding(w, db, required=True)
            if identity['mode'] == 'demo':
                db.execute('UPDATE demo_sessions SET revoked=1 WHERE token_hash=?', (identity['token_hash'],))
            else:
                db.execute('INSERT OR REPLACE INTO demo_sessions VALUES(?,?,?,?,?,1)',
                           (identity['token_hash'], w, 'legacy', 0, self.bank.clock()))
            self.bank._audit(db, w, 'account_logout', {})
        return {'logged_out': True, 'old_requests_revoked': True, 'public_session_logout_required': True}
