/** 共享银行界面：产品、资金依据、独立用户确认和可核验回执。 */
window.BankUI = (() => {
  const B = BankClient;
  let state = null, products = [], dialog = null, busy = false;
  const pendingWrites=new Set();
  let identityEpoch=0,bankRecovery=B.bankReauthenticationWorkspace?.()?{workspace_id:B.bankReauthenticationWorkspace(),epoch:0}:null;
  for(const event of ['navren:privacy-reset','navren:identity-revoked','navren:account-switched','pagehide'])
    window.addEventListener(event,()=>{identityEpoch++;state=null;products=[];bankRecovery=null;dialog?.finish?.();});
  window.addEventListener('navren:bank-reauthentication-required',event=>{
    if(event.detail?.workspace_id!==B.bankReauthenticationWorkspace?.())return;
    bankRecovery={workspace_id:event.detail.workspace_id,epoch:identityEpoch};state=null;products=[];
    window.dispatchEvent(new Event('navren:bank-session-revoked'));
  });
  const statuses = {pending_confirmation:'待确认，尚未执行',succeeded:'已完成模拟操作',pending_settlement:'赎回待结算，尚未到账',settled:'已结算到账',cancelled:'已取消，未执行',expired:'确认已过期',invalidated:'账户已变化，请重新准备',unknown:'操作状态未知，请勿重复提交'};
  const actions = {purchase:'模拟申购',redeem:'模拟赎回',close_mandate:'关闭银行代扣',card_control:'调整卡片交易控制',report_card_lost:'模拟卡片挂失',unblock_card:'解除模拟挂失',password_change:'修改银行访问密码',request_card_limit:'申请提高信用额度',apply_card:'申请银行卡',transfer:'模拟转账',schedule_transfer:'启用定时转账',cancel_scheduled_payment:'取消定时转账',request_split:'发起AA收款',collect_split_payment:'登记AA模拟收款',cancel_subscription:'取消模拟商户订阅',reserve_funds:'预留任务资金',release_funds:'解除资金预留',book_services:'完成模拟服务预约'};
  const controlNames = {online:'线上交易',overseas:'境外交易',contactless:'非接触支付',single_limit:'单笔限额'};
  function operationStatus(op) {
    if(op.state==='succeeded') {
      if(op.action==='card_control')return '交易控制已更新（模拟）';
      if(op.action==='report_card_lost')return '卡片已挂失（模拟）';
      if(op.action==='unblock_card')return '已解除模拟挂失，交易开关仍关闭';
      if(op.action==='password_change')return '银行访问密码已更新，需重新验证';
      if(op.action==='request_card_limit')return '额度申请已提交，等待审核';
      if(op.action==='apply_card')return op.result?.requested_type==='virtual'&&op.result?.status==='issued'?'SIM 虚拟卡已生成':'办卡申请已提交，等待审核';
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
      if(bankRecovery)throw Object.assign(Error('银行访问会话已失效，请用银行访问密码恢复。'),{code:'BANK_RELOGIN_REQUIRED'});
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
  // 仅供本人原生页面使用；恢复证明不进入金融 operation、聊天或持久状态。
  function financialRecoveryForm() {
    const guard=B.guard?.(),workspace=guard?.workspace_id,epoch=identityEpoch;
    if(!guard?.valid()||typeof workspace!=='string')return;
    const d=modal('恢复操作权限');if(!d)return;
    const purpose='financial_write_unlock',form=node('form',undefined,'bank-form');
    const info=note('正在核对操作权限…'),source=note(''),error=errorBox();
    info.setAttribute('role','status');
    form.append(note('仅解除金融操作限制，不会执行转账、挂失或其他交易。'),info,source);
    const pin=field(form,'短信验证码','password');
    pin.inputMode='numeric';pin.maxLength=6;pin.autocomplete='one-time-code';pin.pattern='[0-9]{6}';
    pin.parentElement.hidden=true;pin.disabled=true;
    const send=button('发送恢复验证码',start,true),verify=button('验证验证码',verifyCode,true);
    const confirm=button('确认恢复操作权限',consume,true),recheck=button('重新核对',readStatus);
    const row=node('div',undefined,'bank-actions');row.append(send,verify,confirm,recheck);form.append(error,row);d.append(form);
    let phase='loading',status=null,challenge=null,proof=null,pending=false,disposed=false,finished=false,sequence=0;
    let expiry=0,watch=null,verificationPayload=null,confirmationPayload=null;
    const finish=d.finish;
    const events=['navren:privacy-reset','navren:identity-revoked','navren:account-switched','navren:view',
      'navren:bank-session-revoked','navren:bank-reauthentication-required','pagehide','beforeunload','popstate','hashchange'];
    function clearProof() {
      if(verificationPayload)delete verificationPayload.verification_code;
      if(confirmationPayload)delete confirmationPayload.recovery_token;
      verificationPayload=confirmationPayload=null;
      challenge=null;proof=null;expiry=0;pin.value='';source.textContent='';
    }
    function dispose() {
      if(disposed)return;disposed=true;sequence++;clearProof();status=null;pending=false;
      clearInterval(watch);for(const event of events)window.removeEventListener(event,leave);
      pin.disabled=send.disabled=verify.disabled=confirm.disabled=recheck.disabled=true;
    }
    d.finish=()=>{if(finished)return;finished=true;dispose();finish();};
    function leave(){d.finish();}
    d.addEventListener('close',dispose,{once:true});
    for(const event of events)window.addEventListener(event,leave);
    function current() {
      if(disposed)return false;
      if(dialog!==d||!d.isConnected||!d.open||epoch!==identityEpoch||!guard.valid()){d.finish();return false;}
      return true;
    }
    function failed(message) {
      sequence++;clearProof();status=null;pending=false;phase='failed';info.textContent='本次恢复未完成。';error.textContent=message;sync();
    }
    function sync() {
      const ready=status?.financial_write_locked===true&&status.recovery_configured===true;
      send.hidden=['proof','confirming','done'].includes(phase);send.disabled=pending||!ready||phase!=='ready';
      verify.hidden=!['code','verifying'].includes(phase);verify.disabled=pending||phase!=='code'||!challenge||!/^[0-9]{6}$/.test(pin.value);
      confirm.hidden=!['proof','confirming'].includes(phase);confirm.disabled=pending||phase!=='proof'||!proof;
      pin.parentElement.hidden=!['code','verifying'].includes(phase);pin.disabled=pending||phase!=='code';
      recheck.hidden=pending||!['failed','unavailable','unlocked'].includes(phase);recheck.disabled=pending;
    }
    function expires(value,ceiling=Date.now()/1000+121) {
      return typeof value==='number'&&Number.isFinite(value)&&value>Date.now()/1000&&value<=ceiling;
    }
    function response(data) {
      return data&&guard.valid(data)&&data.workspace_id===workspace&&data.purpose===purpose&&['provider','test_double'].includes(data.source)&&
        data.real_SMS_acceptance===false&&/^[a-f0-9]{32}$/.test(data.recovery_id)&&
        (data.workspace_id==null||data.workspace_id===workspace);
    }
    function showSource(value){source.textContent=value==='test_double'?'本地替身通道，仅用于工程验证。':'';}
    function rejected(error,confirming=false) {
      const code=error?.code;
      if(confirming)return '恢复结果尚未核对，页面不会自动重试。请重新核对操作权限。';
      if(['NOT_FOUND','SECURITY_RECOVERY_NOT_CONFIGURED','RECOVERY_PROVIDER_UNAVAILABLE'].includes(code))
        return '恢复服务或专用短信渠道尚未就绪，操作限制未改变。请稍后重新核对。';
      if(code==='RECOVERY_RATE_LIMITED')return '发送过于频繁，请稍后重新核对再发起验证。';
      if(code==='RECOVERY_EXPIRED')return '本次验证已过期，操作限制未解除。请重新核对后再发起。';
      return '本次验证未通过或恢复条件已变化，操作限制未解除。请重新核对后再发起。';
    }
    async function readStatus() {
      if(!current()||pending)return;
      clearProof();status=null;error.textContent='';phase='loading';pending=true;info.textContent='正在核对操作权限…';sync();
      const request=++sequence;
      try {
        const data=await B.get('/security/recovery');
        if(!current()||request!==sequence)return;
        if(!guard.valid(data)||data?.contract!=='navren.security-event/1'||data.workspace_id!==workspace||
          typeof data.financial_write_locked!=='boolean'||typeof data.recovery_configured!=='boolean'||
          !Number.isSafeInteger(data.failures)||data.failures<0||!Number.isSafeInteger(data.threshold)||data.threshold<1||
          data.reads_and_original_receipt_recovery_allowed!==true||data.login_or_wait_unlocks!==false)
          throw Error('unbound recovery status');
        status=data;
        phase=!data.financial_write_locked?'unlocked':data.recovery_configured?'ready':'unavailable';
        info.textContent=phase==='unlocked'?'当前没有金融操作限制，无需恢复。':phase==='ready'?
          '金融操作已锁定。请先验证本人手机号，再单独确认恢复。':'金融操作已锁定，恢复短信渠道尚未就绪，当前无法恢复。';
      }catch(_){if(current()&&request===sequence)failed('恢复服务暂不可用，操作权限尚未核对。请稍后重新核对。');}
      finally {if(current()&&request===sequence){pending=false;sync();}}
    }
    async function start() {
      if(!current()||pending||phase!=='ready'||status?.financial_write_locked!==true||status.recovery_configured!==true)return;
      clearProof();error.textContent='';pending=true;phase='sending';info.textContent='正在发送恢复验证码…';sync();
      const request=++sequence;
      try {
        const data=await B.post('/security/recovery/sms',{purpose,confirmed:true});
        if(!current()||request!==sequence)return;
        if(!response(data)||!/^[a-f0-9]{32}$/.test(data.challenge_id)||!expires(data.expires_at))throw Error('invalid recovery challenge');
        challenge={id:data.recovery_id,challenge_id:data.challenge_id,source:data.source,expires_at:data.expires_at};
        expiry=data.expires_at*1000;phase='code';showSource(data.source);
        info.textContent='验证码已发送，请在两分钟内输入并验证。';
      }catch(e){if(current()&&request===sequence)failed(rejected(e));}
      finally {if(current()&&request===sequence){pending=false;sync();}}
    }
    async function verifyCode() {
      if(!current()||pending||phase!=='code'||!challenge||!/^[0-9]{6}$/.test(pin.value))return;
      if(Date.now()>=expiry){failed('本次验证已过期，请重新核对后再发起。');return;}
      const sent=challenge,request=++sequence;
      const payload={purpose,challenge_id:sent.challenge_id,verification_code:pin.value};
      verificationPayload=payload;pin.value='';
      pending=true;phase='verifying';info.textContent='正在验证…';error.textContent='';sync();
      try {
        const verification=B.post('/security/recovery/'+sent.id+'/verify',payload);
        const data=await verification;
        if(!current()||request!==sequence)return;
        if(!response(data)||data.recovery_id!==sent.id||data.source!==sent.source||
          typeof data.recovery_token!=='string'||!/^[A-Za-z0-9_-]{43}$/.test(data.recovery_token)||
          !expires(data.expires_at,sent.expires_at))throw Error('invalid recovery proof');
        proof={token:data.recovery_token};delete data.recovery_token;expiry=data.expires_at*1000;phase='proof';showSource(data.source);
        info.textContent='身份验证已通过，操作限制尚未解除。请确认是否恢复操作权限。';
      }catch(e){if(current()&&request===sequence)failed(rejected(e));}
      finally {delete payload.verification_code;if(verificationPayload===payload)verificationPayload=null;pin.value='';if(current()&&request===sequence){pending=false;sync();}}
    }
    async function consume() {
      if(!current()||pending||phase!=='proof'||!proof||!challenge)return;
      if(Date.now()>=expiry){failed('本次验证已过期，请重新核对后再发起。');return;}
      const sent=challenge,request=++sequence;
      const payload={recovery_id:sent.id,purpose,recovery_token:proof.token,confirmed:true};
      confirmationPayload=payload;proof=null;
      pending=true;phase='confirming';info.textContent='正在核对恢复结果…';error.textContent='';sync();
      try {
        const data=await B.post('/security/recovery/confirm',payload);
        if(!current()||request!==sequence)return;
        if(!response(data)||data.recovery_id!==sent.id||data.source!==sent.source||data.state!=='recovered'||
          data.financial_write_locked!==false||data.failures!==0)throw Error('unconfirmed recovery');
        clearProof();phase='done';showSource(data.source);
        info.textContent='操作权限已恢复，未执行任何交易。需要办理的事项请重新核对并确认。';
      }catch(e){if(current()&&request===sequence)failed(rejected(e,true));}
      finally {delete payload.recovery_token;if(confirmationPayload===payload)confirmationPayload=null;if(current()&&request===sequence){pending=false;sync();}}
    }
    pin.oninput=()=>{if(current())sync();};
    form.onsubmit=e=>e.preventDefault();
    watch=setInterval(()=>{if(current()&&expiry&&Date.now()>=expiry)failed('本次验证已过期，请重新核对后再发起。');},500);
    sync();readStatus();
  }
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
      if(op.result.requested_type==='virtual'&&op.result.status==='issued') {
        if(Number.isSafeInteger(op.result.single_transaction_limit_minor))container.append(note('单笔限额：'+B.money(op.result.single_transaction_limit_minor)));
        if(Number.isSafeInteger(op.result.fee_minor))container.append(note('模拟费用：'+B.money(op.result.fee_minor)));
        if(Number.isSafeInteger(op.result.validity_days))container.append(note('有效期：'+op.result.validity_days+' 天'));
      }
      container.append(note(op.result.notice));
    }
    container.append(button('重新查询回执',async()=>{if(!current())return;try {const view=await B.get('/operations/'+op.id);if(!current())return;receipt(view.operation,container,trigger,view.liquidity_floor_minor,readOnly);if(!readOnly)emitChange();}catch(e){if(current())container.append(node('p',e.message,'bank-error'));}}));
    if(op.state==='unknown')container.append(button('生成待人工核查摘要',()=>{
      requestManualHandoff('银行操作状态未知，操作编号：'+op.id);
    }));
    if(['expired','invalidated'].includes(op.state)&&trigger?.dataset.agentOperation==='true') {
      container.append(note('这份 Agent 草案已失效。请返回对话重新提问，重新核验后再确认。'));
    } else if(!readOnly&&op.action==='password_change'&&['expired','invalidated'].includes(op.state))container.append(button('重新输入银行访问密码',()=>{dialog?.finish();passwordChangeForm(op.payload.account_id).catch(()=>{});}));
    else if(!readOnly&&['expired','invalidated'].includes(op.state))container.append(button('重新准备确认卡',()=>{
      if(!current())return;
      const draft={...op.payload,idempotency_key:B.key(),liquidity_floor_minor:floor};
      if(trigger){delete trigger.dataset.operationId;if(trigger.dataset.bankDraft)trigger.dataset.bankDraft=JSON.stringify(draft);}
      dialog?.finish();openDraft(draft,trigger).catch(()=>{});
    }));
    if(trigger) {
      trigger.dataset.operationId=op.id;
      trigger.dataset.bankOperationState=op.state;
      if(op.version!=null)trigger.dataset.bankOperationVersion=String(op.version);
      trigger.textContent='查看操作状态';
      const card=trigger.closest('.bank-action-card'),status=(card||trigger.parentElement).querySelector('.bank-plan-status');
      if(status)status.textContent=operationStatus(op)+' · '+op.id.slice(0,8);
      const reject=card?.querySelector('[data-bank-reject]');
      if(reject)reject.disabled=op.state!=='pending_confirmation';
      persist(trigger);
    }
  }
  const aaKinds={aa_create:'create',aa_pay:'pay',aa_decline:'decline',aa_cancel:'cancel'};
  async function handoffAA(view,sourceDialog,trigger,context,guard,readOnly=false) {
    const op=view.operation;
    if(!guard?.valid(view)||!aaKinds[op?.action]||view.aa?.kind!==aaKinds[op.action])throw Error('AA操作资料无法核对，请重新读取。');
    if(context.reject===true&&op.version==null)throw Error('AA草案版本无法核对，请重新读取。');
    if(typeof window.NavrenAAPayments?.showOperation!=='function')throw Error('AA核对界面暂不可用，请稍后重新读取。');
    if(!readOnly&&context.reject!==true&&view.agent_review&&view.agent_review.status!=='approved')throw Error('这份AA草案尚未通过审核，不能确认执行。');
    const current=()=>!context.signal?.aborted&&(!context.isCurrent||context.isCurrent())&&guard.valid();
    sourceDialog.finish();
    try {return await NavrenAAPayments.showOperation(view,{
      ...context,trigger,readOnly,
      isCurrent:current,
      onChange:()=>emitChange(),
      onState:operation=>{if(!trigger||!guard.valid()||operation?.id!==op.id)return;trigger.dataset.operationId=operation.id;trigger.dataset.bankOperationState=operation.state;persist(trigger);}
    });}catch(error){
      if(!current())return null;
      const unavailable=modal('AA操作暂不可用');if(!unavailable)return null;
      unavailable.operationCurrent=()=>current()&&unavailable.isConnected&&unavailable.open;
      unavailable.append(node('p',error.message,'bank-error'),note('请重新读取原操作，核对后再继续。'));
      const abort=()=>unavailable.finish(),finish=unavailable.finish;
      unavailable.finish=()=>{context.signal?.removeEventListener('abort',abort);finish();};
      context.signal?.addEventListener('abort',abort,{once:true});
      return null;
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
      if(Object.hasOwn(aaKinds,view.operation.action)){await handoffAA(view,d,trigger,context,guard,true);return;}
      if(view.operation.state==='pending_confirmation'){
        d.dataset.operationState='pending_confirmation';
        content.replaceChildren(note('这项操作尚未确认执行，没有成功回执。'),note('操作编号：'+operationId));
      }else receipt(view.operation,content,trigger,view.liquidity_floor_minor,true);
    }catch(e){if(current())content.replaceChildren(node('p',e.message,'bank-error'));else abort();}
    finally{if(dialog===d)busy=false;}
  }
  // 验证码只留在专用表单和本次请求中；渠道资格取自同一操作的服务端视图。
  function operationVerificationError(error) {
    const code=error?.code,meta=error?.sms_verification,typedMeta=!!meta&&typeof meta==='object'&&!Array.isArray(meta)&&Object.keys(meta).sort().join(',')==='reason_code,same_challenge_retry'&&
      ['SMS_CODE_REJECTED','SMS_CODE_INVALID','SMS_CHALLENGE_INVALID','SMS_EXPIRED','SMS_TIMEOUT','SMS_VERIFY_UNAVAILABLE','FINANCIAL_WRITE_LOCKED'].includes(meta.reason_code)&&typeof meta.same_challenge_retry==='boolean'&&(!meta.same_challenge_retry||meta.reason_code==='SMS_CODE_REJECTED');
    const reasonMessage=code==='SMS_VERIFICATION_FAILED'&&typedMeta?({
      SMS_CODE_REJECTED:meta.same_challenge_retry?'验证码不正确，请修改后重新输入。':'验证码不正确或已失效，请等待冷却结束后重新获取验证码。',
      SMS_CODE_INVALID:'请输入六位短信验证码；如仍无法验证，请重新获取。',
      SMS_CHALLENGE_INVALID:'本次短信验证码已失效，请等待冷却结束后重新获取验证码。',
      SMS_EXPIRED:'本次短信验证码已失效，请等待冷却结束后重新获取验证码。',
      SMS_TIMEOUT:'短信核验超时，请等待冷却结束后重新获取验证码。',
      SMS_VERIFY_UNAVAILABLE:'短信核验暂不可用，请等待冷却结束后重新获取验证码。',
      FINANCIAL_WRITE_LOCKED:'金融安全锁已生效，此操作暂不能执行。'
    })[meta.reason_code]:null;
    return {message:reasonMessage||({SMS_CODE_INVALID:'请输入六位短信验证码。',SMS_VERIFICATION_FAILED:'验证码未通过验证，请检查或重新获取。',SMS_CHALLENGE_INVALID:'本次短信验证码已失效，请等待冷却结束后重新发送。',FINANCIAL_WRITE_LOCKED:'金融安全锁已生效，此操作暂不能执行。'})[code]||error?.message||'操作未完成，请查询原操作状态。',
      retryChallenge:code==='SMS_VERIFICATION_FAILED'&&typedMeta&&meta.reason_code==='SMS_CODE_REJECTED'&&meta.same_challenge_retry===true};
  }
  function operationVerification(form,view,{isCurrent=()=>form.isConnected,isBusy=()=>false,onError=()=>{},onChange=()=>{},onExpiredReturn=null,canExpiredReturn=()=>true}={}) {
    const id=view.operation?.id,permission=view.operation?.permission,summary=view.operation?.confirmation_digest;
    let currentView=view,challenge=null,timer=null,sending=false,disposed=false,resendAt=0,sendState='idle';
    const red=permission==='red'||view.verification_required===true;
    const info=note(''),row=node('div',undefined,'bank-actions');
    info.setAttribute('role','status');info.setAttribute('aria-live','polite');
    let pin=null,region=null;
    const valid=()=>!disposed&&isCurrent();
    const now=()=>Date.now()/1000;
    const operationLive=()=>currentView.operation?.id===id&&currentView.operation?.confirmation_digest===summary&&
      currentView.operation?.version===view.operation?.version&&currentView.operation?.permission===permission&&
      currentView.operation?.state==='pending_confirmation'&&Number.isFinite(currentView.operation?.expires_at)&&now()<currentView.operation.expires_at;
    const expired=()=>currentView.operation?.id===id&&currentView.operation?.confirmation_digest===summary&&
      currentView.operation?.version===view.operation?.version&&currentView.operation?.permission===permission&&
      currentView.operation?.state==='pending_confirmation'&&Number.isFinite(currentView.operation?.expires_at)&&now()>=currentView.operation.expires_at;
    const security=()=>{
      if(!operationLive())return {blocked:true,reason:expired()?'确认卡已过期，请取消旧卡后重新准备。':'操作状态已变化，请重新核对。'};
      const status=currentView.strong_verification;
      if(status&&(status.operation_id!==id||status.grade!==(red?'red':permission)||typeof status.financial_write_locked!=='boolean'))
        return {blocked:true,reason:'操作验证资料尚未核对，此操作暂不能确认。'};
      if(status?.financial_write_locked===true)return {blocked:true,reason:red&&status.channel_configured!==true?'金融安全锁已生效，且短信渠道尚未接通，此操作暂不能确认。':'金融安全锁已生效，此操作暂不能执行。'};
      if(!red)return {blocked:false};
      if(!status||status.operation_id!==id||status.grade!=='red'||status.channel!=='sms')
        return {blocked:true,reason:'短信验证资料尚未核对，此操作暂不能确认。'};
      if(status.channel_configured!==true)return {blocked:true,reason:'短信渠道尚未接通，此操作暂不能确认。'};
      return {blocked:false};
    };
    function clearCode(){if(pin)pin.value='';}
    function clear(){challenge=null;clearTimeout(timer);timer=null;clearCode();}
    const liveChallenge=()=>challenge&&operationLive()&&Number.isFinite(challenge.expires_at)&&now()<challenge.expires_at;
    function dispose(){disposed=true;clear();send.disabled=true;if(pin)pin.disabled=true;onChange();}
    function refresh(){
      clearTimeout(timer);timer=null;
      if(!valid())return;
      if(challenge&&!liveChallenge())clear();
      const status=security(),wait=Math.max(0,Math.ceil(resendAt-now()));
      send.disabled=isBusy()||sending||status.blocked||wait>0;
      send.textContent=wait>0?'重新发送短信验证码（'+wait+'秒）':'发送短信验证码';
      if(pin)pin.disabled=isBusy()||status.blocked||sending;
      if(returnButton){returnButton.hidden=!expired();returnButton.disabled=isBusy()||sending||!valid()||!canExpiredReturn();}
      info.textContent=status.reason?(status.reason+(sending?' 短信请求尚未结束。':isBusy()?' 操作正在处理或状态待核。':'')):(sending?'正在发送短信验证码…':isBusy()?'操作正在处理或状态待核，请稍候。':liveChallenge()?
        (sendState==='rate_limited'?'发送较频繁，上一条验证码仍有效，可继续填写并确认。':'短信已发送，有效至 '+new Date(challenge.expires_at*1000).toLocaleTimeString('zh-CN')+'。'):
        sendState==='rate_limited'?'发送较频繁，请等待重发冷却结束后重试。':
        sendState==='failed'?'短信发送未确认，请等待重发冷却结束后重试。':!red?'':'请发送短信验证码后确认。');
      onChange();
      const deadlines=[resendAt,challenge?.expires_at,currentView.operation?.expires_at].filter(t=>Number.isFinite(t)&&t>now());
      if(deadlines.length)timer=setTimeout(refresh,Math.max(1,Math.min(1000,(Math.min(...deadlines)-now())*1000)));
    }
    const send=button('发送短信验证码',async()=>{
      if(!valid()||isBusy()||sending||security().blocked||now()<resendAt)return;
      sending=true;resendAt=now()+60;refresh();
      try {
        const result=await B.post('/operations/'+encodeURIComponent(id)+'/sms',{});
        if(!valid())return;
        if(result.operation_id!==id||result.channel!=='sms'||result.source!=='provider'||
          typeof result.challenge_id!=='string'||!/^[a-f0-9]{32}$/.test(result.challenge_id)||
          !Number.isFinite(result.expires_at)||result.expires_at<=now()||
          Number.isFinite(view.operation.expires_at)&&result.expires_at>view.operation.expires_at)
          throw Error('短信验证响应无法与当前操作核对，请重新发送。');
        clear();challenge={challenge_id:result.challenge_id,expires_at:result.expires_at};
        sendState='sent';resendAt=now()+60;
      } catch(error){
        if(!valid())return;
        sendState=error?.code==='SMS_SEND_RATE_LIMITED'?'rate_limited':'failed';
        if(sendState!=='rate_limited'||!liveChallenge())clear();
        resendAt=now()+60;onError(error);
      }
      finally{sending=false;if(valid())refresh();}
    });
    const returnButton=typeof onExpiredReturn==='function'?button('取消旧确认卡并返回重新填写',()=>{
      if(!valid()||isBusy()||sending||!expired()||!canExpiredReturn())return;
      return onExpiredReturn();
    }):null;
    if(red){
      region=node('section',undefined,'bank-operation-verification');
      region.append(node('h3','短信验证'));row.append(send,info);if(returnButton)row.append(returnButton);region.append(row);
      pin=field(region,'短信验证码','password');pin.inputMode='numeric';pin.pattern='[0-9]{6}';pin.maxLength=6;pin.required=true;
      pin.autocomplete='one-time-code';pin.setAttribute('aria-label','短信验证码');pin.addEventListener('input',()=>onChange());
      form.prepend(region);
    }else if(security().blocked)form.append(info);
    // refresh 的回调由调用方在完整表单建立后首次触发。
    send.disabled=security().blocked;if(pin)pin.disabled=security().blocked;info.textContent=security().reason||(red?'请发送短信验证码后确认。':'');
    return {
      red,reason:()=>security().reason||'',blocked:()=>security().blocked,isSending:()=>sending,
      canConfirm:()=>!disposed&&!isBusy()&&!sending&&!security().blocked&&(!red||!!liveChallenge()&&/^[0-9]{6}$/.test(pin.value)),
      payload(){if(!this.canConfirm())throw Error(security().reason||'请重新发送短信并填写六位验证码。');return red?{challenge_id:challenge.challenge_id,verification_code:pin.value}:{};},
      update(next,{preserveChallenge=false}={}){
        if(next.operation?.id!==id||next.operation?.confirmation_digest!==summary||next.operation?.version!==view.operation?.version||next.operation?.permission!==permission){dispose();throw Error('操作摘要或版本已变化，请重新准备。');}
        currentView=next;
        if(!preserveChallenge||security().blocked||!liveChallenge())clear();else clearCode();
        if(!challenge)sendState='idle';refresh();return !!liveChallenge()&&!security().blocked;
      },
      mount(host){if(disposed||!host?.isConnected)return;if(region)host.prepend(region);else if(security().blocked)host.append(info);refresh();},
      clear,clearCode,refresh,dispose
    };
  }
  function ordinaryTransferTerms(view) {
    const op=view.operation,terms=view.transfer_terms,payload=op?.payload,account=view.account;
    if(view.environment!=='simulation'||op?.action!=='transfer'||!payload||Object.hasOwn(payload,'remittance')||
      !terms||terms.terms_version!=='navren.sim-beneficiary-transfer.zero-fee/1'||terms.environment!=='simulation'||
      terms.scope!=='ordinary_beneficiary_transfer'||terms.operation_id!==op.id||terms.operation_version!==op.version||
      !Number.isSafeInteger(op.version)||terms.workspace_id!==account?.workspace_id||
      terms.account_id!==payload.account_id||terms.account_id!==account?.id||terms.beneficiary_id!==payload.beneficiary_id||
      !Number.isSafeInteger(payload.amount_minor)||payload.amount_minor<=0||terms.amount_minor!==payload.amount_minor||
      terms.currency!=='CNY'||account?.currency!=='CNY'||terms.fee_minor!==0)return null;
    return terms;
  }
  function virtualCardTerms(view) {
    const application=view.card_application,terms=application?.virtual_terms,payload=view.operation?.payload;
    if(view.environment!=='simulation'||application?.requested_type!=='virtual'||payload?.requested_type!=='virtual'||
      application.requested_limit_minor!==payload.requested_limit_minor||!Number.isSafeInteger(payload.requested_limit_minor)||payload.requested_limit_minor<=0||
      !terms||terms.terms_version!=='navren.virtual-card.demo/1'||!['online_payment','subscription','travel'].includes(terms.purpose)||
      terms.purpose!==(payload.purpose||'online_payment')||terms.single_transaction_limit_minor!==payload.requested_limit_minor||!Number.isSafeInteger(terms.fee_minor)||terms.fee_minor<0||
      !Number.isSafeInteger(terms.validity_days)||terms.validity_days<1||terms.credit_limit_minor!==0||
      terms.real_payment_capability!==false)return null;
    return terms;
  }
  async function openDraft(draft,trigger,context={}) {
    // A component may be cleared while ensure()/GET is pending. Keep its original
    // identity and ticket instead of recapturing authorization from the new view.
    if(bankRecovery)return passwordSessionForm(bankRecovery.workspace_id);
    const originalGuard=B.identity?.()?B.guard?.():null;
    if(context.signal?.aborted||context.isCurrent&&!context.isCurrent())return;
    const d=modal(context.reject===true?'拒绝银行草案':'核对银行操作'); if(!d)return;
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
      if(context.reject===true&&!operationId)throw Error('草案没有操作编号，不能取消。请重新核对。');
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
    if(Object.hasOwn(aaKinds,op.action)){
      try{await handoffAA(view,d,trigger,context,guard);}catch(error){if(isCurrent())content.replaceChildren(node('p',error.message,'bank-error'));}
      return;
    }
    if(op.state!=='pending_confirmation') {receipt(op,content,trigger,view.liquidity_floor_minor); return;}
    if(context.reject===true&&op.version==null){content.replaceChildren(note('草案版本无法核对，尚未取消。请重新查询操作状态。'));return;}
    if(context.reject!==true&&view.agent_review&&view.agent_review.status!=='approved') {
      content.replaceChildren(note('这份 Agent 草案尚未通过独立审核，不能确认执行。请返回对话重新准备。'));
      return;
    }
    content.replaceChildren(node('h3',actions[op.action]));
    content.append(note('环境：模拟银行 · 付款账户：'+view.account.name));
    content.append(node('p',view.product?.name||view.mandate?.merchant||
      (view.card?((view.card.type==='virtual'?'SIM 虚拟卡':view.card.type==='credit'?'信用卡':'借记卡')+' · '+view.card.network+' 尾号 '+view.card.last_four):'')));
    if(op.action==='transfer'){
      content.append(node('p','向 '+(view.beneficiary?.display_name||'模拟收款人')+' 转账 '+B.money(op.payload.amount_minor)),
        note('资金确认后只会记入本机模拟账本，不会向真实收款账户转账。'));
      if(!Object.hasOwn(op.payload,'remittance')){
        const terms=ordinaryTransferTerms(view);
        content.append(note(terms?'本机模拟手续费：'+B.money(terms.fee_minor)+'；仅限当前普通本机SIM收款人转账。':'本机模拟手续费条款尚未核对，请重新准备。'));
      }
    }
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
        content.append(note('挂失后该模拟卡的全部交易开关将关闭；符合条件时可另行确认解挂，真实银行卡状态不会改变。'));
      } else if(op.action==='unblock_card') {
        content.append(note('仅解除本账户这张未过期卡片的模拟挂失。交易开关继续关闭，开启交易须另行确认。'));
      } else if(op.action==='request_card_limit') {
        content.append(note('当前模拟信用额度 '+B.money(view.card.credit_limit_minor)+'；拟申请 '+B.money(op.payload.requested_limit_minor)+'。提交后额度不变，等待模拟审核。'));
      }
    } else if(view.card_application) {
      content.append(note('拟申请：'+(view.card_application.requested_type==='virtual'?'SIM 虚拟卡':view.card_application.requested_type==='credit'?'信用卡':'借记卡')+
        (view.card_application.requested_type==='credit'?'；申请额度 '+B.money(view.card_application.requested_limit_minor):'')));
      if(view.card_application.requested_type==='virtual') {
        const terms=virtualCardTerms(view);
        content.append(note('单笔限额 '+B.money(view.card_application.requested_limit_minor)+'；仅本机模拟使用，无真实卡号、授信或对外支付能力。'));
        content.append(note(terms?'用途：'+({online_payment:'线上支付',subscription:'订阅',travel:'旅行'})[terms.purpose]+' · 模拟费用 '+B.money(terms.fee_minor)+' · 有效期 '+terms.validity_days+' 天':'费用与有效期条款尚未核对，暂不能确认生成虚拟卡。'));
      }else content.append(note('申请提交后等待模拟审核；不会自动核发卡片或授予信用额度。'));
    }
    if(op.action==='password_change')content.append(note('修改范围：模拟银行访问密码。Navren 应用登录密码保持不变；成功后银行会话与未执行草案失效，需重新验证。'));
    if(view.budget)content.append(note('确认前可用额度 '+B.money(view.budget.investable_minor)+'；其中生效资金预留 '+B.money(view.budget.held_minor||0)+'。'));
    content.append(note('操作 '+op.id+(Number.isFinite(op.expires_at)?' · 有效至 '+new Date(op.expires_at*1000).toLocaleString('zh-CN'):' · 有效期尚未核对')+'，账户或画像变化后须重新准备。'));
    const form=node('form',undefined,'bank-form'); d.append(form);
    const consentText=['transfer','schedule_transfer','book_services'].includes(op.action)?'我已核对收款人、金额和模拟资金来源，确认这项操作。':
      ['request_split','collect_split_payment','cancel_subscription','reserve_funds','release_funds','cancel_scheduled_payment'].includes(op.action)?'我已核对任务、对象和模拟状态变更，确认继续。':
      op.action==='card_control'?'我已核对卡片尾号和交易控制，确认更新模拟设置。':
      op.action==='report_card_lost'?'我确认这张尾号卡片已遗失，并确认执行模拟挂失。':
      op.action==='unblock_card'?'我已核对本账户卡片与挂失状态，确认仅解挂并保持交易开关关闭。':
      op.action==='password_change'?'我确认修改模拟银行访问密码，并知悉银行会话与旧草案将失效。':
      op.action==='apply_card'&&op.payload.requested_type==='virtual'?'我已核对 SIM 虚拟卡的单笔限额、费用和期限，确认生成本机模拟卡片。':
      ['request_card_limit','apply_card'].includes(op.action)?'我已核对申请类型和额度，确认提交模拟申请。':
      '我已核对对象、金额及资金约束，确认执行上述模拟操作。';
    const consent=context.reject===true?null:check(form,consentText);
    const submit=button('确认执行',null,true); submit.type='submit';
    async function cancelOperation(returnToForm=false){
      const returnForm=returnToForm?trigger?.form:null;
      if(busy||!isCurrent()||returnToForm&&(stateUnknown||verification?.isSending()||pendingWrites.has(op.id)||
        !trigger?.isConnected||!returnForm?.isConnected||trigger.dataset.operationId!==op.id||
        !Number.isFinite(op.expires_at)||Date.now()/1000<op.expires_at))return;
      busy=true; cancel.disabled=true;pendingWrites.add(op.id);if(returnToForm)verification?.refresh();
      try {
        const cancelled=await B.post('/operations/'+op.id+'/cancel',{});
        if(!isCurrent()){abort();return;}
        if(cancelled?.id!==op.id||cancelled.state!=='cancelled')throw Error('尚未取得同一操作的取消回执。');
        if(returnToForm){
          if(trigger.isConnected&&returnForm.isConnected&&trigger.form===returnForm&&returnForm.closest('#bankTransfers')&&trigger.dataset.operationId===op.id){
            delete trigger.dataset.operationId;delete trigger.dataset.bankOperationState;delete trigger.dataset.bankOperationVersion;
            returnForm.dataset.navrenDirty='true';
          d.finish();trigger.focus();return;
          }
          form.remove();error.textContent='旧确认卡已取消，原转账表单已变化，请重新打开转账页面。';
          recovery.replaceChildren();recovery.hidden=true;stateUnknown=false;receipt(cancelled,content,null);return;
        }
        form.remove(); error.textContent=''; recovery.replaceChildren(); recovery.hidden=true; stateUnknown=false;
        receipt(cancelled,content,trigger);
      }
      catch(e){if(isCurrent()){error.textContent=e.message+'\n正在核验操作状态…';await queryOperationState({expiredReturnFailure:returnToForm,protectedReturnForm:returnToForm?returnForm:null});}}
      finally {pendingWrites.delete(op.id);if(dialog===d)busy=false;if(isCurrent()&&!stateUnknown)cancel.disabled=false;if(returnToForm&&isCurrent())verification?.refresh();}
    }
    const cancel=button('取消操作',()=>cancelOperation());
    const row=node('div',undefined,'bank-actions'); row.append(cancel);if(context.reject!==true)row.append(submit);form.append(row);
    const recovery=node('div',undefined,'bank-actions'); recovery.hidden=true;
    let stateUnknown=false;
    const termsMissing=(op.action==='apply_card'&&op.payload.requested_type==='virtual'&&!virtualCardTerms(view))||(op.action==='transfer'&&!Object.hasOwn(op.payload,'remittance')&&!ordinaryTransferTerms(view));
    const originalTransferForm=trigger?.form;
    const canReturnToTransfer=context.reject!==true&&op.action==='transfer'&&!Object.hasOwn(op.payload,'remittance')&&
      trigger?.dataset.agentOperation!=='true'&&trigger?.type==='submit'&&!!originalTransferForm?.closest('#bankTransfers')&&trigger.dataset.operationId===op.id;
    const returnTargetCurrent=()=>!pendingWrites.has(op.id)&&trigger?.isConnected&&originalTransferForm?.isConnected&&
      trigger.form===originalTransferForm&&!!originalTransferForm.closest('#bankTransfers')&&trigger.dataset.operationId===op.id;
    const verification=context.reject===true?null:operationVerification(form,view,{isCurrent,isBusy:()=>busy||stateUnknown,onError:e=>{error.textContent=operationVerificationError(e).message;},onChange:()=>refreshControls(),
      onExpiredReturn:canReturnToTransfer?()=>cancelOperation(true):null,canExpiredReturn:returnTargetCurrent});
    const finishConfirmation=d.finish;d.finish=()=>{verification?.dispose();finishConfirmation();};
    function refreshControls(){submit.disabled=busy||stateUnknown||termsMissing||!!verification&&!verification.canConfirm()||!!consent&&!consent.checked;cancel.disabled=busy||stateUnknown;}
    consent?.addEventListener('change',refreshControls);
    verification?.refresh();
    refreshControls();
    async function queryOperationState({confirmationError='',preserveChallenge=false,expiredReturnFailure=false,protectedReturnForm=null}={}) {
      if(!isCurrent())return;
      try {
        const current=await B.get('/operations/'+op.id), state=current.operation.state;
        if(!isCurrent()){abort();return;}
        if(current.operation.id!==op.id)throw Error('操作回执对象不一致。');
        const receiptTrigger=protectedReturnForm&&!(trigger?.isConnected&&protectedReturnForm.isConnected&&
          trigger.form===protectedReturnForm&&protectedReturnForm.closest('#bankTransfers')&&trigger.dataset.operationId===op.id)?null:trigger;
        if(context.reject===true&&String(current.operation.version)!==String(op.version))throw Error('操作版本已变化，不能继续取消旧草案。');
        d.dataset.operationState=state;
        recovery.replaceChildren(); recovery.hidden=true;
        stateUnknown=false;
        if(['succeeded','pending_settlement','settled'].includes(state)) {
          form.remove(); receipt(current.operation,content,receiptTrigger); error.textContent='已重新查询并核实执行回执。'; emitChange();
        } else if(state==='pending_confirmation') {
          const retained=verification?.update(current,{preserveChallenge})===true;
          error.textContent=expiredReturnFailure?'未取得旧卡的取消回执；操作仍待确认，请重新核对，暂不要重新生成。':confirmationError?confirmationError+'\n已核实该操作尚未执行，请重新核对后手动确认。':context.reject===true?'已查到这项草案仍待确认，尚未取消。可再次拒绝同一操作。':'已查到该操作仍待确认，当前没有成功回执。可继续确认同一操作，或取消。';
          refreshControls();
          return retained;
        } else {
          form.remove(); receipt(current.operation,content,receiptTrigger,current.liquidity_floor_minor);
        }
      } catch(_) {
        if(!isCurrent()){abort();return;}
        stateUnknown=true;verification?.clear();verification?.refresh();
        d.dataset.operationState='unknown';
        submit.disabled=cancel.disabled=true;
        error.textContent='目前无法核实此操作状态。请勿重复确认或取消。操作编号：'+op.id;
        const retry=button('重新查询操作状态',async()=>{retry.disabled=true;await queryOperationState();if(stateUnknown)retry.disabled=false;});
        const handoff=button('生成待人工核查摘要',()=>requestManualHandoff('银行操作状态暂无法核实，操作编号：'+op.id));
        recovery.replaceChildren(retry,handoff); recovery.hidden=false;
      }
    }
    form.onsubmit=async e=>{
      e.preventDefault(); if(context.reject===true||busy||!isCurrent()||termsMissing||!verification.canConfirm()||!consent.checked||!form.reportValidity())return;
      let payload=null,preserveChallenge=false;
      try {
        payload={confirmed:true,confirmation_digest:op.confirmation_digest,...verification.payload()};
        busy=true; submit.disabled=cancel.disabled=true; error.textContent='';pendingWrites.add(op.id);verification.refresh();
        const result=await B.post('/operations/'+op.id+'/confirm',payload);
        if(!isCurrent()){abort();return;}
        if(result.operation?.id!==op.id)throw Error('执行回执对象不一致，请查询同一操作状态。');
        if(op.action==='password_change'&&result.operation.state==='succeeded') {
          if(result.credential_scope!=='simulated_bank_access'||result.relogin_required!==true||result.sessions_revoked!==true)
            throw Error('密码执行回执缺少会话撤销信息，请核查同一操作。');
        }
        verification.dispose();form.remove();receipt(result.operation,content,trigger);
        if(op.action==='password_change'&&result.operation.state==='succeeded') {
          const workspace=guard?.workspace_id||B.identity()?.workspace_id;
          B.requireBankReauthentication(workspace);
          if(dialog===d)d.finish();
          const completed=modal('银行访问密码已更新');if(completed)completed.append(note('操作 '+op.id+' 已完成。银行会话与旧草案已失效；恢复同一模拟银行工作区不会解除金融安全锁。'),button('验证银行访问密码',()=>{completed.finish();passwordSessionForm(workspace);}));
        }else emitChange();
      } catch(e) {
        if(!isCurrent()){abort();return;}
        const failure=operationVerificationError(e);
        error.textContent=failure.message+'\n尚未取得成功回执，正在核验状态…';
        preserveChallenge=await queryOperationState({confirmationError:failure.message,preserveChallenge:failure.retryChallenge})===true;
      } finally {pendingWrites.delete(op.id);if(payload){delete payload.verification_code;delete payload.challenge_id;}if(preserveChallenge)verification.clearCode();else verification.clear();if(dialog===d)busy=false;if(isCurrent()){verification.refresh();refreshControls();}}
    };
    d.append(recovery);
    if(context.reject===true)await cancel.onclick();
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
    const intro=note(replacementCard
      ?'此申请关联已挂失卡片尾号 '+replacementCard.last_four+'，只记录本机模拟审核，不会实际制卡。'
      :'选择申请类型；实体卡申请等待模拟审核，SIM 虚拟卡经本人确认后在本机生成。');d.append(intro);
    const form=node('form',undefined,'bank-form'), error=errorBox();
    let type;
    if(replacementCard) {
      type={value:replacementCard.type};
      form.append(note('补发类型：'+(replacementCard.type==='virtual'?'SIM 虚拟卡':replacementCard.type==='credit'?'信用卡':'借记卡')));
    } else {
      const label=node('label','卡片类型'), select=node('select');
      for(const [value,text] of [['debit','借记卡'],['credit','信用卡'],['virtual','SIM 虚拟卡']]){const option=node('option',text);option.value=value;select.append(option);}
      select.value=['credit','virtual'].includes(requestedType)?requestedType:'debit';select.setAttribute('aria-label','卡片类型');label.append(select);form.append(label);type=select;
    }
    const limit=field(form,'申请信用额度（元）','text','');limit.inputMode='decimal';limit.setAttribute('aria-label','申请信用额度（元）');
    const purposeLabel=node('label','虚拟卡用途'),purpose=node('select');purpose.setAttribute('aria-label','虚拟卡用途');
    for(const [value,text] of [['online_payment','线上支付'],['subscription','订阅'],['travel','旅行']]){const option=node('option',text);option.value=value;purpose.append(option);}purposeLabel.append(purpose);form.append(purposeLabel);
    const updateLimitVisibility=()=>{const needed=['credit','virtual'].includes(type.value);limit.parentElement.style.display=needed?'':'none';limit.required=needed;
      const label=type.value==='virtual'?'单笔交易限额（元）':'申请信用额度（元）';limit.parentElement.firstChild.textContent=label;limit.setAttribute('aria-label',label);
      purposeLabel.hidden=type.value!=='virtual';purpose.disabled=type.value!=='virtual';
      if(!replacementCard)intro.textContent=type.value==='virtual'?'仅生成本机 SIM 虚拟卡，无真实卡号、授信或对外支付能力；费用与期限请在确认卡核对。':'实体卡申请只提交到模拟审核队列，不会实际制卡或授予信用额度。';};
    if(!replacementCard)type.addEventListener('change',updateLimitVisibility);
    updateLimitVisibility();
    const submit=button('生成确认卡',null,true);submit.type='submit';form.append(submit);d.append(form,error);
    form.onsubmit=e=>{
      e.preventDefault();
      try {
        const requested=['credit','virtual'].includes(type.value)?B.minor(limit.value):0;
        if(['credit','virtual'].includes(type.value)&&(!Number.isSafeInteger(requested)||requested<=0))throw new Error(type.value==='virtual'?'单笔限额必须大于零，最多两位小数。':'信用额度必须大于零。');
        const draft={action:'apply_card',account_id:accountId,requested_type:type.value,
          requested_limit_minor:requested,idempotency_key:B.key()};
        if(type.value==='virtual')draft.purpose=purpose.value;
        if(replacementCard)draft.card_id=replacementCard.id;
        d.finish();openDraft(draft).catch(()=>{});
      } catch(e) {error.textContent=e.message;}
    };
    if(['credit','virtual'].includes(type.value))limit.focus();else if(!replacementCard)type.focus();
  }
  function reportCardLost(card) {
    return openDraft({action:'report_card_lost',account_id:card.account_id,card_id:card.id,idempotency_key:B.key()});
  }
  function unblockCard(card) {
    return openDraft({action:'unblock_card',account_id:card.account_id,card_id:card.id,idempotency_key:B.key()});
  }
  function validBankPassword(value){return typeof value==='string'&&Array.from(value).length>=12&&Array.from(value).length<=128&&new TextEncoder().encode(value).length<=512;}
  async function passwordChangeForm(accountId) {
    if(bankRecovery)return passwordSessionForm(bankRecovery.workspace_id);
    const original=B.identity?.()?B.guard():null,epoch=identityEpoch,d=modal('银行访问密码');if(!d)return;
    let guard=original;
    const current=()=>dialog===d&&d.isConnected&&d.open&&epoch===identityEpoch&&(!guard||guard.valid());
    d.append(note('仅修改模拟银行访问密码；Navren 应用登录密码保持不变。修改成功后需重新验证银行访问。'));
    const form=node('form',undefined,'bank-form'),error=errorBox(),password=field(form,'新银行访问密码','password'),repeat=field(form,'再次输入新密码','password');
    for(const input of [password,repeat]){input.required=true;input.minLength=12;input.maxLength=128;input.autocomplete='new-password';}
    const submit=button('生成密码修改确认卡',null,true);submit.type='submit';form.append(submit);d.append(form,error);
    form.onsubmit=async e=>{
      e.preventDefault();if(busy||!current()||!form.reportValidity())return;
      if(!validBankPassword(password.value)||password.value!==repeat.value){error.textContent='请填写12至128个字符的新密码，并保证两次输入一致。';return;}
      busy=true;submit.disabled=true;pendingWrites.add('password-draft');
      try {
        await B.ensure();if(!current())return;guard=guard||B.guard();
        const body={new_password:password.value};password.value=repeat.value='';
        let prepared;try{prepared=await B.post('/security/password-drafts',body);}finally{delete body.new_password;}
        if(!current())return;
        if(prepared.credential_scope!=='simulated_bank_access'||prepared.secret_returned!==false||prepared.execution_authority!==false||
          typeof prepared.replacement_id!=='string'||!/^[a-f0-9]{32}$/.test(prepared.replacement_id)||
          !Number.isFinite(prepared.expires_at)||prepared.expires_at<=Date.now()/1000)throw Error('密码替换资料无法核对，请重新输入。');
        const draft={action:'password_change',account_id:accountId,replacement_id:prepared.replacement_id,idempotency_key:B.key()};
        busy=false;pendingWrites.delete('password-draft');d.finish();await openDraft(draft,null,{isCurrent:()=>epoch===identityEpoch&&guard.valid()});
      }catch(e){if(current())error.textContent=e.message;}
      finally{password.value=repeat.value='';pendingWrites.delete('password-draft');if(dialog===d){busy=false;submit.disabled=false;}}
    };
    password.focus();return d;
  }
  function passwordSessionForm(workspaceId=bankRecovery?.workspace_id) {
    const d=modal('验证银行访问密码');if(!d)return;
    const epoch=identityEpoch,initial=B.identity?.();let controller=null,timeout=null;
    const current=()=>dialog===d&&d.isConnected&&d.open&&epoch===identityEpoch&&
      (!B.identity?.()||B.identity().workspace_id===workspaceId)&&(!initial||B.identity()?.privacy_revision===initial.privacy_revision);
    const finish=d.finish;d.finish=()=>{controller?.abort();clearTimeout(timeout);finish();};
    d.append(note('恢复同一模拟银行工作区的访问；不会修改 Navren 应用登录或解除金融安全锁。'));
    const form=node('form',undefined,'bank-form'),error=errorBox(),password=field(form,'银行访问密码','password');password.required=true;password.minLength=12;password.maxLength=128;password.autocomplete='current-password';
    const submit=button('验证并恢复银行访问',null,true);submit.type='submit';form.append(submit);d.append(form,error);
    if(typeof workspaceId!=='string'||!/^[a-f0-9]{32}$/.test(workspaceId)||initial&&initial.workspace_id!==workspaceId){error.textContent='原银行工作区无法核对，请重新选择账户。';submit.disabled=true;return d;}
    form.onsubmit=async e=>{
      e.preventDefault();if(busy||!current()||!form.reportValidity())return;
      if(!validBankPassword(password.value)){error.textContent='银行访问密码须为12至128个字符。';return;}
      busy=true;submit.disabled=true;pendingWrites.add('password-session');error.textContent='';
      controller=new AbortController();
      timeout=setTimeout(()=>controller.abort(),15000);
      try {
        // 该专用未登录入口不能调用 ensure；否则可能创建另一个默认工作区。
        const body={workspace_id:workspaceId,password:password.value};password.value='';let response;
        try{response=await fetch('/api/finance/security/password-session',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Accept':'application/json','Content-Type':'application/json'},body:JSON.stringify(body),signal:controller.signal});}finally{delete body.password;}
        if(!current())return;const restored=await response.json();if(!current())return;
        if(!response.ok)throw Error(restored.error?.message||'银行访问密码验证失败。');
        if(restored.workspace_id!==workspaceId||restored.credential_scope!=='simulated_bank_access'||typeof restored.csrf!=='string')throw Error('恢复响应与原银行工作区不一致。');
        const sessionResponse=await fetch('/api/finance/session',{method:'GET',credentials:'same-origin',cache:'no-store',headers:{'Accept':'application/json'},signal:controller.signal});
        if(!current())return;const session=await sessionResponse.json();if(!current())return;
        if(!sessionResponse.ok||session.workspace_id!==workspaceId||session.csrf!==restored.csrf||!Number.isInteger(session.privacy_revision))throw Error('银行会话身份尚未核对，请重新验证。');
        B.acceptSelectedSession(session,{bank_reauthentication:B.bankReauthenticationRequired?.()===true});bankRecovery=null;if(dialog===d)d.finish();emitChange();
      }catch(e){if(current())error.textContent=e.name==='AbortError'?'银行访问验证超时，请重新验证。':e.message;}
      finally{password.value='';clearTimeout(timeout);pendingWrites.delete('password-session');if(dialog===d){busy=false;submit.disabled=false;}}
    };
    password.focus();return d;
  }
  function bankRecoveryWorkspace(){return bankRecovery?.workspace_id||B.bankReauthenticationWorkspace?.()||null;}
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
  function bindDraftCard(card,b,reject) {
    let running=false,guard=B.identity?.()?B.guard?.():null;
    const status=card.querySelector('.bank-plan-status');
    const current=()=>card.isConnected&&b.isConnected&&(!guard||guard.valid());
    const run=async rejecting=>{
      if(running||busy||pendingWrites.size||!current())return;
      running=true;b.disabled=true;if(reject)reject.disabled=true;
      try {
        const s=await B.ensure();
        if(!current())return;
        if(s.workspace_id!==b.dataset.bankWorkspace)throw Error('这张草案属于其他模拟账户，请重新提问。');
        if(b.dataset.bankPrivacy!=null&&String(s.privacy_revision)!==b.dataset.bankPrivacy)throw Error('资料授权已变化，请重新查询草案。');
        guard=guard||B.guard?.();if(!current())return;
        const id=b.dataset.operationId;
        if(rejecting&&!id)throw Error('草案没有操作编号，不能取消。请先核对。');
        await openDraft(id?{operation_id:id}:JSON.parse(b.dataset.bankDraft),b,{
          reject:rejecting,isCurrent:current,expected_operation_version:b.dataset.bankOperationVersion
        });
      }catch(e){if(current()&&status)status.textContent=e.message;}
      finally{running=false;if(current()){b.disabled=false;if(reject)reject.disabled=!b.dataset.operationId||b.dataset.bankOperationState&&b.dataset.bankOperationState!=='pending_confirmation';}}
    };
    b.onclick=()=>run(false);if(reject)reject.onclick=()=>run(true);
  }
  function renderResponse(msg,result) {
    const bubble=msg.querySelector('.bubble');if(!bubble||!result.bank)return;
    const box=node('div',undefined,'bank-actions');box.dataset.bankResponse=result.bank_response_id;
    if(Number.isInteger(result.version)&&!bubble.querySelector('.reply-components'))box.append(note('查询时账本版本 '+result.version+'；历史对话保留当时数据，实时状态请查看账户。'));
    if(result.cashflow_timeline?.status!=='withheld'&&Array.isArray(result.cashflow_timeline?.timeline))
      box.append(renderCashflowTimeline(result.cashflow_timeline));
    if(result.agent_workflow)box.append(renderWorkflowEvidence(result.agent_workflow));
    if(result.agent_review?.status==='approved'&&(result.bank_actions||[]).length)box.append(note('本次解释或操作草案已经独立核验；实际操作仍需你核对并确认。'));
    for(const item of result.bank_actions||[]) {
      const card=node('div',undefined,'bank-action-card'),row=node('div',undefined,'bank-actions bank-plan-actions');
      const b=button('核对并确认',null,true),reject=button('拒绝',null);
      b.dataset.bankDraft=JSON.stringify(item.draft||{});b.dataset.bankWorkspace=result.workspace_id;
      const privacy=result.privacy_revision??B.identity?.()?.privacy_revision;
      if(privacy!=null)b.dataset.bankPrivacy=String(privacy);
      const id=item.operation_id||item.draft?.operation_id;
      if(typeof id==='string'&&id){b.dataset.operationId=id;b.dataset.agentOperation='true';}
      if(item.operation_version!=null)b.dataset.bankOperationVersion=String(item.operation_version);
      reject.dataset.bankReject='true';reject.disabled=!b.dataset.operationId;
      row.append(b,reject);card.append(row,node('p','待核对，尚未执行','bank-plan-status'));box.append(card);
      bindDraftCard(card,b,reject);
    }
    if((result.bank_actions||[]).length||result.isFallback)box.append(button('查看账户与操作记录',()=>NavrenApp.navigate('account.html', getInterfaceMode())));
    if(box.childElementCount)bubble.append(box);
  }
  function rebind(msg,{historical=false}={}) {
    msg.querySelectorAll('[data-bank-draft]').forEach(b=>{const card=b.closest('.bank-action-card')||b.parentElement;bindDraftCard(card,b,card.querySelector('[data-bank-reject]'));});
    if(historical)msg.querySelectorAll('[data-bank-draft]').forEach(b=>{
      const text=node('span',b.textContent+'（历史草案，请重新查询）','bank-history-action');
      b.replaceWith(text);
    });
    if(historical)msg.querySelectorAll('[data-bank-reject]').forEach(b=>{b.disabled=true;b.onclick=null;});
    // 历史中的普通跳转按钮也恢复事件。
    msg.querySelectorAll('.bank-actions button:not([data-bank-draft]):not([data-bank-reject]):not([data-calendar-action])').forEach(b=>{b.onclick=()=>NavrenApp.navigate('account.html', getInterfaceMode());});
  }
  document.addEventListener('DOMContentLoaded',()=>{
    load().catch(()=>{});
    window.addEventListener('focus',()=>{if(!dialog)load().catch(()=>{});});
  });
  return {load,node,button,note,panel,table,budgetPanel,showError,openDraft,openReceipt,amountForm,cardControlAction,
    cardSingleLimitForm,cardLimitRequestForm,cardApplicationForm,reportCardLost,unblockCard,passwordChangeForm,passwordSessionForm,bankRecoveryWorkspace,operationVerification,operationVerificationError,financialRecoveryForm,editProfile,renderResponse,rebind,
    state:()=>state,products:()=>products,statuses,actions,operationStatus,
    switchState:()=>{const aa=window.NavrenAAPayments?.switchState?.()||{};return {busy:busy||pendingWrites.size>0||!!aa.busy,unknown:dialog?.dataset.operationState==='unknown'||!!aa.unknown,unconfirmed:dialog?.dataset.operationState==='pending_confirmation'||!!aa.unconfirmed};}};
})();
