/* Actual HTML + mine + workspace + view_context event-chain regression; no real network. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM,VirtualConsole}=require('jsdom');
const repo=process.env.NAVREN_PROJECT||path.resolve(__dirname,'../..');
const input=(key,fallback)=>fs.readFileSync(process.env[key]||path.join(repo,fallback),'utf8');
const sources={mine:input('NAVREN_MINE_SOURCE','js/mine.js'),workspace:input('NAVREN_MINE_WORKSPACE_SOURCE','js/mine_workspace.js'),views:input('NAVREN_VIEW_SOURCE','js/view_context.js'),lifecycle:input('NAVREN_LIFECYCLE_SOURCE','js/lifecycle.js')};
const html=input('NAVREN_MINE_HTML','mine.html');
const evidence=JSON.parse(input('NAVREN_LIFECYCLE_EVIDENCE','outputs/window-progress/ui/account-data-completion-20261017/browser-memory-lifecycle/before-ui.json'));
assert.equal(evidence.post_status,200);assert.ok(evidence.trace.filter(row=>row.type==='navren:privacy-reset').length>=2);assert.ok(evidence.dom.includes('记忆暂未取得'));
const clone=value=>value==null?value:JSON.parse(JSON.stringify(value));
const finalDTO=clone(evidence.state),priorDTO=clone(finalDTO);
const installed=evidence.trace.find(row=>row.type==='installed');
priorDTO.revision=String(installed.state_revision);priorDTO.personal_revision=Number(installed.state_revision);priorDTO.privacy_revision=installed.privacy;
priorDTO.items[0].value=finalDTO.items[0].value.replace('四万元','三万元');
assert.notEqual(priorDTO.items[0].value,finalDTO.items[0].value);assert.ok(installed.text.includes(priorDTO.items[0].value));
const results=[],observations=[];
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const wait=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
async function until(predicate,label){for(let i=0;i<80;i++){if(predicate())return;await delay(1);}assert.fail(label);}
async function quiet(){for(let i=0;i<6;i++)await delay(2);}

async function fixture(){
  const errors=[],vc=new VirtualConsole();vc.on('jsdomError',error=>errors.push(error));
  // Use the real page markup. Product scripts are evaluated explicitly below;
  // no external scripts, styles, browser, or network are loaded by JSDOM.
  const dom=new JSDOM(html,{url:'https://navren.test/mine.html?view=home',runScripts:'outside-only',pretendToBeVisual:true,virtualConsole:vc});
  const w=dom.window,d=w.document;
  if(d.readyState!=='complete')await new Promise(resolve=>w.addEventListener('load',resolve,{once:true}));
  const now=Date.now()/1000;
  const state={identity:{registered_user_id:'a'.repeat(32),workspace_id:priorDTO.workspace_id,privacy_revision:priorDTO.privacy_revision,auth_subject:'registered-owner-a',session_generation:1,csrf:'fixture-csrf',auth_expires_at:now+600,lease_expires_at:now+300,account_id:'fixture-persona'},memory:clone(priorDTO),holdMemory:false,holdRefresh:false,pendingMemory:[],pendingRefresh:[],calls:[],events:[],notices:[],posts:[],memoryUpdates:0};
  const identity=()=>clone(state.identity),list=()=>d.getElementById('financialMemoryList');
  const event=(name,reason)=>w.dispatchEvent(new w.CustomEvent(name,{detail:reason?{reason}:undefined}));
  const snapshot=()=>({view:w.NavrenViews?.revision?.(),memory_reads:state.calls.filter(row=>row.path==='/memories').length,text:list().textContent,memory_revision:w.NavrenMineMemory?.state().revision});
  for(const name of ['navren:privacy-reset','navren:memory-updated','navren:view'])w.addEventListener(name,e=>{state.events.push({name,reason:e.detail?.reason,...snapshot()});if(name==='navren:memory-updated')state.memoryUpdates++;});
  w.BankClient={
    identity,ensure:async()=>identity(),
    refreshIdentity:async()=>{state.calls.push({method:'REFRESH'});if(state.holdRefresh){const row=wait();state.pendingRefresh.push(row);await row.promise;}return identity();},
    guard:()=>{const captured=identity();return {valid:value=>Object.keys(captured).every(key=>captured[key]===state.identity[key])&&Date.now()/1000<state.identity.lease_expires_at&&(!value||value.workspace_id===captured.workspace_id&&value.privacy_revision===captured.privacy_revision)};},
    get:async path=>{
      state.calls.push({method:'GET',path,...snapshot()});
      if(path==='/memories'){
        if(state.holdMemory){const row={...wait(),captured:identity()};state.pendingMemory.push(row);return row.promise;}
        return clone(state.memory);
      }
      const base={workspace_id:state.identity.workspace_id,privacy_revision:state.identity.privacy_revision,personal_revision:state.memory.personal_revision,as_of:state.memory.as_of};
      if(path==='/overview')return {...base,profile:{risk_level:2}};
      if(path==='/personal-graph?view=overview&source=simulation&portfolio=current')return {...base,nodes:[],insights:[]};
      if(path==='/personal-graph?view=preferences&source=simulation&portfolio=current')return {...base,nodes:[],insights:[]};
      throw Error('Unexpected controlled GET '+path);
    },
    post:async(path,payload)=>{
      const prefix='/memories/'+priorDTO.items[0].node_id;
      assert.ok([prefix+'/correct',prefix+'/delete'].includes(path));
      assert.equal(payload.expected_revision,priorDTO.personal_revision);
      if(path.endsWith('/correct'))assert.equal(payload.value,finalDTO.items[0].value);
      state.posts.push({path,payload:clone(payload)});state.calls.push({method:'POST',path});
      state.memory=clone(finalDTO);if(path.endsWith('/delete'))state.memory.items=[];
      // Reproduce the observed formal client event ordering: the first privacy
      // signal precedes identity refresh, then the new privacy identity is known.
      event('navren:privacy-reset','privacy_changed');
      state.identity.privacy_revision=finalDTO.privacy_revision;
      event('navren:privacy-reset','private_data_changed');
      return clone(state.memory);
    },
  };
  const prefs={navren_theme:{id:'light'},qingzhou_mode:'classic',qingzhou_fontSize:16,qingzhou_preferences:{},qingzhou_bookmarks:[]};
  w.Storage={accountReady:()=>true,accountStatus:()=> 'ready',ready:async()=>({}),get:key=>clone(prefs[key]??null),getProfileHistory:()=>[]};
  Object.assign(w,{getInterfaceMode:()=> 'classic',getModeFont:()=>16,applyFontSize:()=>{},fontSizeInPixels:value=>Number(value),FONT_SIZE_MIN:14,FONT_SIZE_MAX:24,showContextualTip:async()=>{},showToast:text=>state.notices.push(text)});
  w.NavrenDesign={theme:{id:'light'},mountSettings(){}};
  w.NavrenGraph={pause(){},invalidate(){}};
  w.NavrenFinancialData={scope:'financial_history',overview:async()=>({data:{accounts:[],cards:[],policies:[]},evidence:{coverage_start:'2026-01-01'},as_of:state.memory.as_of}),clear(){}};
  w.NavrenPersonalProfile={renderSummary(){}};
  w.NavrenApp={href:value=>{const u=new URL(value,w.location.href);return u.pathname+u.search+u.hash;},navigate:()=>{throw Error('Unexpected cross-page navigation');}};
  w.fetch=()=>{throw Error('Actual HTTP forbidden');};w.confirm=()=>true;
  // Product order: view_context, mine, workspace, then lifecycle boot listener.
  w.eval(sources.views);w.eval(sources.mine);w.eval(sources.workspace);w.eval(sources.lifecycle);
  d.dispatchEvent(new w.Event('DOMContentLoaded'));
  await until(()=>list().textContent.includes(priorDTO.items[0].value),'Actual startup/render/loadProfile must show the initial confirmed memory');
  await quiet();assert.equal(errors.length,0,errors.map(error=>error.stack).join('\n'));
  return {w,d,state,list,event,snapshot,
    memoryReads:()=>state.calls.filter(row=>row.path==='/memories').length,
    fullRenders:()=>state.calls.filter(row=>row.path==='/personal-graph?view=overview&source=simulation&portfolio=current').length,
    async close(){for(const row of state.pendingMemory.splice(0))row.reject(Object.assign(new Error('fixture cleanup'),{name:'AbortError'}));for(const row of state.pendingRefresh.splice(0))row.resolve();w.dispatchEvent(new w.Event('pagehide'));await quiet();dom.window.close();assert.equal(errors.length,0,errors.map(error=>error.stack).join('\n'));},
  };
}
async function withFixture(run){const f=await fixture();try{await run(f);}finally{await f.close();}}
async function check(name,run){try{await run();results.push({name,passed:true});}catch(error){results.push({name,passed:false,error:error.stack});throw error;}}
function confirmed(f,value=finalDTO.items[0].value){assert.ok(f.list().textContent.includes(value),'Confirmed UI value must remain visible after lifecycle recovery');assert.ok(!f.list().textContent.includes('记忆暂未取得'));}

(async()=>{
  await check('actual-editor-two-privacy-events-and-restoration-preserve-confirmed-dom',()=>withFixture(async f=>{
    const edit=[...f.list().querySelectorAll('button')].find(b=>b.textContent==='修改');assert.ok(edit);edit.click();
    const field=f.d.getElementById('memoryValue');assert.ok(field);assert.equal(field.value,priorDTO.items[0].value);
    // An older explicit read remains pending while the actual editor POST completes.
    f.state.holdMemory=true;const older=f.w.NavrenMineMemory.load();await until(()=>f.state.pendingMemory.length===1,'Old memory GET must be in flight');
    field.value=finalDTO.items[0].value;field.dispatchEvent(new f.w.Event('input',{bubbles:true}));
    const before={reads:f.memoryReads(),renders:f.fullRenders(),view:f.w.NavrenViews.revision(),events:f.state.events.length};
    f.d.getElementById('modalConfirm').click();
    await until(()=>f.state.memoryUpdates===1,'UI confirmation must emit the actual memory-updated event');await quiet();
    const restored={...f.snapshot(),renders:f.fullRenders()-before.renders,reads:f.memoryReads()-before.reads};
    const pending=f.state.pendingMemory.splice(0);
    for(const [index,row]of pending.entries())row.reject(Object.assign(new Error('排队中的旧 GET 失败'),{name:index%2?'TimeoutError':'AbortError'}));
    await older;await quiet();
    observations.push({case:'editor-lifecycle',before,after_restore:restored,final:f.snapshot(),events:f.state.events.slice(before.events),post:f.state.posts[0],rejected_gets:pending.length});
    confirmed(f);assert.equal(f.w.NavrenMineMemory.state().items[0].value,finalDTO.items[0].value);
    assert.equal(f.state.posts.length,1);assert.equal(f.state.events.slice(before.events).filter(row=>row.name==='navren:privacy-reset').length,2);
    assert.equal(restored.renders,1,'The two privacy events and memory event must coalesce into one actual full render');
    assert.equal(restored.reads,0,'A valid confirmed mutation snapshot must avoid duplicate memory GETs during workspace render');
    assert.equal(f.w.NavrenViews.revision()-before.view,3,'Two privacy invalidations and one restored view must be retained');
  }));
  await check('actual-delete-empty-confirmed-snapshot-is-reused',()=>withFixture(async f=>{
    const remove=[...f.list().querySelectorAll('button')].find(b=>b.textContent==='删除');assert.ok(remove);
    f.state.holdMemory=true;const reads=f.memoryReads(),renders=f.fullRenders();remove.click();
    await until(()=>f.state.memoryUpdates===1,'Actual delete UI must confirm and emit memory-updated');await quiet();
    observations.push({case:'delete-lifecycle',reads:f.memoryReads()-reads,renders:f.fullRenders()-renders,final:f.snapshot(),post:f.state.posts[0]});
    assert.equal(f.w.NavrenMineMemory.state().items.length,0);assert.equal(f.state.pendingMemory.length,0);
    assert.equal(f.memoryReads(),reads,'A verified empty snapshot is still confirmed and must not trigger duplicate GET');
    assert.equal(f.fullRenders()-renders,1);assert.ok(f.list().textContent.includes('暂无已保存记忆'));assert.ok(!f.list().textContent.includes('记忆暂未取得'));
  }));
  await check('explicit-read-current-failure-and-workspace-retry-are-not-hidden',()=>withFixture(async f=>{
    f.state.holdMemory=true;const first=f.w.NavrenMineMemory.load();await until(()=>f.state.pendingMemory.length===1,'Explicit read must issue GET despite confirmed snapshot');f.state.pendingMemory.shift().reject(Error('当前有效读取失败'));await first;
    assert.ok(f.list().textContent.includes('记忆暂未取得'));assert.equal(f.d.getElementById('memoryStatus').textContent,'当前有效读取失败');
    const reopened=f.w.NavrenViews.open('home',{}, {replace:true});await until(()=>f.state.pendingMemory.length===1,'After current failure, workspace reuse must issue a genuine GET');f.state.pendingMemory.shift().reject(Error('当前再次读取失败'));await reopened;
    const retry=[...f.list().querySelectorAll('button')].find(b=>b.textContent==='重新读取记忆');assert.ok(retry);retry.click();await until(()=>f.state.pendingMemory.length===1,'Actual retry button must issue a new GET');f.state.pendingMemory.shift().resolve(clone(priorDTO));await quiet();confirmed(f,priorDTO.items[0].value);
    const reads=f.memoryReads();await f.w.NavrenViews.open('home',{}, {replace:true});assert.equal(f.memoryReads(),reads,'Newly verified snapshot may again be reused only for workspace display');
  }));
  await check('confirmed-snapshot-requires-every-exact-binding-field-and-live-lease',()=>withFixture(async f=>{
    const base=clone(f.state.identity);f.state.holdMemory=true;
    const changes={registered_user_id:'b'.repeat(32),workspace_id:'c'.repeat(32),privacy_revision:base.privacy_revision+1,auth_subject:'other-subject',session_generation:base.session_generation+1,csrf:'other-csrf',auth_expires_at:base.auth_expires_at+60,lease_expires_at:base.lease_expires_at+60};
    for(const [field,value]of Object.entries(changes)){
      f.state.identity={...base,[field]:value};const reads=f.memoryReads();const render=f.w.NavrenViews.open('home',{}, {replace:true});
      await until(()=>f.state.pendingMemory.length===1,'Changed '+field+' must not reuse another binding');assert.equal(f.memoryReads(),reads+1);assert.ok(!f.list().textContent.includes(priorDTO.items[0].value));
      f.state.pendingMemory.shift().reject(Error('Controlled binding rejection'));await render;
    }
    f.state.identity={...base,lease_expires_at:Date.now()/1000-1};const expired=f.w.NavrenViews.open('home',{}, {replace:true});await until(()=>f.state.pendingMemory.length===1,'Expired lease must not reuse snapshot');assert.ok(!f.list().textContent.includes(priorDTO.items[0].value));f.state.pendingMemory.shift().reject(Error('Expired lease'));await expired;
  }));
  for(const kind of ['privacy','owner'])await check(kind+'-reset-clears-old-confirmed-display',()=>withFixture(async f=>{
    f.state.holdMemory=true;
    if(kind==='privacy')f.state.identity.privacy_revision++;
    else Object.assign(f.state.identity,{registered_user_id:'b'.repeat(32),workspace_id:'d'.repeat(32),auth_subject:'new-owner',csrf:'new-owner-csrf'});
    f.event('navren:privacy-reset',kind+'_changed');
    assert.equal(f.w.NavrenMineMemory.state().items.length,0);assert.ok(!f.list().textContent.includes(priorDTO.items[0].value));
    await until(()=>f.state.pendingMemory.length===1,'Reset must restore through a genuine memory GET');
    const empty={...clone(priorDTO),workspace_id:f.state.identity.workspace_id,privacy_revision:f.state.identity.privacy_revision,items:[]};f.state.memory=clone(empty);f.state.pendingMemory.shift().resolve(empty);await quiet();
    assert.equal(f.w.NavrenMineMemory.state().items.length,0);assert.ok(!f.list().textContent.includes(priorDTO.items[0].value));assert.ok(f.list().textContent.includes('暂无已保存记忆'));
  }));
  await check('superseded-async-identity-refresh-cannot-open-an-extra-view',()=>withFixture(async f=>{
    f.state.holdRefresh=true;const generation=f.w.NavrenViews.revision();
    f.event('navren:memory-updated');await until(()=>f.state.pendingRefresh.length===1,'First real restore timer must await identity');
    f.event('navren:memory-updated');await until(()=>f.state.pendingRefresh.length===2,'A newer restore timer must await identity');
    f.state.pendingRefresh.shift().resolve();await quiet();assert.equal(f.w.NavrenViews.revision(),generation,'A stale restore token cannot open a view after its identity resolves');
    f.state.holdRefresh=false;f.state.pendingRefresh.shift().resolve();await quiet();assert.equal(f.w.NavrenViews.revision(),generation+1);confirmed(f,priorDTO.items[0].value);
  }));
})().then(()=>finish(true)).catch(error=>{console.error(error.stack||error);finish(false);process.exitCode=1;});
function finish(passed){const packet={schema:'navren.memory-workspace-lifecycle/1',passed,results,observations,actual_http_requests:0,browser_operations:0,scope:'Actual mine.html DOM, full mine/workspace/view_context/lifecycle source, native 0ms timers and UI editing; controlled BankClient/other read-only widgets. Not backend or actual Chrome acceptance.'};if(process.env.NAVREN_LIFECYCLE_RESULT)fs.writeFileSync(process.env.NAVREN_LIFECYCLE_RESULT,JSON.stringify(packet,null,2)+'\n');console.log(JSON.stringify(packet));}
