/** 禁网请求预算合同：运行真实 BankClient，仅替换计时器与 fetch。 */
'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 浏览器前置夹具：每个 VM 独立且初始为空，与 localStorage 分离。
function navrenSessionStorageFixture() {
  const values = new Map();
  return {
    getItem(key) { return values.get(String(key)) ?? null; },
    setItem(key, value) { values.set(String(key), String(value)); },
    removeItem(key) { values.delete(String(key)); },
    clear() { values.clear(); }
  };
}

const sourcePath = path.resolve(__dirname, '../../js/bank_client.js');
const source = fs.readFileSync(sourcePath, 'utf8');
let assertions = 0;
function equal(actual, expected, label) { assert.equal(actual, expected, label); assertions++; }
function ok(value, label) { assert.ok(value, label); assertions++; }

const timers = new Map(), timerLog = [], requests = [], listeners = new Map(), storage = new Map();
let nextTimer = 0, holdResponse = false;
const sandbox = {
  AbortController,
  Event: class Event { constructor(type) { this.type = type; } },
  sessionStorage: navrenSessionStorageFixture(),
  localStorage: {
    getItem(key) { return storage.get(key) ?? null; },
    setItem(key, value) { storage.set(key, String(value)); },
    removeItem(key) { storage.delete(key); }
  },
  addEventListener(type, callback) {
    if (!listeners.has(type)) listeners.set(type, new Set());
    listeners.get(type).add(callback);
  },
  dispatchEvent(event) {
    for (const callback of listeners.get(event.type) || []) callback(event);
    return true;
  },
  setTimeout(callback, delay) {
    const timer = { id: ++nextTimer, callback, delay, cleared: false };
    timers.set(timer.id, timer); timerLog.push(timer); return timer.id;
  },
  clearTimeout(id) {
    const timer = timerLog.find(item => item.id === id);
    if (timer) timer.cleared = true;
    timers.delete(id);
  },
  fetch(url, options) {
    // 此函数是唯一请求出口；没有导入或调用任何网络实现。
    const request = { url, options, timer: timerLog.at(-1) };
    requests.push(request);
    if (holdResponse) return new Promise((resolve, reject) => {
      const abort = () => { const error = new Error('fixture abort'); error.name = 'AbortError'; reject(error); };
      if (options.signal.aborted) abort();
      else options.signal.addEventListener('abort', abort, { once: true });
    });
    return Promise.resolve({
      ok: true, status: 200, headers: { get() { return null; } },
      async json() { return { workspace_id: 'timeout-fixture', privacy_revision: 42, fixture: true }; }
    });
  }
};
sandbox.window = sandbox;
vm.runInNewContext(source, sandbox, { filename: sourcePath });
sandbox.BankClient.acceptSelectedSession({ workspace_id: 'timeout-fixture', csrf: 'fixture-csrf', privacy_revision: 42 });

async function requestBudget(method, route, budget) {
  const before = requests.length;
  const result = await (method === 'GET' ? sandbox.BankClient.get(route) : sandbox.BankClient.post(route, { fixture: true }));
  const request = requests.at(-1);
  equal(requests.length, before + 1, `${method} ${route} 只创建一个假请求`);
  equal(request.timer.delay, budget, `${method} ${route} 的运行时预算`);
  equal(request.options.method, method, `${route} 请求方法`);
  ok(result.fixture && request.timer.cleared && timers.size === 0 && !request.options.signal.aborted,
    `${method} ${route} 成功返回并清理计时器`);
}

async function pendingRead(route) {
  const before = requests.length;
  const outcome = sandbox.BankClient.get(route).then(value => ({ value }), error => ({ error }));
  for (let i = 0; i < 8 && requests.length === before; i++) await Promise.resolve();
  equal(requests.length, before + 1, `${route} 等待中的假请求已创建`);
  return { outcome, request: requests.at(-1) };
}

(async () => {
  await requestBudget('GET', '/public-research-products', 45000);
  await requestBudget('GET', '/public-research-products?code=000001', 45000);
  await requestBudget('GET', '/public-product-performance?code=000001', 45000);
  await requestBudget('GET', '/public-products?limit=1&offset=0', 15000);
  await requestBudget('GET', '/overview', 15000);
  await requestBudget('POST', '/goals/prepare', 15000);
  await requestBudget('POST', '/public-research-products', 15000);

  holdResponse = true;
  const timeout = await pendingRead('/public-research-products');
  equal(timeout.request.timer.delay, 45000, '等待中的研究请求保持45秒预算');
  timeout.request.timer.callback();
  ok(timeout.request.options.signal.aborted, '达到预算立即中止研究请求');
  const timedOut = await timeout.outcome;
  ok(timedOut.error && /银行请求超时/.test(timedOut.error.message), '超时返回既有错误');
  ok(timeout.request.timer.cleared && timers.size === 0, '超时后清理计时器');

  const guard = sandbox.BankClient.guard();
  const revoked = await pendingRead('/public-research-products?code=000001');
  equal(revoked.request.timer.delay, 45000, '身份失效前详情读取保持45秒预算');
  sandbox.BankClient.beginAccountSwitch();
  ok(revoked.request.options.signal.aborted, '身份失效立即中止研究详情读取');
  ok(!guard.valid() && sandbox.BankClient.identity() === null, '身份失效同时撤销读取守卫');
  const invalidated = await revoked.outcome;
  ok(invalidated.error && !invalidated.value, '身份失效不会返回旧账户资料');
  ok(revoked.request.timer.cleared && timers.size === 0, '身份失效后清理计时器');

  process.stdout.write(JSON.stringify({
    status: 'passed', assertions, fake_requests: requests.length,
    fixture_posts: requests.filter(item => item.options.method === 'POST').length,
    real_network_requests: 0,
    source_sha256: crypto.createHash('sha256').update(source).digest('hex'),
    test_sha256: crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex')
  }, null, 2) + '\n');
})().catch(error => { console.error(error); process.exitCode = 1; });
