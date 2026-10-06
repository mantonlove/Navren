/* 纯内存 DOM 回归：不访问网络、浏览器、正式账户或数据库。 */
const fs=require('fs'),assert=require('assert'),{JSDOM}=require('jsdom');
const code=fs.readFileSync('js/calendar_ui.js','utf8'),i18n=fs.readFileSync('js/i18n.js','utf8');
const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
let checks=0;
function fixture(p={},overrides={}){
  return {view_id:'full-day',workspace_binding:'ws',as_of:'2026-10-03',source_scope:{statement_source:p.source||'simulation',portfolio:p.portfolio||'current',accounting_basis:p.accounting_basis||'holdings_only_v1',kinds:p.kinds||[]},view:p.view||'month',range:{from:p.from||'2026-10-03',to:p.to||'2026-10-03'},summary_by_currency:[],generated_at:'2026-10-06T08:00:00+08:00',days:[],source_versions:{ledger:3},...overrides};
}
function widget(snapshot){return {type:'financial_calendar',schema_version:1,view_id:snapshot.view_id,workspace_binding:snapshot.workspace_binding,source_scope:snapshot.source_scope,range:snapshot.range,view:snapshot.view,snapshot};}
function item(overrides={}){return {id:'event',title:'用户生日计划',date:'2026-10-03',kind:'life_event',state:'planned',source_type:'user_calendar',source_ref:'life-1',source_revision:2,allowed_actions:[],...overrides};}
function setup({language='zh-CN',page='account',withI18n=false}={}){
  const dom=new JSDOM('<body><main id="root"></main></body>',{url:'https://navren.test/account.html',runScripts:'outside-only'}),w=dom.window,root=w.document.getElementById('root');
  w.document.body.dataset.page=page;let identity={workspace_id:'ws',privacy_revision:1},generation=0,revision=0,context={source_scope:'simulation',portfolio_id:'current',filters:{}};
  const calls={query:[],day:[],item:[],get:[],open:[]};
  w.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
  w.Storage={get:()=>language,set(){}};w.STORAGE_KEYS={LANGUAGE:'language'};
  if(withI18n)w.eval(i18n);
  w.NavrenHelp={button:()=>w.document.createElement('span')};
  w.NavrenViews={current:()=>context,revision:()=>revision,update:p=>{context={...context,...p};revision++;},open:(view,p)=>{calls.open.push({view,p});revision++;},reference(){}};
  w.BankClient={identity:()=>({...identity}),ensure:async()=>({...identity}),guard(){const i={...identity},g=generation,r=revision;return {valid:()=>g===generation&&r===revision&&i.workspace_id===identity.workspace_id&&i.privacy_revision===identity.privacy_revision};},get:async path=>{calls.get.push(path);return {versions:[]};},minor:v=>Number(v)*100};
  w.CalendarClient={query:async p=>{calls.query.push(p);return fixture(p);},day:async(id,date,offset)=>{calls.day.push({id,date,offset});return {notice:'快照与来源已核对',total:0,items:[],summary_by_currency:[],source_versions:{ledger:3},has_more:false};},item:async id=>{calls.item.push(id);return item();}};
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
  w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
  w.eval(code);
  return {dom,w,root,calls,changeIdentity(next={workspace_id:'other',privacy_revision:1},privacy=true){identity=next;generation++;if(privacy)w.dispatchEvent(new w.Event('navren:privacy-reset'));},changeView(){revision++;},close(){w.close();}};
}
async function test(name,fn){await fn();checks++;console.log('通过：'+name);}
const findButton=(root,text)=>[...root.querySelectorAll('button')].find(n=>n.textContent===text);
async function withEnv(options,fn){const e=setup(options);try{await fn(e);}finally{e.close();}}
(async()=>{
  await test('已计算盈利、亏损与零各有独立语义和正负号',()=>withEnv({},async({w,root})=>{
    const values=[100,-200,0];const days=values.map((v,i)=>({date:'2026-10-0'+(i+1),item_count:1,summary_by_currency:[{currency:'CNY',investment_pnl_minor:v}],preview:[]}));
    await w.CalendarUI.mount(root,widget(fixture({}, {days,summary_by_currency:[{currency:'CNY',investment_pnl_minor:0,recorded_net_minor:321}],holdings_pnl_minor:0})));
    const amounts=[...root.querySelectorAll('.calendar-day-pnl')];assert.deepEqual(amounts.map(n=>n.dataset.calculationStatus),['positive','negative','zero']);assert.equal(amounts[0].textContent,'+1.00元');assert.equal(amounts[1].textContent,'-2.00元');assert.equal(amounts[2].textContent,'0.00元');
    assert.equal(root.querySelector('.calendar-metric strong').dataset.calculationStatus,'zero');assert.equal(root.querySelectorAll('.calendar-metric strong')[2].textContent,'3.21元','现金净流入只显示后端值');
  }));
  await test('null未知与部分缺失不会渲染为灰色零',()=>withEnv({},async({w,root})=>{
    const days=[{date:'2026-10-01',item_count:0,wealth_status:'not_disclosed',summary_by_currency:[]},{date:'2026-10-02',item_count:0,wealth_status:'missing',summary_by_currency:[]},{date:'2026-10-03',item_count:1,summary_by_currency:[{currency:'CNY',investment_pnl_minor:null,pnl_coverage:'partial'}]}];
    await w.CalendarUI.mount(root,widget(fixture({}, {days,summary_by_currency:[{currency:'CNY',investment_pnl_minor:null,pnl_coverage:'partial'}],holdings_pnl_minor:null})));
    const amounts=[...root.querySelectorAll('.calendar-day-pnl')];assert.deepEqual(amounts.map(n=>n.dataset.calculationStatus),['unknown','missing','missing']);assert(!root.querySelector('.calendar-day .pnl-zero'));assert(root.querySelector('.calendar-metric .pnl-missing').textContent.includes('缺失'));
  }));
  await test('事项损益零值在摘要明确保留',()=>withEnv({},async({w,root})=>{
    w.CalendarClient.item=async()=>item({returns:{status:'available',daily_pnl_minor:0},currency:'CNY'});await w.CalendarUI.renderItem(root,'event',{});
    const n=root.querySelector('.calendar-summary .pnl-zero');assert(n);assert.equal(n.textContent,'0.00元');assert(root.textContent.includes('已计算投资损益'));
  }));
  await test('收益筛选日期展开时查询同来源同组合的全日事项',()=>withEnv({},async({w,root,calls})=>{
    w.CalendarClient.query=async p=>{calls.query.push(p);return fixture(p,{view_id:'all-kinds',limitations:['估值和到账分开'],coverage_basis:'已审核账本口径'});};
    w.CalendarClient.day=async(id,date,offset)=>{calls.day.push({id,date,offset});return {notice:'已核对',total:3,items:[item({id:'cash',kind:'transaction',title:'工资入账'}),item({id:'pnl',kind:'investment_pnl',title:'当日理财损益'}),item({id:'birthday',kind:'birthday',title:'家人生日'})],summary_by_currency:[{currency:'CNY',investment_pnl_minor:0}],source_versions:{ledger:7,nav:4},has_more:false};};
    await w.CalendarUI.renderDay(root,{date:'2026-10-03',source_scope:'simulation',portfolio_id:'history-one',filters:{kind:'returns',calendar_view_id:'filtered-view'}});await tick();
    const p=calls.query[0];assert.equal(p.from,p.to);assert.equal(p.from,'2026-10-03');assert.equal(p.view,'day');assert.equal(p.source,'simulation');assert.equal(p.portfolio,'history-one');assert.equal(p.accounting_basis,'total_equity_v2');assert.equal(p.include_life_events,true);assert.deepEqual(Array.from(p.kinds),[]);assert(!('all' in p));assert.equal(calls.day[0].id,'all-kinds');
    assert.equal(root.querySelectorAll('.calendar-item').length,3);assert(root.textContent.includes('当前筛选事项'));assert(root.textContent.includes('不合并其他账本'));assert(root.querySelector('.calendar-day-evidence').textContent.includes('"ledger":7'));assert(root.textContent.includes('已审核账本口径'));
  }));
  await test('导入账本全日不启用演示叠加且沿用本金口径',()=>withEnv({},async({w,root,calls})=>{
    await w.CalendarUI.renderDay(root,{date:'2026-10-03',source_scope:'imported',portfolio_id:'current',filters:{kind:'life',calendar_view_id:'filtered'}});await tick();assert.equal(calls.query[0].source,'imported');assert.equal(calls.query[0].simulation_overlay,false);assert.equal(calls.query[0].accounting_basis,'holdings_only_v1');
  }));
  await test('现行CalendarClient确实发送空kinds与全日参数',()=>withEnv({},async({w,root})=>{
    const paths=[];w.eval(fs.readFileSync('js/calendar_client.js','utf8'));w.BankClient.get=async path=>{paths.push(path);if(path.startsWith('/calendar?')){const p=Object.fromEntries(new w.URL('https://navren.test'+path).searchParams);return fixture(p,{view_id:'real-client-view'});}return {notice:'已核对',total:0,items:[],summary_by_currency:[],has_more:false};};
    await w.CalendarUI.renderDay(root,{date:'2026-10-03',source_scope:'simulation',filters:{kind:'returns',calendar_view_id:'old'}});await tick();const params=new w.URL('https://navren.test'+paths[0]).searchParams;assert.equal(params.get('kinds'),'');assert.equal(params.get('include_life_events'),'true');assert.equal(params.get('from'),params.get('to'));assert.equal(params.get('portfolio'),'current');assert(paths[1].includes('/real-client-view/days/2026-10-03?offset=0&limit=100'));
  }));
  await test('全日分页继续调用同一快照、不截断第101个事项',()=>withEnv({},async({w,root,calls})=>{
    w.CalendarClient.day=async(id,date,offset)=>{calls.day.push({id,date,offset});return {notice:'已核对',total:101,items:offset?[item({id:'last',title:'最后一个事项'})]:Array.from({length:100},(_,i)=>item({id:'i'+i,title:'事项'+i})),has_more:!offset,summary_by_currency:[]};};
    await w.CalendarUI.renderDay(root,{date:'2026-10-03',source_scope:'simulation',filters:{calendar_view_id:'old'}});await tick();findButton(root,'继续查看 · 共 101 项').click();await tick();assert.equal(root.querySelectorAll('.calendar-item').length,101);assert.equal(calls.day[1].offset,100);assert.equal(calls.day[0].id,calls.day[1].id);
  }));
  await test('全日来源/组合/账户身份不匹配时不读私人事项',async()=>{
    for(const mismatch of ['source','portfolio','workspace'])await withEnv({},async({w,root,calls})=>{w.CalendarClient.query=async p=>{const r=fixture(p);if(mismatch==='workspace')r.workspace_binding='other';else r.source_scope[mismatch==='source'?'statement_source':'portfolio']='wrong';return r;};await w.CalendarUI.renderDay(root,{date:'2026-10-03',source_scope:'simulation',filters:{calendar_view_id:'old'}});await tick();assert.equal(calls.day.length,0);assert(root.querySelector('[role="alert"]'));});
  });
  await test('同一连接容器换详情后旧事项不能插入',()=>withEnv({},async({w,root})=>{
    const request=deferred();w.CalendarClient.item=()=>request.promise;const p=w.CalendarUI.renderItem(root,'old',{});root.replaceChildren(w.document.createTextNode('新详情'));request.resolve(item({title:'上一账户私有事项'}));await p;assert.equal(root.textContent,'新详情');
  }));
  await test('仅视图代次变化也阻断迟到事项',()=>withEnv({},async({w,root,changeView})=>{
    const request=deferred();w.CalendarClient.item=()=>request.promise;const p=w.CalendarUI.renderItem(root,'old',{});changeView();request.resolve(item({title:'旧视图私有事项'}));await p;assert(!root.textContent.includes('旧视图私有事项'));
  }));
  await test('迟到产品响应不覆盖复用容器',()=>withEnv({},async({w,root})=>{
    const request=deferred();w.BankClient.get=()=>request.promise;const p=w.CalendarUI.renderProduct(root,'old-product','2026-10-03','history');root.replaceChildren(w.document.createTextNode('新产品视图'));request.resolve({product:{name:'旧产品'}});await p;assert.equal(root.textContent,'新产品视图');
  }));
  await test('初始化等待期间权限重置不会复活账户日历',()=>withEnv({},async({w,root,calls,changeIdentity})=>{
    const pending=deferred();w.BankClient.ensure=()=>pending.promise;const p=w.CalendarUI.account(root,{date:'2026-10-03'});changeIdentity();pending.resolve({workspace_id:'ws'});await p;assert.equal(calls.query.length,0);assert(root.textContent.includes('权限已变化'));w.dispatchEvent(new w.Event('navren:calendar-changed'));await tick();assert.equal(calls.query.length,0);
  }));
  await test('重复初始化只保留新controller并解绑销毁监听',()=>withEnv({},async({w,root,calls})=>{
    const first=deferred();let boots=0;w.BankClient.ensure=()=>++boots===1?first.promise:Promise.resolve({workspace_id:'ws'});const old=w.CalendarUI.account(root,{date:'2026-08-01'});await w.CalendarUI.account(root,{date:'2026-09-01'});const count=calls.query.length;first.resolve({workspace_id:'ws'});await old;assert.equal(calls.query.length,count);assert.equal(root.querySelector('[aria-label="定位日历日期"]').value,'2026-09-01');w.CalendarUI.destroyAccount(root);w.dispatchEvent(new w.Event('navren:calendar-changed'));await tick();assert.equal(calls.query.length,count);
  }));
  await test('初始化尚未完成时calendar-changed不会进入未就绪load',()=>withEnv({},async({w,root,calls})=>{
    const pending=deferred();let first=true;w.BankClient.ensure=()=>first?(first=false,pending.promise):Promise.resolve({workspace_id:'ws'});const p=w.CalendarUI.account(root,{date:'2026-10-03'});w.dispatchEvent(new w.Event('navren:calendar-changed'));await tick();assert.equal(calls.query.length,0);pending.resolve({workspace_id:'ws'});await p;assert.equal(calls.query.length,2);
  }));
  await test('月份请求乱序只保留最新月份结果',()=>withEnv({},async({w,root,calls})=>{
    await w.CalendarUI.account(root,{date:'2026-10-03'});const requests=[];w.CalendarClient.query=p=>{calls.query.push(p);const d=deferred();requests.push({p,d});return d.promise;};const input=root.querySelector('[aria-label="定位日历日期"]');input.value='2026-08-03';input.dispatchEvent(new w.Event('change'));input.value='2026-09-03';input.dispatchEvent(new w.Event('change'));
    requests[1].d.resolve(fixture(requests[1].p,{days:[{date:'2026-09-01',item_count:1,preview:[{title:'新月份'}],summary_by_currency:[]}]}));await tick();requests[0].d.resolve(fixture(requests[0].p,{days:[{date:'2026-08-01',item_count:1,preview:[{title:'旧月份'}],summary_by_currency:[]}]}));await tick();assert(root.textContent.includes('新月份'));assert(!root.textContent.includes('旧月份'));assert.equal(input.value,'2026-09-01');
  }));
  await test('账户身份无事件变化也阻断迟到月份结果',()=>withEnv({},async({w,root,changeIdentity})=>{
    await w.CalendarUI.account(root,{date:'2026-10-03'});const pending=deferred();let params;w.CalendarClient.query=p=>{params=p;return pending.promise;};const input=root.querySelector('[aria-label="定位日历日期"]');input.value='2026-09-03';input.dispatchEvent(new w.Event('change'));changeIdentity({workspace_id:'other',privacy_revision:2},false);pending.resolve(fixture(params,{days:[{date:'2026-09-01',item_count:1,preview:[{title:'旧账户月数据'}],summary_by_currency:[]}]}));await tick();assert(!root.textContent.includes('旧账户月数据'));
  }));
  await test('controller销毁时连等待身份检查的内部month挂载也失效',()=>withEnv({},async({w,root})=>{
    const pending=deferred();let ensures=0;w.BankClient.ensure=()=>++ensures===1?Promise.resolve({workspace_id:'ws'}):pending.promise;const p=w.CalendarUI.account(root,{date:'2026-10-03'});await tick();w.CalendarUI.destroyAccount(root);pending.resolve({workspace_id:'ws'});await p;assert.equal(root.querySelectorAll('.calendar-day,.calendar-metric').length,0);
  }));
  await test('刷新过程中权限重置不再开启旧日历弹层',()=>withEnv({},async({w,root,changeIdentity})=>{
    await w.CalendarUI.mount(root,widget(fixture()));const pending=deferred();w.CalendarClient.query=()=>pending.promise;findButton(root,'重新查询').click();changeIdentity();pending.resolve(fixture());await tick();assert.equal(w.document.querySelectorAll('dialog').length,0);
  }));
  await test('草案准备等待期间权限重置会关闭弹层并丢弃旧草案',()=>withEnv({},async({w,changeIdentity})=>{
    const pending=deferred();w.CalendarClient.prepare=()=>pending.promise;w.CalendarUI.form();const form=w.document.querySelector('form');form.dispatchEvent(new w.Event('submit',{cancelable:true}));changeIdentity();pending.resolve({action:'create',scope:'series',draft_id:'old',after:{title:'旧私有草案'}});await tick();assert.equal(w.document.querySelectorAll('dialog').length,0);assert(!w.document.body.textContent.includes('旧私有草案'));
  }));
  await test('确认回执迟到不在新身份组件宣称保存成功',()=>withEnv({},async({w,root,changeIdentity})=>{
    w.CalendarClient.draft=async()=>({status:'ready'});const pending=deferred();w.CalendarClient.confirm=()=>pending.promise;await w.CalendarUI.mount(root,{type:'calendar_event_draft',workspace_binding:'ws',draft:{draft_id:'old',action:'create',scope:'series'}});const confirm=findButton(root,'确认保存日历变更');confirm.click();changeIdentity();root.replaceChildren(w.document.createTextNode('新账户'));pending.resolve({committed:true});await tick();assert.equal(root.textContent,'新账户');
  }));
  await test('英文初始化同步翻译状态并保留来源/用户标题原文',()=>withEnv({language:'en',withI18n:true},async({w,root})=>{
    const days=[{date:'2026-10-01',item_count:1,preview:[{title:'我的中文生日计划'}],summary_by_currency:[{currency:'CNY',investment_pnl_minor:100}]}];await w.CalendarUI.mount(root,widget(fixture({}, {days,summary_by_currency:[{currency:'CNY',investment_pnl_minor:null,pnl_coverage:'partial'}]})));
    assert.equal(root.querySelector('.calendar-pnl-legend .pnl-positive').textContent,'Gain');assert.equal(root.querySelector('.calendar-metric .pnl-missing').textContent,'Missing data');assert.equal(root.querySelector('.calendar-day-title').textContent,'我的中文生日计划');assert.equal(root.querySelector('.calendar-day-pnl').textContent,'+1.00 CNY');
    w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(root.querySelector('.calendar-pnl-legend .pnl-positive').textContent,'盈利');assert.equal(root.querySelector('.calendar-day-pnl').textContent,'+1.00元');assert.equal(root.querySelector('.calendar-metric .pnl-missing').textContent,'数据缺失');
    w.NavrenI18n.setLanguage('en',false);assert.equal(root.querySelector('.calendar-day-pnl').textContent,'+1.00 CNY');
  }));
  await test('中文初始化后新词条可切英文且当日筛选说明可还原',()=>withEnv({withI18n:true},async({w,root})=>{
    await w.CalendarUI.renderDay(root,{date:'2026-10-03',source_scope:'simulation',filters:{kind:'returns',calendar_view_id:'old'}});await tick();w.NavrenI18n.setLanguage('en',false);assert(root.textContent.includes('Currently filtered events'));assert(root.textContent.includes('All events for this day'));w.NavrenI18n.setLanguage('zh-CN',false);assert(root.textContent.includes('当前筛选事项'));assert(root.textContent.includes('当日全部事项'));
  }));
  await test('颜色、触摸高度及窄屏内容保留为日历专属样式',async()=>{
    const css=fs.readFileSync('css/bank.css','utf8');assert(/\.calendar-widget \.pnl-positive[^}]+color:#bc5753/.test(css));assert(/\.calendar-widget \.pnl-negative[^}]+color:#368579/.test(css));assert(/\.calendar-widget \.pnl-zero[^}]+color:#84909d/.test(css));assert(css.includes('.calendar-widget .calendar-day .calendar-day-title,.calendar-widget .calendar-day .calendar-day-cash { display:block; white-space:normal; overflow:visible; }'));assert(css.includes('min-height:44px'));assert(css.includes('.mode-senior .calendar-widget .calendar-button'));
  });
  console.log('日历 UI '+checks+' 项隔离 DOM 回归通过（真实网络/桌面视觉未运行）');
})().catch(e=>{console.error(e);process.exitCode=1;});
