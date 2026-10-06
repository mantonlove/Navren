/* 真实路由脚本的内存行为测试；JSDOM 不加载资源，不访问本地网站或任何网络。 */
const fs = require('fs');
const assert = require('assert');
const { JSDOM } = require('jsdom');
const code = fs.readFileSync('js/view_context.js', 'utf8');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
async function until(test, message) { for (let i = 0; i < 40; i++) { if (test()) return; await tick(); } assert(test(), message); }
function fixture(page = 'account', suffix = '') {
  const rootClass = page === 'mine' ? 'mine-page' : 'rec-page';
  const dom = new JSDOM(`<body data-page="${page}"><main class="main"><div class="page-scroll"><section class="${rootClass}"><header class="account-view-heading"><h1 id="viewHeading" data-view-heading tabindex="-1">页面</h1></header><div id="surface"></div><button id="listFocus">原位置</button></section></div></main></body>`, { url: `https://navren.test/${page}.html${suffix}`, runScripts: 'outside-only' });
  const w = dom.window, d = w.document, posts = [], navigations = [], renders = [], resets = [];
  let identity = { workspace_id: 'workspace-one', privacy_revision: 4 }, refresh = async () => ({ ...identity }), post = async () => ({ ...identity, context_id: 'opaque-context-one' });
  w.requestAnimationFrame = fn => w.setTimeout(fn, 0);
  w.fetch = () => { throw new Error('此测试禁止网络访问'); };
  w.NavrenApp = { href: raw => { const u = new URL(raw); return u.pathname + u.search + u.hash; }, navigate: url => navigations.push(url) };
  w.BankClient = {
    identity: () => ({ ...identity }),
    refreshIdentity: async () => { const result = await refresh(); identity = { ...result }; return { ...result }; },
    post: async (path, body) => { posts.push({ path, body: JSON.parse(JSON.stringify(body)) }); return post(); },
    guard: () => { const snapshot = { ...identity }, revision = w.NavrenViews.revision(); return { valid: data => revision === w.NavrenViews.revision() && snapshot.workspace_id === identity.workspace_id && snapshot.privacy_revision === identity.privacy_revision && (!data || data.workspace_id === snapshot.workspace_id && data.privacy_revision === snapshot.privacy_revision) }; }
  };
  w.addEventListener('navren:privacy-reset', () => resets.push({ ...identity }));
  w.eval(code);
  const render = async context => { renders.push(JSON.parse(JSON.stringify(context))); d.getElementById('surface').textContent = context.view; };
  w.NavrenViews.register(page, render);
  return { dom, w, d, posts, navigations, renders, resets, render,
    setIdentity: value => { identity = { ...value }; }, setRefresh: fn => { refresh = fn; }, setPost: fn => { post = fn; },
    async start() { await w.NavrenViews.start(); await tick(); },
    close() { w.close(); } };
}
const suites = [];
function test(name, run) { suites.push({ name, run }); }

test('跨页对象返回沿用应用历史，模式切换快照保留定位',async()=>{
  const t=fixture('account','?view=account-detail&object_kind=account&object_id=one#account-detail');try{
    await t.start();Object.defineProperty(t.d,'referrer',{value:'https://navren.test/mine.html'});
    t.w.history.pushState({...t.w.history.state,navrenDepth:0},'',t.w.location.href);
    let backs=0;t.w.history.back=()=>backs++;t.w.NavrenViews.back();assert.equal(backs,1);
    t.d.querySelector('.page-scroll').scrollTop=207;t.d.getElementById('listFocus').focus();
    const snapshot=t.w.NavrenViews.snapshot();assert.equal(snapshot.scrollposition,207);assert.equal(snapshot.focustarget,'listFocus');assert.equal(snapshot.object_id,'one');assert.equal(snapshot.workspace_id,'workspace-one');
  }finally{t.close();}
});

test('后退恢复日期、来源、组合、筛选、列表位置及焦点', async () => {
  const t = fixture(); try {
    await t.start();
    const meta = { source_scope: 'simulation', portfolio_id: 'portfolio-old', date: '2025-09-17', period: '2025-09', time_view: 'week', filters: { type: 'returns', from: '2025-09-01', to: '2025-09-30', account_id: 'a-one' }, selected: ['return-item'], listposition: 8 };
    await t.w.NavrenViews.open('calendar', meta); await tick();
    const scroller = t.d.querySelector('.page-scroll'); scroller.scrollTop = 123;
    t.d.getElementById('listFocus').focus();
    await t.w.NavrenViews.open('calendar-day', { ...meta, date: '2025-09-18', object_kind: 'calendar_day', object_id: '2025-09-18' });
    const before = t.renders.length; t.w.NavrenViews.back();
    await until(() => t.renders.length > before && t.w.NavrenViews.current()?.view === 'calendar', '后退应完成原视图恢复'); await tick();
    const c = t.w.NavrenViews.current();
    for (const key of ['date', 'period', 'source_scope', 'portfolio_id', 'time_view', 'listposition']) assert.equal(c[key], meta[key], key);
    assert.deepEqual(JSON.parse(JSON.stringify(c.filters)), meta.filters);
    assert.deepEqual(JSON.parse(JSON.stringify(c.selected)), meta.selected);
    assert.equal(scroller.scrollTop, 123); assert.equal(t.d.activeElement.id, 'listFocus');
    assert.equal(new URL(t.w.location.href).searchParams.get('portfolio_id'), 'portfolio-old');
  } finally { t.close(); }
});

test('十一旧账户入口、个人外观与发现别名正确转译', async () => {
  const mapping = { overview: 'home', calendar: 'calendar', payments: 'payments', cards: 'cards', subscriptions: 'subscriptions', wealth: 'holdings', tasks: 'plans', records: 'activity', statements: 'statements', archive: 'archive' };
  for (const [old, expected] of Object.entries(mapping)) { const t = fixture('account', '#' + old); try { await t.start(); assert.equal(t.w.NavrenViews.current().view, expected, old); } finally { t.close(); } }
  const publicPage = fixture('account', '#public'); try { await publicPage.start(); assert.equal(publicPage.navigations.length, 1); assert(publicPage.navigations[0].includes('recommend.html')); assert(publicPage.navigations[0].includes('view=research')); assert.equal(publicPage.renders.length, 0); } finally { publicPage.close(); }
  for (const [page, old, expected] of [['mine', 'appearance', 'settings'], ['recommend', 'products', 'research'], ['recommend', 'marketHotHeading', 'home']]) { const t = fixture(page, '#' + old); try { await t.start(); assert.equal(t.w.NavrenViews.current().view, expected); } finally { t.close(); } }
});

test('旧 graph_view/from/to 与日历组合日期及对象 ID URL 保留', async () => {
  const t = fixture('mine', '?graph_view=cashflow&from=2026-01-01&to=2026-06-30&object_kind=goal&object_id=goal%3Aone&object_version=3&source_scope=simulation#graph');
  try { await t.start(); const c = t.w.NavrenViews.current(); assert.deepEqual(JSON.parse(JSON.stringify(c.filters)), { graph_view: 'cashflow', from: '2026-01-01', to: '2026-06-30' }); assert.equal(c.object_id, 'goal:one'); const url = new URL(t.w.location.href); for (const key of ['graph_view', 'from', 'to', 'object_kind', 'object_id', 'object_version', 'source_scope']) assert(url.searchParams.has(key), key); await t.w.NavrenViews.open('settings'); t.w.NavrenViews.back(); await until(() => t.w.NavrenViews.current()?.view === 'graph', '图谱后退应恢复'); assert.equal(t.w.NavrenViews.current().filters.to, '2026-06-30'); } finally { t.close(); }
  const calendar = fixture('account', '?portfolio=history%3Aone&calendar_on=2025-04-06&source_scope=simulation&object_kind=calendar_item&object_id=payment%3Aone#calendar');
  try { await calendar.start(); const c = calendar.w.NavrenViews.current(); assert.equal(c.portfolio_id, 'history:one'); assert.equal(c.date, '2025-04-06'); assert.equal(c.object_id, 'payment:one'); const url = new URL(calendar.w.location.href); assert.equal(url.searchParams.get('portfolio_id'), c.portfolio_id); assert.equal(url.searchParams.get('date'), c.date); assert.equal(url.searchParams.get('object_id'), c.object_id); } finally { calendar.close(); }
});

test('三个业务页面不创建提问框，对象入口进入聊天草稿而不发送 Agent', async () => {
  for(const page of ['account','recommend','mine']){
    const t=fixture(page);try{
      await t.start();assert.equal(t.posts.length,0);assert.equal(t.d.getElementById('navrenPageAsk'),null);assert.equal(t.d.querySelector('textarea'),null);
      await t.w.NavrenViews.open('account-detail',{object_kind:'account',object_id:'a-one',object_version:9,source_scope:'simulation'});
      const ref={kind:'account',id:'a-one',version:9,source_scope:'simulation',label:'工资账户'};
      await t.w.NavrenViews.reference(ref);
      assert.equal(t.posts.length,1);assert.equal(t.posts[0].path,'/page-contexts');assert.equal(t.posts[0].body.draft_only,true);assert.equal(t.posts[0].body.question,'');
      assert.equal(t.posts[0].body.reference.id,'a-one');assert.equal(t.posts[0].body.return_view.object_id,'a-one');assert.equal(t.navigations[0],'chat.html?context=opaque-context-one');
      assert(!t.navigations[0].includes('a-one'));assert(!t.w.history.state.navren.question);
    }finally{t.close();}
  }
});

test('预设问题可编辑，快速重复点击只创建一份聊天草稿',async()=>{
  const t=fixture();try{
    await t.start();const response=deferred();t.setPost(()=>response.promise);
    const question='请分析我私人的工资 123456789';const pending=t.w.NavrenViews.ask(question,{kind:'account',id:'a-one',version:9,source_scope:'simulation'});
    await until(()=>t.posts.length===1,'草稿应创建');await t.w.NavrenViews.ask(question);assert.equal(t.posts.length,1);
    response.resolve({workspace_id:'workspace-one',privacy_revision:4,context_id:'opaque-context-one'});await pending;
    assert.equal(t.posts[0].body.question,question);assert.equal(t.posts[0].body.draft_only,true);assert.equal(t.navigations.length,1);assert(!t.navigations[0].includes('123456789'));
  }finally{t.close();}
});

test('主动授权重置及核对时工作区变化清除旧引用', async () => {
  for (const change of [{ workspace_id: 'workspace-two', privacy_revision: 4 }, { workspace_id: 'workspace-one', privacy_revision: 5 }]) {
    const t = fixture(); try {
      await t.start();await t.w.NavrenViews.reference({kind:'account',id:'private-old',version:2,source_scope:'simulation'});
      t.setIdentity(change);await t.w.NavrenViews.verify();assert.equal(t.resets.length,1);assert.equal(t.w.NavrenViews.current(),null);
      await t.w.NavrenViews.ask('新的普通问题');assert.equal(t.posts[1].body.reference,null);
    } finally { t.close(); }
  }
});

test('提交前身份变化不得创建已捕获的旧私人引用草稿', async () => {
  const t = fixture(); try {
    await t.start();t.setRefresh(async()=>({workspace_id:'workspace-two',privacy_revision:8}));
    await assert.rejects(()=>t.w.NavrenViews.ask('查看此账户',{kind:'account',id:'private-old',version:2,source_scope:'simulation'}));
    assert.equal(t.posts.length,0);assert.equal(t.navigations.length,0);
  } finally { t.close(); }
});

test('迟到受保护渲染失效，身份失败遮蔽旧内容', async () => {
  const t = fixture(); try {
    await t.start(); const wait = deferred(), surface = t.d.getElementById('surface');
    t.w.NavrenViews.register('account', async c => { const guard = t.w.BankClient.guard(); if (c.view === 'slow') { await wait.promise; if (!guard.valid()) return; } surface.textContent = c.view; });
    const slow = t.w.NavrenViews.open('slow'); await tick(); await t.w.NavrenViews.open('calendar', { date: '2026-10-03' }); wait.resolve(); await slow; await tick();
    assert.equal(surface.textContent, 'calendar'); assert.equal(t.w.NavrenViews.current().view, 'calendar'); assert.equal(t.d.activeElement.id, 'viewHeading');
    t.setRefresh(async () => { throw new Error('身份服务失败'); }); t.w.dispatchEvent(new t.w.PopStateEvent('popstate')); await until(() => t.d.getElementById('navrenIdentityGate'), '身份失败应提示重试');
    assert(t.d.documentElement.classList.contains('navren-private-locked'), '未经核验旧私人视图必须保持遮蔽'); assert.equal(surface.textContent, 'calendar', '失败不重渲染旧视图');
  } finally { t.close(); }
});

test('迟到页面定位不能改变更新后的滚动及焦点', async () => {
  const t = fixture(); try {
    await t.start(); const callbacks = []; t.w.requestAnimationFrame = fn => callbacks.push(fn);
    await t.w.NavrenViews.open('calendar', { date: '2025-09-17' });
    await t.w.NavrenViews.open('activity');
    const scroller = t.d.querySelector('.page-scroll'); scroller.scrollTop = 444; t.d.getElementById('listFocus').focus();
    callbacks[0](); assert.equal(scroller.scrollTop, 444); assert.equal(t.d.activeElement.id, 'listFocus', '旧 RAF 不恢复旧位置或焦点');
    callbacks[1](); assert.equal(scroller.scrollTop, 0); assert.equal(t.d.activeElement.id, 'viewHeading');
  } finally { t.close(); }
});

test('已发送问题的迟到回执不能在切换视图后导航', async () => {
  const t = fixture(); try {
    await t.start(); const reply = deferred(); t.setPost(() => reply.promise);
    const send = t.w.NavrenViews.ask('显式提交的问题').then(() => null, error => error);
    await until(() => t.posts.length === 1, '显式提交应调用一次服务'); await t.w.NavrenViews.open('cards');
    reply.resolve({ workspace_id: 'workspace-one', privacy_revision: 4, context_id: 'late-context' });
    assert((await send)?.message, '旧视图的问题回执应判为失效'); assert.equal(t.navigations.length, 0); assert.equal(t.w.NavrenViews.current().view, 'cards');
  } finally { t.close(); }
});

test('更早身份核验的迟到失败不能覆盖已成功恢复视图', async () => {
  const t = fixture(); try {
    await t.start(); const old = deferred(); let count = 0;
    t.setRefresh(() => ++count === 1 ? old.promise : Promise.resolve({ workspace_id: 'workspace-one', privacy_revision: 4 }));
    t.w.dispatchEvent(new t.w.PopStateEvent('popstate')); await tick();
    t.w.dispatchEvent(new t.w.PopStateEvent('popstate')); await until(() => count === 2 && !t.d.documentElement.classList.contains('navren-private-locked'), '较新的身份核验应成功恢复');
    old.reject(new Error('较早请求失败')); await tick(); await tick();
    assert(!t.d.getElementById('navrenIdentityGate'), '已失效的身份请求错误不得挂回新的视图'); assert(!t.d.documentElement.classList.contains('navren-private-locked'));
  } finally { t.close(); }
});

test('历史热点深链接与筛选更替保留日期类型，引用仅携带版本身份',async()=>{
  const t=fixture('recommend','?view=market_history&date=2026-10-04&category=international#market_history');try{
    await t.start();const c=t.w.NavrenViews.current();assert.equal(c.date,'2026-10-04');assert.equal(c.filters.category,'international');
    const count=t.w.history.length;t.w.NavrenViews.update({date:'2026-10-05',filters:{category:'household'}});
    assert.equal(t.w.history.length,count);const u=new URL(t.w.location.href);assert.equal(u.searchParams.get('date'),'2026-10-05');assert.equal(u.searchParams.get('category'),'household');
    const ref={kind:'event',id:'e_history',source_scope:'market',version:2,parameters:{edition_date:'2026-10-05',revision:3,event_version:2}};
    await t.w.NavrenViews.reference(ref);assert.deepEqual(t.posts[0].body.reference,ref);assert.equal(t.posts[0].body.return_view.filters.category,'household');
    assert.equal(t.posts[0].body.draft_only,true);assert(!t.navigations[0].includes('2026-10-05'));
  }finally{t.close();}
});

test('历史收藏跨页 URL 和返回状态保留完整归档身份',async()=>{
  const t=fixture('recommend','?view=reading&object_kind=event&object_id=e_saved&object_version=2&edition_date=2026-10-04&revision=3#reading');try{
    await t.start();const c=t.w.NavrenViews.current();assert.equal(c.edition_date,'2026-10-04');assert.equal(c.revision,'3');assert.equal(c.object_version,'2');
    const url=new URL(t.w.NavrenViews.url(c),'https://navren.test');assert.equal(url.searchParams.get('edition_date'),'2026-10-04');assert.equal(url.searchParams.get('revision'),'3');
    await t.w.NavrenViews.open('home');t.w.NavrenViews.back();await until(()=>t.w.NavrenViews.current()?.view==='reading','后退应返回同归档对象');
    assert.equal(t.w.NavrenViews.current().edition_date,'2026-10-04');assert.equal(t.w.NavrenViews.current().revision,'3');
  }finally{t.close();}
});

(async () => { let failed = 0; for (const suite of suites) { try { await suite.run(); console.log('通过：' + suite.name); } catch (error) { failed++; console.error('未通过：' + suite.name + '\n' + error.stack); } } console.log(`真实视图路由 ${suites.length - failed}/${suites.length} 组内存行为检查通过`); if (failed) process.exitCode = 1; })();
