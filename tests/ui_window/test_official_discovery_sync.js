/* 官网目录同步的禁网 DOM 验收：真实研究榜、保险组件与保障方向选择器。
 * 仅使用假 GET 响应和手动推进的五分钟时钟；不替代真实 HTTP 或浏览器布局验收。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { JSDOM, VirtualConsole } = require('jsdom');
const base = path.resolve(__dirname, '../..');
const sourceFiles = ['js/discover_workspace.js', 'js/discover_insurance.js', 'js/insurance_needs.js'];
const sources = Object.fromEntries(sourceFiles.map(file => [file, fs.readFileSync(path.join(base, file), 'utf8')]));
const sha256 = text => crypto.createHash('sha256').update(text).digest('hex');
const clone = value => JSON.parse(JSON.stringify(value));
const HEAD = '/public-products?limit=1&offset=0';
const INSURANCE = '/public-products?q=' + encodeURIComponent('保险') + '&limit=500&offset=0';
const RESEARCH = '/public-research-products';
const OVERVIEW = '/data/presentation?view=overview';
const MEMORIES = '/memories';
const OFFICIAL = 'https://www.webank.com/inquiryCenter/query';
const RANK = 'https://fund.eastmoney.com/data/fundranking.html';
const NOW = '2026-10-09T02:00:00.000Z', DATE = '2026-10-08';
const CATALOG_A = 'c'.repeat(64), CATALOG_B = 'd'.repeat(64), CONTENT_A = 'a'.repeat(64), CONTENT_B = 'b'.repeat(64);
let assertions = 0;
const checks = [], failures = [], totals = { reads: 0, writes: 0, financial_actions: 0, network_requests: 0 };
const ok = (value, label) => { assertions++; assert.ok(value, label); };
const eq = (actual, expected, label) => {
  assertions++;
  // DOM 的稳定性必须核对同一节点，不能只比较节点结构或文字。
  if (actual?.nodeType || expected?.nodeType) assert.equal(actual, expected, label);
  else if (Array.isArray(actual) && Array.isArray(expected) && expected.some(value => value?.nodeType)) {
    assert.ok(actual.length === expected.length && actual.every((value, i) => value === expected[i]), label);
  } else assert.deepEqual(actual, expected, label);
};
const pause = () => new Promise(resolve => setTimeout(resolve, 0));
async function stable(host, ready, label) {
  const deadline = Date.now() + 2000; let previous, turns = 0;
  while (Date.now() < deadline) {
    const next = host.innerHTML;
    turns = ready() && previous === next ? turns + 1 : 0;
    if (turns >= 2) return;
    previous = next; await pause();
  }
  throw Error('可见结果未稳定：' + label + '；' + host.textContent);
}
const flush = async () => { for (let i = 0; i < 6; i++) await pause(); };
function rawInsurance(code, name, extra = {}) {
  return { code, name, type: '保险', publish: '假响应保险公司', customer_group_type: '个人客户',
    investor: '正式投保条件原文', charge: '正式费用原文', charge_way: '官方原文', risk: '官方风险原文',
    source_date: '2026-09-28', source_url: OFFICIAL, sale_status: '快照状态', ...extra };
}
function head(version = CATALOG_A, status = 'ok', checked = NOW, extra = {}) {
  return { fetch_status: 'ok', status: 'ok', catalog_version: version, source_date: DATE,
    checked_at: '2026-01-01T00:00:00.000Z', total: 5, matched: 5, offset: 0, limit: 1, has_more: true,
    products: [{ code: '000005', name: '假响应股票基金', type: '股票基金' }],
    catalog_sync: { fetch_status: status, checked_at: checked, background_refresh: false, refresh_interval_seconds: 300 }, ...extra };
}
function board(catalog = CATALOG_A, content = CONTENT_A, prefix = '甲') {
  const products = [['000005', '股票基金', '3.500'], ['000004', '货币基金', '0.000'], ['000006', '债券基金', '-0.200']]
    .map(([code, type, value]) => ({ code, name: prefix + '目录' + type, type, risk: 'R3', manager: '假响应管理人',
      source_scope: 'public_product_research', executable: false, catalog_version: catalog, content_version: content,
      source_url: 'https://fund.eastmoney.com/' + code + '.html', rank_source_url: RANK, as_of: DATE, stale: false,
      metric: { type: 'one_week_return', label: '近1周涨跌幅', value_pct: value, as_of: DATE },
      raw: { source_period: '近1周', catalog_source_date: DATE } }));
  return { status: 'ok', metric_type: 'one_week_return', source_scope: 'public_product_research', executable: false,
    ranking_complete: true, catalog_version: catalog, content_version: content, catalog_source_date: DATE,
    provider: '天天基金／东方财富Choice公开数据', rank_source_url: RANK, stale_after_days: 7, products,
    coverage: { candidate_count: 5, source_count: 3, matched_count: 3, eligible_count: 3,
      type_counts: { 股票基金: 1, 货币基金: 1, 债券基金: 1, 保险: 2 } } };
}
function privateData(identity) {
  const id = identity.account_id;
  return { schema_version: 'navren.financial.v1', source: 'navren_synthetic', read_only: true,
    runtime_ledger_included: false, workspace_binding: identity.workspace_id, workspace_id: identity.workspace_id,
    subject_id: id, privacy_revision: identity.privacy_revision, dataset_version: 'offline-policy-v1', as_of: DATE,
    data: { assets: [], policies: [{ subject_id: id, policy_id: id + '-health', insurance_type: 'health', status: 'active',
      start_date: '2025-01-01', end_date: '2030-12-31', policyholder_id: id, insured_id: id, covered_asset_id: null,
      insurer_connection_id: id + '-insurer', contract_id: id + '-contract' }],
    contracts: [{ subject_id: id, contract_id: id + '-contract', asset_id: null, name: id + ' 的医疗保单' }],
    connections: [{ subject_id: id, connection_id: id + '-insurer', institution_type: 'insurance' }] } };
}
function fixture() {
  const console = new VirtualConsole(), browserErrors = [];
  console.on('jsdomError', error => browserErrors.push(error.message));
  const dom = new JSDOM('<!doctype html><html><body><span class="header-location"></span><h1 id="discoverHeading"></h1>' +
    '<p id="discoverIntro"></p><main id="discoverHome"><section><div id="productPreview"></div>' +
    '<details class="discover-research-help"><p></p></details></section><section><h2 id="insurancePreviewHeading">保险<span id="insuranceHelpHost"></span></h2><div id="insurancePreview"></div></section></main>' +
    '<div id="discoverSubview" hidden></div></body></html>',
  { url: 'https://navren.invalid/recommend.html', runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole: console });
  const w = dom.window, d = w.document, calls = [], writes = [], financial = [], network = [], queues = new Map(), timers = new Map(), timerDelays = [];
  let identity = { account_id: 'offline-a', workspace_id: 'offline-workspace-a', privacy_revision: 9, as_of: DATE }, epoch = 0, visibility = 'visible', sequence = 9000, view = { view: 'home' };
  const state = { head: head(), research: board(), insurance: [rawInsurance('health-a', '甲个人医疗保险'), rawInsurance('pension-a', '甲个人商业养老金')] };
  Object.defineProperty(d, 'visibilityState', { configurable: true, get: () => visibility });
  const RealDate = w.Date;
  w.Date = class extends RealDate { constructor(...args) { super(...(args.length ? args : [NOW])); } static now() { return RealDate.parse(NOW); } };
  const realSetTimeout = w.setTimeout.bind(w), realClearTimeout = w.clearTimeout.bind(w);
  w.setTimeout = (fn, delay, ...args) => {
    if (delay === 300000) { const id = ++sequence; timers.set(id, () => fn(...args)); timerDelays.push(delay); return id; }
    return realSetTimeout(fn, delay, ...args);
  };
  w.clearTimeout = id => { if (!timers.delete(id)) realClearTimeout(id); };
  w.setInterval = (...args) => { financial.push({ type: 'unbounded-interval', args }); throw Error('同步只能使用有界定时器'); };
  for (const type of ['fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource']) w[type] = function (...args) { network.push({ type, args }); throw Error('禁网测试拒绝真实网络'); };
  Object.defineProperty(w.navigator, 'sendBeacon', { value: (...args) => { network.push({ type: 'sendBeacon', args }); throw Error('禁网测试拒绝真实网络'); } });
  w.NavrenI18n = { language: 'zh-CN' };
  w.BankClient = {
    ensure: async () => clone(identity), identity: () => clone(identity), money: value => String(value / 100),
    guard() { const bound = clone(identity), revision = epoch; return { valid(data) { return revision === epoch &&
      bound.workspace_id === identity.workspace_id && bound.account_id === identity.account_id && bound.privacy_revision === identity.privacy_revision &&
      (!data || (!data.workspace_id || data.workspace_id === bound.workspace_id) && (!data.subject_id || data.subject_id === bound.account_id)); } }; },
    async get(request) {
      calls.push(request);
      if (![HEAD, INSURANCE, RESEARCH, OVERVIEW, MEMORIES].includes(request)) throw Error('未授权的读取接口：' + request);
      let value;
      if (request === HEAD) value = clone(state.head);
      if (request === RESEARCH) value = clone(state.research);
      if (request === INSURANCE) value = { catalog_version: state.head.catalog_version, source_date: state.head.source_date,
        matched: state.insurance.length, total: 5, limit: 500, offset: 0, has_more: false, products: clone(state.insurance) };
      if (request === OVERVIEW) value = privateData(identity);
      if (request === MEMORIES) value = { enabled: true, personal_revision: 1, as_of: DATE, items: [] };
      const queued = queues.get(request)?.shift();
      if (queued) { if (queued.gate) await queued.gate; if (queued.error) throw queued.error; if (queued.value !== undefined) value = clone(queued.value); }
      return value;
    }
  };
  for (const method of ['post', 'put', 'patch', 'delete']) w.BankClient[method] = (...args) => { writes.push({ method, args }); throw Error('浏览同步不能提交银行写入'); };
  w.BankUI = { amountForm: (...args) => { financial.push(args); throw Error('浏览同步不能准备金融动作'); } };
  w.Storage = { get: () => null, set: (...args) => { financial.push(args); throw Error('浏览同步不能保存金融操作'); } };
  w.MarketUI = { recommend: async () => {}, ask: (...args) => financial.push(args) };
  w.MarketHistory = { pause: () => {} };
  w.NavrenViews = { register: () => {}, current: () => clone(view), update: meta => { view = { ...view, ...clone(meta) }; },
    url: meta => { const u = new URL('/' + (meta.page || 'recommend') + '.html', w.location.href); for (const [key, value] of Object.entries(meta)) if (key !== 'page' && value != null && typeof value !== 'object') u.searchParams.set(key, value); return u.pathname + u.search; },
    open: (name, meta = {}) => render(name, meta), back: () => {}, reference: (...args) => financial.push(args), ask: (...args) => financial.push(args) };
  w.eval(sources['js/insurance_needs.js']); w.eval(sources['js/discover_insurance.js']); w.eval(sources['js/discover_workspace.js']);
  const target = d.getElementById('productPreview'), insurance = d.getElementById('insurancePreview'), subview = d.getElementById('discoverSubview');
  const render = async (name = 'home', meta = {}) => { view = { view: name, ...clone(identity), ...meta }; await w.DiscoverWorkspace.render(view); };
  const queue = (request, item) => { if (!queues.has(request)) queues.set(request, []); queues.get(request).push(item); };
  const defer = (request, value) => { let release; const gate = new Promise(resolve => { release = resolve; }); queue(request, { gate, value }); return release; };
  const fire = () => { const entry = timers.entries().next().value; assert.ok(entry, '五分钟定时器应已排程'); timers.delete(entry[0]); entry[1](); };
  const ready = () => target.querySelectorAll('.discover-preview').length === 3 && insurance.querySelector('.insurance-root') && !insurance.textContent.includes('正在读取');
  const home = async () => { await render(); await stable(d.getElementById('discoverHome'), ready, '研究榜与保险'); };
  const idle = async () => { await stable(d.getElementById('discoverHome'), () => timers.size === 1, '同步结束并再次排程'); };
  const setVisibility = value => { visibility = value; d.dispatchEvent(new w.Event('visibilitychange')); };
  const setIdentity = (value, event = 'navren:privacy-reset') => { identity = clone(value); epoch++; w.dispatchEvent(new w.Event(event)); };
  const close = () => {
    totals.reads += calls.length; totals.writes += writes.length; totals.financial_actions += financial.length; totals.network_requests += network.length;
    eq(writes.length, 0, '场景零银行写入'); eq(financial.length, 0, '场景零金融或模型动作'); eq(network.length, 0, '场景零真实网络请求');
    eq(browserErrors, [], '场景无未处理 DOM 错误'); ok(calls.every(request => [HEAD, INSURANCE, RESEARCH, OVERVIEW, MEMORIES].includes(request)), '读取仅使用明确白名单');
    dom.window.close();
  };
  return { w, d, target, insurance, subview, state, calls, timers, timerDelays, home, render, idle, fire, defer, queue, setVisibility, setIdentity, close,
    identity: () => clone(identity), cards: () => Array.from(target.querySelectorAll('.discover-preview')), headReads: () => calls.filter(x => x === HEAD).length,
    sourceReads: route => calls.filter(x => x === route).length, warnings: () => Array.from(d.querySelectorAll('.discover-sync-warning,.insurance-sync-warning'), n => n.textContent) };
}
function button(host, text) { const found = Array.from(host.querySelectorAll('button')).find(n => n.textContent === text); ok(found, '找到可见按钮：' + text); return found; }
const preferenceKey = identity => 'navren_insurance_focus:' + identity.workspace_id + ':' + identity.account_id;
function setPreferences(c, keys, type = 'navren:insurance-preferences-updated') {
  const identity = c.identity(), key = preferenceKey(identity);
  c.w.localStorage.setItem(key, JSON.stringify({ schema: 1, keys }));
  c.w.dispatchEvent(type === 'storage'
    ? new c.w.StorageEvent('storage', { key, storageArea: c.w.localStorage })
    : new c.w.CustomEvent(type, { detail: identity }));
}
function previewChrome(c) {
  eq(c.insurance.querySelector('.insurance-editor,.insurance-adjust,.insurance-choice'), null, '发现保险预览没有需求编辑器或调整按钮');
  ok(!c.insurance.textContent.includes('调整需求'), '不残留需求调整文字');
  const help = c.d.querySelector('#insuranceHelpHost > details.insurance-help');
  ok(help, '保险帮助位于标题后的 help host'); eq(c.insurance.querySelector('.insurance-help'), null, '帮助不占用保险卡片区');
  eq(help.querySelector('summary').getAttribute('aria-label'), '展示依据', '标题帮助具有可访问名称');
  for (const [root, text, view] of [[c.target.parentElement, '全部产品 ›', 'research'], [c.insurance, '全部保险 ›', 'insurance']]) {
    const links = Array.from(root.querySelectorAll('a')).filter(a => a.textContent === text);
    eq(links.length, 1, '全部入口只有一处：' + text);
    const url = new URL(links[0].href); eq(url.pathname, '/recommend.html', '全部入口属于发现页'); eq(url.searchParams.get('view'), view, '全部入口使用实际视图');
    ok(!root.textContent.includes('官网披露'), '首页不重新出现目录披露日期');
  }
  ok(!c.insurance.textContent.includes('微众官网保险目录 ›'), '删除的重复目录入口不复现');
  const setup = c.insurance.querySelector('[data-need="setup"]');
  if (setup) {
    const link = Array.from(setup.querySelectorAll('a')).find(a => a.textContent === '设置保障偏好'); ok(link, '资料空位提供设置偏好引导');
    const url = new URL(link.href); eq(url.pathname, '/mine.html', '偏好引导到我的页面'); eq(url.searchParams.get('view'), 'home', '打开我的主页');
    eq(url.searchParams.get('focustarget'), 'mineInsurancePreferences', '直接定位我的保障偏好卡');
  }
  for (const card of c.cards()) ok(card.textContent.includes('近1周涨跌幅') && card.textContent.includes('截至 ' + DATE), '保留卡片实际收益周期与截至日期');
}
async function scenario(name, fn) {
  const c = fixture(); let failure;
  try { await fn(c); } catch (error) { failure = error; }
  finally { try { c.close(); } catch (error) { failure ||= error; } }
  checks.push({ name, passed: !failure }); if (failure) failures.push({ name, error: failure.stack });
}
async function run() {
  await scenario('初次读取共享最新 head 并展示真实同步时间', async c => {
    await c.home(); eq(c.headReads(), 1, '研究与保险共享一次 limit1 最新目录读取');
    eq(c.sourceReads(INSURANCE), 1, '保险沿用 q保险 limit500 offset0'); eq(c.sourceReads(RESEARCH), 1, '研究榜沿用无参 GET');
    ok(c.calls.indexOf(HEAD) < c.calls.indexOf(INSURANCE) && c.calls.indexOf(HEAD) < c.calls.indexOf(RESEARCH), '先取得 head 再读取两个来源');
    eq(c.cards().length, 3, '完整研究榜显示三项'); ok(c.insurance.textContent.includes('1份已接入保单'), '真实选择器接受严格绑定的私有保单夹具');
    const researchLine = c.target.parentElement.querySelector('.discover-catalog-sync'), insuranceLine = c.insurance.querySelector('.insurance-catalog-sync');
    for (const line of [researchLine, insuranceLine]) {
      ok(!line.textContent.includes('官网披露'), '重复目录披露行已删除'); ok(line.textContent.includes('2026/10/9 10:00:00'), '最近校对来自 nested checked_at');
      ok(!line.textContent.includes('2026/1/1'), '不误用外层旧校对时间');
      const link = line.querySelector('a'); eq(link.href, OFFICIAL, '指向微众官网实际目录'); ok(link.rel.includes('noopener') && link.rel.includes('noreferrer'), '官网来源链接保护外部窗口');
    }
    previewChrome(c);
    ok(!/后台自动同步|后台自动更新/.test(c.d.body.textContent), 'background_refresh false 不宣称后台自动同步');
    eq(c.warnings(), [], '成功不显示失败提醒'); eq(c.timers.size, 1, '只排一个有界时钟'); eq(c.timerDelays, [300000], '同步间隔严格五分钟');
  });
  await scenario('等待期间保留内容，unchanged 保留已保存偏好、保单展开与焦点', async c => {
    await c.home(); setPreferences(c, ['pension', 'health'], 'storage');
    eq(Array.from(c.insurance.querySelectorAll('.insurance-need-card'), n => n.dataset.need), ['pension', 'health', 'setup'], 'storage 事件采用我的已保存偏好');
    button(c.insurance.querySelector('[data-need="health"]'), '查看保障 ›').click();
    const focused = button(c.insurance.querySelector('[data-need="health"]'), '查看保障 ›'); focused.focus();
    const panel = c.insurance.querySelector('.insurance-policy-panel'), saved = c.w.localStorage.getItem(preferenceKey(c.identity()));
    const cards = c.cards(), insuranceRoot = c.insurance.querySelector('.insurance-root');
    c.state.head = head(CATALOG_A, 'unchanged', '2026-10-09T02:05:00.000Z');
    const releaseHead = c.defer(HEAD), releaseResearch = c.defer(RESEARCH); c.fire(); await flush();
    eq(c.cards(), cards, 'head 等待期间不清空已有榜'); eq(c.insurance.querySelector('.insurance-policy-panel'), panel, 'head 等待期间不清空本人保障展开');
    releaseHead(); await flush(); eq(c.cards(), cards, '研究响应等待期间不清空已有榜'); ok(!c.target.textContent.includes('正在读取'), '有已有榜时不改成加载占位');
    releaseResearch(); await c.idle(); eq(c.cards(), cards, '相同 content_version 保留原卡片 DOM');
    eq(c.insurance.querySelector('.insurance-root'), insuranceRoot, '相同目录版本保留原保险 DOM'); eq(c.insurance.querySelector('.insurance-policy-panel'), panel, '相同目录版本保留本人保障展开');
    eq(c.w.localStorage.getItem(preferenceKey(c.identity())), saved, '同步不改写我的已保存偏好'); eq(c.d.activeElement, focused, '同版本同步不抢焦点');
    eq(c.sourceReads(INSURANCE), 1, '同版本不重读保险分页'); eq(c.sourceReads(OVERVIEW), 1, '同版本不重读个人保单');
    eq(c.headReads(), 2, '五分钟到期读取最新 head'); eq(c.warnings(), [], 'unchanged 视为校对成功');
    ok(c.insurance.querySelector('.insurance-catalog-sync').textContent.includes('10:05:00'), '保险校对时间更新');
    ok(c.target.parentElement.querySelector('.discover-catalog-sync').textContent.includes('10:05:00'), '研究校对时间同步更新');
    eq(c.timers.size, 1, '完成后只排一个后续时钟'); previewChrome(c);
  });
  await scenario('同版本校对保留已展开的本人保单', async c => {
    await c.home(); button(c.insurance, '查看保障 ›').click(); const panel = c.insurance.querySelector('.insurance-policy-panel'); ok(panel, '本人保单面板已展开');
    c.state.head = head(CATALOG_A, 'unchanged'); c.fire(); await c.idle(); eq(c.insurance.querySelector('.insurance-policy-panel'), panel, '同版本更新保持已展开面板');
    ok(panel.textContent.includes('offline-a'), '展开面板仍属于当前账户');
  });
  await scenario('目录版本变化重新读取两来源并更新保险列表', async c => {
    await c.home(); c.state.head = head(CATALOG_B); c.state.research = board(CATALOG_B, CONTENT_B, '乙');
    c.state.insurance = [rawInsurance('health-b', '乙个人医疗保险')]; c.fire(); await c.idle();
    ok(c.cards().every(n => n.textContent.includes('乙目录')), '研究使用新 head 对齐的新榜'); eq(c.sourceReads(INSURANCE), 2, '目录版本变化重新读取保险');
    eq(c.sourceReads(RESEARCH), 2, '目录版本变化重新读取研究'); eq(c.warnings(), [], '两个版本一致时无警告');
    await c.render('insurance'); await stable(c.subview, () => !!c.subview.querySelector('.insurance-list'), '保险目录列表');
    ok(c.subview.textContent.includes('乙个人医疗保险') && !c.subview.textContent.includes('甲个人医疗保险'), '保险列表使用新目录');
    const oldCard = c.subview.querySelector('.insurance-card'); c.state.head = head(CATALOG_A); c.state.insurance = [rawInsurance('pension-c', '丙个人商业养老金')];
    c.fire(); await stable(c.subview, () => c.subview.textContent.includes('丙个人商业养老金') && c.timers.size === 1, '保险列表定时更新');
    ok(!oldCard.isConnected, '目录版本变化重绘保险列表');
    eq(new URL(c.subview.querySelector('.insurance-card').href).searchParams.get('object_version'), CATALOG_A, '新列表详情携带最新目录版本');
  });
  await scenario('研究版本与最新 head 不一致时清理旧榜并提示未取得', async c => {
    await c.home(); c.state.head = head(CATALOG_B); c.state.research = board(CATALOG_A, CONTENT_B, '未对齐');
    c.fire(); await c.idle(); eq(c.cards().length, 0, '目录与收益版本不一致时不保留跨版本旧榜');
    ok(c.warnings().some(text => /暂未取得/.test(text)) && !c.warnings().some(text => /沿用/.test(text)), '跨版本失败明确资料未取得而不声称沿用');
    ok(!c.target.textContent.includes('未对齐'), '失败响应没有被当成完整新榜');
  });
  await scenario('官网校对失败时保留旧 DOM 并禁止接纳新完整榜', async c => {
    await c.home(); const cards = c.cards(); c.state.head = head(CATALOG_A, 'failed', '2026-10-09T02:05:00.000Z'); c.state.research = board(CATALOG_A, CONTENT_B, '失败更新');
    c.fire(); await c.idle(); eq(c.cards(), cards, 'nested failed 不接纳完整新榜');
    ok(!c.target.textContent.includes('失败更新'), '官网校对失败不冒充新榜完成');
    ok(c.warnings().some(text => /沿用上次/.test(text)), '失败明确沿用上次资料');
    ok(c.insurance.querySelector('.insurance-sync-warning')?.textContent.includes('校对失败'), '保险明确显示官网校对失败');
  });
  await scenario('head 网络异常时沿用最后资料并持续提供下一次校对', async c => {
    await c.home(); const cards = c.cards(), insuranceRoot = c.insurance.querySelector('.insurance-root');
    c.queue(HEAD, { error: Error('假响应官网连接失败') }); c.fire(); await c.idle();
    eq(c.cards(), cards, 'head 失败保留原榜'); eq(c.insurance.querySelector('.insurance-root'), insuranceRoot, 'head 失败保留原保险内容');
    ok(c.warnings().some(text => /沿用上次/.test(text)), '异常结果明确沿用最后资料'); eq(c.timers.size, 1, '异常后仍有单个有界后续校对');
  });
  await scenario('隐藏暂停，恢复可见立即校对并重新排程', async c => {
    await c.home(); const reads = c.calls.length; c.setVisibility('hidden'); eq(c.timers.size, 0, '隐藏页面取消校对时钟');
    await flush(); eq(c.calls.length, reads, '隐藏期间没有新读取');
    c.state.head = head(CATALOG_A, 'unchanged', '2026-10-09T02:08:00.000Z'); c.setVisibility('visible'); await c.idle();
    eq(c.headReads(), 2, '恢复可见立即读取 head'); eq(c.timers.size, 1, '恢复后只排一个校对时钟');
    ok(c.insurance.textContent.includes('10:08:00'), '恢复立即更新校对时间');
  });
  await scenario('隐藏期间到达的响应不重绘，恢复后读取新轮次', async c => {
    await c.home(); const cards = c.cards(); c.state.research = board(CATALOG_A, CONTENT_B, '隐藏迟到');
    const release = c.defer(RESEARCH); c.fire(); await flush(); c.setVisibility('hidden'); release(); await flush();
    eq(c.cards(), cards, '隐藏期间不接纳迟到研究 DOM'); eq(c.timers.size, 0, '隐藏迟到响应不重排时钟');
    c.state.research = board(CATALOG_A, CONTENT_B, '恢复最新'); c.setVisibility('visible'); await c.idle();
    ok(c.cards().every(n => n.textContent.includes('恢复最新')), '恢复后立即重新校对最新响应');
  });
  await scenario('强制刷新清理旧读取并丢弃同账户旧 flight', async c => {
    await c.home(); c.state.research = board(CATALOG_A, CONTENT_B, '旧flight'); const release = c.defer(RESEARCH);
    c.fire(); await flush(); c.state.research = board(CATALOG_A, 'e'.repeat(64), '新flight');
    await c.w.DiscoverWorkspace.refresh(true); await c.idle(); ok(c.cards().every(n => n.textContent.includes('新flight')), 'force 创建新研究读取');
    release(); await flush(); ok(c.cards().every(n => n.textContent.includes('新flight')), '同账户旧flight到达不覆盖新榜');
    eq(c.headReads(), 3, 'force 清理旧 head 并读取最新头信息'); eq(c.timers.size, 1, '旧flight完成不创建重复时钟');
  });
  await scenario('隐私变化丢弃迟到响应，旧保障操作与偏好事件不能恢复旧主体', async c => {
    await c.home(); const oldCoverage = button(c.insurance, '查看保障 ›');
    const oldIdentity = c.identity(); setPreferences(c, ['health', 'pension']);
    const saved = c.w.localStorage.getItem(preferenceKey(oldIdentity));
    const releaseHead = c.defer(HEAD); c.fire(); await flush();
    c.setIdentity({ account_id: 'offline-b', workspace_id: 'offline-workspace-b', privacy_revision: 10, as_of: DATE });
    eq(c.timers.size, 0, '隐私失效立即取消时钟'); oldCoverage.click();
    c.w.dispatchEvent(new c.w.CustomEvent('navren:insurance-preferences-updated', { detail: oldIdentity }));
    eq(c.w.localStorage.getItem(preferenceKey(oldIdentity)), saved, '旧保障按钮和偏好事件不改写本人存储');
    eq(c.w.localStorage.getItem(preferenceKey(c.identity())), null, '旧事件不替新主体保存偏好');
    eq(c.d.getElementById('insuranceHelpHost').textContent, '', '隐私失效清空标题中的旧帮助');
    releaseHead(); await flush(); eq(c.cards().length, 0, '旧 head 迟到不恢复研究榜'); ok(!c.insurance.querySelector('.insurance-root'), '旧 head 迟到不恢复个人保障');
    ok(!c.d.body.textContent.includes('offline-a'), '账户切换后隐藏原主体');
    c.state.head = head(CATALOG_B); c.state.research = board(CATALOG_B, CONTENT_B, '新账户'); await c.home();
    ok(c.cards().every(n => n.textContent.includes('新账户')), '新账户重新读取研究榜');
    ok(!c.insurance.textContent.includes('offline-a'), '新账户不复用旧保障'); eq(c.sourceReads(OVERVIEW), 2, '新账户重新读取保单');
  });
  await scenario('pagehide 停止时钟，缓存恢复立即校对并重建保险', async c => {
    await c.home(); const reads = c.headReads(); c.w.dispatchEvent(new c.w.Event('pagehide')); eq(c.timers.size, 0, '离开页面清理校对时钟');
    ok(!c.insurance.querySelector('.insurance-root'), '离开页面清理私有保险挂载');
    c.w.dispatchEvent(new c.w.PageTransitionEvent('pageshow', { persisted: true })); await c.idle();
    eq(c.headReads(), reads + 1, '缓存恢复立即取得最新 head'); ok(c.insurance.querySelector('.insurance-root'), '缓存恢复重新挂载保险');
  });
  await scenario('保险换版更新公开关联，保留本人保单展开与我的已保存偏好', async c => {
    c.state.insurance = [rawInsurance('pension-a', '甲个人商业养老金')];
    setPreferences(c, ['health', 'pension']); const saved = c.w.localStorage.getItem(preferenceKey(c.identity())); await c.home();
    const health = () => c.insurance.querySelector('.insurance-need-card[data-need="health"]');
    const related = () => Array.from(health().querySelectorAll('button')).find(n => n.textContent === '相关保险 ›');
    const count = () => health().querySelector('.insurance-need-metric strong')?.textContent;
    ok(!related(), '旧目录尚无本人健康方向的公开关联保险'); eq(count(), '1', '旧目录仍显示一份本人已接入保单');
    const show = button(health(), '查看保障 ›'); show.click();
    Array.from(health().querySelectorAll('button')).find(n => n.textContent === '查看保障 ›').focus();
    ok(c.insurance.querySelector('.insurance-policy-panel'), '本人保障明细已经展开');
    c.state.head = head(CATALOG_B); c.state.research = board(CATALOG_B, CONTENT_B, '乙');
    c.state.insurance = [rawInsurance('health-b', '乙新增个人医疗保险')]; c.fire(); await c.idle();
    ok(related(), '新目录新增医疗保险后立即出现相关保险入口');
    ok(c.insurance.querySelector('.insurance-policy-panel')?.textContent.includes('offline-a 的医疗保单'), '换版保留原主体展开的本人保单');
    eq(count(), '1', '公开目录新增保险不改变本人保单数量');
    const currentShow = Array.from(health().querySelectorAll('button')).find(n => n.textContent === '查看保障 ›');
    eq(currentShow.getAttribute('aria-expanded'), 'true', '换版后保障展开状态仍然明确'); eq(c.d.activeElement, currentShow, '换版恢复可匹配保障按钮焦点');
    eq(c.sourceReads(OVERVIEW), 1, '公开目录换版不重取或改换个人保单上下文');
    const withdrawn = 'f'.repeat(64); c.state.head = head(withdrawn); c.state.research = board(withdrawn, 'e'.repeat(64), '丙');
    c.state.insurance = [rawInsurance('pension-c', '丙个人商业养老金')]; c.fire(); await c.idle();
    ok(!related(), '新目录撤销医疗保险后相关保险入口消失');
    ok(health().textContent.includes('暂无该方向公开资料'), '撤销后明确该方向已无公开资料');
    eq(count(), '1', '撤销公开保险仍保留本人已接入保单数量');
    eq(Array.from(c.insurance.querySelectorAll('.insurance-need-card'), n => n.dataset.need), ['health', 'pension', 'setup'], '目录换版不改变我的保障方向顺序');
    const afterWithdrawal = button(health(), '查看保障 ›');
    eq(afterWithdrawal.getAttribute('aria-expanded'), 'true', '撤销公开资料仍保留本人保单展开');
    eq(c.d.activeElement, afterWithdrawal, '再次换版恢复保障按钮焦点');
    eq(c.w.localStorage.getItem(preferenceKey(c.identity())), saved, '同步不会改写我的已保存偏好'); previewChrome(c);
    eq(c.sourceReads(OVERVIEW), 1, '新增与撤销公开保险均保留同一份私有保单上下文');
  });
  console.log(JSON.stringify({ passed: failures.length === 0, assertions, scenarios: checks.length, checks, ...totals,
    sha256: { test: sha256(fs.readFileSync(__filename, 'utf8')), ...Object.fromEntries(sourceFiles.map(file => [file, sha256(sources[file])])) },
    failures, evidence: '严格白名单假 GET、真实 DiscoverInsurance、可见稳定结果与手动五分钟时钟；未使用浏览器、服务、数据库或真实网络' }));
  if (failures.length) process.exitCode = 1;
}
run().catch(error => { console.error(error); process.exitCode = 1; });
