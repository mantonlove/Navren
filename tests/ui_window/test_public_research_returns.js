/** 公开产品列表与当前页近1周收益：真实界面源，全部请求为隔离替身。 */
'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const { JSDOM, VirtualConsole } = require('jsdom');
const base = path.resolve(__dirname, '../..'), files = ['js/discover_workspace.js', 'css/discover_workspace.css', 'js/form_controls.js', 'product_research.py'];
const sha = file => crypto.createHash('sha256').update(fs.readFileSync(path.join(base, file))).digest('hex');
const input = Object.fromEntries(files.map(file => [file, sha(file)]));
const sources = Object.fromEntries(files.slice(0, 3).map(file => [file, fs.readFileSync(path.join(base, file), 'utf8')]));
const clone = value => JSON.parse(JSON.stringify(value));
const NOW = '2026-10-10T02:00:00.000Z', DATE = '2026-10-09', VERSION = 'c'.repeat(64), RANK = 'https://fund.eastmoney.com/data/fundranking.html';
let assertions = 0; const results = [];
const eq = (actual, expected, label) => { assertions++; assert.deepEqual(clone(actual), expected, label); };
const ok = (value, label) => { assertions++; assert.ok(value, label); };
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function flush() { for (let i = 0; i < 5; ++i) await tick(); }
async function waitFor(ready, label) { const deadline = Date.now() + 3000; while (Date.now() < deadline) { if (ready()) return; await tick(); } throw Error('结果未就绪：' + label); }
function fixture() {
  const errors = [], vc = new VirtualConsole(); vc.on('jsdomError', e => errors.push(e.message));
  const dom = new JSDOM('<!doctype html><span class="header-location"></span><h1 id="discoverHeading"></h1><p id="discoverIntro"></p><main id="discoverHome" hidden><div id="productPreview"></div><div id="insurancePreview"></div><div id="planPreview"></div></main><section id="discoverSubview"></section>', { url: 'https://fixture.test/recommend.html', runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole: vc });
  const w = dom.window, d = w.document, root = d.getElementById('discoverSubview'), calls = [], writes = [], network = [], updates = [], opened = [], storage = new Map(), waiting = [];
  let closed = false, revision = 0, context = {}, identity = { workspace_id: 'public-list-a', account_id: 'subject-a', privacy_revision: 42, as_of: DATE }, activeReads = 0, peak = 0;
  const microtask = w.queueMicrotask.bind(w); w.queueMicrotask = fn => microtask(() => { if (!closed) fn(); });
  const OriginalDate = w.Date; w.Date = class extends OriginalDate { constructor(...args) { super(...(args.length ? args : [NOW])); } static now() { return OriginalDate.parse(NOW); } };
  const style = d.createElement('style'); style.textContent = ':root{--danger:rgb(181,72,66);--success:rgb(35,120,91)}\n' + sources['css/discover_workspace.css']; d.head.append(style);
  const state = { version: VERSION, hold: false, overrides: new Map(), rows: Array.from({ length: 55 }, (_, i) => ({ code: i === 5 ? 'INS-005' : String(100000 + i), name: '公开产品' + i, type: ['股票基金','债券基金','银行理财','保险'][i % 4], risk: i % 2 ? 'R3' : 'R1（低风险）', publish: '目录机构', source_date: DATE, source_url: 'https://www.webank.com/inquiryCenter/query' })) };
  const typeCounts = () => state.rows.reduce((all, row) => ({ ...all, [row.type]: (all[row.type] || 0) + 1 }), {});
  function envelope(code, version = state.version) {
    const p = state.rows.find(row => row.code === code), index = state.rows.indexOf(p), missing = index === 3, stale = index === 4;
    const on = stale ? '2026-09-01' : DATE;
    const row = p && { code: p.code, name: p.name, type: p.type, risk: p.risk, manager: p.publish, source_scope: 'public_product_research', executable: false, content_version: 'a'.repeat(64), catalog_version: version, source_url: missing ? null : 'https://fund.eastmoney.com/' + code + '.html', rank_source_url: RANK, as_of: missing ? null : on, stale,
      metric: missing ? null : { type: 'one_week_return', label: '近1周涨跌幅', value_pct: index === 0 ? '3.25' : index === 1 ? '-1.20' : index === 2 ? '0' : '0.10', as_of: on }, raw: { source_name: p.name, source_period: '近1周', catalog_source_date: DATE, missing_reason: missing ? '未取得同周期收益资料' : null } };
    return { status: 'ok', metric_type: 'one_week_return', source_scope: 'public_product_research', executable: false, ranking_complete: true, content_version: 'b'.repeat(64), catalog_version: version, catalog_source_date: DATE, fetched_at: NOW, stale_after_days: 7, provider: '天天基金／东方财富Choice公开数据', rank_source_url: RANK, coverage: { candidate_count: 55, type_counts: typeCounts(), source_count: 55, matched_count: 55, eligible_count: 53 }, products: row ? [row] : [] };
  }
  const page = (limit, offset, rows = state.rows) => ({ status: 'ok', fetch_status: 'ok', catalog_version: state.version, source_date: DATE, total: state.rows.length, matched: rows.length, limit, offset, products: clone(rows.slice(offset, offset + limit)), has_more: offset + limit < rows.length, catalog_sync: { fetch_status: 'ok', checked_at: NOW, source_date: DATE, background_refresh: false } });
  w.BankClient = { ensure: async () => clone(identity), identity: () => clone(identity), money: minor => (minor / 100).toFixed(2) + ' 元', guard() { const before = clone(identity), beforeRevision = revision; return { valid: () => before.workspace_id === identity.workspace_id && before.account_id === identity.account_id && before.privacy_revision === identity.privacy_revision && beforeRevision === revision }; },
    async get(request) {
      calls.push(request); const url = new URL(request, 'https://fixture.test');
      if (url.pathname === '/public-products') {
        const limit = Number(url.searchParams.get('limit')), offset = Number(url.searchParams.get('offset'));
        assert.ok([1, 500].includes(limit), '公开目录分页参数遵循现合同');
        return page(limit, offset, url.searchParams.has('q') ? state.rows.filter(p => p.type === '保险') : state.rows);
      }
      if (url.pathname === '/public-research-products') {
        const code = url.searchParams.get('code'); assert.match(code || '', /^\d{6}$/, '只有当前页六位产品编码可查询'); assert.deepEqual([...url.searchParams.keys()], ['code'], '不拼接非法全池或金融参数');
        let response = state.overrides.has(code) ? clone(state.overrides.get(code)) : envelope(code); activeReads++; peak = Math.max(peak, activeReads);
        try { if (state.hold) await new Promise(resolve => waiting.push(resolve)); else await tick(); return clone(response); } finally { activeReads--; }
      }
      if (request === '/public-product-performance?code=100000&catalog_version=' + state.version) return { status: 'unsupported', product_code: '100000', catalog_version: state.version, metrics: [], nav: [] };
      if (request === '/research-plans') return { plans: [] };
      throw Error('未经授权的假读取：' + request);
    } };
  for (const method of ['post','postText','put','patch','delete']) w.BankClient[method] = (...args) => { writes.push({ method, args }); throw Error('只读产品研究不可写银行'); };
  w.fetch = (...args) => { network.push(args); throw Error('禁止真实网络'); };
  w.XMLHttpRequest = class { constructor() { network.push('xhr'); throw Error('禁止真实网络'); } };
  Object.defineProperty(w, 'sessionStorage', { value: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) } });
  w.Storage = { get: () => null, set() { throw Error('不可执行金融或模型动作'); } }; w.BankUI = { amountForm() { throw Error('不可准备金融动作'); } };
  w.showToast = () => {}; w.MarketUI = { recommend: async () => {}, ask() { throw Error('不可调用模型'); } }; w.MarketHistory = { pause() {}, render() { root.textContent = '当前市场资料'; } };
  w.NavrenViews = { register() {}, current: () => clone(context), revision: () => revision, update(meta) { updates.push(clone(meta)); context = { ...context, ...clone(meta) }; revision++; }, open(view, meta) { opened.push({ view, meta: clone(meta) }); }, back() {}, reference() { throw Error('不可调用模型'); } };
  w.eval(sources['js/form_controls.js']); w.eval(sources['js/discover_workspace.js']);
  const release = () => { state.hold = false; waiting.splice(0).forEach(resolve => resolve()); };
  async function render(filters, view = 'research') { context = { view, ...clone(identity), ...(filters ? { filters: clone(filters) } : {}) }; revision++; await w.DiscoverWorkspace.render(context); await flush(); }
  async function settled() { await waitFor(() => !root.textContent.includes('正在读取收益'), '当前页收益'); await flush(); }
  async function close() { w.dispatchEvent(new w.Event('navren:privacy-reset')); release(); await flush(); eq(writes.length, 0, '场景零银行写入'); eq(network.length, 0, '场景零真实网络'); eq(errors, [], '场景无未处理DOM错误'); closed = true; dom.window.close(); }
  return { w, d, root, state, calls, writes, updates, opened, storage, render, settled, release, envelope, close, peak: () => peak, codeCalls: () => calls.filter(call => call.startsWith('/public-research-products?code=')),
    revoke(next = { ...identity, privacy_revision: identity.privacy_revision + 1 }) { identity = clone(next); w.dispatchEvent(new w.Event('navren:privacy-reset')); }, current: () => clone(context) };
}
const rows = e => [...e.root.querySelectorAll('.discover-product-row')];
const button = (e, label) => [...e.root.querySelectorAll('button')].find(b => b.textContent.trim() === label);
const codeOf = row => new URL(row.querySelector('.discover-product-link').href).searchParams.get('object_id');
const byCode = (e, code) => rows(e).find(row => codeOf(row) === code);
function size(e, count) { const select = e.root.querySelector('select[aria-label="每页条数"]'); select.value = String(count); select.dispatchEvent(new e.w.Event('change', { bubbles: true })); }
function color(e, element) { const c = e.w.getComputedStyle(element).color, v = /^var\((--[\w-]+)(?:,([^)]*))?\)$/.exec(c); return (v ? e.w.getComputedStyle(e.d.documentElement).getPropertyValue(v[1]) || v[2] : c).replace(/\s/g, ''); }
async function scenario(name, run) { const e = fixture(); try { await run(e); results.push({ name, status: 'passed' }); } finally { await e.close(); } }
(async () => {
  await scenario('公开目录排除演示来源与旧选择', async e => {
    e.storage.set('navren_research_selection', JSON.stringify({ workspace_id: 'public-list-a', privacy_epoch: 0, privacy_revision: 42, refs: [{ source_scope: 'bank_product', id: 'demo-fund', version: 1 }] }));
    await e.render({ source: 'bank_product' }); await e.settled();
    eq(e.root.querySelector('[aria-label="产品来源"]'), null, '产品来源控件消失'); eq(rows(e).length, 20, '旧source过滤值忽略'); ok(e.root.textContent.includes('共 55 项产品'), '总数只来自公开完整目录');
    ok(!e.calls.includes('/products'), '不读取演示业务目录'); eq(e.w.DiscoverWorkspace.selected(), [], '旧bankrefs不恢复');
    eq(e.w.DiscoverWorkspace.normalize([{ id: 'demo-fund', name: '演示基金' }], { catalog_version: VERSION, products: [e.state.rows[0], { code: '999999', name: '演示基金', source_scope: 'bank_product' }] }).map(p => p.source_scope), ['public_catalog'], 'normalize不会从bank参数或混入公开响应的bank标记回填演示产品');
    eq(e.w.DiscoverWorkspace.select({ source_scope: 'bank_product', id: 'demo-fund', version: 1 }, true), false, '外部旧bankrefs不能重新选中');
    e.root.querySelector('form').dispatchEvent(new e.w.Event('submit', { bubbles: true, cancelable: true })); await e.settled();
    ok(!Object.hasOwn(e.current().filters, 'source'), '应用后filters不携带旧source'); ok(!e.root.textContent.includes('演示产品'), '结果不含演示标签');
  });
  await scenario('当前页直接展示真实正负零与缺失', async e => {
    for (const [code, value] of [['100013','-0.001'],['100014','0.004']]) { const data = e.envelope(code); data.products[0].metric.value_pct = value; e.state.overrides.set(code, data); }
    await e.render(); await e.settled();
    for (const [code, value] of [['100000','+3.25%'],['100001','-1.20%'],['100002','0.00%']]) {
      const row = byCode(e, code), metric = row.querySelector('.discover-research-return-value'); eq(metric.textContent, value, '收益数值保留符号与零：' + code); eq(color(e, metric), 'rgb(181,72,66)', '正负零统一红色：' + code);
      ok(row.textContent.includes('近1周涨跌幅') && row.textContent.includes('截至 ' + DATE), '期间与截止日期直接显示');
    }
    ok(byCode(e, '100003').textContent.includes('未披露近1周收益') && byCode(e, '100003').textContent.includes('截止日期未披露'), '缺失收益明确显示');
    ok(byCode(e, '100004').textContent.includes('收益资料已过期') && byCode(e, '100004').textContent.includes('2026-09-01'), '过期不能冒充近期收益');
    ok(byCode(e, 'INS-005').textContent.includes('未披露近1周收益'), '非六位目录条目显式缺失');
    eq(e.codeCalls().length, 19, '只读取当前页合法code，未逐查55项'); eq(e.peak(), 3, '实际最多3项并发');
    eq(byCode(e, '100013').querySelector('.discover-research-return-value').textContent, '-0.00%', '极小实际负收益保留负号');
    eq(byCode(e, '100014').querySelector('.discover-research-return-value').textContent, '+0.00%', '极小实际正收益保留正号');
    ok(!button(e, '查看收益'), '无需额外点击收益按钮');
  });
  await scenario('损坏口径身份版本与未完成资料拒绝', async e => {
    const changes = [data => data.products[0].metric.type = 'one_year_return', data => data.products[0].raw.source_period = '近1年', data => data.products[0].name = '另一份额名称', data => data.products[0].type = '另一类型', data => { data.catalog_version = 'd'.repeat(64); data.products[0].catalog_version = data.catalog_version; }, data => { data.ranking_complete = false; data.products = []; }];
    changes.forEach((change, i) => { const code = String(100006 + i), data = e.envelope(code); change(data); e.state.overrides.set(code, data); });
    await e.render(); await e.settled();
    for (let i = 0; i < changes.length; ++i) { const row = byCode(e, String(100006 + i)); ok(row.textContent.includes('收益暂未获取'), '损坏研究资料保持明确失败：' + i); eq(row.querySelector('.discover-research-return-value'), null, '失败无伪造收益：' + i); }
  });
  await scenario('分页条数当前页缓存及详情比较身份', async e => {
    await e.render(); await e.settled(); const firstCalls = e.codeCalls().length;
    button(e, '比较产品').click(); await e.settled(); eq(e.codeCalls().length, firstCalls, '只切比较不重复读取同页缓存');
    byCode(e, '100000').querySelector('input[type="checkbox"]').click(); await e.settled();
    button(e, '下一页').click(); await e.settled(); eq(codeOf(rows(e)[0]), '100020', '下一页显示20项之后的产品'); eq(rows(e).length, 20, '保持20条分页'); eq(e.w.DiscoverWorkspace.selected().map(p => p.id), ['100000'], '比较选择跨页保留');
    size(e, 50); await e.settled(); eq(codeOf(rows(e)[0]), '100000', '改每页条数回第一页'); eq(rows(e).length, 50, '每页50'); eq(e.current().filters.page, 1, '页码保存为1'); eq(e.current().filters.page_size, 50, '每页保存50');
    byCode(e, '100001').querySelector('input[type="checkbox"]').click(); await e.settled(); button(e, '开始比较').click();
    const compare = e.opened.at(-1); eq(compare.view, 'compare', '显式导航比较'); ok(compare.meta.selected.every(p => p.source_scope === 'public_catalog' && p.version === VERSION), '比较保持目录身份与版本');
    byCode(e, '100000').querySelector('.discover-product-link').click(); const detail = e.opened.at(-1); eq(detail.meta.source_scope, 'public_catalog', '详情继续使用原公开资料身份'); eq(detail.meta.object_version, VERSION, '详情版本仍为目录版本'); eq(detail.meta.filters.page_size, 50, '详情保留列表上下文');
    size(e, 100); await e.settled(); eq(rows(e).length, 55, '100条页仅取实际目录数'); ok(e.codeCalls().length <= 54, '同目录访问不同页复用身份版本缓存');
    await e.w.DiscoverWorkspace.refresh(); await e.settled(); eq(rows(e).length, 55, '刷新恢复每页条数'); eq(e.codeCalls().length, 54, '同版本刷新不会重复请求整页收益');
  });
  await scenario('页切取消队列并丢弃旧页迟到响应', async e => {
    e.state.hold = true; await e.render(); eq(e.codeCalls().length, 3, '只有3条实际启动'); const old = rows(e)[0].querySelector('.discover-row-return'), oldText = old.textContent;
    button(e, '下一页').click(); await flush(); eq(e.codeCalls().length, 3, '旧3条完成前新页不突破并发');
    e.release(); await e.settled(); eq(old.textContent, oldText, '旧页迟到响应不改脱离DOM节点');
    const codes = e.codeCalls().map(call => new URL(call, 'https://fixture.test').searchParams.get('code')); ok(codes.slice(3).every(code => Number(code) >= 100020 && Number(code) <= 100039), '旧页尚未启动队列全部取消'); eq(e.peak(), 3, '切页全程保持3并发');
    e.state.hold = true; button(e, '下一页').click(); await flush(); const before = e.codeCalls().length; await e.render(null, 'market_history'); e.release(); await flush();
    eq(e.root.textContent, '当前市场资料', '离开研究后迟到收益不覆盖新视图'); eq(e.codeCalls().length, before, '离页不启动剩余queued读取');
  });
  await scenario('目录换版旧收益不能复用或覆盖', async e => {
    e.state.hold = true; await e.render(); const old = rows(e)[0].querySelector('.discover-row-return'), oldText = old.textContent;
    e.state.version = 'd'.repeat(64); const pending = e.w.DiscoverWorkspace.refresh(true); await flush(); e.release(); await pending; await e.settled();
    eq(old.textContent, oldText, '旧版本响应不写旧节点'); eq(byCode(e, '100000').querySelector('.discover-research-return-value').textContent, '+3.25%', '新版本读取独立成功');
    eq(new URL(rows(e)[0].querySelector('.discover-product-link').href).searchParams.get('object_version'), e.state.version, '当前行身份为新目录版本'); eq(e.codeCalls().filter(call => call.endsWith('100000')).length, 2, '换版重新读取同code不沿用旧缓存');
  });
  await scenario('同workspace隐私与主体切换阻断迟到', async e => {
    e.state.hold = true; await e.render(); const old = rows(e)[0].querySelector('.discover-row-return'), text = old.textContent, before = e.codeCalls().length;
    e.revoke(); e.release(); await flush(); eq(rows(e).length, 0, '隐私失效清旧列表'); ok(e.root.textContent.includes('旧账户资料已失效'), '保留身份失效提示'); eq(old.textContent, text, '旧收益不写脱离节点'); eq(e.codeCalls().length, before, '隐私清queued');
    await e.render(); await e.settled(); eq(e.codeCalls().filter(call => call.endsWith('100000')).length, 2, '同workspace不同授权不能复用缓存');
    e.revoke({ workspace_id: 'public-list-a', account_id: 'subject-b', privacy_revision: 43, as_of: DATE }); await e.render(); await e.settled(); eq(e.codeCalls().filter(call => call.endsWith('100000')).length, 3, '不同主体再次读取，不串旧主体缓存');
  });
  eq(Object.fromEntries(files.map(file => [file, sha(file)])), input, '测试期间真实源文件指纹不变');
  process.stdout.write(JSON.stringify({ status: 'passed', scenarios: results.length, assertions, checks: results, input_sha256: input, test_sha256: sha(path.relative(base, __filename)), real_network_requests: 0, bank_writes: 0 }, null, 2) + '\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
