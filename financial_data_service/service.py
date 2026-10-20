"""有界、按主体隔离、只读；不接受SQL或用户提供的数据库路径。"""
from __future__ import annotations
from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import re
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
 'list_transfers':{'account_id','start_date','end_date','limit','offset'},
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
        self.start_date=self._day(self.metadata.get('start_date',START))
        self.end_date=self._day(self.metadata.get('end_date',END))
        if self.start_date>self.end_date:raise FinancialDataError('SCHEMA_UNSUPPORTED','财务库历史范围无效。')

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
        as_of=self.end_date
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
                if args.get('start_date',self.start_date)>args.get('end_date',self.end_date):raise FinancialDataError('INVALID_RANGE','开始日期晚于结束日期。')
                if 'as_of' in args:
                    as_of=args['as_of']
                    if not self.start_date<=as_of<=self.end_date:raise FinancialDataError('OUTSIDE_COVERAGE','摘要日期不在财务历史覆盖内。')
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
                'coverage_start':self.start_date,'coverage_end':self.end_date,'bank_integration':False,
                'synthetic_contracts':True,'valuation':'synthetic_not_real_NAV','dataset_state':'historical_read_only'}}

    def _query(self,db,subject,op,a,as_of):
        if op=='list_transfers':
            return self._list_transfers(db,subject,a)
        if op=='list_transactions':
            limit,offset=a.get('limit',50),a.get('offset',0)
            if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or not 0<=offset<=1000000:raise FinancialDataError('INVALID_PAGINATION','limit为1至200整数，offset为非负整数。')
            where=['subject_id=?','booked_on>=?','booked_on<=?'];v=[subject,a.get('start_date',self.start_date),a.get('end_date',self.end_date)]
            if 'account_id' in a:where.append('account_id=?');v.append(a['account_id'])
            clause=' AND '.join(where)
            total=db.execute('SELECT count(*) FROM transactions WHERE '+clause,v).fetchone()[0]
            items=_rows(db.execute('SELECT * FROM transactions WHERE '+clause+' ORDER BY occurred_at,transaction_id LIMIT ? OFFSET ?',v+[limit,offset]))
            return {'items':items,'total':total,'limit':limit,'offset':offset,'has_more':offset+len(items)<total,
                    'coverage_partial':a.get('start_date',self.start_date)<self.start_date or a.get('end_date',self.end_date)>self.end_date}
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
             'runtime_operations_included':False,'notice':'截至指定日期的账户历史；现行确认操作另在银行账本中保留，未重复相加。'}
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

    def _list_transfers(self,db,subject,a):
        """按实际转账事件聚合；本人账户调拨的两条账务分录只计一次。"""
        limit,offset=a.get('limit',8),a.get('offset',0)
        if type(limit) is not int or not 1<=limit<=12 or type(offset) is not int or not 0<=offset<=100000:
            raise FinancialDataError('INVALID_PAGINATION','limit为1至12整数，offset为0至100000整数。')
        start,end=a.get('start_date',self.start_date),a.get('end_date',self.end_date)
        where=['subject_id=?','kind IN (\'internal_transfer\',\'transfer\',\'external_transfer\')',
               'booked_on>=?','booked_on<=?']
        values=[subject,start,end]
        account=a.get('account_id')
        if account is not None:
            where.append('account_id=?');values.append(account)
        clause=' AND '.join(where)
        columns=('subject_id,transaction_id,account_id,occurred_at,booked_on,amount_minor,'
                 'signed_minor,currency,counterparty,kind,transfer_group_id')
        matched=[dict(row) for row in db.execute('SELECT '+columns+' FROM transactions WHERE '+clause+
                                                ' ORDER BY occurred_at,transaction_id',values)]
        groups={}
        for row in matched:
            group=row['transfer_group_id']
            key=('internal',group) if row['kind']=='internal_transfer' and isinstance(group,str) and group.strip() else ('row',row['transaction_id'])
            groups.setdefault(key,[]).append(row)
        # 查询账户仅用于选择事件；随后读取同主体完整组，保留另一侧身份。
        full_groups={}
        if any(key[0]=='internal' for key in groups):
            selected='SELECT DISTINCT transfer_group_id FROM transactions WHERE '+clause
            sql=('SELECT '+columns+' FROM transactions WHERE subject_id=? AND kind=\'internal_transfer\' '
                 'AND transfer_group_id IN ('+selected+') ORDER BY occurred_at,transaction_id')
            for row in db.execute(sql,[subject]+values):
                row=dict(row)
                full_groups.setdefault(row['transfer_group_id'],[]).append(row)
        items=[];missing=set();totals={}
        for key,selected_rows in groups.items():
            anchor=selected_rows[0]
            internal=anchor['kind']=='internal_transfer'
            legs=full_groups.get(key[1],[]) if key[0]=='internal' else selected_rows
            valid_currencies=all(isinstance(r['currency'],str) and
                                 re.fullmatch(r'[A-Z]{3}',r['currency']) for r in legs)
            currencies={r['currency'] for r in legs} if valid_currencies else set()
            currency=next(iter(currencies)) if len(currencies)==1 else None
            source=target=amount=signed=None
            counterparty=anchor['counterparty'] or None
            effect=anchor['signed_minor']
            direction='internal' if internal else ('outgoing' if type(effect) is int and effect<0 else
                                                   'incoming' if type(effect) is int and effect>0 else None)
            pair_status='unpaired' if internal else 'single_entry'
            item_missing=set()
            if internal:
                outgoing=[r for r in legs if type(r['signed_minor']) is int and r['signed_minor']<0]
                incoming=[r for r in legs if type(r['signed_minor']) is int and r['signed_minor']>0]
                # 一对等额、反向、同币种且不同本人账户才构成完整调拨。
                valid=(key[0]=='internal' and len(legs)==2 and len(outgoing)==1 and len(incoming)==1
                       and currency is not None and all(type(r['amount_minor']) is int and
                           type(r['signed_minor']) is int for r in legs)
                       and outgoing[0]['account_id']!=incoming[0]['account_id']
                       and outgoing[0]['amount_minor']==incoming[0]['amount_minor']>0
                       and outgoing[0]['signed_minor']==-outgoing[0]['amount_minor']
                       and incoming[0]['signed_minor']==incoming[0]['amount_minor'])
                if len(outgoing)==1:source=outgoing[0]['account_id']
                if len(incoming)==1:target=incoming[0]['account_id']
                if valid:
                    amount=outgoing[0]['amount_minor'];pair_status='paired'
                    if account is None:signed=0
                    elif account==source:direction='outgoing';signed=-amount
                    elif account==target:direction='incoming';signed=amount
                else:
                    item_missing.update(('transfer_pair','amount_minor'))
                    if key[0]!='internal':item_missing.add('transfer_group_id')
                    if source is None:item_missing.add('source_account_id')
                    if target is None:item_missing.add('target_account_id')
                    if account is not None:
                        signs={r['signed_minor']>0 for r in selected_rows
                               if type(r['signed_minor']) is int and r['signed_minor']!=0}
                        direction='incoming' if signs=={True} else 'outgoing' if signs=={False} else None
                        item_missing.add('signed_minor')
            else:
                value=anchor['amount_minor'];effect=anchor['signed_minor']
                if type(value) is int and value>0 and type(effect) is int and abs(effect)==value:
                    amount=value;signed=effect
                else:item_missing.update(('amount_minor','signed_minor'))
                if direction=='outgoing':source=anchor['account_id'];item_missing.add('target_account_id')
                elif direction=='incoming':target=anchor['account_id'];item_missing.add('source_account_id')
                else:item_missing.add('direction')
            if currency is None:item_missing.add('currency')
            if not counterparty:item_missing.add('counterparty')
            missing.update(item_missing)
            items.append({'subject_id':subject,'transfer_id':key[1],
                          'transaction_ids':[r['transaction_id'] for r in legs],
                          'booked_on':anchor['booked_on'],'occurred_at':anchor['occurred_at'],
                          'kind':anchor['kind'],'currency':currency,'amount_minor':amount,
                          'signed_minor':signed,'direction':direction,'source_account_id':source,
                          'target_account_id':target,'counterparty':counterparty,'pair_status':pair_status})
            if currency is not None and direction is not None:
                bucket=totals.setdefault((currency,direction),{'currency':currency,'direction':direction,
                                                              'transfer_count':0,'amount_minor':0})
                bucket['transfer_count']+=1
                if amount is None:bucket['amount_minor']=None
                elif bucket['amount_minor'] is not None:bucket['amount_minor']+=amount
        items.sort(key=lambda item:(item['occurred_at'],item['transfer_id']),reverse=True)
        count=len(items);page=items[offset:offset+limit];has_more=offset+len(page)<count
        dates=[row['booked_on'] for row in matched]
        return {'items':page,'transfer_count':count,'ledger_row_count':len(matched),
                'totals_by_currency_and_direction':[totals[key] for key in sorted(totals)],
                'actual_coverage_start':min(dates) if dates else None,
                'actual_coverage_end':max(dates) if dates else None,
                'offset':offset,'limit':limit,'has_more':has_more,'query_complete':True,
                'item_page_complete':offset==0 and not has_more,'missing_fields':sorted(missing),
                'coverage_partial':start<self.start_date or end>self.end_date,'runtime_ledger_included':False}
