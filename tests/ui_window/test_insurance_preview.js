/* 保险预览独立离线验收：真实选择器与 UI + 严格绑定的 JSDOM 合同夹具。
 * 禁止网络、数据库、模型、真实投保；本测试不替代真实 HTTP 和浏览器布局验收。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { JSDOM } = require('jsdom');
const base = path.resolve(__dirname, '../..');
const read = file => fs.readFileSync(path.join(base, file), 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));
const suite = [], failures = [];
const tick = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
const source = read('js/discover_insurance.js'), selectorSource = read('js/insurance_needs.js');
const revision = 6, asOf = '2026-10-08';

function product(id, name, audience = '个人客户', overrides = {}) {
  return { id, name, source_scope: 'public_catalog', version: 'catalogue-fixture-v1', as_of: asOf,
    raw: { code: id, name, type: '保险', publish: '夹具保险公司', customer_group_type: audience,
      investor: '正式披露的投保条件原文', charge: '正式披露的费用原文', charge_way: '按官方原文',
      risk: '正式披露的风险原文', source_date: asOf, source_url: 'https://official.invalid/disclosure/' + id,
      sale_status: '快照状态', ...overrides } };
}
const products = [
  product('pension-personal', '个人商业养老金'), product('health-personal', '个人医疗保险'),
  product('accident-personal', '个人意外伤害保险'), product('pension-institution', '机构养老保险', '机构客户'),
  product('company-liability', '公众责任保险', '机构客户'), product('unknown-audience', '待核对保险', '')
];
const personaKinds = {
  tycoon: { assets: ['property'], policies: ['health', 'public_liability'] },
  grain_farmer: { assets: ['crop', 'equipment'], policies: ['public_liability'] },
  working_landlady: { assets: ['property'], policies: ['health'] },
  retired_entrepreneur: { assets: [], policies: ['health', 'accident'] }
};
function privateContext(id = 'working_landlady', kinds = personaKinds[id] || { assets: [], policies: [] }) {
  const identity = { account_id: id, workspace_id: 'workspace-' + id, privacy_revision: revision };
  const assets = kinds.assets.map((kind, i) => ({ subject_id: id, asset_id: id + '-asset-' + i,
    asset_type: kind, name: id + ' 的 ' + kind + ' 档案', value_minor: 10000 }));
  const connections = [{ subject_id: id, connection_id: id + '-insurer', institution_type: 'insurance' }];
  const policies = kinds.policies.map((kind, i) => ({ subject_id: id, policy_id: id + '-policy-' + i,
    insurance_type: kind, status: 'active', start_date: '2025-01-01', end_date: '2030-12-31',
    policyholder_id: id, insured_id: id, covered_asset_id: null,
    insurer_connection_id: id + '-insurer', contract_id: id + '-contract-' + i }));
  const contracts = policies.map((p, i) => ({ subject_id: id, contract_id: p.contract_id,
    asset_id: null, name: id + ' 的保单 ' + i }));
  const overview = { schema_version: 'navren.financial.v1', source: 'navren_synthetic',
    read_only: true, runtime_ledger_included: false, workspace_binding: identity.workspace_id,
    workspace_id: identity.workspace_id, subject_id: id, privacy_revision: revision,
    dataset_version: 'insurance-fixture-dataset-v1', as_of: asOf, data: { assets, policies, contracts, connections } };
  const memories = { workspace_id: identity.workspace_id, subject_id: id, privacy_revision: revision,
    enabled: true, personal_revision: 2, as_of: asOf, items: [] };
  return { identity, overview, memories };
}
function confirmedMemory(value, extras = {}) {
  return { node_id: 'need-1', user_confirmed: true, source_type: 'conversation_user_statement',
    category: 'product_interest', value, ...extras };
}
function fixture(options = {}) {
  const dom = new JSDOM('<!doctype html><html><body><h2 id="insurancePreviewHeading">保险<span id="insuranceHelpHost"></span></h2><div id="insurancePreview"></div><div id="list"></div><div id="detail"></div></body></html>',
    { url: 'https://navren.invalid/recommend.html', runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, d = w.document, opens = [], updates = [], asks = [], network = [];
  let current = clone(options.current || options.context?.identity || privateContext().identity);
  let view = { view: 'home', filters: {} };
  for (const kind of ['fetch', 'XMLHttpRequest', 'WebSocket']) w[kind] = (...args) => {
    network.push({ kind, args }); throw Error('保险离线验收禁止网络');
  };
  w.NavrenI18n = { language: 'zh-CN' };
  w.BankClient = { identity: () => clone(current) };
  w.NavrenViews = {
    current: () => clone(view),
    url: meta => { const u = new URL('/' + (meta.page || 'recommend') + '.html', w.location.href);
      for (const [k, value] of Object.entries(meta)) if (k !== 'page' && value != null && typeof value !== 'object') u.searchParams.set(k, value);
      return u.pathname + u.search; },
    open: async (name, meta) => { opens.push({ name, meta: clone(meta) }); },
    update: meta => { updates.push(clone(meta)); view = { ...view, ...clone(meta) }; },
    back: () => opens.push({ name: 'back' }),
    ask: async (...args) => { asks.push(clone(args)); }
  };
  if (options.storage) for (const [k, value] of Object.entries(options.storage)) w.localStorage.setItem(k, value);
  // Storage is a JSDOM exotic object; instance method replacement is silently ignored.
  // Replace this isolated window's prototype so denial really reaches production code.
  if (options.storageReadFailure) Object.defineProperty(w.Storage.prototype, 'getItem', { value: () => { throw Error('storage unavailable'); } });
  if (options.storageWriteFailure) Object.defineProperty(w.Storage.prototype, 'setItem', { value: () => { throw Error('storage unavailable'); } });
  w.eval(selectorSource); w.eval(source);
  const host = d.getElementById('insurancePreview'), list = d.getElementById('list'), detail = d.getElementById('detail');
  const context = options.context || privateContext();
  const mount = (newContext = context) => w.DiscoverInsurance.preview(host, products, newContext);
  const storage = () => Object.fromEntries(Array.from({ length: w.localStorage.length }, (_, i) => w.localStorage.key(i)).map(k => [k, w.localStorage.getItem(k)]));
  const close = () => { assert.equal(network.length, 0); dom.window.close(); };
  return { w, d, dom, host, list, detail, context, opens, updates, asks, network, mount, storage, close,
    setCurrent: i => { current = clone(i); }, setView: v => { view = clone(v); } };
}
const key = i => 'navren_insurance_focus:' + i.workspace_id + ':' + i.account_id;
const needs = c => Array.from(c.host.querySelectorAll('.insurance-need-card'), n => n.dataset.need);
const clickText = (host, text) => { const b = Array.from(host.querySelectorAll('button')).find(n => n.textContent === text); assert.ok(b, '缺少按钮 ' + text); b.click(); return b; };
const snapshot = c => JSON.stringify(c.storage());
const storedPreferences = keys => ({ [key(privateContext().identity)]: JSON.stringify({ schema: 1, keys }) });
function changePreferences(c, keys, event = 'navren:insurance-preferences-updated') {
  const storageKey = key(c.context.identity);
  if (keys) c.w.localStorage.setItem(storageKey, JSON.stringify({ schema: 1, keys }));
  else c.w.localStorage.removeItem(storageKey);
  c.w.dispatchEvent(event === 'storage'
    ? new c.w.StorageEvent('storage', { key: storageKey, storageArea: c.w.localStorage })
    : new c.w.CustomEvent(event, { detail: clone(c.context.identity) }));
}
function assertPreviewChrome(c) {
  assert.equal(c.host.querySelector('.insurance-editor,.insurance-adjust,.insurance-choice'), null);
  assert.ok(!c.host.textContent.includes('调整需求'));
  const help = c.d.querySelector('#insuranceHelpHost > details.insurance-help');
  assert.ok(help, '保险帮助应位于标题后'); assert.equal(c.host.querySelector('.insurance-help'), null);
  assert.equal(help.querySelector('summary').getAttribute('aria-label'), '展示依据');
  const all = c.host.querySelector('.insurance-all-link'); assert.ok(all, '应保留全部保险入口');
  const allUrl = new URL(all.href); assert.equal(allUrl.pathname, '/recommend.html'); assert.equal(allUrl.searchParams.get('view'), 'insurance');
  const settings = Array.from(c.host.querySelectorAll('a')).find(a => a.textContent === '设置保障偏好');
  if (c.host.querySelector('[data-need="setup"]')) {
    assert.ok(settings, '资料空位应引导至我的保障偏好');
    const url = new URL(settings.href); assert.equal(url.pathname, '/mine.html'); assert.equal(url.searchParams.get('view'), 'home');
    assert.equal(url.searchParams.get('focustarget'), 'mineInsurancePreferences');
  } else assert.equal(settings, undefined, '已有三方向时不重复放置偏好调整入口');
  assert.ok(!c.host.textContent.includes('官网披露')); assert.ok(!c.host.textContent.includes('微众官网保险目录 ›'));
}
async function check(name, fn) {
  try { await fn(); suite.push({ name, passed: true }); }
  catch (error) { suite.push({ name, passed: false, error: error.message }); failures.push({ name, error: error.stack }); }
}
async function scenario(fn, options) { const c = fixture(options); try { await fn(c); } finally { c.close(); } }

async function run() {
  await check('四体验主体依据各自已接入保障对象呈现不同方向，不按目录前三项复制', async () => {
    const seen = [];
    for (const id of Object.keys(personaKinds)) await scenario(c => {
      c.mount(); const keys = needs(c); assert.ok(keys.length <= 3); assert.ok(!keys.includes('pension'));
      seen.push(keys.join(',')); assert.ok(!c.host.textContent.includes('机构养老保险'));
    }, { context: privateContext(id) });
    assert.equal(new Set(seen).size, 4);
  });
  await check('空档案请求本人补充需求并链接我的偏好卡，不伪造三个统一推荐', () => scenario(c => {
    c.mount(); assert.deepEqual(needs(c), ['setup']); assert.ok(c.host.textContent.includes('补充你的保障需求')); assertPreviewChrome(c);
  }, { context: privateContext('no-records') }));
  await check('预置三项保障偏好按本人顺序展示，发现页无编辑器和重复调整入口', () => scenario(c => {
    const before = snapshot(c); c.mount(); assert.deepEqual(needs(c), ['pension', 'accident', 'health']);
    assertPreviewChrome(c); assert.equal(snapshot(c), before, '发现预览不得写入偏好');
    assert.ok(c.d.getElementById('insuranceHelpHost').textContent.includes('保单资料截至 ' + asOf));
  }, { storage: storedPreferences(['pension', 'accident', 'health']) }));
  await check('查看与收起本人保障不改变偏好或本地存储', () => scenario(c => {
    c.mount(); const before = snapshot(c); clickText(c.host.querySelector('[data-need="health"]'), '查看保障 ›');
    assert.ok(c.host.querySelector('.insurance-policy-panel')); clickText(c.host, '收起');
    assert.equal(snapshot(c), before); assert.deepEqual(needs(c), ['pension', 'health', 'accident']); assertPreviewChrome(c);
  }, { storage: storedPreferences(['pension', 'health', 'accident']) }));
  await check('设置页更新事件立即采用已保存偏好顺序而不创建首页编辑器', () => scenario(c => {
    c.mount(); changePreferences(c, ['pension', 'accident', 'property']);
    assert.deepEqual(needs(c), ['pension', 'accident', 'property']); const before = snapshot(c); assertPreviewChrome(c);
    c.w.dispatchEvent(new c.w.CustomEvent('navren:insurance-preferences-updated', { detail: clone(c.context.identity) }));
    assert.equal(snapshot(c), before); assert.deepEqual(needs(c), ['pension', 'accident', 'property']);
  }));
  await check('预置偏好在完整新页面重建后恢复', async () => {
    let stored;
    await scenario(c => { c.mount(); assert.deepEqual(needs(c).slice(0, 2), ['pension', 'accident']); stored = c.storage(); }, { storage: storedPreferences(['pension', 'accident']) });
    await scenario(c => { c.mount(); assert.deepEqual(needs(c).slice(0, 2), ['pension', 'accident']); }, { storage: stored });
  });
  await check('工作区与主体均隔离本地选择，不借用其他账户关注项', async () => {
    const original = privateContext('working_landlady'), stored = { [key(original.identity)]: JSON.stringify({ schema: 1, keys: ['pension'] }) };
    await scenario(c => { c.mount(); assert.ok(!needs(c).includes('pension')); }, { context: privateContext('grain_farmer'), storage: stored });
    const otherWorkspace = privateContext('working_landlady'); otherWorkspace.identity.workspace_id = 'alternate-workspace';
    otherWorkspace.overview.workspace_binding = otherWorkspace.overview.workspace_id = otherWorkspace.identity.workspace_id;
    otherWorkspace.memories.workspace_id = otherWorkspace.identity.workspace_id;
    await scenario(c => { c.mount(); assert.ok(!needs(c).includes('pension')); }, { context: otherWorkspace, storage: stored });
  });
  await check('设置页清除偏好后的事件恢复当前绑定档案', () => scenario(c => {
    c.mount(); assert.equal(needs(c)[0], 'pension'); changePreferences(c, null);
    assert.equal(c.w.localStorage.getItem(key(c.context.identity)), null); assert.deepEqual(needs(c), ['property', 'health', 'setup']);
  }, { storage: storedPreferences(['pension']) }));
  await check('浏览器拒绝写入时只读发现预览仍展示绑定档案且不尝试保存', () => scenario(c => {
    c.mount(); assert.deepEqual(needs(c), ['property', 'health', 'setup']); assertPreviewChrome(c);
    clickText(c.host.querySelector('[data-need="health"]'), '查看保障 ›'); clickText(c.host, '收起');
    assert.equal(c.w.localStorage.length, 0); assert.ok(!c.host.textContent.includes('浏览器未能保存'));
  }, { storageWriteFailure: true }));
  await check('浏览器拒绝本地读取时仍读取绑定档案，不显示别人的偏好', () => scenario(c => {
    c.mount(); assert.deepEqual(needs(c), ['property', 'health', 'setup']);
  }, { storageReadFailure: true }));
  await check('有效本人确认需求优先于旧保单与资产方向', () => {
    const context = privateContext(); context.memories.items = [confirmedMemory('我希望补充养老规划')];
    return scenario(c => { c.mount(); assert.equal(needs(c)[0], 'pension'); assert.equal(c.host.querySelector('.insurance-need-badge').textContent, '已确认需求'); }, { context });
  });
  for (const [name, extras, enabled] of [
    ['关闭记忆授权', {}, false], ['过期需求', { expires_on: '2026-10-07' }, true],
    ['已删除需求', { deleted: true }, true], ['未确认资料', { user_confirmed: false }, true],
    ['候选资料', { status: 'candidate' }, true], ['模型推断', { source_type: 'assistant_inference' }, true]
  ]) await check(name + '不得用于个人保障展示', () => {
    const context = privateContext(); context.memories.enabled = enabled;
    context.memories.items = [confirmedMemory('我希望补充养老规划', extras)];
    return scenario(c => { c.mount(); assert.ok(!needs(c).includes('pension')); }, { context });
  });
  await check('否定、假设、他人陈述不转成当前用户的保障需求', () => {
    const context = privateContext(); context.memories.items = ['我不需要养老规划', '如果我想养老规划', '我的朋友需要养老规划']
      .map((value, i) => confirmedMemory(value, { node_id: 'hypothesis-' + i }));
    return scenario(c => { c.mount(); assert.ok(!needs(c).includes('pension')); }, { context });
  });
  for (const [name, change] of [
    ['工作区错绑', c => c.overview.workspace_binding = 'foreign'],
    ['主体错绑', c => c.overview.subject_id = 'foreign'],
    ['授权版本错绑', c => c.overview.privacy_revision++],
    ['保单含其他主体', c => c.overview.data.policies[0].subject_id = 'foreign'],
    ['本人确认资料错绑', c => c.memories.subject_id = 'foreign']
  ]) await check(name + '不得展示私有保障资料', () => {
    const context = privateContext(); change(context);
    return scenario(c => { c.mount(); assert.deepEqual(needs(c), ['setup']); assert.ok(!c.host.textContent.includes('份已接入保单'));
      assert.ok(!c.host.textContent.includes('working_landlady 的 property 档案')); }, { context });
  });
  await check('当前 BankClient 主体已变时，旧 context 不得初次挂载私有卡片', () => {
    const context = privateContext(), current = privateContext('grain_farmer').identity;
    return scenario(c => { c.mount(); assert.ok(!c.host.textContent.includes('working_landlady')); assert.ok(!c.host.querySelector('[data-need="health"]')); }, { context, current });
  });
  await check('数据失败时只保留本人明确选择的公开方向，没有伪造保单或零保障', () => {
    const context = privateContext(); context.overview = null;
    const stored = { [key(context.identity)]: JSON.stringify({ schema: 1, keys: ['pension', 'health'] }) };
    return scenario(c => { c.mount(); assert.deepEqual(needs(c), ['pension', 'health', 'setup']);
      assert.ok(c.host.textContent.includes('保单资料暂未取得')); assert.ok(!c.host.textContent.includes('0 份')); }, { context, storage: stored });
  });
  await check('相关保险携带方向和个人客户过滤，后续目录不会混进机构产品', async () => scenario(async c => {
    c.mount();
    clickText(c.host.querySelector('[data-need="pension"]'), '相关保险 ›'); await tick();
    const route = c.opens.at(-1); assert.equal(route.name, 'insurance');
    assert.deepEqual(route.meta.filters, { insurance_need: 'pension', insurance_audience: 'personal' });
    c.setView({ view: 'insurance', filters: route.meta.filters }); c.w.DiscoverInsurance.render(c.list, products);
    assert.equal(c.list.querySelectorAll('.insurance-card').length, 1); assert.ok(c.list.textContent.includes('个人商业养老金'));
    assert.ok(!c.list.textContent.includes('机构养老保险'));
  }, { storage: storedPreferences(['pension']) }));
  await check('已有保障查看按保单 ID、财务历史来源和数据版本打开真实详情', async () => scenario(async c => {
    c.mount(); const health = c.host.querySelector('[data-need="health"]'); clickText(health, '查看保障 ›');
    const link = c.host.querySelector('.insurance-policy-link'); assert.ok(link);
    const url = new URL(link.href); assert.equal(url.pathname, '/account.html');
    assert.equal(url.searchParams.get('object_kind'), 'policy'); assert.equal(url.searchParams.get('object_id'), 'working_landlady-policy-0');
    assert.equal(url.searchParams.get('source_scope'), 'financial_history');
    assert.equal(url.searchParams.get('object_version'), c.context.overview.dataset_version);
    link.click(); await tick(); assert.deepEqual(c.opens.at(-1), { name: 'record-detail', meta: {
      page: 'account', object_kind: 'policy', object_id: 'working_landlady-policy-0', source_scope: 'financial_history', object_version: c.context.overview.dataset_version } });
    clickText(c.host, '收起'); assert.equal(c.host.querySelector('.insurance-policy-panel'), null);
  }));
  await check('隐私重置清空全部挂载及标题帮助，旧保障和公开目录操作不再跳转', async () => scenario(async c => {
    c.mount(); const oldCoverage = clickText(c.host.querySelector('[data-need="health"]'), '查看保障 ›');
    const oldPolicy = c.host.querySelector('.insurance-policy-link'), oldAll = c.host.querySelector('.insurance-all-link');
    c.w.DiscoverInsurance.render(c.list, products); c.w.DiscoverInsurance.detail(c.detail, products[0]);
    const oldProduct = c.list.querySelector('.insurance-card'); c.w.dispatchEvent(new c.w.Event('navren:privacy-reset'));
    assert.equal(c.host.textContent, ''); assert.equal(c.list.textContent, ''); assert.equal(c.detail.textContent, '');
    assert.equal(c.d.getElementById('insuranceHelpHost').textContent, '');
    oldCoverage.click(); oldPolicy.click(); oldAll.click(); oldProduct.click(); await tick(); assert.equal(c.w.localStorage.length, 0); assert.equal(c.opens.length, 0);
  }));
  await check('再次挂载后旧卡片按钮不会改写新挂载或清除新内容', () => scenario(c => {
    c.mount(); const oldCoverage = Array.from(c.host.querySelectorAll('button')).find(b => b.textContent === '查看保障 ›');
    c.mount(); const before = c.host.textContent; oldCoverage.click(); assert.equal(c.host.textContent, before); assert.equal(c.w.localStorage.length, 0);
  }));
  for (const event of ['navren:identity-revoked', 'navren:account-switched', 'pagehide']) await check(event + ' 清除当前私有保障和旧操作', async () => scenario(async c => {
    c.mount(); const oldRelated = Array.from(c.host.querySelectorAll('button')).find(b => b.textContent === '相关保险 ›');
    c.w.dispatchEvent(new c.w.Event(event)); assert.equal(c.host.textContent, ''); assert.equal(c.d.getElementById('insuranceHelpHost').textContent, ''); oldRelated.click();
    await tick(); assert.equal(c.w.localStorage.length, 0); assert.equal(c.opens.length, 0);
  }));
  await check('其他窗口更新相同主体偏好时同步重绘，其他主体存储事件不干扰', () => scenario(c => {
    c.mount(); changePreferences(c, ['pension'], 'storage');
    assert.equal(needs(c)[0], 'pension'); const before = c.host.textContent;
    c.w.dispatchEvent(new c.w.StorageEvent('storage', { key: 'navren_insurance_focus:foreign:foreign', storageArea: c.w.localStorage }));
    assert.equal(c.host.textContent, before);
  }));
  await check('使用真实 NavrenViews 左击保单链接确实跨到账户详情，不能留在发现页', async () => scenario(async c => {
    const navigated = []; c.d.body.dataset.page = 'recommend';
    c.w.NavrenApp = { href: url => url, navigate: url => navigated.push(url) };
    c.w.eval(read('js/view_context.js')); c.mount();
    clickText(c.host.querySelector('[data-need="health"]'), '查看保障 ›'); c.host.querySelector('.insurance-policy-link').click();
    await tick(); assert.equal(navigated.length, 1); const url = new URL(navigated[0], c.w.location.href);
    assert.equal(url.pathname, '/account.html'); assert.equal(url.searchParams.get('view'), 'record-detail');
    assert.equal(url.searchParams.get('object_kind'), 'policy'); assert.equal(url.searchParams.get('object_id'), 'working_landlady-policy-0');
  }));
  await check('原公开目录保留全部、个人、机构、未明确类型和搜索', () => scenario(c => {
    c.setView({ view: 'insurance', filters: {} }); c.w.DiscoverInsurance.render(c.list, products);
    assert.equal(c.list.querySelectorAll('.insurance-card').length, 6);
    clickText(c.list, '个人 3'); assert.equal(c.list.querySelectorAll('.insurance-card').length, 3);
    clickText(c.list, '机构 2'); assert.equal(c.list.querySelectorAll('.insurance-card').length, 2);
    clickText(c.list, '未明确 1'); assert.equal(c.list.querySelectorAll('.insurance-card').length, 1);
    clickText(c.list, '全部 6'); const search = c.list.querySelector('input[type="search"]'); search.value = '医疗';
    search.dispatchEvent(new c.w.Event('input', { bubbles: true })); assert.equal(c.list.querySelectorAll('.insurance-card').length, 1);
    assert.ok(c.list.textContent.includes('个人医疗保险')); assert.equal(c.updates.at(-1).filters.insurance_query, '医疗');
  }));
  await check('目录详情保留正式费用、条件、风险、官方链接和版本引用', async () => scenario(async c => {
    const p = products[1]; c.w.DiscoverInsurance.detail(c.detail, p);
    for (const expected of ['正式披露的投保条件原文', '正式披露的费用原文', '正式披露的风险原文', '按官方原文']) assert.ok(c.detail.textContent.includes(expected));
    const official = c.detail.querySelector('.insurance-source-link'); assert.equal(official.href, p.raw.source_url);
    assert.equal(official.target, '_blank'); assert.ok(official.rel.includes('noopener')); assert.ok(official.rel.includes('noreferrer'));
    clickText(c.detail, '问 Navren'); await tick(); assert.deepEqual(c.asks[0][1], { kind: 'product', id: p.id,
      source_scope: 'public_catalog', version: p.version, label: p.name });
    c.w.DiscoverInsurance.detail(c.detail, { ...p, version: null });
    assert.equal(Array.from(c.detail.querySelectorAll('button')).find(b => b.textContent === '问 Navren').disabled, true);
  }));
  await check('不安全官方链接不显示，目录仍保留披露原文', () => scenario(c => {
    c.w.DiscoverInsurance.detail(c.detail, product('unsafe', '医疗保险', '个人客户', { source_url: 'javascript:alert(1)' }));
    assert.equal(c.detail.querySelector('.insurance-source-link'), null); assert.ok(c.detail.textContent.includes('正式披露的风险原文'));
  }));
  await check('语言切换保留已保存偏好与产品原名，不重新改推荐顺序', () => scenario(c => {
    c.mount(); const before = needs(c);
    c.w.NavrenI18n.language = 'en'; c.w.dispatchEvent(new c.w.Event('navren:language'));
    assert.deepEqual(needs(c), before); assert.ok(c.host.textContent.includes('Retirement planning'));
    c.w.DiscoverInsurance.detail(c.detail, products[0]); assert.ok(c.detail.textContent.includes('个人商业养老金'));
  }, { storage: storedPreferences(['pension']) }));
  const hashes = {};
  for (const file of ['js/insurance_needs.js', 'js/discover_insurance.js', 'css/discover_insurance.css', 'js/discover_workspace.js', 'recommend.html', 'tests/ui_window/test_insurance_preview.js']) hashes[file] = crypto.createHash('sha256').update(read(file)).digest('hex');
  const result = { passed: failures.length === 0, checks: suite.length, passed_checks: suite.filter(t => t.passed).length,
    failed_checks: failures.length, scope: '严格绑定虚拟合同夹具 + 真实选择器及 UI 的离线 DOM 交互',
    command: 'NODE_PATH=/path/to/node_modules node tests/ui_window/test_insurance_preview.js',
    network_requests: 0, database_access: 0, model_calls: 0, real_transactions: 0,
    limits: ['没有执行实际 HTTP 请求或主体切换服务', 'JSDOM 不检查实际卡片布局、屏幕空间、触屏或键盘焦点视觉', '合成夹具不代表现实投保资格、真实保障缺口或实际承保可用性'],
    hashes, tests: suite, failures };
  const folder = path.join(base, 'outputs/window-progress/ui/insurance-preview-20261008'); fs.mkdirSync(folder, { recursive: true });
  fs.writeFileSync(path.join(folder, 'review.json'), JSON.stringify(result, null, 2) + '\n');
  fs.writeFileSync(path.join(folder, 'review.md'), '# 保险预览独立离线验收\n\n' +
    '结果：' + (result.passed ? '通过' : '未通过') + '；' + result.passed_checks + ' / ' + result.checks + ' 个检查通过。\n\n' +
    '使用真实 UI 和选择器、严格绑定的四主体合同夹具；网络、数据库、模型及真实交易调用均为 0。\n\n' +
    '命令：`' + result.command + '`\n\n' + suite.map(t => '- ' + (t.passed ? '通过' : '失败') + '：' + t.name + (t.error ? '；' + t.error : '')).join('\n') +
    '\n\n边界：实际 HTTP 接线、真实主体切换服务、浏览器布局与视觉未由该离线测试覆盖；不能据此宣称实际承保、保障缺口或投保资格。\n');
  console.log(JSON.stringify({ passed: result.passed, checks: result.checks, passed_checks: result.passed_checks, failed_checks: failures }));
  if (failures.length) process.exitCode = 1;
}
run().catch(error => { console.error(error); process.exitCode = 1; });
