'use strict';
/* 实际完整Storage/BankClient；双window同源CAS合成服务。不是主HTTP/浏览器验收。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const root=path.resolve(__dirname,'../..'),output=process.argv[2],selected=new Set((process.argv[3]||'').split(',').filter(Boolean));
assert(output,'必须由有界runner给出新的结果路径');
const files=['js/storage.js','js/bank_client.js'],source=Object.fromEntries(files.map(file=>[file,fs.readFileSync(path.join(root,file),'utf8')]));
const hash=value=>crypto.createHash('sha256').update(value).digest('hex'),pinned=Object.fromEntries(files.map(file=>[file,hash(source[file])]));
const contractFile=path.join(root,'outputs/window-progress/base/account-data-completion-20261017/api-contract.json'),contract=JSON.parse(fs.readFileSync(contractFile));
assert.equal(contract.contract,'navren.account-state/1');
const clone=value=>JSON.parse(JSON.stringify(value)),asyncErrors=[],network=[],results=[];
const deny=kind=>()=>{network.push(kind);throw Error('禁止实际网络:'+kind);};require('node:net').Socket.prototype.connect=deny('net.connect');for(const name of ['node:http','node:https']){const mod=require(name);mod.request=deny(name+'.request');mod.get=deny(name+'.get');}global.fetch=deny('global.fetch');
const {JSDOM,VirtualConsole}=require('jsdom');process.on('unhandledRejection',error=>asyncErrors.push(error.stack||String(error)));
const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
async function until(predicate,label){const end=Date.now()+1200;while(!predicate()){if(Date.now()>end)throw Error('等候超时:'+label);await tick();}}
function model(){
 const owners=new Map(),windows=new Set(),storage=new Map(),channels=new Map(),calls=[],holds=[];let offline=false,rejectPost=false,failReadAfterPost=false;
 function account(name){if(!owners.has(name))owners.set(name,{id:crypto.randomUUID().replaceAll('-',''),revision:0,initialized:[],sections:clone(contract.get.sections),workspaces:{one:crypto.randomUUID().replaceAll('-',''),two:crypto.randomUUID().replaceAll('-','')},login:0});return owners.get(name);}
 function session(name,workspace='one'){const owner=account(name),now=Date.now()/1000,n=++owner.login;return{registered_user_id:owner.id,workspace_id:owner.workspaces[workspace],account_id:workspace==='one'?'tycoon':'grain_farmer',auth_subject:'isolated-account-'+name+'-'+n+'-subject',csrf:'isolated-account-'+name+'-'+n+'-csrf',session_generation:n,auth_expires_at:now+7200,lease_expires_at:now+3600,privacy_revision:0,personal_revision:0,environment:'simulation',selection_required:false};}
 function dto(identity){const owner=[...owners.values()].find(row=>row.id===identity.registered_user_id),sections=clone(owner.sections);sections.favorites=sections.favorites.filter(item=>['market_news','market_weekly'].includes(item.category)||item.workspace_id===identity.workspace_id);return{contract:contract.contract,registered_user_id:owner.id,workspace_binding:identity.workspace_id,persona_id:identity.account_id,revision:owner.revision,updated_at:null,initialized_sections:[...owner.initialized],sections,migration:{legacy_profile:'none',conflicts:[]},workspace_id:identity.workspace_id,privacy_revision:identity.privacy_revision};}
 function response(data,status,identity){return{ok:status<400,status,json:async()=>clone(data),headers:{get:name=>name.toLowerCase()==='x-navren-workspace'?identity.workspace_id:name.toLowerCase()==='x-navren-privacy-revision'?String(identity.privacy_revision):null}};}
 async function dispatch(client,url,options={}){
  assert.equal(new URL(url,'https://navren.invalid').origin,'https://navren.invalid');const identity=clone(client.session),method=options.method||'GET',body=options.body?JSON.parse(options.body):null,request={client:client.id,path:String(url),method,body,identity};calls.push(request);
  let status=200,data;
  if(url==='/api/finance/session'&&method==='GET')data=identity;
  else if(url===contract.path){
   if(offline||method==='GET'&&client.failNextRead){client.failNextRead=false;throw Error('合成离线');}
   const owner=[...owners.values()].find(row=>row.id===identity.registered_user_id);
   if(method==='GET')data=dto(identity);
   else{
    assert.equal(options.headers['X-QZ-CSRF'],identity.csrf);assert.equal(options.headers['X-Navren-Workspace'],identity.workspace_id);assert.equal(options.headers['X-Navren-Privacy-Revision'],String(identity.privacy_revision));
    assert.deepEqual(Object.keys(body).sort(),body.operation==='migrate'?['expected_revision','operation','section','source','value']:['expected_revision','section','value']);
    if(rejectPost){status=503;data={error:{code:'SYNTHETIC_UNAVAILABLE',message:'合成保存失败'}};}
    else if(body.expected_revision!==owner.revision){status=409;data={error:{code:'ACCOUNT_STATE_VERSION_CONFLICT',message:'合成CAS冲突'}};}
    else if(body.operation==='migrate'&&owner.initialized.includes(body.section)){status=409;data={error:{code:'ACCOUNT_STATE_ALREADY_INITIALIZED',message:'已初始化'}};}
    else{
     if(body.operation==='migrate')assert.deepEqual(body.source,{kind:'owned_browser_cache',workspace_id:identity.workspace_id,auth_subject:identity.auth_subject,privacy_revision:identity.privacy_revision});
     let value=clone(body.value);if(body.section==='favorites'){assert(value.every(item=>['market_news','market_weekly'].includes(item.category)||item.workspace_id===identity.workspace_id));value=owner.sections.favorites.filter(item=>!['market_news','market_weekly'].includes(item.category)&&item.workspace_id!==identity.workspace_id).concat(value);}
     owner.sections[body.section]=value;if(!owner.initialized.includes(body.section))owner.initialized.push(body.section);owner.revision++;data=dto(identity);if(failReadAfterPost)client.failNextRead=true;
    }
   }
  }else throw Error('未声明合成请求:'+method+' '+url);
  const result=response(data,status,identity),hold=holds.find(row=>!row.claimed&&row.method===method&&row.client===client.id&&row.path===url);
  if(hold){hold.claimed=true;return new Promise(resolve=>{hold.release=()=>resolve(result);});}return result;
 }
 return{account,session,dto,calls,storage,windows,channels,fetch:dispatch,hold(client,method='GET'){const row={client:client.id,method,path:contract.path,claimed:false,release:null};holds.push(row);return row;},setOffline:value=>{offline=value;},rejectPost:value=>{rejectPost=value;},failReadAfterPost:value=>{failReadAfterPost=value;}};
}
let clientSequence=0;
async function fixture(service,name='A',workspace='one',{seed,signalMode='both'}={}){
 const errors=[],events=[],vc=new VirtualConsole();vc.on('jsdomError',error=>errors.push(error.stack||error.message));
 const dom=new JSDOM('<!doctype html><html><body><main></main></body></html>',{url:'https://navren.invalid/mine.html',runScripts:'outside-only',virtualConsole:vc}),w=dom.window;
 await tick();const client={id:'window-'+(++clientSequence),session:service.session(name,workspace),w,failNextRead:false,authenticated:true,clockMs:Date.now()};service.windows.add(client);
 const NativeDate=w.Date;w.Date=class extends NativeDate{constructor(...args){super(...(args.length?args:[client.clockMs]));}static now(){return client.clockMs;}};
 const nativeAdd=w.document.addEventListener.bind(w.document);w.document.addEventListener=(type,listener,options)=>type==='DOMContentLoaded'?undefined:nativeAdd(type,listener,options);
 const local={get length(){return service.storage.size;},key:index=>[...service.storage.keys()][index]??null,getItem:key=>service.storage.has(key)?service.storage.get(key):null,setItem(key,value){const old=service.storage.get(key)??null;service.storage.set(key,String(value));if(signalMode==='both'||signalMode==='storage')for(const other of service.windows)if(other!==client)queueMicrotask(()=>{if(!service.windows.has(other))return;const event=new other.w.Event('storage');Object.defineProperties(event,{key:{value:key},oldValue:{value:old},newValue:{value:String(value)}});other.w.dispatchEvent(event);});},removeItem:key=>service.storage.delete(key)};
 Object.defineProperty(w,'localStorage',{value:local});
 w.BroadcastChannel=class{constructor(channelName){this.name=channelName;this.owner=client;this.onmessage=null;if(!service.channels.has(channelName))service.channels.set(channelName,new Set());service.channels.get(channelName).add(this);}postMessage(data){if(signalMode==='none'||signalMode==='storage')return;for(const other of service.channels.get(this.name)||[])if(other!==this)queueMicrotask(()=>{if(service.windows.has(other.owner))other.onmessage?.({data:clone(data)});});}close(){service.channels.get(this.name)?.delete(this);}};
 w.NavrenWelcomeSession={getState:()=>({authenticated:client.authenticated,registered_user_id:client.session.registered_user_id,auth_subject:client.session.auth_subject}),refresh:async()=>w.NavrenWelcomeSession.getState()};
 w.NavrenViews={revision:()=>0};w.fetch=(url,options)=>service.fetch(client,url,options);w.XMLHttpRequest.prototype.open=deny('XMLHttpRequest');w.addEventListener('error',event=>errors.push(String(event.error?.stack||event.message)));w.addEventListener('navren:account-state',event=>events.push(clone(event.detail)));
 if(seed)seed(service,client);
 w.eval(files.map(file=>source[file]).join('\n;\n')+'\nwindow.__accountStateTest={Storage,getModeFont,setModeFont,getInterfaceMode};');w.Storage=w.__accountStateTest.Storage;
 await w.BankClient.ensure();
 return{w,dom,client,events,errors,service,async ready(){return w.Storage.ready();},async switch(next,nextWorkspace='one'){client.session=service.session(next,nextWorkspace);w.BankClient.beginAccountSwitch();w.dispatchEvent(new w.Event('navren:identity-revoked'));await w.BankClient.ensure();await w.Storage.ready();},posts:()=>service.calls.filter(row=>row.client===client.id&&row.method==='POST'),reads:()=>service.calls.filter(row=>row.client===client.id&&row.path===contract.path&&row.method==='GET'),close(){service.windows.delete(client);w.dispatchEvent(new w.Event('pagehide'));w.close();assert.deepEqual(errors,[]);}};
}
async function using(service,name,options,operation){const f=await fixture(service,name,'one',options);try{return await operation(f);}finally{f.close();}}
async function check(id,name,operation){if(selected.size&&!selected.has(id))return;const start=Date.now();try{const details=await operation();results.push({id,name,passed:true,details,elapsed_ms:Date.now()-start});console.log('PASS '+id);}catch(error){results.push({id,name,passed:false,error:error.stack||String(error),elapsed_ms:Date.now()-start});console.error('FAIL '+id+'\n'+(error.stack||error));}}
async function cases(){
 await check('AS01','冷启动真实GET恢复false与原文，不默认写入、不认领设备全局键',async()=>{
  const s=model(),a=s.account('A');a.sections.settings.voice_enabled=false;a.sections.preferences.weeklyReport=false;a.sections.profile.occupation='模拟器工程师';
  return using(s,'A',{seed(service){service.storage.set('navren_theme',JSON.stringify({id:'dark'}));service.storage.set('qingzhou_voiceEnabled','true');service.storage.set('qingzhou_bookmarks',JSON.stringify([{text:'无归属旧收藏'}]));}},async f=>{
   assert.equal(f.w.Storage.get('navren_theme'),null);await f.ready();assert.deepEqual(clone(f.w.Storage.get('navren_theme')),{id:'light'});assert.equal(f.w.Storage.get('qingzhou_voiceEnabled'),false);assert.equal(f.w.Storage.get('qingzhou_preferences').weeklyReport,false);assert.equal((await f.ready()).sections.profile.occupation,'模拟器工程师');assert.equal(f.posts().length,0);assert.equal(f.w.Storage.get('qingzhou_bookmarks').length,0);return{server_false_preserved:true,verbatim_profile:true,implicit_POST:0};
  });
 });
 await check('AS02','真实整节CAS保存后GET读回并确认状态',async()=>{
  const s=model();return using(s,'A',{},async f=>{await f.ready();const before=f.reads().length;assert.equal(await f.w.Storage.set('qingzhou_voiceEnabled',true),true);assert.equal(f.posts().length,1);assert.equal(f.posts()[0].body.expected_revision,0);assert.equal(f.posts()[0].body.section,'settings');assert.equal(f.reads().length,before+1);assert.equal(f.w.Storage.get('qingzhou_voiceEnabled'),true);return{POST:1,confirming_GET:1,actual_guard:true};});
 });
 await check('AS03','合法owner分区三节迁移，已初始化空节不复活',async()=>{
  const observations=[];
  for(const initialized of [false,true]){const s=model();const a=s.account('A');if(initialized)a.initialized=['preferences','favorites','reading'];
   observations.push(await using(s,'A',{seed(service,client){const i=client.session,key='navren:private-cache:provisional:v1:'+i.registered_user_id+':'+i.workspace_id;service.storage.set(key+':data',JSON.stringify({schema:'navren-private-cache-provisional-v1',registered_user_id:i.registered_user_id,workspace_id:i.workspace_id,privacy_revision:0,generation:0,values:{preferences:{weeklyReport:false},bookmarks:[{category:'market_weekly',object_kind:'weekly',object_id:'owned-week',version:1,text:'归属已证实的公开收藏'},{text:'归属已证实的私人收藏',registered_user_id:i.registered_user_id,workspace_id:i.workspace_id,privacy_revision:0}], 'reading-history':[{kind:'weekly',id:'owned-week',version:1,source_scope:'public',label:'归属已证实的阅读'}]}}));}},async f=>{
    await f.ready();if(initialized){assert.equal(f.posts().length,0);assert.equal(f.w.Storage.get('qingzhou_bookmarks').length,0);assert.equal(f.w.Storage.get('navren_reading_history').length,0);assert.equal(f.w.Storage.get('qingzhou_preferences').weeklyReport,true);}else{assert.equal(f.posts().length,3);assert(f.posts().every(row=>row.body.operation==='migrate'));assert.equal(f.w.Storage.get('qingzhou_preferences').weeklyReport,false);assert.equal(f.w.Storage.get('qingzhou_bookmarks').length,2);assert.equal(f.w.Storage.get('navren_reading_history').length,1);}
    return{initialized,posts:f.posts().length,server_first:true,missing_meta_generation0_supported:true};
   }));
  }return observations;
 });
 await check('AS05','冷启离线不ready；保存/读回失败保留旧confirmed而不假成功',async()=>{
  const s=model();return using(s,'A',{},async f=>{
   s.setOffline(true);await assert.rejects(f.ready());assert.equal(f.w.Storage.accountReady(),false);assert.equal(f.w.Storage.get('navren_theme'),null);s.setOffline(false);await f.ready();
   const reads=f.reads().length,readyEvents=f.events.filter(row=>row.status==='ready').length;s.rejectPost(true);await assert.rejects(f.w.Storage.set('qingzhou_voiceEnabled',true));assert.equal(f.reads().length,reads);assert.equal(f.w.Storage.get('qingzhou_voiceEnabled'),false);assert.equal(f.w.Storage.accountReady(),true);assert.equal(f.events.filter(row=>row.status==='ready').length,readyEvents);
   s.rejectPost(false);s.failReadAfterPost(true);await assert.rejects(f.w.Storage.set('qingzhou_voiceEnabled',true));assert.equal(s.account('A').sections.settings.voice_enabled,true);assert.equal(f.w.Storage.get('qingzhou_voiceEnabled'),false);assert.equal(f.events.filter(row=>row.status==='ready').length,readyEvents);s.failReadAfterPost(false);
   s.setOffline(true);await assert.rejects(f.w.Storage.ready({refresh:true}));assert.equal(f.w.Storage.get('qingzhou_voiceEnabled'),false);assert.equal(f.w.Storage.accountReady(),true);s.setOffline(false);await f.w.Storage.ready({refresh:true});assert.equal(f.w.Storage.get('qingzhou_voiceEnabled'),true);
   return{cold_failure_not_ready:true,warm_confirmed_preserved:true,server_committed_but_unconfirmed_not_success:true};
  });
 });
 await check('AS06','全账号section CAS冲突只重读、不覆盖别人写入',async()=>{
  const s=model();return using(s,'A',{signalMode:'none'},async f=>{await f.ready();const a=s.account('A');a.revision=1;a.sections.settings.theme='dark';a.initialized.push('settings');const reads=f.reads().length;await assert.rejects(f.w.Storage.set('qingzhou_voiceEnabled',true),error=>error.code==='ACCOUNT_STATE_VERSION_CONFLICT');assert.equal(f.posts().length,1);assert.equal(f.posts()[0].body.expected_revision,0);assert.equal(f.reads().length,reads+1);assert.equal(a.sections.settings.theme,'dark');assert.equal(a.sections.settings.voice_enabled,false);assert.equal(f.w.Storage.get('navren_theme').id,'dark');assert.equal(f.w.Storage.get('qingzhou_voiceEnabled'),false);return{stale_revision_rejected:true,refresh_GET:1,blind_overwrite_POST:0};});
 });
 await check('AS09','fonts同section原子保存与旧font_size当前模式回退',async()=>{
  const s=model(),a=s.account('A');a.sections.settings.mode='classic';a.sections.settings.font_size=19;
  return using(s,'A',{},async f=>{await f.ready();assert.equal(f.w.__accountStateTest.getModeFont('classic'),19);assert.equal(f.w.__accountStateTest.getModeFont('senior'),'large');const before=f.reads().length;assert.equal(await f.w.__accountStateTest.setModeFont('classic',18),true);assert.equal(f.posts().length,1);const body=f.posts()[0].body;assert.equal(body.section,'settings');assert.equal(body.value.font_size,18);assert.equal(body.value.mode_font_sizes.classic,18);assert.equal(f.reads().length,before+1);assert.equal(f.w.Storage.get('qingzhou_fontSize'),18);assert.equal(f.w.Storage.get('qingzhou_modeFontSizes').classic,18);return{single_POST:true,two_font_fields_atomic:true,current_mode_legacy_fallback:true};});
 });
 await check('AS03R','错owner/workspace/privacy和quarantine分区不导入',async()=>{
  for(const kind of ['owner','workspace','privacy','quarantine']){const s=model();await using(s,'A',{seed(service,client){const i=client.session,key='navren:private-cache:provisional:v1:'+i.registered_user_id+':'+i.workspace_id,generation=kind==='quarantine'?1:0;service.storage.set(key+':data',JSON.stringify({schema:'navren-private-cache-provisional-v1',registered_user_id:kind==='owner'?'f'.repeat(32):i.registered_user_id,workspace_id:kind==='workspace'?'f'.repeat(32):i.workspace_id,privacy_revision:kind==='privacy'?1:0,generation,values:{preferences:{weeklyReport:false},bookmarks:[{category:'market_weekly',text:'不可导入',object_id:'invalid'}]}}));if(kind==='quarantine')service.storage.set(key+':meta',JSON.stringify({schema:'navren-private-cache-provisional-v1',registered_user_id:i.registered_user_id,workspace_id:i.workspace_id,privacy_revision:0,generation,state:'quarantined'}));}},async f=>{await f.ready();assert.equal(f.posts().length,0,kind);assert.equal(f.w.Storage.get('qingzhou_bookmarks').length,0);assert.equal(f.w.Storage.get('qingzhou_preferences').weeklyReport,true);});}return{refused:['owner','workspace','privacy','quarantine'],implicit_imports:0};
 });
 await check('AS04','A/B与同A画像过滤；迟到GET/POST不填入新账号',async()=>{
  const s=model(),a=s.account('A');a.sections.preferences.weeklyReport=false;a.sections.favorites=[{category:'market_news',text:'公开收藏',object_id:'public-1'},{text:'A工作区一',workspace_id:a.workspaces.one,privacy_revision:0},{text:'A工作区二',workspace_id:a.workspaces.two,privacy_revision:0}];
  await using(s,'A',{},async f=>{await f.ready();assert.deepEqual(clone(f.w.Storage.get('qingzhou_bookmarks')).map(x=>x.text),['公开收藏','A工作区一']);await f.switch('A','two');assert.deepEqual(clone(f.w.Storage.get('qingzhou_bookmarks')).map(x=>x.text),['公开收藏','A工作区二']);assert.equal(f.w.Storage.get('qingzhou_preferences').weeklyReport,false);await f.switch('B');assert.equal(f.w.Storage.get('qingzhou_bookmarks').length,0);assert.equal(f.w.Storage.get('qingzhou_preferences').weeklyReport,true);});
  for(const method of ['GET','POST']){const service=model();service.account('B').sections.settings.theme='dark';await using(service,'A',{},async f=>{await f.ready();const hold=service.hold(f.client,method),pending=(method==='GET'?f.w.Storage.ready({refresh:true}):f.w.Storage.set('qingzhou_voiceEnabled',true)).then(value=>({ok:true,value}),error=>({ok:false,code:error.code}));await until(()=>hold.claimed&&hold.release,'A迟到'+method);await f.switch('B');assert.equal(f.w.Storage.get('navren_theme').id,'dark');assert.equal(f.w.Storage.get('qingzhou_voiceEnabled'),false);const events=f.events.length;hold.release();const outcome=await pending;assert.equal(outcome.ok,false);await tick();assert.equal(f.w.Storage.get('navren_theme').id,'dark');assert.equal(f.w.Storage.get('qingzhou_voiceEnabled'),false);assert.equal(f.events.length,events,'迟到响应不再发布当前状态');});}return{A_B_same_persona_isolated:true,same_A_settings_shared_private_favorites_filtered:true,late_GET_and_POST_rejected:true};
 });
 await check('AS07','真实双window CAS冲突与metadata触发GET同步全部设置及集合',async()=>{
  const conflict=model(),first=await fixture(conflict,'A','one',{signalMode:'none'}),second=await fixture(conflict,'A','one',{signalMode:'none'});
  try{await first.ready();await second.ready();await first.w.Storage.set('navren_theme',{id:'dark'});await assert.rejects(second.w.Storage.set('qingzhou_voiceEnabled',true),error=>error.code==='ACCOUNT_STATE_VERSION_CONFLICT');assert.equal(second.w.Storage.get('navren_theme').id,'dark');assert.equal(second.w.Storage.get('qingzhou_voiceEnabled'),false);assert.equal(second.posts().length,1);}finally{first.close();second.close();}
  const service=model(),one=await fixture(service,'A'),two=await fixture(service,'A');
  try{await one.ready();await two.ready();const before=two.reads().length;
   await one.w.Storage.setMany({navren_theme:{id:'dark'},navren_language:'en',qingzhou_mode:'youth',qingzhou_fontSize:18,qingzhou_modeFontSizes:{classic:17,youth:18},qingzhou_voiceEnabled:true,qingzhou_voicePreset:'male_warm'});await until(()=>two.w.Storage.get('qingzhou_voicePreset')==='male_warm','第二window设置同步');
   assert.equal(two.w.Storage.get('navren_theme').id,'dark');assert.equal(two.w.Storage.get('navren_language'),'en');assert.equal(two.w.Storage.get('qingzhou_mode'),'youth');assert.equal(two.w.Storage.get('qingzhou_voiceEnabled'),true);assert.equal(two.w.Storage.get('qingzhou_fontSize'),18);
   const prefs=clone(one.w.Storage.get('qingzhou_preferences'));prefs.weeklyReport=false;await one.w.Storage.set('qingzhou_preferences',prefs);await until(()=>two.w.Storage.get('qingzhou_preferences')?.weeklyReport===false,'偏好同步');
   await one.w.Storage.set('qingzhou_bookmarks',[{category:'market_weekly',object_id:'sync-week',version:1,text:'服务端收藏'}]);await until(()=>two.w.Storage.get('qingzhou_bookmarks')?.[0]?.object_id==='sync-week','收藏同步');
   await one.w.Storage.set('navren_reading_history',[{kind:'weekly',id:'sync-read',version:1,source_scope:'public',label:'服务端阅读'}]);await until(()=>two.w.Storage.get('navren_reading_history')?.[0]?.id==='sync-read','阅读同步');
   assert(two.reads().length>before,'通知必须触发实际BankClient GET，不能直接采用metadata值');assert.equal(two.posts().length,0);const messages=[...service.storage].filter(([key])=>key.startsWith('navren:account-state:v1:')).map(([,value])=>JSON.parse(value));assert(messages.length);for(const value of messages)assert.deepEqual(Object.keys(value).sort(),['nonce','registered_user_id','revision']);
   return{real_windows:2,stale_CAS_rejected:true,metadata_only:true,real_followup_GETs:two.reads().length-before,synchronized:['theme','language','mode','fonts','voice','preferences','favorites','reading'],second_window_POSTs:0};
  }finally{one.close();two.close();}
 });
 await check('AS08','lease过期与注销撤销后confirmed读取清空',async()=>{
  for(const mode of ['expired','logout']){const s=model();await using(s,'A',{},async f=>{await f.ready();assert.equal(f.w.Storage.accountReady(),true);if(mode==='expired')f.client.clockMs=f.client.session.lease_expires_at*1000+1;else{f.client.authenticated=false;f.w.BankClient.beginAccountSwitch();f.w.dispatchEvent(new f.w.Event('navren:identity-revoked'));}assert.equal(f.w.Storage.get('navren_theme'),null);assert.equal(f.w.Storage.accountReady(),false);await assert.rejects(f.ready());assert.equal(f.w.Storage.get('qingzhou_preferences'),null);});}return{expired_lease_hidden:true,revoked_welcome_hidden:true,actual_guard:true,real_logout_HTTP_tested:false};
 });
}
(async()=>{await cases();await tick();const drift=files.filter(file=>hash(fs.readFileSync(path.join(root,file)))!==pinned[file]);const requested=[...selected];if(requested.length)assert.deepEqual(results.map(row=>row.id),requested);const report={task:'ACCOUNT-DATA-COMPLETE-20261017 isolated storage state consumers',status:results.length&&results.every(row=>row.passed)&&!drift.length&&!network.length&&!asyncErrors.length?'PASS_SCOPED_ACCOUNT_STATE_CONSUMER':'FAIL',cases:results,requested_cases:requested,source_sha256:pinned,source_drift:drift,contract_sha256:hash(fs.readFileSync(contractFile)),async_errors:asyncErrors,actual_network_attempts:network,boundary:{actual_modules:files,actual_storage_and_guard:true,synthesized:['shared in-memory section CAS HTTP service','verified welcome adapter','localStorage/BroadcastChannel platform delivery'],real_primary_HTTP_or_browser_acceptance:false,real_network_calls:0,DB_model_SMS_calls:0}};fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n',{flag:'wx'});if(report.status!=='PASS_SCOPED_ACCOUNT_STATE_CONSUMER')process.exitCode=1;})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
