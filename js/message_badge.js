/** 站内消息角标。只读服务端统计，不生成消息、不改变业务状态。 */
window.NavrenMessageBadge = window.NavrenMessageBadge || (() => {
  'use strict';
  let timer=null,epoch=0,busy=false,stopped=false,known=null,observer=null;
  const linkSelector='.sidebar a[data-navren-messages],.sidebar a[href*="messages.html"]';
  const english=()=>window.NavrenI18n?.language==='en';
  function injectStyle(){
    if(document.getElementById('navrenMessageBadgeStyle'))return;const style=document.createElement('style');style.id='navrenMessageBadgeStyle';
    style.textContent='.sidebar .navren-message-link{position:relative}.navren-message-badge{position:absolute;top:5px;right:7px;display:inline-flex;align-items:center;justify-content:center;min-width:16px;height:16px;padding:0 4px;border:2px solid var(--surface-raised,#e4ecf6);border-radius:10px;background:var(--primary-deep,#285b96);color:var(--on-primary,#fff);font:600 9px/1 var(--body,system-ui);font-variant-numeric:tabular-nums}.navren-message-badge[hidden]{display:none}@media(max-width:768px){.navren-message-badge{top:3px;right:calc(50% - 27px);border-color:var(--surface-card,#f8fafe)}}';document.head.append(style);
  }
  function ensureLinks(){
    const sidebar=document.querySelector('.sidebar');if(!sidebar)return;
    if(!sidebar.querySelector('a[href*="messages.html"]')){
      const link=document.createElement('a');link.className='nav-item navren-message-link';link.dataset.navrenMessages='';
      const mode=typeof getInterfaceMode==='function'?getInterfaceMode():undefined;link.href=window.NavrenApp?.href?NavrenApp.href('messages.html',mode):'messages.html';
      link.innerHTML='<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></svg><span class="nav-label" data-i18n-skip></span>';
      const mine=[...sidebar.querySelectorAll('a')].find(a=>{try{return new URL(a.href,location.href).pathname.endsWith('/mine.html')&&!a.classList.contains('shell-settings-entry');}catch(_){return false;}});
      sidebar.insertBefore(link,mine||sidebar.querySelector('.shell-settings-entry')||null);
    }
    for(const link of document.querySelectorAll(linkSelector)){
      link.dataset.navrenMessages='';link.classList.add('navren-message-link');const label=link.querySelector('.nav-label');if(label){label.dataset.i18nSkip='';label.textContent=english()?'Messages':'消息';}
      if(!link.querySelector('.navren-message-badge')){const badge=document.createElement('span');badge.className='navren-message-badge';badge.dataset.i18nSkip='';badge.setAttribute('aria-hidden','true');badge.hidden=true;link.append(badge);}
    }
    paint();
  }
  function paint(failed=false){
    for(const link of document.querySelectorAll(linkSelector)){
      const badge=link.querySelector('.navren-message-badge');if(!badge)continue;const n=known?.unread_count;
      badge.hidden=!(Number.isSafeInteger(n)&&n>0);badge.textContent=n>99?'99+':n==null?'':String(n);
      const base=english()?'Messages':'消息';link.setAttribute('aria-label',n>0?(english()?`${base}, ${n} unread`:`${base}，${n} 条未读`):base);
      link.title=failed?(english()?'Message count is temporarily unavailable':'消息统计暂时无法更新'):n>0?(english()?`${n} unread`:`${n} 条未读`):base;
    }
  }
  function validSummary(data){return data&&Number.isSafeInteger(data.unread_count)&&data.unread_count>=0&&Number.isSafeInteger(data.pending_count)&&data.pending_count>=0;}
  async function refresh(){
    if(stopped||busy||document.visibilityState!=='visible'||!window.BankClient)return;busy=true;const token=epoch;
    try{
      await BankClient.ensure();if(stopped||token!==epoch)return;const guard=BankClient.guard();const data=await BankClient.get('/messages/summary');
      if(stopped||token!==epoch||!guard.valid(data))return;if(!validSummary(data))throw Error('invalid_message_summary');known={...data};paint();window.dispatchEvent(new CustomEvent('navren:message-summary-updated',{detail:{...data}}));
    }catch(_){if(!stopped&&token===epoch)paint(true);}finally{busy=false;}
  }
  function pause(){if(timer){clearInterval(timer);timer=null;}}
  function start(){if(stopped||document.visibilityState!=='visible')return;pause();refresh();timer=setInterval(refresh,45000);}
  function clear(){epoch++;known=null;pause();stopped=true;paint();}
  function summaryEvent(e){const data=e.detail,identity=window.BankClient?.identity();if(!identity||!validSummary(data)||data.workspace_id!==identity.workspace_id||data.privacy_revision!==identity.privacy_revision)return;epoch++;known={...data};stopped=false;paint();if(!timer)start();}
  function init(){
    if(!document.querySelector('.sidebar'))return;
    injectStyle();ensureLinks();observer=new MutationObserver(records=>{if(records.some(r=>[...r.addedNodes].some(n=>n.nodeType===1&&(n.matches?.('.sidebar')||n.querySelector?.('.sidebar')||n.matches?.('.nav-item')||n.querySelector?.('.nav-item')))))ensureLinks();});observer.observe(document.body,{childList:true,subtree:true});
    for(const name of ['navren:privacy-reset','navren:identity-revoked','navren:account-switched'])window.addEventListener(name,clear);
    window.addEventListener('navren:message-summary',summaryEvent);window.addEventListener('navren:language',ensureLinks);
    document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')pause();else if(!stopped)start();});
    window.addEventListener('pagehide',()=>{clear();observer?.disconnect();});window.addEventListener('pageshow',e=>{if(e.persisted){epoch++;stopped=false;observer?.observe(document.body,{childList:true,subtree:true});start();}});start();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
  return {refresh,clear};
})();
