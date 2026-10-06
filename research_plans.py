"""研究备选方案保存与真实回执覆盖统计；不把名称转换为交易指令。"""
import json
import uuid
from contextlib import closing
from goal_service import version
from simulated_bank import BankError
from simulated_bank.service import strict_fields

class ResearchPlans:
    def __init__(self,g):
        self.g=g
        with g.bank.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS navren_research_plans(id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspaces(id),payload TEXT NOT NULL,created_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS navren_plan_operations(plan_id TEXT NOT NULL REFERENCES navren_research_plans(id),operation_id TEXT NOT NULL REFERENCES operations(id),PRIMARY KEY(plan_id,operation_id))')

    def _view(self,w,row):
        p=json.loads(row['payload']);stale=False
        for ref in p['products']:
            try:self.g.page_contexts.resolve(w,ref)
            except BankError:stale=True
        with closing(self.g.bank.connect()) as db:
            ops=[dict(r) for r in db.execute('SELECT o.id,o.state,o.payload FROM operations o JOIN navren_plan_operations po ON po.operation_id=o.id WHERE po.plan_id=? AND o.workspace_id=?',(row['id'],w))]
        completed={json.loads(op['payload']).get('product_id') for op in ops if op['state']=='succeeded'}
        covered=sum(r['source_scope']=='bank_product' and r['id'] in completed for r in p['products'])
        count=len(p['products']);status='implemented' if covered==count else 'partial' if covered else 'stale' if stale else 'candidate'
        return {**p,'id':row['id'],'created_at':row['created_at'],'object_version':version(p),'status':status,'source_stale':stale,
                'execution':{'status':'implemented' if covered==count else 'partial' if covered else 'not_started','covered':covered,'total':count,'operations':[{'id':o['id'],'state':o['state']} for o in ops]},
                'notice':'研究记录不是申购授权；落实进度仅按同一产品ID的已完成银行回执统计。'}

    def read(self,w):
        with closing(self.g.bank.connect()) as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM navren_research_plans WHERE workspace_id=? ORDER BY created_at DESC,id LIMIT 50',(w,))]
        return {'items':[self._view(w,r) for r in rows]}

    def create(self,w,p):
        strict_fields(p,['name','products','assumptions','confirmed'],['name','products','assumptions','confirmed'])
        if p['confirmed'] is not True:raise BankError('CONFIRMATION_REQUIRED','保存研究备选需要明确确认。')
        if not isinstance(p['name'],str) or not 1<=len(p['name'])<=100 or not isinstance(p['assumptions'],str) or len(p['assumptions'])>2000:
            raise BankError('PLAN_INVALID','名称或研究假设无效。')
        if not isinstance(p['products'],list) or not 2<=len(p['products'])<=3:raise BankError('PLAN_INVALID','备选包含2至3个稳定产品引用。')
        refs=[];keys=set()
        for ref in p['products']:
            if not isinstance(ref,dict) or ref.get('kind')!='product':raise BankError('PLAN_INVALID','需要带来源与版本的产品引用。')
            if ref.get('version') is None:raise BankError('PLAN_VERSION_REQUIRED','保存前需要核对每个产品的实际资料版本。')
            resolved=self.g.page_contexts.resolve(w,ref)
            if resolved['version'] is None:raise BankError('PLAN_VERSION_REQUIRED','产品资料版本尚无法核验，不能保存为有效研究备选。')
            key=(resolved['source_scope'],resolved['id'])
            if key in keys:raise BankError('PLAN_INVALID','产品不能重复。')
            keys.add(key);refs.append({k:v for k,v in resolved.items() if k!='execution_authority'})
        payload={**p,'products':refs};rid=uuid.uuid4().hex;now=self.g.bank.clock()
        with self.g.bank.transaction() as db:
            db.execute('INSERT INTO navren_research_plans VALUES(?,?,?,?)',(rid,w,json.dumps(payload,ensure_ascii=False),now))
            self.g.bank._audit(db,w,'research_plan_saved',{'plan_id':rid})
        return {'plan':self._view(w,{'id':rid,'payload':json.dumps(payload),'created_at':now})}

    def associate(self,w,rid,p):
        strict_fields(p,['operation_id','confirmed'],['operation_id','confirmed'])
        if p['confirmed'] is not True:raise BankError('CONFIRMATION_REQUIRED','关联回执需要确认。')
        with self.g.bank.transaction() as db:
            row=db.execute('SELECT * FROM navren_research_plans WHERE workspace_id=? AND id=?',(w,rid)).fetchone()
            if not row:raise BankError('PLAN_NOT_FOUND','备选方案不属于当前账户。',404)
            op=self.g.bank.operation(w,p['operation_id']);selected=json.loads(row['payload'])['products']
            if op['action']!='purchase' or not any(r['source_scope']=='bank_product' and r['id']==op['payload'].get('product_id') for r in selected):
                raise BankError('PLAN_OPERATION_MISMATCH','回执与方案产品的实际身份不一致。',409)
            db.execute('INSERT OR IGNORE INTO navren_plan_operations VALUES(?,?)',(rid,p['operation_id']))
        return {'plan':self._view(w,dict(row))}
