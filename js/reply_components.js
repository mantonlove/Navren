(function (global) {
  'use strict';

  const SCHEMA = 'navren.reply-presentation.v1';
  const TYPES = new Set(['fact_summary', 'transaction_list', 'category_bars', 'funding_breakdown', 'account_balances', 'period_series']);
  const MAX_ROWS = 80;
  const ROW_TEXT_FIELDS = ['date', 'occurred_at', 'category', 'account_id', 'account_name', 'record_id', 'kind', 'counterparty', 'description', 'direction', 'role', 'unit', 'note'];
  const DETAIL_LABELS = {
    date: '日期', occurred_at: '交易时间', category: '分类', account_id: '账户标识', account_name: '所属账户',
    record_id: '流水编号', kind: '交易类型', counterparty: '交易对方', description: '说明', direction: '方向', unit: '单位', note: '备注'
  };
  const KIND_NAMES = { expense: '支出', income: '收入', refund: '退款', transfer: '转账', investment_purchase: '投资买入', investment_redemption: '投资赎回', loan_repayment: '还款' };
  const activeCards = new Map();
  const INCOME_UI_PREFIX = 'navren-income-ui:v1:';
  const incomeUI = new Map();
  const LOCAL_TEXT = {
    '收入来源': 'Income sources', '清晰账本': 'Ledger', '图形概览': 'Overview',
    '已确认收入合计': 'Confirmed income', '统计说明': 'Calculation notes', '查看精确金额': 'Exact amount', '占比最高': 'Largest share',
    '分类口径': 'Category basis', '支出口径': 'Spending basis', '数据覆盖起始': 'Coverage starts', '数据覆盖截止': 'Coverage ends',
    '已记录收入分类': 'Recorded income categories', '已实现投资损益': 'Realized investment gains and losses',
    '个人商铺租金收入': 'Shop rental income', '个人住宅租金收入': 'Residential rental income',
    '商铺租金': 'Shop rent', '住宅租金': 'Residential rent', '存款利息': 'Deposit interest',
    '同主体同期间已确认收入科目净贷方，加运行收入流水；含已实现投资损益，不含投资本金及未实现估值。': 'Confirmed net income for the same owner and period, including posted income and realized investment gains and losses. Investment principal and unrealized valuations are excluded.',
    '负数表示该收入科目的净损失或冲减。': 'A negative amount is a net loss or adjustment in this income category.',
    '统计限于已记录范围。': 'Statistics cover the recorded range only.',
    '这里只展示本次选中的收入分类。': 'Only the income categories selected in this response are shown.',
    '本期收入合计为零，不计算占比。': 'Total income is zero; percentages are not calculated.',
    '收入按净额列示，不计算占比。': 'Income is shown as signed net amounts; percentages are not calculated.',
    '收入分类资料不完整，暂不计算占比。': 'Income category coverage is incomplete; percentages are unavailable.',
    '收入分类币种不同，不计算占比。': 'Income categories use different currencies; percentages are not calculated.',
    '分类合计与收入总额不一致，暂不计算占比。': 'The category sum does not match total income; percentages are unavailable.',
    '收入统计口径不满足占比计算条件。': 'This income structure does not support percentage calculations.',
    '万元': '×10,000 CNY', '元': 'CNY',
    '已核验资金项': 'Verified funding items', '其他已核验项': 'Other verified items', '已核验附项': 'Additional verified items',
    '银行账户记录': 'Bank account records', '本地导入账单': 'Locally imported statements', '账户数据': 'Account data',
    '本地导入 · 只读': 'Local import · read only', '历史快照': 'Historical snapshot',
    '历史快照 · 本地导入': 'Historical snapshot · local import', '资料来源': 'Source', '资料日期': 'Source date',
    '查询期间': 'Query period', '账户标识': 'Account reference', '所属账户': 'Account', '流水编号': 'Transaction reference',
    '记录范围': 'Recorded range', '已覆盖月份': 'Months with records',
    '交易时间': 'Transaction time', '交易类型': 'Transaction type', '查看该分类明细': 'View category transactions',
    '该筛选期间没有交易记录。': 'No transactions match this period and filter.',
    '本次未提供逐笔明细；筛选结果仍有交易记录。': 'This response contains no itemized details; matching transactions exist.',
    '当前页记录，尚有未展示记录': 'This page is a partial selection; more records exist.',
    '先展示前3条已核验明细；其余记录可展开。': 'The first 3 verified transactions are shown; expand for the remaining records.',
    '先展示前3类；其余已核验分类可展开。': 'The first 3 categories are shown; expand for the remaining verified categories.',
    '仅展示已核验的资金项；资料不足以说明完整扣减关系。': 'Only verified funding items are shown. The complete deduction breakdown is unavailable.',
    '现金消费分类': 'Cash spending categories', '收支记录': 'Income and spending', '年度收支记录': 'Annual income and spending',
    '账户资金安排': 'Account funding', '资金情景测算': 'Funding scenario', '账户与已结算现金': 'Accounts and settled cash', '已核验记录': 'Verified records',
    '已结算现金': 'Settled cash', '应急保留': 'Emergency reserve', '30天已知到期款': 'Known obligations within 30 days',
    '生效资金预留': 'Active fund reservations', '额外规划留款': 'Additional planning reserve', '可安排资金': 'Available funding', '情景可安排资金': 'Scenario available funding',
    '笔数与合计统计全部筛选记录；下方仅展示本次已核验明细。': 'Count and total cover all matching records; only the verified details from this response are shown below.',
    '分类仅含现金消费；账单收支结余不等于账户余额。': 'Categories include cash spending only. Statement surplus is not the account balance.',
    '账单收支结余不等于账户余额。': 'Statement surplus is not the account balance.',
    '已扣保留资金、预留和30天已知到期款；未预支未来收入或估算其他生活开支。': 'Reserves, reservations and known obligations within 30 days are deducted. Future income and other living costs are not assumed.',
    '仅列本次已核验资金项，不能据此还原完整扣减过程。': 'Only verified funding items from this response are listed. They cannot reconstruct the complete deduction breakdown.',
    '已结算现金、持仓本金与待到账赎回分别列示；信用额度不计入现金。': 'Settled cash, holding principal and pending redemptions are shown separately. Credit limits are not counted as cash.'
  };

  function localText(text) {
    if (global.NavrenI18n && global.NavrenI18n.language === 'en') {
      if (LOCAL_TEXT[text]) return LOCAL_TEXT[text];
      let match = /^共 (\d+) 笔(?: · 当前页 (\d+) 笔，尚有未展示记录)?$/.exec(text);
      if (match) return match[2] ? match[1] + ' transactions · ' + match[2] + ' shown; more records exist' : match[1] + ' transactions';
      match = /^共 (\d+) 笔 · 本次已核验 (\d+) 笔(，尚有分页记录)?$/.exec(text);
      if (match) return match[1] + ' transactions · ' + match[2] + ' verified here' + (match[3] ? '; more pages exist' : '');
      match = /^展开其余 (\d+) (类|条)已核验(分类|记录)$/.exec(text);
      if (match) return 'Show ' + match[1] + ' remaining verified ' + (match[2] === '类' ? 'categories' : 'transactions');
      match = /^(−?[\d,]+\.\d{2}) 元$/.exec(text);
      if (match) return 'CNY ' + match[1];
      match = /^约 ([\d,]+\.\d{2})$/.exec(text);
      if (match) return '≈ ' + match[1];
      const incomeBasis = '同主体同期间已确认收入科目净贷方，加运行收入流水；含已实现投资损益，不含投资本金及未实现估值。';
      if (text.startsWith(incomeBasis + '账单收入不等于账户余额。') || text.startsWith('已记录收入分类账单收入不等于账户余额。')) {
        const basis = text.startsWith(incomeBasis) ? incomeBasis : '已记录收入分类';
        let suffix = text.slice(basis.length);
        const notes = [];
        for (const sentence of ['账单收入不等于账户余额。', '负数表示该收入科目的净损失或冲减。', '统计限于已记录范围。', '这里只展示本次选中的收入分类。']) {
          if (suffix.startsWith(sentence)) { notes.push(LOCAL_TEXT[sentence]); suffix = suffix.slice(sentence.length); }
        }
        if (!suffix) return LOCAL_TEXT[basis] + ' ' + notes.join(' ');
      }
      if (/\d{4}年\d{1,2}月/.test(text)) return text.replace(/(\d{4})年(\d{1,2})月(?:(\d{1,2})日)?/g, function (_, year, month, day) {
        return new Intl.DateTimeFormat('en', { year: 'numeric', month: 'short', ...(day ? { day: 'numeric' } : {}), timeZone: 'UTC' })
          .format(new Date(Date.UTC(Number(year), Number(month) - 1, day ? Number(day) : 1)));
      });
      return global.NavrenI18n.t(text);
    }
    return text;
  }

  function setText(node, text) {
    const raw = String(text);
    node.dataset.nrcText = raw;
    node.setAttribute('data-i18n-skip', '');
    const localized = localText(raw);
    if (global.NavrenI18n && typeof global.NavrenI18n.setText === 'function') global.NavrenI18n.setText(node, localized);
    else node.textContent = localized;
  }

  function string(value, max) {
    return typeof value === 'string' && value.trim() && value.length <= (max || 2000) ? value.trim() : null;
  }

  function plain(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value) &&
      (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
  }

  // Metadata is cloned as inert data. It never supplies an amount or an executable action.
  function metadata(value, depth) {
    if (typeof value === 'string') return value.length <= 2000 ? value : null;
    if (typeof value === 'boolean') return value;
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (value === null) return null;
    if (!plain(value) || depth > 3) return null;
    const out = {};
    for (const key of Object.keys(value).slice(0, 24)) {
      if (['__proto__', 'constructor', 'prototype'].includes(key) || key.length > 80) continue;
      const child = metadata(value[key], depth + 1);
      if (child !== null) out[key] = child;
    }
    return Object.keys(out).length ? out : null;
  }

  function source(value) {
    if (typeof value === 'string') return string(value, 2000);
    if (!plain(value)) return null;
    const out = metadata(value, 0);
    return out && Object.values(out).some(item => typeof item === 'string' && item.trim()) ? out : null;
  }

  function scalar(value) {
    return typeof value === 'number' && Number.isFinite(value) || typeof value === 'string' && value.length <= 500;
  }

  function datum(value, factIds, blockCurrency) {
    if (!plain(value)) return null;
    const id = string(value.id, 300);
    const label = string(value.label, 300);
    const factId = string(value.fact_id, 300);
    if (!id || !label || !factId || !factIds.has(factId)) return null;
    const hasMoney = Object.prototype.hasOwnProperty.call(value, 'value_minor');
    const hasScalar = Object.prototype.hasOwnProperty.call(value, 'value_scalar');
    if (hasMoney === hasScalar) return null;
    const out = { id, label, fact_id: factId };
    if (hasMoney) {
      const currency = string(value.currency, 24) || blockCurrency;
      if (!Number.isSafeInteger(value.value_minor) || !currency) return null;
      out.value_minor = value.value_minor;
      out.currency = currency;
    } else {
      if (!scalar(value.value_scalar)) return null;
      out.value_scalar = value.value_scalar;
      const currency = string(value.currency, 24);
      if (currency) out.currency = currency;
    }
    for (const key of ROW_TEXT_FIELDS) {
      const text = string(value[key], 1000);
      if (text) out[key] = text;
    }
    if (value.equivalent_fact_ids !== undefined) {
      if (!Array.isArray(value.equivalent_fact_ids) || value.equivalent_fact_ids.length > 24 ||
          value.equivalent_fact_ids.some(id => !string(id, 300) || !factIds.has(id))) return null;
      out.equivalent_fact_ids = [...new Set(value.equivalent_fact_ids)];
    }
    for (const key of ['source', 'period']) {
      if (value[key] !== undefined) {
        const item = key === 'source' ? source(value[key]) : typeof value[key] === 'string' ? string(value[key], 500) : metadata(value[key], 0);
        if (!item) return null;
        out[key] = item;
      }
    }
    if (value.details !== undefined) {
      if (!Array.isArray(value.details) || value.details.length > 24) return null;
      out.details = [];
      for (const detail of value.details) {
        if (!plain(detail) || detail.details !== undefined) return null;
        const normal = datum(detail, factIds, out.currency || blockCurrency);
        if (!normal || normal.details) return null;
        out.details.push(normal);
      }
    }
    return out;
  }

  function block(value, factIds) {
    if (!plain(value) || !TYPES.has(value.type)) return null;
    const id = string(value.id, 300);
    const title = string(value.title, 300);
    const origin = source(value.source);
    if (!id || !title || !origin) return null;
    const out = { id, type: value.type, title, source: origin };
    if (value.statement_kind !== undefined) {
      if (value.statement_kind !== 'income') return null;
      out.statement_kind = value.statement_kind;
    }
    const currency = string(value.currency, 24);
    if (currency) out.currency = currency;
    if (value.period !== undefined && value.period !== null && value.period !== '') {
      const period = typeof value.period === 'string' ? string(value.period, 500) : metadata(value.period, 0);
      if (!period) return null;
      out.period = period;
    }
    for (const key of ['evidence_id', 'source_version', 'source_date', 'coverage_start', 'coverage_end', 'note', 'account_id', 'account_name', 'kind', 'category', 'category_basis', 'expense_basis']) {
      const text = string(value[key], key === 'note' ? 2000 : 500);
      if (text) out[key] = text;
      else if (key === 'source_version' && Number.isSafeInteger(value[key])) out[key] = value[key];
    }
    for (const key of ['rows', 'metrics']) {
      if (value[key] === undefined) continue;
      if (!Array.isArray(value[key]) || value[key].length > MAX_ROWS) return null;
      out[key] = [];
      const ids = new Set();
      for (const raw of value[key]) {
        const normal = datum(raw, factIds, currency);
        if (!normal || ids.has(normal.id)) return null;
        ids.add(normal.id);
        out[key].push(normal);
      }
    }
    if (value.total_count !== undefined) {
      if (!Number.isSafeInteger(value.total_count) || value.total_count < 0) return null;
      if (!(out.metrics || []).some(row => row.label === '笔数' && row.value_scalar === value.total_count)) return null;
      out.total_count = value.total_count;
    }
    if (value.has_more !== undefined) {
      if (typeof value.has_more !== 'boolean') return null;
      out.has_more = value.has_more;
    }
    if (value.offset !== undefined) {
      if (!Number.isSafeInteger(value.offset) || value.offset < 0) return null;
      out.offset = value.offset;
    }
    for (const key of ['rows_complete', 'scenario', 'breakdown_complete']) {
      if (value[key] !== undefined) {
        if (typeof value[key] !== 'boolean') return null;
        out[key] = value[key];
      }
    }
    for (const key of ['recorded_range', 'covered_months']) {
      if (value[key] === undefined || value[key] === null) continue;
      if (Array.isArray(value[key])) {
        if (value[key].length > 24 || value[key].some(item => typeof item !== 'string' && !Number.isSafeInteger(item))) return null;
        out[key] = value[key].map(item => typeof item === 'string' ? string(item, 500) : item);
        if (out[key].some(item => item === null)) return null;
      } else {
        const info = typeof value[key] === 'string' ? string(value[key], 1000) : metadata(value[key], 0);
        if (!info) return null;
        out[key] = info;
      }
    }
    out.complete = value.complete === true || value.is_complete === true || value.breakdown_complete === true;
    const coverage = string(value.coverage, 100);
    if (coverage) out.coverage = coverage;
    const data = [...(out.metrics || []), ...(out.rows || [])];
    if (out.type === 'fact_summary') {
      if (!data.length) return null;
    } else if (!data.length && !(out.type === 'transaction_list' && out.total_count !== undefined)) {
      return null;
    }
    if (out.type === 'category_bars' || out.type === 'period_series') {
      const chartRows = (out.rows || []).filter(row => out.type !== 'category_bars' || row.category);
      // Explicit income blocks can retain mixed or incomplete facts as a ledger; their chart has a stricter gate.
      if (!isIncomeBlock(out) && (!chartRows.length || !chartRows.every(row => typeof row.value_minor === 'number') || new Set(chartRows.map(row => row.currency)).size !== 1)) return null;
    }
    return out;
  }

  function validate(presentation, context) {
    const ctx = context || {};
    if (ctx.isFallback || ctx.answer_kind && ctx.answer_kind !== 'verified_facts' || !plain(presentation) || presentation.schema_version !== SCHEMA ||
        presentation.status !== 'verified' || presentation.environment !== 'simulation') return null;
    const workspaceId = string(presentation.workspace_id, 300);
    const responseId = string(presentation.response_id, 300);
    if (!workspaceId || !responseId || workspaceId !== ctx.workspace_id || responseId !== ctx.response_id) return null;
    if (!Array.isArray(presentation.fact_ids) || !presentation.fact_ids.length || presentation.fact_ids.length > 1000) return null;
    const factIds = presentation.fact_ids.map(item => string(item, 300));
    if (factIds.some(item => !item) || new Set(factIds).size !== factIds.length) return null;
    if (!Array.isArray(presentation.blocks) || !presentation.blocks.length || presentation.blocks.length > 12) return null;
    const out = {
      schema_version: SCHEMA, status: 'verified', workspace_id: workspaceId, response_id: responseId,
      environment: 'simulation', fact_ids: factIds, blocks: []
    };
    const allowed = new Set(factIds);
    const ids = new Set();
    for (const raw of presentation.blocks) {
      const normal = block(raw, allowed);
      if (!normal || ids.has(normal.id)) return null;
      ids.add(normal.id);
      out.blocks.push(normal);
    }
    const represented = new Set();
    function mark(row) {
      represented.add(row.fact_id);
      for (const id of row.equivalent_fact_ids || []) represented.add(id);
      for (const detail of row.details || []) mark(detail);
    }
    for (const item of out.blocks) for (const row of [...(item.metrics || []), ...(item.rows || [])]) mark(row);
    if (factIds.some(id => !represented.has(id))) return null;
    const hash = string(presentation.content_hash, 128);
    if (hash) out.content_hash = hash;
    for (const key of ['lead', 'text']) {
      if (presentation[key] === null) out[key] = null;
      else if (presentation[key] !== undefined) {
        const text = string(presentation[key], 12000);
        if (!text) return null;
        out[key] = text;
      }
    }
    return out;
  }

  function snapshot(presentation, context) {
    const normal = validate(presentation, context);
    return normal ? JSON.parse(JSON.stringify(normal)) : null;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) setText(node, text);
    return node;
  }

  function chevron() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 20 20');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'nrc-chevron');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'm7 4 6 6-6 6');
    svg.appendChild(path);
    return svg;
  }

  function money(minor, currency) {
    // Work from the integer's digits, avoiding floating point rounding even near MAX_SAFE_INTEGER.
    const sign = minor < 0 ? '−' : '';
    const digits = String(Math.abs(minor)).padStart(3, '0');
    const whole = digits.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    const suffix = currency === 'CNY' || currency === 'RMB' ? '元' : currency;
    return sign + whole + '.' + digits.slice(-2) + (suffix ? ' ' + suffix : '');
  }

  function valueText(row) {
    if (Object.prototype.hasOwnProperty.call(row, 'value_minor')) return money(row.value_minor, row.currency);
    return String(row.value_scalar) + (row.unit ? ' ' + row.unit : '');
  }

  function periodText(period) {
    if (typeof period === 'string') return period;
    if (!plain(period)) return '';
    const start = period.start || period.date_from || period.start_date;
    const end = period.end || period.date_to || period.end_date;
    if (typeof start === 'string' && typeof end === 'string') return start === end ? start : start + ' — ' + end;
    for (const key of ['label', 'month', 'period', 'date', 'year']) {
      if (typeof period[key] === 'string' || typeof period[key] === 'number') return String(period[key]);
    }
    return '';
  }

  function sourceText(origin) {
    if (typeof origin === 'string') return origin === 'simulation' ? '银行账户记录' : origin === 'imported_local_csv' || origin === 'imported' ? '本地导入账单' : origin;
    if (!plain(origin)) return '';
    for (const key of ['label', 'name', 'source_name', 'provider', 'dataset', 'source', 'type']) {
      if (typeof origin[key] === 'string' && origin[key].trim()) return sourceText(origin[key].trim());
    }
    const first = Object.values(origin).find(item => typeof item === 'string' && item.trim());
    return first || '';
  }

  function sourceDate(origin) {
    if (!plain(origin)) return '';
    for (const key of ['as_of', 'date', 'observed_at', 'reference_date', 'snapshot_date', 'retrieved_at']) {
      if (typeof origin[key] === 'string') return origin[key];
    }
    return '';
  }

  function appendPair(list, label, value) {
    if (value === undefined || value === null || value === '') return;
    list.append(element('dt', '', label), element('dd', '', value));
  }

  function detailsFor(row, item) {
    const list = element('dl', 'nrc-detail-fields');
    appendPair(list, '金额' , Object.prototype.hasOwnProperty.call(row, 'value_minor') ? valueText(row) : null);
    for (const key of Object.keys(DETAIL_LABELS)) {
      if (key === 'unit' && row.value_minor !== undefined) continue;
      appendPair(list, DETAIL_LABELS[key], key === 'kind' ? KIND_NAMES[row[key]] || row[key] : row[key]);
    }
    for (const detail of row.details || []) appendPair(list, detail.label, valueText(detail));
    if (!row.account_id && item.account_id) appendPair(list, '账户标识', item.account_id);
    const origin = row.source || item.source;
    appendPair(list, '资料来源', sourceText(origin));
    appendPair(list, '资料日期', sourceDate(origin) || item.source_date || (isIncomeBlock(item) ? '' : item.coverage_end));
    const period = isIncomeBlock(item) ? incomePeriod(row.period || item.period) : periodText(row.period || item.period);
    if (period && !row.date) appendPair(list, '查询期间', period);
    return list;
  }

  function expandableRow(row, item, context, options) {
    const opts = options || {};
    const details = element('details', 'nrc-row');
    details.dataset.factId = row.fact_id;
    const summary = element('summary', 'nrc-row-summary');
    const label = element('span', 'nrc-row-caption');
    label.appendChild(element('span', 'nrc-row-label', row.label));
    if (row.date || row.account_name) label.appendChild(element('span', 'nrc-row-subtitle', row.date || row.account_name));
    summary.append(label, element('span', 'nrc-row-value', valueText(row)), chevron());
    summary.addEventListener('click', function (event) {
      if (!interactionAllowed(context)) { event.preventDefault(); details.open = false; }
    });
    if (opts.geometry !== undefined) {
      const track = element('span', 'nrc-bar-track');
      const bar = element('span', 'nrc-bar-fill');
      bar.style.width = opts.geometry + '%';
      track.appendChild(bar);
      summary.appendChild(track);
    }
    details.append(summary, detailsFor(row, item));
    return details;
  }

  function dataRows(item) { return item.rows || []; }

  function remainder(label, context) {
    const fold = element('details', 'nrc-remainder');
    const toggle = element('summary', 'nrc-remainder-toggle');
    toggle.append(element('span', '', label), chevron());
    toggle.addEventListener('click', function (event) {
      if (!interactionAllowed(context)) { event.preventDefault(); fold.open = false; }
    });
    fold.appendChild(toggle);
    return fold;
  }

  function renderSummary(item, rows) {
    const grid = element('div', 'nrc-metrics');
    for (const row of rows || [...(item.metrics || []), ...(item.rows || [])]) {
      const metric = element('div', 'nrc-metric' + (row.role === 'primary' ? ' nrc-metric-primary' : ''));
      metric.append(element('span', 'nrc-metric-label', row.label), element('strong', 'nrc-metric-value', valueText(row)));
      grid.appendChild(metric);
    }
    return grid;
  }

  function renderList(item, context) {
    const list = element('div', 'nrc-rows');
    const rows = dataRows(item);
    if (!rows.length && item.type === 'transaction_list') {
      const message = item.total_count === 0 ? '该筛选期间没有交易记录。' : '本次未提供逐笔明细；筛选结果仍有交易记录。';
      list.appendChild(element('p', 'nrc-empty', message));
    }
    const compact = item.type === 'transaction_list' && rows.length > 3;
    const more = compact ? remainder('展开其余 ' + (rows.length - 3) + ' 条已核验记录', context) : null;
    for (let index = 0; index < rows.length; index++) {
      (compact && index >= 3 ? more : list).appendChild(expandableRow(rows[index], item, context));
    }
    if (more) {
      list.appendChild(element('p', 'nrc-compact-note', '先展示前3条已核验明细；其余记录可展开。'));
      list.appendChild(more);
    }
    if (item.total_count !== undefined) {
      const text = item.rows_complete !== true && (item.has_more || rows.length < item.total_count)
        ? '共 ' + item.total_count + ' 笔 · 本次已核验 ' + rows.length + ' 笔' + (item.has_more ? '，尚有分页记录' : '')
        : '共 ' + item.total_count + ' 笔';
      list.appendChild(element('p', 'nrc-count', text));
    } else if (item.has_more) {
      list.appendChild(element('p', 'nrc-count', '当前页记录，尚有未展示记录'));
    }
    return list;
  }

  function monthFor(item, row) {
    const candidate = periodText(row.period || item.period);
    const match = /^(\d{4})-(0[1-9]|1[0-2])(?:$|[^\d])/.exec(candidate);
    return match ? match[1] + '-' + match[2] : null;
  }

  function isCurrent(context) {
    if (context._revoked || context.historical || typeof context.isCurrent !== 'function') return false;
    try { return context.isCurrent() === true; } catch (_) { return false; }
  }
  function interactionAllowed(context) {
    if (context._revoked) return false;
    if (context.historical && typeof context.isCurrent !== 'function') return true;
    if (typeof context.isCurrent !== 'function') return context.historical === true;
    try { return context.isCurrent() === true; } catch (_) { return false; }
  }

  function renderCategories(item, context) {
    const list = element('div', 'nrc-rows nrc-category-rows');
    const rows = dataRows(item).filter(row => row.category);
    const max = Math.max.apply(null, rows.map(row => Math.abs(row.value_minor)));
    const more = rows.length > 3 ? remainder('展开其余 ' + (rows.length - 3) + ' 类已核验分类', context) : null;
    for (let index = 0; index < rows.length; index++) {
      const row = rows[index];
      const detail = expandableRow(row, item, context, { geometry: max ? Math.abs(row.value_minor) / max * 100 : 0 });
      const month = monthFor(item, row);
      const category = row.category || row.label;
      if (month && typeof context.onQuestion === 'function' && isCurrent(context)) {
        const action = element('button', 'nrc-detail-action', '查看该分类明细');
        action.type = 'button';
        action.addEventListener('click', function () {
          if (!isCurrent(context)) { action.disabled = true; return; }
          context.onQuestion('请列出 ' + month + ' 的“' + category + '”分类交易明细。');
        });
        detail.appendChild(action);
      }
      (more && index >= 3 ? more : list).appendChild(detail);
    }
    if (more) {
      list.appendChild(element('p', 'nrc-compact-note', '先展示前3类；其余已核验分类可展开。'));
      list.appendChild(more);
    }
    const extra = dataRows(item).filter(row => !row.category);
    if (extra.length) {
      list.appendChild(element('div', 'nrc-section-label', '已核验附项'));
      for (const row of extra) list.appendChild(expandableRow(row, item, context));
    }
    return list;
  }

  function renderFunding(item, context) {
    const list = element('div', 'nrc-rows nrc-funding-rows');
    for (const row of dataRows(item)) {
      const detail = expandableRow(row, item, context);
      if (row.role === 'result' || row.role === 'investable' || row.role === 'available') detail.classList.add('nrc-funding-result');
      list.appendChild(detail);
    }
    if (!item.complete && !item.note) list.appendChild(element('p', 'nrc-note', '仅展示已核验的资金项；资料不足以说明完整扣减关系。'));
    return list;
  }

  function renderSeries(item, context) {
    const rows = dataRows(item);
    const wrap = element('div', 'nrc-series');
    const values = rows.map(row => row.value_minor);
    const min = Math.min.apply(null, values);
    const max = Math.max.apply(null, values);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 600 110');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'nrc-series-chart');
    const points = values.map((value, index) => ({
      x: values.length === 1 ? 300 : 12 + index / (values.length - 1) * 576,
      y: max === min ? 55 : 96 - (value - min) / (max - min) * 82
    }));
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', points.map((point, index) => (index ? 'L' : 'M') + point.x + ' ' + point.y).join(' '));
    svg.appendChild(path);
    for (const point of points) {
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', String(point.x)); circle.setAttribute('cy', String(point.y)); circle.setAttribute('r', '3');
      svg.appendChild(circle);
    }
    wrap.appendChild(svg);
    const list = element('div', 'nrc-series-values nrc-rows');
    for (const row of rows) list.appendChild(expandableRow(row, item, context));
    wrap.appendChild(list);
    return wrap;
  }

  function incomeInfo(item) {
    const primaries = (item.metrics || []).filter(row => row.role === 'primary');
    const total = primaries.length === 1 && primaries[0].label === '已确认收入合计' && Number.isSafeInteger(primaries[0].value_minor) ? primaries[0] : null;
    const rows = (item.rows || []).filter(row => row.category);
    const comparable = !!total && rows.length > 0 && rows.every(row => Number.isSafeInteger(row.value_minor) && row.currency === total.currency);
    const unique = new Set(rows.map(row => row.fact_id)).size === rows.length && (!total || !rows.some(row => row.fact_id === total.fact_id));
    const sumMatches = comparable && rows.reduce((sum, row) => sum + BigInt(row.value_minor), 0n) === BigInt(total.value_minor);
    const complete = comparable && unique && sumMatches && item.rows_complete === true;
    const chart = complete && total.value_minor > 0 && rows.every(row => row.value_minor >= 0);
    return { total, rows, complete, chart, comparable, unique, sumMatches };
  }

  function isIncomeBlock(item) {
    if (!['category_bars', 'fact_summary'].includes(item.type)) return false;
    if (item.kind === 'income' || item.statement_kind === 'income') return true;
    // Older persisted projections dropped statement_kind. Require the exact income structure, never the title alone.
    return !item.kind && !item.statement_kind && /:income$/.test(item.id) && incomeInfo(item).complete;
  }

  function incomeChartNote(item, info) {
    if (info.total && info.total.value_minor === 0) return '本期收入合计为零，不计算占比。';
    if (info.total && info.total.value_minor < 0 || info.rows.some(row => row.value_minor < 0)) return '收入按净额列示，不计算占比。';
    if (item.rows_complete !== true || !info.rows.length) return '收入分类资料不完整，暂不计算占比。';
    if (new Set([...(info.total ? [info.total.currency] : []), ...info.rows.map(row => row.currency)].filter(Boolean)).size > 1) return '收入分类币种不同，不计算占比。';
    if (info.comparable && info.unique && !info.sumMatches) return '分类合计与收入总额不一致，暂不计算占比。';
    return '收入统计口径不满足占比计算条件。';
  }

  function incomePeriod(period) {
    return periodText(period).replace(/\b(\d{4})-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?\b/g, function (_, year, month, day) {
      return year + '年' + Number(month) + '月' + (day ? Number(day) + '日' : '');
    });
  }

  function incomeShare(minor, total, precision) {
    const scale = 10n ** BigInt(precision);
    const denominator = BigInt(total);
    const rounded = (BigInt(minor) * 100n * scale + denominator / 2n) / denominator;
    const digits = String(rounded).padStart(precision + 1, '0');
    return digits.slice(0, -precision) + '.' + digits.slice(-precision);
  }

  // These digests are UI namespaces, not authorization tokens. Storage contains no raw identities, facts or prose.
  function incomeToken(parts) {
    const text = JSON.stringify(parts);
    return [2166136261, 2246822507, 3266489909, 668265263].map(seed => {
      let hash = seed;
      for (let index = 0; index < text.length; index++) hash = Math.imul(hash ^ text.charCodeAt(index), 16777619);
      return (hash >>> 0).toString(16).padStart(8, '0');
    }).join('');
  }

  function incomeStateKey(item, context) {
    // Old snapshots can omit content_hash; a stale external hash must not reuse another content's UI state either.
    return INCOME_UI_PREFIX + incomeToken([context.workspace_id]) + ':' + incomeToken([context.response_id, item.id, context._contentHash || '', incomeToken([item])]);
  }

  function readIncomeState(key) {
    let stored = incomeUI.get(key);
    try { stored = JSON.parse(global.localStorage.getItem(key)) || stored; } catch (_) { /* Storage may be unavailable. */ }
    if (!plain(stored)) return { view: 'ledger', open: [] };
    return {
      view: stored.view === 'overview' ? 'overview' : 'ledger',
      open: Array.isArray(stored.open) ? stored.open.filter(id => typeof id === 'string' && /^[a-f0-9]{32}$/.test(id)).slice(0, MAX_ROWS * 2 + 2) : []
    };
  }

  function clearIncomeState() {
    incomeUI.clear();
    try {
      for (const key of Object.keys(global.localStorage)) if (key.startsWith(INCOME_UI_PREFIX)) global.localStorage.removeItem(key);
    } catch (_) { /* Revocation also clears the in-memory fallback. */ }
  }

  function renderIncome(item, context) {
    const info = incomeInfo(item);
    const card = element('section', 'navren-reply-component nrc-income ni-card');
    card.dataset.replyComponent = 'v1';
    card.dataset.blockId = item.id;
    card.dataset.historical = context.historical ? 'true' : 'false';
    const key = incomeStateKey(item, context);
    const state = readIncomeState(key);
    const folds = new Map();
    let view = state.view === 'overview' && info.chart ? 'overview' : 'ledger';
    function persist() {
      if (!interactionAllowed(context)) return;
      const saved = { view, open: [...folds].filter(([, node]) => node.open).map(([id]) => id) };
      incomeUI.set(key, saved);
      try { global.localStorage.setItem(key, JSON.stringify(saved)); } catch (_) { /* The current page still retains the choice. */ }
    }
    function bindFold(details, identity) {
      const id = incomeToken([identity]);
      folds.set(id, details);
      details.open = state.open.includes(id);
      details.querySelector('summary').addEventListener('click', function (event) {
        if (!interactionAllowed(context)) { event.preventDefault(); return; }
      });
      details.addEventListener('toggle', persist);
    }
    // Save the last click even if navigation precedes the queued native details toggle.
    context._incomeSavers = (context._incomeSavers || []).concat(persist);
    const top = element('div', 'ni-top');
    const heading = element('div', 'ni-heading');
    heading.appendChild(element('h3', 'nrc-title', item.title));
    const period = incomePeriod(item.period);
    if (period) heading.appendChild(element('span', 'ni-period', period));
    const switches = element('div', 'ni-view-switch');
    switches.setAttribute('role', 'group');
    const buttons = ['ledger', 'overview'].map(name => {
      const button = element('button', '', name === 'ledger' ? '清晰账本' : '图形概览');
      button.type = 'button';
      button.dataset.incomeView = name;
      button.disabled = name === 'overview' && !info.chart;
      switches.appendChild(button);
      return button;
    });
    top.append(heading, switches);
    card.appendChild(top);
    if (item.account_name) card.appendChild(element('p', 'nrc-account-name', item.account_name));
    const ledgerHero = element('div', 'ni-ledger-hero');
    const totalHero = element('div', 'ni-total');
    const amount = element('div', 'ni-amount');
    const number = element('span', 'ni-number');
    const unit = element('span', 'ni-unit');
    const exact = element('details', 'ni-exact');
    exact.append(element('summary', '', '查看精确金额'), element('p', '', info.total ? valueText(info.total) : ''));
    bindFold(exact, 'exact');
    amount.append(number, unit);
    if (info.total) totalHero.append(element('div', 'ni-eyebrow', info.total.label), amount, exact);
    ledgerHero.appendChild(totalHero);
    card.appendChild(ledgerHero);
    const ringLayout = element('div', 'ni-ring-layout');
    const distribution = element('div', 'ni-distribution');
    let ring;
    if (info.chart) {
      const stops = [];
      let cumulative = 0n;
      ring = element('div', 'ni-ring');
      ring.setAttribute('aria-hidden', 'true');
      const largest = info.rows.reduce((a, b) => a.value_minor >= b.value_minor ? a : b);
      const core = element('div', 'ni-ring-core');
      core.append(element('span', 'ni-ring-number', incomeShare(largest.value_minor, info.total.value_minor, 2) + '%'), element('span', 'ni-ring-label', '占比最高'));
      ring.appendChild(core);
      info.rows.forEach((row, index) => {
        const color = 'var(--ni-series-' + 'abcd'[index % 4] + ')';
        const start = incomeShare(Number(cumulative), info.total.value_minor, 6);
        cumulative += BigInt(row.value_minor);
        const end = incomeShare(Number(cumulative), info.total.value_minor, 6);
        stops.push(color + ' ' + start + '% ' + end + '%');
        const segment = element('span', 'ni-segment');
        segment.style.setProperty('--ni-color', color);
        segment.style.width = incomeShare(row.value_minor, info.total.value_minor, 6) + '%';
        distribution.appendChild(segment);
      });
      ring.style.background = 'conic-gradient(' + stops.join(', ') + ')';
      ringLayout.appendChild(ring);
      distribution.setAttribute('aria-hidden', 'true');
      card.append(ringLayout, distribution);
    } else {
      card.appendChild(element('p', 'nrc-note ni-chart-note', incomeChartNote(item, info)));
    }
    const list = element('div', 'ni-list');
    info.rows.forEach((row, index) => {
      const detail = element('details', 'ni-row');
      detail.dataset.factId = row.fact_id;
      const summary = element('summary', '');
      const label = element('span', 'ni-label');
      const dot = element('span', 'ni-dot');
      dot.style.setProperty('--ni-color', 'var(--ni-series-' + 'abcd'[index % 4] + ')');
      const arrow = chevron();
      arrow.classList.add('ni-arrow');
      label.append(dot, element('span', '', row.label));
      summary.append(label, element('span', 'ni-value', valueText(row)), element('span', 'ni-percent', info.chart ? incomeShare(row.value_minor, info.total.value_minor, 2) + '%' : '—'), arrow);
      const body = element('div', 'ni-row-body');
      body.appendChild(detailsFor(row, item));
      detail.append(summary, body);
      bindFold(detail, ['row', row.id]);
      list.appendChild(detail);
    });
    card.appendChild(list);
    const extras = [...(item.metrics || []).filter(row => row !== info.total), ...(item.rows || []).filter(row => !row.category)];
    if (extras.length) {
      const additional = element('div', 'nrc-rows ni-extras');
      additional.appendChild(element('div', 'nrc-section-label', '已核验附项'));
      for (const row of extras) {
        const detail = expandableRow(row, item, context);
        bindFold(detail, ['extra', row.id]);
        additional.appendChild(detail);
      }
      card.appendChild(additional);
    }
    const footer = element('div', 'ni-foot');
    const origin = element('span', 'ni-foot-source');
    const date = sourceDate(item.source) || item.source_date;
    origin.appendChild(element('span', 'nrc-origin', sourceText(item.source)));
    if (date) origin.appendChild(element('span', '', date));
    const imported = item.source === 'imported_local_csv' || item.source === 'imported';
    origin.appendChild(element('span', 'nrc-state', context.historical ? imported ? '历史快照 · 本地导入' : '历史快照' : imported ? '本地导入 · 只读' : '账户数据'));
    const methods = element('details', 'ni-methods');
    methods.appendChild(element('summary', '', '统计说明'));
    const fields = info.total ? detailsFor(info.total, item) : element('dl', 'nrc-detail-fields');
    for (const [field, label] of [['category_basis', '分类口径'], ['expense_basis', '支出口径'], ['coverage_start', '数据覆盖起始'], ['coverage_end', '数据覆盖截止']]) appendPair(fields, label, item[field]);
    if (item.recorded_range) appendPair(fields, '记录范围', Array.isArray(item.recorded_range) ? item.recorded_range.map(incomePeriod).join(' — ') : incomePeriod(item.recorded_range));
    if (item.covered_months) appendPair(fields, '已覆盖月份', Array.isArray(item.covered_months) ? item.covered_months.map(incomePeriod).join('、') : incomePeriod(item.covered_months));
    methods.appendChild(fields);
    if (item.note) methods.appendChild(element('p', 'nrc-note', item.note));
    bindFold(methods, 'methods');
    footer.append(origin, methods);
    card.appendChild(footer);
    function applyView() {
      card.dataset.incomeView = view;
      const overview = view === 'overview' && info.chart;
      ledgerHero.hidden = overview;
      ringLayout.hidden = !overview;
      distribution.hidden = !info.chart || overview;
      (overview ? ringLayout : ledgerHero).appendChild(totalHero);
      const compact = overview && info.total && ['CNY', 'RMB'].includes(info.total.currency) && info.total.value_minor >= 1000000;
      if (info.total) {
        setText(number, compact ? '约 ' + money(Number((BigInt(info.total.value_minor) + 5000n) / 10000n), '') : money(info.total.value_minor, ''));
        setText(unit, compact ? '万元' : ['CNY', 'RMB'].includes(info.total.currency) ? '元' : info.total.currency);
      }
      exact.hidden = !compact;
      buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.incomeView === view)));
    }
    buttons.forEach(button => button.addEventListener('click', function () {
      if (!interactionAllowed(context) || button.disabled) return;
      view = button.dataset.incomeView;
      applyView();
      persist();
    }));
    applyView();
    return card;
  }

  function renderBlock(item, context) {
    const ctx = context || {};
    if (isIncomeBlock(item)) return renderIncome(item, ctx);
    const card = element('section', 'navren-reply-component nrc-' + item.type);
    card.dataset.replyComponent = 'v1';
    card.dataset.blockId = item.id;
    card.dataset.historical = ctx.historical ? 'true' : 'false';
    const head = element('div', 'nrc-head');
    const title = item.type === 'funding_breakdown' && !item.complete ? '已核验资金项' : item.title;
    head.appendChild(element('h3', 'nrc-title', title));
    const period = periodText(item.period);
    if (period) head.appendChild(element('span', 'nrc-period', period));
    card.appendChild(head);
    if (item.recorded_range || item.covered_months && item.covered_months.length) {
      const coverage = element('dl', 'nrc-coverage');
      let range = '';
      if (Array.isArray(item.recorded_range)) range = item.recorded_range.join(' — ');
      else range = periodText(item.recorded_range) || (typeof item.recorded_range === 'string' ? item.recorded_range : '');
      if (range) appendPair(coverage, '记录范围', range);
      let months = '';
      if (Array.isArray(item.covered_months)) months = item.covered_months.join('、');
      else months = periodText(item.covered_months) || (typeof item.covered_months === 'string' ? item.covered_months : '');
      if (months) appendPair(coverage, '已覆盖月份', months);
      if (coverage.childElementCount) card.appendChild(coverage);
    }
    if (item.account_name && !title.includes(item.account_name)) card.appendChild(element('p', 'nrc-account-name', item.account_name));
    let metrics = item.metrics || [];
    // The selected count remains visible in the pagination line, so no duplicate metric is needed.
    if (item.type === 'transaction_list' && item.total_count !== undefined) {
      metrics = metrics.filter(row => !(row.label === '笔数' && row.value_scalar === item.total_count));
    }
    if (item.type !== 'fact_summary' && metrics.length) {
      const summary = renderSummary(item, metrics);
      if (item.type === 'funding_breakdown') summary.classList.add('nrc-funding-metrics');
      card.appendChild(summary);
    }
    let contents;
    switch (item.type) {
      case 'fact_summary': contents = renderSummary(item); break;
      case 'category_bars': contents = renderCategories(item, ctx); break;
      case 'funding_breakdown': contents = renderFunding(item, ctx); break;
      case 'period_series': contents = renderSeries(item, ctx); break;
      default: contents = renderList(item, ctx);
    }
    card.appendChild(contents);
    if (item.note) card.appendChild(element('p', 'nrc-note', item.note));
    const footer = element('div', 'nrc-source');
    const origin = sourceText(item.source);
    const date = sourceDate(item.source) || item.source_date || item.coverage_end || (/^\d{4}-\d{2}-\d{2}$/.test(period) ? period : '');
    footer.appendChild(element('span', 'nrc-origin', [origin, date].filter(Boolean).join(' · ')));
    const imported = item.source === 'imported_local_csv' || item.source === 'imported';
    footer.appendChild(element('span', 'nrc-state', ctx.historical ? imported ? '历史快照 · 本地导入' : '历史快照' : imported ? '本地导入 · 只读' : '账户数据'));
    card.appendChild(footer);
    return card;
  }

  function render(message, presentation, context) {
    const ctx = Object.assign({}, context || {}, { _revoked: false });
    const normal = validate(presentation, ctx);
    if (!normal || !message || typeof message.querySelector !== 'function') return false;
    ctx._contentHash = normal.content_hash || '';
    const bubble = message.classList && message.classList.contains('bubble') ? message : message.querySelector('.bubble') || message;
    const group = element('div', 'reply-components');
    group.dataset.replyPresentation = normal.response_id;
    group.appendChild(renderBlock(normal.blocks[0], ctx));
    if (normal.blocks.length > 1) {
      const other = element('details', 'nrc-other-blocks');
      const toggle = element('summary', 'nrc-other-toggle');
      toggle.append(element('span', '', '其他已核验项'), chevron());
      toggle.addEventListener('click', event => { if (!interactionAllowed(ctx)) { event.preventDefault(); other.open = false; } });
      other.appendChild(toggle);
      for (const item of normal.blocks.slice(1)) other.appendChild(renderBlock(item, ctx));
      group.appendChild(other);
    }
    for (const old of bubble.querySelectorAll('.reply-components')) {
      const oldContext = activeCards.get(old);
      if (oldContext) oldContext._revoked = true;
      activeCards.delete(old); old.remove();
    }
    for (const old of bubble.querySelectorAll('.navren-reply-component[data-reply-component="v1"]')) old.remove();
    const tools = bubble.querySelector(':scope > .message-tools');
    if (tools) bubble.insertBefore(group, tools); else bubble.appendChild(group);
    activeCards.set(group, ctx);
    if (global.NavrenI18n && typeof global.NavrenI18n.apply === 'function') global.NavrenI18n.apply(group);
    return true;
  }

  function revokeInteractions(event) {
    const leaving = event && event.type === 'pagehide';
    if (!leaving) clearIncomeState();
    for (const [card, context] of activeCards) {
      if (!card.isConnected) { activeCards.delete(card); continue; }
      if (leaving) for (const save of context._incomeSavers || []) save();
      // Queued native toggle events must not write closed states after revocation.
      context._revoked = true;
      for (const details of card.querySelectorAll('details[open]')) details.open = false;
      for (const button of card.querySelectorAll('button')) button.disabled = true;
      card.dataset.identityRevoked = 'true';
      // Historic amounts stay identified as snapshots; no old query can be sent from them.
      for (const state of card.querySelectorAll('.nrc-state')) {
        const imported = state.dataset.nrcText.includes('本地导入');
        setText(state, imported ? '历史快照 · 本地导入' : '历史快照');
      }
    }
  }

  for (const event of ['navren:privacy-reset', 'navren:identity-revoked', 'navren:account-switched', 'pagehide']) global.addEventListener(event, revokeInteractions);
  global.addEventListener('navren:language', function () {
    for (const [card] of activeCards) {
      if (!card.isConnected) { activeCards.delete(card); continue; }
      for (const node of card.querySelectorAll('[data-nrc-text]')) setText(node, node.dataset.nrcText);
      if (global.NavrenI18n && typeof global.NavrenI18n.apply === 'function') global.NavrenI18n.apply(card);
    }
  });

  global.NavrenReplyComponents = Object.freeze({ validate, render, snapshot });
})(window);
