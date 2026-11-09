// 仅由同源欢迎网关注入。欢迎主体与业务身份分别核验；事件不授予工作区权限。
(() => {
  const entry = '/?view=experience';
  let enabled = false;
  try { enabled = new URL(document.currentScript?.src || '', location.href).searchParams.get('contract') === 'welcome-business-v1'; } catch (_) {}
  if (!enabled) {
    // 静态文件可能先于认证服务发布。旧注入不调用欢迎-only logout，也不撤销现役界面。
    const unsupported = Object.freeze({status: 'unsupported_contract', authenticated: null, registered_user_id:null, auth_subject: null,
      current_business_identity: null, business_identity_status: 'unavailable', revoked: false, business_revoked: false,
      logout_status: 'unavailable', error: '退出接线待更新，请稍后重试'});
    window.NavrenWelcomeSession = Object.freeze({
      refresh: () => Promise.resolve(unsupported),
      getState: () => unsupported,
      logout: button => {
        if (button) {
          button.textContent = unsupported.error;
          button.disabled = false;
          button.setAttribute('aria-busy', 'false');
        }
        return Promise.resolve(false);
      },
      returnToExperienceSelection: () => location.replace(entry)
    });
    return;
  }
  const accounts = new Set(['tycoon', 'grain_farmer', 'working_landlady', 'retired_entrepreneur']);
  let epoch = 0, subject = null, registeredUser = null, flight = null, logoutFlight = null, channel;
  let revoked = false, businessRevoked = false, paused = false, navigated = false, lastIdentity = null;
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, options) => {
    const target = typeof input === 'string' ? input : input?.url || input?.href;
    if (revoked || businessRevoked) {
      let url;
      try { url = new URL(target, location.href); } catch (_) {}
      if (url?.origin === location.origin && url.pathname.startsWith('/api/') && !url.pathname.startsWith('/api/welcome/auth/')) {
        const error = new Error('当前账户已失效，业务请求已停止。');
        error.code = 'WELCOME_IDENTITY_REVOKED'; error.status = 401;
        return Promise.reject(error);
      }
    }
    return originalFetch(input, options);
  };
  async function boundedJSON(request, path, options) {
    let timeout, onAbort;
    const interrupted = new Promise((_, reject) => {
      onAbort = () => reject(new Error('welcome_request_interrupted'));
      request.controller.signal.addEventListener('abort', onAbort, {once: true});
    });
    timeout = setTimeout(() => request.controller.abort(), 15000);
    try {
      return await Promise.race([(async () => {
        const response = await fetch(path, {...options, signal: request.controller.signal});
        return {response, data: response.ok ? await response.json() : null};
      })(), interrupted]);
    } finally {
      clearTimeout(timeout);
      request.controller.signal.removeEventListener('abort', onAbort);
    }
  }
  const logoutButtons = new Set(), buttonLabels = new WeakMap();
  const copy = value => {
    if (Array.isArray(value)) return Object.freeze(value.map(copy));
    if (value && typeof value === 'object') return Object.freeze(Object.fromEntries(Object.entries(value).map(([k, v]) => [k, copy(v)])));
    return value;
  };
  let state = copy({authenticated: false, registered_user_id:null, auth_subject: null, mode: null, user: null, csrf: null,
    entry_experience_account: null, experience_account: null, current_business_identity: null,
    business_identity_status: 'unavailable', status: 'checking', revoked: false, business_revoked: false,
    logout_status: 'idle', error: null, epoch});
  const update = fields => { state = copy({...state, ...fields, epoch}); return state; };
  const getState = () => {
    if(state.current_business_identity&&!leaseCurrent(state.current_business_identity))revoke('business_lease_expired');
    return state;
  };
  const signal = (name, detail) => window.dispatchEvent(new CustomEvent(name, {detail: copy(detail)}));
  const descriptor = value => value && accounts.has(value.user_id) && typeof value.display_name === 'string'
    ? copy({user_id: value.user_id, display_name: value.display_name}) : null;

  function leaseCurrent(value){
    return Number.isSafeInteger(value?.session_generation)&&value.session_generation>=0&&
      Number.isFinite(value.auth_expires_at)&&Number.isFinite(value.lease_expires_at)&&
      Date.now()/1000<value.lease_expires_at&&value.lease_expires_at<=value.auth_expires_at;
  }

  function businessIdentity(data) {
    const value = data.current_business_identity;
    if (data.business_identity_status !== 'verified' || !value || value.environment !== 'simulation' ||
      !accounts.has(value.account_id) || value.registered_user_id!==data.registered_user_id ||
      value.auth_subject!==data.auth_subject || typeof value.workspace_id !== 'string' || !/^[a-f0-9]{32}$/.test(value.workspace_id) ||
      !Number.isSafeInteger(value.privacy_revision) || value.privacy_revision < 0||!leaseCurrent(value)) return null;
    const next = {environment: 'simulation', registered_user_id:value.registered_user_id,auth_subject:value.auth_subject,account_id: value.account_id, workspace_id: value.workspace_id,
      privacy_revision: value.privacy_revision,session_generation:value.session_generation,
      auth_expires_at:value.auth_expires_at,lease_expires_at:value.lease_expires_at};
    for (const key of ['personal_revision', 'generation']) {
      if (value[key] != null) {
        if (!Number.isSafeInteger(value[key]) || value[key] < 0 || (key==='generation'&&value[key]!==value.session_generation)) return null;
        next[key] = value[key];
      }
    }
    if (typeof value.display_name === 'string') next.display_name = value.display_name;
    return copy(next);
  }
  function navigateToEntry() {
    if (navigated) return;
    navigated = true;
    location.replace(entry);
  }
  function cancelRefresh() {
    epoch++;
    const old = flight;
    flight = null;
    old?.controller.abort();
  }
  function isLogoutButton(element) {
    return (element && buttonLabels.has(element)) || element?.matches?.('[data-navren-logout]') || (element?.tagName === 'BUTTON' &&
      /^\s*logout\s*\(\s*\)\s*;?\s*$/.test(element.getAttribute('onclick') || ''));
  }
  function privateGate() {
    document.documentElement.dataset.navrenWelcomeRevoked = 'true';
    const allowed = [...document.querySelectorAll('[data-navren-logout],button[onclick]')].filter(isLogoutButton);
    for (const button of logoutButtons) if (button.isConnected) allowed.push(button);
    // 保留现有退出控件可操作，失败后可重试；其他旧资料交互立即停用。
    function lock(element) {
      if (allowed.some(button => element === button)) return;
      if (allowed.some(button => element.contains(button))) {
        for (const child of element.children) lock(child);
      } else element.inert = true;
    }
    document.querySelectorAll('.page-scroll,.chat-messages,.chat-input-area,[data-navren-private]').forEach(lock);
  }
  function stopPrivateActivity() {
    window.BankClient?.beginAccountSwitch?.();
    window.NavrenSpeech?.stop?.();
    document.querySelectorAll('audio,video').forEach(media => { try { media.pause(); } catch (_) {} });
    try { Storage.unloadPrivateContext?.(); } catch (_) {}
    for (const key of ['navren_return_view', 'navren_research_selection', 'navren_calendar_question']) {
      try { sessionStorage.removeItem(key); } catch (_) {}
    }
    signal('navren:privacy-reset', {scope: 'welcome_subject', reason:'session_unloaded', persistent_history_deleted:false, epoch});
  }
  function revoke(reason, navigate = true) {
    cancelRefresh();
    const first = !revoked;
    revoked = true;
    update({authenticated: false, registered_user_id:null, csrf: null, current_business_identity: null, experience_account: null,
      business_identity_status: 'revoked', status: 'revoked', revoked: true, error: reason});
    privateGate();
    if (first) {
      stopPrivateActivity();
      signal('navren:identity-revoked', {scope: 'welcome_subject', reason, epoch});
    }
    if (navigate) navigateToEntry();
  }
  function applySession(data) {
    if (data?.authenticated !== true || typeof data.auth_subject !== 'string' || !/^[A-Za-z0-9:_-]{16,192}$/.test(data.auth_subject)) {
      revoke('welcome_session_revoked'); return state;
    }
    if (typeof data.registered_user_id!=='string'||!/^[a-f0-9]{32}$/.test(data.registered_user_id)) { revoke('registered_user_missing'); return state; }
    if (registeredUser && data.registered_user_id!==registeredUser) { revoke('registered_user_changed'); return state; }
    if (subject && data.auth_subject !== subject) { revoke('welcome_subject_changed'); return state; }
    subject = data.auth_subject; registeredUser=data.registered_user_id;
    const identity = businessIdentity(data), previous = state.current_business_identity;
    const status = identity ? 'verified' : ['not_connected', 'unavailable', 'revoked'].includes(data.business_identity_status)
      ? data.business_identity_status : 'unavailable';
    const alias = descriptor(data.experience_account);
    const businessWasRevoked = status === 'revoked' && !businessRevoked;
    const businessChanged = identity && lastIdentity && JSON.stringify(lastIdentity) !== JSON.stringify(identity);
    if (businessWasRevoked) businessRevoked = true;
    if (businessWasRevoked || businessChanged) epoch++;
    if (identity) lastIdentity = identity;
    update({authenticated: true, registered_user_id:registeredUser,auth_subject: subject, csrf: typeof data.csrf === 'string' ? data.csrf : null,
      mode: ['local', 'experience'].includes(data.mode) ? data.mode : null, user: data.user || null,
      entry_experience_account: descriptor(data.entry_experience_account),
      experience_account: identity && alias?.user_id === identity.account_id ? alias : null,
      current_business_identity: identity, business_identity_status: status, business_revoked: businessRevoked,
      status: status === 'revoked' ? 'business_revoked' : 'ready', error: null});
    if (businessWasRevoked) {
      privateGate();
      stopPrivateActivity();
      signal('navren:identity-revoked', {scope: 'business_identity', reason: 'business_session_revoked', epoch});
    } else if (businessChanged) {
      // 当前 Cookie 的业务画像变了；清理旧资料，不改变仍有效的欢迎主体。
      stopPrivateActivity();
    }
    if (identity && JSON.stringify(previous) !== JSON.stringify(identity)) {
      signal('navren:business-identity', {registered_user_id:registeredUser,auth_subject: subject, current_business_identity: identity,
        business_identity_status: 'verified', epoch});
    }
    return state;
  }
  function refresh() {
    if (revoked || paused) return Promise.resolve(state);
    if (flight) return flight.promise;
    const request = {epoch, controller: new AbortController()};
    flight = request;
    update({status: 'checking', error: null});
    request.promise = (async () => {
      try {
        const {response, data} = await boundedJSON(request, '/api/welcome/auth/session', {credentials: 'same-origin', cache: 'no-store'});
        if (request.epoch !== epoch || revoked || paused) return state;
        if (response.status === 401) { revoke('welcome_session_401'); return state; }
        if (!response.ok) throw new Error('session_unavailable');
        if (request.epoch !== epoch || revoked || paused) return state;
        return applySession(data);
      } catch (_) {
        if (request.epoch === epoch && !revoked && !paused) update({current_business_identity: null, experience_account: null,
          business_identity_status: 'unavailable', status: 'unavailable', error: '欢迎会话暂时无法核对，请重试。'});
        return state;
      } finally { if (flight === request) flight = null; }
    })();
    return request.promise;
  }
  function setButton(button, busy, failed = false) {
    if (!button) return;
    if (!buttonLabels.has(button)) buttonLabels.set(button, button.textContent);
    button.disabled = busy;
    button.setAttribute('aria-busy', String(busy));
    button.textContent = failed ? '退出未成功，点击重试' : busy ? '正在退出…' : buttonLabels.get(button);
  }
  function logout(button) {
    if (button) { logoutButtons.add(button); setButton(button, true); }
    if (logoutFlight) return logoutFlight.promise;
    revoke('logout_started', false);
    update({logout_status: 'pending', error: null});
    const request = {epoch, controller: new AbortController()};
    logoutFlight = request;
    request.promise = (async () => {
      try {
        // 退出失败后欢迎 Cookie 仍可存在。仅取新 CSRF，不恢复已撤销的客户端视图。
        const {response, data: session} = await boundedJSON(request, '/api/welcome/auth/session', {credentials: 'same-origin', cache: 'no-store'});
        if (request.epoch !== epoch || paused) return false;
        if (response.status === 401) { revoke('welcome_session_401'); return false; }
        if (!response.ok) throw new Error('session_unavailable');
        if (request.epoch !== epoch || paused) return false;
        if (session?.authenticated !== true || typeof session.auth_subject !== 'string' || !/^[A-Za-z0-9:_-]{16,192}$/.test(session.auth_subject)) { revoke('welcome_session_revoked'); return false; }
        if (registeredUser&&session.registered_user_id!==registeredUser) { revoke('registered_user_changed'); return false; }
        if (subject && session.auth_subject !== subject) { revoke('welcome_subject_changed'); return false; }
        subject = session.auth_subject;
        if (typeof session.csrf !== 'string' || !session.csrf) throw new Error('csrf_unavailable');
        const {response: result, data} = await boundedJSON(request, '/api/welcome/auth/logout', {method: 'POST', credentials: 'same-origin', cache: 'no-store',
          headers: {'Content-Type': 'application/json', 'X-Welcome-CSRF': session.csrf}, body: '{}'});
        if (request.epoch !== epoch || paused) return false;
        if (result.status === 401) { revoke('welcome_session_401'); return false; }
        if (!result.ok) throw new Error('logout_failed');
        if (request.epoch !== epoch || paused) return false;
        if (data.authenticated !== false || data.scope !== 'welcome_subject' || data.business_requests_revoked !== true) throw new Error('logout_unverified');
        update({auth_subject: null, user: null, entry_experience_account: null, logout_status: 'complete', error: null});
        channel?.postMessage({action: 'logout', auth_subject: subject});
        navigateToEntry();
        return true;
      } catch (_) {
        if (request.epoch === epoch && !paused) update({logout_status: 'failed', error: '退出未完成，旧界面已停用，请重试退出。'});
        return false;
      } finally {
        if (logoutFlight === request) {
          logoutFlight = null;
          for (const control of logoutButtons) setButton(control, state.logout_status === 'complete', state.logout_status !== 'complete');
          logoutButtons.clear();
        }
      }
    })();
    return request.promise;
  }
  function returnToExperienceSelection() { navigateToEntry(); }
  window.NavrenWelcomeSession = Object.freeze({refresh, getState, logout, returnToExperienceSelection});
  // 兼容原业务按钮的假 logout；不新增按钮、不改入口视觉。
  document.addEventListener('click', event => {
    const control = event.target?.closest?.('button,[data-navren-logout]');
    if (isLogoutButton(control)) {
      event.preventDefault(); event.stopImmediatePropagation(); logout(control);
    } else if ((revoked || businessRevoked) && event.target?.closest?.('.page-scroll,.chat-messages,.chat-input-area,[data-navren-private]')) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  try {
    channel = new BroadcastChannel('navren-welcome-auth');
    channel.onmessage = event => {
      const message = event.data;
      if (message?.action === 'logout' && (!message.auth_subject || !subject || message.auth_subject === subject)) revoke('welcome_logout_broadcast');
      else refresh();
    };
  } catch (_) { /* focus/pageshow 仍会重新核验。 */ }
  window.addEventListener('focus', refresh);
  window.addEventListener('pageshow', () => { paused = false; refresh(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('navren:account-switched', refresh);
  window.addEventListener('pagehide', event => {
    paused = true;
    cancelRefresh();
    const oldLogout = logoutFlight;
    logoutFlight = null;
    oldLogout?.controller.abort();
    if (oldLogout) {
      update({logout_status: 'failed', error: '退出确认已中断，请重试核对。'});
      for (const control of logoutButtons) setButton(control, false, true);
      logoutButtons.clear();
    }
    update({current_business_identity: null, experience_account: null, status: revoked ? 'revoked' : 'paused'});
    window.NavrenSpeech?.stop?.();
    if (!event.persisted) channel?.close();
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', refresh, {once: true});
  else refresh();
})();
