/** 首屏/子视图的展示就绪门。只管理内容可见性，不发业务请求或授予权限。 */
(() => {
  'use strict';
  const page = location.pathname.split('/').pop()?.replace(/\.html$/, '');
  if (!['chat', 'account', 'recommend', 'mine', 'messages'].includes(page)) return;
  const root = document.documentElement;
  root.classList.add('navren-shell');
  root.dataset.navrenPageLoad = 'loading';
  let paint = null;
  try {
    const value = JSON.parse(localStorage.getItem('navren:paint-layout:v1') || 'null');
    if (value && ['classic', 'senior', 'youth'].includes(value.mode) && Number.isInteger(value.fontSize) && value.fontSize >= 14 && value.fontSize <= 24) {
      paint = Object.freeze({mode:value.mode, fontSize:value.fontSize});
      const n = paint.fontSize;
      root.style.fontSize = n + 'px';
      for (const [key, px] of Object.entries({'font-size-base':n,'font-size-msg':n,'font-size-sm':n-1,'font-size-xs':Math.max(13,n-3),'font-size-lg':n+4,'font-size-xl':n+10,'fs-base':n,'fs-sm':n-1,'fs-lg':n+4,'fs-xl':n+10})) root.style.setProperty('--' + key, px + 'px');
    }
  } catch (_) { /* 仅展示缓存；权限与偏好仍由原Storage链核验。 */ }
  function paintBody() {
    if (!document.body) return false;
    if (paint) document.body.classList.add('mode-' + paint.mode);
    return true;
  }
  if (!paintBody()) {
    const observer = new MutationObserver(() => { if (paintBody()) observer.disconnect(); });
    observer.observe(root, {childList:true});
  }

  let serial = 0, active = null, layer = null, main = null, header = null;
  let shellReady = false, mounted = false, hidden = false, slowTimer = null, bootEpoch = 0;
  let shellError = null, resizeObserver = null, languageObserver = null;
  const frames = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const english = () => root.lang === 'en' || root.dataset.language === 'en';
  const text = (zh, en) => english() ? en : zh;
  const chat = page === 'chat';
  function geometry() {
    if (!main || !header || !layer) return;
    const m = main.getBoundingClientRect(), h = header.getBoundingClientRect();
    layer.style.top = Math.max(0, h.bottom - m.top) + 'px';
  }
  function syncCopy() {
    if (!layer) return;
    const failed = active?.error, slow = active?.slow, identityChanged = active?.reason === 'PAGE_IDENTITY_CHANGED';
    layer.querySelector('.npl-title').textContent = failed ? (chat ? identityChanged ? text('登录状态已变化', 'Your sign-in state has changed') : text('对话暂时无法载入', 'This conversation could not load') : text('页面暂时未能载入', 'This page could not load')) : chat ? text('正在准备对话', 'Preparing your conversation') : text('正在载入', 'Loading');
    const description = layer.querySelector('.npl-description');
    if (description) description.textContent = failed ? '' : text('历史消息与附件就绪后，将一起呈现', 'Your history and attachments will appear together when ready');
    layer.querySelector('.npl-note').textContent = failed ? (identityChanged ? text('请重新进入对话后继续。', 'Reopen the conversation to continue.') : text('请重新加载，核对当前页面。', 'Reload to check the current page.')) : slow ? text('读取时间有些长，可以重新加载。', 'This is taking longer than usual. You can reload.') : '';
    layer.querySelector('.npl-retry').textContent = identityChanged ? text('重新进入对话', 'Reopen conversation') : text('重新加载', 'Reload');
    layer.querySelector('.npl-retry').hidden = !failed && !slow;
    layer.dataset.state = failed ? 'error' : 'loading';
  }
  function mount() {
    if (mounted) return;
    main = document.querySelector('.app > .main');
    header = main?.querySelector(':scope > .header');
    if (!main || !header) return;
    mounted = true;
    layer = main.querySelector(':scope > .navren-page-loader') || document.createElement('section');
    layer.className = 'navren-page-loader';
    if (chat) layer.dataset.nplPage = 'chat';
    layer.dataset.i18nSkip = '';
    layer.setAttribute('aria-label', text('页面加载', 'Page loading'));
    if (chat) layer.innerHTML = "<div class=\"npl-stage npl-chat-stage\"><div class=\"npl-chat-art\" aria-hidden=\"true\"><svg class=\"npl-mark\" viewBox=\"0 0 72 64\" aria-hidden=\"true\"><path class=\"npl-wave npl-wave-back\" d=\"M9 37C19 37 23 30 31 23S46 12 63 12\"/><path class=\"npl-wave npl-wave-front\" d=\"M9 51C25 51 29 43 38 36S51 26 63 26\"/></svg><div class=\"npl-chat-sheet\"><i></i><i></i><i></i></div><span class=\"npl-chat-dot\"></span></div><p class=\"npl-title\" role=\"status\" aria-live=\"polite\">正在准备对话</p><p class=\"npl-description\">历史消息与附件就绪后，将一起呈现</p><p class=\"npl-note\"></p><a class=\"npl-retry\" href=\"\">重新加载对话页</a></div>";
    else layer.innerHTML = '<div class="npl-stage"><svg class="npl-mark" viewBox="0 0 72 64" aria-hidden="true"><path class="npl-wave npl-wave-back" d="M9 37C19 37 23 30 31 23S46 12 63 12"/><path class="npl-wave npl-wave-front" d="M9 51C25 51 29 43 38 36S51 26 63 26"/></svg><p class="npl-title" role="status" aria-live="polite"></p><p class="npl-note"></p><button class="npl-retry" type="button" hidden></button></div>';
    layer.querySelector('.npl-retry').addEventListener('click', event => { event.preventDefault(); location.reload(); });
    main.append(layer);
    main.setAttribute('aria-busy', 'true');
    syncCopy(); geometry();
    if (typeof ResizeObserver === 'function') {
      resizeObserver = new ResizeObserver(geometry);
      resizeObserver.observe(header); resizeObserver.observe(main);
    }
    window.addEventListener('resize', geometry);
  }
  function stopTimer() { clearTimeout(slowTimer); slowTimer = null; }
  function begin() {
    if (active && !active.finished) { active.finished = true; active.resolve(false); }
    stopTimer();
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    const turn = {id:++serial, ready:false, error:null, slow:false, finished:false, revealing:false, resolve, promise};
    active = turn;
    root.dataset.navrenPageLoad = 'loading';
    if (layer) layer.hidden = false;
    main?.setAttribute('aria-busy', 'true');
    syncCopy(); geometry();
    // 超时只给出重试入口，绝不把半成品当作已就绪揭开。
    slowTimer = setTimeout(() => { if (active === turn && !turn.finished) { turn.slow = true; syncCopy(); } }, 15000);
    return lease(turn);
  }
  function lease(turn) {
    return Object.freeze({
      ready() { if (active !== turn || hidden) return Promise.resolve(false); turn.ready = true; maybeReveal(turn); return turn.promise; },
      fail(error) { if (active === turn && !hidden) fail(error); }
    });
  }
  function join() { return active && !active.finished ? lease(active) : begin(); }
  function fail(_error) {
    if (!active || hidden) return;
    stopTimer(); active.error = true; active.reason = _error?.code;
    root.dataset.navrenPageLoad = 'error';
    if (layer) layer.hidden = false;
    syncCopy();
    if (!active.finished) { active.finished = true; active.resolve(false); }
  }
  async function settleImages(turn) {
    // lazy内容保留其原尺寸与按需读取；只等当前已挂载、已开始加载的图片。
    const images = [...main.querySelectorAll('img')].filter(image => !header.contains(image) && image.loading !== 'lazy' && image.getAttribute('src'));
    await Promise.all(images.map(image => image.complete ? Promise.resolve() : new Promise(resolve => {
      const done = () => { image.removeEventListener('load', done); image.removeEventListener('error', done); resolve(); };
      image.addEventListener('load', done, {once:true}); image.addEventListener('error', done, {once:true});
      if (image.complete || active !== turn) done();
    })));
  }
  async function maybeReveal(turn) {
    if (!mounted || !shellReady || hidden || active !== turn || !turn.ready || turn.error || turn.revealing || turn.finished) return;
    turn.revealing = true;
    try {
      await frames();
      if (document.fonts?.ready) await document.fonts.ready;
      await settleImages(turn);
      await frames();
      if (hidden || active !== turn || turn.error || turn.finished) return;
      geometry(); stopTimer();
      root.dataset.navrenPageLoad = 'ready';
      layer.hidden = true;
      main.removeAttribute('aria-busy');
      turn.finished = true; turn.resolve(true);
      window.dispatchEvent(new CustomEvent('navren:page-ready', {detail:{page, generation:turn.id}}));
    } catch (error) { if (active === turn) fail(error); }
  }
  function languageReady() {
    if (root.dataset.i18nReady === 'true') return Promise.resolve();
    return new Promise(resolve => {
      languageObserver = new MutationObserver(() => { if (root.dataset.i18nReady === 'true') { languageObserver.disconnect(); resolve(); } });
      languageObserver.observe(root, {attributes:true, attributeFilter:['data-i18n-ready']});
    });
  }
  async function boot() {
    const epoch = ++bootEpoch, turn = active;
    mount();
    try {
      if (typeof Storage === 'undefined' || typeof Storage.ready !== 'function') throw new Error('PAGE_SETTINGS_UNAVAILABLE');
      await Promise.all([Storage.ready({refresh:false}), languageReady()]);
      if (epoch !== bootEpoch || (chat && active !== turn) || hidden) return;
      shellError = null; shellReady = true;
      syncCopy(); geometry();
      // 账户/发现/消息/我的没有数据就绪回调，壳层与语言同步完成即可揭开；对话页仍等 chat.js 调 ready()。
      if (!chat && active === turn) turn.ready = true;
      if (shellError) fail(shellError); else if (active) maybeReveal(active);
    } catch (error) { if (epoch === bootEpoch && (!chat || active === turn) && !hidden) { shellError = error; fail(error); } }
  }
  function resume() {
    if (!chat || (active && !active.finished && !active.error)) return join();
    const token = begin(); shellReady = false; queueMicrotask(boot); return token;
  }
  function invalidate() {
    if (!chat || hidden) return;
    begin(); shellReady = false; fail({code:'PAGE_IDENTITY_CHANGED'});
  }
  window.NavrenPageLoad = Object.freeze({begin, join, resume, fail, invalidate, initialPaint:paint, getState:() => ({phase:root.dataset.navrenPageLoad, generation:active?.id || 0})});
  begin();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once:true}); else boot();
  window.addEventListener('navren:language', syncCopy);
  for (const event of ['navren:identity-revoked', 'navren:account-change', 'navren:account-switched', 'navren:privacy-reset']) window.addEventListener(event, () => {
    if (hidden) return;
    if (chat) { invalidate(); return; }
    if (event === 'navren:privacy-reset') return;
    begin(); shellReady = false; Promise.resolve().then(boot);
  });
  window.addEventListener('pagehide', () => { hidden = true; bootEpoch++; stopTimer(); if (active && !active.finished) { active.finished = true; active.resolve(false); } });
  window.addEventListener('pageshow', event => { if (event.persisted) { hidden = false; shellReady = false; begin(); queueMicrotask(boot); } });
})();
