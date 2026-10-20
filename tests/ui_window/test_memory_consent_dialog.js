/* 有限消费者检查：真实 mine/storage/BankClient；合成同源服务，无网络或数据库。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {JSDOM, VirtualConsole} = require('jsdom');
const root = path.resolve(__dirname, '../..');
const files = ['js/storage.js', 'js/bank_client.js', 'js/mine.js'];
const source = Object.fromEntries(files.map(file => [file, fs.readFileSync(path.join(root, file), 'utf8')]));
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const pinned = Object.fromEntries(files.map(file => [file, sha(source[file])]));
const clone = value => JSON.parse(JSON.stringify(value));
const output = process.argv[2];
const selected = new Set((process.argv[3] || '').split(',').filter(Boolean));
const results = [];
const monitored = ['navren:privacy-reset', 'navren:account-switched', 'navren:identity-revoked', 'navren:account-change', 'navren:view', 'pagehide'];

async function fixture({enabled = false, rejectPost = false} = {}) {
  const errors = [], calls = [], counts = {privacy: 0, updated: 0};
  const console = new VirtualConsole();
  console.on('jsdomError', error => errors.push(String(error)));
  const dom = new JSDOM(`<!doctype html><html style="overflow:auto"><body data-page="mine">
    <section id="appearance"><button id="memoryToggle" role="switch" aria-checked="false"></button>
    <p id="memoryStatus"></p><p id="mineConsentStatus"></p><div id="financialMemoryList"></div></section>
    <div id="archiveContent"></div><div id="riskDetail"></div><div id="bookmarksList"></div>
    <div id="modalOverlay" class="hidden"><div class="modal-foot"><button onclick="closeModal()">取消</button></div></div>
    </body></html>`, {url: 'https://navren.invalid/mine.html?view=settings', runScripts: 'outside-only', virtualConsole: console});
  const w = dom.window, d = w.document;
  const now = Date.now() / 1000;
  const service = {session: {
    registered_user_id: 'aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa', workspace_id: '11111111111141118111111111111111',
    account_id: 'tycoon', auth_subject: 'welcome-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', csrf: 'fixture-csrf-a',
    session_generation: 1, auth_expires_at: now + 7200, lease_expires_at: now + 3600,
    privacy_revision: 0, personal_revision: 17, environment: 'simulation'
  }, enabled, rejectPost, viewRevision: 1};
  const welcome = () => ({authenticated: true, registered_user_id: service.session.registered_user_id, auth_subject: service.session.auth_subject});
  w.NavrenWelcomeSession = {getState: welcome, refresh: async () => welcome()};
  w.NavrenViews = {revision: () => service.viewRevision};
  w.NavrenI18n = {t: text => text, language: 'zh-CN'};
  w.confirm = () => {throw new Error('Unexpected native confirm');};
  w.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const request = {path: String(url), method, body: options.body ? JSON.parse(options.body) : null};
    calls.push(request);
    let status = 200, data;
    const memories = () => ({workspace_id: service.session.workspace_id, registered_user_id: service.session.registered_user_id,
      privacy_revision: service.session.privacy_revision, personal_revision: service.session.personal_revision,
      enabled: service.enabled, items: []});
    if (request.path === '/api/finance/session' && method === 'GET') data = clone(service.session);
    else if (request.path === '/api/finance/memories' && method === 'GET') data = memories();
    else if (request.path === '/api/finance/memories/settings' && method === 'POST') {
      assert.deepEqual(Object.keys(request.body).sort(), ['enabled', 'expected_revision']);
      assert.equal(typeof request.body.enabled, 'boolean');
      assert.equal(request.body.expected_revision, service.session.personal_revision);
      if (service.rejectPost) {status = 503; data = {error: {code: 'FIXTURE_UNAVAILABLE', message: '合成服务暂不可用'}};}
      else {service.enabled = request.body.enabled; service.session.privacy_revision++; service.session.personal_revision++; data = memories();}
    } else throw new Error('Forbidden fixture request: ' + method + ' ' + request.path);
    return {ok: status < 400, status, json: async () => clone(data), headers: {get: name =>
      name === 'X-Navren-Workspace' && data.workspace_id ? data.workspace_id :
        name === 'X-Navren-Privacy-Revision' && data.privacy_revision !== undefined ? String(data.privacy_revision) : null}};
  };
  // JSDOM 缺原生 dialog；只补公开 show/close 事件语义，绝不补 guard 或授权结果。
  w.HTMLDialogElement.prototype.showModal = function() {this.setAttribute('open', '');};
  w.HTMLDialogElement.prototype.close = function() {this.removeAttribute('open'); this.dispatchEvent(new w.Event('close'));};
  const listeners = new Map(monitored.map(name => [name, new Set()]));
  const add = w.addEventListener.bind(w), remove = w.removeEventListener.bind(w);
  w.addEventListener = (name, handler, options) => {listeners.get(name)?.add(handler); return add(name, handler, options);};
  w.removeEventListener = (name, handler, options) => {listeners.get(name)?.delete(handler); return remove(name, handler, options);};
  // 不执行无关的全页启动/声音/偏好渲染；目标函数与隐私重置监听原样执行。
  const documentAdd = d.addEventListener.bind(d);
  d.addEventListener = (name, handler, options) => name === 'DOMContentLoaded' ? undefined : documentAdd(name, handler, options);
  w.eval(files.map(file => source[file]).join('\n;\n') + `\nwindow.__memoryConsentTest = {
    bumpSequence: () => { memoryReadSequence++; },
    replaceState: value => { financialMemoryState = value; },
    pending: () => memoryTogglePending
  };`);
  add('navren:privacy-reset', () => counts.privacy++);
  add('navren:memory-updated', () => counts.updated++);
  await w.BankClient.ensure();
  await w.loadFinancialMemories();
  assert.equal(w.BankClient.guard().valid(w.NavrenMineMemory.state()), true);
  const baseline = Object.fromEntries([...listeners].map(([name, handlers]) => [name, handlers.size]));
  const posts = () => calls.filter(call => call.method === 'POST');
  const reads = () => calls.filter(call => call.path === '/api/finance/memories' && call.method === 'GET');
  const dialog = () => d.querySelector('.memory-consent-dialog');
  function cleanupAssertion() {
    assert.equal(dialog(), null, 'dialog must be removed');
    assert.equal(d.documentElement.style.overflow, 'auto', 'scroll state restored');
    assert.equal(w.__memoryConsentTest.pending(), false, 'single-operation gate released');
    assert.equal(d.getElementById('memoryToggle').disabled, false);
    for (const [name, handlers] of listeners) assert.equal(handlers.size, baseline[name], name + ' listener leak');
  }
  return {w, d, dom, service, calls, get toasts() {return [...d.querySelectorAll('.toast')].map(node => node.textContent);}, counts, posts, reads, dialog, errors, cleanupAssertion,
    begin: () => w.toggleFinancialMemory(),
    allow: () => {assert.ok(dialog()); dialog().querySelector('.memory-consent-allow').click();},
    finish: () => {dom.window.close(); assert.deepEqual(errors, [], 'unexpected JSDOM errors');}};
}

async function check(id, name, operation, options) {
  if (selected.size && !selected.has(id)) return;
  let f;
  try {f = await fixture(options); await operation(f); f.cleanupAssertion(); f.finish(); results.push({id, name, passed: true});}
  catch (error) {try {f?.dom.window.close();} catch (_) {} results.push({id, name, passed: false, error: error.stack || String(error)});}
}

(async () => {
  await check('MC01', '明确允许只提交一次，真实隐私重置后成功状态仍可接收', async f => {
    const before = f.w.BankClient.guard();
    const pending = f.begin();
    assert.equal(f.posts().length, 0);
    assert.equal(f.dialog().open, true);
    assert.notEqual(f.d.activeElement, f.dialog().querySelector('.memory-consent-allow'));
    f.allow(); await pending;
    assert.equal(f.posts().length, 1);
    assert.deepEqual(f.posts()[0].body, {enabled: true, expected_revision: 17});
    assert.equal(before.valid(), false, 'original guard really invalidates after privacy revision');
    assert.equal(f.w.BankClient.identity().privacy_revision, 1);
    assert.equal(f.w.NavrenMineMemory.state().enabled, true);
    assert.equal(f.d.getElementById('memoryToggle').getAttribute('aria-checked'), 'true');
    assert.ok(f.counts.privacy >= 2);
    assert.equal(f.counts.updated, 1);
    assert.ok(f.toasts.some(text => text.includes('已允许')));
  });
  for (const mode of ['cancel', 'escape', 'x', 'close', 'backdrop']) {
    await check('MC02-' + mode, mode + ' 取消零请求、监听与滚动状态清理', async f => {
      const pending = f.begin(), dialog = f.dialog();
      if (mode === 'cancel') dialog.querySelector('.memory-consent-actions .p-btn').click();
      if (mode === 'escape') dialog.dispatchEvent(new f.w.Event('cancel', {cancelable: true}));
      if (mode === 'x') dialog.querySelector('.memory-consent-close').click();
      if (mode === 'close') dialog.close();
      if (mode === 'backdrop') {
        dialog.getBoundingClientRect = () => ({left: 20, top: 20, right: 300, bottom: 500});
        dialog.dispatchEvent(new f.w.MouseEvent('click', {bubbles: true, clientX: 1, clientY: 1}));
      }
      await pending; assert.equal(f.posts().length, 0); assert.equal(f.w.NavrenMineMemory.state().enabled, false);
    });
  }
  await check('MC03', '重复点击仅一弹窗且一次允许只提交一次', async f => {
    const first = f.begin(), second = f.begin();
    assert.equal(f.d.querySelectorAll('.memory-consent-dialog').length, 1);
    assert.equal(f.d.getElementById('memoryToggle').disabled, true);
    await second; f.allow(); await first; assert.equal(f.posts().length, 1);
  });
  for (const mode of ['owner', 'privacy', 'view', 'pagehide']) {
    await check('MC04-' + mode, mode + ' 变化自动取消且零设置请求', async f => {
      const pending = f.begin();
      if (mode === 'owner') {
        f.service.session = {...f.service.session, registered_user_id: 'bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb',
          workspace_id: '22222222222242228222222222222222', auth_subject: 'welcome-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', csrf: 'fixture-csrf-b'};
        f.w.BankClient.beginAccountSwitch();
        f.w.dispatchEvent(new f.w.Event('navren:account-switched'));
      }
      if (mode === 'privacy') {
        f.service.session.privacy_revision++;
        await f.w.BankClient.refreshIdentity();
      }
      if (mode === 'view') {f.service.viewRevision++; f.w.dispatchEvent(new f.w.Event('navren:view'));}
      if (mode === 'pagehide') f.w.dispatchEvent(new f.w.Event('pagehide'));
      await pending; assert.equal(f.posts().length, 0);
    });
  }
  for (const mode of ['revision', 'sequence', 'state']) {
    await check('MC05-' + mode, '无通知事件的 ' + mode + ' 变化，点允许仍零提交', async f => {
      const pending = f.begin(), state = f.w.NavrenMineMemory.state();
      if (mode === 'revision') state.personal_revision++;
      if (mode === 'sequence') f.w.__memoryConsentTest.bumpSequence();
      if (mode === 'state') f.w.__memoryConsentTest.replaceState({...state});
      f.allow(); await pending; assert.equal(f.posts().length, 0);
    });
  }
  await check('MC06', '设置失败后真实读取函数重读并恢复关闭状态', async f => {
    const reads = f.reads().length, pending = f.begin(); f.allow(); await pending;
    assert.equal(f.posts().length, 1); assert.equal(f.reads().length, reads + 1);
    assert.equal(f.counts.updated, 0); assert.equal(f.w.NavrenMineMemory.state().enabled, false);
    assert.equal(f.d.getElementById('memoryToggle').getAttribute('aria-checked'), 'false');
    assert.ok(f.toasts.some(text => text.includes('授权设置未更新')));
  }, {rejectPost: true});
  const drift = files.filter(file => sha(fs.readFileSync(path.join(root, file))) !== pinned[file]);
  const report = {status: results.every(item => item.passed) && !drift.length ? 'PASS_SCOPED_MEMORY_CONSENT_CONSUMER' : 'FAIL',
    passed: results.filter(item => item.passed).length, failed: results.filter(item => !item.passed).length,
    cases: results, requested_cases: [...selected], source_sha256: pinned, source_drift: drift,
    fixture: {actual_modules: files, actual_guard: true, actual_load_and_accept_mutation: true,
      synthesized: ['in-memory same-origin HTTP responses', 'verified welcome-session adapter', 'view revision adapter', 'dialog showModal/close event shim'],
      omitted: ['unrelated DOMContentLoaded full-page initialization', 'real backend authorization', 'CSS layout and browser focus rendering'],
      real_network_calls: 0, database_writes: 0, paid_calls: 0}};
  if (output) fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
  if (report.status !== 'PASS_SCOPED_MEMORY_CONSENT_CONSUMER') process.exitCode = 1;
})().catch(error => {console.error(error.stack || error); process.exitCode = 1;});
