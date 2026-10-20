"""从旧合成事件按业务用途重构账户级资金链；不修改旧源或运行银行库。"""
from __future__ import annotations
import calendar
from collections import defaultdict
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import uuid

from .schema import DDL, SCHEMA_VERSION, START, END

NS=uuid.UUID('baee3c7d-91d5-5b79-939b-26cc0e86481f')
SOURCE='navren_synthetic'
REGION='浙江省A市（合成地区，非真实保单地域）'
AG_URL='https://jcs.moa.gov.cn/zcjd/202407/t20240702_6458234.htm'
CC_URL='https://ebank.ccb.com/cn/creditcard/service/card_zhangcheng.shtml'
DEP_URL='https://www.cmbchina.com/cmbpb/pfhelpurl/v60/HelpPage/203/03011.htm'
CONFIG={
 'tycoon': [('daily','招行合成连接','bank_settlement','daily'),('reserve','建行合成连接','bank_settlement','reserve'),('operating','工行合成连接','bank_settlement','operating'),('rent','工行合成连接','bank_settlement','daily'),('broker','中信证券合成连接','securities_cash','daily'),('wallet','微信支付合成连接','payment_wallet','daily'),('credit','招行合成连接','credit_card','daily')],
 'grain_farmer': [('daily','邮储合成连接','bank_settlement','daily'),('reserve','建行合成连接','bank_settlement','reserve'),('operating','农行合成连接','bank_settlement','operating'),('machinery','农商行合成连接','bank_settlement','operating'),('broker','中信证券合成连接','securities_cash','daily'),('wallet','支付宝合成连接','payment_wallet','daily'),('credit','邮储合成连接','credit_card','daily')],
 'working_landlady': [('daily','招行合成连接','bank_settlement','daily'),('reserve','建行合成连接','bank_settlement','reserve'),('operating','工行合成连接','bank_settlement','operating'),('rent1','工行合成连接','bank_settlement','daily'),('rent2','建行合成连接','bank_settlement','daily'),('rent3','农行合成连接','bank_settlement','daily'),('broker','中信证券合成连接','securities_cash','daily'),('wallet','微信支付合成连接','payment_wallet','daily'),('credit','建行合成连接','credit_card','daily')],
 'retired_entrepreneur': [('daily','工行合成连接','bank_settlement','daily'),('reserve','邮储合成连接','bank_settlement','reserve'),('operating','建行合成连接','bank_settlement','operating'),('broker','中信证券合成连接','securities_cash','daily'),('wallet','支付宝合成连接','payment_wallet','daily'),('shop_wallet','微信支付合成连接','payment_wallet','operating'),('credit','工行合成连接','credit_card','daily')],
}

def js(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def file_sha(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()
def readonly(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro&immutable=1',uri=True);c.row_factory=sqlite3.Row
    return c
def aid(uid,role):return uid+'::account:'+role

class Builder:
    def __init__(self,db,source,profiles):
        self.db,self.source,self.profiles=db,source,profiles
        self.txs=[];self.balances={};self.account_meta={};self.cards={};self.counter=0
        self.card_sequence=0
        self.synthetic_expense_components=0

    def insert(self,table,**v):
        for k in ('details',):
            if k in v and not isinstance(v[k],str):v[k]=js(v[k])
        self.db.execute('INSERT INTO '+table+'('+','.join(v)+') VALUES('+','.join('?' for _ in v)+')',list(v.values()))

    def tx(self,uid,account,stamp,signed,kind,category,**kw):
        assert type(signed) is int and signed!=0
        self.counter+=1
        ident=uid+'::transaction:'+str(self.counter)
        card=kw.pop('card_id',None)
        if card is None and account in self.cards:
            card=self.cards[account][0 if stamp[:10]<'2021-10-04' else -1]
        row={'subject_id':uid,'transaction_id':ident,'account_id':account,'occurred_at':stamp,'booked_on':stamp[:10],
             'status':'booked','amount_minor':abs(signed),'signed_minor':signed,'currency':'CNY',
             'counterparty':kw.pop('counterparty',category),'category':category,'kind':kind,
             'scope':kw.pop('scope','personal'),'card_id':card,'contract_id':kw.pop('contract_id',None),
             'mandate_id':kw.pop('mandate_id',None),'original_transaction_id':kw.pop('original_transaction_id',None),
             'transfer_group_id':kw.pop('transfer_group_id',None),'source_event_id':kw.pop('source_event_id',None),
             'source_transaction_id':kw.pop('source_transaction_id',None),'economic_event_id':kw.pop('economic_event_id',ident),
             'expense_minor':kw.pop('expense_minor',0),'details':js(kw.pop('details',{}))}
        assert not kw,kw
        self.txs.append(row);self.balances[account]+=signed
        return ident

    def transfer(self,uid,from_id,to_id,amount,stamp,reason,scope='personal'):
        if not amount:return
        group=uid+'::transfer:'+str(self.counter+1)
        common={'transfer_group_id':group,'economic_event_id':group,'scope':scope,'details':{'reconstruction':'deterministic_purpose_funding','reason':reason,'expense_counted':False}}
        self.tx(uid,from_id,stamp,-amount,'internal_transfer',reason,**common)
        self.tx(uid,to_id,stamp,amount,'internal_transfer',reason,**common)

    def fund(self,uid,target,amount,stamp):
        missing=amount-self.balances[target]
        if missing<=0:return
        pool=self.account_meta[target]['source_pool_id']
        donors=[k for k,v in self.account_meta.items() if v['subject_id']==uid and v['source_pool_id']==pool and v['account_type']!='credit_card' and k!=target]
        donors.sort(key=lambda k:(k!=aid(uid,pool.split(':')[-1]),k))
        for other in donors:
            take=min(missing,max(0,self.balances[other]));self.transfer(uid,other,target,take,stamp,'同主体用途账户资金调拨')
            missing-=take
            if not missing:return
        raise ValueError('资金池不足：'+target+' '+str(missing))

    def setup(self,uid,user):
        self.insert('subjects',subject_id=uid,display_name=user['display_name'],workspace_id=uuid.uuid5(NS,'navren:contest:'+uid).hex)
        old={r['account_id']:dict(r) for r in self.source.execute('SELECT * FROM ledger_accounts WHERE user_id=?',(uid,))}
        names={'daily':'工资/养老金与个人结算','reserve':'储备存款电子账户','operating':'经营结算','rent':'个人物业租金结算','rent1':'出租住宅一租金结算','rent2':'出租住宅二租金结算','rent3':'出租住宅三租金结算','machinery':'农机作业回款结算','broker':'证券投资资金账户','wallet':'个人支付钱包','shop_wallet':'餐饮收款钱包','credit':'个人信用卡账务账户'}
        for role,institution,typ,pool in CONFIG[uid]:
            con=uid+'::connection:'+institution
            self.db.execute('INSERT OR IGNORE INTO connections VALUES(?,?,?,?,?,?)',(uid,con,institution,'securities' if typ=='securities_cash' else 'payment' if typ=='payment_wallet' else 'bank','synthetic_connected',SOURCE))
            pool_id=uid+'::cash:'+pool;account=aid(uid,role)
            opening=old[pool_id]['opening_balance_minor'] if role==pool else 0
            row={'subject_id':uid,'account_id':account,'connection_id':con,'name':names[role],'account_type':typ,
                 'currency':'CNY','scope':old[pool_id]['scope'],'source_pool_id':pool_id,'opened_on':START,
                 'opening_balance_minor':opening,'balance_minor':opening,'status':'active',
                 'details':{'source':SOURCE,'routing':'explicit_business_purpose','institution_relationship':'synthetic_not_bank_connection',
                            'opening_date_semantics':'historical_coverage_start_not_real_opening','legacy_role':pool}}
            self.insert('accounts',**row);self.account_meta[account]=row;self.balances[account]=opening
            if typ=='credit_card':
                facility=uid+'::facility:credit'
                self.insert('credit_facilities',subject_id=uid,facility_id=facility,account_id=account,
                            limit_minor=500000000 if uid=='tycoon' else 20000000,outstanding_minor=0)
            else:facility=None
            if typ in ('bank_settlement','credit_card'):
                if role=='reserve':
                    # 新增储备卡使用独立尾号，不改变原有卡片及历史流水的卡号。
                    number=sorted(CONFIG).index(uid)+1
                    old_tail,new_tail=7000+number,8000+number
                else:
                    self.card_sequence+=1
                    old_tail,new_tail=1000+self.card_sequence,2000+self.card_sequence
                old_card=uid+'::card:'+role+':2016';new_card=uid+'::card:'+role+':2021'
                self.insert('cards',subject_id=uid,card_id=old_card,account_id=account,card_type='credit' if facility else 'debit',
                            masked_number='SIM-****-'+str(old_tail),status='replaced',issued_on=START,
                            closed_on='2021-10-03',replaces_card_id=None,facility_id=facility,role='primary')
                self.insert('cards',subject_id=uid,card_id=new_card,account_id=account,card_type='credit' if facility else 'debit',
                            masked_number='SIM-****-'+str(new_tail),status='active',issued_on='2021-10-04',
                            closed_on=None,replaces_card_id=old_card,facility_id=facility,role='primary')
                self.cards[account]=[old_card,new_card]
        # 同账户副卡只连一次授信；挂失状态不影响账户active。
        if uid in ('tycoon','working_landlady'):
            self.insert('cards',subject_id=uid,card_id=uid+'::card:credit:supplementary',account_id=aid(uid,'credit'),
                        card_type='credit',masked_number='SIM-****-9001',status='lost',issued_on='2022-01-01',closed_on=None,
                        replaces_card_id=None,facility_id=uid+'::facility:credit',role='supplementary')
        profile=self.profiles[uid];assets={x['id']:x for x in profile['assets']}
        for item in profile['assets']:
            asset=uid+'::asset:'+item['id']
            self.insert('assets',subject_id=uid,asset_id=asset,name=item['name'],asset_type=item['type'],ownership_scope='personal',source_asset_id=asset,
                        details={'valuation_source':SOURCE,'company_balance_sheet_included':False,'quantity_status':'unknown'})
            contract=uid+'::contract:'+item['id']
            if item['type'].startswith('fund_'):
                self.insert('contracts',subject_id=uid,contract_id=contract,contract_type='investment',account_id=aid(uid,'broker'),asset_id=asset,
                            name=item['name']+'合成持仓合同',start_date=START,end_date=None,status='research_only',principal_minor=item['opening_value_minor'],
                            details={'terms_status':'unknown','execution_enabled':False,'source_asset_id':asset,'real_NAV':False})
                value=dict(self.source.execute('SELECT * FROM asset_values WHERE asset_id=? AND date=?',(asset,END)).fetchone())
                self.insert('positions',subject_id=uid,position_id=uid+'::position:'+item['id'],account_id=aid(uid,'broker'),asset_id=asset,contract_id=contract,
                            cost_basis_minor=value['cost_basis_minor'],book_value_minor=value['book_value_minor'],quantity=None,valuation_date=END,valuation_source=SOURCE)
            elif '出租' in item['name']:
                role='rent' if uid=='tycoon' else 'rent'+item['id'][-1]
                self.insert('contracts',subject_id=uid,contract_id=contract,contract_type='lease',account_id=aid(uid,role),asset_id=asset,
                            name=item['name']+'租赁合同',start_date=START,end_date=None,status='active',principal_minor=None,
                            details={'tenant_id':'SIM-TENANT-'+item['id'],'terms_status':'synthetic','ownership':'personal'})
        for loan in profile['loans']:
            asset_code={'TY-LN-PR':'TY-PR-H','GF-LN-EQ':'GF-EQ-H','WL-LN-R1':'WL-PR-R1','WL-LN-R2':'WL-PR-R2','RE-LN-H':'RE-PR'}[loan['id']]
            contract=uid+'::contract:'+loan['id'];account=aid(uid,'operating' if loan['scope']=='self_employed' else 'daily')
            latest=self.source.execute('SELECT principal_minor FROM loan_balances WHERE user_id=? AND loan_id=? AND date=?',(uid,uid+'::liability:'+loan['id'],END)).fetchone()[0]
            self.insert('contracts',subject_id=uid,contract_id=contract,contract_type='loan',account_id=account,asset_id=uid+'::asset:'+asset_code,
                        name=loan['name'],start_date=START,end_date=None,status='active' if latest else 'closed',principal_minor=latest,
                        details={'annual_rate_bps':loan['annual_rate_bps'],'opening_principal_minor':loan['opening_principal_minor'],
                                 'monthly_principal_minor':loan['monthly_principal_minor'],'terms_status':'synthetic_original_config','actual_origination_date':None})
            self.insert('mandates',subject_id=uid,mandate_id=uid+'::mandate:'+loan['id'],account_id=account,contract_id=contract,
                        merchant=loan['name'],status='active' if latest else 'closed',details={'payment_type':'loan_repayment','confirmation_for_changes_required':True})
            self.db.executemany('INSERT INTO loan_balances VALUES(?,?,?,?)',[(uid,contract,r['date'],r['principal_minor']) for r in self.source.execute('SELECT * FROM loan_balances WHERE user_id=? AND loan_id=?',(uid,uid+'::liability:'+loan['id']))])
        self.db.executemany('INSERT INTO valuations VALUES(?,?,?,?,?)',[(uid,r['asset_id'],r['date'],r['book_value_minor'],r['cost_basis_minor']) for r in self.source.execute('SELECT * FROM asset_values WHERE user_id=?',(uid,))])
        for role in ('daily','reserve'):
            self.insert('contracts',subject_id=uid,contract_id=uid+'::contract:deposit:'+role,contract_type='demand_deposit',account_id=aid(uid,role),asset_id=None,
                        name='结算/储备活期存款合同',start_date=START,end_date=None,status='active',principal_minor=None,
                        details={'balance_is_in_account_not_second_asset':True,'interest_rate':None,'terms_status':'synthetic','rule_id':'deposit-structure'})
        if uid=='retired_entrepreneur':
            self.insert('contracts',subject_id=uid,contract_id=uid+'::contract:shop-lease',contract_type='business_lease',account_id=aid(uid,'operating'),asset_id=None,
                        name='个人自营餐饮门店租赁',start_date='2023-01-01',end_date=None,status='active',principal_minor=None,details={'not_owned_property':True,'business_scope':'self_employed_not_corporate'})
        return old

    def policy_plan(self,uid,rows):
        allocations=defaultdict(list);specs=[]
        for item in self.profiles[uid]['assets']:
            if item['type']=='property':specs.append((item['id'],'property',50000,'个人家庭/出租物业保障'))
            elif item['type']=='equipment':specs.append((item['id'],'equipment',200000,'农机财产保障'))
        specs.append((None,'health',100000,'个人意外健康保障'))
        if uid=='grain_farmer':
            self.insert('assets',subject_id=uid,asset_id=uid+'::crop:field-1',name='合成水稻地块',asset_type='crop_exposure',ownership_scope='self_employed',source_asset_id=None,
                        details={'region':REGION,'area_mu':600,'area_source':'synthetic_configuration','asset_value_minor':None,'not_added_to_assets':True})
            specs.append(('crop:field-1','crop',900000,'同一水稻地块年度保障'))
        if uid=='retired_entrepreneur':specs += [(None,'shop_property',100000,'租赁餐饮场所自有用品保障'),(None,'public_liability',100000,'自营餐饮公众责任保障')]
        insurer=uid+'::connection:insurer'
        self.insert('connections',subject_id=uid,connection_id=insurer,institution_name='Navren合成承保机构',institution_type='insurance',status='synthetic_connected',source=SOURCE)
        for year in range(2016,2027):
            start=max(START,f'{year}-01-01');end=f'{year}-12-31'
            for index,(asset_code,typ,base_premium,label) in enumerate(specs):
                if typ in ('shop_property','public_liability') and year<2023:continue
                ag=typ=='crop';subsidized=ag and year>=2025
                premium=base_premium;paid=premium*30//100 if subsidized else premium
                status='active' if start<=END<=end else 'expired'
                rule_id=f'synthetic-insurance-{uid}-{year}'
                self.db.execute('INSERT OR IGNORE INTO rules VALUES(?,?,?,?,?,?,?,?,?)',(rule_id,'insurance',AG_URL if ag else None,'2026-10-07',start,end,REGION,'synthetic_terms',
                        js({'applies_year':year,'real_historical_local_terms_verified':False,'national_2024_reference_used_only_from_2025':subsidized,'no_actual_insurer_product_claim':True})))
                asset=(uid+'::'+asset_code if ag else uid+'::asset:'+asset_code) if asset_code else None
                policy=f'{uid}::policy:{year}:{index}';contract=policy.replace('::policy:', '::contract:policy:')
                pool='operating' if typ in ('crop','equipment','shop_property','public_liability') else 'daily'
                account=aid(uid,pool)
                self.insert('contracts',subject_id=uid,contract_id=contract,contract_type='insurance',account_id=account,asset_id=asset,name=label,
                            start_date=start,end_date=end,status=status,principal_minor=None,details={'sum_insured_is_not_asset':True,'rule_id':rule_id})
                self.insert('policies',subject_id=uid,policy_id=policy,contract_id=contract,insurer_connection_id=insurer,policyholder_id=uid,
                            insured_id=uid,beneficiary_id=None,covered_asset_id=asset,coverage_key=asset or typ,insurance_type=typ,
                            terms_version=rule_id,start_date=start,end_date=end,status=status,sum_insured_minor=60000000 if ag else base_premium*1000,
                            premium_minor=premium,farmer_paid_minor=paid,subsidy_minor=premium-paid if subsidized else None,deductible_minor=None,cash_value_minor=None,
                            payment_account_id=account,rule_id=rule_id,details={'policy_number':'SIM-'+uid+'-'+str(year)+'-'+str(index),
                                'beneficiary_status':'not_provided','cash_value_status':'no_verified_cash_value','claims_status':'not_provided',
                                'premium_semantics':'synthetic_component_of_original_combined_expense_not_extra_charge',
                                'agricultural_operating_subsidy_is_separate':ag})
                self.insert('premium_funding',subject_id=uid,policy_id=policy,payer_type='policyholder',amount_minor=paid,personal_cash_effect=1,details={'source':'synthetic_reconstruction'})
                if ag:
                    for payer,percent in [('central',35),('provincial',25),('local',10)]:
                        self.insert('premium_funding',subject_id=uid,policy_id=policy,payer_type=payer,amount_minor=premium*percent//100 if subsidized else None,
                                    personal_cash_effect=0,details={'source':'synthetic_local_allocation' if subsidized else 'historical_subsidy_unknown','not_bank_income':True})
                self.insert('mandates',subject_id=uid,mandate_id=policy.replace('::policy:','::mandate:policy:'),account_id=account,contract_id=contract,merchant='合成保险保费',status='active' if status=='active' else 'closed',details={'synthetic_authorization':True,'auto_execution_enabled':False})
                budget=paid
                preferred=('农业经营仓储与管理支出','农机年度维护与保险支出') if pool=='operating' and uid=='grain_farmer' else ('自营水电与杂费支出','自营门店租赁支出') if pool=='operating' else ('家庭日常支出','家庭生活与赡养支出','家庭住房与保障支出','个人出租物业管理支出','出租物业管理支出','个人日常支出')
                for r in rows:
                    if r['occurred_at'][:4]!=str(year) or r['kind']!='expense' or r['category'] not in preferred:continue
                    used=sum(x[1] for x in allocations[r['transaction_id']]);take=min(budget,r['amount_minor']-used)
                    if take>0:allocations[r['transaction_id']].append((policy,take,contract,policy.replace('::policy:','::mandate:policy:')));budget-=take
                    if not budget:break
                if budget:raise ValueError('原组合支出不足以重构保费：'+policy)
        return allocations

    def route(self,uid,r):
        category=r['category'];pool=r['account_id'].split(':')[-1]
        if r['kind'].startswith('investment') or '持股分红' in category:return aid(uid,'broker')
        if uid=='tycoon' and '租金' in category:return aid(uid,'rent')
        if uid=='working_landlady' and '租金' in category:
            role={'一':'rent1','二':'rent2','三':'rent3'}[category[4]]
            return aid(uid,role)
        if uid=='grain_farmer' and '农机作业' in category:return aid(uid,'machinery')
        if uid=='retired_entrepreneur' and category=='自营餐饮回款':return aid(uid,'shop_wallet')
        if r['scope']!='self_employed' and category in ('餐饮','日用品','休闲娱乐'):return aid(uid,'wallet')
        if r['scope']!='self_employed' and category=='个人购物' and r['kind']=='expense':return aid(uid,'credit')
        return aid(uid,pool)

    def replay(self,uid):
        rows=[dict(r) for r in self.source.execute('SELECT * FROM cash_transactions WHERE user_id=? ORDER BY occurred_at,transaction_id',(uid,))]
        allocations=self.policy_plan(uid,rows)
        byday=defaultdict(list)
        for r in rows:byday[r['occurred_at'][:10]].append(r)
        refs={r['event_id']:r['reference_id'] for r in self.source.execute('SELECT event_id,reference_id FROM journal_events WHERE user_id=?',(uid,))}
        event_targets={};event_accounts={}
        event_contract={}
        for r in self.source.execute("SELECT l.event_id,a.code,a.account_class FROM journal_lines l JOIN ledger_accounts a USING(account_id) WHERE l.user_id=? AND a.account_class IN ('investment','liability')",(uid,)):
            event_contract[r['event_id']]=uid+'::contract:'+r['code'].split(':')[-1]
        loan_interest={r['event_id']:r['amount'] for r in self.source.execute("SELECT l.event_id,sum(l.debit_minor-l.credit_minor) amount FROM journal_lines l JOIN ledger_accounts a USING(account_id) JOIN journal_events e USING(event_id) WHERE l.user_id=? AND a.account_class='expense' AND e.event_type='loan_repayment' GROUP BY l.event_id",(uid,))}
        day=date.fromisoformat(START);end=date.fromisoformat(END);last_statement=None
        all_accounts=[k for k,v in self.account_meta.items() if v['subject_id']==uid]
        snapshots=[]
        while day<=end:
            stamp=day.isoformat()+'T07:00:00+08:00'
            if day.day==10 and last_statement:
                account=aid(uid,'credit');amount=min(last_statement['statement_minor'],max(0,-self.balances[account]))
                self.fund(uid,aid(uid,'daily'),amount,stamp)
                self.transfer(uid,aid(uid,'daily'),account,amount,stamp,'信用卡还款（不重复消费）')
                last_statement['paid_minor']=amount;last_statement['status']='paid' if amount==last_statement['statement_minor'] else 'adjusted_by_refund'
                self.insert('card_statements',**last_statement);last_statement=None
            for r in byday[day.isoformat()]:
                ref=refs[r['event_id']];account=event_accounts.get(ref) if r['kind']=='refund' and ref else None
                account=account or self.route(uid,r)
                # 不改变原资金池归属；缺现金时只用同原资金池的真实配对调拨。
                if self.account_meta[account]['source_pool_id']!=r['account_id']:account=aid(uid,r['account_id'].split(':')[-1])
                if r['signed_minor']<0 and self.account_meta[account]['account_type']!='credit_card':self.fund(uid,account,r['amount_minor'],r['occurred_at'])
                contract=event_contract.get(r['event_id'])
                if r['kind']=='income' and '租金' in r['category']:
                    code=('TY-PR-H' if '住宅' in r['category'] else 'TY-PR-S') if uid=='tycoon' else 'WL-PR-R'+{'一':'1','二':'2','三':'3'}[r['category'][4]]
                    contract=uid+'::contract:'+code
                if r['category']=='自营门店租赁支出':contract=uid+'::contract:shop-lease'
                mandate=uid+'::mandate:'+contract.split(':')[-1] if r['kind']=='loan_repayment' and contract else None
                pieces=allocations.get(r['transaction_id'],[]);remaining=r['amount_minor']-sum(x[1] for x in pieces)
                common={'scope':r['scope'],'counterparty':r['merchant'],'source_event_id':r['event_id'],'source_transaction_id':r['transaction_id'],
                        'transfer_group_id':r['transfer_group_id'],'economic_event_id':r['event_id'],
                        'original_transaction_id':event_targets.get(ref),'contract_id':contract,'mandate_id':mandate}
                if remaining:
                    ident=self.tx(uid,account,r['occurred_at'],remaining if r['signed_minor']>0 else -remaining,r['kind'],r['category'],
                        expense_minor=remaining if r['kind']=='expense' else -remaining if r['kind']=='refund' else loan_interest.get(r['event_id'],0) if r['kind']=='loan_repayment' else 0,
                        details={'source':SOURCE,'source_category':r['category'],'reconstructed_channel':True},**common)
                    event_targets[r['event_id']]=ident;event_accounts[r['event_id']]=account
                    if uid=='retired_entrepreneur' and account==aid(uid,'shop_wallet') and r['kind']=='income' and r['signed_minor']>0:
                        # 演示商户收款先进入支付钱包，再以配对调拨结算到本人经营银行卡。
                        self.transfer(uid,account,aid(uid,'operating'),remaining,r['occurred_at'],
                                      '经营收款结算至本人经营账户（合成演示）',scope='self_employed')
                for policy,amount,pcontract,mandate in pieces:
                    self.tx(uid,account,r['occurred_at'],-amount,'insurance_premium','合成保险保费',expense_minor=amount,
                        **{**common,'contract_id':pcontract,'mandate_id':mandate},details={'policy_id':policy,'allocation':'explicit_synthetic_component',
                        'original_combined_expense':r['category'],'not_extra_expense':True})
                    self.synthetic_expense_components+=1
                if self.account_meta[account]['account_type']=='credit_card' and self.balances[account]>0:
                    self.transfer(uid,account,aid(uid,'daily'),self.balances[account],r['occurred_at'],'信用卡退款溢缴款转回本人结算账户')
            if day.day==calendar.monthrange(day.year,day.month)[1]:
                next_month=(day.replace(day=1)+timedelta(days=32)).replace(day=10)
                last_statement={'subject_id':uid,'statement_id':uid+'::statement:'+day.strftime('%Y-%m'),'account_id':aid(uid,'credit'),
                    'period_start':max(START,day.replace(day=1).isoformat()),'period_end':day.isoformat(),'due_on':next_month.isoformat(),
                    'statement_minor':max(0,-self.balances[aid(uid,'credit')]),'paid_minor':0,'status':'open'}
            for account in all_accounts:
                value=self.balances[account]
                if self.account_meta[account]['account_type']!='credit_card' and value<0:raise ValueError('现金负余额 '+account)
                snapshots.append((uid,account,day.isoformat(),value))
            day+=timedelta(days=1)
        if last_statement:
            self.insert('card_statements',**last_statement)
            self.insert('obligations',subject_id=uid,obligation_id=uid+'::obligation:credit',account_id=aid(uid,'daily'),contract_id=None,mandate_id=None,
                        category='信用卡账单还款',due_on=last_statement['due_on'],amount_minor=last_statement['statement_minor'],status='forecast_not_paid',details={'not_second_consumption':True})
        self.db.executemany('INSERT INTO account_balances VALUES(?,?,?,?)',snapshots)
        for account in all_accounts:self.db.execute('UPDATE accounts SET balance_minor=? WHERE subject_id=? AND account_id=?',(self.balances[account],uid,account))
        self.db.execute("UPDATE contracts SET principal_minor=(SELECT balance_minor FROM accounts a WHERE a.subject_id=contracts.subject_id AND a.account_id=contracts.account_id) WHERE subject_id=? AND contract_type='demand_deposit'",(uid,))
        self.db.execute('UPDATE credit_facilities SET outstanding_minor=? WHERE subject_id=?',(max(0,-self.balances[aid(uid,'credit')]),uid))
        for ob in self.source.execute('SELECT * FROM recurring_obligations WHERE user_id=?',(uid,)):
            account=aid(uid,'operating' if ob['scope']=='self_employed' else 'daily');mandate=uid+'::mandate:'+ob['obligation_id']
            self.insert('mandates',subject_id=uid,mandate_id=mandate,account_id=account,contract_id=None,merchant=ob['category'],status='active',details={'source_obligation_id':ob['obligation_id'],'authorization':'synthetic','execution_enabled':False})
            self.insert('obligations',subject_id=uid,obligation_id=ob['obligation_id'],account_id=account,contract_id=None,mandate_id=mandate,
                        category=ob['category'],due_on=ob['next_due_on'],amount_minor=ob['amount_minor'],status='forecast_not_paid',details={'frequency':ob['frequency'],'source':SOURCE})

    def finish_transactions(self):
        if not self.txs:return
        keys=list(self.txs[0])
        self.db.executemany('INSERT INTO transactions('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',[[r[k] for k in keys] for r in self.txs])

def build(source_path,profiles_path,output_path):
    source_path,profiles_path,output_path=map(lambda p:Path(p).expanduser().resolve(),(source_path,profiles_path,output_path))
    if output_path==source_path:raise ValueError('新库不能覆盖旧源库')
    if output_path.exists():raise FileExistsError('目标版本已存在；请选择新路径，不覆盖原件')
    output_path.parent.mkdir(parents=True,exist_ok=True)
    source_before=file_sha(source_path);profiles_before=file_sha(profiles_path)
    wal=Path(str(source_path)+'-wal')
    if wal.exists() and wal.stat().st_size:raise ValueError('旧源存在未封存WAL，不能生成快照')
    backup=output_path.parent/'backups'/('source-'+source_before[:16]+'.sqlite3')
    backup.parent.mkdir(exist_ok=True)
    if backup.exists():
        if file_sha(backup)!=source_before:raise ValueError('旧源备份摘要不匹配')
    else:
        shutil.copyfile(source_path,backup);backup.chmod(0o600)
    profiles={v['id']:v for v in json.loads(profiles_path.read_text())['profiles']}
    code_hashes={p.name:file_sha(p) for p in Path(__file__).parent.glob('*.py')}
    version='navren-financial-'+hashlib.sha256(js({'source':source_before,'profiles':profiles_before,'code':code_hashes}).encode()).hexdigest()[:16]
    temporary=output_path.with_name(output_path.name+'.building-'+uuid.uuid4().hex)
    source=readonly(source_path);db=sqlite3.connect(temporary);db.row_factory=sqlite3.Row
    try:
        db.executescript(DDL);db.execute('BEGIN');db.execute('PRAGMA defer_foreign_keys=ON')
        metadata={'schema_version':SCHEMA_VERSION,'dataset_version':version,'source':SOURCE,'source_database_sha256':source_before,
                  'source_profiles_sha256':profiles_before,'start_date':START,'end_date':END,'source_preserved':'true','runtime_activation':'explicit_env_required'}
        db.executemany('INSERT INTO metadata VALUES(?,?)',metadata.items())
        db.executemany('INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?)',[
          ('card-structure','account_card',CC_URL,'2026-10-07',START,END,'合成个人账户','synthetic_business_structure',js({'shared_facility_once':True,'source_page_is_reference_not_historical_terms':True})),
          ('deposit-structure','deposit',DEP_URL,'2026-10-07',START,END,'合成个人账户','synthetic_business_structure',js({'deposit_balance_not_duplicate_asset':True,'historical_rate':None})),
          ('agricultural-2024-national-reference','agricultural_policy',AG_URL,'2026-10-07','2024-05-21',None,'中国全国种粮地区','official_structure_reference',js({'eastern_central_subsidy_percent':35,'provincial_minimum_percent':25,'specific_local_product_verified':False}))])
        b=Builder(db,source,profiles)
        for user in source.execute('SELECT * FROM users ORDER BY user_id'):
            uid=user['user_id'];b.setup(uid,user);b.replay(uid)
        b.finish_transactions()
        for row in db.execute('SELECT subject_id,source_pool_id,account_id FROM accounts').fetchall():
            b.insert('source_links',subject_id=row['subject_id'],source_id=row['source_pool_id'],target_type='account',target_id=row['account_id'],relationship='reconstructed_funding_pool_not_duplicate_balance')
        if db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('新库外键归属检查失败')
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('新库完整性失败')
        db.close();source.close()
        if file_sha(source_path)!=source_before or file_sha(profiles_path)!=profiles_before:raise ValueError('生成期间旧源变化，未发布')
        temporary.chmod(0o600)
        with temporary.open('rb') as f:os.fsync(f.fileno())
        os.link(temporary,output_path);temporary.unlink()
        return {'database_path':str(output_path),'database_sha256':file_sha(output_path),'dataset_version':version,
                'source_database_path':str(source_path),'source_database_sha256':source_before,'profiles_sha256':profiles_before,
                'generator_sources':code_hashes,'synthetic_premium_components':b.synthetic_expense_components,
                'source_backup_path':str(backup),'source_backup_sha256':file_sha(backup),
                'source_unchanged':True,'generated':True,'runtime_enabled':False,'server_deployed':False}
    except Exception:
        db.close();source.close()
        if temporary.exists():temporary.unlink()
        raise
