/* ACCOUNT-DATA-COMPLETE：实际前端消费者＋可控异步账号快照；无HTTP、数据库或模型。 */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM,VirtualConsole}=require('jsdom');
const project=path.resolve(__dirname,'../..'),tick=()=>new Promise(resolve=>setImmediate(resolve));
const clone=value=>value==null?value:JSON.parse(JSON.stringify(value));
const stable=value=>value&&typeof value==='object'?Array.isArray(value)?'['+value.map(stable).join(',')+']':'{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+stable(value[key])).join(',')+'}':JSON.stringify(value);
const binding={registered_user_id:'a'.repeat(32),workspace_id:'1'.repeat(32),privacy_revision:0,auth_subject:'registered:'+('a'.repeat(32))};
const article={id:'a1',version:1,publisher:'国家统计局',title:'公开原文',url:'https://www.stats.gov.cn/sj/zxfb/202610/t20261014_1965449.html',published_at:'2026-10-14T01:30:00Z',published_precision:'minute',status:'active',read_scope:'full_text',link_status:'verified',access_status:'free_full_text'};
const marketEvent={id:'event1',version:2,status:'published',availability:'active',primary_article_id:article.id,headline:'公开事件',summary:'已核对的公开摘要。',category:'macro_policy',theme:'macro',articles:[article],facts:[],archive:{edition_date:'2026-10-14',revision:1,event_version:2}};
const weekly={period:'2026-10-05',period_start:'2026-10-05T00:00:00+08:00',period_end_exclusive:'2026-10-12T00:00:00+08:00',version:3,status:'published',released_at:'2026-10-16T08:30:00Z',title:'市场周报',summary:'已核验摘要。',changes:[],source_articles:[article]};

function fixture({ready=true,widgets=false}={}){
  const errors=[],vc=new VirtualConsole();vc.on('jsdomError',error=>errors.push(error));
  const dom=new JSDOM('<body>'+(widgets?'<div id="marketHotHeading"></div><div id="marketHot"></div><div id="weeklyReport"></div>':'')+'</body>',{url:'https://navren.test/recommend.html',runScripts:'outside-only',virtualConsole:vc}),w=dom.window;
  const state={ready,identity:{...binding},owned:null,revision:0,hold:false,fail:false,pending:[],notices:[],reads:[],writes:[],rawHints:[],network:[],networkHold:false,networkPending:[],beforeMutation:null,store:new Map([['qingzhou_bookmarks',[]],['navren_reading_history',[]],['qingzhou_preferences',{marketHotVisible:true,weeklyReport:true}]])};
  const emit=(status,keys=[])=>w.dispatchEvent(new w.CustomEvent('navren:account-state',{detail:{status,keys}}));
  const nativeGet=w.Storage.prototype.getItem,nativeSet=w.Storage.prototype.setItem;
  nativeSet.call(w.localStorage,'navren_legacy_plan_workspace',binding.workspace_id);
  w.Storage.prototype.getItem=function(key){if(key==='navren_legacy_plan_workspace')state.rawHints.push(['get',key]);return nativeGet.call(this,key);};
  w.Storage.prototype.setItem=function(key,value){if(key==='navren_legacy_plan_workspace')state.rawHints.push(['set',key]);return nativeSet.call(this,key,value);};
  w.NavrenAccountData={itemKey:stable};
  w.Storage={accountReady:()=>state.ready,ready:async()=>{if(!state.ready)throw Error('ACCOUNT_STATE_NOT_READY');},privateIdentity:()=>clone(state.owned),get:key=>{state.reads.push(key);return state.ready?clone(state.store.get(key)??null):null;},update:async(key,mutate)=>{
    if(!state.ready)throw Error('ACCOUNT_STATE_NOT_READY');if(state.fail)throw Error('ACCOUNT_STATE_VERSION_CONFLICT');
    const identity=stable(state.identity),revision=state.revision;if(state.beforeMutation){const hook=state.beforeMutation;state.beforeMutation=null;hook();}
    const value=mutate(clone(state.store.get(key)??null));state.writes.push({key,value:clone(value)});
    const commit=()=>{if(!state.ready||identity!==stable(state.identity))throw Error('STALE_ACCOUNT_STATE');if(revision!==state.revision)throw Error('ACCOUNT_STATE_VERSION_CONFLICT');state.store.set(key,value);state.revision++;emit('ready',[key]);};
    if(state.hold)await new Promise((resolve,reject)=>state.pending.push({resolve:()=>{try{commit();resolve(true);}catch(error){reject(error);}},reject}));else commit();return true;
  }};
  w.BankClient={identity:()=>clone(state.identity)};w.showToast=text=>state.notices.push(text);w.NavrenHelp={button:()=>w.document.createElement('button')};
  w.setInterval=()=>1;w.NavrenViews={current:()=>({view:'home'})};w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');this.dispatchEvent(new w.Event('close'));};
  w.MarketDeck=class {constructor(root,options){this.root=root;this.renderCard=options.renderCard;this.state='idle';this.update(options.events);}update(events){this.cards=new Map(events.map(event=>[event.id,{card:this.renderCard(event)}]));this.root.replaceChildren(...[...this.cards.values()].map(row=>row.card));}destroy(){this.root.replaceChildren();}};
  w.fetch=async url=>{state.network.push(url);if(!widgets)throw Error('NETWORK_FORBIDDEN');const data=url.endsWith('/hotspots')?{edition_date:'2026-10-14',server_today:'2026-10-14',server_now:'2026-10-14T12:00:00+08:00',events:[clone(marketEvent)]}:{report:null,status:'no_verified_weekly',expected_period:'2026-10-05',history:[]};if(state.networkHold)await new Promise(resolve=>state.networkPending.push(resolve));return {ok:true,json:async()=>data};};
  w.eval(fs.readFileSync(process.env.NAVREN_MARKET_SOURCE||path.join(project,'js/market_ui.js'),'utf8'));
  let source=fs.readFileSync(process.env.NAVREN_DISCOVER_SOURCE||path.join(project,'js/discover_workspace.js'),'utf8');
  const marker='routeMetadata,recordMarketReading };';assert(source.includes(marker),'受检发现组件导出边界必须明确，不能静默跳过私有函数检查');
  // 仅在内存副本暴露既有函数；正式源码没有测试出口。
  source=source.replace(marker,'routeMetadata,recordMarketReading,__accountTest:{recordReading,recordHistoryReading,legacyPlans} };');w.eval(source);
  return {w,state,emit,async close(){state.ready=false;emit('unbound');for(const pending of state.pending.splice(0))pending.reject(Error('fixture_closed'));for(const resolve of state.networkPending.splice(0))resolve();w.dispatchEvent(new w.Event('pagehide'));await tick();assert.equal(errors.length,0,errors.map(error=>error.stack).join('\n'));dom.window.close();}};
}
async function withFixture(options,run){const f=fixture(options);try{await tick();await run(f);}finally{await f.close();}}
const cases=[];
async function check(name,run){const start=Date.now();try{await run();cases.push({name,status:'passed',elapsed_ms:Date.now()-start});console.log('PASS '+name);}catch(error){cases.push({name,status:'failed',elapsed_ms:Date.now()-start,error:error.stack});throw error;}}

(async()=>{
  await check('legacy-owned-partition-only',()=>withFixture({},async({w,state,emit})=>{
    state.store.set('qingzhou_allocation',{summary:'旧研究记录',allocation:[{name:'历史名称'}],_history:[{summary:'更旧记录',allocation:[{name:'历史条目'}]}]});const read=w.DiscoverWorkspace.__accountTest.legacyPlans;
    assert.deepEqual(Array.from(read()),[],'旧raw hint即便吻合也不能证明归属');assert.equal(state.reads.filter(key=>key==='qingzhou_allocation').length,0);
    for(const [key,value] of [['registered_user_id','b'.repeat(32)],['workspace_id','2'.repeat(32)],['privacy_revision',1],['auth_subject','registered:other']]){state.owned={...state.identity,[key]:value};assert.equal(read().length,0,'四字段任一不匹配均拒绝：'+key);}
    state.owned={...state.identity};const plans=read();assert.equal(plans.length,2);assert(plans.every(plan=>plan.source_scope==='browser_history'&&plan.id.startsWith('legacy-')));assert.equal(state.rawHints.length,0,'不得读写raw归属hint');assert.equal(state.writes.length,0,'旧研究记录不静默持久迁移');
    state.ready=false;emit('unbound');state.ready=true;emit('ready');assert.equal(read().length,0,'保留失效后legacyVisible关闭边界');
  }));
  await check('favorites-delay-failure-confirmed-control',()=>withFixture({},async({w,state})=>{
    const root=w.MarketUI.weeklyCard({report:weekly,expected_period:weekly.period}),save=root.querySelector('[data-weekly-action="save"]');w.document.body.append(root);state.hold=true;save.click();assert.equal(save.getAttribute('aria-pressed'),'false');assert(save.disabled);assert.equal(state.store.get('qingzhou_bookmarks').length,0);assert.equal(state.notices.length,0,'无确认不能提示成功');state.pending.shift().resolve();await tick();assert.equal(save.getAttribute('aria-pressed'),'true');assert(state.notices.includes('已收藏'));
    state.hold=false;state.fail=true;const before=stable(state.store.get('qingzhou_bookmarks')),success=state.notices.filter(text=>text==='已收藏'||text==='已取消收藏').length;save.click();await tick();assert.equal(stable(state.store.get('qingzhou_bookmarks')),before);assert.equal(save.getAttribute('aria-pressed'),'true');assert.equal(state.notices.filter(text=>text==='已收藏'||text==='已取消收藏').length,success);assert(state.notices.at(-1).includes('CONFLICT'));
  }));
  await check('favorites-stable-delete-and-500-no-truncation',()=>withFixture({},async({w,state})=>{
    const privateRows=Array.from({length:499},(_,n)=>({text:'私人财务'+n,workspace_id:binding.workspace_id,privacy_revision:0}));state.store.set('qingzhou_bookmarks',clone(privateRows));assert.equal(await w.MarketUI.toggleBookmark('event',marketEvent),true);assert.equal(state.store.get('qingzhou_bookmarks').length,500);
    assert.equal(await w.MarketUI.toggleBookmark('event',{...marketEvent,id:'event2'}),false);assert.equal(state.store.get('qingzhou_bookmarks').length,500);assert(state.notices.at(-1).includes('500'));
    state.beforeMutation=()=>state.store.get('qingzhou_bookmarks').reverse();assert.equal(await w.MarketUI.toggleBookmark('event',marketEvent),true);assert.equal(state.store.get('qingzhou_bookmarks').length,499);assert.deepEqual(state.store.get('qingzhou_bookmarks').map(row=>row.text).sort(),privateRows.map(row=>row.text).sort(),'删除按稳定原件身份，重排不能删到私人类别');
  }));
  await check('public-history-shared-across-persona',()=>withFixture({},async({w,state})=>{
    const ref={kind:'weekly',id:'2026-09-28',version:1,time_view:'historical_audit'};await w.MarketUI.historyBookmark(ref,'公开旧周报');const rows=state.store.get('qingzhou_bookmarks');assert.equal(rows.length,1);assert(!Object.hasOwn(rows[0],'workspace_id'));assert(!Object.hasOwn(rows[0],'privacy_revision'));state.identity={...state.identity,workspace_id:'2'.repeat(32)};await w.MarketUI.historyBookmark(ref,'公开旧周报');assert.equal(state.store.get('qingzhou_bookmarks').length,1);assert.equal(state.store.get('qingzhou_bookmarks')[0].version,1);
  }));
  await check('reading-complete-reference-identity-and-public-payload',()=>withFixture({},async({w,state})=>{
    const api=w.DiscoverWorkspace.__accountTest;await api.recordReading('public_product','same','v1','目录产品','public_catalog');await api.recordReading('public_product','same','v1','历史净值','public_nav');await api.recordReading('public_product','same','v2','新目录','public_catalog');await api.recordReading('public_product','same','v2','再次阅读','public_catalog');assert.equal(state.store.get('navren_reading_history').length,3);
    await w.DiscoverWorkspace.recordMarketReading(marketEvent);await w.DiscoverWorkspace.recordMarketReading({...marketEvent,archive:{...marketEvent.archive,revision:2}});await api.recordHistoryReading({kind:'article',id:'event1',version:2,time_view:'historical_audit'},'明确旧版');const rows=state.store.get('navren_reading_history');assert.equal(rows.length,6);assert.equal(rows[0].time_view,'historical_audit');assert.equal(rows.filter(row=>row.archive).length,2);assert(rows.every(row=>!Object.hasOwn(row,'workspace_id')&&!Object.hasOwn(row,'privacy_revision')));
    state.hold=true;const before=stable(rows),delayed=api.recordReading('event','delayed',1,'延迟阅读');assert.equal(stable(state.store.get('navren_reading_history')),before);state.pending.shift().reject(Error('offline'));assert.equal(await delayed,false);assert.equal(stable(state.store.get('navren_reading_history')),before);assert(state.notices.at(-1).includes('阅读记录未保存'));
  }));
  await check('reading-200-retains-beyond-old-20-cap',()=>withFixture({},async({w,state})=>{
    state.store.set('navren_reading_history',Array.from({length:200},(_,n)=>({kind:'event',id:'old'+n,version:1,source_scope:'public_market',label:'旧记录'+n,updated_at:'2026-10-01T00:00:00Z'})));await w.DiscoverWorkspace.recordMarketReading(marketEvent);const rows=state.store.get('navren_reading_history');assert.equal(rows.length,200);assert.equal(rows[0].id,marketEvent.id);assert(rows.some(row=>row.id==='old150'));assert(!rows.some(row=>row.id==='old199'));assert.equal(rows.filter(row=>row.id===marketEvent.id).length,1);
  }));
  await check('account-state-preferences-wait-confirm-and-cross-tab',()=>withFixture({ready:false,widgets:true},async({w,state,emit})=>{
    state.store.set('qingzhou_preferences',{marketHotVisible:false,weeklyReport:false});await tick();const hot=w.document.getElementById('marketHot');assert(hot.textContent.includes('尚未载入'));assert(!hot.textContent.includes('你已关闭'));assert.equal(state.network.length,0);
    state.ready=true;emit('ready');await tick();assert(hot.textContent.includes('你已关闭市场热点'));state.hold=true;hot.querySelector('button').click();assert(hot.textContent.includes('你已关闭市场热点'));assert.equal(state.network.length,0);state.pending.shift().reject(Error('offline'));await tick();assert(hot.textContent.includes('你已关闭市场热点'));assert.equal(state.store.get('qingzhou_preferences').marketHotVisible,false);
    state.hold=false;hot.querySelector('button').click();await tick();assert(hot.textContent.includes('公开事件'));const save=hot.querySelector('[data-bookmark-id]');assert.equal(save.getAttribute('aria-pressed'),'false');state.store.set('qingzhou_bookmarks',[{category:'market_news',object_kind:'event',object_id:marketEvent.id,version:marketEvent.version,archive:clone(marketEvent.archive),text:'公开事件'}]);state.revision++;emit('ready',['qingzhou_bookmarks']);assert.equal(save.getAttribute('aria-pressed'),'true','账号事件同步确认收藏，不依赖裸storage键');
    state.store.set('qingzhou_preferences',{marketHotVisible:false,weeklyReport:false});state.revision++;emit('ready',['qingzhou_preferences']);await tick();assert(hot.textContent.includes('你已关闭市场热点'));state.ready=false;emit('unavailable');assert(hot.textContent.includes('尚未载入'));assert(!hot.textContent.includes('公开事件'));
  }));
  await check('late-favorite-and-market-read-discarded-on-account-change',()=>withFixture({widgets:true},async({w,state,emit})=>{
    state.hold=true;const adding=w.MarketUI.toggleBookmark('event',marketEvent);const pending=state.pending.shift(),success=state.notices.length;state.ready=false;emit('unbound');state.identity={...state.identity,registered_user_id:'b'.repeat(32),workspace_id:'2'.repeat(32),auth_subject:'registered:'+('b'.repeat(32))};state.store.set('qingzhou_bookmarks',[]);state.ready=true;emit('ready');pending.resolve();assert.equal(await adding,false);assert.equal(state.notices.length,success);assert.equal(state.store.get('qingzhou_bookmarks').length,0);
    state.networkHold=true;const reading=w.MarketUI.refreshMarket();state.ready=false;emit('unbound');for(const resolve of state.networkPending.splice(0))resolve();await reading;assert(w.document.getElementById('marketHot').textContent.includes('尚未载入'));assert(!w.document.getElementById('marketHot').textContent.includes('公开事件'));
  }));
  console.log(JSON.stringify({schema:'navren.account-data-market-consumer-checks/1',status:'passed',cases,network:'controlled in-memory public response stubs only',actual_http_requests:0,browser_operations:0,production_db_writes:0,paid_calls:0}));
})().catch(error=>{console.error(error.stack);console.log(JSON.stringify({schema:'navren.account-data-market-consumer-checks/1',status:'failed',cases,error:error.stack}));process.exitCode=1;});
