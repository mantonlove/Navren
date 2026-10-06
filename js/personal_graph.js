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
  let root,data,dataGuard,host,inspector,graph3d,observer,styleObserver,repaintQueued=false,seq=0,view='overview',metric='amount',dimension='2d',expanded=false,selectedId=null;
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
    const d=el('details',undefined,'fg-evidence');d.append(el('summary','查看全部 '+(refs?.length||0)+' 条计算依据'));
    for(const ref of refs||[]){
      const row=el('section',undefined,'fg-evidence-record');row.append(el('p',(ref.occurred_at?ref.occurred_at.slice(0,10)+' · ':'')+(tableNames[ref.source_table]||'来源记录')+(ref.amount_minor!=null?' · '+money(ref.amount_minor):'')));
      const out=el('div'),read=btn('核对原记录',async()=>{const turn=seq;try{const guard=await BankClient.guard(),q=new URLSearchParams({source:ref.source_scope,table:ref.source_table,id:ref.source_id}),result=await BankClient.get('/graph/evidence?'+q);if(turn!==seq||!guard.valid(result)||!row.isConnected)return;out.replaceChildren();const names={amount_minor:'金额',balance_minor:'余额',target_minor:'目标金额',saved_minor:'已安排金额',merchant:'商户',occurred_at:'发生时间',kind:'记录类型',status:'状态',category:'类别',value:'本人陈述',quote:'本人原话',name:'名称',due_on:'目标截止日',currency:'币种',expires_on:'有效至',label:'内容',delta_minor:'流水金额',outstanding_minor:'未偿本金',principal_minor:'本金',started_on:'开始日期',matures_on:'到期日',date:'日期'};for(const [key,value]of Object.entries(result.record||{}))if(names[key])out.append(el('p',names[key]+'：'+(key.endsWith('_minor')?money(value,result.record.currency):typeof value==='object'?JSON.stringify(value):value)));out.append(sourceLink(ref));}catch(error){if(turn===seq)out.replaceChildren(el('p',error.message));}},'fg-index-button');row.append(read,out);d.append(row);
    }return d;
  }
  function show(object){
    if(!canRepaint()||!inspector)return;
    const expectedRevision=data.personal_revision,confirmationToken=object.confirmation_token;
    selectedId=object.id;
    window.NavrenViews?.update({filters:{...parameters(),graph_view:view,selected_id:selectedId,dimension,metric}});
    inspector.replaceChildren(el('span',object.predicate?t('关系与计算'):t('记录与依据'),'eyebrow'),original('h3',object.predicate?edgeLabel(object):object.label?nodeLabel(object):object.title));
    const pairs=[['金额',Object.hasOwn(object,'amount_minor')?money(object.amount_minor,object.currency):null],['笔数',object.transaction_count],['期限',object.date||object.due_on||object.next_on],['目标金额',Object.hasOwn(object,'target_minor')?money(object.target_minor,object.currency):null],['记录口径',({cash_stock:'账户余额存量',period_支出:'期间现金净支出',period_收入:'期间收入',contract_cycle:'单个合同周期',goal_target:'目标金额',future_obligation:'未来义务'}[object.metric_family]||object.metric_family)],['关系类型',object.derivation_type?relationship(object):null],['说明',object.notice||object.metric_definition||object.refund_policy]];
    for(const [label,value] of pairs)if(value!=null)inspector.append(el('p',label+'：'+value));
    if(object.decision){const d=object.decision;inspector.append(el('p','替代项：'+d.alternatives.join('、')),el('p','选择理由：'+d.reason),el('p','执行：'+(d.execution_ids.length?'已有账户回执':'尚无执行回执')),el('p','反馈：'+(d.feedback||'待补充')));}
    if(object.returns)for(const [f,label]of [['daily_pnl_minor','区间损益'],['unrealized_pnl_minor','持仓损益'],['closing_equity_minor','投资权益']]){const value=object.returns[f],p=el('p',t(label)+'：');p.append(el('strong',(f!=='closing_equity_minor'&&Number.isSafeInteger(value)&&value>0?'+':'')+money(value,object.currency),f==='closing_equity_minor'?undefined:'pnl-'+(!Number.isSafeInteger(value)?'unknown':value>0?'positive':value<0?'negative':'zero')));inspector.append(p);}
    if(object.quote)inspector.append(el('p',t('本人原话'),'eyebrow'),original('blockquote',object.quote));
    if(object.status==='candidate')inspector.append(el('p','这条陈述等待你核实，尚未作为事实。','fg-limit'));
    const c=data.calculations?.find(c=>c.id===object.calculation_id);
    if(c){inspector.append(el('p','期间 '+c.period.from+' 至 '+c.period.to_exclusive+'（末日不含）'),el('p','平均每笔 '+money(c.average_minor,c.currency)),el('p',c.change_minor==null?'相邻期间比较不可用':'已记录金额变化 '+money(c.change_minor,c.currency)),el('p',c.comparison_notice));}
    const refs=object.evidence_refs||[];inspector.append(evidence(refs));
    for(const c of data.related_knowledge||[]){const d=el('details');d.append(el('summary','相关概念：'+c.label),el('p',c.summary));const a=el('a',c.publisher);a.href=c.url;a.target='_blank';a.rel='noopener noreferrer';d.append(a);inspector.append(d);}
    if(object.observation_id)for(const [decision,label] of [['confirm','确认资料'],['delete','删除资料']]){
      if(decision==='confirm'&&object.status==='confirmed')continue;
      inspector.append(btn(label,async()=>{if(!confirm(decision==='delete'?'删除这项资料及派生关系？银行交易不会删除。':'确认这是本人的陈述？不会改变业务风险或交易资格。'))return;try{if(!Number.isSafeInteger(expectedRevision)||decision==='confirm'&&!confirmationToken)throw Error('资料版本或确认凭据未取得，请重新核对。');const payload={decision,expected_revision:expectedRevision};if(decision==='confirm')payload.confirmation_token=confirmationToken;await BankClient.post('/profile-observations/'+object.observation_id,payload);await BankClient.refreshIdentity();await refresh();}catch(e){showToast(e.message);}}));
    }
    inspector.append(btn('问 Navren',()=>ask(object.id?object.predicate?{edge_id:object.id}:object.label?{node_id:object.id}:{insight_id:object.id}:{},'解释这项财务关系的金额、时间口径和证据。')));
    if(object.fund_id){const a=el('a','产品与收益详情 ›'),u=new URL('account.html',location.href);u.searchParams.set('view','wealth-product');u.searchParams.set('object_kind','fund');u.searchParams.set('object_id',object.fund_id);u.searchParams.set('source_scope','historical');u.searchParams.set('portfolio_id',object.portfolio_version||data.portfolio_version);u.searchParams.set('date',object.nav_asof||data.nav_asof||'');a.href=u.pathname+u.search;inspector.append(a);}
    if(object.fund_id)inspector.append(btn('查看收益日历',()=>{const u=new URL('/account.html',location.href);u.hash='calendar';u.searchParams.set('view','calendar');u.searchParams.set('source_scope','historical');u.searchParams.set('portfolio_id',object.portfolio_version||data.portfolio_version);if(object.nav_asof||data.nav_asof)u.searchParams.set('date',object.nav_asof||data.nav_asof);location.href=u;}));
    const details=el('details');details.append(el('summary','时间与版本'),el('p','时点 '+data.as_of+'；净值日 '+(object.nav_asof||data.nav_asof||'未提供')),el('p','资料 '+data.version+'；组合 '+data.portfolio_version));inspector.append(details);
  }
  function svg(tag,attrs,text){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs||{}))n.setAttribute(k,v);if(text!=null)n.textContent=clean(text);return n;}
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
  function render2d(){
    graph3d?._destructor();graph3d=null;host.replaceChildren();
    const detailGroup=n=>n.kind==='merchant'?'merchant':n.kind==='event'&&n.derivation_type==='observed'?'event':view==='cashflow'&&n.kind==='category'&&n.metric_family!=='period_收入'?'consumption':null;
    const ns=expanded?data.nodes:data.nodes.filter(n=>!detailGroup(n)||data.nodes.filter(x=>detailGroup(x)===detailGroup(n)).indexOf(n)<6),ids=new Set(ns.map(n=>n.id)),es=data.links.filter(e=>ids.has(e.source)&&ids.has(e.target));
    const columns=[[],[],[]];for(const n of ns){const col=view==='cashflow'?(n.metric_family==='period_收入'?0:n.role==='surplus'?1:2):view==='wealth'?(n.role==='liability'?2:n.role==='net_assets'?1:0):view==='plans'?(n.kind==='account'||n.kind==='person'?0:n.kind==='goal'?1:2):(n.kind==='memory'?0:n.role==='condition'?1:2);columns[col].push(n);}
    const labelFont=fontSize(),lineHeight=labelFont+4;const extent=n=>Math.max(radius(n,ns),labelLines(nodeLabel(n)).length*lineHeight/2)+30;
    const heights=columns.map(col=>col.reduce((sum,n)=>sum+extent(n)*2+18,0)),height=Math.max(450,...heights)+60;
    const priority=n=>['surplus','net_assets','condition'].includes(n.role)||n.kind==='concept'&&['period_收入','period_支出'].includes(n.metric_family)?0:n.kind==='account'||n.kind==='goal'?1:n.kind==='category'||n.role==='liability'?2:3;
    const width=Math.max(690,host.clientWidth||690);const positions={};for(let c=0;c<3;c++){columns[c].sort((a,b)=>priority(a)-priority(b)||a.id.localeCompare(b.id));let y=30;for(const n of columns[c]){const h=extent(n);positions[n.id]={x:width*(c+.5)/3,y:y+h};y+=h*2+18;}}
    const s=svg('svg',{viewBox:'0 0 '+width+' '+height,role:'group','aria-label':t('个人财务关系图。每个节点显示名称，可用键盘逐项查看。'),'data-i18n-skip':''});s.classList.add('fg-svg');s.style.width=width+'px';s.style.height=height+'px';
    const defs=svg('defs');const m=svg('marker',{id:'fg-arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:5,markerHeight:5,orient:'auto-start-reverse'});m.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#809bac'}));defs.append(m);s.append(defs);
    for(const [i,e] of es.entries()){
      const a=positions[e.source],b=positions[e.target],r=radius(ns.find(n=>n.id===e.target),ns),dx=b.x-a.x,dy=b.y-a.y,len=Math.hypot(dx,dy)||1;
      const start={x:a.x+dx/len*24,y:a.y+dy/len*24},stop={x:b.x-dx/len*(r+7),y:b.y-dy/len*(r+7)};
      const width=edgeWidth(e,es);
      const g=svg('g',{role:'button',tabindex:0,'aria-label':edgeLabel(e)+'，'+(e.amount_minor==null?t('非定量关系'):money(e.amount_minor,e.currency))});g.classList.add('fg-edge');
      const dash=e.derivation_type==='candidate'?'2 5':e.derivation_type==='confirmed'?'7 5':null;
      g.append(svg('path',{d:`M${start.x},${start.y} Q${(a.x+b.x)/2},${(a.y+b.y)/2-12} ${stop.x},${stop.y}`,fill:'none',stroke:'#91aab7','stroke-width':width,'marker-end':'url(#fg-arrow)',...(dash?{'stroke-dasharray':dash}:{})}));
      g.append(svg('text',{x:a.x+dx*.43,y:a.y+dy*.43-10-i%2*9,'text-anchor':'middle',fill:'#5e7989','font-size':Math.max(13,labelFont-1)},edgeLabel(e)));
      g.onclick=()=>show(e);g.onkeydown=e2=>{if(['Enter',' '].includes(e2.key)){e2.preventDefault();show(e);}};s.append(g);
    }
    for(const n of ns){const p=positions[n.id],r=radius(n,ns),g=svg('g',{role:'button',tabindex:0,'aria-label':nodeLabel(n),transform:`translate(${p.x},${p.y})`});g.classList.add('fg-node');g.id='fg-node-'+n.id;
      g.append(svg('circle',{r,fill:colors[n.kind]||colors.account,'fill-opacity':.17,stroke:colors[n.kind]||colors.account,'stroke-width':n.status==='candidate'?2:1.5,...(n.status==='candidate'?{'stroke-dasharray':'3 3'}:{})}));
      const lines=labelLines(nodeLabel(n));const label=svg('text',{'text-anchor':'middle',fill:'#29485b','font-size':labelFont,'font-weight':600});
      lines.forEach((l,i)=>label.append(svg('tspan',{x:0,y:(i-(lines.length-1)/2)*lineHeight},l)));g.append(label);
      const value=caption(n);if(value)g.append(svg('text',{y:r+24,'text-anchor':'middle',fill:'#5d7788','font-size':Math.max(13,labelFont-1)},value));
      g.onclick=()=>show(n);g.onkeydown=e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();show(n);}};s.append(g);
    }
    host.append(s);if(ns.length<data.nodes.length)host.append(btn('展开其余 '+(data.nodes.length-ns.length)+' 个明细节点',()=>{expanded=true;render2d();}));else if(expanded)host.append(btn('收起明细节点',()=>{expanded=false;render2d();}));legend();
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
    for(const i of data.insights.slice(0,3)){const c=el('article',undefined,'fg-insight');c.append(el('h3',i.title),el('strong',money(i.amount_minor,i.currency)));c.append(btn('查看关系与依据',()=>show(i)),btn('问 Navren',()=>ask({insight_id:i.id},'解释这项洞察及其计算依据。')));box.append(c);}
  }
  function selectView(id){
    if(!views[id]&&id!=='overview')return;
    const p=parameters();return NavrenViews.open('graph',{source_scope:p.source,portfolio_id:p.portfolio,filters:{...p,graph_view:id,dimension,metric} });
  }
  function render(){
    root.querySelector('[data-meta]').textContent='资料截至 '+data.as_of+' · '+data.nodes.length+' / '+data.node_count_total+' 个节点';
    root.querySelector('[data-gaps]').replaceChildren(...data.coverage.gaps.map(g=>el('p',g)));
    root.querySelector('[data-consent]').textContent=data.memory_enabled?'本人陈述经确认后进入个人资料；可以在“Navren 了解的我”中修正、删除。':'对话记忆已关闭，账本关系仍可查看；不会在对话中读取未启用的个人记忆。';
    cards();root.querySelector('.fg-views').hidden=true;root.querySelector('.finance-graph-stage').hidden=view==='overview';root.querySelector('.fg-back').hidden=view==='overview';root.querySelector('.fg-controls').hidden=view==='overview';if(view!=='overview'){if(dimension==='3d')render3d();else render2d();}
    const list=root.querySelector('[data-index]');list.replaceChildren();const categories={account:'账户',holding:'持仓',category:'收支类别',merchant:'商户',contract:'合同与责任',goal:'长期目标',memory:'本人陈述',concept:'计算与概念',event:'日历与事项',person:'关系人物',profile:'业务配置'};
    for(const [kind,label]of Object.entries(categories)){const items=data.nodes.filter(n=>n.kind===kind);if(!items.length)continue;const group=el('details',undefined,'fg-index-group');group.dataset.kind=kind;group.append(el('summary',label+' · '+items.length));const rows=el('div');for(const n of items){const b=btn(nodeLabel(n),()=>show(n),'fg-index-button');b.setAttribute('translate','no');rows.append(b);}group.append(rows);list.append(group);}
    const path=root.querySelector('[data-paths]');path.replaceChildren();for(const e of data.links){const a=data.nodes.find(n=>n.id===e.source),b=data.nodes.find(n=>n.id===e.target);if(!a||!b)continue;const bPath=btn(nodeLabel(a)+' → '+edgeLabel(e)+' → '+nodeLabel(b)+(Object.hasOwn(e,'amount_minor')?' · '+money(e.amount_minor,e.currency):''),()=>show(e),'fg-path');bPath.setAttribute('translate','no');path.append(bPath);}
    if(selectedId&&[...data.nodes,...data.links,...data.insights].find(x=>x.id===selectedId))show([...data.nodes,...data.links,...data.insights].find(x=>x.id===selectedId));else if(data.insights[0])show(data.insights[0]);else if(data.nodes[0])show(data.nodes[0]);else inspector.replaceChildren(el('p','此视图暂无可核验的关系。'));
    root.querySelector('[data-allocate]').hidden=view!=='plans'||data.source!=='simulation'||!!data.portfolio;root.querySelector('[data-scenario]').hidden=view!=='plans'||data.source!=='simulation'||!!data.portfolio;
    root.querySelector('[data-decision]').hidden=view!=='preferences'||!!data.portfolio;root.querySelector('[data-rank]').replaceChildren();if(view==='cashflow'){const rank=root.querySelector('[data-rank]');rank.append(el('h3','本期已记录金额排序'));const max=Math.max(1,...data.insights.map(i=>Math.abs(i.amount_minor||0)));for(const i of data.insights){const b=btn(i.title+' · '+money(i.amount_minor,i.currency),()=>show(i),'fg-rank-row');b.style.setProperty('--bar',(Math.abs(i.amount_minor||0)/max*100)+'%');rank.append(b);}}root.querySelector('[data-timeline]').replaceChildren();if(data.timeline){const table=el('div',undefined,'fg-timeline');for(const slot of data.timeline.timeline)for(const a of slot.accounts)table.append(el('p',slot.date+' · '+money(a.available_after_confirmed_minor,a.currency)+' 可用 · 含候选 '+money(a.available_if_candidates_occur_minor,a.currency)));root.querySelector('[data-timeline]').append(el('h3','与财务日历共用的资金时间轴'),table);}
  }
  async function refresh(extra={}){
    if(!root)return;
    const id=++seq;root.querySelector('[data-meta]').textContent='正在核对记录与关系…';graph3d?.pauseAnimation();data=null;dataGuard=null;host.replaceChildren();inspector.replaceChildren();for(const key of ['[data-insights]','[data-index]','[data-paths]','[data-rank]','[data-timeline]'])root.querySelector(key)?.replaceChildren();
    try{await BankClient.ensure();const guard=await BankClient.guard();const p=parameters(extra),qs=new URLSearchParams(Object.entries(p).filter(([k,v])=>v));const result=await BankClient.get('/personal-graph?'+qs);if(id!==seq||!guard.valid(result)||root.closest('[hidden]'))return;data=result;dataGuard=guard;if(!root.querySelector('[data-from]').value){root.querySelector('[data-from]').value=data.period.from;const d=new Date(data.period.to_exclusive+'T00:00:00Z');d.setUTCDate(d.getUTCDate()-1);root.querySelector('[data-to]').value=d.toISOString().slice(0,10);}render();}
    catch(e){if(id===seq)root.querySelector('[data-meta]').textContent=e.message;}
  }
  function invalidate(){seq++;data=null;dataGuard=null;selectedId=null;for(const d of document.querySelectorAll('.fg-dialog')){d.close();d.remove();}graph3d?._destructor();graph3d=null;if(!root)return;host?.replaceChildren();inspector?.replaceChildren();for(const key of ['[data-insights]','[data-index]','[data-paths]','[data-rank]','[data-timeline]','[data-gaps]'])root.querySelector(key)?.replaceChildren();root.querySelector('[data-meta]').textContent='资料权限已变化，请重新核对。';}
  async function restore(context={}){
    if(!root)return;
    const filters=context.filters||{},u=new URL(location.href);view=views[filters.graph_view]?filters.graph_view:'overview';metric=['amount','count','share'].includes(filters.metric)?filters.metric:'amount';dimension=filters.dimension==='3d'?'3d':'2d';selectedId=filters.selected_id||null;
    const source=context.source_scope||filters.source||u.searchParams.get('source')||'simulation';if(!['simulation','imported'].includes(source)){invalidate();root.querySelector('[data-meta]').textContent='此来源不支持个人财务关系。请明确选择银行演示账户或导入账单；不会自动换成另一来源。';root.querySelector('.fg-controls').hidden=false;return;}root.querySelector('[data-source]').value=source;
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
  function init(panel,context={}){root=panel;root.innerHTML='<div class="fg-help"></div><button class="p-btn fg-back" hidden>返回四板块</button><div class="fg-views" role="tablist" aria-label="财务图谱视图"></div><div class="fg-controls"><label>来源<select data-source><option value="simulation">银行演示账户</option><option value="imported">导入账单</option></select></label><label>从<input data-from type="date"></label><label>至<input data-to type="date"></label><label>组合<select data-portfolio><option value="current">当前账户</option><option value="historical">历史模拟组合</option></select></label><label>大小口径<select data-metric><option value="amount">金额</option><option value="count">笔数</option><option value="share">子图占比</option></select></label><label>视图<select data-dimension><option value="2d">二维关系</option><option value="3d">三维探索</option></select></label><button class="p-btn" data-refresh>更新资料</button></div><div class="fg-status" data-meta role="status"></div><div class="fg-insights" data-insights></div><div class="fg-plan-actions"><button class="p-btn" data-allocate hidden>规划目标资金</button><button class="p-btn" data-scenario hidden>比较规划情景</button><button class="p-btn" data-decision hidden>记录我的选择</button></div><div class="finance-graph-stage"><div class="fg-view"><div class="fg-canvas"></div><p class="fg-legend" data-legend></p><p class="fg-key"></p><p class="fg-key" data-causality></p></div><aside class="fg-inspector" aria-live="polite"></aside></div><div data-rank></div><div data-timeline></div><details class="fg-node-index"><summary>查找资料与节点</summary><input type="search" data-search placeholder="按名称查找" aria-label="按名称查找节点"><div data-index></div></details><details class="fg-mobile-paths"><summary>按路径阅读关系</summary><div data-paths></div></details><details class="fg-coverage"><summary>数据覆盖与依据边界</summary><div data-gaps></div><p>来源说明：银行演示账户为本机模拟数据，导入账单独立计算。未与真实银行连接。</p></details><p class="fg-consent" data-consent></p>';
    root.querySelector('.fg-help').append(NavrenHelp.button('graph'));root.querySelector('.fg-back').onclick=()=>selectView('overview');host=root.querySelector('.fg-canvas');inspector=root.querySelector('.fg-inspector');for(const [id,name] of Object.entries(views)){const b=btn(name,()=>selectView(id),'fg-view-tab');b.dataset.view=id;b.role='tab';b.setAttribute('aria-selected',String(id===view));root.querySelector('.fg-views').append(b);}
    for(const s of ['[data-source]','[data-from]','[data-to]','[data-portfolio]'])root.querySelector(s).onchange=()=>{NavrenViews.update({source_scope:parameters().source,portfolio_id:parameters().portfolio,filters:{...parameters(),graph_view:view,dimension,metric}});refresh();};root.querySelector('[data-metric]').onchange=e=>{metric=e.target.value;NavrenViews.update({filters:{...parameters(),graph_view:view,dimension,metric}});if(canRepaint())render();};root.querySelector('[data-dimension]').onchange=e=>{dimension=e.target.value;NavrenViews.update({filters:{...parameters(),graph_view:view,dimension,metric}});if(canRepaint())render();};root.querySelector('[data-refresh]').onclick=()=>refresh();root.querySelector('[data-allocate]').onclick=()=>allocation().catch(e=>showToast(e.message));root.querySelector('[data-scenario]').onclick=()=>scenario().catch(e=>showToast(e.message));root.querySelector('[data-decision]').onclick=()=>decisionForm().catch(e=>showToast(e.message));root.querySelector('[data-search]').oninput=e=>{const value=e.target.value;for(const group of root.querySelectorAll('.fg-index-group')){let visible=0;for(const b of group.querySelectorAll('button')){b.hidden=!b.textContent.includes(value);if(!b.hidden)visible++;}group.hidden=!visible;if(value&&visible)group.open=true;}};
    observer=new ResizeObserver(()=>{if(graph3d&&canRepaint())graph3d.width(host.clientWidth).height(host.clientHeight||480).nodeThreeObject(n=>threeLabel(n));});observer.observe(host);styleObserver=new MutationObserver(queueRepaint);watchStyles();window.addEventListener('navren:language',queueRepaint);window.addEventListener('navren:theme',queueRepaint);window.addEventListener('navren:section',e=>{if(e.detail==='graph'&&canRepaint())graph3d?.resumeAnimation();else graph3d?.pauseAnimation();});window.addEventListener('pagehide',()=>{observer.disconnect();styleObserver.disconnect();invalidate();});window.addEventListener('pageshow',e=>{if(e.persisted){observer.observe(host);watchStyles();/* 由NavrenViews重新核验身份后读取，不能绘制旧缓存。 */}});return restore(context);
  }
  window.addEventListener('navren:privacy-reset',invalidate);
  return {init,refresh,restore,invalidate,pause:()=>graph3d?.pauseAnimation()};
})();
