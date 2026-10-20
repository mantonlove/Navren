"""本人已有借记卡的本地登记；不连接银行，不创建账户或资金。"""
from datetime import datetime
import hashlib
import json
import re
import uuid
from zoneinfo import ZoneInfo

from .connected_assets import INSTITUTIONS
from .service import BankError, encoded, strict_fields


SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS navren_card_registrations(
       card_fingerprint TEXT PRIMARY KEY,
       workspace_id TEXT NOT NULL REFERENCES workspaces(id),subject_id TEXT NOT NULL,
       account_id TEXT NOT NULL,card_id TEXT NOT NULL,recorded_at REAL NOT NULL,
       UNIQUE(workspace_id,card_id),
       FOREIGN KEY(workspace_id,account_id) REFERENCES accounts(workspace_id,id),
       FOREIGN KEY(workspace_id,card_id) REFERENCES cards(workspace_id,id))''',
    '''CREATE TABLE IF NOT EXISTS navren_card_registration_requests(
       workspace_id TEXT NOT NULL REFERENCES workspaces(id),idempotency_key TEXT NOT NULL,
       payload_fingerprint TEXT NOT NULL,receipt TEXT NOT NULL CHECK(json_valid(receipt)),
       PRIMARY KEY(workspace_id,idempotency_key))''',
)
FIELDS = {'account_id','card_number','confirmed','expected_version','expected_workspace_id',
          'expected_subject_id','expected_privacy_revision','idempotency_key'}
NOTICE = '已登记本人借记卡资料；未进行真实银行授权或验证，不新增账户、余额、授信或交易。'


def _identifier(value):
    return isinstance(value,str) and 1 <= len(value) <= 128 and not re.search(r'[\x00-\x1f\x7f]',value)


class CardBindings:
    def __init__(self,gateway):
        self.g = gateway
        self.bank = gateway.bank
        with self.bank.transaction() as db:
            for statement in SCHEMA:
                db.execute(statement)

    def register(self,workspace,payload):
        strict_fields(payload,FIELDS,FIELDS)
        if payload['confirmed'] is not True:
            raise BankError('CARD_BINDING_CONFIRMATION_REQUIRED','请核对本人银行卡和所属账户后确认登记。',422)
        if any(not _identifier(payload[key]) for key in
               ('account_id','expected_workspace_id','expected_subject_id','idempotency_key')):
            raise BankError('INVALID_FIELDS','账户、主体和登记凭据格式无效。',422)
        if type(payload['expected_version']) is not int or payload['expected_version'] < 0:
            raise BankError('INVALID_FIELDS','请先读取当前账户版本。',422)
        if type(payload['expected_privacy_revision']) is not int or payload['expected_privacy_revision'] < 0:
            raise BankError('INVALID_FIELDS','请先读取当前资料授权版本。',422)
        supplied = payload['card_number']
        if not isinstance(supplied,str) or len(supplied) > 80:
            raise BankError('CARD_NUMBER_INVALID','请输入16至19位银行卡号，可使用空格分组。',422)
        number = supplied.replace(' ','')
        if not re.fullmatch(r'[0-9]{16,19}',number):
            raise BankError('CARD_NUMBER_INVALID','请输入16至19位银行卡号，可使用空格分组。',422)
        fingerprint = hashlib.sha256(('navren:registered-card:v1:'+number).encode('ascii')).hexdigest()
        tail = number[-4:]
        # 日志、审计、幂等回执和数据库均不存完整卡号。
        request_fingerprint = hashlib.sha256(encoded({**{key:payload[key] for key in FIELDS-{'card_number'}},
                                                     'card_fingerprint':fingerprint}).encode()).hexdigest()
        with self.bank.transaction() as db:
            binding = self.g.connected_assets._binding(db,workspace)
            subject = binding['subject_id']
            from registered_instances import experience_identity
            actor = experience_identity(db,workspace=workspace)
            if not actor or actor['user_id'] != subject:
                raise BankError('CARD_BINDING_IDENTITY_REQUIRED','当前本人身份尚未完成账户接入。',409)
            if payload['expected_workspace_id'] != workspace or payload['expected_subject_id'] != subject:
                raise BankError('CARD_BINDING_IDENTITY_CHANGED','当前用户或账户已变化，请重新核对银行卡。',409)
            from user_memory import privacy_revision
            revision = privacy_revision(db,workspace)
            if revision != payload['expected_privacy_revision']:
                raise BankError('PRIVACY_REVISION_CHANGED','资料授权已变化，请重新读取后核对登记。',409)
            previous = db.execute('SELECT * FROM navren_card_registration_requests WHERE workspace_id=? AND idempotency_key=?',
                                  (workspace,payload['idempotency_key'])).fetchone()
            if previous:
                if previous['payload_fingerprint'] != request_fingerprint:
                    raise BankError('IDEMPOTENCY_CONFLICT','这项登记凭据已用于其他内容，请重新开始。',409)
                result = json.loads(previous['receipt'])
                return {**result,'changed':False,'replayed':True}
            current_version = self.bank._workspace(db,workspace)['version']
            if payload['expected_version'] != current_version:
                raise BankError('CARD_BINDING_VERSION_CHANGED','银行卡或账户资料已更新，请重新读取后核对登记。',409)
            account_id = payload['account_id']
            account = db.execute('''SELECT a.*,l.account_type,l.connection_id,d.customer_id,d.status AS account_status,
                                    c.status AS customer_status
                                    FROM accounts a JOIN connected_account_links l
                                    ON l.workspace_id=a.workspace_id AND l.account_id=a.id
                                    JOIN account_details d ON d.workspace_id=a.workspace_id AND d.account_id=a.id
                                    JOIN customers c ON c.workspace_id=d.workspace_id AND c.id=d.customer_id
                                    WHERE a.workspace_id=? AND a.id=?''',(workspace,account_id)).fetchone()
            if not account or account['account_type'] != 'bank_settlement' or account['account_status'] != 'active' or account['customer_status'] != 'active':
                raise BankError('CARD_BINDING_ACCOUNT_UNAVAILABLE','只能登记到本人当前可用的银行结算账户。',422)
            opening = json.loads(binding['snapshot'])
            connection = next((row for row in opening['connections']
                               if row['connection_id'] == account['connection_id'] and row['institution_type'] == 'bank'),None)
            if not connection:
                raise BankError('CARD_BINDING_BANK_UNAVAILABLE','开户银行与当前账户目录无法核对。',422)
            registration = db.execute('SELECT * FROM navren_card_registrations WHERE card_fingerprint=?',(fingerprint,)).fetchone()
            changed = registration is None
            if registration:
                if registration['workspace_id'] != workspace or registration['subject_id'] != subject or registration['account_id'] != account_id:
                    raise BankError('CARD_ALREADY_REGISTERED','这张卡已有登记，不能重复登记到其他账户。',409)
                card = db.execute('SELECT * FROM cards WHERE workspace_id=? AND id=?',
                                  (workspace,registration['card_id'])).fetchone()
                if not card or card['status'] != 'active':
                    raise BankError('CARD_BINDING_INACTIVE','原登记卡片已停止使用，请先核对已有卡片资料。',409)
                card_id = registration['card_id']
            else:
                if db.execute('SELECT count(*) FROM cards WHERE workspace_id=?',(workspace,)).fetchone()[0] >= 200:
                    raise BankError('CARD_BINDING_LIMIT_REACHED','当前银行卡目录已达到登记数量上限。',409)
                if db.execute("SELECT 1 FROM cards WHERE workspace_id=? AND account_id=? AND type='debit' AND last_four=?",
                              (workspace,account_id,tail)).fetchone():
                    raise BankError('CARD_NUMBER_AMBIGUOUS','该账户已有相同尾号的借记卡，请先核对现有卡片。',409)
                card_id = uuid.uuid4().hex
                now = self.bank.clock()
                day = datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai')).date().isoformat()
                details = encoded({'origin':'user_card_registration','registration_only':True,'real_connection':False,
                                   'recorded_on':day,'issued_on_unknown':True,'expires_on_unknown':True})
                # 旧档案日期列不可为空：登记日和远期哨兵仅为存储兼容值，
                # 回执明确返回未知发行/有效期，展示不得解释为真实发卡日期。
                db.execute('''INSERT INTO cards(id,workspace_id,source_id,data_origin,details,customer_id,account_id,
                             type,network,last_four,status,issued_on,expires_on,credit_limit_minor,outstanding_minor)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (card_id,workspace,'qingzhou','qingzhou_synthetic',details,account['customer_id'],account_id,
                            'debit','本人登记',tail,'active',day,'9999-12-31',0,0))
                db.execute('INSERT INTO connected_card_links VALUES(?,?,?,?,?)',
                           (workspace,card_id,'registered:card:'+card_id,None,'primary'))
                db.execute('INSERT INTO navren_card_registrations VALUES(?,?,?,?,?,?)',
                           (fingerprint,workspace,subject,account_id,card_id,now))
                self.bank._bump(db,workspace)
                self.bank._audit(db,workspace,'own_debit_card_registered',
                                 {'card_id':card_id,'account_id':account_id,'last_four':tail,
                                  'registration_only':True,'real_connection':False,'funds_unchanged':True})
            result = {'environment':'simulation','real_connection':False,'workspace_id':workspace,'workspace_binding':workspace,
                      'subject_id':subject,'current_account_id':subject,'privacy_revision':revision,
                      'version':self.bank._workspace(db,workspace)['version'],'changed':changed,'replayed':False,
                      'card':{'card_id':card_id,'account_id':account_id,'card_type':'debit','masked_number':'**** **** **** '+tail,
                              'status':'active','registration_only':True,'issued_on':None,'expires_on':None,
                              'institution_name':INSTITUTIONS.get(connection['institution_name'],connection['institution_name'])},
                      'notice':NOTICE}
            db.execute('INSERT INTO navren_card_registration_requests VALUES(?,?,?,?)',
                       (workspace,payload['idempotency_key'],request_fingerprint,encoded(result)))
            return result
