"""Final-source formal-page zero-new-hold acceptance, with actual loaded body SHA."""
import hashlib
import json
from pathlib import Path
import os
import sqlite3
import subprocess
from evaluation.product_insurance.run_joint_matrix import OUT, ROOT

CLI=os.environ.get('PLAYWRIGHT_CLI','playwright-cli')


def main():
    runtime=json.loads((OUT/'t01-browser-runtime.json').read_text());assert runtime['is_isolated'] and not runtime.get('stopped')
    source=hashlib.sha256((ROOT/'js/research_workspace.js').read_bytes()).hexdigest()
    with sqlite3.connect('file:'+str(Path(runtime['state'])/'bank.sqlite3')+'?mode=ro',uri=True) as db:
        before=db.execute('SELECT count(*) FROM operations WHERE workspace_id=?',(runtime['workspace_id'],)).fetchone()[0]
    code=r'''async(page)=>{
      const loaded=page.waitForResponse(r=>r.url().includes('/js/research_workspace.js'));
      await page.goto('__URL__/recommend.html?ui=root-final-__HASH__');
      const response=await loaded,bytes=[...await response.body()];
      const loadedSha=await page.evaluate(async bytes=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new Uint8Array(bytes)))).map(n=>n.toString(16).padStart(2,'0')).join(''),bytes);
      await page.getByRole('button',{name:/现金安排/}).click();
      await page.getByRole('button',{name:'核对用款与预留安排',exact:true}).click();
      await page.getByRole('button',{name:'资金账户：选择人民币账户',exact:true}).click();
      await page.getByRole('option',{name:'工资/养老金与个人结算',exact:true}).click();
      await page.getByRole('checkbox',{name:'验收本人医疗合同 · 2026-11-01',exact:true}).check();
      await page.getByRole('checkbox',{name:'确认按所选账户及保单测算，并保存本次条件与资料版本。',exact:true}).check();
      const quoteResponse=page.waitForResponse(r=>r.url().endsWith('/research/calculate')&&r.request().method()==='POST');
      await page.getByRole('button',{name:'核对用款安排',exact:true}).click();
      const quote=await(await quoteResponse).json();
      await page.getByRole('textbox',{name:'拟新增预留金额（元）',exact:true}).waitFor();
      const input=await page.getByRole('textbox',{name:'拟新增预留金额（元）',exact:true}).inputValue();
      await page.getByRole('textbox',{name:'资金安排名称',exact:true}).fill('Root最终后继零新增安排');
      await page.getByRole('checkbox',{name:'在财务日历提醒',exact:true}).check();
      await page.getByRole('checkbox',{name:'发送到消息中心',exact:true}).check();
      await page.getByRole('checkbox',{name:'确认保存此安排及所选提醒；新增资金预留另进入原操作确认和验证。',exact:true}).check();
      const savedResponse=page.waitForResponse(r=>r.url().endsWith('/research/tasks')&&r.request().method()==='POST');
      await page.getByRole('button',{name:'保存安排并核对操作',exact:true}).click();
      const saved=await(await savedResponse).json();
      await page.getByText('安排与提醒已保存并读回；本次不新增预留。',{exact:true}).waitFor();
      await page.getByRole('button',{name:'查看已保存安排',exact:true}).scrollIntoViewIfNeeded();
      await page.screenshot({path:'__OUT__/ui-evidence/t01-final-zero-new.png'});
      const read=await page.evaluate(async id=>(await window.BankClient.get('/research/records/'+id)).record,saved.record.id);
      return {loadedSha,loadedUrl:response.url(),quote,input,read,prepareButtons:await page.getByRole('button',{name:'进入资金预留确认',exact:true}).count()};
    }'''.replace('__URL__',runtime['url']).replace('__HASH__',source[:12]).replace('__OUT__',str(OUT))
    subprocess.run([CLI,'-s=pi-root-t01','snapshot'],cwd=ROOT,text=True,capture_output=True,check=True,timeout=20)
    proc=subprocess.run([CLI,'-s=pi-root-t01','run-code',code],cwd=ROOT,text=True,capture_output=True,timeout=90)
    (OUT/'t01-final-formal-ui.log').write_text(proc.stdout+proc.stderr)
    assert proc.returncode==0 and '### Error' not in proc.stdout,proc.stdout[-1800:]+proc.stderr[-1500:]
    result=json.loads(proc.stdout.split('### Result\n',1)[1].split('\n### Ran',1)[0])
    assert result['loadedSha']==source==hashlib.sha256((ROOT/'js/research_workspace.js').read_bytes()).hexdigest()
    c=result['quote']['record']['value']['result']['calculations']
    assert (c['required_reserve_minor'],c['already_protected_minor'],c['additional_reserve_minor'],c['uncommitted_cash_minor'])==(1080000,1080000,0,1420000)
    record=result['read'];assert record['value']['conditions']['amount']=='0.00' and not record['operation_links']
    assert record['value']['reminder']=={'enabled':True,'calendar':True,'message':True}
    assert record['links'].get('calendar_id') and record['links'].get('message_id')
    assert result['input']=='0.00' and result['prepareButtons']==0
    with sqlite3.connect('file:'+str(Path(runtime['state'])/'bank.sqlite3')+'?mode=ro',uri=True) as db:
        after=db.execute('SELECT count(*) FROM operations WHERE workspace_id=?',(runtime['workspace_id'],)).fetchone()[0]
    assert before==after==1
    report={'schema':'navren.pi.final-formal-zero/1','status':'PASS_FINAL_SOURCE_FORMAL_ZERO_NEW_SAVE',
        'actual_loaded_body_sha256':source,'backend_sha256':hashlib.sha256((ROOT/'research_workspace.py').read_bytes()).hexdigest(),
        'real_registered_login_and_BankClient':True,'identity_mock':False,'result':result,
        'financial_operation_count_before':before,'financial_operation_count_after':after,
        'UI_execution_accepted':False,'provider_MFA_accepted':False,'full_model_journey_accepted':False,
        'models':0,'real_sms':0,'production_writes':0,'scope':'same owned fixture after separate original HTTP test confirmation; zero-new save through actual final-source UI'}
    path=OUT/'t01-final-formal-zero.json';assert not path.exists();path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'loaded_sha256':source,'operation_count_before':before,'operation_count_after':after}))


if __name__=='__main__':main()
