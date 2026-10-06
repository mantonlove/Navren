/* 禁网内存 DOM：使用真实控制器/i18n，三维库仅为行为替身，不代表实屏验证。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const base=path.resolve(__dirname,'../..'),read=file=>fs.readFileSync(path.join(base,file),'utf8');
const tick=()=>new Promise(resolve=>setTimeout(resolve,10));
let checks=0;
function check(name,fn){fn();checks++;}
function setup(){
  const dom=new JSDOM(read('mine.html'),{url:'https://navren.invalid/mine.html',runScripts:'outside-only'}),w=dom.window,d=w.document;
  const store=new Map(),posts=[],gets=[],instances=[],resize=[],network=[];
  const identity={workspace_id:'graph-fixture',privacy_revision:1};let generation=0,pending=null;
  const envelope=data=>({...data,...identity});
  const fixture=()=>envelope({version:'graph-exact-v1',personal_revision:7,as_of:'2026-10-06',source:'simulation',memory_enabled:true,
    period:{from:'2026-09-01',to_exclusive:'2026-10-01'},portfolio_version:'current-fixture',node_count_total:5,coverage:{gaps:[]},insights:[],metrics:[],related_knowledge:[],
    nodes:[
      {id:'account:one',kind:'account',label:'日常收支',currency:'CNY',amount_minor:100000,metric_family:'cash_stock',evidence_refs:[]},
      {id:'category:one',kind:'category',label:'日常收支',currency:'CNY',amount_minor:10000,transaction_count:10,metric_family:'period_支出',evidence_refs:[]},
      {id:'category:count',kind:'category',label:'无金额有笔数',currency:'CNY',amount_minor:null,transaction_count:5,metric_family:'period_支出',evidence_refs:[]},
      {id:'memory:quote',kind:'memory',label:'日常收支',quote:'日常收支',observation_id:'quote',status:'candidate',confirmation_token:'token-quote',evidence_refs:[]},
      {id:'holding:null',kind:'holding',label:'原始基金名称',currency:'CNY',amount_minor:0,metric_family:'cash_stock',returns:{daily_pnl_minor:null,unrealized_pnl_minor:0,closing_equity_minor:null},evidence_refs:[]}
    ],links:[
      {id:'edge-cny-small',source:'account:one',target:'category:one',predicate:'支付',currency:'CNY',amount_minor:10000,derivation_type:'observed',evidence_refs:[]},
      {id:'edge-cny-large',source:'account:one',target:'category:count',predicate:'支付',currency:'CNY',amount_minor:1000000,derivation_type:'observed',evidence_refs:[]},
      {id:'edge-usd',source:'account:one',target:'category:one',predicate:'支付',currency:'USD',unit:'minor',amount_minor:10000,derivation_type:'observed',evidence_refs:[]},
      {id:'edge-stock',source:'account:one',target:'holding:null',predicate:'支付',currency:'CNY',unit:'minor',amount_minor:10000,derivation_type:'observed',evidence_refs:[]},
      {id:'edge-daily',source:'account:one',target:'category:one',predicate:'支付',currency:'CNY',amount_minor:10000,period:{from:'2026-09-01',to_exclusive:'2026-09-02'},derivation_type:'observed',evidence_refs:[]},
      {id:'edge-confirmed',source:'account:one',target:'holding:null',predicate:'本人确认',currency:'CNY',unit:'minor',amount_minor:10000,derivation_type:'confirmed',evidence_refs:[]},
      {id:'edge-candidate',source:'memory:quote',target:'category:one',predicate:'<候选条件>',currency:'CNY',amount_minor:null,derivation_type:'candidate',evidence_refs:[]},
      {id:'edge-count',source:'account:one',target:'category:one',predicate:'笔数关系',currency:'CNY',unit:'count',amount_minor:10,derivation_type:'calculated',evidence_refs:[]},
      {id:'edge-scenario',source:'account:one',target:'holding:null',predicate:'规划关联',currency:'CNY',amount_minor:null,derivation_type:'scenario',evidence_refs:[]}
    ]});
  w.Storage={get:key=>store.get(key),set:(key,value)=>store.set(key,value)};
  w.STORAGE_KEYS={LANGUAGE:'navren_language'};w.getInterfaceMode=()=>d.body.className.includes('senior')?'senior':'classic';
  w.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
  w.structuredClone=structuredClone;w.confirm=()=>true;w.showToast=()=>{};w.NavrenHelp={button:()=>d.createElement('button')};
  for(const kind of ['fetch','XMLHttpRequest','WebSocket'])w[kind]=function(...args){network.push({kind,args});throw Error('网络已禁用');};
  w.ResizeObserver=class{constructor(fn){this.fn=fn;resize.push(this);}observe(){}disconnect(){}};
  w.NavrenNodeText=class{constructor(value,height=10){this.text=value;this.textHeight=height;this.material={};this.center={};}};
  w.ForceGraph3D=()=>host=>{
    const props={},instance={props,host,destroyed:false},api=new Proxy(instance,{get(target,key){
      if(key==='camera')return ()=>({fov:50});
      if(key==='d3Force')return ()=>({strength(){}});
      if(key==='_destructor')return ()=>{target.destroyed=true;};
      if(key in target)return target[key];
      return value=>{if(arguments.length===0)return props[key];props[key]=value;return api;};
    }});instances.push(instance);return api;
  };
  w.BankClient={ensure:async()=>({...identity}),refreshIdentity:async()=>({...identity}),identity:()=>({...identity}),
    guard:async()=>{const g=generation,p=identity.privacy_revision;return {valid:data=>g===generation&&p===identity.privacy_revision&&(!data||data.workspace_id===identity.workspace_id&&data.privacy_revision===p)};},
    get:async endpoint=>{gets.push(endpoint);if(endpoint.startsWith('/personal-graph'))return pending||fixture();if(endpoint==='/overview')return envelope({profile:{risk_level:2},as_of:'2026-10-06'});throw Error('未提供夹具 '+endpoint);},
    post:async(endpoint,payload)=>{posts.push({endpoint,payload});return envelope({});}};
  let current={page:'mine',view:'graph'};const renderers=new Map();
  w.NavrenViews={register:(page,renderer)=>renderers.set(page,renderer),update:metadata=>{current={...current,...metadata};},
    current:()=>current,open:async(view,metadata={})=>{current={page:'mine',view,...metadata};return renderers.get('mine')?.(current);},back(){},ask:async(question,ref)=>{posts.push({endpoint:'ask',payload:{question,ref}});}};
  w.closeModal=()=>{};w.renderRiskProfile=()=>{};w.renderArchive=()=>{};w.loadFinancialMemories=async()=>{};w.renderBookmarks=()=>{};
  return {dom,w,d,store,posts,gets,instances,resize,network,fixture,identity,changeIdentity(){generation++;identity.privacy_revision++;},setPending(p){pending=p;}};
}
async function graphChecks(){
  const ctx=setup(),{w,d,instances,posts}=ctx;
  try{
    w.eval(read('js/i18n.js'));w.eval(read('js/personal_graph.js'));await tick();
    const root=d.getElementById('mineGraphHost');d.getElementById('graph').hidden=false;
    Object.defineProperties(root,{clientWidth:{value:900},clientHeight:{value:480}});
    d.documentElement.style.setProperty('--font-size-base','15px');d.documentElement.style.setProperty('--ink','#14253a');d.documentElement.style.setProperty('--surface-card','#fff9ef');
    await w.NavrenGraph.init(root,{filters:{graph_view:'cashflow'}});await tick();
    const graphNode=id=>[...d.querySelectorAll('.fg-node')].find(n=>n.id==='fg-node-'+id);
    const change=(selector,value)=>{const control=root.querySelector(selector);control.value=value;control.dispatchEvent(new w.Event('change',{bubbles:true}));};
    check('二维节点键盘名称与独立非因果说明',()=>{
      assert.equal(graphNode('account:one').getAttribute('aria-label'),'日常收支');assert.equal(graphNode('account:one').getAttribute('tabindex'),'0');
      assert.match(root.querySelector('[data-causality]').textContent,/不证明因果/);
      assert.match(root.querySelector('[data-legend]').textContent,/同币种、金额单位、口径、期间和关系类型/);
    });
    graphNode('holding:null').dispatchEvent(new w.MouseEvent('click',{bubbles:true}));
    check('未知损益不使用计算零值状态',()=>{
      assert.equal(root.querySelector('.pnl-unknown').textContent,'未知');assert.equal(root.querySelectorAll('.pnl-zero').length,1);
      assert.match(root.querySelector('.pnl-zero').textContent,/0\.00/);assert.match(graphNode('holding:null').textContent,/0\.00/);
      assert.match(graphNode('category:count').textContent,/金额未知/);
    });
    graphNode('memory:quote').dispatchEvent(new w.MouseEvent('click',{bubbles:true}));
    change('[data-metric]','count');
    check('金额未知而笔数已知仍展示笔数及非最小面积',()=>{
      assert.match(graphNode('category:count').textContent,/5 笔/);assert.ok(Number(graphNode('category:count').querySelector('circle').getAttribute('r'))>16);
      assert.match(graphNode('holding:null').textContent,/笔数不适用/);
    });
    w.NavrenI18n.setLanguage('en');await tick();
    check('语言切换重排生成标签但保护本人原话和账户名称',()=>{
      assert.equal(graphNode('category:one').getAttribute('aria-label'),'Everyday cash flow');
      assert.equal(graphNode('account:one').getAttribute('aria-label'),'日常收支');
      assert.equal(root.querySelector('blockquote').textContent,'日常收支');assert.equal(root.querySelector('blockquote').getAttribute('translate'),'no');
      assert.match(graphNode('category:count').textContent,/5 transactions/);
      assert.match(root.querySelector('[data-causality]').textContent,/not proof of causation/);
    });
    change('[data-dimension]','3d');await tick();
    const instance=()=>instances.at(-1),label=id=>instance().props.nodeThreeObject(instance().props.graphData.nodes.find(n=>n.id===id));
    check('三维每个节点具有名称字幕且原名不改写',()=>{
      for(const n of instance().props.graphData.nodes)assert.ok(instance().props.nodeThreeObject(n).text);
      assert.match(label('category:one').text,/Everyday cash flow\n10 transactions/);
      assert.equal(label('memory:quote').text,'日常收支');assert.match(label('category:count').text,/5 transactions/);
      assert.match(root.querySelector('.fg-key').textContent,/All 3D connections are solid/);assert.doesNotMatch(root.querySelector('.fg-key').textContent,/dashed:/);
    });
    check('三维体积按笔数而非半径三次方夸大量差',()=>{
      const p=instance().props,find=id=>p.graphData.nodes.find(n=>n.id===id);
      assert.ok(Math.abs(p.nodeVal(find('category:count'))/p.nodeVal(find('category:one'))-.5)<1e-9);
      assert.match(p.nodeThreeObject({...find('category:count'),transaction_count:null}).text,/Count unknown/);
    });
    check('三维关系状态颜色与安全双语提示',()=>{
      const links=instance().props.graphData.links,color=instance().props.linkColor;
      assert.notEqual(color(links.find(e=>e.derivation_type==='observed')),color(links.find(e=>e.derivation_type==='confirmed')));
      assert.notEqual(color(links.find(e=>e.derivation_type==='candidate')),color(links.find(e=>e.derivation_type==='scenario')));
      assert.match(instance().props.linkLabel(links.find(e=>e.id==='edge-candidate')),/Unverified candidate/);
      assert.match(instance().props.linkLabel(links.find(e=>e.id==='edge-candidate')),/&lt;候选条件&gt;/);
    });
    check('真实主词典翻译22个受控关系谓词但不改本人原文',()=>{
      const expected={'发生于':'Transacted at','经济净收入':'Net economic income','经济净支出':'Net economic spending','持有本金':'Principal held','偿还责任来源':'Repayment funding source','结算后到账':'Credited after settlement','本人确认规划分配':'Planning allocation confirmed by you','预计到账':'Expected inflow','释放预留':'Reservation released','应付':'Amount due','可能扣费':'Possible debit','续费候选':'Possible renewal','形成收入':'Contributes to income','用于消费':'Used for consumption','构成资产':'Included in assets','抵减净资产':'Reduces net assets','提出条件':'States a condition','作为选择依据':'Used as a basis for the choice','本人关联计划':'Plan linked by you','本人关联承诺':'Commitment linked by you','已考虑的替代项':'Alternatives considered','选择并说明原因':'Choice with rationale'};
      const p=instance().props;
      for(const [zh,en]of Object.entries(expected))assert.equal(p.linkLabel({...p.graphData.links[0],predicate:zh}),en+' · Recorded fact');
      assert.equal(p.nodeThreeObject({...p.graphData.nodes[3],label:'发生于',quote:'发生于'}).text,'发生于');
    });
    change('[data-metric]','amount');await tick();
    check('线粗不跨币种、金额口径、关系类型或笔数单位比较',()=>{
      const p=instance().props,width=id=>p.linkWidth(p.graphData.links.find(e=>e.id===id));
      assert.ok(width('edge-cny-small')<width('edge-cny-large'));
      for(const id of ['edge-usd','edge-stock','edge-confirmed','edge-daily'])assert.equal(width(id),4,id);
      assert.equal(width('edge-count'),1);assert.equal(width('edge-candidate'),1);
      assert.equal(p.linkWidth({...p.graphData.links[0],source:'memory:quote',target:'holding:null'}),1,'无法界定端点口径保持细线');
    });
    const oldLabel=label('category:one'),oldCount=instances.length;
    check('三维字号换算与背景尺寸使用同一单位',()=>{
      assert.equal(oldLabel.material.sizeAttenuation,false);assert.equal(oldLabel.color,'#14253a');assert.equal(oldLabel.backgroundColor,'#fff9ef');
      assert.ok(Math.abs(oldLabel.textHeight-15*2*Math.tan(Math.PI*25/180)/480)<1e-9);
      assert.equal(oldLabel.padding,oldLabel.textHeight*.15);
    });
    check('金额字幕保留最大安全整数的最后一分',()=>{
      assert.match(instance().props.nodeThreeObject({...instance().props.graphData.nodes[1],amount_minor:Number.MAX_SAFE_INTEGER}).text,/CNY 90,071,992,547,409\.91/);
    });
    d.documentElement.style.setProperty('--font-size-base','20px');await tick();
    check('大字号重建三维标签且保持当前选中对象',()=>{
      assert.ok(instances.length>oldCount);assert.ok(label('category:one').textHeight>oldLabel.textHeight);
      assert.ok(Math.abs(label('category:one').textHeight/oldLabel.textHeight-20/15)<1e-9);
      assert.equal(root.querySelector('blockquote').textContent,'日常收支');assert.equal(w.NavrenViews.current().filters.selected_id,'memory:quote');
    });
    d.documentElement.style.setProperty('--ink','#e5efff');d.documentElement.style.setProperty('--surface-card','#172331');
    w.dispatchEvent(new w.CustomEvent('navren:theme'));await tick();
    check('主题和中文切换更新三维文字而不提交业务',()=>{
      assert.equal(label('category:one').color,'#e5efff');assert.equal(instance().props.backgroundColor,'#172331');
    });
    w.NavrenI18n.setLanguage('zh-CN');await tick();
    check('三维中文图例与原话回切',()=>{
      assert.match(label('category:one').text,/日常收支/);assert.match(root.querySelector('.fg-key').textContent,/三维连线均为实线/);
      assert.equal(root.querySelector('blockquote').textContent,'日常收支');assert.equal(posts.length,0);
    });
    d.body.className='mode-senior';await tick();change('[data-dimension]','2d');await tick();
    check('大字号二维几何与语义字号同步',()=>{
      assert.equal(graphNode('category:one').querySelector('text').getAttribute('font-size'),'20');
      assert.match(root.querySelector('.fg-key').textContent,/二维实线/);
    });
    const count=instances.length;ctx.changeIdentity();w.NavrenI18n.setLanguage('en');await tick();
    check('身份改变后设置事件不重绘旧缓存',()=>assert.equal(instances.length,count));
    w.dispatchEvent(new w.Event('navren:privacy-reset'));await tick();
    check('权限重置立即清空旧图谱且设置事件不能复活',()=>{
      w.dispatchEvent(new w.CustomEvent('navren:theme'));assert.equal(root.querySelector('.fg-svg'),null);assert.equal(root.querySelector('blockquote'),null);
    });
    let release;ctx.setPending(new Promise(resolve=>release=resolve));const late=w.NavrenGraph.refresh();await tick();ctx.changeIdentity();w.NavrenGraph.invalidate();
    release({...ctx.fixture(),privacy_revision:ctx.identity.privacy_revision-1});await late;ctx.setPending(null);
    check('迟到图谱响应不能恢复旧权限正文',()=>assert.equal(root.querySelector('.fg-svg'),null));
    await w.NavrenGraph.restore({filters:{graph_view:'wealth',dimension:'3d'}});await tick();
    w.dispatchEvent(new w.PageTransitionEvent('pagehide',{persisted:true}));w.dispatchEvent(new w.PageTransitionEvent('pageshow',{persisted:true}));w.NavrenI18n.setLanguage('zh-CN');await tick();
    check('缓存返回在核对身份前保持清空',()=>{assert.equal(root.querySelector('blockquote'),null);assert.equal(root.querySelector('.fg-svg'),null);assert.ok(instance().destroyed);});
    check('所有语义/样式操作无业务提交和真实网络',()=>{assert.equal(posts.length,0);assert.equal(ctx.network.length,0);});
  }finally{ctx.dom.window.close();}
}
async function mineChecks(){
  const ctx=setup(),{w,d,store,posts}=ctx;
  try{
    w.NavrenGraph={pause(){},invalidate(){}};w.eval(read('js/i18n.js'));w.eval(read('js/mine_workspace.js'));await tick();
    await w.NavrenMine.render({page:'mine',view:'profile'});await tick();w.NavrenI18n.setLanguage('en');await tick();
    check('个人资料原话和本人标题独立排除翻译',()=>{
      assert.equal(d.querySelector('#mineObservationList blockquote').textContent,'日常收支');
      assert.equal(d.querySelector('#mineObservationList blockquote').getAttribute('translate'),'no');
      assert.equal(d.querySelector('#mineObservationList h3').textContent,'日常收支');
    });
    w.NavrenI18n.setLanguage('zh-CN');await tick();
    // DOMContentLoaded 已结束后载入真实 mine.js，不以替身冒充收藏的完整 DOM 接线。
    w.eval(read('js/mine.js'));
    const historical={kind:'event',id:'historic-event',version:4,source_scope:'public',label:'归档热点',archive:{edition_date:'2026-10-01',revision:2,event_version:4}};
    const archivedURL=new URL(w.NavrenMine.readingHref(historical),'https://navren.invalid');
    check('继续阅读携带确切归档日期版次和事件版本',()=>{
      assert.equal(archivedURL.searchParams.get('edition_date'),'2026-10-01');assert.equal(archivedURL.searchParams.get('revision'),'2');
      assert.equal(archivedURL.searchParams.get('object_version'),'4');assert.equal(archivedURL.searchParams.get('object_id'),'historic-event');
      assert.equal(archivedURL.searchParams.get('source_scope'),'public_market');
    });
    const bookmark={object_kind:'event',object_id:'historic-event',version:4,archive:{...historical.archive}};
    check('收藏与继续阅读共享历史身份合同',()=>assert.equal(w.NavrenMine.readingHref(bookmark),w.NavrenMine.readingHref(historical)));
    check('缺失或矛盾的归档身份不得打开当前正文',()=>{
      for(const archive of [{edition_date:'2026-10-01',revision:2},{edition_date:'2026-02-30',revision:2,event_version:4},{edition_date:'2026-10-01',revision:0,event_version:4},{edition_date:'2026-10-01',revision:2,event_version:5},{edition_date:'2026-10-01',revision:true,event_version:4},{edition_date:'2026-10-01',revision:'2',event_version:4},{edition_date:'2026-10-01',revision:2,event_version:'4'},{edition_date:'2026-10-01',revision:2,event_version:true}])assert.equal(w.NavrenMine.readingHref({...historical,archive}),null);
      assert.equal(w.NavrenMine.readingHref({...historical,version:'4'}),null);
      assert.equal(w.NavrenMine.readingHref({...historical,source_scope:'simulation'}),null);
    });
    check('公开产品与周报恢复准确类型来源',()=>{
      const product=new URL(w.NavrenMine.readingHref({kind:'public_product',id:'fund-v1',version:'nav-exact-v1',source_scope:'public',product_source:'public_nav'}),'https://navren.invalid');
      assert.equal(product.searchParams.get('view'),'product');assert.equal(product.searchParams.get('object_kind'),'product');assert.equal(product.searchParams.get('source_scope'),'public_nav');assert.equal(product.searchParams.get('object_version'),'nav-exact-v1');
      assert.equal(w.NavrenMine.readingHref({kind:'public_product',id:'fund-v1',version:1,source_scope:'public'}),null);
      const weekly=new URL(w.NavrenMine.readingHref({kind:'weekly',id:'2026-09-28',version:2,source_scope:'public'}),'https://navren.invalid');assert.equal(weekly.searchParams.get('view'),'weekly');assert.equal(weekly.searchParams.get('object_version'),'2');
    });
    store.set('navren_reading_history',[historical,{...historical,id:'incomplete',archive:{edition_date:'2026-10-01',revision:2}}]);
    store.set('qingzhou_bookmarks',[{...bookmark,category:'market_news',text:'历史收藏原文'},{...bookmark,object_id:'incomplete',category:'market_news',text:'缺失归档',archive:{edition_date:'2026-10-01',revision:2}},{category:'market_weekly',object_kind:'weekly',object_id:'2026-09-28',version:2,text:'周报收藏原文'}]);
    await w.NavrenMine.render({page:'mine',view:'saved'});await tick();
    check('继续阅读 DOM 显示归档身份及不可用原因',()=>{
      const list=d.getElementById('mineContinueReading');assert.equal(list.querySelectorAll('a').length,1);
      assert.match(list.querySelector('a').href,/edition_date=2026-10-01/);assert.match(list.textContent,/归档日 2026-10-01 · 日版次 2 · 事件版本 4/);
      assert.match(list.textContent,/保存的来源或历史版本不完整/);assert.equal(list.querySelector('a').getAttribute('translate'),'no');
    });
    check('真实 mine.js 收藏入口使用同一归档合同',()=>{
      const saved=d.getElementById('bookmarksList'),a=saved.querySelector('#mine-bookmark-0');
      assert.equal(a.getAttribute('href'),w.NavrenMine.readingHref(historical));assert.equal(saved.querySelector('#mine-bookmark-1'),null);
      assert.match(saved.textContent,/保存的来源或历史版本不完整/);assert.match(saved.querySelector('#mine-bookmark-2').href,/view=weekly/);
      assert.equal(saved.querySelector('article p').getAttribute('translate'),'no');
    });
    check('删除一项收藏后其他原归档仍可准确继续阅读',()=>{
      const row=[...d.querySelectorAll('#bookmarksList article')].find(x=>x.textContent.includes('缺失归档'));row.querySelector('button').click();
      assert.equal(store.get('qingzhou_bookmarks').length,2);
      assert.equal(d.querySelector('#mine-bookmark-0').getAttribute('href'),w.NavrenMine.readingHref(historical));
      assert.match(d.querySelector('#mine-bookmark-1').href,/view=weekly/);
      assert.equal(d.getElementById('mineContinueReading').querySelectorAll('a').length,1);
    });
    w.NavrenI18n.setLanguage('en');await tick();
    check('真实收藏和归档身份标签切英文而原文保留',()=>{
      assert.match(d.getElementById('mineContinueReading').textContent,/Archive date 2026-10-01 · Daily revision 2 · Event version 4/);
      assert.equal(d.getElementById('mineContinueReading').querySelector('a').textContent,'归档热点');
      assert.match(d.getElementById('bookmarksList').textContent,/历史收藏原文/);
    });
    check('个人资料与阅读链接不自动提交确认或提问',()=>{assert.equal(posts.length,0);assert.equal(ctx.network.length,0);});
  }finally{ctx.dom.window.close();}
}
(async()=>{await graphChecks();await mineChecks();console.log(`图谱语义及历史阅读通过：${checks} 组；纯内存禁网 DOM，三维库替身，未验证实屏/WebGL。`);})().catch(error=>{console.error(error);process.exitCode=1;});
