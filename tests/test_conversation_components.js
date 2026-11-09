'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { renderCode, plainText } = require('../js/conversation_components.js');
let passed = 0;

function test(name, run) {
  run();
  passed += 1;
  process.stdout.write('通过 ' + passed + '：' + name + '\n');
}

function render(lang, text) { return renderCode({ lang, text }); }
function count(html, tag) { return (html.match(new RegExp('<' + tag + '(?:\\s|>)', 'g')) || []).length; }

function assertSafeMarkup(html) {
  assert.equal(typeof html, 'string');
  const allowedTags = new Set(['section', 'div', 'span', 'h3', 'dl', 'dt', 'dd', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'ol', 'li', 'p']);
  const fixedLabels = new Set(['要点', '方案对比', '步骤', '补充说明']);
  for (const tag of html.matchAll(/<([a-z][a-z0-9]*)([^>]*)>/g)) {
    assert.ok(allowedTags.has(tag[1]), '不允许的标签：' + tag[1]);
    const attributes = Array.from(tag[2].matchAll(/\s+([a-z-]+)="([^"]*)"/g));
    assert.equal(attributes.map(function (attribute) { return attribute[0]; }).join(''), tag[2]);
    for (const attribute of attributes) {
      assert.ok(attribute[1] === 'class' || attribute[1] === 'aria-label');
      if (attribute[1] === 'class') {
        assert.ok(attribute[2].split(' ').every(function (className) { return /^ncc-[a-z0-9-]+$/.test(className); }));
      } else assert.ok(fixedLabels.has(attribute[2]));
    }
  }
  assert.ok(!/<(?:script|style|a|button|input|iframe|img|svg)(?:\s|>)/i.test(html));
}

const payloads = {
  facts: '回复正文 | 约 15px | 以连续阅读为主\n关键数值 | 约 17px | 只突出当前问题需要核对的值\n适用内容 | 要点、进度和事实摘要',
  compare: '| 维度 | 普通段落 | 对比组件 |\n| :--- | --- | ---: |\n| 信息关系 | 依次说明 | 同一维度并排阅读 |\n| 适用时机 | 一条解释 | 两个以上方案需要比较 |\n| 内容来源 | 本轮已有信息 | 同样需要明确来源 |',
  steps: '明确问题 | 先说明需要比较或完成的目标。\n选取结构 | 根据内容关系选择要点、对比、步骤或旁注。\n保留依据 | 对未确认的信息写明状态，不用组件替代证据。',
  note: '组件用于组织这次交流中的已有内容。\n没有足够信息时，先用简短文字说明缺口；格式不合要求时保留原始代码块。'
};

test('四类真实内容均生成受限静态结构', function () {
  for (const type of Object.keys(payloads)) {
    const html = render('navren-' + type + ' 对话中的' + type, payloads[type]);
    assertSafeMarkup(html);
    assert.ok(html.includes('ncc-' + type));
  }
});

test('要点保留标签、值与可选说明', function () {
  const html = render('navren-facts 阅读尺度', payloads.facts);
  assert.equal(count(html, 'dt'), 3);
  assert.equal(count(html, 'dd'), 3);
  assert.equal((html.match(/class="ncc-description"/g) || []).length, 2);
  assert.ok(html.includes('约 15px'));
  assert.ok(html.includes('约 17px'));
  assert.equal(count(render('navren-facts', '日期 | 2026-10-26 |'), 'dt'), 1);
});

test('对比为三列表头、三行数据，Markdown分隔行不进入正文', function () {
  const html = render('navren-compare 何时使用组件', payloads.compare);
  assert.equal(count(html, 'thead'), 1);
  assert.equal(count(html, 'tbody'), 1);
  assert.equal(count(html, 'tr'), 4);
  assert.equal(count(html, 'th'), 6);
  assert.equal(count(html, 'td'), 6);
  assert.ok(!html.includes(':---'));
  assert.ok(html.includes('ncc-columns-3'));
});

test('对比支持2至4列，且每个数据行列数必须一致', function () {
  for (const columns of [2, 3, 4]) {
    const row = Array.from({ length: columns }, function (_, index) { return '内容' + index; }).join(' | ');
    const html = render('navren-compare', row + '\n' + row);
    assertSafeMarkup(html);
    assert.equal(count(html, 'th'), columns + 1);
    assert.equal(count(html, 'td'), columns - 1);
    assert.ok(html.includes('ncc-columns-' + columns));
  }
  for (const text of ['维度\n内容', '维度|甲|乙|丙|丁\n尺寸|一|二|三|四', '维度|甲|乙\n尺寸|一', '维度|甲\n尺寸|', '维度|甲\n---|--\n尺寸|一', '维度|甲\n---|---']) {
    assert.equal(render('navren-compare', text), null);
  }
});

test('步骤使用连续的小序号，旁注保留换行', function () {
  const html = render('navren-steps', payloads.steps);
  assert.equal(count(html, 'ol'), 1);
  assert.equal(count(html, 'li'), 3);
  for (const number of [1, 2, 3]) assert.ok(html.includes('class="ncc-step-number">' + number + '</span>'));
  assert.ok(render('navren-note', payloads.note).includes('内容。\n没有足够信息'));
});

test('标题和所有文字转义恶意HTML，单元格不能注入标签或属性', function () {
  const attack = '<img src=x onerror=alert(1)>&\'"';
  const escaped = '&lt;img src=x onerror=alert(1)&gt;&amp;&#39;&quot;';
  for (const type of ['facts', 'compare', 'steps', 'note']) {
    const text = type === 'facts' ? attack + ' | ' + attack + ' | ' + attack :
      type === 'compare' ? attack + ' | 方案\n维度 | ' + attack :
      type === 'steps' ? attack + ' | ' + attack : attack;
    const html = render('navren-' + type + ' <svg/onload=alert(2)>', text);
    assertSafeMarkup(html);
    assert.ok(html.includes(escaped));
    assert.ok(html.includes('&lt;svg/onload=alert(2)&gt;'));
    assert.ok(!html.includes('<img'));
    assert.ok(!html.includes('<svg'));
  }
});

test('实体二次转义，防止预编码HTML被解释为标签', function () {
  assert.ok(render('navren-note', '&lt;script&gt;alert(1)&lt;/script&gt;').includes('&amp;lt;script&amp;gt;'));
});

test('每类行数上限为6，超过时整块回退而非截断', function () {
  for (const type of ['facts', 'steps', 'compare']) {
    const header = type === 'compare' ? '维度 | 方案\n--- | ---\n' : '';
    assert.ok(render('navren-' + type, header + Array(6).fill('名称 | 说明').join('\n')));
    assert.equal(render('navren-' + type, header + Array(7).fill('名称 | 说明').join('\n')), null);
  }
});

test('标题、单元格、说明和整块正文长度超限均回退', function () {
  assert.ok(render('navren-note ' + '题'.repeat(40), '正文'));
  assert.equal(render('navren-note ' + '题'.repeat(41), '正文'), null);
  for (const text of ['名'.repeat(33) + ' | 值', '标签 | ' + '值'.repeat(81), '标签 | 值 | ' + '注'.repeat(141)]) {
    assert.equal(render('navren-facts', text), null);
  }
  assert.equal(render('navren-steps', '名'.repeat(41) + ' | 说明'), null);
  assert.equal(render('navren-steps', '名称 | ' + '注'.repeat(161)), null);
  assert.equal(render('navren-compare', '维度 | ' + '题'.repeat(41) + '\n名称 | 内容'), null);
  assert.equal(render('navren-compare', '维度 | 方案\n名称 | ' + '值'.repeat(121)), null);
  assert.equal(render('navren-facts', '名 | ' + '值'.repeat(2500)), null);
});

test('旁注400个Unicode码点可完整显示，401个回退', function () {
  assert.ok(render('navren-note', '字'.repeat(400)).includes('字'.repeat(400)));
  assert.equal(render('navren-note', '字'.repeat(401)), null);
  assert.ok(render('navren-note', '😀'.repeat(400)));
  assert.equal(render('navren-note', '😀'.repeat(401)), null);
});

test('空值、空行、字段缺失、额外字段和畸形分隔均回退', function () {
  for (const input of [null, undefined, 'navren-note', [], {}, { lang: 'navren-note' }, { lang: 'navren-note', text: 1 }, { lang: 'navren-note', text: '正文', html: '<b>内容</b>' }, { lang: 'navren-note', text: '正文', url: 'https://example.com' }, { lang: 'navren-note', text: '正文', action: 'transfer' }, { lang: 'navren-note', text: '正文', permission: 'allow' }]) {
    assert.equal(renderCode(input), null);
  }
  for (const type of ['facts', 'compare', 'steps', 'note']) assert.equal(render('navren-' + type, '  \n '), null);
  for (const type of ['facts', 'steps']) {
    for (const text of ['只有一列', '标签 | ', ' | 值', '标签 | 值\n\n标签 | 值', '标签 | 值 | 注 | 额外']) {
      assert.equal(render('navren-' + type, text), null);
    }
  }
  assert.equal(render('navren-note', '正文\u0000隐藏内容'), null);
});

test('普通代码围栏、未知类型和近似名称不被劫持', function () {
  for (const lang of ['', 'js', 'javascript', 'html', 'css', 'json', 'navren', 'navren-chart', 'Navren-facts', 'language-navren-facts', 'navren-facts-extra', 'navren-note\n标题']) {
    assert.equal(render(lang, '标签 | 值'), null);
  }
});

test('CRLF换行可用，原输入对象不被修改，输出确定', function () {
  const input = Object.freeze({ lang: 'navren-facts 基础信息', text: '名称 | Navren\r\n版本 | 候选' });
  const before = JSON.stringify(input);
  const html = renderCode(input);
  assert.ok(html);
  assert.equal(html, renderCode(input));
  assert.equal(JSON.stringify(input), before);
});

test('浏览器全局与Node导出一致，加载不访问DOM或网络', function () {
  const source = fs.readFileSync(path.join(__dirname, '../js/conversation_components.js'), 'utf8');
  const sandbox = { window: {} };
  vm.runInNewContext(source, sandbox, { timeout: 1000 });
  assert.equal(typeof sandbox.window.NavrenConversationComponents.renderCode, 'function');
  assert.equal(sandbox.window.NavrenConversationComponents.renderCode({ lang: 'navren-note', text: '只读候选' }), render('navren-note', '只读候选'));
  assert.deepEqual(Object.keys(sandbox.window), ['NavrenConversationComponents']);
});

test('摘要仅去除通过同一校验的围栏，完整保留标题、数字和说明', function () {
  const source = '前文\n```navren-facts 待核对的金额\n计划金额 | 1000元 | 尚未经确认\n费用 | 12.50元 | 以实际回执为准\n```\n后文';
  const summary = plainText(source);
  assert.equal(summary, '前文\n待核对的金额\n计划金额 | 1000元 | 尚未经确认\n费用 | 12.50元 | 以实际回执为准\n后文');
  assert.equal(plainText('~~~navren-note 风险说明\n未经确认不得转账1000元，也不能视为已经授权。\n~~~'), '风险说明\n未经确认不得转账1000元，也不能视为已经授权。\n');
});

test('摘要对未知、畸形、超限和无闭合围栏逐字保留', function () {
  for (const source of [
    '```javascript\nconst amount = 1000;\n```',
    '```navren-unknown 金额\n1000元\n```',
    '```navren-facts 金额\n只有一列：1000元\n```',
    '```navren-compare\n维度 | 方案\n金额 | 1000元 | 多余内容\n```',
    '```navren-note\n' + '字'.repeat(401) + '1000元\n```',
    '前文\n```navren-note 风险\n尚未经确认，1000元\n',
    '```navren-note\n金额1000元\n``',
    '````navren-note\n金额1000元\n```'
  ]) assert.equal(plainText(source), source);
});

test('摘要不处理未知围栏内部的组件示例，支持CRLF和不同闭合长度', function () {
  const nested = '````javascript\n```navren-note\n风险金额1000元\n```\n````';
  assert.equal(plainText(nested), nested);
  assert.equal(plainText('```navren-note\r\n金额1000元\r\n````\r\n'), '金额1000元\r\n');
  assert.equal(plainText('普通文字1000元'), '普通文字1000元');
  assert.equal(plainText(''), '');
});

process.stdout.write('\n全部通过：' + passed + ' 组；仅本地纯函数验证，未调用模型或业务接口。\n');
