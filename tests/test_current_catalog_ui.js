/* 官网新目录规模与分页失败回归；只在内存 DOM 使用合成夹具。 */
const assert = require('node:assert/strict'), fs = require('node:fs');
const { JSDOM } = require('jsdom');
const script = fs.readFileSync(process.env.NAVREN_CATALOG_UI_PATH || 'outputs/current-catalog-integration/staged/js/discover_workspace.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const fixtures = count => Array.from({ length: count }, (_, i) => ({
  code: String(i).padStart(6, '0'), name: i === count - 1 ? '末页产品' : '目录样本 ' + i,
  risk: 'R' + (i % 5 + 1) + '官方风险文字', publish: '夹具管理人', sale_status: '在售',
  charge: '详见产品页面', source_date: '2026-09-24', source_url: 'https://www.webank.com/inquiryCenter/query',
  annual_return: null, current_sale_status: null, executable: false
}));
async function harness(rows, alter = page => page, wait = null) {
  const dom = new JSDOM('<main><h1 id="discoverHeading"></h1><p id="discoverIntro"></p><span class="header-location"></span><section id="discoverHome"></section><section id="discoverSubview"></section><section id="productPreview"></section><section id="planPreview"></section></main>', { url: 'https://navren.test/recommend.html', runScripts: 'outside-only' });
  const w = dom.window, d = w.document, reads = [], store = new Map();
  let renderer, identity = { workspace_id: 'workspace-a', privacy_revision: 1 };
  w.showToast = () => {};
  w.Storage = { get: k => store.get(k) || null, set: (k, v) => store.set(k, v) };
  w.MarketUI = { ask: () => {}, recommend: async () => {} };
  w.NavrenViews = { register: (_, r) => { renderer = r; }, update: () => {}, back: () => {}, reference: () => {}, open: (view, metadata = {}) => renderer({ view, ...metadata, ...identity }) };
  w.BankClient = {
    ensure: async () => ({ ...identity }), identity: () => ({ ...identity }), money: n => String(n / 100),
    get: async path => {
      if (path === '/products') return [];
      if (path === '/research-plans') return { items: [], ...identity };
      if (!path.startsWith('/public-products?')) throw Error(path);
      const q = new URLSearchParams(path.split('?')[1]), offset = Number(q.get('offset')), limit = Number(q.get('limit'));
      reads.push({ offset, limit });
      if (wait) await wait(offset);
      return alter({ catalog_version: 'v1', sha256: 'v1', matched: rows.length, total: rows.length, offset, limit, has_more: offset + limit < rows.length, products: structuredClone(rows.slice(offset, offset + limit)), verified_nav_products: [], source_date: '2026-09-24', stale: true }, offset);
    }
  };
  w.eval(script); await tick(); assert.ok(renderer, '发现视图已注册');
  return { dom, w, d, reads, open: (view, metadata = {}) => renderer({ view, ...metadata, ...identity }), reset: () => { w.dispatchEvent(new w.Event('navren:privacy-reset')); identity = { workspace_id: 'workspace-b', privacy_revision: 2 }; } };
}
const rowsIn = h => h.d.querySelectorAll('.discover-product-row').length;
const buttons = h => [...h.d.querySelectorAll('button')];
const submit = h => h.d.querySelector('.discover-toolbar').dispatchEvent(new h.w.Event('submit', { cancelable: true }));
let scenarios = 0;
(async () => {
  const h = await harness(fixtures(13658));
  try {
    await h.open('research');
    assert.equal(h.reads.length, 28, '13658 条完整目录按每页 500 只读 28 页');
    assert.ok(h.reads.every(p => p.limit === 500));
    assert.equal(h.reads.at(-1).offset, 13500);
    assert.equal(rowsIn(h), 100, '首屏 DOM 只显示 100 条');
    assert.ok(h.d.querySelector('[role="status"]').textContent.includes('13658 项资料，已显示 100 项'));
    h.d.querySelector('.discover-load-more').click(); assert.equal(rowsIn(h), 200);
    buttons(h).find(b => b.textContent === '比较产品').click(); assert.equal(rowsIn(h), 200, '切换比较保留已加载行');
    h.d.querySelectorAll('.discover-select-cell input')[150].click(); assert.equal(rowsIn(h), 200, '选择追加页产品不折回首屏');
    assert.equal(h.w.DiscoverWorkspace.selected()[0].id, '000150');
    const search = h.d.querySelector('[aria-label="搜索产品或机构"]'); search.value = '末页产品'; submit(h);
    assert.equal(rowsIn(h), 1, '筛选覆盖完整内存目录而非已渲染行');
    assert.ok(h.d.querySelector('.discover-product-row').textContent.includes('末页产品'));
    search.value = ''; h.d.querySelector('[aria-label="风险"]').value = 'R2'; submit(h);
    assert.equal(rowsIn(h), 100, '新筛选重新显示前 100 条');
    assert.ok(h.d.querySelector('[role="status"]').textContent.includes('2732 项资料'));
    assert.ok([...h.d.querySelectorAll('.discover-product-row')].every(row => row.textContent.includes('R2官方风险文字')), '筛选解析 R2，显示保留原文');
    await h.open('product', { source_scope: 'public_catalog', object_id: '013657', object_version: 'v1' });
    assert.ok(h.d.getElementById('discoverSubview').textContent.includes('2026-09-24 快照销售状态：在售'));
    assert.ok(h.d.getElementById('discoverSubview').textContent.includes('当前可申购条件未知'));
    assert.equal(buttons(h).find(b => b.textContent === '准备申购'), undefined, '快照在售不会生成申购操作');
    scenarios++;
  } finally { h.dom.window.close(); }

  const errors = [
    ['跨页版本变化', (p, offset) => offset ? { ...p, catalog_version: 'v2', sha256: 'v2' } : p, '刚刚修订'],
    ['分页不推进', (p, offset) => offset ? { ...p, offset: 0 } : p, '分页未推进'],
    ['返回条数不足', p => ({ ...p, products: p.products.slice(1) }), '数量未齐'],
    ['提前结束', (p, offset) => offset ? p : { ...p, has_more: false }, '数量未齐'],
    ['匹配数量变化', (p, offset) => offset ? { ...p, matched: 700 } : p, '数量发生变化'],
    ['缺少目录版本', p => { const result = { ...p }; delete result.catalog_version; delete result.sha256; return result; }, '版本无法核对']
  ];
  for (const [label, alter, text] of errors) {
    const h = await harness(fixtures(600), alter);
    try { await h.open('research'); assert.equal(rowsIn(h), 0, label + '不得发布部分目录'); assert.ok(h.d.querySelector('.discover-error').textContent.includes(text), label + '明确报错'); scenarios++; }
    finally { h.dom.window.close(); }
  }

  const boundary = fixtures(3); boundary[0].risk = '低风险'; boundary[1].risk = 'R20'; boundary[2].risk = 'R2中低风险';
  const riskHarness = await harness(boundary);
  try {
    await riskHarness.open('research'); const f = riskHarness.d.querySelector('[aria-label="风险"]');
    f.value = 'R1'; submit(riskHarness); assert.equal(rowsIn(riskHarness), 0, '低风险保险不推断为 R1，R20 不截成 R2');
    f.value = 'R2'; submit(riskHarness); assert.equal(rowsIn(riskHarness), 1); assert.ok(riskHarness.d.querySelector('.discover-product-row').textContent.includes('R2中低风险'));
    scenarios++;
  } finally { riskHarness.dom.window.close(); }

  let release; const gate = new Promise(r => { release = r; });
  const late = await harness(fixtures(600), p => p, offset => offset ? gate : null);
  try {
    const pending = late.open('research'); await tick(); assert.equal(late.reads.length, 2);
    late.reset(); release(); await pending;
    assert.equal(rowsIn(late), 0, '撤销身份后第二页晚到不得渲染旧目录');
    assert.ok(late.d.getElementById('discoverSubview').textContent.includes('旧账户资料已失效'));
    scenarios++;
  } finally { late.dom.window.close(); }

  const stale = await harness(fixtures(201));
  try {
    await stale.open('research'); const more = stale.d.querySelector('.discover-load-more');
    stale.reset(); more.click(); assert.equal(rowsIn(stale), 0, '失效的加载更多闭包不得重新展示旧目录');
    scenarios++;
  } finally { stale.dom.window.close(); }
  console.log('通过 ' + scenarios + ' 个场景：13658 条完整分页、100 条分批展示、完整筛选、原始风险、快照销售只读、6 类分页失败、风险边界和隐私晚到。');
})().catch(e => { console.error(e); process.exitCode = 1; });
