#!/usr/bin/env python3
import json,os,pathlib,sys
conf=json.loads(pathlib.Path('/etc/navren-public/runtime.json').read_text())
directory=pathlib.Path(os.environ['CREDENTIALS_DIRECTORY'])
for name in ('DEEPSEEK_API_KEY','GLM_API_KEY','NAVREN_SMS_ACCESS_KEY_ID','NAVREN_SMS_ACCESS_KEY_SECRET','NAVREN_PERSONAL_DATA_KEY'):
    os.environ[name]=(directory/name).read_text().strip()
os.environ.update(conf['environment'])
os.environ.pop('AIPING_API_KEY',None)
os.environ.pop('DASHSCOPE_API_KEY',None)
sms=pathlib.Path('/var/lib/navren-public/state/sms-config.json')
temporary=sms.with_suffix('.tmp')
fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'wb') as f:f.write((directory/'navren-sms-config').read_bytes())
temporary.chmod(0o600);temporary.replace(sms)
os.environ['NAVREN_SMS_CONFIG']=str(sms)
release=pathlib.Path(conf['release_path'])
os.chdir(release/'app')
python='/opt/navren/public-runtime-20261020/bin/python'
if len(sys.argv)>1 and sys.argv[1]=='--initialize':
    os.execv(python,[python,'-c',conf['initialize_code']])
os.execv(python,[python,'server.py',*conf['arguments'],*sys.argv[1:]])
