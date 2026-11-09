"""Read back the actual formally logged-in browser and its private fixture.

The preceding interactions use normal controls. This collector only reads DOM,
HTTP records and the owned temporary DB; it does not mutate browser state.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from evaluation.product_insurance.run_joint_matrix import Client, OUT, ROOT

CLI='/Users/manton/.codex/skills/playwright/scripts/playwright_cli.sh'


def main():
    runtime=json.loads((OUT/'t01-browser-runtime.json').read_text());assert runtime['is_isolated'] and not runtime.get('stopped')
    code="""async(page)=>{const scripts=[...document.scripts].map(s=>s.src).filter(s=>s.includes('research_workspace.js'));const raw=await(await fetch(scripts[0])).arrayBuffer();const sha=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',raw))).map(n=>n.toString(16).padStart(2,'0')).join('');return {url:page.url(),body:await page.locator('.rw-workspace').innerText(),prepareButtons:await page.getByRole('button',{name:'进入资金预留确认',exact:true}).count(),scripts,source_sha256:sha,identity:await page.evaluate(async()=>{const d=await window.BankClient.get('/session');return {workspace_id:d.workspace_id,privacy_revision:d.privacy_revision};})};}"""
    # Node scope has no window.fetch; read that one response in the page instead.
    code=code.replace("const raw=await(await fetch(scripts[0])).arrayBuffer();const sha=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',raw))).map(n=>n.toString(16).padStart(2,'0')).join('');",
        "const sha=await page.evaluate(async url=>{const raw=await(await fetch(url)).arrayBuffer();return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',raw))).map(n=>n.toString(16).padStart(2,'0')).join('');},scripts[0]);")
    code=code.replace("const scripts=[...document.scripts].map(s=>s.src).filter(s=>s.includes('research_workspace.js'));",
        "const scripts=await page.evaluate(()=>[...document.scripts].map(s=>s.src).filter(s=>s.includes('research_workspace.js')));")
    proc=subprocess.run([CLI,'-s=pi-root-t01','run-code',code],cwd=ROOT,text=True,capture_output=True,timeout=45)
    (OUT/'t01-formal-collector.log').write_text(proc.stdout+proc.stderr)
    assert proc.returncode==0 and '### Error' not in proc.stdout,proc.stdout[-1500:]+proc.stderr[-1500:]
    browser=json.loads(proc.stdout.split('### Result\n',1)[1].split('\n### Ran',1)[0])
    client=Client(int(runtime['url'].rsplit(':',1)[1]));client.login(persona='tycoon')
    assert client.finance['workspace_id']==browser['identity']['workspace_id']==runtime['workspace_id']
    tasks=client.call('/api/finance/research/tasks')['items'];zero=[r for r in tasks if r['value']['conditions'].get('amount')=='0.00']
    assert len(zero)==1;z=zero[0]
    assert z['links'].get('calendar_id') and z['links'].get('message_id') and not z['operation_links']
    assert z['value']['reminder']=={'enabled':True,'calendar':True,'message':True}
    assert browser['prepareButtons']==0 and '安排与提醒已保存并读回；本次无需新增预留。' in browser['body']
    active=[r for r in tasks if r['operation_links']];assert len(active)==1
    task=active[0];assert task['value']['conditions']['amount']=='1800.00'
    assert task['value']['conditions']['obligation_ids']==['policy:'+runtime['policy_ids'][0]]
    op=client.call('/api/finance/research/operations/'+task['operation_links'][0]['operation_id'])['operation']
    assert op['state']=='succeeded' and op['payload']['amount_minor']==180000
    quote=client.call('/api/finance/research/records/'+z['value']['record_ids'][0])['record']
    c=quote['value']['result']['calculations'];assert (c['required_reserve_minor'],c['already_protected_minor'],c['additional_reserve_minor'],c['uncommitted_cash_minor'])==(1080000,1080000,0,1420000)
    with sqlite3.connect('file:'+str(Path(runtime['state'])/'bank.sqlite3')+'?mode=ro',uri=True) as db:
        count=db.execute('SELECT count(*) FROM operations WHERE workspace_id=?',(runtime['workspace_id'],)).fetchone()[0]
        assert count==1
    source=hashlib.sha256((ROOT/'js/research_workspace.js').read_bytes()).hexdigest();assert source==browser['source_sha256']
    report={'schema':'navren.pi.formal-funding-consumer/1','status':'PASS_SCOPED_FORMAL_PREPARATION_READBACK_ZERO_SAVE',
        'registered_password_login':True,'real_BankClient':True,'identity_mock':False,'browser':browser,
        'current_js_sha256':source,'bank_source_sha256':hashlib.sha256((ROOT/'research_workspace.py').read_bytes()).hexdigest(),
        'same_UI_prepared_operation':op,'zero_new_task':z,'zero_new_quote':quote,'actual_operation_count':count,
        'screenshots':['ui-evidence/t01-formal-protection.png','ui-evidence/t01-formal-metrics.png','ui-evidence/t01-formal-zero-new.png'],
        'confirmation_scope':'same UI-prepared ID confirmed separately by original test_double HTTP fixture; UI/provider MFA is NOT accepted',
        'test_confirmation_receipt':'t01-formal-fixture-confirmation.json','UI_execution_accepted':False,
        'full_model_journey_accepted':False,'production_entry_accepted':False,'models':0,'real_sms':0,'production_writes':0}
    p=OUT/'t01-formal-consumer.json';assert not p.exists();p.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'source':source,'operation_count':count,'zero_new_task':z['id']}))


if __name__=='__main__':main()
