"""只读盘点与SQLite一致备份；不创建或迁移现役库。"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import urllib.request


def backup_database(path, destination):
    path, destination = Path(path).resolve(), Path(destination).resolve()
    if path == destination or destination.exists():
        raise ValueError('备份目标必须是独立的新文件')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
    destination.chmod(0o600)
    with closing(sqlite3.connect(destination.as_uri()+'?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('备份数据库完整性检查失败')
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        counts = {t: db.execute('SELECT count(*) FROM "'+t.replace('"','""')+'"').fetchone()[0] for t in tables}
        workspaces = []
        if 'workspaces' in tables:
            workspaces = [dict(zip(('workspace_id','as_of','version'), r)) for r in db.execute('SELECT id,as_of,version FROM workspaces')]
    return {'source': str(path), 'backup': str(destination), 'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
            'counts': counts, 'workspaces': workspaces, 'integrity': 'ok',
            'restore': '停止服务后恢复整套备份；个人库需原钥匙串加密密钥。公共库独立，禁止以新建空库替换。'}


def inventory(root, output):
    root, output = Path(root), Path(output)
    raw = subprocess.run(['launchctl','print',f'gui/{__import__("os").getuid()}/com.manton.navren.preview'],capture_output=True,text=True).stdout
    # 仅允许部署路径和端口字段，绝不记录整个环境或模型密钥。
    config = [line.strip() for line in raw.splitlines() if '/server.py' in line or
              any(line.strip().startswith(k) for k in ('NAVREN_PORT=','NAVREN_SIM_DB=','QINGZHOU_SIM_DB=','NAVREN_MARKET_DB=','NAVREN_KNOWLEDGE_DB=','pid =','state ='))]
    values = {line.split('=',1)[0]:line.split('=',1)[1] for line in config if line.startswith(('NAVREN_','QINGZHOU_')) and '=' in line}
    bank = Path(values.get('NAVREN_SIM_DB') or values.get('QINGZHOU_SIM_DB') or Path.home()/'Library/Application Support/Qingzhou/simulated-bank.sqlite3')
    paths = {'bank': bank, 'personal': bank.with_name(bank.stem+'.personal.sqlite3'),
             'knowledge': Path(values.get('NAVREN_KNOWLEDGE_DB') or bank.with_name(bank.stem+'.knowledge.sqlite3')),
             'market': Path(values.get('NAVREN_MARKET_DB') or Path.home()/'Library/Application Support/Navren/market.sqlite3')}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    result = {'observed_at': stamp, 'service': config, 'databases': {}, 'source_root': str(root),
              'backup_consistency': '每个SQLite使用backup API；多个库逐一快照，最终恢复前需停服务核对跨库版本。'}
    for kind, path in paths.items():
        result['databases'][kind] = backup_database(path, output/'backups'/stamp/(kind+'.sqlite3')) if path.exists() else {'source':str(path),'status':'missing'}
    try:
        with urllib.request.urlopen('http://127.0.0.1:'+values.get('NAVREN_PORT','18767')+'/api/app-version',timeout=5) as response:
            result['health'] = json.load(response)
    except Exception as error:
        result['health'] = {'status':'unavailable','reason':str(error)}
    output.mkdir(parents=True, exist_ok=True)
    (output/'service-inventory.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (output/'service-inventory.json').chmod(0o600)
    return result
