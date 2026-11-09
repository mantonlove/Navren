/** 发现工作区。公开资料与银行业务使用不同身份，浏览不准备交易。 */
window.DiscoverWorkspace = (() => {
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const action = (text, fn, cls = '') => { const b = node('button', text, 'discover-action ' + cls); b.type = 'button'; b.onclick = fn; return b; };
  const B = () => window.BankClient;
  const accountReady=()=>Storage.accountReady?.()===true;
  const itemKey=value=>window.NavrenAccountData.itemKey(value);
  const accountIdentity=()=>{const i=B()?.identity?.();return i&&JSON.stringify([i.registered_user_id,i.workspace_id,i.privacy_revision,i.auth_subject]);};
  let accountEpoch=0,accountWasReady=accountReady();
  const accountCurrent=(epoch,identity)=>epoch===accountEpoch&&accountReady()&&identity===accountIdentity();
  let generation = 0, privacyEpoch = 0, catalogue = [], selected = [], session = null, catalogueFlight = null, weeklyHistory = [], coverFlight = null, serverPlans = [], plansFlight = null;
  let active = { view: 'home' }, comparisonMode = false, legacyVisible = true, invalidSelection = [], publicCatalogueAvailable = false;
  let moneyPool = null, moneyFlight = null;
  let researchPool = null, researchFlight = null;
  let catalogHead = null, headFlight = null, insuranceCatalog = null, officialFlight = null, officialTimer = null, pageActive = true;
  const officialInterval = 300000, officialPage = 'https://www.webank.com/inquiryCenter/query';
  const names = { bank_product: '账户产品', public_catalog: '微众官方公开目录', public_nav: '历史管理人资料（已移出产品研究）' };
  const key = p => p.source_scope + ':' + p.id;
  const has = value => value !== null && value !== undefined && value !== '';
  const value = (v, fallback = '未披露') => has(v) ? String(v) : fallback;
  const money = v => Number.isSafeInteger(v) ? B().money(v) : '未披露';
  const date = d => !d ? '日期未知' : /^\d{4}-\d{2}-\d{2}$/.test(d) ? d : new Date(d).toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai' });
  function risk(v) { if (Number.isInteger(v) && v >= 1 && v <= 5) return 'R' + v; return has(v) ? String(v) : '风险未披露'; }
  // 筛选只认披露中的明确 R1–R5 等级；完整官方风险文字继续显示。
  function riskCode(v) { const m = /^R([1-5])(?=$|\D)/.exec(String(v || '')); return m ? 'R' + m[1] : null; }
  function normalize(bank, published) {
    const result = [];
    for (const p of published?.products || []) {
      if (!p.code || p.source_scope === 'bank_product') continue;
      result.push({ id: p.code, source_scope: 'public_catalog', version: published.catalog_version || published.sha256 || p.version || null, name: p.name, raw: p, risk: risk(p.risk), manager: value(p.publish), term: isFundRaw(p) ? value(p.type, '公募基金') : '期限未披露', minimum: '起购未披露', fees: value(p.charge), liquidity: '申赎与到账规则未披露', returns: isFundRaw(p) ? '管理人历史涨跌幅按期间与截止日期单独核验' : '收益指标与期间未披露', as_of: p.source_date || published.source_date, source: '微众银行官方公开目录 · ' + value(p.source_date || published.source_date, '日期未知'), eligibility: value(p.source_date || published.source_date, '日期未知') + ' 快照销售状态：' + value(p.sale_status, '未披露') + '；当前可申购条件未知，仅供资料研究。', gaps: published.stale ? '目录已过期；不作为当前在售或交易依据。' : '目录公开不代表当前在售；关键交易条件尚未核验。', executable: false });
    }
    return [...new Map(result.map(p => [key(p), p])).values()];
  }
  function live(token, epoch) { const current = B().identity?.(); return token === generation && epoch === privacyEpoch && (!active.workspace_id || !session?.workspace_id || active.workspace_id === session.workspace_id) && (!current || !session || current.workspace_id === session.workspace_id && current.privacy_revision === session.privacy_revision); }
  const insuranceRows = () => catalogue.filter(p => p.source_scope === 'public_catalog' && p.raw.type === '保险');
  function insuranceModule() { if (!window.DiscoverInsurance) throw new Error('保险组件暂时无法载入，请刷新页面。'); return window.DiscoverInsurance; }
  function requireInsuranceCatalogue() { if (!publicCatalogueAvailable) throw new Error('保险官方资料暂时无法读取，请重新读取。'); return insuranceRows(); }
  function syncContext(head = catalogHead) {
    if (!head) return null;
    return { catalog_version: head.catalog_version, source_url: officialPage, source_date: head.source_date,
      catalog_sync: { ...(head.catalog_sync || {}), fetch_status: head.catalog_sync?.fetch_status || head.fetch_status,
        source_date: head.source_date, checked_at: head.catalog_sync?.checked_at || head.checked_at || null } };
  }
  function catalogFailure() {
    return { ...(catalogHead || {}), fetch_status: 'failed', catalog_sync: { ...(catalogHead?.catalog_sync || {}), fetch_status: 'failed' } };
  }
  function catalogVerified(head) {
    const sync = head?.catalog_sync, success = sync?.last_success_at || head?.last_success_at;
    const states = [head?.fetch_status, sync?.fetch_status || head?.fetch_status];
    return states.every(s => ['ok','unchanged'].includes(s)) || states.every(s => ['ok','unchanged','fetching'].includes(s)) && states.includes('fetching') && /^[a-f0-9]{64}$/.test(head.catalog_version || '') && isoDate(head.source_date) && typeof success === 'string' && Number.isFinite(Date.parse(success)) && Date.parse(success) <= Date.now();
  }
  async function readCatalogHead() {
    if (headFlight) return headFlight.promise;
    const epoch = privacyEpoch;
    const promise = (async () => {
      await B().ensure(); const identity = performanceIdentity();
      const head = await B().get('/public-products?limit=1&offset=0');
      if (epoch !== privacyEpoch || identity !== performanceIdentity() || headFlight?.promise !== promise) throw new Error('账户权限或读取轮次已变化，目录响应已丢弃。');
      const failedEmpty = ['failed','not_checked','fetching'].includes(head?.fetch_status) && head?.status === 'unavailable' && head?.total === 0;
      if (!head || !['ok', 'unchanged', 'failed','fetching','not_checked'].includes(head.fetch_status) || !failedEmpty && (!/^[a-f0-9]{64}$/.test(head.catalog_version || '') || !isoDate(head.source_date)) || head.offset !== 0 || head.limit !== 1 || !Array.isArray(head.products) || !Number.isSafeInteger(head.total) || head.total < 0 || head.total > 100000 || head.matched !== head.total || head.products.length !== Math.min(1, head.total) || head.has_more !== (head.total > 1)) throw new Error('官网目录快照无法核验。');
      if (catalogHead?.catalog_version !== head.catalog_version) { catalogue = []; publicCatalogueAvailable = false; researchPool = null; insuranceCatalog = null; cancelListReturns(true); }
      catalogHead = head; return head;
    })().finally(() => { if (headFlight?.promise === promise) headFlight = null; });
    headFlight = { promise }; return promise;
  }
  async function readInsuranceCatalog(head) {
    if (!head.catalog_version) return [];
    if (insuranceCatalog?.version === head.catalog_version) return insuranceCatalog.rows;
    const epoch = privacyEpoch, token = generation, identity = performanceIdentity(), rows = []; let offset = 0, matched = null;
    do {
      const page = await B().get('/public-products?q=' + encodeURIComponent('保险') + '&limit=500&offset=' + offset);
      if (epoch !== privacyEpoch || token !== generation || identity !== performanceIdentity() || catalogHead?.catalog_version !== head.catalog_version) throw new Error('旧账户或旧轮次目录已丢弃。');
      if (!page || page.catalog_version !== head.catalog_version || page.source_date !== head.source_date || page.offset !== offset || page.limit !== 500 || !Number.isSafeInteger(page.matched) || page.matched < 0 || page.matched > 1000 || matched !== null && page.matched !== matched || !Array.isArray(page.products) || page.products.length !== Math.min(500, page.matched - offset) || page.has_more !== (offset + page.products.length < page.matched)) throw new Error('保险目录版本或完整性无法核验。');
      matched = page.matched; rows.push(...page.products); offset += page.products.length;
    } while (offset < matched);
    const products = rows.filter(p => p.type === '保险');
    if (new Set(products.map(p => p.code)).size !== products.length || products.some(p => typeof p.code !== 'string' || !p.code || typeof p.name !== 'string' || !p.name)) throw new Error('保险产品身份无法核验。');
    const normalized = normalize([], { ...head, products }); insuranceCatalog = { version: head.catalog_version, rows: normalized }; return normalized;
  }
  function drawCatalogSync(target, head, note = '') {
    if (!target) return;
    let line = target.parentElement?.querySelector('.discover-catalog-sync');
    if (!line) { line = node('div', undefined, 'discover-catalog-sync'); target.before(line); }
    const meta = syncContext(head), checked = meta?.catalog_sync?.checked_at;
    const shown = typeof checked === 'string' && Number.isFinite(Date.parse(checked)) ? new Date(checked).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未取得';
    line.replaceChildren(node('span', '最近校对 ' + shown), evidenceLink('微众官网 ↗', officialPage));
    const all = viewLink('全部产品 ›', 'research', {}, 'discover-catalog-action');
    line.append(all);
    if (!['ok','unchanged'].includes(meta?.catalog_sync?.fetch_status) || note) line.append(node('span', note || (meta?.catalog_sync?.fetch_status === 'fetching' ? '正在校对官网' : head?.catalog_version ? '校对失败，沿用上次资料' : '官网资料暂未取得'), meta?.catalog_sync?.fetch_status === 'fetching' && !note ? 'discover-note' : 'discover-sync-warning'));
  }
  async function loadCatalogue(force = false) {
    const currentHead = await readCatalogHead();
    if (!force && catalogue.length) return catalogue;
    if (catalogueFlight) return catalogueFlight;
    const epoch = privacyEpoch;
    const promise = (async () => {
      const s = await B().ensure(); if (epoch !== privacyEpoch) return [];
      session = s; const identity = performanceIdentity();
      const current = () => epoch === privacyEpoch && identity === performanceIdentity() && currentHead.catalog_version === catalogHead?.catalog_version && catalogueFlight === promise;
      const pub = await B().get('/public-products?limit=500&offset=0');
      if (!current()) throw new Error('目录身份或读取轮次已变化，旧分页响应已丢弃。');
      // 分页始终要求同一目录内容版本，避免把中途修订拼接成一份目录。
      const publicRows = [], expected = pub.catalog_version || pub.sha256;
      {
        if (expected !== currentHead.catalog_version) throw new Error('官网目录刚刚更新，请重新读取。');
        const matched = pub.matched;
        if (!Number.isSafeInteger(matched) || matched < 0 || matched > 100000) throw new Error('产品目录数量无法核对，请重新读取。');
        const pageOffset = (page, offset) => {
          if (expected && (page.catalog_version || page.sha256) !== expected) throw new Error('产品目录刚刚修订，请重新读取。');
          if (!Array.isArray(page.products) || !Number.isSafeInteger(page.limit) || page.limit < 1 || page.limit > 500 || page.offset !== offset || page.matched !== matched) throw new Error('产品目录分页未推进或数量发生变化，请重新读取。');
          if (page.products.length !== Math.min(page.limit, matched - offset) || page.has_more !== (offset + page.products.length < matched)) throw new Error('产品目录数量未齐，请重新读取。');
          if (page.has_more && !expected) throw new Error('产品目录版本无法核对，请重新读取。');
          publicRows.push(...page.products);
          return page.has_more ? offset + page.limit : null;
        };
        let offset = pageOffset(pub, 0);
        while (offset != null) {
          const next = await B().get('/public-products?limit=500&offset=' + encodeURIComponent(offset));
          if (!current()) throw new Error('目录身份或读取轮次已变化，旧分页响应已丢弃。');
          offset = pageOffset(next, offset);
        }
        if (publicRows.length !== matched) throw new Error('产品目录数量未齐，请重新读取。');
      }
      const normalized = normalize([], { ...pub, products: publicRows });
      if (!current()) throw new Error('目录快照已变化，旧分页响应已丢弃。');
      catalogue = normalized;
      publicCatalogueAvailable = true;
      return catalogue;
    })().finally(() => { if (catalogueFlight === promise) catalogueFlight = null; });
    catalogueFlight = promise;
    return catalogueFlight;
  }
  const routeMetadata = p => ({ object_kind: 'product', object_id: p.id, source_scope: p.source_scope, object_version: p.version, as_of: p.as_of });
  function href(view, metadata = {}) {
    if(metadata.time_view==='historical_audit'){const exact=MarketUI.historySelection(metadata);return NavrenViews.url({page:'recommend',view,...exact});}
    const params = new URLSearchParams({ view });
    for (const k of ['object_kind', 'object_id', 'source_scope', 'object_version', 'period']) if (metadata[k] != null) params.set(k, String(metadata[k]));
    return 'recommend.html?' + params;
  }
  function open(view, metadata = {}, options = {}) { return window.NavrenViews?.open(view, metadata, options); }
  function viewLink(text, view, metadata = {}, cls = '') {
    const a = node('a', text, cls); a.href = href(view, metadata); a.onclick = e => { if (!e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) { e.preventDefault(); open(view, metadata); } }; return a;
  }
  function ref(p) { return { kind: 'product', id: p.id, source_scope: p.source_scope, version: p.version, label: p.name }; }
  function referenceProduct(p) { window.NavrenViews?.reference(ref(p)); document.querySelector('#navrenPageQuestion')?.focus(); }
  function field(label, kind = 'text', options = []) {
    const box = node('label', undefined, 'discover-field'), caption = node('span', label), input = node(kind === 'select' ? 'select' : 'input');
    input.setAttribute('aria-label', label); if (kind !== 'select') input.type = kind;
    else for (const [id, text] of options) { const option = node('option', text); option.value = id; input.append(option); }
    box.append(caption, input); return { box, input };
  }
  // 公开业绩按产品和目录内容版本缓存；页面发布还需通过路由、权限及节点存活检查。
  const performanceCache = new Map(), performanceFlights = new Map(), performanceQueue = [];
  let performanceReaders = 0;
  const performancePeriods = { '1m': '近1月涨跌幅', '3m': '近3月涨跌幅', '1y': '近一年涨跌幅' };
  const isFundRaw = p => /基金/.test(String(p?.type || '')) && p?.type !== '保险';
  const isPublicFund = p => p.source_scope === 'public_catalog' && isFundRaw(p.raw);
  const performanceIdentity = () => { const i = B().identity?.() || session; return JSON.stringify([i?.workspace_id ?? null, i?.account_id ?? null, i?.privacy_revision ?? null]); };
  const isoDate = d => typeof d === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(d) && Number.isFinite(Date.parse(d)) && new Date(d).toISOString().slice(0, 10) === d;
  const decimalValue = v => typeof v === 'string' && /^[+-]?\d+(?:\.\d+)?$/.test(v) && Number.isFinite(Number(v));
  const percent = v => { if (!decimalValue(v)) return '收益数据暂未获取'; const n = Number(v); return (n > 0 ? '+' : '') + n.toFixed(2) + '%'; };
  function performanceQueueRun() {
    while (performanceReaders < 3 && performanceQueue.length) {
      const task = performanceQueue.shift(); ++performanceReaders;
      Promise.resolve().then(task.run).then(task.resolve, task.reject).finally(() => { --performanceReaders; performanceQueueRun(); });
    }
  }
  function validatePerformance(data, p) {
    if (!data || data.product_code !== p.id || data.catalog_version !== p.version) throw new Error('业绩产品或目录版本不一致，请重新读取。');
    if (!['ok', 'unavailable', 'unsupported'].includes(data.status)) throw new Error('业绩数据状态无法核对。');
    if (data.status !== 'ok') return { ...data, metrics: [], nav: [] };
    let source; try { source = new URL(data.source_url); } catch { throw new Error('官方业绩来源无法核对。'); }
    if (source.protocol !== 'https:' || !['www.chinaamc.com', 'www.westleadfund.com', 'www.jsfund.cn'].includes(source.hostname) || source.username || source.password || typeof data.source_name !== 'string' || !data.source_name.trim() || !isoDate(data.as_of) || !Array.isArray(data.metrics) || !Array.isArray(data.nav)) throw new Error('官方业绩来源或日期缺失。');
    const seenPeriods = new Set(), seenDates = new Set();
    for (const m of data.metrics) {
      if (!m || !Object.hasOwn(performancePeriods, m.period) || seenPeriods.has(m.period) || !decimalValue(m.value_pct) || !isoDate(m.as_of)) throw new Error('收益指标、期间或日期无法核对。');
      seenPeriods.add(m.period);
    }
    for (const point of data.nav) {
      if (!point || !isoDate(point.date) || seenDates.has(point.date) || !decimalValue(point.unit_nav) || Number(point.unit_nav) <= 0) throw new Error('单位净值记录无法核对。');
      seenDates.add(point.date);
    }
    return { ...data, nav: data.nav.slice().sort((a, b) => a.date.localeCompare(b.date)) };
  }
  function readPerformance(p, force, guard) {
    const id = JSON.stringify([p.id, p.version]), cached = performanceCache.get(id);
    if (!force && cached && cached.until > Date.now()) return Promise.resolve(cached.data);
    if (performanceFlights.has(id)) return performanceFlights.get(id);
    const promise = new Promise((resolve, reject) => { performanceQueue.push({ resolve, reject, run: async () => {
      if (!guard()) throw new Error('当前页面已变化。');
      const data = validatePerformance(await B().get('/public-product-performance?code=' + encodeURIComponent(p.id) + '&catalog_version=' + encodeURIComponent(p.version)), p);
      if (data.status === 'ok') performanceCache.set(id, { data, until: Date.now() + 10 * 60 * 1000 });
      return data;
    } }); performanceQueueRun(); }).finally(() => performanceFlights.delete(id));
    performanceFlights.set(id, promise); return promise;
  }
  function validateMoneyPool(data) {
    const hosts = new Map([['华夏基金管理有限公司', 'www.chinaamc.com'], ['嘉实基金管理有限公司', 'www.jsfund.cn'], ['西部利得基金管理有限公司', 'www.westleadfund.com']]);
    if (!data || !['ok', 'partial', 'unavailable'].includes(data.status) || data.metric_type !== 'seven_day_annualized' || data.source_scope !== 'official_money_market' || data.executable !== false || !Array.isArray(data.products) || data.products.length > 900 || !Array.isArray(data.coverage) || !/^[a-f0-9]{64}$/.test(data.content_version) || (data.stale_after_days != null && (!Number.isInteger(data.stale_after_days) || data.stale_after_days < 0 || data.stale_after_days > 7))) throw new Error('货币研究池身份或范围无法核验。');
    const codes = new Set();
    for (const p of data.products) {
      let source; try { source = new URL(p.source_url); } catch { throw new Error('货币资料来源缺失。'); }
      if (!/^\d{6}$/.test(p.code) || codes.has(p.code) || typeof p.name !== 'string' || !p.name.startsWith(p.manager?.replace('基金管理有限公司', '')) || p.type !== '货币基金' || !hosts.has(p.manager) || source.hostname !== hosts.get(p.manager) || source.protocol !== 'https:' || source.username || source.password || p.source_scope !== 'official_money_market' || p.executable !== false || typeof p.stale !== 'boolean' || !/^[a-f0-9]{64}$/.test(p.content_version)) throw new Error('货币份额身份无法核验。');
      codes.add(p.code);
      if (p.metric && (p.metric.type !== 'seven_day_annualized' || !decimalValue(p.metric.value_pct) || !isoDate(p.metric.as_of) || p.metric.as_of !== p.as_of)) throw new Error('七日年化口径或日期无法核验。');
    }
    return data;
  }
  function currentMoneyPool(data) {
    const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date()), days = data.stale_after_days ?? 3;
    return { ...data, products: data.products.map(p => ({ ...p, stale: p.stale || !isoDate(p.as_of) || p.as_of > today || (Date.parse(today) - Date.parse(p.as_of)) / 86400000 > days })) };
  }
  async function readMoneyPool() {
    const epoch = privacyEpoch; await B().ensure();
    const identity = performanceIdentity();
    if (moneyPool?.identity === identity && moneyPool.until > Date.now()) return currentMoneyPool(moneyPool.data);
    if (moneyFlight?.identity === identity) return moneyFlight.promise;
    const promise = (async () => {
      const data = validateMoneyPool(await B().get('/public-money-products'));
      if (epoch !== privacyEpoch || identity !== performanceIdentity()) throw new Error('账户权限已变化，旧研究资料已丢弃。');
      moneyPool = { identity, until: Date.now() + (data.status === 'ok' ? 10 * 60 * 1000 : 60 * 1000), data }; return currentMoneyPool(data);
    })().finally(() => { if (moneyFlight?.promise === promise) moneyFlight = null; });
    moneyFlight = { identity, promise }; return promise;
  }
  function moneyProduct(p) {
    return { id: p.code, source_scope: 'official_money_market', version: p.content_version, name: p.name, raw: p,
      risk: risk(p.risk), manager: p.manager, term: '货币基金', as_of: p.as_of, executable: false };
  }
  function moneyPerformance(host, p, preview = true) {
    host.classList.add('discover-performance'); if (preview) host.classList.add('discover-performance-preview');
    const metricBox = node('div', undefined, 'discover-performance-metric'), labels = node('div', undefined, 'discover-performance-labels'), metric = p.raw.metric;
    metricBox.append(node('strong', metric ? Number(metric.value_pct).toFixed(3) + '%' : '收益暂未获取', 'discover-performance-value'));
    labels.append(node('span', '7日年化', 'discover-note'), node('small', metric ? '截至 ' + metric.as_of : '尚未核验', 'discover-note')); metricBox.append(labels); host.append(metricBox);
    const footer = node('div', undefined, 'discover-performance-footer'), source = evidenceLink('来源 ↗', p.raw.source_url); source.title = p.manager + '官方货币基金列表'; footer.append(source);
    if (preview) footer.append(viewLink('详情 ›', 'product', routeMetadata(p)));
    if (p.raw.stale) { const stale = node('small', '待更新', 'discover-performance-stale'); stale.title = '披露资料已过期，不参与首页排序。'; footer.append(stale); }
    host.append(footer);
  }
  async function moneyPreview(target, token, epoch) {
    target.replaceChildren(node('p', '正在读取七日年化…', 'discover-note'));
    try {
      const data = await readMoneyPool(); if (!live(token, epoch)) return;
      // 先核验完整返回池，再按同一种已披露年化指标排序，最后取三项。
      const rows = data.products.filter(p => p.metric && !p.stale).sort((a, b) => Number(b.metric.value_pct) - Number(a.metric.value_pct) || a.code.localeCompare(b.code));
      target.replaceChildren(); for (const p of rows.slice(0, 3)) target.append(preview(moneyProduct(p), token, epoch));
      if (rows.length < 3) target.append(node('p', rows.length ? '已核验可用 ' + rows.length + ' 项' : '暂无已核验的七日年化资料', 'discover-empty'));
      if (!rows.length) target.append(action('重试', () => { moneyPool = null; moneyPreview(target, token, epoch); }));
      const help = document.querySelector('.discover-research-help p');
      if (help) help.textContent = '按已接入官方货币研究池的七日年化从高到低展示3项。暂估、缺失与过期资料不参与排序。覆盖：' + data.coverage.map(c => c.name + (c.status === 'ok' ? '（' + c.count + '项）' : '（暂未获取）')).join('、') + '。';
    } catch (e) { if (live(token, epoch)) { target.replaceChildren(node('p', '七日年化暂未获取', 'discover-empty'), action('重试', () => moneyPreview(target, token, epoch))); } }
  }
  async function moneyDetail(context, token, epoch, root) {
    const data = await readMoneyPool(); if (!live(token, epoch)) return;
    const row = data.products.find(p => p.code === context.object_id);
    if (!row) { error(root, new Error('当前研究池未找到该货币份额，请返回重新读取。')); return; }
    const p = moneyProduct(row), box = node('article', undefined, 'discover-detail'), performance = node('section');
    box.append(node('h2', p.name)); moneyPerformance(performance, p, false); box.append(performance);
    const dl = node('dl', undefined, 'discover-specs');
    for (const [label, text] of [['份额代码', p.id], ['管理人', p.manager], ['风险', p.risk], ['产品类型', p.term], ['申购门槛', '未接入'], ['资料身份', '管理人官方公开研究资料 · 只读']]) dl.append(node('dt', label), node('dd', text));
    if (context.object_version && context.object_version !== p.version) box.append(node('p', '官方资料已更新，当前显示新披露。', 'discover-note'));
    const explanation = node('details'); explanation.append(node('summary', '七日年化说明'), node('p', '直接采用管理人官方披露的七日年化收益率；历史短期年化不代表预期或承诺收益。此研究池与微众销售目录分开，不据此推定当前渠道在售、申购门槛或适合本人。', 'discover-note'));
    box.append(dl, explanation); root.replaceChildren(action('‹ 返回', () => window.NavrenViews?.back()), box);
  }
  // 排名在后端核验完整目录与完整周收益池后生成，不以产品类型或风险缩小候选。
  function validateResearchPool(data, code = null) {
    const version = v => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v);
    const count = n => Number.isSafeInteger(n) && n >= 0 && n <= 100000;
    const rankUrl = 'https://fund.eastmoney.com/data/fundranking.html', coverage = data?.coverage;
    if (!data || !['ok', 'partial', 'unavailable'].includes(data.status) || data.metric_type !== 'one_week_return' || data.source_scope !== 'public_product_research' || data.executable !== false || typeof data.ranking_complete !== 'boolean' || !version(data.content_version) || data.catalog_version !== null && !version(data.catalog_version) || data.rank_source_url !== rankUrl || data.provider !== '天天基金／东方财富Choice公开数据' || !Array.isArray(data.products) || data.products.length > (code ? 1 : 3) || !coverage || !['candidate_count', 'source_count', 'matched_count', 'eligible_count'].every(k => count(coverage[k])) || !coverage.type_counts || typeof coverage.type_counts !== 'object' || Array.isArray(coverage.type_counts) || !Object.values(coverage.type_counts).every(count) || Object.values(coverage.type_counts).reduce((sum, n) => sum + n, 0) !== coverage.candidate_count || coverage.eligible_count > coverage.matched_count || coverage.matched_count > Math.min(coverage.candidate_count, coverage.source_count) || !Number.isInteger(data.stale_after_days) || data.stale_after_days < 0 || data.stale_after_days > 7) throw new Error('产品研究范围或完整排名无法核验。');
    const codes = new Set(), today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
    for (const p of data.products) {
      if (!p || !/^\d{6}$/.test(p.code) || codes.has(p.code) || code && p.code !== code || typeof p.name !== 'string' || !p.name.trim() || typeof p.type !== 'string' || !p.type.trim() || p.source_scope !== data.source_scope || p.executable !== false || !version(p.content_version) || p.catalog_version !== data.catalog_version || typeof p.stale !== 'boolean' || (p.source_url !== 'https://fund.eastmoney.com/' + p.code + '.html' && !(code && !p.metric && p.source_url === null)) || p.rank_source_url !== rankUrl) throw new Error('产品份额、来源或版本无法核验。');
      codes.add(p.code);
      if (p.metric && (p.metric.type !== 'one_week_return' || p.metric.label !== '近1周涨跌幅' || p.raw?.source_period !== '近1周' || !decimalValue(p.metric.value_pct) || !isoDate(p.metric.as_of) || p.metric.as_of !== p.as_of || p.as_of > today || Number(p.metric.value_pct) <= -100)) throw new Error('近1周收益口径或日期无法核验。');
      if (!code && (!p.metric || p.stale || (Date.parse(today) - Date.parse(p.as_of)) / 86400000 > data.stale_after_days)) throw new Error('未核验或过期的收益不能参与排名。');
    }
    if (!code && data.ranking_complete && (data.products.length !== Math.min(3, coverage.eligible_count) || data.products.some((p, i) => i > 0 && (Number(p.metric.value_pct) > Number(data.products[i - 1].metric.value_pct) || Number(p.metric.value_pct) === Number(data.products[i - 1].metric.value_pct) && p.code < data.products[i - 1].code)))) throw new Error('完整排名的数量或顺序无法核验。');
    if (!data.ranking_complete && data.products.length) throw new Error('不完整的收益资料不能显示榜单。');
    return data;
  }
  async function readResearchPool(code = null, head = null) {
    const epoch = privacyEpoch; await B().ensure(); const identity = performanceIdentity();
    head = head || await readCatalogHead();
    if (!code && researchFlight?.identity === identity && researchFlight.version === head.catalog_version) return researchFlight.promise;
    const promise = (async () => {
      const data = validateResearchPool(await B().get('/public-research-products' + (code ? '?code=' + encodeURIComponent(code) : '')), code);
      if (epoch !== privacyEpoch || identity !== performanceIdentity()) throw new Error('账户权限已变化，旧研究资料已丢弃。');
      if (data.ranking_complete && (!catalogVerified(head) || !catalogVerified(catalogHead) || data.catalog_version !== head.catalog_version || data.catalog_version !== catalogHead?.catalog_version)) throw new Error('收益候选与官网最新目录尚未对齐。');
      if (!code && researchFlight?.promise !== promise) throw new Error('研究读取轮次已变化，旧响应已丢弃。');
      if (!code && data.ranking_complete) researchPool = { identity, data };
      return data;
    })().finally(() => { if (researchFlight?.promise === promise) researchFlight = null; });
    if (!code) researchFlight = { identity, version: head.catalog_version, promise }; return promise;
  }
  const listReturnCache = new Map(), listReturnFlights = new Map(), listReturnQueue = [];
  let listReturnReaders = 0;
  const returnTaskCurrent = task => task.guards.some(guard => guard());
  function cancelListReturns(clear = false) {
    for (let i = listReturnQueue.length - 1; i >= 0; --i) {
      const task = listReturnQueue[i];
      if (clear || !returnTaskCurrent(task)) {
        listReturnQueue.splice(i, 1); if (listReturnFlights.get(task.id) === task) listReturnFlights.delete(task.id);
        task.reject(new Error('已离开这页产品，收益读取已取消。'));
      }
    }
    if (clear) { listReturnCache.clear(); listReturnFlights.clear(); }
  }
  function runListReturns() {
    cancelListReturns();
    while (listReturnReaders < 3 && listReturnQueue.length) {
      const task = listReturnQueue.shift(); ++listReturnReaders;
      Promise.resolve().then(task.run).then(task.resolve, task.reject).finally(() => { --listReturnReaders; runListReturns(); });
    }
  }
  function readListReturn(p, head, guard) {
    const identity = performanceIdentity(), epoch = privacyEpoch, id = JSON.stringify([identity, head.catalog_version, p.id]);
    const cached = listReturnCache.get(id);
    if (cached && cached.until > Date.now()) return Promise.resolve(cached.data);
    const existing = listReturnFlights.get(id);
    if (existing) { existing.guards.push(guard); return existing.promise; }
    const task = { id, guards: [guard] };
    task.promise = new Promise((resolve, reject) => {
      task.resolve = resolve; task.reject = reject;
      task.run = async () => {
        if (!returnTaskCurrent(task)) throw new Error('已离开这页产品。');
        const data = await readResearchPool(p.id, head);
        if (!returnTaskCurrent(task) || epoch !== privacyEpoch || identity !== performanceIdentity() || listReturnFlights.get(id) !== task || head.catalog_version !== catalogHead?.catalog_version) throw new Error('旧页面或旧账户收益已丢弃。');
        if (!data.ranking_complete || data.catalog_version !== head.catalog_version || data.catalog_source_date !== head.source_date) throw new Error('当前目录的收益资料尚未完成核验。');
        const row = data.products[0];
        if (row && (row.name !== p.name || String(p.raw.type || '').trim() && row.type !== String(p.raw.type).trim() || row.raw?.catalog_source_date !== head.source_date)) throw new Error('收益与产品份额身份不一致。');
        listReturnCache.set(id, { data, until: Date.now() + officialInterval }); return data;
      };
      listReturnQueue.push(task);
    }).finally(() => { if (listReturnFlights.get(id) === task) listReturnFlights.delete(id); });
    listReturnFlights.set(id, task); runListReturns(); return task.promise;
  }
  function listReturnPanel(host, p, head, guard) {
    const paint = (text, metric = null, asOf = null) => {
      if (!guard()) return;
      host.replaceChildren(node('strong', text, metric ? 'discover-research-return-value discover-research-value' : 'discover-research-return-state'), node('span', '近1周涨跌幅', 'discover-note'), node('small', asOf ? '截至 ' + asOf : '截止日期未披露', 'discover-note'));
    };
    if (!/^\d{6}$/.test(p.id)) { paint('未披露近1周收益'); return; }
    paint('正在读取收益…');
    return readListReturn(p, head, guard).then(data => {
      const row = data.products[0], metric = row?.metric;
      const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
      if (!metric) paint('未披露近1周收益', null, isoDate(row?.as_of) ? row.as_of : null);
      else if (row.stale || (Date.parse(today) - Date.parse(metric.as_of)) / 86400000 > data.stale_after_days) paint('收益资料已过期', null, metric.as_of);
      else paint(percent(metric.value_pct), metric, metric.as_of);
    }).catch(() => paint('收益暂未获取'));
  }
  function researchProduct(p) {
    return { id: p.code, source_scope: p.source_scope, version: p.content_version, name: p.name, raw: p, risk: risk(p.risk), manager: value(p.manager), term: p.type, as_of: p.as_of, executable: false };
  }
  function researchPerformance(host, p, previewMode = true) {
    host.classList.add('discover-performance'); if (previewMode) host.classList.add('discover-performance-preview');
    const metric = p.raw.metric, box = node('div', undefined, 'discover-performance-metric'), labels = node('div', undefined, 'discover-performance-labels');
    box.append(node('strong', metric ? percent(metric.value_pct) : '收益暂未获取', 'discover-performance-value discover-research-value'));
    labels.append(node('span', '近1周涨跌幅', 'discover-note'), node('small', metric ? '截至 ' + metric.as_of : '尚未核验', 'discover-note')); box.append(labels); host.append(box);
    const footer = node('div', undefined, 'discover-performance-footer');
    if (p.raw.source_url) { const source = evidenceLink('来源 ↗', p.raw.source_url); source.title = '天天基金／东方财富Choice公开数据'; footer.append(source); }
    if (previewMode) footer.append(viewLink('详情 ›', 'product', routeMetadata(p)));
    if (p.raw.stale) footer.append(node('small', '待更新', 'discover-performance-stale')); host.append(footer);
  }
  async function researchPreview(target, token, epoch, head) {
    const previous = !!target.querySelector('.discover-preview') && target.dataset.catalogVersion === head?.catalog_version;
    if (!previous) target.replaceChildren(node('p', '正在读取近期收益…', 'discover-note'));
    try {
      const data = await readResearchPool(null, head); if (!live(token, epoch) || document.visibilityState === 'hidden') return;
      if (!data.ranking_complete) throw new Error(data.failure_reason || '近期收益资料未完成核验。');
      const unchanged = target.dataset.researchVersion === data.content_version && previous;
      if (!unchanged) { target.replaceChildren(); for (const p of data.products) target.append(preview(researchProduct(p), token, epoch)); }
      target.dataset.researchVersion = data.content_version;
      target.dataset.catalogVersion = data.catalog_version;
      drawCatalogSync(target, head);
      if (data.products.length < 3) target.append(node('p', data.products.length ? '已核验可用 ' + data.products.length + ' 项' : '暂无已核验的近期收益资料', 'discover-empty'));
      if (!data.products.length) target.append(action('重试', () => { researchPool = null; researchPreview(target, token, epoch); }));
      const help = document.querySelector('.discover-research-help p');
      if (help) help.textContent = '全部已接入产品类型均为候选，按已核验的近1周涨跌幅排序展示3项；无同周期收益资料的产品暂不参与。来源：天天基金／东方财富Choice公开数据。候选 ' + data.coverage.candidate_count + ' 项，收益资料匹配 ' + data.coverage.matched_count + ' 项。';
    } catch (e) { if (live(token, epoch)) { console.warn('产品研究读取未完成：' + e.message); drawCatalogSync(target, head, previous ? '本次未完成核验，沿用上次收益资料' : '收益资料暂未取得'); if (!previous) target.replaceChildren(node('p', '近期收益暂未获取', 'discover-empty'), action('重试', () => syncOfficialHome(token, epoch))); } }
  }
  async function researchDetail(context, token, epoch, root) {
    if (!/^\d{6}$/.test(context.object_id || '')) throw new Error('产品份额代码无法核验。');
    const data = await readResearchPool(context.object_id); if (!live(token, epoch)) return;
    const row = data.products.find(p => p.code === context.object_id);
    if (!row) { error(root, new Error('当前研究池未找到该产品份额，请返回重新读取。')); return; }
    const p = researchProduct(row), box = node('article', undefined, 'discover-detail'), performance = node('section'), dl = node('dl', undefined, 'discover-specs');
    box.append(node('h2', p.name)); researchPerformance(performance, p, false); box.append(performance);
    for (const [label, text] of [['份额代码', p.id], ['管理人', p.manager], ['风险', p.risk], ['产品类型', p.term], ['申购门槛', '未接入'], ['收益来源', data.provider], ['目录披露日期', data.catalog_source_date || '未披露'], ['资料身份', '公开产品收益研究资料 · 只读']]) dl.append(node('dt', label), node('dd', text));
    if (context.object_version && context.object_version !== p.version) box.append(node('p', '产品资料已更新，当前显示新披露。', 'discover-note'));
    if (row.stale || !row.metric) box.append(node('p', '该份额暂无可用于当前排名的近期收益资料。', 'discover-note'));
    const explanation = node('details'); explanation.append(node('summary', '收益说明'), node('p', '采用数据源披露的近1周涨跌幅；该指标不等于七日年化，也不是预期收益。产品目录与收益资料分别核验，当前渠道在售、交易条件和适合本人情况仍需核对。', 'discover-note'));
    box.append(dl, explanation); root.replaceChildren(action('‹ 返回', () => window.NavrenViews?.back()), box);
  }
  function periodStart(end, period) {
    const d = new Date(end + 'T00:00:00Z'), day = d.getUTCDate(); d.setUTCDate(1);
    if (period === '1y') d.setUTCFullYear(d.getUTCFullYear() - 1); else d.setUTCMonth(d.getUTCMonth() - (period === '3m' ? 3 : 1));
    const last = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)).getUTCDate(); d.setUTCDate(Math.min(day, last)); return d.toISOString().slice(0, 10);
  }
  function navChart(data, period, compact, preview = false) {
    const end = data.metrics.find(m => m.period === period)?.as_of || data.as_of, start = periodStart(end, period), points = data.nav.filter(p => p.date >= start && p.date <= end);
    const chart = node('figure', undefined, 'discover-performance-chart' + (compact ? ' compact' : '') + (preview ? ' preview' : ''));
    if (points.length < 2) { chart.title = '单位净值历史不足，暂不展示走势。'; chart.append(node('figcaption', preview ? '走势资料不足' : chart.title, 'discover-note')); return chart; }
    const values = points.map(p => Number(p.unit_nav)), low = Math.min(...values), high = Math.max(...values), first = Date.parse(points[0].date), last = Date.parse(points[points.length - 1].date), width = 320, height = preview ? 64 : compact ? 76 : 150, pad = 8;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', '实际单位净值走势：' + points[0].date + ' 至 ' + points[points.length - 1].date + '，共 ' + points.length + ' 个披露日');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path'); path.setAttribute('d', points.map((p, i) => (i ? 'L' : 'M') + (pad + (Date.parse(p.date) - first) / (last - first) * (width - pad * 2)).toFixed(2) + ' ' + (high === low ? height / 2 : pad + (high - Number(p.unit_nav)) / (high - low) * (height - pad * 2)).toFixed(2)).join(' ')); svg.append(path);
    const coverage = '单位净值 · 实际覆盖 ' + points[0].date + ' 至 ' + points[points.length - 1].date;
    const incomplete = Date.parse(points[0].date) - Date.parse(start) > 7 * 86400000 || Date.parse(end) - last > 7 * 86400000;
    const coverageText = coverage + (incomplete ? '；所选期间尚未完整覆盖' : '');
    chart.title = coverageText; chart.append(svg);
    if (!preview || incomplete) chart.append(node('figcaption', preview ? '走势覆盖不足' : coverageText, 'discover-note'));
    return chart;
  }
  function performancePanel(host, p, token, epoch, mode = 'compact') {
    const identity = performanceIdentity(), guard = () => live(token, epoch) && performanceIdentity() === identity && host.isConnected;
    let period = '1y', result = null, attempt = 0;
    host.classList.add('discover-performance'); if (mode === 'preview') host.classList.add('discover-performance-preview'); host.dataset.productCode = p.id; host.dataset.catalogVersion = String(p.version);
    function draw(data, issue) {
      if (!guard()) return;
      host.replaceChildren();
      if (!data || data.status !== 'ok') {
        const state = node('div', undefined, 'discover-performance-state'); state.append(node('p', mode === 'preview' ? '收益暂未获取' : mode === 'compact' ? '官方业绩暂未获取' : '收益数据暂未获取', 'discover-performance-missing'), action(mode === 'compact' || mode === 'preview' ? '重试' : '重试读取收益', () => fetch(true), 'discover-performance-retry')); host.append(state);
        if (mode === 'preview' && (issue || data?.notice)) state.title = issue || data.notice;
        if (mode !== 'compact' && mode !== 'preview' && (issue || data?.notice)) host.append(node('p', issue || data.notice, 'discover-note')); return;
      }
      if (mode === 'detail') {
        const tabs = node('div', undefined, 'discover-performance-periods'); tabs.setAttribute('aria-label', '历史涨跌幅期间');
        for (const id of ['1m', '3m', '1y']) { const b = action(performancePeriods[id].replace('涨跌幅', ''), () => { period = id; draw(result); }); b.setAttribute('aria-pressed', id === period); tabs.append(b); }
        host.append(node('h3', '官方历史表现'), tabs);
      }
      if (mode === 'compare') {
        host.append(node('strong', '基金历史涨跌幅（来源披露）'));
        for (const id of ['1m', '3m', '1y']) { const m = data.metrics.find(m => m.period === id); host.append(node('p', performancePeriods[id] + '：' + (m ? percent(m.value_pct) + ' · 截止 ' + m.as_of : '暂未获取'), 'discover-performance-comparison')); }
      } else {
        const metric = data.metrics.find(m => m.period === period), metricBox = node('div', undefined, 'discover-performance-metric');
        const metricValue = node('strong', metric ? percent(metric.value_pct) : '收益数据暂未获取', 'discover-performance-value' + (metric && Number(metric.value_pct) < 0 ? ' negative' : metric && Number(metric.value_pct) > 0 ? ' positive' : ''));
        if (mode === 'preview') {
          const labels = node('div', undefined, 'discover-performance-labels'); labels.append(node('span', performancePeriods[period], 'discover-note'), node('small', metric ? '截至 ' + metric.as_of : '该期间收益尚未核验', 'discover-note')); metricBox.append(metricValue, labels);
        } else metricBox.append(node('span', performancePeriods[period], 'discover-note'), metricValue, node('small', metric ? '截止 ' + metric.as_of : '该期间收益尚未核验', 'discover-note'));
        host.append(metricBox); if (!metric) host.append(action(mode === 'compact' || mode === 'preview' ? '重试' : '重试读取收益', () => fetch(true), 'discover-performance-retry'));
        const latest = data.nav[data.nav.length - 1]; if (latest) host.append(node('p', (mode === 'preview' ? '净值 ' : '最新单位净值 ') + latest.unit_nav + ' · ' + latest.date, 'discover-performance-nav'));
        host.append(navChart(data, period, mode === 'compact' || mode === 'preview', mode === 'preview'));
      }
      if (mode === 'compare' && data.metrics.length < 3) host.append(action('重试读取收益', () => fetch(true), 'discover-performance-retry'));
      if (mode === 'preview') {
        const footer = node('div', undefined, 'discover-performance-footer'), source = evidenceLink('来源 ↗', data.source_url); source.title = '收益来源：' + data.source_name; footer.append(source, viewLink('详情 ›', 'product', routeMetadata(p)));
        if (data.stale) { const stale = node('small', '待更新', 'discover-performance-stale'); stale.title = '业绩资料已过期，请核对来源最新披露。'; footer.append(stale); }
        host.append(footer);
      } else host.append(evidenceLink('收益来源：' + data.source_name, data.source_url));
      if (mode === 'compact') host.append(node('small', '历史表现不代表未来收益', 'discover-note'));
      if (data.stale && mode !== 'preview') host.append(node('p', '业绩资料已过期，请核对来源最新披露。', 'discover-note'));
      if (mode === 'detail') host.append(node('p', data.notice || '历史涨跌幅不代表未来收益；目录日期与业绩截止日期分别核验。', 'discover-note'));
    }
    async function fetch(force = false) {
      if (!guard()) return; const currentAttempt = ++attempt; host.replaceChildren(node('p', mode === 'preview' ? '正在读取收益…' : '正在读取官方收益资料…', 'discover-note'));
      try { const data = await readPerformance(p, force, guard); if (!guard() || currentAttempt !== attempt) return; result = data; draw(data); }
      catch (e) { if (guard() && currentAttempt === attempt) draw(null, e.message || '官方资料读取失败，请重试。'); }
    }
    return fetch();
  }
  function preview(p, token, epoch) {
    const card = node('article', undefined, 'discover-preview'), link = viewLink(null, 'product', routeMetadata(p), 'discover-preview-link'), publicFund = isPublicFund(p), moneyFund = p.source_scope === 'official_money_market', research = p.source_scope === 'public_product_research'; card.id = 'discover-preview-' + p.source_scope + '-' + encodeURIComponent(p.id);
    if (!publicFund && !moneyFund && !research) link.append(node('span', names[p.source_scope], 'discover-identity'));
    link.append(node('h3', p.name), node('p', p.risk + ' · ' + p.term, 'discover-meta')); card.append(link);
    if (research) { const host = node('section'); card.append(host); researchPerformance(host, p); }
    else if (moneyFund) { const host = node('section'); card.append(host); moneyPerformance(host, p); }
    else if (publicFund) { const host = node('section'); card.append(host); queueMicrotask(() => performancePanel(host, p, token, epoch, 'preview')); }
    else card.append(node('small', p.source_scope === 'bank_product' ? '账户产品；办理条件需再次核对' : '公开资料 · 只读', 'discover-note'), node('p', p.returns, 'discover-note'));
    return card;
  }
  function title(text, intro) { const h = document.getElementById('discoverHeading'), p = document.getElementById('discoverIntro'), header = document.querySelector('.header-location'); if (h) h.textContent = text; if (p) p.textContent = intro; if (header) header.textContent = text; }
  function resetSubview() { const root = document.getElementById('discoverSubview'); root.classList.remove('discover-research-page'); root.replaceChildren(action('‹ 返回', () => window.NavrenViews?.back())); return root; }
  function error(root, e, retry) { root.replaceChildren(node('p', e.message || '资料暂不可用', 'discover-error')); if (retry) root.append(action('重新读取', retry)); }
  function selectionLoad() {
    if (!session?.workspace_id) return;
    try { const data = JSON.parse(sessionStorage.getItem('navren_research_selection') || 'null'); if (data?.workspace_id === session.workspace_id && data.privacy_epoch === privacyEpoch && data.privacy_revision === session.privacy_revision) selected = data.refs.filter(r => r && r.source_scope === 'public_catalog' && typeof r.id === 'string' && r.id.length <= 128 && r.version != null).slice(0, 3); } catch { selected = []; }
    reconcileSelection();
  }
  function reconcileSelection() {
    const valid = selected.filter(s => catalogue.some(p => key(p) === key(s) && String(p.version) === String(s.version)));
    const removed = selected.filter(s => !valid.includes(s));
    invalidSelection = [...new Map([...invalidSelection, ...removed].filter(s => s.source_scope !== 'bank_product').map(s => [key(s) + ':' + s.version, s])).values()];
    selected = valid;
    if (removed.length) selectionSave();
  }
  function selectionSave() {
    if (!session?.workspace_id) return;
    try { sessionStorage.setItem('navren_research_selection', JSON.stringify({ workspace_id: session.workspace_id, privacy_epoch: privacyEpoch, privacy_revision: session.privacy_revision, refs: selected.map(p => ({ source_scope: p.source_scope, id: p.id, version: p.version })) })); } catch { /* 临时选择仍保留在本页。 */ }
  }
  function select(p, checked) {
    if (checked && p.source_scope !== 'public_catalog') return false;
    if (checked && p.version == null) { showToast('资料版本尚未核验，暂不能加入比较。'); return false; }
    if (checked && selected.length >= 3 && !selected.some(x => key(x) === key(p))) { showToast('已选 3 款，先移除一款再添加。'); return false; }
    selected = selected.filter(x => key(x) !== key(p)); if (checked) { selected.push({ source_scope: p.source_scope, id: p.id, version: p.version }); invalidSelection = invalidSelection.filter(x => key(x) !== key(p)); } selectionSave(); return true;
  }
  function selectionBar(root, redraw, allowed = () => true) {
    const bar = node('section', undefined, 'discover-selection'); bar.setAttribute('aria-label', '产品比较选择');
    const count = node('strong', '已选 ' + selected.length + ' / 3'), notice = node('p', selected.length < 2 ? '还需选择 ' + (2 - selected.length) + ' 款；至少 2 款才能比较。' : selected.length === 3 ? '已选满 3 款，可先移除再添加。' : '可开始比较，最多 3 款。', 'discover-note');
    const chips = node('div', undefined, 'discover-actions');
    for (const s of selected) { const p = catalogue.find(x => key(x) === key(s)), label = p?.name || '资料已不可用：' + s.id, stale = !p || String(p.version) !== String(s.version); const b = action(label + (stale ? '（需重核）' : '') + ' ×', () => { if (!allowed()) return; select(s, false); redraw(); }, 'discover-chip'); b.setAttribute('aria-label', '移除 ' + label); chips.append(b); }
    const controls = node('div', undefined, 'discover-actions'), compare = action('开始比较', () => { if (allowed()) open('compare', { selected, filters: active.filters || {} }); }, 'primary'); compare.disabled = selected.length < 2;
    controls.append(compare, action('清空', () => { if (!allowed()) return; selected = []; invalidSelection = []; selectionSave(); redraw(); }), action('退出比较', () => { if (!allowed()) return; comparisonMode = false; redraw(); })); bar.append(count, notice, chips, controls); root.append(bar);
    if (invalidSelection.length) { const invalid = node('aside', undefined, 'discover-error'); invalid.append(node('strong', '以下产品资料已变化或不可用，已退出选择；需重新核对后加入。')); for (const s of invalidSelection) invalid.append(node('p', (names[s.source_scope] || s.source_scope) + ' · ' + s.id + ' · 原版本 ' + value(s.version, '未知'))); root.append(invalid); }
  }
  async function research(context, token, epoch) {
    title('产品研究', ''); const root = resetSubview(); root.classList.add('discover-research-page');
    root.append(node('p', '正在读取产品…', 'discover-note'));
    try {
      await loadCatalogue(); if (!live(token, epoch)) return; selectionLoad();
      const allowed = () => live(token, epoch), guarded = fn => (...args) => { if (allowed()) return fn(...args); };
      const heading = node('div', undefined, 'discover-research-page-head'), headingText = node('div', undefined, 'discover-research-title');
      headingText.append(node('h2', '全部产品'));
      heading.append(action('‹ 返回发现', guarded(() => window.NavrenViews?.back()), 'discover-back'), headingText);
      root.replaceChildren(heading);
      const productType = p => String(p.raw.type || '').trim();
      const types = [...new Set(catalogue.map(productType).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'zh-CN'));
      const toolbar = node('form', undefined, 'discover-toolbar discover-research-toolbar');
      const search = field('搜索产品、机构或编码', 'search');
      const type = field('产品类型', 'select', [['', '全部类型'], ...types.map(t => [t, t]), ['unknown', '类型未披露']]);
      const riskFilter = field('风险等级', 'select', [['', '全部风险'], ...['R1', 'R2', 'R3', 'R4', 'R5'].map(r => [r, r]), ['unknown', '等级未披露']]);
      const termFilter = field('投资期限', 'select', [['', '全部期限'], ['flex', '无锁定期'], ['short', '90 天及以内'], ['long', '超过 90 天'], ['unknown', '期限未披露']]);
      const amount = field('起购上限（元）', 'number'); amount.input.min = '0'; amount.input.step = '0.01'; amount.input.placeholder = '不限';
      search.box.classList.add('grow'); search.input.placeholder = '输入名称、机构或产品编码'; search.input.maxLength = 200;
      const inputs = { q: search.input, type: type.input, risk: riskFilter.input, term: termFilter.input, maximum: amount.input };
      const maximumMinor = v => { if (v === '') return null; if (!/^\d+(?:\.\d{1,2})?$/.test(v)) return NaN; const [whole, fraction = ''] = v.split('.'); const cents = Number(whole) * 100 + Number(fraction.padEnd(2, '0')); return Number.isSafeInteger(cents) ? cents : NaN; };
      const pageSizes = [20, 50, 100];
      let pageSize = pageSizes.includes(Number(context.filters?.page_size)) ? Number(context.filters.page_size) : 20;
      let page = Number.isSafeInteger(Number(context.filters?.page)) && Number(context.filters.page) > 0 ? Number(context.filters.page) : 1;
      for (const [k, input] of Object.entries(inputs)) input.value = String(context.filters?.[k] || '');
      if (!Number.isFinite(maximumMinor(amount.input.value)) && amount.input.value !== '') amount.input.value = '';
      const readInputs = () => Object.fromEntries(Object.entries(inputs).map(([k, input]) => [k, input.value.trim()]));
      let applied = readInputs();
      const filters = () => ({ ...applied, page, page_size: pageSize });
      const persist = () => { const f = filters(); active = { ...active, filters: f }; window.NavrenViews?.update({ filters: f }); };
      const filterActions = node('div', undefined, 'discover-research-filter-actions'), apply = action('应用筛选', null, 'primary'); apply.type = 'submit';
      const reset = action('重置', guarded(() => { for (const input of Object.values(inputs)) input.value = ''; amount.input.setCustomValidity(''); applied = readInputs(); page = 1; redraw(true); }));
      filterActions.append(apply, reset); toolbar.append(search.box, type.box, riskFilter.box, termFilter.box, amount.box, filterActions);
      const resultHead = node('div', undefined, 'discover-research-results-head'), status = node('p', '', 'discover-note'); status.setAttribute('role', 'status');
      const compareMode = action('比较产品', guarded(() => { comparisonMode = !comparisonMode; redraw(); }));
      resultHead.append(status, compareMode);
      const results = node('div', undefined, 'discover-results'), bar = node('div'), pagination = node('nav', undefined, 'discover-research-pagination'); pagination.setAttribute('aria-label', '产品列表分页');
      const range = node('p', '', 'discover-research-page-range'), size = field('每页条数', 'select', pageSizes.map(n => [String(n), n + ' 条'])); size.input.value = String(pageSize);
      const paging = node('div', undefined, 'discover-actions'), pageNumber = node('span', '', 'discover-research-page-number');
      const previous = action('上一页', guarded(() => { --page; redraw(true); })), next = action('下一页', guarded(() => { ++page; redraw(true); }));
      paging.append(previous, pageNumber, next); pagination.append(range, size.box, paging);
      size.input.onchange = guarded(() => { const chosen = Number(size.input.value); if (!pageSizes.includes(chosen)) return; pageSize = chosen; page = 1; redraw(true); });
      root.append(toolbar, resultHead, results, pagination, bar);
      function productLink(text, p, cls) {
        const a = viewLink(text, 'product', { ...routeMetadata(p), filters: filters() }, cls), navigate = a.onclick;
        a.onclick = e => { if (!allowed()) { e.preventDefault(); return; } navigate(e); }; return a;
      }
      let pageRound = 0;
      function redraw(save = false) {
        if (!allowed()) return;
        const round = ++pageRound, returnsReady = []; cancelListReturns();
        const f = applied, maximum = maximumMinor(f.maximum);
        const rows = catalogue.filter(p => {
          if (f.q && !(p.name + ' ' + p.manager + ' ' + p.id).toLowerCase().includes(f.q.toLowerCase())) return false;
          if (f.type && (f.type === 'unknown' ? Boolean(productType(p)) : productType(p) !== f.type)) return false;
          if (f.risk && (f.risk === 'unknown' ? riskCode(p.risk) !== null : riskCode(p.risk) !== f.risk)) return false;
          const days = Number.isSafeInteger(p.raw.term_days) && p.raw.term_days >= 0 ? p.raw.term_days : null;
          if (f.term && (f.term === 'unknown' ? days !== null : f.term === 'flex' ? days !== 0 : f.term === 'short' ? days === null || days > 90 : days === null || days <= 90)) return false;
          if (maximum !== null && (!Number.isSafeInteger(p.raw.min_purchase_minor) || p.raw.min_purchase_minor < 0 || p.raw.min_purchase_minor > maximum)) return false;
          return true;
        });
        const pages = Math.max(1, Math.ceil(rows.length / pageSize)); page = Math.max(1, Math.min(page, pages));
        if (save) persist(); else active = { ...active, filters: filters() };
        results.replaceChildren(); bar.replaceChildren();
        compareMode.setAttribute('aria-pressed', String(comparisonMode)); compareMode.textContent = comparisonMode ? '退出比较模式' : selected.length ? '比较产品（已选 ' + selected.length + '）' : '比较产品';
        status.textContent = '共 ' + rows.length.toLocaleString('zh-CN') + ' 项产品' + (Object.values(applied).some(Boolean) ? ' · 已筛选' : '');
        const offset = (page - 1) * pageSize, shown = rows.slice(offset, offset + pageSize);
        for (const p of shown) {
          const row = node('article', undefined, 'discover-product-row'); row.dataset.comparison = String(comparisonMode);
          const choice = node('label', undefined, 'discover-select-cell'), check = node('input'); check.type = 'checkbox'; check.id = 'discover-select-' + p.source_scope + '-' + encodeURIComponent(p.id);
          check.checked = selected.some(s => key(s) === key(p)); check.setAttribute('aria-label', '比较 ' + p.name); check.disabled = p.version == null || !check.checked && selected.length >= 3;
          check.onchange = guarded(() => { if (!select(p, check.checked)) check.checked = false; redraw(); }); choice.hidden = !comparisonMode; choice.append(check);
          const link = productLink(null, p, 'discover-product-link'); link.id = 'discover-product-' + p.source_scope + '-' + encodeURIComponent(p.id);
          const metadata = [p.risk, productType(p)];
          if (p.source_scope === 'public_catalog' && has(p.raw.publish)) metadata.push(String(p.raw.publish));
          const titleLine = node('div', undefined, 'discover-product-title-line');
          titleLine.append(node('h3', p.name), node('span', '公开资料', 'discover-identity'));
          link.append(titleLine, node('p', metadata.filter(Boolean).join(' · '), 'discover-meta'));
          const actions = node('div', undefined, 'discover-product-actions'); actions.append(productLink('查看资料 ›', p, 'discover-action'));
          const returnHost = node('section', undefined, 'discover-row-return'); returnHost.setAttribute('aria-label', p.name + '近期收益');
          row.append(choice, link, returnHost, actions); results.append(row);
          const head = catalogHead, identity = performanceIdentity();
          returnsReady.push(listReturnPanel(returnHost, p, head, () => pageActive && allowed() && round === pageRound && returnHost.isConnected && head?.catalog_version === catalogHead?.catalog_version && identity === performanceIdentity()));
        }
        if (!rows.length) results.append(node('p', '没有匹配的产品，试试其他条件。', 'discover-empty'));
        range.textContent = rows.length ? (offset + 1) + '–' + (offset + shown.length) + ' / ' + rows.length.toLocaleString('zh-CN') + ' 项' : '0 项';
        pageNumber.textContent = page + ' / ' + pages; previous.disabled = page === 1; next.disabled = page >= pages;
        if (comparisonMode) selectionBar(bar, () => redraw(), allowed);
        return Promise.allSettled(returnsReady);
      }
      toolbar.onsubmit = e => { e.preventDefault(); if (!allowed()) return; if (amount.input.value !== '' && !Number.isFinite(maximumMinor(amount.input.value))) { amount.input.setCustomValidity('请输入有效金额，最多两位小数。'); amount.input.reportValidity(); return; } amount.input.setCustomValidity(''); applied = readInputs(); page = 1; redraw(true); };
      amount.input.oninput = guarded(() => amount.input.setCustomValidity('')); await redraw();
    } catch (e) { if (live(token, epoch)) error(root, e, () => refresh(true)); }
  }
  const specRows = p => [['产品身份 / 管理人', names[p.source_scope] + '\n' + p.manager], ['风险', p.risk], ['产品类型 / 期限', p.term], ['起购', p.minimum], ['费用', p.fees], ['申赎与到账', p.liquidity], ['收益指标类型与期间', p.returns], ['资料日期', date(p.as_of)], ['资料来源', p.source], ['条件与缺项', p.eligibility + '\n' + p.gaps]];
  function specs(p) { const dl = node('dl', undefined, 'discover-specs'); for (const [label, text] of specRows(p)) dl.append(node('dt', label), node('dd', text)); return dl; }
  function lookup(context) { return catalogue.find(p => p.id === context.object_id && p.source_scope === context.source_scope); }
  function mismatch(p, context) { return context.object_version != null && String(context.object_version) !== String(p.version); }
  async function purchase(p, root) {
    const epoch = privacyEpoch, token = generation;
    try {
      const [current, overview, identity] = await Promise.all([B().get('/products'), B().get('/overview'), B().ensure()]);
      if (epoch !== privacyEpoch || identity.workspace_id !== session?.workspace_id) throw new Error('账户身份已变化，请重新核对产品。');
      const valid = current.find(x => x.id === p.id && String(x.version) === String(p.version));
      if (!valid || valid.source !== 'simulation' || valid.profile_eligible !== true || !live(token, epoch)) throw new Error('产品资料或匹配条件已变化，重新读取后才能准备。');
      const old = root.querySelector('.discover-purchase-accounts'); old?.remove();
      const form = node('section', undefined, 'discover-purchase-accounts'), accounts = field('用于申购的账户', 'select', (overview.accounts || []).filter(a => a.currency === 'CNY').map(a => [a.id, a.name]));
      form.append(node('p', '选定账户后填写金额。生成确认卡仍须核对并由本人确认。', 'discover-note'), accounts.box);
      const next = action('填写申购金额', () => { const id = accounts.input.value; if (!(overview.accounts || []).some(a => a.id === id)) return; window.BankUI.amountForm(valid, 'purchase', id); }); next.disabled = !accounts.input.options.length; form.append(next); root.append(form); accounts.input.focus();
    } catch (e) { showToast(e.message); }
  }
  function evidenceLink(label, url) { const a = node('a', label); try { const u = new URL(url); if (u.protocol !== 'https:' || u.username || u.password) return node('span', label + '（链接暂不可核验）'); a.href = u.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; } catch { return node('span', label + '（链接未披露）'); } return a; }
  function productEvidence(p) { const section = node('section'); section.append(node('h3', '资料与销售证据')); const links = node('div', undefined, 'discover-actions');
    if (p.source_scope === 'public_catalog') links.append(evidenceLink('微众官方产品查询', p.raw.source_url), node('span', '披露日期 ' + date(p.as_of), 'discover-note'));
    if (p.source_scope === 'public_nav') { const source = p.raw.product; links.append(evidenceLink('管理人产品资料', source.detail_url)); if (source.sales_evidence?.url) links.append(evidenceLink('历史销售机构公告', source.sales_evidence.url), node('span', '生效日期 ' + date(source.sales_evidence.effective_on) + '；不证明当前微众在售。', 'discover-note')); if (source.fee_rules?.text) { const fees = node('details'); fees.append(node('summary', '完整费用资料与渠道缺项'), node('p', source.fee_rules.text, 'discover-note')); section.append(fees); } }
    section.append(links); return section;
  }
  async function product(context, token, epoch) {
    title('产品资料', '先辨认身份、日期与缺项，操作条件另行核对。'); const root = resetSubview(); root.append(node('p', '正在读取产品资料…', 'discover-note'));
    try { if (context.source_scope === 'public_product_research') return await researchDetail(context, token, epoch, root); if (context.source_scope === 'official_money_market') return await moneyDetail(context, token, epoch, root); await loadCatalogue(); if (!live(token, epoch)) return; const p = lookup(context); if (!p) { error(root, new Error(context.source_scope === 'public_nav' ? '管理人净值资料已移出当前产品研究。历史组合仍可在账户中回看，请返回研究列表查看微众披露。' : '没有找到这份产品资料。可能已删除或目录变化，请返回研究列表。'), () => open('research')); return; }
      root.replaceChildren(action('‹ 返回', () => window.NavrenViews?.back()));
      if (p.source_scope === 'public_catalog' && p.raw.type === '保险') {
        title('保险资料');
        insuranceModule().detail(root, p, { sync_context: syncContext() });
        if (mismatch(p, context)) root.prepend(node('p', '目录已更新，以下为新快照。', 'discover-error'));
        recordReading('public_product', p.id, p.version, p.name, p.source_scope);
        return;
      }
      const box = node('article', undefined, 'discover-detail'); box.append(node('span', names[p.source_scope], 'discover-identity'), node('h2', p.name)); let performanceHost; if (isPublicFund(p)) { performanceHost = node('section'); box.append(performanceHost); } box.append(specs(p));
      if (p.source_scope !== 'bank_product') recordReading('public_product', p.id, p.version, p.name, p.source_scope);
      const changed = mismatch(p, context); if (changed) box.prepend(node('p', '所引用版本已变化，下面为当前资料。请重新核对后再选择操作。', 'discover-error'));
      const controls = node('div', undefined, 'discover-actions'), cite = action('引用到提问', () => referenceProduct(p)), add = action(selected.some(s => key(s) === key(p)) ? '已加入比较' : '加入比较', () => { if (select(p, true)) { comparisonMode = true; open('research'); } }); cite.disabled = add.disabled = p.version == null; controls.append(cite, add);
      if (p.version == null) box.append(node('p', '资料缺少可核验版本，暂不能作为比较或对话中的稳定引用。', 'discover-note'));
      if (p.source_scope === 'bank_product') { const buy = action('准备申购', () => purchase(p, box), 'primary'); buy.disabled = changed || !p.executable; controls.append(buy); }
      else box.append(node('p', '这份公开资料只用于查看和比较，不能据此创建申购草案。', 'discover-note'));
      if (p.source_scope !== 'bank_product') box.append(productEvidence(p)); box.append(controls); root.append(box); if (performanceHost) await performancePanel(performanceHost, p, token, epoch, 'detail');
    } catch (e) { if (live(token, epoch)) error(root, e, () => refresh(true)); }
  }
  async function compare(context, token, epoch) {
    title('产品比较', '按相同字段核对。不同类型的收益指标保持原口径，不评选收益赢家。'); const root = resetSubview();
    try { await loadCatalogue(); if (!live(token, epoch)) return; selectionLoad(); if (context.selected?.length) { selected = context.selected.slice(0, 3); reconcileSelection(); selectionSave(); }
      const rows = selected.map(s => catalogue.find(p => key(p) === key(s) && String(p.version) === String(s.version))).filter(Boolean), invalid = invalidSelection;
      active = { ...context, selected: selected.slice() }; window.NavrenViews?.update({ selected: selected.slice() });
      if (invalid.length) { const notice = node('section', undefined, 'discover-error'); notice.append(node('strong', invalid.length + ' 项资料已变化或不可用，已退出选择。以下保留仍有效的 ' + rows.length + ' 项对照；需本人重新核对失效产品后再加入。')); for (const s of invalid) notice.append(node('p', (names[s.source_scope] || s.source_scope) + ' · ' + s.id + ' · 原版本 ' + value(s.version, '未知'))); root.append(notice); }
      if (rows.length < 2) { root.append(node('p', '比较至少需要 2 款有效资料。旧版本不会被悄悄替换为同名产品。', 'discover-empty'), action('选择产品', () => { comparisonMode = true; open('research'); })); return; }
      const controls = node('div', undefined, 'discover-actions'); controls.append(action('调整选择', () => { comparisonMode = true; open('research', { filters: context.filters || {} }); }), action('保存为备选方案', () => savePlanForm(root, rows)));  root.append(controls);
      const wrap = node('div', undefined, 'discover-compare-wrap'); wrap.tabIndex = 0; wrap.setAttribute('role', 'region'); wrap.setAttribute('aria-label', '产品逐项比较，可横向滚动');
      const table = node('table', undefined, 'discover-compare' + (rows.length === 3 ? ' has-third' : '')); const caption = node('caption', '产品逐项对照'); caption.className = 'sr-only'; table.append(caption);
      const head = node('thead'), tr = node('tr'); tr.append(node('th', '对照字段')); for (const p of rows) { const th = node('th'); th.scope = 'col'; th.append(viewLink(p.name, 'product', routeMetadata(p))); tr.append(th); } head.append(tr); table.append(head);
      const body = node('tbody'), fields = rows.map(specRows), performanceCells = []; fields[0].forEach(([label], i) => { const row = node('tr'), h = node('th', label); h.scope = 'row'; row.append(h); fields.forEach((f, index) => { const td = node('td', f[i][1]); if (label === '收益指标类型与期间' && isPublicFund(rows[index])) { td.replaceChildren(); performanceCells.push([td, rows[index]]); } row.append(td); }); body.append(row); });
      const operations = node('tr'), heading = node('th', '明确操作'); heading.scope = 'row'; operations.append(heading);
      for (const p of rows) { const td = node('td'); td.append(viewLink('查看完整资料', 'product', routeMetadata(p))); if (p.source_scope === 'bank_product') { const buy = action('准备申购', () => purchase(p, root)); buy.disabled = !p.executable; td.append(buy); } else td.append(node('p', '公开资料 · 只读')); operations.append(td); } body.append(operations); table.append(body); wrap.append(table);
      root.append(node('p', rows.length === 3 ? '共 3 款。手机可在表格内横向查看第三款，左侧字段保持对应。' : '手机可在表格内横向查看，左侧字段保持对应。', 'discover-compare-help'), wrap); await Promise.allSettled(performanceCells.map(([cell, p]) => performancePanel(cell, p, token, epoch, 'compare')));
    } catch (e) { if (live(token, epoch)) error(root, e, () => refresh(true)); }
  }
  function fingerprint(plan) { let h = 2166136261; for (const c of JSON.stringify(plan)) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); } return 'legacy-' + (h >>> 0).toString(16); }
  function legacyPlans() { if (!legacyVisible) return []; const identity=B().identity?.(),owned=Storage.privateIdentity?.();if(!identity||!owned||['registered_user_id','workspace_id','privacy_revision','auth_subject'].some(key=>owned[key]!==identity[key]))return [];const data=Storage.get('qingzhou_allocation');if(!data?.allocation?.length)return [];return [data,...(data._history||[]).slice().reverse()].map(p=>({...p,id:fingerprint({generated_at:p.generated_at,summary:p.summary,allocation:p.allocation,based_on:p.based_on}),source_scope:'browser_history'})); }
  function legacyPlanPreview() { const root = document.getElementById('planPreview'); if (!root) return; root.replaceChildren(); const plan = legacyPlans()[0]; if (!plan) { root.append(node('p', '还没有备选方案。你可以在对话中讨论资金安排；查看研究资料不会建立订单。', 'discover-empty')); return; }
    const a = viewLink(null, 'plan', { object_kind: 'research_plan', object_id: plan.id, source_scope: 'browser_history' }, 'discover-plan-cover'); a.append(node('span', '待核对 · 历史浏览器研究记录', 'discover-identity'), node('h3', plan.summary || '备选研究方案'), node('p', date(plan.generated_at) + ' · ' + plan.allocation.length + ' 项安排 · 落实情况未知', 'discover-meta')); root.append(a);
  }
  function legacyPlanView(context) {
    title(context.view === 'plans' ? '历史备选方案' : '备选方案', '研究状态与实际落实分别判断。旧浏览器记录只读，不能按名称下单。'); const root = resetSubview(), all = legacyPlans();
    if (context.view === 'plans') { if (!all.length) root.append(node('p', '没有历史研究方案。', 'discover-empty')); for (const p of all) { const a = viewLink(null, 'plan', { object_kind: 'research_plan', object_id: p.id, source_scope: 'browser_history' }, 'discover-plan-cover'); a.append(node('h3', p.summary || '历史方案'), node('p', date(p.generated_at) + ' · 待核对 · 只读', 'discover-meta')); root.append(a); } return; }
    const plan = all.find(p => p.id === context.object_id); if (!plan) { root.append(node('p', '这份历史方案已不在当前浏览器记录中，不能替换为另一方案。', 'discover-empty'), action('查看历史方案', () => open('plans'))); return; }
    const box = node('article', undefined, 'discover-detail'); box.append(node('h2', plan.summary || '备选研究方案'), node('p', '研究状态：待核对 · 落实情况：未知（没有关联操作 ID 与有效回执）', 'discover-note'), node('p', '这份旧记录未保存可核验的银行业务产品身份与来源版本。名称、比例和历史条件仅用于回看，不连接当前申购。', 'discover-error'));
    const dl = node('dl', undefined, 'discover-specs'); for (const [label, text] of [['生成时间', date(plan.generated_at)], ['风险假设', value(plan.based_on?.risk_level, '未记录；不是正式风险测评')], ['金额假设', value(plan.based_on?.amount, '未记录')], ['期限假设', value(plan.based_on?.horizon, '未记录')], ['资料身份', '历史浏览器研究记录 · ' + plan.id]]) dl.append(node('dt', label), node('dd', text)); box.append(dl);
    const items = node('div', undefined, 'discover-plan-items'); for (const item of plan.allocation || []) { const card = node('section', undefined, 'discover-plan-item'); card.append(node('h3', item.name || '名称未记录'), node('p', '计划比例 ' + value(item.ratio, '未知') + (has(item.ratio) ? '%' : '') + ' · ' + risk(item.risk), 'discover-meta'), node('p', '产品 ID、来源版本、条件证据及关联执行回执未记录。', 'discover-note')); items.append(card); } box.append(items);
    if (plan.report || plan.content) { const report = node('details'); report.append(node('summary', '历史研究报告'), node('p', plan.report || plan.content, 'discover-note')); box.append(report); }
    root.append(box);
  }
  async function readServerPlans() {
    if (plansFlight) return plansFlight; const epoch = privacyEpoch;
    plansFlight = (async () => { await B().ensure(); const guard = B().guard?.(); const data = await B().get('/research-plans'); if (epoch !== privacyEpoch || guard && !guard.valid(data)) return []; serverPlans = (data.items || []).map(p => ({ ...p, source_scope: 'server_research' })); return serverPlans; })().finally(() => { plansFlight = null; }); return plansFlight;
  }
  async function planPreview() {
    const token = generation, epoch = privacyEpoch, root = document.getElementById('planPreview'); if (!root) return;
    try { await readServerPlans(); if (!live(token, epoch)) return; const plan = serverPlans[0]; if (!plan) { legacyPlanPreview(); return; }
      root.replaceChildren(); const cover = viewLink(null, 'plan', { object_kind: 'research_plan', object_id: plan.id, object_version: plan.object_version, source_scope: 'server_research' }, 'discover-plan-cover');
      cover.append(node('span', plan.source_stale || plan.status === 'stale' ? '已过期 · 条件需重新核对' : '备选 · 已保存研究依据', 'discover-identity'), node('h3', plan.name), node('p', executionText(plan.execution), 'discover-meta')); root.append(cover);
    } catch (e) { if (live(token, epoch)) { legacyPlanPreview(); root.append(node('p', '服务端研究记录暂时无法读取：' + e.message, 'discover-note')); } }
  }
  function executionText(e) { if (!e) return '落实情况未知；没有执行回执'; const completed = Number(e.covered) || 0, count = Number(e.total) || 0; return (count > 0 && completed === count ? '产品回执已覆盖' : completed > 0 ? '部分产品回执已覆盖' : '尚无已完成产品回执') + ' · 已完成 ' + (e.covered || 0) + ' / ' + (e.total || 0) + ' 项；受理或待结算不作为完成，金额与期限假设另行核对'; }
  function savePlanForm(root, rows) {
    if (root.querySelector('.discover-plan-save')) { root.querySelector('.discover-plan-save input')?.focus(); return; }
    const form = node('form', undefined, 'discover-plan-save discover-detail'), name = field('方案名称'), assumptions = field('金额、期限与研究假设'), note = node('p', '保存产品身份、当前来源版本与你的研究假设。保存方案不会准备或执行申购。', 'discover-note'), issue = node('p', '', 'discover-error'); issue.hidden = true;
    name.input.required = true; name.input.maxLength = 80; name.input.value = '我的产品备选'; assumptions.input.maxLength = 1200; assumptions.input.placeholder = '例如：尚未确定金额；预计半年内使用，需继续核验流动性';
    const submit = action('确认保存备选', null, 'primary'); submit.type = 'submit'; form.append(note, name.box, assumptions.box, submit, issue); root.append(form);
    form.onsubmit = async e => { e.preventDefault(); if (submit.disabled) return; submit.disabled = true; issue.hidden = true; const epoch = privacyEpoch, token = generation;
      try { const result = await B().post('/research-plans', { name: name.input.value.trim(), products: rows.map(ref), assumptions: assumptions.input.value.trim() || '金额与期限尚未确定；仅保存当前比较资料。', confirmed: true }); if (!live(token, epoch)) return; if (!result.plan?.id) throw new Error('未取得服务端保存回执，请查阅研究记录。'); serverPlans.unshift({ ...result.plan, source_scope: 'server_research' }); await open('plan', { object_kind: 'research_plan', object_id: result.plan.id, object_version: result.plan.object_version, source_scope: 'server_research' }); }
      catch (error) { if (live(token, epoch)) { issue.hidden = false; issue.textContent = error.message; submit.disabled = false; } }
    }; name.input.focus();
  }
  async function associationForm(plan, root) {
    const token = generation, epoch = privacyEpoch;
    try { const data = await B().get('/overview'); if (!live(token, epoch)) return; const ids = new Set((plan.products || []).filter(p => p.source_scope === 'bank_product').map(p => p.id));
      const operations = (data.operations || []).filter(op => { try { const payload = typeof op.payload === 'string' ? JSON.parse(op.payload) : op.payload; return op.action === 'purchase' && ids.has(payload?.product_id); } catch { return false; } });
      const form = node('form', undefined, 'discover-plan-association discover-detail'); root.querySelector('.discover-plan-association')?.remove();
      if (!operations.length) { form.append(node('p', '当前没有与方案中业务产品 ID 对应的申购操作。先在产品详情按现行条件办理，再回查受理或完成回执。', 'discover-note')); root.append(form); return; }
      const choose = field('关联实际申购操作', 'select', operations.map(op => [op.id, '申购 · ' + (BankUI.statuses[op.state] || op.state) + ' · ' + op.id.slice(0, 8)])), submit = action('确认关联回执', null), issue = node('p', '', 'discover-note'); submit.type = 'submit';
      form.append(node('p', '关联已有操作只用于核对落实进度，不会重新执行。完成状态由服务端当前回执判断。', 'discover-note'), choose.box, submit, issue); root.append(form);
      form.onsubmit = async e => { e.preventDefault(); if (submit.disabled) return; submit.disabled = true; try { await B().post('/research-plans/' + encodeURIComponent(plan.id) + '/execution', { operation_id: choose.input.value, confirmed: true }); if (live(token, epoch)) await render(active); } catch (error) { if (live(token, epoch)) { issue.textContent = error.message; submit.disabled = false; } } }; choose.input.focus();
    } catch (e) { if (live(token, epoch)) showToast(e.message); }
  }
  async function planView(context, token, epoch) {
    title(context.view === 'plans' ? '历史备选方案' : '备选方案', '研究状态与实际落实分别判断；落实进度来自关联操作的实际回执。'); const root = resetSubview(); root.append(node('p', '正在读取服务端研究记录…', 'discover-note'));
    try { await Promise.all([readServerPlans(), loadCatalogue()]); if (!live(token, epoch)) return;
      if (context.view === 'plans') { root.replaceChildren(action('‹ 返回', () => window.NavrenViews?.back())); for (const p of serverPlans) { const a = viewLink(null, 'plan', { object_kind: 'research_plan', object_id: p.id, object_version: p.object_version, source_scope: 'server_research' }, 'discover-plan-cover'); a.append(node('h3', p.name), node('p', (p.source_stale || p.status === 'stale' ? '已过期 · ' : '备选 · ') + executionText(p.execution), 'discover-meta')); root.append(a); } for (const p of legacyPlans()) { const a = viewLink(null, 'plan', { object_kind: 'research_plan', object_id: p.id, source_scope: 'browser_history' }, 'discover-plan-cover'); a.append(node('h3', p.summary || '历史方案'), node('p', date(p.generated_at) + ' · 历史浏览器记录 · 只读', 'discover-meta')); root.append(a); } if (!serverPlans.length && !legacyPlans().length) root.append(node('p', '还没有备选方案，可在比较结果中保存研究资料。', 'discover-empty')); return; }
      if (context.source_scope === 'browser_history' || context.object_id?.startsWith('legacy-')) { legacyPlanView(context); return; }
      const plan = serverPlans.find(p => p.id === context.object_id); if (!plan) { error(root, new Error('方案已删除或不属于当前账户。'), () => open('plans')); return; }
      root.replaceChildren(action('‹ 返回', () => window.NavrenViews?.back())); const box = node('article', undefined, 'discover-detail');
      box.append(node('h2', plan.name), node('p', '研究状态：' + (plan.source_stale || plan.status === 'stale' ? '已过期；当前资料已变化' : plan.status === 'needs_review' ? '待核对' : '备选') + ' · ' + executionText(plan.execution), 'discover-note'), node('p', '研究假设：' + (plan.assumptions || '未记录金额与期限'), 'discover-note'));
      if (context.object_version != null && String(context.object_version) !== String(plan.object_version)) box.append(node('p', '方案版本已变化。下面为服务端当前记录；操作前须重新核对。', 'discover-error'));
      const items = node('div', undefined, 'discover-plan-items'); for (const stored of plan.products || []) { const p = catalogue.find(p => p.id === stored.id && p.source_scope === stored.source_scope), card = node('section', undefined, 'discover-plan-item'); card.append(node('h3', stored.label || p?.name || stored.id), node('p', (names[stored.source_scope] || stored.source_scope) + ' · 产品 ' + stored.id + ' · 资料版本 ' + value(stored.version, '未知'), 'discover-note'));
        if (p) { card.append(node('p', p.risk + ' · ' + p.term + ' · 当前资料日期 ' + date(p.as_of), 'discover-meta'), viewLink('核对当前产品资料', 'product', routeMetadata(p))); if (String(stored.version) !== String(p.version)) card.append(node('p', '研究引用版本与当前资料不同，需重新研究。', 'discover-error')); } else card.append(node('p', '产品资料当前无法读取；保留原身份，不替换为同名产品。', 'discover-note')); items.append(card); } box.append(items);
      if (plan.execution?.operations?.length) { const receipts = node('section'); receipts.append(node('h3', '关联操作与当前回执')); for (const receipt of plan.execution.operations) { const a = node('a', '查看操作 ' + receipt.id.slice(0, 8) + ' · ' + (BankUI.statuses[receipt.state] || receipt.state)); a.href = 'account.html?view=activity&object_kind=operation&object_id=' + encodeURIComponent(receipt.id); receipts.append(a); } box.append(receipts); }
      const controls = node('div', undefined, 'discover-actions'); controls.append(action('引用到提问', () => NavrenViews.reference({ kind: 'research_plan', id: plan.id, version: plan.object_version, source_scope: 'server_research', label: plan.name })), action('关联已有操作回执', () => associationForm(plan, box)), action('刷新实际进度', () => render(context))); box.append(controls); root.append(box);
    } catch (e) { if (live(token, epoch)) { if (context.source_scope === 'browser_history' || context.object_id?.startsWith('legacy-')) legacyPlanView(context); else error(root, e, () => render(context)); } }
  }
  function readingIdentity(row){const kind=row.kind||row.object_kind;return {kind,id:row.id||row.object_id,version:String(row.version??''),source_scope:['event','article','weekly'].includes(kind)&&['public','market','public_market'].includes(row.source_scope)?'public_market':row.source_scope,product_source:row.product_source||null,archive:row.archive?{edition_date:row.archive.edition_date,revision:Number(row.archive.revision),event_version:Number(row.archive.event_version)}:null,time_view:row.time_view||null};}
  async function storeReading(entry){
    const epoch=accountEpoch,identity=accountIdentity();
    try{if(!accountReady())return false;const target=itemKey(readingIdentity(entry));await Storage.update('navren_reading_history',value=>{const rows=(Array.isArray(value)?value:[]).filter(row=>itemKey(readingIdentity(row))!==target);return [entry,...rows].slice(0,200);});return accountCurrent(epoch,identity);}
    catch(error){if(accountCurrent(epoch,identity))showToast('阅读记录未保存：'+error.message);return false;}
  }
  function recordReading(kind,id,version,label,productSource,archive){if(!id||version==null)return Promise.resolve(false);return storeReading({kind,id,version,source_scope:['event','article','weekly'].includes(kind)?'public_market':'public',...(productSource?{product_source:productSource}:{}),...(archive?{archive:{edition_date:archive.edition_date,revision:Number(archive.revision),event_version:Number(archive.event_version)}}:{}),label,updated_at:new Date().toISOString()});}
  function recordMarketReading(item){return recordReading('event',item.id,item.archive?.event_version??item.version,item.display_title||item.headline,undefined,item.archive);}
  function recordHistoryReading(context,label){
    const c=MarketUI.historySelection(context);return storeReading({kind:c.object_kind,id:c.object_id,version:c.object_version,time_view:'historical_audit',source_scope:'public_market',label,updated_at:new Date().toISOString()});
  }
  async function historical(context,token,epoch){
    title(context.object_kind==='weekly'?'历史市场周报':'历史文章','查阅明确选定的旧版本。');const root=resetSubview();root.append(node('p','正在核对选定历史版次…','discover-note'));
    try{const exact=MarketUI.historySelection(context);await B().refreshIdentity();if(!live(token,epoch))return;const guard=B().guard(),data=await MarketUI.getHistory(exact);if(!live(token,epoch)||!guard.valid())return;
      const object=MarketUI.renderHistory(root,data,exact);await recordHistoryReading(exact,object.title_cn||object.title||exact.object_id);
    }catch(e){if(live(token,epoch))MarketUI.historyUnavailable(root,context,e);}
  }
  async function reading(context, token, epoch) {
    if(context.time_view==='historical_audit')return historical(context,token,epoch);
    if (context.object_kind === 'weekly') return weekly({ ...context, period: context.period || context.object_id }, token, epoch);
    if (context.object_kind !== 'event' || !context.object_id) { error(resetSubview(), new Error('缺少公开市场资料身份，不能替换为其他报道。')); return; }
    const homeRoot = document.getElementById('discoverHome'); homeRoot.hidden = false; document.getElementById('discoverSubview').hidden = true; await home(token, epoch); if (!live(token, epoch)) return;
    let archive=null;
    if(context.edition_date||context.revision!=null){if(!context.edition_date||context.revision==null||context.object_version==null){error(resetSubview(),new Error('归档身份不完整，暂不能打开详情。'));return;}archive={edition_date:context.edition_date,revision:Number(context.revision),event_version:Number(context.object_version)};}
    else{const saved=[...(Storage.get('qingzhou_bookmarks')||[]),...(Storage.get('navren_reading_history')||[])].find(r=>(r.object_kind==='event'||r.kind==='event')&&(r.object_id||r.id)===context.object_id&&String(r.version)===String(context.object_version)&&r.archive);if(saved)archive={...saved.archive};}
    await MarketUI.openBookmark({ object_kind: 'event', object_id: context.object_id, version: Number(context.object_version), ...(archive?{archive}:{} ) }); if (!live(token, epoch)) return;
    const dialog = document.querySelector('.market-panel-layer[open]'); if (dialog) { const footer = dialog.querySelector('[data-bookmark-id]'); if (footer) recordReading('event', footer.dataset.bookmarkId, Number(footer.dataset.bookmarkVersion), dialog.querySelector('.market-panel-title')?.textContent || '市场资料',undefined,archive); dialog.addEventListener('close', () => { if (dialog.dataset.marketActionClose !== 'true' && token === generation && active.view === 'reading') open('home', {}, { replace: true }); }, { once: true }); }
  }
  function readable(a) { return a?.status === 'active' && a.read_scope === 'full_text' && a.link_status === 'verified' && a.access_status === 'free_full_text'; }
  function sourceRow(article) { const root = node('div'), message = node('div'), bubble = node('div', undefined, 'bubble'); message.append(bubble); MarketUI.sources(message, [article]); root.append(node('h4', article.title_cn || article.title || article.publisher), ...bubble.children); return root; }
  function marketReference(kind, id, version, label, period) { window.NavrenViews?.reference({ kind, id, version, source_scope: 'market', label }); document.querySelector('#navrenPageQuestion')?.focus(); }
  function syncSavedWeeklies(){for(const b of document.querySelectorAll('[data-discover-weekly-save]')){const matches=accountReady()?(Storage.get('qingzhou_bookmarks')||[]).filter(row=>row.category==='market_weekly'&&(row.object_kind||row.kind)==='weekly'&&(row.object_id||row.id)===b.dataset.bookmarkId&&!row.time_view&&!row.archive):[],saved=matches.some(row=>String(row.version)===b.dataset.bookmarkVersion);b.disabled=!accountReady()||b.dataset.accountSaving==='true';b.setAttribute('aria-pressed',String(saved));b.textContent=!accountReady()?'正在核对收藏…':saved?'取消收藏':matches.length?'替换为当前版本收藏':'收藏周报';}}
  async function saveWeekly(w,b){try{return await MarketUI.toggleBookmark('weekly',w,b);}catch(error){showToast('收藏未保存：'+error.message);return false;}finally{syncSavedWeeklies();}}
  function weeklyCover(data) { const w = data.report, cover = viewLink(null, 'weekly', { period: w.period, object_kind: 'weekly', object_id: w.period, object_version: w.version, source_scope: 'public_market' }, 'discover-weekly-cover'); cover.dataset.discoveryCover = 'true'; cover.append(node('span', MarketUI.periodLabel(w) + ' · 第 ' + w.version + ' 版', 'discover-meta'), node('h3', w.title)); const items = node('ul'); for (const change of (w.changes || []).slice(0, 3)) items.append(node('li', change.heading)); if (items.childElementCount) cover.append(items); cover.append(node('span', '每周一更新 · 北京时间 · 阅读完整周报 ›', 'discover-cover-read')); return cover; }
  async function adaptWeeklyCover() {
    const root = document.getElementById('weeklyReport'), original = root?.querySelector('.market-weekly-card:not([data-discovery-cover])'); if (!original || coverFlight) return;
    coverFlight = (async () => { try { const data = await MarketUI.get('weeklies/latest'); if (!original.isConnected || data.report?.status !== 'published' || !data.report.source_articles?.every(readable)) return; const version = original.querySelector('.market-weekly-meta')?.textContent; if (version && !version.includes('第' + data.report.version + '版')) return; weeklyHistory = data.history || []; const marketStatus = [...root.children].filter(item => item.matches('[data-weekly-status], .market-weekly-refresh-notice')), archive = root.querySelector('.market-weekly-archive'); root.replaceChildren(...marketStatus, weeklyCover(data), ...(archive ? [archive] : [])); } catch { /* 原有已核验卡片保留，可继续阅读。 */ } finally { coverFlight = null; if (!original.isConnected && root.isConnected && root.querySelector('.market-weekly-card:not([data-discovery-cover])')) queueMicrotask(adaptWeeklyCover); } })(); await coverFlight;
  }
  async function weekly(context, token, epoch) {
    if(context.time_view==='historical_audit')return historical(context,token,epoch);
    title('市场周报', '阅读本期变化、传导条件与原始资料。'); const root = resetSubview(); root.append(node('p', '正在读取已发布完整周报…', 'discover-note'));
    try { const data = await MarketUI.get('weeklies/' + encodeURIComponent(context.period || context.object_id || 'latest')); if (!live(token, epoch)) return; const w = data.report;
      if (!w || w.status !== 'published' || !w.source_articles?.length || !w.source_articles.every(readable)) { error(root, new Error('周报暂无完整已核验版本，原文修订后需重新审核。')); return; }
      root.replaceChildren(action('‹ 返回', () => window.NavrenViews?.back())); const reader = node('article', undefined, 'discover-weekly-reader'); reader.append(node('p', MarketUI.periodLabel(w) + ' · 第 ' + w.version + ' 版 · 北京时间', 'discover-note'), node('h2', w.title)); if (context.object_version != null && String(context.object_version) !== String(w.version)) reader.append(node('p', '所引用版本已变化，下面显示最新已审核版本。', 'discover-note')); reader.append(node('p', w.summary));
      function section(heading, text) { const s = node('section', undefined, 'market-panel-section'); s.append(node('h3', heading)); if (text) s.append(node('p', text)); reader.append(s); return s; }
      for (const c of w.changes || []) { const s = section(c.heading, c.text); if (c.conditions) s.append(node('strong', '关注条件'), node('p', c.conditions)); }
      section('传导与分歧', w.transmission);
      if (w.observations?.length) { const s = section('关键数据对照'); for (const x of w.observations) s.append(node('p', x.label + '：' + x.value + ' ' + x.unit)); }
      if (w.next_week_schedule?.length) { const s = section('下周关注'); for (const x of w.next_week_schedule) { const item = node('div'); item.append(node('strong', x.title + ' · ' + x.date), action('准备日历提醒', () => window.NavrenViews?.ask('请为周报中“' + x.title + '”准备一个日历提醒确认卡。', { kind: 'weekly', id: w.period, version: w.version, source_scope: 'market', label: w.title }))); s.append(item); } }
      const sources = section('原始资料'), unique = [...new Map(w.source_articles.map(a => [a.url || a.id, a])).values()]; for (const a of unique) sources.append(sourceRow(a)); const limits = section('阅读说明'); for (const text of [w.observation_notice, w.schedule_notice, ...(w.limitations || [])].filter(Boolean)) limits.append(node('p', text));
      const controls = node('div', undefined, 'discover-actions'), save = action('收藏周报', () => saveWeekly(w, save));save.dataset.discoverWeeklySave='true';save.dataset.bookmarkKind='weekly';save.dataset.bookmarkId=w.period;save.dataset.bookmarkVersion=String(w.version);controls.append(action('引用到提问', () => marketReference('weekly', w.period, w.version, w.title, w.period)), save); reader.append(controls);
      const history = data.history || weeklyHistory; if (history.length) { const s = section('历史期号'); s.classList.add('discover-weekly-history'); for (const h of history) s.append(viewLink(h.period + ' · 第 ' + h.version + ' 版' + (h.status === 'published' ? '' : ' · 等待修订'), 'weekly', { period: h.period, object_version: h.version, object_kind: 'weekly', object_id: h.period, source_scope: 'public_market', time_view:'historical_audit' })); }
      root.append(reader);syncSavedWeeklies();recordReading('weekly', w.period, w.version, w.title);
    } catch (e) { if (live(token, epoch)) error(root, e, () => render(context)); }
  }
  async function insurance(context, token, epoch) {
    title('保险'); const root = resetSubview(); root.append(node('p', '正在读取保险资料…', 'discover-note'));
    try {
      const head = await readCatalogHead(), rows = await readInsuranceCatalog(head); if (!live(token, epoch)) return;
      root.replaceChildren(); insuranceModule().render(root, rows, { sync_context: syncContext(head) });
    } catch (e) { if (live(token, epoch)) error(root, e, () => refresh(true)); }
  }
  function pauseOfficial() { if (officialTimer) clearTimeout(officialTimer); officialTimer = null; }
  function scheduleOfficial(token, epoch) {
    if (!live(token, epoch)) return;
    pauseOfficial();
    if (!pageActive || document.visibilityState === 'hidden' || !live(token, epoch) || !['home','insurance'].includes(active.view || 'home')) return;
    officialTimer = setTimeout(() => { officialTimer = null; updateOfficial(token, epoch).finally(() => scheduleOfficial(token, epoch)); }, officialInterval);
  }
  let researchRetryToken=null,researchRetryCount=0;
  function researchServiceNotice(host,token,epoch) {
    if (!host || !live(token,epoch) || window.ResearchWorkspace?.probeStatus?.state !== 'unavailable') return;
    host.querySelector('.rw-service-status')?.remove();
    const bar=node('section',undefined,'rw-service-status'),label=node('span','产品资料暂时未能完整读取，已保留可用内容。'),retry=action('重试',async()=>{
      retry.disabled=true;
      try { await window.ResearchWorkspace.available({force:true});if(live(token,epoch))await render(active); }
      finally { if(retry.isConnected)retry.disabled=false; }
    });bar.setAttribute('role','status');bar.append(label,retry);host.prepend(bar);
    const diagnostic=window.ResearchWorkspace.probeStatus;
    if(researchRetryToken!==token){researchRetryToken=token;researchRetryCount=0;}
    if(researchRetryCount<2 && ![401,403].includes(diagnostic.failure?.http_status)) {
      researchRetryCount++;
      setTimeout(async()=>{if(!live(token,epoch)||document.visibilityState==='hidden')return;const ready=await window.ResearchWorkspace.available({force:true});if(!live(token,epoch))return;if(ready){if((active.view||'home')==='home')window.ResearchWorkspace.renderHome({products:document.getElementById('productPreview'),insurance:document.getElementById('insurancePreview'),navigate:open});else render(active);}else researchServiceNotice(host,token,epoch);},1500);
    }
  }
  async function syncOfficialHome(token, epoch) {
    if (!pageActive || document.visibilityState === 'hidden' || !live(token, epoch)) return;
    if (officialFlight?.token === token) return officialFlight.promise;
    const promise = (async () => {
      const s = await B().ensure(); if (!live(token, epoch)) return; session = s;
      if (window.ResearchWorkspace && await window.ResearchWorkspace.available()) {
        if (!live(token, epoch)) return;
        window.ResearchWorkspace.renderHome({products:document.getElementById('productPreview'),insurance:document.getElementById('insurancePreview'),navigate:open});
        return;
      }
      if (!live(token, epoch)) return;
      let head;
      try { head = await readCatalogHead(); } catch (e) { if (!live(token, epoch)) return; head = catalogFailure(); }
      if (!live(token, epoch) || document.visibilityState === 'hidden') return;
      const target = document.getElementById('productPreview'), insuranceTarget = document.getElementById('insurancePreview');
      target?.classList.remove('rw-goals');insuranceTarget?.classList.remove('rw-goals');
      drawCatalogSync(target, head);
      await Promise.allSettled([researchPreview(target, token, epoch, head), (async () => {
        if (!insuranceTarget) return;
        let rows, meta = syncContext(head);
        try { rows = await readInsuranceCatalog(head); } catch (e) { meta = { ...meta, catalog_sync: { ...meta?.catalog_sync, fetch_status: 'failed' } }; rows = null; }
        if (!live(token, epoch) || document.visibilityState === 'hidden') return;
        if (insuranceModule().sync(insuranceTarget, rows, meta)) return;
        const identity = B().identity(), guard = B().guard();
        const reads = await Promise.allSettled([B().get('/data/presentation?view=overview'), B().get('/memories')]);
        if (!live(token, epoch) || !guard.valid()) return;
        const overview = reads[0].status === 'fulfilled' && guard.valid(reads[0].value) ? reads[0].value : null;
        const memories = reads[1].status === 'fulfilled' && guard.valid(reads[1].value) ? { ...reads[1].value, workspace_id: identity.workspace_id, subject_id: identity.account_id, privacy_revision: identity.privacy_revision } : null;
        insuranceModule().preview(insuranceTarget, rows || [], { identity, overview, memories, sync_context: meta });
      })()]);
      researchServiceNotice(target,token,epoch);
    })().finally(() => { if (officialFlight?.promise === promise) officialFlight = null; });
    officialFlight = { token, promise }; return promise;
  }
  async function updateOfficial(token, epoch) {
    if (!pageActive || !live(token, epoch) || document.visibilityState === 'hidden') return;
    if ((active.view || 'home') === 'home') {
      try { return await syncOfficialHome(token, epoch); }
      catch { if (live(token, epoch)) { drawCatalogSync(document.getElementById('productPreview'), catalogFailure(), '本次校对未完成，沿用上次资料'); insuranceModule().sync(document.getElementById('insurancePreview'), null, syncContext(catalogFailure())); } }
    }
    if (active.view === 'insurance' && window.ResearchWorkspace?.configured) return;
    if (active.view === 'insurance') {
      try { const head = await readCatalogHead(), rows = await readInsuranceCatalog(head); if (live(token, epoch) && document.visibilityState !== 'hidden') insuranceModule().sync(document.getElementById('discoverSubview'), rows, syncContext(head)); }
      catch { if (live(token, epoch)) insuranceModule().sync(document.getElementById('discoverSubview'), null, syncContext(catalogFailure())); }
    }
  }
  async function home(token, epoch) {
    title('发现', '了解市场变化，研究产品，再核对你的安排。');
    const restoredId = active.listposition, plansReady = planPreview();
    const marketReady = MarketUI.recommend().then(async () => { if (!live(token, epoch)) return; await adaptWeeklyCover(); if (live(token, epoch) && restoredId) MarketUI.restoreHotspot?.(restoredId); }).catch(() => {});
    const target = document.getElementById('productPreview'), insuranceTarget = document.getElementById('insurancePreview');
    if (!insuranceTarget?.querySelector('.insurance-root')) insuranceTarget?.replaceChildren(node('p', '正在读取保险资料…', 'discover-note'));
    const officialReady = (async () => { try { await syncOfficialHome(token, epoch); } catch (e) { if (live(token, epoch)) { drawCatalogSync(target, catalogFailure(), '本次校对未完成'); if (!target.querySelector('.discover-preview')) error(target, e, () => refresh(true)); } } finally { scheduleOfficial(token, epoch); } })();
    await Promise.allSettled([plansReady,marketReady,officialReady]);
  }
  async function render(context = {}) {
    pauseOfficial(); officialFlight = null;
    active = context; const token = ++generation, epoch = privacyEpoch, view = context.view || 'home'; cancelListReturns();
    document.querySelectorAll('.market-panel-layer[open],.market-dialog[open]').forEach(dialog=>dialog.close());
    const h = document.getElementById('discoverHome'), s = document.getElementById('discoverSubview'); if (!h || !s) return; window.DiscoverInsurance?.clear?.(s); h.hidden = view !== 'home'; s.hidden = view === 'home';
    if(view!=='market_history')window.MarketHistory?.pause();
    if (!['research','public','insurance','product','compare'].includes(view)) window.ResearchWorkspace?.leave?.();
    if (view === 'home') return home(token, epoch);
    if(view==='market_history'){title('历史热点');return window.MarketHistory?.render(context);}
    if (['research','public','insurance','product','compare'].includes(view) && window.ResearchWorkspace?.canRender?.(context) && await window.ResearchWorkspace.available()) {
      if (!live(token, epoch)) return;
      title(view==='insurance'?'保险与保障':view==='compare'?'产品比较':'产品研究');
      return window.ResearchWorkspace.render(context,s,{navigate:open,ask:entity=>window.NavrenViews.ask('请继续核对“'+entity.name+'”的条款、费用及适用条件，先读取这项产品当前的公开资料。',null).catch(e=>showToast(e.message))});
    }
    if (!live(token, epoch)) return;
    window.ResearchWorkspace?.leave?.();
    if (view === 'research' || view === 'public') {await research(context, token, epoch);researchServiceNotice(s,token,epoch);return;}
    if (view === 'insurance') { try { await insurance(context, token, epoch);researchServiceNotice(s,token,epoch);return; } finally { scheduleOfficial(token, epoch); } }
    if (view === 'product') {await product(context, token, epoch);researchServiceNotice(s,token,epoch);return;}
    if (view === 'compare') {await compare(context, token, epoch);researchServiceNotice(s,token,epoch);return;}
    if (view === 'plans' || view === 'plan') return planView(context, token, epoch);
    if (view === 'weekly') return weekly(context, token, epoch);
    if (view === 'reading') return reading(context, token, epoch);
    title('发现', '这个视图不存在，当前资料没有被替换为其他对象。'); error(resetSubview(), new Error('找不到指定视图，请返回发现。'), () => open('home'));
  }
  function refresh(force = false) { if (force) { cancelListReturns(true); catalogue = []; publicCatalogueAvailable = false; catalogueFlight = null; moneyPool = moneyFlight = null; researchPool = researchFlight = null; catalogHead = headFlight = insuranceCatalog = officialFlight = null; } return render(active); }
  function openCurrentPlan(report) { const p = serverPlans[0] || legacyPlans()[0]; return open(p ? 'plan' : 'plans', p ? { object_kind: 'research_plan', object_id: p.id, source_scope: p.source_scope || 'server_research', object_version: p.object_version, filters: { report: !!report } } : {}); }
  function resetPrivacy() { window.ResearchWorkspace?.clear?.(); accountEpoch++;accountWasReady=false;if(active.time_view==='historical_audit')active={view:'home'}; legacyVisible = false; ++privacyEpoch; ++generation; cancelListReturns(true); pauseOfficial(); catalogHead = headFlight = insuranceCatalog = officialFlight = null; document.querySelectorAll('.discover-catalog-sync').forEach(n => n.remove()); catalogue = []; publicCatalogueAvailable = false; moneyPool = moneyFlight = null; researchPool = researchFlight = null; window.DiscoverInsurance?.clear?.(); selected = []; invalidSelection = []; serverPlans = []; session = null; catalogueFlight = null; plansFlight = null; try { sessionStorage.removeItem('navren_research_selection'); } catch {} document.getElementById('planPreview')?.replaceChildren(node('p', '旧账户研究记录已隐藏。', 'discover-note')); document.getElementById('productPreview')?.replaceChildren(node('p', '账户权限已变化，请重新读取资料。', 'discover-note')); document.getElementById('insurancePreview')?.replaceChildren(node('p', '账户权限已变化，请重新读取资料。', 'discover-note')); const root = document.getElementById('discoverSubview'); if (root && !root.hidden) error(root, new Error('旧账户资料已失效，请重新读取。'), () => refresh(true)); }
  document.addEventListener('visibilitychange', () => {
    pauseOfficial(); if (document.visibilityState === 'hidden' || !document.getElementById('discoverHome')) return;
    const token = generation, epoch = privacyEpoch; updateOfficial(token, epoch).finally(() => scheduleOfficial(token, epoch));
  });
  window.addEventListener('pagehide', () => { pageActive = false; cancelListReturns(); pauseOfficial(); });
  window.addEventListener('pageshow', e => { pageActive = true; if (e.persisted) { if (['research','public'].includes(active.view)) { render(active); return; } const token = generation, epoch = privacyEpoch; updateOfficial(token, epoch).finally(() => scheduleOfficial(token, epoch)); } });
  document.addEventListener('DOMContentLoaded', () => {
    if (!document.getElementById('discoverHome')) return;
    window.NavrenViews?.register('recommend', render);
    document.querySelectorAll('[data-discover-view]').forEach(a => a.addEventListener('click', e => { e.preventDefault(); open(a.dataset.discoverView); }));
    const root = document.getElementById('weeklyReport'); if (root) new MutationObserver(() => { if (root.querySelector('.market-weekly-card')) adaptWeeklyCover(); }).observe(root, { childList: true, subtree: false });
    // 接住旧市场组件的显式提问，避免其闭包中的 q 链接泄露问题文本。
    // 只替换当前发现页面的按钮行为，不修改共用市场组件文件。
    document.addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b || !window.NavrenViews?.ask) return;
      if(b.dataset.marketReference==='true')return;
      if (b.dataset.deckFocus === 'explanation') { const slot = b.closest('[data-event-id]'), card = b.closest('.market-deck-card'); if (slot?.dataset.eventId && card?.dataset.version) recordReading('event', slot.dataset.eventId, Number(card.dataset.version), card.querySelector('.market-deck-copy h3')?.textContent || '市场资料',undefined,card.dataset.editionDate?{edition_date:card.dataset.editionDate,revision:Number(card.dataset.editionRevision),event_version:Number(card.dataset.version)}:null); }
      const sheet = b.closest('.market-panel'), legacyAsk = /^(问 Navren|加入财务日历提醒)$/.test(b.textContent.trim());
      if (!legacyAsk || !sheet) return;
      e.preventDefault(); e.stopImmediatePropagation();
      const bookmark = sheet.querySelector('[data-bookmark-id]'), kind = bookmark?.dataset.bookmarkKind || 'event', id = bookmark?.dataset.bookmarkId;
      if (!id) { showToast('资料身份尚未读取完成，请稍后重试。'); return; }
      const version = Number(bookmark.dataset.bookmarkVersion), label = sheet.querySelector('.market-panel-title')?.textContent || '公开市场资料';
      const question = b.textContent.trim() === '加入财务日历提醒' ? '请为周报中“' + (b.closest('.market-schedule')?.querySelector('strong')?.textContent || label) + '”准备日历提醒确认卡。' : '请解读这条已核验市场资料，说明主要变化与条件。';
      sheet.closest('dialog')?.close();
      window.NavrenViews.ask(question, { kind, id, version, source_scope: 'market', label }).catch(err => showToast(err.message));
    }, true);
    // 只在显式跨页提问动作中使用共享一次发送合同；新闻浏览不会调用模型。
    const originalAsk = MarketUI.ask; MarketUI.ask = (kind, id, text, reference) => {
      if(reference?.parameters&&Object.hasOwn(reference.parameters,'reading_mode'))return originalAsk(kind,id,text,reference);
      return window.NavrenViews?.ask ? window.NavrenViews.ask(text, { kind, id, source_scope: 'market', label: '公开市场资料' }) : originalAsk(kind, id, text);
    };
  });
  for(const name of ['navren:privacy-reset','navren:identity-revoked','navren:account-switched'])window.addEventListener(name,resetPrivacy);
  window.addEventListener('navren:account-state',event=>{if(event.detail?.status!=='ready'||!accountReady()){resetPrivacy();syncSavedWeeklies();return;}const restore=!accountWasReady;accountWasReady=true;syncSavedWeeklies();if(restore&&pageActive&&document.body.dataset.page==='recommend')refresh(true).catch(error=>{if(accountReady())showToast(error.message);});});
  return { render, refresh, normalize, select, selected: () => selected.slice(), weeklyCover, adaptWeeklyCover, openCurrentPlan, routeMetadata,recordMarketReading };
})();
