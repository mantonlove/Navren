// Node 内建 VM + 隔离 DOM；fetch 只接受登记夹具路由，禁止网络、服务重启和真实账户写入。
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const sourceFiles = ['js/welcome_session.js', 'js/bank_client.js'];
const sources = Object.fromEntries(sourceFiles.map(path => [path, fs.readFileSync(path, 'utf8')]));
const entry = '/?view=experience';
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((r, j) => { resolve = r; reject = j; }); return {promise, resolve, reject}; };
class DetailEvent extends Event { constructor(type, options = {}) { super(type, options); this.detail = options.detail; } }
class Element {
  constructor(tag = 'div', cls = '', attrs = {}) { this.tagName = tag.toUpperCase(); this.className = cls; this.attrs = {...attrs}; this.children = []; this.parentElement = null; this.dataset = {}; this.inert = false; this.disabled = false; this.isConnected = true; this.textContent = ''; }
  append(...children) { for (const child of children) { child.parentElement = this; this.children.push(child); } }
  contains(element) { return this === element || this.children.some(child => child.contains(element)); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] ?? null; }
  matches(selectors) { return selectors.split(',').some(selector => {
    selector = selector.trim();
    if (selector === 'button') return this.tagName === 'BUTTON';
    if (selector === 'button[onclick]') return this.tagName === 'BUTTON' && this.attrs.onclick != null;
    if (selector === 'audio' || selector === 'video') return this.tagName === selector.toUpperCase();
    if (selector.startsWith('[')) return this.attrs[selector.slice(1, -1)] != null;
    if (selector.startsWith('.')) return this.className.split(' ').includes(selector.slice(1));
    return false;
  }); }
  closest(selectors) { let element = this; while (element) { if (element.matches(selectors)) return element; element = element.parentElement; } return null; }
}
function storage(initial = {}) { const data = new Map(Object.entries(initial)); return {getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, String(value)), removeItem: key => data.delete(key), data}; }
function fixture(overrides = {}) {
  const account = overrides.current_business_identity?.account_id || 'tycoon';
  return {authenticated: true, auth_subject: 'fixture-welcome-subject-A', csrf: 'fixture-welcome-csrf-A', mode: 'experience',
    user: {username: '手机号占位', display_name: '初始姓名'}, entry_experience_account: {user_id: 'tycoon', display_name: '初选账户'},
    current_business_identity: {environment: 'simulation', account_id: account, workspace_id: 'fixture-workspace-' + account,
      privacy_revision: 0, personal_revision: 0, generation: 1, display_name: '账户姓名'},
    business_identity_status: 'verified', experience_account: {user_id: account, display_name: '当前姓名'}, ...overrides};
}
const response = (data, status = 200, json) => ({ok: status >= 200 && status < 300, status,
  headers: {get: key => key === 'X-Navren-Workspace' ? data?.workspace_id ?? null : key === 'X-Navren-Privacy-Revision' ? data?.privacy_revision?.toString() ?? null : null},
  json: json || (async () => JSON.parse(JSON.stringify(data)))});
const completeLogout = () => response({authenticated: false, scope: 'welcome_subject', business_requests_revoked: true, redirect: 'https://forbidden.example/'});
function setup({initial = fixture(), bank = false, readyState = 'complete', contract = 'welcome-business-v1'} = {}) {
  const window = new EventTarget(), document = new EventTarget();
  const html = new Element('html'), privateRoot = new Element('main', 'page-scroll'), oldPanel = new Element('section'),
    logoutGroup = new Element('div'), button = new Element('button', '', {'data-navren-logout': ''}), audio = new Element('audio');
  button.textContent = '退出登录'; logoutGroup.append(button); privateRoot.append(oldPanel, logoutGroup); html.append(privateRoot, audio);
  const flatten = element => [element, ...element.children.flatMap(flatten)];
  document.documentElement = html; document.readyState = readyState; document.hidden = false;
  document.currentScript = contract === false ? null : {src: 'https://navren.test/js/welcome_session.js' + (contract ? '?contract=' + contract : '')};
  document.querySelectorAll = selectors => flatten(html).filter(element => element.matches(selectors));
  const requests = [], queue = [], channels = [], broadcasts = [], events = [], navigations = [];
  const local = storage({qingzhou_chatHistory: 'fixture-history', qingzhou_chatKeyMoments: 'fixture-moments', unrelated: 'keep'}), session = storage({navren_return_view: 'fixture-old-view'});
  let stopped = 0, paused = 0, invalidated = 0, privateRoute = null;
  audio.pause = () => { paused++; };
  class Channel {
    constructor(name) { this.name = name; this.closed = false; channels.push(this); }
    postMessage(data) { assert(!this.closed); broadcasts.push({name: this.name, data}); }
    close() { this.closed = true; }
  }
  const fetch = async (path, options = {}) => {
    assert.equal(typeof path, 'string');
    if (path === '/assets/fixture.svg' || path === 'https://outside.test/api/private') {
      requests.push({path, options, method: options.method || 'GET'}); return response({fixture_public_or_external: true});
    }
    assert(['/api/welcome/auth/session', '/api/welcome/auth/logout', '/api/finance/overview', '/api/finance/session'].includes(path), '禁止非夹具路由：' + path);
    assert.equal(options.credentials, 'same-origin'); assert.equal(options.cache, 'no-store');
    const method = options.method || 'GET'; requests.push({path, method, options});
    if (path === '/api/finance/overview' || path === '/api/finance/session') { assert(bank && privateRoute); return privateRoute(options, path); }
    if (path === '/api/welcome/auth/logout') { assert.equal(method, 'POST'); assert.equal(options.body, '{}'); assert(options.headers['X-Welcome-CSRF']); }
    else assert.equal(method, 'GET');
    const next = queue.length ? queue.shift() : response(initial);
    if (typeof next === 'function') return next({path, method, options});
    return next;
  };
  Object.assign(window, {window, document, fetch, location: {href: 'https://navren.test/mine.html', origin: 'https://navren.test', replace: path => navigations.push(path)}, URL,
    Event, CustomEvent: DetailEvent, AbortController, BroadcastChannel: Channel, localStorage: local, sessionStorage: session,
    setTimeout, clearTimeout, console, crypto: crypto.webcrypto, NavrenSpeech: {stop() { stopped++; }}});
  const context = vm.createContext(window);
  if (bank) vm.runInContext(sources['js/bank_client.js'], context, {filename: sourceFiles[1]});
  else window.BankClient = {beginAccountSwitch() { invalidated++; }};
  for (const type of ['navren:identity-revoked', 'navren:privacy-reset', 'navren:business-identity']) window.addEventListener(type, event => events.push({type, detail: event.detail}));
  if (initial?.then) queue.push(initial);
  vm.runInContext(sources['js/welcome_session.js'], context, {filename: sourceFiles[0]});
  const api = window.NavrenWelcomeSession;
  return {window, document, html, privateRoot, oldPanel, button, api, nativeFetch: fetch, requests, broadcasts, channels, events, navigations, local, session,
    push(...responses) { queue.push(...responses); }, stats: () => ({stopped, paused, invalidated}), setPrivate(fn) { privateRoute = fn; },
    signal(type, options = {}) { const event = new Event(type); for (const [key, value] of Object.entries(options)) Object.defineProperty(event, key, {value}); window.dispatchEvent(event); },
    click(target = button) { const event = new Event('click', {cancelable: true}); Object.defineProperty(event, 'target', {value: target}); document.dispatchEvent(event); return event; },
    close() { for (const channel of channels) channel.close(); }};
}
const results = [];
async function test(name, run) {
  try { await run(); results.push({name, passed: true}); console.log('通过：' + name); }
  catch (error) { results.push({name, passed: false, error: error.message}); console.error('失败：' + name + '\n' + error.stack); }
}
async function withEnv(options, run) { const e = setup(options); try { await e.api.refresh(); await run(e); } finally { e.close(); } }
const revokedEvents = e => e.events.filter(event => event.type === 'navren:identity-revoked');
const businessEvents = e => e.events.filter(event => event.type === 'navren:business-identity');
(async () => {
  await test('旧注入无 contract marker：冻结兼容 API 不请求、不失效现役页面、不假退出', () => withEnv({contract: null, initial: {authenticated: true, user: {username: '旧后端已登录账户'}}}, async e => {
    assert(Object.isFrozen(e.api)); assert(Object.isFrozen(e.api.getState())); assert.equal(e.api.getState().status, 'unsupported_contract');
    assert.equal(e.api.getState().authenticated, null); assert.equal(e.window.fetch, e.nativeFetch); assert.equal(e.channels.length, 0);
    e.signal('focus'); e.signal('pageshow'); e.document.dispatchEvent(new Event('visibilitychange')); e.signal('navren:account-switched');
    assert.equal((await e.api.refresh()).status, 'unsupported_contract'); assert.equal(await e.api.logout(e.button), false);
    assert.equal(e.button.textContent, '退出接线待更新，请稍后重试'); assert.equal(e.button.disabled, false);
    assert.equal(e.requests.length, 0); assert.equal(e.broadcasts.length, 0); assert.equal(e.events.length, 0); assert.equal(e.navigations.length, 0);
    assert.equal(e.stats().invalidated, 0); assert.equal(e.stats().stopped, 0); assert.equal(e.stats().paused, 0); assert.equal(e.oldPanel.inert, false);
    assert.equal(e.local.getItem('qingzhou_chatHistory'), 'fixture-history'); assert.equal(e.html.dataset.navrenWelcomeRevoked, undefined);
  }));
  await test('错误 contract 或缺少 currentScript 均不自动启用新会话协议', async () => {
    for (const contract of ['welcome-business-v0', false]) {
      const e = setup({contract});
      try { assert.equal((await e.api.refresh()).status, 'unsupported_contract'); assert.equal(await e.api.logout(), false); assert.equal(e.requests.length, 0); assert.equal(e.navigations.length, 0); }
      finally { e.close(); }
    }
  });
  await test('兼容 API 返回体验选择仍为唯一同源入口，不能创建业务会话', () => withEnv({contract: null}, async e => {
    e.api.returnToExperienceSelection('https://forbidden.example'); assert.deepEqual(e.navigations, [entry]); assert.equal(e.requests.length, 0); assert.equal(e.broadcasts.length, 0);
  }));
  await test('公开 API 与状态深冻结，不新增设置按钮，不以事件授予 BankClient 身份', () => withEnv({}, async e => {
    assert(Object.isFrozen(e.api)); assert.deepEqual(Object.keys(e.api).sort(), ['getState', 'logout', 'refresh', 'returnToExperienceSelection']);
    const state = e.api.getState(); assert(Object.isFrozen(state)); assert(Object.isFrozen(state.current_business_identity));
    assert.throws(() => { state.current_business_identity.workspace_id = 'forged'; }, TypeError);
    assert.equal(e.document.querySelectorAll('button').length, 1); assert.equal(e.stats().invalidated, 0);
    assert.equal(businessEvents(e).length, 1); assert.equal(businessEvents(e)[0].detail.csrf, undefined); assert(Object.isFrozen(businessEvents(e)[0].detail));
  }));
  await test('相同欢迎主体内姓名及业务画像改变不返回欢迎，初选描述与当前画像分离', () => withEnv({}, async e => {
    e.push(response(fixture({user: {username: '更换的展示名称'}, current_business_identity: {environment: 'simulation', account_id: 'grain_farmer', workspace_id: 'fixture-farmer', privacy_revision: 2, display_name: '新昵称'}})));
    await e.api.refresh(); const state = e.api.getState();
    assert.equal(state.auth_subject, 'fixture-welcome-subject-A'); assert.equal(state.entry_experience_account.user_id, 'tycoon');
    assert.equal(state.experience_account.user_id, 'grain_farmer'); assert.equal(state.current_business_identity.account_id, 'grain_farmer');
    assert.equal(businessEvents(e).length, 2); assert.equal(revokedEvents(e).length, 0); assert.equal(e.navigations.length, 0);
    assert.equal(e.stats().invalidated, 1); assert.equal(e.local.getItem('qingzhou_chatHistory'), null);
  }));
  await test('欢迎主体变化，即使账户姓名相同也失效并进入固定同源选择入口', () => withEnv({}, async e => {
    e.push(response(fixture({auth_subject: 'fixture-welcome-subject-B'}))); await e.api.refresh();
    assert.equal(e.api.getState().authenticated, false); assert.equal(e.api.getState().revoked, true);
    assert.equal(e.api.getState().current_business_identity, null); assert.deepEqual(e.navigations, [entry]); assert.equal(revokedEvents(e).length, 1);
    assert.equal(e.stats().invalidated, 1); assert(e.stats().stopped > 0); assert(e.stats().paused > 0); assert.equal(e.local.getItem('qingzhou_chatHistory'), null); assert.equal(e.local.getItem('unrelated'), 'keep');
  }));
  await test('401 清旧身份、历史和媒体，不保留可消费的业务状态', () => withEnv({}, async e => {
    e.push(response({error: 'revoked'}, 401)); await e.api.refresh();
    assert.deepEqual(e.navigations, [entry]); assert.equal(e.api.getState().business_identity_status, 'revoked'); assert(e.oldPanel.inert);
    assert.equal(e.local.getItem('qingzhou_chatKeyMoments'), null); assert.equal(e.session.getItem('navren_return_view'), null);
  }));
  await test('缺少 auth_subject 不退回 username 兼容授权', () => withEnv({initial: fixture({auth_subject: undefined})}, async e => {
    assert.equal(e.api.getState().authenticated, false); assert.deepEqual(e.navigations, [entry]); assert.equal(businessEvents(e).length, 0);
  }));
  await test('首次 session 与 focus/pageshow/可见性读合并，调用者得到同一 Promise', async () => {
    const d = deferred(), e = setup({initial: d.promise});
    try {
      const one = e.api.refresh(), two = e.api.refresh(); assert.equal(one, two);
      e.signal('focus'); e.signal('pageshow'); e.document.dispatchEvent(new Event('visibilitychange')); assert.equal(e.requests.length, 1);
      d.resolve(response(fixture())); await one; assert.equal(e.api.getState().status, 'ready'); assert.equal(businessEvents(e).length, 1);
    } finally { e.close(); }
  });
  await test('未接通和无效身份不发行已核验业务事件，不让初选描述充当当前账户', () => withEnv({initial: fixture({business_identity_status: 'not_connected'})}, async e => {
    assert.equal(e.api.getState().entry_experience_account.user_id, 'tycoon'); assert.equal(e.api.getState().current_business_identity, null);
    assert.equal(e.api.getState().experience_account, null); assert.equal(businessEvents(e).length, 0);
    e.push(response(fixture({current_business_identity: {environment: 'simulation', account_id: 'tycoon', workspace_id: 'fixture', privacy_revision: '0'}})));
    await e.api.refresh(); assert.equal(e.api.getState().business_identity_status, 'unavailable'); assert.equal(businessEvents(e).length, 0);
  }));
  await test('四固定目录之外的 account_id 被拒绝', () => withEnv({initial: fixture({current_business_identity: {environment: 'simulation', account_id: 'invented', workspace_id: 'fixture', privacy_revision: 0}})}, async e => {
    assert.equal(e.api.getState().current_business_identity, null); assert.equal(e.api.getState().experience_account, null); assert.equal(businessEvents(e).length, 0);
  }));
  await test('业务状态暂不可核对可重试，不伪造欢迎主体撤销或业务成功事件', () => withEnv({}, async e => {
    e.push(response({}, 503)); await e.api.refresh(); assert.equal(e.api.getState().status, 'unavailable'); assert.equal(e.api.getState().current_business_identity, null); assert.equal(e.navigations.length, 0);
    e.push(response(fixture())); await e.api.refresh(); assert.equal(e.api.getState().status, 'ready'); assert.equal(e.api.getState().revoked, false);
  }));
  await test('logout 开始即清旧客户端，完整服务端成功后才广播和跳转', () => withEnv({}, async e => {
    const d = deferred(); e.push(response(fixture({csrf: 'fresh-fixture-csrf'})), d.promise);
    const pending = e.api.logout(e.button); assert.equal(e.api.getState().revoked, true); assert.equal(e.button.disabled, true); assert.equal(e.navigations.length, 0);
    await tick(); const post = e.requests.find(request => request.path.endsWith('/logout')); assert.equal(post.options.headers['X-Welcome-CSRF'], 'fresh-fixture-csrf'); assert.equal(e.broadcasts.length, 0);
    d.resolve(completeLogout()); assert.equal(await pending, true); assert.equal(e.api.getState().logout_status, 'complete');
    assert.deepEqual(e.navigations, [entry]); assert.equal(e.broadcasts.length, 1); assert.equal(e.broadcasts[0].data.action, 'logout');
  }));
  await test('服务端 logout 失败不广播、不跳转，旧视图仍失效且现有按钮可重试', () => withEnv({}, async e => {
    e.push(response(fixture()), response({error: 'business_logout_failed'}, 502)); assert.equal(await e.api.logout(e.button), false);
    assert.equal(e.api.getState().logout_status, 'failed'); assert.equal(e.api.getState().revoked, true); assert.equal(e.api.getState().current_business_identity, null);
    assert.equal(e.button.disabled, false); assert.equal(e.button.inert, false); assert.equal(e.privateRoot.inert, false); assert(e.oldPanel.inert);
    assert.equal(e.button.textContent, '退出未成功，点击重试'); assert.equal(e.navigations.length, 0); assert.equal(e.broadcasts.length, 0);
    const count = e.requests.length; await e.api.refresh(); assert.equal(e.requests.length, count, '普通 refresh 不恢复失败退出后的旧私有 UI');
    e.push(response(fixture({csrf: 'retry-fixture-csrf'})), completeLogout()); assert.equal(await e.api.logout(e.button), true);
    assert.equal(e.requests.at(-1).options.headers['X-Welcome-CSRF'], 'retry-fixture-csrf'); assert.equal(e.broadcasts.length, 1); assert.deepEqual(e.navigations, [entry]);
  }));
  await test('403 不自动重复提交退出，显式重试重新获取 CSRF', () => withEnv({}, async e => {
    e.push(response(fixture()), response({}, 403)); assert.equal(await e.api.logout(), false);
    assert.equal(e.requests.filter(request => request.method === 'POST').length, 1); assert.equal(e.navigations.length, 0);
    e.push(response(fixture({csrf: 'rotated-fixture-csrf'})), completeLogout()); assert.equal(await e.api.logout(), true);
    assert.equal(e.requests.filter(request => request.method === 'POST').length, 2);
  }));
  await test('退出动作合并，按钮连点不会重复服务端 POST', () => withEnv({}, async e => {
    const d = deferred(); e.push(d.promise, completeLogout()); const one = e.api.logout(e.button), two = e.api.logout(e.button); assert.equal(one, two);
    d.resolve(response(fixture())); assert.equal(await one, true); assert.equal(e.requests.filter(request => request.method === 'POST').length, 1);
  }));
  await test('退出 GET 时发现欢迎主体已换，不注销新主体', () => withEnv({}, async e => {
    e.push(response(fixture({auth_subject: 'fixture-subject-new'}))); assert.equal(await e.api.logout(e.button), false);
    assert.equal(e.requests.filter(request => request.method === 'POST').length, 0); assert.deepEqual(e.navigations, [entry]); assert.equal(e.broadcasts.length, 0);
  }));
  await test('200 但缺完整退出回执仍按失败处理，忽略响应外部 redirect', () => withEnv({}, async e => {
    e.push(response(fixture()), response({authenticated: true, scope: 'welcome_subject', redirect: 'https://forbidden.example'}));
    assert.equal(await e.api.logout(), false); assert.equal(e.api.getState().logout_status, 'failed'); assert.equal(e.broadcasts.length, 0); assert.equal(e.navigations.length, 0);
  }));
  await test('200 只有欢迎退出但未核验业务请求撤销不宣称退出成功', () => withEnv({}, async e => {
    e.push(response(fixture()), response({authenticated: false, scope: 'welcome_subject', business_requests_revoked: false}));
    assert.equal(await e.api.logout(), false); assert.equal(e.broadcasts.length, 0); assert.equal(e.navigations.length, 0);
  }));
  await test('业务 Cookie 已撤销只撤销业务前端，保留欢迎主体并可继续完整 logout', () => withEnv({}, async e => {
    e.push(response(fixture({business_identity_status: 'revoked', current_business_identity: null, experience_account: null})));
    await e.api.refresh(); const state = e.api.getState();
    assert.equal(state.authenticated, true); assert.equal(state.auth_subject, 'fixture-welcome-subject-A'); assert.equal(state.business_revoked, true);
    assert.equal(state.revoked, false); assert.equal(state.status, 'business_revoked'); assert.equal(state.current_business_identity, null); assert(e.oldPanel.inert);
    assert.equal(revokedEvents(e).length, 1); assert.equal(revokedEvents(e)[0].detail.scope, 'business_identity'); assert.equal(e.navigations.length, 0);
    e.push(response(fixture({business_identity_status: 'revoked', current_business_identity: null})), completeLogout());
    assert.equal(await e.api.logout(e.button), true); assert.deepEqual(e.navigations, [entry]);
  }));
  await test('注销后忽略 abort 的迟到 session 响应不能重新恢复业务身份', () => withEnv({}, async e => {
    const d = deferred(); e.push(d.promise); const old = e.api.refresh(); await tick(); const oldRequest = e.requests.at(-1);
    e.push(response(fixture()), completeLogout()); assert.equal(await e.api.logout(e.button), true); assert.equal(oldRequest.options.signal.aborted, true);
    d.resolve(response(fixture({auth_subject: 'late-fixture-subject'}))); await old;
    assert.equal(e.api.getState().current_business_identity, null); assert.equal(e.api.getState().logout_status, 'complete'); assert.equal(businessEvents(e).length, 1);
  }));
  await test('JSON 解析迟到同样受 epoch 保护', () => withEnv({}, async e => {
    const d = deferred(); e.push(response({}, 200, () => d.promise)); const old = e.api.refresh(); await tick();
    e.channels.find(channel => channel.name === 'navren-welcome-auth').onmessage({data: {action: 'logout'}});
    d.resolve(fixture()); await old; assert.equal(e.api.getState().authenticated, false); assert.equal(businessEvents(e).length, 1); assert.deepEqual(e.navigations, [entry]);
  }));
  await test('真实 BankClient 的旧 GET 中止，迟到响应不能进入已退出的客户端', () => withEnv({bank: true}, async e => {
    const identity = {workspace_id: 'fixture-business', privacy_revision: 0, csrf: 'fixture-business-csrf'};
    e.window.BankClient.acceptSelectedSession(identity);
    const d = deferred(); let oldSignal; e.setPrivate(options => { oldSignal = options.signal; return d.promise; });
    const old = e.window.BankClient.get('/overview').catch(error => error); await tick();
    e.push(response(fixture()), completeLogout()); const pending = e.api.logout(e.button); assert(oldSignal.aborted); assert.equal(e.window.BankClient.identity(), null);
    d.resolve(response({...identity, fixture_private_for: 'old'})); const oldResult = await old; assert.equal(oldResult.code, 'STALE_RESPONSE'); assert.equal(await pending, true);
  }));
  await test('真实 finance 代理 CAS 409 封套由 BankClient 展示 message，消费 SESSION_CHANGED 并清身份/历史', () => withEnv({bank: true}, async e => {
    e.window.BankClient.acceptSelectedSession({workspace_id: 'fixture-business', privacy_revision: 0, csrf: 'fixture-business-csrf'});
    e.local.setItem('qingzhou_chatHistory', 'fixture-old-private-history'); e.local.setItem('qingzhou_chatKeyMoments', 'fixture-old-private-moments');
    const before = e.events.filter(event => event.type === 'navren:privacy-reset').length;
    e.setPrivate(() => response({error: {code: 'SESSION_CHANGED', message: '当前业务账户已变化，请重新读取。', welcome_code: 'WELCOME_BUSINESS_CHANGED'}}, 409));
    const error = await e.window.BankClient.get('/overview').catch(error => error);
    assert.equal(error.message, '当前业务账户已变化，请重新读取。'); assert.equal(error.code, 'SESSION_CHANGED'); assert.equal(error.status, 409);
    assert.equal(e.window.BankClient.identity(), null); assert.equal(e.local.getItem('qingzhou_chatHistory'), null); assert.equal(e.local.getItem('qingzhou_chatKeyMoments'), null);
    assert.equal(e.events.filter(event => event.type === 'navren:privacy-reset').length, before + 1);
    assert.equal(e.navigations.length, 0, '业务 CAS 不冒充欢迎主体退出成功');
  }));
  await test('finance 候选服务不可用的非 2xx 必须保留失败结果，不能当接线成功', () => withEnv({bank: true}, async e => {
    e.window.BankClient.acceptSelectedSession({workspace_id: 'fixture-business', privacy_revision: 0, csrf: 'fixture-business-csrf'});
    e.setPrivate(() => response({error: {code: 'UPSTREAM_UNAVAILABLE', message: '业务候选服务尚不可用。'}}, 503));
    const error = await e.window.BankClient.get('/overview').catch(error => error);
    assert.equal(error.code, 'UPSTREAM_UNAVAILABLE'); assert.equal(error.message, '业务候选服务尚不可用。'); assert.equal(error.status, 503);
    assert.equal(e.window.BankClient.identity().workspace_id, 'fixture-business'); assert.equal(e.navigations.length, 0);
  }));
  await test('退出 privacy-reset 自动调用真实 BankClient.refreshIdentity 时，门禁拒绝新业务 POST/资料请求', () => withEnv({bank: true}, async e => {
    e.window.BankClient.acceptSelectedSession({workspace_id: 'fixture-business', privacy_revision: 0, csrf: 'fixture-business-csrf'});
    let automatic;
    e.window.addEventListener('navren:privacy-reset', () => { automatic = e.window.BankClient.refreshIdentity().catch(error => error); });
    e.setPrivate(() => { throw Error('退出期间不应到达业务 transport'); });
    e.push(response(fixture()), response({}, 502)); assert.equal(await e.api.logout(e.button), false); await tick();
    const error = await automatic; assert.equal(error.code, 'WELCOME_IDENTITY_REVOKED'); assert.equal(e.window.BankClient.identity(), null);
    assert.equal(e.requests.filter(request => request.path.startsWith('/api/finance/')).length, 0);
    await assert.rejects(e.window.fetch('/api/agent'), error => error.code === 'WELCOME_IDENTITY_REVOKED');
    assert.equal(e.navigations.length, 0);
  }));
  await test('业务身份 revoked 的 privacy-reset 也不能自动重建真实 BankClient context', () => withEnv({bank: true}, async e => {
    e.window.BankClient.acceptSelectedSession({workspace_id: 'fixture-business', privacy_revision: 0, csrf: 'fixture-business-csrf'});
    let automatic; e.window.addEventListener('navren:privacy-reset', () => { automatic = e.window.BankClient.refreshIdentity().catch(error => error); });
    e.push(response(fixture({business_identity_status: 'revoked', current_business_identity: null}))); await e.api.refresh(); await tick();
    assert.equal((await automatic).code, 'WELCOME_IDENTITY_REVOKED'); assert.equal(e.requests.filter(request => request.path.startsWith('/api/finance/')).length, 0);
    assert.equal(e.window.BankClient.identity(), null); assert.equal(e.api.getState().authenticated, true);
  }));
  await test('同欢迎主体已核验正常切号后，真实 BankClient 可按当前 Cookie 重读身份', () => withEnv({bank: true}, async e => {
    e.window.BankClient.acceptSelectedSession({workspace_id: 'fixture-old', privacy_revision: 0, csrf: 'fixture-old-csrf'});
    const current = {workspace_id: 'fixture-new', privacy_revision: 1, csrf: 'fixture-new-csrf'};
    let automatic; e.window.addEventListener('navren:privacy-reset', () => { automatic = e.window.BankClient.refreshIdentity().catch(error => error); });
    e.setPrivate(() => response(current));
    e.push(response(fixture({current_business_identity: {environment: 'simulation', account_id: 'grain_farmer', workspace_id: 'fixture-new', privacy_revision: 1}})));
    await e.api.refresh(); await tick(); const actual = await automatic;
    assert.equal(actual.workspace_id, 'fixture-new'); assert.equal(e.window.BankClient.identity().workspace_id, 'fixture-new'); assert.equal(e.navigations.length, 0);
    assert.equal(e.requests.filter(request => request.path === '/api/finance/session').length, 2);
  }));
  await test('同源业务门禁不拦静态/外域，只保留欢迎 auth 请求用于可靠退出重试', () => withEnv({}, async e => {
    e.push(response(fixture()), response({}, 502)); await e.api.logout();
    assert.equal((await e.window.fetch('/assets/fixture.svg')).ok, true);
    assert.equal((await e.window.fetch('https://outside.test/api/private')).ok, true);
    await assert.rejects(e.window.fetch('https://navren.test/api/finance/session'), error => error.code === 'WELCOME_IDENTITY_REVOKED');
    e.push(response(fixture()), completeLogout()); assert.equal(await e.api.logout(), true);
  }));
  await test('广播 login/切换只触发 GET 核验，不采用广播伪造的工作区', () => withEnv({}, async e => {
    const d = deferred(); e.push(d.promise); const channel = e.channels.find(channel => channel.name === 'navren-welcome-auth');
    channel.onmessage({data: {action: 'login', authenticated: true, auth_subject: 'forged', current_business_identity: {workspace_id: 'forged'}}});
    assert.equal(e.api.getState().auth_subject, 'fixture-welcome-subject-A'); assert.equal(e.navigations.length, 0);
    d.resolve(response(fixture())); await e.api.refresh(); assert.equal(e.api.getState().current_business_identity.workspace_id, 'fixture-workspace-tycoon');
  }));
  await test('兼容捕获旧 onclick logout()，屏蔽旧假退出且无新增按钮', () => withEnv({}, async e => {
    delete e.button.attrs['data-navren-logout']; e.button.attrs.onclick = 'logout()'; const child = new Element('span'); e.button.append(child);
    let fakeLogout = 0; e.document.addEventListener('click', () => { fakeLogout++; }); e.push(response(fixture()), completeLogout());
    const click = e.click(child); await tick(); assert.equal(click.defaultPrevented, true); assert.equal(fakeLogout, 0); assert.equal(e.document.querySelectorAll('button').length, 1); assert.deepEqual(e.navigations, [entry]);
  }));
  await test('返回体验选择仅导航固定同源路径，不请求或创建任何业务会话', () => withEnv({}, async e => {
    const count = e.requests.length; e.api.returnToExperienceSelection('https://forbidden.example'); assert.deepEqual(e.navigations, [entry]); assert.equal(e.requests.length, count);
  }));
  await test('API 传入的现有按钮无 data 属性也可在失败后点击重试', () => withEnv({}, async e => {
    delete e.button.attrs['data-navren-logout']; e.push(response(fixture()), response({}, 502)); await e.api.logout(e.button);
    e.push(response(fixture()), completeLogout()); const event = e.click(); await tick();
    assert(event.defaultPrevented); assert.equal(e.api.getState().logout_status, 'complete'); assert.deepEqual(e.navigations, [entry]);
  }));
  await test('pagehide 时退出 transport 忽略 abort，pageshow 仍能新发重试，旧回执不覆盖', () => withEnv({}, async e => {
    const d = deferred(); e.push(response(fixture()), d.promise); const old = e.api.logout(e.button); await tick();
    const oldSignal = e.requests.at(-1).options.signal; e.signal('pagehide', {persisted: true}); assert(oldSignal.aborted); assert.equal(e.button.disabled, false);
    e.signal('pageshow', {persisted: true}); e.push(response(fixture()), completeLogout()); assert.equal(await e.api.logout(e.button), true);
    d.resolve(completeLogout()); assert.equal(await old, false); assert.equal(e.broadcasts.length, 1); assert.equal(e.api.getState().logout_status, 'complete');
  }));
  await test('pagehide 取消旧读；bfcache pageshow 复核，旧页面响应不覆盖新身份', () => withEnv({}, async e => {
    const d = deferred(); e.push(d.promise); const old = e.api.refresh(); await tick(); const oldSignal = e.requests.at(-1).options.signal;
    e.signal('pagehide', {persisted: true}); assert(oldSignal.aborted); assert.equal(e.api.getState().current_business_identity, null);
    e.push(response(fixture({current_business_identity: {environment: 'simulation', account_id: 'retired_entrepreneur', workspace_id: 'fixture-retired', privacy_revision: 0}})));
    e.signal('pageshow', {persisted: true}); await e.api.refresh(); d.resolve(response(fixture())); await old;
    assert.equal(e.api.getState().current_business_identity.account_id, 'retired_entrepreneur'); assert.equal(e.navigations.length, 0);
  }));
  const passed = results.filter(result => result.passed).length;
  console.log(JSON.stringify({passed, total: results.length, network: 'forbidden; isolated fetch fixtures only',
    source_hashes: Object.fromEntries(sourceFiles.map(path => [path, crypto.createHash('sha256').update(sources[path]).digest('hex')])), failures: results.filter(result => !result.passed)}, null, 2));
  if (passed !== results.length) process.exitCode = 1;
})().catch(error => { console.error(error); process.exitCode = 1; });
