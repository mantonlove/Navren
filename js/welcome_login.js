// The welcome page enters a dedicated phone login or test account view.
// Both paths require a verified server response before opening the home route.
export function createWelcomeLogin({ canvas, onPhaseChange = () => {} }) {
  const stage = canvas.parentElement;
  const copy = document.querySelector('.welcome-copy');
  const byId = id => document.getElementById(id);
  const enter = byId('welcomeEnter');
  const shell = byId('welcomeAuthShell');
  const panel = byId('welcomeLogin');
  const form = byId('welcomeLoginForm');
  const credentials = byId('welcomeCredentials');
  const button = byId('welcomeLoginSubmit');
  const label = button.querySelector('span');
  const phone = byId('welcomePhone');
  const password = byId('welcomePassword');
  const passwordToggle = byId('welcomePasswordToggle');
  const title = byId('welcomeLoginTitle');
  const intro = byId('welcomeLoginIntro');
  const status = byId('welcomeLoginStatus');
  const modeSwitch = byId('welcomeLoginSwitch');
  const experience = byId('welcomeExperience');
  const accounts = byId('welcomeExperienceAccounts');
  const experienceReturn = byId('welcomeExperienceReturn');
  const accountInputs = [...accounts.querySelectorAll('input[type="radio"]')];
  let started = false, ready = false, busy = false, mode = 'login';
  let selectedAccount = '', csrf = '', sessionPromise;

  async function session() {
    const response = await fetch('/api/welcome/auth/session', { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error('登录服务暂时不可用，请稍后重试。');
    const data = await response.json();
    csrf = data.csrf || '';
    return data;
  }
  sessionPromise = session().catch(() => null);

  function setPasswordVisible(visible) {
    password.type = visible ? 'text' : 'password';
    passwordToggle.textContent = visible ? '隐藏' : '显示';
    passwordToggle.setAttribute('aria-pressed', String(visible));
    passwordToggle.setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
  }

  function setBusy(value) {
    busy = value;
    form.setAttribute('aria-busy', String(value));
    button.disabled = modeSwitch.disabled = passwordToggle.disabled = experienceReturn.disabled = value;
    phone.readOnly = password.readOnly = value;
    accountInputs.forEach(input => { input.disabled = value; });
    experience.disabled = value || (mode === 'experience' && !selectedAccount);
  }

  function renderMode() {
    const testing = mode === 'experience', register = mode === 'register';
    credentials.hidden = testing;
    accounts.hidden = !testing;
    button.hidden = testing;
    modeSwitch.hidden = testing;
    experienceReturn.hidden = !testing;
    title.textContent = testing ? '体验 Navren' : register ? '开启你的旅程' : '欢迎回来';
    intro.textContent = testing ? '选择一个测试账户，开始体验。' : register ? '使用手机号，创建你的 Navren 账户。' : '登录，继续你的旅程。';
    document.title = testing ? 'Navren · 测试体验' : register ? 'Navren · 创建账户' : 'Navren · 登录';
    modeSwitch.textContent = register ? '已有账户？返回登录' : '首次使用？创建账户';
    password.autocomplete = register ? 'new-password' : 'current-password';
    password.placeholder = register ? '至少 8 位密码' : '输入密码';
    label.textContent = register ? '创建账户' : '登录';
    button.setAttribute('aria-label', register ? '创建账户并登录 Navren' : '登录 Navren');
    experience.textContent = testing ? '选择体验账户' : '测试体验';
    experience.dataset.confirm = String(testing);
    experience.setAttribute('aria-expanded', String(testing));
    panel.dataset.mode = mode;
    status.textContent = '';
    phone.removeAttribute('aria-invalid');
    password.removeAttribute('aria-invalid');
    setPasswordVisible(false);
    setBusy(false);
  }

  function focusPhone() {
    if (window.matchMedia('(pointer: fine)').matches) phone.focus({ preventScroll: true });
    else { title.tabIndex = -1; title.focus({ preventScroll: true }); }
  }

  function depart() {
    if (started) return;
    const now = performance.now();
    started = ready = true;
    copy.hidden = true;
    shell.hidden = panel.hidden = false;
    panel.inert = false;
    stage.dataset.phase = 'login';
    renderMode();
    canvas.dataset.loginReady = 'true';
    canvas.dataset.transitionStartedAt = String(now);
    onPhaseChange('login');
    canvas.dataset.transitionCompletedAt = String(performance.now());
    focusPhone();
  }

  async function authenticate(path, payload) {
    setBusy(true);
    status.textContent = '';
    const testing = mode === 'experience';
    const activeLabel = testing ? experience : label;
    activeLabel.textContent = testing ? '正在进入体验' : '验证中';
    try {
      await sessionPromise;
      if (!csrf) await session();
      const response = await fetch('/api/welcome/auth/' + path, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Welcome-CSRF': csrf },
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (data.csrf) csrf = data.csrf;
      if (!response.ok || data.authenticated !== true) throw new Error(data.error || '登录未成功，请检查手机号和密码。');
      password.value = '';
      activeLabel.textContent = testing ? '即将进入' : '已登录';
      try { const channel = new BroadcastChannel('navren-welcome-auth'); channel.postMessage({ action: 'login' }); channel.close(); } catch (_) { /* Focus checks also refresh other same-origin tabs. */ }
      location.assign('/chat.html');
    } catch (error) {
      status.textContent = error.message || '连接失败，请稍后重试。';
      activeLabel.textContent = testing ? '选择体验账户' : mode === 'register' ? '创建账户' : '登录';
      setBusy(false);
      csrf = '';
      sessionPromise = session().catch(() => null);
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (!ready || busy) return;
    if (mode === 'experience') { if (selectedAccount) await authenticate('experience', { user_id: selectedAccount }); return; }
    status.textContent = '';
    if (!/^1[3-9][0-9]{9}$/.test(phone.value.trim())) {
      status.textContent = '请输入正确的 11 位手机号。';
      phone.setAttribute('aria-invalid', 'true');
      phone.focus();
      return;
    }
    phone.removeAttribute('aria-invalid');
    if (password.value.length < 8) {
      status.textContent = '请输入至少 8 位密码。';
      password.setAttribute('aria-invalid', 'true');
      password.focus();
      return;
    }
    password.removeAttribute('aria-invalid');
    await authenticate(mode, { phone: phone.value.trim(), password: password.value });
  }

  function switchMode() {
    if (busy) return;
    mode = mode === 'register' ? 'login' : 'register';
    renderMode();
  }
  async function testExperience() {
    if (!ready || busy) return;
    if (mode !== 'experience') {
      mode = 'experience';
      password.value = '';
      renderMode();
      accountInputs[0].focus({ preventScroll: true });
    } else if (selectedAccount) {
      await authenticate('experience', { user_id: selectedAccount });
    }
  }
  function selectAccount() {
    if (busy) return;
    selectedAccount = accountInputs.find(input => input.checked)?.value || '';
    panel.dataset.selectedAccount = selectedAccount;
    status.textContent = '';
    experience.disabled = !selectedAccount;
  }
  function returnPhone() {
    if (busy) return;
    mode = 'login';
    selectedAccount = '';
    accountInputs.forEach(input => { input.checked = false; });
    delete panel.dataset.selectedAccount;
    renderMode();
    focusPhone();
  }
  const togglePassword = () => setPasswordVisible(password.type === 'password');
  const listeners = [[enter, 'click', depart], [form, 'submit', submit], [modeSwitch, 'click', switchMode],
    [passwordToggle, 'click', togglePassword], [experience, 'click', testExperience],
    [accounts, 'change', selectAccount], [experienceReturn, 'click', returnPhone]];
  listeners.forEach(([target, type, handler]) => target.addEventListener(type, handler));
  // 固定同源入口只恢复已认可的选择界面，不选择账户、不建立业务身份。
  const entryView = new URLSearchParams(location.search).get('view');
  if (entryView === 'experience' || entryView === 'login') {
    queueMicrotask(() => {
      if (entryView === 'experience') mode = 'experience';
      depart();
      if (mode === 'experience') accountInputs[0].focus({ preventScroll: true });
    });
  }
  return { depart, get started() { return started; }, dispose() {
    listeners.forEach(([target, type, handler]) => target.removeEventListener(type, handler));
  } };
}
