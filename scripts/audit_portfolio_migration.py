"""实际服务库与本轮备份的只读核对；输出脱敏结果，不导出客户记录。"""
import json,sqlite3,hashlib,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from finance_gateway import FinanceGateway
LIVE=Path.home()/'Library/Application Support/Qingzhou/simulated-bank.sqlite3'
BASE=LIVE.parent/'backups/four-equity-20261005T015711.sqlite3'
RESTORE=LIVE.parent/'backups/four-equity-restore-verification.sqlite3'
OUT=Path('output/equity');OUT.mkdir(parents=True,exist_ok=True)
def fp(db,table,w):
    rows=db.execute('SELECT * FROM '+table+' WHERE workspace_id=?',(w,)).fetchall()
    # 原账本严格逐字段对比，仅写摘要。
    return hashlib.sha256(json.dumps(sorted([dict(r)for r in rows],key=lambda r:json.dumps(r,sort_keys=True)),sort_keys=True).encode()).hexdigest(),len(rows)
with sqlite3.connect(BASE)as b,sqlite3.connect(LIVE)as current:
    b.row_factory=current.row_factory=sqlite3.Row
    before=[r[0]for r in b.execute('SELECT id FROM workspaces ORDER BY id')];after={r[0]for r in current.execute('SELECT id FROM workspaces')};reports=[]
    for index,w in enumerate(before,1):
        fields={t:{'unchanged':fp(b,t,w)[0]==fp(current,t,w)[0],'before_rows':fp(b,t,w)[1],'after_rows':fp(current,t,w)[1]}for t in ['accounts','holdings','orders','operations']}
        reports.append({'workspace_index':index,'present':w in after,'tables':fields})
    integrity=current.execute('PRAGMA integrity_check').fetchone()[0]
g=FinanceGateway(LIVE);migration=[{'workspace_index':i,**g.wealth.migrate(w)}for i,w in enumerate(before,1)]
for w in after:g.wealth.provision(w)
with sqlite3.connect(BASE)as b,sqlite3.connect(RESTORE)as copy:
    b.backup(copy);restored=copy.execute('PRAGMA integrity_check').fetchone()[0]
RESTORE.chmod(0o600)
result={'actual_database':str(LIVE),'backup':str(BASE),'restoration_copy':str(RESTORE),'integrity':integrity,'restored_integrity':restored,'before_workspaces':len(before),'current_workspaces':len(after),'extra_isolated_probe_workspaces':len(after-set(before)),'original_records':reports,'all_original_ledger_unchanged':all(v['unchanged']for r in reports for v in r['tables'].values()),'migration':migration,'all_cash_unchanged':all(r['tables']['accounts']['unchanged']for r in reports),'current_transition_complete':False,'limitation':'没有与R1活钱用途匹配且完整核验的微众货币基金数据，原组合保留；历史回放是独立示例。'}
(OUT/'migration_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps({k:result[k]for k in ['integrity','restored_integrity','before_workspaces','current_workspaces','all_original_ledger_unchanged','all_cash_unchanged']},ensure_ascii=False))
assert result['all_original_ledger_unchanged'] and integrity==restored=='ok'
