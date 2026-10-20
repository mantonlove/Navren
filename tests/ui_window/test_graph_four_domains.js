/* 四域真实 SVG 图谱控制器验证。冻结夹具、禁网 JSDOM；不验证实屏布局或银行接入。 */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const base=path.resolve(__dirname,'../..'),read=file=>fs.readFileSync(path.join(base,file),'utf8');
const tick=()=>new Promise(resolve=>setTimeout(resolve,10));
const passed=[];
function check(name,fn){fn();passed.push(name);}
const kinds=['cashflow','wealth','plans','preferences'];

function fixture(domain,identity){
  const ref=(table,id,date)=>({source_scope:'simulation',source_table:table,source_id:id,...(date?{occurred_at:date}:{})});
  const nodes=[],links=[];
  const node=(id,kind,label,extra={})=>{const n={id,kind,label,currency:'CNY',metric_family:'cash_stock',amount_minor:120001,evidence_refs:[ref(kind==='account'?'accounts':'fixture_records',id)],...extra};nodes.push(n);return n;};
  const edge=(id,a,b,extra={})=>{const e={id,source:a.id,target:b.id,predicate:'构成资产',amount_minor:120001,currency:'CNY',derivation_type:'observed',evidence_refs:[ref('fixture_records',id)],...extra};links.push(e);return e;};
  let anchor;
  if(domain==='cashflow')anchor=node('cash:surplus','concept','本期日常结余',{role:'surplus',amount_minor:-762311,metric_family:'period_surplus'});
  if(domain==='wealth')anchor=node('wealth:net','concept','净资产',{role:'net_assets',amount_minor:180000000,metric_family:'net_assets_principal'});
  if(domain==='plans')anchor=node('account:everyday','account','日常账户',{amount_minor:7000000});
  if(domain==='preferences')anchor=node('memory:condition','memory','本人条件',{role:'condition',quote:'我希望保留充足的备用资金。',status:'confirmed',amount_minor:undefined});
  const sourceKind=domain==='preferences'?'memory':domain==='wealth'?'holding':'category';
  for(let i=0;i<14;i++){
    const n=node(domain+':direct-'+i,sourceKind,'独立事项 '+i,{amount_minor:100001+i,metric_family:domain==='cashflow'?'period_支出':'cash_stock',...(domain==='preferences'?{quote:'本人资料 '+i,status:i===1?'candidate':'confirmed',amount_minor:undefined}:{})});
    edge(domain+':edge-'+i,anchor,n,{predicate:domain==='cashflow'?'用于消费':domain==='preferences'?'作为选择依据':'构成资产',amount_minor:domain==='preferences'?null:100001+i,derivation_type:i===1?'candidate':'observed'});
  }
  const far=node(domain+':two-hop','goal','只与另一节点相连的目标',{target_minor:20000000,amount_minor:20000000,metric_family:'goal_target'});
  edge(domain+':two-hop-edge',nodes.find(n=>n.id===domain+':direct-0'),far,{predicate:'本人关联计划',derivation_type:'confirmed'});
  node(domain+':unconnected','account','独立无关系账户',{amount_minor:99999999});
  const unknown=node(domain+':unknown','holding','未知金额持仓',{amount_minor:null,returns:{daily_pnl_minor:null,unrealized_pnl_minor:0,closing_equity_minor:null}});
  const zero=node(domain+':zero','holding','明确零余额',{amount_minor:0,returns:{daily_pnl_minor:0,unrealized_pnl_minor:0,closing_equity_minor:0}});
  edge(domain+':unknown-edge',anchor,unknown,{amount_minor:null});edge(domain+':zero-edge',anchor,zero,{amount_minor:0});
  if(domain==='plans'){
    function event(id,date,options={}){
      const account=options.account||anchor.id,source=options.source||'commute-A',currency=options.currency||'CNY',certainty=options.certainty||'candidate',delta=options.delta??-120001;
      const refs=options.noSource?[]:[ref('recurring_obligations',source,date)];
      const n=node(id,'event','每月交通支出（未付预测）',{date,amount_minor:Math.abs(delta),currency,metric_family:'future_obligation',account_id:account.slice(8),source_table:options.noSource?undefined:'recurring_obligations',source_id:options.noSource?undefined:source,event_type:'monthly_expense',cash_delta_minor:delta,certainty,status:certainty==='candidate'?'candidate':'active',derivation_type:certainty,evidence_refs:refs});
      const a=nodes.find(n=>n.id===account);edge('edge:'+id,a,n,{predicate:delta>0?'预计到账':certainty==='confirmed'?'应付':'可能扣费',currency,amount_minor:Math.abs(delta),derivation_type:certainty,evidence_refs:refs});
      return n;
    }
    for(const [i,date]of ['2026-10-12','2026-11-12','2026-12-12'].entries())event('event:commute-A:'+i,date,{delta:-120001-i});
    for(const [i,date]of ['2026-10-13','2026-11-13'].entries())event('event:commute-B:'+i,date,{source:'commute-B',delta:-210005-i});
    event('event:confirmed','2026-10-14',{certainty:'confirmed'});
    event('event:inflow','2026-10-15',{delta:120006});
    event('event:usd','2026-10-16',{currency:'USD'});
    event('event:no-source-1','2026-10-17',{noSource:true});event('event:no-source-2','2026-10-18',{noSource:true});
    const second=node('account:reserve','account','储备账户',{amount_minor:40000000});
    event('event:another-account','2026-10-19',{account:second.id});
    const goal=node('goal:emergency','goal','应急储备',{target_minor:30000000,saved_minor:18000000,amount_minor:30000000,metric_family:'goal_target',due_on:'2027-10-01',evidence_refs:[ref('wealth_goals','emergency')]});
    // 目标没有分配边，saved_minor 不能变成一条推断的账户关系或已确认分配。
    return {...identity,version:'four-domains-plans-v1',personal_revision:7,as_of:'2026-10-08',source:'simulation',memory_enabled:true,period:{from:'2026-09-01',to_exclusive:'2026-10-01'},portfolio:null,portfolio_version:'fixture-current',node_count_total:nodes.length,coverage:{gaps:[]},metrics:[],calculations:[],related_knowledge:[],nodes,links,insights:[{id:'insight:goal-empty',view:'plans',title:'应急储备的资金约束',node_ids:[goal.id],amount_minor:0,target_minor:30000000,currency:'CNY',due_on:'2027-10-01',coverage:'confirmed_allocations_only',evidence_refs:goal.evidence_refs}],timeline:{as_of:'2026-10-08',horizon_end:'2027-01-06',horizon_days:90,totals_by_currency:{CNY:{confirmed_outflow_minor:120001},USD:{confirmed_outflow_minor:0}},accounts:[{account_id:'everyday',account_name:'日常账户',currency:'CNY',current_available_minor:7000000,minimum_available_after_confirmed_minor:6879999,minimum_available_if_candidates_occur_minor:5179931},{account_id:'reserve',account_name:'储备账户',currency:'CNY',current_available_minor:40000000,minimum_available_after_confirmed_minor:40000000,minimum_available_if_candidates_occur_minor:39879999}],timeline:[{date:'2026-10-14',events:[],accounts:[{account_id:'everyday',currency:'CNY',available_after_confirmed_minor:6879999,available_if_candidates_occur_minor:5179931}]}]}};
  }
  return {...identity,version:'four-domains-'+domain+'-v1',personal_revision:7,as_of:'2026-10-08',source:'simulation',memory_enabled:true,period:{from:'2026-09-01',to_exclusive:'2026-10-01'},portfolio:null,portfolio_version:'fixture-current',node_count_total:nodes.length,coverage:{gaps:[]},metrics:[],calculations:[],related_knowledge:[],nodes,links,insights:[{id:'insight:'+domain,view:domain,title:domain==='cashflow'?'日常收支与结余':domain==='wealth'?'资产与负债':'本人条件与依据',node_ids:[anchor.id],amount_minor:domain==='preferences'?null:anchor.amount_minor,currency:'CNY',evidence_refs:anchor.evidence_refs}]};
}

function setup(){
  const dom=new JSDOM(read('mine.html'),{url:'https://navren.invalid/mine.html',runScripts:'outside-only'}),w=dom.window,d=w.document;
  const identity={workspace_id:'four-domains-fixture',privacy_revision:1};let generation=0,pending=null,current={page:'mine',view:'graph',filters:{}};
  const store=new Map(),network=[],posts=[],gets=[],observers=[],fixtures=new Map(kinds.map(kind=>[kind,fixture(kind,identity)]));
  const NativeMutationObserver=w.MutationObserver;
  w.MutationObserver=class extends NativeMutationObserver{constructor(callback){super(callback);observers.push(this);}};
  w.Storage={get:key=>store.get(key),set:(key,value)=>store.set(key,value)};w.STORAGE_KEYS={LANGUAGE:'navren_language'};
  w.getInterfaceMode=()=>d.body.className.includes('senior')?'senior':'classic';w.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
  w.structuredClone=structuredClone;w.confirm=()=>true;w.showToast=()=>{};w.NavrenHelp={button:()=>d.createElement('button')};
  for(const kind of ['fetch','XMLHttpRequest','WebSocket'])w[kind]=function(...args){network.push({kind,args});throw Error('四域夹具禁止真实网络');};
  w.ResizeObserver=class{observe(){}disconnect(){}};
  w.BankClient={ensure:async()=>({...identity}),refreshIdentity:async()=>({...identity}),identity:()=>({...identity}),guard:async()=>{const g=generation,p=identity.privacy_revision;return {valid:value=>g===generation&&p===identity.privacy_revision&&(!value||value.workspace_id===identity.workspace_id&&value.privacy_revision===p)};},get:async endpoint=>{gets.push(endpoint);if(endpoint.startsWith('/personal-graph')){const kind=new URL(endpoint,'https://navren.invalid').searchParams.get('view');return pending||structuredClone(fixtures.get(kind)||fixtures.get('cashflow'));}if(endpoint.startsWith('/graph/evidence?'))return {...identity,record:{name:'确切来源',amount_minor:120001,currency:'CNY'}};throw Error('没有提供夹具 '+endpoint);},post:async(endpoint,payload)=>{posts.push({endpoint,payload});return {...identity};}};
  w.NavrenViews={update:metadata=>{current={...current,...metadata};},current:()=>current,open:async(view,metadata={})=>{current={page:'mine',view,...metadata};return w.NavrenGraph.restore(current);},ask:async(question,ref)=>posts.push({endpoint:'ask',payload:{question,ref}})};
  const root=d.getElementById('mineGraphHost');d.getElementById('graph').hidden=false;
  Object.defineProperties(root,{clientWidth:{value:900},clientHeight:{value:480}});
  return {dom,w,d,root,posts,gets,network,identity,fixtures,observers,setPending:value=>pending=value,changeIdentity(){generation++;identity.privacy_revision++;}};
}

async function run(){
  const ctx=setup(),{w,d,root}=ctx;
  const control=s=>{const c=root.querySelector(s);assert.ok(c,'缺少真实用户控件 '+s);return c;};
  const svgNodes=()=>[...root.querySelectorAll('svg .fg-network-node[data-node-id]')];
  const svgEdges=()=>[...root.querySelectorAll('svg .fg-network-edge[data-edge-id]')];
  const displayedIDs=()=>svgNodes().map(n=>n.dataset.nodeId);
  const node=id=>svgNodes().find(n=>n.dataset.nodeId===id);
  const edge=id=>svgEdges().find(e=>e.dataset.edgeId===id);
  const activate=async element=>{assert.ok(element,'待激活对象不存在');element.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));await tick();};
  const click=async element=>{assert.ok(element,'待点击对象不存在');element.dispatchEvent(new w.MouseEvent('click',{bubbles:true}));await tick();};
  const action=name=>root.querySelector('[data-network-action="'+name+'"]');
  const more=async()=>{for(let i=0;i<10&&action('more');i++)await click(action('more'));};
  const inspector=()=>control('.fg-inspector');
  const ask=async()=>{const b=[...inspector().querySelectorAll('button')].find(b=>/^(问 Navren|Ask Navren)$/.test(b.textContent.trim()));await click(b);return ctx.posts.at(-1).payload.ref;};
  const restore=async kind=>{await w.NavrenGraph.restore({filters:{graph_view:kind,dimension:'2d'}});await tick();};
  const snapshot=JSON.stringify([...ctx.fixtures]);
  try{
    w.eval(read('js/i18n.js'));w.eval(read('js/personal_graph.js'));await tick();await w.NavrenGraph.init(root,{filters:{graph_view:'cashflow'}});await tick();
    for(const kind of kinds){
      await click(control('.fg-view-tab[data-view="'+kind+'"]'));const f=ctx.fixtures.get(kind);
      check(kind+' 使用 SVG 圆节点和真实关系边，而非清单替代图谱',()=>{
        assert.equal(root.dataset.graphView,kind);assert.ok(root.querySelector('svg.fg-svg'));
        const activeTab=control('.fg-view-tab[data-view="'+kind+'"]');assert.equal(w.NavrenViews.current().filters.graph_view,kind);assert.equal(activeTab.getAttribute('aria-selected'),'true');
        assert.ok(svgNodes().length>1);assert.ok(svgEdges().length>0);
        assert.equal(root.querySelector('.fg-plan-map'),null,'旧版列表图不能保留为主体');
        for(const n of svgNodes()){assert.ok(f.nodes.some(x=>x.id===n.dataset.nodeId));assert.ok(Number(n.querySelector('circle')?.getAttribute('r'))>0);assert.equal(n.getAttribute('tabindex'),'0');}
        for(const e of svgEdges()){assert.ok(f.links.some(x=>x.id===e.dataset.edgeId));assert.ok(e.querySelector('path'));assert.equal(e.getAttribute('tabindex'),'0');}
        if(kind==='cashflow'){assert.ok(inspector().textContent.includes('期间收支结余'));assert.ok(!inspector().textContent.includes('period_surplus'));}
      });
      check(kind+' 默认限制可见节点，并提供渐进展开而不删除原资料',()=>{
        assert.ok(svgNodes().length+root.querySelectorAll('svg .fg-network-node[data-series-id]').length<=11,'默认投影不能铺开全部节点');assert.ok(action('more'));
      });
      if(kind==='wealth'){
        w.NavrenI18n.setLanguage('en');await tick();const englishDetail=inspector().textContent;w.NavrenI18n.setLanguage('zh-CN');await tick();
        check('财富域初始详情对应真实中心与已知金额，本金口径使用中英可读名称',()=>{
          assert.equal(inspector().querySelector('h3').textContent,'净资产');assert.equal(w.NavrenViews.current().filters.selected_id,'wealth:net');assert.ok(inspector().textContent.includes('1,800,000.00 元'));assert.ok(!inspector().textContent.includes('金额：未知'));
          assert.ok(inspector().textContent.includes('已覆盖本金净额'));assert.ok(englishDetail.includes('Covered principal net amount'));assert.ok(!inspector().textContent.includes('net_assets_principal'));assert.ok(!englishDetail.includes('net_assets_principal'));
        });
      }
      if(kind==='plans')check('目标域顶部资金摘要对应图中真实中心账户，并直接读取后台最低值',()=>{
        const centers=svgNodes().filter(n=>n.dataset.networkAnchor==='true');assert.equal(centers.length,1,'必须唯一标识来源中的真实中心节点');const center=centers[0];
        assert.ok(center);const actual=f.nodes.find(n=>n.id===center.dataset.nodeId),card=root.querySelectorAll('.fg-plan-summary-card')[1];
        assert.ok(card.textContent.includes(actual.label));assert.ok(card.textContent.includes('68,799.99 元'));assert.ok(!card.textContent.includes('51,799.31 元'),'候选情景不能混入仅确认最低值');
      });
      await more();
      check(kind+' 展开全图后真实节点完整可达，选择会保留真实 ID',()=>{
        if(kind!=='plans')assert.deepEqual(displayedIDs().sort(),f.nodes.map(n=>n.id).sort());
        assert.ok(node(kind+':direct-0'));assert.ok(node(kind+':two-hop'));
      });
      const target=node(kind+':direct-0');await activate(target);
      check(kind+' 键盘选择节点显示确切名称金额和版本引用',()=>{
        assert.equal(inspector().querySelector('h3').textContent,'独立事项 0');assert.equal(w.NavrenViews.current().filters.selected_id,kind+':direct-0');
        if(kind!=='preferences')assert.ok(inspector().textContent.includes('1,000.01 元'));
      });
      const nodeRef=await ask();
      check(kind+' 提问绑定真正节点的来源和图谱版本',()=>{
        assert.equal(nodeRef.kind,'graph');assert.equal(nodeRef.id,kind+':direct-0');assert.equal(nodeRef.source_scope,'simulation');assert.equal(nodeRef.version,f.version);assert.equal(nodeRef.parameters.node_id,nodeRef.id);assert.equal(nodeRef.parameters.expected_version,f.version);
      });
      await activate(edge(kind+':edge-0'));const edgeRef=await ask();
      check(kind+' 键盘选择真实边显示关系，并以 edge_id 提问',()=>{
        assert.equal(w.NavrenViews.current().filters.selected_id,kind+':edge-0');assert.equal(edgeRef.id,kind+':edge-0');assert.equal(edgeRef.parameters.edge_id,kind+':edge-0');assert.equal(edgeRef.parameters.node_id,undefined);
        assert.ok(inspector().textContent.includes(f.links.find(e=>e.id===kind+':edge-0').predicate));
      });
      await click(node(kind+':direct-0'));await click(action('focus'));await more();
      check(kind+' 聚焦仅显示当前真实节点的一跳关系，排除两跳及独立账户',()=>{
        const focus=kind+':direct-0',adjacent=f.links.filter(e=>e.source===focus||e.target===focus),expected=[focus,...adjacent.map(e=>e.source===focus?e.target:e.source)];
        assert.deepEqual(displayedIDs().sort(),[...new Set(expected)].sort());assert.deepEqual(svgEdges().map(e=>e.dataset.edgeId).sort(),adjacent.map(e=>e.id).sort());
        assert.ok(!node(kind+':direct-1'));assert.ok(!node(kind+':unconnected'));
      });
      await click(action('reset'));await more();
      check(kind+' 恢复全图保留当前选中对象，不重置真实详情',()=>{
        assert.ok(node(kind+':unconnected'));assert.equal(w.NavrenViews.current().filters.selected_id,kind+':direct-0');assert.equal(inspector().querySelector('h3').textContent,'独立事项 0');
      });
    }
    await restore('wealth');await more();await click(node('wealth:unknown'));
    check('未知金额和未知损益保持未知，已知零值仍显示 0.00',()=>{
      assert.ok(node('wealth:unknown').textContent.includes('金额未知'));assert.ok(node('wealth:zero').textContent.includes('0.00 元'));assert.ok(!node('wealth:zero').textContent.includes('金额未知'));
      assert.ok(inspector().querySelector('.pnl-unknown'));assert.ok(inspector().querySelector('.pnl-zero'));assert.ok(inspector().querySelector('.pnl-zero').textContent.includes('0.00'));
    });
    d.documentElement.style.setProperty('--font-size-base','20px');await tick();
    check('字号调节同步到真实 SVG 名称，不能硬封在默认小字号',()=>{assert.equal(node('wealth:unknown').querySelector('.fg-network-label').getAttribute('font-size'),'20');});
    d.documentElement.style.setProperty('--font-size-base','15px');await tick();
    await restore('plans');await more();
    const series=()=>[...root.querySelectorAll('svg .fg-network-node[data-series-id]')];
    const eventIDs=()=>series().map(s=>JSON.parse(s.dataset.eventIds));
    check('周期组合依据确切来源折叠，同名不同合同不混合',()=>{
      const all=eventIDs();assert.ok(all.some(ids=>ids.length===3&&ids.every(id=>id.startsWith('event:commute-A:'))));assert.ok(all.some(ids=>ids.length===2&&ids.every(id=>id.startsWith('event:commute-B:'))));
      for(const ids of all){const rows=ids.map(id=>ctx.fixtures.get('plans').nodes.find(n=>n.id===id));assert.ok(rows.every(Boolean));assert.equal(new Set(rows.map(n=>n.source_id)).size,1);assert.equal(new Set(rows.map(n=>n.currency)).size,1);assert.equal(new Set(rows.map(n=>n.account_id)).size,1);assert.equal(new Set(rows.map(n=>Math.sign(n.cash_delta_minor))).size,1);}
      for(const id of ['event:confirmed','event:inflow','event:usd','event:no-source-1','event:no-source-2','event:another-account'])assert.ok(node(id),'不能误合并 '+id);
    });
    const commute=series().find(s=>JSON.parse(s.dataset.eventIds).includes('event:commute-A:0'));await activate(commute);
    check('周期组合详情逐期保留原始日期、金额、ID，并可展开到真实圆节点',()=>{
      const occurrences=[...inspector().querySelectorAll('[data-occurrence-id]')];assert.deepEqual(occurrences.map(x=>x.dataset.occurrenceId).sort(),['event:commute-A:0','event:commute-A:1','event:commute-A:2']);
      for(const date of ['2026-10-12','2026-11-12','2026-12-12'])assert.ok(inspector().textContent.includes(date));for(const value of ['1,200.01','1,200.02','1,200.03'])assert.ok(inspector().textContent.includes(value));
      assert.ok(action('expand-series'));
      assert.equal([...inspector().querySelectorAll('button')].filter(b=>/^(问 Navren|Ask Navren)$/.test(b.textContent.trim())).length,0,'展示组不能以虚构金融事件发起提问');
    });
    await click(inspector().querySelector('[data-occurrence-id="event:commute-A:1"]'));await click(action('focus'));await more();
    check('折叠中的真实单期从详情聚焦时自动显示该期与一跳连边',()=>{
      assert.deepEqual(displayedIDs().sort(),['account:everyday','event:commute-A:1']);assert.deepEqual(svgEdges().map(e=>e.dataset.edgeId),['edge:event:commute-A:1']);assert.equal(w.NavrenViews.current().filters.network_focus,'event:commute-A:1');
    });
    await restore('plans');await more();await activate(series().find(s=>JSON.parse(s.dataset.eventIds).includes('event:commute-A:0')));
    await click(action('expand-series'));
    check('展开周期后每期是真实 SVG 节点和真实边，未创造金融事件',()=>{
      for(let i=0;i<3;i++){assert.ok(node('event:commute-A:'+i));assert.ok(edge('edge:event:commute-A:'+i));}assert.equal(series().some(s=>JSON.parse(s.dataset.eventIds).includes('event:commute-A:0')),false);
    });
    await activate(node('event:commute-A:1'));const eventRef=await ask();
    check('单期预测提问绑定单期原始事件，而非展示组 ID',()=>{
      assert.equal(eventRef.id,'event:commute-A:1');assert.equal(eventRef.parameters.node_id,'event:commute-A:1');assert.equal(eventRef.version,'four-domains-plans-v1');assert.ok(inspector().textContent.includes('2026-11-12'));assert.ok(inspector().textContent.includes('1,200.02 元'));
    });
    await click(node('goal:emergency'));await click(action('focus'));
    check('未分配目标不从 saved_minor 推断账户关联或把目标金额作为可用余额',()=>{
      assert.deepEqual(displayedIDs(),['goal:emergency']);assert.equal(svgEdges().length,0);assert.ok(inspector().textContent.includes('300,000.00 元'));assert.ok(inspector().textContent.includes('0.00 元'));assert.ok(!inspector().textContent.includes('180,000.00 元'));
    });
    const originalPlans=ctx.fixtures.get('plans'),withExtraConnection=structuredClone(originalPlans);
    withExtraConnection.links.push({id:'edge:period-to-goal',source:'event:commute-A:1',target:'goal:emergency',predicate:'本人关联计划',amount_minor:null,currency:'CNY',derivation_type:'confirmed',evidence_refs:[]});
    ctx.fixtures.set('plans',withExtraConnection);await restore('plans');await more();
    check('周期折叠不吞掉某一期与账户之外的其他真实关系',()=>{
      assert.ok(node('event:commute-A:1'),'具有额外原始关系的单期不能从图中消失');assert.ok(edge('edge:period-to-goal'),'该期与目标的真实边不能被折叠过程删掉');
    });
    ctx.fixtures.set('plans',originalPlans);
    await restore('preferences');await more();await click(node('memory:condition'));w.NavrenI18n.setLanguage('en');await tick();
    check('界面语言切换保留本人原话与真实节点名称',()=>{assert.equal(inspector().querySelector('blockquote').textContent,'我希望保留充足的备用资金。');assert.equal(inspector().querySelector('blockquote').getAttribute('translate'),'no');assert.equal(node('memory:condition').getAttribute('aria-label'),'本人条件');});
    w.NavrenI18n.setLanguage('zh-CN');await tick();
    check('图形筛选、聚焦、提问引用不修改冻结业务资料或发起业务写入',()=>{assert.equal(JSON.stringify([...ctx.fixtures]),snapshot);assert.equal(ctx.network.length,0);assert.ok(ctx.posts.every(p=>p.endpoint==='ask'));});
    ctx.changeIdentity();w.dispatchEvent(new w.Event('navren:privacy-reset'));await tick();w.dispatchEvent(new w.CustomEvent('navren:theme'));await tick();
    check('权限变化立即清空，主题语言事件不能复活旧节点与详情',()=>{assert.equal(root.querySelector('.fg-svg'),null);assert.equal(inspector().querySelector('h3'),null);});
    let release;ctx.setPending(new Promise(resolve=>release=resolve));const late=w.NavrenGraph.refresh();await tick();ctx.changeIdentity();w.NavrenGraph.invalidate();
    release({...ctx.fixtures.get('preferences'),privacy_revision:ctx.identity.privacy_revision-1});await late;ctx.setPending(null);
    check('迟到图谱响应不能恢复已撤销权限的 SVG 或陈述',()=>{assert.equal(root.querySelector('.fg-svg'),null);assert.equal(inspector().querySelector('blockquote'),null);});
    console.log(JSON.stringify({passed:passed.length,checks:passed,boundary:'纯内存禁网真实控制器；未验证实屏布局、WebGL、真实银行或模型'},null,2));
  }finally{for(const observer of ctx.observers)observer.disconnect();ctx.dom.window.close();}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
