/* 问题驱动的稳定二维投影；三维复用本地 3d-force-graph 和 SpriteText。 */
window.NavrenGraph=(()=>{
  const views={cashflow:'日常收支',wealth:'财富结构',plans:'目标与责任',preferences:'偏好与认知'};
  const colors={account:'#417cb3',category:'#6099ba',merchant:'#8baeba',contract:'#b58a59',event:'#b58a59',goal:'#619584',memory:'#8b7daf',concept:'#647da2',holding:'#417cb3',profile:'#8b7daf',person:'#8b7daf'};
  const relationshipColors={observed:'#627084',calculated:'#417cb3',confirmed:'#2c7d86',candidate:'#b58a59',scenario:'#8b7daf'};
  const copy={
    amountUnknown:['金额未知','Amount unknown'],countUnknown:['笔数未知','Count unknown'],countNA:['笔数不适用','Count not applicable'],shareNA:['占比不适用','Share not applicable'],count:['笔','transactions'],
    observed:['观测事实','Recorded fact'],calculated:['确定性计算','Deterministic calculation'],confirmed:['确认未来关系','Confirmed future connection'],candidate:['待核实候选','Unverified candidate'],scenario:['独立情景','Independent scenario'],unknownRelation:['关系状态未知','Connection status unknown'],
    originals:['节点名称与本人原话保留资料原文；界面标签随语言切换。','Names and your statements retain their source text; interface labels follow the selected language.'],
    causality:['连线表示记录、计算或本人确认的关联；箭头仅表示关系方向，不证明因果。','Connections show recorded, calculated or personally confirmed relationships. Arrows show direction, not proof of causation.'],
    thickness:['线粗只比较同币种、金额单位、口径、期间和关系类型的绝对金额；不同组不能直接比较。边未单列口径时按端点记录口径与关系分组；未知金额或口径使用细线，不代表零。','Line thickness compares absolute amounts only within the same currency, monetary unit, basis, period and relationship type. Different groups are not directly comparable. When no separate connection basis is given, endpoint record bases and the relationship define its group. Unknown amounts or bases use thin lines; this does not mean zero.'],
    nodeTypes:['节点颜色：蓝为资金，棕为义务，绿为目标，紫为本人陈述。','Node colors: blue for funds, brown for obligations, green for goals, purple for your statements.'],
    lines2d:['二维实线：已记录或计算；虚线：确认未来；点线：候选；情景以关系文字标识。','2D solid lines: recorded or calculated; dashed: confirmed future connections; dotted: candidates. Scenarios are identified in the connection text.'],
    lines3d:['三维连线均为实线。灰：已记录；蓝：计算；青：确认未来；棕：候选；紫：情景；浅灰：状态未知。请同时查看关系文字。','All 3D connections are solid. Gray: recorded; blue: calculated; teal: confirmed future; brown: candidate; purple: scenario; light gray: unknown status. Also read the connection label.'],
    sizeAmount:['同类型、同币种、同口径按金额映射节点面积；最小尺寸仅便于选择，存量与期间流量分别缩放。','Node area reflects amounts within the same type, currency and basis. Minimum size is for selection only; stocks and period flows scale separately.'],
    sizeCount:['节点面积仅按同组已记录笔数；没有笔数资料时显示未知或不适用。','Node area reflects recorded transaction counts within the same group. Missing counts are shown as unknown or not applicable.'],
    sizeShare:['占比分母为当前子图同类型、同币种、同口径的正金额合计，不代表全账户份额。','Share uses positive amounts within the same type, currency and basis in this subgraph; it is not a share of the entire account.'],
    size3d:['三维球体体积按同组口径缩放，最小尺寸仅便于选择；精确金额、期间和依据在详情中查看。','3D sphere volume scales within the same metric group. Minimum size is for selection only. Review exact amounts, periods and evidence in the details.']
  };
  const clean=v=>String(v??'');
  const english=()=>window.NavrenI18n?.language==='en'||document.documentElement.dataset.language==='en';
  const t=v=>window.NavrenI18n?.t(clean(v))||clean(v);
  const text=key=>copy[key]?.[english()?1:0]||key;
  const el=(tag,text,cls)=>{const x=document.createElement(tag);if(text!=null)x.textContent=clean(text);if(cls)x.className=cls;return x;};
  const original=(tag,value,cls)=>{const x=el(tag,value,cls);x.setAttribute('translate','no');return x;};
  const btn=(text,fn,cls='p-btn')=>{const b=el('button',text,cls);b.type='button';b.onclick=fn;return b;};
  const money=(n,c='CNY')=>{if(!Number.isSafeInteger(n))return t('未知');const value=Math.abs(n);return (english()?c+' ':'')+(n<0?'-':'')+Math.trunc(value/100).toLocaleString(english()?'en-US':'zh-CN',{maximumFractionDigits:0})+'.'+String(value%100).padStart(2,'0')+(english()?'':c==='CNY'?' 元':' '+c);};
  let root,data,dataGuard,host,inspector,graph3d,observer,styleObserver,repaintQueued=false,seq=0,view='overview',metric='amount',dimension='2d',selectedId=null;
  let planFocusId=null,planQuestion='liquidity',planLimit=6,planTrail=[];
  let networkFocusId=null,networkLimit=9,networkSeriesOpen=new Set(),networkSelectedDisplay=null,networkWidth=0;
  // 资料名称不能因恰好匹配词典而被当成界面文字改写。
  const sourceName=n=>n.quote||n.observation_id||['account','holding','merchant','goal','memory','person','contract','event'].includes(n.kind);
  const nodeLabel=n=>sourceName(n)?clean(n.label):t(n.label);
  const relationship=e=>text(Object.hasOwn(relationshipColors,e.derivation_type)?e.derivation_type:'unknownRelation');
  const edgeLabel=e=>t(e.predicate)+(e.derivation_type==='scenario'?' · '+relationship(e):'');
  const escapeHTML=v=>clean(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function fontSize(){const s=getComputedStyle(document.documentElement),value=s.getPropertyValue('--font-size-base').trim();const rootSize=parseFloat(s.fontSize)||16;return Math.max(14,value.endsWith('rem')?parseFloat(value)*rootSize:parseFloat(value)||rootSize);}
  function themeColor(token,fallback){const value=getComputedStyle(document.documentElement).getPropertyValue(token).trim();return value&&!value.startsWith('var(')?value:fallback;}
  function canRepaint(){return root?.isConnected&&data&&!root.closest('[hidden]')&&dataGuard?.valid(data);}
  function queueRepaint(){if(repaintQueued)return;repaintQueued=true;queueMicrotask(()=>{repaintQueued=false;if(canRepaint())render();});}
  function watchStyles(){styleObserver.observe(document.documentElement,{attributes:true,attributeFilter:['style']});styleObserver.observe(document.body,{attributes:true,attributeFilter:['class']});}
  function legend(){
    const size=text(metric==='count'?'sizeCount':metric==='share'?'sizeShare':'sizeAmount');
    root.querySelector('[data-legend]').textContent=size+(dimension==='3d'?' '+text('size3d'):'')+' '+text('thickness');
    root.querySelector('.fg-key').textContent=text('nodeTypes')+' '+text(dimension==='3d'?'lines3d':'lines2d')+' '+text('originals');
    root.querySelector('[data-causality]').textContent=text('causality');
    for(const selector of ['[data-legend]','.fg-key','[data-causality]'])root.querySelector(selector).setAttribute('data-i18n-skip','');
  }
  function parameters(extra={}){return {view,source:root.querySelector('[data-source]').value,from:root.querySelector('[data-from]').value,to:root.querySelector('[data-to]').value,portfolio:root.querySelector('[data-portfolio]').value,accounting_basis:root.querySelector('[data-portfolio]').value==='current'?'holdings_only_v1':'total_equity_v2',...extra};}
  function ask(extra,text){
    const q=parameters({...extra,expected_version:data?.version});
    if(!data)return;
    return NavrenViews.ask(text,{kind:'graph',id:extra.node_id||extra.edge_id||extra.insight_id||view,source_scope:q.source,version:data.version,label:'财务关系 · '+(views[view]||'四域概览'),parameters:q});
  }
  const tableNames={transactions:'银行流水',archived_transactions:'银行历史流水',statement_transactions:'导入账单',wealth_goals:'长期目标',user_memories:'本人保存的记忆',profile_observations:'聊天中的本人陈述',user_memory_relations:'本人维护的记忆关联',goal_allocations:'本人确认的规划分配',portfolio_valuations:'组合估值',holdings:'当前持仓',loans:'贷款合同',deposit_contracts:'存款合同',calendar_series:'本人日历事件',mandates:'银行代扣',subscriptions:'商户订阅'};
  function sourceLink(ref){
    const accountViews={transactions:'statements',archived_transactions:'statements',statement_transactions:'statements',holdings:'holding-detail',loans:'record-detail',deposit_contracts:'record-detail',mandates:'subscriptions',subscriptions:'subscriptions',calendar_series:'calendar'};
    const a=el('a',ref.source_table==='wealth_goals'?'查看同一目标':ref.source_table==='user_memories'||ref.source_table==='profile_observations'?'查看本人资料':'前往对应记录');
    const page=['wealth_goals','user_memories','profile_observations'].includes(ref.source_table)?'mine.html':'account.html',u=new URL(page,location.href);
    u.searchParams.set('view',ref.source_table==='wealth_goals'?'goal':page==='mine.html'?'profile':accountViews[ref.source_table]||'activity');
    u.searchParams.set('object_kind',({wealth_goals:'goal',holdings:'holding',loans:'loan',deposit_contracts:'deposit',calendar_series:'calendar_series',transactions:'transaction',archived_transactions:'transaction',statement_transactions:'transaction',user_memories:'memory',profile_observations:'observation'})[ref.source_table]||ref.source_table);
    u.searchParams.set('object_id',ref.source_id);u.searchParams.set('source_scope',ref.source_scope);if(ref.occurred_at)u.searchParams.set('date',ref.occurred_at.slice(0,10));a.href=u.pathname+u.search;return a;
  }
  function evidence(refs){
    const d=el('details',undefined,'fg-evidence');d.append(el('summary',pt('原始依据','Source evidence')+' · '+(refs?.length||0)));
    for(const ref of refs||[]){
      const row=el('section',undefined,'fg-evidence-record');row.append(el('p',(ref.occurred_at?ref.occurred_at.slice(0,10)+' · ':'')+(tableNames[ref.source_table]||'来源记录')+(ref.amount_minor!=null?' · '+money(ref.amount_minor):'')));
      const out=el('div'),read=btn('核对原记录',async()=>{const turn=seq;try{const guard=await BankClient.guard(),q=new URLSearchParams({source:ref.source_scope,table:ref.source_table,id:ref.source_id}),result=await BankClient.get('/graph/evidence?'+q);if(turn!==seq||!guard.valid(result)||!row.isConnected)return;out.replaceChildren();const names={amount_minor:'金额',balance_minor:'余额',target_minor:'目标金额',saved_minor:'已安排金额',merchant:'商户',occurred_at:'发生时间',kind:'记录类型',status:'状态',category:'类别',value:'本人陈述',quote:'本人原话',name:'名称',due_on:'目标截止日',currency:'币种',expires_on:'有效至',label:'内容',delta_minor:'流水金额',outstanding_minor:'未偿本金',principal_minor:'本金',started_on:'开始日期',matures_on:'到期日',date:'日期'};for(const [key,value]of Object.entries(result.record||{}))if(names[key])out.append(el('p',names[key]+'：'+(key.endsWith('_minor')?money(value,result.record.currency):typeof value==='object'?JSON.stringify(value):value)));out.append(sourceLink(ref));}catch(error){if(turn===seq)out.replaceChildren(el('p',error.message));}},'fg-index-button');row.append(read,out);d.append(row);
    }return d;
  }
  function layoutInspector(object){
    const header=el('header',undefined,'fg-detail-header'),sections=el('div',undefined,'fg-detail-sections'),actions=el('div',undefined,'fg-detail-actions');
    for(const node of [...inspector.children])if(node.tagName==='H3'||node.classList.contains('eyebrow')&&header.children.length===0||node.classList.contains('fg-plan-detail-status'))header.append(node);
    const facts=[...inspector.children].filter(node=>node.tagName==='DL'),primaryLabels=['金额','当前余额','目标金额','已确认规划分配','Amount','Current balance','Target amount','Confirmed planning allocation'];
    let value;
    for(const factsList of facts){
      factsList.classList.add('fg-detail-facts');
      const label=factsList.querySelector('dt'),amount=label?.nextElementSibling;
      if(!value&&primaryLabels.includes(label?.textContent)&&amount?.tagName==='DD'){
        value=el('div',undefined,'fg-detail-value');value.append(el('span',label.textContent),original('strong',amount.textContent));label.remove();amount.remove();
      }
      if(!factsList.children.length)factsList.remove();
    }
    const notes=el('div',undefined,'fg-detail-notes');
    for(const node of [...inspector.children]){
      if(node.tagName==='DL')continue;
      if(node.tagName==='DETAILS')sections.append(node);
      else if(node.tagName==='BUTTON'||node.tagName==='A')actions.append(node);
      else if(node.classList.contains('fg-plan-detail-actions'))actions.append(...node.children);
      else notes.append(node);
    }
    inspector.replaceChildren(header);if(value)inspector.append(value);
    for(const factsList of facts)if(factsList.children.length)inspector.append(factsList);
    if(notes.children.length){
      if(object?.quote||object?.kind==='memory'||object?.role==='condition')inspector.append(notes);
      else{const more=el('details');more.append(el('summary',pt('补充说明','Additional notes')),notes);sections.prepend(more);}
    }
    if(sections.children.length)inspector.append(sections);if(actions.children.length)inspector.append(actions);
  }
  function show(object){
    if(!canRepaint()||!inspector)return;
    networkSelectedDisplay=null;
    if(view==='plans'){showPlanObject(object);networkMark();return;}
    const expectedRevision=data.personal_revision,confirmationToken=object.confirmation_token;
    selectedId=object.id;
    window.NavrenViews?.update({filters:{...parameters(),graph_view:view,selected_id:selectedId,network_focus:networkFocusId,dimension,metric}});
    inspector.replaceChildren(el('span',object.predicate?t('关系与计算'):t('记录与依据'),'eyebrow'),original('h3',object.predicate?edgeLabel(object):object.label?nodeLabel(object):object.title));
    const basisNames={cash_stock:['账户余额存量','Account balance stock'],period_支出:['期间现金净支出','Period net cash spending'],period_收入:['期间收入','Period income'],period_surplus:['期间收支结余','Period income less consumption'],contract_cycle:['单个合同周期','Single contract cycle'],goal_target:['目标金额','Target amount'],future_obligation:['未来义务','Future obligation'],net_assets_principal:['已覆盖本金净额','Covered principal net amount'],holding_principal:['持仓本金','Holding principal'],liability_stock:['未偿债务余额','Outstanding debt balance'],redemption_receivable:['赎回待到账','Redemption receivable'],investment_equity_stock:['投资权益估值','Investment equity valuation']};
    const basis=basisNames[object.metric_family];
    const pairs=[['金额',Object.hasOwn(object,'amount_minor')?money(object.amount_minor,object.currency):null],['笔数',object.transaction_count],['期限',object.date||object.due_on||object.next_on],['目标金额',Object.hasOwn(object,'target_minor')?money(object.target_minor,object.currency):null],['记录口径',basis?pt(...basis):null],['关系类型',object.derivation_type?relationship(object):null],['说明',object.notice||object.metric_definition||object.refund_policy]];
    const facts=el('dl',undefined,'fg-network-pairs');for(const [label,value]of pairs)if(value!=null&&label!=='说明'){facts.append(el('dt',t(label)),original('dd',value));}inspector.append(facts);
    if(object.notice||object.metric_definition||object.refund_policy){const explanation=el('details',undefined,'fg-network-explanation');explanation.append(el('summary',pt('口径说明','Basis notes')),el('p',object.notice||object.metric_definition||object.refund_policy));inspector.append(explanation);}
    if(object.decision){const d=object.decision;inspector.append(el('p','替代项：'+d.alternatives.join('、')),el('p','选择理由：'+d.reason),el('p','执行：'+(d.execution_ids.length?'已有账户回执':'尚无执行回执')),el('p','反馈：'+(d.feedback||'待补充')));}
    if(object.returns)for(const [f,label]of [['daily_pnl_minor','区间损益'],['unrealized_pnl_minor','持仓损益'],['closing_equity_minor','投资权益']]){const value=object.returns[f],p=el('p',t(label)+'：');p.append(el('strong',(f!=='closing_equity_minor'&&Number.isSafeInteger(value)&&value>0?'+':'')+money(value,object.currency),f==='closing_equity_minor'?undefined:'pnl-'+(!Number.isSafeInteger(value)?'unknown':value>0?'positive':value<0?'negative':'zero')));inspector.append(p);}
    if(object.quote)inspector.append(el('p',t('本人原话'),'eyebrow'),original('blockquote',object.quote));
    if(object.status==='candidate')inspector.append(el('p','这条陈述等待你核实，尚未作为事实。','fg-limit'));
    const c=data.calculations?.find(c=>c.id===object.calculation_id);
    if(c){inspector.append(el('p','期间 '+c.period.from+' 至 '+c.period.to_exclusive+'（末日不含）'),el('p','平均每笔 '+money(c.average_minor,c.currency)),el('p',c.change_minor==null?'相邻期间比较不可用':'已记录金额变化 '+money(c.change_minor,c.currency)),el('p',c.comparison_notice));}
    const refs=object.evidence_refs||[];if(refs.length)inspector.append(evidence(refs));
    for(const c of data.related_knowledge||[]){const d=el('details');d.append(el('summary','相关概念：'+c.label),el('p',c.summary));const a=el('a',c.publisher);a.href=c.url;a.target='_blank';a.rel='noopener noreferrer';d.append(a);inspector.append(d);}
    if(object.observation_id)for(const [decision,label] of [['confirm','确认资料'],['delete','删除资料']]){
      if(decision==='confirm'&&object.status==='confirmed')continue;
      inspector.append(btn(label,async()=>{if(!confirm(decision==='delete'?'删除这项资料及派生关系？银行交易不会删除。':'确认这是本人的陈述？不会改变业务风险或交易资格。'))return;try{if(!Number.isSafeInteger(expectedRevision)||decision==='confirm'&&!confirmationToken)throw Error('资料版本或确认凭据未取得，请重新核对。');const payload={decision,expected_revision:expectedRevision};if(decision==='confirm')payload.confirmation_token=confirmationToken;await BankClient.post('/profile-observations/'+object.observation_id,payload);await BankClient.refreshIdentity();await refresh();}catch(e){showToast(e.message);}}));
    }
    inspector.append(btn('问 Navren',()=>ask(object.id?object.predicate?{edge_id:object.id}:object.label?{node_id:object.id}:{insight_id:object.id}:{},'解释这项财务关系的金额、时间口径和证据。')));
    if(object.fund_id){const a=el('a','产品与收益详情 ›'),u=new URL('account.html',location.href);u.searchParams.set('view','wealth-product');u.searchParams.set('object_kind','fund');u.searchParams.set('object_id',object.fund_id);u.searchParams.set('source_scope','historical');u.searchParams.set('portfolio_id',object.portfolio_version||data.portfolio_version);u.searchParams.set('date',object.nav_asof||data.nav_asof||'');a.href=u.pathname+u.search;inspector.append(a);}
    if(object.fund_id)inspector.append(btn('查看收益日历',()=>{const u=new URL('/account.html',location.href);u.hash='calendar';u.searchParams.set('view','calendar');u.searchParams.set('source_scope','historical');u.searchParams.set('portfolio_id',object.portfolio_version||data.portfolio_version);if(object.nav_asof||data.nav_asof)u.searchParams.set('date',object.nav_asof||data.nav_asof);location.href=u;}));
    if(data.nodes.some(node=>node.id===object.id)){const focusButton=btn(pt('聚焦直接关系','Focus direct connections'),()=>networkFocus(object.id));focusButton.dataset.networkAction='focus';inspector.append(focusButton);}networkMark();
    const details=el('details');details.append(el('summary','时间与版本'),el('p','时点 '+data.as_of+'；净值日 '+(object.nav_asof||data.nav_asof||'未提供')),el('p','资料 '+data.version+'；组合 '+data.portfolio_version));inspector.append(details);layoutInspector(object);
  }
  function svg(tag,attrs,text){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs||{}))n.setAttribute(k,v);if(text!=null)n.textContent=clean(text);return n;}
  // Plans are a one-hop display projection over the unchanged evidence graph.
  // No projected cash, reserve or allocation value is calculated in this renderer.
  const pt=(zh,en)=>english()?en:zh;
  const planEndpoint=value=>typeof value==='object'?value.id:value;
  function planState(object){
    if(object.derivation_type==='candidate'||object.certainty==='candidate'||object.status==='candidate')return 'candidate';
    if(object.derivation_type==='confirmed'||object.certainty==='confirmed')return 'confirmed';
    return 'recorded';
  }
  function planStateLabel(object){return planState(object)==='candidate'?pt('待核实预测','Unverified forecast'):planState(object)==='confirmed'?pt('已确认','Confirmed'):pt('已记录','Recorded');}
  function planPersist(){window.NavrenViews?.update({filters:{...parameters(),graph_view:view,selected_id:selectedId,plan_focus:planFocusId,plan_question:planQuestion,network_focus:networkFocusId,dimension,metric}});}
  function planFind(id){return [...data.nodes,...data.links,...data.insights].find(item=>item.id===id);}
  function configurePlanLayout(){
    const on=view==='plans';root.dataset.graphView=view;
    host.classList.remove('fg-plan-workspace');host.classList.toggle('fg-network',view!=='overview');
    const stage=root.querySelector('.finance-graph-stage');stage.classList.remove('fg-plans-stage');stage.classList.toggle('fg-network-stage',view!=='overview');
    inspector.classList.remove('fg-plan-inspector');inspector.classList.toggle('fg-network-inspector',view!=='overview');
    root.querySelector('[data-insights]').classList.toggle('fg-plans-summary',on);
    root.querySelector('.fg-controls').classList.remove('fg-plans-controls');
    for(const name of ['metric','dimension'])root.querySelector('[data-'+name+']').closest('label').hidden=false;
    for(const name of ['from','to'])root.querySelector('[data-'+name+']').closest('label').hidden=on;
    for(const selector of ['.fg-mobile-paths','[data-consent]'])root.querySelector(selector).hidden=view!=='overview';
    for(const selector of ['[data-legend]','.fg-key','[data-causality]'])root.querySelector(selector).hidden=false;
    for(const selector of ['[data-rank]','[data-timeline]']){const area=root.querySelector(selector);area.hidden=true;area.replaceChildren();}
  }
  function planChooseFocus(){
    if(planFocusId&&!data.nodes.some(node=>node.id===planFocusId))planFocusId=null;
    if(selectedId&&!planFind(selectedId))selectedId=null;
    planTrail=planTrail.filter(id=>data.nodes.some(node=>node.id===id));
    if(planFocusId&&data.nodes.some(node=>node.id===planFocusId))return data.nodes.find(node=>node.id===planFocusId);
    const selected=selectedId&&planFind(selectedId);
    if(selected){
      if(data.nodes.includes(selected))planFocusId=selected.id;
      else if(selected.predicate)planFocusId=planEndpoint(selected.source);
      else planFocusId=(selected.node_ids||[]).find(id=>data.nodes.some(node=>node.id===id&&['account','goal'].includes(node.kind)));
    }
    if(!planFocusId){
      const available=data.nodes.filter(node=>node.kind==='account');
      const atRisk=data.timeline?.accounts?.find(account=>Number.isSafeInteger(account.minimum_available_after_confirmed_minor)&&account.minimum_available_after_confirmed_minor<0);
      const futureConnected=available.find(account=>data.links.some(edge=>planEndpoint(edge.source)===account.id&&data.nodes.some(node=>node.id===planEndpoint(edge.target)&&node.kind==='event')));
      const preferred=available.find(account=>account.id==='account:'+atRisk?.account_id)||futureConnected||available.find(node=>Number.isSafeInteger(node.amount_minor)&&node.amount_minor>0)||available[0];
      planFocusId=(planQuestion==='goal'?data.nodes.find(node=>node.kind==='goal'):preferred)?.id||data.nodes[0]?.id||null;
    }
    const focus=data.nodes.find(node=>node.id===planFocusId);
    if(focus?.kind==='goal')planQuestion='goal';else if(focus?.kind==='account')planQuestion='liquidity';
    return focus;
  }
  function setPlanFocus(id){return networkFocus(id);}
  function planAccount(focus){
    let accountId=focus?.kind==='account'?focus.id.slice(8):focus?.account_id;
    if(!accountId&&focus?.kind==='event'){
      const accounts=[...new Set(data.links.filter(edge=>planEndpoint(edge.target)===focus.id).map(edge=>planEndpoint(edge.source)).filter(id=>data.nodes.some(node=>node.id===id&&node.kind==='account')))];
      if(accounts.length===1)accountId=accounts[0].slice(8);
    }
    return data.timeline?.accounts?.find(account=>account.account_id===accountId)||null;
  }
  function planMinimumDate(account,key){
    if(!account||!Number.isSafeInteger(account[key]))return null;
    if(account.current_available_minor===account[key])return data.timeline.as_of;
    const slotKey=key==='minimum_available_if_candidates_occur_minor'?'available_if_candidates_occur_minor':'available_after_confirmed_minor';
    return data.timeline.timeline?.find(slot=>slot.accounts?.some(row=>row.account_id===account.account_id&&row[slotKey]===account[key]))?.date||null;
  }
  function planGoalInsight(focus){
    const candidates=data.insights.filter(item=>Number.isSafeInteger(item.target_minor));
    if(focus?.kind==='goal')return candidates.find(item=>item.node_ids?.includes(focus.id));
    const goalEdge=data.links.find(edge=>edge.derivation_type==='confirmed'&&edge.predicate==='本人确认规划分配'&&(planEndpoint(edge.source)===focus?.id||planEndpoint(edge.target)===focus?.id));
    const goalId=goalEdge&&(data.nodes.find(node=>node.id===planEndpoint(goalEdge.source)&&node.kind==='goal')||data.nodes.find(node=>node.id===planEndpoint(goalEdge.target)&&node.kind==='goal'))?.id;
    return candidates.find(item=>item.node_ids?.includes(goalId))||candidates[0]||null;
  }
  function planSummaries(focus){
    const box=root.querySelector('[data-insights]');box.replaceChildren();box.classList.remove('fg-overview');
    const add=(title,value,note,action)=>{
      const card=el('article',undefined,'fg-plan-summary-card');card.append(el('span',title,'eyebrow'),el('strong',value),original('small',note));
      if(action)card.append(btn(pt('查看关系','Inspect relationships'),action,'fg-plan-summary-link'));
      box.append(card);return card;
    };
    const totals=data.timeline?.totals_by_currency||{},currencies=Object.keys(totals);
    const payment=add(pt('未来已确认付款','Confirmed future payments'),currencies.length===1?money(totals[currencies[0]].confirmed_outflow_minor,currencies[0]):currencies.length?pt('按币种查看','View by currency'):pt('暂无可核验资料','No verified data'),pt('全部已覆盖账户 · 不含候选预测','All covered accounts · forecasts excluded'));
    if(currencies.length>1)for(const currency of currencies)payment.append(el('small',money(totals[currency].confirmed_outflow_minor,currency)));
    const account=planAccount(focus),date=planMinimumDate(account,'minimum_available_after_confirmed_minor');
    add(pt('未来最低可用','Lowest future available funds'),account?money(account.minimum_available_after_confirmed_minor,account.currency):pt('选择资金账户','Choose a funding account'),account?account.account_name+(date?' · '+date:'')+' · '+pt('仅含已确认事项','confirmed items only'):pt('逐账户查看，不合并不同账户的最低值','Inspect each account; minima are not combined'));
    const goal=planGoalInsight(focus),goalNode=goal&&data.nodes.find(node=>node.kind==='goal'&&goal.node_ids?.includes(node.id));
    add(pt('目标已确认分配','Confirmed goal allocation'),goal?money(goal.amount_minor,goal.currency):pt('尚无目标记录','No goal record'),goalNode?nodeLabel(goalNode)+' · '+pt('规划记录，不是账户余额','planning record, not account balance'):pt('本人确认后显示','Shown after your confirmation'),goalNode&&focus?.id!==goalNode.id?()=>setPlanFocus(goalNode.id):null);
  }
  function planRelations(focus){
    const connected=data.links.filter(edge=>planEndpoint(edge.source)===focus.id||planEndpoint(edge.target)===focus.id);
    const result=[],groups=new Map();
    for(const edge of connected){
      const id=planEndpoint(edge.source)===focus.id?planEndpoint(edge.target):planEndpoint(edge.source),node=data.nodes.find(item=>item.id===id);if(!node)continue;
      const item={node,edge},refs=node.evidence_refs||[],sourceTable=node.source_table||refs[0]?.source_table;
      // A legacy projection can prove a recurring series through the exact source
      // record and account edge. Labels alone still never establish identity.
      const sourceIds=[...new Set(refs.filter(ref=>ref.source_table===sourceTable&&ref.source_id).map(ref=>ref.source_id))],sourceId=node.source_id||(sourceIds.length===1?sourceIds[0]:null);
      const sourceAccount=data.nodes.find(item=>item.id===planEndpoint(edge.source)&&item.kind==='account');
      const account=node.account_id||sourceAccount?.id.slice(8)||null;
      const legacyDirection=sourceTable==='recurring_obligations'?({'可能扣费':-1,'预计到账':1}[edge.predicate]??null):null;
      const direction=Number.isSafeInteger(node.cash_delta_minor)?Math.sign(node.cash_delta_minor):legacyDirection;
      const category=node.category||node.event_type||(legacyDirection!=null?node.label:null);
      const groupable=node.kind==='event'&&planState(node)==='candidate'&&planState(edge)==='candidate'&&sourceTable&&sourceId&&category&&account&&node.currency&&node.metric_family==='future_obligation'&&direction!=null&&node.date;
      if(!groupable){result.push({rows:[item],state:planState(edge),series:false});continue;}
      const key=JSON.stringify([account,node.currency,planState(node),sourceTable,sourceId,node.metric_family,category,node.label,direction,edge.predicate]);
      let group=groups.get(key);if(!group){group={rows:[],state:'candidate',series:true};groups.set(key,group);result.push(group);}group.rows.push(item);
    }
    for(const group of result){group.rows.sort((a,b)=>clean(a.node.date).localeCompare(clean(b.node.date))||a.node.id.localeCompare(b.node.id));group.series=group.series&&group.rows.length>1;}
    return result.sort((a,b)=>(a.state==='candidate')-(b.state==='candidate')||clean(a.rows[0].node.date||a.rows[0].node.due_on).localeCompare(clean(b.rows[0].node.date||b.rows[0].node.due_on)));
  }
  function planEvidence(refs){
    const details=el('details',undefined,'fg-evidence fg-plan-evidence');details.append(el('summary',pt('原始依据','Source evidence')+' · '+refs.length));
    for(const ref of refs){
      const row=el('section',undefined,'fg-evidence-record');row.append(el('p',(ref.occurred_at?ref.occurred_at.slice(0,10)+' · ':'')+t(tableNames[ref.source_table]||pt('来源记录','Source record'))));
      const out=el('div');const read=btn(pt('核对原记录','Inspect source record'),async()=>{
        const turn=seq;try{
          const guard=await BankClient.guard(),result=await BankClient.get('/graph/evidence?'+new URLSearchParams({source:ref.source_scope,table:ref.source_table,id:ref.source_id}));
          if(turn!==seq||!guard.valid(result)||!row.isConnected)return;out.replaceChildren();
          const fields={amount_minor:['金额','Amount'],balance_minor:['账户余额','Account balance'],target_minor:['目标金额','Target'],saved_minor:['目标记录中的储备额','Savings recorded in the goal'],merchant:['商户','Merchant'],occurred_at:['发生时间','Occurred at'],name:['名称','Name'],due_on:['目标日期','Goal date'],currency:['币种','Currency'],quote:['本人原话','Your statement'],date:['日期','Date'],next_due_on:['原始下次日期','Original next due date'],frequency:['周期','Frequency'],delta_minor:['流水金额','Transaction amount'],outstanding_minor:['未偿本金','Outstanding principal'],started_on:['开始日期','Start date'],matures_on:['到期日','Maturity']};
          for(const [key,value]of Object.entries(result.record||{}))if(fields[key]&&value!=null&&typeof value!=='object'){
            const label=pt(...fields[key]);out.append(original('p',label+'：'+(key.endsWith('_minor')?money(value,result.record.currency):key==='frequency'?({monthly:pt('每月','Monthly'),yearly:pt('每年','Yearly'),weekly:pt('每周','Weekly'),daily:pt('每日','Daily')}[value]||value):value)));
          }
          const link=sourceLink(ref);link.textContent=pt('前往对应记录','Open related record');out.append(link);
        }catch(error){if(turn===seq&&row.isConnected)out.replaceChildren(el('p',error.message));}
      },'fg-index-button');row.append(read,out);details.append(row);
    }return details;
  }
  function showPlanObject(object){
    if(!canRepaint()||!inspector||!object)return;
    selectedId=object.id;planPersist();
    inspector.replaceChildren(el('span',pt('金额、时间与依据','Amount, time and evidence'),'eyebrow'),original('h3',object.predicate?edgeLabel(object):object.label?nodeLabel(object):object.title));
    const pairs=el('dl',undefined,'fg-plan-detail-pairs'),add=(name,value)=>{if(value!=null&&value!==''){pairs.append(el('dt',name),original('dd',value));}};
    const focus=data.nodes.find(node=>node.id===planFocusId),account=object.kind==='account'?planAccount(object):null,goal=object.kind==='goal'?planGoalInsight(object):Object.hasOwn(object,'target_minor')&&!object.kind?object:null;
    add(object.kind==='account'?pt('当前余额','Current balance'):object.kind==='goal'?pt('目标金额','Target amount'):goal?pt('已确认规划分配','Confirmed planning allocation'):pt('金额','Amount'),Object.hasOwn(object,'amount_minor')?money(object.kind==='goal'?object.target_minor:object.amount_minor,object.currency):null);
    add(pt('日期','Date'),object.date||object.due_on||object.occurred_at||object.next_on);
    if(goal){if(object.kind==='goal')add(pt('已确认规划分配','Confirmed planning allocation'),money(goal.amount_minor,goal.currency));else add(pt('目标金额','Target amount'),money(goal.target_minor,goal.currency));}
    if(account){
      add(pt('当前可用','Currently available'),money(account.current_available_minor,account.currency));
      add(pt('未来最低可用 · 已确认','Lowest future available · confirmed'),money(account.minimum_available_after_confirmed_minor,account.currency));
      add(pt('最低日期','Minimum date'),planMinimumDate(account,'minimum_available_after_confirmed_minor'));
      add(pt('候选全部发生时','If all candidates occur'),money(account.minimum_available_if_candidates_occur_minor,account.currency));
    }
    const basis={cash_stock:['账户余额存量','Account balance stock'],future_obligation:['未来事项 · 单期金额','Future event · amount per occurrence'],goal_target:['目标金额','Target amount'],contract_cycle:['合同单个周期','Single contract cycle']};
    inspector.append(el('span',planStateLabel(object),'fg-plan-detail-status is-'+planState(object)),pairs);
    if(planState(object)==='candidate')inspector.append(el('p',pt('尚未计入已确认付款。','Excluded from confirmed payments.'),'fg-plan-detail-note'));
    if(object.kind==='goal'||goal)inspector.append(el('p',pt('这里的分配是本人确认的规划记录，不是账户余额或银行预留。','Allocations here are planning records confirmed by you, not account balances or bank holds.'),'fg-plan-detail-note'));
    const allRefs=[...(object.evidence_refs||[]),...(goal?.evidence_refs||[])],refs=allRefs.filter((ref,index)=>allRefs.findIndex(other=>other.source_scope===ref.source_scope&&other.source_table===ref.source_table&&other.source_id===ref.source_id&&other.occurred_at===ref.occurred_at)===index);if(refs.length)inspector.append(planEvidence(refs));
    const metadata=el('details',undefined,'fg-plan-meta');metadata.append(el('summary',pt('口径与时间','Basis and time')));
    if(basis[object.metric_family])metadata.append(el('p',pt('金额口径','Amount basis')+'：'+pt(...basis[object.metric_family])));
    if(data.timeline)metadata.append(el('p',pt('未来检查窗口','Future window')+'：'+data.timeline.as_of+' — '+data.timeline.horizon_end));
    if(metadata.children.length>1)inspector.append(metadata);
    const actions=el('div',undefined,'fg-plan-detail-actions');
    const askButton=btn(pt('问 Navren','Ask Navren'),()=>ask(object.predicate?{edge_id:object.id}:data.nodes.some(node=>node.id===object.id)?{node_id:object.id}:{insight_id:object.id},pt('围绕这项资金关系，说明金额口径、日期、确认状态和原始依据。','Explain the amount basis, dates, confirmation status and source evidence for this connection.')));askButton.dataset.planAsk='';actions.append(askButton);
    if(data.nodes.some(node=>node.id===object.id)){const focusButton=btn(pt('聚焦直接关系','Focus direct connections'),()=>networkFocus(object.id));focusButton.dataset.networkAction='focus';actions.append(focusButton);}inspector.append(actions);layoutInspector(object);
  }
  function magnitude(n){if(n.kind==='memory'||n.role==='condition')return null;if(metric==='count')return Number.isSafeInteger(n.transaction_count)&&n.transaction_count>=0?n.transaction_count:null;if(metric==='share'){const siblings=data.nodes.filter(a=>a.kind===n.kind&&a.currency===n.currency&&a.metric_family===n.metric_family&&Number.isSafeInteger(a.amount_minor)&&a.amount_minor>0);const sum=siblings.reduce((s,a)=>s+a.amount_minor,0);return sum&&Number.isSafeInteger(n.amount_minor)&&n.amount_minor>=0?n.amount_minor/sum:null;}return Number.isSafeInteger(n.amount_minor)?n.amount_minor:null;}
  function radius(n,ns){const v=magnitude(n);if(v==null||v===0)return 16;const max=Math.max(...ns.filter(a=>a.kind===n.kind&&a.currency===n.currency&&a.metric_family===n.metric_family).map(a=>Math.abs(magnitude(a)||0)));return Math.max(16,Math.min(42,42*Math.sqrt(Math.abs(v)/(max||1))));}
  function volume(n,ns){const v=magnitude(n);if(v==null||v===0)return Math.pow(16/18,3);const max=Math.max(...ns.filter(a=>a.kind===n.kind&&a.currency===n.currency&&a.metric_family===n.metric_family).map(a=>Math.abs(magnitude(a)||0)));return Math.max(Math.pow(16/18,3),Math.pow(42/18,3)*Math.abs(v)/(max||1));}
  function caption(n){
    if(n.kind==='memory'||n.role==='condition')return '';
    if(metric==='count')return Number.isSafeInteger(n.transaction_count)&&n.transaction_count>=0?n.transaction_count+' '+text('count'):Object.hasOwn(n,'transaction_count')?text('countUnknown'):text('countNA');
    if(!Object.hasOwn(n,'amount_minor'))return '';
    return metric==='amount'?Number.isSafeInteger(n.amount_minor)?money(n.amount_minor,n.currency):text('amountUnknown'):magnitude(n)==null?text('shareNA'):(magnitude(n)*100).toFixed(1)+'%';
  }
  function edgeBasis(e){
    if(e.metric_family)return e.metric_family;
    const endpoint=ref=>data.nodes.find(n=>n.id===(typeof ref==='object'?ref.id:ref)),a=endpoint(e.source),b=endpoint(e.target);
    // 现役边不一定单列 metric_family，不能把所有 observed 金额默认为同一资金口径。
    return a?.metric_family&&b?.metric_family?[a.kind,a.metric_family,b.kind,b.metric_family].join('→'):null;
  }
  function edgeGroup(e){const p=e.period||data.period;return [e.currency,e.unit||'minor',edgeBasis(e),e.predicate,e.derivation_type||'unknown',p?.from,p?.to_exclusive].join('|');}
  function edgeWidth(e,edges){
    if(metric!=='amount'||!Number.isSafeInteger(e.amount_minor)||!e.currency||!edgeBasis(e)||(e.unit&&e.unit!=='minor'))return 1;
    const max=Math.max(1,...edges.filter(x=>edgeGroup(x)===edgeGroup(e)&&Number.isSafeInteger(x.amount_minor)).map(x=>Math.abs(x.amount_minor)));
    return 1+3*Math.sqrt(Math.abs(e.amount_minor)/max);
  }
  function labelLines(value){
    const valueText=clean(value),limit=english()?18:9;
    if(!english())return valueText.match(/.{1,9}/gu)||[''];
    const lines=[];let line='';
    for(const word of valueText.split(/\s+/)){const parts=Array.from(word);while(parts.length>limit){if(line){lines.push(line);line='';}lines.push(parts.splice(0,limit).join(''));}const rest=parts.join('');if(!rest)continue;if(line&&Array.from(line+' '+rest).length>limit){lines.push(line);line='';}line+=(line?' ':'')+rest;}
    if(line)lines.push(line);return lines.length?lines:[''];
  }
  // The projection changes visibility only. Every financial object, amount and
  // source relation remains the backend object; recurring groups have no money.
  function networkPersist(){window.NavrenViews?.update({filters:{...parameters(),graph_view:view,selected_id:selectedId,network_focus:networkFocusId,plan_focus:planFocusId,dimension,metric}});}
  function networkMark(){
    for(const node of host.querySelectorAll('.fg-network-node')){const selected=networkSelectedDisplay?node.dataset.seriesId===networkSelectedDisplay:node.dataset.nodeId===selectedId;node.classList.toggle('is-selected',selected);node.setAttribute('aria-pressed',String(selected));const circle=node.querySelector('circle');if(circle)circle.setAttribute('stroke-width',selected?'3':'1.5');}
    for(const edge of host.querySelectorAll('.fg-network-edge')){const selected=networkSelectedDisplay?edge.dataset.seriesId===networkSelectedDisplay:edge.dataset.edgeId===selectedId;edge.classList.toggle('is-selected',selected);edge.setAttribute('aria-pressed',String(selected));}
  }
  function networkFocus(id){
    if(!canRepaint()||!data.nodes.some(node=>node.id===id))return;
    networkFocusId=id;selectedId=id;networkSelectedDisplay=null;networkLimit=9;if(view==='plans')planFocusId=id;
    render();networkPersist();
  }
  function networkProjection(){
    let nodes=[...data.nodes],links=[...data.links];const groups=[];
    if(view==='plans'){
      const consumed=new Set();
      for(const account of data.nodes.filter(node=>node.kind==='account'))for(const relation of planRelations(account)){
        if(!relation.series||relation.rows.some(row=>consumed.has(row.node.id)||row.node.id===networkFocusId))continue;
        // Folding cannot discard another contract, goal or account connection.
        if(relation.rows.some(row=>data.links.filter(edge=>[planEndpoint(edge.source),planEndpoint(edge.target)].includes(row.node.id)).length!==1))continue;
        const id='series-display:'+relation.rows[0].node.id;
        if(networkSeriesOpen.has(id))continue;
        for(const row of relation.rows)consumed.add(row.node.id);
        const group={id,label:nodeLabel(relation.rows[0].node),kind:'event',display_group:true,status:'candidate',series_rows:relation.rows,account_id:account.id};groups.push(group);
      }
      if(groups.length){
        const eventMap=new Map();for(const group of groups)for(const row of group.series_rows)eventMap.set(row.node.id,group);
        nodes=nodes.filter(node=>!eventMap.has(node.id)).concat(groups);
        links=links.filter(edge=>!eventMap.has(planEndpoint(edge.source))&&!eventMap.has(planEndpoint(edge.target)));
        for(const group of groups){const first=group.series_rows[0].edge;links.push({source:group.account_id,target:group.id,predicate:first.predicate,derivation_type:'candidate',display_group:true,series:group});}
      }
    }
    // Category→surplus edges already encode income and consumption. Generic
    // classification hubs would duplicate the same categories in this view.
    if(view==='cashflow'&&nodes.some(node=>node.role==='surplus'))nodes=nodes.filter(node=>!(node.kind==='concept'&&node.role!=='surplus'&&['period_收入','period_支出'].includes(node.metric_family)));
    const ids=new Set(nodes.map(node=>node.id));links=links.filter(edge=>ids.has(planEndpoint(edge.source))&&ids.has(planEndpoint(edge.target)));
    return {nodes,links};
  }
  function networkPriority(node){
    if(view==='cashflow')return node.role==='surplus'?0:node.metric_family==='period_收入'?1:node.kind==='category'?2:node.kind==='contract'?3:5;
    if(view==='wealth')return node.role==='net_assets'?0:node.role==='liability'?1:node.kind==='holding'?2:node.kind==='account'?3:5;
    if(view==='plans')return node.kind==='goal'?1:node.kind==='account'?2:node.display_group?3:node.kind==='event'?4:5;
    return node.role==='decision'&&node.status==='confirmed'?0:node.role==='condition'?1:node.status==='confirmed'?2:node.kind==='memory'?3:5;
  }
  function networkSelection(projection){
    const {nodes,links}=projection,degree=new Map(nodes.map(node=>[node.id,0]));for(const edge of links){for(const id of [planEndpoint(edge.source),planEndpoint(edge.target)])degree.set(id,(degree.get(id)||0)+1);}
    const order=(a,b)=>networkPriority(a)-networkPriority(b)||(degree.get(b.id)||0)-(degree.get(a.id)||0)||a.id.localeCompare(b.id);
    if(networkFocusId&&!nodes.some(node=>node.id===networkFocusId))networkFocusId=null;
    let anchor=networkFocusId&&nodes.find(node=>node.id===networkFocusId);
    if(!anchor){
      if(view==='plans')anchor=nodes.find(node=>node.id===planFocusId&&['account','goal'].includes(node.kind))||nodes.filter(node=>node.kind==='account').sort((a,b)=>(degree.get(b.id)||0)-(degree.get(a.id)||0)||order(a,b))[0]||nodes.filter(node=>node.kind==='goal').sort(order)[0];
      else anchor=[...nodes].sort(order)[0];
    }
    if(!anchor)return {nodes:[],links:[],total:0,anchor:null};
    const direct=new Set(links.filter(edge=>[planEndpoint(edge.source),planEndpoint(edge.target)].includes(anchor.id)).flatMap(edge=>[planEndpoint(edge.source),planEndpoint(edge.target)]));
    const candidates=nodes.filter(node=>node.id!==anchor.id&&(!networkFocusId||direct.has(node.id))).sort((a,b)=>Number(!direct.has(a.id))-Number(!direct.has(b.id))||order(a,b));
    const visible=[anchor,...candidates.slice(0,networkLimit-1)],visibleIds=new Set(visible.map(node=>node.id));
    return {nodes:visible,links:links.filter(edge=>visibleIds.has(planEndpoint(edge.source))&&visibleIds.has(planEndpoint(edge.target))),total:candidates.length+1,anchor};
  }
  function showNetworkSeries(group){
    if(!canRepaint())return;networkSelectedDisplay=group.id;networkMark();
    inspector.replaceChildren(el('span',pt('周期预测展示组','Recurring forecast display group'),'eyebrow'),original('h3',group.label),el('span',pt('待核实预测','Unverified forecasts'),'fg-plan-detail-status is-candidate'));
    const rows=group.series_rows,pairs=el('dl',undefined,'fg-network-pairs');pairs.append(el('dt',pt('预测期数','Occurrences')),el('dd',rows.length),el('dt',pt('日期范围','Date range')),original('dd',rows[0].node.date+' — '+rows.at(-1).node.date));inspector.append(pairs);
    inspector.append(el('p',pt('这是同一来源的展示折叠；逐期金额与依据保持独立。','This display folds occurrences from one source. Amounts and evidence remain separate.'),'fg-plan-detail-note'));
    const expand=btn(pt('展开单期节点','Expand occurrence nodes'),()=>{networkSeriesOpen.add(group.id);networkSelectedDisplay=null;render();},'p-btn');expand.dataset.networkAction='expand-series';expand.dataset.seriesId=group.id;inspector.append(expand);
    const periods=el('details',undefined,'fg-network-occurrences');periods.append(el('summary',pt('逐期查看原记录','Inspect each original occurrence')+' · '+rows.length));
    for(const row of rows){const item=btn(row.node.date+' · '+money(row.node.amount_minor,row.node.currency),()=>{networkSelectedDisplay=null;show(row.node);networkMark();},'fg-index-button');item.dataset.occurrenceId=row.node.id;periods.append(item);}inspector.append(periods);layoutInspector(group);
  }
  function networkTools(selection){
    const tools=el('div',undefined,'fg-network-tools');const mode=networkFocusId?pt('直接关系','Direct connections'):pt('关系全图','Relationship map');tools.append(el('span',mode,'fg-network-mode'));
    if(networkFocusId){const name=original('span',nodeLabel(selection.anchor),'fg-network-focus-name');tools.append(name);const reset=btn(pt('恢复全图','Restore map'),()=>{networkFocusId=null;networkLimit=9;render();networkPersist();},'p-btn');reset.dataset.networkAction='reset';tools.append(reset);}
    if(selection.nodes.length<selection.total){const more=btn(pt('更多节点','More nodes')+' · '+(selection.total-selection.nodes.length),()=>{networkLimit+=8;render();},'p-btn');more.dataset.networkAction='more';tools.append(more);}
    else if(networkLimit>9){const less=btn(pt('收起节点','Show fewer nodes'),()=>{networkLimit=9;render();},'p-btn');less.dataset.networkAction='less';tools.append(less);}
    host.append(tools);
  }
  function render2d(){
    graph3d?._destructor();graph3d=null;host.replaceChildren();host.classList.add('fg-network');
    const selection=networkSelection(networkProjection());networkTools(selection);
    const ns=selection.nodes,es=selection.links,anchor=selection.anchor;
    if(!anchor){host.append(el('p',view==='preferences'?pt('尚无本人确认或待核实的认知资料。可在对话中补充后查看真实关系。','No personal statements or confirmed conditions are available. Add them in conversation to view their recorded connections.'):pt('此板块暂无可核验的节点关系。','No verified node relationships are available in this domain.'),'fg-network-empty'));legend();return;}
    const font=fontSize(),lineHeight=font+3,edgeFont=Math.max(12,font-2),scale=font/16;
    networkWidth=host.clientWidth;const available=host.clientWidth>=700?Math.max(700,host.clientWidth-32):900,margin=Math.max(194,font*9+60);
    const count=Math.max(1,ns.length-1),rx=count<=8?Math.max(150,(available-margin*2)/2):Math.max((available-margin*2)/2,count*25*scale),ry=Math.max(205,count*19)*scale,width=count<=8?available:rx*2+margin*2,height=ry*2+210*scale,cx=width/2,cy=height/2;
    const positions=new Map([[anchor.id,{x:cx,y:cy,angle:0,center:true}]]);
    let surrounding=ns.filter(node=>node.id!==anchor.id);
    if(view==='cashflow')surrounding.sort((a,b)=>Number(a.metric_family!=='period_收入')-Number(b.metric_family!=='period_收入')||networkPriority(a)-networkPriority(b)||a.id.localeCompare(b.id));
    // A stable ellipse separates node labels. The central object is real; no
    // invisible financial hub or invented relationship is added for layout.
    surrounding.forEach((node,index)=>{const angle=-Math.PI/2+index*2*Math.PI/count;positions.set(node.id,{x:cx+Math.cos(angle)*rx,y:cy+Math.sin(angle)*ry,angle});});
    const nodeInk=themeColor('--ink','#1c2637'),muted=themeColor('--muted','#64748b'),surface=themeColor('--surface-card','#f8fafe');
    const s=svg('svg',{viewBox:'0 0 '+width+' '+height,role:'group','aria-label':pt('个人财务知识图谱。可用键盘选择节点与连线，在右侧查看真实记录。','Personal financial knowledge graph. Use the keyboard to select nodes and connections and inspect their source records.'),'data-i18n-skip':''});s.classList.add('fg-svg','fg-network-svg');s.style.width=width+'px';s.style.height=height+'px';s.dataset.layout=view;
    const defs=svg('defs'),marker=svg('marker',{id:'fg-network-arrow',viewBox:'0 0 10 10',refX:8,refY:5,markerWidth:4,markerHeight:4,orient:'auto-start-reverse'});marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:muted}));defs.append(marker);s.append(defs);
    const occupied=[];
    const nodeRadius=node=>node.display_group?29:node.id===anchor.id?Math.max(35,radius(node,ns)):radius(node,ns);
    function labelGeometry(node){
      const p=positions.get(node.id),r=nodeRadius(node),wrapLimit=Math.max(4,Math.min(english()?18:9,Math.floor((width/2-rx-60)/(font*(english()?.56:1))))),lines=Array.from(nodeLabel(node)).length?nodeLabel(node).match(new RegExp('.{1,'+wrapLimit+'}','gu')):[''],short=lines.slice(0,2);if(lines.length>2)short[1]=short[1].slice(0,-1)+'…';
      if(p.center)return {x:p.x,y:p.y+r+25,anchor:'middle',lines:short};
      const side=Math.cos(p.angle),vertical=Math.sin(p.angle);
      if(Math.abs(side)>.8)return {x:p.x+Math.sign(side)*(r+12),y:p.y-(short.length-1)*lineHeight/2,anchor:side>0?'start':'end',lines:short};
      return {x:p.x,y:p.y+(vertical<0?-r-lineHeight-(short.length-1)*lineHeight-(node.display_group||node.date?lineHeight:0):r+lineHeight+6),anchor:'middle',lines:short};
    }
    for(const node of ns){const p=positions.get(node.id),r=nodeRadius(node),l=labelGeometry(node),w=Math.max(...l.lines.map(line=>Array.from(line).length))*font*(english()?.56:1);occupied.push({x:p.x-r-6,y:p.y-r-6,w:r*2+12,h:r*2+12});occupied.push({x:l.x-(l.anchor==='middle'?w/2:l.anchor==='end'?w:0)-4,y:l.y-font,w:w+8,h:l.lines.length*lineHeight+8+(node.display_group||node.date||p.center?lineHeight+4:0)});}
    const collides=box=>occupied.some(other=>box.x<other.x+other.w&&box.x+box.w>other.x&&box.y<other.y+other.h&&box.y+box.h>other.y);
    for(const [index,edge] of es.entries()){
      const a=positions.get(planEndpoint(edge.source)),b=positions.get(planEndpoint(edge.target)),na=ns.find(node=>node.id===planEndpoint(edge.source)),nb=ns.find(node=>node.id===planEndpoint(edge.target)),dx=b.x-a.x,dy=b.y-a.y,length=Math.hypot(dx,dy)||1,nx=-dy/length,ny=dx/length;
      const start={x:a.x+dx/length*(nodeRadius(na)+4),y:a.y+dy/length*(nodeRadius(na)+4)},end={x:b.x-dx/length*(nodeRadius(nb)+9),y:b.y-dy/length*(nodeRadius(nb)+9)};
      const hubEdge=na.id===anchor.id||nb.id===anchor.id,curve=hubEdge?0:(index%2?1:-1)*Math.min(55,length*.18),control={x:(start.x+end.x)/2+nx*curve,y:(start.y+end.y)/2+ny*curve};
      const group=svg('g',{role:'button',tabindex:0,'aria-label':edge.display_group?edgeLabel(edge)+' · '+edge.series.series_rows.length+pt('期预测展示折叠',' folded forecast occurrences'):edgeLabel(edge)});group.classList.add('fg-edge','fg-network-edge');
      if(edge.display_group){group.dataset.seriesId=edge.series.id;group.dataset.eventIds=JSON.stringify(edge.series.series_rows.map(row=>row.node.id));group.dataset.edgeIds=JSON.stringify(edge.series.series_rows.map(row=>row.edge.id));}else group.dataset.edgeId=edge.id;
      const dash=edge.derivation_type==='candidate'?'2 5':edge.derivation_type==='confirmed'?'7 4':null,color=relationshipColors[edge.derivation_type]||muted,path=`M${start.x},${start.y} Q${control.x},${control.y} ${end.x},${end.y}`;
      group.append(svg('path',{d:path,fill:'none',stroke:'transparent','stroke-width':18}),svg('path',{d:path,fill:'none',stroke:color,'stroke-opacity':.64,'stroke-width':edge.display_group?1.5:edgeWidth(edge,es),'marker-end':'url(#fg-network-arrow)',...(dash?{'stroke-dasharray':dash}:{})}));
      const raw=edge.display_group?t(edge.predicate)+' · '+edge.series.series_rows.length+pt('期',' periods'):edgeLabel(edge),line=Array.from(raw).length>16?Array.from(raw).slice(0,15).join('')+'…':raw,lw=Math.max(44,Array.from(line).length*(english()?edgeFont*.56:edgeFont)+14),lh=edgeFont+10;let labelBox;
      for(const fraction of [.55,.68,.42,.78]){for(const shift of [0,24,-24,45,-45]){const x=(1-fraction)**2*start.x+2*(1-fraction)*fraction*control.x+fraction**2*end.x+nx*shift,y=(1-fraction)**2*start.y+2*(1-fraction)*fraction*control.y+fraction**2*end.y+ny*shift,box={x:x-lw/2,y:y-lh/2,w:lw,h:lh};if(!collides(box)){labelBox=box;break;}}if(labelBox)break;}
      const redundantCashflow=view==='cashflow'&&['形成收入','用于消费','经济净收入','经济净支出'].includes(edge.predicate);
      if(labelBox&&!redundantCashflow){occupied.push(labelBox);const label=svg('g',{'class':'fg-network-edge-label'});label.append(svg('rect',{x:labelBox.x,y:labelBox.y,width:lw,height:lh,rx:6,fill:surface,'fill-opacity':.94}),svg('text',{x:labelBox.x+lw/2,y:labelBox.y+edgeFont+3,'text-anchor':'middle',fill:muted,'font-size':edgeFont},line));group.append(label);}
      group.append(svg('title',{},raw));const activate=()=>{if(edge.display_group)showNetworkSeries(edge.series);else{networkSelectedDisplay=null;show(edge);networkMark();}};group.onclick=activate;group.onkeydown=event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();activate();}};s.append(group);
    }
    for(const node of ns){
      const p=positions.get(node.id),r=nodeRadius(node),label=labelGeometry(node),color=colors[node.kind]||colors.account,group=svg('g',{role:'button',tabindex:0,'aria-label':node.display_group?node.label+' · '+node.series_rows.length+pt('期预测展示组',' forecast occurrence display group'):nodeLabel(node)});group.classList.add('fg-node','fg-network-node');group.dataset.networkAnchor=String(!!p.center);
      if(node.display_group){group.dataset.seriesId=node.id;group.dataset.eventIds=JSON.stringify(node.series_rows.map(row=>row.node.id));group.dataset.edgeIds=JSON.stringify(node.series_rows.map(row=>row.edge.id));}else{group.dataset.nodeId=node.id;group.id='fg-node-'+node.id;}
      const candidate=node.status==='candidate'||node.derivation_type==='candidate';group.append(svg('circle',{cx:p.x,cy:p.y,r,fill:color,'fill-opacity':p.center?.19:.13,stroke:color,'stroke-width':p.center?2:1.5,...(candidate?{'stroke-dasharray':'3 4'}:{})}));
      if(node.display_group)group.append(svg('text',{x:p.x,y:p.y+5,'text-anchor':'middle',fill:color,'font-size':Math.max(13,font-2),'font-weight':600},node.series_rows.length+pt('期','×')));
      else group.append(svg('circle',{cx:p.x,cy:p.y,r:4,fill:color}));
      const name=svg('text',{'text-anchor':label.anchor,fill:nodeInk,'font-size':font,'font-weight':p.center?650:550,'class':'fg-network-label','translate':'no'});label.lines.forEach((line,index)=>name.append(svg('tspan',{x:label.x,y:label.y+index*lineHeight},line)));group.append(name);
      if(node.display_group)group.append(svg('text',{x:label.x,y:label.y+label.lines.length*lineHeight+2,'text-anchor':label.anchor,fill:muted,'font-size':Math.max(12,font-3)},pt('周期预测展示组','Forecast display group')));
      else if(!p.center&&node.date)group.append(svg('text',{x:label.x,y:label.y+label.lines.length*lineHeight+2,'text-anchor':label.anchor,fill:muted,'font-size':Math.max(12,font-3)},node.date));
      else if(p.center){const value=caption(node);if(value)group.append(svg('text',{x:label.x,y:label.y+label.lines.length*lineHeight+5,'text-anchor':label.anchor,fill:muted,'font-size':Math.max(13,font-2)},(node.metric_family==='net_assets_principal'?pt('本金净额 ','Principal net amount '):'')+value));}
      group.append(svg('title',{},nodeLabel(node)+(node.display_group?' · '+node.series_rows.length+pt('期，逐期记录独立',' occurrences, each record is independent'):caption(node)?' · '+caption(node):'')+(node.date?' · '+node.date:'')));
      const activate=()=>{if(node.display_group)showNetworkSeries(node);else{networkSelectedDisplay=null;show(node);networkMark();}};group.onclick=activate;group.onkeydown=event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();activate();}};s.append(group);
    }
    const scroll=el('div',undefined,'fg-network-scroll');scroll.append(s);host.append(scroll);
    // Measure after mounting: two-line names, occurrence dates and series
    // subtitles extend beyond the circle. Keep a 1:1 viewport and font size;
    // only add the vertical space that the actual rendered content needs.
    const padding=18;let top=Math.min(0,...occupied.map(box=>box.y-padding)),bottom=Math.max(height,...occupied.map(box=>box.y+box.h+padding));
    if(typeof s.getBBox==='function')try{const bounds=s.getBBox();if(bounds.height>0){top=Math.min(top,bounds.y-padding);bottom=Math.max(bottom,bounds.y+bounds.height+padding);s.dataset.contentBounds=JSON.stringify({x:bounds.x,y:bounds.y,width:bounds.width,height:bounds.height});}}catch{/* Hidden/unsupported SVGs retain the geometry estimate. */}
    const fittedHeight=bottom-top;s.setAttribute('viewBox','0 '+top+' '+width+' '+fittedHeight);s.style.height=fittedHeight+'px';
    if(!es.length)host.append(el('p',pt('当前节点尚无已记录的连线。','No source connections are recorded between these nodes.'),'fg-network-note'));networkMark();legend();
  }
  function threeLabel(n){
    // SpriteText 的 padding 与 textHeight 同为世界单位；禁用距离衰减后，按相机视角换算固定屏幕字号。
    const value=caption(n),fov=graph3d?.camera?.()?.fov||50,height=fontSize()*2*Math.tan(Math.PI*fov/360)/(host.clientHeight||480);
    const l=new NavrenNodeText(labelLines(nodeLabel(n)).join('\n')+(value?'\n'+value:''),height);
    l.fontFace='system-ui, "PingFang SC", sans-serif';l.color=themeColor('--ink','#1c2637');l.backgroundColor=themeColor('--surface-card','#f8fafe');l.padding=height*.15;
    l.material.sizeAttenuation=false;l.material.depthWrite=false;l.material.depthTest=false;
    l.center.y=-.5;return l;
  }
  function render3d(){
    graph3d?._destructor();host.replaceChildren();
    if(!window.ForceGraph3D||!window.NavrenNodeText){dimension='2d';render2d();return;}
    const h=host.clientHeight||480;
    graph3d=ForceGraph3D()(host).width(host.clientWidth).height(h).backgroundColor(themeColor('--surface-card','#f8fafe')).showNavInfo(false).nodeColor(n=>colors[n.kind]||colors.account).nodeVal(n=>volume(n,data.nodes))
      .nodeThreeObject(n=>threeLabel(n)).nodeThreeObjectExtend(true)
      .linkColor(e=>relationshipColors[e.derivation_type]||'#96a1b0').linkWidth(e=>edgeWidth(e,data.links)).linkDirectionalArrowLength(3).linkLabel(e=>escapeHTML(t(e.predicate)+' · '+relationship(e))).onNodeClick(n=>show(n)).onLinkClick(e=>show(e)).cooldownTicks(70).onEngineStop(()=>graph3d?.zoomToFit(0,60));
    graph3d.d3Force('charge').strength(-200);graph3d.graphData({nodes:structuredClone(data.nodes),links:structuredClone(data.links)});legend();
  }
  function cards(){const box=root.querySelector('[data-insights]');box.replaceChildren();
    if(view==='overview'){
      box.classList.add('fg-overview');
      for(const [id,label]of Object.entries(views)){
        const c=btn('',()=>selectView(id),'fg-overview-card');c.append(el('h3',label));
        const relevant=data.insights.filter(i=>i.view===id),metricRow=data.metrics?.find(m=>id==='cashflow'?Object.hasOwn(m,'daily_surplus_minor'):Object.hasOwn(m,'known_principal_net_assets_minor'));
        const amount=id==='cashflow'?metricRow?.daily_surplus_minor:id==='wealth'?metricRow?.net_assets_minor:null;
        c.append(el('strong',id==='cashflow'?'结余 '+money(amount,metricRow?.currency):id==='wealth'?(amount==null?'资产与负债分别查看':'净资产 '+money(amount,metricRow?.currency)):id==='plans'?data.nodes.filter(n=>n.kind==='goal').length+' 项目标':data.nodes.filter(n=>n.kind==='memory'&&n.status==='confirmed').length+' 项确认条件'));
        const lines={cashflow:'收入来源、消费去向和本期结余',wealth:'现金、投资权益、应收与负债',plans:'目标、家庭责任、生活安排与保障',preferences:'本人条件、知识、选择与反馈'};c.append(el('p',lines[id]));
        if(relevant.length)c.append(el('p',relevant[0].title));else c.append(el('p','尚缺确认资料，可在对话中补充。'));
        box.append(c);
      }return;
    }
    box.classList.remove('fg-overview');
    for(const i of data.insights.slice(0,3)){const c=el('article',undefined,'fg-insight');c.append(el('h3',i.title));if(Number.isSafeInteger(i.amount_minor))c.append(el('strong',money(i.amount_minor,i.currency)));const actions=el('div',undefined,'fg-insight-actions');actions.append(btn('查看依据',()=>show(i)),btn('问 Navren',()=>ask({insight_id:i.id},'解释这项洞察及其计算依据。')));c.append(actions);box.append(c);}
  }
  function selectView(id){
    if(!views[id]&&id!=='overview')return;
    networkFocusId=null;networkLimit=9;networkSeriesOpen.clear();networkSelectedDisplay=null;selectedId=null;const p=parameters();return NavrenViews.open('graph',{source_scope:p.source,portfolio_id:p.portfolio,filters:{...p,graph_view:id,dimension,metric} });
  }
  function render(){
    root.querySelector('[data-meta]').textContent='';
    configurePlanLayout();

    root.querySelector('[data-gaps]').replaceChildren(...data.coverage.gaps.map(g=>el('p',g)));
    root.querySelector('[data-consent]').textContent=data.memory_enabled?'本人陈述经确认后进入个人资料；可以在“Navren 了解的我”中修正、删除。':'对话记忆已关闭，账本关系仍可查看；不会在对话中读取未启用的个人记忆。';
    if(view==='plans'){const anchor=networkSelection(networkProjection()).anchor;if(anchor&&!anchor.display_group)planFocusId=anchor.id;planSummaries(anchor);}else cards();root.querySelector('.fg-views').hidden=view==='overview';root.querySelector('.finance-graph-stage').hidden=view==='overview';root.querySelector('.fg-controls').hidden=view==='overview';if(view!=='overview'){if(dimension==='3d')render3d();else render2d();}
    const list=root.querySelector('[data-index]');list.replaceChildren();const categories={account:'账户',holding:'持仓',category:'收支类别',merchant:'商户',contract:'合同与责任',goal:'长期目标',memory:'本人陈述',concept:'计算与概念',event:'日历与事项',person:'关系人物',profile:'业务配置'};
    for(const [kind,label]of Object.entries(categories)){const items=data.nodes.filter(n=>n.kind===kind);if(!items.length)continue;const group=el('details',undefined,'fg-index-group');group.dataset.kind=kind;group.append(el('summary',label+' · '+items.length));const rows=el('div');for(const n of items){const b=btn(nodeLabel(n),()=>show(n),'fg-index-button');b.setAttribute('translate','no');rows.append(b);}group.append(rows);list.append(group);}
    const path=root.querySelector('[data-paths]');path.replaceChildren();for(const e of data.links){const a=data.nodes.find(n=>n.id===e.source),b=data.nodes.find(n=>n.id===e.target);if(!a||!b)continue;const bPath=btn(nodeLabel(a)+' → '+edgeLabel(e)+' → '+nodeLabel(b)+(Object.hasOwn(e,'amount_minor')?' · '+money(e.amount_minor,e.currency):''),()=>show(e),'fg-path');bPath.setAttribute('translate','no');path.append(bPath);}
    const projection=networkProjection(),selectedGroup=networkSelectedDisplay&&projection.nodes.find(node=>node.id===networkSelectedDisplay&&node.display_group);
    const currentObject=selectedId&&[...data.nodes,...data.links,...data.insights].find(object=>object.id===selectedId);
    const anchor=view!=='overview'&&networkSelection(projection).anchor;
    if(selectedGroup)showNetworkSeries(selectedGroup);
    else if(currentObject)show(currentObject);
    else if(anchor&&!anchor.display_group)show(anchor);
    else if(data.insights[0])show(data.insights[0]);
    else if(data.nodes[0])show(data.nodes[0]);
    else inspector.replaceChildren(el('p','此视图暂无可核验的关系。'));
    root.querySelector('[data-allocate]').hidden=view!=='plans'||data.source!=='simulation'||!!data.portfolio;root.querySelector('[data-scenario]').hidden=view!=='plans'||data.source!=='simulation'||!!data.portfolio;
    root.querySelector('[data-decision]').hidden=view!=='preferences'||!!data.portfolio;root.querySelector('[data-rank]').replaceChildren();root.querySelector('[data-timeline]').replaceChildren();
  }
  async function refresh(extra={}){
    if(!root)return;
    const id=++seq;root.querySelector('[data-meta]').textContent='正在核对记录与关系…';graph3d?.pauseAnimation();data=null;dataGuard=null;host.replaceChildren();inspector.replaceChildren();for(const key of ['[data-insights]','[data-index]','[data-paths]','[data-rank]','[data-timeline]'])root.querySelector(key)?.replaceChildren();
    try{await BankClient.ensure();const guard=await BankClient.guard();const p=parameters(extra),qs=new URLSearchParams(Object.entries(p).filter(([k,v])=>v));const result=await BankClient.get('/personal-graph?'+qs);if(id!==seq||!guard.valid(result)||root.closest('[hidden]'))return;data=result;dataGuard=guard;if(!root.querySelector('[data-from]').value){root.querySelector('[data-from]').value=data.period.from;const d=new Date(data.period.to_exclusive+'T00:00:00Z');d.setUTCDate(d.getUTCDate()-1);root.querySelector('[data-to]').value=d.toISOString().slice(0,10);}render();}
    catch(e){if(id===seq)root.querySelector('[data-meta]').textContent=e.message;}
  }
  function invalidate(){seq++;data=null;dataGuard=null;selectedId=null;planFocusId=null;planTrail=[];planLimit=6;networkFocusId=null;networkLimit=9;networkSeriesOpen.clear();networkSelectedDisplay=null;for(const d of document.querySelectorAll('.fg-dialog')){d.close();d.remove();}graph3d?._destructor();graph3d=null;if(!root)return;host?.replaceChildren();inspector?.replaceChildren();for(const key of ['[data-insights]','[data-index]','[data-paths]','[data-rank]','[data-timeline]','[data-gaps]'])root.querySelector(key)?.replaceChildren();root.querySelector('[data-meta]').textContent='资料权限已变化，请重新核对。';}
  async function restore(context={}){
    if(!root)return;
    const filters=context.filters||{},u=new URL(location.href);view=views[filters.graph_view]?filters.graph_view:'overview';metric=['amount','count','share'].includes(filters.metric)?filters.metric:'amount';dimension=filters.dimension==='3d'?'3d':'2d';selectedId=filters.selected_id||null;planFocusId=filters.plan_focus||null;planQuestion=filters.plan_question==='goal'?'goal':'liquidity';planLimit=6;planTrail=[];networkFocusId=filters.network_focus||null;networkLimit=9;networkSeriesOpen=new Set();networkSelectedDisplay=null;
    const source=context.source_scope||filters.source||u.searchParams.get('source')||'simulation';if(!['simulation','imported'].includes(source)){invalidate();root.querySelector('[data-meta]').textContent='此来源不支持个人财务关系，请从“我的”重新进入。';root.querySelector('.fg-controls').hidden=false;return;}root.querySelector('[data-source]').value=source;
    for(const key of ['from','to'])root.querySelector('[data-'+key+']').value=filters[key]||u.searchParams.get(key)||'';
    const portfolio=context.portfolio_id||filters.portfolio||u.searchParams.get('portfolio')||'current',select=root.querySelector('[data-portfolio]');if(![...select.options].some(o=>o.value===portfolio)){const option=el('option','指定历史版本');option.value=portfolio;select.append(option);}select.value=portfolio;root.querySelector('[data-metric]').value=metric;root.querySelector('[data-dimension]').value=dimension;for(const b of root.querySelectorAll('.fg-view-tab'))b.setAttribute('aria-selected',String(b.dataset.view===view));await refresh();
  }
  function dialog(title){for(const old of document.querySelectorAll('.fg-dialog')){old.close();old.remove();}const d=el('dialog',undefined,'fg-dialog');d.append(el('h2',title),btn('关闭',()=>d.close()));d.addEventListener('close',()=>d.remove());document.body.append(d);d.showModal();return d;}
  function field(d,label,type='text'){const row=el('label',label,'fg-field'),input=el('input');input.type=type;row.append(input);d.append(row);return input;}
  async function allocation(){const turn=seq,guard=await BankClient.guard();const r=await BankClient.get('/personal-graph?view=goals&source=simulation');const all=await BankClient.get('/personal-graph?view=records&source=simulation');if(turn!==seq||!guard.valid(r)||!guard.valid(all)||root.closest('[hidden]'))return;const goals=all.nodes.filter(n=>n.kind==='goal'),accounts=all.nodes.filter(n=>n.kind==='account');const d=dialog('确认目标规划分配');d.append(el('p','这是一项规划记录，没有冻结、预留或扣款。'));
    function select(label,rows){const l=el('label',label,'fg-field'),s=el('select');for(const n of rows){const o=el('option',n.label);o.value=n.id.split(':').slice(1).join(':');s.append(o);}l.append(s);d.append(l);return s;}
    const goal=select('目标',goals),account=select('资金来源',accounts),amount=field(d,'规划金额（元）','number');amount.min='0.01';amount.step='.01';const check=field(d,'我确认把这笔可规划资金关联到此目标','checkbox'),status=el('p',undefined,'fg-limit');d.append(status,btn('确认规划分配',async()=>{try{if(!check.checked)throw new Error('请先确认规划分配。');const cents=parseMinor(amount.value);await BankClient.post('/graph/allocations',{goal_id:goal.value,account_id:account.value,amount_minor:cents,expected_version:r.version,confirmed:true});d.close();refresh();}catch(e){status.textContent=e.message;}}));
    for(const e of all.links.filter(e=>e.predicate==='本人确认规划分配')){const id=e.evidence_refs[0].source_id;d.append(btn('移除 '+money(e.amount_minor,e.currency)+' 的规划关联',async()=>{try{await BankClient.post('/graph/allocations/delete',{allocation_id:id});d.close();refresh();}catch(e){status.textContent=e.message;}}));}
  }
  async function decisionForm(){const turn=seq,guard=await BankClient.guard();const d=dialog('记录我的选择');d.append(el('p','保留你的选择、理由和反馈；保存记录不会执行资金操作。'));const alternatives=field(d,'考虑过的方案（用中文分号分隔）'),choice=field(d,'最终选择'),reason=field(d,'选择理由'),feedback=field(d,'反馈（选填）'),expires=field(d,'有效至（选填）','date'),confirm=field(d,'我确认这是自己的选择','checkbox'),status=el('p',undefined,'fg-limit');const refs=[];const all=await BankClient.get('/personal-graph?'+new URLSearchParams({...parameters(),view:'overview'}));if(turn!==seq||!guard.valid(all)||!d.isConnected){if(d.isConnected)d.close();return;}const related=el('details');related.append(el('summary','关联本人条件、目标或产品（选填）'));d.append(related);for(const n of all.nodes.filter(n=>(n.kind==='memory'&&n.status==='confirmed'&&n.role!=='decision')||['goal','holding'].includes(n.kind))){const b=field(related,'作为依据：'+n.label,'checkbox');b.onchange=()=>{for(const r of n.evidence_refs||[]){const i=refs.findIndex(x=>x.source_id===r.source_id&&x.source_table===r.source_table);if(b.checked&&i<0)refs.push({source_scope:r.source_scope,source_table:r.source_table,source_id:r.source_id});else if(!b.checked&&i>=0)refs.splice(i,1);}};}d.append(status,btn('确认保存选择',async()=>{try{if(!confirm.checked)throw Error('请确认是本人选择。');await BankClient.post('/graph/decisions',{choice:choice.value.trim(),alternatives:alternatives.value.split(/[；;]/).map(s=>s.trim()).filter(Boolean),reason:reason.value.trim(),feedback:feedback.value.trim(),...(expires.value?{expires_on:expires.value}:{}),evidence_refs:refs,confirmed:true});d.close();await refresh();}catch(e){status.textContent=e.message;}}));}
  function parseMinor(v){if(!/^\d+(\.\d{1,2})?$/.test(v))throw new Error('金额最多两位小数。');const [a,b='']=v.split('.');const n=Number(a)*100+Number(b.padEnd(2,'0'));if(!Number.isSafeInteger(n))throw new Error('金额超出范围。');return n;}
  async function scenario(){const turn=seq,guard=await BankClient.guard();const all=await BankClient.get('/personal-graph?view=records&source=simulation');if(turn!==seq||!guard.valid(all)||root.closest('[hidden]'))return;const d=dialog('比较资金规划情景');d.append(el('p','调整目标或应急底线，比较现有时间轴与规划结果。不会执行金融操作。'));const account=el('select');for(const n of all.nodes.filter(n=>n.kind==='account')){const o=el('option',n.label);o.value=n.id.slice(8);account.append(o);}d.append(account);const floor=field(d,'应急底线（元，可留空）','number');floor.step='.01';const fields=[];
    for(const n of all.nodes.filter(n=>n.kind==='goal')){const details=el('details');details.append(el('summary',n.label));d.append(details);const amount=field(details,'本情景投入金额（元，留空则不计入）','number'),on=field(details,'计划支出日期','date'),monthly=field(details,'每月可投入假设（元，可留空）','number');monthly.step='.01';amount.step='.01';on.value=n.due_on;fields.push({n,amount,on,monthly});}
    const out=el('div');d.append(out,btn('计算并比较',async()=>{try{const p={account_id:account.value,expected_version:all.version,horizon_days:90,goals:fields.filter(f=>f.amount.value).map(f=>({goal_id:f.n.id.slice(5),amount_minor:parseMinor(f.amount.value),date:f.on.value,...(f.monthly.value?{monthly_contribution_minor:parseMinor(f.monthly.value)}:{})}))};if(floor.value)p.floor_minor=parseMinor(floor.value);const r=await BankClient.post('/graph/scenarios',p);out.replaceChildren(el('h3','规划底线缺口 '+money(r.planning_floor_shortfall_minor,r.currency)),el('p','原时间轴最低可用 '+money(r.baseline.minimum_available_minor,r.currency)+'；此情景最低可用 '+money(r.minimum_available_minor,r.currency)),el('p',r.notice));for(const projection of r.goal_projections||[])out.append(el('p','规划假设目标缺口 '+money(projection.planning_gap_minor,r.currency)+' · '+projection.notice));for(const row of r.rows)out.append(el('p',row.date+' · 日终现金 '+money(row.cash_after_plans_minor,r.currency)+' · 扣除底线后 '+money(row.available_after_plans_minor,r.currency)));}catch(e){out.textContent=e.message;}}));
  }
  function init(panel,context={}){root=panel;root.innerHTML='<div class="fg-views" role="tablist" aria-label="财务图谱视图"></div><div class="fg-controls"><input type="hidden" data-source value="simulation"><label>从<input data-from type="date"></label><label>至<input data-to type="date"></label><label>组合<select data-portfolio><option value="current">当前账户</option><option value="historical">历史组合</option></select></label><label>大小口径<select data-metric><option value="amount">金额</option><option value="count">笔数</option><option value="share">子图占比</option></select></label><label>视图<select data-dimension><option value="2d">二维关系</option><option value="3d">三维探索</option></select></label><div class="fg-control-actions"><button class="p-btn" data-refresh>更新资料</button><div class="fg-help"></div></div></div><div class="fg-status" data-meta role="status"></div><div class="fg-insights" data-insights></div><div class="fg-plan-actions"><button class="p-btn" data-allocate hidden>规划目标资金</button><button class="p-btn" data-scenario hidden>比较规划情景</button><button class="p-btn" data-decision hidden>记录我的选择</button></div><div class="finance-graph-stage"><div class="fg-view"><div class="fg-canvas"></div><details class="fg-network-guide"><summary>如何阅读图谱</summary><p class="fg-legend" data-legend></p><p class="fg-key"></p><p class="fg-key" data-causality></p></details></div><aside class="fg-inspector" aria-live="polite"></aside></div><div data-rank></div><div data-timeline></div><details class="fg-node-index"><summary>查找资料与节点</summary><input type="search" data-search placeholder="按名称查找" aria-label="按名称查找节点"><div data-index></div></details><details class="fg-mobile-paths"><summary>按路径阅读关系</summary><div data-paths></div></details><details class="fg-coverage"><summary>数据覆盖与依据边界</summary><div data-gaps></div><p>导入账单按各自覆盖期间独立计算。</p></details><p class="fg-consent" data-consent></p>';
    root.querySelector('.fg-help').append(NavrenHelp.button('graph'));host=root.querySelector('.fg-canvas');inspector=root.querySelector('.fg-inspector');for(const [id,name] of Object.entries(views)){const b=btn(name,()=>selectView(id),'fg-view-tab');b.dataset.view=id;b.role='tab';b.setAttribute('aria-selected',String(id===view));root.querySelector('.fg-views').append(b);}
    for(const s of ['[data-from]','[data-to]','[data-portfolio]'])root.querySelector(s).onchange=()=>{if(view==='plans'){selectedId=null;planLimit=6;planTrail=[];inspector.replaceChildren();}NavrenViews.update({source_scope:parameters().source,portfolio_id:parameters().portfolio,filters:{...parameters(),graph_view:view,network_focus:networkFocusId,dimension,metric}});refresh();};root.querySelector('[data-metric]').onchange=e=>{metric=e.target.value;NavrenViews.update({filters:{...parameters(),graph_view:view,network_focus:networkFocusId,dimension,metric}});if(canRepaint())render();};root.querySelector('[data-dimension]').onchange=e=>{dimension=e.target.value;NavrenViews.update({filters:{...parameters(),graph_view:view,network_focus:networkFocusId,dimension,metric}});if(canRepaint())render();};root.querySelector('[data-refresh]').onclick=()=>refresh();root.querySelector('[data-allocate]').onclick=()=>allocation().catch(e=>showToast(e.message));root.querySelector('[data-scenario]').onclick=()=>scenario().catch(e=>showToast(e.message));root.querySelector('[data-decision]').onclick=()=>decisionForm().catch(e=>showToast(e.message));root.querySelector('[data-search]').oninput=e=>{const value=e.target.value;for(const group of root.querySelectorAll('.fg-index-group')){let visible=0;for(const b of group.querySelectorAll('button')){b.hidden=!b.textContent.includes(value);if(!b.hidden)visible++;}group.hidden=!visible;if(value&&visible)group.open=true;}};
    observer=new ResizeObserver(()=>{if(graph3d&&canRepaint())graph3d.width(host.clientWidth).height(host.clientHeight||480).nodeThreeObject(n=>threeLabel(n));else if(canRepaint()&&dimension==='2d'&&Math.abs(host.clientWidth-networkWidth)>2)queueRepaint();});observer.observe(host);styleObserver=new MutationObserver(queueRepaint);watchStyles();window.addEventListener('navren:language',queueRepaint);window.addEventListener('navren:theme',queueRepaint);window.addEventListener('navren:section',e=>{if(e.detail==='graph'&&canRepaint())graph3d?.resumeAnimation();else graph3d?.pauseAnimation();});window.addEventListener('pagehide',()=>{observer.disconnect();styleObserver.disconnect();invalidate();});window.addEventListener('pageshow',e=>{if(e.persisted){observer.observe(host);watchStyles();/* 由NavrenViews重新核验身份后读取，不能绘制旧缓存。 */}});return restore(context);
  }
  window.addEventListener('navren:privacy-reset',invalidate);
  return {init,refresh,restore,invalidate,pause:()=>graph3d?.pauseAnimation()};
})();
