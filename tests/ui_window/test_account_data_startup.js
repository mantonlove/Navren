/* 实际 classic 脚本加载顺序：完整共享源码先于完整 BankClient；无接口替换。 */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {JSDOM,ResourceLoader,VirtualConsole}=require('jsdom');
const root=path.resolve(__dirname,'../..');
const task=path.join(root,'outputs/window-progress/ui/account-data-completion-20261017');
const output=process.argv[2];
const sha=value=>crypto.createHash('sha256').update(value).digest('hex');
const clone=value=>JSON.parse(JSON.stringify(value));
const pages=['account.html','recommend.html'];
const html=Object.fromEntries(pages.map(page=>[page,fs.readFileSync(path.join(root,page),'utf8')]));
const prefix=Object.fromEntries(pages.map(page=>{
  const tags=[...html[page].matchAll(/<script\b[^>]*\bsrc="([^"]+)"[^>]*><\/script>/g)];
  const end=tags.findIndex(match=>match[1].split('?')[0]==='js/bank_client.js');assert.ok(end>=0,page+' BankClient script exists');
  const rows=tags.slice(0,end+1).map(match=>{assert.doesNotMatch(match[0],/\b(?:defer|async)\b|\btype="module"/,page+' actual classic prefix');return {src:match[1],file:match[1].split('?')[0]};});
  assert.equal(rows[0].file,'js/storage.js');assert.ok(rows.findIndex(row=>row.file==='js/design.js')<end);
  return [page,rows];
}));
const files=[...new Set(pages.concat(...Object.values(prefix).map(rows=>rows.map(row=>row.file))))];
const sources=Object.fromEntries(files.map(file=>[file,fs.readFileSync(path.join(root,file),'utf8')]));
const sourcePins=Object.fromEntries(files.map(file=>[file,sha(sources[file])]));
const baselineFile=path.join(task,'tests-startup/storage.before-tdz.js');
const baseline=fs.readFileSync(baselineFile,'utf8');
const baselinePin='cadf642bd15f483b8c5631ed45fe27ecc7ce74d57e26113b770d10c0e2b3f099';
assert.equal(sha(baseline),baselinePin,'reconstructed source must exactly match original pre-hotfix consumer pin');
assert.equal(sourcePins['js/storage.js'],'6fcf1e86fc9a34c828689c1c50a3cf6e75dee8bc58344e4da9275373b00be189','frozen current source');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function settle(){for(let i=0;i<6;i++)await tick();}
async function until(test,label){const started=Date.now();while(!test()){if(Date.now()-started>3000)throw Error('Fixture timeout: '+label);await tick();}}
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});promise.abort=()=>{};return {promise,resolve};}
function summary(error){return {name:error.name,message:error.message,code:error.code??null};}

async function scenario(page,useBaseline){
  const bankGate=deferred(),calls=[],resources=[],states=[],jsdomErrors=[],unhandled=[];let w;
  const onUnhandled=error=>unhandled.push(summary(error));process.on('unhandledRejection',onUnhandled);
  const console=new VirtualConsole();console.on('jsdomError',error=>jsdomErrors.push(String(error)));
  class Loader extends ResourceLoader {
    fetch(url){const location=new URL(url),file=location.pathname.slice(1);assert.ok(prefix[page].some(row=>row.file===file),'unexpected resource '+url);resources.push(file);
      if(file==='js/bank_client.js')return bankGate.promise;
      return Promise.resolve(Buffer.from(file==='js/storage.js'&&useBaseline?baseline:sources[file]));}
  }
  const now=Date.now()/1000;
  const session={registered_user_id:'aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa',workspace_id:'11111111111141118111111111111111',account_id:'tycoon',privacy_revision:0,
    auth_subject:'welcome-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',session_generation:1,csrf:'fixture-startup-csrf',auth_expires_at:now+7200,lease_expires_at:now+3600};
  const state={contract:'navren.account-state/1',registered_user_id:session.registered_user_id,workspace_binding:session.workspace_id,workspace_id:session.workspace_id,privacy_revision:0,
    persona_id:session.account_id,revision:7,updated_at:'2026-10-17T00:00:00Z',initialized_sections:['profile','settings','preferences','insurance','favorites','reading','markers'],
    migration:{legacy_profile:'none',conflicts:[]},sections:{
      profile:{nickname:'本人昵称',full_name:'',phone:'',email:'',city:'',occupation:'模拟交易研究员',company:'',birthday:'',gender:'',bio:'',avatar:'',avatar_preset:'navren-flow-3'},
      settings:{theme:'dark',language:'en',mode:'youth',font_size:23,mode_font_sizes:{},voice_enabled:false,voice_preset:'male_podcast'},
      preferences:{recommendByRisk:false,recommendByHorizon:false,marketHotPush:false,marketHotVisible:false,weeklyReport:false},insurance:{topics:[]},favorites:[],reading:[],
      markers:{onboarded:true,privacy_read:true,voice_guide_read:true,tips:{chat:true,recommend:true,account:true,mine:true}}}};
  // 唯一插入的只读脚本暴露 classic lexical Storage，既不执行 eval，也不替换任何产品接口。
  const probe='<script>window.__startupApi={ready:options=>Storage.ready(options),isReady:()=>Storage.accountReady(),status:()=>Storage.accountStatus(),get:key=>Storage.get(key),font:()=>getModeFont(getInterfaceMode())};</script>';
  const scripts=prefix[page].map((row,index)=>'<script src="'+row.src+'"></script>'+(index===0?probe:'')).join('\n');
  const dom=new JSDOM('<!doctype html><html><head></head><body data-page="'+page.replace('.html','')+'"><div class="app"><header class="header"></header><main><select id="interfaceMode"><option value="classic">classic</option><option value="senior">senior</option><option value="youth">youth</option></select></main></div>'+scripts+'</body></html>',{
    url:'https://navren.invalid/'+page,runScripts:'dangerously',resources:new Loader(),pretendToBeVisual:true,virtualConsole:console,
    beforeParse(window){w=window;
      window.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
      const welcome=()=>({authenticated:true,registered_user_id:session.registered_user_id,auth_subject:session.auth_subject});
      window.NavrenWelcomeSession={getState:welcome,refresh:async()=>welcome()};
      window.fetch=async(url,options={})=>{const method=options.method||'GET';calls.push({url:String(url),method});assert.equal(method,'GET','no writes in startup fixture');
        const data=url==='/api/finance/session'?session:url==='/api/finance/account-state'?state:null;assert.ok(data,'unexpected fetch '+url);
        return {ok:true,status:200,json:async()=>clone(data),headers:{get:name=>name==='X-Navren-Workspace'?session.workspace_id:name==='X-Navren-Privacy-Revision'?'0':null}};};
      window.addEventListener('navren:account-state',event=>states.push({status:event.detail?.status,bank_loaded:typeof window.BankClient!=='undefined',ready_state:window.document.readyState}));
    }});
  try{
    // 真正阻塞后续 classic BankClient 资源，使实际 design.readAccount 自然发生 early ready。
    await until(()=>w.NavrenDesign&&states.some(row=>row.status==='unavailable'),'natural frontend-first unavailable state');
    assert.equal(typeof w.BankClient,'undefined');assert.equal(w.document.readyState,'loading');assert.equal(w.__startupApi.isReady(),false);
    const beforeError=await w.__startupApi.ready().then(()=>{throw Error('early ready unexpectedly succeeded');},error=>error);
    const before={bank_loaded:false,ready_state:w.document.readyState,status:w.__startupApi.status(),error:summary(beforeError),fetch_count:calls.length};
    assert.equal(calls.length,0,'no BankClient mock or premature real-session response');
    bankGate.resolve(Buffer.from(sources['js/bank_client.js']));
    await until(()=>w.document.readyState==='complete','native DOMContentLoaded/load');await settle();
    assert.ok(w.BankClient);assert.equal(typeof w.BankClient.guard,'function');
    let after;
    if(useBaseline){
      assert.match(beforeError.message,/Cannot access 'task' before initialization/,'real pre-hotfix TDZ, not missing API');
      const actualSession=await w.BankClient.ensure();assert.equal(actualSession.registered_user_id,session.registered_user_id);
      assert.equal(w.BankClient.guard().valid(actualSession),true,'actual client and guard work after script load');
      const afterError=await w.__startupApi.ready().then(()=>{throw Error('pre-hotfix should stay rejected');},error=>error);
      assert.equal(afterError,beforeError,'original rejected flight is sticky across real BankClient load');assert.equal(w.__startupApi.isReady(),false);
      assert.equal(calls.filter(row=>row.url==='/api/finance/account-state').length,0);
      after={is_ready:false,status:w.__startupApi.status(),error:summary(afterError),same_error_object:true,actual_bank_guard_valid:true};
    }else{
      assert.match(beforeError.message,/BankClient is not defined/,'missing dependency is contained without a TDZ');
      assert.equal(w.__startupApi.isReady(),true,'DOMContentLoaded automatically retries and hydrates without explicit after-load ready');
      const afterDOMReads=calls.filter(row=>row.url==='/api/finance/account-state').length;assert.ok(afterDOMReads>=1);
      const restored=await w.__startupApi.ready();assert.equal(restored.revision,7);assert.equal(w.BankClient.guard().valid(restored),true);
      assert.equal(w.__startupApi.get('qingzhou_voiceEnabled'),false);assert.equal(w.__startupApi.get('qingzhou_voicePreset'),'male_podcast');assert.equal(w.__startupApi.font(),23);
      assert.equal(w.document.documentElement.dataset.theme,'dark');assert.equal(w.NavrenI18n.language,'en');assert.equal(w.document.documentElement.style.fontSize,'23px');
      assert.equal(calls.filter(row=>row.url==='/api/finance/account-state').length,afterDOMReads,'cached ready is no longer a rejected flight');
      after={is_ready:true,status:w.__startupApi.status(),revision:restored.revision,actual_bank_guard_valid:true,automatic_read_before_explicit_ready:true};
    }
    assert.deepEqual(resources,prefix[page].map(row=>row.file),'all actual prefix scripts loaded in exact HTML order');
    assert.deepEqual(jsdomErrors,[]);assert.deepEqual(unhandled,[]);
    return {page,storage_source:useBaseline?'exact reconstructed cadf before':'formal frozen current 6fcf',script_order:prefix[page],before,after,events:states,calls,jsdom_errors:jsdomErrors,unhandled_rejections:unhandled};
  }finally{bankGate.resolve(Buffer.from(sources['js/bank_client.js']));dom.window.close();await settle();process.removeListener('unhandledRejection',onUnhandled);}
}

(async()=>{
  const groups=[];
  for(const [id,name,useBaseline] of [['START01','实际 account/recommend classic 顺序在早期缺依赖后自动恢复',false],['START02','精确 cadf 修前源码在同一顺序重现 TDZ 与 sticky flight',true]]){
    const paths=[];let failure=null;
    for(const page of pages){try{paths.push(await scenario(page,useBaseline));}catch(error){failure=error;paths.push({page,passed:false,error:error.stack||String(error)});}}
    groups.push({id,name,passed:!failure,paths});
  }
  const drift=files.filter(file=>sha(fs.readFileSync(path.join(root,file)))!==sourcePins[file]);
  const result={status:groups.every(group=>group.passed)&&!drift.length?'PASS_SCOPED_CLASSIC_STARTUP':'FAIL',passed_groups:groups.filter(group=>group.passed).length,failed_groups:groups.filter(group=>!group.passed).length,groups,
    current_source_sha256:sourcePins,current_source_drift:drift,before_storage_sha256:baselinePin,
    fixture:{real_classic_script_elements:true,runScripts:'dangerously',actual_full_modules_before_and_including_bank:prefix,actual_Storage:true,actual_BankClient:true,actual_ensure_and_guard:true,
      synthesized:['in-memory script ResourceLoader with BankClient resource gate','minimal inert body, omitting unrelated business views','validated welcome-session adapter and in-memory GET session/account-state DTOs','matchMedia platform shim','read-only lexical Storage probe script'],
      not_used:['outside-only eval','BankClient mock','Storage adapter mock','ensure/guard replacement','manual after-load readiness to make positive case pass'],
      not_covered:['scripts after bank_client.js','full page business initialization','real browser visual acceptance','real server persistence'],real_network_calls:0,database_writes:0,paid_calls:0}};
  if(output){fs.mkdirSync(output,{recursive:true});fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');}
  for(const group of groups){process.stdout.write((group.passed?'PASS ':'FAIL ')+group.id+' '+group.name+'\n');for(const row of group.paths)if(row.error)process.stdout.write(row.error+'\n');}
  process.stdout.write(JSON.stringify({status:result.status,passed_groups:result.passed_groups,failed_groups:result.failed_groups,current_source_drift:drift})+'\n');process.exitCode=result.status.startsWith('PASS')?0:1;
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
