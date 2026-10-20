/* 完整真实 Storage/BankClient；只读版本冲突的一次补读与现有身份/写入边界。 */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {JSDOM,VirtualConsole}=require('jsdom');
const root=path.resolve(__dirname,'../..'),output=process.argv[2];
const storageFile=process.argv[3]?path.resolve(process.argv[3]):path.join(root,'js/storage.js');
const selected=new Set((process.argv[4]||'').split(',').filter(Boolean));
const bankFile=path.join(root,'js/bank_client.js'),contractFile=path.join(root,'outputs/window-progress/base/account-data-completion-20261017/api-contract.json');
const storageSource=fs.readFileSync(storageFile,'utf8'),bankSource=fs.readFileSync(bankFile,'utf8'),contract=JSON.parse(fs.readFileSync(contractFile,'utf8'));
const sha=value=>crypto.createHash('sha256').update(value).digest('hex');
const pins={[path.relative(root,storageFile)]:sha(storageSource),'js/bank_client.js':sha(bankSource)};
assert.equal(pins['js/bank_client.js'],'fc99b07aaa9547e574def66a2b4c1e8573476deb5a16bd6dad74ad09370f2dc4');
assert.ok(['6fcf1e86fc9a34c828689c1c50a3cf6e75dee8bc58344e4da9275373b00be189','4a6622d6006cfb5eea7e44d4e9a4e89781bb177f98ef92ce35c4f5d953129f41'].includes(sha(storageSource)),'declared before/current source');
const clone=value=>value==null?value:JSON.parse(JSON.stringify(value));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function settle(){for(let i=0;i<5;i++)await tick();}
async function until(test,label){const start=Date.now();while(!test()){if(Date.now()-start>1800)throw Error('定向夹具等待超时：'+label);await tick();}}
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
const results=[],asyncErrors=[];
process.on('unhandledRejection',error=>asyncErrors.push(error.stack||String(error)));
const conflict=()=>({status:409,code:'ACCOUNT_STATE_VERSION_CONFLICT'});

async function fixture(){
  const errors=[],events=[],calls=[],readActions=[],postActions=[];
  const console=new VirtualConsole();console.on('jsdomError',error=>errors.push(String(error)));
  const dom=new JSDOM('<!doctype html><html><body><main></main></body></html>',{url:'https://navren.invalid/mine.html',runScripts:'dangerously',virtualConsole:console});
  const w=dom.window,d=w.document;if(d.readyState!=='complete')await new Promise(resolve=>w.addEventListener('load',resolve,{once:true}));
  function makeSession(name){const now=Date.now()/1000,other=name==='B';return {registered_user_id:other?'bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb':'aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa',workspace_id:other?'22222222222242228222222222222222':'11111111111141118111111111111111',
    account_id:other?'grain_farmer':'tycoon',privacy_revision:0,auth_subject:'verified-read-conflict-'+name+'-subject',session_generation:other?2:1,csrf:'fixture-csrf-'+name,auth_expires_at:now+7200,lease_expires_at:now+3600};}
  const sections=()=>{const value=clone(contract.get.sections);for(const key of Object.keys(value.preferences))value.preferences[key]=false;return value;};
  const client={serverSession:makeSession('A'),authenticated:true};
  const accounts=new Map([[client.serverSession.registered_user_id,{revision:2,sections:sections()}]]);
  function account(identity){if(!accounts.has(identity.registered_user_id)){const value=sections();value.settings.theme='dark';accounts.set(identity.registered_user_id,{revision:5,sections:value});}return accounts.get(identity.registered_user_id);}
  function dto(identity=client.serverSession){const value=account(identity);return {contract:'navren.account-state/1',registered_user_id:identity.registered_user_id,workspace_binding:identity.workspace_id,workspace_id:identity.workspace_id,
    privacy_revision:identity.privacy_revision,persona_id:identity.account_id,revision:value.revision,updated_at:null,initialized_sections:['profile','settings','preferences','insurance','favorites','reading','markers'],
    sections:clone(value.sections),migration:{legacy_profile:'none',conflicts:[]}};}
  function response(data,status,identity,onStatus){const captured=clone(data);let notified=false;return {ok:status<400,get status(){if(onStatus&&!notified){notified=true;queueMicrotask(onStatus);}return status;},json:async()=>clone(captured),
    headers:{get:name=>name.toLowerCase()==='x-navren-workspace'?identity.workspace_id:name.toLowerCase()==='x-navren-privacy-revision'?String(identity.privacy_revision):null}};}
  const welcome=()=>({authenticated:client.authenticated,registered_user_id:client.serverSession.registered_user_id,auth_subject:client.serverSession.auth_subject});
  w.NavrenWelcomeSession={getState:welcome,refresh:async()=>welcome()};
  w.fetch=async(url,options={})=>{
    const method=options.method||'GET',identity=clone(client.serverSession),body=options.body?JSON.parse(options.body):null;assert.ok(['/api/finance/session','/api/finance/account-state'].includes(String(url)),'unexpected real module request '+url);
    assert.equal(options.credentials,'same-origin');assert.equal(options.cache,'no-store');const row={url:String(url),method,body,identity,aborted:false};calls.push(row);options.signal?.addEventListener('abort',()=>{row.aborted=true;},{once:true});
    if(url==='/api/finance/session'){assert.equal(method,'GET');return response(identity,200,identity);}
    const actions=method==='GET'?readActions:postActions,action=actions.shift()||{};action.claimed=true;
    let data,status=action.status||200;
    if(status>=400)data={error:{code:action.code||'SYNTHETIC_ERROR',message:'受控同源响应 '+status}};
    else data=action.data?clone(action.data):dto(identity);
    if(method==='POST'){
      assert.equal(options.headers['X-QZ-CSRF'],identity.csrf);assert.equal(options.headers['X-Navren-Workspace'],identity.workspace_id);assert.equal(options.headers['X-Navren-Privacy-Revision'],String(identity.privacy_revision));
      assert.deepEqual(Object.keys(body).sort(),['expected_revision','section','value']);
      // 此夹具只观察冲突写入，不提供可成功写入的服务路径。
      assert.equal(status,409,'all POSTs in this scope must be rejected conflicts');
    }
    const result=response(data,status,identity,action.onStatus);
    if(action.hold){const hold=deferred();action.release=()=>hold.resolve(result);return hold.promise;}
    return result;
  };
  w.addEventListener('navren:account-state',event=>events.push(clone(event.detail)));
  const load=source=>{const script=d.createElement('script');script.textContent=source;d.body.append(script);};
  // classic 脚本共享 lexical 全局；模块完整载入，Storage/BankClient/guard/ensure 均不替换。
  load(storageSource);load(bankSource);load('window.__readConflict={Storage};');
  const Storage=w.__readConflict.Storage;await w.BankClient.ensure();
  const reads=()=>calls.filter(row=>row.url==='/api/finance/account-state'&&row.method==='GET');
  const posts=()=>calls.filter(row=>row.method==='POST');
  return {w,dom,Storage,client,accounts,account,dto,makeSession,events,calls,readActions,postActions,reads,posts,
    async finish(){await settle();assert.deepEqual(errors,[]);w.dispatchEvent(new w.Event('pagehide'));w.close();}};
}
async function check(id,name,operation){if(selected.size&&!selected.has(id))return;let f;try{f=await fixture();const details=await operation(f);await f.finish();results.push({id,name,passed:true,details});}
catch(error){f?.dom.window.close();results.push({id,name,passed:false,error:error.stack||String(error)});}}
const attempt=promise=>promise.then(value=>({value}),error=>({error}));

(async()=>{
  await check('RC01','精准只读409仅补读一次并恢复 revision2，false/空值不被默认覆盖',async f=>{
    f.readActions.push(conflict(),{});const result=await f.Storage.ready();assert.equal(f.reads().length,2);assert.equal(result.revision,2);assert.equal(f.Storage.accountReady(),true);
    assert.equal(f.Storage.get('qingzhou_voiceEnabled'),false);assert.ok(Object.values(f.Storage.get('qingzhou_preferences')).every(value=>value===false));
    for(const key of ['qingzhou_bookmarks','navren_reading_history','navren_insurancePreferences'])assert.deepEqual(clone(f.Storage.get(key)),[]);
    assert.deepEqual(clone(f.Storage.get('qingzhou_modeFontSizes')),{});assert.equal(result.sections.profile.email,'');assert.equal(f.Storage.get('qingzhou_privacyRead'),false);
    assert.equal(f.w.BankClient.guard().valid(result),true);assert.equal(f.posts().length,0);return {state_GETs:2,POSTs:0,actual_guard:true,revision:result.revision};
  });
  await check('RC02','重复精准409最多总两次GET，终止后显式重试仍可恢复',async f=>{
    f.readActions.push(conflict(),conflict(),{});await assert.rejects(f.Storage.ready(),error=>error.status===409&&error.code==='ACCOUNT_STATE_VERSION_CONFLICT');await settle();
    assert.equal(f.reads().length,2);assert.equal(f.Storage.accountReady(),false);assert.equal(f.Storage.get('qingzhou_voiceEnabled'),null);
    assert.equal((await f.Storage.ready()).revision,2);assert.equal(f.reads().length,3);assert.equal(f.Storage.accountReady(),true);return {failed_call_GETs:2,explicit_retry_GETs:1};
  });
  for(const [id,status,code] of [['RC03a',409,'OTHER_VERSION_CONFLICT'],['RC03b',403,'ACCOUNT_STATE_VERSION_CONFLICT'],['RC03c',503,'ACCOUNT_STATE_VERSION_CONFLICT']]){
    await check(id,'其他 code/status '+status+' '+code+' 不自动重试',async f=>{
      f.readActions.push({status,code},{});await assert.rejects(f.Storage.ready(),error=>error.status===status&&error.code===code);await settle();assert.equal(f.reads().length,1);assert.equal(f.Storage.accountReady(),false);assert.equal(f.posts().length,0);return {state_GETs:1};
    });
  }
  await check('RC04','首次409交付后欢迎绑定撤销，补读前实际身份检查拒绝',async f=>{
    // status getter 仅安排微任务时序：真实客户端已核验响应并构造409，Storage catch 前发生注销。
    f.readActions.push({...conflict(),onStatus:()=>{f.client.authenticated=false;}},{});
    await assert.rejects(f.Storage.ready());await settle();assert.equal(f.reads().length,1);assert.equal(f.Storage.accountReady(),false);assert.equal(f.w.BankClient.identity(),null);return {state_GETs:1,revoked_between_error_delivery_and_retry:true};
  });
  await check('RC05','补读飞行中切账号，迟到A200不能填入B',async f=>{
    const stale=f.dto();stale.revision=99;stale.sections.settings.voice_enabled=true;const hold={hold:true,data:stale};f.readActions.push(conflict(),hold);
    const old=attempt(f.Storage.ready());await until(()=>typeof hold.release==='function','A补读飞行中');const oldGuard=f.w.BankClient.guard();
    f.client.serverSession=f.makeSession('B');f.w.BankClient.beginAccountSwitch();f.w.dispatchEvent(new f.w.Event('navren:identity-revoked'));await f.w.BankClient.ensure();const current=await f.Storage.ready();
    hold.release();const late=await old;assert.ok(late.error);assert.equal(oldGuard.valid(),false);assert.equal(f.Storage.accountReady(),true);assert.equal((await f.Storage.ready()).registered_user_id,current.registered_user_id);
    assert.equal(current.revision,5);assert.equal(f.Storage.get('qingzhou_voiceEnabled'),false);assert.equal(f.Storage.get('navren_theme').id,'dark');return {late_A_rejected:true,B_revision:current.revision,actual_old_guard_invalid:true};
  });
  await check('RC06','补读飞行中真实privacy变化，迟到旧授权200拒绝',async f=>{
    const stale=f.dto();stale.revision=99;stale.sections.settings.voice_enabled=true;const hold={hold:true,data:stale};f.readActions.push(conflict(),hold);
    const old=attempt(f.Storage.ready());await until(()=>typeof hold.release==='function','旧授权补读飞行中');const oldGuard=f.w.BankClient.guard();
    f.client.serverSession.privacy_revision=1;f.account(f.client.serverSession).revision=3;await f.w.BankClient.refreshIdentity();
    await until(()=>f.Storage.accountReady(),'privacy-reset后的新授权恢复');hold.release();const late=await old;assert.ok(late.error);const current=await f.Storage.ready();
    assert.equal(oldGuard.valid(),false);assert.equal(current.privacy_revision,1);assert.equal(current.revision,3);assert.equal(f.Storage.get('qingzhou_voiceEnabled'),false);assert.equal(f.w.BankClient.guard().valid(current),true);
    assert.ok(f.calls.some(row=>row.url==='/api/finance/account-state'&&row.identity.privacy_revision===0&&row.aborted),'actual BankClient abort signaled old request; fixture still delivers it to test late-response rejection');
    return {late_old_privacy_rejected:true,current_privacy:1,current_revision:3,actual_old_guard_invalid:true};
  });
  await check('RC07','write409不重放POST，不把未确认用户写入覆盖到服务端',async f=>{
    await f.Storage.ready();const server=f.account(f.client.serverSession);server.revision=3;server.sections.settings.theme='dark';f.postActions.push(conflict());
    await assert.rejects(f.Storage.set('qingzhou_voiceEnabled',true),error=>error.status===409&&error.code==='ACCOUNT_STATE_VERSION_CONFLICT');await settle();
    assert.equal(f.posts().length,1);assert.equal(f.posts()[0].body.expected_revision,2);assert.equal(f.posts()[0].body.section,'settings');assert.equal(f.posts()[0].body.value.voice_enabled,true);
    assert.equal(f.reads().length,2,'initial read plus existing write-conflict refresh only');assert.equal(server.sections.settings.voice_enabled,false);assert.equal(f.Storage.get('qingzhou_voiceEnabled'),false);assert.equal(f.Storage.get('navren_theme').id,'dark');
    return {POSTs:1,POST_replays:0,server_voice_enabled:false,confirmed_revision:(await f.Storage.ready()).revision};
  });
  await settle();const drift=Object.entries(pins).filter(([file,hash])=>sha(fs.readFileSync(path.join(root,file)))!==hash).map(([file])=>file);
  const report={status:results.length&&results.every(row=>row.passed)&&!drift.length&&!asyncErrors.length?'PASS_SCOPED_READONLY_CONFLICT':'FAIL',passed:results.filter(row=>row.passed).length,failed:results.filter(row=>!row.passed).length,cases:results,requested_cases:[...selected],source_sha256:pins,source_drift:drift,contract_sha256:sha(fs.readFileSync(contractFile)),unhandled_rejections:asyncErrors,
    boundaries:{actual_complete_modules:[path.relative(root,storageFile),'js/bank_client.js'],actual_Storage:true,actual_BankClient_ensure_guard_and_request_validation:true,classic_script_shared_lexical_context:true,
      synthesized:['verified welcome-session adapter','same-origin in-memory session/account-state HTTP DTOs and deferred responses','HTTP error-delivery microtask for one binding-revocation case'],
      not_replaced:['Storage adapter','BankClient','ensure','guard','postSection','identity/epoch/generation checks'],not_covered:['real service/DB persistence','browser UI/visual acceptance'],real_network_calls:0,database_writes:0,paid_calls:0}};
  if(output){fs.mkdirSync(output,{recursive:true});fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2)+'\n');}
  for(const row of results)process.stdout.write((row.passed?'PASS ':'FAIL ')+row.id+' '+row.name+'\n'+(row.error?row.error+'\n':''));
  process.stdout.write(JSON.stringify({status:report.status,passed:report.passed,failed:report.failed,source_drift:drift})+'\n');process.exitCode=report.status.startsWith('PASS')?0:1;
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
