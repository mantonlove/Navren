/* 四账户选择合同夹具。禁止网络，不替代基座实际接线或浏览器验收。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require('jsdom');
const source=fs.readFileSync('js/account_switcher.js','utf8');
const ids=['tycoon','grain_farmer','working_landlady','retired_entrepreneur'];
const tick=()=>new Promise(r=>setTimeout(r,5));
async function fixture(options={}){
  const dom=new JSDOM('<!doctype html><section id="root"></section><div class="chat-messages">旧资料</div><div class="chat-input-area"><textarea id="userInput"></textarea></div>',{url:'https://navren.test/chat.html',runScripts:'outside-only'}),w=dom.window;
  w.NavrenI18n={language:options.language||'zh-CN',t:s=>s};w.fetch=()=>{throw Error('forbidden_network');};w.eval(source);
  let current=ids[0],selects=0,reloads=0,quiesced=0;
  const identity=()=>({account_id:current,workspace_id:'w-'+current,privacy_revision:1});
  const directory=()=>({schema_version:1,data_scope:'navren_synthetic',workspace_binding:identity().workspace_id,privacy_revision:1,current_account_id:current,accounts:ids.map((id,i)=>({account_id:id,label:'账户 '+(i+1),source_version:'fixture-ten-year-v1'}))});
  const adapter={safe_switch:true,identity:async()=>identity(),catalogue:async()=>directory(),activity:()=>({input:true,operation:{unconfirmed:true}}),confirm:async()=>true,
    quiesce:()=>{quiesced++;w.dispatchEvent(new w.Event('navren:privacy-reset'));},reload:()=>{reloads++;},
    select:async(id,expected)=>{selects++;assert.equal(expected.expected_workspace_id,identity().workspace_id);assert.equal(expected.expected_privacy_revision,1);current=id;return identity();},...options.adapter};
  const controller=w.NavrenAccountSwitcher.mount(w.document.getElementById('root'),adapter);await tick();
  return {dom,w,controller,adapter,identity,directory,counts:()=>({selects,reloads,quiesced})};
}
(async()=>{
  const a=await fixture();assert.equal(a.w.document.querySelectorAll('[data-account-id]').length,4);
  assert.equal(await a.controller.choose(ids[1]),true);assert.deepEqual(a.counts(),{selects:1,reloads:1,quiesced:1});
  assert.equal(a.w.document.querySelector('[aria-current="true"]').dataset.accountId,ids[1]);
  assert.equal(a.w.document.querySelector('.chat-messages').inert,true,'新身份核实后，旧页面保持锁定直到重载');
  assert.ok(a.w.document.documentElement.classList.contains('navren-switch-pending'));
  assert.equal(a.w.localStorage.length,0,'不在本地存储伪造账户选择/业务成功');a.controller.destroy();a.dom.window.close();

  const b=await fixture({adapter:{confirm:async()=>false}});assert.equal(await b.controller.choose(ids[2]),false);assert.deepEqual(b.counts(),{selects:0,reloads:0,quiesced:0});assert.equal(b.w.document.querySelector('[aria-current="true"]').dataset.accountId,ids[0]);b.controller.destroy();b.dom.window.close();
  for(const state of [{sending:true},{operation:{busy:true}},{operation:{unknown:true}}]){const c=await fixture({adapter:{activity:()=>state}});assert.equal(await c.controller.choose(ids[3]),false);assert.equal(c.counts().selects,0);assert.ok(c.w.document.querySelector('[role="alert"]'));c.controller.destroy();c.dom.window.close();}

  const d=await fixture({language:'en'});assert.equal(d.w.document.querySelector('.account-switch-label').textContent,'Demo account');assert.equal(d.w.document.querySelector('.account-switch-source p').textContent,'Experience users are fictional. Account and financial data are simulated, and financial actions only affect the experience environment. Public information retains its stated sources.');assert.equal(d.w.document.querySelector('[aria-current="true"]').textContent,'账户 1','来源标签不冒充译后的原件');d.controller.destroy();d.dom.window.close();

  const e=await fixture({adapter:{select:async()=>{throw Error('unknown outcome');}}});assert.equal(await e.controller.choose(ids[1]),false);assert.equal(e.counts().reloads,0);assert.equal(e.w.document.querySelector('.chat-messages').inert,true);e.w.document.querySelector('button').click();await tick();assert.equal(e.counts().reloads,1,'失败后身份复验通过也必须重载，不能暴露旧界面');assert.equal(e.w.document.querySelector('.chat-messages').inert,true);e.controller.destroy();e.dom.window.close();

  let release;const pending=new Promise(r=>release=r);const f=await fixture({adapter:{select:async()=>pending}});const first=f.controller.choose(ids[1]);await tick();assert.equal(await f.controller.choose(ids[2]),false,'切换中不发第二个选择');f.controller.destroy();release({account_id:ids[1],workspace_id:'wrong',privacy_revision:1});assert.equal(await first,false,'卸载后的迟到结果不更新或重载');assert.equal(f.counts().reloads,0);f.dom.window.close();

  const g=await fixture({adapter:{catalogue:async()=>({schema_version:1,data_scope:'navren_synthetic',accounts:[]})}});assert.equal(g.w.document.querySelectorAll('[data-account-id]').length,0);assert.ok(g.w.document.querySelector('[role="alert"]'));g.controller.destroy();g.dom.window.close();
  const blank=new JSDOM('<section id="root"></section>',{url:'https://navren.test',runScripts:'outside-only'});blank.window.eval(source);const blankRoot=blank.window.document.getElementById('root'),unavailable=blank.window.NavrenAccountSwitcher.mount(blankRoot);assert.equal(blankRoot.hidden,true,'未接通目录不展示空账户提示栏');assert.equal(blankRoot.textContent,'');assert.equal(blank.window.document.querySelectorAll('[data-account-id]').length,0);assert.equal(await unavailable.setAdapter({...a.adapter,identity:async()=>a.identity(),catalogue:async()=>a.directory()}),true);assert.equal(blankRoot.hidden,false,'目录接通后仍保留真实选择功能');assert.equal(blankRoot.querySelectorAll('[data-account-id]').length,4);unavailable.destroy();blank.window.close();
  console.log('通过：四账户目录、明确取消、处理中/未知操作阻断、私有区锁定与重载、失败身份复验、并发与迟到结果、双语与来源原件、目录缺失；固定合同夹具，无网络。');
})().catch(e=>{console.error(e);process.exitCode=1;});
