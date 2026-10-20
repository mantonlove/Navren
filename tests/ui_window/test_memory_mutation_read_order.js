/* Actual mine module, real memory DTO shape, controlled old/new BankClient GET ordering. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM,VirtualConsole} = require('jsdom');
const project = process.env.NAVREN_PROJECT || path.resolve(__dirname,'../..');
const source = fs.readFileSync(process.env.NAVREN_MINE_SOURCE || path.join(project,'js/mine.js'),'utf8');
const evidence = JSON.parse(fs.readFileSync(process.env.NAVREN_MEMORY_DTO || path.join(project,'output/account-data-completion-20261017/memory-created-readback.json'),'utf8'));
const clone = value => value == null ? value : JSON.parse(JSON.stringify(value));
const dto = evidence.memories;
assert.ok(dto.items?.length && dto.workspace_id && Number.isInteger(dto.privacy_revision) && Number.isInteger(dto.personal_revision),'Keep the real readback DTO shape');
assert.ok(evidence.text.includes('记忆暂未取得'),'The source evidence must retain the actual stale-error DOM symptom');
const tick = () => new Promise(resolve => setImmediate(resolve));
const results = [];
async function settle(predicate,label) {
  for(let i=0;i<30;i++){if(predicate())return;await tick();}
  assert.fail(label);
}
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}

async function fixture(){
  const errors=[],virtualConsole=new VirtualConsole();virtualConsole.on('jsdomError',error=>errors.push(error));
  const dom=new JSDOM('<body><button id="memoryToggle"></button><p id="memoryStatus"></p><p id="mineConsentStatus"></p><div id="financialMemoryList"></div></body>',{url:'https://navren.test/mine.html?view=home',runScripts:'outside-only',virtualConsole});
  const w=dom.window;
  // Full source is evaluated unchanged after load so unrelated whole-page boot is not run.
  if(w.document.readyState!=='complete')await new Promise(resolve=>w.addEventListener('load',resolve,{once:true}));
  const state={identity:{workspace_id:dto.workspace_id,privacy_revision:dto.privacy_revision,session_generation:1},pending:[],calls:[],updates:0};
  w.BankClient={
    ensure:async()=>{state.calls.push('ensure');return clone(state.identity);},
    identity:()=>clone(state.identity),
    refreshIdentity:async()=>{state.calls.push('refreshIdentity');return clone(state.identity);},
    guard:()=>{const captured=clone(state.identity);return {valid:value=>Object.keys(captured).every(key=>captured[key]===state.identity[key])&&(!value||value.workspace_id===captured.workspace_id&&value.privacy_revision===captured.privacy_revision)};},
    get:async url=>{assert.equal(url,'/memories');state.calls.push('get');const wait=deferred();state.pending.push(wait);return wait.promise;},
  };
  w.fetch=()=>{throw Error('Real network forbidden');};
  w.addEventListener('navren:memory-updated',()=>state.updates++);
  w.eval(source);
  assert.equal(typeof w.acceptMemoryMutation,'function','Use the actual mutation confirmation function');
  assert.equal(typeof w.NavrenMineMemory?.load,'function','Use the actual memory read entry');
  const list=()=>w.document.getElementById('financialMemoryList');
  return {w,state,list,async begin(){const promise=w.NavrenMineMemory.load();await settle(()=>state.pending.length>0,'The actual memory GET must start');return {promise,request:state.pending.shift()};},close(){dom.window.close();assert.equal(errors.length,0,errors.map(error=>error.stack).join('\n'));}};
}
async function withFixture(run){const f=await fixture();try{await run(f);}finally{f.close();}}
async function check(name,run){try{await run();results.push({name,passed:true});}catch(error){results.push({name,passed:false,error:error.stack});throw error;}}
function assertConfirmed(f,value){
  assert.deepEqual(clone(f.w.NavrenMineMemory.state()),value);
  assert.ok(f.list().textContent.includes(value.items[0].value),'Confirmed memory must remain visible');
  assert.ok(!f.list().textContent.includes('记忆暂未取得'),'A stale error must not replace confirmed memory');
  assert.ok(![...f.list().querySelectorAll('button')].some(button=>button.textContent==='重新读取记忆'));
}
const updatedDTO=()=>{const value=clone(dto);value.revision=String(Number(value.revision)+1);value.personal_revision++;value.items[0].value+='（后续有效读取）';return value;};

(async()=>{
  for(const errorName of ['TimeoutError','AbortError'])await check('old-'+errorName+'-cannot-cover-confirmed-mutation',()=>withFixture(async f=>{
    const old=await f.begin();
    assert.equal(await f.w.acceptMemoryMutation(clone(dto)),true);
    assert.equal(f.state.calls.filter(call=>call==='refreshIdentity').length,1);
    assert.equal(f.state.updates,1);assertConfirmed(f,dto);
    const confirmedDOM=f.list().innerHTML,status=f.w.document.getElementById('memoryStatus').textContent;
    old.request.reject(Object.assign(new Error('旧读取的受控失败'),{name:errorName}));await old.promise;
    assertConfirmed(f,dto);assert.equal(f.list().innerHTML,confirmedDOM,'Late old catch must not change the confirmed DOM');
    assert.equal(f.w.document.getElementById('memoryStatus').textContent,status);
  }));
  await check('new-load-after-confirmed-mutation-still-renders',()=>withFixture(async f=>{
    assert.equal(await f.w.acceptMemoryMutation(clone(dto)),true);
    const next=await f.begin(),value=updatedDTO();next.request.resolve(value);await next.promise;
    assertConfirmed(f,value);assert.equal(f.state.updates,1,'A read must not masquerade as another mutation');
  }));
  await check('current-read-failure-keeps-error-and-working-retry',()=>withFixture(async f=>{
    assert.equal(await f.w.acceptMemoryMutation(clone(dto)),true);
    const current=await f.begin();current.request.reject(Object.assign(new Error('当前读取确实失败'),{name:'TimeoutError'}));await current.promise;
    assert.ok(f.list().textContent.includes('记忆暂未取得'));
    const retry=[...f.list().querySelectorAll('button')].find(button=>button.textContent==='重新读取记忆');assert.ok(retry,'Current failure must retain the retry action');
    assert.equal(f.w.document.getElementById('memoryStatus').textContent,'当前读取确实失败');
    assert.ok(f.w.document.getElementById('mineConsentStatus').textContent.includes('暂未取得'));
    const value=updatedDTO(),pending=retry.onclick();await settle(()=>f.state.pending.length>0,'Retry must issue a new GET');f.state.pending.shift().resolve(value);await pending;
    assertConfirmed(f,value);
  }));
  await check('invalidated-guard-with-unchanged-sequence-keeps-current-dom',()=>withFixture(async f=>{
    const old=await f.begin();
    // A new session/view takes the DOM without another memory load or mutation:
    // the memory sequence stays untouched, but the old identity guard is invalid.
    f.state.identity.session_generation++;
    f.list().textContent='当前会话的新页面内容';
    f.w.document.getElementById('memoryStatus').textContent='当前会话状态';
    const currentDOM=f.list().innerHTML;
    old.request.reject(Object.assign(new Error('旧会话读取失败'),{name:'AbortError'}));await old.promise;
    assert.equal(f.list().innerHTML,currentDOM,'An invalid old guard must not replace the current view');
    assert.equal(f.w.document.getElementById('memoryStatus').textContent,'当前会话状态');
    assert.equal(f.w.NavrenMineMemory.state().items.length,0);
  }));
  for(const field of ['workspace_id','privacy_revision'])await check('invalid-'+field+'-mutation-does-not-cancel-valid-load',()=>withFixture(async f=>{
    const valid=await f.begin(),invalid=clone(dto);invalid[field]=field==='workspace_id'?'f'.repeat(32):dto.privacy_revision+1;
    assert.equal(await f.w.acceptMemoryMutation(invalid),false);
    assert.equal(f.state.updates,0);assert.equal(f.w.NavrenMineMemory.state().items.length,0);
    assert.ok(f.list().textContent.includes('正在核对记忆来源'));
    valid.request.resolve(clone(dto));await valid.promise;assertConfirmed(f,dto);
  }));
})().then(()=>finish(true)).catch(error=>{console.error(error.stack||error);finish(false);process.exitCode=1;});
function finish(passed){
  const result={schema:'navren.memory-mutation-read-order/1',passed,results,actual_http_requests:0,browser_operations:0,scope:'Full actual mine source and real memory readback DTO; controlled BankClient promises; only the bounded memory read/mutation order path. Not backend/BankClient/browser acceptance.'};
  if(process.env.NAVREN_MEMORY_RESULT)fs.writeFileSync(process.env.NAVREN_MEMORY_RESULT,JSON.stringify(result,null,2)+'\n');
  console.log(JSON.stringify(result));
}
