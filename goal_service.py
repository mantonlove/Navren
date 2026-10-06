"""同一财务目标的有版本草案、人工确认和回执；日历仅投影目标期限。"""
import hashlib
import json
import re
import uuid
from contextlib import closing
from datetime import date
from simulated_bank import BankError
from simulated_bank.service import strict_fields, integer
from user_memory import privacy_revision, invalidate_graph


def version(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


class GoalService:
    def __init__(self, bank):
        self.bank=bank
        with bank.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS navren_goal_drafts(
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                goal_id TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, privacy_revision INTEGER NOT NULL,
                status TEXT NOT NULL, expires_at REAL NOT NULL, result TEXT,
                UNIQUE(workspace_id,idempotency_key))''')
            if 'occupancy_binding' not in {r[1] for r in db.execute('PRAGMA table_info(navren_goal_drafts)')}:
                db.execute('ALTER TABLE navren_goal_drafts ADD COLUMN occupancy_binding TEXT')
            from base_runtime.finance_occupancy import SCHEMA
            for statement in SCHEMA:db.execute(statement)

    def _items(self,db,w):
        self.bank._workspace(db,w)
        return [self._item(dict(r)) for r in db.execute('SELECT * FROM wealth_goals WHERE workspace_id=? ORDER BY due_on,id',(w,))]

    @staticmethod
    def _item(row):
        data={k:row[k] for k in ('id','name','target_minor','saved_minor','due_on','priority','status')}
        return {**data,'currency':'CNY','object_version':version(data),'source_scope':'simulation',
                'funding_state':'planning_only','saved_minor_basis':'declared_plan_allocation',
                'notice':'目标金额和已保存计划不等于银行冻结或实际付款。'}

    def state(self,w,goal_id=None):
        with closing(self.bank.connect()) as db:
            db.execute('BEGIN')
            items=self._items(db,w)
            projected=[{**item,**self._confirmation(db,w,item)} for item in items]
            result={'items':projected,'version':version(items),'as_of':self.bank._workspace(db,w)['as_of'],
                    'workspace_id':w,'privacy_revision':privacy_revision(db,w)}
            result['input_refs']=list(dict.fromkeys(ref for item in projected for ref in item['input_refs']))
            result['source_versions']={key:value for item in projected for key,value in item['source_versions'].items()}
            result['drafts']=[self._draft(dict(r)) for r in db.execute('SELECT * FROM navren_goal_drafts WHERE workspace_id=? ORDER BY rowid DESC LIMIT 20',(w,))]
        if goal_id:
            result['goal']=next((r for r in projected if r['id']==goal_id),None)
            if result['goal'] is None:raise BankError('GOAL_NOT_FOUND','目标不存在或已删除。',404)
        return result

    @staticmethod
    def _priority(value):
        if type(value) is not int or not 0<=value<=2**63-1:
            raise BankError('GOAL_INVALID','目标优先级必须为现有整数存储可表达的非负整数。')
        return value

    @staticmethod
    def _historical_confirmation(row,w,payload,receipt):
        """依原提交快照重算投影；不把当前资料版本写回历史回执。"""
        snapshot=receipt['goal'];ref='goal/'+w+'/'+row['goal_id']
        confirmation_ref='goal_confirmation/'+w+'/'+row['id']
        proof={'payload':payload,'digest':row['digest'],'receipt':receipt['receipt'],
               'goal_version':snapshot['object_version'],'privacy_revision':receipt['privacy_revision']}
        result={'confirmed':False,'confirmed_fields':['amount_minor','needed_on'],
                'funding_kind':None,'priority_confirmed':False,'input_refs':[ref,confirmation_ref],
                'source_versions':{ref:snapshot['object_version'],'goal_privacy/'+w:str(receipt['privacy_revision']),
                                   confirmation_ref:version(proof)},'reasons':[]}
        if 'priority' in payload:
            result['priority_confirmed']=True;result['confirmed_fields'].append('priority')
        else:result['reasons'].append('goal_priority_unconfirmed')
        if payload.get('funding_kind')=='cash':
            result['funding_kind']='cash';result['confirmed_fields'].append('funding_kind')
        else:result['reasons'].append('goal_cash_use_unconfirmed')
        if snapshot['status']!='active':result['reasons'].append('goal_not_active')
        result['confirmed']=not result['reasons']
        return result

    @staticmethod
    def _bound_payload(row):
        try:
            payload=json.loads(row['payload'])
            body={'draft_id':row['id'],'goal_id':row['goal_id'],'payload':payload}
            if row.get('occupancy_binding') is not None:
                body['occupancy_binding']=json.loads(row['occupancy_binding'])
            if (not isinstance(payload,dict) or payload.get('action') not in ('create','update','delete')
                    or row['digest']!=version(body)):
                raise ValueError()
            if payload['action']!='create' and payload.get('goal_id')!=row['goal_id']:raise ValueError()
            return payload
        except (ValueError,TypeError,KeyError):
            raise BankError('GOAL_CONFIRMATION_INVALID','目标草案的原始内容与摘要不一致。',409) from None

    @classmethod
    def _committed_result(cls,row,w):
        """只核验原回执完整性；成功重放不要求当前目标仍与旧回执相同。"""
        payload=cls._bound_payload(row)
        try:
            result=json.loads(row['result'])
            receipt={'id':row['id'],'state':'succeeded','action':payload['action'],'goal_id':row['goal_id']}
            if (row['workspace_id']!=w or not isinstance(result,dict) or result.get('workspace_id')!=w
                    or result.get('receipt')!=receipt or type(result.get('privacy_revision')) is not int
                    or result['privacy_revision']!=row['privacy_revision']+1):raise ValueError()
            if payload['action']=='delete':
                if result.get('goal') is not None:raise ValueError()
            else:
                goal=result.get('goal')
                fields=('id','name','target_minor','saved_minor','due_on','status')
                core={key:goal[key] for key in fields}
                if 'priority' in goal:core['priority']=cls._priority(goal['priority'])
                fixed=cls._item({**core,'priority':core.get('priority',3)})
                if (any(goal.get(key)!=fixed[key] for key in ('currency','source_scope','funding_state','saved_minor_basis'))
                        or core['id']!=row['goal_id']
                        or goal.get('object_version')!=version(core)
                        or core['name']!=payload['name'].strip()
                        or type(core['target_minor']) is not int or core['target_minor']!=payload['target_minor']
                        or type(core['saved_minor']) is not int or core['saved_minor']<0
                        or core['due_on']!=payload['due_on'] or core['status']!=payload.get('status','active')):raise ValueError()
                if 'priority' in payload and (cls._priority(payload['priority'])!=core.get('priority')):raise ValueError()
                if 'funding_kind' in payload and payload['funding_kind']!='cash':raise ValueError()
                metadata=cls._historical_confirmation(row,w,payload,result)
                if any(key in goal for key in metadata):
                    actual={key:goal[key] for key in metadata}
                    if version(actual)!=version(metadata):raise ValueError()
            return payload,result
        except (ValueError,TypeError,KeyError,AttributeError,BankError):
            raise BankError('GOAL_CONFIRMATION_INVALID','目标确认回执与原始内容不一致。',409) from None

    @classmethod
    def _confirmation(cls,db,w,item,committed=None):
        ref='goal/'+w+'/'+item['id']
        result={'confirmed':False,'confirmed_fields':[],'funding_kind':None,'priority_confirmed':False,
                'input_refs':[ref],'source_versions':{ref:item['object_version'],
                    'goal_privacy/'+w:str(privacy_revision(db,w))},'reasons':[]}
        row=committed or db.execute("SELECT * FROM navren_goal_drafts WHERE workspace_id=? AND goal_id=? "
                                  "AND status='committed' ORDER BY rowid DESC LIMIT 1",(w,item['id'])).fetchone()
        if not row:
            result['reasons']=['goal_confirmation_missing','goal_priority_unconfirmed','goal_cash_use_unconfirmed']
            return result
        row=dict(row)
        try:
            payload,receipt=cls._committed_result(row,w)
            if payload['action']=='delete':raise BankError('GOAL_CONFIRMATION_INVALID','目标已删除。',409)
            snapshot=receipt['goal']
            fields=('id','name','target_minor','saved_minor','due_on','status')
            if any(type(snapshot.get(k)) is not type(item[k]) or snapshot.get(k)!=item[k] for k in fields):
                raise BankError('GOAL_CONFIRMATION_INVALID','目标已改变。',409)
            if 'priority' in snapshot and snapshot['object_version']!=item['object_version']:
                raise BankError('GOAL_CONFIRMATION_INVALID','目标版本已改变。',409)
            result=cls._historical_confirmation(row,w,payload,receipt)
            result['source_versions'][ref]=item['object_version']
            result['source_versions']['goal_privacy/'+w]=str(privacy_revision(db,w))
        except BankError:
            result['reasons']=['goal_confirmation_invalid_or_stale']
        return result

    def constraints(self,w,db=None):
        """同 db 只读的 AllocationGoal 字段；方法 scope 由可信预算适配器绑定。"""
        if db is None:
            with closing(self.bank.connect()) as connection:
                connection.execute('BEGIN')
                return self.constraints(w,connection)
        items=self._items(db,w)
        collection_ref='goal_set/'+w
        goals=[];refs=[collection_ref];versions={collection_ref:version(items),
            'goal_privacy/'+w:str(privacy_revision(db,w))};reasons=[]
        for item in items:
            proof=self._confirmation(db,w,item)
            refs.extend(proof['input_refs']);versions.update(proof['source_versions'])
            reasons.extend(reason+':'+item['id'] for reason in proof['reasons'])
            if item['status']!='active':continue
            confirmed_fields=proof['confirmed_fields']
            goal={'subject_id':w,'currency':'CNY','data_scope':None,'goal_id':item['id'],
                  'amount_minor':item['target_minor'] if 'amount_minor' in confirmed_fields else None,
                  'priority':item['priority'] if proof['priority_confirmed'] else None,
                  'needed_on':item['due_on'] if 'needed_on' in confirmed_fields else None,
                  'confirmed':proof['confirmed'],'input_refs':proof['input_refs'],
                  'source_versions':proof['source_versions'],'already_occupied_minor':0,
                  'occupancy_refs':[],'occupancy_verified_in_baseline':False}
            if proof['confirmed']:goal['product_key']=None  # 原 committed 合同明确保留现金用途。
            from base_runtime.finance_occupancy import goal_occupancy_facts
            occupancy,gaps=goal_occupancy_facts(db,w,item['id'])
            goal.update({key:occupancy[key] for key in ('already_occupied_minor','occupancy_refs','occupancy_verified_in_baseline')})
            goal['input_refs']=list(dict.fromkeys([*goal['input_refs'],*occupancy['input_refs']]))
            goal['source_versions']={**goal['source_versions'],**occupancy['source_versions']}
            refs.extend(occupancy['input_refs']);versions.update(occupancy['source_versions'])
            reasons.extend(reason+':'+item['id'] for reason in gaps)
            goals.append(goal)
        return {'goals':goals,'input_refs':list(dict.fromkeys(refs)),'source_versions':versions,
                'reasons':reasons,'workspace_id':w,'source_scope':'simulation',
                'as_of':self.bank._workspace(db,w)['as_of'],'execution_authority':False}

    def prepare(self,w,p):
        strict_fields(p,['action','goal_id','name','target_minor','due_on','priority','funding_kind','status','expected_version','idempotency_key','occupancy_hold_ids','occupancy_account_id'],
                      ['action','expected_version','idempotency_key'])
        action=p['action']; key=p['idempotency_key']
        if action not in ('create','update','delete') or not isinstance(key,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{8,128}',key):
            raise BankError('GOAL_INVALID','目标操作或幂等键无效。')
        if action!='delete':
            if 'priority' in p:self._priority(p['priority'])
            if 'funding_kind' in p and p['funding_kind']!='cash':raise BankError('GOAL_INVALID','当前目标用途仅支持明确保留现金。')
        elif 'priority' in p or 'funding_kind' in p:raise BankError('GOAL_INVALID','删除目标不接受新的用途或优先级。')
        with self.bank.transaction() as db:
            items=self._items(db,w); current=next((r for r in items if r['id']==p.get('goal_id')),None)
            existing=db.execute('SELECT * FROM navren_goal_drafts WHERE workspace_id=? AND idempotency_key=?',(w,key)).fetchone()
            if existing:
                if json.loads(existing['payload'])!=p:raise BankError('IDEMPOTENCY_CONFLICT','同一目标操作不可更改内容。',409)
                return self._draft(dict(existing))
            expected=version(items) if action=='create' else current['object_version'] if current else None
            if expected is None:raise BankError('GOAL_NOT_FOUND','目标不存在或已删除。',404)
            if p['expected_version']!=expected:raise BankError('GOAL_VERSION_CONFLICT','目标已变化，请重新核对后准备。',409)
            goal_id=uuid.uuid4().hex if action=='create' else current['id']
            if action!='delete':
                if not isinstance(p.get('name'),str) or not 1<=len(p['name'].strip())<=100:raise BankError('GOAL_INVALID','目标名称需要1至100字。')
                integer(p.get('target_minor'),'目标金额',minimum=1)
                try:
                    if not isinstance(p.get('due_on'),str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',p['due_on']):raise ValueError()
                    date.fromisoformat(p['due_on'])
                except (ValueError,TypeError,KeyError):raise BankError('GOAL_INVALID','目标期限需要有效日期。')
                if p.get('status','active') not in ('active','completed'):raise BankError('GOAL_INVALID','目标状态无效。')
            from base_runtime.finance_occupancy import prepare_occupancy_binding
            occupancy=prepare_occupancy_binding(db,w,p,goal_id)
            draft_id=uuid.uuid4().hex; body=json.dumps(p,ensure_ascii=False,sort_keys=True)
            bound={'draft_id':draft_id,'goal_id':goal_id,'payload':p}
            if occupancy is not None:bound['occupancy_binding']=occupancy
            digest=version(bound)
            db.execute('INSERT INTO navren_goal_drafts(id,workspace_id,goal_id,payload,digest,idempotency_key,privacy_revision,status,expires_at,result,occupancy_binding) VALUES(?,?,?,?,?,?,?,?,?,NULL,?)',
                (draft_id,w,goal_id,body,digest,key,privacy_revision(db,w),'prepared',self.bank.clock()+1800,
                 json.dumps(occupancy,ensure_ascii=False,sort_keys=True) if occupancy is not None else None))
            row=dict(db.execute('SELECT * FROM navren_goal_drafts WHERE id=?',(draft_id,)).fetchone())
        return self._draft(row)

    def _draft(self,row):
        status=row['status']
        if status=='prepared' and self.bank.clock()>=row['expires_at']:status='expired'
        p=None;result=None;result_error=None
        if status=='committed':
            try:
                p=self._bound_payload(row)
                result=self._committed_result(row,row['workspace_id'])[1]
            except BankError:result=None;result_error='GOAL_CONFIRMATION_INVALID'
        else:
            p=json.loads(row['payload']);result=json.loads(row['result']) if row['result'] else None
        draft={'id':row['id'],'goal_id':row['goal_id'],'status':status,'summary':p,
                'confirmation_digest':row['digest'],'expires_at':row['expires_at'],
                'impacts':['目标确认记录需重新核验。' if p is None else
                           '目标及其截止日投影将更新；独立提醒和生活事件不变。' if p['action']!='delete' else
                           '删除目标及截止日投影；不删除独立提醒、银行交易或合同。'],
                'result':result}
        if result_error:draft['result_error']=result_error
        if row.get('occupancy_binding') is not None:
            try:draft['occupancy_binding']=json.loads(row['occupancy_binding'])
            except (ValueError,TypeError):draft['occupancy_binding']=None
        return draft

    def draft(self,w,draft_id):
        with closing(self.bank.connect()) as db:
            row=db.execute('SELECT * FROM navren_goal_drafts WHERE workspace_id=? AND id=?',(w,draft_id)).fetchone()
        if not row:raise BankError('GOAL_DRAFT_NOT_FOUND','目标草案不属于当前账户。',404)
        return self._draft(dict(row))

    def confirm(self,w,draft_id,p):
        strict_fields(p,['confirmed','confirmation_digest','priority','funding_kind'],['confirmed','confirmation_digest'])
        with self.bank.transaction() as db:
            row=db.execute('SELECT * FROM navren_goal_drafts WHERE workspace_id=? AND id=?',(w,draft_id)).fetchone()
            if not row:raise BankError('GOAL_DRAFT_NOT_FOUND','目标草案不属于当前账户。',404)
            row=dict(row)
            if p['confirmed'] is not True or p['confirmation_digest']!=row['digest']:raise BankError('CONFIRMATION_REQUIRED','请确认一致的目标变更摘要。')
            data=self._bound_payload(row)
            for field in ('priority','funding_kind'):
                if field in p:
                    if field=='priority':self._priority(p[field])
                    if field not in data or type(p[field]) is not type(data[field]) or p[field]!=data[field]:
                        raise BankError('CONFIRMATION_REQUIRED','优先级和用途必须与已展示的目标摘要一致。')
            if row['status']=='committed':return self._committed_result(row,w)[1]
            if row['status']=='cancelled':raise BankError('GOAL_DRAFT_CANCELLED','草案已取消，不能再确认。',409)
            if self.bank.clock()>=row['expires_at']:raise BankError('GOAL_DRAFT_EXPIRED','草案已过期，请重新准备。',409)
            if privacy_revision(db,w)!=row['privacy_revision']:raise BankError('GOAL_VERSION_CONFLICT','资料授权已变化，请重新核对目标。',409)
            items=self._items(db,w)
            current=next((r for r in items if r['id']==row['goal_id']),None)
            expected=version(items) if data['action']=='create' else current['object_version'] if current else None
            if expected!=data['expected_version']:raise BankError('GOAL_VERSION_CONFLICT','目标已变化，请重新核对，未覆盖新内容。',409)
            from base_runtime.finance_occupancy import validate_occupancy_binding, commit_occupancy_binding
            occupancy=validate_occupancy_binding(db,w,data,row)
            if data['action']=='delete':
                db.execute('DELETE FROM wealth_goals WHERE workspace_id=? AND id=?',(w,row['goal_id']))
                db.execute('DELETE FROM goal_allocations WHERE workspace_id=? AND goal_id=?',(w,row['goal_id']))
            elif data['action']=='create':
                customer=db.execute('SELECT id FROM customers WHERE workspace_id=? LIMIT 1',(w,)).fetchone()
                db.execute('INSERT INTO wealth_goals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (row['goal_id'],w,'qingzhou','qingzhou_synthetic','{"record_origin":"user_entered","user_confirmed":true}',customer['id'],data['name'].strip(),data['target_minor'],0,data['due_on'],data.get('priority',3),data.get('status','active')))
            else:
                db.execute('UPDATE wealth_goals SET name=?,target_minor=?,due_on=?,priority=?,status=? WHERE workspace_id=? AND id=?',
                    (data['name'].strip(),data['target_minor'],data['due_on'],data.get('priority',current['priority']),data.get('status','active'),w,row['goal_id']))
            db.execute('UPDATE workspaces SET version=version+1 WHERE id=?',(w,))
            invalidate_graph(db,w)
            updated=next((r for r in self._items(db,w) if r['id']==row['goal_id']),None)
            result={'goal':updated,'receipt':{'id':draft_id,'state':'succeeded','action':data['action'],'goal_id':row['goal_id']},
                    'workspace_id':w,'privacy_revision':privacy_revision(db,w)}
            if updated:
                prospective={**row,'status':'committed','result':json.dumps(result,ensure_ascii=False)}
                result['goal']={**updated,**self._confirmation(db,w,updated,prospective)}
            commit_occupancy_binding(db,w,row,data,updated,result,occupancy)
            db.execute("UPDATE navren_goal_drafts SET status='committed',result=? WHERE id=?",(json.dumps(result,ensure_ascii=False),draft_id))
            self.bank._audit(db,w,'goal_'+data['action'],{'goal_id':row['goal_id'],'draft_id':draft_id})
        return result

    def cancel(self,w,draft_id,p):
        strict_fields(p,[])
        with self.bank.transaction() as db:
            row=db.execute('SELECT * FROM navren_goal_drafts WHERE workspace_id=? AND id=?',(w,draft_id)).fetchone()
            if not row:raise BankError('GOAL_DRAFT_NOT_FOUND','草案不属于当前账户。',404)
            if row['status']=='committed':raise BankError('GOAL_ALREADY_COMMITTED','已完成变更不能作为未执行草案取消。',409)
            db.execute("UPDATE navren_goal_drafts SET status='cancelled' WHERE id=?",(draft_id,))
        return {'receipt':{'id':draft_id,'goal_id':row['goal_id'],'state':'cancelled'},'draft':self.draft(w,draft_id)}
