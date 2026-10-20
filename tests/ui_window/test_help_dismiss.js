/** 共用说明浮层的窄回归：真实 help.js，禁网 jsdom。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { JSDOM } = require('jsdom');

const sourcePath = path.resolve(__dirname, '../../js/help.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const dom = new JSDOM('<!doctype html><main id="fixture"></main><button id="outside"><span>页面按钮</span></button>', {
  url: 'https://fixture.test/recommend.html', runScripts: 'outside-only', pretendToBeVisual: true
});
const w = dom.window, d = w.document, host = d.getElementById('fixture'), outside = d.getElementById('outside');
let assertions = 0, networkAttempts = 0, outsideActions = 0;
function check(value, label) { assert.ok(value, label); assertions++; }
function equal(actual, expected, label) { assert.equal(actual, expected, label); assertions++; }
w.fetch = () => { networkAttempts++; throw Error('说明回归禁止网络'); };
w.XMLHttpRequest = class { constructor() { networkAttempts++; throw Error('说明回归禁止网络'); } };
outside.addEventListener('click', () => outsideActions++);

function disclosure(className, id) {
  const details = d.createElement('details'); details.className = className;
  if (id) details.id = id;
  const summary = d.createElement('summary'), icon = d.createElement('span'); icon.textContent = '?'; summary.append(icon);
  const content = d.createElement('div'), text = d.createElement('span'), button = d.createElement('button');
  text.textContent = '说明内部'; button.type = 'button'; button.textContent = '内部控件'; content.append(text, button);
  details.append(summary, content); host.append(details);
  return { details, summary, icon, text, button };
}
const research = disclosure('discover-research-help');
const profile = disclosure('mine-profile-help', 'mineProfileHelp');
const business = [disclosure('ah-action-card', 'accountRemittanceCard'), disclosure('market-explanation'), disclosure('ordinary-details')];
business.forEach(item => { item.details.open = true; });
w.eval(source);
// 模拟异步保险首次绘制与后续替换，均在共用代码初始化以后。
const insurance = disclosure('insurance-help');
const explicit = disclosure('future-help'); explicit.details.dataset.helpPopover = '';

function pointer(target) {
  const event = new w.MouseEvent('pointerdown', { bubbles: true, cancelable: true });
  target.dispatchEvent(event); return event;
}
function pressEscape(target) {
  const event = new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
  target.dispatchEvent(event); return event;
}
function clickOutside() {
  const event = pointer(outside.firstElementChild);
  // jsdom 不执行鼠标默认聚焦；显式补齐浏览器的按钮聚焦，再派发原生 click。
  outside.focus(); outside.firstElementChild.click(); return event;
}

try {
  for (const [name, item] of [['产品研究', research], ['我的账户资料', profile], ['动态保险', insurance], ['显式说明标记', explicit]]) {
    item.details.open = true;
    pointer(item.text); item.text.click();
    check(item.details.open, name + '内部点击保持展开');
    pointer(item.icon);
    check(item.details.open, name + 'summary子元素的pointerdown不误关');
    item.icon.click();
    check(!item.details.open, name + 'summary原生点击正常收回');
    pointer(item.icon); item.icon.click();
    check(item.details.open, name + 'summary原生点击正常展开');
    item.button.focus(); const escaped = pressEscape(item.button);
    check(!item.details.open, name + '内部Esc收回');
    equal(d.activeElement, item.summary, name + '内部Esc焦点返回summary');
    check(!escaped.defaultPrevented, name + 'Esc不吞其它页面键盘行为');
    item.details.open = true; outside.focus(); pressEscape(outside);
    check(!item.details.open && d.activeElement === outside, name + '外部Esc收回且不抢外部焦点');
    item.details.open = true; const before = outsideActions, pressed = clickOutside();
    check(!item.details.open && d.activeElement === outside, name + '外部点击收回且按钮保持焦点');
    equal(outsideActions, before + 1, name + '外部按钮动作照常执行一次');
    check(!pressed.defaultPrevented, name + '外部pointerdown不阻止默认行为');
  }

  insurance.details.open = true;
  const replacement = disclosure('insurance-help');
  insurance.details.replaceWith(replacement.details);
  check(!insurance.details.isConnected, '保险旧节点已被异步绘制替换');
  replacement.details.open = true; pointer(replacement.text);
  check(replacement.details.open, '保险替换节点内部点击保持展开');
  clickOutside(); check(!replacement.details.open, '保险替换节点无需重绑定即可外部收回');
  replacement.details.open = true; replacement.button.focus(); pressEscape(replacement.button);
  check(!replacement.details.open && d.activeElement === replacement.summary, '保险替换节点Esc收回且返回当前summary');

  const trigger = w.NavrenHelp.button('market'), child = d.createElement('span'); child.textContent = '图标'; trigger.append(child); host.append(trigger);
  trigger.click();
  let panel = d.querySelector('.navren-help-popover');
  check(panel && trigger.getAttribute('aria-expanded') === 'true', 'NavrenHelp原有浮层正常创建');
  equal(d.activeElement, panel.querySelector('button'), 'NavrenHelp展开时聚焦关闭控件');
  pointer(panel.querySelector('p')); panel.querySelector('p').click();
  equal(d.querySelector('.navren-help-popover'), panel, 'NavrenHelp内部点击保留同一浮层');
  pointer(child);
  equal(d.querySelector('.navren-help-popover'), panel, 'NavrenHelp触发按钮子元素pointerdown不误关');
  child.click();
  check(!d.querySelector('.navren-help-popover') && trigger.getAttribute('aria-expanded') === 'false', 'NavrenHelp触发按钮子元素点击正常切换关闭');
  equal(d.activeElement, trigger, 'NavrenHelp切换关闭后返回触发按钮');

  trigger.click(); panel = d.querySelector('.navren-help-popover'); pressEscape(panel.querySelector('button'));
  check(!d.querySelector('.navren-help-popover') && trigger.getAttribute('aria-expanded') === 'false', 'NavrenHelp原有Esc收回保持正常');
  equal(d.activeElement, trigger, 'NavrenHelp原有Esc返回触发按钮');
  trigger.click(); const before = outsideActions, pressed = clickOutside();
  check(!d.querySelector('.navren-help-popover') && trigger.getAttribute('aria-expanded') === 'false', 'NavrenHelp原有外部点击收回保持正常');
  check(d.activeElement === outside && !pressed.defaultPrevented, 'NavrenHelp收回后页面按钮可继续聚焦');
  equal(outsideActions, before + 1, 'NavrenHelp收回不吞页面按钮动作');
  trigger.click(); d.querySelector('.navren-help-popover button').click();
  check(!d.querySelector('.navren-help-popover'), 'NavrenHelp原有关闭按钮保持正常');

  business.forEach(item => check(item.details.open, item.details.className + '业务折叠始终保持展开'));
  equal(networkAttempts, 0, '整个说明回归没有网络请求');
  process.stdout.write(JSON.stringify({
    status: 'passed', assertions, outside_button_actions: outsideActions, real_network_requests: networkAttempts,
    source_sha256: crypto.createHash('sha256').update(source).digest('hex'),
    test_sha256: crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex')
  }, null, 2) + '\n');
} finally { dom.window.close(); }
