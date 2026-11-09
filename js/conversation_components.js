/*
 * Navren 静态对话组件。只解析受限文本，不执行内容、不读取状态。
 * 浏览器：NavrenConversationComponents.renderCode({ lang, text })
 * Node：require('./conversation_components.js').renderCode({ lang, text })
 * 返回安全 HTML 字符串；未知或不完整的块返回 null，由调用方保留普通代码块。
 */
(function (root, factory) {
  'use strict';
  const panels = typeof module === 'object' && module.exports ? require('./conversation_panels.js') : root?.NavrenConversationPanels;
  const api = factory(panels);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else if (root) {
    root.NavrenConversationComponents = api;
  }
}(typeof window !== 'undefined' ? window : this, function (panels) {
  'use strict';

  // 所有长度按 Unicode 码点计数；越界整体拒绝，不截取正文。
  const LIMITS = Object.freeze({
    title: 40,
    total: 2400,
    rows: 6,
    factLabel: 32,
    factValue: 80,
    factDescription: 140,
    compareHeader: 40,
    compareCell: 120,
    stepName: 40,
    stepDescription: 160,
    note: 400
  });
  const LABELS = Object.freeze({
    facts: '要点',
    compare: '方案对比',
    steps: '步骤',
    note: '补充说明'
  });
  const COLUMN_CLASSES = Object.freeze({
    2: 'ncc-columns-2',
    3: 'ncc-columns-3',
    4: 'ncc-columns-4'
  });
  const CONTROL_CHARACTERS = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/;

  function lengthOf(value) {
    return Array.from(value).length;
  }

  function validText(value, maximum, allowEmpty) {
    return (allowEmpty || value.length > 0) && lengthOf(value) <= maximum;
  }

  function escapeText(value) {
    return value.replace(/[&<>"']/g, function (character) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character];
    });
  }

  function titleMarkup(title) {
    return title ? '<h3 class="ncc-title">' + escapeText(title) + '</h3>' : '';
  }

  function section(type, title, content, extraClass) {
    // 标签、类名与 aria-label 均来自本文件的固定字面量。
    return '<section class="ncc-component ncc-' + type + (extraClass ? ' ' + extraClass : '') +
      '" aria-label="' + LABELS[type] + '">' + titleMarkup(title) + content + '</section>';
  }

  function tableCells(line) {
    let row = line.trim();
    if (row.startsWith('|')) row = row.slice(1);
    if (row.endsWith('|')) row = row.slice(0, -1);
    return row.split('|').map(function (cell) { return cell.trim(); });
  }

  function parseFacts(lines, title) {
    if (lines.length > LIMITS.rows) return null;
    const rows = [];
    for (const line of lines) {
      const cells = line.split('|').map(function (cell) { return cell.trim(); });
      if (cells.length < 2 || cells.length > 3 ||
          !validText(cells[0], LIMITS.factLabel) ||
          !validText(cells[1], LIMITS.factValue) ||
          (cells.length === 3 && !validText(cells[2], LIMITS.factDescription, true))) return null;
      rows.push('<div class="ncc-fact"><dt class="ncc-label">' + escapeText(cells[0]) +
        '</dt><dd class="ncc-value"><span class="ncc-value-text">' + escapeText(cells[1]) + '</span>' +
        (cells[2] ? '<span class="ncc-description">' + escapeText(cells[2]) + '</span>' : '') + '</dd></div>');
    }
    return section('facts', title, '<dl class="ncc-facts-list">' + rows.join('') + '</dl>');
  }

  function parseCompare(lines, title) {
    // 首行为表头，第二行可为 Markdown 分隔行，后面必须有 1–6 个数据行。
    if (lines.length < 2 || lines.length > LIMITS.rows + 2) return null;
    const header = tableCells(lines[0]);
    const columns = header.length;
    if (columns < 2 || columns > 4 || header.some(function (cell) {
      return !validText(cell, LIMITS.compareHeader);
    })) return null;
    const rows = [];
    for (let index = 1; index < lines.length; index += 1) {
      const cells = tableCells(lines[index]);
      if (cells.length !== columns || cells.some(function (cell) {
        return !validText(cell, LIMITS.compareCell);
      })) return null;
      const separatorLike = cells.every(function (cell) { return /^[-:]+$/.test(cell); });
      if (separatorLike) {
        if (index !== 1 || !cells.every(function (cell) { return /^:?-{3,}:?$/.test(cell); })) return null;
        continue;
      }
      if (rows.length >= LIMITS.rows) return null;
      rows.push('<tr class="ncc-compare-row">' + cells.map(function (cell, cellIndex) {
        // 行标签使用 th，保持二维比较语义；不建立交互式 grid。
        const tag = cellIndex === 0 ? 'th' : 'td';
        const content = cellIndex === 0 ? escapeText(cell) : '<span class="ncc-mobile-label">' + escapeText(header[cellIndex]) + '</span><span class="ncc-cell-text">' + escapeText(cell) + '</span>';
        return '<' + tag + ' class="ncc-cell">' + content + '</' + tag + '>';
      }).join('') + '</tr>');
    }
    if (!rows.length) return null;
    const table = '<div class="ncc-table-wrap"><table class="ncc-table"><thead class="ncc-table-head">' +
      '<tr class="ncc-compare-row">' + header.map(function (cell) {
        return '<th class="ncc-cell">' + escapeText(cell) + '</th>';
      }).join('') + '</tr></thead><tbody class="ncc-table-body">' + rows.join('') + '</tbody></table></div>';
    return section('compare', title, table, COLUMN_CLASSES[columns]);
  }

  function parseSteps(lines, title) {
    if (lines.length > LIMITS.rows) return null;
    const rows = [];
    for (let index = 0; index < lines.length; index += 1) {
      const cells = lines[index].split('|').map(function (cell) { return cell.trim(); });
      if (cells.length !== 2 || !validText(cells[0], LIMITS.stepName) ||
          !validText(cells[1], LIMITS.stepDescription)) return null;
      rows.push('<li class="ncc-step"><span class="ncc-step-number">' + (index + 1) +
        '</span><div class="ncc-step-copy"><span class="ncc-step-name">' + escapeText(cells[0]) +
        '</span><p class="ncc-step-description">' + escapeText(cells[1]) + '</p></div></li>');
    }
    return section('steps', title, '<ol class="ncc-steps-list">' + rows.join('') + '</ol>');
  }

  function renderCode(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) return null;
    const keys = Object.keys(input);
    if (keys.length !== 2 || !keys.every(function (key) { return key === 'lang' || key === 'text'; }) ||
        typeof input.lang !== 'string' || typeof input.text !== 'string') return null;
    if (input.lang === 'navren-panel') return panels?.renderCode(input) ?? null;
    // 仅识别完整的围栏信息字符串；不把普通代码、嵌套围栏或额外参数当成组件。
    const match = /^navren-(facts|compare|steps|note)(?:[ \t]+([^\r\n]*))?$/.exec(input.lang);
    if (!match || CONTROL_CHARACTERS.test(input.text)) return null;
    const type = match[1];
    const title = (match[2] || '').trim();
    if (!validText(title, LIMITS.title, true) || CONTROL_CHARACTERS.test(title)) return null;
    const text = input.text.replace(/\r\n?/g, '\n').trim();
    if (!text || lengthOf(text) > LIMITS.total) return null;
    if (type === 'note') {
      if (!validText(text, LIMITS.note)) return null;
      return section('note', title, '<p class="ncc-note-text">' + escapeText(text) + '</p>');
    }
    const lines = text.split('\n');
    // 不跳过中间空行，否则会把坏块中的部分内容静默丢掉。
    if (lines.some(function (line) { return !line.trim(); })) return null;
    if (type === 'facts') return parseFacts(lines, title);
    if (type === 'compare') return parseCompare(lines, title);
    return parseSteps(lines, title);
  }

  function plainText(source) {
    if (typeof source !== 'string') return '';
    // 带换行保存每一行，未被识别的围栏必须逐字保留。
    const lines = source.match(/[^\n]*\n|[^\n]+$/g) || [];
    const output = [];
    for (let index = 0; index < lines.length; index += 1) {
      const openingLine = lines[index].replace(/\r?\n$/, '');
      const opening = /^ {0,3}(`{3,}|~{3,})([^\r\n]*)$/.exec(openingLine);
      if (!opening || (opening[1][0] === '`' && opening[2].includes('`'))) {
        output.push(lines[index]);
        continue;
      }
      const marker = opening[1][0];
      const closingPattern = new RegExp('^ {0,3}' + marker + '{' + opening[1].length + ',}[ \\t]*$');
      let closing = index + 1;
      while (closing < lines.length && !closingPattern.test(lines[closing].replace(/\r?\n$/, ''))) closing += 1;
      if (closing === lines.length) {
        output.push(lines.slice(index).join(''));
        break;
      }
      const lang = opening[2].trim();
      const body = lines.slice(index + 1, closing).join('');
      if (renderCode({ lang: lang, text: body }) !== null) {
        if (lang === 'navren-panel') {
          output.push(panels.plainCode({lang: lang, text: body}) + (body.endsWith('\n') ? '\n' : ''));
        } else {
          const title = lang.replace(/^navren-(?:facts|compare|steps|note)/, '').trim();
          if (title) output.push(title + (lines[index].endsWith('\r\n') ? '\r\n' : '\n'));
          output.push(body);
        }
      } else output.push(lines.slice(index, closing + 1).join(''));
      index = closing;
    }
    return output.join('');
  }

  return Object.freeze({ renderCode: renderCode, plainText: plainText });
}));
