/* 实际 BankClient / AA / account_home 初始化恢复；仅同源 HTTP 与非 AA 资产目录受控。 */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {JSDOM,VirtualConsole}=require('jsdom');
const root=path.resolve(__dirname,'../..'),out=process.argv[2];
const aaFile=path.resolve(process.argv[3]||path.join(root,'js/aa_payments.js'));
const homeFile=path.resolve(process.argv[4]||path.join(root,'js/account_home.js'));
const selected=new Set((process.argv[5]||'').split(',').filter(Boolean));
const files={aa:aaFile,home:homeFile,bank:path.join(root,'js/bank_client.js'),money:path.join(root,'js/money_input.js'),html:path.join(root,'account.html')};
const sources=Object.fromEntries(Object.entries(files).map(([key,file])=>[key,fs.readFileSync(file,'utf8')]));
const sha=value=>crypto.createHash('sha256').update(value).digest('hex');
const pins=Object.fromEntries(Object.entries(files).map(([key,file])=>[path.relative(root,file),sha(sources[key])]));
const clone=value=>value==null?value:JSON.parse(JSON.stringify(value));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function settle(){for(let i=0;i<10;i++)await tick();}
async function until(test,label){const started=Date.now();while(!test()){if(Date.now()-started>700)throw Error('定向等待超时：'+label);await tick();}}
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
const results=[],unhandled=[];process.on('unhandledRejection',error=>unhandled.push(error.stack||String(error)));

async function fixture({coldSession=false}={}){
  const errors=[],calls=[],homeEvents=[],contactHolds=[],billHolds=[],connectedHolds=[],sessionHolds=[];
  const vc=new VirtualConsole();vc.on('jsdomError',error=>errors.push(error.stack||String(error)));
  const dom=new JSDOM(sources.html,{url:'https://navren.invalid/account.html?view=home',runScripts:'outside-only',pretendToBeVisual:true,virtualConsole:vc});
  const w=dom.window,d=w.document;let viewRevision=1,valid=true,authenticated=true,directoryName=null;
  function session(name,privacy=0){const now=Date.now()/1000;return {registered_user_id:(name==='A'?'a':'b').repeat(32),workspace_id:(name==='A'?'1':'2').repeat(32),account_id:name==='A'?'working_landlady':'grain_farmer',privacy_revision:privacy,
    environment:'simulation',auth_subject:'registered-aa-initialization-'+name,session_generation:name==='A'?1:2,csrf:'fixture-csrf-'+name,auth_expires_at:now+7200,lease_expires_at:now+3600};}
  let current=session('A');
  const binding=identity=>({environment:'simulation',registered_user_id:identity.registered_user_id,workspace_id:identity.workspace_id,privacy_revision:identity.privacy_revision});
  function directory(identity){return {...binding(identity),current_user_id:identity.account_id,contacts:[],available_users:[{user_id:identity.account_id==='working_landlady'?'grain_farmer':'working_landlady',display_name:directoryName||(identity.account_id==='working_landlady'?'A目录成员':'B目录成员')}]};}
  const ledger=identity=>({...binding(identity),current_user_id:identity.account_id,initiated:[],participating:[],pending:[]});
  const state=()=>({...binding(current),current_user_id:current.account_id,version:1,as_of:'2026-10-17',accounts:[],cards:[],holdings:[],profile:{display_name:'受控账户'}});
  const connected=()=>({...binding(current),subject_id:current.account_id,version:1,as_of:'2026-10-17',observed_at:'2026-10-17T06:00:00Z',dataset_version:'controlled-assets-1',data:{accounts:[],cards:[],connections:[],credit_facilities:[],summary:{total_assets_minor:0,net_worth_minor:0,cash_minor:0,investment_minor:0,liabilities_minor:0,real_assets_minor:0,pending_redemption_minor:0}}});
  const welcome=()=>({authenticated,registered_user_id:current.registered_user_id,auth_subject:current.auth_subject});
  w.NavrenWelcomeSession={getState:welcome,refresh:async()=>welcome()};
  w.NavrenViews={revision:()=>viewRevision,current:()=>({page:'account',view:'home',filters:{}}),update:()=>{},url:()=>'/account.html?view=home'};
  w.NavrenI18n={language:'zh',t:value=>value};
  w.Storage={unloadPrivateContext(){},invalidatePrivateCache(){}};
  w.fetch=async(url,options={})=>{
    const pathname=String(url),method=options.method||'GET',identity=clone(current);
    assert.equal(method,'GET','本定点绝不重放或发起 POST');assert.equal(options.credentials,'same-origin');assert.equal(options.cache,'no-store');
    assert.ok(['/api/finance/session','/api/finance/payment-contacts','/api/finance/aa/bills'].includes(pathname),'仅允许实际 BankClient 的冻结读接口 '+pathname);
    const row={path:pathname,method,owner:identity.registered_user_id,workspace:identity.workspace_id,privacy:identity.privacy_revision,aborted:false};calls.push(row);options.signal?.addEventListener('abort',()=>{row.aborted=true;},{once:true});
    const value=pathname.endsWith('/session')?identity:pathname.endsWith('/payment-contacts')?directory(identity):ledger(identity);
    const response={ok:true,status:200,json:async()=>clone(value),headers:{get:name=>name.toLowerCase()==='x-navren-workspace'?identity.workspace_id:name.toLowerCase()==='x-navren-privacy-revision'?String(identity.privacy_revision):null}};
    const hold=(pathname.endsWith('/session')?sessionHolds:pathname.endsWith('/payment-contacts')?contactHolds:pathname.endsWith('/aa/bills')?billHolds:[]).shift();
    if(hold){hold.claimed=true;hold.release=()=>hold.resolve(response);return hold.promise;}return response;
  };
  const n=(tag,text)=>{const el=d.createElement(tag);if(text!=null)el.textContent=text;return el;};
  const link=(text,view)=>{const el=n('a',text);el.href='/account.html?view='+view;return el;};
  w.BankUI={node:n,button:(text,action)=>{const el=n('button',text);el.type='button';el.onclick=action;return el;},formatMoney:minor=>(minor/100).toFixed(2)+' 元',load:async()=>{homeEvents.push('BankUI.load');return {state:state()};}};
  // 资产、月报与图形不是本次目标。目录返回受控零余额；AA/Home 目标函数完整执行。
  w.NavrenConnectedAssetsUI={paymentPlatforms:[],institution:()=>''};
  w.NavrenFinancialUI={institution:()=>'',basis:()=>n('span','受控来源'),transactionTable:()=>{}};
  w.NavrenConnectedAssets={scope:'connected_bank',clear(){},reference:(_envelope,kind,id,extra)=>({source_scope:'connected_bank',object_kind:kind,object_id:id,...extra}),
    overview:async()=>{homeEvents.push('connected-overview');const value=connected(),hold=connectedHolds.shift();if(hold){hold.claimed=true;hold.release=()=>hold.resolve(value);return hold.promise;}return value;},
    securitiesSummary:()=>({accounts:[],total_minor:0}),transactions:async()=>({...connected(),data:{items:[],total:0}})};
  w.NavrenFinancialData={scope:'historical',clear(){},overview:async()=>({dataset_version:'controlled-history-1',as_of:'2026-10-17',evidence:{coverage_start:'2026-10-01',coverage_end:'2026-10-17'}}),
    month:async month=>({data:{month,opening:{date:'2026-10-01',total_assets_minor:0,cash_minor:0},closing:{total_assets_minor:0,net_worth_minor:0,cash_minor:0},totals:{income_minor:0,expense_minor:0,refund_minor:0,net_cash_change_minor:0}}}),assetCurve:async()=>[]};
  w.eval(sources.bank);if(!coldSession)await w.BankClient.ensure();w.eval(sources.money);w.eval(sources.aa);w.eval(sources.home);
  function options(){return {state:state(),isCurrent:()=>valid,link,validateConnectedEnvelope:value=>value.workspace_id===current.workspace_id};}
  const host=()=>d.getElementById('accountSplitForm'),home=()=>d.getElementById('accountHome'),card=()=>d.getElementById('accountSplitCard');
  const button=text=>[...host().querySelectorAll('button')].find(item=>item.textContent.trim()===text);
  function snapshot(){return {text:host().textContent,create_disabled:button('新建账单')?.disabled??null,reads:calls.filter(row=>!row.path.endsWith('/session')).map(clone),home_events:[...homeEvents]};}
  async function ready(){await until(()=>button('新建账单')&&!button('新建账单').disabled&&!host().textContent.includes('账单资料尚未取得'),'合法目录与账单恢复');}
  function hold(queue){const item=deferred();queue.push(item);return item;}
  const aaReads=()=>calls.filter(row=>!row.path.endsWith('/session'));
  return {w,d,dom,errors,calls,homeEvents,contactHolds,billHolds,connectedHolds,host,home,card,button,snapshot,ready,aaReads,
    holdContacts:()=>hold(contactHolds),holdBills:()=>hold(billHolds),holdConnected:()=>hold(connectedHolds),
    holdSession:()=>hold(sessionHolds),
    setDirectoryName(value){directoryName=value;},
    render:()=>w.NavrenAccountHome.render(options()),
    toggle(open=true){card().open=open;card().dispatchEvent(new w.Event('toggle'));},
    view(){viewRevision++;},hide(value=true){home().hidden=value;},revoke(){valid=false;viewRevision++;home().hidden=true;},
    async change(name,privacy=0){w.BankClient.beginAccountSwitch();current=session(name,privacy);w.BankClient.acceptSelectedSession(current);await w.BankClient.refreshIdentity();await settle();},
    get identity(){return clone(current);},
    async close(){w.NavrenAccountHome.clear();await settle();assert.deepEqual(errors,[],'无未捕获 DOM 错误');w.close();}};
}
async function check(id,name,operation,fixtureOptions){if(selected.size&&!selected.has(id))return;let f;try{f=await fixture(fixtureOptions);const details=await operation(f);await f.close();results.push({id,name,passed:true,details});}
catch(error){const observed=f?.snapshot();try{await f?.close();}catch(_){}results.push({id,name,passed:false,error:error.stack||String(error),evidence:error.evidence||null,observed});}}

(async()=>{
  await check('AA01','初始隐藏 host，connected 回来重挂载自动完成首次读取',async f=>{
    const assets=f.holdConnected();f.hide();const render=f.render();await settle();assert.equal(f.aaReads().length,0,'不可见时不读取业务目录');
    f.hide(false);assets.release();await render;await settle();
    let failed;try{await f.ready();}catch(error){failed=error;}
    if(failed){const before=f.snapshot();assert.equal(f.button('新建账单')?.disabled,true);f.button('重新读取').click();await f.ready();failed.message+='；手动重新读取可恢复，修前观测='+JSON.stringify(before);throw failed;}
    assert.equal(f.aaReads().length,2,'合法重挂载只取得一对目录/账单');return f.snapshot();
  });
  await check('AA02','合法 GET200 因旧视图失效丢弃后，connected 重挂载补读且旧结果不绘制',async f=>{
    const contacts=f.holdContacts(),bills=f.holdBills(),assets=f.holdConnected(),render=f.render();await until(()=>contacts.claimed&&bills.claimed,'第一对实际 GET 发出');
    f.hide();f.view();contacts.release();bills.release();await settle();assert.equal(f.host().textContent.includes('暂无本人发起的账单'),false,'旧视图迟到结果不可绘制');
    f.hide(false);assets.release();await render;
    try{await f.ready();}catch(error){const before=f.snapshot();assert.equal(f.button('新建账单')?.disabled,true);f.button('重新读取').click();await f.ready();error.evidence={before_manual_retry:before,manual_retry_recovers:true,after_manual_retry:f.snapshot()};throw error;}
    assert.equal(f.aaReads().length,4,'只补一对读，不重放写操作');return f.snapshot();
  });
  await check('AA03','初始隐藏后仅展开真实 AA 卡片可恢复，不依赖整页手动重读',async f=>{
    f.hide();await f.render();await settle();assert.equal(f.aaReads().length,0);f.hide(false);f.toggle();await f.ready();assert.equal(f.aaReads().length,2);return f.snapshot();
  });
  await check('AA04','已核验资料反复展开和渲染不重复 GET，未提交草稿保留且无 POST',async f=>{
    await f.render();await f.ready();f.toggle();f.button('新建账单').click();const title=f.host().querySelector('[name="title"]');assert.ok(title);title.value='本人尚未确认的账单';title.dispatchEvent(new f.w.Event('input',{bubbles:true}));
    const count=f.aaReads().length;for(let i=0;i<5;i++){f.toggle(false);f.toggle(true);await f.render();}await new Promise(resolve=>setTimeout(resolve,650));
    assert.equal(f.aaReads().length,count,'已确认目录不随重复挂载形成读风暴');assert.equal(f.host().querySelector('[name="title"]')?.value,'本人尚未确认的账单');assert.equal(f.calls.some(row=>row.method==='POST'),false);return {...f.snapshot(),stable_wait_ms:650};
  });
  await check('AA05','初始化飞行中切账户，A迟到响应不得填入B',async f=>{
    const contacts=f.holdContacts(),bills=f.holdBills(),assets=f.holdConnected(),oldRender=f.render();await until(()=>contacts.claimed&&bills.claimed,'A读请求');
    const guard=f.w.BankClient.guard();await f.change('B');await f.render();await f.ready();contacts.release();bills.release();assets.release();await oldRender;await settle();
    assert.equal(guard.valid(),false);f.button('新建账单').click();assert.ok(f.host().textContent.includes('B目录成员'));assert.equal(f.host().textContent.includes('A目录成员'),false);return {...f.snapshot(),old_guard_valid:false};
  });
  await check('AA06','隐私版本改变后旧 GET200 仍拒绝，新版本独立恢复',async f=>{
    const contacts=f.holdContacts(),bills=f.holdBills(),assets=f.holdConnected(),oldRender=f.render();await until(()=>contacts.claimed&&bills.claimed,'隐私0读请求');const guard=f.w.BankClient.guard();
    await f.change('A',1);await f.render();await f.ready();contacts.release();bills.release();assets.release();await oldRender;await settle();assert.equal(guard.valid(),false);assert.equal(f.w.BankClient.identity().privacy_revision,1);
    assert.equal(f.aaReads().filter(row=>row.privacy===1).length,2);return {...f.snapshot(),old_guard_valid:false,current_privacy:1};
  });
  await check('AA07','视图撤销时迟到响应及展开事件均不能重新启用业务入口',async f=>{
    const contacts=f.holdContacts(),bills=f.holdBills(),assets=f.holdConnected(),render=f.render();await until(()=>contacts.claimed&&bills.claimed,'撤销前读请求');
    f.revoke();contacts.release();bills.release();assets.release();await render;await settle();f.toggle();await settle();assert.notEqual(f.button('新建账单')?.disabled,false);assert.equal(f.aaReads().length,2);return f.snapshot();
  });
  await check('AA08','旧视图读尚在飞行时 connected 已合法重挂载，收尾后仅接续一对读',async f=>{
    const contacts=f.holdContacts(),bills=f.holdBills(),assets=f.holdConnected(),render=f.render();await until(()=>contacts.claimed&&bills.claimed,'第一对读飞行中');
    f.view();assets.release();await render;await settle();assert.equal(f.aaReads().length,2,'旧读未结束前不并发新增目录读取');
    contacts.release();bills.release();await f.ready();assert.equal(f.aaReads().length,4,'旧读失效后仅接续一对合法读');return f.snapshot();
  });
  await check('AA09','实际 session GET 尚未返回且 identity 为 null 时重挂载，ready 不得提前成功',async f=>{
    const session=f.holdSession(),controller=f.w.NavrenAAPayments.mount(f.host(),{isCurrent:()=>true});
    await until(()=>session.claimed,'实际 BankClient.ensure 发出 session GET');assert.equal(f.w.BankClient.identity(),null);assert.equal(f.aaReads().length,0);
    const remounted=f.w.NavrenAAPayments.mount(f.host(),{isCurrent:()=>true});assert.equal(remounted,controller,'合法重挂载复用现有 controller');
    let settled=false;remounted.ready.then(()=>{settled=true;});await settle();const before={identity:f.w.BankClient.identity(),ready_settled:settled,aa_GETs:f.aaReads().length,text:f.host().textContent};
    session.release();await controller.ready;await f.ready();const details={before_session_release:before,after_session_release:f.snapshot()};
    if(before.ready_settled){const error=Error('身份尚未建立时重挂载 ready 提前成功');error.evidence=details;throw error;}
    assert.equal(before.aa_GETs,0);assert.equal(f.aaReads().length,2,'会话核验后仅初始化一对目录读');return details;
  },{coldSession:true});
  await check('AA10','用户明确重新读取可越过旧有效飞行请求，新目录胜出且旧迟到不可覆盖',async f=>{
    const contacts=f.holdContacts(),bills=f.holdBills(),assets=f.holdConnected(),render=f.render();await until(()=>contacts.claimed&&bills.claimed,'旧合法目录 GET 飞行中');
    assets.release();await render;await settle();assert.ok(f.button('重新读取'),'实际 connected 重挂载显示原刷新按钮');assert.equal(f.aaReads().length,2);
    f.setDirectoryName('本人明确刷新取得的新成员');f.button('重新读取').click();
    try{await until(()=>f.aaReads().length===4,'用户刷新实际发出第二对 GET');}catch(error){const before=f.snapshot();contacts.release();bills.release();await f.ready();f.button('新建账单').click();error.evidence={before_old_release:before,after_old_release:f.snapshot(),explicit_refresh_did_not_issue_new_GET:true};throw error;}
    await f.ready();f.button('新建账单').click();assert.ok(f.host().textContent.includes('本人明确刷新取得的新成员'));assert.equal(f.host().textContent.includes('A目录成员'),false);
    const before=f.snapshot();contacts.release();bills.release();await settle();assert.ok(f.host().textContent.includes('本人明确刷新取得的新成员'),'旧请求迟到后仍保留新目录');assert.equal(f.host().textContent.includes('A目录成员'),false);assert.equal(f.aaReads().length,4,'旧token不安排额外接续');
    return {new_snapshot_before_old_release:before,after_old_release:f.snapshot()};
  });
  await settle();const drift=Object.fromEntries(Object.entries(files).map(([key,file])=>[path.relative(root,file),sha(fs.readFileSync(file))]));assert.deepEqual(drift,pins,'运行前后完整生产源冻结');
  const result={contract:'navren.ui-aa-initialization-tests/1',started_at:process.env.NAVREN_TEST_STARTED_AT||null,finished_at:new Date().toISOString(),source_sha256:pins,
    boundary:{actual:['完整 js/bank_client.js，包括 ensure/raw/identity/guard 与真实异步请求守卫','完整 js/aa_payments.js，包括 mount/load/live/request/draw/草稿控制','完整 js/account_home.js，包括 render/splitPayments/connected/refresh/toggle','实际 account.html 结构与原生 DOM、Promise、计时器'],controlled:['注册欢迎身份和同源 GET200/延迟 transport','非 AA 资产目录、月报与路由链接'],not_tested:['真实后端/数据库','真实浏览器/正式入口','AA prepare/confirm/pay 写操作与消费或图片上传','模型、短信和付费服务']},results,unhandled,passed:results.every(row=>row.passed)&&unhandled.length===0};
  if(!out)throw Error('必须提供本次独立结果文件');fs.mkdirSync(path.dirname(out),{recursive:true});fs.writeFileSync(out,JSON.stringify(result,null,2)+'\n');
  console.log(JSON.stringify({passed:result.passed,cases:results.map(({id,passed})=>({id,passed})),unhandled:unhandled.length,output:out}));process.exitCode=result.passed?0:1;
})().catch(error=>{console.error(error.stack||String(error));process.exitCode=2;});
