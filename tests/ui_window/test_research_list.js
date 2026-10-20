/** 产品研究列表的隔离DOM合同；真实工作区、完整公开分片、纯内存存储。 */
'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const { JSDOM, VirtualConsole } = require('jsdom');
const sourcePath = path.resolve(__dirname, '../../js/discover_workspace.js'), source = fs.readFileSync(sourcePath, 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));
const VERSION = 'c'.repeat(64), DATE = '2026-10-08', NOW = '2026-10-09T02:00:00.000Z';
const HEAD = '/public-products?limit=1&offset=0';
let assertions = 0;
const totals = { fake_reads: 0, fixture_selection_writes: 0, fixture_reading_history_writes: 0, bank_writes: 0, financial_or_model_actions: 0, real_network_requests: 0 };
function check(value, label) { assert.ok(value, label); assertions++; }
function equal(actual, expected, label) { assert.deepEqual(clone(actual), expected, label); assertions++; }
const flush = async () => { for (let i = 0; i < 4; i++) await new Promise(resolve => setTimeout(resolve, 0)); };

function publicProducts() {
  const types = ['股票基金', '债券基金', '货币基金', '保险', '银行理财'];
  const risks = ['R1（低风险）', 'R2', 'R3', 'R4', 'R5', null, '风险原文但未披露R等级'];
  const rows = Array.from({ length: 503 }, (_, i) => ({ code: String(100000 + i), name: '公开产品' + String(i).padStart(3, '0'), type: types[i % 5], risk: risks[i % 7], publish: '夹具公开机构',
    charge: '官方费用原文', sale_status: '快照销售状态', source_date: DATE, source_url: 'https://www.webank.com/inquiryCenter/query' }));
  Object.assign(rows[498], { name: '边界专项高风险保险', type: '保险', risk: 'R4' });
  Object.assign(rows[499], { name: '边界专项未知风险保险', type: '保险', risk: null });
  Object.assign(rows[500], { name: '边界专项低风险保险', type: '保险', risk: 'R1（低风险）' });
  Object.assign(rows[501], { name: '完整后片目标债券基金', type: '债券基金', risk: 'R2', publish: '第二片机构' });
  Object.assign(rows[502], { name: '边界专项风险原文保险', type: '保险', risk: '低风险文字，未披露R等级' });
  return rows;
}
function bankProducts() {
  const make = (id, name, risk, term, minimum, extra = {}) => ({ id, name, source: 'simulation', version: 1, risk_level: risk, term_days: term, min_purchase_minor: minimum, redemption_delay_days: 0, profile_eligible: true, risk_notice: '夹具模拟业务', ...extra });
  return [make('SIM-FLEX', '模拟无锁定产品', 1, 0, 10000), make('SIM-SHORT', '模拟30天产品', 2, 30, 100000), make('SIM-90', '模拟90天产品', 2, 90, 1000000),
    make('SIM-LONG', '模拟180天产品', 4, 180, 100000), make('SIM-UNKNOWN', '模拟缺项产品', null, null, null),
    make('SIM-NO-MINIMUM', '模拟无起购披露', 1, 0, undefined), make('SIM-365', '模拟365天产品', 5, 365, 500000), make('SIM-15', '模拟15天产品', 3, 15, 200000)];
}
function fixture() {
  const errors = [], virtualConsole = new VirtualConsole(); virtualConsole.on('jsdomError', error => errors.push(error.message));
  const dom = new JSDOM('<!doctype html><body data-page="recommend"><span class="header-location"></span><h1 id="discoverHeading"></h1><p id="discoverIntro"></p><main id="discoverHome" hidden><div id="productPreview"></div><div id="insurancePreview"></div><div id="planPreview"></div></main><section id="discoverSubview"></section>', { url: 'https://fixture.test/recommend.html?view=research', runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole });
  const w = dom.window, d = w.document, root = d.getElementById('discoverSubview'), calls = [], writes = [], actions = [], network = [], updates = [], opened = [], storage = new Map(), storageWrites = [], readingHistory = new Map(), historyWrites = [], queue = new Map();
  let identity = { workspace_id: 'list-workspace-a', account_id: 'list-subject-a', privacy_revision: 42, as_of: DATE }, identityEpoch = 0, revision = 0, context = { page: 'recommend', view: 'research' };
  const state = { public: publicProducts(), bank: bankProducts(), version: VERSION };
  Object.defineProperty(w, 'sessionStorage', { value: { getItem: name => storage.get(name) ?? null, setItem(name, value) { storageWrites.push({ name, value }); storage.set(name, String(value)); }, removeItem(name) { storageWrites.push({ name, remove: true }); storage.delete(name); } } });
  const deferred = (request, response) => { let release; const pause = new Promise(resolve => { release = resolve; }); if (!queue.has(request)) queue.set(request, []); queue.get(request).push({ pause, response }); return release; };
  w.BankClient = {
    ensure: async () => clone(identity), identity: () => clone(identity), money: minor => (minor / 100).toFixed(2) + ' 元',
    guard() { const before = clone(identity), epoch = identityEpoch; return { valid(data) { return epoch === identityEpoch && before.workspace_id === identity.workspace_id && before.account_id === identity.account_id && before.privacy_revision === identity.privacy_revision && (!data || !data.workspace_id || data.workspace_id === before.workspace_id); } }; },
    async get(request) {
      calls.push(request); let response;
      if (request === '/products') response = clone(state.bank);
      else if (request === HEAD) response = { status: 'ok', fetch_status: 'ok', catalog_version: state.version, source_date: DATE, total: state.public.length, matched: state.public.length, limit: 1, offset: 0, products: clone(state.public.slice(0, 1)), has_more: state.public.length > 1, catalog_sync: { fetch_status: 'ok', checked_at: NOW, source_date: DATE, background_refresh: false } };
      else {
        const url = new URL(request, 'https://fixture.test');
        if (url.pathname === '/public-products') {
          assert.equal(url.searchParams.get('limit'), '500', '完整公开目录只用500项分片'); assert.ok([...url.searchParams.keys()].every(key => ['limit', 'offset'].includes(key)), '研究目录不从筛选后的公开子集加载');
          const offset = Number(url.searchParams.get('offset')); assert.ok([0, 500].includes(offset), '公开分片偏移必须准确');
          response = { status: 'ok', fetch_status: 'ok', catalog_version: state.version, source_date: DATE, total: state.public.length, matched: state.public.length, limit: 500, offset, products: clone(state.public.slice(offset, offset + 500)), has_more: offset + 500 < state.public.length, stale: false };
        } else if (url.pathname === '/public-product-performance') {
          response = { product_code: url.searchParams.get('code'), catalog_version: state.version, status: 'unsupported', metrics: [], nav: [] };
        } else throw Error('未授权的假读取：' + request);
      }
      const hold = queue.get(request)?.shift(); if (hold) { await hold.pause; if (hold.response !== undefined) response = clone(hold.response); }
      return response;
    }
  };
  for (const method of ['post', 'postText', 'put', 'patch', 'delete']) w.BankClient[method] = (...args) => { writes.push({ method, args }); throw Error('产品研究回归不提交业务操作'); };
  w.fetch = (...args) => { network.push(args); throw Error('隔离DOM禁止真实网络'); };
  w.XMLHttpRequest = class { constructor() { network.push('xhr'); throw Error('隔离DOM禁止真实网络'); } };
  w.BankUI = { amountForm: (...args) => { actions.push(args); throw Error('研究列表不准备金融操作'); } };
  w.Storage = { get: name => readingHistory.has(name) ? clone(readingHistory.get(name)) : null, set(name, value) {
    if (name === 'navren_reading_history') { historyWrites.push({ name, value: clone(value) }); readingHistory.set(name, clone(value)); return; }
    actions.push({ name, value }); throw Error('研究列表不保存金融操作');
  } };
  w.MarketUI = { recommend: async () => {}, ask: (...args) => actions.push(args) }; w.MarketHistory = { pause() {} }; w.showToast = () => {};
  w.NavrenViews = { register() {}, current: () => clone(context), revision: () => revision, update(meta) { updates.push(clone(meta)); context = { ...context, ...clone(meta) }; },
    async open(view, meta = {}) { opened.push({ view, meta: clone(meta) }); return render(view, meta); }, back() {}, reference: (...args) => actions.push(args), ask: (...args) => actions.push(args) };
  w.eval(source);
  async function render(view = 'research', meta = {}) { context = { page: 'recommend', view, ...clone(identity), ...clone(meta) }; revision++; await w.DiscoverWorkspace.render(context); await flush(); }
  async function research(filters) { await render('research', filters ? { filters } : {}); }
  const close = () => {
    totals.fake_reads += calls.length; totals.fixture_selection_writes += storageWrites.length; totals.fixture_reading_history_writes += historyWrites.length; totals.bank_writes += writes.length; totals.financial_or_model_actions += actions.length; totals.real_network_requests += network.length;
    equal(writes.length, 0, '场景零银行业务写入'); equal(actions.length, 0, '场景零金融或模型动作'); equal(network.length, 0, '场景零真实网络'); equal(errors, [], '场景没有未处理DOM错误'); dom.window.close();
  };
  return { w, d, root, calls, writes, actions, network, updates, opened, state, storage, storageWrites, render, research, deferred, close, current: () => clone(context),
    revoke(next = { workspace_id: 'list-workspace-b', account_id: 'list-subject-b', privacy_revision: 43, as_of: DATE }) { identity = clone(next); identityEpoch++; w.dispatchEvent(new w.Event('navren:privacy-reset')); } };
}

const controls = e => Object.fromEntries(Object.entries({ q: '搜索产品、机构或编码', source: '产品来源', type: '产品类型', risk: '风险等级', term: '投资期限', maximum: '起购上限（元）', page_size: '每页条数' }).map(([name, label]) => [name, e.root.querySelector('[aria-label="' + label + '"]')]));
const rows = e => [...e.root.querySelectorAll('.discover-product-row')];
const route = row => new URL(row.querySelector('.discover-product-link').href);
const ids = e => rows(e).map(row => route(row).searchParams.get('object_id'));
const rowById = (e, id) => rows(e).find(row => route(row).searchParams.get('object_id') === id);
const button = (e, label) => [...e.root.querySelectorAll('button')].find(item => item.textContent.trim() === label);
function submit(e, values = {}) {
  const fields = controls(e); for (const [name, value] of Object.entries(values)) { assert.ok(fields[name], '筛选控件存在：' + name); fields[name].value = String(value); }
  e.root.querySelector('.discover-toolbar').dispatchEvent(new e.w.Event('submit', { bubbles: true, cancelable: true }));
}
function pageSize(e, value) { const field = controls(e).page_size; field.value = String(value); field.dispatchEvent(new e.w.Event('change', { bubbles: true })); }
const pageNumbers = e => (e.root.querySelector('.discover-research-page-number')?.textContent.match(/\d+/g) || []).map(Number);
const rangeNumbers = e => (e.root.querySelector('.discover-research-page-range')?.textContent.match(/\d+/g) || []).map(Number);
function selectRow(e, id) { const row = rowById(e, id); assert.ok(row, '当前页产品存在：' + id); row.querySelector('input[type="checkbox"]').click(); }
const checks = [];
async function scenario(name, run) {
  const e = fixture(); try { await run(e); checks.push({ name, status: 'passed' }); } finally { e.close(); }
}

(async () => {
  await scenario('完整公开分片与规范列表入口', async e => {
    await e.research();
    equal(rows(e).length, 20, '默认每页20项');
    equal(controls(e).page_size.value, '20', '默认每页控件为20');
    equal([...controls(e).page_size.options].map(option => option.value), ['20', '50', '100'], '只提供20/50/100三个每页条数');
    check(e.calls.includes(HEAD) && e.calls.includes('/public-products?limit=500&offset=0') && e.calls.includes('/public-products?limit=500&offset=500'), '先核验head再取得完整的两片公开目录');
    check(/共\s*511\s*项产品/.test(e.root.textContent), '结果总量包含503公开与8业务资料');
    equal(pageNumbers(e), [1, 26], '默认显示第1页共26页');
    equal(rangeNumbers(e).slice(0, 2), [1, 20], '默认显示1至20项范围');
    check(button(e, '上一页').disabled && !button(e, '下一页').disabled, '第一页只允许向后翻页');
    check(rows(e).every(row => row.dataset.comparison === 'false'), '默认横向行不进入比较布局');
    check(rows(e).every(row => [...row.querySelectorAll('a')].some(link => link.textContent.trim() === '查看资料 ›')), '每一产品行提供显式查看资料入口');
    check([...controls(e).type.options].some(option => option.value === 'unknown'), '未知类型保留明确筛选入口');
    submit(e, { q: '完整后片目标' });
    equal(ids(e), ['100501'], '搜索可命中第500项以后的产品');
    equal(e.current().filters.q, '完整后片目标', '搜索写入共享视图filters');
    submit(e, { q: '第二片机构' }); equal(ids(e), ['100501'], '搜索匹配产品机构');
    submit(e, { q: '100502' }); equal(ids(e), ['100502'], '搜索匹配精确产品编码');
    equal(e.calls.filter(call => call.startsWith('/public-product-performance')).length, 0, '列表筛选不会隐式读取收益');
  });

  await scenario('草稿筛选、类型、风险与重置空态', async e => {
    await e.research(); const initial = ids(e), field = controls(e).q;
    field.value = '边界专项'; field.dispatchEvent(new e.w.Event('input', { bubbles: true }));
    equal(ids(e), initial, '搜索草稿输入不立即改变已应用列表');
    button(e, '下一页').click();
    equal(rows(e).length, 20, '带未应用草稿翻页仍显示完整目录页');
    check(!e.current().filters.q, '翻页不悄悄应用草稿查询');
    submit(e, { q: '  边界专项  ', type: '', risk: '' });
    equal(ids(e), ['100498', '100499', '100500', '100502'], '提交搜索去空格并筛选完整目录');
    equal(e.current().filters.page, 1, '提交从第二页重置到第一页');
    controls(e).type.value = '债券基金'; controls(e).type.dispatchEvent(new e.w.Event('change', { bubbles: true }));
    equal(ids(e), ['100498', '100499', '100500', '100502'], '类型草稿变更也只在提交后应用');
    submit(e); equal(rows(e).length, 0, '不相交的类型与查询给出空结果');
    check(e.root.querySelector('.discover-empty') && button(e, '上一页').disabled && button(e, '下一页').disabled, '空态保留重置入口并停用翻页');
    submit(e, { type: '保险', risk: 'R1' }); equal(ids(e), ['100500'], '明确R等级可以从完整官方风险文字筛选');
    submit(e, { risk: 'unknown' }); equal(ids(e), ['100499', '100502'], '缺失与仅有风险文字不被猜成R等级');
    submit(e, { risk: 'R4' }); equal(ids(e), ['100498'], '类型与风险组合只保留匹配产品');
    button(e, '重置').click();
    equal(Object.fromEntries(['q', 'source', 'type', 'risk', 'term', 'maximum'].map(name => [name, controls(e)[name].value])), { q: '', source: '', type: '', risk: '', term: '', maximum: '' }, '重置清空全部筛选草稿');
    equal(e.current().filters.page, 1, '重置回到第一页');
    check(/共\s*511\s*项产品/.test(e.root.textContent), '重置恢复全部资料总量');
    submit(e, { source: 'bank_product', type: 'unknown' });
    equal(ids(e), ['SIM-FLEX', 'SIM-SHORT', 'SIM-90', 'SIM-LONG', 'SIM-UNKNOWN', 'SIM-NO-MINIMUM', 'SIM-365', 'SIM-15'], '银行缺失raw.type只归未知，不从名称猜类型');
    submit(e, { source: 'public_catalog', type: '保险', q: '边界专项', risk: '' });
    equal(ids(e), ['100498', '100499', '100500', '100502'], '公开来源和明确类型组合筛选');
    check(rows(e).every(row => route(row).searchParams.get('source_scope') === 'public_catalog'), '查看资料链接保持对应公开身份');
  });

  await scenario('期限与已知起购金额边界', async e => {
    await e.research();
    submit(e, { source: 'bank_product', term: 'flex' }); equal(ids(e), ['SIM-FLEX', 'SIM-NO-MINIMUM'], '无锁定筛选只接受明确0天');
    submit(e, { term: 'short' }); equal(ids(e), ['SIM-FLEX', 'SIM-SHORT', 'SIM-90', 'SIM-NO-MINIMUM', 'SIM-15'], '短期筛选包括0、30、90天且排除未知');
    submit(e, { term: 'long' }); equal(ids(e), ['SIM-LONG', 'SIM-365'], '长期筛选只接受明确超过90天');
    submit(e, { term: 'unknown' }); equal(ids(e), ['SIM-UNKNOWN'], '未披露期限不按0天处理');
    submit(e, { term: '', maximum: '1000' }); equal(ids(e), ['SIM-FLEX', 'SIM-SHORT', 'SIM-LONG'], '1000元起购上限排除null/缺失起购及超过上限产品');
    check(!ids(e).includes('SIM-UNKNOWN') && !ids(e).includes('SIM-NO-MINIMUM'), '未知起购条件不会通过金额筛选');
    submit(e, { source: 'public_catalog' }); equal(rows(e).length, 0, '公开目录未披露起购不冒充金额可匹配');
    submit(e, { source: '', maximum: '', q: '边界专项', term: 'unknown' }); equal(rows(e).length, 4, '公开目录期限未披露在未知期限中仍可研究');
  });

  await scenario('页码、每页条数与共享filters恢复', async e => {
    await e.research(); button(e, '下一页').click();
    equal(pageNumbers(e), [2, 26], '下一页推进实际页码'); equal(rangeNumbers(e).slice(0, 2), [21, 40], '下一页显示21至40项'); equal(e.current().filters.page, 2, '翻页持久化page');
    button(e, '上一页').click(); equal(pageNumbers(e), [1, 26], '上一页回到第一页');
    button(e, '下一页').click(); pageSize(e, 50);
    equal(rows(e).length, 50, '每页50真实改变显示范围'); equal(pageNumbers(e), [1, 11], '改变每页条数从第一页重新计算总页数');
    equal(e.current().filters.page_size, 50, '每页条数写入filters'); equal(e.current().filters.page, 1, '每页条数改变重置page');
    pageSize(e, 100); equal(rows(e).length, 100, '每页100真实显示100项'); equal(pageNumbers(e), [1, 6], '每页100共6页');
    for (let i = 0; i < 5; i++) button(e, '下一页').click();
    equal(rows(e).length, 11, '最后一页只显示实际剩余11项'); equal(pageNumbers(e), [6, 6], '最后一页页码准确'); check(button(e, '下一页').disabled, '最后一页不能继续推进');
    await e.research({ q: '公开', source: 'public_catalog', type: '', risk: '', term: '', maximum: '', page_size: 50, page: 3 });
    equal(controls(e).q.value, '公开', '返回context恢复查询'); equal(controls(e).source.value, 'public_catalog', '返回context恢复来源'); equal(rows(e).length, 50, '返回context恢复每页条数'); equal(pageNumbers(e), [3, 11], '返回context恢复第三页及筛选后总页数');
    const beforeRefresh = ids(e); await e.w.DiscoverWorkspace.refresh(true); await flush();
    equal(ids(e), beforeRefresh, '强制刷新后恢复同一筛选和页内容'); equal(controls(e).page_size.value, '50', '刷新保留每页条数');
    equal(pageNumbers(e), [3, 11], '刷新保留页码');
    const returnFilters = e.current().filters;
    const detailRow = rows(e).find(row => e.state.public.find(item => item.code === route(row).searchParams.get('object_id'))?.type === '银行理财');
    assert.ok(detailRow, '本页存在只读银行理财详情');
    [...detailRow.querySelectorAll('a')].find(link => link.textContent.trim() === '查看资料 ›').click(); await flush();
    equal(e.opened.at(-1).view, 'product', '从当前页进入对应产品详情');
    equal(e.opened.at(-1).meta.filters, returnFilters, '产品详情路由携带完整筛选及页上下文');
    await e.research(e.opened.at(-1).meta.filters);
    equal(ids(e), beforeRefresh, '由详情返回的context恢复原页资料');
    equal(pageNumbers(e), [3, 11], '由详情返回context恢复原页码');
    submit(e, { q: '完整后片目标' }); equal(pageNumbers(e), [1, 1], '从第三页提交新查询重置到唯一有效页');
  });

  await scenario('比较上限与跨页选择及比较返回', async e => {
    await e.research(); submit(e, { source: 'bank_product' }); button(e, '比较产品').click();
    check(rows(e).every(row => row.dataset.comparison === 'true'), '比较模式显式启用横向行选择列');
    selectRow(e, 'SIM-FLEX'); selectRow(e, 'SIM-SHORT');
    equal(e.w.DiscoverWorkspace.selected().map(item => item.id), ['SIM-FLEX', 'SIM-SHORT'], '选择保存精确产品身份');
    button(e, '重置').click(); button(e, '下一页').click(); selectRow(e, '100013');
    equal(e.w.DiscoverWorkspace.selected().map(item => item.id), ['SIM-FLEX', 'SIM-SHORT', '100013'], '跨页保留前两款并加入第三款');
    const fourth = rows(e).find(row => !row.querySelector('input[type="checkbox"]').checked).querySelector('input[type="checkbox"]');
    check(fourth.disabled, '选满三款时其余比较复选框禁用');
    fourth.checked = true; fourth.dispatchEvent(new e.w.Event('change', { bubbles: true }));
    equal(e.w.DiscoverWorkspace.selected().length, 3, '强制触发第四项仍不能超过三款');
    button(e, '上一页').click();
    check(rowById(e, 'SIM-FLEX').querySelector('input[type="checkbox"]').checked && rowById(e, 'SIM-SHORT').querySelector('input[type="checkbox"]').checked, '返回原页恢复两款已选状态');
    equal(e.root.querySelectorAll('.discover-chip').length, 3, '比较栏展示跨页三款选择');
    submit(e, { q: '模拟', source: 'bank_product', type: 'unknown' }); pageSize(e, 50); const compareFilters = e.current().filters;
    button(e, '开始比较').click(); await flush();
    equal(e.opened.at(-1).view, 'compare', '开始比较进入实际比较视图');
    equal(e.opened.at(-1).meta.filters, compareFilters, '开始比较携带当前查询/类型/每页上下文');
    equal(e.root.querySelectorAll('.discover-compare thead th').length, 4, '三款比较各自对应列');
    button(e, '调整选择').click(); await flush();
    equal(e.opened.at(-1).view, 'research', '比较调整返回研究列表');
    equal(e.w.DiscoverWorkspace.selected().length, 3, '比较返回保留三款选中');
    check(controls(e).q.value === '模拟' && controls(e).source.value === 'bank_product' && controls(e).type.value === 'unknown' && controls(e).page_size.value === '50', '比较返回实际恢复已应用的非空筛选');
    const saved = JSON.parse(e.storage.get('navren_research_selection'));
    check(saved.refs.every(item => item.source_scope && item.id && item.version != null), '临时选择存储只保留来源、ID和版本身份');
    check(saved.workspace_id === 'list-workspace-a' && saved.privacy_revision === 42, '临时比较选择受账户授权版本绑定');
  });

  await scenario('隐私迟到与旧控制回调失效', async e => {
    await e.research(); button(e, '比较产品').click(); selectRow(e, 'SIM-FLEX');
    const oldNext = button(e, '下一页'), oldSubmit = e.root.querySelector('.discover-toolbar'), oldChoice = rowById(e, 'SIM-SHORT').querySelector('input[type="checkbox"]'), beforeUpdates = e.updates.length;
    const oldSelectionButtons = ['清空', '退出比较', '开始比较'].map(label => button(e, label)).concat(e.root.querySelector('.discover-chip'));
    const oldSize = controls(e).page_size, beforeOpen = e.opened.length;
    const release = e.deferred('/public-products?limit=500&offset=500');
    const pending = e.w.DiscoverWorkspace.refresh(true); await flush();
    check(e.calls.filter(call => call === '/public-products?limit=500&offset=500').length >= 2, '新读取已进入迟到的公开第二分片');
    e.revoke();
    equal(e.w.DiscoverWorkspace.selected().length, 0, '隐私失效立即清空比较选择');
    check(!e.storage.has('navren_research_selection'), '隐私失效删除本机临时选择');
    const afterRevokeWrites = e.storageWrites.length;
    oldNext.click(); oldSubmit.dispatchEvent(new e.w.Event('submit', { bubbles: true, cancelable: true })); oldChoice.checked = true; oldChoice.dispatchEvent(new e.w.Event('change', { bubbles: true }));
    oldSize.value = '100'; oldSize.dispatchEvent(new e.w.Event('change', { bubbles: true })); oldSelectionButtons.forEach(control => { if (control.disabled) control.onclick(); else control.click(); });
    equal(e.updates.length, beforeUpdates, '旧翻页和筛选控件不能写新视图filters');
    equal(e.w.DiscoverWorkspace.selected().length, 0, '旧比较控件不能重新写入失效选择');
    equal(e.opened.length, beforeOpen, '旧比较栏按钮不能导航或提交选择');
    equal(e.storageWrites.length, afterRevokeWrites, '旧比较栏不会再次写入失效临时选择');
    release(); await pending; await flush();
    equal(rows(e).length, 0, '迟到完整目录响应不重绘旧账户产品');
    check(e.root.textContent.includes('旧账户资料已失效'), '失效后仍保留明确身份提示');
  });

  process.stdout.write(JSON.stringify({ status: 'passed', assertions, scenarios: checks.length, checks, totals,
    source_sha256: crypto.createHash('sha256').update(source).digest('hex'), test_sha256: crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex') }, null, 2) + '\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
