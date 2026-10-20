// 使用正式欢迎模板和登录控制器；所有认证响应均为隔离夹具，不发送短信或访问真实账户。
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const { JSDOM } = require('jsdom');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'outputs/welcome-scene/index.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'js/welcome_login.js'), 'utf8');
const registeredUserId = 'a'.repeat(32), workspaceId = 'b'.repeat(32);
const anonymous = () => ({ authenticated: false, csrf: 'fixture-csrf', sms: { configured: true, registration_required: true } });
const logoutResult = () => ({ authenticated: false, scope: 'welcome_subject', business_requests_revoked: true });
function identity({ verified = true, lease = false } = {}) {
  return {
    authenticated: true, registered_user_id: registeredUserId, auth_subject: 'fixture-subject',
    csrf: 'fixture-authenticated-csrf', sms: { configured: true, registration_required: true, phone_verified: verified },
    ...(lease ? { redirect: '/chat.html', business_identity_status: 'verified', current_business_identity: {
      registered_user_id: registeredUserId, auth_subject: 'fixture-subject', workspace_id: workspaceId, account_id: 'grain_farmer'
    } } : {})
  };
}
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };

async function setup(options = {}) {
  const dom = new JSDOM(html, { url: 'https://navren.invalid/' + (options.query || '?view=login'), runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, d = w.document, requests = [], navigations = [];
  let currentSession = options.session || anonymous(), now = Date.now();
  w.matchMedia = query => ({ matches: query === '(prefers-reduced-motion: reduce)', addEventListener() {}, removeEventListener() {} });
  const routes = { ...options.routes };
  const fetch = async (url, init = {}) => {
    const method = init.method || 'GET';
    const request = { url, method, headers: init.headers, body: init.body ? JSON.parse(init.body) : null };
    requests.push(request);
    let result;
    if (url === '/api/welcome/auth/session' && method === 'GET') result = { data: currentSession };
    else {
      assert.equal(method, 'POST');
      assert.equal(init.credentials, 'same-origin');
      assert.equal(init.headers['X-Welcome-CSRF'], currentSession.csrf);
      assert.ok(routes[url], '未登记的网络请求：' + url);
      result = await routes[url](request);
      if (result.data?.authenticated === true) currentSession = result.data;
      if (url.endsWith('/logout') && result.data?.authenticated === false) currentSession = options.afterLogoutSession || anonymous();
    }
    return { ok: !result.status || result.status < 400, status: result.status || 200, json: async () => result.data };
  };
  const context = vm.createContext({
    window: w, document: d, history: w.history, performance: w.performance, fetch,
    location: { get href() { return w.location.href; }, get search() { return w.location.search; }, assign: url => navigations.push(url) },
    queueMicrotask, setInterval, clearInterval, URL, URLSearchParams, Date: { now: () => now },
    BroadcastChannel: class { postMessage() {} close() {} }
  });
  vm.runInContext(source.replace('export function createWelcomeLogin', 'function createWelcomeLogin'), context);
  const controller = context.createWelcomeLogin({ canvas: d.querySelector('canvas') });
  await settle();
  const get = id => d.getElementById(id);
  const click = async id => { get(id).click(); await settle(); };
  const submit = async ({ phone = '13700000000', password = 'fixture-only-password' } = {}) => {
    get('welcomePhone').value = phone;
    get('welcomePassword').value = password;
    get('welcomeLoginForm').dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
    await settle();
  };
  const choose = value => {
    const input = d.querySelector(`input[name="experienceAccount"][value="${value}"]`);
    input.checked = true; input.dispatchEvent(new w.Event('change', { bubbles: true }));
  };
  return { w, d, get, click, submit, choose, requests, navigations,
    advanceTime: ms => { now += ms; },
    mode: () => get('welcomeLogin').dataset.mode,
    close() { controller.dispose(); dom.window.close(); } };
}

const passed = [];
async function check(name, options, run) {
  const c = await setup(options);
  try { await run(c); passed.push(name); } finally { c.close(); }
}

async function main() {
  for (const session of [anonymous(), identity({ lease: true })]) {
    await check(`登录页提供双方式切换且不显示画像入口（会话${session.authenticated ? '已登录' : '未登录'}）`, { session }, async c => {
      assert.equal(c.get('welcomeLoginMethod'), null);
      assert.equal(c.get('welcomeLoginMethods').hidden, false);
      assert.equal(c.get('welcomePasswordLoginTab').getAttribute('aria-selected'), 'true');
      assert.equal(c.get('welcomeSmsLoginTab').getAttribute('aria-selected'), 'false');
      assert.equal(c.get('welcomeExperienceReturn').hidden, true);
      assert.equal(c.get('welcomeExperience').hidden, true);
      assert.equal(c.get('welcomeExperienceAccounts').hidden, true);
      assert.equal(c.get('welcomeSmsField').hidden, true);
      assert.equal(c.get('welcomePassword').required, true);
      await c.click('welcomeExperience');
      await c.click('welcomeExperienceReturn');
      assert.equal(c.mode(), 'login');
      assert.deepEqual(c.requests.map(r => r.method), ['GET']);
    });
  }
  await check('验证码表单不要求密码，切换保留手机号并支持键盘', {}, async c => {
    c.get('welcomePhone').value = '13700000000';
    c.get('welcomePassword').value = 'private-password-draft';
    await c.click('welcomeSmsLoginTab');
    assert.equal(c.get('welcomePhone').value, '13700000000');
    assert.equal(c.get('welcomePassword').value, '');
    assert.equal(c.get('welcomePassword').required, false);
    assert.equal(c.get('welcomePasswordField').hidden, true);
    assert.equal(c.get('welcomeSmsField').hidden, false);
    assert.equal(c.get('welcomeSmsCode').required, true);
    assert.equal(c.get('welcomeCredentials').getAttribute('aria-labelledby'), 'welcomeSmsLoginTab');
    c.get('welcomeSmsLoginTab').dispatchEvent(new c.w.KeyboardEvent('keydown', { key:'ArrowLeft', bubbles:true, cancelable:true }));
    assert.equal(c.get('welcomePasswordLoginTab').getAttribute('aria-selected'), 'true');
    assert.equal(c.get('welcomePassword').required, true);
    assert.equal(c.get('welcomeSmsCode').required, false);
    assert.equal(c.get('welcomeSmsField').hidden, true);
    assert.equal(c.d.activeElement.id, 'welcomePasswordLoginTab');
    assert.deepEqual(c.requests.map(r => r.method), ['GET']);
  });
  const sendFixture = r => {
    assert.deepEqual(r.body, { phone:'13700000000', purpose:'login' });
    return { data:{ source:'provider', challenge_id:'e'.repeat(32), expires_at:Date.now()/1000+300 } };
  };
  async function requestLoginCode(c) {
    await c.click('welcomeSmsLoginTab');
    c.get('welcomePhone').value = '13700000000';
    await c.click('welcomeSmsSend');
  }
  await check('验证码登录走专用接口，不提交密码，成功后选择画像', {
    routes: {
      '/api/welcome/auth/sms/send': sendFixture,
      '/api/welcome/auth/sms/login': r => {
        assert.deepEqual(r.body, { phone:'13700000000', challenge_id:'e'.repeat(32), verification_code:'123456' });
        return { data:identity({ lease:true }) };
      }
    }
  }, async c => {
    await requestLoginCode(c);
    assert.equal(c.get('welcomeSmsSend').disabled, true);
    assert.match(c.get('welcomeSmsSend').textContent, /60秒后重发/);
    c.get('welcomeSmsCode').value = '123456'; await c.submit({ password:'' });
    assert.equal(c.mode(), 'experience');
    assert.equal(c.get('welcomeLoginMethods').hidden, true);
    assert.equal(c.get('welcomeExperience').disabled, true);
    assert.deepEqual(c.navigations, []);
    assert.deepEqual(c.requests.filter(r => r.method === 'POST').map(r => r.url), ['/api/welcome/auth/sms/send', '/api/welcome/auth/sms/login']);
  });
  await check('未发送验证码时不能提交验证码登录', {}, async c => {
    await c.click('welcomeSmsLoginTab'); c.get('welcomeSmsCode').value = '123456';
    await c.submit({ password:'' });
    assert.match(c.get('welcomeLoginStatus').textContent, /本次有效的六位短信验证码/);
    assert.deepEqual(c.requests.map(r => r.method), ['GET']);
  });
  for (const invalidate of ['phone', 'method', 'expiry']) {
    await check(`验证码不会在${{ phone:'修改手机号', method:'切换登录方式', expiry:'过期' }[invalidate]}后复用`, {
      routes: { '/api/welcome/auth/sms/send':sendFixture }
    }, async c => {
      await requestLoginCode(c);
      if (invalidate === 'phone') {
        c.get('welcomePhone').value = '13900000000';
        c.get('welcomePhone').dispatchEvent(new c.w.Event('input', { bubbles:true }));
      } else if (invalidate === 'method') {
        await c.click('welcomePasswordLoginTab'); await c.click('welcomeSmsLoginTab');
        assert.equal(c.get('welcomeSmsSend').disabled, true, '切换方式不能绕过重发冷却');
      } else c.advanceTime(301000);
      c.get('welcomeSmsCode').value = '123456'; await c.submit({ password:'' });
      assert.equal(c.mode(), 'login');
      assert.match(c.get('welcomeLoginStatus').textContent, /本次有效的六位短信验证码/);
      assert.equal(c.requests.filter(r => r.url.endsWith('/sms/login')).length, 0);
    });
  }
  await check('验证码错误保留验证码登录及错误提示', {
    routes: {
      '/api/welcome/auth/sms/send':sendFixture,
      '/api/welcome/auth/sms/login':() => ({ status:401, data:{ authenticated:false, error:'验证码不正确或已失效' } })
    }
  }, async c => {
    await requestLoginCode(c); c.get('welcomeSmsCode').value = '999999'; await c.submit({ password:'' });
    assert.equal(c.mode(), 'login');
    assert.equal(c.get('welcomeSmsLoginTab').getAttribute('aria-selected'), 'true');
    assert.match(c.get('welcomeLoginStatus').textContent, /验证码不正确或已失效/);
    assert.equal(c.get('welcomeExperience').hidden, true);
    assert.deepEqual(c.navigations, []);
  });
  await check('短信服务不可用时不退化为无密码登录，仍可使用密码', {
    session:{ ...anonymous(), sms:{ configured:false, registration_required:true } },
    routes:{ '/api/welcome/auth/login':() => ({ data:identity() }) }
  }, async c => {
    await c.click('welcomeSmsLoginTab'); await c.submit({ password:'' });
    assert.equal(c.get('welcomeSmsSend').disabled, true);
    assert.equal(c.get('welcomePasswordField').hidden, true);
    assert.match(c.get('welcomeLoginStatus').textContent, /短信服务暂不可用/);
    assert.deepEqual(c.requests.map(r => r.method), ['GET']);
    await c.click('welcomePasswordLoginTab'); await c.submit();
    assert.equal(c.mode(), 'experience');
  });
  await check('短信发送失败不会生成可用验证码挑战', {
    routes:{ '/api/welcome/auth/sms/send':() => ({ status:429, data:{ error:'发送过于频繁，请稍后再试' } }) }
  }, async c => {
    await requestLoginCode(c);
    assert.match(c.get('welcomeLoginStatus').textContent, /发送过于频繁/);
    c.get('welcomeSmsCode').value = '123456'; await c.submit({ password:'' });
    assert.equal(c.requests.filter(r => r.url.endsWith('/sms/login')).length, 0);
  });
  await check('画像选择阶段仍可主动退出，登录表单不显示退出', {
    query:'?view=experience', session:identity({ lease:true }),
    routes:{ '/api/welcome/auth/logout':() => ({ data:logoutResult() }) }
  }, async c => {
    assert.equal(c.get('welcomeExperienceReturn').hidden, false);
    assert.equal(c.get('welcomeExperienceReturn').textContent, '退出登录');
    await c.click('welcomeExperienceReturn');
    assert.equal(c.mode(), 'login');
    assert.equal(c.get('welcomeExperienceReturn').hidden, true);
    await c.click('welcomeSmsLoginTab');
    assert.equal(c.get('welcomeSmsField').hidden, false);
    assert.equal(c.requests.filter(r => r.url.endsWith('/logout')).length, 1);
    assert.equal(c.get('welcomeExperience').hidden, true);
  });
  await check('保留会话只在主动提交密码登录时更新，再使用新CSRF登录', {
    session:identity({ lease:true }),
    routes:{
      '/api/welcome/auth/logout':() => ({ data:logoutResult() }),
      '/api/welcome/auth/login':() => ({ data:identity() })
    }
  }, async c => {
    assert.deepEqual(c.requests.map(r => r.method), ['GET']);
    await c.submit();
    assert.deepEqual(c.requests.map(r => r.url), ['/api/welcome/auth/session','/api/welcome/auth/logout','/api/welcome/auth/session','/api/welcome/auth/login']);
    assert.equal(c.requests[1].headers['X-Welcome-CSRF'], 'fixture-authenticated-csrf');
    assert.equal(c.requests[3].headers['X-Welcome-CSRF'], 'fixture-csrf');
    assert.equal(c.mode(), 'experience');
  });
  await check('主动发送登录验证码时更新旧会话，验证码在新会话提交', {
    session:identity({ lease:true }),
    routes:{
      '/api/welcome/auth/logout':() => ({ data:logoutResult() }),
      '/api/welcome/auth/sms/send':sendFixture,
      '/api/welcome/auth/sms/login':() => ({ data:identity() })
    }
  }, async c => {
    await c.click('welcomeSmsLoginTab');
    assert.deepEqual(c.requests.map(r => r.method), ['GET']);
    c.get('welcomePhone').value = '13700000000'; await c.click('welcomeSmsSend');
    assert.equal(c.requests.find(r => r.url.endsWith('/sms/send')).headers['X-Welcome-CSRF'], 'fixture-csrf');
    c.get('welcomeSmsCode').value = '123456'; await c.submit({ password:'' });
    assert.deepEqual(c.requests.filter(r => r.method === 'POST').map(r => r.url), ['/api/welcome/auth/logout','/api/welcome/auth/sms/send','/api/welcome/auth/sms/login']);
    assert.equal(c.mode(), 'experience');
  });
  await check('字段校验未通过时不修改保留会话', { session:identity({ lease:true }) }, async c => {
    await c.submit({ password:'short' });
    assert.match(c.get('welcomeLoginStatus').textContent, /至少 8 位/);
    await c.click('welcomeSmsLoginTab'); c.get('welcomePhone').value = '123'; await c.click('welcomeSmsSend');
    assert.deepEqual(c.requests.map(r => r.method), ['GET']);
  });
  for (const target of ['password','sms']) {
    for (const failedLogout of [
      { status:503, data:{ error:'退出服务暂不可用' } },
      { data:{ authenticated:false } }
    ]) await check(`${target === 'password' ? '密码登录' : '短信发送'}在旧会话退出${failedLogout.status ? '失败' : '未完整确认'}时停止`, {
      session:identity({ lease:true }),
      routes:{ '/api/welcome/auth/logout':() => failedLogout }
    }, async c => {
      if (target === 'password') await c.submit();
      else await requestLoginCode(c);
      assert.match(c.get('welcomeLoginStatus').textContent, /登录状态未能更新/);
      assert.deepEqual(c.requests.filter(r => r.method === 'POST').map(r => r.url), ['/api/welcome/auth/logout']);
      assert.equal(c.mode(), 'login');
      assert.equal(c.get('welcomeExperienceReturn').hidden, true);
      assert.deepEqual(c.navigations, []);
    });
  }
  await check('旧会话退出后仍返回已登录身份时禁止继续登录', {
    session:identity({ lease:true }), afterLogoutSession:identity({ lease:true }),
    routes:{ '/api/welcome/auth/logout':() => ({ data:logoutResult() }) }
  }, async c => {
    await c.submit();
    assert.match(c.get('welcomeLoginStatus').textContent, /登录状态未能更新/);
    assert.equal(c.requests.filter(r => r.url.endsWith('/login')).length, 0);
    assert.equal(c.get('welcomeExperienceReturn').hidden, true);
  });
  for (const lease of [false, true]) {
    await check(`登录成功后选择画像（${lease ? '有' : '无'}历史空间）`, {
      routes: { '/api/welcome/auth/login': () => ({ data: identity({ lease }) }) }
    }, async c => {
      await c.submit();
      assert.equal(c.mode(), 'experience');
      assert.equal(c.get('welcomeExperienceAccounts').hidden, false);
      assert.equal(c.get('welcomeExperience').hidden, false);
      assert.equal(c.get('welcomeExperience').disabled, true);
      assert.equal(c.get('welcomeCredentials').hidden, true);
      assert.deepEqual(c.navigations, []);
      assert.deepEqual(c.requests.map(r => r.url), ['/api/welcome/auth/session', '/api/welcome/auth/login']);
    });
  }
  await check('密码错误停留登录页', {
    routes: { '/api/welcome/auth/login': () => ({ status: 401, data: { authenticated: false, error: '密码不正确' } }) }
  }, async c => {
    await c.submit(); assert.equal(c.mode(), 'login');
    assert.equal(c.get('welcomeExperience').hidden, true);
    assert.equal(c.get('welcomeLoginStatus').textContent, '密码不正确');
    assert.deepEqual(c.navigations, []);
  });
  await check('手机号未验证时先验证，画像继续隐藏', {
    routes: { '/api/welcome/auth/login': () => ({ data: identity({ verified: false }) }) }
  }, async c => {
    await c.submit(); assert.equal(c.mode(), 'bind');
    assert.equal(c.get('welcomeSmsField').hidden, false);
    assert.equal(c.get('welcomeSmsCode').required, true);
    assert.equal(c.get('welcomeExperience').hidden, true);
    assert.deepEqual(c.navigations, []);
  });
  await check('注册仍要求密码确认及有效短信验证码', {}, async c => {
    await c.click('welcomeLoginSwitch'); assert.equal(c.mode(), 'register');
    assert.equal(c.get('welcomeSmsField').hidden, false);
    assert.equal(c.get('welcomeSmsCode').required, true);
    assert.equal(c.get('welcomePasswordConfirmation').required, true);
    assert.equal(c.get('welcomeExperience').hidden, true);
    c.get('welcomePasswordConfirmation').value = 'fixture-only-password';
    await c.submit();
    assert.match(c.get('welcomeLoginStatus').textContent, /有效的六位短信验证码/);
    assert.deepEqual(c.requests.map(r => r.method), ['GET']);
  });
  await check('注册验证完成后进入画像选择', {
    routes: {
      '/api/welcome/auth/sms/send': r => {
        assert.equal(r.body.purpose, 'register');
        return { data: { source: 'provider', challenge_id: 'd'.repeat(32), expires_at: Date.now() / 1000 + 300 } };
      },
      '/api/welcome/auth/register': r => {
        assert.equal(r.body.challenge_id, 'd'.repeat(32));
        assert.equal(r.body.verification_code, '123456');
        assert.equal(r.body.password_confirmation, r.body.password);
        return { data: identity() };
      }
    }
  }, async c => {
    await c.click('welcomeLoginSwitch');
    c.get('welcomePhone').value = '13700000000';
    await c.click('welcomeSmsSend');
    c.get('welcomeSmsCode').value = '123456';
    c.get('welcomePasswordConfirmation').value = 'fixture-only-password';
    await c.submit();
    assert.equal(c.mode(), 'experience');
    assert.equal(c.get('welcomeExperience').disabled, true);
    assert.deepEqual(c.navigations, []);
  });
  for (const session of [anonymous(), identity({ verified: false })]) {
    await check(`直接打开画像地址仍需${session.authenticated ? '手机号验证' : '登录'}`, { query: '?view=experience', session }, async c => {
      assert.equal(c.mode(), session.authenticated ? 'bind' : 'login');
      assert.equal(c.get('welcomeExperienceAccounts').hidden, true);
      assert.equal(c.get('welcomeExperience').hidden, true);
      assert.deepEqual(c.requests.map(r => r.method), ['GET']);
    });
  }
  await check('已登录可恢复画像选择；选择前不产生认证请求', { query: '?view=experience', session: identity({ lease: true }) }, async c => {
    assert.equal(c.mode(), 'experience');
    assert.equal(c.get('welcomeExperience').disabled, true);
    await c.click('welcomeExperience');
    c.get('welcomeLoginForm').dispatchEvent(new c.w.Event('submit', { bubbles: true, cancelable: true }));
    await settle();
    assert.deepEqual(c.requests.map(r => r.method), ['GET']);
  });
  await check('选中画像后验证归属并进入对话', {
    routes: {
      '/api/welcome/auth/login': () => ({ data: identity({ lease: true }) }),
      '/api/welcome/auth/experience': r => { assert.deepEqual(r.body, { user_id: 'grain_farmer' }); return { data: identity({ lease: true }) }; }
    }
  }, async c => {
    await c.submit(); c.choose('grain_farmer');
    assert.equal(c.get('welcomeExperience').disabled, false);
    await c.click('welcomeExperience');
    assert.deepEqual(c.navigations, ['/chat.html']);
    assert.equal(c.requests.filter(r => r.url.endsWith('/experience')).length, 1);
  });
  await check('画像响应的空间归属不符时禁止进入', {
    routes: {
      '/api/welcome/auth/login': () => ({ data: identity() }),
      '/api/welcome/auth/experience': () => {
        const data = identity({ lease: true }); data.current_business_identity.registered_user_id = 'c'.repeat(32);
        return { data };
      }
    }
  }, async c => {
    await c.submit(); c.choose('grain_farmer'); await c.click('welcomeExperience');
    assert.deepEqual(c.navigations, []);
    assert.match(c.get('welcomeLoginStatus').textContent, /个人体验空间尚未核对/);
  });
  console.log(JSON.stringify({ source_sha256: crypto.createHash('sha256').update(source).digest('hex'), passed,
    limitation: '隔离认证响应的正式模板和控制器检查；没有真实登录、短信或账户写入。' }, null, 2));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
