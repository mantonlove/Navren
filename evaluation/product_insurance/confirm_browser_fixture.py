"""Advance an already UI-prepared original operation in the owned test service.

This HTTP fixture confirmation is deliberately separate from UI/provider MFA
acceptance. It never creates another operation or changes the verification source.
"""
import json
from pathlib import Path
import sys
from evaluation.product_insurance.run_joint_matrix import Client, OUT
from test_sms_permissions import CODE


def main():
    runtime=json.loads((OUT/'t01-browser-runtime.json').read_text())
    assert runtime['is_isolated'] and not runtime.get('stopped')
    oid=sys.argv[1];assert len(oid)==32 and all(c in '0123456789abcdef' for c in oid)
    client=Client(int(runtime['url'].rsplit(':',1)[1]));client.login(persona='tycoon')
    assert client.finance['workspace_id']==runtime['workspace_id']
    path='/api/finance/operations/'+oid;op=client.call(path)['operation']
    assert op['action']=='reserve_funds' and op['payload']['account_id']==runtime['account_id']
    assert op['payload']['amount_minor']==180000 and op['state']=='pending_confirmation'
    challenge=client.call(path+'/sms',{})
    assert challenge['source']=='test_double'
    done=client.call(path+'/confirm',{'confirmed':True,'confirmation_digest':op['confirmation_digest'],
        'challenge_id':challenge['challenge_id'],'verification_code':CODE})
    assert done['operation']['state']=='succeeded'
    report={'schema':'navren.pi.formal-preparation-http-fixture-confirmation/1',
        'scope':'original UI-prepared ID confirmed by explicit synthetic HTTP fixture; not UI/provider MFA acceptance',
        'operation':done['operation'],'budget':client.call('/api/finance/budget?account_id='+runtime['account_id']),
        'source':'test_double','models':0,'real_sms':0,'production_touched':False,'UI_execution_accepted':False}
    path=OUT/'t01-formal-fixture-confirmation.json';assert not path.exists()
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'state':done['operation']['state'],'operation_id':oid,
        'hold_id':done['operation']['result']['hold_id'],'budget':report['budget'],'UI_execution_accepted':False}))


if __name__=='__main__':main()
