/** 本机保险偏好的隔离存储与身份生命周期合同；禁止网络。 */
'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const { JSDOM } = require('jsdom');
const sourcePath = path.resolve(__dirname, '../../js/insurance_preferences.js'), source = fs.readFileSync(sourcePath, 'utf8');
const stylePath = path.resolve(__dirname, '../../css/insurance_preferences.css');
let assertions = 0, identityReads = 0, forbiddenCalls = 0, networkAttempts = 0;
const storage = new Map(), writes = [], instances = [];
const A = { workspace_id: 'fixture-a', account_id: 'subject-a', privacy_revision: 42 };
const B = { workspace_id: 'fixture-a', account_id: 'subject-b', privacy_revision: 42 };
const key = identity => 'navren_insurance_focus:' + identity.workspace_id + ':' + identity.account_id;
const clone = value => JSON.parse(JSON.stringify(value));
const same = (a, b) => a.workspace_id === b.workspace_id && a.account_id === b.account_id && a.privacy_revision === b.privacy_revision;
function check(value, label) { assert.ok(value, label); assertions++; }
function equal(actual, expected, label) { assert.deepEqual(clone(actual), expected, label); assertions++; }
const settle = async () => { for (let i = 0; i < 3; i++) await new Promise(resolve => setTimeout(resolve, 0)); };

function environment(initial = A, url = 'https://fixture.test/mine.html?view=home') {
  const dom = new JSDOM('<!doctype html><body data-page="mine"><main id="mineHome"><article id="mineInsurancePreferences" class="card mine-preference-card insurance-preferences" aria-labelledby="mineInsurancePreferencesTitle"></article></main><section id="settings" hidden></section>', { url, runScripts: 'outside-only', pretendToBeVisual: true });
  instances.push(dom); const w = dom.window, d = w.document; let identity = clone(initial), generation = 0, revision = 0, current = { page: 'mine', view: 'home' }, delay = null;
  const events = [];
  Object.defineProperty(w, 'localStorage', { value: {
    getItem(name) { return storage.get(name) ?? null; },
    setItem(name, value) { writes.push({ action: 'set', name, value }); storage.set(name, String(value)); },
    removeItem(name) { writes.push({ action: 'remove', name }); storage.delete(name); }
  } });
  w.BankClient = {
    async ensure() { identityReads++; const captured = clone(identity), pause = delay; delay = null; if (pause) await pause; return captured; },
    identity() { return clone(identity); },
    guard() { const captured = clone(identity), before = generation, view = revision; return { valid() { return before === generation && view === revision && same(captured, identity); } }; }
  };
  for (const method of ['get', 'post', 'postText', 'agentHeaders']) w.BankClient[method] = () => { forbiddenCalls++; throw Error('偏好组件不得读取业务或提交操作'); };
  w.fetch = () => { networkAttempts++; throw Error('测试禁止网络'); };
  w.XMLHttpRequest = class { constructor() { networkAttempts++; throw Error('测试禁止网络'); } };
  w.NavrenViews = { current: () => clone(current), revision: () => revision };
  w.NavrenI18n = { language: 'zh-CN' };
  w.addEventListener('navren:insurance-preferences-updated', event => events.push(clone(event.detail)));
  w.eval(source);
  const host = d.getElementById('mineInsurancePreferences');
  return {
    w, d, host, events, UI: w.NavrenInsurancePreferences,
    selected: () => [...host.querySelectorAll('[data-insurance-need][aria-pressed="true"]')].sort((a, b) => Number(a.textContent.split(' · ')[0]) - Number(b.textContent.split(' · ')[0])).map(button => button.dataset.insuranceNeed),
    choice: name => host.querySelector('[data-insurance-need="' + name + '"]'),
    action: name => host.querySelector('[data-insurance-action="' + name + '"]'),
    setIdentity(next) { identity = clone(next); generation++; },
    view(next) { current = { page: 'mine', view: next }; revision++; d.getElementById('mineHome').hidden = next !== 'home'; w.dispatchEvent(new w.CustomEvent('navren:view', { detail: clone(current) })); },
    delayIdentity() { let resolve; delay = new Promise(done => { resolve = done; }); return resolve; },
    event(name) { w.dispatchEvent(new w.Event(name)); },
    storageEvent(name) { w.dispatchEvent(new w.StorageEvent('storage', { key: name })); }
  };
}

(async () => {
  storage.set(key(A), JSON.stringify({ schema: 1, keys: ['property', 'health'] }));
  const e = environment(); await settle(); await e.UI.refresh();
  equal(e.UI.read(A), ['property', 'health'], '沿用旧存储key/schema及有序方向');
  equal(e.selected(), ['property', 'health'], '旧选择无需迁移显示在新卡');
  equal(e.host.querySelectorAll('[data-insurance-need]').length, 7, '仅提供七个原有方向');
  equal(e.host.querySelector('h3').textContent, '保险需求', '卡片沿用简洁保险需求标题');
  equal([...e.host.querySelectorAll('[data-insurance-need]')].map(button => button.textContent.replace(/^\d · /, '')), ['健康与医疗', '房产保障', '农业保障', '设备保障', '经营责任', '养老规划', '意外保障'], '七个方向沿用原有名称');
  check(!e.host.querySelector('.insurance-preferences-note'), '不插入多余说明段');
  equal(e.UI.read(B), [], '同workspace不同主体的偏好隔离');
  equal(e.UI.read({ ...A, workspace_id: 'other-workspace' }), [], '不同workspace的偏好隔离');
  equal(e.UI.read(null), [], '身份缺失不读取偏好');
  storage.set(key(B), JSON.stringify({ schema: 1, keys: ['liability', 'liability', 'bogus', 'health', 'pension', 'accident'] }));
  equal(e.UI.read(B), ['liability', 'health', 'pension'], '读取剔除非法方向、去重并限制三项');
  storage.set(key(B), '{bad'); equal(e.UI.read(B), [], '损坏存储不生成方向');
  storage.set(key(B), JSON.stringify({ schema: 2, keys: ['liability'] })); equal(e.UI.read(B), [], '未知schema不当作旧偏好');

  const beforeDraft = writes.length;
  e.choice('pension').click();
  equal(e.selected(), ['property', 'health', 'pension'], '第三个方向按本人选择顺序追加');
  check(e.choice('accident').disabled, '达到三项后禁用未选方向');
  e.choice('accident').onclick(); equal(e.selected(), ['property', 'health', 'pension'], '强制触发回调也不能超出三项');
  e.choice('health').click(); e.choice('accident').click();
  equal(e.selected(), ['property', 'pension', 'accident'], '移除一项后可选择另一方向并保留顺序');
  equal(writes.length, beforeDraft, '未保存草稿不写存储');
  e.action('cancel').click(); equal(e.selected(), ['property', 'health'], '取消恢复已保存方向');
  equal(writes.length, beforeDraft, '取消不写存储');

  e.choice('pension').click(); e.action('save').click();
  equal(JSON.parse(storage.get(key(A))), { schema: 1, keys: ['property', 'health', 'pension'] }, '保存写入兼容的真实夹具存储格式');
  equal(e.UI.read(A), ['property', 'health', 'pension'], '公开read读取刚保存的方向');
  equal(e.events.at(-1), A, '保存派发同页更新事件且只带身份字段');
  const reloaded = environment(); await settle(); await reloaded.UI.refresh();
  equal(reloaded.selected(), ['property', 'health', 'pension'], '新页面实例从存储恢复保存结果');
  reloaded.w.close();
  e.action('automatic').click();
  check(!storage.has(key(A)), '按已有资料展示删除显式偏好');
  equal(e.selected(), [], '按已有资料展示清除七项的显式选择');
  equal(e.UI.read(A), [], '自动展示后的read不造默认方向');

  storage.set(key(A), JSON.stringify({ schema: 1, keys: ['health'] })); e.storageEvent(key(A));
  equal(e.selected(), ['health'], '其他页面保存后无草稿时立即刷新');
  e.choice('property').click(); const dirty = e.selected();
  await e.UI.refresh(); equal(e.selected(), dirty, '同身份刷新保留未保存草稿');
  e.w.NavrenI18n.language = 'en'; e.event('navren:language');
  equal(e.selected(), dirty, '切换语言保留草稿');
  equal(e.host.querySelector('h3').textContent, 'Insurance interests', '独立卡片可切换语言');
  e.event('pageshow'); await settle(); equal(e.selected(), dirty, '同身份pageshow保留草稿');
  storage.set(key(A), JSON.stringify({ schema: 1, keys: ['pension'] })); e.storageEvent(key(A));
  equal(e.selected(), dirty, '跨页存储更新不覆盖未保存草稿');
  check(e.host.querySelector('[role="status"]').textContent.includes('unsaved'), '保留草稿时明确提示外部偏好更新');
  const beforeOtherStorage = e.host.innerHTML; e.storageEvent(key(B));
  equal(e.host.innerHTML, beforeOtherStorage, '其他主体存储事件不重绘本人的草稿');
  e.action('cancel').click(); equal(e.selected(), ['pension'], '取消回到最新外部保存值');

  e.choice('health').click(); const beforeView = e.selected(), hiddenSave = e.action('save'), beforeHiddenWrite = writes.length;
  e.view('settings'); hiddenSave.click(); await settle();
  equal(writes.length, beforeHiddenWrite, '离开我的首页后旧保存按钮不能写入');
  e.view('home'); await settle(); equal(e.selected(), beforeView, '同身份返回首页保留未保存方向');
  hiddenSave.click(); equal(writes.length, beforeHiddenWrite, '重绘后保留的旧节点不能写入');

  const staleSave = e.action('save'); e.setIdentity(B); staleSave.click();
  equal(writes.length, beforeHiddenWrite, '主体已变化时保存直接拒绝');
  e.event('navren:privacy-reset');
  equal(e.host.querySelectorAll('button').length, 0, '隐私失效立即清除旧编辑DOM');
  staleSave.click(); equal(writes.length, beforeHiddenWrite, '隐私失效后旧按钮不能写入');
  storage.set(key(B), JSON.stringify({ schema: 1, keys: ['liability'] })); e.event('navren:account-switched'); e.view('home'); await settle();
  equal(e.selected(), ['liability'], '新主体仅载入本人的偏好');
  equal(e.UI.read(A), ['pension'], '切换主体不覆盖原主体存储');

  const release = e.delayIdentity(), late = e.UI.refresh();
  e.event('navren:identity-revoked'); e.setIdentity(A); e.view('home'); await settle();
  equal(e.selected(), ['pension'], '身份撤销后新的视图载入当前主体');
  release(); await late; equal(e.selected(), ['pension'], '迟到的旧身份读取不能覆盖当前主体');
  const revisionSave = e.action('save'), beforeRevision = writes.length; e.setIdentity({ ...A, privacy_revision: 43 }); revisionSave.click();
  equal(writes.length, beforeRevision, '同主体授权版本变化仍拒绝旧保存');
  e.event('navren:privacy-reset'); equal(e.host.querySelectorAll('button').length, 0, '授权变化立即移除操作入口');
  e.view('home'); await settle(); equal(e.selected(), ['pension'], '新授权核对后仍兼容原本机偏好');
  e.event('pagehide'); equal(e.host.querySelectorAll('button').length, 0, '页面离开时清理旧编辑控件');
  e.event('pageshow'); await settle(); equal(e.selected(), ['pension'], '页面恢复重新核对身份并读取保存值');

  const noHost = environment(); noHost.host.remove(); noHost.d.body.dataset.page = 'recommend'; const beforeNoHost = identityReads;
  await settle(); equal(noHost.UI.read(A), ['pension'], '发现页仅用read可读取相同偏好');
  equal(identityReads, beforeNoHost, '发现页没有卡片时不提前读取身份');
  equal(forbiddenCalls, 0, '没有保单、业务POST或模型调用');
  equal(networkAttempts, 0, '全部存储与请求仅为内存夹具');

  // 使用真实视图恢复器验证URL规范化与最终默认定位的竞态。
  const linked = environment(A, 'https://fixture.test/mine.html?view=home&focustarget=mineInsurancePreferences');
  const scroll = linked.d.getElementById('mineHome'); scroll.className = 'page-scroll'; scroll.scrollTop = 123;
  const heading = linked.d.createElement('h1'); heading.id = 'mineViewTitle'; heading.dataset.viewHeading = ''; heading.textContent = '我的'; scroll.prepend(heading);
  let positioned = 0, finishRenderer;
  linked.host.scrollIntoView = () => { positioned++; scroll.scrollTop = 777; };
  const renderPending = new Promise(resolve => { finishRenderer = resolve; });
  linked.w.BankClient.refreshIdentity = async () => clone(A);
  linked.w.eval(fs.readFileSync(path.resolve(__dirname, '../../js/view_context.js'), 'utf8'));
  linked.w.NavrenViews.register('mine', async () => { await renderPending; });
  const restoring = linked.w.NavrenViews.start(); await settle();
  check(linked.w.document.documentElement.classList.contains('navren-private-locked'), '真实页面恢复期间保留身份锁');
  equal(new URL(linked.w.location.href).searchParams.get('focustarget'), null, '真实恢复器已规范化URL并删除定位参数');
  equal(linked.host.querySelectorAll('[data-insurance-need]').length, 7, '参数被删后新卡仍已挂载');
  equal(positioned, 0, '渲染未完成时不提前抢滚动和焦点');
  finishRenderer(); await restoring; await new Promise(resolve => setTimeout(resolve, 40));
  equal(positioned, 1, '恢复完成后从初始捕获参数定位一次');
  check(linked.d.activeElement === linked.host && scroll.scrollTop === 777, '卡片最终定位覆盖全局默认标题焦点与归零');
  scroll.scrollTop = 456; linked.choice('health').focus(); linked.event('pageshow'); await settle(); await new Promise(resolve => setTimeout(resolve, 40));
  equal(positioned, 1, '后续pageshow不会重复执行显式跳转定位');
  check(scroll.scrollTop === 456 && linked.d.activeElement === linked.choice('health'), '后续刷新保留用户滚动和当前控件焦点');
  process.stdout.write(JSON.stringify({ status: 'passed', assertions, fixture_storage_writes: writes.length, fixture_identity_reads: identityReads, financial_or_model_calls: forbiddenCalls, real_network_requests: networkAttempts,
    source_sha256: crypto.createHash('sha256').update(source).digest('hex'), css_sha256: crypto.createHash('sha256').update(fs.readFileSync(stylePath)).digest('hex'), test_sha256: crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex') }, null, 2) + '\n');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => instances.forEach(dom => dom.window.close()));
