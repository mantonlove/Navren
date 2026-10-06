#!/usr/bin/env python3
"""安装本机公开资讯刷新任务，不含密钥；不依赖 Codex 会话在线。"""
import os,plistlib,subprocess,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
label='com.manton.navren.market'
path=Path.home()/'Library/LaunchAgents'/f'{label}.plist'
logs=Path.home()/'Library/Logs/Navren';logs.mkdir(parents=True,exist_ok=True)
database=Path.home()/'Library/Application Support/Navren/market.sqlite3'
job={'Label':label,'ProgramArguments':['/Users/manton/CC/services/cc-secrets/cc-with-secrets',str(root/'.venv/bin/python'),str(root/'scripts/refresh_market.py'),'--scheduled','--backfill-weeks','1','--database',str(database),'--report',str(logs/'market-refresh-latest.json')],
     'WorkingDirectory':str(root),'StartCalendarInterval':[{'Hour':h,'Minute':30} for h in (8,12,18)],'StartInterval':300,'RunAtLoad':True,
     'StandardOutPath':str(logs/'market-refresh.log'),'StandardErrorPath':str(logs/'market-refresh-error.log'),
     'EnvironmentVariables':{'TZ':'Asia/Shanghai','NAVREN_MARKET_DB':str(database)},
     'ProcessType':'Background','LowPriorityIO':True,'Nice':10}
if not (root/'.venv/bin/python').is_file():raise RuntimeError('Python运行环境不存在，未修改任务。')
if not (root/'scripts/refresh_market.py').is_file():raise RuntimeError('刷新入口不存在，未修改任务。')
path.parent.mkdir(parents=True,exist_ok=True)
if path.exists():
 old=plistlib.loads(path.read_bytes())
 if old.get('Label')!=label:raise RuntimeError('现有任务身份不符，未修改。')
 subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}',str(path)],capture_output=True)
fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.'+label+'-')
with os.fdopen(fd,'wb') as file:file.write(plistlib.dumps(job));file.flush();os.fsync(file.fileno())
os.chmod(tmp,0o600);os.replace(tmp,path)
subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],check=True)
print(str(path))
