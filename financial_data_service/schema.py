"""主体归属采用组合外键；卡、授信、保障额度不成为资产账户。"""
SCHEMA_VERSION = 'navren.financial.v1'
START, END = '2016-10-04', '2026-10-03'
DDL = '''
PRAGMA foreign_keys=ON;
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE subjects(subject_id TEXT PRIMARY KEY,display_name TEXT NOT NULL,workspace_id TEXT UNIQUE NOT NULL);
CREATE TABLE rules(rule_id TEXT PRIMARY KEY,kind TEXT NOT NULL,source_url TEXT,verified_on TEXT,
 effective_from TEXT NOT NULL,effective_to TEXT,region TEXT NOT NULL,provenance TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE connections(subject_id TEXT NOT NULL REFERENCES subjects,connection_id TEXT NOT NULL,
 institution_name TEXT NOT NULL,institution_type TEXT NOT NULL,status TEXT NOT NULL,source TEXT NOT NULL,
 PRIMARY KEY(subject_id,connection_id));
CREATE TABLE accounts(subject_id TEXT NOT NULL,account_id TEXT NOT NULL,connection_id TEXT NOT NULL,
 name TEXT NOT NULL,account_type TEXT NOT NULL,currency TEXT NOT NULL,scope TEXT NOT NULL,
 source_pool_id TEXT NOT NULL,opened_on TEXT NOT NULL,opening_balance_minor INTEGER NOT NULL,
 balance_minor INTEGER NOT NULL,status TEXT NOT NULL,details TEXT NOT NULL,
 PRIMARY KEY(subject_id,account_id),FOREIGN KEY(subject_id,connection_id) REFERENCES connections);
CREATE TABLE credit_facilities(subject_id TEXT NOT NULL,facility_id TEXT NOT NULL,account_id TEXT NOT NULL,
 limit_minor INTEGER NOT NULL,outstanding_minor INTEGER NOT NULL,PRIMARY KEY(subject_id,facility_id),
 FOREIGN KEY(subject_id,account_id) REFERENCES accounts);
CREATE TABLE card_statements(subject_id TEXT NOT NULL,statement_id TEXT NOT NULL,account_id TEXT NOT NULL,
 period_start TEXT NOT NULL,period_end TEXT NOT NULL,due_on TEXT NOT NULL,statement_minor INTEGER NOT NULL,
 paid_minor INTEGER NOT NULL,status TEXT NOT NULL,PRIMARY KEY(subject_id,statement_id),
 FOREIGN KEY(subject_id,account_id) REFERENCES accounts);
CREATE TABLE cards(subject_id TEXT NOT NULL,card_id TEXT NOT NULL,account_id TEXT NOT NULL,
 card_type TEXT NOT NULL,masked_number TEXT NOT NULL,status TEXT NOT NULL,issued_on TEXT NOT NULL,
 closed_on TEXT,replaces_card_id TEXT,facility_id TEXT,role TEXT NOT NULL,
 PRIMARY KEY(subject_id,card_id),FOREIGN KEY(subject_id,account_id) REFERENCES accounts,
 FOREIGN KEY(subject_id,replaces_card_id) REFERENCES cards,
 FOREIGN KEY(subject_id,facility_id) REFERENCES credit_facilities);
CREATE TABLE assets(subject_id TEXT NOT NULL,asset_id TEXT NOT NULL,name TEXT NOT NULL,asset_type TEXT NOT NULL,
 ownership_scope TEXT NOT NULL,source_asset_id TEXT,details TEXT NOT NULL,PRIMARY KEY(subject_id,asset_id));
CREATE TABLE contracts(subject_id TEXT NOT NULL,contract_id TEXT NOT NULL,contract_type TEXT NOT NULL,
 account_id TEXT NOT NULL,asset_id TEXT,name TEXT NOT NULL,start_date TEXT NOT NULL,end_date TEXT,
 status TEXT NOT NULL,principal_minor INTEGER,details TEXT NOT NULL,
 PRIMARY KEY(subject_id,contract_id),FOREIGN KEY(subject_id,account_id) REFERENCES accounts,
 FOREIGN KEY(subject_id,asset_id) REFERENCES assets);
CREATE TABLE positions(subject_id TEXT NOT NULL,position_id TEXT NOT NULL,account_id TEXT NOT NULL,
 asset_id TEXT NOT NULL,contract_id TEXT NOT NULL,cost_basis_minor INTEGER NOT NULL,
 book_value_minor INTEGER NOT NULL,quantity TEXT,valuation_date TEXT NOT NULL,valuation_source TEXT NOT NULL,
 PRIMARY KEY(subject_id,position_id),FOREIGN KEY(subject_id,account_id) REFERENCES accounts,
 FOREIGN KEY(subject_id,asset_id) REFERENCES assets,FOREIGN KEY(subject_id,contract_id) REFERENCES contracts);
CREATE TABLE mandates(subject_id TEXT NOT NULL,mandate_id TEXT NOT NULL,account_id TEXT NOT NULL,
 contract_id TEXT,merchant TEXT NOT NULL,status TEXT NOT NULL,details TEXT NOT NULL,
 PRIMARY KEY(subject_id,mandate_id),FOREIGN KEY(subject_id,account_id) REFERENCES accounts,
 FOREIGN KEY(subject_id,contract_id) REFERENCES contracts);
CREATE TABLE policies(subject_id TEXT NOT NULL,policy_id TEXT NOT NULL,contract_id TEXT NOT NULL,
 insurer_connection_id TEXT NOT NULL,policyholder_id TEXT NOT NULL,insured_id TEXT NOT NULL,beneficiary_id TEXT,
 covered_asset_id TEXT,coverage_key TEXT NOT NULL,insurance_type TEXT NOT NULL,terms_version TEXT NOT NULL,
 start_date TEXT NOT NULL,end_date TEXT NOT NULL,status TEXT NOT NULL,sum_insured_minor INTEGER,
 premium_minor INTEGER NOT NULL,farmer_paid_minor INTEGER NOT NULL,subsidy_minor INTEGER,
 deductible_minor INTEGER,cash_value_minor INTEGER,payment_account_id TEXT NOT NULL,rule_id TEXT NOT NULL,
 details TEXT NOT NULL,PRIMARY KEY(subject_id,policy_id),
 UNIQUE(subject_id,coverage_key,start_date,end_date),
 FOREIGN KEY(subject_id,contract_id) REFERENCES contracts,
 FOREIGN KEY(subject_id,insurer_connection_id) REFERENCES connections,
 FOREIGN KEY(subject_id,covered_asset_id) REFERENCES assets,
 FOREIGN KEY(subject_id,payment_account_id) REFERENCES accounts,FOREIGN KEY(rule_id) REFERENCES rules);
CREATE TABLE premium_funding(subject_id TEXT NOT NULL,policy_id TEXT NOT NULL,payer_type TEXT NOT NULL,
 amount_minor INTEGER,personal_cash_effect INTEGER NOT NULL,details TEXT NOT NULL,
 PRIMARY KEY(subject_id,policy_id,payer_type),FOREIGN KEY(subject_id,policy_id) REFERENCES policies);
CREATE TABLE claims(subject_id TEXT NOT NULL,claim_id TEXT NOT NULL,policy_id TEXT NOT NULL,
 status TEXT NOT NULL,occurred_on TEXT,paid_minor INTEGER,transaction_id TEXT,details TEXT NOT NULL,
 PRIMARY KEY(subject_id,claim_id),FOREIGN KEY(subject_id,policy_id) REFERENCES policies);
CREATE TABLE transactions(subject_id TEXT NOT NULL,transaction_id TEXT NOT NULL,account_id TEXT NOT NULL,
 occurred_at TEXT NOT NULL,booked_on TEXT NOT NULL,status TEXT NOT NULL,amount_minor INTEGER NOT NULL CHECK(amount_minor>0),
 signed_minor INTEGER NOT NULL,currency TEXT NOT NULL,counterparty TEXT NOT NULL,category TEXT NOT NULL,
 kind TEXT NOT NULL,scope TEXT NOT NULL,card_id TEXT,contract_id TEXT,mandate_id TEXT,
 original_transaction_id TEXT,transfer_group_id TEXT,source_event_id TEXT,source_transaction_id TEXT,
 economic_event_id TEXT NOT NULL,expense_minor INTEGER NOT NULL,details TEXT NOT NULL,
 PRIMARY KEY(subject_id,transaction_id),FOREIGN KEY(subject_id,account_id) REFERENCES accounts,
 FOREIGN KEY(subject_id,card_id) REFERENCES cards,FOREIGN KEY(subject_id,contract_id) REFERENCES contracts,
 FOREIGN KEY(subject_id,mandate_id) REFERENCES mandates,
 FOREIGN KEY(subject_id,original_transaction_id) REFERENCES transactions DEFERRABLE INITIALLY DEFERRED);
CREATE INDEX tx_subject_date ON transactions(subject_id,booked_on,occurred_at,transaction_id);
CREATE INDEX tx_account_date ON transactions(subject_id,account_id,booked_on);
CREATE TABLE account_balances(subject_id TEXT NOT NULL,account_id TEXT NOT NULL,date TEXT NOT NULL,
 balance_minor INTEGER NOT NULL,PRIMARY KEY(subject_id,account_id,date),FOREIGN KEY(subject_id,account_id) REFERENCES accounts);
CREATE TABLE valuations(subject_id TEXT NOT NULL,asset_id TEXT NOT NULL,date TEXT NOT NULL,
 book_value_minor INTEGER NOT NULL,cost_basis_minor INTEGER NOT NULL,
 PRIMARY KEY(subject_id,asset_id,date),FOREIGN KEY(subject_id,asset_id) REFERENCES assets);
CREATE TABLE loan_balances(subject_id TEXT NOT NULL,contract_id TEXT NOT NULL,date TEXT NOT NULL,principal_minor INTEGER NOT NULL,
 PRIMARY KEY(subject_id,contract_id,date),FOREIGN KEY(subject_id,contract_id) REFERENCES contracts);
CREATE TABLE obligations(subject_id TEXT NOT NULL,obligation_id TEXT NOT NULL,account_id TEXT NOT NULL,
 contract_id TEXT,mandate_id TEXT,category TEXT NOT NULL,due_on TEXT NOT NULL,amount_minor INTEGER,status TEXT NOT NULL,
 details TEXT NOT NULL,PRIMARY KEY(subject_id,obligation_id),FOREIGN KEY(subject_id,account_id) REFERENCES accounts,
 FOREIGN KEY(subject_id,contract_id) REFERENCES contracts,FOREIGN KEY(subject_id,mandate_id) REFERENCES mandates);
CREATE TABLE source_links(subject_id TEXT NOT NULL,source_id TEXT NOT NULL,target_type TEXT NOT NULL,
 target_id TEXT NOT NULL,relationship TEXT NOT NULL,PRIMARY KEY(subject_id,source_id,target_type,target_id));
'''
