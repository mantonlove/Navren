"""Owned disposable formal-page service for the fixed T01 browser consumer.

The credentials are the repository's public synthetic fixture. The parent seeds
only a stopped temporary bank. Original verification still declares test SMS.
"""
from datetime import datetime, timezone
import json
import os
import signal
import threading

from evaluation.product_insurance.run_joint_matrix import Joint, Client, OUT
from evaluation.product_insurance.run_t01_http import seed_stopped_fixture


def main():
    j=Joint();stop=threading.Event();meta=OUT/'t01-browser-runtime.json'
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    value={'schema':'navren.pi.t01-formal-browser-instance/1','parent_pid':os.getpid(),
        'at':datetime.now(timezone.utc).isoformat(),'models':0,'real_sms':0,'production_touched':False}
    try:
        j.setup();workspace=j.a.finance['workspace_id'];j.stop()
        account=seed_stopped_fixture(j,workspace);j.start()
        j.a=Client(j.port);j.a.login(persona='tycoon')
        first=j.policy();second=j.post('policies',{'value':{**first['value'],
            'label':'第二项明确合成的本人保费义务','premium':{**first['value']['premium'],'amount':'700.00'}}})['record']
        value.update(url='http://127.0.0.1:'+str(j.port),state=str(j.state),workspace_id=workspace,
            account_id=account,policy_ids=[first['id'],second['id']],is_isolated=True,
            verification_scope='original LocalSms test provider, never provider MFA acceptance',
            source_before=j.source_pins(),service_processes=j.processes)
        meta.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
        print(value['url'],flush=True);stop.wait(1200)
    finally:
        j.stop();value.update(stopped=True,source_after=j.source_pins(),service_processes=j.processes)
        meta.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');j.temp.cleanup()


if __name__=='__main__':main()
