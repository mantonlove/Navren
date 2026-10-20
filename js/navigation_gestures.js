/** Shared back gesture. Wheel events expose motion, not a trackpad finger count. */
(() => {
  'use strict';
  if (window.NavrenNavigationGestures) return;
  window.NavrenNavigationGestures = Object.freeze({ enabled: true });

  const IDLE = 220, COMMIT_DELAY = 80, LOCK = 700, DISTANCE = 100, DURATION = 40;
  const LOCK_KEY = 'navren_back_gesture_until';
  const PAGES = new Set(['index.html', 'account.html', 'mine.html', 'recommend.html', 'chat.html', 'copilot.html']);
  const EXCLUDED = 'input,select,textarea,[contenteditable]:not([contenteditable="false"]),canvas,svg,.fg-view,.fg-canvas,.market-deck,[data-deck-drag-surface],[draggable="true"],[data-navren-gesture="ignore"]';
  let wheel = null, touch = null, timer = null, cue = null, lockedUntil = 0;

  function savedLock() {
    try {
      const until = Number(sessionStorage.getItem(LOCK_KEY));
      const remaining = until - Date.now();
      return remaining > 0 && remaining <= LOCK * 2 ? until : 0;
    } catch (_) { return 0; }
  }
  function lock() {
    lockedUntil = Date.now() + LOCK;
    try { sessionStorage.setItem(LOCK_KEY, String(lockedUntil)); } catch (_) {}
  }
  function isLocked() {
    lockedUntil = Math.max(lockedUntil, savedLock());
    return lockedUntil > Date.now();
  }
  function visible(element) {
    if (!element || element.hidden || element.closest('[hidden],.hidden,[inert]')) return false;
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' && element.getClientRects().length > 0;
  }
  function editable(element) {
    return element?.matches('textarea,input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]):not([type="reset"]):not([type="hidden"]),[contenteditable]:not([contenteditable="false"])');
  }
  function blocked() {
    if (document.hidden || document.documentElement.classList.contains('navren-private-locked')) return true;
    if (document.querySelector('form[data-navren-dirty="true"]')) return true;
    if ([...document.querySelectorAll('form[aria-busy="true"]')].some(visible)) return true;
    if ([...document.querySelectorAll('dialog[open],[aria-modal="true"],[role="dialog"],.modal-overlay,#modalOverlay,#navrenIdentityGate')].some(visible)) return true;
    const active = document.activeElement;
    if (editable(active) && String(active.value ?? active.textContent ?? '').trim()) return true;
    return false;
  }
  function targetOf(event) {
    let target = event.composedPath?.()[0] || event.target;
    if (target?.nodeType !== 1) target = target?.parentElement || document.body;
    return target;
  }
  function excluded(target) {
    if (!target || target.closest(EXCLUDED)) return true;
    for (let element = target; element; element = element.parentElement) {
      // A viewport's overflow is not an intentional horizontal control.
      if (element === document.documentElement || element === document.body || element.matches('.page-scroll,.main,.app')) continue;
      const style = getComputedStyle(element);
      if (/^(auto|scroll)$/.test(style.overflowX) && element.scrollWidth > element.clientWidth + 1) return true;
    }
    return false;
  }
  function internalReferrer() {
    try {
      const previous = new URL(document.referrer);
      return previous.origin === location.origin && (PAGES.has(previous.pathname.split('/').pop()) || previous.pathname === '/') && previous.pathname.slice(0, previous.pathname.lastIndexOf('/')) === location.pathname.slice(0, location.pathname.lastIndexOf('/'));
    } catch (_) { return false; }
  }
  function destination() {
    const current = window.NavrenViews?.current?.();
    const internalHistory = history.state?.navrenDepth > 0 || internalReferrer() && history.length > 1;
    if (current && typeof window.NavrenViews.back === 'function') {
      if (internalHistory || current.view !== 'home') return () => window.NavrenViews.back();
      return null;
    }
    return internalHistory ? () => history.back() : null;
  }
  function snapshot(target) {
    return { target, href: location.href, revision: window.NavrenViews?.revision?.() };
  }
  function valid(sequence) {
    return sequence && sequence.href === location.href && sequence.revision === window.NavrenViews?.revision?.() && sequence.target.isConnected && !blocked() && !excluded(sequence.target);
  }
  function feedback(progress) {
    if (!cue) {
      cue = document.createElement('div');
      cue.className = 'navren-back-gesture';
      cue.setAttribute('aria-hidden', 'true');
      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      icon.setAttribute('viewBox', '0 0 24 24');
      icon.setAttribute('focusable', 'false');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M19 12H5m7-7-7 7 7 7');
      icon.append(path);
      cue.append(icon);
      document.body.append(cue);
    }
    cue.hidden = false;
    const amount = Math.min(1, Math.max(0, progress));
    cue.style.setProperty('--gesture-progress', String(amount));
    cue.dataset.ready = String(amount === 1);
  }
  function hide() { if (cue) cue.hidden = true; }
  function clearTimer() { if (timer !== null) clearTimeout(timer); timer = null; }
  function abort() {
    clearTimer();
    if (wheel) wheel.mode = 'ignored';
    if (touch) touch.mode = 'ignored';
    hide();
  }
  function commit(sequence) {
    if (!valid(sequence) || isLocked()) return;
    const back = destination();
    if (!back) return;
    clearTimer(); hide(); wheel = null;
    if (touch) touch.mode = 'ignored';
    lock();
    back();
  }
  function idle() {
    clearTimer();
    const ready = wheel?.mode === 'candidate' && wheel.distance >= DISTANCE && wheel.count >= 2;
    timer = setTimeout(() => {
      timer = null;
      const sequence = wheel;
      if (sequence?.mode === 'candidate' && sequence.distance >= DISTANCE && sequence.count >= 2) commit(sequence);
      wheel = null;
      hide();
    }, ready ? COMMIT_DELAY : IDLE);
  }
  function wheelEvent(event) {
    const now = Date.now();
    if (isLocked()) { lock(); abort(); return; }
    if (touch) { abort(); return; }
    if (wheel && now - wheel.last > IDLE) { clearTimer(); wheel = null; hide(); }
    const target = targetOf(event), dx = event.deltaX, dy = event.deltaY;
    // Trackpads often begin at zero/subpixel motion and end with neutral momentum frames.
    const finite = Number.isFinite(dx) && Number.isFinite(dy);
    const neutral = finite && Math.abs(dx) < 4 && Math.abs(dy) < 4;
    const horizontal = finite && dx < 0 && Math.abs(dx) >= Math.abs(dy) * 2;
    // Some browsers expose only the first event in a wheel burst as cancelable.
    const ownsBurst = wheel?.mode === 'candidate' || wheel?.mode === 'pending' && wheel.firstCancelable;
    const eligible = finite && (event.cancelable || ownsBurst) && !event.defaultPrevented && event.deltaMode === 0 && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey;
    if (!wheel) wheel = { ...snapshot(target), mode: eligible && (horizontal || neutral) && !blocked() && !excluded(target) && destination() ? 'pending' : 'ignored', firstCancelable: event.cancelable, started: now, last: now, distance: 0, count: 0 };
    wheel.last = now;
    if (wheel.mode === 'ignored') { idle(); return; }
    if (!eligible || !valid(wheel) || excluded(target)) { abort(); idle(); return; }
    // Once ready, tiny momentum frames must not keep postponing the return.
    if (neutral && wheel.mode === 'candidate' && wheel.distance >= DISTANCE && wheel.count >= 2) return;
    if (!horizontal) {
      if (neutral) { if(wheel.mode === 'pending')idle(); return; }
      abort(); idle(); return;
    }
    wheel.mode = 'candidate';
    if (event.cancelable) event.preventDefault();
    wheel.distance -= dx;
    wheel.count++;
    feedback(wheel.distance / DISTANCE);
    idle();
  }
  function points(event) {
    return [...event.touches].map(point => ({ id: point.identifier, x: point.clientX, y: point.clientY, target: point.target }));
  }
  function separation(pair) { return Math.hypot(pair[0].x - pair[1].x, pair[0].y - pair[1].y); }
  function touchStart(event) {
    if (wheel) abort();
    if (touch) { abort(); return; }
    const pair = points(event), target = targetOf(event);
    const eligible = !isLocked() && event.cancelable && !event.defaultPrevented && pair.length === 2 && !blocked() && !excluded(target) && pair.every(point => !excluded(point.target || target)) && destination();
    touch = { ...snapshot(target), mode: eligible ? 'pending' : 'ignored', pair, separation: pair.length === 2 ? separation(pair) : 0, started: Date.now(), distance: 0, ready: false };
  }
  function touchMove(event) {
    if (!touch || touch.mode === 'ignored') return;
    const pair = points(event);
    if (!event.cancelable || event.defaultPrevented || pair.length !== 2 || !valid(touch)) { abort(); return; }
    const ordered = touch.pair.map(start => pair.find(point => point.id === start.id));
    if (ordered.some(point => !point) || Math.abs(separation(ordered) - touch.separation) > Math.max(10, touch.separation * .08)) { abort(); return; }
    const motion = ordered.map((point, index) => ({ x: point.x - touch.pair[index].x, y: point.y - touch.pair[index].y }));
    if (motion.every(point => Math.abs(point.x) < 4 && Math.abs(point.y) < 4)) return;
    if (motion.some(point => point.x <= 0 || Math.abs(point.y) > Math.max(6, point.x * .5)) || touch.lastPair && ordered.some((point, index) => point.x < touch.lastPair[index].x - 4)) { abort(); return; }
    touch.mode = 'candidate';
    touch.distance = (motion[0].x + motion[1].x) / 2;
    touch.lastPair = ordered;
    touch.ready = touch.distance >= DISTANCE && motion.every(point => point.x >= 80) && Date.now() - touch.started >= DURATION;
    event.preventDefault();
    feedback(touch.distance / DISTANCE);
  }
  function touchEnd(event) {
    if (!touch) return;
    const sequence = touch;
    if (event.touches.length < 2 && sequence.mode === 'candidate' && sequence.ready && event.cancelable && !event.defaultPrevented) commit(sequence);
    else hide();
    if (event.touches.length === 0) touch = null;
    else sequence.mode = 'ignored';
  }
  function start() {
    lockedUntil = savedLock();
    document.addEventListener('wheel', wheelEvent, { passive: false });
    document.addEventListener('touchstart', touchStart, { passive: false });
    document.addEventListener('touchmove', touchMove, { passive: false });
    document.addEventListener('touchend', touchEnd, { passive: false });
    document.addEventListener('touchcancel', () => { abort(); touch = null; }, { passive: true });
    document.addEventListener('visibilitychange', abort);
    document.addEventListener('pointerdown', () => { if (wheel) abort(); });
    document.addEventListener('keydown', abort);
    for (const name of ['blur', 'pagehide', 'popstate', 'hashchange', 'navren:view', 'navren:privacy-reset']) window.addEventListener(name, abort);
    window.addEventListener('pageshow', () => { abort(); lockedUntil = Math.max(lockedUntil, savedLock()); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
