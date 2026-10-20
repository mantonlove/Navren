"""Agent 草案来源及审核绑定。模型没有写入审核记录或执行权限。"""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

POLICY_VERSION = 'navren-agent-policy-1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS agent_draft_reviews(
 operation_id TEXT PRIMARY KEY REFERENCES operations(id),
 workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','superseded')),
 payload_hash TEXT NOT NULL, account_version INTEGER NOT NULL,
 context_hash TEXT NOT NULL, evidence_hash TEXT,
 policy_version TEXT NOT NULL, expires_at REAL NOT NULL,
 reviewer_model TEXT, reviewed_at REAL, review_id TEXT);
CREATE TABLE IF NOT EXISTS agent_dialogues(
 workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),
 messages TEXT NOT NULL, updated_at REAL NOT NULL);
'''


def context_hash(db, workspace):
    """不含口令、确认凭据或草案；在银行同一读/写事务内计算关键数据修订。"""
    ws = db.execute('SELECT version,as_of FROM workspaces WHERE id=?', (workspace,)).fetchone()
    values = {'workspace': dict(ws)}
    # 账本 version 覆盖金融/档案变更；这些独立数据源不修改账本 version。
    for table in ('profiles', 'accounts', 'finance_data_sources', 'statement_datasets',
                  'periodic_candidate_marks', 'imported_periodic_marks',
                  'calendar_series','calendar_occurrence_overrides','wealth_valuation_points',
                  'goal_allocations','conversation_privacy_versions','portfolio_versions','portfolio_positions','portfolio_valuations','personal_decisions'):
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            where=" AND status='confirmed'" if table=='profile_observations' else ''
            values[table] = [dict(row) for row in db.execute(
                'SELECT * FROM '+table+' WHERE workspace_id=?'+where+' ORDER BY rowid', (workspace,))]
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='personal_data_binding'").fetchone():
        binding=db.execute('SELECT database_path FROM personal_data_binding WHERE id=1').fetchone()
        if binding:
            from .service import BankError
            private_path=Path(binding[0])
            if not private_path.is_file():raise BankError('PERSONAL_DATA_UNAVAILABLE','个人资料库不可用，审核依据无法核验。',503)
            with closing(sqlite3.connect(private_path.as_uri()+'?mode=ro',uri=True,timeout=10)) as personal_db:
                row=personal_db.execute('SELECT revision FROM personal_versions WHERE workspace_id=?',(workspace,)).fetchone()
                values['personal_revision']=row[0] if row else 0
    values['products'] = [dict(row) for row in db.execute('SELECT * FROM products ORDER BY id')]
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def register(db, bank, workspace, operation):
    row = bank._operation(db, workspace, operation)
    db.execute('''INSERT INTO agent_draft_reviews(operation_id,workspace_id,status,payload_hash,
                  account_version,context_hash,policy_version,expires_at)
                  VALUES(?,?,'pending',?,?,?,?,?)''',
               (operation, workspace, row['payload_hash'], row['version'],
                context_hash(db, workspace), POLICY_VERSION, row['expires_at']))


def require_approved(db, bank, workspace, row):
    from .service import BankError
    review = db.execute('SELECT * FROM agent_draft_reviews WHERE operation_id=? AND workspace_id=?',
                        (row['id'], workspace)).fetchone()
    if review is None:
        return  # 账户页由用户主动准备的原有操作仍走既有确认规则。
    if review['status'] != 'approved':
        raise BankError('AGENT_REVIEW_REQUIRED', '这份 Agent 草案尚未通过独立审核，不能执行。', 409)
    if (review['payload_hash'] != row['payload_hash']
            or review['account_version'] != row['version']
            or review['policy_version'] != POLICY_VERSION
            or bank.clock() >= review['expires_at']
            or review['context_hash'] != context_hash(db, workspace)):
        bank._reject(db, workspace, row, 'STALE_AGENT_REVIEW',
                     '审核依据或有效期已变化，请重新提问、准备并确认。', 'invalidated')


def bind_review(bank, workspace, operation, *, expected_context, evidence_hash,
                reviewer_model, review_id):
    from .service import BankError
    with bank.transaction() as db:
        row = bank._operation(db, workspace, operation)
        bank._check_pending(db, workspace, row)
        review = db.execute('SELECT * FROM agent_draft_reviews WHERE operation_id=? AND workspace_id=?',
                            (operation, workspace)).fetchone()
        if (review is None or review['status'] != 'pending'
                or review['payload_hash'] != row['payload_hash']
                or review['context_hash'] != expected_context
                or context_hash(db, workspace) != expected_context):
            raise BankError('STALE_AGENT_REVIEW', '草案或审核依据已变化，不能放行。', 409)
        db.execute("UPDATE agent_draft_reviews SET status='approved',evidence_hash=?,reviewer_model=?,"
                   'reviewed_at=?,review_id=? WHERE operation_id=?',
                   (evidence_hash, reviewer_model, bank.clock(), review_id, operation))
        bank._audit(db, workspace, 'agent_review_approved',
                    {'policy_version': POLICY_VERSION, 'evidence_hash': evidence_hash,
                     'reviewer_model': reviewer_model, 'review_id': review_id}, operation)
