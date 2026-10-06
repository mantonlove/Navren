/* 实际 selector + 基座适配器 + BankClient 的隔离链；fetch 只接受基座已定义路由。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),crypto=require('node:crypto'),{JSDOM}=require('jsdom');
const files=['js/bank_client.js','js/account_switcher.js','js/base_account_adapter.js'];
const source=Object.fromEntries(files.map(p=>[p,fs.readFileSync(p,'utf8')]));
const profiles=JSON.parse(fs.readFileSync('outputs/contest-finances/contest_profiles.json','utf8'));
const ids=profiles.profiles.map(p=>p.id),sourceVersion=crypto.createHash('sha256').update(JSON.stringify(profiles)).digest('hex');
const namespace=Buffer.from('baee3c7d91d55b79939b26cc0e86481f','hex');
function workspace(id){const b=crypto.createHash('sha1').update(namespace).update('navren:contest:'+id).digest().subarray(0,16);b[6]=(b[6]&15)|80;b[8]=(b[8]&63)|128;return b.toString('hex');}
const tick=()=>new Promise(r=>setTimeout(r,5));
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {resolve,promise};};
function identity(id,rotation=0){return {workspace_id:id?workspace(id):'legacy-workspace',privacy_revision:0,account_id:id||null,csrf:'fixture-csrf-'+(id||'legacy')+'-'+rotation,demo_identity:id?{user_id:id}:null};}
function response(data,status=200,binding=data){return {ok:status<400,status,headers:{get:k=>k==='X-Navren-Workspace'?binding.workspace_id:k==='X-Navren-Privacy-Revision'?String(binding.privacy_revision):null},json:async()=>JSON.parse(JSON.stringify(data))};}
async function setup({initial=null}={}){
  const dom=new JSDOM('<section id="root"></section><div class="chat-messages">旧私有资料</div><div class="page-scroll">旧账户详情</div><div class="chat-input-area"><textarea id="userInput"></textarea></div>',{url:'https://navren.test/chat.html',runScripts:'outside-only'}),w=dom.window,requests=[],channels=[],events=[];
  let cookie=identity(initial),rotation=0,reloads=0,privateOverride=null,sessionOverride=null,selectOverride=null,importOverride=null;
  w.BroadcastChannel=class{constructor(name){this.name=name;channels.push(this);}postMessage(data){events.push({type:'broadcast',data});}close(){}};
  w.NavrenI18n={language:'zh-CN',t:s=>s};w.NavrenViews={revision:()=>0};w.NavrenSpeech={stop(){events.push({type:'speech-stopped'});}};
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
  w.addEventListener('navren:privacy-reset',()=>events.push({type:'privacy-reset'}));w.addEventListener('navren:identity-revoked',()=>events.push({type:'identity-revoked'}));w.addEventListener('navren:account-switched',e=>events.push({type:'switched',detail:e.detail}));
  w.fetch=async(path,options={})=>{
    assert.equal(typeof path,'string');assert(path.startsWith('/api/finance/'),'不允许真实网络或自定API');assert.equal(options.credentials,'same-origin');assert.equal(options.cache,'no-store');const method=options.method||'GET',current={...cookie};requests.push({path,method,options,workspace:current.workspace_id});
    if(path==='/api/finance/session'){if(sessionOverride)return sessionOverride({method,current,options});if(method==='POST')assert.deepEqual(JSON.parse(options.body),{});return response(current);}
    if(path==='/api/finance/demo/accounts'&&method==='GET')return response({environment:'simulation',source:'navren_synthetic',schema_version:1,data_scope:'navren_synthetic',workspace_binding:current.workspace_id,privacy_revision:current.privacy_revision,current_account_id:current.account_id,initialized_count:4,import_version:'four-profiles-v1',is_real_bank_data:false,accounts:profiles.profiles.map(p=>({user_id:p.id,display_name:p.display_name,workspace_id:workspace(p.id),as_of:profiles.end_date,source:'navren_synthetic',source_version:sourceVersion,coverage_start:profiles.start_date,coverage_end:profiles.end_date}))},200,current);
    if(path==='/api/finance/demo/select'&&method==='POST'){
      const body=JSON.parse(options.body);assert.deepEqual(Object.keys(body).sort(),['expected_privacy_revision','expected_workspace_id','user_id']);assert(ids.includes(body.user_id));assert.equal(options.headers['X-QZ-CSRF'],current.csrf);
      if(selectOverride)return selectOverride({body,current,options});
      if(body.expected_workspace_id!==current.workspace_id||body.expected_privacy_revision!==current.privacy_revision)return response({error:{code:'PRIVACY_REVISION_CHANGED',message:'权限已变化'}},409,current);
      cookie=identity(body.user_id,++rotation);return response(cookie);
    }
    if(path==='/api/finance/overview'&&method==='GET')return privateOverride?privateOverride({current,options}):response({...current,fixture_private_for:current.account_id||'legacy'});
    if(path==='/api/finance/calendar/import/preview'&&method==='POST')return importOverride?importOverride({current,options}):response({...current,preview_id:'fixture-preview'});
    if(path==='/api/finance/goals/prepare'&&method==='POST'){
      if(options.headers['X-QZ-CSRF']!==current.csrf)return response({error:{code:'CSRF_DENIED',message:'CSRF拒绝'}},403,current);
      return response({...current,draft_id:'fixture-draft'});
    }
    throw Error('未登记模拟路由：'+method+' '+path);
  };
  for(const p of files)w.eval(source[p]);await tick();
  const adapter=w.NavrenBaseAccountAdapter;adapter.confirm=async()=>true;adapter.reload=()=>{reloads++;};adapter.activity=()=>({});
  const root=w.document.getElementById('root'),controller=w.NavrenAccountSwitcher.mount(root,adapter);await tick();
  return {dom,w,root,controller,adapter,requests,events,channels,cookie:()=>({...cookie}),setCookie:id=>{cookie=identity(id,++rotation);},counts:()=>({reloads}),setPrivate:fn=>privateOverride=fn,setSession:fn=>sessionOverride=fn,setSelect:fn=>selectOverride=fn,setImport:fn=>importOverride=fn,close(){controller.destroy();w.close();}};
}
const results=[];
async function test(name,fn){try{await fn();results.push({name,passed:true});console.log('通过：'+name);}catch(e){results.push({name,passed:false,error:e.message});console.error('失败：'+name+'\n'+e.stack);}}
async function withEnv(options,fn){const e=await setup(options);try{await fn(e);}finally{e.close();}}
(async()=>{
  await test('legacy Cookie可显示实际四账户目录且可进入固定画像',()=>withEnv({},async e=>{
    assert.equal(e.root.querySelectorAll('[data-account-id]').length,4);assert.equal(e.w.BankClient.identity().account_id,null);assert.deepEqual([...e.root.querySelectorAll('[data-account-id]')].map(n=>n.textContent),profiles.profiles.map(p=>p.display_name));assert.equal(await e.controller.choose(ids[1]),true);assert.equal(e.w.BankClient.identity().workspace_id,workspace(ids[1]));assert.equal(e.cookie().account_id,ids[1]);assert.equal(e.counts().reloads,1);assert(e.w.document.documentElement.classList.contains('navren-private-locked'));
  }));
  await test('选择请求仅传user_id与原身份，Cookie复验后才发送切换事件',()=>withEnv({initial:ids[0]},async e=>{
    const before=e.w.BankClient.identity();assert.equal(await e.controller.choose(ids[2]),true);const selected=e.requests.find(r=>r.path==='/api/finance/demo/select');const body=JSON.parse(selected.options.body);assert.equal(body.user_id,ids[2]);assert.equal(body.expected_workspace_id,before.workspace_id);assert.equal(body.expected_privacy_revision,before.privacy_revision);assert.equal(selected.options.headers['X-QZ-CSRF'],before.csrf);const sequence=e.requests.slice(e.requests.indexOf(selected)+1).map(r=>r.path);assert.deepEqual(sequence,['/api/finance/session','/api/finance/demo/accounts']);assert.equal(e.events.filter(x=>x.type==='switched').length,1);
  }));
  await test('切换前旧GET实际中止，忽略abort的迟到响应仍被拒绝',()=>withEnv({initial:ids[0]},async e=>{
    const d=deferred();let old;e.setPrivate(({current,options})=>{old={current,options};return d.promise;});const p=e.w.BankClient.get('/overview').catch(x=>x);await tick();assert.equal(await e.controller.choose(ids[1]),true);assert.equal(old.options.signal.aborted,true);d.resolve(response({...old.current,fixture_private_for:'OLD'},200,old.current));const result=await p;assert.equal(result.code,'STALE_RESPONSE');assert.equal(e.w.BankClient.identity().account_id,ids[1]);
  }));
  await test('切换后的私有GET与POST使用Cookie新工作区及新CSRF',()=>withEnv({initial:ids[0]},async e=>{
    await e.controller.choose(ids[3]);const next=e.cookie();const data=await e.w.BankClient.get('/overview');assert.equal(data.fixture_private_for,ids[3]);assert.equal(data.workspace_id,next.workspace_id);await e.w.BankClient.post('/goals/prepare',{title:'测试目标'});const post=e.requests.at(-1);assert.equal(post.workspace,next.workspace_id);assert.equal(post.options.headers['X-QZ-CSRF'],next.csrf);assert(!Object.keys(post.options.headers).some(k=>/workspace|account/i.test(k)),'工作区不通过可伪造请求头授权');
  }));
  await test('错误工作区的私有响应使身份失效而不返回内容',()=>withEnv({initial:ids[0]},async e=>{
    e.setPrivate(()=>response({secret:'other'},200,identity(ids[1])));await assert.rejects(e.w.BankClient.get('/overview'),x=>x.code==='STALE_IDENTITY');assert.equal(e.w.BankClient.identity(),null);assert(e.events.some(x=>x.type==='privacy-reset'));
  }));
  await test('数组私有响应缺身份头时拒绝展示而非继承当前账户',()=>withEnv({initial:ids[0]},async e=>{
    e.setPrivate(()=>({ok:true,status:200,headers:{get:()=>null},json:async()=>[{fixture_private:'unbound'}]}));await assert.rejects(e.w.BankClient.get('/overview'),x=>x.code==='UNBOUND_RESPONSE');
  }));
  await test('CSRF拒绝响应清除会话，下一次身份须走Cookie核对',()=>withEnv({initial:ids[0]},async e=>{
    const before=e.w.BankClient.identity();e.setCookie(ids[1]);await assert.rejects(e.w.BankClient.post('/goals/prepare',{title:'测试'}),x=>x.code==='CSRF_DENIED');assert.equal(e.w.BankClient.identity(),null);assert.equal((await e.w.BankClient.ensure()).account_id,ids[1]);assert.notEqual(e.w.BankClient.identity().csrf,before.csrf);
  }));
  await test('切换使旧组件身份guard失效且不将CSRF保存为浏览器状态',()=>withEnv({initial:ids[0]},async e=>{
    const guard=e.w.BankClient.guard();assert(guard.valid());await e.controller.choose(ids[1]);assert.equal(guard.valid(),false);const storage=[...Array(e.w.localStorage.length).keys()].map(i=>[e.w.localStorage.key(i),e.w.localStorage.getItem(e.w.localStorage.key(i))]);assert(storage.every(([k,v])=>!k.includes('csrf')&&!v.includes('fixture-csrf')));assert.equal(e.w.sessionStorage.getItem('account_id'),null);
  }));
  await test('选择结果错误账户必须维持锁定且禁止成功事件/重载',()=>withEnv({initial:ids[0]},async e=>{
    e.setSelect(()=>response(identity(ids[3])));assert.equal(await e.controller.choose(ids[1]),false);assert.equal(e.counts().reloads,0);assert.equal(e.events.filter(x=>x.type==='switched').length,0);assert(e.w.document.documentElement.classList.contains('navren-private-locked'));assert(e.root.querySelector('[role="alert"]'));
  }));
  await test('目录Cookie返回不一致不能假称切换成功',()=>withEnv({initial:ids[0]},async e=>{
    e.setSelect(({body})=>response(identity(body.user_id,3)));assert.equal(await e.controller.choose(ids[1]),false);assert.equal(e.counts().reloads,0);assert(e.root.querySelector('[role="alert"]'));
  }));
  await test('切换失败后重新核对真实Cookie，旧UI保持锁定至重载',()=>withEnv({initial:ids[0]},async e=>{
    e.setSelect(({current})=>response({error:{code:'PRIVACY_REVISION_CHANGED',message:'权限已变化'}},409,current));assert.equal(await e.controller.choose(ids[1]),false);assert(e.w.document.querySelector('.chat-messages').inert);[...e.root.querySelectorAll('button')].find(b=>b.textContent==='重试').click();await tick();assert.equal(e.counts().reloads,1);assert.equal(e.w.BankClient.identity().account_id,ids[0]);
  }));
  await test('跨标签撤销清空BankClient身份与本地聊天历史',()=>withEnv({initial:ids[0]},async e=>{
    e.w.localStorage.setItem('qingzhou_chatHistory','OLD');e.w.localStorage.setItem('qingzhou_chatKeyMoments','OLD');e.channels[0].onmessage({data:{type:'invalidate'}});assert.equal(e.w.BankClient.identity(),null);assert.equal(e.w.localStorage.getItem('qingzhou_chatHistory'),null);assert(e.events.some(x=>x.type==='identity-revoked'));
  }));
  await test('跨标签identity-revoked必须锁定私有区并撤下旧账户选择',()=>withEnv({initial:ids[0]},async e=>{
    e.setCookie(ids[1]);e.channels[0].onmessage({data:{type:'invalidate'}});await tick();assert(e.w.document.documentElement.classList.contains('navren-private-locked'),'跨标签已撤销身份后页面必须锁定');assert.equal(e.w.document.querySelector('.chat-messages').inert,true);assert.equal(e.root.querySelectorAll('[data-account-id]').length,0,'旧目录选择不能继续用旧身份');
  }));
  await test('切换中迟到的POST/session不能覆盖已经接受的新身份',()=>withEnv({initial:ids[0]},async e=>{
    const old={...e.cookie()},d=deferred();e.w.BankClient.beginAccountSwitch();e.setSession(({method,current})=>method==='POST'?d.promise:response(current));const boot=e.w.BankClient.ensure().catch(error=>error);e.setCookie(ids[1]);e.w.BankClient.acceptSelectedSession(e.cookie());d.resolve(response(old));const outcome=await boot;if(outcome instanceof e.w.Error)assert(['STALE_RESPONSE','STALE_IDENTITY'].includes(outcome.code));assert.equal(e.w.BankClient.identity().account_id,ids[1],'迟到会话初始化不得回写旧账户');
  }));
  await test('旧refreshIdentity成功响应被代次拒绝并保持新身份',()=>withEnv({initial:ids[0]},async e=>{
    const old=e.cookie(),d=deferred();e.setSession(({method,current})=>method==='GET'?d.promise:response(current));const pending=e.w.BankClient.refreshIdentity().catch(error=>error);await tick();e.w.BankClient.beginAccountSwitch();e.setCookie(ids[1]);e.w.BankClient.acceptSelectedSession(e.cookie());d.resolve(response(old));const outcome=await pending;assert.equal(outcome.code,'STALE_RESPONSE');assert.equal(e.w.BankClient.identity().account_id,ids[1]);
  }));
  await test('旧refreshIdentity的401错误不得清除已接受的新身份',()=>withEnv({initial:ids[0]},async e=>{
    const old=e.cookie(),d=deferred();e.setSession(({method,current})=>method==='GET'?d.promise:response(current));const pending=e.w.BankClient.refreshIdentity().catch(error=>error);await tick();e.w.BankClient.beginAccountSwitch();e.setCookie(ids[1]);e.w.BankClient.acceptSelectedSession(e.cookie());d.resolve(response({error:{code:'UNAUTHENTICATED',message:'旧令牌已撤销'}},401,old));await pending;assert.equal(e.w.BankClient.identity()?.account_id,ids[1],'旧请求错误不能清掉新账户会话');
  }));
  await test('旧GET资料头与正文不一致不得清除新身份',()=>withEnv({initial:ids[0]},async e=>{
    const old=e.cookie(),d=deferred();e.setPrivate(()=>d.promise);const pending=e.w.BankClient.get('/overview').catch(error=>error);await tick();e.w.BankClient.beginAccountSwitch();e.setCookie(ids[1]);e.w.BankClient.acceptSelectedSession(e.cookie());d.resolve(response({...old,privacy_revision:1},200,old));await pending;assert.equal(e.w.BankClient.identity()?.account_id,ids[1],'旧响应绑定错误不能清掉新身份');
  }));
  await test('旧日历导入预览的401不得清除新身份或历史',()=>withEnv({initial:ids[0]},async e=>{
    const old=e.cookie(),d=deferred();e.setImport(()=>d.promise);const pending=e.w.BankClient.postText('/calendar/import/preview','date,title\n2026-10-06,固定样例').catch(error=>error);await tick();e.w.BankClient.beginAccountSwitch();e.setCookie(ids[1]);e.w.BankClient.acceptSelectedSession(e.cookie());e.w.localStorage.setItem('qingzhou_chatHistory','NEW_ACCOUNT_HISTORY');d.resolve(response({error:{code:'UNAUTHENTICATED',message:'旧令牌已撤销'}},401,old));await pending;assert.equal(e.w.BankClient.identity()?.account_id,ids[1]);assert.equal(e.w.localStorage.getItem('qingzhou_chatHistory'),'NEW_ACCOUNT_HISTORY');
  }));
  await test('目录权限版本过期时服务端阻断选择且没有新Cookie',()=>withEnv({initial:ids[0]},async e=>{
    const previous=e.cookie();e.setSelect(({current})=>response({error:{code:'PRIVACY_REVISION_CHANGED',message:'权限版本已变化'}},409,current));assert.equal(await e.controller.choose(ids[1]),false);assert.equal(e.cookie().account_id,previous.account_id);assert.equal(e.events.filter(x=>x.type==='switched').length,0);assert(e.root.querySelector('[role="alert"]'));const body=JSON.parse(e.requests.find(r=>r.path==='/api/finance/demo/select').options.body);assert.equal(body.expected_privacy_revision,previous.privacy_revision);
  }));
  const passed=results.filter(r=>r.passed).length;console.log(JSON.stringify({passed,total:results.length,network:'mock-only',source_hashes:Object.fromEntries(files.map(p=>[p,crypto.createHash('sha256').update(source[p]).digest('hex')])),failures:results.filter(r=>!r.passed)},null,2));if(passed!==results.length)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1;});
