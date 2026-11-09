/* 我的工作区：长期目标由服务端维护，浏览器只保存恢复界面所需元数据。 */
window.NavrenMine = (() => {
  const titles = {home:'我的',goals:'目标与责任',goal:'目标详情','goal-draft':'核对目标变化',graph:'财务关系',profile:'Navren 了解的我',settings:'设置与数据授权',saved:'我的收藏','payment-contacts':'收付款联系人'};
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
  let refreshTimer=null, refreshSequence=0;
  function cancelWorkspaceRefresh() { refreshSequence++;clearTimeout(refreshTimer);refreshTimer=null; }
  function scheduleWorkspaceRefresh() {
    cancelWorkspaceRefresh();const sequence=refreshSequence;
    refreshTimer=setTimeout(async()=>{
      refreshTimer=null;
      try {
        await BankClient.refreshIdentity();
        if(sequence!==refreshSequence||document.body.dataset.page!=='mine')return;
        await NavrenViews.open(current.view,{...current},{replace:true});
      } catch (_) {}
    },0);
  }
  const pendingDrafts=new Set(), inFlightDrafts=new Set();
  const drafts=new Map();
  function open(view,metadata={}) { return window.NavrenViews?.open(view,metadata); }
  function safeLink(text,view,metadata={}) {const a=node('a',text);const u=new URL('mine.html',location.href);u.searchParams.set('view',view);if(metadata.object_id)u.searchParams.set('object_id',metadata.object_id);if(metadata.object_kind)u.searchParams.set('object_kind',metadata.object_kind);a.href=u.pathname+u.search;a.onclick=e=>{if(e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;e.preventDefault();open(view,metadata);};return a;}
  function failure(target,error,retry) {target.replaceChildren(node('p',error.message||'这部分资料暂未取得。','mine-state'));if(retry)target.append(button('重新核对',retry));}
  async function read(path,id=request) {await BankClient.ensure();const guard=await BankClient.guard();const result=await BankClient.get(path);if(id!==request||!guard.valid(result))return null;return result;}
  function available(data,ctx) {return !ctx?.object_version || ctx.object_version === data.object_version || ctx.object_version === data.version;}
  function goalRef(goal) {return {kind:'goal',id:goal.id,source_scope:'simulation',version:goal.object_version,label:goal.name};}
  function goalCard(goal,full=false) {const card=node('article',null,'mine-goal-card'),a=safeLink(goal.name||'未命名目标','goal',{object_kind:'goal',object_id:goal.id,object_version:goal.object_version,source_scope:'simulation'});a.className='mine-object-link';a.id='mine-goal-'+goal.id;a.append(node('span','›','mine-object-arrow'));card.append(a,node('p',money(goal.target_minor,goal.currency),'mine-goal-amount'));const meta=node('p',null,'mine-goal-meta');meta.append(node('span',goal.due_on?'目标截止 '+goal.due_on:'截止日未设置'),node('span',statusNames[goal.status]||goal.status||'状态未知'));card.append(meta,node('p','目标记录已安排 '+money(goal.saved_minor,goal.currency)+' · 不代表冻结或已支付','mine-source'));if(full&&goal.object_version)card.append(node('p','版本 '+String(goal.object_version).slice(0,12),'mine-source'));return card;}
  async function loadGoals(preview,id=request) {const target=document.getElementById(preview?'mineGoalPreview':'mineGoalList');target.replaceChildren(node('p','正在核对目标与职责…','mine-state'));try{const data=await read('/goals',id);if(!data)return;goalVersion=data.version;const items=data.items||data.goals||[];target.replaceChildren();if(!items.length){target.append(node('p','还没有结构化目标。可以从一个明确的目标开始。','mine-state'));}for(const goal of items.slice(0,preview?3:undefined))target.append(goalCard(goal,!preview));if(preview&&items.length>3)target.append(safeLink('查看全部 '+items.length+' 项目标','goals'));if(data.drafts?.length){const pending=data.drafts.filter(d=>['prepared','unknown','submitting'].includes(d.status)),history=data.drafts.filter(d=>!['prepared','unknown','submitting'].includes(d.status));for(const [list,label,tag]of [[pending,'仍需核对的目标变化','div'],[history,'目标变化回执与历史','details']]){if(!list.length||preview&&tag==='details')continue;const p=node(tag,null,'mine-pending-drafts');p.append(node(tag==='details'?'summary':'h3',label));for(const draft of list)p.append(safeLink((draft.summary?.name||'目标变化')+' · '+(statusNames[draft.status]||draft.status),'goal-draft',{object_kind:'goal_draft',object_id:draft.id}));target.append(p);}}}catch(error){if(id===request)failure(target,error,()=>loadGoals(preview));}}
  async function loadOverview(id=request) {
    const box=document.getElementById('mineDomainOverview');
    box.replaceChildren();
    for(const [key,[label,description]] of Object.entries(domains)) box.append(domainCard(key,label,description));
    syncRelationTitle();
    try {
      const data=await read('/personal-graph?view=overview&source=simulation&portfolio=current',id);
      if(!data)return;
      for(const card of box.children) {
        const key=Object.keys(domains).find(k=>card.classList.contains('mine-domain-'+k));
        const insight=(data.insights||[]).find(item=>item.view===key);
        if(insight)card.append(node('p',insight.title,'mine-domain-note'));
      }
    } catch(error) {
      if(id===request) {
        const status=node('p','财务关系暂未取得，请稍后重试。','mine-state');
        box.append(status);
      }
    }
  }
  function profileCopy(tag,zh,en,cls) {
    const element=node(tag,zh,cls);element.dataset.profileZh=zh;element.dataset.profileEn=en;element.dataset.i18nSkip='';
    element.textContent=window.NavrenI18n?.language==='en'?en:zh;return element;
  }
  function syncProfileCopy() {
    const english=window.NavrenI18n?.language==='en';
    for(const element of document.querySelectorAll('#mineAccountSection [data-profile-zh]'))element.textContent=english?element.dataset.profileEn:element.dataset.profileZh;
    document.querySelector('#mineProfileHelp summary')?.setAttribute('aria-label',english?'About account profile':'账户资料说明');
    document.querySelector('#mineFinancialProfile .mine-profile-links')?.setAttribute('aria-label',english?'Account profile links':'账户资料入口');
    window.NavrenPersonalProfile?.renderSummary();
  }
  window.addEventListener('navren:language',syncProfileCopy);
  document.addEventListener('keydown',event=>{if(event.key==='Escape'){const help=document.getElementById('mineProfileHelp');if(help?.open){help.open=false;help.querySelector('summary').focus();}}});
  document.addEventListener('pointerdown',event=>{const help=document.getElementById('mineProfileHelp');if(help?.open&&!help.contains(event.target))help.open=false;});
  async function loadFinancialProfile(id=request) {
    const host=document.getElementById('mineFinancialProfile');if(!host)return;
    const history=document.getElementById('mineProfileHistory');if(history){history.textContent='';delete history.dataset.profileZh;delete history.dataset.profileEn;}
    host.replaceChildren(node('p','正在读取独立财务账户资料…','mine-source'));
    try{const envelope=await NavrenFinancialData.overview();if(id!==request)return;const data=envelope.data;
      const history=document.getElementById('mineProfileHistory');
      if(history){history.dataset.profileZh='独立合成历史 · '+envelope.evidence.coverage_start+' — '+envelope.as_of;history.dataset.profileEn='Separate synthetic history · '+envelope.evidence.coverage_start+' — '+envelope.as_of;}
      const list=node('dl',null,'mine-profile-stats');
      const items=[['资金账户','Financial accounts',data.accounts.length],['有效银行卡','Active bank cards',data.cards.filter(card=>card.status==='active').length],['有效保单','Active policies',data.policies.length]];
      for(const [zh,en,value]of items){const stat=node('div',null,'mine-profile-stat');stat.append(profileCopy('dt',zh,en),node('dd',value));list.append(stat);}
      const actions=node('nav',null,'mine-profile-links');actions.setAttribute('aria-label','账户资料入口');
      for(const [zh,en,view,icon]of [['账户与资产','Accounts and assets','funds','wealth'],['保险档案','Insurance records','archive','plans'],['财务日历','Financial calendar','calendar','preferences']]){
        const link=node('a',null,'mine-profile-link');link.href=NavrenViews.url({page:'account',view,source_scope:NavrenFinancialData.scope});
        const mark=node('span',null,'mine-profile-link-icon');mark.setAttribute('aria-hidden','true');
        if(view==='calendar'){
          const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
          for(const [key,value]of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round'}))svg.setAttribute(key,value);
          const rect=document.createElementNS(svg.namespaceURI,'rect');for(const [key,value]of Object.entries({x:3,y:5,width:18,height:16,rx:3}))rect.setAttribute(key,value);
          const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d','M7 3v4m10-4v4M3 11h18');svg.append(rect,path);mark.append(svg);
        }else if(view==='archive'){
          const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');for(const [key,value]of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round'}))svg.setAttribute(key,value);
          const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d','M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6l-8-3Z');svg.append(path);mark.append(svg);
        }else mark.append(domainIcon(icon));
        const arrow=node('span','›','mine-profile-link-arrow');arrow.setAttribute('aria-hidden','true');link.append(mark,profileCopy('span',zh,en),arrow);actions.append(link);
      }
      const contacts=safeLink('','payment-contacts');contacts.className='mine-profile-link';
      const contactIcon=node('span',null,'mine-profile-link-icon');contactIcon.setAttribute('aria-hidden','true');
      const contactSvg=document.createElementNS('http://www.w3.org/2000/svg','svg');
      for(const [key,value]of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round'}))contactSvg.setAttribute(key,value);
      const circle=document.createElementNS(contactSvg.namespaceURI,'circle');for(const [key,value]of Object.entries({cx:9,cy:8,r:3}))circle.setAttribute(key,value);
      const contactPath=document.createElementNS(contactSvg.namespaceURI,'path');contactPath.setAttribute('d','M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 5');contactSvg.append(circle,contactPath);contactIcon.append(contactSvg);
      const contactArrow=node('span','›','mine-profile-link-arrow');contactArrow.setAttribute('aria-hidden','true');
      contacts.append(contactIcon,profileCopy('span','收付款联系人','Payment contacts'),contactArrow);actions.append(contacts);
      host.replaceChildren(list,actions);syncProfileCopy();
    }catch(error){if(id===request)failure(host,error,()=>{NavrenFinancialData.clear();loadFinancialProfile();});}
  }
  async function loadProfile(id=request) {renderRiskProfile();const risk=document.getElementById('mineBusinessRisk'),observations=document.getElementById('mineObservationList');risk.replaceChildren(node('h3','资金与风险设置'),node('p','正在核对业务配置…'));observations.replaceChildren(node('p','正在读取聊天陈述…','mine-state'));await Promise.allSettled([
    loadFinancialProfile(id),
    (async()=>{try{const data=await read('/overview',id);if(!data)return;const level=data.profile?.risk_level;risk.replaceChildren(node('h3','资金与风险设置'),node('p',Number.isInteger(level)&&level>=1&&level<=5?'业务配置 R'+level:'业务等级未知'),node('p','来源：账户手动配置 · 参考日期 '+data.as_of+'。不等于正式问卷测评。','mine-source'));const a=node('a','查看业务资金与风险设置 ›');a.href='account.html?view=wealth#wealth';risk.append(a);}catch(error){if(id===request)failure(risk,error,()=>loadProfile());}})(),
    (async()=>{try{const data=await read('/personal-graph?view=preferences&source=simulation&portfolio=current',id);if(!data)return;const expectedRevision=data.personal_revision;observations.replaceChildren();const items=(data.nodes||[]).filter(n=>n.observation_id);if(!items.length)observations.append(node('p','暂无聊天陈述。','mine-state'));for(const item of items){const confirmationToken=item.confirmation_token;const row=node('article',null,'mine-observation');row.append(original('h3',item.label),node('span',statusNames[item.status]||'状态未知','mine-status'),item.quote?original('blockquote',item.quote):node('blockquote','原话未提供'),node('p','来源：对话陈述，未经银行核验。','mine-source'));for(const [decision,label]of [['confirm','确认是我的陈述'],['delete','删除这项资料']]){if(decision==='confirm'&&item.status==='confirmed')continue;row.append(button(label,async()=>{if(!confirm(decision==='delete'?'删除这项陈述与派生关系？银行交易和目标仍保留。':'确认这段原话是本人陈述？不会改变银行业务等级。'))return;try{if(!Number.isSafeInteger(expectedRevision)||decision==='confirm'&&!confirmationToken)throw Error('资料版本或确认凭据未取得，请重新核对。');const payload={decision,expected_revision:expectedRevision};if(decision==='confirm')payload.confirmation_token=confirmationToken;await BankClient.post('/profile-observations/'+item.observation_id,payload);await BankClient.refreshIdentity();window.NavrenViews?.open('profile',{},{replace:true});}catch(error){showToast('资料未更新：'+error.message);}}));}observations.append(row);}}catch(error){if(id===request)failure(observations,error,()=>loadProfile());}})(),
    loadFinancialMemories({reuseConfirmed:true})
  ]);}
  async function goalDetail(ctx,id=request) {const box=document.getElementById('mineGoalDetail');box.replaceChildren(node('p','正在核对目标对象…','mine-state'));if(!ctx.object_id){failure(box,new Error('缺少目标标识。请选择目标后查看。'));return;}try{const data=await read('/goals/'+encodeURIComponent(ctx.object_id),id);if(!data)return;const goal=data.goal||data.item||data;if(!goal.id||goal.id!==ctx.object_id)throw Error('目标身份无法核验。');box.replaceChildren();box.append(node('h2',goal.name||'未命名目标'));if(!available(goal,ctx))box.append(node('p','目标已有新版本，以下为最新核实状态；旧编辑条件已失效。','mine-state'));const dl=node('dl',null,'mine-goal-details');for(const [label,value]of [['目标金额',money(goal.target_minor,goal.currency)],['目标记录已安排',money(goal.saved_minor,goal.currency)],['目标截止日',goal.due_on||'未知'],['状态',statusNames[goal.status]||goal.status||'未知'],['来源','账户目标服务'],['参考日期',data.as_of||'未知']]){dl.append(node('dt',label),node('dd',value));}box.append(dl,node('p','记录已安排金额不等于账户资金冻结、预留或扣款。目标完成由本人确认，不由提醒勾选推断。','mine-source'));const actions=node('div',null,'mine-row-actions');actions.append(button('修改目标',()=>editGoal(goal)),button('删除目标',()=>prepareDeletion(goal)),button('问 Navren',()=>NavrenViews.ask('请解释这个目标的资金联系、期限和下一步需要核实的条件。',goalRef(goal))));actions.append(safeLink('查看此目标的资金联系 ›','graph',{source_scope:'simulation',portfolio_id:'current',filters:{graph_view:'plans',selected_id:'goal:'+goal.id}}));box.append(actions);const calendar=node('a','在财务日历查看截止日 ›');const url=new URL('account.html',location.href);url.searchParams.set('view','calendar');url.searchParams.set('object_kind','goal');url.searchParams.set('object_id',goal.id);url.searchParams.set('source','simulation');if(goal.due_on)url.searchParams.set('calendar_on',goal.due_on);url.hash='calendar';calendar.href=url.pathname+url.search+url.hash;box.append(calendar,node('p','目标截止日由目标服务维护；独立提醒由日历维护。修改或删除提醒不会修改此目标。','mine-source'));}catch(error){if(id===request)failure(box,error,()=>goalDetail(ctx));}}
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
  // 供 mine.js 的收藏入口打开原对象；归档不能退回同 ID 的当前正文。
  function readingHref(item) {
    const kind=item?.kind||item?.object_kind,id=item?.id||item?.object_id,version=item?.object_version??item?.version;
    if(item?.time_view!=null){
      if(item.time_view!=='historical_audit'||!['article','weekly'].includes(kind)||item.archive!=null||
          item.source_scope&&!['public','market','public_market'].includes(item.source_scope)||
          !window.MarketUI?.historySelection||!window.NavrenViews?.url)return null;
      try{const exact=MarketUI.historySelection({kind,id,version,time_view:'historical_audit',...(item.period?{period:item.period}:{})});
        return NavrenViews.url({page:'recommend',view:kind==='weekly'?'weekly':'reading',...exact});
      }catch(_){return null;}
    }
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
  function syncHomeSections(){const labels={'账户资料':'Account profile','资金与风险':'Funds and risk','偏好与记忆':'Preferences and memories','我的收藏':'My favorites'};for(const heading of document.querySelectorAll('[data-home-section-title]'))heading.textContent=window.NavrenI18n?.language==='en'?labels[heading.dataset.homeSectionTitle]:heading.dataset.homeSectionTitle;}
  window.addEventListener('navren:language',syncHomeSections);
  function syncWorkspaceNavigation(view){
    const selected=view==='settings'?document.getElementById('navrenSettings'):document.querySelector('.sidebar .nav-item[href*="mine.html"]');
    for(const link of document.querySelectorAll('.sidebar .nav-item, #navrenSettings')){
      const active=link===selected;link.classList.toggle('active',active);
      if(active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
    }
  }
  async function render(ctx) {if(['profile','saved','continuereading'].includes(ctx.view)){const target=['saved','continuereading'].includes(ctx.view)?'mineSavedSection':ctx.object_kind==='observation'?'mineObservationList':'mineAccountSection';return NavrenViews.open('home',{...ctx,view:'home',focustarget:target},{replace:true});}syncHomeSections();if(ctx.view!==current.view||ctx.object_id!==current.object_id){closeModal();window.NavrenAAPayments?.clear?.();window.NavrenPaymentContacts?.clear?.();}current={...ctx,view:ctx.view||'home'};if(['appearance'].includes(current.view))current.view='settings';if(current.view==='goal-detail')current.view='goal';const id=++request;const view=Object.hasOwn(titles,current.view)?current.view:'unavailable';syncWorkspaceNavigation(view);for(const panel of document.querySelectorAll('[data-mine-view]'))panel.hidden=panel.dataset.mineView!==(view==='goal-draft'?'goal':view);document.getElementById('mineViewTitle').textContent=titles[view]||'对象不可用';document.getElementById('mineViewEyebrow').textContent=view==='home'?'长期安排与个人理解':'我的 / '+(titles[view]||'原对象无法打开');const back=document.querySelector('[data-mine-back]');back.id='mineBackToPrevious';back.hidden=['home','settings'].includes(view);back.closest('.mine-view-heading').hidden=back.hidden;back.onclick=e=>{e.preventDefault();NavrenViews.back();};document.querySelector('.header-location').textContent=titles[view]||'我的';syncRelationTitle();NavrenGraph?.pause?.();if(view!=='graph')NavrenGraph?.invalidate?.();if(view==='unavailable'){const panel=document.getElementById('mineGoalDetail');panel.hidden=false;failure(panel,new Error('对象不存在或已删除。不会替换为另一个同名对象。'));return;}if(view==='home'){window.NavrenPersonalProfile?.renderSummary();syncProfileCopy();await Promise.allSettled([loadOverview(id),loadProfile(id),window.NavrenPersonalProfile?.whenReady?.()]);if(id!==request)return;const observation=document.getElementById('mineObservationList');if(current.focustarget===observation?.id){const disclosure=observation.closest('details');if(disclosure)disclosure.open=true;}await BankClient.refreshIdentity();if(id!==request)return;renderBookmarks();}else if(view==='payment-contacts'){
      const host=document.getElementById('minePaymentContactsHost');
      if(window.NavrenPaymentContacts){const controller=NavrenPaymentContacts.mount(host,{isCurrent:()=>id===request&&current.view==='payment-contacts'});await controller?.ready;}else failure(host,new Error('收付款联系人暂未取得，请稍后重新读取。'));
    }else if(view==='goals')await loadGoals(false,id);else if(view==='goal')await goalDetail(current,id);else if(view==='goal-draft')await draftDetail(current,id);else if(view==='profile')await loadProfile(id);else if(view==='settings'){NavrenDesign.mountSettings?.(document.getElementById('navrenThemeSettings'));await loadFinancialMemories({reuseConfirmed:true});}else if(view==='saved'){await BankClient.refreshIdentity();if(id!==request)return;renderBookmarks();}else if(view==='graph'){const host=document.getElementById('mineGraphHost');if(!graphStarted){const ready=NavrenGraph.init(host,current);graphStarted=true;await ready;}else await NavrenGraph.restore?.(current);}}
  function init() {for(const a of document.querySelectorAll('[data-mine-open]'))a.addEventListener('click',async e=>{if(e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;e.preventDefault();const target=a.dataset.mineFocustarget;await open(a.dataset.mineOpen,target?{focustarget:target}:{});if(target)requestAnimationFrame(()=>{const section=document.getElementById(target);if(section&&!section.closest('[hidden]')){section.tabIndex=-1;section.focus({preventScroll:true});section.scrollIntoView({block:'start',behavior:'auto'});}});});for(const b of document.querySelectorAll('[data-goal-create]'))b.onclick=()=>editGoal();window.NavrenViews?.register('mine',render);}
  window.addEventListener('navren:privacy-reset',()=>{window.NavrenAAPayments?.clear?.();window.NavrenPaymentContacts?.clear?.();request++;for(const id of ['mineGoalPreview','mineGoalList','mineGoalDetail','mineObservationList','mineBusinessRisk','mineFinancialProfile','minePaymentContactsHost'])document.getElementById(id)?.replaceChildren(node('p','正在核验新的资料权限…','mine-state'));NavrenGraph?.invalidate?.();activeDraft=null;drafts.clear();scheduleWorkspaceRefresh();});
  window.addEventListener('navren:memory-updated',()=>{if(['home','profile','settings'].includes(current.view))scheduleWorkspaceRefresh();});
  for(const event of ['navren:identity-revoked','navren:account-switched','pagehide'])window.addEventListener(event,cancelWorkspaceRefresh);
  window.addEventListener('pagehide',()=>{window.NavrenAAPayments?.clear?.();window.NavrenPaymentContacts?.clear?.();request++;for(const panel of document.querySelectorAll('[data-mine-view]'))panel.hidden=true;closeModal();NavrenGraph?.invalidate?.();});
  window.addEventListener('navren:account-state',()=>{if(['home','saved'].includes(current.view))renderBookmarks();});
  window.addEventListener('beforeunload',event=>{if(drafts.size){event.preventDefault();event.returnValue='';}});
  document.addEventListener('DOMContentLoaded',init);
  return {render,editGoal,readingHref,current:()=>({...current})};
})();
