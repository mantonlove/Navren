/* 我的工作区：长期目标由服务端维护，浏览器只保存恢复界面所需元数据。 */
window.NavrenMine = (() => {
  const titles = {home:'我的',goals:'目标与责任',goal:'目标详情','goal-draft':'核对目标变化',graph:'财务关系',profile:'Navren 了解的我',settings:'设置与数据授权',saved:'收藏与继续阅读'};
  const domains = {cashflow:['日常收支','收入、消费与结余'],wealth:['财富结构','资金、投资与负债'],plans:['目标与责任','目标、家庭与生活安排'],preferences:['偏好与认知','本人条件、选择与反馈']};


  function syncRelationTitle() {
    const english=window.NavrenI18n?.language==='en';
    const heading=document.getElementById('mineRelationsTitle');
    if(heading)heading.textContent=english?'Financial connections':'财务关系';
    const englishDomains={cashflow:['Daily cash flow','Income, spending and surplus'],wealth:['Wealth structure','Cash, investments and liabilities'],plans:['Goals and responsibilities','Goals, family and life arrangements'],preferences:['Preferences and understanding','Personal conditions, choices and feedback']};
    for(const a of document.querySelectorAll('.mine-domain-card')){const pair=(english?englishDomains:domains)[a.dataset.graphView];if(!pair)continue;a.querySelector('h3').textContent=pair[0];a.querySelector('[data-domain-description]').textContent=pair[1];}
    if(current.view==='graph'&&!domains[current.filters?.graph_view]){const title=english?'Financial connections':'财务关系';document.getElementById('mineViewTitle').textContent=title;document.querySelector('.header-location').textContent=title;}
  }
  window.addEventListener('navren:language',syncRelationTitle);
  function domainIcon(key) {
    const shapes={
      cashflow:[['path',{d:'M4 7h15m-4-4 4 4-4 4M20 17H5m4-4-4 4 4 4'}]],
      wealth:[['path',{d:'M4 20h16M6 16V9m6 7V4m6 12v-5'}]],
      plans:[['circle',{cx:12,cy:12,r:8}],['circle',{cx:12,cy:12,r:4}],['path',{d:'m12 12 7-7m-3 0h3v3'}]],
      preferences:[['path',{d:'M4 6h4m4 0h8M4 12h9m4 0h3M4 18h3m4 0h9'}],['circle',{cx:10,cy:6,r:2}],['circle',{cx:15,cy:12,r:2}],['circle',{cx:9,cy:18,r:2}]]
    };
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    for(const [name,value]of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',focusable:'false'}))svg.setAttribute(name,value);
    for(const [tag,attrs]of shapes[key]||[]){const shape=document.createElementNS(svg.namespaceURI,tag);for(const [name,value]of Object.entries(attrs))shape.setAttribute(name,value);svg.append(shape);}
    return svg;
  }
  function domainCard(key,label,description,translate=value=>value) {
    const a=safeLink('','graph',{filters:{graph_view:key}});
    a.className='mine-domain-card mine-domain-'+key;a.id='mine-domain-'+key;
    a.dataset.graphView=key;a.dataset.i18nSkip='';
    const head=node('div',null,'mine-domain-head'),icon=node('span',null,'mine-domain-icon');
    icon.setAttribute('aria-hidden','true');icon.append(domainIcon(key));
    const title=node('h3',translate(label));title.id=a.id+'-title';
    const arrow=node('span','↗','mine-domain-arrow');arrow.setAttribute('aria-hidden','true');
    const copy=node('p',translate(description),'mine-domain-description');copy.dataset.domainDescription='';
    head.append(icon,title,arrow);a.append(head,copy);a.setAttribute('aria-labelledby',title.id);
    return a;
  }
  const statusNames = {active:'进行中',planned:'计划中',completed:'本人标记完成',cancelled:'已取消',prepared:'待本人确认',committed:'已确认保存',succeeded:'已保存',deleted:'已删除',unknown:'结果待核实',submitting:'正在提交',expired:'已过期',rejected:'被拒绝',failed:'未保存',candidate:'待核实',confirmed:'本人确认'};
  const node = (tag,text,cls) => { const x=document.createElement(tag);if(text!=null)x.textContent=String(text);if(cls)x.className=cls;return x; };
  const original = (tag,text,cls) => {const x=node(tag,text,cls);x.setAttribute('translate','no');return x;};
  const button = (label,fn,cls='p-btn') => {const b=node('button',label,cls);b.type='button';b.onclick=fn;return b;};
  const money = (minor,currency='CNY') => Number.isSafeInteger(minor) ? (minor/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})+(currency==='CNY'?' 元':' '+currency) : '未知';
  let current={page:'mine',view:'home'}, request=0, graphStarted=false, goalVersion=null, activeDraft=null;
  const pendingDrafts=new Set(), inFlightDrafts=new Set();
  const drafts=new Map();
  function open(view,metadata={}) { window.NavrenViews?.open(view,metadata); }
  function safeLink(text,view,metadata={}) {const a=node('a',text);const u=new URL('mine.html',location.href);u.searchParams.set('view',view);if(metadata.object_id)u.searchParams.set('object_id',metadata.object_id);if(metadata.object_kind)u.searchParams.set('object_kind',metadata.object_kind);a.href=u.pathname+u.search;a.onclick=e=>{if(e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;e.preventDefault();open(view,metadata);};return a;}
  function failure(target,error,retry) {target.replaceChildren(node('p',error.message||'这部分资料暂未取得。','mine-state'));if(retry)target.append(button('重新核对',retry));}
  async function read(path,id=request) {await BankClient.ensure();const guard=await BankClient.guard();const result=await BankClient.get(path);if(id!==request||!guard.valid(result))return null;return result;}
  function available(data,ctx) {return !ctx?.object_version || ctx.object_version === data.object_version || ctx.object_version === data.version;}
  function goalRef(goal) {return {kind:'goal',id:goal.id,source_scope:'simulation',version:goal.object_version,label:goal.name};}
  function goalCard(goal,full=false) {const card=node('article',null,'mine-goal-card'),a=safeLink(goal.name||'未命名目标','goal',{object_kind:'goal',object_id:goal.id,object_version:goal.object_version,source_scope:'simulation'});a.className='mine-object-link';a.id='mine-goal-'+goal.id;a.append(node('span','›','mine-object-arrow'));card.append(a,node('p',money(goal.target_minor,goal.currency),'mine-goal-amount'));const meta=node('p',null,'mine-goal-meta');meta.append(node('span',goal.due_on?'目标截止 '+goal.due_on:'截止日未设置'),node('span',statusNames[goal.status]||goal.status||'状态未知'));card.append(meta,node('p','目标记录已安排 '+money(goal.saved_minor,goal.currency)+' · 不代表冻结或已支付','mine-source'));if(full&&goal.object_version)card.append(node('p','版本 '+String(goal.object_version).slice(0,12),'mine-source'));return card;}
  async function loadGoals(preview,id=request) {const target=document.getElementById(preview?'mineGoalPreview':'mineGoalList');target.replaceChildren(node('p','正在核对目标与职责…','mine-state'));try{const data=await read('/goals',id);if(!data)return;goalVersion=data.version;const items=data.items||data.goals||[];target.replaceChildren();if(!items.length){target.append(node('p','还没有结构化目标。可以从一个明确的目标开始。','mine-state'));}for(const goal of items.slice(0,preview?3:undefined))target.append(goalCard(goal,!preview));if(preview&&items.length>3)target.append(safeLink('查看全部 '+items.length+' 项目标','goals'));if(data.drafts?.length){const pending=data.drafts.filter(d=>['prepared','unknown','submitting'].includes(d.status)),history=data.drafts.filter(d=>!['prepared','unknown','submitting'].includes(d.status));for(const [list,label,tag]of [[pending,'仍需核对的目标变化','div'],[history,'目标变化回执与历史','details']]){if(!list.length||preview&&tag==='details')continue;const p=node(tag,null,'mine-pending-drafts');p.append(node(tag==='details'?'summary':'h3',label));for(const draft of list)p.append(safeLink((draft.summary?.name||'目标变化')+' · '+(statusNames[draft.status]||draft.status),'goal-draft',{object_kind:'goal_draft',object_id:draft.id}));target.append(p);}}}catch(error){if(id===request)failure(target,error,()=>loadGoals(preview));}}
  async function loadOverview(id=request) {const box=document.getElementById('mineDomainOverview'),review=document.getElementById('mineReviewList');box.replaceChildren();for(const [key,[label,description]]of Object.entries(domains))box.append(domainCard(key,label,description));syncRelationTitle();review.replaceChildren(node('p','正在核对本人陈述…','mine-state'));try{const data=await read('/personal-graph?view=overview&source=simulation&portfolio=current',id);if(!data)return;const candidates=(data.nodes||[]).filter(n=>n.observation_id&&n.status==='candidate');review.replaceChildren();if(!candidates.length)review.append(node('p','当前没有已记录的待核实陈述；未取得的资料保持未知。','mine-state'));for(const item of candidates.slice(0,3)){const row=node('article',null,'mine-review-item');row.append(node('p',item.label),node('span','聊天中的本人陈述 · 待核实','mine-source'),safeLink('查看原话与用途','profile',{object_id:item.observation_id,object_kind:'observation'}));review.append(row);}for(const a of box.children){const key=Object.keys(domains).find(k=>a.classList.contains('mine-domain-'+k));const insight=(data.insights||[]).find(i=>i.view===key);if(insight)a.append(node('p',insight.title,'mine-domain-note'));}}catch(error){if(id===request)failure(review,error,()=>loadOverview());}}
  async function loadFinancialProfile(id=request) {
    const host=document.getElementById('mineFinancialProfile');if(!host)return;
    host.replaceChildren(node('p','正在读取独立财务账户资料…','mine-source'));
    try{const envelope=await NavrenFinancialData.overview();if(id!==request)return;const data=envelope.data;
      host.replaceChildren(node('p','独立合成历史 · '+envelope.evidence.coverage_start+' — '+envelope.as_of,'mine-source'));
      const items=[['资金账户',data.accounts.length+' 个'],['有效银行卡',data.cards.filter(card=>card.status==='active').length+' 张'],['有效保单',data.policies.length+' 份']];
      const list=node('div',null,'mine-row-actions');for(const [label,value]of items)list.append(node('span',label+' '+value));host.append(list);
      const actions=node('div',null,'mine-row-actions');for(const [label,view]of [['账户与资产','funds'],['保险档案','archive'],['财务日历','calendar']]){const link=node('a',label,'p-btn');link.href=NavrenViews.url({page:'account',view,source_scope:NavrenFinancialData.scope});actions.append(link);}host.append(actions);
    }catch(error){if(id===request)failure(host,error,()=>{NavrenFinancialData.clear();loadFinancialProfile();});}
  }
  async function loadProfile(id=request) {renderRiskProfile();renderArchive();const risk=document.getElementById('mineBusinessRisk'),observations=document.getElementById('mineObservationList');risk.replaceChildren(node('h3','模拟业务风险配置'),node('p','正在核对业务配置…'));observations.replaceChildren(node('p','正在核对本人陈述与证据…','mine-state'));await Promise.allSettled([
    loadFinancialProfile(id),
    (async()=>{try{const data=await read('/overview',id);if(!data)return;const level=data.profile?.risk_level;risk.replaceChildren(node('h3','模拟业务风险配置'),node('p',Number.isInteger(level)&&level>=1&&level<=5?'业务配置 R'+level:'业务等级未知'),node('p','来源：演示账户手动配置 · 参考日期 '+data.as_of+'。不等于正式问卷测评。','mine-source'));const a=node('a','查看业务资金与风险设置 ›');a.href='account.html?view=wealth#wealth';risk.append(a);}catch(error){if(id===request)failure(risk,error,()=>loadProfile());}})(),
    (async()=>{try{const data=await read('/personal-graph?view=preferences&source=simulation&portfolio=current',id);if(!data)return;const expectedRevision=data.personal_revision;observations.replaceChildren();const items=(data.nodes||[]).filter(n=>n.observation_id);if(!items.length)observations.append(node('p','没有已记录的聊天陈述。缺少本人资料时会在具体任务中询问。','mine-state'));for(const item of items){const confirmationToken=item.confirmation_token;const row=node('article',null,'mine-observation');row.append(original('h3',item.label),node('span',statusNames[item.status]||'状态未知','mine-status'),item.quote?original('blockquote',item.quote):node('blockquote','原话未提供'),node('p','来源：对话中的本人陈述；仅在授权范围内用于理解条件，不改变余额、业务风险或交易资格。','mine-source'));for(const [decision,label]of [['confirm','确认是我的陈述'],['delete','删除这项资料']]){if(decision==='confirm'&&item.status==='confirmed')continue;row.append(button(label,async()=>{if(!confirm(decision==='delete'?'删除这项陈述与派生关系？银行交易和目标仍保留。':'确认这段原话是本人陈述？不会改变银行业务等级。'))return;try{if(!Number.isSafeInteger(expectedRevision)||decision==='confirm'&&!confirmationToken)throw Error('资料版本或确认凭据未取得，请重新核对。');const payload={decision,expected_revision:expectedRevision};if(decision==='confirm')payload.confirmation_token=confirmationToken;await BankClient.post('/profile-observations/'+item.observation_id,payload);await BankClient.refreshIdentity();window.NavrenViews?.open('profile',{},{replace:true});}catch(error){showToast('资料未更新：'+error.message);}}));}observations.append(row);}}catch(error){if(id===request)failure(observations,error,()=>loadProfile());}})(),
    loadFinancialMemories()
  ]);}
  async function goalDetail(ctx,id=request) {const box=document.getElementById('mineGoalDetail');box.replaceChildren(node('p','正在核对目标对象…','mine-state'));if(!ctx.object_id){failure(box,new Error('缺少目标标识。请选择目标后查看。'));return;}try{const data=await read('/goals/'+encodeURIComponent(ctx.object_id),id);if(!data)return;const goal=data.goal||data.item||data;if(!goal.id||goal.id!==ctx.object_id)throw Error('目标身份无法核验。');box.replaceChildren();box.append(node('h2',goal.name||'未命名目标'));if(!available(goal,ctx))box.append(node('p','目标已有新版本，以下为最新核实状态；旧编辑条件已失效。','mine-state'));const dl=node('dl',null,'mine-goal-details');for(const [label,value]of [['目标金额',money(goal.target_minor,goal.currency)],['目标记录已安排',money(goal.saved_minor,goal.currency)],['目标截止日',goal.due_on||'未知'],['状态',statusNames[goal.status]||goal.status||'未知'],['来源','演示账户目标服务'],['参考日期',data.as_of||'未知']]){dl.append(node('dt',label),node('dd',value));}box.append(dl,node('p','记录已安排金额不等于账户资金冻结、预留或扣款。目标完成由本人确认，不由提醒勾选推断。','mine-source'));const actions=node('div',null,'mine-row-actions');actions.append(button('修改目标',()=>editGoal(goal)),button('删除目标',()=>prepareDeletion(goal)),button('问 Navren',()=>NavrenViews.ask('请解释这个目标的资金联系、期限和下一步需要核实的条件。',goalRef(goal))));actions.append(safeLink('查看此目标的资金联系 ›','graph',{source_scope:'simulation',portfolio_id:'current',filters:{graph_view:'plans',selected_id:'goal:'+goal.id}}));box.append(actions);const calendar=node('a','在财务日历查看截止日 ›');const url=new URL('account.html',location.href);url.searchParams.set('view','calendar');url.searchParams.set('object_kind','goal');url.searchParams.set('object_id',goal.id);url.searchParams.set('source','simulation');if(goal.due_on)url.searchParams.set('calendar_on',goal.due_on);url.hash='calendar';calendar.href=url.pathname+url.search+url.hash;box.append(calendar,node('p','目标截止日由目标服务维护；独立提醒由日历维护。修改或删除提醒不会修改此目标。','mine-source'));}catch(error){if(id===request)failure(box,error,()=>goalDetail(ctx));}}
  function modal(title) {closeModal();const overlay=document.getElementById('modalOverlay');document.getElementById('modalTitle').textContent=title;const body=document.getElementById('modalBody');body.replaceChildren();overlay.classList.remove('hidden');return body;}
  function input(body,label,type,value) {const row=node('label',label,'mine-form-field'),field=node('input');field.type=type;field.value=value??'';row.append(field);body.append(row);return field;}
  async function editGoal(goal=null) {try{await BankClient.refreshIdentity();if(!goal&&!goalVersion){const data=await read('/goals');if(!data)return;goalVersion=data.version;}const key=goal?.id||'new',saved=drafts.get(key);const body=modal(goal?'修改目标':'创建目标');const name=input(body,'目标名称','text',saved?.name??goal?.name),amount=input(body,'目标金额（元）','text',saved?.amount??(goal?.target_minor==null?'':(goal.target_minor/100).toFixed(2))),due=input(body,'目标截止日','date',saved?.due_on??goal?.due_on);name.maxLength=100;amount.inputMode='decimal';due.min='';const statusRow=node('label','本人确认的目标状态','mine-form-field'),state=node('select');for(const [value,label]of [['active','进行中'],['completed','本人确认已完成']]){const option=node('option',label);option.value=value;state.append(option);}state.value=saved?.status||goal?.status||'active';statusRow.append(state);body.append(statusRow);const hint=node('p','先准备变化摘要，再由本人核对确认。关闭会保留尚未提交的非敏感输入；可选择放弃。目标截止日投影会同步到日历，独立提醒和银行交易不受影响。','mine-source'),error=node('p',null,'mine-state');body.append(hint,error);const save=()=>drafts.set(key,{name:name.value,amount:amount.value,due_on:due.value,status:state.value});for(const field of [name,amount,due,state])field.addEventListener('input',save);const keep=document.querySelector('.modal-foot [onclick="closeModal()"]');if(keep)keep.textContent='保留输入并关闭';const discard=button('放弃未提交输入',()=>{drafts.delete(key);closeModal();});discard.id='goalFormDiscard';body.append(discard);const submit=document.getElementById('modalConfirm');submit.textContent='核对变化';submit.disabled=false;submit.onclick=async()=>{try{if(!name.value.trim()||!due.value)throw Error('请填写目标名称和截止日。');const cents=BankClient.minor(amount.value);if(cents<=0)throw Error('目标金额须大于零。');submit.disabled=true;const payload={action:goal?'update':'create',...(goal?{goal_id:goal.id}:{}),name:name.value.trim(),target_minor:cents,due_on:due.value,status:state.value,expected_version:goal?.object_version||goalVersion,idempotency_key:BankClient.key()};const guard=await BankClient.guard();const result=await BankClient.post('/goals/prepare',payload);if(!guard.valid(result))return;drafts.delete(key);closeModal();open('goal-draft',{object_kind:'goal_draft',object_id:result.draft?.id||result.id});}catch(e){error.textContent=e.message;}finally{submit.disabled=false;}};name.focus();}catch(error){showToast('目标编辑暂不可用：'+error.message);}}
  async function prepareDeletion(goal) {if(!confirm('准备删除“'+goal.name+'”？请在下一步核对日历投影和关联影响。银行交易、合同与独立提醒不会删除。'))return;try{const guard=await BankClient.guard();const result=await BankClient.post('/goals/prepare',{action:'delete',goal_id:goal.id,expected_version:goal.object_version,idempotency_key:BankClient.key()});if(!guard.valid(result))return;open('goal-draft',{object_kind:'goal_draft',object_id:result.draft?.id||result.id});}catch(error){showToast('删除尚未准备：'+error.message);}}
  async function draftDetail(ctx,id=request) {
    const box=document.getElementById('mineGoalDetail');
    box.replaceChildren(node('p','正在核对同一目标变化单…','mine-state'));
    try {
      const data=await read('/goals/drafts/'+encodeURIComponent(ctx.object_id||''),id);
      if(!data)return;
      const draft=data.draft||data;activeDraft=draft;
      if(draft.id!==ctx.object_id)throw Error('变化单身份不一致。');
      const action=draft.action||draft.summary?.action;
      box.replaceChildren(node('h2',action==='delete'?'删除目标前核对':'保存目标前核对'),node('p',statusNames[draft.status]||draft.status||'状态未知','mine-status'));
      const summary=draft.summary||{};
      if(typeof summary==='string')box.append(node('p',summary));
      else for(const [key,value]of Object.entries(summary)){
        const labels={name:'目标',target_minor:'目标金额',due_on:'截止日',status:'状态',previous_due_on:'原截止日',new_due_on:'新截止日'};
        if(labels[key])box.append(node('p',labels[key]+'：'+(key==='target_minor'?money(value):typeof value==='object'?JSON.stringify(value):value)));
      }
      const impacts=node('section',null,'mine-goal-impacts');impacts.append(node('h3','本次变化的影响'));
      for(const impact of draft.impacts||[])impacts.append(node('p',typeof impact==='string'?impact:impact.notice||impact.description||JSON.stringify(impact)));
      if(draft.result?.receipt)box.append(node('p','回执 '+draft.result.receipt.id+' · '+(draft.result.receipt.action==='delete'?'目标已删除':'目标已保存'),'mine-status'));
      box.append(impacts,node('p','关闭或返回不等于取消。待确认单保留原操作身份；结果待核实时不会开放重复确认。','mine-source'));
      const message=node('p',null,'mine-state');box.append(message);
      const isVisible=()=>current.view==='goal-draft'&&current.object_id===draft.id&&box.isConnected;
      const query=async()=>{
        if(inFlightDrafts.has(draft.id)||!isVisible())return;
        pendingDrafts.delete(draft.id);
        await NavrenViews.open('goal-draft',{object_kind:'goal_draft',object_id:draft.id},{replace:true});
      };
      const queryButton=()=>{const b=button('核查同一变化单',query);b.disabled=inFlightDrafts.has(draft.id);return b;};
      if(!['prepared','unknown','submitting'].includes(draft.status))pendingDrafts.delete(draft.id);
      if(['prepared','expired'].includes(draft.status)&&!pendingDrafts.has(draft.id)){
        const actions=node('div',null,'mine-row-actions');
        let submit,cancel;
        const commit=async(operation)=>{
          if(pendingDrafts.has(draft.id)||inFlightDrafts.has(draft.id)||!isVisible())return;
          if(operation==='cancel'&&!confirm('取消这张待确认目标变化单？目标现状保持不变。'))return;
          const workspace=BankClient.identity()?.workspace_id;
          pendingDrafts.add(draft.id);inFlightDrafts.add(draft.id);
          submit.disabled=cancel.disabled=true;
          message.textContent=operation==='confirm'?'正在确认同一变化单…':'正在核对取消回执…';
          try {
            const payload=operation==='confirm'?{confirmed:true,confirmation_digest:draft.confirmation_digest}:{};
            const result=await BankClient.post('/goals/drafts/'+draft.id+'/'+operation,payload);
            await BankClient.refreshIdentity();
            const identity=BankClient.identity();
            inFlightDrafts.delete(draft.id);
            if(identity?.workspace_id!==workspace||result.workspace_id!==workspace||result.privacy_revision!==identity?.privacy_revision)return;
            pendingDrafts.delete(draft.id);
            if(!isVisible())return;
            await NavrenViews.open('goal-draft',{object_kind:'goal_draft',object_id:draft.id},{replace:true});
          } catch(error) {
            inFlightDrafts.delete(draft.id);
            if(!isVisible())return;
            message.textContent=(operation==='cancel'?'取消结果未核实：':'确认结果未核实：')+error.message+' 请核查同一变化单状态，暂不重复提交。';
            actions.replaceChildren(queryButton());
          }
        };
        submit=button('我已核对，确认'+(action==='delete'?'删除':'保存'),()=>commit('confirm'),'mine-primary');
        cancel=button('明确取消这项变化',()=>commit('cancel'));
        if(draft.status==='prepared')actions.append(submit);actions.append(cancel);box.append(actions);
      }else if(['unknown','submitting'].includes(draft.status)||pendingDrafts.has(draft.id))box.append(queryButton());
      if(draft.goal_id&&!(draft.result?.receipt?.action==='delete'&&draft.status==='committed'))box.append(safeLink('查看目标最新状态 ›','goal',{object_kind:'goal',object_id:draft.goal_id}));
    } catch(error) {if(id===request)failure(box,error,()=>draftDetail(ctx));}
  }
  // 同时供“继续阅读”和 mine.js 的收藏入口使用；归档不能退回同 ID 的当前正文。
  function readingHref(item) {
    const kind=item?.kind||item?.object_kind,id=item?.id||item?.object_id,version=item?.object_version??item?.version;
    if(typeof id!=='string'||!id.trim()||!['number','string'].includes(typeof version)||String(version).trim()===''||!['event','weekly','public_product','product'].includes(kind))return null;
    const product=['public_product','product'].includes(kind),source=product?(item.product_source||item.source_scope):'public_market';
    if(!product&&(!Number.isSafeInteger(Number(version))||Number(version)<1))return null;
    if(product&&typeof version==='number'&&(!Number.isSafeInteger(version)||version<1))return null;
    if(product&&!['public_nav','public_catalog'].includes(source))return null;
    if(!product&&item.source_scope&&!['public','market','public_market'].includes(item.source_scope))return null;
    const u=new URL('recommend.html',location.href);
    u.searchParams.set('view',product?'product':kind==='weekly'?'weekly':'reading');
    u.searchParams.set('object_kind',product?'product':kind);u.searchParams.set('object_id',id);u.searchParams.set('source_scope',source);
    if(item.archive!=null) {
      const a=item.archive,day=a?.edition_date,revision=a?.revision,eventVersion=a?.event_version;
      if(kind!=='event'||typeof day!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(day)||!Number.isSafeInteger(revision)||revision<1||!Number.isSafeInteger(eventVersion)||eventVersion<1||version!==eventVersion)return null;
      const parsed=new Date(day+'T00:00:00Z');if(!Number.isFinite(parsed.valueOf())||parsed.toISOString().slice(0,10)!==day)return null;
      u.searchParams.set('edition_date',day);u.searchParams.set('revision',revision);
      // NavrenViews 的 object_version 对应归档 event_version，不能改用当前事件版本。
      u.searchParams.set('object_version',eventVersion);
    } else u.searchParams.set('object_version',version);
    return u.pathname+u.search;
  }
  function continuing() {
    const target=document.getElementById('mineContinueReading');target.replaceChildren();
    const rows=Storage.get('navren_reading_history')||[],items=rows.filter(r=>r.source_scope==='public'&&r.id&&['event','weekly','public_product'].includes(r.kind));
    if(!items.length){target.append(node('p','尚未记录可核验的继续阅读位置。收藏的原对象仍可从上方进入。','mine-state'));return;}
    for(const item of items.slice(0,10)) {
      const row=node('article',null,'mine-saved-item'),href=readingHref(item);
      if(href){const a=item.label?original('a',item.label):node('a','继续阅读');a.href=href;row.append(a);}
      else row.append(item.label?original('p',item.label):node('p','继续阅读'),node('p','保存的来源或历史版本不完整，请重新核对。','mine-state'));
      if(href&&item.archive){const meta=node('p',null,'mine-source');for(const [label,value]of [['归档日',item.archive.edition_date],['日版次',item.archive.revision],['事件版本',item.archive.event_version]]){if(meta.childNodes.length)meta.append(document.createTextNode(' · '));meta.append(node('span',label),document.createTextNode(' '+value));}row.append(meta);}
      target.append(row);
    }
  }
  function syncHomeSections(){const labels={'账户资料':'Account profile','资金与风险':'Funds and risk','偏好与授权':'Preferences and permissions','收藏阅读':'Saved reading'};for(const heading of document.querySelectorAll('[data-home-section-title]'))heading.textContent=window.NavrenI18n?.language==='en'?labels[heading.dataset.homeSectionTitle]:heading.dataset.homeSectionTitle;}
  window.addEventListener('navren:language',syncHomeSections);
  async function render(ctx) {if(['profile','saved'].includes(ctx.view)){const target=ctx.view==='saved'?'mineSavedSection':ctx.object_kind==='observation'?'mineObservationList':'mineAccountSection';return NavrenViews.open('home',{...ctx,view:'home',focustarget:target},{replace:true});}syncHomeSections();if(ctx.view!==current.view||ctx.object_id!==current.object_id)closeModal();current={...ctx,view:ctx.view||'home'};if(['appearance'].includes(current.view))current.view='settings';if(current.view==='goal-detail')current.view='goal';if(['continuereading'].includes(current.view))current.view='saved';const id=++request;const view=Object.hasOwn(titles,current.view)?current.view:'unavailable';for(const panel of document.querySelectorAll('[data-mine-view]'))panel.hidden=panel.dataset.mineView!==(view==='goal-draft'?'goal':view);document.getElementById('mineViewTitle').textContent=titles[view]||'对象不可用';document.getElementById('mineViewEyebrow').textContent=view==='home'?'长期安排与个人理解':'我的 / '+(titles[view]||'原对象无法打开');const back=document.querySelector('[data-mine-back]');back.id='mineBackToPrevious';back.hidden=view==='home';back.onclick=e=>{e.preventDefault();NavrenViews.back();};document.querySelector('.header-location').textContent=titles[view]||'我的';syncRelationTitle();NavrenGraph?.pause?.();if(view!=='graph')NavrenGraph?.invalidate?.();if(view==='unavailable'){const panel=document.getElementById('mineGoalDetail');panel.hidden=false;failure(panel,new Error('对象不存在或已删除。不会替换为另一个同名对象。'));return;}if(view==='home'){const info=Storage.get('qingzhou_userInfo');document.getElementById('mineHomeName').textContent=info?.nickname||'我的账户';await Promise.allSettled([loadOverview(id),loadProfile(id)]);if(id!==request)return;const observation=document.getElementById('mineObservationList');if(current.focustarget===observation?.id){const disclosure=observation.closest('details');if(disclosure)disclosure.open=true;}await BankClient.refreshIdentity();if(id!==request)return;renderBookmarks();continuing();}else if(view==='goals')await loadGoals(false,id);else if(view==='goal')await goalDetail(current,id);else if(view==='goal-draft')await draftDetail(current,id);else if(view==='profile')await loadProfile(id);else if(view==='settings'){NavrenDesign.mountSettings?.(document.getElementById('navrenThemeSettings'));await loadFinancialMemories();}else if(view==='saved'){await BankClient.refreshIdentity();if(id!==request)return;renderBookmarks();continuing();}else if(view==='graph'){const host=document.getElementById('mineGraphHost');if(!graphStarted){NavrenGraph.init(host,current);graphStarted=true;}else await NavrenGraph.restore?.(current);}}
  function init() {const advisor=document.querySelector('.mine-advisor-link');if(advisor)advisor.addEventListener('click',()=>{const identity=BankClient.identity();if(identity)sessionStorage.setItem('navren_advisor_return',JSON.stringify({...NavrenViews.current(),workspace_id:identity.workspace_id,privacy_revision:identity.privacy_revision}));});for(const a of document.querySelectorAll('[data-mine-open]'))a.addEventListener('click',e=>{if(e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;e.preventDefault();open(a.dataset.mineOpen);});for(const b of document.querySelectorAll('[data-goal-create]'))b.onclick=()=>editGoal();window.NavrenViews?.register('mine',render);}
  window.addEventListener('navren:privacy-reset',()=>{request++;for(const id of ['mineGoalPreview','mineGoalList','mineGoalDetail','mineReviewList','mineObservationList','mineBusinessRisk','mineFinancialProfile'])document.getElementById(id)?.replaceChildren(node('p','正在核验新的资料权限…','mine-state'));NavrenGraph?.invalidate?.();activeDraft=null;drafts.clear();setTimeout(async()=>{try{await BankClient.refreshIdentity();if(document.body.dataset.page==='mine')await NavrenViews.open(current.view,{...current},{replace:true});}catch{}},0);});
  window.addEventListener('navren:memory-updated',()=>{if(current.view==='home'||current.view==='profile'){const next={...current};render(next);}});
  window.addEventListener('pagehide',()=>{request++;for(const panel of document.querySelectorAll('[data-mine-view]'))panel.hidden=true;closeModal();NavrenGraph?.invalidate?.();});
  window.addEventListener('storage',e=>{if(e.key==='qingzhou_bookmarks'&&['home','saved'].includes(current.view))renderBookmarks();});
  window.addEventListener('beforeunload',event=>{if(drafts.size){event.preventDefault();event.returnValue='';}});
  document.addEventListener('DOMContentLoaded',init);
  return {render,editGoal,readingHref,current:()=>({...current})};
})();
