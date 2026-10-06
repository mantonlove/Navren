/** 共享银行界面：产品、资金依据、独立用户确认和可核验回执。 */
window.BankUI = (() => {
  const B = BankClient;
  let state = null, products = [], dialog = null, busy = false;
  const pendingWrites=new Set();
  window.addEventListener('navren:privacy-reset',()=>{state=null;products=[];dialog?.finish?.();});
  const statuses = {pending_confirmation:'待确认，尚未执行',succeeded:'已完成模拟操作',pending_settlement:'赎回待结算，尚未到账',settled:'已结算到账',cancelled:'已取消，未执行',expired:'确认已过期',invalidated:'账户已变化，请重新准备',unknown:'操作状态未知，请勿重复提交'};
  const actions = {purchase:'模拟申购',redeem:'模拟赎回',close_mandate:'关闭银行代扣',card_control:'调整卡片交易控制',report_card_lost:'模拟卡片挂失',request_card_limit:'申请提高信用额度',apply_card:'申请银行卡',transfer:'模拟转账',schedule_transfer:'启用定时转账',cancel_scheduled_payment:'取消定时转账',request_split:'发起AA收款',collect_split_payment:'登记AA模拟收款',cancel_subscription:'取消模拟商户订阅',reserve_funds:'预留任务资金',release_funds:'解除资金预留',book_services:'完成模拟服务预约'};
  const controlNames = {online:'线上交易',overseas:'境外交易',contactless:'非接触支付',single_limit:'单笔限额'};
  function operationStatus(op) {
    if(op.state==='succeeded') {
      if(op.action==='card_control')return '交易控制已更新（模拟）';
      if(op.action==='report_card_lost')return '卡片已挂失（模拟）';
      if(op.action==='request_card_limit')return '额度申请已提交，等待审核';
      if(op.action==='apply_card')return '办卡申请已提交，等待审核';
      if(op.action==='close_mandate')return '银行代扣已关闭（模拟）';
      if(op.action==='transfer')return '转账已记入模拟账本';
      if(op.action==='schedule_transfer')return '定时转账已启用（模拟）';
      if(op.action==='cancel_scheduled_payment')return '定时转账已取消';
      if(op.action==='request_split')return 'AA收款请求已记录（模拟）';
      if(op.action==='collect_split_payment')return 'AA模拟收款已记账';
      if(op.action==='cancel_subscription')return '模拟商户订阅已取消';
      if(op.action==='reserve_funds')return '任务资金已预留（未扣款）';
      if(op.action==='release_funds')return '资金预留已解除';
      if(op.action==='book_services')return '模拟服务预约已完成';
    }
    return statuses[op.state]||op.state;
  }
  function requestManualHandoff(reason) {
    if(typeof window.triggerHandoff==='function') { window.triggerHandoff(reason); return; }
    if(window.NavrenViews){NavrenViews.ask('我需要人工顾问帮助：'+reason).catch(e=>showError(e));return;}
    const url=new URL('/chat.html',location.href);
    url.searchParams.set('mode',document.body.classList.contains('mode-senior')?'senior':
      document.body.classList.contains('mode-youth')?'youth':'classic');
    url.searchParams.set('action','handoff');
    url.searchParams.set('reason',reason);
    location.assign(url.href);
  }
  function node(tag, text, cls) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) {
      const source = ['button','h2','h3','th','option'].includes(tag)?NavrenPresentation.name(text):NavrenPresentation.text(text);
      if (window.NavrenI18n) NavrenI18n.setText(n,source); else n.textContent=source;
    }
    return n;
  }
  function button(text, fn, primary=false) { const n=node('button',text,'bank-button'+(primary?' primary':'')); n.type='button'; n.onclick=fn; return n; }
  function note(text) { return node('p',text,'bank-note'); }
  function errorBox() { const n=node('p','','bank-error'); n.setAttribute('role','alert'); return n; }
  function panel(title) { const n=node('section',undefined,'bank-panel'); if (title) n.append(node('h3',title)); return n; }
  function formatTimelineMoney(minor,currency) {
    if(!Number.isSafeInteger(minor))return '金额不可用';
    const amount=(minor/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
    return currency==='CNY'?'¥'+amount:amount+' '+String(currency||'原币');
  }
  function cashflowEventCard(event) {
    const card=node('article',undefined,'cashflow-event');
    const certainty=event.certainty==='confirmed'?'已确认事项':event.certainty==='candidate'?'周期候选情景':'安排确定性待核实';
    const status={overdue:'日期已过，按当前日期纳入',candidate:'候选日期',scheduled:'计划日期'}[event.date_status]||'未注明';
    const delta=Number.isSafeInteger(event.cash_delta_minor)?event.cash_delta_minor:null;
    const flow=delta==null?'金额未知':delta>0?'预计流入':delta<0?'预计流出':'不改变现金余额';
    card.append(node('strong',event.title||'未命名事项','cashflow-event-title'));
    const labels=note('');labels.append(node('span',certainty,'cashflow-event-certainty'),document.createTextNode(' · '),
      node('span',status,'cashflow-event-date-state'),document.createTextNode(' · '),node('span',flow,'cashflow-event-flow'));
    if(delta!=null)labels.append(document.createTextNode(' '+formatTimelineMoney(Math.abs(delta),event.currency)));
    card.append(labels);
    if(event.reserved_delta_minor)card.append(note('资金预留变化 '+formatTimelineMoney(Math.abs(event.reserved_delta_minor),event.currency)));
    const source=node('details',undefined,'cashflow-source');
    source.append(node('summary','查看来源记录'));
    source.append(node('code',event.evidence_ref||((event.source_table||'模拟档案')+'/'+(event.evidence_id||'未知记录'))));
    card.append(source);
    return card;
  }
  function renderCashflowTimeline(timeline) {
    const root=panel('模拟资金时间轴 · '+timeline.horizon_days+' 天');
    root.classList.add('cashflow-panel');
    const currencies=new Set((timeline.accounts||[]).map(account=>account.currency).filter(Boolean));
    const amountLabel=currencies.size>1?'各账户按原币种展示，不跨币种汇总':currencies.has('CNY')?'金额按元展示':'金额按账户币种展示';
    root.append(note(timeline.as_of+' 至 '+timeline.horizon_end+' · '+amountLabel));
    const accounts=node('div',undefined,'cashflow-account-grid');
    (timeline.accounts||[]).forEach(account=>{
      const card=node('article',undefined,'cashflow-account');
      card.append(node('h4',account.account_name+' · '+account.currency));
      const metrics=node('dl',undefined,'cashflow-metrics');
      const rows=[
        ['当前可用',account.current_available_minor],
        ['期末可用 · 已确认事项',account.projected_available_after_confirmed_minor],
        ['最低可用 · 已确认事项',account.minimum_available_after_confirmed_minor],
        ['最低可用 · 若候选扣费发生',account.minimum_available_if_candidates_occur_minor],
      ];
      rows.forEach(([label,value])=>{metrics.append(node('dt',label),node('dd',formatTimelineMoney(value,account.currency)));});
      card.append(metrics);
      if(account.reserve_floor_minor)card.append(note('应急保留 '+formatTimelineMoney(account.reserve_floor_minor,account.currency)));
      if(account.held_minor)card.append(note('当前生效预留 '+formatTimelineMoney(account.held_minor,account.currency)));
      accounts.append(card);
    });
    root.append(accounts);
    const days=node('div',undefined,'cashflow-days');
    (timeline.timeline||[]).forEach(day=>{
      const disclosure=node('details',undefined,'cashflow-day');
      disclosure.append(node('summary',day.date+' · '+(day.events||[]).length+' 项'));
      (day.events||[]).forEach(event=>disclosure.append(cashflowEventCard(event)));
      days.append(disclosure);
    });
    if(days.children.length){
      root.append(node('h4','按日查看事项','cashflow-subheading'),days);
    }else root.append(note('此时间范围内没有已登记的未来收支事项；这不代表不会发生其他生活支出。'));
    root.append(note(timeline.notice||'仅为模拟推演，不是银行余额或支付承诺。'));
    return root;
  }
  function renderWorkflowEvidence(workflow) {
    const root=panel('本次分析依据');
    root.classList.add('workflow-evidence-panel');
    const trace=workflow.trace||[];
    const reviewEvent=trace.find(item=>item.stage==='reviewer.review');
    const reviewed=reviewEvent?.outcome==='approve';
    root.classList.add(reviewed?'is-reviewed':workflow.status==='blocked'?'is-blocked':'is-pending');
    const statusNames={approved:'独立复核通过',blocked:'安全规则阻断',handoff:'需补充证据',fallback:'主流程失败，已安全降级'};
    const route=trace.find(item=>item.stage==='supervisor.route');
    const synthesis=trace.find(item=>item.stage==='supervisor.synthesize');
    const labels=[];
    if(route?.model_id)labels.push('主模型 '+route.model_id);
    if(synthesis?.model_id)labels.push('综合 '+synthesis.model_id);
    if(reviewEvent?.model_id)labels.push('复核 '+reviewEvent.model_id);
    const summary=[statusNames[workflow.status]||'状态待核验',...labels].join(' · ');
    root.append(node('p',summary,'workflow-review-status'));
    if(!reviewed){
      const reasons={missing_api_key:'模型密钥未配置',provider_http_error:'模型服务返回错误',provider_timeout:'模型请求超时',
        provider_unavailable:'模型服务暂不可用',provider_error:'模型响应结构异常',invalid_model_contract:'模型答复格式无效',response_truncated:'模型答复超出长度限制',contract_invalid:'模型答复未通过合同校验'};
      const reason=workflow.fallback_reason;
      if(reason)root.append(note('未展示资金金额：'+(reasons[reason]||'工作流未完成审核。')));
      else if(workflow.status!=='approved')root.append(note('未展示资金金额：当前结果尚未通过全部审核步骤。'));
    }
    const steps=node('ol',undefined,'workflow-task-list');
    const taskNames={account_snapshot:'读取模拟账户快照',cashflow_projection:'推演已确认事项与候选情景'};
    (workflow.task_graph||[]).forEach(task=>steps.append(node('li',taskNames[task.capability]||task.capability)));
    if(steps.children.length)root.append(node('h4','执行步骤','cashflow-subheading'),steps);
    const evidenceDetails=node('details',undefined,'workflow-source-list');
    evidenceDetails.append(node('summary','查看 '+(workflow.evidence||[]).length+' 项来源引用'));
    const list=node('ul');
    (workflow.evidence||[]).forEach(item=>{
      const source=item.source==='user_memory:user_declared'?'用户手动保存的财务记忆':item.source||'来源未注明';
      const row=node('li');
      row.append(node('span',source),node('code',item.evidence_id||'未提供标识'));
      list.append(row);
    });
    evidenceDetails.append(list);root.append(evidenceDetails);
    return root;
  }
  function table(headers, rows) {
    const wrap=node('div',undefined,'bank-table-wrap'), t=node('table',undefined,'bank-table');
    const head=node('thead'), tr=node('tr'); headers.forEach(v=>tr.append(node('th',v))); head.append(tr); t.append(head);
    const body=node('tbody'); rows.forEach(row=>{const r=node('tr'); row.forEach((v,i)=>{const cell=node('td'); cell.dataset.label=headers[i]; if (v instanceof Node) cell.append(v); else cell.textContent=String(v ?? ''); r.append(cell);}); body.append(r);}); t.append(body); wrap.append(t); return wrap;
  }
  function mainAccount(s=state) { return s.accounts.find(a=>a.id.endsWith('-main')); }
  function budgetPanel(s=state) {
    const p=panel('资金安排依据'),a=s?.accounts?.find(a=>a.id.endsWith('-main')),guard=B.guard();
    p.append(note('正在核对账户规则…'));
    if(!a){p.replaceChildren(note('账户资料缺失，暂不显示可安排金额。'));return p;}
    B.get('/budget?'+new URLSearchParams({account_id:a.id})).then(r=>{
      if(!p.isConnected||!guard.valid())return;
      if(String(r.version)!==String(s.version))throw Error('账户版本已变化，请重新读取后再核对可安排资金。');
      const currency=r.currency||a.currency;
      if(!currency||(!r.currency&&currency!=='CNY'))throw Error('资金币种口径尚未核对，暂不显示可安排金额。');
      p.replaceChildren(node('h3','资金安排依据'),node('p','此账户可安排资金 '+formatTimelineMoney(r.investable_minor,currency),'bank-success bank-usable'));
      p.append(note('已结算现金 '+formatTimelineMoney(r.cash_minor,currency)+' − 应急保留 '+formatTimelineMoney(r.reserve_minor,currency)+' − 已知到期 '+formatTimelineMoney(r.upcoming_due_minor,currency)+' − 生效资金预留 '+formatTimelineMoney(r.held_minor,currency)));
      const details=node('details',undefined,'bank-details');
      details.append(node('summary','查看 '+(r.commitments||[]).length+' 项近期支出'),table(['已知到期事项','金额','日期'],(r.commitments||[]).map(c=>[c.name,formatTimelineMoney(c.amount_minor,currency),c.due_on])));
      p.append(details,note('参考日期 '+r.as_of+' · 账本版本 '+r.version),note(s.notice));
    }).catch(e=>{if(p.isConnected&&guard.valid())showError(p,e,()=>{const replacement=budgetPanel(state);p.replaceWith(replacement);});});
    return p;
  }
  function showError(target,e,retry) { target.replaceChildren(errorBox()); target.firstChild.textContent=e.message; if(retry) target.append(button('重新读取',retry)); }
  async function load() {
    const banner=document.getElementById('bankBanner');
    try {
      const session=await B.ensure();
      [state,products]=await Promise.all([B.get('/overview'),B.get('/products')]);
      banner?.replaceChildren(node('strong','模拟银行'),node('span','业务日期 '+state.as_of),button('刷新',()=>load()));
      if(banner)banner.title='模拟账户 '+session.workspace_id.slice(0,8)+' · 账本版本 '+state.version;
      if (document.getElementById('bankRecommendations')) renderRecommendations();
      window.dispatchEvent(new CustomEvent('bank:ready',{detail:{state,products}}));
      return {state,products};
    } catch(e) { state=null; if (banner) showError(banner,e,load); window.dispatchEvent(new CustomEvent('bank:unavailable',{detail:e})); throw e; }
  }
  function emitChange() {
    load().catch(()=>{});
    try { channel?.postMessage('changed'); } catch(_) {}
  }
  let channel;
  try { channel=new BroadcastChannel('qingzhou-bank-state'); channel.onmessage=()=>load().catch(()=>{}); } catch(_) {}

  function modal(title) {
    if (dialog||busy||document.querySelector('dialog[open]')) return null;
    const d=node('dialog',undefined,'bank-dialog');
    const heading=node('h2',title), head=node('div',undefined,'bank-row');
    heading.id='bankDialogHeading'; d.setAttribute('aria-labelledby',heading.id);
    const close=button('关闭',()=>d.dispatchEvent(new Event('cancel',{cancelable:true}))); close.setAttribute('aria-label','关闭对话框');
    head.append(heading,close); d.append(head);
    d.finish=()=>{d.querySelectorAll('input[type=password]').forEach(n=>n.value='');d.close(); d.remove(); if(dialog===d){dialog=null;busy=false;}};
    d.addEventListener('cancel',e=>{e.preventDefault();if(pendingWrites.size)return;d.finish();});
    document.body.append(d); dialog=d; d.showModal(); return d;
  }
  function field(form,label,type,value) {
    const l=node('label',label), input=node('input'); input.type=type; if (value!==undefined) input.value=value;
    input.autocomplete='off'; l.append(input); form.append(l); return input;
  }
  function check(form,text) { const l=node('label',undefined,'bank-check'), n=node('input'); n.type='checkbox'; n.required=true; l.append(n,node('span',text)); form.append(l); return n; }
  function persist(buttonEl) {
    const card=buttonEl?.closest('[data-bank-response]'), bubble=buttonEl?.closest('.bubble');
    if(!card||!bubble||typeof Storage==='undefined')return;
    const history=Storage.get('qingzhou_chatHistory')||[];
    const item=history.find(h=>h.bank_response_id===card.dataset.bankResponse);
    if(item) {item.htmlContent=bubble.innerHTML; Storage.set('qingzhou_chatHistory',history);}
  }
  function receipt(op,container,trigger,floor=0,readOnly=false) {
    const currentDialog=dialog,guard=B.guard?.();
    const current=()=>container.isConnected&&(!guard||guard.valid())&&
      (!currentDialog||dialog===currentDialog&&(!currentDialog.operationCurrent||currentDialog.operationCurrent()));
    if(!current())return;
    if(dialog)dialog.dataset.operationState=op.state;
    container.replaceChildren(node('p',operationStatus(op),['expired','invalidated'].includes(op.state)?'bank-error':'bank-success'));
    container.append(note('操作编号：'+op.id));
    if(op.result) {
      if(op.result.order_id)container.append(note('订单编号：'+op.result.order_id));
      if(op.result.amount_minor!==undefined)container.append(note('金额：'+B.money(op.result.amount_minor)+
        (op.result.settlement_date?'；预计结算日：'+op.result.settlement_date:'')));
      else if(op.result.total_minor!==undefined)container.append(note('金额：'+B.money(op.result.total_minor)));
      if(op.result.payment_id)container.append(note('模拟付款编号：'+op.result.payment_id));
      if(op.result.scheduled_payment_id)container.append(note('定时转账编号：'+op.result.scheduled_payment_id));
      if(op.result.hold_id)container.append(note('资金预留编号：'+op.result.hold_id));
      if(op.result.reservation_ids?.length)container.append(note('模拟预约编号：'+op.result.reservation_ids.join('、')));
      if(op.result.balance_after_minor!==undefined)container.append(note('执行后付款账户余额：'+B.money(op.result.balance_after_minor)));
      container.append(note(op.result.notice));
    }
    container.append(button('重新查询回执',async()=>{if(!current())return;try {const view=await B.get('/operations/'+op.id);if(!current())return;receipt(view.operation,container,trigger,view.liquidity_floor_minor,readOnly);if(!readOnly)emitChange();}catch(e){if(current())container.append(node('p',e.message,'bank-error'));}}));
    if(op.state==='unknown')container.append(button('生成待人工核查摘要',()=>{
      requestManualHandoff('银行操作状态未知，操作编号：'+op.id);
    }));
    if(['expired','invalidated'].includes(op.state)&&trigger?.dataset.agentOperation==='true') {
      container.append(note('这份 Agent 草案已失效。请返回对话重新提问，重新核验后再确认。'));
    } else if(!readOnly&&['expired','invalidated'].includes(op.state))container.append(button('重新准备确认卡',()=>{
      if(!current())return;
      const draft={...op.payload,idempotency_key:B.key(),liquidity_floor_minor:floor};
      if(trigger){delete trigger.dataset.operationId;if(trigger.dataset.bankDraft)trigger.dataset.bankDraft=JSON.stringify(draft);}
      dialog?.finish();openDraft(draft,trigger).catch(()=>{});
    }));
    if(trigger) {
      trigger.dataset.operationId=op.id;
      trigger.textContent='查看操作状态';
      const status=trigger.parentElement.querySelector('.bank-plan-status');
      if(status)status.textContent=operationStatus(op)+' · '+op.id.slice(0,8);
      persist(trigger);
    }
  }
  async function openReceipt(operationId,trigger,context={}) {
    if(typeof operationId!=='string'||!operationId||context.signal?.aborted||context.isCurrent&&!context.isCurrent())return;
    let guard=B.identity?.()?B.guard?.():null;
    const d=modal('查看操作回执');if(!d)return;
    const current=()=>d.isConnected&&d.open&&dialog===d&&!context.signal?.aborted&&
      (!context.isCurrent||context.isCurrent())&&(!guard||guard.valid());
    const abort=()=>{if(dialog===d)d.finish();};
    d.operationCurrent=current;d.dataset.operationState='loading';
    context.signal?.addEventListener('abort',abort,{once:true});
    const finish=d.finish;d.finish=()=>{context.signal?.removeEventListener('abort',abort);finish();};
    const content=panel();d.append(content);content.append(note('正在查询回执…'));busy=true;
    try {
      await B.ensure();if(!current()){abort();return;}
      guard=guard||B.guard?.();if(!current()){abort();return;}
      const view=await B.get('/operations/'+encodeURIComponent(operationId));
      if(!current()){abort();return;}
      if(view.operation?.id!==operationId)throw Error('操作回执对象不一致，已停止显示。');
      if(context.expected_operation_version!=null&&String(context.expected_operation_version)!==String(view.operation.version))throw Error('操作版本已变化，请重新查询。');
      if(view.operation.state==='pending_confirmation'){
        d.dataset.operationState='pending_confirmation';
        content.replaceChildren(note('这项操作尚未确认执行，没有成功回执。'),note('操作编号：'+operationId));
      }else receipt(view.operation,content,trigger,view.liquidity_floor_minor,true);
    }catch(e){if(current())content.replaceChildren(node('p',e.message,'bank-error'));else abort();}
    finally{if(dialog===d)busy=false;}
  }
  async function openDraft(draft,trigger,context={}) {
    // A component may be cleared while ensure()/GET is pending. Keep its original
    // identity and ticket instead of recapturing authorization from the new view.
    const originalGuard=B.identity?.()?B.guard?.():null;
    if(context.signal?.aborted||context.isCurrent&&!context.isCurrent())return;
    const d=modal('核对银行操作'); if(!d)return;
    let guard=originalGuard;
    const isCurrent=()=>d.isConnected&&d.open&&dialog===d&&
      !context.signal?.aborted&&(!context.isCurrent||context.isCurrent())&&(!guard||guard.valid());
    d.operationCurrent=isCurrent;
    const abort=()=>{if(dialog===d)d.finish();};
    context.signal?.addEventListener('abort',abort,{once:true});
    const finish=d.finish;d.finish=()=>{context.signal?.removeEventListener('abort',abort);finish();};
    d.dataset.operationState='loading';
    const content=panel(), error=errorBox(); d.append(content,error);
    content.append(note('正在读取操作内容，尚未执行…')); busy=true;
    let view;
    try {
      await B.ensure();
      if(!isCurrent()){abort();return;}
      guard=guard||B.guard?.();
      if(!isCurrent()){abort();return;}
      const operationId=draft?.operation_id||trigger?.dataset.operationId;
      view=operationId ? await B.get('/operations/'+operationId) : await B.post('/operations/prepare',draft);
      if(!isCurrent()){abort();return;}
      if(operationId&&view.operation?.id!==operationId)throw Error('操作对象不一致，已停止显示。');
      if(context.expected_operation_version!=null&&String(context.expected_operation_version)!==String(view.operation?.version))throw Error('操作版本已变化，请重新查询。');
      if(trigger){trigger.dataset.operationId=view.operation.id; persist(trigger);}
    } catch(e) {if(!isCurrent()){abort();return;}error.textContent=e.message; content.replaceChildren(note('准备操作失败，没有确认执行。')); busy=false; return;}
    if(dialog===d)busy=false;
    if(!isCurrent()){abort();return;}
    const op=view.operation;
    d.dataset.operationState=op.state;
    if(op.state!=='pending_confirmation') {receipt(op,content,trigger,view.liquidity_floor_minor); return;}
    if(view.agent_review&&view.agent_review.status!=='approved') {
      content.replaceChildren(note('这份 Agent 草案尚未通过独立审核，不能确认执行。请返回对话重新准备。'));
      return;
    }
    content.replaceChildren(node('h3',actions[op.action]));
    content.append(note('环境：模拟银行 · 付款账户：'+view.account.name));
    content.append(node('p',view.product?.name||view.mandate?.merchant||
      (view.card?((view.card.type==='credit'?'信用卡':'借记卡')+' · '+view.card.network+' 尾号 '+view.card.last_four):'')));
    if(op.action==='transfer')content.append(node('p','向 '+(view.beneficiary?.display_name||'模拟收款人')+' 转账 '+B.money(op.payload.amount_minor)),
      note('资金确认后只会记入本机模拟账本，不会向真实收款账户转账。'));
    else if(op.action==='schedule_transfer')content.append(node('p',(view.beneficiary?.display_name||'模拟收款人')+' · '+B.money(op.payload.amount_minor)+' · '+(op.payload.frequency==='monthly'?'每月':'单次')+' · 首次 '+op.payload.next_due_on),
      note('启用后只在模拟日期推进到期日时执行；推进时会重新检查收款人、限额和可用余额。'));
    else if(op.action==='cancel_scheduled_payment')content.append(note('将取消 '+(view.schedule?.next_due_on||'后续')+' 起尚未执行的模拟转账。'));
    else if(op.action==='request_split')content.append(node('p',(view.split?.title||'AA账单')+' · '+B.money(view.split?.total_minor||0)+' · '+(view.split?.participants?.length||0)+' 位参与人'),
      note('确认后只创建本机模拟收款请求，不会向联系人发送消息。'));
    else if(op.action==='collect_split_payment')content.append(node('p','登记 '+(view.participant?.display_name||'参与人')+' 已支付 '+B.money(view.participant?(view.participant.share_minor-view.participant.paid_minor):0)),
      note('确认后只在本机模拟账户记入一笔收款，不代表真实资金到账。'));
    else if(op.action==='cancel_subscription')content.append(node('p','取消 '+(view.subscription?.plan_name||'模拟订阅')),
      note('本次模拟商户取消不会自动关闭银行代扣授权；真实取消结果需以商户确认为准。'));
    else if(op.action==='reserve_funds')content.append(node('p','为 '+(view.task?.title||'模拟任务')+' 预留 '+B.money(op.payload.amount_minor)),
      note('只占用模拟可用额度，不会扣款；预留到期后会自动释放。'));
    else if(op.action==='release_funds')content.append(node('p','解除 '+B.money(view.hold?.amount_minor||0)+' 的模拟资金预留。'));
    else if(op.action==='book_services')content.append(node('p','预约 '+(view.quotes||[]).map(q=>q.item).join('、')+' · 合计 '+formatTimelineMoney(view.amount_minor,'CNY')),
      note('确认后从模拟账户扣款并生成本机预约记录；不会联系真实服务商。'));
    if(view.product) {
      content.append(node('p','金额 '+B.money(op.payload.amount_minor)));
      content.append(note('R'+view.product.risk_level+' · 锁定 '+view.product.term_days+' 天 · 赎回后 '+view.product.redemption_delay_days+' 个模拟自然日到账'));
      content.append(note(view.product.risk_notice));
      content.append(note('费用未模拟（不是实际收费报价）；待结算赎回不计入可用余额。'));
    } else if(view.mandate) content.append(note('仅关闭此模拟银行代扣协议。商户订阅合同及应付款不随之取消。'));
    else if(view.card) {
      if(op.action==='card_control') {
        const kind=op.payload.control_type;
        const current=view.card.controls.find(c=>c.control_type===kind);
        content.append(note(kind==='single_limit'?'将单笔限额调整为 '+B.money(op.payload.limit_minor)+'；当前为 '+B.money(current?.limit_minor||0):
          '将'+controlNames[kind]+'设为'+(op.payload.enabled?'开启':'关闭')+'。'));
      } else if(op.action==='report_card_lost') {
        content.append(note('挂失后该模拟卡的全部交易开关将关闭。此操作不可撤销；真实银行卡状态不会改变。'));
      } else if(op.action==='request_card_limit') {
        content.append(note('当前模拟信用额度 '+B.money(view.card.credit_limit_minor)+'；拟申请 '+B.money(op.payload.requested_limit_minor)+'。提交后额度不变，等待模拟审核。'));
      }
    } else if(view.card_application) {
      content.append(note('拟申请：'+(view.card_application.requested_type==='credit'?'信用卡':'借记卡')+
        (view.card_application.requested_type==='credit'?'；申请额度 '+B.money(view.card_application.requested_limit_minor):'')));
      if(view.card)content.append(note('补卡申请关联已挂失的尾号 '+view.card.last_four+'；审核完成前不会生成新卡。'));
      content.append(note('申请提交后等待模拟审核；不会自动核发卡片或授予信用额度。'));
    }
    if(view.budget)content.append(note('确认前可用额度 '+B.money(view.budget.investable_minor)+'；其中生效资金预留 '+B.money(view.budget.held_minor||0)+'。'));
    content.append(note('操作 '+op.id+' · 5分钟内有效，账户或画像变化后须重新准备。'));
    const form=node('form',undefined,'bank-form'); d.append(form);
    let pin=null;
    if(view.verification_required) {
      pin=field(form,'模拟额外验证码','password'); pin.inputMode='numeric'; pin.pattern='[0-9]{6}'; pin.maxLength=6; pin.required=true; pin.setAttribute('aria-label','模拟额外验证码');
      const code=note('验证码仅在此用户确认界面使用。请勿发送到对话。');
      form.append(button('查看演示验证码',async()=>{if(!isCurrent())return;try {const x=await B.get('/demo-code');if(isCurrent())code.textContent='本次模拟账户验证码：'+x.code+'。仅用于本机演示。';}catch(e){if(isCurrent())error.textContent=e.message;}}),code);
    }
    const consentText=['transfer','schedule_transfer','book_services'].includes(op.action)?'我已核对收款人、金额和模拟资金来源，确认这项操作。':
      ['request_split','collect_split_payment','cancel_subscription','reserve_funds','release_funds','cancel_scheduled_payment'].includes(op.action)?'我已核对任务、对象和模拟状态变更，确认继续。':
      op.action==='card_control'?'我已核对卡片尾号和交易控制，确认更新模拟设置。':
      op.action==='report_card_lost'?'我确认这张尾号卡片已遗失，并确认执行模拟挂失。':
      ['request_card_limit','apply_card'].includes(op.action)?'我已核对申请类型和额度，确认提交模拟申请。':
      '我已核对对象、金额及资金约束，确认执行上述模拟操作。';
    const consent=check(form,consentText);
    const submit=button('确认执行',null,true); submit.type='submit';
    const cancel=button('取消操作',async()=>{
      if(busy||!isCurrent())return; busy=true; cancel.disabled=true;pendingWrites.add(op.id);
      try {
        const cancelled=await B.post('/operations/'+op.id+'/cancel',{});
        if(!isCurrent()){abort();return;}
        form.remove(); error.textContent=''; recovery.replaceChildren(); recovery.hidden=true; stateUnknown=false;
        receipt(cancelled,content,trigger);
      }
      catch(e){if(isCurrent()){error.textContent=e.message+'\n正在核验操作状态…';await queryOperationState();}}
      finally {pendingWrites.delete(op.id);if(dialog===d)busy=false;if(isCurrent()&&!stateUnknown)cancel.disabled=false;}
    });
    const row=node('div',undefined,'bank-actions'); row.append(cancel,submit);form.append(row);
    const recovery=node('div',undefined,'bank-actions'); recovery.hidden=true;
    let stateUnknown=false;
    async function queryOperationState() {
      if(!isCurrent())return;
      try {
        const current=await B.get('/operations/'+op.id), state=current.operation.state;
        if(!isCurrent()){abort();return;}
        d.dataset.operationState=state;
        recovery.replaceChildren(); recovery.hidden=true;
        stateUnknown=false;
        if(['succeeded','pending_settlement','settled'].includes(state)) {
          form.remove(); receipt(current.operation,content,trigger); error.textContent='已重新查询并核实执行回执。'; emitChange();
        } else if(state==='pending_confirmation') {
          error.textContent='已查到该操作仍待确认，当前没有成功回执。可继续确认同一操作，或取消。';
          submit.disabled=cancel.disabled=false;
        } else {
          form.remove(); receipt(current.operation,content,trigger,current.liquidity_floor_minor);
        }
      } catch(_) {
        if(!isCurrent()){abort();return;}
        stateUnknown=true;
        d.dataset.operationState='unknown';
        submit.disabled=cancel.disabled=true;
        error.textContent='目前无法核实此操作状态。请勿重复确认或取消。操作编号：'+op.id;
        const retry=button('重新查询操作状态',async()=>{retry.disabled=true;await queryOperationState();if(stateUnknown)retry.disabled=false;});
        const handoff=button('生成待人工核查摘要',()=>requestManualHandoff('银行操作状态暂无法核实，操作编号：'+op.id));
        recovery.replaceChildren(retry,handoff); recovery.hidden=false;
      }
    }
    form.onsubmit=async e=>{
      e.preventDefault(); if(busy||!isCurrent()||!consent.checked||!form.reportValidity())return;
      busy=true; submit.disabled=cancel.disabled=true; error.textContent='';
      pendingWrites.add(op.id);
      const payload={confirmed:true,confirmation_digest:op.confirmation_digest};
      if(pin)payload.verification_code=pin.value;
      try {
        const result=await B.post('/operations/'+op.id+'/confirm',payload);
        if(!isCurrent()){abort();return;}
        form.remove(); receipt(result.operation,content,trigger); emitChange();
      } catch(e) {
        if(!isCurrent()){abort();return;}
        error.textContent=e.message+'\n尚未取得成功回执，正在核验状态…';
        await queryOperationState();
      } finally {pendingWrites.delete(op.id);if(pin)pin.value='';if(dialog===d)busy=false;if(isCurrent()&&!stateUnknown)submit.disabled=cancel.disabled=false;}
    };
    d.append(recovery);
    // 关闭视图会保留待确认草案，便于重新核验；没有“已取消”的虚假提示。
  }
  async function amountForm(product,action,accountId,maximum) {
    const d=modal((action==='purchase'?'申购':'赎回')+' '+product.name);if(!d)return;
    d.append(note('R'+product.risk_level+' · 锁定'+product.term_days+'天 · 赎回后'+product.redemption_delay_days+'个模拟自然日到账'));
    d.append(note(product.risk_notice));
    if(maximum!==undefined)d.append(note('当前可赎回本金 '+B.money(maximum)));
    const form=node('form',undefined,'bank-form'), error=errorBox();
    const amount=field(form,'金额（元）','text',''); amount.required=true; amount.inputMode='decimal'; amount.setAttribute('aria-label','金额（元）');
    const submit=button('生成确认卡',null,true);submit.type='submit';form.append(submit);d.append(form,error);
    form.onsubmit=e=>{
      e.preventDefault();
      try {const minor=B.minor(amount.value);if(!minor)throw new Error('金额必须大于零。');
        const draft={action,product_id:product.id,account_id:accountId,amount_minor:minor,idempotency_key:B.key()};
        d.finish();openDraft(draft).catch(()=>{});
      }catch(e){error.textContent=e.message;}
    };
    amount.focus();
  }
  function cardControlAction(card,control) {
    return openDraft({action:'card_control',account_id:card.account_id,card_id:card.id,
      control_type:control.control_type,enabled:!control.enabled,idempotency_key:B.key()});
  }
  function cardMoneyDraft(title,description,label,initialValue,makeDraft) {
    const d=modal(title);if(!d)return;
    if(description)d.append(note(description));
    const form=node('form',undefined,'bank-form'), error=errorBox();
    const amount=field(form,label,'text',initialValue);amount.required=true;amount.inputMode='decimal';amount.setAttribute('aria-label',label);
    const submit=button('生成确认卡',null,true);submit.type='submit';form.append(submit);d.append(form,error);
    form.onsubmit=e=>{
      e.preventDefault();
      try {
        const minor=B.minor(amount.value);if(!minor)throw new Error('金额必须大于零。');
        const draft=makeDraft(minor);d.finish();openDraft(draft).catch(()=>{});
      } catch(e) {error.textContent=e.message;}
    };
    amount.focus();
  }
  function cardSingleLimitForm(card,control) {
    const ceiling=card.type==='credit'?card.credit_limit_minor:100000000;
    cardMoneyDraft('调整单笔交易限额',
      '输入新的模拟单笔限额。限额始终启用；信用卡限额不能超过当前信用额度 '+B.money(ceiling)+'。',
      '单笔限额（元）',String(control.limit_minor/100),limit=>{
        if(limit===control.limit_minor)throw new Error('新限额与当前相同，请修改金额后再生成确认卡。');
        if(limit>ceiling)throw new Error('单笔限额不能超过 '+B.money(ceiling)+'。');
        return {action:'card_control',account_id:card.account_id,card_id:card.id,
          control_type:'single_limit',enabled:true,limit_minor:limit,idempotency_key:B.key()};
      });
  }
  function cardLimitRequestForm(card) {
    if(card.type!=='credit')return;
    cardMoneyDraft('申请提高信用额度',
      '当前模拟额度 '+B.money(card.credit_limit_minor)+'。提交后额度不会变化，申请仅进入模拟审核队列。',
      '期望额度（元）','',requested=>{
        if(requested<=card.credit_limit_minor)throw new Error('申请额度必须高于当前额度 '+B.money(card.credit_limit_minor)+'。');
        return {action:'request_card_limit',account_id:card.account_id,card_id:card.id,
          requested_limit_minor:requested,idempotency_key:B.key()};
      });
  }
  function cardApplicationForm(accountId,requestedType='debit',replacementCard=null) {
    if(!accountId)return;
    const d=modal(replacementCard?'申请补发银行卡':'申请银行卡');if(!d)return;
    d.append(note(replacementCard
      ?'此申请关联已挂失卡片尾号 '+replacementCard.last_four+'，只记录本机模拟审核，不会实际制卡。'
      :'选择申请类型。申请仅提交到本机模拟审核队列，不会实际制卡或授予信用额度。'));
    const form=node('form',undefined,'bank-form'), error=errorBox();
    let type;
    if(replacementCard) {
      type={value:replacementCard.type};
      form.append(note('补发类型：'+(replacementCard.type==='credit'?'信用卡':'借记卡')));
    } else {
      const label=node('label','卡片类型'), select=node('select');
      for(const [value,text] of [['debit','借记卡'],['credit','信用卡']]){const option=node('option',text);option.value=value;select.append(option);}
      select.value=requestedType==='credit'?'credit':'debit';select.setAttribute('aria-label','卡片类型');label.append(select);form.append(label);type=select;
    }
    const limit=field(form,'申请信用额度（元）','text','');limit.inputMode='decimal';limit.setAttribute('aria-label','申请信用额度（元）');
    const updateLimitVisibility=()=>{limit.parentElement.style.display=type.value==='credit'?'':'none';limit.required=type.value==='credit';};
    if(!replacementCard)type.addEventListener('change',updateLimitVisibility);
    updateLimitVisibility();
    const submit=button('生成确认卡',null,true);submit.type='submit';form.append(submit);d.append(form,error);
    form.onsubmit=e=>{
      e.preventDefault();
      try {
        const requested=type.value==='credit'?B.minor(limit.value):0;
        if(type.value==='credit'&&!requested)throw new Error('信用额度必须大于零。');
        const draft={action:'apply_card',account_id:accountId,requested_type:type.value,
          requested_limit_minor:requested,idempotency_key:B.key()};
        if(replacementCard)draft.card_id=replacementCard.id;
        d.finish();openDraft(draft).catch(()=>{});
      } catch(e) {error.textContent=e.message;}
    };
    if(type.value==='credit')limit.focus();else if(!replacementCard)type.focus();
  }
  function reportCardLost(card) {
    return openDraft({action:'report_card_lost',account_id:card.account_id,card_id:card.id,idempotency_key:B.key()});
  }
  function editProfile() {
    if(!state)return;
    const d=modal('模拟风险与资金设置');if(!d)return;
    d.append(note('此处用于选择演示账户画像，不替代正式风险测评。更改后，之前的确认卡将失效。'));
    const form=node('form',undefined,'bank-form'), error=errorBox();
    const riskLabel=node('label','模拟风险等级'), risk=node('select');risk.setAttribute('aria-label','模拟风险等级');
    for(let i=1;i<=5;i++){const o=node('option','R'+i);o.value=i;risk.append(o);}risk.value=state.profile.risk_level;riskLabel.append(risk);form.append(riskLabel);
    const horizon=field(form,'最长投资期限（天）','number',state.profile.horizon_days);horizon.min=0;horizon.max=3650;horizon.step=1;horizon.required=true;
    const reserve=field(form,'应急保留（元）','text',String(state.profile.reserve_minor/100));reserve.required=true;
    const consent=check(form,'我确认更新这个模拟账户的风险等级、投资期限与保留金额。');
    const save=button('确认更新',null,true);save.type='submit';form.append(save);d.append(form,error);
    form.onsubmit=async e=>{e.preventDefault();if(busy||!consent.checked||!form.reportValidity())return;
      try {const amount=B.minor(reserve.value);busy=true;save.disabled=true;
        await B.post('/profile',{risk_level:Number(risk.value),horizon_days:Number(horizon.value),reserve_minor:amount,confirmed:true});
        busy=false;d.finish();emitChange();
      }catch(e){error.textContent=e.message;}finally{busy=false;save.disabled=false;}
    };
  }
  function renderRecommendations() {
    const root=document.getElementById('bankRecommendations');if(!root||!state)return;
    const intro=panel('先看资金，再选产品'); intro.classList.add('bank-intro'); intro.append(note('当前模拟画像为 R'+state.profile.risk_level+'，最长投资期限 '+state.profile.horizon_days+' 天。下面先留出应急资金和近期支出，再比较风险与期限。收益未模拟。'));
    intro.append(button('修改模拟风险与资金设置',editProfile));
    root.replaceChildren(intro,budgetPanel());
    const p=panel('产品比较与申购');
    p.append(note('全部为虚构产品；下方产品可接入模拟记账。其他市场研究内容仅供信息参考。'));
    const grid=node('div',undefined,'bank-product-grid');
    for(const product of products) {
      const card=node('article',undefined,'bank-product-card'), status=node('div',undefined,'bank-product-status');
      status.append(node('span','R'+product.risk_level,'bank-tag'),node('span',product.profile_eligible?'风险与期限匹配':'暂不匹配',product.profile_eligible?'matched':''));
      card.append(status,node('h4',product.name));
      const specs=node('dl');
      for(const [label,value] of [['锁定期限',product.term_days+' 天'],['起购金额',B.money(product.min_purchase_minor)],['赎回到账',product.redemption_delay_days+'个模拟自然日']]) specs.append(node('dt',label),node('dd',value));
      card.append(specs,note(product.profile_eligible?'可准备申购，确认前会再次核对可用资金。':product.exclusion_reasons.join(' ')));
      const buy=button('申购 '+product.name,()=>amountForm(product,'purchase',mainAccount().id));buy.disabled=!product.profile_eligible;card.append(buy);grid.append(card);
    }
    p.append(grid);
    p.append(note('风险与期限匹配不代表适合买入；还需通过最低金额、可用资金和执行时状态检查。'));root.append(p);
  }
  function renderResponse(msg,result) {
    const bubble=msg.querySelector('.bubble');if(!bubble||!result.bank)return;
    const box=node('div',undefined,'bank-actions');box.dataset.bankResponse=result.bank_response_id;
    if(Number.isInteger(result.version))box.append(note('查询时账本版本 '+result.version+'；历史对话保留当时数据，实时状态请查看账户。'));
    if(result.cashflow_timeline?.status!=='withheld'&&Array.isArray(result.cashflow_timeline?.timeline))
      box.append(renderCashflowTimeline(result.cashflow_timeline));
    if(result.agent_workflow)box.append(renderWorkflowEvidence(result.agent_workflow));
    if(result.agent_review?.status==='approved')box.append(note('本次解释或操作草案已经独立核验；实际操作仍需你核对并确认。'));
    for(const item of result.bank_actions||[]) {
      const card=node('div'), b=button(item.label,()=>openDraft(item.draft,b));
      b.dataset.bankDraft=JSON.stringify(item.draft); b.dataset.bankWorkspace=result.workspace_id;
      if(item.operation_id){b.dataset.operationId=item.operation_id;b.dataset.agentOperation='true';}
      card.append(b,node('p','待核对，尚未执行','bank-plan-status'));box.append(card);
    }
    box.append(button('查看账户与操作记录',()=>NavrenApp.navigate('account.html', getInterfaceMode())));
    bubble.append(box);
  }
  function rebind(msg) {
    msg.querySelectorAll('[data-bank-draft]').forEach(b=>{b.onclick=async()=>{
      try {const s=await B.ensure();if(s.workspace_id!==b.dataset.bankWorkspace)throw new Error('这张草案属于其他模拟账户，请重新提问。');await openDraft(JSON.parse(b.dataset.bankDraft),b);}
      catch(e){b.parentElement.querySelector('.bank-plan-status').textContent=e.message;}
    };});
    // 历史中的普通跳转按钮也恢复事件。
    msg.querySelectorAll('.bank-actions button:not([data-bank-draft]):not([data-calendar-action])').forEach(b=>{b.onclick=()=>NavrenApp.navigate('account.html', getInterfaceMode());});
  }
  document.addEventListener('DOMContentLoaded',()=>{
    load().catch(()=>{});
    window.addEventListener('focus',()=>{if(!dialog)load().catch(()=>{});});
  });
  return {load,node,button,note,panel,table,budgetPanel,showError,openDraft,openReceipt,amountForm,cardControlAction,
    cardSingleLimitForm,cardLimitRequestForm,cardApplicationForm,reportCardLost,editProfile,renderResponse,rebind,
    state:()=>state,products:()=>products,statuses,actions,operationStatus,
    switchState:()=>({busy:busy||pendingWrites.size>0,unknown:dialog?.dataset.operationState==='unknown',unconfirmed:dialog?.dataset.operationState==='pending_confirmation'})};
})();
