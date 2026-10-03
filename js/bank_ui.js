/** 共享银行界面：产品、资金依据、独立用户确认和可核验回执。 */
window.BankUI = (() => {
  const B = BankClient;
  let state = null, products = [], dialog = null, busy = false;
  const statuses = {pending_confirmation:'待确认，尚未执行',succeeded:'已完成模拟记账',pending_settlement:'赎回待结算，尚未到账',settled:'已结算到账',cancelled:'已取消，未执行',expired:'确认已过期',invalidated:'账户已变化，请重新准备'};
  const actions = {purchase:'模拟申购',redeem:'模拟赎回',close_mandate:'关闭银行代扣'};
  function node(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
  function button(text, fn, primary=false) { const n=node('button',text,'bank-button'+(primary?' primary':'')); n.type='button'; n.onclick=fn; return n; }
  function note(text) { return node('p',text,'bank-note'); }
  function errorBox() { const n=node('p','','bank-error'); n.setAttribute('role','alert'); return n; }
  function panel(title) { const n=node('section',undefined,'bank-panel'); if (title) n.append(node('h3',title)); return n; }
  function table(headers, rows) {
    const wrap=node('div',undefined,'bank-table-wrap'), t=node('table',undefined,'bank-table');
    const head=node('thead'), tr=node('tr'); headers.forEach(v=>tr.append(node('th',v))); head.append(tr); t.append(head);
    const body=node('tbody'); rows.forEach(row=>{const r=node('tr'); row.forEach((v,i)=>{const cell=node('td'); cell.dataset.label=headers[i]; if (v instanceof Node) cell.append(v); else cell.textContent=String(v ?? ''); r.append(cell);}); body.append(r);}); t.append(body); wrap.append(t); return wrap;
  }
  function mainAccount(s=state) { return s.accounts.find(a=>a.id.endsWith('-main')); }
  function budgetPanel(s=state) {
    const p=panel('资金安排依据'), a=mainAccount(s);
    const usable=Math.max(0,a.balance_minor-s.profile.reserve_minor-s.upcoming_due_minor);
    p.append(node('p','日常账户可投资 '+B.money(usable),'bank-success bank-usable'));
    p.append(note('日常账户 '+B.money(a.balance_minor)+' − 应急保留 '+B.money(s.profile.reserve_minor)+' − 30天已知到期款 '+B.money(s.upcoming_due_minor)));
    const details=node('details',undefined,'bank-details');
    details.append(node('summary','查看 '+s.commitments.length+' 项近期支出'),table(['已知到期事项','金额','日期'],s.commitments.map(c=>[c.name,B.money(c.amount_minor),c.due_on])));
    p.append(details);
    p.append(note(s.notice)); return p;
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
    if (dialog) return null;
    const d=node('dialog',undefined,'bank-dialog');
    const heading=node('h2',title), head=node('div',undefined,'bank-row');
    heading.id='bankDialogHeading'; d.setAttribute('aria-labelledby',heading.id);
    const close=button('关闭',()=>d.dispatchEvent(new Event('cancel',{cancelable:true}))); close.setAttribute('aria-label','关闭对话框');
    head.append(heading,close); d.append(head);
    d.finish=()=>{if(busy)return; d.close(); d.remove(); dialog=null;};
    d.addEventListener('cancel',e=>{e.preventDefault(); if(!busy)d.finish();});
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
  function receipt(op,container,trigger,floor=0) {
    container.replaceChildren(node('p',statuses[op.state]||op.state,['expired','invalidated'].includes(op.state)?'bank-error':'bank-success'));
    container.append(note('操作编号：'+op.id));
    if(op.result) {
      if(op.result.order_id)container.append(note('订单编号：'+op.result.order_id));
      if(op.result.amount_minor)container.append(note('金额：'+B.money(op.result.amount_minor)+'；预计结算日：'+op.result.settlement_date));
      if(op.result.balance_after_minor!==undefined)container.append(note('执行后付款账户余额：'+B.money(op.result.balance_after_minor)));
      container.append(note(op.result.notice));
    }
    container.append(button('重新查询回执',async()=>{try {const view=await B.get('/operations/'+op.id); receipt(view.operation,container,trigger,view.liquidity_floor_minor); emitChange();}catch(e){container.append(node('p',e.message,'bank-error'));}}));
    if(['expired','invalidated'].includes(op.state))container.append(button('重新准备确认卡',()=>{
      const draft={...op.payload,idempotency_key:B.key(),liquidity_floor_minor:floor};
      if(trigger){delete trigger.dataset.operationId;if(trigger.dataset.bankDraft)trigger.dataset.bankDraft=JSON.stringify(draft);}
      dialog?.finish();openDraft(draft,trigger).catch(()=>{});
    }));
    if(trigger) {
      trigger.dataset.operationId=op.id;
      trigger.textContent='查看操作状态';
      const status=trigger.parentElement.querySelector('.bank-plan-status');
      if(status)status.textContent=(statuses[op.state]||op.state)+' · '+op.id.slice(0,8);
      persist(trigger);
    }
  }
  async function openDraft(draft,trigger) {
    const d=modal('核对银行操作'); if(!d)return;
    const content=panel(), error=errorBox(); d.append(content,error);
    content.append(note('正在读取操作内容，尚未执行…')); busy=true;
    let view;
    try {
      await B.ensure();
      view=trigger?.dataset.operationId ? await B.get('/operations/'+trigger.dataset.operationId) : await B.post('/operations/prepare',draft);
      if(trigger){trigger.dataset.operationId=view.operation.id; persist(trigger);}
    } catch(e) {error.textContent=e.message; content.replaceChildren(note('准备操作失败，没有确认执行。')); busy=false; return;}
    busy=false;
    const op=view.operation;
    if(op.state!=='pending_confirmation') {receipt(op,content,trigger,view.liquidity_floor_minor); return;}
    content.replaceChildren(node('h3',actions[op.action]));
    content.append(note('环境：模拟银行 · 付款账户：'+view.account.name));
    content.append(node('p',view.product?.name||view.mandate?.merchant||''));
    if(view.product) {
      content.append(node('p','金额 '+B.money(op.payload.amount_minor)));
      content.append(note('R'+view.product.risk_level+' · 锁定 '+view.product.term_days+' 天 · 赎回后 '+view.product.redemption_delay_days+' 个模拟自然日到账'));
      content.append(note(view.product.risk_notice));
      content.append(note('费用未模拟（不是实际收费报价）；待结算赎回不计入可用余额。'));
    } else content.append(note('仅关闭此模拟银行代扣协议。商户订阅合同及应付款不随之取消。'));
    content.append(note('当前付款余额 '+B.money(view.account.balance_minor)+'；保留资金 '+B.money(view.budget.reserve_minor)+'；近期已知到期款 '+B.money(view.budget.upcoming_due_minor)));
    content.append(note('操作 '+op.id+' · 5分钟内有效，账户或画像变化后须重新准备。'));
    const form=node('form',undefined,'bank-form'); d.append(form);
    let pin=null;
    if(view.verification_required) {
      pin=field(form,'模拟额外验证码','password'); pin.inputMode='numeric'; pin.pattern='[0-9]{6}'; pin.maxLength=6; pin.required=true; pin.setAttribute('aria-label','模拟额外验证码');
      const code=note('验证码仅在此用户确认界面使用。请勿发送到对话。');
      form.append(button('查看演示验证码',async()=>{try {const x=await B.get('/demo-code'); code.textContent='本次模拟账户验证码：'+x.code+'。仅用于本机演示。';}catch(e){error.textContent=e.message;}}),code);
    }
    const consent=check(form,'我已核对对象、金额及资金约束，确认执行上述模拟操作。');
    const submit=button('确认执行',null,true); submit.type='submit';
    const cancel=button('取消操作',async()=>{
      if(busy)return; busy=true; cancel.disabled=true;
      try {const cancelled=await B.post('/operations/'+op.id+'/cancel',{}); form.remove(); receipt(cancelled,content,trigger);}
      catch(e){error.textContent=e.message;}
      finally {busy=false;cancel.disabled=false;}
    });
    const row=node('div',undefined,'bank-actions'); row.append(cancel,submit);form.append(row);
    form.onsubmit=async e=>{
      e.preventDefault(); if(busy||!consent.checked||!form.reportValidity())return;
      busy=true; submit.disabled=cancel.disabled=true; error.textContent='';
      const payload={confirmed:true,confirmation_digest:op.confirmation_digest};
      if(pin)payload.verification_code=pin.value;
      try {
        const result=await B.post('/operations/'+op.id+'/confirm',payload);
        form.remove(); receipt(result.operation,content,trigger); emitChange();
      } catch(e) {
        error.textContent=e.message+'\n尚未取得成功回执，请先核验状态。';
        try {
          const current=await B.get('/operations/'+op.id);
          if(['succeeded','pending_settlement','settled'].includes(current.operation.state)) {form.remove(); receipt(current.operation,content,trigger);error.textContent='已重新查询并核实执行回执。';emitChange();}
          else if(current.operation.state!=='pending_confirmation') {form.remove();receipt(current.operation,content,trigger,current.liquidity_floor_minor);}
        } catch(_) {error.textContent+='\n状态查询也失败，请到「账户」刷新后检查操作记录。';}
      } finally {if(pin)pin.value='';busy=false;submit.disabled=cancel.disabled=false;}
    };
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
    for(const item of result.bank_actions||[]) {
      const card=node('div'), b=button(item.label,()=>openDraft(item.draft,b));
      b.dataset.bankDraft=JSON.stringify(item.draft); b.dataset.bankWorkspace=result.workspace_id;
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
    msg.querySelectorAll('.bank-actions button:not([data-bank-draft])').forEach(b=>{b.onclick=()=>NavrenApp.navigate('account.html', getInterfaceMode());});
  }
  document.addEventListener('DOMContentLoaded',()=>{
    const header=document.querySelector('.main > .header');
    const banner=node('div','正在读取模拟银行…','bank-banner');banner.id='bankBanner';banner.setAttribute('aria-live','polite');header?.after(banner);
    load().catch(()=>{});
    window.addEventListener('focus',()=>{if(!dialog)load().catch(()=>{});});
  });
  return {load,node,button,note,panel,table,budgetPanel,showError,openDraft,amountForm,editProfile,renderResponse,rebind,
    state:()=>state,products:()=>products,statuses,actions};
})();
