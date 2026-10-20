"""把真实短信核验接入既有操作绑定合同，不向模型提供号码或验证码。"""
from contextlib import closing
from dataclasses import asdict
import hashlib
import hmac
import json
import re

from .service import BankError

from .sms_mfa import SmsVerified


class AliyunOperationSmsProvider:
    source = 'provider'

    def __init__(self, auth=None, bank=None, *, experience_provider=None):
        self.auth, self.bank = auth, bank
        self.experience_provider = experience_provider
        self.registered_mode = auth is not None and not getattr(auth,'allow_legacy_experience',True)
        if auth is not None and bank is not None:
            self.attach(auth,bank,experience_provider=experience_provider)

    def attach(self, auth, bank, *, experience_provider=None):
        self.auth, self.bank = auth, bank
        self.experience_provider = experience_provider
        self.registered_mode = not getattr(auth,'allow_legacy_experience',True)
        if self.registered_mode and experience_provider is not None:
            if getattr(experience_provider,'bank',None) is not bank or not callable(getattr(experience_provider,'validate',None)):
                raise TypeError('trusted_read_only_experience_validator_required')
        if getattr(auth.sms.transport, 'source', None) == 'test_double':
            self.source = 'test_double'

    def _resolve(self, workspace):
        if self.auth is None or self.bank is None or not self.auth.sms.configured:
            return None
        if self.registered_mode:
            return self._resolve_registered(workspace)
        with closing(self.bank.connect()) as db:
            row = db.execute('SELECT token_hash FROM workspaces WHERE id=?', (workspace,)).fetchone()
        if not row:
            return None
        expected = row['token_hash']
        with self.auth.connect() as db:
            candidates = db.execute('''SELECT p.*,c.value AS bank_cookie FROM sms_phone_bindings p
                JOIN welcome_users u ON u.id=p.user_id AND u.identity_kind='local'
                JOIN welcome_upstream_cookies c ON c.user_id=p.user_id AND c.name='qz_bank_user'
                WHERE EXISTS(SELECT 1 FROM welcome_sessions s WHERE s.user_id=p.user_id AND s.expires_at>?)''',
                (self.auth.clock(),)).fetchall()
        # 只接受原属该用户的独立工作区令牌；共享体验账户的 demo_sessions 令牌不能绑定真实手机号。
        matches = [dict(p) for p in candidates
                   if hmac.compare_digest(hashlib.sha256(p['bank_cookie'].encode()).hexdigest(), expected)]
        if len(matches) != 1:
            return None
        result = matches[0]
        result.pop('bank_cookie', None)
        return result

    def _resolve_registered(self, workspace):
        """从可信实例登记解析owner；本次HTTP操作还必须匹配当前bank request的subject链。"""
        provider = self.experience_provider
        if provider is None:
            return None  # 私有模式不能回退旧共享jar或用任意demo token认领手机号。
        try:
            owner = provider.owner_for_workspace(workspace)
        except BankError:
            return None
        if not isinstance(owner,str) or re.fullmatch('[0-9a-f]{32}',owner) is None:
            return None
        request_context = getattr(getattr(provider,'demo_sessions',None),'_request',None)
        request = request_context.get() if request_context is not None else None
        if request is not None and request[1]!=workspace:
            return None
        # 先关闭welcome读连接，再检查银行lease，不在银行写锁内反取welcome锁。
        with self.auth.connect() as db:
            rows = db.execute('''SELECT p.*,s.token_hash AS auth_hash,s.expires_at AS auth_expires,
                c.value AS bank_cookie FROM sms_phone_bindings p
                JOIN welcome_users u ON u.id=p.user_id AND u.identity_kind='local'
                JOIN welcome_sessions s ON s.user_id=u.id
                JOIN welcome_session_upstream_cookies c ON c.token_hash=s.token_hash AND c.name='qz_bank_user'
                LEFT JOIN welcome_session_business_state b ON b.token_hash=s.token_hash
                WHERE u.registered_user_id=? AND s.expires_at>? AND COALESCE(b.revoked,0)=0''',
                (owner,self.auth.clock())).fetchall()
        matches=[]
        for raw in rows:
            row=dict(raw)
            if request is not None and not hmac.compare_digest(row['bank_cookie'],request[0]):
                continue
            try:
                # 确认/消费调用方可能已持银行写锁；这里只读_validate，不调用可能签发/登记的resume。
                lease = provider.validate(row['bank_cookie'],workspace)
            except BankError:
                continue
            if (lease.get('registered_user_id')==owner and lease.get('workspace_id')==workspace
                    and lease.get('auth_subject')=='welcome-'+row['auth_hash']
                    and lease.get('auth_expires_at')==row['auth_expires']
                    and type(lease.get('expires_at')) in (int,float)
                    and self.auth.clock()<lease['expires_at']<=row['auth_expires']
                    and lease.get('token_hash')==hashlib.sha256(row['bank_cookie'].encode()).hexdigest()):
                matches.append(row)
        if not matches or len({row['id'] for row in matches})!=1:
            return None
        result={key:matches[0][key] for key in ('id','user_id','phone','verified_at')}
        result['auth_hashes']=tuple(row['auth_hash'] for row in matches)
        return result

    def available(self, workspace):
        return self._resolve(workspace) is not None

    def binding_is_current(self, workspace, expected_hash):
        current = self._resolve(workspace)
        if not current:
            return False
        encoded = json.dumps(current['id'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hmac.compare_digest(hashlib.sha256(encoded.encode()).hexdigest(), expected_hash)

    def send(self, binding, *, deadline):
        phone = self._resolve(binding.subject)
        if not phone:
            return False
        self.auth.sms.send('bank-user:' + str(phone['user_id']), 'operation', phone['phone'],
            {'operation': asdict(binding), 'phone_binding_id': phone['id']},
            client_ip='bank-operation', challenge_id=binding.challenge_id,
            expires_at=binding.expires_at, deadline=deadline)
        current = self._resolve(binding.subject)
        return bool(current and current['id'] == phone['id'])

    def verify(self, binding, code, *, deadline):
        phone = self._resolve(binding.subject)
        if not phone:
            return None
        proof = self.auth.sms.verify(binding.challenge_id, 'bank-user:' + str(phone['user_id']),
            'operation', phone['phone'], code,
            {'operation': asdict(binding), 'phone_binding_id': phone['id']}, deadline=deadline)
        current = self._resolve(binding.subject)
        if not current or current['id'] != phone['id']:
            return None
        with self.auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # 外部核验完成后再次核对会话；验证只为这个既有操作签发一次凭据。
            if self.registered_mode:
                placeholders=','.join('?' for _ in phone['auth_hashes'])
                active=db.execute('''SELECT 1 FROM welcome_sessions s LEFT JOIN welcome_session_business_state b
                    ON b.token_hash=s.token_hash WHERE s.user_id=? AND s.expires_at>?
                    AND COALESCE(b.revoked,0)=0 AND s.token_hash IN ('''+placeholders+')',
                    (phone['user_id'],self.auth.clock(),*phone['auth_hashes'])).fetchone()
            else:
                active = db.execute('SELECT 1 FROM welcome_sessions WHERE user_id=? AND expires_at>?',
                                    (phone['user_id'], self.auth.clock())).fetchone()
            if not active:
                return None
            self.auth.sms.consume(db, proof)
        return SmsVerified(provider_id='aliyun-pnvs', proof_reference=proof.provider_reference,
            phone_binding_reference=phone['id'], binding=binding,
            verified_at=proof.verified_at, expires_at=min(proof.expires_at, binding.expires_at))

    def _recovery_phone(self, workspace):
        """恢复仅接受当前注册会话；拥有workspace或另一个已登录会话均不够。"""
        if not self.registered_mode or self.experience_provider is None:
            return None
        context = getattr(getattr(self.experience_provider, 'demo_sessions', None), '_request', None)
        request = context.get() if context is not None else None
        if request is None or request[1] != workspace:
            return None
        supports = getattr(self.auth.sms.transport, 'supports_purpose', None)
        if not callable(supports) or supports('financial_recovery') is not True:
            return None
        phone = self._resolve_registered(workspace)
        if not phone or len(phone['auth_hashes']) != 1:
            return None
        owner = self.experience_provider.owner_for_workspace(workspace)
        phone['identity'] = {'subject_ref': hashlib.sha256(owner.encode()).hexdigest(),
                             'session_ref': hashlib.sha256(request[0].encode()).hexdigest(),
                             'phone_binding_hash': hashlib.sha256(json.dumps(phone['id'], ensure_ascii=False,
                                 sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        return phone

    def recovery_identity(self, workspace):
        phone = self._recovery_phone(workspace)
        return phone['identity'] if phone else None

    def _recovery_bound(self, binding):
        from base_runtime.security_recovery import RecoveryBinding, PURPOSE, POLICY
        if not isinstance(binding, RecoveryBinding) or binding.purpose != PURPOSE or binding.policy != POLICY:
            return None
        phone = self._recovery_phone(binding.subject)
        if not phone or any(getattr(binding, k) != v for k, v in phone['identity'].items()):
            return None
        return phone

    def send_recovery(self, binding, *, deadline):
        phone = self._recovery_bound(binding)
        if not phone:
            return False
        self.auth.sms.send('bank-user:' + str(phone['user_id']), 'financial_recovery', phone['phone'],
            {'recovery': asdict(binding), 'phone_binding_id': phone['id']}, client_ip='bank-recovery',
            challenge_id=binding.challenge_id, expires_at=binding.expires_at, deadline=deadline)
        return self._recovery_bound(binding) is not None

    def verify_recovery(self, binding, code, *, deadline):
        from base_runtime.security_recovery import RecoveryVerified
        phone = self._recovery_bound(binding)
        if not phone:
            return None
        proof = self.auth.sms.verify(binding.challenge_id, 'bank-user:' + str(phone['user_id']),
            'financial_recovery', phone['phone'], code,
            {'recovery': asdict(binding), 'phone_binding_id': phone['id']}, deadline=deadline)
        if self._recovery_bound(binding) is None:
            return None
        with self.auth.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute('''SELECT 1 FROM welcome_sessions s
                LEFT JOIN welcome_session_business_state b ON b.token_hash=s.token_hash
                WHERE s.token_hash=? AND s.user_id=? AND s.expires_at>? AND COALESCE(b.revoked,0)=0''',
                (phone['auth_hashes'][0], phone['user_id'], self.auth.clock())).fetchone()
            if not active:
                return None
            self.auth.sms.consume(db, proof)
        return RecoveryVerified(binding, 'aliyun-pnvs', proof.provider_reference,
                                proof.verified_at, min(proof.expires_at, binding.expires_at))
