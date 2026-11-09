/* Navren conversation panels: a bounded data vocabulary rendered as native HTML. */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.NavrenConversationPanels = factory();
}(typeof window !== 'undefined' ? window : globalThis, function () {
    'use strict';

    const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
    const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
    const controls = /[\u0000-\u001f\u007f-\u009f]/;
    const count = value => Array.from(value).length;

    function shape(value, required, optional) {
        return isObject(value) && required.every(key => own(value, key)) &&
            Object.keys(value).every(key => required.includes(key) || optional.includes(key));
    }

    function string(value, maximum, required) {
        if (typeof value !== 'string' || controls.test(value)) return null;
        const result = value.trim();
        if ((required && !result) || count(result) > maximum) return null;
        return result;
    }

    function optionalStrings(source, target, limits) {
        for (const key of Object.keys(limits)) {
            if (!own(source, key)) continue;
            const value = string(source[key], limits[key], false);
            if (value === null) return false;
            target[key] = value;
        }
        return true;
    }

    function requiredStrings(source, target, limits) {
        for (const key of Object.keys(limits)) {
            const value = string(source[key], limits[key], true);
            if (value === null) return false;
            target[key] = value;
        }
        return true;
    }

    function finite(value, maximum, token) {
        return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= maximum &&
            !(value === maximum && token && exceedsPowerOfTen(token, maximum === 100 ? 2 : 9));
    }

    // A boundary value must not enter the schema by floating-point rounding.
    function exceedsPowerOfTen(token, power) {
        const parts = token.toLowerCase().split('e');
        const mantissa = parts[0].replace(/^-/, '');
        const fraction = (mantissa.split('.')[1] || '').length;
        const digits = mantissa.replace('.', '').replace(/^0+/, '');
        const order = digits.length + Number(parts[1] || 0) - fraction - 1;
        if (order !== power) return order > power;
        return digits[0] !== '1' || /[1-9]/.test(digits.slice(1));
    }

    function safeNumberToken(token) {
        const value = Number(token);
        if (!Number.isFinite(value)) return false;
        const mantissa = token.toLowerCase().split('e')[0];
        if (value === 0 && /[1-9]/.test(mantissa)) return false;
        return true;
    }

    // JSON.parse establishes syntax. This second pass rejects duplicate keys at
    // every depth (including escaped aliases), and numeric overflow/underflow.
    function strictJSON(text) {
        const value = JSON.parse(text);
        const numbers = new WeakMap();
        let cursor = 0;
        function whitespace() { while (/[\x20\t\r\n]/.test(text[cursor] || '\0')) cursor++; }
        function quoted() {
            const start = cursor++;
            while (cursor < text.length) {
                if (text[cursor] === '\\') cursor += 2;
                else if (text[cursor++] === '"') return JSON.parse(text.slice(start, cursor));
            }
            throw new Error('Unclosed JSON string');
        }
        function visit(depth, parsed, parent, key) {
            if (depth > 16) throw new Error('Panel nesting exceeds the schema');
            whitespace();
            const start = text[cursor];
            if (start === '"') { quoted(); return; }
            if (start === '{') {
                const keys = new Set();
                cursor++; whitespace();
                if (text[cursor] === '}') { cursor++; return; }
                while (cursor < text.length) {
                    whitespace();
                    const key = quoted();
                    if (keys.has(key)) throw new Error('Duplicate JSON key');
                    keys.add(key);
                    whitespace(); cursor++; // colon, already checked by JSON.parse
                    visit(depth + 1, parsed[key], parsed, key); whitespace();
                    if (text[cursor++] === '}') return;
                }
            } else if (start === '[') {
                cursor++; whitespace();
                if (text[cursor] === ']') { cursor++; return; }
                let index = 0;
                while (cursor < text.length) {
                    visit(depth + 1, parsed[index], parsed, index); index++; whitespace();
                    if (text[cursor++] === ']') return;
                }
            } else {
                const offset = cursor;
                while (cursor < text.length && !/[,\]}\x20\t\r\n]/.test(text[cursor])) cursor++;
                const token = text.slice(offset, cursor);
                if (/^-?\d/.test(token)) {
                    if (!safeNumberToken(token)) throw new Error('Unsafe JSON number');
                    if (parent) {
                        if (!numbers.has(parent)) numbers.set(parent, new Map());
                        numbers.get(parent).set(key, token);
                    }
                }
                return;
            }
            throw new Error('Incomplete JSON value');
        }
        visit(0, value);
        return { value, numbers };
    }

    function item(source, kind, numbers) {
        let required, optional, strings, options;
        switch (kind) {
            case 'metrics': required = ['label', 'value']; optional = ['unit', 'note']; strings = { label: 40, value: 80 }; options = { unit: 12, note: 160 }; break;
            case 'timeline': required = ['at', 'title']; optional = ['detail']; strings = { at: 40, title: 80 }; options = { detail: 160 }; break;
            case 'checklist': required = ['text']; optional = ['detail', 'due']; strings = { text: 120 }; options = { detail: 160, due: 40 }; break;
            case 'progress': required = ['label', 'value']; optional = ['note']; strings = { label: 40 }; options = { note: 160 }; break;
            case 'bars': required = ['label', 'value']; optional = []; strings = { label: 40 }; options = {}; break;
            case 'quiz': required = ['question', 'answer']; optional = []; strings = { question: 120, answer: 300 }; options = {}; break;
            default: return null;
        }
        if (!shape(source, required, optional)) return null;
        const result = {};
        if (!requiredStrings(source, result, strings) || !optionalStrings(source, result, options)) return null;
        if (kind === 'progress' || kind === 'bars') {
            const token = numbers.has(source) ? numbers.get(source).get('value') : undefined;
            if (!finite(source.value, kind === 'progress' ? 100 : 1000000000, token)) return null;
            result.value = source.value === 0 ? 0 : source.value;
        }
        return result;
    }

    function block(source, numbers) {
        if (!isObject(source) || typeof source.type !== 'string') return null;
        const type = source.type;
        const result = { type };
        if (type === 'text') {
            if (!shape(source, ['type', 'text'], ['title']) ||
                !requiredStrings(source, result, { text: 600 }) ||
                !optionalStrings(source, result, { title: 40 })) return null;
            return result;
        }
        const maxima = { metrics: 3, timeline: 6, checklist: 6, progress: 6, bars: 6, quiz: 5 };
        if (!own(maxima, type) || !shape(source, ['type', 'items'], type === 'bars' ? ['title', 'unit'] : ['title'])) return null;
        if (!optionalStrings(source, result, type === 'bars' ? { title: 40, unit: 12 } : { title: 40 })) return null;
        if (!Array.isArray(source.items) || source.items.length < 1 || source.items.length > maxima[type]) return null;
        result.items = [];
        for (const sourceItem of source.items) {
            const parsed = item(sourceItem, type, numbers);
            if (parsed === null) return null;
            result.items.push(parsed);
        }
        return result;
    }

    function parse(text) {
        if (typeof text !== 'string' || count(text) > 6000) return null;
        try {
            const json = strictJSON(text);
            const source = json.value;
            if (!shape(source, ['title', 'blocks'], ['summary'])) return null;
            const result = {};
            if (!requiredStrings(source, result, { title: 40 }) || !optionalStrings(source, result, { summary: 120 })) return null;
            if (!Array.isArray(source.blocks) || source.blocks.length < 1 || source.blocks.length > 3) return null;
            result.blocks = [];
            for (const sourceBlock of source.blocks) {
                const parsed = block(sourceBlock, json.numbers);
                if (parsed === null) return null;
                result.blocks.push(parsed);
            }
            return result;
        } catch (_) {
            return null;
        }
    }

    function codePanel(code) {
        if (!isObject(code) || code.lang !== 'navren-panel') return null;
        return parse(code.text);
    }

    function escape(value) {
        return String(value).replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
    }

    function content(tag, className, value) {
        return '<' + tag + ' class="' + className + '">' + escape(value) + '</' + tag + '>';
    }

    function note(tag, className, value) { return value ? content(tag, className, value) : ''; }
    function unit(value) { return note('small', 'ncp-unit', value ? ' ' + value : ''); }
    function toggle(className, openText, closeText) {
        return '<span class="' + className + '">' + content('span', 'ncp-label-open', openText) + content('span', 'ncp-label-close', closeText) + '</span>';
    }

    function renderBlock(block) {
        let body = note('h3', 'ncp-block-title', block.title);
        switch (block.type) {
            case 'text': body += content('p', 'ncp-text', block.text); break;
            case 'metrics':
                body += '<div class="ncp-metrics-grid">' + block.items.map(item =>
                    '<div class="ncp-metric">' + content('span', 'ncp-label', item.label) +
                    '<strong class="ncp-value">' + escape(item.value) + unit(item.unit) + '</strong>' + note('p', 'ncp-note', item.note) + '</div>'
                ).join('') + '</div>'; break;
            case 'timeline':
                body += '<ul class="ncp-timeline-list">' + block.items.map(item =>
                    '<li class="ncp-timeline-item">' + content('span', 'ncp-at', item.at) + content('strong', 'ncp-item-title', item.title) + note('p', 'ncp-detail', item.detail) + '</li>'
                ).join('') + '</ul>'; break;
            case 'checklist':
                body += content('p', 'ncp-note', '勾选仅用于本条阅读核对，不会保存或执行事项。');
                body += '<ul class="ncp-checklist-list">' + block.items.map(item =>
                    '<li class="ncp-checklist-item"><label class="ncp-checklist-label"><input class="ncp-checkbox" type="checkbox">' +
                    '<span class="ncp-checklist-copy">' + content('strong', 'ncp-item-title', item.text) + note('small', 'ncp-due', item.due ? '截止：' + item.due : '') + note('span', 'ncp-detail', item.detail) + '</span></label></li>'
                ).join('') + '</ul>'; break;
            case 'progress':
            case 'bars': {
                const bars = block.type === 'bars';
                const max = bars ? Math.max(1, ...block.items.map(item => item.value)) : 100;
                body += '<ul class="ncp-measure-list">' + block.items.map(item => {
                    const displayed = String(item.value) + (bars ? (block.unit ? ' ' + block.unit : '') : '%');
                    return '<li class="ncp-measure-item"><div class="ncp-measure-heading">' + content('span', 'ncp-label', item.label) +
                        content('strong', 'ncp-value', displayed) + '</div><progress class="ncp-meter' + (bars ? ' ncp-bars-meter' : '') +
                        '" value="' + String(item.value) + '" max="' + String(max) + '" aria-label="' + (bars ? '相对数值' : '进度') + '">' + escape(displayed) + '</progress>' + note('p', 'ncp-note', item.note) + '</li>';
                }).join('') + '</ul>'; break;
            }
            case 'quiz':
                body += '<div class="ncp-quiz-list">' + block.items.map(item =>
                    '<details class="ncp-question"><summary class="ncp-question-summary">' + content('span', 'ncp-question-text', item.question) +
                    toggle('ncp-answer-toggle', '查看答案', '收起答案') + '</summary>' + content('p', 'ncp-answer', item.answer) + '</details>'
                ).join('') + '</div>'; break;
        }
        return '<div class="ncp-block ncp-' + block.type + '">' + body + '</div>';
    }

    function renderCode(code) {
        const panel = codePanel(code);
        if (panel === null) return null;
        return '<details class="ncp-panel"><summary class="ncp-summary"><span class="ncp-heading">' +
            content('span', 'ncp-title', panel.title) + note('span', 'ncp-description', panel.summary) + '</span>' +
            toggle('ncp-toggle', '展开', '收起') + '</summary><div class="ncp-body">' + panel.blocks.map(renderBlock).join('') + '</div></details>';
    }

    function plainCode(code) {
        const panel = codePanel(code);
        if (panel === null) return null;
        const lines = [panel.title];
        if (panel.summary) lines.push(panel.summary);
        for (const block of panel.blocks) {
            if (block.title) lines.push(block.title);
            if (block.type === 'text') { lines.push(block.text); continue; }
            for (const item of block.items) {
                switch (block.type) {
                    case 'metrics': lines.push(item.label + '：' + item.value + (item.unit ? ' ' + item.unit : '')); if (item.note) lines.push(item.note); break;
                    case 'timeline': lines.push(item.at + '：' + item.title); if (item.detail) lines.push(item.detail); break;
                    case 'checklist': lines.push(item.text); if (item.detail) lines.push(item.detail); if (item.due) lines.push('截止：' + item.due); break;
                    case 'progress': lines.push(item.label + '：' + String(item.value) + '%'); if (item.note) lines.push(item.note); break;
                    case 'bars': lines.push(item.label + '：' + String(item.value) + (block.unit ? ' ' + block.unit : '')); break;
                    case 'quiz': lines.push('问：' + item.question, '答：' + item.answer); break;
                }
            }
        }
        return lines.join('\n');
    }

    return Object.freeze({ parse, renderCode, plainCode });
}));
