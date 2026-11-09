/* 账户页与对话共享安全 DOM 组件。金额只显示后端结果，历史卡片保留快照。 */
window.CalendarUI=(()=>{
  let uiEpoch=0;const renderEpochs=new WeakMap(),dialogs=new Set(),financialRoots=new Set();
  const localEn={'盈利':'Gain','亏损':'Loss','已计算零值':'Calculated zero','未知':'Unknown','数据缺失':'Missing data','未披露':'Not disclosed','当前筛选事项':'Currently filtered events','当日全部事项':'All events for this day','所选来源与组合内的全部事项；不合并其他账本。':'All events within the selected source and portfolio; other ledgers remain separate.','来源版本':'Source versions','计算与来源依据':'Calculation and source basis','正在读取全部事项…':'Loading all events…','日历权限已变化，请重新核对。':'Calendar permission changed. Verify it again.','无收益披露':'No return disclosed'};
  Object.assign(localEn,{'未提供合计':'Total unavailable','来源未提供可展示的合计，不能按事项数量推算。':'The source did not issue a total. Item counts do not determine an amount.','已发布资金快照，仅供阅读。':'Published funding snapshot; read only.','历史已发布快照，仅供阅读。':'Historical published snapshot; read only.','正在核对原已发布资金快照…':'Verifying the original published funding snapshot…','尚缺生活开支、收入稳定性和目标资料，不能据此认定应急储备充足。':'Living costs, income stability and goals are missing; reserve adequacy is unknown.','当前结果未评估日内偿付能力。':'Intraday solvency has not been assessed.','指标':'Metric','所属账户':'Account identity','币种':'Currency','覆盖范围':'Coverage','计算状态':'Calculation status','计算口径':'Calculation basis','方法':'Method','日期与期间':'Date and period','输入引用':'Input references','条件与假设':'Conditions and assumptions','排除项':'Excluded items','覆盖缺口':'Coverage gaps','业务日期':'Business date','时区':'Time zone','日期口径':'Clock basis','身份与授权版本':'Identity and authorization revision','来源范围':'Source scope','缺项':'Missing inputs','资金规则':'Funding rules','资金限制与适用条件':'Funding limits and conditions','资金来源引用':'Funding source reference','图形依据':'Geometry basis'});
  const t=text=>{const value=window.NavrenI18n?.t?.(text)??text;return value===text&&window.NavrenI18n?.language==='en'?(localEn[text]||text):value;};
  Object.assign(localEn,{'输入尚未完整覆盖，具体缺项见来源依据。':'Input coverage is incomplete; see the source basis for the missing inputs.','存在未覆盖资料，具体缺项见来源依据。':'Some inputs are not covered; see the source basis.','视图缺项':'View coverage gaps'});
  function readToken(root){const epoch=(renderEpochs.get(root)||0)+1;renderEpochs.set(root,epoch);const generation=uiEpoch,guard=BankClient.identity?.()?BankClient.guard?.():null,view=window.NavrenViews?.revision?.();return {valid(){return root.isConnected&&uiEpoch===generation&&renderEpochs.get(root)===epoch&&(!guard||guard.valid())&&(view===undefined||window.NavrenViews?.revision?.()===view);}};}
  function hideFinancial(){for(const root of financialRoots){delete root.__navrenFinancialPacket;if(root.isConnected)root.replaceChildren(el('p','账户或授权已变化，资金快照已隐藏。请重新查询。','calendar-note'));}financialRoots.clear();}
  window.addEventListener('navren:privacy-reset',()=>{uiEpoch++;hideFinancial();for(const d of [...dialogs]){d.close?.();if(d.isConnected)d.remove();}dialogs.clear();});
  window.addEventListener('navren:identity-revoked',()=>{uiEpoch++;hideFinancial();});
  window.addEventListener('navren:account-switched',()=>{uiEpoch++;hideFinancial();});
  window.addEventListener('pagehide',()=>{uiEpoch++;hideFinancial();});
  const states={recorded:'已记录',planned:'计划',candidate:'候选',completed:'手工完成',cancelled:'已取消',unknown:'数据缺失',awaiting_confirmation:'待确认',failed:'失败'};
  const kinds={transaction:'收支',investment_pnl:'理财损益',distribution:'分红到账',maturity:'到期/解锁',settlement:'到账安排',bill:'缴费/还款',subscription:'订阅',birthday:'生日',life_event:'生活事件'};
  const sources={simulation:'账户记录',imported:'本地导入账单',user_calendar:'本人日历计划',bank_profile:'已授权账户资料'};
  const money=(v,c)=>Number.isSafeInteger(v)?(v/100).toLocaleString(window.NavrenI18n?.locale||'zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})+(c==='CNY'?(window.NavrenI18n?.language==='en'?' CNY':'元'):' '+c):t('未知');
  function el(tag,text,cls){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined){n.textContent=String(text);if(window.NavrenI18n?.apply)NavrenI18n.apply(n);else n.textContent=t(text);if(localEn[text]){n.dataset.calendarLocalText=text;n.textContent=t(text);}}return n;}
  window.addEventListener('navren:language',()=>{for(const n of document.querySelectorAll('[data-calendar-local-text]'))n.textContent=t(n.dataset.calendarLocalText);for(const n of document.querySelectorAll('[data-calendar-amount]')){const value=n.dataset.calendarAmount==='null'?null:Number(n.dataset.calendarAmount);n.textContent=n.dataset.calendarPnl==='true'?pnlText(value,n.dataset.calendarCurrency,n.dataset.calendarCoverage):money(value,n.dataset.calendarCurrency);}});
  function original(tag,text,cls){const n=el(tag,undefined,cls);n.textContent=String(text??'');n.setAttribute('translate','no');return n;}
  function pnlState(value,coverage){return Number.isSafeInteger(value)?value>0?'positive':value<0?'negative':'zero':coverage==='partial'||coverage==='missing'?'missing':'unknown';}
  function pnlText(value,currency,coverage){if(!Number.isSafeInteger(value))return t(coverage==='partial'||coverage==='missing'?'数据缺失':'未知');return (value>0?'+':'')+money(value,currency);}
  function amountNode(tag,value,currency,coverage,pnl=false,cls=''){const n=el(tag,undefined,cls);n.dataset.calendarAmount=Number.isSafeInteger(value)?String(value):'null';n.dataset.calendarCurrency=currency;n.dataset.calendarCoverage=coverage||'';n.dataset.calendarPnl=String(pnl);n.textContent=pnl?pnlText(value,currency,coverage):money(value,currency);if(pnl){const state=pnlState(value,coverage);n.classList.add('pnl-'+state);n.dataset.calculationStatus=state;}return n;}
  function btn(text,fn){const n=el('button',text,'calendar-button');n.type='button';n.dataset.calendarAction='true';n.onclick=fn;return n;}
  function notice(root,text){const n=el('p',text,'calendar-note');root.append(n);return n;}
  function err(root,e){const p=el('p',e.message||'查询失败，请重试。','calendar-error');p.role='alert';root.append(p);}
  function modal(title){
    const prior=document.activeElement;const d=el('dialog',undefined,'calendar-dialog');d.setAttribute('aria-label',title);
    const head=el('header');head.append(el('h2',title),btn('关闭',()=>d.close()));d.append(head);document.body.append(d);dialogs.add(d);d.showModal();
    d.addEventListener('close',()=>{dialogs.delete(d);d.remove();if(prior?.isConnected)prior.focus();},{once:true});return d;
  }
  function summary(root,rows){
    const box=el('div',undefined,'calendar-summary');
    for(const row of rows||[]){
      const card=el('div',undefined,'calendar-summary-currency');card.append(el('strong',row.currency));
      for(const [key,label] of [['recorded_inflow_minor','已记录流入'],['recorded_outflow_minor','已记录流出'],['recorded_net_minor','已记录净流入'],['confirmed_due_minor','确认待付'],['candidate_due_minor','候选待付'],['user_budget_minor','用户计划预算'],['investment_pnl_minor','已计算投资损益']]){
        if(row[key]===undefined)continue;
        const line=el('span',undefined,'calendar-summary-value');line.append(el('span',label+' '));
        const amount=amountNode('span',row[key],row.currency,row.pnl_coverage,key==='investment_pnl_minor');line.append(amount);card.append(line);
      }
      if(row.unknown_direction_count)card.append(el('span',row.unknown_direction_count+' 笔方向未确定'));
      box.append(card);
    }
    root.append(box);
  }
  function ask(item){
    if(window.NavrenViews){NavrenViews.reference({source_scope:item.source_type==='imported'?'imported':item.portfolio_version?'historical':'simulation',kind:'calendar_item',id:item.id,version:item.source_revision,label:item.title,parameters:{date:item.date,portfolio_id:item.portfolio_version}});return;}
    const text='请查询日历事项「'+item.title+'」，日期 '+item.date+'，事项引用 '+item.id+'，解释最新状态与可操作内容。';
    const input=document.getElementById('userInput');
    if(input&&typeof sendMessage==='function'){input.value=text;sendMessage();}
    else{sessionStorage.setItem('navren_calendar_question',text);location.href='/chat.html';}
  }
  async function itemDetail(item,target=null){
    if(!target&&document.body.dataset.page==='account'&&window.NavrenViews){NavrenViews.open('calendar-item',{object_kind:'calendar_item',object_id:item.id,date:item.date,source_scope:item.source_type==='imported'?'imported':'simulation',portfolio_id:item.portfolio_version||NavrenViews.current().portfolio_id||'current'});return;}
    const d=target||modal('事项详情'),token=readToken(d),loading=notice(d,'正在核对最新来源…');
    try{
      const fresh=await CalendarClient.item(item.id);if(!token.valid()||loading.parentNode!==d)return;loading.remove();
      d.append(original('h3',fresh.title));notice(d,fresh.date+' · '+t(states[fresh.state]||fresh.state)+' · '+t(fresh.portfolio_version?'历史组合（真实净值）':(sources[fresh.source_type]||fresh.source_type)));
      if(fresh.scheduled_date&&fresh.scheduled_date!==fresh.date)notice(d,'原应付日期 '+fresh.scheduled_date);
      if(fresh.amount_minor!=null)notice(d,'金额 '+money(fresh.amount_minor,fresh.currency));
      if(fresh.budget_minor!=null)notice(d,'计划预算 '+money(fresh.budget_minor,fresh.currency)+'，没有扣款');
      if(fresh.fund_id)d.append(btn('产品权益与收益详情',()=>productDetail(fresh.fund_id,fresh.date,fresh.portfolio_version)));
      if(fresh.returns){summary(d,[{currency:fresh.currency,investment_pnl_minor:fresh.returns.daily_pnl_minor,pnl_coverage:fresh.returns.status==='available'?'complete':'partial'}]);notice(d,fresh.returns.status==='available'?'损益可复算，现金到账另列。':t('不可计算')+'：'+(fresh.returns.missing||[]).join('、'));for(const key of ['cumulative_pnl_minor','realized_pnl_minor'])if(fresh.returns[key]!=null)notice(d,t(key==='cumulative_pnl_minor'?'累计损益':'本日已实现收益')+' '+money(fresh.returns[key],fresh.currency));}
      if(fresh.notice)notice(d,fresh.notice);
      if(fresh.goal_id||fresh.linked_role==='goal_deadline'){notice(d,'这是目标截止日的只读投影；修改期限须在目标详情核对影响并确认，不通过日历编辑。');const a=el('a','查看与管理这个目标 ›');a.href='/mine.html?'+new URLSearchParams({view:'goal',object_kind:'goal',object_id:fresh.goal_id||fresh.source_ref,source_scope:'simulation'});d.append(a);}
      if(fresh.payload){notice(d,'重复规则 '+({once:'单次',weekly:'每周',monthly:'每月',yearly:'每年'}[fresh.payload.recurrence]||'')+' · '+(fresh.payload.month_policy==='last_day'?'遇无效日期取当月最后一天':'遇无效日期跳过该期'));notice(d,'提前 '+fresh.payload.remind_days+' 天站内提醒');if(fresh.payload.note)notice(d,fresh.payload.note);}
      const source=el('details');source.append(el('summary','来源与关联'));source.append(el('p',fresh.source_ref),el('p','来源版本 '+(fresh.source_revision||'见银行版本')));if(fresh.linked_commitment_ids?.length)source.append(el('p','关联承诺：'+fresh.linked_commitment_ids.join('、')));d.append(source);
      d.append(btn('继续向 Navren 提问',()=>{d.close?.();ask(fresh);}));
      if((fresh.allowed_actions||[]).includes('edit'))d.append(btn('修改事件',()=>{d.close?.();form(fresh,'edit');}),btn('删除事件',()=>{d.close?.();form(fresh,'delete');}),btn('标记手工完成',()=>{d.close?.();form(fresh,'complete');}));
    }catch(e){if(token.valid()&&loading.parentNode===d){loading.remove();err(d,e);}}
  }
  function itemRow(item){
    const n=btn('',()=>itemDetail(item));n.classList.add('calendar-item');n.append(original('span',item.title),el('span',' · '),el('span',states[item.state]||item.state));
    const meta=el('small');meta.append(el('span',kinds[item.kind]||item.kind),el('span',' · '),el('span',item.portfolio_version?'历史组合（真实行情）':sources[item.source_type]||item.source_type));if(item.overdue)meta.append(el('span',' · '),el('span','原日期已过'));n.append(meta);return n;
  }
  async function dayDetail(widget,on,target=null){
    if(!target&&document.body.dataset.page==='account'&&window.NavrenViews){const base=NavrenViews.current();NavrenViews.open('calendar-day',{date:on,source_scope:widget.source_scope.statement_source,portfolio_id:widget.source_scope.portfolio||'current',time_view:widget.view,object_kind:'calendar_day',object_id:on,filters:{...base.filters,calendar_view_id:widget.view_id},period:widget.range.from});return;}
    const d=target||modal(on+' '+t('财务明细')),token=readToken(d),list=el('div');d.append(list);let offset=0,viewId=widget.view_id,fullView=null;
    notice(list,'当日全部事项');notice(list,'所选来源与组合内的全部事项；不合并其他账本。');
    async function load(){
      if(!list.childNodes.length){notice(list,'当日全部事项');notice(list,'所选来源与组合内的全部事项；不合并其他账本。');}
      const loading=el('p','正在读取全部事项…');list.append(loading);
      try{
        if(!offset&&!fullView){const scope=widget.source_scope||{},portfolio=scope.portfolio||'current';fullView=await CalendarClient.query({from:on,to:on,view:'day',source:scope.statement_source,kinds:[],include_life_events:true,simulation_overlay:scope.simulation_overlay===true,portfolio,accounting_basis:scope.accounting_basis||(portfolio==='current'?'holdings_only_v1':'total_equity_v2')});if(!token.valid()||!list.isConnected)return;const identity=BankClient.identity?.();if((identity&&fullView.workspace_binding!==identity.workspace_id)||fullView.source_scope?.statement_source!==scope.statement_source||fullView.source_scope?.portfolio!==portfolio)throw Error('日历来源已变化，请重新核对。');viewId=fullView.view_id;}
        const result=await CalendarClient.day(viewId,on,offset);if(!token.valid()||!list.isConnected)return;loading.remove();
        if(!offset){if(widget.snapshot?.portfolio){notice(list,widget.snapshot.portfolio.identity+(widget.snapshot.nav_asof?' · 净值截至 '+widget.snapshot.nav_asof:' · 净值未完整覆盖'));if(widget.snapshot.portfolio.archived)notice(list,widget.snapshot.portfolio.archive_notice);}notice(list,result.notice+(result.changed_since_snapshot?' 来源已有更新。':''));summary(list,result.summary_by_currency);if(!result.total)notice(list,'暂无记录，不能据此认定消费或收益为零。');}
        if(!offset){const basis=el('details',undefined,'calendar-day-evidence');basis.append(el('summary','计算与来源依据'));const scope=el('p');scope.append(el('span','来源'),el('span',' · '),el('span',sources[fullView?.source_scope?.statement_source]||fullView?.source_scope?.statement_source||''));basis.append(scope);const versions=el('p',undefined);versions.append(el('span','来源版本'),el('span',' '),original('span',JSON.stringify(result.source_versions||fullView?.source_versions||{})));basis.append(versions);for(const limitation of fullView?.limitations||[])notice(basis,limitation);if(fullView?.coverage_basis)notice(basis,fullView.coverage_basis);list.append(basis);}
        for(const item of result.items)list.append(itemRow(item));offset+=result.items.length;
        if(result.has_more){const more=btn('继续查看 · 共 '+result.total+' 项',()=>{more.remove();load();});list.append(more);}
      }catch(e){if(!token.valid()||!list.isConnected)return;loading.remove();err(list,e);list.append(btn('重新查询',()=>{offset=0;fullView=null;list.replaceChildren();load();}));}
    }load();
  }
  const fundingBlocks=['cash_balance','current_available','current_budget','emergency_floor','obligations','holds'];
  const fpPlain=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
  const fpText=v=>typeof v==='string'&&v.trim().length>0;
  const fpList=v=>Array.isArray(v);
  function fpStable(v){if(Array.isArray(v))return v.map(fpStable);if(fpPlain(v))return Object.fromEntries(Object.keys(v).sort().map(k=>[k,fpStable(v[k])]));return v;}
  const fpEqual=(a,b)=>JSON.stringify(fpStable(a))===JSON.stringify(fpStable(b));
  const fpVersions=(a,b)=>fpPlain(a)&&fpPlain(b)&&Object.keys(a).length>0&&Object.entries(a).every(([k,v])=>fpText(v)&&b[k]===v);
  const fpVersionOverlap=(a,b)=>fpPlain(a)&&fpPlain(b)&&Object.keys(a).length>0&&Object.entries(a).every(([k,v])=>fpText(v)&&(!Object.prototype.hasOwnProperty.call(b,k)||b[k]===v));
  function financialPacket(widget,read,session){
    const fail=()=>{throw Error('资金视图的身份、来源或审核依据不完整，请重新查询。');};
    const p=widget.presentation,response=read?.response,proof=read?.original_agent_final_proof,verified=read?.verification;
    if(widget.schema_version!==1||!fpPlain(p)||p.schema_version!==1||p.contract!=='navren.presentation/1'||
      p.layout!=='funding_blocks'||p.series_kind!=='funding_breakdown'||widget.widget_id!==p.view_id||
      !fpText(p.view_id)||!fpText(p.message_id)||p.request_id!==p.message_id||!fpPlain(p.authorization_revision)||
      p.workspace_binding!==session.workspace_id||p.authorization_revision.privacy!==session.privacy_revision||
      !Number.isSafeInteger(p.authorization_revision.personal)||p.authorization_revision.personal<0||
      session.personal_revision!==undefined&&p.authorization_revision.personal!==session.personal_revision||
      !['current','historical_readonly'].includes(read?.status)||read.workspace_id!==session.workspace_id||
      read.privacy_revision!==session.privacy_revision||read.execution_authority!==false||read.can_reissue_old_approval!==false||
      !verified?.original_message_verified||!verified.identity_verified||!verified.language_verified||!verified.source_versions_verified||
      verified.semantic_review_status!=='approved'||verified.final_guard_status!=='verified'||!fpPlain(response)||
      response.bank_response_id!==p.message_id||!fpEqual(response.presentation,p)||!fpPlain(proof)||
      proof.contract!=='navren.agent-final-proof/1'||proof.workspace_id!==p.workspace_binding||proof.request_id!==p.request_id||
      proof.message_id!==p.message_id||proof.language!==read.language||proof.approved_content!==response.reply||
      proof.verdict?.verdict!=='approve'||proof.approved_response_hash!==read.original_response_hash||
      read.original_publication_receipt?.original_response_hash!==read.original_response_hash||
      read.original_publication_receipt?.publication_state!=='server_send_completed'||
      read.original_publication_receipt?.response_id!==p.message_id)fail();
    const review=proof.approved_review_projection?.presentation_review,batch=response.metric_batch;
    if(!fpPlain(review)||!fpEqual(review.presentation,p)||!fpPlain(batch)||batch.batch_contract!=='navren.financial-tool-metrics/1'||
      batch.workspace_id!==p.workspace_binding||batch.tool_name!=='presentation_view'||!fpText(batch.service_signature)||
      !fpEqual(batch.binding,{view_id:p.view_id,request_id:p.request_id,message_id:p.message_id})||
      !fpVersions(p.source_versions,batch.source_versions)||p.source_versions.personal!==String(p.authorization_revision.personal)||
      !fpPlain(p.scope)||!fpText(p.scope.source)||!fpText(p.scope.currency)||! /^[A-Z]{3}$/.test(p.scope.currency)||
      !fpPlain(p.scope.funding_rule)||!fpPlain(p.scope.funding_query)||!fpPlain(p.funding_limits)||
      !fpText(p.scope.funding_query.coverage_scope)||!fpText(p.coverage)||!fpText(p.data_scope)||
      !fpText(p.as_of)||!fpPlain(p.range)||!fpText(p.range.from)||!fpText(p.range.to)||!fpText(p.timezone)||
      !fpText(p.clock_basis)||!fpText(p.measure_basis)||!fpPlain(p.funding_reference)||
      !fpText(p.funding_reference.snapshot_id)||!fpText(p.funding_reference.snapshot_hash)||
      !fpList(p.important_states)||!p.important_states.length||!p.important_states.every(fpText)||!fpList(p.missing)||
      !fpList(p.points)||p.points.length!==fundingBlocks.length||!fpList(batch.metrics)||batch.metrics.length!==p.points.length||
      !fpList(review.display_amount_occurrences)||review.display_amount_occurrences.length!==p.points.length)fail();
    const metrics=new Map();
    for(const m of batch.metrics){
      if(!fpPlain(m)||!fpText(m.metric_id)||metrics.has(m.metric_id)||m.subject_id!==p.workspace_binding||
        m.unit!=='minor_currency'||!fpText(m.metric_name)||!fpText(m.currency)||!fpText(m.basis)||!fpText(m.formula_id)||
        !fpText(m.data_scope)||!fpVersionOverlap(m.source_versions,p.source_versions)||!fpVersionOverlap(m.source_versions,batch.source_versions)||
        !['as_of','period_start','period_end'].every(k=>Object.prototype.hasOwnProperty.call(m,k)&&(m[k]===null||fpText(m[k])))||
        m.as_of!==null&&m.as_of!==p.as_of||m.period_start!==null&&m.period_start!==p.range.from||m.period_end!==null&&m.period_end!==p.range.to||
        !fpList(m.input_refs)||!m.input_refs.every(fpText)||!fpList(m.assumed_inputs)||!fpList(m.excluded_items)||!fpList(m.coverage_gaps))fail();
      metrics.set(m.metric_id,m);
    }
    const blocks=new Set(),metricIds=new Set();
    p.points.forEach((point,index)=>{
      const value=point?.value,m=metrics.get(value?.metric_id),slot=review.display_amount_occurrences[index];
      if(!fpPlain(point)||!fundingBlocks.includes(point.block)||blocks.has(point.block)||!fpText(point.label)||
        !fpText(point.point_ref)||!fpText(point.coverage)||!fpList(point.missing)||!fpPlain(value)||!m||metricIds.has(m.metric_id)||
        value.currency!==p.scope.currency||value.currency!==m.currency||value.value_minor!==m.value_minor||
        value.calculation_status!==m.calculation_status||!fpText(value.calculation_status)||m.data_scope!==p.data_scope||
        value.value_minor!==null&&!Number.isSafeInteger(value.value_minor)||
        value.value_minor===null&&value.calculation_status==='calculated'||
        value.value_minor!==null&&!['calculated','partial'].includes(value.calculation_status)||
        !fpEqual(slot,{path:'points/'+index+'/value',metric_id:m.metric_id,currency:m.currency,value_minor:m.value_minor}))fail();
      blocks.add(point.block);metricIds.add(m.metric_id);
    });
    const claims=review.body_amount_contract?.amount_claims;
    if(!fpList(claims)||proof.body_amount_occurrence_count!==claims.length||
      proof.total_amount_occurrence_count!==p.points.length+claims.length||
      review.visible_amount_occurrence_count!==proof.total_amount_occurrence_count||
      verified.body_amount_occurrence_count!==claims.length||verified.total_amount_occurrence_count!==proof.total_amount_occurrence_count)fail();
    for(const row of claims){const claim=row?.claim,m=metrics.get(claim?.metric_id);
      if(!m||claim.subject_id!==p.workspace_binding||claim.value_minor!==m.value_minor||claim.currency!==m.currency||
        !Number.isSafeInteger(claim.occurrence_start)||!fpText(claim.text)||
        response.reply.slice(claim.occurrence_start,claim.occurrence_start+claim.text.length)!==claim.text)fail();
    }
    return {presentation:p,metric_batch:batch,verification:verified,proof,reply:response.reply,status:read.status};
  }
  function financialBasis(root,rows){const details=el('details'),list=el('dl',undefined,'calendar-note');details.append(el('summary','计算与来源依据'));
    for(const [label,value] of rows){const dt=el('dt',label),dd=original('dd',typeof value==='string'?value:JSON.stringify(value,null,2));dd.style.whiteSpace='pre-wrap';dd.style.marginInlineStart='0';list.append(dt,dd);}details.append(list);root.append(details);
  }
  function financialDisplay(root,packet){
    const p=packet.presentation;root.replaceChildren();root.dataset.financialViewId=p.view_id;root.dataset.financialProjectionHash=p.projection_hash;
    root.__navrenFinancialPacket=packet;root.append(original('h3',p.title),original('p',(sources[p.scope.source]||p.scope.source)+' · '+p.scope.currency+' · '+p.range.from+' — '+p.range.to,'calendar-identity'));
    notice(root,packet.status==='historical_readonly'?'历史已发布快照，仅供阅读。':'已发布资金快照，仅供阅读。');
    for(const text of new Set([p.scope.funding_query.coverage_scope,...p.important_states]))root.append(original('p',text,'calendar-note'));
    if(p.funding_limits.adequacy==='unknown_without_living_costs_income_stability_and_goals')notice(root,'尚缺生活开支、收入稳定性和目标资料，不能据此认定应急储备充足。');
    if(p.funding_limits.intraday_solvency==='not_assessed_daily_net_only')notice(root,'当前结果未评估日内偿付能力。');
    const grid=el('div',undefined,'calendar-summary');
    for(const point of p.points){
      const m=packet.metric_batch.metrics.find(x=>x.metric_id===point.value.metric_id),card=el('article',undefined,'calendar-metric');card.dataset.financialSlot=point.block;
      const amount=point.value.value_minor===null?el('strong','未提供合计','pnl-missing'):amountNode('strong',point.value.value_minor,point.value.currency,point.coverage);
      amount.dataset.financialAmount=point.value.value_minor===null?'null':String(point.value.value_minor);card.append(original('span',point.label),amount);
      if(point.value.value_minor===null){notice(card,'来源未提供可展示的合计，不能按事项数量推算。');}
      if(point.missing.length)notice(card,point.missing.includes('input_coverage_incomplete')?'输入尚未完整覆盖，具体缺项见来源依据。':'存在未覆盖资料，具体缺项见来源依据。');
      financialBasis(card,[['指标',m.metric_id],['所属账户',m.subject_id],['币种',m.currency],['覆盖范围',m.data_scope],['计算状态',m.calculation_status],['计算口径',m.basis],['方法',m.formula_id],['日期与期间',{as_of:m.as_of,period_start:m.period_start,period_end:m.period_end}],['输入引用',m.input_refs],['来源版本',m.source_versions],['条件与假设',m.assumed_inputs],['排除项',m.excluded_items],['覆盖缺口',m.coverage_gaps],['视图缺项',point.missing]]);
      grid.append(card);
    }root.append(grid);
    financialBasis(root,[['业务日期',p.as_of],['时区',p.timezone],['日期口径',p.clock_basis],['身份与授权版本',{workspace_binding:p.workspace_binding,request_id:p.request_id,message_id:p.message_id,authorization_revision:p.authorization_revision}],['来源范围',p.scope],['来源版本',p.source_versions],['覆盖范围',p.coverage],['缺项',p.missing],['资金规则',p.scope.funding_rule],['资金限制与适用条件',Object.fromEntries(Object.entries(p.funding_limits).filter(([key])=>!key.endsWith('_minor')))],['资金来源引用',p.funding_reference],['图形依据',p.geometry]]);
  }
  async function mountFinancial(root,widget,token){
    financialRoots.add(root);root.classList.add('financial-presentation-widget');root.replaceChildren(el('p','正在核对原已发布资金快照…','calendar-note'));
    try{
      const frozen=JSON.parse(JSON.stringify(widget)),p=frozen.presentation;
      const session=await BankClient.ensure();if(!token.valid())return;
      if(!fpPlain(p)||p.workspace_binding!==session.workspace_id||p.authorization_revision?.privacy!==session.privacy_revision)throw Error('此资金视图属于其他账户或授权版本，快照已隐藏。请重新查询。');
      const responseId=root.closest('.message')?.dataset.bankResponseId;if(responseId&&responseId!==p.message_id)throw Error('资金视图与这条回复不匹配，请重新查询。');
      const language=window.NavrenI18n?.language==='en'?'en':'zh';
      const read=await BankClient.get('/approved-responses/'+encodeURIComponent(p.message_id)+'?language='+language);
      if(!token.valid())return;
      const current=BankClient.identity?.();if(!current||current.workspace_id!==session.workspace_id||current.privacy_revision!==session.privacy_revision)return;
      const packet=financialPacket(frozen,read,current),bubble=root.closest('.bubble');
      if(bubble){let body=bubble.querySelector(':scope > .message-body');if(!body){body=bubble.cloneNode(true);body.querySelectorAll('.calendar-widget').forEach(n=>n.remove());}
        const plain=text=>String(text||'').replace(/\s+/g,''),expected=document.createElement('div'),text=window.NavrenPresentation?.text?.(packet.reply)||packet.reply;
        if(typeof window.renderReplyText==='function')expected.innerHTML=window.renderReplyText(text);else expected.textContent=text;
        if(plain(body.textContent)!==plain(expected.textContent))throw Error('资金视图与回复正文不匹配，请重新查询。');
      }
      financialDisplay(root,packet);
    }catch(e){if(token.valid()){delete root.__navrenFinancialPacket;root.replaceChildren(el('p',e.message||'资金视图已失效或无法核对，请重新查询。','calendar-error'));}}
  }
  async function bound(root,widget,token){
    root.replaceChildren();const loading=notice(root,'正在核对日历所属账户…');
    try{const s=await BankClient.ensure();if(!token.valid()||loading.parentNode!==root)return false;if(widget.workspace_binding!==s.workspace_id){root.replaceChildren(el('p','此日历属于另一个账户，私人快照已隐藏。请重新查询。'));return false;}root.replaceChildren();return true;}catch(e){if(token.valid()&&loading.parentNode===root)root.replaceChildren(el('p','会话不可用，请重新查询日历。'));return false;}
  }
  async function mount(root,widget){
    const token=readToken(root);root.classList.add('calendar-widget');if(widget?.type==='financial_presentation'){await mountFinancial(root,widget,token);return;}if(!await bound(root,widget,token)||!token.valid())return;
    if(widget.type==='calendar_event_draft'){
      try{const status=await CalendarClient.draft(widget.draft.draft_id);if(!token.valid())return;draft(root,widget.draft);
        if(status.status!=='ready'){root.querySelector('.calendar-confirm button')?.remove();notice(root,status.status==='committed'?'此变更已确认保存。':'此草案已失效或未放行，请重新准备。');}
      }catch(e){if(token.valid())err(root,e);}return;
    }
    if(widget.type!=='financial_calendar'||widget.schema_version!==1)return;
    const s=widget.snapshot;if(document.body.dataset.page==='chat')root.append(el('h3','财务日历 · '+widget.range.from+' — '+widget.range.to));
    const identity=el('p',s.portfolio?s.portfolio.identity+' · '+s.portfolio.currency:'当前账户 · '+(sources[widget.source_scope.statement_source]||'账单'),'calendar-identity');
    if(s.nav_asof)identity.append(el('span','净值截至 '+s.nav_asof));if(s.portfolio?.archived)identity.append(el('span',s.portfolio.archive_notice));identity.append(NavrenHelp.button('sources'));root.append(identity);
    const row=(s.summary_by_currency||[])[0]||{};const metrics=el('div',undefined,'calendar-summary');
    for(const [label,value,key]of [['期间投资损益',row.investment_pnl_minor,'equity'],['持仓累计损益',s.holdings_pnl_minor,'equity'],['期间现金净流入',row.recorded_net_minor,'calendar']]){const card=el('article',undefined,'calendar-metric');const title=el('span',label);title.append(NavrenHelp.button(key));const amount=amountNode('strong',value,row.currency||'CNY',row.pnl_coverage,key==='equity');card.append(title,amount);if(value==null)card.append(el('small',row.known_investment_pnl_minor!=null&&label==='期间投资损益'?'已核验部分 '+money(row.known_investment_pnl_minor,row.currency):s.portfolio&&label==='期间现金净流入'?'此独立组合不含银行现金收支':'数据未完整覆盖'));metrics.append(card);}root.append(metrics);
    const maximum=Math.max(1,...(s.days||[]).flatMap(d=>(d.summary_by_currency||[]).map(r=>r.investment_pnl_minor).filter(Number.isSafeInteger).map(Math.abs)));
    const grid=el('div',undefined,'calendar-grid');if(widget.view==='day')grid.classList.add('calendar-one-day');
    if(widget.view!=='day'&&widget.view!=='agenda'){
      ['一','二','三','四','五','六','日'].forEach(x=>grid.append(el('span',x,'calendar-weekday')));
      const pad=(new Date(widget.range.from+'T00:00:00Z').getUTCDay()+6)%7;for(let i=0;i<pad;i++)grid.append(el('span'));
    }
    if(widget.view==='agenda')grid.classList.add('calendar-agenda');
    for(const day of s.days||[]){
      if(widget.view==='agenda'&&!day.item_count)continue;
      const cell=btn('',()=>dayDetail(widget,day.date));cell.classList.add('calendar-day');if(document.body.dataset.page==='account')cell.id='account-calendar-day-'+day.date;cell.setAttribute('aria-label',day.date+'，'+day.item_count+'个事项');cell.append(el('strong',day.date.slice(8)));
      let hasPnl=false;
      for(const row of day.summary_by_currency||[]){
        if(Number.isSafeInteger(row.investment_pnl_minor)){const n=row.investment_pnl_minor;hasPnl=true;const sign=pnlState(n);cell.dataset.pnl=sign;cell.style.setProperty('--pnl-intensity',String(Math.sqrt(Math.abs(n)/maximum)));cell.append(amountNode('span',n,row.currency,row.pnl_coverage,true,'calendar-day-pnl'));}
        else if(row.pnl_coverage==='partial'){hasPnl=true;const missing=el('small','部分缺失','calendar-day-pnl pnl-missing');missing.dataset.calculationStatus='missing';cell.append(missing);}
        if(row.recorded_inflow_minor>0||row.recorded_outflow_minor>0)cell.append(el('small','入 '+money(row.recorded_inflow_minor,row.currency)+' / 出 '+money(row.recorded_outflow_minor,row.currency),'calendar-day-cash'));
      }
      if(!hasPnl){const missing=day.wealth_status==='missing',text=missing?'不可计算':day.wealth_status==='not_disclosed'?'未披露':day.coverage==='unknown'?'数据缺失':'无收益披露',state=missing||day.coverage==='unknown'?'missing':'unknown',amount=el('small',text,'calendar-day-pnl pnl-'+state);amount.dataset.calculationStatus=state;cell.append(amount);}
      if(day.preview?.some(i=>i.interval_days>1))cell.append(el('small','跨日区间','calendar-day-cash'));
      for(const item of day.preview||[])cell.append(el('span',item.title,'calendar-day-title'));
      if(day.item_count>2)cell.append(el('small','另有 '+(day.item_count-2)+' 项'));

      grid.append(cell);
    }root.append(grid);
    const legend=el('p',undefined,'calendar-identity calendar-pnl-legend');for(const [label,state]of [['盈利','positive'],['亏损','negative'],['已计算零值','zero'],['未知','unknown'],['数据缺失','missing']])legend.append(el('span',label,'pnl-'+state));legend.append(NavrenHelp.button('equity'));root.append(legend);
    const controls=el('div',undefined,'calendar-actions');controls.append(btn('重新查询',()=>refresh(widget,root)));
    if(document.body.dataset.page==='chat')controls.append(btn('查看完整日历',()=>{const u=new URL('/account.html',location.href);u.hash='calendar';u.searchParams.set('portfolio',s.portfolio?.id||'current');u.searchParams.set('calendar_on',widget.range.from);u.searchParams.set('source_scope',widget.source_scope.statement_source);u.searchParams.set('time_view',widget.view);location.href=u;}));
    root.append(controls);
  }
  async function refresh(widget,root){const token=readToken(root||document.body);try{const view=await CalendarClient.query({from:widget.range.from,to:widget.range.to,view:widget.view,source:widget.source_scope.statement_source,include_life_events:widget.source_scope.include_life_events,simulation_overlay:widget.source_scope.simulation_overlay,kinds:widget.source_scope.kinds||[],portfolio:widget.source_scope.portfolio||'current',accounting_basis:widget.source_scope.accounting_basis||((widget.source_scope.portfolio||'current')==='current'?'holdings_only_v1':'total_equity_v2')});if(!token.valid())return;const next={type:'financial_calendar',schema_version:1,widget_id:view.view_id,view_id:view.view_id,workspace_binding:view.workspace_binding,view:view.view,range:view.range,source_scope:view.source_scope,generated_at:view.generated_at,snapshot:view};
    if(root){const d=modal('重新查询的日历');mount(d.appendChild(el('section')),next);}else{const d=modal('最新日历');mount(d.appendChild(el('section')),next);}
  }catch(e){if(root&&token.valid())err(root,e);}}
  function draft(root,p,onConfirm){
    const box=el('section',undefined,'calendar-confirm');box.append(el('h3','核对日历变更'));
    notice(box,({create:'创建事件',edit:'修改事件',delete:'删除事件'}[p.action]||p.action)+' · '+({occurrence:'仅这次',future:'这次及以后',series:'整个系列'}[p.scope]));
    function description(event,label){if(!event)return;box.append(el('strong',label+' '+event.title));notice(box,event.start_on+' · '+(kinds[event.kind]||event.kind)+' · '+({once:'单次',weekly:'每周',monthly:'每月',yearly:'每年'}[event.recurrence]));if(event.budget_minor!=null)notice(box,'预算 '+money(event.budget_minor,event.currency));notice(box,'提前 '+event.remind_days+' 天提醒，'+(event.month_policy==='last_day'?'无效日期取当月最后一天':'无效日期跳过该期'));}
    description(p.before,'变更前');description(p.after,'变更后');notice(box,p.notice);const status=el('p');status.role='status';box.append(status);
    const confirm=btn('确认保存日历变更',async()=>{const token=readToken(box);confirm.disabled=true;try{const result=await CalendarClient.confirm(p.draft_id);if(!token.valid())return;window.NavrenI18n?.setText?.(status,'已保存日历变更，没有执行资金操作。')||(status.textContent=t('已保存日历变更，没有执行资金操作。'));confirm.remove();window.dispatchEvent(new Event('navren:calendar-changed'));onConfirm?.(result);}catch(e){if(!token.valid())return;status.textContent=e.message;confirm.disabled=false;if(['CALENDAR_EXPIRED','CALENDAR_STALE','CALENDAR_REVIEW_REQUIRED'].includes(e.code)){confirm.remove();notice(box,'请重新准备草案。');}}});box.append(confirm);root.append(box);
  }
  function form(item=null,action='create'){
    const d=modal(action==='create'?'新建日历事件':'修改或删除日历事件');const f=el('form',undefined,'calendar-form');d.append(f);const fields={};const old=item?.payload||{};
    function field(key,label,type='text',values=null){const l=el('label',label),input=el(values?'select':'input');if(values)for(const [v,t]of values){const o=el('option',t);o.value=v;input.append(o);}else input.type=type;input.name=key;fields[key]=input;l.append(input);f.append(l);return input;}
    if(item)field('scope','变更范围','text',[['occurrence','仅这次'],['future','这次及以后'],['series','整个系列']]);
    if(action!=='delete'){
      field('title','标题').value=old.title||'';field('kind','类型','text',Object.entries(kinds).filter(([k])=>['birthday','life_event','bill','subscription'].includes(k))).value=old.kind||'life_event';
      field('start_on','公历日期','date').value=item?.date||'';field('recurrence','重复','text',[['once','单次'],['weekly','每周'],['monthly','每月'],['yearly','每年']]).value=old.recurrence||'once';
      field('month_policy','不存在的日期','text',[['skip','跳过该期'],['last_day','当月最后一天']]).value=old.month_policy||'skip';
      field('budget','计划预算（留空表示未知）','text').value=old.budget_minor==null?'':(old.budget_minor/100).toFixed(2);
      field('currency','币种','text',['CNY','USD','EUR','GBP','JPY'].map(x=>[x,x])).value=old.currency||'CNY';field('remind_days','提前提醒天数','number').value=old.remind_days||0;field('note','备注').value=old.note||'';
    }
    const submit=btn('生成待确认草案',()=>f.requestSubmit());f.append(submit);
    f.onsubmit=async e=>{e.preventDefault();const token=readToken(d);submit.disabled=true;try{let event;
      if(action!=='delete'){event={title:fields.title.value,kind:fields.kind.value,start_on:fields.start_on.value,recurrence:fields.recurrence.value,month_policy:fields.month_policy.value,currency:fields.currency.value,remind_days:Number(fields.remind_days.value),note:fields.note.value,budget_minor:fields.budget.value.trim()?BankClient.minor(fields.budget.value):null};if(action==='complete')event.status='completed';}
      const p={action:action==='complete'?'edit':action,...(event?{event}:{}),...(item?{series_id:item.series_id,original_on:item.original_on,scope:fields.scope.value}:{})};
      const result=await CalendarClient.prepare(p);if(!token.valid()||!f.isConnected)return;draft(d,result,()=>d.close());submit.remove();
    }catch(error){if(token.valid()&&f.isConnected){err(f,error);submit.disabled=false;}}};
  }
  async function importEvents(){
    const d=modal('导入生活事件 CSV');notice(d,'UTF-8，最多1.5MB和1000个事件；整批预览、核对后保存。流水请使用账单报告的原导入入口。');
    const sample='外部编号,标题,类型,日期,重复周期,月份日期策略,预算金额,币种,提前提醒天数,备注\nBIRTH-MOM,妈妈生日,生日,2026-11-05,每年,跳过该期,500.00,CNY,7,礼物预算\n';
    const download=btn('下载中文模板',()=>{const a=el('a');const url=URL.createObjectURL(new Blob(['\ufeff'+sample],{type:'text/csv;charset=utf-8'}));a.href=url;a.download='Navren生活事件模板.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});d.append(download);
    const input=el('input');input.type='file';input.accept='.csv,text/csv';input.setAttribute('aria-label','选择生活事件CSV');d.append(input);
    input.onchange=async()=>{const file=input.files[0];if(!file)return;const token=readToken(d);try{if(file.size>1572864)throw Error('文件不能超过1.5MB');const text=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());if(!token.valid())return;const p=await CalendarClient.preview(text);if(!token.valid())return;const panel=el('section');d.append(panel);notice(panel,'预览 '+p.rows.length+' 行，错误 '+p.errors.length+' 项。');
      for(const r of p.rows){const detail=el('details');detail.append(el('summary',r.row+' · '+({create:'新增',update:'更新',duplicate:'重复'}[r.action])+' · '+r.event.title));notice(detail,'新内容：'+JSON.stringify(r.event));if(r.before)notice(detail,'原内容：'+JSON.stringify(r.before));panel.append(detail);}for(const e of p.errors)notice(panel,'第 '+e.row+' 行：'+e.message);
      if(p.can_commit){const c=btn('确认整批导入',async()=>{const committed=readToken(panel);c.disabled=true;try{const result=await CalendarClient.commit(p.batch_id);if(!committed.valid())return;notice(panel,'已保存：新增 '+result.counts.create+'，更新 '+result.counts.update+'，重复 '+result.counts.duplicate);c.remove();window.dispatchEvent(new Event('navren:calendar-changed'));}catch(e){if(committed.valid()){err(panel,e);c.disabled=false;}}});panel.append(c);}
    }catch(e){if(token.valid())err(d,e);}};
  }
  async function productDetail(fid,on,portfolio,target=null){
    if(!target&&document.body.dataset.page==='account'&&window.NavrenViews){NavrenViews.open('wealth-product',{object_kind:'historical_product',object_id:fid,date:on,portfolio_id:portfolio,source_scope:'historical'});return;}
    const d=target||modal('产品权益与收益'),token=readToken(d),loading=notice(d,'正在核对权益与行情…');
    try{const r=await BankClient.get('/wealth/product?'+new URLSearchParams({fund_id:fid,on,portfolio}));if(!token.valid()||loading.parentNode!==d)return;loading.remove();d.append(original('h3',r.product.name));notice(d,r.source_identity+' · '+t('净值日')+' '+r.nav_asof);if(r.point.complete===false)notice(d,'账户估值缺失：'+r.point.missing_accounts.join('、')+'；完整产品份额与权益未知。');if(r.portfolio.archived)notice(d,r.portfolio.archive_notice);const fields=el('div',undefined,'calendar-product-grid');
      for(const [label,value]of [['持有份额',r.point.quantity],['单位净值',r.point.unit_nav],['持仓市值',money(r.returns.closing_value_minor,'CNY')],['总权益',money(r.returns.closing_equity_minor,'CNY')],['本次损益',money(r.returns.daily_pnl_minor,'CNY')],['未实现损益',money(r.returns.unrealized_pnl_minor,'CNY')],['累计损益',money(r.returns.cumulative_pnl_minor,'CNY')],['已实现净损益',money(r.returns.realized_pnl_minor,'CNY')],['剩余成本',money(r.point.remaining_cost_minor,'CNY')],['分红应收',money(r.point.closing.dividend_receivable_minor,'CNY')],['赎回应收',money(r.point.closing.redemption_receivable_minor,'CNY')],['待确认申购',money(r.point.closing.pending_subscription_minor,'CNY')],['投资现金',money(r.point.closing.investment_cash_minor,'CNY')],['投资应付款',money(r.point.closing.payable_minor,'CNY')],['本期资本投入',money(r.point.owner_contribution_minor,'CNY')],['本期资本提取',money(r.point.owner_withdrawal_minor,'CNY')]]){const card=el('article');card.append(el('span',label),el('strong',value==null?'未知':String(value)));fields.append(card);}d.append(fields);notice(d,r.returns.equation+'：'+money(r.returns.closing_equity_minor,'CNY')+' − '+money(r.returns.opening_equity_minor,'CNY')+' − '+money(r.returns.owner_net_flow_minor,'CNY'));notice(d,r.point.fee_assumptions);notice(d,'当前微众渠道状态未知，暂不开放新申购。');
      const ns='http://www.w3.org/2000/svg',chart=document.createElementNS(ns,'svg');chart.classList.add('calendar-chart');chart.setAttribute('viewBox','0 0 800 160');chart.setAttribute('role','img');chart.setAttribute('aria-label','历史权益损益趋势');const history=r.history.filter(x=>x.returns.cumulative_pnl_minor!=null),values=history.map(x=>x.returns.cumulative_pnl_minor),low=Math.min(...values,0),high=Math.max(...values,1);const line=document.createElementNS(ns,'polyline');line.setAttribute('points',values.map((v,i)=>(15+i*770/Math.max(1,values.length-1))+','+(140-(v-low)*120/(high-low))).join(' '));line.setAttribute('fill','none');line.setAttribute('stroke','#4b82aa');line.setAttribute('stroke-width','2');chart.append(line);d.append(chart);
      const source=el('details');source.append(el('summary','净值、条款、费用与销售证据'));
      for(const [label,url]of [['产品资料',r.product.detail_url],['基金费率',r.product.fee_rules?.source?.url],['微众历史销售公告',r.product.sales_evidence.url],['管理人风险名录',r.product.risk_evidence?.url],['本次净值',r.point.nav_evidence.url]]){if(!url)continue;const a=el('a',label);a.href=url;a.target='_blank';a.rel='noopener noreferrer';const line=el('p');line.append(a);source.append(line);}notice(source,r.product.fee_rules.text);notice(source,'成本口径 '+r.point.cost_rule+'；组合版本 '+r.portfolio.id);d.append(source);
      const all=el('details');all.append(el('summary','全部历史估值 '+r.history.length+' 条'));for(const row of r.history)all.append(el('p',row.date+' · 净值 '+row.nav+' · 份额 '+(row.quantity??'未知')+(row.complete===false?'（已知 '+row.known_quantity+'，缺 '+row.missing_accounts.join('、')+'）':'')+' · 损益 '+money(row.returns.daily_pnl_minor,'CNY')));d.append(all,btn('查看财富关系',()=>{location.href='/mine.html?'+new URLSearchParams({graph_view:'wealth',portfolio:r.portfolio.id,from:r.nav_asof,to:r.nav_asof})+'#graph';}));
    }catch(e){if(token.valid()&&loading.parentNode===d){loading.remove();err(d,e);}}
  }
  const accountControllers=new WeakMap();
  async function account(root,context={}){
    accountControllers.get(root)?.destroy();root.replaceChildren();
    let current=null,alive=true,initialized=false,epoch=0,section=context.filters?.kind||'overview',anchor=context.date||context.period||null;
    const heading=el('div',undefined,'calendar-heading'),title=el('h2','时间工作区');title.append(NavrenHelp.button('calendar'));const controls=el('div',undefined,'calendar-main-controls');heading.append(title,controls);root.append(heading);
    const monthLabel=btn('选择日期',()=>{dateInput.focus();dateInput.showPicker?.();});monthLabel.classList.add('calendar-date-label');
    const dateInput=el('input');dateInput.type='date';dateInput.setAttribute('aria-label','定位日历日期');
    const view=el('select');view.setAttribute('aria-label','日历时间视图');for(const [v,t]of [['month','月'],['week','周'],['day','日'],['agenda','待办']]){const o=el('option',t);o.value=v;view.append(o);}view.value=['month','week','day','agenda'].includes(context.time_view)?context.time_view:'month';
    const sourceControls=el('div',undefined,'calendar-source-controls'),sourceWrap=el('label','来源'),source=el('select');source.setAttribute('aria-label','日历数据来源');for(const [v,t]of [['simulation','账户记录'],['imported','导入账单']]){const o=el('option',t);o.value=v;source.append(o);}sourceWrap.append(source);
    const portfolioWrap=el('label','组合'),portfolioChoice=el('select');portfolioChoice.setAttribute('aria-label','日历组合');const currentChoice=el('option','当前持仓 · 本金口径');currentChoice.value='current';portfolioChoice.append(currentChoice);portfolioWrap.append(portfolioChoice);sourceControls.append(sourceWrap,portfolioWrap);
    const more=el('details',undefined,'calendar-more');more.append(el('summary','日历工具'),btn('导入事件',importEvents),btn('站内提醒',async()=>{const d=modal('站内提醒'),token=readToken(d);try{const r=await CalendarClient.reminders();if(!token.valid())return;if(!r.items?.length)notice(d,'暂时没有待提醒事项。');for(const item of r.items||[])d.append(original('p',item.item.title+' · '+item.item.date),btn('标为已读',async()=>{await CalendarClient.read(item.id);if(token.valid())d.close();}));}catch(e){if(token.valid())err(d,e);}}));
    const tabs=el('div',undefined,'calendar-type-filter');tabs.setAttribute('aria-label','事项类型');const host=el('section');root.append(sourceControls,tabs,host);const tabButtons={};
    function syncTabs(){for(const [key,b]of Object.entries(tabButtons))b.setAttribute('aria-pressed',String(key===section));}
    const initToken=readToken(root),changed=()=>{if(alive&&initialized)load();},privateReset=()=>{alive=false;epoch++;renderEpochs.set(host,(renderEpochs.get(host)||0)+1);root.replaceChildren(el('p','日历权限已变化，请重新核对。'));};
    const controller={destroy(){alive=false;epoch++;renderEpochs.set(root,(renderEpochs.get(root)||0)+1);renderEpochs.set(host,(renderEpochs.get(host)||0)+1);window.removeEventListener('navren:calendar-changed',changed);window.removeEventListener('navren:privacy-reset',privateReset);}};
    accountControllers.set(root,controller);window.addEventListener('navren:calendar-changed',changed);window.addEventListener('navren:privacy-reset',privateReset);
    const session=await BankClient.ensure();if(!alive||!initToken.valid())return controller;const savedKey='navren_calendar_view:'+session.workspace_id;let saved=null;try{saved=JSON.parse(sessionStorage.getItem(savedKey)||'null');}catch(_){}
    const params=new URL(location.href).searchParams;source.value=context.source_scope==='imported'?'imported':context.source_scope==='simulation'||context.source_scope==='historical'?'simulation':params.get('source_scope')||params.get('source')||saved?.source||'simulation';
    let portfolio=context.portfolio_id||params.get('portfolio')||(context.object_kind==='goal'?'current':saved?.portfolio)||'current';if(!context.filters?.kind&&saved?.section)section=saved.section;anchor=anchor||params.get('calendar_on')||saved?.anchor;if(/^\d{4}-\d{2}$/.test(anchor||''))anchor+='-01';
    const versions=await BankClient.get('/wealth/versions');if(!alive||!initToken.valid())return controller;const historicalVersions=(versions.versions||[]).filter(v=>v.kind==='historical');
    for(const v of historicalVersions){const o=el('option',(v.archived?'归档组合':'历史组合')+' · 真实净值 · '+(v.metadata?.to||v.id));o.value=v.id;portfolioChoice.append(o);}if(portfolio==='historical')portfolio=historicalVersions.find(v=>v.active)?.id||portfolio;
    if(![...portfolioChoice.options].some(o=>o.value===portfolio)){const missing=el('option','组合不可用');missing.value=portfolio;portfolioChoice.append(missing);}portfolioChoice.value=portfolio;
    const latest=()=>historicalVersions.find(v=>v.id===portfolio)?.metadata?.to;
    const reset=btn('回到参考月',()=>load(0,true));
    function updateReset(){const label=portfolio!=='current'?'回到最新披露月':source.value==='simulation'?'回到参考月':'回到本月';if(window.NavrenI18n?.setText)NavrenI18n.setText(reset,label);else reset.textContent=t(label);}
    function save(){const metadata={source_scope:source.value,portfolio_id:portfolio,date:anchor,period:anchor.slice(0,7),time_view:view.value,filters:{kind:section}};sessionStorage.setItem(savedKey,JSON.stringify({section,anchor,portfolio,source:source.value,time_view:view.value}));NavrenViews?.update?.(metadata);}
    function compatible(){if(source.value==='imported'&&portfolio!=='current'){host.replaceChildren(el('div','导入账单不支持历史组合估值。请明确选择「账户记录」来源，或将组合改为「当前持仓」查看导入收支；当前日期与组合没有被自动修改。','calendar-source-unavailable'));return false;}if(portfolio!=='current'&&!historicalVersions.some(v=>v.id===portfolio)){host.replaceChildren(el('p','此组合已不可用，请选择一个可核验的组合。'));return false;}return true;}
    async function load(shift=0,resetAnchor=false){const token=++epoch,identity=BankClient.guard?.(),valid=()=>alive&&root.isConnected&&token===epoch&&(!identity||identity.valid());try{updateReset();if(!compatible()){if(anchor)save();return;}
      if(!current){const initial=await CalendarClient.query({source:source.value,portfolio:'current',accounting_basis:'holdings_only_v1'});if(!valid())return;current=initial;if(!anchor)anchor=portfolio!=='current'?latest()||initial.as_of:initial.as_of;}
      if(resetAnchor)anchor=portfolio!=='current'?latest()||anchor:current.as_of;
      if(!/^\d{4}-\d{2}-\d{2}$/.test(anchor||''))throw Error('日期不可用，请重新选择日历日期。');
      let date=new Date(anchor+'T00:00:00Z');const mode=view.value;
      if(mode==='month'){date.setUTCDate(1);date.setUTCMonth(date.getUTCMonth()+shift);}else date.setUTCDate(date.getUTCDate()+shift*(mode==='day'?1:7));
      if(mode==='week')date.setUTCDate(date.getUTCDate()-(date.getUTCDay()+6)%7);
      const start=date.toISOString().slice(0,10),end=new Date(date);if(mode==='month'){end.setUTCMonth(end.getUTCMonth()+1);end.setUTCDate(0);}else end.setUTCDate(end.getUTCDate()+(mode==='day'?0:mode==='agenda'?30:6));
      const kinds=section==='returns'?['investment_pnl','distribution','settlement']:section==='cashflow'?['transaction']:section==='life'?['birthday','life_event','bill','subscription']:[];
      host.setAttribute('aria-busy','true');const result=await CalendarClient.query({from:start,to:end.toISOString().slice(0,10),view:mode,source:source.value,kinds,portfolio,accounting_basis:portfolio==='current'?'holdings_only_v1':'total_equity_v2'});
      if(!valid())return;if(result.workspace_binding!==session.workspace_id||result.source_scope.statement_source!==source.value||(result.source_scope.portfolio!=='historical'&&result.source_scope.portfolio!==portfolio))throw Error('日历来源已变化，请重新核对。');
      current=result;anchor=start;monthLabel.textContent=start.slice(0,7);dateInput.value=start;save();await mount(host,{type:'financial_calendar',schema_version:1,widget_id:result.view_id,view_id:result.view_id,workspace_binding:result.workspace_binding,view:result.view,range:result.range,source_scope:result.source_scope,generated_at:result.generated_at,snapshot:result});if(alive&&root.isConnected&&token===epoch)host.setAttribute('aria-busy','false');
    }catch(e){if(!valid())return;host.replaceChildren();err(host,e);host.append(btn('重试',()=>load()));host.setAttribute('aria-busy','false');}}
    const previous=btn('‹',()=>load(-1)),next=btn('›',()=>load(1));previous.setAttribute('aria-label','上一期');next.setAttribute('aria-label','下一期');controls.append(previous,monthLabel,next,reset,view,btn('新建事件',()=>form()),more);
    sourceControls.append(dateInput);
    for(const [key,label]of [['overview','全部事项'],['cashflow','收支'],['returns','收益'],['life','生活']]){const b=btn(label,()=>{section=key;syncTabs();load();});tabButtons[key]=b;tabs.append(b);}syncTabs();
    source.onchange=()=>{current=null;load();};portfolioChoice.onchange=()=>{portfolio=portfolioChoice.value;current=null;load();};dateInput.onchange=()=>{anchor=dateInput.value;load();};view.onchange=()=>load();initialized=true;await load();return controller;
  }
  async function renderDay(root,context){const heading=el('div',undefined,'calendar-day-context'),title=el('h2'),scope=el('p');title.append(original('span',context.date+' · '),el('span','当日明细'));scope.append(el('span',context.portfolio_id&&context.portfolio_id!=='current'?'历史组合 · 真实净值':sources[context.source_scope]||'日历来源'),el('span','；'),el('span','该快照不是交易授权'));heading.append(title,scope);if(context.filters?.kind&&context.filters.kind!=='overview'){const filter=el('p');filter.append(el('span','当前筛选事项'),el('span',' · '),el('span',{returns:'收益',cashflow:'收支',life:'生活'}[context.filters.kind]||context.filters.kind),el('span',' → '),el('span','当日全部事项'));heading.append(filter);}root.append(heading);const viewId=context.filters?.calendar_view_id;if(!viewId){notice(root,'这份日历快照已不可用，请返回完整日历重新查询。');return;}await dayDetail({view_id:viewId,source_scope:{statement_source:context.source_scope,portfolio:context.portfolio_id||'current'},snapshot:{},range:{from:context.period||context.date,to:context.date}},context.date,root);}
  async function renderItem(root,id,context){await itemDetail({id,date:context.date},root);}
  async function renderProduct(root,id,on,portfolio){await productDetail(id,on,portfolio,root);}
  function response(message,widgets){const bubble=message.querySelector('.bubble');bubble.querySelectorAll('.calendar-widget').forEach(n=>{financialRoots.delete(n);delete n.__navrenFinancialPacket;n.remove();});for(const widget of widgets||[]){const root=el('section');bubble.append(root);mount(root,widget);}}
  return {mount,response,account,form,productDetail,renderDay,renderItem,renderProduct,destroyAccount(root){accountControllers.get(root)?.destroy();}};
})();

document.addEventListener('DOMContentLoaded',async()=>{
 if(document.body.dataset.page!=='chat')return;
 const question=sessionStorage.getItem('navren_calendar_question');
 if(question){sessionStorage.removeItem('navren_calendar_question');const input=document.getElementById('userInput');input.value=question;}
 // 服务端元数据可恢复组件；本地只保留原可见文本，避免凭浏览器历史授权。
 try{const data=await BankClient.get('/calendar/history');for(const entry of data.entries){
  const message=[...document.querySelectorAll('.message.ai')].find(n=>n.dataset.bankResponseId===entry.response_id);
  if(message&&!message.querySelector('.calendar-widget'))CalendarUI.response(message,entry.widgets);
 }}catch(e){/* 原快照仍可读，点击时会明确报告会话/引用错误。 */}
});
