/* 偏好与记忆整合：真实DOM和原交互函数，全部个人资料/本机存储均为内存夹具。 */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),{JSDOM}=require('jsdom');
const ROOT=path.resolve(__dirname,'../..'),OUT=path.join(ROOT,'outputs/contact-controls-20261010/preferences/validation.json');
const FILES=['mine.html','css/mine_workspace.css','js/mine_workspace.js','js/mine.js','js/insurance_preferences.js','css/insurance_preferences.css'];
const source=file=>fs.readFileSync(path.join(ROOT,file),'utf8'),hash=file=>crypto.createHash('sha256').update(source(file)).digest('hex');
const clone=value=>value===undefined?undefined:JSON.parse(JSON.stringify(value)),tick=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setTimeout(resolve,0));};
const A={workspace_id:'fixture-a',account_id:'subject-a',privacy_revision:42},B={workspace_id:'fixture-a',account_id:'subject-b',privacy_revision:42};
const key=identity=>'navren_insurance_focus:'+identity.workspace_id+':'+identity.account_id;
let assertions=0,memoryPosts=0,insuranceWrites=0;
const equal=(value,expected,label)=>{assertions++;assert.deepEqual(clone(value),expected,label);},check=(value,label)=>{assertions++;assert.ok(value,label);};
const same=(a,b)=>a.workspace_id===b.workspace_id&&a.account_id===b.account_id&&a.privacy_revision===b.privacy_revision;

function insurance() {
  const dom=new JSDOM('<body data-page="mine"><section id="mineHome"><article id="mineInsurancePreferences"></article></section></body>',{url:'https://fixture.test/mine.html?view=home',runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window;
  let identity=clone(A),generation=0,current={page:'mine',view:'home'},delay=null;const store=new Map(),writes=[],events=[];
  store.set(key(A),JSON.stringify({schema:1,keys:['property','health']}));
  Object.defineProperty(w,'localStorage',{value:{getItem:name=>store.get(name)||null,setItem(name,value){writes.push({name,value});insuranceWrites++;store.set(name,value);},removeItem(name){writes.push({name,remove:true});insuranceWrites++;store.delete(name);}}});
  w.BankClient={identity:()=>clone(identity),async ensure(){const captured=clone(identity),waiting=delay;delay=null;if(waiting)await waiting;return captured;},guard(){const captured=clone(identity),epoch=generation;return {valid:()=>epoch===generation&&same(captured,identity)};},get(){assert.fail('保险不调用业务读取');},post(){assert.fail('保险不调用金融或记忆POST');}};
  w.NavrenViews={current:()=>clone(current)};w.NavrenI18n={language:'zh-CN'};
  w.fetch=()=>assert.fail('禁止真实网络');w.addEventListener('navren:insurance-preferences-updated',event=>events.push(clone(event.detail)));
  w.eval(source('js/insurance_preferences.js'));const host=w.document.querySelector('article');
  const action=name=>{const value=host.querySelector('[data-insurance-action="'+name+'"]');check(value,'有操作 '+name);return value;},choice=name=>host.querySelector('[data-insurance-need="'+name+'"]');
  const selected=()=>[...host.querySelectorAll('[data-insurance-need][aria-pressed=true]')].sort((a,b)=>Number(a.textContent.split(' · ')[0])-Number(b.textContent.split(' · ')[0])).map(button=>button.dataset.insuranceNeed);
  const summary=()=>[...host.querySelectorAll('[data-insurance-summary]')].map(value=>value.dataset.insuranceSummary);
  return {dom,w,host,store,writes,events,action,choice,selected,summary,UI:w.NavrenInsurancePreferences,
    async ready(){await tick();await w.NavrenInsurancePreferences.refresh();},
    changeIdentity(next){identity=clone(next);generation++;},
    view(view){current={page:'mine',view};host.closest('section').hidden=view!=='home';generation++;w.dispatchEvent(new w.CustomEvent('navren:view',{detail:clone(current)}));},
    event:name=>w.dispatchEvent(new w.Event(name)),
    external(keys){store.set(key(identity),JSON.stringify({schema:1,keys}));w.dispatchEvent(new w.StorageEvent('storage',{key:key(identity)}));},
    delay(){let release;delay=new Promise(resolve=>{release=resolve;});return release;},close(){dom.window.close();}};
}

async function memoryEnvironment(observations=[]) {
  const dom=new JSDOM(source('mine.html'),{url:'https://fixture.test/mine.html?view=home',runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window;
  await tick(); // 避开页面启动；本检查显式调用原读取/编辑函数，不初始化无关外观功能。
  let identity=clone(A),revision=7,serial=0,state={enabled:false,as_of:'2026-10-10',items:[],personal_revision:7};
  const requests=[],opened=[],toasts=[],bankState={balance_minor:123400,operations:[],transactions:[],permissions:['fixture-original']};
  const bind=value=>({...clone(value),workspace_id:identity.workspace_id,privacy_revision:identity.privacy_revision});
  const item=body=>({node_id:String(++serial).padStart(32,'0'),category:body.category,category_label:'财务目标',value:body.value,expires_on:body.expires_on,source_type:'manual',created_at:1791590400,confirmed_at:1791590400,expired:false,relations:[]});
  w.Storage={get:()=>null,set:()=>assert.fail('禁止实际用户本机持久化')};w.confirm=()=>true;w.showToast=text=>toasts.push(text);
  w.BankClient={identity:()=>clone(identity),ensure:async()=>clone(identity),refreshIdentity:async()=>clone(identity),guard(){const captured=clone(identity);return {valid:value=>same(captured,identity)&&(!value||value.workspace_id===identity.workspace_id&&value.privacy_revision===identity.privacy_revision)};},
    async get(url){requests.push({method:'GET',url});if(url==='/memories')return bind(state);if(url==='/overview')return bind({profile:{risk_level:2},as_of:'2026-10-10'});if(url.includes('view=preferences'))return bind({nodes:clone(observations),personal_revision:revision});if(url.includes('view=overview'))return bind({insights:[]});assert.fail('未知读取 '+url);},
    async post(url,body){requests.push({method:'POST',url,body:clone(body)});memoryPosts++;equal(body.expected_revision,revision,'原版本绑定保留');
      if(url==='/memories')state.items.push(item(body));else if(url==='/memories/settings')state.enabled=body.enabled;
      else if(/^\/memories\/[a-f0-9]{32}\/correct$/.test(url)){const target=state.items.find(item=>url.includes(item.node_id));check(target,'编辑原记忆');Object.assign(target,{value:body.value,expires_on:body.expires_on});}
      else if(/^\/memories\/[a-f0-9]{32}\/delete$/.test(url))state.items=state.items.filter(item=>!url.includes(item.node_id));
      else if(url.startsWith('/profile-observations/')){const target=observations.find(item=>url.includes(item.observation_id));check(target,'处理原陈述');if(body.decision==='confirm'){equal(body.confirmation_token,target.confirmation_token,'原确认凭据保留');target.status='confirmed';}else observations.splice(observations.indexOf(target),1);}
      else assert.fail('禁止付款或无关POST '+url);
      revision++;state.personal_revision=revision;identity.privacy_revision++;return bind(state);
    }};
  w.NavrenViews={current:()=>({page:'mine',view:'home'}),open:async(view,metadata)=>{opened.push({view,metadata});},back(){},register(){}};
  w.NavrenGraph={pause(){},invalidate(){}};w.NavrenDesign={};w.NavrenI18n={language:'zh-CN'};
  w.NavrenFinancialData={overview:async()=>({data:{accounts:[],cards:[],policies:[]},evidence:{coverage_start:'2026-10-10'},as_of:'2026-10-10'}),clear(){}};
  w.fetch=()=>assert.fail('禁止真实网络');w.eval(source('js/mine.js'));
  return {dom,w,requests,opened,toasts,bankState,state:()=>state,revision:()=>revision,identity:()=>identity,
    async renderHome(){w.eval(source('js/mine_workspace.js'));await w.NavrenMine.render({page:'mine',view:'home'});await tick();},
    close(){dom.window.close();}};
}

async function main() {
  const started=new Date().toISOString(),before=Object.fromEntries(FILES.map(file=>[file,hash(file)])),results=[];
  async function scenario(name,fn){try{await fn();results.push({name,status:'passed'});}catch(error){results.push({name,status:'failed',error:error.stack});}}
  await scenario('一个容器、统一新增操作、一行授权；帐号与退出节点保留',async()=>{
    const dom=new JSDOM(source('mine.html')),d=dom.window.document,region=d.querySelector('#minePreferencesSection');
    equal(region.querySelectorAll('.mine-preferences-board').length,1,'只有一个偏好容器');equal(region.querySelectorAll('#addMemoryButton').length,1,'一个新增按钮');
    equal(region.querySelectorAll('.mine-preference-card').length,0,'不再拆成三张高卡');equal(region.querySelectorAll('.mine-memory-group').length,2,'保存和聊天仍各有独立分区');
    check(region.querySelector('.mine-preferences-consent #memoryStatus'),'授权状态集中一行');check(region.querySelector('.mine-preferences-consent [data-mine-focustarget=settingsMemoryConsentTitle]'),'授权链接直接定位开关');
    for(const id of ['mineAccountSection','profileAvatar','profileName','switchLoginAccount','logoutAccount','memoryToggle'])check(d.getElementById(id),'保留原节点 '+id);
    equal(d.getElementById('memoryToggle').getAttribute('onclick'),'toggleFinancialMemory()','授权开关原函数保留');dom.window.close();
  });
  await scenario('保险默认仅有序摘要，修改展开七方向，草稿上限三项与取消不写',async()=>{
    const e=insurance();try{await e.ready();equal(e.summary(),['property','health'],'已存方向顺序不变');equal(e.host.querySelectorAll('[data-insurance-need]').length,0,'默认不挂七项编辑器');equal(e.host.querySelectorAll('button').length,1,'默认只有修改操作');
      e.action('edit').click();equal(e.host.querySelectorAll('[data-insurance-need]').length,7,'修改后七个原方向');equal(e.selected(),['property','health'],'展开沿用选择');
      e.choice('pension').click();equal(e.selected(),['property','health','pension'],'第三项按顺序追加');check(e.choice('accident').disabled,'三项时未选项禁用');e.choice('accident').onclick();equal(e.selected().length,3,'强制事件也不能第四项');
      e.choice('health').click();e.choice('accident').click();equal(e.selected(),['property','pension','accident'],'取消一项后按新顺序追加');equal(e.writes.length,0,'草稿不写');e.action('cancel').click();equal(e.summary(),['property','health'],'取消恢复保存摘要');equal(e.writes.length,0,'取消不写');equal(e.host.querySelectorAll('[data-insurance-need]').length,0,'取消后折叠');
    }finally{e.close();}
  });
  await scenario('保存折叠并沿用原schema，自动展示不造默认方向',async()=>{
    const e=insurance();try{await e.ready();e.action('edit').click();e.choice('pension').click();e.action('save').click();equal(JSON.parse(e.store.get(key(A))),{schema:1,keys:['property','health','pension']},'存储合同不变');equal(e.summary(),['property','health','pension'],'保存后只显示三项摘要');equal(e.host.querySelectorAll('[data-insurance-need]').length,0,'保存后折叠');equal(e.events.at(-1),A,'事件只带原身份字段');
      e.action('edit').click();e.action('automatic').click();equal(e.UI.read(A),[],'自动展示没有显式方向');check(!e.store.has(key(A)),'自动展示删除显式本机偏好');equal(e.summary(),[],'摘要不造默认选项');check(e.host.textContent.includes('按已有资料展示'),'明确空偏好语义');
    }finally{e.close();}
  });
  await scenario('身份、授权、视图和旧节点检查保持；跨页更新不覆盖未保存选择',async()=>{
    const e=insurance();try{await e.ready();e.action('edit').click();e.choice('pension').click();const draft=e.selected();await e.UI.refresh();equal(e.selected(),draft,'同身份刷新保留草稿');e.external(['liability']);equal(e.selected(),draft,'外部更新保留草稿');e.action('cancel').click();equal(e.summary(),['liability'],'取消读取最新已保存值');
      e.action('edit').click();const stale=e.action('save'),before=e.writes.length;e.view('settings');stale.click();equal(e.writes.length,before,'隐藏视图旧按钮不写');e.view('home');await tick();stale.click();equal(e.writes.length,before,'重绘后旧节点不写');
      const old=e.action('save');e.changeIdentity(B);old.click();equal(e.writes.length,before,'变更身份旧按钮不写');e.event('navren:privacy-reset');equal(e.host.querySelectorAll('button').length,0,'失效立即移除编辑器');e.store.set(key(B),JSON.stringify({schema:1,keys:['accident']}));e.event('navren:account-switched');await tick();equal(e.summary(),['accident'],'新主体只读本人方向');equal(e.UI.read(A),['liability'],'原主体未覆盖');
      const release=e.delay(),late=e.UI.refresh();e.event('navren:identity-revoked');e.changeIdentity(A);e.view('home');await tick();release();await late;equal(e.summary(),['liability'],'迟到身份不覆盖');e.action('edit').click();const oldRevision=e.action('save');e.changeIdentity({...A,privacy_revision:43});oldRevision.click();equal(e.writes.length,before,'新授权拒绝旧保存');e.event('pagehide');equal(e.host.querySelectorAll('button').length,0,'离页清理');e.event('pageshow');await tick();equal(e.summary(),['liability'],'恢复再次核对身份');
    }finally{e.close();}
  });
  await scenario('原财务记忆读取、新增、修改、删除与授权仍使用相同API和版本',async()=>{
    const e=await memoryEnvironment();try{const initialBank=clone(e.bankState);await e.w.NavrenMineMemory.load();equal(e.w.document.querySelector('#memoryStatus').textContent,'读取已关闭，保存内容仍可管理','短状态保留关闭语义');check(e.w.document.querySelector('#financialMemoryList').textContent.includes('暂无已保存记忆'),'短空态');
      e.w.openFinancialMemoryEditor();e.w.document.querySelector('#memoryValue').value='夹具目标';await e.w.document.querySelector('#modalConfirm').onclick();equal(e.state().items[0].value,'夹具目标','新增仍保存原字段');equal(e.state().enabled,false,'新增不自动授权');
      const entry=e.w.document.querySelector('.memory-item');entry.querySelector('[aria-label^=修改]').click();equal(e.w.document.querySelector('#memoryValue').value,'夹具目标','编辑回填原值');e.w.document.querySelector('#memoryValue').value='夹具修订';await e.w.document.querySelector('#modalConfirm').onclick();equal(e.state().items[0].value,'夹具修订','修改走原correct');
      await e.w.toggleFinancialMemory();equal(e.state().enabled,true,'原开关可开启');equal(e.w.document.querySelector('#memoryToggle').getAttribute('aria-checked'),'true','toggle状态同步');equal(e.w.document.querySelector('#memoryStatus').textContent,'已允许在资金分析中参考记忆','短状态仍说明读取范围');
      e.w.document.querySelector('.memory-item [aria-label^=删除]').click();await tick();equal(e.state().items,[],'删除走原delete');await e.w.toggleFinancialMemory();equal(e.state().enabled,false,'原开关可撤销');equal(e.bankState,initialBank,'记忆操作不修改假账本或权限');
      equal(e.requests.filter(row=>row.method==='POST').map(row=>row.url),['/memories','/memories/'+('1'.padStart(32,'0'))+'/correct','/memories/settings','/memories/'+('1'.padStart(32,'0'))+'/delete','/memories/settings'],'API与保存逻辑保持原合同');
    }finally{e.close();}
  });
  await scenario('真实节点status区分待核实与本人确认，确认/删除保留原凭据及API',async()=>{
    const candidate={observation_id:'a'.repeat(32),label:'夹具陈述甲',status:'candidate',quote:'夹具原话甲',confirmation_token:'fixture-token'},confirmed={observation_id:'b'.repeat(32),label:'夹具陈述乙',status:'confirmed',quote:'夹具原话乙',confirmation_token:'fixture-confirmed'};
    const e=await memoryEnvironment([candidate,confirmed]);try{await e.renderHome();const rows=[...e.w.document.querySelectorAll('.mine-observation')];equal(rows.length,2,'沿用真实nodes形状');equal(rows[0].querySelector('.mine-status').textContent,'待核实','候选不冒充已确认');equal(rows[1].querySelector('.mine-status').textContent,'本人确认','本人确认保留区分');check(!rows[1].textContent.includes('确认是我的陈述'),'已确认无重复确认按钮');equal(rows[0].querySelector('blockquote').textContent,candidate.quote,'原话没有改写');
      rows[0].querySelector('button').click();await tick();equal(e.requests.find(row=>row.url.includes(candidate.observation_id)).body,{decision:'confirm',expected_revision:7,confirmation_token:'fixture-token'},'确认继续绑定版本和原凭据');equal(e.opened.at(-1).view,'profile','确认后走原刷新流程');
      check(e.w.document.querySelector('#financeMemoryTitle').closest('.mine-memory-group')!==e.w.document.querySelector('#mineStatementsTitle').closest('.mine-memory-group'),'两种资料保持独立语义容器');equal(e.w.document.querySelector('#minePreferencesSectionTitle').textContent,'偏好与记忆','统一标题');
    }finally{e.close();}
  });
  const after=Object.fromEntries(FILES.map(file=>[file,hash(file)])),changed=FILES.filter(file=>before[file]!==after[file]),failed=results.filter(row=>row.status==='failed').length;
  const report={schema:'navren.preferences-integrated-checks.v1',started_at:started,completed_at:new Date().toISOString(),status:failed||changed.length?'failed':'passed',passed:results.length-failed,failed,assertions,tests:results,input_hashes:before,after_hashes:after,changed_inputs:changed,execution:{node:process.version,jsdom:require('jsdom/package.json').version,fixture_memory_posts:memoryPosts,fixture_insurance_storage_writes:insuranceWrites,actual_http_requests:0,paid_model_calls:0,real_personal_database_writes:0,real_local_storage_writes:0,browser_run:false,real_funds:false},not_tested:['真实浏览器视觉与窄屏布局','真实个人资料的新增、修改、删除或授权持久化']};
  fs.mkdirSync(path.dirname(OUT),{recursive:true});fs.writeFileSync(OUT,JSON.stringify(report,null,2)+'\n');process.stdout.write(JSON.stringify({status:report.status,passed:report.passed,failed,assertions,changed_inputs:changed,failures:results.filter(row=>row.status==='failed')},null,2)+'\n');if(failed||changed.length)process.exitCode=1;
}
main().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
