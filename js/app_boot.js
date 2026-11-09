/** Navren 页面版本与内部跳转；不修改账户、聊天记录和阅读偏好。 */
window.NavrenApp = (() => {
  const release = document.querySelector('meta[name="navren-release"]')?.content;
  if(location.pathname==='/'||location.pathname.endsWith('index.html'))document.addEventListener('DOMContentLoaded',()=>{
    if(typeof Storage==='undefined'||!window.BankClient)return;
    Storage.ready().then(()=>{if(Storage.accountReady()&&Storage.get('navren_onboarded')===true)location.replace('/chat.html'+(release?'?ui='+encodeURIComponent(release):''));}).catch(()=>{});
  });
  // 一次性站内导航意图只控制展示，不保存身份、消息或授权信息。
  const entryKey = 'navren:chat-entry:v1';
  const applicationPages = new Set(['chat.html','recommend.html','account.html','mine.html','messages.html','copilot.html']);
  const pagePath = value => { try { const url=new URL(value,location.href);return url.origin===location.origin?url.pathname:null; } catch (_) { return null; } };
  const applicationPath = value => applicationPages.has(value?.split('/').pop());
  function clearEntry() { try { sessionStorage.removeItem(entryKey); } catch (_) {} }
  function takeEntry(persisted = false) {
    let record;try { record=JSON.parse(sessionStorage.getItem(entryKey)||'null'); } catch (_) {}
    clearEntry();
    const navigation=performance.getEntriesByType?.('navigation')?.[0];
    const path=location.pathname,referrer=pagePath(document.referrer);
    if (!record || record.version!==1 || !['internal','carry-open','carry-folded'].includes(record.kind) ||
        !Number.isSafeInteger(record.at) || Date.now()-record.at<0 || Date.now()-record.at>60000 ||
        record.to!==path || !applicationPath(record.from) || (!persisted && navigation?.type==='reload') ||
        (!persisted && referrer!==record.from)) return 'new-entry';
    return record.kind==='carry-folded'?'new-entry':'internal';
  }
  let chatEntry = takeEntry(), navigationPending = false;
  function rememberEntry(target, carry) {
    const from=location.pathname,to=pagePath(target);
    if (!applicationPath(from) || !applicationPath(to)) { clearEntry();return; }
    try { sessionStorage.setItem(entryKey,JSON.stringify({version:1,from,to,at:Date.now(),kind:carry==null?'internal':carry==='internal'?'carry-open':'carry-folded'})); } catch (_) {}
  }
  window.addEventListener('pageshow',event=>{navigationPending=false;if(event.persisted)chatEntry=takeEntry(true);});
  const pages = new Set(['index.html', 'chat.html', 'recommend.html', 'account.html', 'mine.html', 'copilot.html', 'messages.html']);
  function href(target, mode) {
    const url = new URL(target, location.href);
    if (url.origin === location.origin && pages.has(url.pathname.split('/').pop())) {
      if (release) url.searchParams.set('ui', release);
      if (mode) url.searchParams.set('mode', mode);
      return url.pathname + url.search + url.hash;
    }
    return url.href;
  }
  function navigate(target, mode) { const next=href(target, mode);navigationPending=true;rememberEntry(next);location.assign(next); }
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (link) link.href = href(link.href);
  }, true);
  // 目标和文档的点击处理后记录普通导航，取消和新标签不遗留本站意图。
  window.addEventListener('click',event=>{
    const link=event.target.closest?.('a[href]');if(!link)return;
    if(event.defaultPrevented){if(!navigationPending)clearEntry();return;}
    if(event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey||
        link.hasAttribute('download')||link.target&&link.target!=='_self'){clearEntry();return;}
    const destination=new URL(link.href,location.href);
    if(destination.pathname===location.pathname&&destination.search===location.search&&destination.hash!==location.hash){clearEntry();return;}
    if(link.isConnected)rememberEntry(link.href);
  });
  // 浏览器后退恢复页面、切回旧标签页时，也核对当前服务版本。
  let checking = false;
  async function checkRelease() {
    if (!release || checking || document.visibilityState === 'hidden') return;
    checking = true;
    try {
      const response = await fetch('/api/app-version', {cache:'no-store'});
      if (!response.ok) return;
      const current = await response.json();
      if (current.release && current.release !== release) {
        const url = new URL(location.href);
        url.searchParams.set('ui', current.release);
        // 外观更新不重新发送已执行过的提问或动作。
        url.searchParams.delete('q'); url.searchParams.delete('action');
        rememberEntry(url.href,chatEntry);
        location.replace(url.pathname + url.search + url.hash);
      }
    } catch (_) { /* 服务暂时离线时保留当前页面。 */ }
    finally { checking = false; }
  }
  window.addEventListener('pageshow', checkRelease);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') checkRelease();
  });
  // 各应用页共用消息入口与角标；消息本身由后台规则生成。
  function mountMessageBadge() {
    const page = location.pathname.split('/').pop() || 'index.html';
    if (!pages.has(page) || document.querySelector('script[src*="message_badge.js"]')) return;
    const script = document.createElement('script');
    script.src = '/js/message_badge.js' + (release ? '?v=' + encodeURIComponent(release) : '');
    document.body.append(script);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountMessageBadge);
  else mountMessageBadge();
  return {release, href, navigate, chatHistoryEntry:()=>chatEntry};
})();
