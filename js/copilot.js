/** 同一服务对象的顾问演示视角，无额外后台权限。 */
document.addEventListener('DOMContentLoaded',async()=>{
  const el=(tag,text)=>{const n=document.createElement(tag);n.textContent=text;return n;};
  const fill=(id,rows)=>{const root=document.getElementById(id);if(root)root.replaceChildren(...rows.map(s=>el('p',s)));};
  try{
    await BankClient.refreshIdentity();const guard=BankClient.guard();
    const [state,memories]=await Promise.all([BankClient.get('/overview'),BankClient.get('/memories')]);
    if(!guard.valid(state)||!guard.valid(memories))return;
    fill('clientProfile',['服务对象：当前账户 '+state.workspace_id.slice(0,8),'参考日期：'+state.as_of,
      '风险配置 R'+state.profile.risk_level+'（演示业务配置，非正式风险测评）',
      '已结算现金 '+BankClient.money(state.total_cash_minor),'可安排资金 '+BankClient.money(state.investable_minor)+' · 按全户规则']);
    fill('talkingPoints',memories.enabled?(memories.items||[]).filter(m=>m.user_confirmed).slice(0,5).map(m=>'本人确认资料：'+m.value):['对话资料读取已关闭；账户记录与本人保存的资料分开。']);
    const handoff=Storage.get('qingzhou_handoffPackage');
    const matching=handoff?.workspace_id===state.workspace_id&&handoff.privacy_revision===state.privacy_revision;
    if(matching){
      const box=document.createElement('section');box.className='card';box.append(el('h2','本地人工交接摘要'),el('p','准备时间：'+new Date(handoff.timestamp).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})),el('p','触发原因：'+handoff.reason));
      for(const text of handoff.syncedItems||[])box.append(el('p',text));
      const details=document.createElement('details');details.append(el('summary','回看最近沟通记录'));
      for(const message of handoff.recentMessages||[])details.append(el('p',(message.role==='user'?'本人：':'Navren：')+message.text));
      box.append(details,el('p',handoff.productCtx));document.getElementById('talkingPoints')?.after(box);
      box.dataset.privateHandoff='true';
    }else fill('talkingPoints',[...(memories.enabled?(memories.items||[]).filter(m=>m.user_confirmed).slice(0,5).map(m=>'本人确认资料：'+m.value):['对话资料读取已关闭。']),'尚无当前账户、当前授权版本下的交接摘要；历史未绑定资料不自动恢复。']);
    fill('productHighlights',['完整产品资料、来源和比较请进入发现；本视角不按历史名称生成申购。']);
    const a=el('a','进入产品研究');a.href=NavrenApp.href('recommend.html?view=research#research');document.getElementById('productHighlights')?.append(a);
    fill('complianceNotes',['每一项资金或合同变更仍需要本人在确认卡确认。','顾问演示不代表银行后台角色，也不产生新的资料读取授权。']);
    const page=document.querySelector('.workbench-page');
    const control=document.createElement('details');control.className='card';control.append(el('summary','演示控制：推进参考日期'));
    control.append(el('p','仅作用于本机演示账户；可能触发到期定时转账、代扣、结算及资金预留释放。'));
    const form=document.createElement('form'),label=el('label','推进天数'),input=document.createElement('input');input.type='number';input.min='1';input.max='90';input.value='1';input.required=true;label.append(input);
    const consent=document.createElement('input');consent.type='checkbox';consent.required=true;const cl=el('label','我已了解并确认推进参考日期');cl.prepend(consent);
    const button=el('button','确认推进');button.type='submit';const result=el('p','');result.role='status';form.append(label,cl,button,result);control.append(form);page?.append(control);
    form.onsubmit=async e=>{e.preventDefault();if(button.disabled||!consent.checked||!form.reportValidity())return;button.disabled=true;try{const data=await BankClient.post('/simulation/advance',{days:Number(input.value),confirmed:true});result.textContent='已推进参考日期；最新日期 '+(data.as_of||'请回到账户核对')+'。';consent.checked=false;}catch(err){result.textContent=err.message;}finally{button.disabled=false;}};
    try{const returned=JSON.parse(sessionStorage.getItem('navren_advisor_return')||'null');if(returned?.workspace_id===state.workspace_id&&returned.privacy_revision===state.privacy_revision){const back=document.getElementById('advisorReturn');back.href=NavrenViews.url(returned);back.onclick=()=>sessionStorage.setItem('navren_return_view',JSON.stringify(returned));}}catch(_){}
  }catch(e){fill('clientProfile',['账户身份或资料暂时无法核对，请重试。']);}
});
window.addEventListener('navren:privacy-reset',()=>{for(const id of ['clientProfile','talkingPoints','productHighlights'])document.getElementById(id)?.replaceChildren();document.querySelectorAll('[data-private-handoff]').forEach(n=>n.remove());});
