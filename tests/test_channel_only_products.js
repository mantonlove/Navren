/* 官网新目录规模与分页失败回归；只在内存 DOM 使用合成夹具。 */
const assert = require('node:assert/strict'), fs = require('node:fs');
const { JSDOM } = require('jsdom');
const script = fs.readFileSync('js/discover_workspace.js', 'utf8');
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
      if (path === '/products') return [{id:'SIM-1',name:'模拟活钱',source:'simulation',version:1,risk_level:1,term_days:0,min_purchase_minor:100,redemption_delay_days:0,profile_eligible:true}];
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

(async () => {
  const oldNav={version:'old-nav',product:{fund_id:'ORIENT:006210',name:'旧管理人净值',manager:'东方基金'},coverage:{actual_to:'2026-09-30'},latest_nav:{unit_nav:'1.0123',nav_on:'2026-09-30'}};
  const h=await harness(fixtures(2),p=>({...p,verified_nav_products:[oldNav]}));
  try {
    assert.equal(h.w.DiscoverWorkspace.normalize([],{products:fixtures(2),verified_nav_products:[oldNav]}).length,2,'旧响应的管理人附加资料不进入研究');
    await h.open('home');
    const cards=[...h.d.querySelectorAll('#productPreview .discover-preview')];
    assert.equal(cards.length,3);assert.ok(cards[0].id.includes('public_catalog'),'首页优先微众公开资料');
    assert.ok(cards[1].id.includes('public_catalog'));assert.ok(cards[2].id.includes('bank_product'));
    assert.ok(!h.d.getElementById('productPreview').textContent.includes('旧管理人净值'));
    await h.open('research');assert.equal(rowsIn(h),3);
    assert.ok(![...h.d.querySelector('[aria-label="资料身份"]').options].some(o=>o.value==='public_nav'));
    await h.open('research',{filters:{source:'public_nav'}});
    assert.ok(h.d.getElementById('discoverSubview').textContent.includes('原来源筛选已失效'));
    h.w.sessionStorage.setItem('navren_research_selection',JSON.stringify({workspace_id:'workspace-a',privacy_revision:1,privacy_epoch:0,refs:[{id:'ORIENT:006210',source_scope:'public_nav',version:'old-nav'}]}));
    await h.open('research');buttons(h).find(b=>b.textContent==='比较产品').click();
    assert.equal(h.w.DiscoverWorkspace.selected().length,0);
    assert.ok(h.d.getElementById('discoverSubview').textContent.includes('已退出选择'));
    await h.open('product',{source_scope:'public_nav',object_id:'ORIENT:006210',object_version:'old-nav'});
    assert.ok(h.d.getElementById('discoverSubview').textContent.includes('已移出当前产品研究'));
    assert.ok(h.d.getElementById('discoverSubview').textContent.includes('历史模拟组合仍可在账户中回看'));
    assert.equal(buttons(h).find(b=>b.textContent==='准备申购'),undefined);
    await h.open('product',{source_scope:'public_catalog',object_id:'000000',object_version:'v1'});
    const source=h.d.querySelector('a[href^="https://www.webank.com/"]');assert.ok(source);
    assert.ok(!h.d.querySelector('a[href*="orient-fund.com"]'));
    assert.ok(h.d.getElementById('discoverSubview').textContent.includes('收益指标与期间未披露'));
    console.log('通过：产品研究仅接微众披露、首页渠道优先、来源筛选、旧链接与旧选择失效、微众原文、未知收益。');
  } finally {h.dom.window.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
