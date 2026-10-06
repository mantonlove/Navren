"""NetworkX 来源图：实际记录、本人确认资料、对话待核实资料有不同来源。"""
from contextlib import closing
import json
import hashlib
import time
import uuid
from simulated_bank import BankError
from simulated_bank.service import strict_fields
from personal_data import explicit_self_statement

CATEGORIES = {'goal':'财务目标','shopping':'购物偏好','wealth':'理财偏好',
              'liquidity':'流动性偏好','life':'生活安排','communication':'交流偏好'}


class PersonalGraph:
    def __init__(self, gateway):
        self.g = gateway
        self.store = gateway.memories.store

    def remember(self, workspace, text, request_id, args):
        state = self.g.memories.state(workspace)
        if not state['enabled']:
            return {'saved':False,'notice':'用户尚未启用个性化记忆，未保存对话画像。'}
        if not isinstance(args,dict) or args.get('category') not in CATEGORIES:
            raise BankError('INVALID_PROFILE_CATEGORY','画像类别无效。')
        quote = args.get('quote')
        if (not isinstance(quote,str) or len(quote)<4 or quote not in text
                or not explicit_self_statement(text) or not explicit_self_statement(quote)):
            raise BankError('PROFILE_SOURCE_REQUIRED','只能保存本人明确原话，不能保存假设、第三人资料或模型推测。')
        # 候选正文来自原话，模型给出的 value 不具备写入权。
        value = self.g.memories._value(quote)
        try:
            source = self.store.source_statement(workspace,request_id)
        except BankError as error:
            if error.code!='INVALID_MEMORY_SOURCE':raise
            self.store.register_conversation_source(workspace,request_id,text,expected_revision=state['personal_revision'])
            source = self.store.source_statement(workspace,request_id)
        if quote not in source['text'] or source['expected_revision']!=state['personal_revision']:
            raise BankError('PROFILE_SOURCE_REQUIRED','候选与本轮已登记原话或个人版本不符。',409)
        node_id = hashlib.sha256((workspace+':'+request_id+':'+args['category']+':'+quote).encode()).hexdigest()[:32]
        with self.store.transaction(workspace,expected_revision=state['personal_revision'],change=False) as db:
            setting=db.execute('SELECT enabled FROM user_memory_settings WHERE workspace_id=?',(workspace,)).fetchone()
            if not setting or not setting[0]:raise BankError('MEMORY_DISABLED','记忆已关闭。',409)
            db.execute("INSERT OR IGNORE INTO profile_observations(id,workspace_id,category,value,quote,source_ref,status,created_at,expected_revision,confirmation_token,source_verified) VALUES(?,?,?,?,?,?,'candidate',?,?,?,1)",
                       (node_id,workspace,args['category'],self.store.protect(value),self.store.protect(quote),request_id,self.g.bank.clock(),state['personal_revision'],uuid.uuid4().hex))
            row=db.execute('SELECT confirmation_token,status FROM profile_observations WHERE workspace_id=? AND id=?',(workspace,node_id)).fetchone()
        return {'saved':True,'status':row['status'],'value':value,'observation_id':node_id,
                'confirmation_token':row['confirmation_token'],'expected_revision':state['personal_revision'],
                'notice':'加入本人原话待核实区；未改变正式风险评级、余额或操作权限。'}

    def update(self, workspace, node_id, payload):
        strict_fields(payload,{'decision','confirmation_token','expected_revision'},{'decision'})
        if payload['decision'] not in ('confirm','delete'):
            raise BankError('INVALID_DECISION','请选择确认或删除。')
        with self.store.transaction(workspace,expected_revision=payload.get('expected_revision'),event='profile_'+payload['decision'],object_id=node_id) as db:
            row=db.execute('SELECT * FROM profile_observations WHERE id=? AND workspace_id=?',(node_id,workspace)).fetchone()
            if not row:raise BankError('NOT_FOUND','画像记录不存在。',404)
            if payload['decision']=='delete':
                db.execute('DELETE FROM profile_observations WHERE id=? AND workspace_id=?',(node_id,workspace))
                self.store.discard_unused_source(db,workspace,row['source_ref'])
            else:
                import hmac
                revision=self.store.check_revision(db,workspace,None)
                setting=db.execute('SELECT enabled FROM user_memory_settings WHERE workspace_id=?',(workspace,)).fetchone()
                if not setting or not setting[0]:raise BankError('MEMORY_DISABLED','记忆已关闭，候选不能确认。',409)
                token=payload.get('confirmation_token')
                if (type(payload.get('expected_revision')) is not int or not row['source_verified']
                        or row['status']!='candidate' or row['expected_revision']!=revision
                        or not isinstance(token,str) or not row['confirmation_token']
                        or not hmac.compare_digest(row['confirmation_token'],token)):
                    raise BankError('INVALID_PROFILE_CONFIRMATION','候选来源、确认凭据或个人版本不符，请重新核对。',409)
                source=db.execute('SELECT text,created_at FROM conversation_sources WHERE workspace_id=? AND source_ref=?',(workspace,row['source_ref'])).fetchone()
                if (not source or row['quote'] not in source['text'] or not explicit_self_statement(source['text'])
                        or self.g.bank.clock()-source['created_at']>86400):
                    raise BankError('PROFILE_SOURCE_REQUIRED','候选原话缺失、已过期或没有可靠本人依据。',409)
                db.execute("UPDATE profile_observations SET status='confirmed',confirmed_at=? WHERE id=? AND workspace_id=?",(self.g.bank.clock(),node_id,workspace))
        return self.read(workspace)

    def read(self, workspace, for_agent=False):
        import networkx as nx
        archive = self.g.bank.customer_archive(workspace)
        records, graph = archive['records'], nx.MultiDiGraph()
        def add(key, label, group, source, ref, **extra):
            graph.add_node(key,label=str(label)[:140],group=group,source=source,source_ref=ref,**extra)
            return key
        def link(a,b,relation,ref):
            graph.add_edge(a,b,relation=relation,source_ref=ref)
        add('self','我','self','account','current_workspace')
        for a in archive['accounts']:
            key=add('account:'+a['id'],a['name'],'account','simulation',a['id'],currency=a['currency'],balance_minor=a['balance_minor'])
            link('self',key,'持有账户',a['id'])
        profile=archive['profile']
        key=add('risk','模拟配置风险等级 R'+str(profile['risk_level']),'profile',archive.get('data_scope','simulation'),'formal_profile')
        link('self',key,'演示配置','formal_profile')
        merchants={r['id']:r['name'] for r in records.get('merchants',[])}
        txs={r['id']:r for r in records.get('archived_transactions',[])}
        txs.update({r['id']:r for r in records.get('transactions',[])})
        imported=self.g.source(workspace)['active_source']=='imported'
        if imported: txs={r['id']:r for r in self.g.transactions(workspace)}
        # 按来源和类别聚合，观测到的支出不自动等价为购物偏好。
        for tx in txs.values():
            source='imported_statement' if imported else 'simulation'
            kind='收款' if tx['delta_minor']>0 else '转出' if tx['kind']=='transfer' else '支付'
            category=tx.get('category') or kind
            cat='category:'+kind+':'+category
            if cat not in graph:
                add(cat,category,'receipt' if kind=='收款' else 'payment',source,'statement',transaction_count=0)
                link('self',cat,kind+'记录','statement')
            graph.nodes[cat]['transaction_count']+=1
            merchant=tx.get('merchant') or merchants.get(tx.get('merchant_id'),'未标注商户')
            key='merchant:'+hashlib.sha256((source+merchant).encode()).hexdigest()[:16]
            if key not in graph: add(key,merchant,'merchant',source,tx['id'],transaction_count=0,latest_on=tx['occurred_at'])
            graph.nodes[key]['transaction_count']+=1
            graph.nodes[key]['latest_on']=max(graph.nodes[key]['latest_on'],tx['occurred_at'])
            # 每组关系保留汇总和最多五条来源；不向浏览器投递整份流水。
            edge=next((d for _,b,d in graph.edges(cat,data=True) if b==key),None)
            if edge is None: link(cat,key,'发生交易',tx['id'])
        for collection, group, field, relation in [
            ('beneficiaries','person','display_name','保存收款人'),('relationships','person','display_name','关联人物'),
            ('cards','card','last_four','持有卡片'),('subscriptions','subscription','plan_name','订阅合同'),
            ('wealth_goals','goal','name','财务目标'),('loans','loan','loan_type','借贷记录'),
            ('insurance_policies','insurance','product_name','保障合同'),
            ('deposit_contracts','deposit','name','存款合同'),('employment','income','occupation','收入与职业')]:
            for r in records.get(collection,[]):
                label=r.get(field) or r.get('title') or r.get('goal_type') or relation
                if group=='card':label='尾号 '+str(label)
                key=add(collection+':'+r['id'],label,group,'simulation',r['id'],status=r.get('status',r.get('merchant_status')))
                link('self',key,relation,r['id'])
        for r in records.get('payment_instructions',[])+records.get('scheduled_payments',[]):
            a='account:'+r['account_id'];b='beneficiaries:'+r['beneficiary_id']
            if a in graph and b in graph:
                link(a,b,'计划付款' if r.get('next_due_on') else '转账记录',r['id'])
        products={p['id']:p['name'] for p in self.g.bank.products(workspace)}
        for r in records.get('holdings',[]):
            key=add('holding:'+r['product_id'],products.get(r['product_id'],'模拟持仓'),'wealth','simulation',r['product_id'])
            link('account:'+r['account_id'],key,'持仓本金',r['product_id'])
        memory=self.g.memories.state(workspace)
        with self.store.connection(workspace) as db:
            observations=[dict(r) for r in db.execute('SELECT * FROM profile_observations WHERE workspace_id=? ORDER BY created_at DESC LIMIT 100',(workspace,))]
        with closing(self.g.bank.connect()) as db:
            calendar_rows=[dict(r) for r in db.execute("SELECT id,payload FROM calendar_series WHERE workspace_id=? AND status='active'",(workspace,))]
        for event in calendar_rows:
            p=json.loads(event['payload'])
            key=add('calendar:'+event['id'],p['title'],'goal','user_calendar',event['id'],
                status=p['status'],start_on=p['start_on'],recurrence=p['recurrence'],budget_minor=p.get('budget_minor'),currency=p['currency'])
            link('self',key,'本人日历计划',event['id'])
        if memory['enabled'] or not for_agent:
            for m in memory['items']:
                if m['expired'] or (for_agent and not m['user_confirmed']):continue
                confirmed=m['user_confirmed']
                key=add('memory:'+m['node_id'],m['value'],'preference',
                        'user_confirmed' if confirmed else 'unverified_personal',m['node_id'],
                        category=m['category_label'],status='confirmed' if confirmed else 'unverified',user_confirmed=confirmed)
                link('self',key,'本人确认' if confirmed else '待重新核实',m['node_id'])
            for o in observations:
                if (o['expires_on'] and o['expires_on']<archive['as_of']) or (for_agent and (o['status']!='confirmed' or not o['source_verified'])):continue
                key=add('observation:'+o['id'],o['value'],'preference','conversation',o['source_ref'],
                        status=o['status'],category=CATEGORIES.get(o['category'],o['category']),quote=o['quote'],observation_id=o['id'],confirmation_token=o['confirmation_token'],expected_revision=o['expected_revision'])
                link('self',key,'待本人核实' if o['status']=='candidate' else '本人确认',o['source_ref'])
        if memory['enabled']:
            for event in calendar_rows:
                for node in json.loads(event['payload']).get('linked_memory_ids',[]):
                    if 'memory:'+node in graph:link('calendar:'+event['id'],'memory:'+node,'本人授权关联',event['id'])
        nodes=[dict(id=k,**v) for k,v in graph.nodes(data=True)]
        links=[dict(source=a,target=b,**d) for a,b,d in graph.edges(data=True)]
        return {'nodes':nodes,'links':links,'as_of':archive['as_of'],'memory_enabled':memory['enabled'],
                'observation_count':len(observations),'personal_revision':memory['personal_revision'],'statement_source':self.g.source(workspace)['active_source'],
                'notice':'交易记录是观测事实，不等于偏好或风险评级。对话资料待本人核实；个性化资料不授予金融权限。'}
