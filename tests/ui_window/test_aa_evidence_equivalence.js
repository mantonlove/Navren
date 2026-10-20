/* Actual AA recovery/review + actual BankClient, observed DTO values and key orders. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {JSDOM,VirtualConsole}=require('jsdom');
const repo=process.env.NAVREN_PROJECT||path.resolve(__dirname,'../..');
const read=(name,fallback)=>fs.readFileSync(process.env[name]||path.join(repo,fallback),'utf8');
const aaSource=read('NAVREN_AA_SOURCE','js/aa_payments.js'),bankSource=read('NAVREN_BANK_SOURCE','js/bank_client.js');
const preview=JSON.parse(read('NAVREN_AA_PREVIEW','output/account-operations-recovery-20261017/aa-first-preview-response.json'));
const upload=JSON.parse(read('NAVREN_AA_UI_DRAFT','output/account-operations-recovery-20261017/ui-aa-draft.json')).writes.find(row=>row.data?.attachment).data.attachment;
const consumptions=JSON.parse(read('NAVREN_AA_CONSUMPTIONS','output/account-operations-recovery-20261017/initial-A-working_landlady-consumptions.json'));
const clone=value=>value==null?value:JSON.parse(JSON.stringify(value));
const fields=['id','source','merchant','purpose','occurred_at','paid_minor','refunded_minor','eligible_minor','record_type','environment'];
const selected=consumptions.consumptions.find(row=>row.id===preview.aa.bill.consumption.id);assert.ok(selected);
const candidateConsumption=Object.fromEntries(fields.map(key=>[key,selected[key]]));
assert.deepEqual(candidateConsumption,preview.aa.bill.consumption);assert.deepEqual(upload,preview.aa.bill.attachments[0]);
assert.notEqual(JSON.stringify(candidateConsumption),JSON.stringify(preview.aa.bill.consumption));
assert.notEqual(JSON.stringify(upload),JSON.stringify(preview.aa.bill.attachments[0]));
const canonical=value=>value&&typeof value==='object'?Array.isArray(value)?value.map(canonical):Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonical(value[key])])):value;
const equalValueHash=crypto.createHash('sha256').update(JSON.stringify(canonical(preview))).digest('hex');
const self=preview.aa.bill.participants.find(row=>row.status==='own').user_id;
const results=[],observations=[];
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(predicate,label){for(let i=0;i<100;i++){if(predicate())return;await delay(1);}assert.fail(label);}

async function fixture({expose=false}={}){
  const errors=[],vc=new VirtualConsole();vc.on('jsdomError',error=>errors.push(error));
  const dom=new JSDOM('<body><div id="aaHost"></div></body>',{url:'https://navren.test/account.html?view=home',runScripts:'outside-only',virtualConsole:vc}),w=dom.window;
  if(w.document.readyState!=='complete')await new Promise(resolve=>w.addEventListener('load',resolve,{once:true}));
  const now=Date.now()/1000;
  const session={registered_user_id:'a'.repeat(32),workspace_id:preview.workspace_id,account_id:self,privacy_revision:preview.privacy_revision,auth_subject:'controlled-authenticated:'+('a'.repeat(32)),csrf:'controlled-fixture-csrf',session_generation:1,auth_expires_at:now+600,lease_expires_at:now+300,environment:'simulation'};
  const welcome=()=>({authenticated:true,registered_user_id:session.registered_user_id,auth_subject:session.auth_subject});
  const state={calls:[],onState:[],imageDenied:0};
  w.NavrenWelcomeSession={getState:welcome,refresh:async()=>welcome()};
  w.NavrenViews={revision:()=>1};w.BroadcastChannel=class{postMessage(){}close(){}};
  w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
  w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');this.dispatchEvent(new w.Event('close'));};
  w.fetch=async(url,options={})=>{
    const method=options.method||'GET',target=String(url);state.calls.push({method,path:target});
    assert.equal(method,'GET','Review/recovery must not prepare, confirm, cancel or execute anything');
    let data,status=200;
    if(target==='/api/finance/session')data=clone(session);
    else if(target==='/api/finance/operations/'+preview.operation.id)data=clone(preview);
    else if(target==='/api/finance/aa/attachments/'+upload.id){state.imageDenied++;status=503;data={error:{code:'CONTROLLED_ORIGINAL_UNAVAILABLE',message:'测试未取得上传后的图片原件。'}};}
    else throw Error('Unexpected controlled HTTP '+target);
    return {ok:status===200,status,json:async()=>clone(data),headers:{get:name=>name==='X-Navren-Workspace'&&data.workspace_id?data.workspace_id:name==='X-Navren-Privacy-Revision'&&data.privacy_revision!=null?String(data.privacy_revision):null}};
  };
  w.eval(bankSource);
  let source=aaSource;
  if(expose){const marker='return Object.freeze({mount:';assert.ok(source.includes(marker));source=source.replace(marker,'Object.defineProperty(window,"__AAReadOnlyEquivalenceProbe",{value:Object.freeze({same,sameAttachments,checkEvidence,consumptionSnapshot})});\n  '+marker);}
  w.eval(source);await w.BankClient.ensure();
  return {w,state,dialog:()=>w.document.querySelector('dialog.aa-dialog[open]'),async close(){w.NavrenAAPayments.clear();await delay(1);dom.window.close();assert.equal(errors.length,0,errors.map(error=>error.stack).join('\n'));}};
}
async function withFixture(run,options){const f=await fixture(options);try{await run(f);}finally{await f.close();}}
async function check(name,run){try{await run();results.push({name,passed:true});}catch(error){results.push({name,passed:false,error:error.stack});throw error;}}

(async()=>{
  await check('unaltered-real-dto-complete-aa-review-and-original-operation-recovery',()=>withFixture(async f=>{
    let controller;
    try{controller=await f.w.NavrenAAPayments.showOperation(clone(preview),{isCurrent:()=>true,readOnly:true,expected_operation_version:preview.operation.version,onState:op=>f.state.onState.push({id:op.id,state:op.state})});}
    catch(error){assert.fail('Unaltered real operation DTO must open the actual AA review: '+error.message);}
    assert.ok(controller);await until(()=>f.dialog()?.textContent.includes('待本人核对，尚未执行'),'Actual AA review must reach pending read-only presentation');
    const query=[...f.dialog().querySelectorAll('button')].find(button=>button.textContent==='重新读取状态');assert.ok(query);await until(()=>!query.disabled,'Actual recovery control must become available');query.click();
    await until(()=>f.state.onState.length>=2,'Actual query path must re-read and accept the original operation');
    assert.equal(f.state.calls.filter(row=>row.path==='/api/finance/operations/'+preview.operation.id).length,1);
    assert.ok(f.dialog().textContent.includes('待本人核对，尚未执行'));assert.ok(!f.dialog().querySelector('.aa-error').textContent);
    const confirm=[...f.dialog().querySelectorAll('button')].find(button=>button.textContent==='确认');assert.ok(confirm.hidden,'This check uses read-only review and grants no confirmation');
    assert.equal(f.state.calls.filter(row=>row.method!=='GET').length,0);
    observations.push({name:'actual unaltered DTO review/recovery',canonical_value_sha256:equalValueHash,full_dto_values_and_key_order_unchanged:true,baseline_already_expected_to_pass:true,consumer:'complete AA showOperation → operation → preview/checkEvidence → actual query change/readOperation/request → actual BankClient.get',consumption_fields:fields.length,attachment_fields:Object.keys(upload).length,operation_states:clone(f.state.onState),requests:clone(f.state.calls),image_original_read:false,confirmation_or_execution_accepted:false});
  }));
  await check('actual-checkEvidence-accepts-unaltered-real-bill-against-observed-ui-expected-snapshot',()=>withFixture(async f=>{
    const helpers=f.w.__AAReadOnlyEquivalenceProbe;
    const expected={consumption:helpers.consumptionSnapshot(clone(selected)),attachments:[clone(upload)]};
    assert.deepEqual(clone(expected.consumption),candidateConsumption,'The actual snapshot helper must generate the observed UI key order');
    assert.deepEqual(canonical(clone(expected)),canonical({consumption:preview.aa.bill.consumption,attachments:preview.aa.bill.attachments}));
    let actual;
    try{actual=helpers.checkEvidence(clone(preview.aa.bill),expected);}
    catch(error){assert.fail('Observed UI expected evidence must equal real frozen bill: '+error.message);}
    assert.deepEqual(clone(actual.consumption),preview.aa.bill.consumption);assert.deepEqual(clone(actual.attachments),preview.aa.bill.attachments);
    observations.push({name:'exact UI evidence equivalence regression',real_bill_unmodified:true,actual_consumptionSnapshot:true,actual_checkEvidence:true,readonly_export_only:true,ui_consumption_key_order:Object.keys(expected.consumption),bill_consumption_key_order:Object.keys(preview.aa.bill.consumption),ui_attachment_key_order:Object.keys(upload),bill_attachment_key_order:Object.keys(preview.aa.bill.attachments[0]),field_type_or_value_differences:[]});
  },{expose:true}));
  await check('complete-consumer-rejects-real-field-value-id-sha-size-and-shape-differences',async()=>{
    const variants=[
      ['consumption merchant',v=>v.aa.bill.consumption.merchant+='changed'],
      ['consumption amount',v=>v.aa.bill.consumption.paid_minor++],
      ['consumption id',v=>v.aa.bill.consumption.id+='changed'],
      ['consumption numeric type',v=>v.aa.bill.consumption.paid_minor=String(v.aa.bill.consumption.paid_minor)],
      ['consumption extra field',v=>v.aa.bill.consumption.extra='forbidden'],
      ['consumption missing field',v=>delete v.aa.bill.consumption.merchant],
      ['picture sha',v=>{const a=v.aa.bill.attachments[0];a.sha256=(a.sha256[0]==='a'?'b':'a')+a.sha256.slice(1);}],
      ['picture width',v=>v.aa.bill.attachments[0].width++],
      ['picture height',v=>v.aa.bill.attachments[0].height++],
      ['picture byte size',v=>v.aa.bill.attachments[0].byte_size++],
      ['picture id',v=>v.aa.bill.attachments[0].id='f'.repeat(32)],
      ['picture extra field',v=>v.aa.bill.attachments[0].extra='forbidden'],
      ['picture missing field',v=>delete v.aa.bill.attachments[0].sha256],
      ['bill total amount',v=>v.aa.bill.total_minor++],
    ];
    const rejected=[];
    for(const [name,change]of variants)await withFixture(async f=>{const value=clone(preview);change(value);await assert.rejects(f.w.NavrenAAPayments.showOperation(value,{isCurrent:()=>true,readOnly:true,expected_operation_version:preview.operation.version}),error=>/凭证|摘要|消费|图片|预览金额|草稿/.test(error.message),'Reject '+name);assert.equal(f.dialog(),null);assert.equal(f.state.calls.filter(row=>row.method!=='GET').length,0);rejected.push(name);});
    observations.push({name:'strict negative evidence variants',rejected});
  });
  await check('actual-equivalence-helper-sorts-only-object-keys-and-retains-array-and-id-rules',()=>withFixture(async f=>{
    const {same,sameAttachments}=f.w.__AAReadOnlyEquivalenceProbe;
    assert.equal(same({b:{y:2,x:1},a:[{q:1,p:2},3]},{a:[{p:2,q:1},3],b:{x:1,y:2}}),true);
    assert.equal(same([1,2],[2,1]),false);assert.equal(same([{a:1},{a:2}],[{a:2},{a:1}]),false);
    assert.equal(same({a:1},{a:'1'}),false);assert.equal(same({a:1},{a:1,b:2}),false);
    const first=clone(upload),second={...clone(upload),id:'f'.repeat(32)};
    assert.equal(same([first,second],[second,first]),false,'Recursive canonicalization must not reorder arrays');
    assert.equal(sameAttachments([first,second],[second,first]),true,'Existing attachment ID-set comparison remains unchanged');
    assert.equal(sameAttachments([first],[{...first,sha256:'0'.repeat(64)}]),false);
  },{expose:true}));
})().then(()=>finish(true)).catch(error=>{console.error(error.stack||error);finish(false);process.exitCode=1;});
function finish(passed){const packet={schema:'navren.aa-evidence-equivalence/1',passed,results,observations,actual_network_calls:0,browser_operations:0,scope:'Full unchanged AA module and actual BankClient recover the unaltered real DTO (also passes before). Targeted key-order regression uses readonly exports of actual checkEvidence/consumptionSnapshot with the unmodified real bill, actual GET candidate and actual upload metadata. Controlled HTTP; original image unavailable; no confirmation/execution/backend/media-byte acceptance.'};if(process.env.NAVREN_AA_EQ_RESULT)fs.writeFileSync(process.env.NAVREN_AA_EQ_RESULT,JSON.stringify(packet,null,2)+'\n');console.log(JSON.stringify(packet));}
