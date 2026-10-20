"""当前工作区的用户录入收款联系人；保存资料不授予付款权限。"""
import re
import sqlite3
import uuid


SCHEMA = '''CREATE TABLE IF NOT EXISTS navren_remittance_contacts(
 id TEXT PRIMARY KEY,
 workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 recipient_name TEXT NOT NULL CHECK(length(recipient_name) BETWEEN 1 AND 64),
 recipient_account TEXT NOT NULL CHECK(length(recipient_account) BETWEEN 10 AND 32),
 recipient_bank TEXT NOT NULL CHECK(length(recipient_bank) BETWEEN 1 AND 100),
 recipient_branch TEXT NOT NULL DEFAULT '' CHECK(length(recipient_branch)<=100),
 alias TEXT NOT NULL DEFAULT '' CHECK(length(alias)<=64),
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(workspace_id,recipient_account,recipient_bank));
CREATE INDEX IF NOT EXISTS navren_remittance_contacts_workspace
 ON navren_remittance_contacts(workspace_id,created_at,id);'''

FIELDS = {'recipient_name', 'recipient_account', 'recipient_bank', 'recipient_branch', 'alias'}
REQUIRED = {'recipient_name', 'recipient_account', 'recipient_bank'}
LIMITS = {'recipient_name': 64, 'recipient_account': 32, 'recipient_bank': 100,
          'recipient_branch': 100, 'alias': 64}


def initialize(db):
    db.executescript(SCHEMA)


def normalize(payload):
    from .service import BankError, strict_fields
    strict_fields(payload, FIELDS, REQUIRED)
    result = {}
    for name, maximum in LIMITS.items():
        value = payload.get(name, '')
        if (not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise BankError('INVALID_CONTACT_FIELD', '联系人资料格式无效。')
        value = value.strip()
        if len(value) > maximum or (name in REQUIRED and not value):
            raise BankError('INVALID_CONTACT_FIELD', '请填写有效的联系人姓名、收款账号和开户银行。')
        result[name] = value
    account = result['recipient_account']
    if not re.fullmatch(r'[0-9]{10,32}', account) or not account.strip('0'):
        raise BankError('INVALID_CONTACT_ACCOUNT', '请提供完整的10至32位数字收款账号。')
    return result


def view(row):
    value = {name: row[name] for name in ('id', *LIMITS, 'created_at', 'updated_at')}
    account = value['recipient_account']
    return {**value, 'masked_account': '*' * (len(account) - 4) + account[-4:]}


def contacts(bank, workspace, payload=None, contact_id=None, action=None):
    from .service import BankError, strict_fields
    binding = {'environment': 'simulation', 'source': 'user_entered', 'workspace_id': workspace}
    with bank.transaction() as db:
        bank._workspace(db, workspace)
        if action is None:
            rows = db.execute('SELECT * FROM navren_remittance_contacts WHERE workspace_id=? '
                              'ORDER BY created_at,id', (workspace,)).fetchall()
            return {**binding, 'contacts': [view(row) for row in rows]}
        if action not in ('create', 'update', 'delete'):
            raise BankError('INVALID_FIELDS', '联系人操作无效。')
        if action != 'create':
            if not isinstance(contact_id, str) or not re.fullmatch(r'[a-f0-9]{32}', contact_id):
                raise BankError('CONTACT_NOT_FOUND', '联系人不存在或无权访问。', 404)
            row = db.execute('SELECT * FROM navren_remittance_contacts WHERE workspace_id=? AND id=?',
                             (workspace, contact_id)).fetchone()
            if row is None:
                raise BankError('CONTACT_NOT_FOUND', '联系人不存在或无权访问。', 404)
        if action == 'delete':
            strict_fields(payload, set())
            db.execute('DELETE FROM navren_remittance_contacts WHERE workspace_id=? AND id=?',
                       (workspace, contact_id))
            return {**binding, 'deleted': True, 'id': contact_id}
        values = normalize(payload)
        existing = db.execute('SELECT * FROM navren_remittance_contacts WHERE workspace_id=? '
                              'AND recipient_account=? AND recipient_bank=?',
                              (workspace, values['recipient_account'], values['recipient_bank'])).fetchone()
        if existing and (action == 'create' or existing['id'] != contact_id):
            if action == 'create' and all(existing[key] == value for key, value in values.items()):
                return {**binding, 'contact': view(existing)}
            raise BankError('CONTACT_EXISTS', '这个开户银行的收款账号已在联系人中，请编辑已有联系人。', 409)
        now = bank.clock()
        try:
            if action == 'create':
                contact_id = uuid.uuid4().hex
                db.execute('INSERT INTO navren_remittance_contacts('
                           'id,workspace_id,recipient_name,recipient_account,recipient_bank,recipient_branch,alias,created_at,updated_at) '
                           'VALUES(?,?,?,?,?,?,?,?,?)',
                           (contact_id, workspace, *(values[key] for key in LIMITS), now, now))
            else:
                db.execute('UPDATE navren_remittance_contacts SET '
                           'recipient_name=?,recipient_account=?,recipient_bank=?,recipient_branch=?,alias=?,updated_at=? '
                           'WHERE workspace_id=? AND id=?',
                           (*(values[key] for key in LIMITS), now, workspace, contact_id))
        except sqlite3.IntegrityError:
            raise BankError('CONTACT_EXISTS', '收款联系人资料冲突，请重新读取后核对。', 409) from None
        saved = db.execute('SELECT * FROM navren_remittance_contacts WHERE workspace_id=? AND id=?',
                           (workspace, contact_id)).fetchone()
        return {**binding, 'contact': view(saved)}
