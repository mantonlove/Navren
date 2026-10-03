/** 账户页只展示同源模拟银行状态，不读取本地示例余额与持仓。 */
let currentMode='classic';
const U=BankUI, B=BankClient;
document.addEventListener('DOMContentLoaded',()=>{
  currentMode=getInterfaceMode();
  document.body.className='mode-'+currentMode;
  applyFontSize(getModeFont(currentMode));
  document.getElementById('quickActions').replaceChildren(
    U.button('账单与异常',()=>document.getElementById('bankReports').scrollIntoView({behavior:'smooth'})),
    U.button('比较理财产品',()=>goTo('recommend.html')),
    U.button('管理银行代扣',()=>document.getElementById('bankMandates').scrollIntoView({behavior:'smooth'})),
    U.button('风险与资金设置',()=>U.editProfile())
  );
  if(U.state())renderAccount(U.state(),U.products());
});
window.addEventListener('bank:ready',event=>renderAccount(event.detail.state,event.detail.products));
window.addEventListener('bank:unavailable',event=>{
  ['assetOverview','holdingsList','bankBudget','bankMandates','bankOrders','bankReports'].forEach(id=>{
    const target=document.getElementById(id);if(target)U.showError(target,event.detail,()=>U.load().catch(()=>{}));
  });
});
function renderAccount(s,products) {
  const metrics=[['总资产（本金）',s.total_assets_principal_minor],['现金余额',s.total_cash_minor],['理财本金',s.holding_principal_minor],['待结算赎回',s.pending_redemption_minor]];
  document.getElementById('assetOverview').replaceChildren(...metrics.map(([label,value])=>{const p=U.node('div',undefined,'bank-panel bank-metric');p.append(U.note(label),U.node('strong',B.money(value)));return p;}));
  const accounts=U.panel('账户与负债');
  accounts.append(U.table(['账户','已结算现金'],s.accounts.map(a=>[a.name,B.money(a.balance_minor)])));
  accounts.append(U.note('贷款与信用卡负债 '+B.money(s.liabilities_minor)+' · 净资产（本金） '+B.money(s.net_assets_principal_minor)+'。收益及市场估值未模拟。'));
  accounts.append(U.note('模拟画像 R'+s.profile.risk_level+' · 最长投资期限 '+s.profile.horizon_days+' 天'),U.button('修改模拟风险与资金设置',()=>U.editProfile()));
  document.getElementById('bankBudget').replaceChildren(accounts,U.budgetPanel(s));
  const holdings=document.getElementById('holdingsList');holdings.replaceChildren();
  for(const h of s.holdings.filter(h=>h.amount_minor>0)) {
    const product=products.find(p=>p.id===h.product_id), p=U.panel(product.name);
    const lots=s.lots.filter(l=>l.account_id===h.account_id&&l.product_id===h.product_id&&l.remaining_minor>0);
    const eligible=lots.filter(l=>product.early_redemption||l.locked_until<=s.as_of).reduce((a,l)=>a+l.remaining_minor,0);
    p.append(U.node('p','持有本金 '+B.money(h.amount_minor)+' · 当前可赎回 '+B.money(eligible)));
    p.append(U.table(['批次','剩余本金','锁定至'],lots.map(l=>[l.id.slice(-8),B.money(l.remaining_minor),l.locked_until])));
    const redeem=U.button('赎回 '+product.name,()=>U.amountForm(product,'redeem',h.account_id,eligible));redeem.disabled=!eligible;
    p.append(redeem,U.note('赎回按已解锁批次处理；到账延迟 '+product.redemption_delay_days+' 个模拟自然日。'));holdings.append(p);
  }
  if(!holdings.children.length)holdings.append(U.note('暂无持仓。'));
  renderMandates(s);renderOperations(s,products);renderReports().catch(()=>{});
}
function renderMandates(s) {
  const root=document.getElementById('bankMandates'), p=U.panel('银行代扣授权');
  p.append(U.note('关闭银行代扣不等于取消商户订阅合同；续费和欠款仍需向商户核实。'));
  for(const m of s.mandates) {
    const row=U.node('div',undefined,'bank-row'), text=U.node('div');
    text.append(U.node('strong',m.merchant),U.note(B.money(m.amount_minor)+' · '+(m.status==='active'?'有效；下次 '+m.next_charge_date:'银行代扣已关闭；商户合同未改变')));row.append(text);
    const close=U.button(m.status==='active'?'关闭 '+m.merchant+' 代扣':'已关闭',()=>U.openDraft({action:'close_mandate',account_id:m.account_id,mandate_id:m.id,idempotency_key:B.key()}));close.disabled=m.status!=='active';row.append(close);p.append(row);
  }
  root.replaceChildren(p);
}
function renderOperations(s,products) {
  const root=document.getElementById('bankOrders'), p=U.panel('订单与操作记录');
  p.append(U.note('包含待确认、取消、失效及已执行状态。待确认不代表已扣款；待结算赎回不代表已到账。'));
  if(!s.operations.length)p.append(U.note('当前账户暂无操作记录。'));
  else p.append(U.table(['操作','对象 / 金额','状态','编号','核验'],[...s.operations].reverse().map(op=>{
    const payload=op.payload;
    const name=products.find(p=>p.id===payload.product_id)?.name||s.mandates.find(m=>m.id===payload.mandate_id)?.merchant||'';
    const view=U.button('查看 '+op.id.slice(0,8),()=>U.openDraft(payload,view));view.dataset.operationId=op.id;
    return [U.actions[op.action],name+(payload.amount_minor?' / '+B.money(payload.amount_minor):''),U.statuses[op.state]||op.state,op.id.slice(0,8),view];
  })));
  if(s.orders.length)p.append(U.table(['订单','产品','方向','金额','状态','结算日'],s.orders.map(o=>[o.id.slice(0,8),o.product_id,U.actions[o.side],B.money(o.amount_minor),U.statuses[o.status]||o.status,o.settlement_date])));
  root.replaceChildren(p);
}
let reportEpoch=0, selectedMonth='2026-09', selectedYear='2025';
async function renderReports() {
  const epoch=++reportEpoch, root=document.getElementById('bankReports');if(!root)return;
  const control=U.panel('账单分析与周期扣费'), form=U.node('form',undefined,'bank-form');
  const ml=U.node('label','月报月份'), month=U.node('input');month.type='month';month.value=selectedMonth;month.required=true;ml.append(month);
  const yl=U.node('label','年度'), year=U.node('input');year.type='number';year.min=2025;year.max=2100;year.value=selectedYear;yl.append(year);
  const submit=U.button('查看报告',null,true);submit.type='submit';form.append(ml,yl,submit);control.append(form);
  const reports=U.node('div'), error=U.node('p','','bank-error');error.setAttribute('role','alert');root.replaceChildren(control,reports,error);
  form.onsubmit=e=>{e.preventDefault();selectedMonth=month.value;selectedYear=year.value;renderReports().catch(()=>{});};
  try {
    const [monthly,annual,anomaly,periodic,rows]=await Promise.all([
      B.get('/analysis?month='+encodeURIComponent(selectedMonth)),B.get('/annual-analysis?year='+encodeURIComponent(selectedYear)),
      B.get('/anomalies'),B.get('/periodic-charges'),B.get('/transactions')]);
    if(epoch!==reportEpoch)return;
    const mp=U.panel(monthly.month+' 月报');
    mp.append(U.node('p','收入 '+B.money(monthly.totals_minor.income)+' · 消费 '+B.money(monthly.totals_minor.expense)+' · 退款 '+B.money(monthly.totals_minor.refund)));
    mp.append(U.note('已记录 '+monthly.transaction_count+' 笔；消费收支结余 '+B.money(monthly.income_expense_surplus_minor)+'，现金净变动 '+B.money(monthly.cash_delta_minor)+'。'));
    if(!monthly.transaction_count)mp.append(U.note('没有已记录流水，不代表没有消费。月报读取2026年6月起的活期流水；更早的存档请查看年报。'));
    mp.append(U.table(['消费分类','金额'],Object.entries(monthly.category_expense_minor).map(([k,v])=>[k,B.money(v)])),U.note(monthly.notice));reports.append(mp);
    const ap=U.panel(annual.year+' 年报');
    ap.append(U.node('p','收入 '+B.money(annual.totals_minor.income||0)+' · 消费 '+B.money(annual.totals_minor.expense||0)+' · 退款 '+B.money(annual.totals_minor.refund||0)));
    ap.append(U.note('覆盖 '+annual.covered_months.length+' 个月；'+(annual.complete_calendar_year?'全年完整':'非完整年度')+'。存档 '+annual.archived_count+' 笔，当前流水 '+annual.live_count+' 笔。'));
    ap.append(U.table(['消费分类','金额'],Object.entries(annual.category_expense_minor).map(([k,v])=>[k,B.money(v)])),U.note(annual.notice));reports.append(ap);
    const an=U.panel('异常流水（全部现有流水）');an.append(U.note(anomaly.notice+' 所有标记均待核实，不代表已确认欺诈。'));
    for(const flag of [...anomaly.duplicates,...anomaly.high_amounts]) {
      const p=U.panel(flag.rule==='high_amount'?'异常大额消费':'5分钟内疑似重复扣款');
      p.append(U.table(['流水编号','时间','商户','金额'],flag.transaction_ids.map(id=>{const r=rows.find(r=>r.id===id);return [id,r?.occurred_at,r?.merchant,r?B.money(r.amount_minor):'记录不可用'];})));
      if(flag.baseline_count)p.append(U.note('历史分类基线 '+flag.baseline_count+' 笔，中位数 '+B.money(flag.baseline_median_minor)));an.append(p);
    }
    if(!anomaly.duplicates.length&&!anomaly.high_amounts.length)an.append(U.note('规则未发现异常标记。'));reports.append(an);
    const pc=U.panel('疑似周期扣费');pc.append(U.note('按至少三个月的金额与周期识别。结果需核实，固定生活支出不等于商户订阅。'));
    pc.append(U.table(['商户','分类','参考金额','历史推算日期','银行代扣状态'],periodic.candidates.map(c=>[c.merchant,c.classification,B.money(c.amount_minor),c.expected_next_date,U.state()?.mandates.find(m=>m.id===c.mandate_id)?.status==='closed'?'已关闭':'需查看授权'])));reports.append(pc);
    const tx=U.panel(selectedMonth+' 月流水明细');tx.append(U.table(['时间','商户','类型','金额','分类'],rows.filter(r=>r.occurred_at.startsWith(selectedMonth)).map(r=>[r.occurred_at,r.merchant,{income:'收入',expense:'消费',refund:'退款',transfer:'转账',investment_purchase:'申购',investment_redemption:'赎回'}[r.kind]||r.kind,B.money(r.delta_minor),r.category])));reports.append(tx);
  } catch(e) {if(epoch===reportEpoch)error.textContent=e.message;}
}
