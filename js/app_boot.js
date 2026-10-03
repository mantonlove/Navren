/** Navren 页面版本与内部跳转；不修改账户、聊天记录和阅读偏好。 */
window.NavrenApp = (() => {
  const release = document.querySelector('meta[name="navren-release"]')?.content;
  const pages = new Set(['index.html', 'chat.html', 'recommend.html', 'account.html', 'mine.html', 'copilot.html']);
  function href(target, mode) {
    const url = new URL(target, location.href);
    if (url.origin === location.origin && pages.has(url.pathname.split('/').pop())) {
      if (release) url.searchParams.set('ui', release);
      if (mode) url.searchParams.set('mode', mode);
      return url.pathname + url.search + url.hash;
    }
    return url.href;
  }
  function navigate(target, mode) { location.assign(href(target, mode)); }
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (link) link.href = href(link.href);
  }, true);
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
        location.replace(url.pathname + url.search + url.hash);
      }
    } catch (_) { /* 服务暂时离线时保留当前页面。 */ }
    finally { checking = false; }
  }
  window.addEventListener('pageshow', checkRelease);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') checkRelease();
  });
  return {release, href, navigate};
})();
