/* 禁网对象绑定复核：实际 BankClient、NavrenViews、AccountWorkspace，纯内存 DOM。
 * BankUI 仅替代节点/展示适配器，不替代身份、请求守卫或账户业务渲染。
 * 已知缺陷按验收合同失败；输出复现证据，不把缺陷复现称为验收通过。
 */
const fs = require('fs');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const clone = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const settle = async () => { for (let i = 0; i < 5; i++) await tick(); };
const fixtures = (workspace = 'ws-a', version = 7, cash = 2039000) => ({
  workspace_id: workspace, privacy_revision: 1, version, as_of: '2026-10-06',
  total_assets_principal_minor: cash, total_cash_minor: cash,
  holding_principal_minor: 0, pending_redemption_minor: 0, liabilities_minor: 0,
  investable_minor: cash - 100000, commitment_window_end: '2026-11-05',
  notice: '本金口径', accounts: [{ id: 'a-main', name: '日常账户', balance_minor: cash, currency: 'CNY' }],
  cards: [], holdings: [], operations: [], commitments: [], orders: [],
  scheduled_payments: [], beneficiaries: [], subscriptions: [], mandates: [],
  orchestration_tasks: []
});
const identity = state => ({ workspace_id: state.workspace_id, privacy_revision: state.privacy_revision, csrf: 'offline-csrf' });
const budget = (state, amount = state.investable_minor) => ({
  workspace_id: state.workspace_id, privacy_revision: state.privacy_revision,
  version: state.version, cash_minor: state.total_cash_minor, investable_minor: amount,
  reserve_minor: 100000, upcoming_due_minor: 0, held_minor: 0, as_of: state.as_of
});
const scenarios = [], failures = [], evidence = {};

async function setup({ initial = fixtures(), url = 'https://navren.invalid/account.html', holdBudget = false, records = {} } = {}) {
  const dom = new JSDOM(read('account.html'), { url, runScripts: 'outside-only' });
  const w = dom.window;
  await new Promise(resolve => w.document.readyState === 'loading'
    ? w.document.addEventListener('DOMContentLoaded', resolve, { once: true }) : resolve());
  let selected = clone(initial), displayState = clone(initial);
  const requests = [], navigations = [], pending = [], draftInvocations = [];
  w.XMLHttpRequest = class { constructor() { throw Error('真实网络已禁止'); } };
  w.WebSocket = class { constructor() { throw Error('真实网络已禁止'); } };
  w.BroadcastChannel = undefined;
  w.requestAnimationFrame = fn => { fn(); return 0; };
  w.getInterfaceMode = () => 'classic';
  w.showToast = () => {};
  w.NavrenApp = { href: value => value, navigate: value => navigations.push(value) };
  const response = data => ({ ok: true, status: 200, json: async () => clone(data), headers: {
    get: key => key === 'X-Navren-Workspace' ? data.workspace_id
      : key === 'X-Navren-Privacy-Revision' ? String(data.privacy_revision) : null
  } });
  w.fetch = async (resource, options) => {
    const pathname = new URL(resource, w.location.href).pathname;
    const request = { path: resource, method: options.method, workspace: selected.workspace_id,
      body: options.body ? JSON.parse(options.body) : null, signal: options.signal };
    requests.push(request);
    if (pathname === '/api/finance/session') return response(identity(selected));
    if (pathname === '/api/finance/financial-records') return response({
      workspace_id: selected.workspace_id, privacy_revision: selected.privacy_revision,
      version: selected.version, as_of: selected.as_of,
      records: { loans: [], deposit_contracts: [], wealth_goals: [], support_cases: [], permission_grants: [], ...clone(records) }
    });
    if (pathname === '/api/finance/budget') {
      const snapshot = clone(selected);
      // 故意忽略 AbortSignal，验证客户端收到真实晚到响应后仍拒绝旧身份。
      if (holdBudget) return new Promise(resolve => pending.push({ workspace: snapshot.workspace_id,
        signal: options.signal, resolve: value => resolve(response(value || budget(snapshot))) }));
      return response(budget(snapshot));
    }
    if (pathname === '/api/finance/page-contexts') return response({
      workspace_id: selected.workspace_id, privacy_revision: selected.privacy_revision,
      context_id: 'offline-page-context'
    });
    throw Error('禁网测试未定义请求：' + resource);
  };
  const node = (tag, text, cls) => {
    const n = w.document.createElement(tag);
    if (text != null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  w.BankUI = {
    node, state: () => displayState, products: () => [], actions: {},
    button: (text, handler) => { const b = node('button', text); b.type = 'button'; b.onclick = handler; return b; },
    showError: (target, error) => target.replaceChildren(node('p', error.message)),
    operationStatus: op => op.state, load: async () => {},
    // 只记录打开既有回执的入参，不调用 prepare 或 confirm。
    openDraft: (draft, trigger, context) => { draftInvocations.push({ draft: clone(draft), operation_id: trigger?.dataset.operationId, context: clone(context || {}) }); }
  };
  w.CalendarUI = {};
  w.eval(read('js/bank_client.js'));
  w.BankClient.acceptSelectedSession(identity(selected));
  w.eval(read('js/view_context.js'));
  w.eval(read('js/account_workspace.js'));
  w.document.dispatchEvent(new w.Event('DOMContentLoaded'));
  return { w, dom, requests, pending, navigations, draftInvocations,
    cashText: cash => w.BankClient.money(cash),
    visible: () => w.document.getElementById('accountSubview').textContent,
    switchTo(next) {
      w.BankClient.beginAccountSwitch(); selected = clone(next); displayState = clone(next);
      w.BankClient.acceptSelectedSession(identity(selected));
      w.dispatchEvent(new w.CustomEvent('navren:account-rendered', { detail: { state: displayState, products: [] } }));
    },
    close: () => dom.window.close()
  };
}

async function scenario(name, fn) {
  try { await fn(); scenarios.push({ name, result: '通过' }); }
  catch (error) { scenarios.push({ name, result: '合同失败', message: error.message }); failures.push(name); }
}

(async () => {
  await scenario('普通新标签 URL 必须保留原对象版本', async () => {
    const t = await setup(); let opened;
    try {
      await t.w.NavrenViews.open('home'); await settle();
      const a = [...t.w.document.querySelectorAll('#accountFundsPreview a')]
        .find(x => new URL(x.href).searchParams.get('object_kind') === 'account');
      assert(a, '真实首页必须有账户对象链接');
      const nativeHref = a.href;
      a.dispatchEvent(new t.w.MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));
      await settle();
      assert.equal(t.w.NavrenViews.current().object_version, 7, '普通左键应保留版本');
      opened = await setup({ url: nativeHref, initial: fixtures('ws-a', 8, 8888800) });
      await opened.w.NavrenViews.start(); await settle();
      evidence.native_url = { href: nativeHref,
        ordinary_click_version: t.w.NavrenViews.current().object_version,
        fresh_tab_version: opened.w.NavrenViews.current().object_version ?? null,
        fresh_tab_displays_new_ledger: opened.visible().includes(opened.cashText(8888800)) };
      assert.equal(new URL(nativeHref).searchParams.get('object_version'), '7', 'href 丢失账本版本');
      assert(!opened.visible().includes(opened.cashText(8888800)), '旧版本链接不能静默显示新账本金额');
    } finally { opened?.close(); t.close(); }
  });

  await scenario('旧账本版本不得显示最新金额或改写成最新提问引用', async () => {
    const t = await setup();
    try {
      await t.w.NavrenViews.open('account-detail', {
        object_kind: 'account', object_id: 'a-main', source_scope: 'simulation', object_version: 6
      }); await settle();
      const text = t.visible();
      const cite = [...t.w.document.querySelectorAll('#accountSubview button')].find(b => b.textContent === '引用此对象提问');
      if (cite) { cite.click(); await settle(); }
      const sent = t.requests.find(r => r.path === '/api/finance/page-contexts');
      evidence.stale_ledger = { requested: 6, current: 7,
        displays_current_cash: text.includes(t.cashText(2039000)),
        has_transfer_action: !!t.w.document.querySelector('#accountSubview a[href*="view=payments"]'),
        citation_version: sent?.body?.reference?.version ?? null,
        return_view_version: sent?.body?.return_view?.object_version ?? null };
      assert(!text.includes(t.cashText(2039000)), '请求版本 6 时显示版本 7 的现金');
      assert(!cite, '失效版本不能生成最新引用按钮');
    } finally { t.close(); }
  });

  await scenario('错误来源与对象类型不能静默替换为当前现金账户', async () => {
    const t = await setup();
    try {
      const observed = [];
      for (const source of ['imported', 'historical', 'public_nav']) {
        await t.w.NavrenViews.open('account-detail', {
          object_kind: 'account', object_id: 'a-main', source_scope: source, object_version: 7
        }); await settle();
        observed.push({ requested_source: source, route_source: t.w.NavrenViews.current().source_scope,
          displays_current_cash: t.visible().includes(t.cashText(2039000)),
          has_transfer_action: !!t.w.document.querySelector('#accountSubview a[href*="view=payments"]') });
      }
      await t.w.NavrenViews.open('account-detail', {
        object_kind: 'holding', object_id: 'a-main', source_scope: 'simulation', object_version: 7
      }); await settle();
      evidence.wrong_source = observed;
      evidence.wrong_kind = { requested_kind: 'holding', displays_cash_account: t.visible().includes(t.cashText(2039000)) };
      assert(observed.every(x => !x.displays_current_cash && !x.has_transfer_action), '错误来源被替换成当前现金账户并显示操作入口');
      assert(!evidence.wrong_kind.displays_cash_account, 'holding 引用被当成 account');
      await t.w.NavrenViews.open('home'); await settle();
      const homeRejections = [];
      for (const metadata of [
        { source_scope: 'historical', object_version: 7 },
        { source_scope: 'simulation', object_version: 6 }
      ]) {
        await t.w.NavrenViews.open('home', { object_kind: 'account', object_id: 'a-main', ...metadata });
        await settle();
        const homeHidden = t.w.document.getElementById('accountHome').hidden;
        const warningVisible = !t.w.document.getElementById('accountSubview').hidden && !!t.visible();
        homeRejections.push({ ...metadata, home_hidden: homeHidden, warning_visible: warningVisible });
        assert(homeHidden && warningVisible, '首页拒绝失效引用时必须隐藏旧首页并显示重查提示');
      }
      evidence.home_invalid_reference = homeRejections;
    } finally { t.close(); }
  });

  await scenario('真实 BankClient 拒绝同数值版本、同对象 ID 的跨账户迟到 budget', async () => {
    const t = await setup({ holdBudget: true });
    try {
      const metadata = { object_kind: 'account', object_id: 'a-main', source_scope: 'simulation', object_version: 7 };
      await t.w.NavrenViews.open('account-detail', metadata); await settle();
      assert.equal(t.pending.length, 1);
      const old = t.pending[0];
      const next = fixtures('ws-b', 7, 3033330);
      t.switchTo(next); await settle();
      assert.equal(t.pending.length, 2, '切换后重取当前账户的 budget');
      t.pending[1].resolve(budget(next, 2222200)); await settle();
      const before = t.visible();
      old.resolve(budget(fixtures('ws-a'), 1111100)); await settle();
      const after = t.visible();
      evidence.late_budget = { old_request_aborted: old.signal.aborted,
        same_ledger_version: next.version === 7, same_object_id: next.accounts[0].id === 'a-main',
        current_budget_visible: after.includes(t.cashText(2222200)),
        old_budget_visible: after.includes(t.cashText(1111100)), unchanged_after_old_response: before === after };
      assert(old.signal.aborted, '真实 client 需要废止原 GET');
      assert(after.includes(t.cashText(2222200)), '当前账户 budget 应展示');
      assert(!after.includes(t.cashText(1111100)), '旧身份预算不能复活');
      assert.equal(after, before, '迟到响应不能修改当前详情');
      assert.equal(t.w.BankClient.identity().workspace_id, 'ws-b');
    } finally { for (const p of t.pending) p.resolve(); t.close(); }
  });

  await scenario('操作引用须使用操作版本并仅打开同一 operation_id 的回执', async () => {
    const initial = fixtures();
    initial.operations = [{ id: 'op-a', version: 3, action: 'transfer', state: 'unknown',
      payload: { action: 'transfer', amount_minor: 5000, idempotency_key: 'existing-key' } }];
    const t = await setup({ initial });
    try {
      await t.w.NavrenViews.open('home'); await settle();
      const row = [...t.w.document.querySelectorAll('#accountUpcomingPreview a')]
        .find(a => new URL(a.href).searchParams.get('object_kind') === 'operation');
      assert(row);
      const hrefVersion = new URL(row.href).searchParams.get('object_version');
      row.dispatchEvent(new t.w.MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));
      await settle();
      const normalVersion = t.w.NavrenViews.current().object_version;
      await t.w.NavrenViews.open('operation-detail', {
        object_kind: 'operation', object_id: 'op-a', source_scope: 'simulation', object_version: 7
      }); await settle();
      const conflictHasReceipt = !!t.w.document.querySelector('#accountSubview [data-operation-id]');
      await t.w.NavrenViews.open('operation-detail', {
        object_kind: 'operation', object_id: 'op-a', source_scope: 'simulation', object_version: 3
      }); await settle();
      const receipt = t.w.document.querySelector('#accountSubview [data-operation-id]');
      assert(receipt, '真实有效操作版本仍须可查询回执');
      receipt.click();
      const invocation = t.draftInvocations[0];
      evidence.operation_binding = { ledger_version: 7, operation_version: 3,
        native_link_version: hrefVersion, ordinary_click_version: normalVersion,
        wrong_operation_version_has_receipt: conflictHasReceipt, receipt_invocation: invocation };
      assert.equal(hrefVersion, '3', '原生链接必须携带操作版本而非账本版本');
      assert.equal(normalVersion, 3, '普通点击必须携带具体操作版本');
      assert(!conflictHasReceipt, '账本版本不能作为操作版本解锁回执');
      assert.deepEqual(invocation.draft, { operation_id: 'op-a' }, '从详情打开同一操作，不重新传业务 payload');
      assert.equal(invocation.context.expected_operation_version, 3);
    } finally { t.close(); }
  });

  await scenario('目标档案缺少自身版本时不能借用账本版本', async () => {
    // finance_gateway.financial_records 的真实字段白名单不含 object_version。
    // goal_service._item 使用目标字段摘要作为自身版本，不能以账本数值代替。
    const goal = { id: 'goal-one', name: '旅行安排', target_minor: 250000,
      saved_minor: 50000, due_on: '2026-10-20', status: 'active' };
    const t = await setup({ records: { wealth_goals: [goal] } });
    try {
      await t.w.NavrenViews.open('home'); await settle();
      const row = [...t.w.document.querySelectorAll('#accountUpcomingPreview a')]
        .find(a => new URL(a.href).searchParams.get('object_kind') === 'goal');
      assert(row, '真实形状的目标档案应保留只读入口');
      const hrefVersion = new URL(row.href).searchParams.get('object_version');
      row.dispatchEvent(new t.w.MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));
      await settle();
      const routeVersion = t.w.NavrenViews.current().object_version ?? null;
      assert(t.visible().includes(goal.name), '旧无版本目标档案仍可只读查看');
      const cite = [...t.w.document.querySelectorAll('#accountSubview button')]
        .find(b => b.textContent === '引用此对象提问');
      assert(cite); cite.click(); await settle();
      const sent = t.requests.find(r => r.path === '/api/finance/page-contexts');
      await t.w.NavrenViews.open('record-detail', {
        object_kind: 'goal', object_id: goal.id, source_scope: 'simulation', object_version: 7
      }); await settle();
      evidence.goal_binding = { ledger_version: 7, archive_has_goal_version: false,
        native_link_version: hrefVersion, ordinary_click_version: routeVersion,
        citation_version: sent?.body?.reference?.version ?? null,
        ledger_version_request_displays_goal: t.visible().includes(goal.name) };
      assert.equal(hrefVersion, null, '无目标版本不能伪造账本版本链接');
      assert.equal(routeVersion, null, '无目标版本不能伪造路由版本');
      assert.equal(evidence.goal_binding.citation_version, null, '目标引用不能伪造账本版本');
      assert(!evidence.goal_binding.ledger_version_request_displays_goal,
        '未知目标自身版本时必须拒绝以账本版本冒充的旧引用');
    } finally { t.close(); }
  });

  console.log(JSON.stringify({ scenarios, evidence, contract_failures: failures.length,
    boundary: '仅内存 JSDOM；实际三模块，展示适配器与金融响应为假数据；未运行浏览器或真实 HTTP/金融操作' }, null, 2));
  if (failures.length) process.exitCode = 1;
})().catch(error => { console.error(error); process.exitCode = 1; });
