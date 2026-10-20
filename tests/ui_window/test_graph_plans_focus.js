/* 真实图谱控制器的禁网 JSDOM 验证；金额和日期由冻结夹具提供，不代表实屏或银行接入。 */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const base=path.resolve(__dirname,'../..'),read=file=>fs.readFileSync(path.join(base,file),'utf8');
const tick=()=>new Promise(resolve=>setTimeout(resolve,10));
const passed=[];
function check(name,fn){fn();passed.push(name);}

function graphFixture(identity,overrides={}){
  const ref=(table,id,date)=>({source_scope:'simulation',source_table:table,source_id:id,...(date?{occurred_at:date}:{})});
  const accounts=[
    {id:'account:everyday',kind:'account',label:'日常账户',amount_minor:7000000,currency:'CNY',metric_family:'cash_stock',evidence_refs:[ref('accounts','everyday')]},
    {id:'account:reserve',kind:'account',label:'储备账户',amount_minor:40000000,currency:'CNY',metric_family:'cash_stock',evidence_refs:[ref('accounts','reserve')]}
  ];
  const goals=[
    {id:'goal:unallocated',kind:'goal',label:'应急储备',amount_minor:30000000,target_minor:30000000,saved_minor:18000000,currency:'CNY',metric_family:'goal_target',due_on:'2027-10-01',evidence_refs:[ref('wealth_goals','unallocated')]},
    {id:'goal:allocated',kind:'goal',label:'进修计划',amount_minor:4000000,target_minor:4000000,saved_minor:4000000,currency:'CNY',metric_family:'goal_target',due_on:'2027-09-01',evidence_refs:[ref('wealth_goals','allocated')]}
  ];
  const events=[],links=[];
  function event(id,date,amount,options={}){
    const account_id=options.account_id||'everyday',currency=options.currency||'CNY',certainty=options.certainty||'candidate';
    const derivation_type=certainty==='confirmed'?'confirmed':'candidate',source_table=options.source_table||'subscriptions',source_id=options.source_id||'commute-series';
    const cash_delta_minor=options.cash_delta_minor??-amount,event_type=options.event_type||'subscription';
    const n={id:'event:'+id,kind:'event',label:options.label||'个人日常交通支出（未付预测）',amount_minor:amount,currency,metric_family:'future_obligation',date,account_id,certainty,derivation_type,status:certainty==='confirmed'?'active':'candidate',cash_delta_minor,event_type,source_table,source_id,evidence_refs:[ref(source_table,source_id,date)]};
    if(options.noSource){delete n.source_id;delete n.source_table;n.evidence_refs=[];}
    events.push(n);links.push({id:'edge:'+id,source:'account:'+account_id,target:n.id,predicate:cash_delta_minor>0?'预计到账':certainty==='confirmed'?'应付':'可能扣费',amount_minor:Math.abs(cash_delta_minor),currency,derivation_type,occurred_at:date,evidence_refs:n.evidence_refs});
    return n;
  }
  // 十一个真正独立的周期记录，名称相同，金额有分的差异；不能改写成一个虚构事件。
  for(let i=0;i<11;i++)event('commute-'+i,'2026-10-'+String(10+i).padStart(2,'0'),120001+i);
  event('other-contract','2026-10-10',210005,{source_id:'another-commute-contract'});
  event('another-account','2026-10-10',130001,{account_id:'reserve'});
  event('another-currency','2026-10-11',120003,{currency:'USD'});
  event('confirmed-commute','2026-10-12',120004,{certainty:'confirmed'});
  event('inflow-commute','2026-10-13',120006,{cash_delta_minor:120006});
  event('no-source-1','2026-10-14',120008,{noSource:true});
  event('no-source-2','2026-10-15',120009,{noSource:true});
  for(let i=0;i<7;i++)event('unique-'+i,'2026-10-'+String(22+i).padStart(2,'0'),220011+i,{label:'独立付款事项 '+i,source_id:'unique-contract-'+i});
  links.push({id:'edge:allocation',source:'account:reserve',target:'goal:allocated',predicate:'本人确认规划分配',amount_minor:4000000,currency:'CNY',derivation_type:'confirmed',evidence_refs:[ref('goal_allocations','allocation-real')]});
  const insights=[
    {id:'insight:goal-empty',view:'plans',title:'应急储备的资金约束',node_ids:['goal:unallocated'],amount_minor:0,target_minor:30000000,currency:'CNY',due_on:'2027-10-01',coverage:'confirmed_allocations_only',evidence_refs:goals[0].evidence_refs,notice:'仅列本人确认的规划分配；不是账户余额。'},
    {id:'insight:goal-funded',view:'plans',title:'进修计划的资金约束',node_ids:['goal:allocated','account:reserve'],amount_minor:4000000,target_minor:4000000,currency:'CNY',coverage:'confirmed_allocations_only',evidence_refs:goals[1].evidence_refs}
  ];
  const timeline={horizon_days:90,as_of:'2026-10-08',horizon_end:'2027-01-06',totals_by_currency:{CNY:{confirmed_outflow_minor:120004,candidate_outflow_minor:3440187,confirmed_inflow_minor:0,candidate_inflow_minor:120006},USD:{confirmed_outflow_minor:0,candidate_outflow_minor:120003,confirmed_inflow_minor:0,candidate_inflow_minor:0}},accounts:[
    {account_id:'everyday',account_name:'日常账户',currency:'CNY',current_available_minor:7000000,minimum_available_after_confirmed_minor:6754996,minimum_available_if_candidates_occur_minor:3220029},
    {account_id:'reserve',account_name:'储备账户',currency:'CNY',current_available_minor:40000000,minimum_available_after_confirmed_minor:40000000,minimum_available_if_candidates_occur_minor:39869999}
  ],timeline:[
    {date:'2026-10-10',events:[],accounts:[{account_id:'reserve',currency:'CNY',available_after_confirmed_minor:40000000,available_if_candidates_occur_minor:39869999}]},
    {date:'2026-10-12',events:[],accounts:[{account_id:'everyday',currency:'CNY',available_after_confirmed_minor:6754996,available_if_candidates_occur_minor:5000000}]},
    {date:'2026-10-28',events:[],accounts:[{account_id:'everyday',currency:'CNY',available_after_confirmed_minor:6754996,available_if_candidates_occur_minor:3220029}]}
  ],notice:'同一账户的未来时间轴。'};
  return {...identity,version:'plans-fixture-v1',personal_revision:7,as_of:'2026-10-08',source:'simulation',memory_enabled:true,period:{from:'2026-09-01',to_exclusive:'2026-10-01'},portfolio:null,portfolio_version:'fixture-current',node_count_total:accounts.length+goals.length+events.length,coverage:{gaps:[]},metrics:[],calculations:[],related_knowledge:[],nodes:[...accounts,...goals,...events],links,insights,timeline,...overrides};
}

function setup(){
  const dom=new JSDOM(read('mine.html'),{url:'https://navren.invalid/mine.html',runScripts:'outside-only'}),w=dom.window,d=w.document;
  const identity={workspace_id:'plans-focus-fixture',privacy_revision:1};let generation=0,pending=null,fixture=graphFixture(identity),current={page:'mine',view:'graph',filters:{}};
  const store=new Map(),network=[],posts=[],gets=[],observers=[];
  const NativeMutationObserver=w.MutationObserver;
  w.MutationObserver=class extends NativeMutationObserver{constructor(callback){super(callback);observers.push(this);}};
  w.Storage={get:key=>store.get(key),set:(key,value)=>store.set(key,value)};w.STORAGE_KEYS={LANGUAGE:'navren_language'};
  w.getInterfaceMode=()=>d.body.className.includes('senior')?'senior':'classic';w.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
  w.structuredClone=structuredClone;w.confirm=()=>true;w.showToast=()=>{};w.NavrenHelp={button:()=>d.createElement('button')};
  for(const kind of ['fetch','XMLHttpRequest','WebSocket'])w[kind]=function(...args){network.push({kind,args});throw Error('夹具禁止真实网络');};
  w.ResizeObserver=class{observe(){}disconnect(){}};
  w.BankClient={ensure:async()=>({...identity}),refreshIdentity:async()=>({...identity}),identity:()=>({...identity}),guard:async()=>{const g=generation,p=identity.privacy_revision;return {valid:value=>g===generation&&p===identity.privacy_revision&&(!value||value.workspace_id===identity.workspace_id&&value.privacy_revision===p)};},get:async endpoint=>{gets.push(endpoint);if(endpoint.startsWith('/personal-graph'))return pending||structuredClone(fixture);throw Error('夹具没有提供 '+endpoint);},post:async(endpoint,payload)=>{posts.push({endpoint,payload});return {...identity};}};
  w.NavrenViews={update:metadata=>{current={...current,...metadata};},current:()=>current,open:async(view,metadata={})=>{current={page:'mine',view,...metadata};return w.NavrenGraph.restore(current);},ask:async(question,ref)=>posts.push({endpoint:'ask',payload:{question,ref}})};
  const root=d.getElementById('mineGraphHost');d.getElementById('graph').hidden=false;
  Object.defineProperties(root,{clientWidth:{value:900},clientHeight:{value:480}});
  return {dom,w,d,root,posts,gets,network,identity,observers,fixture:()=>fixture,setFixture:value=>fixture=value,setPending:value=>pending=value,changeIdentity(){generation++;identity.privacy_revision++;}};
}

async function run(){
  const ctx=setup(),{w,d,root}=ctx;
  const control=s=>{const c=root.querySelector(s);assert.ok(c,'缺少用户控件 '+s);return c;};
  const inspector=()=>control('.fg-inspector');
  const nodeButton=id=>[...root.querySelectorAll('svg .fg-network-node[data-node-id]')].find(n=>n.dataset.nodeId===id);
  const edge=id=>[...root.querySelectorAll('svg .fg-network-edge[data-edge-id]')].find(n=>n.dataset.edgeId===id);
  const series=()=>[...root.querySelectorAll('svg .fg-network-node[data-series-id]')];
  const allIDs=()=>series().map(s=>JSON.parse(s.dataset.eventIds));
  const activate=async node=>{assert.ok(node,'待激活对象不存在');node.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));await tick();};
  const clickNode=async id=>activate(nodeButton(id));
  const more=async()=>{for(let i=0;i<12&&root.querySelector('[data-network-action="more"]');i++){root.querySelector('[data-network-action="more"]').click();await tick();}};
  const focus=async id=>{
    if(!nodeButton(id)){const reset=root.querySelector('[data-network-action="reset"]');if(reset){reset.click();await tick();}await more();}
    await clickNode(id);control('[data-network-action="focus"]').click();await tick();
  };
  try{
    w.eval(read('js/i18n.js'));w.eval(read('js/personal_graph.js'));await tick();await w.NavrenGraph.init(root,{filters:{graph_view:'plans',network_focus:'account:everyday'}});await tick();
    check('摘要读取服务确认付款和当前账户最低值，币种分列且不偷换为候选值',()=>{
      const cards=[...root.querySelectorAll('.fg-plan-summary-card')];assert.equal(cards.length,3);
      assert.ok(cards[0].textContent.includes('按币种查看'));assert.ok(cards[0].textContent.includes('1,200.04 元'));assert.ok(cards[0].textContent.includes('0.00 USD'));
      assert.ok(cards[1].textContent.includes('67,549.96 元'));assert.ok(cards[1].textContent.includes('2026-10-12'));assert.ok(cards[1].textContent.includes('日常账户'));
      assert.ok(!cards[1].textContent.includes('32,200.29'),'候选最低值不能冒充仅确认最低值');
    });
    check('聚焦当前账户后其他账户事项不混入一跳范围，预测与确认边状态不同',()=>{
      assert.equal(w.NavrenViews.current().filters.network_focus,'account:everyday');assert.equal(nodeButton('event:another-account'),undefined);
      assert.ok(root.querySelector('svg.fg-svg'));assert.ok(root.querySelector('[data-network-action="more"]'));
      const confirmed=edge('edge:confirmed-commute');assert.ok(confirmed);assert.equal(confirmed.querySelectorAll('path')[1].getAttribute('stroke-dasharray'),'7 4');
      const candidate=series()[0];assert.ok(candidate);assert.equal(candidate.querySelector('circle').getAttribute('stroke-dasharray'),'3 4');
    });
    const before=root.querySelectorAll('svg .fg-network-node').length;
    check('初次展示限制图节点数量而非铺开所有未来周期',()=>assert.ok(before<=9,'初次展示节点数 '+before));
    await more();
    const recurrence=series().find(s=>JSON.parse(s.dataset.eventIds).includes('event:commute-0'));
    check('周期折叠保存十一项真实记录，不同账户币种状态和方向不误并',()=>{
      assert.ok(recurrence,'同源周期组应可展开');assert.deepEqual(JSON.parse(recurrence.dataset.eventIds),Array.from({length:11},(_,i)=>'event:commute-'+i));
      for(const group of allIDs()){
        const originals=group.map(id=>ctx.fixture().nodes.find(n=>n.id===id));assert.ok(originals.every(Boolean),'每个组成员必须是真实节点');
        const keys=new Set(originals.map(n=>JSON.stringify([n.account_id,n.currency,n.certainty,n.source_table,n.source_id,n.event_type,Math.sign(n.cash_delta_minor)])));assert.equal(keys.size,1,'周期折叠不得跨财务口径');
        assert.ok(originals.every(n=>n.source_id&&n.source_table),'没有可信同源字段时保持独立');
      }
      assert.ok(nodeButton('event:confirmed-commute'));assert.ok(nodeButton('event:another-currency'));assert.ok(nodeButton('event:other-contract'));assert.ok(nodeButton('event:inflow-commute'));assert.ok(nodeButton('event:no-source-1'));assert.ok(nodeButton('event:no-source-2'));
    });
    await activate(recurrence);
    check('展开周期逐项保留原始日期与最后一分，不生成汇总伪事件',()=>{
      assert.equal(recurrence.dataset.nodeId,undefined);assert.equal(inspector().querySelector('[data-plan-ask]'),null);
      for(let i=0;i<11;i++){const n=ctx.fixture().nodes.find(n=>n.id==='event:commute-'+i),b=inspector().querySelector('[data-occurrence-id="'+n.id+'"]');assert.ok(b);assert.ok(b.textContent.includes(n.date),n.id+' 日期');assert.ok(b.textContent.includes('1,200.'+String(i+1).padStart(2,'0')),n.id+' 金额');}
    });
    inspector().querySelector('[data-occurrence-id="event:commute-5"]').click();await tick();
    check('逐期选择使用真实对象，详情金额日期与选择一致',()=>{
      assert.equal(w.NavrenViews.current().filters.selected_id,'event:commute-5');assert.ok(inspector().textContent.includes('2026-10-15'));assert.ok(inspector().textContent.includes('1,200.06'));assert.ok(inspector().textContent.includes('待核实'));
    });
    control('[data-plan-ask]').click();await tick();
    check('提问绑定真实事件和当前来源版本，不把折叠组当金融对象',()=>{
      const asked=ctx.posts.at(-1);assert.equal(asked.endpoint,'ask');assert.equal(asked.payload.ref.id,'event:commute-5');assert.equal(asked.payload.ref.parameters.node_id,'event:commute-5');assert.equal(asked.payload.ref.source_scope,'simulation');assert.equal(asked.payload.ref.version,'plans-fixture-v1');assert.equal(asked.payload.ref.parameters.expected_version,'plans-fixture-v1');
    });
    await focus('goal:unallocated');
    check('目标未确认分配0元明确是分配口径，不推断账户缺钱',()=>{
      const content=root.textContent;assert.ok(content.includes('应急储备'));assert.ok(content.includes('0.00'));assert.ok(content.includes('确认')&&content.includes('分配'));
      assert.equal(root.querySelectorAll('svg .fg-network-edge').length,0);assert.doesNotMatch(inspector().textContent,/缺钱|缺口|余额不足/);assert.doesNotMatch(inspector().textContent,/1,200\.06|2026-10-15/);
    });
    await focus('goal:allocated');
    const funding=nodeButton('account:reserve');assert.ok(funding,'目标已确认来源账户必须可进入');
    check('相关账户的SVG圆节点可键盘激活，保留标准交互语义',()=>{assert.equal(funding.namespaceURI,'http://www.w3.org/2000/svg');assert.equal(funding.getAttribute('role'),'button');assert.equal(funding.getAttribute('tabindex'),'0');assert.ok(funding.querySelector('circle'));});
    await activate(funding);
    check('进入相关资金账户清除旧目标详情',()=>{assert.ok(inspector().textContent.includes('储备账户'));assert.doesNotMatch(inspector().textContent,/应急储备/);});
    await focus('account:everyday');
    let release;ctx.setPending(new Promise(resolve=>release=resolve));const updating=w.NavrenGraph.refresh();await tick();
    check('刷新核对期间立即清除旧金额与旧依据',()=>{assert.equal(inspector().textContent,'');assert.equal(root.querySelector('[data-plan-ask]'),null);assert.equal(root.querySelector('svg .fg-network-node'),null);});
    const next=graphFixture(ctx.identity,{version:'plans-fixture-v2'});next.nodes=next.nodes.filter(n=>n.id!=='account:everyday'&&n.account_id!=='everyday');next.links=next.links.filter(e=>e.source!=='account:everyday');next.timeline.accounts=next.timeline.accounts.filter(a=>a.account_id!=='everyday');
    release(next);await updating;ctx.setPending(null);ctx.setFixture(next);await tick();
    check('刷新后不存在的焦点回到当前真实对象，没有上一账户残留',()=>{assert.notEqual(w.NavrenViews.current().filters.network_focus,'account:everyday');assert.equal(nodeButton('account:everyday'),undefined);assert.doesNotMatch(inspector().textContent,/日常账户|1,200\.06/);});
    ctx.changeIdentity();w.NavrenGraph.invalidate();await tick();
    check('权限改变立即清图，主题与语言事件不能复活旧个人资金',()=>{
      w.dispatchEvent(new w.CustomEvent('navren:theme'));w.NavrenI18n.setLanguage('en');assert.equal(root.querySelector('svg .fg-network-node'),null);assert.equal(inspector().textContent,'');
    });
    const unknown=graphFixture(ctx.identity);unknown.timeline.accounts[0].minimum_available_after_confirmed_minor=null;ctx.setFixture(unknown);w.NavrenI18n.setLanguage('zh-CN');
    await w.NavrenGraph.restore({filters:{graph_view:'plans',plan_focus:'account:everyday',network_focus:'account:everyday'}});await tick();
    check('未提供可用最低值时显示未知，日期不被补成当前日',()=>{
      const card=root.querySelectorAll('.fg-plan-summary-card')[1];assert.ok(card.querySelector('strong').textContent.includes('未知'));assert.ok(!card.textContent.includes('0.00'));assert.ok(!card.querySelector('small').textContent.includes('2026-10-08'));
    });
    const noDate=graphFixture(ctx.identity);noDate.timeline.timeline=[];ctx.setFixture(noDate);await w.NavrenGraph.refresh();await tick();
    check('最低金额缺少可核验日期时保留金额但不虚构时点',()=>{const card=root.querySelectorAll('.fg-plan-summary-card')[1];assert.ok(card.textContent.includes('67,549.96'));assert.ok(!card.querySelector('small').textContent.includes('2026-10-08'));assert.ok(!card.querySelector('small').textContent.includes('2026-10-12'));});
    let releaseLate;ctx.setPending(new Promise(resolve=>releaseLate=resolve));const stale=w.NavrenGraph.refresh();await tick();const staleResponse=graphFixture(ctx.identity);ctx.changeIdentity();w.NavrenGraph.invalidate();releaseLate(staleResponse);await stale;ctx.setPending(null);await tick();
    check('账户或权限切换后的迟到响应不能恢复旧个人关系或提问入口',()=>{assert.equal(root.querySelector('svg.fg-svg'),null);assert.equal(root.querySelector('[data-plan-ask]'),null);assert.equal(inspector().textContent,'');});
    const legacy=graphFixture(ctx.identity);
    for(const node of legacy.nodes.filter(n=>n.kind==='event')){
      for(const name of ['account_id','cash_delta_minor','event_type','source_table','source_id','derivation_type','status'])delete node[name];
      for(const ref of node.evidence_refs)ref.source_table='recurring_obligations';
    }
    ctx.setFixture(legacy);await w.NavrenGraph.restore({filters:{graph_view:'plans',plan_focus:'account:everyday',network_focus:'account:everyday'}});await tick();await more();
    const oldSeries=series().find(s=>JSON.parse(s.dataset.eventIds).includes('event:commute-0'));await activate(oldSeries);
    check('旧服务响应凭唯一同源依据与真实账户关系可安全折叠，保持原ID日期金额',()=>{
      assert.deepEqual(JSON.parse(oldSeries.dataset.eventIds),Array.from({length:11},(_,i)=>'event:commute-'+i));
      const occurrence=inspector().querySelector('[data-occurrence-id="event:commute-5"]');assert.ok(occurrence.textContent.includes('2026-10-15'));assert.ok(occurrence.textContent.includes('1,200.06'));
      assert.ok(nodeButton('event:other-contract'));assert.ok(nodeButton('event:inflow-commute'));assert.ok(nodeButton('event:no-source-1'));assert.ok(nodeButton('event:no-source-2'));
    });
    const ambiguous=structuredClone(legacy);ambiguous.nodes.find(n=>n.id==='event:commute-0').evidence_refs.push({source_scope:'simulation',source_table:'recurring_obligations',source_id:'conflicting-source'});ambiguous.links.find(e=>e.target==='event:commute-1').predicate='无法确认资金方向';
    ctx.setFixture(ambiguous);await w.NavrenGraph.refresh();await tick();await more();
    check('旧响应存在多个来源ID或无法核验资金方向时拒绝误并',()=>{
      for(const id of ['event:commute-0','event:commute-1']){assert.ok(nodeButton(id));assert.ok(allIDs().every(ids=>!ids.includes(id)),id+' 不应进入任何周期组');}
    });
    ctx.setFixture(graphFixture(ctx.identity));w.NavrenI18n.setLanguage('zh-CN');await w.NavrenGraph.restore({filters:{graph_view:'wealth'}});await tick();await more();
    check('财富视图使用四域SVG渲染，计划残留焦点与周期展示不侵入其他板块',()=>{assert.ok(root.querySelector('svg.fg-svg'));assert.equal(root.dataset.graphView,'wealth');assert.equal(root.querySelector('svg .fg-network-node[data-series-id]'),null);assert.ok([...root.querySelectorAll('.fg-node')].some(n=>n.getAttribute('aria-label')==='储备账户'));});
    check('查看、聚焦、展开与设置不产生业务操作或真实网络',()=>{assert.equal(ctx.posts.filter(p=>p.endpoint!=='ask').length,0);assert.equal(ctx.network.length,0);assert.equal(ctx.posts.length,1);});
    console.log(JSON.stringify({status:'passed',checks:passed.length,passed,scope:'真实控制器、冻结夹具、禁网内存DOM；未验证实屏或真实金融数据'},null,2));
  }finally{for(const observer of ctx.observers)observer.disconnect();ctx.dom.window.close();}
}
run().catch(error=>{console.error(error);console.error(JSON.stringify({status:'failed',checks_passed:passed.length,passed},null,2));process.exitCode=1;});
