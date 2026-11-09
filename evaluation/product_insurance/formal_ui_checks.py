"""Reuse verified UI interactions through formal pages and real BankClient.

No joint.html, identity bootstrap mock, substituted BankClient or backend writes
from browser evaluation. Browser evaluation is used solely for readback.
"""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import time

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'outputs/product-insurance-implementation-20261018/acceptance'
ART=OUT/'ui-evidence';ART.mkdir(exist_ok=True)
CLI='/Users/manton/.codex/skills/playwright/scripts/playwright_cli.sh'
SESSION='pi-root-final'
runtime=json.loads((OUT.parent/'control/root-browser-runtime.json').read_text())
assert runtime['is_isolated'] and not runtime.get('stopped')
base=runtime['url']+'/recommend.html?view=research'
report={'schema':'navren.pi.formal-ui-engineering/1','runtime':runtime['url'],
    'registered_password_login':True,'real_BankClient':True,'identity_mock':False,
    'passed':[],'failed':[],'checks':{},'models':0,'real_sms':0,'current_production_entry':False}
prior=OUT/'formal-ui-engineering.json'
if prior.exists():
    saved=json.loads(prior.read_text())
    if saved.get('runtime')==runtime['url']:
        report=saved
tree=ast.parse((OUT.parent/'ui/browser_checks.py').read_text())
flows={ast.literal_eval(n.args[0]):ast.literal_eval(n.args[1]) for n in tree.body
    if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Name)
    and n.value.func.id=='run' for n in [n.value]}


def run(name):
    code=flows[name].replace('__URL__',base).replace('__OUT__',str(ART)).replace('__RUN__',str(time.time_ns())[-9:])
    code=code.replace(base+'?route=policies',runtime['url']+'/recommend.html?view=insurance')
    code=code.replace(base+'?route=cash',base)
    if name=='reserve_original_confirmation_and_status':
        code=code.replace('await page.goto("'+base+'");',
            'await page.goto("'+runtime['url']+'/recommend.html");await page.getByRole("button",{name:/现金安排/}).waitFor();await page.getByRole("button",{name:/现金安排/}).click();')
    code=code.replace('#root','.rw-workspace')
    for method in ('workspace','policies','tasks'):
        code=code.replace('jointApi.'+method+'()',"window.BankClient.get('/research/"+method+"')")
    code=code.replace('jointApi.record(id)',"window.BankClient.get('/research/records/'+id)")
    code=code.replace('jointApi.operation(id)',"window.BankClient.get('/research/operations/'+id)")
    prefix="const dateOverlayChecks=[];const setDate=async(label,value)=>{const input=page.locator('input[aria-label=\"'+label+'\"]'),height=await page.evaluate(()=>document.body.scrollHeight);await input.locator('..').getByRole('button').first().click();const popup=page.getByRole('dialog',{name:label,exact:true});dateOverlayChecks.push(Math.abs((await page.evaluate(()=>document.body.scrollHeight))-height)<=1);await popup.getByRole('button',{name:'选择年份和月份',exact:true}).click();await popup.getByRole('spinbutton',{name:'年份',exact:true}).fill(value.slice(0,4));await popup.getByRole('button',{name:Number(value.slice(5,7))+'月',exact:true}).click();await popup.getByRole('gridcell',{name:value.replaceAll('-','/'),exact:true}).click();};"
    if (OUT/'formal-ui-engineering.json').exists():
        import shutil
        shutil.copyfile(OUT/'formal-ui-engineering.json',OUT/('formal-ui-history-'+str(time.time_ns())+'.json'))
    report['failed']=[x for x in report.get('failed',[]) if x['name']!=name]
    # Each journey begins with the current DOM snapshot. Existing fixture flow
    # selectors operate on the same formal renderer; no eNN references are reused.
    subprocess.run([CLI,'-s='+SESSION,'snapshot'],cwd=ROOT,capture_output=True,text=True,timeout=20,check=True)
    r=subprocess.run([CLI,'-s='+SESSION,'run-code','async(page)=>{'+prefix+code+'}'],cwd=ROOT,
        capture_output=True,text=True,timeout=90)
    (ART/(name+'-formal.log')).write_text(r.stdout+r.stderr)
    if r.returncode or '### Error' in r.stdout:
        report['failed'].append({'name':name,'error':(r.stdout+r.stderr)[-2500:]})
    else:
        value=json.loads(r.stdout.split('### Result\n',1)[1].split('\n### Ran',1)[0].strip())
        if name=='readonly_comparison_history_conditions':
            assert value['sameId'] and value['staleWithdrawn']
            assert '30 天' in value['currentConditions'] and '365 天' in value['historicalConditions']
            assert value['serverRevision']==2 and value['serverHoldingDays']==30
        elif name=='actual_medical_private_binding_and_saved_task':
            assert value['record']['calculations']['expense_compensation_minor']==3200000
            assert value['record']['calculations']['claim_approved'] is False
            assert value['tasks'][0]['kind']=='claim_materials'
        elif name=='pension_type_and_mobile_320':
            assert '缴费与领取' in value['tabs']
            assert value['overflow']['body']<=value['overflow']['viewport']+1
            assert value['navOverflow']['body']<=value['navOverflow']['viewport']+1
        elif name=='create_policy_editable_coverages':
            assert value['coverage']['evidence_state']=='unverified' and value['coverage']['annual_limit'] is None
        elif name=='full_original_upload_and_download':
            source=(ART/'original-fixture.pdf').read_bytes();downloaded=(ART/'downloaded-original.pdf').read_bytes()
            assert source==downloaded and value['uploadEnabled']
            value.update(byte_count=len(source),sha256=hashlib.sha256(source).hexdigest(),byte_for_byte_equal=True)
        elif name=='reserve_original_confirmation_and_status':
            assert value['disabled'] and value['read']['state']=='pending_confirmation'
            assert value['read']['public_share_execution'] is False
            value['acceptance_credit']='PREPARATION_AND_REFUSAL_ONLY_NOT_FULL_TASK_POSITIVE'
        report['checks'][name]=value;report['passed'].append(name)
    report['source_sha256']={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in
        ('js/research_workspace.js','css/research_workspace.css','js/discover_workspace.js','recommend.html')}
    report['status']='FAIL_FORMAL_UI_ENGINEERING' if report['failed'] else 'PASS_SCOPED_FORMAL_UI_ENGINEERING'
    (OUT/'formal-ui-engineering.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(name, 'PASS' if name in report['passed'] else 'FAIL',flush=True)


if __name__=='__main__':
    import sys
    for name in sys.argv[1:] or ['readonly_comparison_history_conditions','actual_medical_private_binding_and_saved_task','pension_type_and_mobile_320']:
        run(name)
    sys.exit(bool(report['failed']))
