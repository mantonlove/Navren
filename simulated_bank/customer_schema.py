"""单客户银行档案的数据合同；第三方模型适配，不是银行真实客户数据。"""
import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = 5
DATASET_VERSION = 'customer-bank-2026-10-06-v5'
LEGACY_SEED = '50a2f1257e568c23b3fe2e8e28fad09c4de01c8b7cfa7dc82d2340e79f508839'

# 格式：表名、领域、结构参照、字段。@指向工作区内的关联对象；?允许未知。
# 主体字段进入关系表；仅附加说明放入details，禁止用JSON代替金额/状态/关联。
SPECS = [
 ('bank_branches','客户与机构','fineract:m_office', 'name:text,bank_name:text,bank_code:text,city:text,channel:text'),
 ('customers','客户与机构','fineract:m_client', 'branch_id:@bank_branches,display_name:text,birth_date:date?,nationality:text,status:status(active|closed),opened_on:date,segment:text'),
 ('identifiers','身份与授权','fineract:m_client_identifier', 'customer_id:@customers,document_type:text,masked_number:text,issued_on:date,expires_on:date,verified:bool'),
 ('contacts','身份与授权','ibm:users', 'customer_id:@customers,kind:status(mobile|email|emergency),masked_value:text,verified:bool,preferred:bool'),
 ('addresses','身份与授权','fineract:m_address', 'customer_id:@customers,kind:status(home|mailing),country:text,city:text,postal_code:text,masked_address:text'),
 ('relationships','客户与机构','fineract:m_family_members', 'customer_id:@customers,display_name:text,relationship:text,birthday:date?,beneficiary_id:@beneficiaries?'),
 ('employment','身份与授权','ibm:users', 'customer_id:@customers,occupation:text,employer:text,annual_income_minor:money,income_source:text,verified_on:date'),
 ('tax_residencies','身份与授权','qingzhou:extension', 'customer_id:@customers,country:text,masked_tax_id:text,crs_status:status(declared|review_required),declared_on:date'),
 ('kyc_reviews','身份与授权','fineract:m_client', 'customer_id:@customers,reviewed_on:date,expires_on:date,risk_band:status(low|medium|high),status:status(passed|pending|expired),pep:bool,sanctions_match:bool'),
 ('documents','身份与授权','fineract:m_document', 'customer_id:@customers,document_type:text,title:text,issued_on:date,expires_on:date?,content_hash:text,storage_ref:text'),
 ('consents','身份与授权','openbanking:account-access-consents', 'customer_id:@customers,scope:text,status:status(active|revoked|expired),granted_on:date,expires_on:date,revoked_on:date?'),
 ('account_details','账户与存款','fineract:m_savings_account', 'customer_id:@customers,account_id:@accounts,branch_id:@bank_branches,masked_number:text,type:status(current|savings),status:status(active|frozen|closed),opened_on:date,interest_bps:int,overdraft_minor:money'),
 ('signatories','账户与存款','openbanking:parties', 'account_id:@accounts,customer_id:@customers,role:status(owner|authorized),status:status(active|revoked)'),
 ('account_limits','账户与存款','qingzhou:extension', 'account_id:@accounts,kind:status(transfer_daily|transfer_single|cash_daily),limit_minor:money,used_minor:money,effective_on:date'),
 ('account_restrictions','账户与存款','qingzhou:extension', 'account_id:@accounts,kind:status(debit|credit|all),status:status(proposed|released),reason:text,created_on:date,expires_on:date?'),
 ('deposit_contracts','账户与存款','fineract:m_deposit_account_term_and_preclosure', 'account_id:@accounts,principal_minor:money,interest_bps:int,started_on:date,matures_on:date,status:status(active|matured|closed),interest_paid_minor:money'),
 ('beneficiaries','转账与收款','openbanking:beneficiaries', 'display_name:text,alias:text,masked_mobile:text,masked_account:text,bank_name:text,currency:text,verified:bool,status:status(active|blocked)'),
 ('transfer_templates','转账与收款','openbanking:standing-orders', 'account_id:@accounts,beneficiary_id:@beneficiaries,label:text,amount_minor:money,remark:text'),
 ('payment_instructions','转账与收款','ibm:bank_xfers', 'account_id:@accounts,beneficiary_id:@beneficiaries,amount_minor:money,currency:text,status:status(draft|awaiting_confirmation|succeeded|failed|cancelled),requested_on:date,reference:text,failure_reason:text?'),
 ('scheduled_payments','转账与收款','openbanking:scheduled-payments', 'account_id:@accounts,beneficiary_id:@beneficiaries,amount_minor:money,frequency:status(once|monthly),next_due_on:date,status:status(draft|active|cancelled|completed|failed),end_on:date?'),
 ('split_bills','转账与收款','qingzhou:extension', 'account_id:@accounts,title:text,total_minor:money,status:status(draft|open|partially_paid|paid|cancelled),due_on:date'),
 ('split_participants','转账与收款','qingzhou:extension', 'split_id:@split_bills,display_name:text,share_minor:money,paid_minor:money,status:status(unrequested|requested|paid|declined|cancelled),request_ref:text'),
 ('merchants','账单与订阅','ibm:trans', 'name:text,mcc:text,category:text,channel:text'),
 ('archived_transactions','账单与订阅','fineract:m_savings_account_transaction', 'account_id:@accounts,occurred_at:text,merchant_id:@merchants,amount_minor:positive,delta_minor:signed,kind:status(income|expense|refund|transfer),category:text,reference:text'),
 ('statement_periods','账单与订阅','openbanking:statements', 'account_id:@accounts,period_start:date,period_end:date,opening_minor:money,closing_minor:money,credits_minor:money,debits_minor:money,status:status(complete),coverage:text'),
 ('subscriptions','账单与订阅','qingzhou:extension', 'account_id:@accounts,mandate_id:@mandates,merchant_id:@merchants,plan_name:text,amount_minor:money,cycle:status(monthly|yearly),next_renewal_on:date,merchant_status:status(active|cancelled),cancellation_route:text'),
 ('subscription_events','账单与订阅','qingzhou:extension', 'subscription_id:@subscriptions,event_type:status(renewed|reminder|cancellation_requested|cancelled),occurred_on:date,status:status(recorded|draft),amount_minor:money'),
 ('cards','卡片与信用','ibm:cards', 'customer_id:@customers,account_id:@accounts,type:status(debit|credit),network:text,last_four:text,status:status(active|lost|expired),issued_on:date,expires_on:date,credit_limit_minor:money,outstanding_minor:money'),
 ('card_controls','卡片与信用','qingzhou:extension', 'card_id:@cards,control_type:status(online|overseas|contactless|single_limit),enabled:bool,limit_minor:money,updated_on:date'),
 ('card_applications','卡片与信用','qingzhou:extension', 'customer_id:@customers,card_id:@cards?,requested_type:status(debit|credit),requested_limit_minor:money,status:status(issued|draft|submitted|declined),requested_on:date,decision_reason:text'),
 ('card_limit_requests','卡片与信用','qingzhou:extension', 'card_id:@cards,requested_limit_minor:money,status:status(draft|submitted|approved|declined),requested_on:date,reason:text'),
 ('card_events','卡片与信用','qingzhou:extension', 'card_id:@cards,event_type:status(issued|lost|blocked|unblocked|limit_updated),occurred_on:date,status:status(recorded),reason:text'),
 ('credit_statements','卡片与信用','openbanking:statements', 'card_id:@cards,period_start:date,period_end:date,total_due_minor:money,minimum_due_minor:money,due_on:date,paid_minor:money,status:status(open|paid)'),
 ('credit_statement_lines','卡片与信用','ibm:trans', 'statement_id:@credit_statements,merchant_id:@merchants,occurred_on:date,amount_minor:positive,kind:status(purchase|fee|refund),reference:text'),
 ('loans','贷款与负债','fineract:m_loan', 'customer_id:@customers,repayment_account_id:@accounts,loan_type:text,original_principal_minor:money?,outstanding_minor:money,annual_rate_bps:int,started_on:date?,matures_on:date?,status:status(active|closed),overdue_minor:money'),
 ('loan_installments','贷款与负债','fineract:m_loan_repayment_schedule', 'loan_id:@loans,due_on:date,principal_minor:money,interest_minor:money,fees_minor:money,paid_minor:money,status:status(paid|scheduled|overdue)'),
 ('loan_repayments','贷款与负债','fineract:m_loan_transaction', 'loan_id:@loans,installment_id:@loan_installments,occurred_on:date,amount_minor:money,status:status(archived),external_reference:text'),
 ('collateral','贷款与负债','fineract:m_loan_collateral', 'loan_id:@loans,description:text,value_minor:money,valued_on:date'),
 ('product_terms','理财与适当性','qingzhou:extension', 'product_id:@products,issuer:text,asset_class:text,fee_bps:int,risk_disclosure:text,valuation_mode:status(principal_only),document_id:@documents'),
 ('risk_assessments','理财与适当性','qingzhou:extension', 'customer_id:@customers,assessed_on:date,expires_on:date,risk_level:level,horizon_days:int,reserve_minor:money,status:status(initial|superseded|current),answers_json:json'),
 ('wealth_goals','理财与适当性','qingzhou:extension', 'customer_id:@customers,name:text,target_minor:money,saved_minor:money,due_on:date?,priority:int,status:status(active|completed)'),
 ('budgets','理财与适当性','qingzhou:extension', 'account_id:@accounts,period:text,category:text,limit_minor:money,status:status(active|archived)'),
 ('advice_records','理财与适当性','qingzhou:extension', 'customer_id:@customers,assessment_id:@risk_assessments,created_on:date,status:status(draft),recommendation_json:json,rationale:text,liquidity_floor_minor:money'),
 ('service_providers','跨场景任务','qingzhou:extension', 'name:text,service_type:status(flowers|cake),city:text,booking_mode:status(simulation_only)'),
 ('service_quotes','跨场景任务','qingzhou:extension', 'provider_id:@service_providers,item:text,price_minor:money,valid_until:date,currency:text,status:status(illustrative)'),
 ('orchestration_tasks','跨场景任务','qingzhou:extension', 'customer_id:@customers,title:text,budget_minor:money,due_on:date,status:status(draft|cancelled|in_progress|completed|failed),approval_policy:text'),
 ('task_steps','跨场景任务','qingzhou:extension', 'task_id:@orchestration_tasks,sequence:int,action:text,amount_minor:money,status:status(planned|succeeded|failed|cancelled),dependency_id:@task_steps?'),
 ('fund_holds','跨场景任务','qingzhou:extension', 'account_id:@accounts,task_id:@orchestration_tasks,amount_minor:money,status:status(proposed|held|consumed|expired|released),created_on:date,expires_on:date,reason:text'),
 ('service_reservations','跨场景任务','qingzhou:extension', 'task_id:@orchestration_tasks,quote_id:@service_quotes,delivery_on:date,status:status(draft|confirmed|completed|failed|cancelled),amount_minor:money,external_reference:text?'),
 ('notifications','提醒与服务','qingzhou:extension', 'customer_id:@customers,kind:status(subscription|loan_due|goal|security),related_entity:text,due_on:date,status:status(draft|read),channel:status(in_app),message:text'),
 ('disputes','提醒与服务','qingzhou:extension', 'transaction_id:@transactions,reason:text,opened_on:date,status:status(draft|resolved),requested_refund_minor:money'),
 ('support_cases','提醒与服务','qingzhou:extension', 'customer_id:@customers,topic:text,opened_on:date,status:status(draft|closed),resolution:text?'),
 ('security_devices','安全与审计','qingzhou:extension', 'customer_id:@customers,label:text,device_type:text,status:status(registered|revoked),registered_on:date,last_seen_on:date'),
 ('login_events','安全与审计','qingzhou:extension', 'device_id:@security_devices,occurred_at:text,result:status(success|denied),channel:text,reason:text'),
 ('permission_grants','安全与审计','qingzhou:extension', 'customer_id:@customers,scope:text,permission:status(read|prepare),status:status(active|revoked),expires_on:date'),
]

SOURCES = [
 {'id':'fineract','name':'Apache Fineract','version':'4684c663805560d5b6948045224440441fa4485c','url':'https://github.com/apache/fineract','license':'Apache-2.0','use':'核心银行实体概念、字段映射及7条官方示例客户；不是运行完整Fineract'},
 {'id':'ibm','name':'IBM Synthetic Data Sets schemas','version':'4cbd5aab7d8a18600ee0f4b622c225d44918ecd1 / v1.1.0','url':'https://github.com/IBM/IBM-Synthetic-Data-Sets','license':'Apache-2.0 repository','use':'公开数据结构和文档；没有购买或取得其商业完整数据集'},
 {'id':'openbanking','name':'Open Banking UK Read Write API','version':'484a1795680d9852600247878b9fd2bfe584c0aa / 4.0.1','url':'https://github.com/OpenBankingUK/read-write-api-specs','license':'Open Licence - reference only','use':'资源概念参照；不复制客户数据，不把Open Licence PDF当成本仓库代码的Apache许可'},
 {'id':'qingzhou','name':'轻舟补充模拟场景','version':DATASET_VERSION,'url':'local:simulated_bank/customer_seed.py','license':'项目原创模拟资料','use':'全部演示客户、金融产品、现金金额、卡片和场景记录由本项目生成'},
]


def columns(fields):
    result = []
    for item in fields.split(','):
        name, kind = item.split(':',1)
        nullable = kind.endswith('?')
        kind = kind.rstrip('?')
        target = kind[1:] if kind.startswith('@') else None
        if kind.startswith('status('):
            values = kind[7:-1].split('|')
            sql = 'TEXT CHECK('+name+' IN ('+','.join("'"+x+"'" for x in values)+'))'
        elif kind in ('money','positive','int','bool','level'):
            constraint = {'money':'>=0','positive':'>0','int':'>=0','bool':' IN (0,1)','level':' BETWEEN 1 AND 5'}[kind]
            sql = 'INTEGER CHECK(('+name+' IS NULL OR typeof('+name+")='integer') AND "+name+constraint+')'
        elif kind == 'signed':
            sql = 'INTEGER'
        elif kind == 'json':
            sql = 'TEXT CHECK(json_valid('+name+'))'
        elif kind == 'date':
            sql = 'TEXT CHECK('+name+" GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')"
        else:
            sql = 'TEXT'
        result.append({'name':name,'kind':kind,'nullable':nullable,'target':target,'sql':sql+('' if nullable else ' NOT NULL')})
    return result


def ddl():
    statements = [
        'CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_at REAL NOT NULL,description TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS data_sources(id TEXT PRIMARY KEY,name TEXT NOT NULL,version TEXT NOT NULL,url TEXT NOT NULL,license TEXT NOT NULL,usage TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS reference_examples(id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES data_sources(id),upstream_entity TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)),data_origin TEXT NOT NULL CHECK(data_origin=\'upstream_synthetic_sample\'))',
        'CREATE TABLE IF NOT EXISTS workspace_datasets(workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),dataset_version TEXT NOT NULL,seed_hash TEXT NOT NULL,core_seed_hash TEXT NOT NULL,populated_at REAL NOT NULL)',
    ]
    for table in ('accounts','mandates','transactions','orders'):
        statements.append(f'CREATE UNIQUE INDEX IF NOT EXISTS {table}_workspace_key ON {table}(workspace_id,id)')
    for name,domain,source,fields in SPECS:
        definition = ['id TEXT NOT NULL','workspace_id TEXT NOT NULL REFERENCES workspaces(id)',
                      'source_id TEXT NOT NULL REFERENCES data_sources(id)',
                      "data_origin TEXT NOT NULL CHECK(data_origin='qingzhou_synthetic')",
                      "details TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details))"]
        foreign_keys = []
        for column in columns(fields):
            definition.append(column['name']+' '+column['sql'])
            target = column['target']
            if target == 'products':
                foreign_keys.append('FOREIGN KEY('+column['name']+') REFERENCES products(id)')
            elif target:
                foreign_keys.append('FOREIGN KEY(workspace_id,'+column['name']+') REFERENCES '+target+'(workspace_id,id)')
        definition += ['PRIMARY KEY(workspace_id,id)'] + foreign_keys
        if name == 'account_details': definition.append('UNIQUE(workspace_id,account_id)')
        statements.append('CREATE TABLE IF NOT EXISTS '+name+'('+','.join(definition)+')')
    statements += [
      '''CREATE TABLE IF NOT EXISTS investment_lots(
       id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspaces(id),account_id TEXT NOT NULL,
       product_id TEXT NOT NULL REFERENCES products(id),order_id TEXT,
       acquired_on TEXT,locked_until TEXT NOT NULL,original_minor INTEGER NOT NULL CHECK(original_minor>0),
       remaining_minor INTEGER NOT NULL CHECK(remaining_minor>=0 AND remaining_minor<=original_minor),
       origin TEXT NOT NULL CHECK(origin IN ('legacy_aggregate','fixture','purchase')),
       UNIQUE(workspace_id,id),FOREIGN KEY(workspace_id,account_id) REFERENCES accounts(workspace_id,id),
       FOREIGN KEY(workspace_id,order_id) REFERENCES orders(workspace_id,id))''',
      '''CREATE TABLE IF NOT EXISTS lot_redemptions(
       workspace_id TEXT NOT NULL,lot_id TEXT NOT NULL,order_id TEXT NOT NULL,amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
       PRIMARY KEY(workspace_id,lot_id,order_id),FOREIGN KEY(workspace_id,lot_id) REFERENCES investment_lots(workspace_id,id),
       FOREIGN KEY(workspace_id,order_id) REFERENCES orders(workspace_id,id))''',
      'CREATE INDEX IF NOT EXISTS lots_available ON investment_lots(workspace_id,account_id,product_id,locked_until)',
      '''CREATE TRIGGER IF NOT EXISTS order_enum_insert BEFORE INSERT ON orders
         WHEN NEW.side NOT IN ('purchase','redeem') OR NEW.status NOT IN ('succeeded','pending_settlement','settled')
         BEGIN SELECT RAISE(ABORT,'invalid order enum'); END''',
      '''CREATE TRIGGER IF NOT EXISTS order_enum_update BEFORE UPDATE ON orders
         WHEN NEW.side NOT IN ('purchase','redeem') OR NEW.status NOT IN ('succeeded','pending_settlement','settled')
         BEGIN SELECT RAISE(ABORT,'invalid order enum'); END''',
    ]
    return statements


def contract():
    entities = []
    for name,domain,source,fields in SPECS:
        execution='只读档案；该表不驱动资金交易或真实银行业务'
        if name in ('payment_instructions','scheduled_payments','split_bills','split_participants'):
            execution='本机模拟转账、定时转账及AA状态；所有资金变更须独立确认并记入模拟账本'
        elif name in ('subscriptions','subscription_events'):
            execution='本机模拟商户取消结果与银行代扣分别记录；不连接真实商户或银行'
        elif name in ('cards','card_controls','card_applications','card_limit_requests','card_events'):
            execution='本机模拟卡片控制、申请与演示审核；不代表真实发卡或授信'
        elif name in ('orchestration_tasks','task_steps','fund_holds','service_reservations'):
            execution='本机模拟任务编排、资金预留与服务预约；需用户确认，不连接真实商户'
        entities.append({'table':name,'domain':domain,'schema_reference':source,
                         'record_origin':'qingzhou_synthetic','execution':execution,
                         'columns':[{k:v for k,v in c.items() if k!='sql'} for c in columns(fields)]})
    return {'schema_version':SCHEMA_VERSION,'dataset_version':DATASET_VERSION,'sources':SOURCES,'entities':entities,
            'null_means':'未知或不适用，不表示零；有无关系由覆盖报告明确说明',
            'scope':'一个虚构零售银行客户及银行可知的账户、借贷、理财与服务信息；不模拟整个银行内部数据仓库',
            'excluded':['密钥/支付密码/CVV/完整身份证号','银行内部资金池与总账全库','监管报送全库','不在银行托管的外部资产','真实医疗/司法/征信报告'],
            'money':'人民币分；只读信用/贷款档案不并入活期现金账本；持仓以本金计量',
            'future_change':'已知范围覆盖完整；新增业务通过版本化迁移扩展，不承诺永不调整结构'}


def write_contract():
    path = Path(__file__).parent/'customer-contract.json'
    path.write_text(json.dumps(contract(),ensure_ascii=False,indent=2)+'\n')
    return path


def seed_fingerprint():
    paths = [Path(__file__),Path(__file__).parent/'customer_seed.py']
    return hashlib.sha256(b''.join(p.read_bytes() for p in paths)).hexdigest()
