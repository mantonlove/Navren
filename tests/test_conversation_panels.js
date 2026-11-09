'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const panels = require('../js/conversation_panels.js');
const modulePath = path.resolve(__dirname, '../js/conversation_panels.js');
const outcomes = [];

function test(name, action) {
    try { action(); outcomes.push({ name, passed: true }); }
    catch (error) { outcomes.push({ name, passed: false, error: error.message }); }
}
const clone = value => JSON.parse(JSON.stringify(value));
const itemFor = type => ({
    metrics: { label: '已核实', value: '0', unit: '条', note: '截至本轮；仍待人工核验。' },
    timeline: { at: '周五 18:00', title: '提交候选', detail: '仅在数据完整时提交。' },
    checklist: { text: '核对样本', detail: '先检查缺失值，不能把待核实写成完成。', due: '周五 18:00' },
    progress: { label: '候选整理', value: 0, note: '0%不代表业务已验证。' },
    bars: { label: '待核实', value: 0 },
    quiz: { question: '什么时候可以确认？', answer: '仅当原始记录与读回一致；否则保持待核实。' }
}[type]);
const blockFor = type => type === 'text' ? { type, title: '限定', text: '这是候选结果，尚未验收。' } :
    { type, title: '阅读内容', ...(type === 'bars' ? { unit: '条' } : {}), items: [itemFor(type)] };
const make = (blocks = [blockFor('text')], extra = {}) => ({ title: '本轮阅读面板', summary: '候选结果；不自动更新。', blocks, ...extra });
const json = value => JSON.stringify(value);
const code = value => ({ lang: 'navren-panel', text: typeof value === 'string' ? value : json(value) });
function reject(value) {
    const input = code(value);
    assert.equal(panels.parse(input.text), null);
    assert.equal(panels.renderCode(input), null);
    assert.equal(panels.plainCode(input), null);
}
function setAt(value, keys, replacement) {
    const copy = clone(value);
    let parent = copy;
    for (const key of keys.slice(0, -1)) parent = parent[key];
    parent[keys[keys.length - 1]] = replacement;
    return copy;
}

test('UMD browser and CommonJS expose only a frozen pure API', () => {
    assert.deepEqual(Object.keys(panels).sort(), ['parse', 'plainCode', 'renderCode']);
    assert(Object.isFrozen(panels));
    const hostile = new Proxy({}, { get() { throw new Error('External facility accessed'); } });
    const context = vm.createContext({ window: {}, fetch: hostile, document: hostile, localStorage: hostile,
        sessionStorage: hostile, XMLHttpRequest: hostile, WebSocket: hostile });
    vm.runInContext(fs.readFileSync(modulePath, 'utf8'), context);
    const api = context.window.NavrenConversationPanels;
    assert(Object.isFrozen(api));
    for (const type of ['text', 'metrics', 'timeline', 'checklist', 'progress', 'bars', 'quiz']) {
        assert(api.renderCode(code(make([blockFor(type)]))));
        assert(api.plainCode(code(make([blockFor(type)]))));
    }
});

test('only the exact code language is recognized', () => {
    const text = json(make());
    for (const lang of ['', 'json', 'Navren-panel', 'navren-panel ', ' navren-panel', 'navren-panel title="x"', 'navren-panel\n']) {
        assert.equal(panels.renderCode({ lang, text }), null);
        assert.equal(panels.plainCode({ lang, text }), null);
    }
    for (const input of [null, [], 'navren-panel', {}, { lang: 'navren-panel' }, { lang: 'navren-panel', text: {} }]) {
        assert.equal(panels.renderCode(input), null);
        assert.equal(panels.plainCode(input), null);
    }
});

test('every supported block renders with native semantics and starts folded', () => {
    for (const type of ['text', 'metrics', 'timeline', 'checklist', 'progress', 'bars', 'quiz']) {
        const input = code(make([blockFor(type)]));
        assert(panels.parse(input.text));
        const html = panels.renderCode(input);
        assert(html.startsWith('<details class="ncp-panel">'));
        assert(html.includes('<summary class="ncp-summary">'));
        assert(html.includes('<div class="ncp-body">'));
        assert(html.includes('ncp-' + type));
        assert(!/<details\b[^>]*\bopen(?:\s|=|>)/.test(html));
        assert(html.includes('ncp-label-open') && html.includes('ncp-label-close'));
        if (type === 'checklist') {
            assert(html.includes('<label class="ncp-checklist-label"><input class="ncp-checkbox" type="checkbox">'));
            assert(html.includes('不会保存或执行事项'));
            assert(!/\b(?:checked|name|id|value|form)=/.test(html));
        }
        if (type === 'progress') assert(html.includes('value="0" max="100"'));
        if (type === 'quiz') {
            assert(html.includes('<details class="ncp-question">'));
            assert(html.includes('查看答案') && html.includes('收起答案'));
            assert(html.includes('仅当原始记录与读回一致'));
        }
    }
});

test('plain text keeps all values, qualifiers, deadlines and question answers in reading order', () => {
    const first = make([blockFor('metrics'), blockFor('timeline'), blockFor('checklist')]);
    assert.equal(panels.plainCode(code(first)), [
        '本轮阅读面板', '候选结果；不自动更新。', '阅读内容', '已核实：0 条', '截至本轮；仍待人工核验。',
        '阅读内容', '周五 18:00：提交候选', '仅在数据完整时提交。', '阅读内容', '核对样本',
        '先检查缺失值，不能把待核实写成完成。', '截止：周五 18:00'
    ].join('\n'));
    const second = make([blockFor('progress'), blockFor('bars'), blockFor('quiz')]);
    assert.equal(panels.plainCode(code(second)), [
        '本轮阅读面板', '候选结果；不自动更新。', '阅读内容', '候选整理：0%', '0%不代表业务已验证。',
        '阅读内容', '待核实：0 条', '阅读内容', '问：什么时候可以确认？', '答：仅当原始记录与读回一致；否则保持待核实。'
    ].join('\n'));
    assert.equal(panels.plainCode(code(make([blockFor('text')]))), '本轮阅读面板\n候选结果；不自动更新。\n限定\n这是候选结果，尚未验收。');
    const plain = panels.plainCode(code(second));
    for (const leakedKey of ['blocks', 'items', 'question', 'answer', 'progress', 'summary', 'type', 'value']) assert(!plain.includes(leakedKey));
});

test('model text cannot create elements, attributes, class names or actions', () => {
    const attack = '<img src=x onerror="fetch(\'https://bad.invalid\')">&</summary><script>alert(1)</script>\'"';
    const input = code(make([{ type: 'metrics', title: attack.slice(0, 35), items: [{ label: '<script>x</script>', value: attack.slice(0, 75), unit: '<x>', note: attack }] }],
        { title: '<svg/onload=alert(1)>', summary: attack }));
    const html = panels.renderCode(input);
    assert(html);
    assert(html.includes('&lt;svg/onload=alert(1)&gt;'));
    assert(html.includes('&lt;script&gt;x&lt;/script&gt;'));
    assert(html.includes('&amp;') && html.includes('&quot;') && html.includes('&#39;'));
    assert(!/<(?:script|img|svg)\b/i.test(html));
    const allowed = new Set(['details', 'summary', 'div', 'span', 'p', 'strong', 'small', 'ul', 'li', 'label', 'input', 'progress', 'h3']);
    for (const match of html.matchAll(/<\/?([a-z][a-z0-9-]*)([^>]*)>/gi)) {
        assert(allowed.has(match[1]), 'Unexpected element: ' + match[1]);
        const attrs = match[2];
        let consumed = '';
        for (const attr of attrs.matchAll(/\s+([\w-]+)="([^"]*)"/g)) {
            assert(['class', 'aria-label', 'value', 'max', 'type'].includes(attr[1]), 'Unexpected attribute: ' + attr[1]);
            if (attr[1] === 'class') assert(attr[2].split(' ').every(name => /^ncp-[a-z-]+$/.test(name)));
            consumed += attr[0];
        }
        assert.equal(consumed, attrs);
    }
    assert(panels.plainCode(input).includes('<script>x</script>'));
});

test('unknown keys anywhere reject the entire panel without retaining a valid subset', () => {
    const cases = [make([blockFor('text')], { href: 'https://bad.invalid' }),
        make([{ ...blockFor('text'), html: '<b>x</b>' }]),
        make([{ ...blockFor('bars'), style: 'width:100%' }]),
        make([{ type: 'metrics', items: [{ ...itemFor('metrics'), action: 'send' }] }]),
        make([{ type: 'timeline', items: [{ ...itemFor('timeline'), unknown: { title: 'x' } }] }]),
        make([{ type: 'checklist', items: [{ ...itemFor('checklist'), checked: true }] }]),
        make([{ type: 'progress', items: [{ ...itemFor('progress'), max: 100 }] }]),
        make([{ type: 'bars', items: [{ ...itemFor('bars'), unit: '条' }] }]),
        make([{ type: 'quiz', items: [{ ...itemFor('quiz'), ask: '继续' }] }]),
        make([blockFor('text'), { type: 'tabs', items: [] }])];
    for (const value of cases) reject(value);
    for (const key of ['__proto__', 'constructor', 'prototype']) {
        reject('{"title":"面板","blocks":[{"type":"text","text":"正文"}],"' + key + '":{}}');
    }
});

test('malformed JSON, duplicate keys and non-object schemas fail closed', () => {
    for (const text of ['', '{}', 'null', '[]', 'true', '42', '"x"', '{', '{"title":"a",}', '\ufeff' + json(make()),
        '{"title":"a","title":"b","blocks":[{"type":"text","text":"x"}]}',
        '{"title":"a","\\u0074itle":"b","blocks":[{"type":"text","text":"x"}]}',
        '{"title":"a","blocks":[{"type":"text","text":"x","text":"y"}]}',
        '{"title":"a","blocks":[{"type":"bars","items":[{"label":"x","value":0,"value":1}]}]}',
        json(make()) + ' trailing', '// comment\n' + json(make()), json(make()).replace('"blocks"', 'blocks')]) reject(text);
    for (const value of [null, undefined, 1, {}, [], true]) assert.equal(panels.parse(value), null);
    for (const badBlocks of [[], [blockFor('text'), blockFor('text'), blockFor('text'), blockFor('text')], {}, null, 'text']) reject(make(badBlocks));
    assert(panels.parse(json(make([blockFor('text'), blockFor('metrics'), blockFor('quiz')]))));
});

test('raw source limit uses Unicode code points and includes JSON whitespace', () => {
    const raw = json(make([{ type: 'text', text: '🌱' }]));
    const length = Array.from(raw).length;
    const exact = raw + ' '.repeat(6000 - length);
    assert.equal(Array.from(exact).length, 6000);
    assert(panels.parse(exact));
    reject(exact + ' ');
    assert(panels.parse(' \n\t' + raw + '\r\n'));
});

test('every string limit accepts the exact boundary and rejects one extra Unicode character', () => {
    const fields = [
        [make(), ['title'], 40], [make(), ['summary'], 120],
        [make(), ['blocks', 0, 'title'], 40], [make(), ['blocks', 0, 'text'], 600],
        [make([blockFor('metrics')]), ['blocks', 0, 'items', 0, 'label'], 40],
        [make([blockFor('metrics')]), ['blocks', 0, 'items', 0, 'value'], 80],
        [make([blockFor('metrics')]), ['blocks', 0, 'items', 0, 'unit'], 12],
        [make([blockFor('metrics')]), ['blocks', 0, 'items', 0, 'note'], 160],
        [make([blockFor('timeline')]), ['blocks', 0, 'items', 0, 'at'], 40],
        [make([blockFor('timeline')]), ['blocks', 0, 'items', 0, 'title'], 80],
        [make([blockFor('timeline')]), ['blocks', 0, 'items', 0, 'detail'], 160],
        [make([blockFor('checklist')]), ['blocks', 0, 'items', 0, 'text'], 120],
        [make([blockFor('checklist')]), ['blocks', 0, 'items', 0, 'detail'], 160],
        [make([blockFor('checklist')]), ['blocks', 0, 'items', 0, 'due'], 40],
        [make([blockFor('progress')]), ['blocks', 0, 'items', 0, 'label'], 40],
        [make([blockFor('progress')]), ['blocks', 0, 'items', 0, 'note'], 160],
        [make([blockFor('bars')]), ['blocks', 0, 'unit'], 12],
        [make([blockFor('bars')]), ['blocks', 0, 'items', 0, 'label'], 40],
        [make([blockFor('quiz')]), ['blocks', 0, 'items', 0, 'question'], 120],
        [make([blockFor('quiz')]), ['blocks', 0, 'items', 0, 'answer'], 300]
    ];
    for (const [base, keys, maximum] of fields) {
        const boundary = setAt(base, keys, '🌱'.repeat(maximum));
        assert(panels.parse(json(boundary)), keys.join('.') + ' exact boundary');
        reject(setAt(base, keys, '🌱'.repeat(maximum + 1)));
        reject(setAt(base, keys, 0));
        reject(setAt(base, keys, null));
    }
    for (const type of ['metrics', 'timeline', 'checklist', 'progress', 'bars', 'quiz']) {
        const base = make([blockFor(type)]);
        assert(panels.parse(json(setAt(base, ['blocks', 0, 'title'], '🌱'.repeat(40)))));
        reject(setAt(base, ['blocks', 0, 'title'], '🌱'.repeat(41)));
    }
});

test('required text cannot be blank, optional text can be empty, and normalization is deterministic', () => {
    const required = [[make(), ['title']], [make(), ['blocks', 0, 'text']],
        ...['metrics', 'timeline', 'checklist', 'progress', 'bars', 'quiz'].flatMap(type => {
            const keys = { metrics: ['label', 'value'], timeline: ['at', 'title'], checklist: ['text'], progress: ['label'], bars: ['label'], quiz: ['question', 'answer'] }[type];
            return keys.map(key => [make([blockFor(type)]), ['blocks', 0, 'items', 0, key]]);
        })];
    for (const [base, keys] of required) {
        reject(setAt(base, keys, '  '));
        const missing = setAt(base, keys, 'x');
        let parent = missing;
        for (const key of keys.slice(0, -1)) parent = parent[key];
        delete parent[keys[keys.length - 1]];
        reject(missing);
    }
    const empty = make([{ type: 'metrics', title: '', items: [{ label: '  保留  ', value: '  0  ', unit: '', note: '' }] }], { title: ' \ufeff面板\u3000', summary: '' });
    assert.deepEqual(panels.parse(json(empty)), { title: '面板', summary: '', blocks: [{ type: 'metrics', title: '', items: [{ label: '保留', value: '0', unit: '', note: '' }] }] });
    assert.equal(panels.plainCode(code(empty)), '面板\n保留：0');
});

test('control characters cannot hide in required or optional field edges', () => {
    for (const character of ['\u0000', '\t', '\n', '\r', '\u001f', '\u007f', '\u0085', '\u009f']) {
        reject(make(undefined, { title: character + '面板' }));
        reject(make(undefined, { summary: '说明' + character }));
        reject(make([{ type: 'text', text: '正文' + character }]));
        reject(make([{ type: 'metrics', items: [{ label: '值', value: '0', note: character }] }]));
    }
});

test('all item cardinalities are bounded and a single invalid item rejects its siblings', () => {
    for (const [type, maximum] of [['metrics', 3], ['timeline', 6], ['checklist', 6], ['progress', 6], ['bars', 6], ['quiz', 5]]) {
        const block = blockFor(type);
        block.items = Array.from({ length: maximum }, () => itemFor(type));
        assert(panels.parse(json(make([block]))), type + ' maximum');
        reject(make([{ ...block, items: [...block.items, itemFor(type)] }]));
        reject(make([{ ...block, items: [] }]));
        reject(make([{ ...block, items: {} }]));
        reject(make([{ ...block, items: [...block.items.slice(1), null] }]));
        reject(make([blockFor('text'), { ...block, items: [itemFor(type), {}] }]));
    }
});

test('numbers preserve zero, constrain ranges and reject coercion, overflow and rounding bypasses', () => {
    function numeric(type, token) {
        return '{"title":"数值","blocks":[{"type":"' + type + '","items":[{"label":"原值","value":' + token + '}]}]}';
    }
    for (const type of ['progress', 'bars']) {
        for (const token of ['0', '-0', '0.5', '1e-7', '1e-6', '1', '100']) assert(panels.parse(numeric(type, token)), type + ': ' + token);
        for (const token of ['-1', '-1e-400', '1e-400', '1e309', 'NaN', 'Infinity', '-Infinity', '"50"', 'null', 'true', '[]', '{}', '01', '+1', '.5', '1.']) reject(numeric(type, token));
    }
    assert(panels.parse(numeric('bars', '1000000000')));
    assert(panels.parse(numeric('bars', '100.000000000000000001')));
    for (const token of ['100.000001', '100.000000000000000001', '1.00000000000000000001e2', '101', '1000000000']) reject(numeric('progress', token));
    for (const token of ['1000000000.000001', '1000000000.00000000001', '1.00000000000000000001e9', '1000000001']) reject(numeric('bars', token));
    assert.equal(panels.parse(numeric('progress', '-0')).blocks[0].items[0].value, 0);
    assert.equal(panels.plainCode(code(numeric('progress', '1e-7'))), '数值\n原值：1e-7%');
});

test('bar geometry uses a safe max while labels retain unscaled values and units', () => {
    const base = make([{ type: 'bars', unit: '万元', items: [{ label: '零值', value: 0 }, { label: '候选', value: 0.25 }, { label: '上限', value: 1000000000 }] }]);
    const html = panels.renderCode(code(base));
    assert(html.includes('value="0" max="1000000000"'));
    assert(html.includes('value="0.25" max="1000000000"'));
    assert(html.includes('1000000000 万元'));
    assert.equal(panels.plainCode(code(base)), '本轮阅读面板\n候选结果；不自动更新。\n零值：0 万元\n候选：0.25 万元\n上限：1000000000 万元');
    const zeros = panels.renderCode(code(make([{ type: 'bars', items: [{ label: '零值', value: 0 }] }])));
    assert(zeros.includes('value="0" max="1"'));
});

const failed = outcomes.filter(outcome => !outcome.passed);
const summary = { executedAt: new Date().toISOString(), suite: 'conversation_panels', total: outcomes.length,
    passed: outcomes.length - failed.length, failed: failed.length, outcomes };
if (process.argv.includes('--record')) {
    fs.writeFileSync(path.join(__dirname, 'conversation_panels_test_result.json'), JSON.stringify(summary, null, 2) + '\n');
}
console.log(JSON.stringify(summary));
if (failed.length) process.exitCode = 1;
