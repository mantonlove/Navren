#!/usr/bin/env python3
"""只增加读取绑定；不改余额、凭据、记忆、历史操作。默认仅生成计划。"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from financial_data_service import FinancialDataService
from financial_data_service.build import readonly, file_sha

def migrate(financial_db, bank_db=None, output_bank=None):
    service=FinancialDataService(financial_db)
    with closing(readonly(financial_db)) as db:
        subjects=[dict(r) for r in db.execute('SELECT * FROM subjects')]
        mappings={r['subject_id']:[dict(v) for v in db.execute('SELECT account_id,source_pool_id,account_type FROM accounts WHERE subject_id=?',(r['subject_id'],))] for r in subjects}
    plan={'dataset_version':service.dataset_version,'financial_database':str(Path(financial_db).resolve()),
          'generated':True,'binding_plan':subjects,'account_mappings':mappings,'applied':False,
          'runtime_accounts_replaced':False,'memory_and_operations_preserved':True,'activation':'NAVREN_FINANCIAL_DB显式装配；读入口与原操作账本分开，禁止资产双加'}
    plan.update(keep_existing_bank_database_path=True,live_applied=False,
                personal_sidecars_migrated=False,
                activation_notice='启用只添加NAVREN_FINANCIAL_DB，保持现有银行库路径及其.personal/knowledge侧库配置。隔离副本仅验证迁移，不用于直接替换现役。')
    if bank_db:
        bank_path=Path(bank_db).expanduser().resolve()
        # 用可见SQLite只读快照，支持现有WAL，绝不写源库。
        with closing(sqlite3.connect(bank_path.as_uri()+'?mode=ro',uri=True)) as original:
            original.row_factory=sqlite3.Row
            rows={r['user_id']:dict(r) for r in original.execute("SELECT user_id,workspace_id,status FROM navren_demo_accounts")}
            for r in subjects:
                if r['subject_id'] not in rows or rows[r['subject_id']]['workspace_id']!=r['workspace_id'] or rows[r['subject_id']]['status']!='ready':
                    raise ValueError('现有会话映射不一致，拒绝迁移 '+r['subject_id'])
            if output_bank:
                target=Path(output_bank).expanduser().resolve()
                if target==bank_path or target.exists():raise ValueError('迁移只允许新隔离副本，不覆盖源或既有文件')
                target.parent.mkdir(parents=True,exist_ok=True)
                try:
                    with closing(sqlite3.connect(target)) as copied:
                        original.backup(copied)
                        copied.execute('''CREATE TABLE IF NOT EXISTS navren_financial_data_bindings(
                            workspace_id TEXT PRIMARY KEY,subject_id TEXT UNIQUE NOT NULL,
                            dataset_version TEXT NOT NULL,financial_database TEXT NOT NULL,account_mapping TEXT NOT NULL)''')
                        copied.executemany('INSERT OR REPLACE INTO navren_financial_data_bindings VALUES(?,?,?,?,?)',[
                            (r['workspace_id'],r['subject_id'],service.dataset_version,str(Path(financial_db).resolve()),json.dumps(mappings[r['subject_id']],ensure_ascii=False)) for r in subjects])
                        if copied.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('迁移副本完整性失败')
                        copied.commit()
                    target.chmod(0o600)
                except Exception:
                    if target.exists():target.unlink()
                    raise
                plan.update(applied=True,applied_scope='isolated_copy_only',isolated_bank_database=str(target),isolated_bank_sha256=file_sha(target))
    elif output_bank:raise ValueError('需要明确的--bank-db来源')
    return plan

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--financial-db',required=True)
    p.add_argument('--bank-db')
    p.add_argument('--output-bank',help='仅创建隔离副本；省略则只读生成计划')
    p.add_argument('--receipt',required=True)
    a=p.parse_args();result=migrate(a.financial_db,a.bank_db,a.output_bank)
    Path(a.receipt).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'applied':result['applied'],'dataset_version':result['dataset_version']}))
