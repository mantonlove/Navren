"""有界、按主体隔离、只读；不接受SQL或用户提供的数据库路径。"""
from __future__ import annotations
from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3

from .schema import SCHEMA_VERSION, START, END

class FinancialDataError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)

OPERATIONS = {
 'list_connections':set(), 'list_accounts':set(), 'list_cards':{'account_id'},
 'get_account_overview':{'account_id'},
 'list_transactions':{'account_id','start_date','end_date','limit','offset'},
 'list_positions':{'account_id'}, 'list_insurance':{'status'}, 'get_policy':{'policy_id'},
 'list_assets':set(), 'list_obligations':{'start_date','end_date'}, 'get_financial_summary':{'as_of'},
}

def _rows(cursor):
    result=[]
    for r in cursor:
        row=dict(r)
        if 'details' in row:row['details']=json.loads(row['details'])
        result.append(row)
    return result

class FinancialDataService:
    def __init__(self, database_path):
        self.database_path=Path(database_path).expanduser().resolve()
        if not self.database_path.is_file():raise FinancialDataError('DATABASE_UNAVAILABLE','独立财务库尚未生成。')
        self._stat=self._signature()
        with closing(self._connect()) as db:
            self.metadata=dict(db.execute('SELECT key,value FROM metadata'))
        if self.metadata.get('schema_version')!=SCHEMA_VERSION:raise FinancialDataError('SCHEMA_UNSUPPORTED','财务库版本不兼容。')
        self.dataset_version=self.metadata['dataset_version']

    def _signature(self):
        s=self.database_path.stat()
        return s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns

    def _connect(self):
        if self._signature()!=self._stat:raise FinancialDataError('DATASET_CHANGED','财务库已更新，请重新装配读取服务。')
        db=sqlite3.connect(self.database_path.as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return db

    @staticmethod
    def _day(value):
        try:
            if not isinstance(value,str) or len(value)!=10 or date.fromisoformat(value).isoformat()!=value:raise ValueError
        except ValueError:raise FinancialDataError('INVALID_DATE','日期必须是真实的YYYY-MM-DD。') from None
        return value

    @staticmethod
    def _owned(db,subject,table,field,value):
        if not isinstance(value,str) or not value or len(value)>160:raise FinancialDataError('INVALID_ID','实体ID格式无效。')
        if not db.execute(f'SELECT 1 FROM {table} WHERE subject_id=? AND {field}=?',(subject,value)).fetchone():
            raise FinancialDataError('ENTITY_NOT_ACCESSIBLE','实体不存在或不属于当前主体。')

    def query(self, subject_id, operation, arguments):
        as_of=END
        try:
            if not isinstance(subject_id,str):raise FinancialDataError('SUBJECT_INVALID','读取主体无效。')
            if not isinstance(operation,str) or operation not in OPERATIONS:raise FinancialDataError('OPERATION_NOT_ALLOWED','读取操作未开放。')
            if type(arguments) is not dict or set(arguments)-OPERATIONS[operation]:raise FinancialDataError('INVALID_ARGUMENTS','读取参数不符合合同。')
            args=dict(arguments)
            if operation=='get_account_overview' and 'account_id' not in args:raise FinancialDataError('INVALID_ARGUMENTS','需要account_id。')
            if operation=='get_policy' and 'policy_id' not in args:raise FinancialDataError('INVALID_ARGUMENTS','需要policy_id。')
            with closing(self._connect()) as db:
                db.execute('BEGIN')
                if not db.execute('SELECT 1 FROM subjects WHERE subject_id=?',(subject_id,)).fetchone():raise FinancialDataError('SUBJECT_NOT_FOUND','读取主体未登记。')
                for field,table in (('account_id','accounts'),('policy_id','policies')):
                    if field in args:self._owned(db,subject_id,table,field,args[field])
                for field in ('start_date','end_date','as_of'):
                    if field in args:args[field]=self._day(args[field])
                if args.get('start_date',START)>args.get('end_date',END):raise FinancialDataError('INVALID_RANGE','开始日期晚于结束日期。')
                if 'as_of' in args:
                    as_of=args['as_of']
                    if not START<=as_of<=END:raise FinancialDataError('OUTSIDE_COVERAGE','摘要日期不在十年数据覆盖内。')
                data=self._query(db,subject_id,operation,args,as_of)
                if self._signature()!=self._stat:raise FinancialDataError('DATASET_CHANGED','读取期间财务库已更新。')
            return self._envelope(subject_id,as_of,data)
        except FinancialDataError as error:
            return {**self._envelope(subject_id,as_of,None),'error':{'code':error.code,'message':error.message}}
        except (sqlite3.Error,OSError):
            return {**self._envelope(subject_id,as_of,None),'error':{'code':'DATABASE_READ_FAILED','message':'独立财务库读取失败。'}}

    def _envelope(self,subject,as_of,data):
        return {'schema_version':SCHEMA_VERSION,'subject_id':subject,'source':'navren_synthetic',
                'dataset_version':self.dataset_version,'as_of':as_of,'data':data,
                'evidence':{'source_database_sha256':self.metadata.get('source_database_sha256'),
                'coverage_start':START,'coverage_end':END,'bank_integration':False,
                'synthetic_contracts':True,'valuation':'synthetic_not_real_NAV','dataset_state':'historical_read_only'}}

    def _query(self,db,subject,op,a,as_of):
        if op=='list_transactions':
            limit,offset=a.get('limit',50),a.get('offset',0)
            if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or not 0<=offset<=1000000:raise FinancialDataError('INVALID_PAGINATION','limit为1至200整数，offset为非负整数。')
            where=['subject_id=?','booked_on>=?','booked_on<=?'];v=[subject,a.get('start_date',START),a.get('end_date',END)]
            if 'account_id' in a:where.append('account_id=?');v.append(a['account_id'])
            clause=' AND '.join(where)
            total=db.execute('SELECT count(*) FROM transactions WHERE '+clause,v).fetchone()[0]
            items=_rows(db.execute('SELECT * FROM transactions WHERE '+clause+' ORDER BY occurred_at,transaction_id LIMIT ? OFFSET ?',v+[limit,offset]))
            return {'items':items,'total':total,'limit':limit,'offset':offset,'has_more':offset+len(items)<total,
                    'coverage_partial':a.get('start_date',START)<START or a.get('end_date',END)>END}
        if op=='get_financial_summary':
            b=_rows(db.execute('SELECT b.*,a.account_type FROM account_balances b JOIN accounts a USING(subject_id,account_id) WHERE b.subject_id=? AND b.date=?',(subject,as_of)))
            cash=sum(r['balance_minor'] for r in b if r['account_type']!='credit_card')
            credit=-sum(r['balance_minor'] for r in b if r['account_type']=='credit_card')
            val=_rows(db.execute('SELECT v.*,a.asset_type FROM valuations v JOIN assets a USING(subject_id,asset_id) WHERE v.subject_id=? AND v.date=?',(subject,as_of)))
            investments=sum(r['book_value_minor'] for r in val if r['asset_type'].startswith('fund_'))
            real=sum(r['book_value_minor'] for r in val if not r['asset_type'].startswith('fund_'))
            loans=db.execute('SELECT coalesce(sum(principal_minor),0) FROM loan_balances WHERE subject_id=? AND date=?',(subject,as_of)).fetchone()[0]
            if not b or not val:raise FinancialDataError('INCOMPLETE_COVERAGE','指定日期财务快照不完整。')
            return {'cash_minor':cash,'investment_minor':investments,'real_assets_minor':real,
             'loan_principal_minor':loans,'credit_card_outstanding_minor':credit,'liabilities_minor':loans+credit,
             'total_assets_minor':cash+investments+real,'net_worth_minor':cash+investments+real-loans-credit,
             'insurance_cash_value_minor':None,'insurance_sum_excluded':True,'credit_limits_excluded':True,
             'runtime_operations_included':False,'notice':'截至指定日期的合成历史；现行确认操作另在银行账本中保留，未重复相加。'}
        if op=='get_account_overview':
            account=_rows(db.execute('SELECT * FROM accounts WHERE subject_id=? AND account_id=?',(subject,a['account_id'])))[0]
            return {'account':account,'cards':_rows(db.execute('SELECT * FROM cards WHERE subject_id=? AND account_id=?',(subject,a['account_id']))),
                    'credit_facilities':_rows(db.execute('SELECT * FROM credit_facilities WHERE subject_id=? AND account_id=?',(subject,a['account_id']))),
                    'recent_card_statements':_rows(db.execute('SELECT * FROM card_statements WHERE subject_id=? AND account_id=? ORDER BY period_end DESC LIMIT 12',(subject,a['account_id']))),
                    'contracts':_rows(db.execute('SELECT * FROM contracts WHERE subject_id=? AND account_id=?',(subject,a['account_id'])))}
        if op=='get_policy':
            p=_rows(db.execute('SELECT * FROM policies WHERE subject_id=? AND policy_id=?',(subject,a['policy_id'])))[0]
            return {'policy':p,'premium_funding':_rows(db.execute('SELECT * FROM premium_funding WHERE subject_id=? AND policy_id=?',(subject,a['policy_id']))),
                    'claims':_rows(db.execute('SELECT * FROM claims WHERE subject_id=? AND policy_id=?',(subject,a['policy_id']))),
                    'rule':_rows(db.execute('SELECT * FROM rules WHERE rule_id=?',(p['rule_id'],)))[0]}
        table={'list_connections':'connections','list_accounts':'accounts','list_cards':'cards','list_positions':'positions','list_insurance':'policies','list_assets':'assets','list_obligations':'obligations'}[op]
        where=['subject_id=?'];v=[subject]
        if 'account_id' in a:where.append('account_id=?');v.append(a['account_id'])
        if 'status' in a:
            if a['status'] not in ('active','expired','cancelled','pending'):raise FinancialDataError('INVALID_STATUS','保险状态无效。')
            where.append('status=?');v.append(a['status'])
        if op=='list_obligations':
            for key,operator in (('start_date','>='),('end_date','<=')):
                if key in a:where.append('due_on'+operator+'?');v.append(a[key])
        items=_rows(db.execute('SELECT * FROM '+table+' WHERE '+' AND '.join(where)+' ORDER BY rowid LIMIT 201',v))
        if len(items)>200:raise FinancialDataError('RESULT_TOO_LARGE','列表超出读取上限；请缩小查询范围。')
        return {'items':items,'total':len(items),'has_more':False}
