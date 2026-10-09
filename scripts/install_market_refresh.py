#!/usr/bin/env python3
"""安装本机公开资讯刷新任务，不含密钥；不依赖 Codex 会话在线。"""
import os,plistlib,subprocess,tempfile
from pathlib import Path

# 保留既有任务身份，使重新安装替换同一任务，不重复注册刷新任务。
LABEL='com.manton.navren.market'


def build_job(root,home):
 root,home=Path(root),Path(home)
 logs=home/'Library/Logs/Navren'
 database=home/'Library/Application Support/Navren/market.sqlite3'
 # 后端从环境或系统钥匙串加载密钥；plist不保存密钥或依赖外部加载脚本。
 return {'Label':LABEL,'ProgramArguments':[str(root/'.venv/bin/python'),str(root/'scripts/refresh_market.py'),'--scheduled','--backfill-weeks','1','--database',str(database),'--report',str(logs/'market-refresh-latest.json')],
         'WorkingDirectory':str(root),'StartCalendarInterval':[{'Hour':h,'Minute':30} for h in (8,12,18)],'StartInterval':300,'RunAtLoad':True,
         'StandardOutPath':str(logs/'market-refresh.log'),'StandardErrorPath':str(logs/'market-refresh-error.log'),
         'EnvironmentVariables':{'TZ':'Asia/Shanghai','NAVREN_MARKET_DB':str(database)},
         'ProcessType':'Background','LowPriorityIO':True,'Nice':10}


def main():
 root=Path(__file__).resolve().parents[1]
 home=Path.home()
 job=build_job(root,home)
 path=home/'Library/LaunchAgents'/f'{LABEL}.plist'
 logs=home/'Library/Logs/Navren'
 if not (root/'.venv/bin/python').is_file():raise RuntimeError('Python运行环境不存在，未修改任务。')
 if not (root/'scripts/refresh_market.py').is_file():raise RuntimeError('刷新入口不存在，未修改任务。')
 path.parent.mkdir(parents=True,exist_ok=True)
 logs.mkdir(parents=True,exist_ok=True)
 if path.exists():
  old=plistlib.loads(path.read_bytes())
  if old.get('Label')!=LABEL:raise RuntimeError('现有任务身份不符，未修改。')
  subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}',str(path)],capture_output=True)
 fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.'+LABEL+'-')
 with os.fdopen(fd,'wb') as file:file.write(plistlib.dumps(job));file.flush();os.fsync(file.fileno())
 os.chmod(tmp,0o600);os.replace(tmp,path)
 subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],check=True)
 print(str(path))


if __name__=='__main__':main()
