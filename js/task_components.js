/** 任务结果显示合同 v1。金融值来自基座；本模块没有计算、准备、确认或执行接口。 */
window.NavrenTaskComponents = (() => {
  'use strict';
  const VERSION = 'navren.task-result.v1';
  const controllers = new Set();
  const translations = {
    result:['任务结果','Task result'], conditions:['重要条件','Important conditions'],
    metrics:['关键金额与安排','Key amounts & arrangements'], evidence:['查看计算依据与来源','Calculation basis & sources'],
    actions:['下一步','Next steps'], source:['来源','Source'], version:['数据版本','Data version'],
    period:['期间','Period'], asof:['截至','As of'], scope:['数据范围','Data scope'], basis:['口径','Basis'],
    exclusive:['末日不包含','end date excluded'],inclusive:['包含末日','end date included'],
    calculated:['已计算','Calculated'], partial:['部分已计算','Partially calculated'], unavailable:['不可计算','Unavailable'],
    forecast:['假设推演','Assumed scenario'], missing:['缺失项','Missing inputs'], exclusions:['未纳入项','Excluded items'],
    assumptions:['假设输入','Assumed inputs'], included:['已纳入','Included'], deducted:['已扣除','Deducted'],
    formula:['公式标识','Formula ID'], inputs:['输入记录','Input records'], references:['来源版本','Source versions'],
    unknown:['未知','Unknown'], fixture:['固定合同样例 · 尚未接入实际任务','Fixed contract example · not connected to live tasks'],
    stale:['账户或资料授权已变化，请重新查询。','The account or privacy permission has changed. Query again.'],
    invalid:['结果资料不完整，请重新查询。','The result is incomplete. Query again.'],
    adapter:['此操作暂不可核验，请重新查询。','This operation cannot currently be verified. Query again.'],
    failed:['暂未完成，请重试。','This step could not be completed. Try again.'],
    open_detail:['查看详情','View details'], ask:['问 Navren','Ask Navren'],
    revise_conditions:['修改分析条件','Change analysis conditions'],
    review_operation:['核对待确认操作','Review pending operation'], view_receipt:['查看操作回执','View operation receipt'],
    no_execution:['此结果仅供分析。','This result is for analysis.'],
    gain:['盈利','Gain'], loss:['亏损','Loss'], zero:['已计算为零','Calculated zero']
  };
  const statusNames = {calculated:'calculated',partial:'partial',unavailable:'unavailable',forecast:'forecast'};
  const referenceKinds = new Set(['account','transaction','financial_metric','cashflow','product','research_plan','event','weekly','goal','calendar_event','operation','receipt']);
  const actionKinds = new Set(['open_detail','ask','revise_conditions','review_operation','view_receipt']);
  const currencyPattern = /^[A-Z]{3}$/;
  const identityValid = identity => identity && typeof identity.workspace_id === 'string' && identity.workspace_id.length > 0 && Number.isSafeInteger(identity.privacy_revision) && identity.privacy_revision >= 0;
  const sameIdentity = (a,b) => identityValid(a) && identityValid(b) && a.workspace_id === b.workspace_id && a.privacy_revision === b.privacy_revision;
  const nonempty = (value,max=1000) => typeof value === 'string' && value.trim().length > 0 && value.length <= max;
  const versionValid = value => Number.isSafeInteger(value) && value >= 1 || nonempty(value,160);
  const textValid = value => nonempty(value,6000) || value && typeof value === 'object' && nonempty(value.zh,6000) && (value.en == null || nonempty(value.en,6000));
  const dateValid = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value+'T12:00:00Z')) && new Date(value+'T12:00:00Z').toISOString().slice(0,10) === value;
  const array = (value,max=200) => Array.isArray(value) && value.length <= max;
  const clone = value => JSON.parse(JSON.stringify(value));
  const language = () => window.NavrenI18n?.language != null ? window.NavrenI18n.language === 'en' ? 'en' : 'zh-CN' : document.documentElement.dataset.language === 'en' ? 'en' : 'zh-CN';
  const translated = key => translations[key]?.[language() === 'en' ? 1 : 0] || key;
  const label = value => typeof value === 'string' ? value : language() === 'en' ? value.en || value.zh : value.zh;
  function element(tag,text,className) { const n=document.createElement(tag);if(className)n.className=className;if(text!=null)n.textContent=String(text);return n; }
  function original(tag,text,className) { const n=element(tag,text,className);n.setAttribute('data-i18n-skip','');return n; }
  function ui(tag,key,className) { return element(tag,translated(key),className); }
  function addOriginalList(parent,key,items) { if(!items?.length)return;const row=element('div',null,'task-component-basis-row');row.append(ui('strong',key));const list=element('ul');for(const item of items)list.append(original('li',label(item)));row.append(list);parent.append(row); }
  function referenceValid(ref,binding) {
    return ref && referenceKinds.has(ref.kind) && nonempty(ref.id,160) && nonempty(ref.source_scope,160) && versionValid(ref.version) && sameIdentity(ref,binding)&&
      !(ref.kind==='weekly'&&ref.parameters?.period!=null&&ref.parameters.period!==ref.id);
  }
  const sameReference=(a,b)=>versionValid(a?.version) && versionValid(b?.version) && a?.kind===b?.kind && a?.id===b?.id && a?.source_scope===b?.source_scope && String(a.version)===String(b.version);
  function issuesFor(payload) {
    const issues=[];const check=(valid,field)=>{if(!valid)issues.push(field);};
    check(payload && typeof payload === 'object' && !Array.isArray(payload),'result');if(!payload || typeof payload!=='object' || Array.isArray(payload))return issues;
    check(payload.contract_version === VERSION,'contract_version');check(nonempty(payload.result_id,160),'result_id');check(versionValid(payload.result_version),'result_version');
    check(['analysis','operation','receipt'].includes(payload.result_kind),'result_kind');check(identityValid(payload),'identity');
    check(nonempty(payload.subject_id,160),'subject_id');check(nonempty(payload.source_scope,160),'source_scope');check(versionValid(payload.source_version),'source_version');check(textValid(payload.conclusion),'conclusion');
    check(array(payload.conditions,30) && payload.conditions.every(textValid),'conditions');
    check(array(payload.metrics,24),'metrics');check(array(payload.evidence,200),'evidence');check(array(payload.actions,12),'actions');
    const ids=new Set();for(const [i,m] of (Array.isArray(payload.metrics)?payload.metrics:[]).entries()){
      const field='metrics['+i+']';check(m && nonempty(m.metric_id,160) && !ids.has(m.metric_id),field+'.metric_id');if(!m)continue;ids.add(m.metric_id);
      check(nonempty(m.subject_id,160) && sameIdentity(m,payload),field+'.identity');check(textValid(m.metric_name),field+'.metric_name');
      check(typeof m.currency==='string' && currencyPattern.test(m.currency) && m.unit === 'minor' && m.minor_unit_scale === 2,field+'.currency_unit');
      check(m.value_minor === null || Number.isSafeInteger(m.value_minor),field+'.value_minor');check(typeof m.calculation_status==='string' && Object.hasOwn(statusNames,m.calculation_status),field+'.calculation_status');
      if(m.calculation_status === 'calculated')check(Number.isSafeInteger(m.value_minor),field+'.calculated_value');
      if(m.calculation_status === 'unavailable')check(m.value_minor === null,field+'.unavailable_value');
      const period=m.period_start != null || m.period_end != null;check(period ? dateValid(m.period_start) && dateValid(m.period_end) && m.period_start <= m.period_end && typeof m.period_end_inclusive==='boolean' : nonempty(m.as_of,80) && Number.isFinite(Date.parse(m.as_of)),field+'.period');
      check(nonempty(m.data_scope,160) && nonempty(m.source_scope,160) && versionValid(m.source_version),field+'.source');
      check(m.basis && nonempty(m.basis.kind,160) && textValid(m.basis.label),field+'.basis');
      for(const key of ['included','deducted'])check(array(m.basis?.[key],100) && m.basis[key].every(textValid),field+'.basis.'+key);
      check(nonempty(m.formula_id,160),field+'.formula_id');
      check(array(m.input_refs,200) && m.input_refs.every(ref=>referenceValid(ref,payload)),field+'.input_refs');
      check(array(m.source_versions,200) && m.source_versions.every(ref=>referenceValid(ref,payload)),field+'.source_versions');
      for(const key of ['excluded_items','coverage_gaps','assumed_inputs'])check(array(m[key],100) && m[key].every(textValid),field+'.'+key);
      if(['partial','unavailable'].includes(m.calculation_status))check(m.coverage_gaps?.length > 0,field+'.missing_inputs');
      if(m.calculation_status === 'forecast')check(m.assumed_inputs?.length > 0,field+'.assumptions');
    }
    for(const [i,e] of (Array.isArray(payload.evidence)?payload.evidence:[]).entries())check(e && nonempty(e.evidence_id,160) && textValid(e.title) && referenceValid(e.reference,payload) && (e.description==null || textValid(e.description)),'evidence['+i+']');
    const cards=Array.isArray(payload.business_refs)?payload.business_refs:[];check(array(payload.business_refs,12) && cards.every(ref=>referenceValid(ref,payload)),'business_refs');
    check(!Object.hasOwn(payload,'bank_actions') && !Object.hasOwn(payload,'draft') && !Object.hasOwn(payload,'confirmation_token'),'no_legacy_authority');
    if(payload.result_kind === 'analysis')check(cards.length === 0,'analysis_business_refs');
    if(payload.result_kind === 'operation')check(cards.length > 0 && cards.every(ref=>ref?.kind === 'operation'),'operation_refs');
    if(payload.result_kind === 'receipt')check(cards.length > 0 && cards.every(ref=>ref?.kind === 'receipt'),'receipt_refs');
    const actionIDs=new Set();for(const [i,a] of (Array.isArray(payload.actions)?payload.actions:[]).entries()){
      const field='actions['+i+']';check(a && nonempty(a.action_id,160) && !actionIDs.has(a.action_id) && actionKinds.has(a.type),field);if(!a)continue;actionIDs.add(a.action_id);
      check(a.label==null || textValid(a.label),field+'.label');check(referenceValid(a.reference,payload),field+'.reference');
      if(payload.result_kind === 'analysis')check(!['review_operation','view_receipt'].includes(a.type) && !['operation','receipt'].includes(a.reference?.kind),field+'.analysis_readonly');
      if(a.type === 'review_operation')check(payload.result_kind === 'operation' && a.reference?.kind==='operation' && cards.some(ref=>sameReference(ref,a.reference)),field+'.operation_ref');
      if(a.type === 'view_receipt')check(payload.result_kind === 'receipt' && a.reference?.kind==='receipt' && cards.some(ref=>sameReference(ref,a.reference)),field+'.receipt_ref');
    }
    for(const [i,ref] of cards.entries()){const type=ref?.kind==='operation'?'review_operation':'view_receipt';check(Array.isArray(payload.actions) && payload.actions.some(a=>a && a.type===type && sameReference(a.reference,ref)),'business_refs['+i+'].action');}
    return issues;
  }
  function amount(metric) {
    if(metric.value_minor === null)return metric.currency+' '+translated('unknown');
    // 整数分只作十进制展示换算。BigInt避免大整数除以100时丢掉分位；不推算金融结果。
    const negative=metric.value_minor < 0,raw=String(Math.abs(metric.value_minor)).padStart(3,'0'),integer=raw.slice(0,-2),fraction=raw.slice(-2);
    const digits=new Intl.NumberFormat(language()==='en'?'en-US':'zh-CN',{maximumFractionDigits:0}).format(BigInt(integer));
    const sign=negative?'−':metric.basis.kind==='period_profit_loss' && metric.value_minor > 0?'+':'';
    return metric.currency+' '+sign+digits+'.'+fraction;
  }
  function metadataRow(parent,key,value) { const row=element('div',null,'task-component-meta-row');row.append(ui('dt',key),original('dd',value));parent.append(row); }
  function metricCard(metric) {
    const card=element('article',null,'task-component-metric');card.dataset.metricId=metric.metric_id;card.dataset.subjectId=metric.subject_id;card.dataset.sourceScope=metric.source_scope;card.dataset.sourceVersion=String(metric.source_version);card.dataset.calculationStatus=metric.calculation_status;
    const title=original('h4',label(metric.metric_name));const value=original('p',amount(metric),'task-component-amount');value.dataset.currency=metric.currency;value.dataset.unit=metric.unit;
    if(metric.value_minor !== null)value.dataset.valueMinor=String(metric.value_minor);
    if(metric.basis.kind==='period_profit_loss' && metric.calculation_status==='calculated') { const kind=metric.value_minor > 0?'gain':metric.value_minor < 0?'loss':'zero';value.classList.add('is-'+kind);value.setAttribute('aria-label',amount(metric)+' · '+translated(kind)); }
    const state=ui('span',statusNames[metric.calculation_status],'task-component-status');card.append(title,value,state);
    const meta=element('dl',null,'task-component-meta');metadataRow(meta,metric.period_start?'period':'asof',metric.period_start?metric.period_start+' — '+metric.period_end+' · '+translated(metric.period_end_inclusive?'inclusive':'exclusive'):metric.as_of);metadataRow(meta,'basis',label(metric.basis.label));metadataRow(meta,'scope',metric.data_scope);card.append(meta);
    addOriginalList(card,'included',metric.basis.included);addOriginalList(card,'deducted',metric.basis.deducted);
    // 影响金额解释的缺项和假设直接可见，不藏在依据折叠里。
    addOriginalList(card,'missing',metric.coverage_gaps);addOriginalList(card,'assumptions',metric.assumed_inputs);addOriginalList(card,'exclusions',metric.excluded_items);return card;
  }
  function create(host,options={}) {
    if(!host || typeof host.replaceChildren !== 'function')throw new TypeError('task host required');
    const identity=options.identity || (()=>window.BankClient?.identity());const pendingActions=new Set();let generation=0,destroyed=false,data=null,binding=null,renderTicket=null,abort=new AbortController();
    const identityNow=()=>{const current=identity();return identityValid(current)?{workspace_id:current.workspace_id,privacy_revision:current.privacy_revision}:null;};
    const current=ticket=>!destroyed && ticket?.generation===generation && sameIdentity(ticket,identityNow()) && (!ticket.guard || typeof ticket.guard.valid==='function' && ticket.guard.valid());
    function clear(reason) { const hadData=!!data;generation++;abort.abort();abort=new AbortController();pendingActions.clear();data=null;binding=null;renderTicket=null;host.replaceChildren();host.removeAttribute('data-task-result-id');host.removeAttribute('data-task-workspace');if(hadData)options.onInvalidate?.();if(reason)host.append(ui('p',reason,'task-component-notice')); }
    function begin() { const snapshot=identityNow();if(!snapshot)throw new Error('task identity unavailable');if(data)clear();generation++;return Object.freeze({...snapshot,generation,guard:options.guard?.()||window.BankClient?.guard?.()}); }
    function rejection(reason,issues=[],ticket) { if(binding&&(!sameIdentity(binding,identityNow()) || ticket===renderTicket && !current(ticket)))clear('stale');return {ok:false,reason,issues}; }
    function refRow(ref) { const row=element('li',null,'task-component-reference');row.append(original('code',ref.kind+'/'+ref.id),original('span',ref.source_scope+' · '+String(ref.version)));return row; }
    function evidenceBox(payload) {
      const details=element('details',null,'task-component-evidence');details.append(ui('summary','evidence'));const body=element('div',null,'task-component-evidence-body');
      for(const m of payload.metrics){const section=element('section');section.append(original('h4',label(m.metric_name)),original('code',m.metric_id,'task-component-evidence-id'));const meta=element('dl',null,'task-component-meta');metadataRow(meta,'formula',m.formula_id);metadataRow(meta,'source',m.source_scope+' · '+String(m.source_version));section.append(meta);for(const [key,refs] of [['inputs',m.input_refs],['references',m.source_versions]]){if(!refs.length)continue;section.append(ui('h5',key));const list=element('ul');for(const ref of refs)list.append(refRow(ref));section.append(list);}body.append(section);}
      for(const e of payload.evidence){const section=element('section');section.append(original('h4',label(e.title)),original('code',e.evidence_id,'task-component-evidence-id'));if(e.description)section.append(original('p',label(e.description)));const list=element('ul');list.append(refRow(e.reference));section.append(list);body.append(section);}details.append(body);return details;
    }
    function paint(payload,ticket) {
      const expanded=host.querySelector('.task-component-evidence')?.open || false,focusAction=host.contains(document.activeElement)?document.activeElement.dataset.taskActionId:null;
      const root=element('section',null,'navren-task-result');root.setAttribute('aria-label',translated('result'));root.dataset.resultKind=payload.result_kind;root.dataset.objectId=payload.result_id;root.dataset.objectVersion=String(payload.result_version);root.dataset.sourceScope=payload.source_scope;
      const conclusion=original('p',label(payload.conclusion),'task-component-conclusion');root.append(conclusion);
      if(options.fixture)root.append(ui('p','fixture','task-component-fixture'));
      if(payload.conditions.length){const conditions=element('section',null,'task-component-conditions');conditions.append(ui('h3','conditions'));const list=element('ul');for(const condition of payload.conditions)list.append(original('li',label(condition)));conditions.append(list);root.append(conditions);}
      if(payload.metrics.length){const section=element('section',null,'task-component-metrics-section');section.append(ui('h3','metrics'));const cards=element('div',null,'task-component-metrics');for(const m of payload.metrics)cards.append(metricCard(m));section.append(cards);root.append(section);}
      if(payload.metrics.length || payload.evidence.length){const evidence=evidenceBox(payload);evidence.open=expanded;root.append(evidence);}
      if(payload.actions.length){
        const actions=element('section',null,'task-component-actions');actions.append(ui('h3','actions'));
        const row=element('div',null,'task-component-action-row');
        for(const action of payload.actions){
          const b=action.label?original('button',label(action.label)):ui('button',action.type);
          b.type='button';b.className='task-component-button';b.dataset.taskActionId=action.action_id;
          const business=['review_operation','view_receipt'].includes(action.type);
          // 回执必须由显式只读适配器提供；openDraft可能包含准备/确认功能，只供核对操作。
          const receiptAction=action.type==='view_receipt',receiptAdapter=receiptAction&&typeof options.onReceipt==='function';
          const bankAdapter=action.type==='review_operation' && action.reference.source_scope==='bank_operation' && typeof window.BankUI?.openDraft==='function';
          if(bankAdapter)b.dataset.agentOperation='true';
          const supported=receiptAction?receiptAdapter:typeof options.onAction==='function'||bankAdapter;
          const pendingKey=ticket.generation+':'+action.action_id;
          b.disabled=pendingActions.has(pendingKey) || options.fixture&&business || !supported;
          b.onclick=async()=>{
            if(options.fixture&&business || !supported)return;
            if(!current(ticket)){rejection('STALE_BINDING',[],ticket);return;}if(pendingActions.has(pendingKey))return;
            pendingActions.add(pendingKey);b.disabled=true;
            const context={workspace_id:ticket.workspace_id,privacy_revision:ticket.privacy_revision,signal:abort.signal,isCurrent:()=>current(ticket)};
            try{if(receiptAdapter)await options.onReceipt(clone(action),context);else if(typeof options.onAction==='function')await options.onAction(clone(action),context);else if(bankAdapter)await window.BankUI.openDraft({operation_id:action.reference.id},b,{...context,expected_operation_version:action.reference.version});}
            catch(error){const present=host.querySelector('.task-component-actions');if(current(ticket)&&present&&!present.querySelector('.task-component-action-error')){const notice=ui('p','failed','task-component-action-error');notice.setAttribute('role','status');present.append(notice);}}
            finally{pendingActions.delete(pendingKey);if(current(ticket)){const present=[...host.querySelectorAll('[data-task-action-id]')].find(n=>n.dataset.taskActionId===action.action_id);if(present)present.disabled=false;}}
          };
          row.append(b);if(!supported){const notice=ui('p','adapter','task-component-action-unavailable');notice.setAttribute('role','status');row.append(notice);}
        }
        actions.append(row);root.append(actions);
      }
      if(payload.result_kind==='analysis')root.append(ui('p','no_execution','task-component-analysis-state'));
      host.replaceChildren(root);host.dataset.taskResultId=payload.result_id;host.dataset.taskWorkspace=payload.workspace_id;
      if(focusAction)[...host.querySelectorAll('[data-task-action-id]')].find(b=>b.dataset.taskActionId===focusAction)?.focus({preventScroll:true});
    }
    function render(payload,ticket) {
      if(destroyed)return {ok:false,reason:'DESTROYED',issues:[]};
      if(!ticket){try{ticket=begin();}catch{return rejection('IDENTITY_UNAVAILABLE');}}
      if(!current(ticket) || !sameIdentity(payload,ticket))return rejection('STALE_BINDING',[],ticket);
      const issues=issuesFor(payload);if(issues.length){clear('invalid');return {ok:false,reason:'INVALID_CONTRACT',issues};}
      try{data=clone(payload);}catch{clear('invalid');return {ok:false,reason:'INVALID_CONTRACT',issues:['json']};}
      binding={workspace_id:payload.workspace_id,privacy_revision:payload.privacy_revision};renderTicket=ticket;paint(data,ticket);return {ok:true,result_id:payload.result_id};
    }
    function refreshLanguage() { if(!data)return;if(!current(renderTicket)){clear('stale');return;}paint(data,renderTicket); }
    const invalidate=()=>clear();for(const type of ['navren:privacy-reset','navren:account-change','pagehide'])window.addEventListener(type,invalidate);
    window.addEventListener('navren:language',refreshLanguage);
    const controller={begin,render,clear,isCurrent:()=>!!data&&current(renderTicket),destroy(){if(destroyed)return;clear();destroyed=true;for(const type of ['navren:privacy-reset','navren:account-change','pagehide'])window.removeEventListener(type,invalidate);window.removeEventListener('navren:language',refreshLanguage);controllers.delete(controller);},snapshot:()=>data?{result_id:data.result_id,result_kind:data.result_kind,workspace_id:data.workspace_id,privacy_revision:data.privacy_revision}:null};controllers.add(controller);return controller;
  }
  return {contractVersion:VERSION,create,validate:payload=>{const issues=issuesFor(payload);return {ok:issues.length===0,issues};},invalidate:()=>{for(const controller of controllers)controller.clear();}};
})();
