"""补充可重现的单客户完整档案，官方例子另库存放，不冒充第三方客户。"""
import calendar
import json
from datetime import date
from pathlib import Path

from .customer_schema import DATASET_VERSION, SPECS, SOURCES, columns, seed_fingerprint


def load_sources(db):
    for s in SOURCES:
        db.execute('INSERT OR IGNORE INTO data_sources VALUES(?,?,?,?,?,?)',
                   (s['id'],s['name'],s['version'],s['url'],s['license'],s['use']))
    examples = json.loads((Path(__file__).parent/'reference/fineract-client-examples.json').read_text())
    for r in examples:
        db.execute('INSERT OR IGNORE INTO reference_examples VALUES(?,?,?,?,?)',
                   ('fineract-client-'+str(r['id']),'fineract','m_client',json.dumps(r,ensure_ascii=False),'upstream_synthetic_sample'))


def populate(db, workspace, clock, core_seed_hash=None):
    if db.execute('SELECT 1 FROM workspace_datasets WHERE workspace_id=?',(workspace,)).fetchone():
        return
    db.execute('PRAGMA defer_foreign_keys=ON')
    prefix = workspace[:12]
    specs = {name:(source,columns(fields)) for name,_,source,fields in SPECS}
    def key(name): return prefix+'-'+name
    def add(table, name, *values, details=None):
        source, cols = specs[table]
        if len(values) != len(cols): raise ValueError(table+'字段数不符')
        row = dict(zip([c['name'] for c in cols],values))
        for c in cols:
            if c['target'] and c['target'] not in ('accounts','mandates','transactions','products') and row[c['name']] is not None:
                row[c['name']] = key(row[c['name']])
            if c['kind']=='json': row[c['name']] = json.dumps(row[c['name']],ensure_ascii=False)
        row.update(id=key(name),workspace_id=workspace,source_id=source.split(':')[0],
                   data_origin='qingzhou_synthetic',details=json.dumps(details or {},ensure_ascii=False))
        db.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        return key(name)

    ws = db.execute('SELECT * FROM workspaces WHERE id=?',(workspace,)).fetchone()
    accounts = list(db.execute('SELECT * FROM accounts WHERE workspace_id=? ORDER BY name',(workspace,)))
    main = next(r['id'] for r in accounts if r['id'].endswith('-main'))
    savings = next(r['id'] for r in accounts if r['id'].endswith('-savings'))
    add('bank_branches','branch','模拟深圳数字营业部','轻舟模拟银行','SIM-QZ-001','深圳','digital')
    add('customers','customer','branch','林舟（虚构）','1999-06-18','CN','active','2024-01-01','retail')
    add('identifiers','identity','customer','模拟身份证','SIM-ID-****-0618','2024-01-01','2034-01-01',1)
    add('contacts','mobile','customer','mobile','SIM-PHONE-****-1001',1,1)
    add('contacts','email','customer','email','linzhou@example.invalid',1,0)
    add('addresses','home','customer','home','CN','深圳','518000','模拟居住地址，不对应真实住址')
    add('addresses','mailing','customer','mailing','CN','深圳','518000','模拟通信地址，不对应真实住址')
    add('employment','employment','customer','软件工程师（虚构）','模拟科技公司',7800000,'salary','2026-06-01')
    add('tax_residencies','tax','customer','CN','SIM-TAX-****','declared','2026-01-01')
    add('kyc_reviews','kyc','customer','2026-06-01','2027-06-01','low','passed',0,0)
    add('documents','identity-document','customer','identity','模拟身份核验记录','2026-06-01','2027-06-01','simulation-no-binary-document','simulation://identity')
    add('documents','terms-document','customer','product_terms','模拟产品条款与风险说明','2026-06-01',None,'simulation-no-binary-document','simulation://terms')
    add('consents','consent-active','customer','accounts,transactions,wealth','active','2026-06-01','2027-06-01',None)
    add('consents','consent-revoked','customer','marketing','revoked','2026-01-01','2027-01-01','2026-09-01')
    for i,a in enumerate(accounts):
        add('account_details','account-detail-'+str(i),'customer',a['id'],'branch','SIM-ACCOUNT-****-'+str(1001+i),'current' if a['id']==main else 'savings','active','2024-01-01',0,0)
        add('signatories','owner-'+str(i),a['id'],'customer','owner','active')
        for kind,limit in [('transfer_daily',5000000),('transfer_single',2000000),('cash_daily',1000000)]:
            add('account_limits','limit-'+str(i)+'-'+kind,a['id'],kind,limit,0,'2026-10-03')
    add('account_restrictions','restriction-released',main,'debit','released','示例历史解除限制','2025-06-01','2025-06-02')
    add('deposit_contracts','matured-deposit',savings,0,180,'2024-12-01','2025-03-01','matured',4500,
        details={'historical_principal_minor':1000000,'notice':'存款已到期；本金为0，无新增当前资产或现金冻结'})
    for name,display,alias,number,verified,status in [
      ('mother','陈女士（虚构）','妈妈','2001',1,'active'),('friend','张同学（虚构）','同学小张','2002',1,'active'),
      ('same-name','张同学（虚构）','另一位张同学','2003',0,'active'),('blocked','测试风险收款人','风险测试','2004',0,'blocked')]:
        add('beneficiaries',name,display,alias,'SIM-PHONE-****-'+number,'SIM-BENEFICIARY-****-'+number,'模拟收款银行','CNY',verified,status)
    add('relationships','family','customer','陈女士（虚构）','mother','1975-10-18','mother')
    add('transfer_templates','family-template',main,'mother','每月家庭支持',100000,'生活费')
    add('transfer_templates','friend-template',main,'friend','聚餐转账',15000,'聚餐分摊')
    add('payment_instructions','payment-draft',main,'mother',100000,'CNY','awaiting_confirmation','2026-10-03','模拟待转账',None)
    add('payment_instructions','payment-failed',main,'blocked',500000,'CNY','failed','2026-10-02','风险拒绝样本','模拟收款人被拦截；未记账')
    add('scheduled_payments','monthly-transfer',main,'mother',100000,'monthly','2026-10-10','draft','2027-03-10')
    add('scheduled_payments','once-transfer',main,'friend',15000,'once','2026-10-08','draft',None)
    add('split_bills','dinner-split',main,'模拟周末聚餐',45000,'draft','2026-10-10')
    for i,name in enumerate(['林舟（虚构）','张同学（虚构）','李同学（虚构）']):
        add('split_participants','split-person-'+str(i),'dinner-split',name,15000,0,'unrequested','simulation://request/'+str(i))
    merchant_def = [('salary','模拟工资','0000','工资'),('rent','模拟房租','6513','住房'),('food','模拟餐饮','5812','餐饮'),
                    ('video','模拟视频会员','4899','订阅'),('cloud','模拟云盘','4816','订阅'),('fitness','模拟健身会员','7997','健康'),
                    ('shopping','模拟百货','5311','购物')]
    for name,title,mcc,category in merchant_def: add('merchants',name,title,mcc,category,'simulation')

    # 2025-01至2026-05的封存银行流水；其最后期末=首版2026-06期初。
    # 不改旧accounts期初、当前余额、原流水或订单。按月倒推，再逐笔向前验证。
    months = [(2025+m//12,m%12+1) for m in range(17)]
    monthly = [('salary',650000,650000,'income','工资',5),('rent',180000,-180000,'expense','住房',8),
               ('food',90000,-90000,'expense','餐饮',12),('video',2500,-2500,'expense','订阅',15),
               ('shopping',280000,-280000,'expense','购物',20),('cloud',1800,-1800,'expense','订阅',22),
               ('fitness',29900,-29900,'expense','健康',25),('shopping',4000,4000,'refund','购物',27)]
    net = sum(x[2] for x in monthly)
    for a in accounts:
        closing = a['opening_balance_minor']
        opening = closing - net*len(months) if a['id']==main else closing
        # 倒推期初可能为负时用无消费历史，而不能伪造负存款或覆盖原始余额。
        effective_monthly = monthly if a['id']==main and opening>=0 else []
        if not effective_monthly: opening = closing
        balance = opening
        for year,month in months:
            before,credits,debits = balance,0,0
            for index,(merchant,amount,delta,kind,category,day) in enumerate(effective_monthly):
                add('archived_transactions',f'archive-{a["id"]}-{year}-{month}-{index}',a['id'],f'{year}-{month:02d}-{day:02d}T12:00:00+08:00',merchant,amount,delta,kind,category,'模拟封存流水')
                balance += delta
                if delta>0: credits += delta
                else: debits -= delta
            add('statement_periods',f'statement-{a["id"]}-{year}-{month}',a['id'],f'{year}-{month:02d}-01',f'{year}-{month:02d}-{calendar.monthrange(year,month)[1]}',before,balance,credits,debits,'complete','archive')
        if balance != closing: raise ValueError('封存账本与首版期初不衔接')

    mandates = list(db.execute('SELECT * FROM mandates WHERE workspace_id=?',(workspace,)))
    for i,m in enumerate(mandates):
        merchant = 'video' if '视频' in m['merchant'] else 'cloud' if '云盘' in m['merchant'] else 'fitness'
        add('subscriptions','subscription-'+str(i),m['account_id'],m['id'],merchant,m['merchant'],m['amount_minor'],'monthly',m['next_charge_date'],'active','模拟商户页面；无真实取消能力')
        add('subscription_events','renewal-'+str(i),'subscription-'+str(i),'renewed','2026-09-05','recorded',m['amount_minor'])
    add('cards','debit','customer',main,'debit','SIM-UnionPay','1001','active','2024-01-01','2029-01-01',0,0)
    add('cards','credit','customer',main,'credit','SIM-UnionPay','3001','active','2024-06-01','2029-06-01',1000000,200000)
    add('cards','lost-card','customer',savings,'debit','SIM-UnionPay','1002','lost','2024-01-01','2029-01-01',0,0)
    for card in ['debit','credit','lost-card']:
        for kind,enabled,limit in [('online',1,0),('overseas',0,0),('contactless',1,0),('single_limit',1,200000)]:
            if card=='lost-card': enabled=0
            add('card_controls',card+'-'+kind,card,kind,enabled,limit,'2026-10-03')
    add('card_applications','issued-application','customer','credit','credit',1000000,'issued','2024-06-01','模拟历史审批')
    add('card_applications','draft-application','customer',None,'credit',1500000,'draft','2026-10-03','尚未提交银行审批')
    add('card_limit_requests','limit-request','credit',1500000,'draft','2026-10-03','模拟提额申请草案')
    add('card_events','card-issued','credit','issued','2024-06-01','recorded','模拟历史发卡')
    add('card_events','card-lost','lost-card','lost','2026-09-30','recorded','历史模拟挂失')
    add('credit_statements','credit-open','credit','2026-09-01','2026-09-30',200000,20000,'2026-10-20',0,'open')
    add('credit_statements','credit-paid','credit','2026-08-01','2026-08-31',100000,10000,'2026-09-20',100000,'paid')
    for i,amount in enumerate([120000,80000]): add('credit_statement_lines','credit-line-'+str(i),'credit-open','shopping',f'2026-09-{10+i:02d}',amount,'purchase','模拟信用卡账单，不扣活期')
    add('credit_statement_lines','credit-previous-line','credit-paid','shopping','2026-08-10',100000,'purchase','历史信用卡账单')
    add('loans','loan','customer',main,'模拟无抵押消费贷款',650000,600000,480,'2026-09-01','2027-10-01','active',0)
    add('loan_installments','installment-paid','loan','2026-10-01',50000,2600,0,52600,'paid')
    add('loan_repayments','repayment','loan','installment-paid','2026-10-01',52600,'archived','模拟外部还款记录；不属于当前活期流水')
    for i in range(12):
        y,m = 2026+(10+i)//12,(10+i)%12+1
        add('loan_installments','installment-'+str(i),'loan',f'{y}-{m:02d}-01',50000,(600000-i*50000)*480//120000,0,0,'scheduled')
    for p in db.execute('SELECT id,data FROM products ORDER BY id'):
        product = json.loads(p['data'])
        if product.get('historical_synthetic_position') or product.get('execution_enabled') is False or product.get('workspace_id'):
            continue  # 历史头寸没有业务条款，不能给新工作区拼造发行方和资格。
        add('product_terms','terms-'+p['id'],p['id'],'轻舟模拟发行方','cash' if 'CASH' in p['id'] else 'fixed_income' if 'BOND' in p['id'] else 'mixed',0,product['risk_notice'],'principal_only','terms-document')
    profile = db.execute('SELECT * FROM profiles WHERE workspace_id=?',(workspace,)).fetchone()
    add('risk_assessments','assessment','customer','2026-10-03','2027-10-03',profile['risk_level'],profile['horizon_days'],profile['reserve_minor'],'current',{'source':'模拟问卷','loss_tolerance':'low','experience':'basic'})
    for name,title,target,saved,day,priority in [('reserve','应急资金',1000000,1000000,'2026-12-31',1),('study','进修储蓄',3000000,800000,'2027-06-01',2),('birthday','家庭生日预算',50000,0,'2026-10-18',3)]:
        add('wealth_goals',name,'customer',title,target,saved,day,priority,'active')
    add('budgets','food-budget',main,'2026-10','餐饮',100000,'active')
    add('budgets','shopping-budget',main,'2026-10','购物',100000,'active')
    add('advice_records','advice','customer','assessment','2026-10-03','draft',{'candidate_product':'SIM-CASH-01','proposed_amount_minor':300000},'演示建议草案：先保留应急金，再比较流动性与风险；无真实推荐承诺',profile['reserve_minor'])
    for name,typ,price in [('flowers','flowers',19900),('cake','cake',25900)]:
        add('service_providers',name,'模拟'+('花店' if name=='flowers' else '蛋糕店'),typ,'深圳','simulation_only')
        add('service_quotes',name+'-quote',name,'模拟生日礼品',price,'2026-10-18','CNY','illustrative')
    add('orchestration_tasks','birthday-task','customer','妈妈生日礼物',50000,'2026-10-18','draft','每个资金操作单独确认；没有自动执行')
    add('task_steps','step-budget','birthday-task',1,'check_budget',0,'planned',None)
    add('task_steps','step-hold','birthday-task',2,'reserve_funds',45800,'planned','step-budget')
    add('task_steps','step-flowers','birthday-task',3,'book_flowers',19900,'planned','step-hold')
    add('task_steps','step-cake','birthday-task',4,'book_cake',25900,'planned','step-hold')
    add('fund_holds','birthday-hold',main,'birthday-task',45800,'proposed','2026-10-03','2026-10-19','模拟预留草案，尚未生效，不减少可用现金')
    add('service_reservations','cake-reservation','birthday-task','cake-quote','2026-10-18','draft',25900,None)
    add('notifications','subscription-reminder','customer','subscription','subscription-0','2026-10-05','draft','in_app','模拟续费提醒，未发送')
    add('notifications','loan-reminder','customer','loan_due','installment-0','2026-11-01','draft','in_app','模拟贷款到期提醒，未发送')
    duplicate = db.execute("SELECT id FROM transactions WHERE workspace_id=? AND note LIKE '%重复%' LIMIT 1",(workspace,)).fetchone()
    if not duplicate: duplicate = db.execute('SELECT id FROM transactions WHERE workspace_id=? LIMIT 1',(workspace,)).fetchone()
    add('disputes','dispute',duplicate['id'],'疑似重复扣款待核实','2026-10-03','draft',10000)
    add('support_cases','case','customer','模拟挂失咨询','2026-10-03','draft',None)
    add('security_devices','device','customer','模拟演示电脑','desktop','registered','2026-06-01','2026-10-03')
    add('security_devices','old-device','customer','模拟旧手机','mobile','revoked','2025-01-01','2026-09-01')
    add('login_events','login-ok','device','2026-10-03T09:00:00+08:00','success','simulation','演示成功')
    add('login_events','login-denied','old-device','2026-10-03T09:05:00+08:00','denied','simulation','演示已撤销设备')
    add('permission_grants','read-grant','customer','account_archive','read','active','2027-10-03')
    add('permission_grants','prepare-grant','customer','operation_draft','prepare','active','2027-10-03',details={'notice':'只读权限档案，不替代HTTP鉴权逻辑'})
    if core_seed_hash is None:
        row = db.execute("SELECT details FROM audit WHERE workspace_id=? AND event='workspace_created' ORDER BY id LIMIT 1",(workspace,)).fetchone()
        core_seed_hash = json.loads(row['details'])['seed_hash'] if row else db.execute("SELECT value FROM metadata WHERE key='seed_hash'").fetchone()[0]
    db.execute('INSERT INTO workspace_datasets VALUES(?,?,?,?,?)',(workspace,DATASET_VERSION,seed_fingerprint(),core_seed_hash,clock()))
