/** 现行操作账本组件。独立历史资产由账户工作区取数，历史ID不会传入执行流程。 */
let currentMode='classic';
const U=BankUI, B=BankClient;
let accountFormWorkspace=null;
function markAccountFormDirty(event){const form=event.target.closest?.('form');if(form&&document.body.dataset.page==='account'&&!form.closest('#bankReports')&&(form.closest('#accountLegacyViews')||form.closest('#accountSubview')||form.closest('#accountHome'))){form.dataset.navrenDirty='true';if(event.isTrusted)delete form.querySelector('[type=submit]')?.dataset.operationId;}}
document.addEventListener('input',markAccountFormDirty);document.addEventListener('change',markAccountFormDirty);
window.addEventListener('navren:privacy-reset',()=>{accountFormWorkspace=null;document.querySelectorAll('#accountLegacyViews form,#accountSubview form,#accountHome form').forEach(form=>{form.reset();delete form.dataset.navrenDirty;});});
document.addEventListener('DOMContentLoaded',()=>{
  currentMode=getInterfaceMode();
  document.body.className='mode-'+currentMode;
  applyFontSize(getModeFont(currentMode));
  if(U.state())renderAccount(U.state(),U.products());
});
window.addEventListener('bank:ready',event=>renderAccount(event.detail.state,event.detail.products));
window.addEventListener('bank:unavailable',event=>{
  ['holdingsList','bankBudget','bankTransfers','bankOrchestration','bankCards','bankMandates','bankOrders','bankReports'].forEach(id=>{
    const target=document.getElementById(id);if(target)U.showError(target,event.detail,()=>U.load().catch(()=>{}));
  });
});
function renderAccount(s,products) {
  if(s.workspace_id!==B.identity()?.workspace_id)return;
  const formRoots=['bankTransfers','bankOrchestration'];const savedForms=accountFormWorkspace===s.workspace_id?formRoots.flatMap(id=>[...document.getElementById(id).querySelectorAll('form')].map((form,index)=>({id,index,dirty:form.dataset.navrenDirty==='true',operation_id:form.querySelector('[type=submit]')?.dataset.operationId,fields:[...form.querySelectorAll('input,select,textarea')].map(input=>({type:input.type,value:input.value,checked:input.checked}))}))).filter(x=>x.dirty||x.operation_id):[];accountFormWorkspace=s.workspace_id;
  const accounts=U.panel('账户与负债');
  accounts.append(U.table(['账户','已结算现金'],s.accounts.map(a=>[a.name,B.money(a.balance_minor)])));
  accounts.append(U.note('贷款与信用卡负债 '+B.money(s.liabilities_minor)+' · 净资产（本金） '+B.money(s.net_assets_principal_minor)+'。收益及市场估值未模拟。'));
  accounts.append(U.note('业务风险配置 R'+s.profile.risk_level+' · 最长投资期限 '+s.profile.horizon_days+' 天；手动配置不代表正式风险测评。'),U.button('修改业务风险与资金设置',()=>U.editProfile()));
  const budget=U.panel('全户资金安排依据');budget.append(U.node('strong','可安排资金 '+B.money(s.investable_minor)),U.note('已结算现金 '+B.money(s.total_cash_minor)+' · 应急保留 '+B.money(s.profile.reserve_minor)+' · 30日已知到期 '+B.money(s.upcoming_due_minor)+' · 生效预留 '+B.money(s.held_funds_minor)),U.note('此数值由当前工作区资金规则返回；转账或申购时仍须核对实际付款账户。'),U.note(s.notice));
  document.getElementById('bankBudget').replaceChildren(accounts,budget);
  const holdings=document.getElementById('holdingsList');holdings.replaceChildren();
  for(const h of s.holdings.filter(h=>h.amount_minor>0)) {
    const product=products.find(p=>p.id===h.product_id);if(!product)continue;const p=U.panel(product.name);p.dataset.accountObjectKind='holding';p.dataset.accountObjectId=h.account_id+'|'+h.product_id;
    const lots=s.lots.filter(l=>l.account_id===h.account_id&&l.product_id===h.product_id&&l.remaining_minor>0);
    const eligible=lots.filter(l=>product.early_redemption||l.locked_until<=s.as_of).reduce((a,l)=>a+l.remaining_minor,0);
    p.append(U.node('p','持有本金 '+B.money(h.amount_minor)+' · 当前可赎回 '+B.money(eligible)));
    p.append(U.table(['批次','剩余本金','锁定至'],lots.map(l=>[l.id.slice(-8),B.money(l.remaining_minor),l.locked_until])));
    const redeem=U.button('赎回 '+product.name,()=>U.amountForm(product,'redeem',h.account_id,eligible));redeem.disabled=!eligible;
    p.append(redeem,U.note('赎回按已解锁批次处理；到账延迟 '+product.redemption_delay_days+' 个模拟自然日。'));holdings.append(p);
  }
  if(!holdings.children.length)holdings.append(U.note('暂无持仓。'));
  renderTransfers(s);renderOrchestration(s);renderCards(s);renderMandates(s);renderOperations(s,products);for(const saved of savedForms){const form=document.getElementById(saved.id).querySelectorAll('form')[saved.index];if(!form)continue;const inputs=[...form.querySelectorAll('input,select,textarea')];saved.fields.forEach((field,index)=>{const input=inputs[index];if(input&&input.type===field.type&&!['password','file','hidden'].includes(input.type)){input.value=field.value;if(input.type==='checkbox')input.checked=field.checked;input.dispatchEvent(new Event('change'));}});if(saved.dirty)form.dataset.navrenDirty='true';else delete form.dataset.navrenDirty;const trigger=form.querySelector('[type=submit]');if(trigger&&saved.operation_id)trigger.dataset.operationId=saved.operation_id;}window.dispatchEvent(new CustomEvent('navren:account-rendered',{detail:{state:s,products}}));
}

function labeledInput(label,type,value) {
  const wrap=U.node('label',label), input=U.node('input');input.type=type;if(value!==undefined)input.value=value;
  input.autocomplete='off';wrap.append(input);return {wrap,input};
}
function payingAccountSelect(s){const wrap=U.node('label','付款账户'),select=U.node('select');for(const a of s.accounts||[]){const o=U.node('option',a.name+' · 已结算 '+B.money(a.balance_minor));o.value=a.id;select.append(o);}select.value=s.accounts.find(a=>a.id.endsWith('-main'))?.id||s.accounts[0]?.id||'';select.setAttribute('aria-label','付款账户');wrap.append(select);return {wrap,select};}
function recipientSelect(beneficiaries) {
  const wrap=U.node('label','收款人'), select=U.node('select');
  for(const b of beneficiaries) {const option=U.node('option',b.display_name+' · '+b.bank_name+' · '+b.masked_account);option.value=b.id;select.append(option);}
  select.required=true;select.setAttribute('aria-label','收款人');wrap.append(select);return {wrap,select};
}
function submitDraft(form,error,makeDraft) {
  form.onsubmit=e=>{e.preventDefault();try {const trigger=form.querySelector('[type=submit]');U.openDraft(makeDraft(),trigger).then(()=>{if(trigger?.dataset.operationId)delete form.dataset.navrenDirty;}).catch(err=>{error.textContent=err.message;});}
    catch(err){error.textContent=err.message;}};
}

function renderTransfers(s) {
  const root=document.getElementById('bankTransfers');if(!root)return;
  const p=U.panel('转账与AA收款');
  p.append(U.note('所有转账和收款都只写入本机模拟账本。收款人核验、单笔／日累计限额、应急资金和近期到期款会在准备与确认时再次检查。'));
  const beneficiaries=(s.beneficiaries||[]).filter(b=>b.status==='active'&&b.verified);
  if(!beneficiaries.length)p.append(U.note('当前没有可用的已核验模拟收款人。'));
  else {
    const form=U.node('form',undefined,'bank-form'), error=U.node('p','','bank-error'), recipient=recipientSelect(beneficiaries),payer=payingAccountSelect(s);
    const amount=labeledInput('转账金额（元）','text','');amount.input.inputMode='decimal';amount.input.required=true;
    const memo=labeledInput('备注（可选）','text','');
    const submit=U.button('生成转账确认卡',null,true);submit.type='submit';form.append(payer.wrap,recipient.wrap,amount.wrap,memo.wrap,submit);p.append(form,error);
    submitDraft(form,error,()=>{const minor=B.minor(amount.input.value);if(!minor)throw new Error('金额必须大于零。');
      return {action:'transfer',account_id:payer.select.value,
        beneficiary_id:recipient.select.value,amount_minor:minor,reference:memo.input.value.trim(),idempotency_key:B.key()};});

    const schedule=U.node('form',undefined,'bank-form'), scheduleError=U.node('p','','bank-error'), sr=recipientSelect(beneficiaries),scheduledPayer=payingAccountSelect(s);
    const sa=labeledInput('每次金额（元）','text','');sa.input.inputMode='decimal';sa.input.required=true;
    const frequencyWrap=U.node('label','执行频率'), frequency=U.node('select');
    for(const [v,t] of [['once','单次'],['monthly','每月']]){const o=U.node('option',t);o.value=v;frequency.append(o);}frequencyWrap.append(frequency);
    const due=labeledInput('首次执行日期','date',nextDate(s.as_of,1));due.input.required=true;
    const end=labeledInput('结束日期（每月转账可选）','date','');
    frequency.onchange=()=>{end.wrap.hidden=frequency.value!=='monthly';};end.wrap.hidden=true;
    const scheduleButton=U.button('生成定时转账确认卡',null);scheduleButton.type='submit';
    schedule.append(scheduledPayer.wrap,sr.wrap,sa.wrap,frequencyWrap,due.wrap,end.wrap,scheduleButton);p.append(U.note('定时转账在日期推进到期日后才会执行，并留下成功或失败回执。'),schedule,scheduleError);
    submitDraft(schedule,scheduleError,()=>{const minor=B.minor(sa.input.value);if(!minor)throw new Error('金额必须大于零。');
      return {action:'schedule_transfer',account_id:scheduledPayer.select.value,
        beneficiary_id:sr.select.value,amount_minor:minor,frequency:frequency.value,next_due_on:due.input.value,
        ...(end.input.value?{end_on:end.input.value}:{}),idempotency_key:B.key()};});
  }
  const schedules=s.scheduled_payments||[];
  p.append(U.node('h4','定时转账记录'));
  if(!schedules.length)p.append(U.note('暂无定时转账。'));
  else p.append(U.table(['收款人','频率','金额','下次执行','状态','操作'],schedules.map(item=>{
    const b=(s.beneficiaries||[]).find(x=>x.id===item.beneficiary_id),action=U.node('div',undefined,'bank-row');
    if(item.status==='active')action.append(U.button('取消',()=>U.openDraft({action:'cancel_scheduled_payment',
      account_id:item.account_id,scheduled_payment_id:item.id,idempotency_key:B.key()})));
    return [b?.display_name||'模拟收款人',item.frequency==='monthly'?'每月':'单次',B.money(item.amount_minor),item.next_due_on,item.status,action];
  })));
  p.append(U.node('h4','AA分摊收款'));
  for(const split of s.split_bills||[]) {
    const item=U.panel(split.title+' · '+B.money(split.total_minor)+' · '+split.status);
    const people=(s.split_participants||[]).filter(x=>x.split_id===split.id);
    item.append(U.table(['参与人','应收','已收','状态','操作'],people.map(person=>{
      const action=U.node('div',undefined,'bank-row');
      if(split.status==='draft')action.append(U.note('提交整张AA账单后可登记模拟收款'));
      else if(person.status==='requested')action.append(U.button('登记已收款（模拟）',()=>U.openDraft({action:'collect_split_payment',
        account_id:split.account_id,split_id:split.id,participant_id:person.id,idempotency_key:B.key()})));
      return [person.display_name,B.money(person.share_minor),B.money(person.paid_minor),person.status,action];
    })));
    if(split.status==='draft')item.append(U.button('向参与人发起AA请求',()=>U.openDraft({action:'request_split',
      account_id:split.account_id,split_id:split.id,idempotency_key:B.key()}),true));
    item.append(U.note('只记录本机模拟请求／收款，不会给真实联系人发消息或产生真实入账。'));p.append(item);
  }
  root.replaceChildren(p);
}
function nextDate(iso,days) {const d=new Date(iso+'T12:00:00');d.setDate(d.getDate()+days);return d.toISOString().slice(0,10);}

function renderOrchestration(s) {
  const root=document.getElementById('bankOrchestration');if(!root)return;
  const p=U.panel('生活计划与资金安排');
  p.append(U.note('资金预留会占用可用额度但不扣款；预约确认后才扣款并写入流水。供应商、报价、预约和日期推进均为本机模拟。'));
  for(const task of s.orchestration_tasks||[]) {
    const taskPanel=U.panel(task.title+' · '+task.status+' · 预算 '+B.money(task.budget_minor));taskPanel.dataset.accountObjectKind='plan';taskPanel.dataset.accountObjectId=task.id;
    taskPanel.append(U.note('计划日期：'+task.due_on));
    const quotes=(s.service_quotes||[]).filter(q=>q.status==='illustrative');
    const total=quotes.reduce((sum,q)=>sum+q.price_minor,0);
    const relatedHolds=(s.fund_holds||[]).filter(h=>h.task_id===task.id);
    const hold=relatedHolds.find(h=>h.status==='held');
    if(task.status==='draft'&&!hold)taskPanel.append(U.note('计划报价合计 '+B.money(total)+'。'));
    taskPanel.append(U.table(['模拟服务','日期','报价','状态'],quotes.map(q=>[
      q.item,q.valid_until,B.money(q.price_minor),q.status])));
    if(task.status==='draft'&&!hold)taskPanel.append(U.button('核对并预留报价金额',()=>U.openDraft({action:'reserve_funds',
      account_id:s.accounts.find(a=>a.id.endsWith('-main')).id,task_id:task.id,amount_minor:total,
      expires_on:nextDate(task.due_on,-1),idempotency_key:B.key()}),true));
    if(hold) {
      taskPanel.append(U.note('已预留 '+B.money(hold.amount_minor)+'，到期 '+hold.expires_on+'；现金余额尚未扣减。'));
      taskPanel.append(U.button('确认并完成模拟预约',()=>U.openDraft({action:'book_services',
        account_id:hold.account_id,task_id:task.id,hold_id:hold.id,
        quote_ids:quotes.map(q=>q.id),idempotency_key:B.key()}),true));
      taskPanel.append(U.button('解除资金预留',()=>U.openDraft({action:'release_funds',
        account_id:hold.account_id,hold_id:hold.id,idempotency_key:B.key()})));
    }
    const reservations=(s.service_reservations||[]).filter(r=>r.task_id===task.id);
    if(reservations.length)taskPanel.append(U.table(['预约编号','服务日期','金额','状态'],reservations.map(r=>[r.id.slice(0,10),r.delivery_on,B.money(r.amount_minor),r.status])));
    p.append(taskPanel);
  }
  root.replaceChildren(p);
}
function renderCards(s) {
  const root=document.getElementById('bankCards');if(!root)return;
  const p=U.panel('银行卡与交易控制');
  p.append(U.note('此处只操作本机模拟卡片。办卡和提额申请会在模拟日期推进后按演示规则审核，不代表真实银行审核、制卡或授信。'));
  p.append(U.button('申请新卡',()=>U.cardApplicationForm(s.accounts[0]?.id),true));
  for(const card of s.cards||[]) {
    const type=card.type==='credit'?'信用卡':'借记卡';
    const status=card.status==='active'?'正常':card.status==='lost'?'已挂失':'已过期';
    const item=U.panel(type+' · '+card.network+' 尾号 '+card.last_four+' · '+status);item.dataset.accountObjectKind='card';item.dataset.accountObjectId=card.id;
    item.append(U.note('账户：'+(s.accounts.find(a=>a.id===card.account_id)?.name||'模拟账户')));
    if(card.type==='credit') {
      item.append(U.note('模拟信用额度 '+B.money(card.credit_limit_minor)+' · 当前未还 '+B.money(card.outstanding_minor)));
      const open=card.statements.filter(row=>row.status==='open');
      if(open.length)item.append(U.table(['账单期间','应还金额','最低还款','到期日','已还'],open.map(row=>[
        row.period_start+' 至 '+row.period_end,B.money(row.total_due_minor),B.money(row.minimum_due_minor),row.due_on,B.money(row.paid_minor)])));
    }
    const controls=U.panel('交易控制');
    const labels={online:'线上交易',overseas:'境外交易',contactless:'非接触支付'};
    for(const control of card.controls) {
      const row=U.node('div',undefined,'bank-row'),label=labels[control.control_type];
      if(label) {
        row.append(U.note(label+'：'+(control.enabled?'已开启':'已关闭')));
        const toggle=U.button(control.enabled?'关闭':'开启',()=>U.cardControlAction(card,control));
        toggle.disabled=card.status!=='active';row.append(toggle);
      } else if(control.control_type==='single_limit') {
        row.append(U.note('单笔交易限额：'+B.money(control.limit_minor)+'（限额始终开启）'));
        const edit=U.button('调整单笔限额',()=>U.cardSingleLimitForm(card,control));edit.disabled=card.status!=='active';row.append(edit);
      }
      controls.append(row);
    }
    item.append(controls);
    if(card.status==='active') {
      if(card.type==='credit')item.append(U.button('申请提高信用额度',()=>U.cardLimitRequestForm(card)));
      item.append(U.button('挂失此卡',()=>U.reportCardLost(card)));
    } else if(card.status==='lost') {
      item.append(U.note('交易控制已关闭。模拟挂失不可撤销；如需继续用卡，请提交补卡申请。'));
      item.append(U.button('申请补卡',()=>U.cardApplicationForm(card.account_id,card.type,card)));
    }
    p.append(item);
  }
  const applications=s.card_applications||[];
  if(applications.length)p.append(U.table(['申请类型','申请额度','状态','日期','审核说明'],applications.map(row=>[
    row.requested_type==='credit'?'信用卡':'借记卡',B.money(row.requested_limit_minor),
    row.status==='submitted'?'已提交，待模拟审核':row.status==='issued'?'模拟审核通过并生成卡片':row.status==='declined'?'模拟审核未通过':row.status,
    row.requested_on,row.decision_reason])));
  root.replaceChildren(p);
}
function renderMandates(s) {
  const root=document.getElementById('bankMandates'), p=U.panel('银行代扣授权');
  p.append(U.note('银行代扣授权与商户订阅合同分开管理。取消模拟商户合同不会自动关闭银行授权；关闭银行授权也不会取消商户合同。'));
  for(const m of s.mandates) {
    const row=U.node('div',undefined,'bank-row'), text=U.node('div');row.dataset.accountObjectKind='mandate';row.dataset.accountObjectId=m.id;
    text.append(U.node('strong',m.merchant),U.note(B.money(m.amount_minor)+' · '+(m.status==='active'?'授权有效；下次 '+m.next_charge_date:'银行代扣已关闭')));row.append(text);
    const close=U.button(m.status==='active'?'关闭银行代扣':'银行代扣已关闭',()=>U.openDraft({action:'close_mandate',account_id:m.account_id,mandate_id:m.id,idempotency_key:B.key()}));close.disabled=m.status!=='active';row.append(close);
    const subscription=(s.subscriptions||[]).find(x=>x.mandate_id===m.id);
    if(subscription) {
      row.append(U.note('商户订阅：'+subscription.plan_name+' · '+(subscription.merchant_status==='active'?'有效，下次 '+subscription.next_renewal_on:'已在模拟商户侧取消')));
      const cancel=U.button(subscription.merchant_status==='active'?'取消商户订阅':'商户订阅已取消',()=>U.openDraft({action:'cancel_subscription',
        account_id:subscription.account_id,subscription_id:subscription.id,idempotency_key:B.key()}));
      cancel.disabled=subscription.merchant_status!=='active';row.append(cancel);
    }
    p.append(row);
  }
  p.append(U.button('核查周期扣费线索',()=>NavrenViews.open('statements',{source_scope:'simulation',filters:{report_tab:'analysis'}})));
  root.replaceChildren(p);
}
function renderOperations(s,products) {
  const root=document.getElementById('bankOrders'), p=U.panel('订单与操作记录');
  p.append(U.note('包含待确认、取消、失效及已执行状态。待确认不代表已扣款；待结算赎回不代表已到账。'));
  if(!s.operations.length)p.append(U.note('当前账户暂无操作记录。'));
  else p.append(U.table(['操作','对象 / 金额','状态','编号','核验'],[...s.operations].reverse().map(op=>{
    const payload=op.payload;
    const card=(s.cards||[]).find(c=>c.id===payload.card_id);
    const beneficiary=(s.beneficiaries||[]).find(x=>x.id===payload.beneficiary_id);
    const subscription=(s.subscriptions||[]).find(x=>x.id===payload.subscription_id);
    const task=(s.orchestration_tasks||[]).find(x=>x.id===payload.task_id);
    const name=products.find(p=>p.id===payload.product_id)?.name||s.mandates.find(m=>m.id===payload.mandate_id)?.merchant||
      beneficiary?.display_name||subscription?.plan_name||task?.title||
      (card?(card.type==='credit'?'信用卡':'借记卡')+' 尾号 '+card.last_four:payload.requested_type?payload.requested_type==='credit'?'信用卡申请':'借记卡申请':'');
    const view=U.button('查看 '+op.id.slice(0,8),()=>U.openDraft(payload,view));view.dataset.operationId=op.id;
    const amount=payload.amount_minor||payload.requested_limit_minor||payload.limit_minor;
    return [U.actions[op.action]||op.action,name+(amount?' / '+B.money(amount):''),U.operationStatus(op),op.id.slice(0,8),view];
  })));
  if(s.orders.length)p.append(U.table(['订单','产品','方向','金额','状态','结算日'],s.orders.map(o=>[o.id.slice(0,8),o.product_id,U.actions[o.side],B.money(o.amount_minor),U.statuses[o.status]||o.status,o.settlement_date])));
  root.replaceChildren(p);
}
let reportEpoch=0, selectedMonth=null, selectedYear=null, reportSource=null, activePreview=null;
function invalidateAccountReports(){reportEpoch++;activePreview=null;}
window.addEventListener('navren:privacy-reset',invalidateAccountReports);
window.addEventListener('navren:view',e=>{if(e.detail?.page==='account'&&e.detail?.view!=='statements')invalidateAccountReports();});
async function renderReports() {
  const epoch=++reportEpoch, root=document.getElementById('bankReports');if(!root)return;
  const reportMoney=v=>Number.isSafeInteger(v)?B.money(v):'未知';
  const context=window.NavrenViews?.current?.()||{};reportSource=context.source_scope==='imported'?'imported':context.source_scope==='simulation'?'simulation':reportSource;
  selectedMonth=context.period||selectedMonth||U.state()?.as_of?.slice(0,7)||new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'}).slice(0,7);selectedYear=context.filters?.year||selectedYear||selectedMonth.slice(0,4);
  const sourcePanel=U.panel('账单数据来源'), sourceMessage=U.note('正在读取当前来源…');sourcePanel.append(sourceMessage);
  const control=U.panel('账单分析与周期扣费'), form=U.node('form',undefined,'bank-form');
  const ml=U.node('label','月报月份'), month=U.node('input');month.type='month';month.value=selectedMonth;month.required=true;ml.append(month);
  const yl=U.node('label','年度'), year=U.node('input');year.type='number';year.min=1970;year.max=2100;year.value=selectedYear;yl.append(year);
  const submit=U.button('查看报告',null,true);submit.type='submit';form.append(ml,yl,submit);control.append(form);
  const reportTab=context.filters?.report_tab==='analysis'?'analysis':'transactions';const tabs=U.node('div',undefined,'account-report-tabs');tabs.setAttribute('aria-label','流水与分析');for(const [value,label]of [['transactions','流水'],['analysis','分析']]){const b=U.button(label,()=>NavrenViews.open('statements',{source_scope:reportSource,period:selectedMonth,filters:{...context.filters,year:selectedYear,report_tab:value}},{replace:true}));b.setAttribute('aria-pressed',String(value===reportTab));tabs.append(b);}control.append(tabs);
  const reports=U.node('div'), error=U.node('p','','bank-error');error.setAttribute('role','alert');root.replaceChildren(sourcePanel,control,reports,error);
  form.onsubmit=e=>{e.preventDefault();selectedMonth=month.value;selectedYear=year.value;if(window.NavrenViews)NavrenViews.open('statements',{source_scope:reportSource,period:selectedMonth,filters:{...context.filters,year:selectedYear}},{replace:true});else renderReports().catch(()=>{});};
  try {
    const source=await B.get('/source');if(epoch!==reportEpoch)return;
    sourceMessage.textContent=source.notice;
    const sourceLabel=U.node('label','账单统计来源'), selector=U.node('select');
    for(const [value,label] of [['simulation','演示账户'],['imported','导入账单']]) {
      const option=U.node('option',label);option.value=value;selector.append(option);
    }
    selector.value=reportSource||source.active_source;reportSource=selector.value;
    selector.querySelector('option[value="imported"]').disabled=!source.imported_available;
    sourceLabel.append(selector);sourcePanel.append(sourceLabel);
    selector.onchange=()=>{reportSource=selector.value;activePreview=null;if(window.NavrenViews)NavrenViews.open('statements',{source_scope:reportSource,period:selectedMonth,filters:{...context.filters,year:selectedYear}},{replace:true});else renderReports().catch(()=>{});};
    const fileLabel=U.node('label','选择CSV账单'), fileInput=U.node('input');fileInput.type='file';fileInput.accept='.csv,text/csv';fileInput.setAttribute('aria-label','选择CSV账单文件');fileLabel.append(fileInput);
    const template=U.button('下载CSV模板',()=>downloadStatementTemplate());
    const sourceActions=U.node('div',undefined,'bank-row'), previewButton=U.button('校验并预览',async()=>{
      const file=fileInput.files?.[0];if(!file){showToast('请先选择CSV文件。');return;}
      if(file.size>1_572_864){showToast('CSV不能超过1.5MB。');return;}
      try {const guard=B.guard(),started=reportEpoch;const text=await file.text();if(started!==reportEpoch||!guard.valid())return;const preview=await B.postText('/statements/preview',text);if(started!==reportEpoch||!guard.valid(preview))return;activePreview=preview;await renderReports();}
      catch(e){showToast('预览失败：'+e.message);}
    },true);
    sourceActions.append(previewButton,template);sourcePanel.append(fileLabel,sourceActions);
    if(activePreview)renderStatementPreview(sourcePanel,activePreview);
    const sourceVersion=source.sources?.[reportSource]?.source_version;
    if(sourceVersion==null)throw Error('这份来源尚不能核对版本，报告已暂停显示。请更新账户服务后重试。');
    const snapshot=await B.get('/reports/snapshot?'+new URLSearchParams({source:reportSource,source_version:String(sourceVersion),month:selectedMonth,year:selectedYear}));
    if(epoch!==reportEpoch)return;
    const binding=snapshot.source_binding,session=await B.ensure();
    if(epoch!==reportEpoch)return;
    if(!binding||binding.workspace_id!==session.workspace_id||binding.source_scope!==reportSource||String(binding.source_version)!==String(sourceVersion))throw Error('报告来源或版本已变化，请重新读取；没有展示混合来源。');
    const {analysis:monthly,annual_analysis:annual,anomalies:anomaly,periodic_charges:periodic,transactions:rows}=snapshot;
    sourceMessage.textContent=(reportSource==='imported'?'导入账单':'演示账户')+' · 来源版本 '+sourceVersion+' · '+source.notice;
    const reportReference={kind:'report',id:'monthly:'+selectedMonth,source_scope:reportSource,version:sourceVersion,parameters:{month:selectedMonth,year:Number(selectedYear)},label:selectedMonth+' '+(reportSource==='imported'?'导入账单':'账户流水报告')};
    sourcePanel.append(U.button('引用此报告提问',()=>NavrenViews.reference(reportReference)));
    const mp=U.panel(monthly.month+' 月报');
    mp.append(U.node('p','收入 '+reportMoney(monthly.totals_minor.income)+' · 消费 '+reportMoney(monthly.totals_minor.expense)+' · 退款 '+reportMoney(monthly.totals_minor.refund)));
    mp.append(U.note('已记录 '+monthly.transaction_count+' 笔；收支结余 '+reportMoney(monthly.income_expense_surplus_minor)+'。'+
      (monthly.source==='imported_local_csv'?'本地文件无法推算银行余额。':'模拟账本现金净变动 '+reportMoney(monthly.cash_delta_minor)+'。')));
    if(!monthly.transaction_count)mp.append(U.note(reportSource==='imported'?'所选月份没有已导入流水，不代表没有消费；请检查文件覆盖期间。':'没有已记录流水，不代表没有消费。月报读取2026年6月起的活期流水；更早的存档请查看年报。'));
    const monthlyCategories=U.table(['消费分类','金额'],Object.entries(monthly.category_expense_minor).map(([k,v])=>[k,reportMoney(v)]));monthlyCategories.hidden=reportTab!=='analysis';mp.append(monthlyCategories,U.note(monthly.notice));reports.append(mp);
    const ap=U.panel(annual.year+' 年报');ap.hidden=reportTab!=='analysis';
    ap.append(U.node('p','收入 '+reportMoney(annual.totals_minor.income)+' · 消费 '+reportMoney(annual.totals_minor.expense)+' · 退款 '+reportMoney(annual.totals_minor.refund)));
    ap.append(U.note('覆盖 '+annual.covered_months.length+' 个月；'+(annual.complete_calendar_year?'全年完整':'非完整年度')+'。存档 '+annual.archived_count+' 笔，当前流水 '+annual.live_count+' 笔。'));
    ap.append(U.table(['消费分类','金额'],Object.entries(annual.category_expense_minor).map(([k,v])=>[k,reportMoney(v)])),U.note(annual.notice));reports.append(ap);
    const an=U.panel('异常流水（全部现有流水）');an.hidden=reportTab!=='analysis';an.append(U.note(anomaly.notice+' 所有标记均待核实，不代表已确认欺诈。'));
    for(const flag of [...anomaly.duplicates,...anomaly.high_amounts]) {
      const p=U.panel(flag.rule==='high_amount'?'异常大额消费':'5分钟内疑似重复扣款');
      p.append(U.table(['流水编号','时间','商户','金额'],flag.transaction_ids.map(id=>{const r=rows.find(r=>r.id===id);return [id,r?.occurred_at,r?.merchant,r?reportMoney(r.amount_minor):'记录不可用'];})));
      if(flag.baseline_count)p.append(U.note('历史分类基线 '+flag.baseline_count+' 笔，中位数 '+reportMoney(flag.baseline_median_minor)));an.append(p);
    }
    if(!anomaly.duplicates.length&&!anomaly.high_amounts.length)an.append(U.note('规则未发现异常标记。'));reports.append(an);
    const pc=U.panel('疑似周期扣费');pc.hidden=reportTab!=='analysis';pc.append(U.note('按至少三个月的金额与周期识别。结果需核实，固定生活支出不等于商户订阅。'));
    pc.append(U.note('标记只保存本地分析判断，不会关闭银行代扣，也不会取消商户订阅。'));
    pc.append(U.table(['商户','分类','参考金额','历史推算日期','银行代扣状态','本地判断','操作'],periodic.candidates.map(c=>{
      const mandateStatus=!c.mandate_id?'导入账单不含授权信息':U.state()?.mandates.find(m=>m.id===c.mandate_id)?.status==='closed'?'已关闭':'需查看授权';
      const controls=U.node('div',undefined,'bank-row');
      const action=(decision,label)=>{const b=U.button(label,()=>markPeriodicCandidate(c,decision));b.disabled=c.user_decision===decision;controls.append(b);};
      action('confirmed',c.user_decision==='confirmed'?'已确认':'确认扣费');
      action('ignored',c.user_decision==='ignored'?'已忽略':'忽略候选');
      return [c.merchant,c.classification,reportMoney(c.amount_minor),c.expected_next_date,mandateStatus,
        c.user_decision==='confirmed'?'已确认是周期扣费（本地）':c.user_decision==='ignored'?'已忽略（本地）':'待核实',controls];
    })));if(!periodic.candidates.length)pc.append(U.note('当前来源没有符合月度周期规则的候选。'));reports.append(pc);
    const tx=U.panel(selectedMonth+' 月流水明细'+(context.filters?.account_id?' · 指定账户':''));tx.hidden=reportTab!=='transactions';tx.append(U.table(['时间','商户','类型','金额','分类'],rows.filter(r=>r.occurred_at.startsWith(selectedMonth)&&(!context.filters?.account_id||r.account_id===context.filters.account_id)).map(r=>[r.occurred_at,r.merchant,statementKindLabel(r,monthly.source),reportMoney(r.amount_minor),r.category])));reports.append(tx);
  } catch(e) {if(epoch===reportEpoch)error.textContent=e.message;}
}

function statementKindLabel(row,source) {
  const labels={income:'收入',expense:'消费',refund:'退款',investment_purchase:'申购',investment_redemption:'赎回'};
  if(row.kind==='transfer') {
    if(source==='imported_local_csv'||!Number.isFinite(row.delta_minor))return '转账（方向未提供）';
    return row.delta_minor<0?'转出':'转入';
  }
  return labels[row.kind]||row.kind;
}

function downloadStatementTemplate() {
  const content='\ufeff流水号,日期,商户,金额,类型,分类,币种,备注\r\nexample-001,2026-09-01,咖啡店,28.50,支出,餐饮,CNY,可留空\r\n';
  const url=URL.createObjectURL(new Blob([content],{type:'text/csv;charset=utf-8'})), link=U.node('a');
  link.href=url;link.download='Navren账单导入模板.csv';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}

function renderStatementPreview(target,preview) {
  const panel=U.panel('账单导入预览');
  panel.append(U.note(preview.notice));
  panel.append(U.note(preview.valid
    ?'输入 '+preview.input_count+' 笔 · 新增 '+preview.new_count+' 笔 · 重复 '+preview.duplicate_count+' 笔 · 覆盖 '+(preview.date_range?preview.date_range.join(' 至 '):'无')
    :'已校验 '+preview.input_count+' 笔；存在问题，整批无法导入。'));
  if(preview.errors?.length)panel.append(U.table(['行号','问题'],preview.errors.map(e=>[e.row,e.message])));
  if(preview.sample?.length)panel.append(U.table(['流水号','日期','商户','金额','类型','分类'],preview.sample.map(r=>[r.transaction_id,r.occurred_at,r.merchant,B.money(r.amount_minor),r.kind,r.category])));
  if(preview.valid) {
    const checkLabel=U.node('label',undefined,'bank-check'), check=U.node('input');check.type='checkbox';
    checkLabel.append(check,U.node('span','我已检查来源、期间和样例，确认写入独立的本地账单数据集。'));
    const buttons=U.node('div',undefined,'bank-actions'), commit=U.button('确认导入',async()=>{
      if(!check.checked){showToast('请先勾选确认。');return;}
      commit.disabled=true;try {const guard=B.guard(),started=reportEpoch;const result=await B.post('/statements/commit',{batch_id:preview.batch_id,confirmed:true});if(started!==reportEpoch||!guard.valid(result))return;
        reportSource='imported';activePreview=null;showToast('已导入 '+result.inserted_count+' 笔；重复跳过 '+result.skipped_count+' 笔。');await renderReports();
      }catch(e){commit.disabled=false;showToast('导入未完成：'+e.message);}
    },true);
    const cancel=U.button('取消预览',async()=>{try{await B.post('/statements/cancel',{batch_id:preview.batch_id});activePreview=null;await renderReports();}catch(e){showToast('取消失败：'+e.message);}});
    commit.disabled=true;check.onchange=()=>{commit.disabled=!check.checked;};buttons.append(commit,cancel);panel.append(checkLabel,buttons);
  } else panel.append(U.note('这份CSV存在问题或流水冲突，整批已禁用导入。请修正文件后重新预览。'));
  target.append(panel);
}

async function markPeriodicCandidate(candidate,decision) {
  try {
    const result=await B.post('/periodic-charges/'+encodeURIComponent(candidate.candidate_id)+'/mark',{decision,source:reportSource});
    showToast(result.status+'；'+result.notice);
    await renderReports();
  } catch(error) { showToast('本地标记未保存：'+error.message); }
}
