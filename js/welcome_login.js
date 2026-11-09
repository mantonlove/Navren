// Phone login precedes experience selection. Both steps require a verified
// server response before opening the home route.
export function createWelcomeLogin({ canvas, onPhaseChange = () => {}, onTransition = () => {} }) {
  const stage = canvas.closest('.welcome-stage') || canvas.parentElement;
  const copy = document.querySelector('.welcome-copy');
  const byId = id => document.getElementById(id);
  const enter = byId('welcomeEnter');
  const frame = byId('welcomeSceneFrame') || canvas;
  const shell = byId('welcomeAuthShell');
  const panel = byId('welcomeLogin');
  const form = byId('welcomeLoginForm');
  const credentials = byId('welcomeCredentials');
  const button = byId('welcomeLoginSubmit');
  const label = button.querySelector('span');
  const phone = byId('welcomePhone');
  const password = byId('welcomePassword');
  const passwordField = byId('welcomePasswordField');
  const passwordConfirmation = byId('welcomePasswordConfirmation');
  const passwordConfirmationField = byId('welcomePasswordConfirmationField');
  const smsField = byId('welcomeSmsField');
  const smsCode = byId('welcomeSmsCode');
  const smsSend = byId('welcomeSmsSend');
  const loginMethods = byId('welcomeLoginMethods');
  const methodButtons = [...loginMethods.querySelectorAll('[data-login-method]')];
  const passwordToggle = byId('welcomePasswordToggle');
  const title = byId('welcomeLoginTitle');
  const intro = byId('welcomeLoginIntro');
  const status = byId('welcomeLoginStatus');
  const modeSwitch = byId('welcomeLoginSwitch');
  const experience = byId('welcomeExperience');
  const accounts = byId('welcomeExperienceAccounts');
  const experienceNotice = byId('welcomeExperienceNotice');
  const experienceReturn = byId('welcomeExperienceReturn');
  const accountInputs = [...accounts.querySelectorAll('input[type="radio"]')];
  let started = false, ready = false, busy = false, mode = 'login';
  let selectedAccount = '', csrf = '', sessionPromise;
  let authenticated = false, phoneVerified = false, registeredUserId = '', authSubject = '';
  const allowedViews = new Set(['login', 'register', 'experience', 'bind']);
  let smsConfigured = false, smsRegistrationRequired = true, smsConfigurationKnown = false, loginMethod = 'password';
  let smsChallenge = null, smsSending = false, smsTimer = null;
  const smsResendDeadlines = new Map();
  let currentEntry;
  let disposed = false, paused = false, requestEpoch = 0;
  const requests = new Set();
  const requestCurrent = turn => !disposed && !paused && turn === requestEpoch;
  async function requestJSON(url, options = {}) {
    const turn = requestEpoch, controller = new window.AbortController();
    if (!requestCurrent(turn)) throw new Error('本次验证已中断，请重新核对。');
    let rejectInterrupted;
    const interrupted = new Promise((_, reject) => { rejectInterrupted = reject; });
    const request = {cancel() { rejectInterrupted(new Error('本次验证已中断，请重新核对。')); controller.abort(); }};
    const timeout = window.setTimeout(() => {
      rejectInterrupted(new Error('请求超时，结果尚未确认，请重新核对后重试。'));
      controller.abort();
    }, 15000);
    requests.add(request);
    try {
      const result = await Promise.race([(async () => {
        const response = await fetch(url, {...options, signal: controller.signal});
        return {response, data: await response.json()};
      })(), interrupted]);
      if (!requestCurrent(turn) || controller.signal.aborted) throw new Error('本次验证已中断，请重新核对。');
      return result;
    } finally { window.clearTimeout(timeout); requests.delete(request); }
  }
  function interruptRequests() { requestEpoch++; for (const request of requests) request.cancel(); requests.clear(); }
  function pauseRequests() {
    paused = true; interruptRequests(); smsSending = false;
    password.value = ''; if (passwordConfirmation) passwordConfirmation.value = '';
    clearSms(); clearInterval(smsTimer); smsTimer = null; setBusy(false);
  }
  function resumeRequests() {
    if (disposed || !paused) return;
    paused = false; watchSmsDeadline();
    sessionPromise = loadSession({preserveStatus: true});
  }
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let entryAnimations = [], entering = false, entryRun = 0;

  function applyIdentity(data) {
    authenticated = data.authenticated === true;
    authSubject = authenticated && typeof data.auth_subject === 'string' ? data.auth_subject : '';
    phoneVerified = authenticated && data.sms?.phone_verified === true;
    registeredUserId = authenticated && /^[0-9a-f]{32}$/.test(data.registered_user_id || '') ? data.registered_user_id : '';
  }
  function canSelectExperience() { return authenticated && phoneVerified && !!registeredUserId; }
  function hasPrivateLease(data) {
    const identity = data.current_business_identity;
    return canSelectExperience() && data.redirect === '/chat.html' && data.business_identity_status === 'verified' &&
      identity?.registered_user_id === registeredUserId && identity.auth_subject === data.auth_subject &&
      /^[0-9a-f]{32}$/.test(identity.workspace_id || '') && accountInputs.some(input => input.value === identity.account_id);
  }
  function requireExperienceIdentity() {
    if (canSelectExperience()) return true;
    const view = authenticated ? 'bind' : 'login';
    recordView(view); showView(view);
    status.textContent = authenticated ? '请先完成手机号验证，再选择体验画像。' : '请先使用手机号注册并登录，再选择体验画像。';
    return false;
  }

  function trackEntryAnimation(animation) {
    animation.finished.catch(() => {});
    entryAnimations.push(animation);
    return animation;
  }

  function cancelEntryTransition() {
    const animations = entryAnimations;
    entryAnimations = [];
    entering = false;
    entryRun++;
    animations.forEach(animation => animation.cancel());
    onTransition('cancel');
    copy.inert = false;
    delete stage.dataset.transitioning;
    enter.disabled = false;
  }

  function viewFromURL() {
    const view = new URLSearchParams(location.search).get('view');
    return allowedViews.has(view) ? view : '';
  }

  function recordView(view, push = false) {
    const url = new URL(location.href);
    if (view) url.searchParams.set('view', view);
    else url.searchParams.delete('view');
    const depth = Number.isInteger(history.state?.navrenDepth) && history.state.navrenDepth >= 0 ? history.state.navrenDepth : 0;
    const state = { ...history.state, navrenWelcome: { view }, navrenDepth: depth + (push ? 1 : 0) };
    history[push ? 'pushState' : 'replaceState'](state, '', url.href);
    currentEntry = { state, href: url.href };
  }

  async function session({ preserveStatus = false } = {}) {
    const {response, data} = await requestJSON('/api/welcome/auth/session', { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error('登录服务暂时不可用，请稍后重试。');
    csrf = data.csrf || '';
    applyIdentity(data);
    smsConfigured = data.sms?.configured === true && !!smsField;
    smsRegistrationRequired = data.sms?.registration_required !== false;
    smsConfigurationKnown = true;
    if (!busy && (mode !== 'experience' || requireExperienceIdentity())) renderMode(preserveStatus);
    return data;
  }
  function loadSession(options = {}) {
    const turn = requestEpoch;
    return session(options).catch(error => {
      if (requestCurrent(turn) && (!options.preserveStatus || !status.textContent))
        status.textContent = error.message || '登录服务暂时无法核对，请稍后重试。';
      return null;
    });
  }
  sessionPromise = loadSession();

  function setPasswordVisible(visible) {
    password.type = visible ? 'text' : 'password';
    passwordToggle.setAttribute('aria-pressed', String(visible));
    passwordToggle.setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
  }

  function setBusy(value) {
    busy = value;
    form.setAttribute('aria-busy', String(value));
    button.disabled = modeSwitch.disabled = passwordToggle.disabled = value;
    experienceReturn.disabled = value || smsSending;
    phone.readOnly = password.readOnly = value;
    if (passwordConfirmation) passwordConfirmation.readOnly = value;
    if (smsCode) smsCode.readOnly = value || smsSending;
    methodButtons.forEach(tab => { tab.disabled = value || smsSending; });
    refreshSmsButton();
    accountInputs.forEach(input => { input.disabled = value || !canSelectExperience(); });
    experience.hidden = mode !== 'experience' || !canSelectExperience();
    experience.disabled = value || mode !== 'experience' || !canSelectExperience() || !selectedAccount;
  }

  function renderMode(preserveStatus = false) {
    const testing = mode === 'experience', register = mode === 'register', binding = mode === 'bind';
    const needsCode = needsSms();
    credentials.hidden = testing;
    accounts.hidden = !testing;
    if (experienceNotice) experienceNotice.hidden = !testing;
    button.hidden = testing;
    modeSwitch.hidden = testing || binding;
    experienceReturn.hidden = !testing && !binding;
    loginMethods.hidden = mode !== 'login';
    methodButtons.forEach(tab => {
      const selected = tab.dataset.loginMethod === loginMethod;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    if (mode === 'login') {
      credentials.setAttribute('role', 'tabpanel');
      credentials.setAttribute('aria-labelledby', methodButtons.find(tab => tab.dataset.loginMethod === loginMethod).id);
    } else {
      credentials.removeAttribute('role');
      credentials.removeAttribute('aria-labelledby');
    }
    title.textContent = testing ? '选择你的体验画像' : binding ? '验证你的手机号' : register ? '开启你的旅程' : '欢迎回来';
    intro.textContent = testing ? '每个画像都会保存你自己的聊天和操作。' : binding ? '验证后可选择自己的体验画像，并确认需要验证的操作。' : register ? '使用手机号，创建你的 Navren 账户。' : '登录，继续你的旅程。';
    document.title = testing ? 'Navren · 测试体验' : register ? 'Navren · 创建账户' : 'Navren · 登录';
    modeSwitch.textContent = register ? '已有账户？返回登录' : '首次使用？创建账户';
    if (smsField) smsField.hidden = !needsCode;
    if (smsCode) smsCode.required = needsCode;
    const passwordNeeded = !testing && !(mode === 'login' && loginMethod === 'sms');
    if (passwordField) passwordField.hidden = !passwordNeeded;
    password.required = passwordNeeded;
    if (passwordConfirmationField) passwordConfirmationField.hidden = !register;
    if (passwordConfirmation) {
      passwordConfirmation.required = register;
      passwordConfirmation.removeAttribute('aria-invalid');
      if (!register) passwordConfirmation.value = '';
    }
    password.autocomplete = register ? 'new-password' : 'current-password';
    password.placeholder = register ? '至少 8 位密码' : '输入密码';
    label.textContent = binding ? '验证手机号' : register ? '创建账户' : '登录';
    button.setAttribute('aria-label', binding ? '验证手机号' : register ? '创建账户并登录 Navren' : '登录 Navren');
    experience.textContent = '进入所选画像';
    experienceReturn.textContent = authenticated ? '退出登录' : '返回登录';
    experience.dataset.confirm = String(testing);
    panel.dataset.mode = mode;
    if (!preserveStatus) status.textContent = needsCode && smsConfigurationKnown && !smsConfigured ? smsUnavailableMessage() : '';
    phone.removeAttribute('aria-invalid');
    password.removeAttribute('aria-invalid');
    setPasswordVisible(false);
    setBusy(false);
  }

  function needsSms() {
    return mode === 'register' && smsRegistrationRequired || mode === 'bind' || mode === 'login' && loginMethod === 'sms';
  }
  function smsUnavailableMessage() {
    return mode === 'register' ? '短信服务暂不可用，请稍后再创建账户。' : mode === 'bind' ? '短信服务暂不可用，请稍后再验证手机号。' : '短信服务暂不可用，请稍后重试或使用密码登录。';
  }
  async function clearAuthenticatedSession() {
    if (!authenticated) return;
    const previousSubject = authSubject;
    const {response, data} = await requestJSON('/api/welcome/auth/logout', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Welcome-CSRF': csrf }, body: '{}' });
    if (!response.ok || data.authenticated !== false || data.scope !== 'welcome_subject' || data.business_requests_revoked !== true)
      throw new Error('登录状态未能更新，请稍后重试。');
    applyIdentity({}); csrf = ''; clearSms();
    selectedAccount = '';
    accountInputs.forEach(input => { input.checked = false; });
    delete panel.dataset.selectedAccount;
    try { const channel = new BroadcastChannel('navren-welcome-auth'); channel.postMessage({ action: 'logout', auth_subject: previousSubject }); channel.close(); } catch (_) {}
    sessionPromise = loadSession({ preserveStatus: true });
    const refreshed = await sessionPromise;
    if (!refreshed || authenticated || !csrf) throw new Error('登录状态未能更新，请刷新后重试。');
  }
  function smsResendKey() { return `${mode}:${phone.value.trim()}`; }
  function smsResendAt() { return smsResendDeadlines.get(smsResendKey()) || 0; }
  function clearSms() {
    smsChallenge = null;
    if (smsCode) smsCode.value = '';
    if (Date.now() >= smsResendAt()) { clearInterval(smsTimer); smsTimer = null; }
    refreshSmsButton();
  }
  function refreshSmsButton() {
    if (!smsSend) return;
    const wait = Math.max(0, Math.ceil((smsResendAt()-Date.now())/1000));
    smsSend.disabled = disposed || paused || busy || smsSending || wait > 0 || !smsConfigured;
    smsSend.textContent = smsSending ? '发送中' : wait ? `${wait}秒后重发` : '发送验证码';
  }
  function watchSmsDeadline() {
    clearInterval(smsTimer);
    if (Date.now() >= smsResendAt() && !smsChallenge) { smsTimer = null; return; }
    smsTimer = setInterval(() => {
      refreshSmsButton();
      if (smsChallenge && Date.now() >= smsChallenge.expiresAt) clearSms();
      if (!smsChallenge && Date.now() >= smsResendAt()) { clearInterval(smsTimer); smsTimer = null; }
    }, 1000);
  }
  async function sendSms() {
    const turn = requestEpoch;
    if (!requestCurrent(turn) || busy || smsSending || !ready || !needsSms() || !smsConfigured || Date.now() < smsResendAt()) return;
    const number = phone.value.trim();
    if (!/^1[3-9][0-9]{9}$/.test(number)) { status.textContent = '请输入正确的11位手机号。'; phone.focus(); return; }
    if (mode === 'bind' && password.value.length < 8) { status.textContent = '请先输入原密码。'; password.focus(); return; }
    clearSms(); smsSending = true; refreshSmsButton();
    methodButtons.forEach(tab => { tab.disabled = true; });
    experienceReturn.disabled = true;
    const purpose = mode;
    try {
      await sessionPromise;
      if (!csrf) await session();
      if (purpose === 'login' || purpose === 'register') await clearAuthenticatedSession();
      if (!requestCurrent(turn) || purpose !== mode || number !== phone.value.trim()) return;
      const payload = { phone: number, purpose };
      if (purpose === 'bind') payload.password = password.value;
      smsResendDeadlines.set(`${purpose}:${number}`, Date.now() + 60000);
      watchSmsDeadline(); refreshSmsButton();
      const {response, data} = await requestJSON('/api/welcome/auth/sms/send', { method:'POST', credentials:'same-origin',
        headers:{'Content-Type':'application/json','X-Welcome-CSRF':csrf}, body:JSON.stringify(payload) });
      if (!response.ok) throw new Error(data.error || '短信发送未成功，请稍后再试。');
      if (!requestCurrent(turn) || purpose !== mode || number !== phone.value.trim()) return;
      if (data.source !== 'provider' || !/^[a-f0-9]{32}$/.test(data.challenge_id) || !Number.isFinite(data.expires_at))
        throw new Error('短信验证资料无法核对，请重新发送。');
      smsChallenge = { id:data.challenge_id, phone:number, purpose, expiresAt:data.expires_at*1000 };
      status.textContent = '验证码已提交发送，请填写手机收到的六位验证码。';
      watchSmsDeadline();
      smsCode.focus();
    } catch (error) { if (requestCurrent(turn)) status.textContent = error.message || '短信暂时不可用。'; }
    finally {
      if (!requestCurrent(turn)) return;
      smsSending = false; refreshSmsButton();
      if (smsCode) smsCode.readOnly = busy;
      methodButtons.forEach(tab => { tab.disabled = busy; });
      experienceReturn.disabled = busy;
    }
  }
  function switchLoginMethod(method) {
    if (busy || smsSending || mode !== 'login' || loginMethod === method || !['password', 'sms'].includes(method)) return;
    loginMethod = method;
    password.value = '';
    clearSms();
    renderMode();
  }
  function loginMethodKeydown(event) {
    if (mode !== 'login' || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || busy || smsSending) return;
    event.preventDefault();
    const method = event.key === 'Home' ? 'password' : event.key === 'End' ? 'sms' : loginMethod === 'password' ? 'sms' : 'password';
    switchLoginMethod(method);
    methodButtons.find(tab => tab.dataset.loginMethod === method).focus();
  }
  function focusPhone() {
    if (window.matchMedia('(pointer: fine)').matches) phone.focus({ preventScroll: true });
    else { title.tabIndex = -1; title.focus({ preventScroll: true }); }
  }

  function showView(view, focus = true, duringTransition = false) {
    if (view === 'experience' && !requireExperienceIdentity()) return;
    clearSms();
    if (!duringTransition) cancelEntryTransition();
    if (!view) {
      started = ready = false;
      mode = 'login';
      renderMode();
      shell.hidden = panel.hidden = true;
      panel.inert = true;
      copy.hidden = false;
      stage.dataset.phase = 'welcome';
      delete canvas.dataset.loginReady;
      delete canvas.dataset.transitionStartedAt;
      delete canvas.dataset.transitionCompletedAt;
      document.title = 'Navren · 欢迎画面';
      onPhaseChange('welcome');
      if (focus) enter.focus({ preventScroll: true });
      return;
    }
    const firstEntry = !started;
    mode = view;
    started = ready = true;
    copy.hidden = true;
    shell.hidden = panel.hidden = false;
    panel.inert = false;
    stage.dataset.phase = 'login';
    renderMode();
    canvas.dataset.loginReady = 'true';
    if (firstEntry && !duringTransition) canvas.dataset.transitionStartedAt = String(performance.now());
    onPhaseChange('login');
    if (firstEntry && !duringTransition) canvas.dataset.transitionCompletedAt = String(performance.now());
    if (focus) {
      if (mode === 'experience') accountInputs[0].focus({ preventScroll: true });
      else focusPhone();
    }
  }

  function depart() {
    if (started || busy || entering) return;
    recordView('login', true);
    if (reducedMotion.matches || stage.clientWidth <= 900 || !frame.animate) {
      showView('login');
      return;
    }
    entering = true;
    enter.disabled = true;
    const run = ++entryRun;
    stage.dataset.transitioning = 'true';
    canvas.dataset.transitionStartedAt = String(performance.now());
    try {
      const from = frame.getBoundingClientRect();
      // Freeze the live scene before its layout changes. The animation is a
      // compositor transform; it never resizes the WebGL buffer frame by frame.
      onTransition('start');
      showView('login', false, true);
      ready = false;
      canvas.dataset.loginReady = 'false';
      panel.inert = true;
      copy.hidden = false;
      copy.inert = true;
      const to = frame.getBoundingClientRect();
      const move = trackEntryAnimation(frame.animate([
        { transform: `translate(${from.x - to.x}px, ${from.y - to.y}px) scale(${from.width / to.width}, ${from.height / to.height})`, borderRadius: '0px' },
        { transform: 'none', borderRadius: '24px' }
      ], { duration: 720, easing: 'cubic-bezier(.32,0,.18,1)', fill: 'both' }));
      canvas.dataset.transitionAnimationStartedAt = String(performance.now());
      trackEntryAnimation(copy.animate([
        { opacity: 1, transform: 'translateX(0)' },
        { opacity: 0, transform: 'translateX(-8px)' }
      ], { duration: 150, easing: 'ease-out', fill: 'both' }));
      trackEntryAnimation(panel.animate([
        { opacity: 0, transform: 'translateX(8px)' },
        { opacity: 1, transform: 'translateX(0)' }
      ], { duration: 260, delay: 420, easing: 'cubic-bezier(.2,0,.2,1)', fill: 'both' }));
      onTransition('animate', { animation: move, from, to });
      move.finished.catch(() => {}).finally(() => {
        if (run !== entryRun) return;
        canvas.dataset.transitionAnimationCompletedAt = String(performance.now());
        entryAnimations.forEach(animation => animation.cancel());
        entryAnimations = [];
        entering = false;
        copy.hidden = true;
        copy.inert = false;
        delete stage.dataset.transitioning;
        enter.disabled = false;
        ready = true;
        panel.inert = false;
        canvas.dataset.loginReady = 'true';
        onTransition('finish');
        onPhaseChange('login');
        canvas.dataset.transitionCompletedAt = String(performance.now());
        focusPhone();
      });
    } catch (_) {
      showView('login');
    }
  }

  function settleEntryTransition() {
    if (entering) showView(viewFromURL(), false);
  }

  async function authenticate(path, payload) {
    const turn = requestEpoch;
    if (!requestCurrent(turn)) return;
    setBusy(true);
    status.textContent = '';
    const testing = mode === 'experience';
    const activeLabel = testing ? experience : label;
    activeLabel.textContent = testing ? '正在进入体验' : '验证中';
    try {
      await sessionPromise;
      if (!csrf) await session();
      if (path === 'login' || path === 'register') await clearAuthenticatedSession();
      if (!requestCurrent(turn)) return;
      const {response, data} = await requestJSON('/api/welcome/auth/' + path, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Welcome-CSRF': csrf },
        body: JSON.stringify(payload)
      });
      if (!requestCurrent(turn)) return;
      if (data.csrf) csrf = data.csrf;
      if (!response.ok || data.authenticated !== true) throw new Error(data.error || '登录未成功，请检查手机号和密码。');
      applyIdentity(data);
      password.value = '';
      if (passwordConfirmation) passwordConfirmation.value = '';
      clearSms();
      if (data.sms?.phone_verified !== true && mode !== 'experience') {
        setBusy(false); recordView('bind', true); showView('bind');
        status.textContent = '请输入原密码并验证手机号，再选择体验画像。';
        return;
      }
      if (!canSelectExperience()) throw new Error('账户归属尚未就绪，请刷新后再试。');
      if (path !== 'experience') {
        selectedAccount = '';
        accountInputs.forEach(input => { input.checked = false; });
        delete panel.dataset.selectedAccount;
        setBusy(false); recordView('experience', true); showView('experience');
        status.textContent = path === 'register' ? '注册成功，请选择一个体验画像。' : '请选择你的体验画像。';
        try { const channel = new BroadcastChannel('navren-welcome-auth'); channel.postMessage({action:'login',registered_user_id:registeredUserId}); channel.close(); } catch (_) {}
        return;
      }
      if (!hasPrivateLease(data))
        throw new Error('个人体验空间尚未核对，请重新选择。');
      activeLabel.textContent = testing ? '即将进入' : '已登录';
      try { const channel = new BroadcastChannel('navren-welcome-auth'); channel.postMessage({ action: 'login',registered_user_id:registeredUserId }); channel.close(); } catch (_) { /* Focus checks also refresh other same-origin tabs. */ }
      location.assign('/chat.html');
    } catch (error) {
      if (!requestCurrent(turn)) return;
      status.textContent = error.message || '连接失败，请稍后重试。';
      activeLabel.textContent = testing ? '进入所选画像' : mode === 'register' ? '创建账户' : '登录';
      setBusy(false);
      csrf = '';
      sessionPromise = loadSession({ preserveStatus: true });
    } finally {
      delete payload.verification_code; delete payload.password; delete payload.password_confirmation;
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (!requestCurrent(requestEpoch) || !ready || busy || smsSending) return;
    if (mode === 'experience') { if (selectedAccount) await authenticate('experience', { user_id: selectedAccount }); return; }
    status.textContent = '';
    if (needsSms() && !smsConfigured) {
      status.textContent = smsUnavailableMessage(); return;
    }
    if (!/^1[3-9][0-9]{9}$/.test(phone.value.trim())) {
      status.textContent = '请输入正确的 11 位手机号。';
      phone.setAttribute('aria-invalid', 'true');
      phone.focus();
      return;
    }
    phone.removeAttribute('aria-invalid');
    if (password.required && password.value.length < 8) {
      status.textContent = '请输入至少 8 位密码。';
      password.setAttribute('aria-invalid', 'true');
      password.focus();
      return;
    }
    password.removeAttribute('aria-invalid');
    if (mode === 'register' && (!passwordConfirmation || passwordConfirmation.value !== password.value)) {
      status.textContent = '两次输入的密码不一致。';
      passwordConfirmation?.setAttribute('aria-invalid','true'); passwordConfirmation?.focus(); return;
    }
    if (needsSms()) {
      if (!smsChallenge || smsChallenge.phone !== phone.value.trim() || smsChallenge.purpose !== mode ||
          Date.now() >= smsChallenge.expiresAt || !/^[0-9]{6}$/.test(smsCode.value)) {
        status.textContent = '请先发送并填写本次有效的六位短信验证码。'; return;
      }
      const payload = {phone:phone.value.trim(),challenge_id:smsChallenge.id,verification_code:smsCode.value};
      if (mode !== 'login') payload.password = password.value;
      if (mode === 'register') payload.password_confirmation = passwordConfirmation.value;
      await authenticate(mode === 'register' ? 'register' : 'sms/' + mode, payload);
    } else {
      await authenticate(mode, { phone: phone.value.trim(), password: password.value });
    }
  }

  function switchMode() {
    if (busy || smsSending) return;
    if (mode === 'bind') return;
    const next = mode === 'register' ? 'login' : 'register';
    recordView(next, true);
    showView(next, false);
  }
  async function enterExperience() {
    if (!ready || busy || mode !== 'experience') return;
    await sessionPromise;
    if (!requireExperienceIdentity()) return;
    if (selectedAccount) {
      await authenticate('experience', { user_id: selectedAccount });
    }
  }
  function selectAccount() {
    if (busy) return;
    selectedAccount = accountInputs.find(input => input.checked)?.value || '';
    panel.dataset.selectedAccount = selectedAccount;
    status.textContent = '';
    experience.disabled = !selectedAccount || !canSelectExperience();
  }
  async function returnPhone() {
    if (busy || smsSending || !['experience', 'bind'].includes(mode)) return;
    if (authenticated) {
      setBusy(true);
      try {
        await clearAuthenticatedSession();
      } catch (error) { status.textContent = error.message; setBusy(false); return; }
      setBusy(false);
    }
    selectedAccount = '';
    accountInputs.forEach(input => { input.checked = false; });
    delete panel.dataset.selectedAccount;
    recordView('login', true);
    showView('login');
  }
  function restoreView() {
    // A submitted authentication request keeps its stage until its response is known.
    if (busy) {
      history.pushState(currentEntry.state, '', currentEntry.href);
      return;
    }
    const view = viewFromURL();
    currentEntry = { state: history.state, href: location.href };
    showView(view);
  }
  const togglePassword = () => setPasswordVisible(password.type === 'password');
  const listeners = [[enter, 'click', depart], [form, 'submit', submit], [modeSwitch, 'click', switchMode],
    [passwordToggle, 'click', togglePassword], [experience, 'click', enterExperience],
    [accounts, 'change', selectAccount], [experienceReturn, 'click', returnPhone]];
  if (smsSend) listeners.push([smsSend,'click',sendSms]);
  methodButtons.forEach(tab => listeners.push([tab, 'click', () => switchLoginMethod(tab.dataset.loginMethod)]));
  listeners.push([loginMethods, 'keydown', loginMethodKeydown]);
  listeners.push([phone,'input',clearSms]);
  listeners.forEach(([target, type, handler]) => target.addEventListener(type, handler));
  window.addEventListener('popstate', restoreView);
  window.addEventListener('resize', settleEntryTransition);
  window.addEventListener('pagehide', pauseRequests);
  window.addEventListener('pageshow', resumeRequests);
  reducedMotion.addEventListener?.('change', settleEntryTransition);
  // 固定同源入口只恢复已认可的选择界面，不选择账户、不建立业务身份。
  const entryView = viewFromURL();
  recordView(entryView);
  if (entryView) queueMicrotask(async () => {
    if (entryView === 'experience') await sessionPromise;
    if (!busy) showView(viewFromURL());
  });
  return { depart, get started() { return started; }, dispose() {
    if (disposed) return; disposed = true; paused = true; interruptRequests();
    password.value = '';
    if (passwordConfirmation) passwordConfirmation.value = '';
    smsResendDeadlines.clear();
    clearSms();
    cancelEntryTransition();
    listeners.forEach(([target, type, handler]) => target.removeEventListener(type, handler));
    window.removeEventListener('popstate', restoreView);
    window.removeEventListener('resize', settleEntryTransition);
    window.removeEventListener('pagehide', pauseRequests);
    window.removeEventListener('pageshow', resumeRequests);
    reducedMotion.removeEventListener?.('change', settleEntryTransition);
  } };
}
