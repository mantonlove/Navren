/** 发现工作区。公开资料与银行业务使用不同身份，浏览不准备交易。 */
window.DiscoverWorkspace = (() => {
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  const action = (text, fn, cls = '') => { const b = node('button', text, 'discover-action ' + cls); b.type = 'button'; b.onclick = fn; return b; };
  const B = () => window.BankClient;
  let generation = 0, privacyEpoch = 0, catalogue = [], selected = [], session = null, catalogueFlight = null, weeklyHistory = [], coverFlight = null, serverPlans = [], plansFlight = null;
  let active = { view: 'home' }, comparisonMode = false, legacyVisible = true, invalidSelection = [];
  const names = { bank_product: '模拟业务产品', public_catalog: '微众官方公开目录', public_nav: '历史管理人资料（已移出产品研究）' };
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
    for (const p of bank || []) {
      if (!p.id) continue;
      const known = Number.isInteger(p.version) && Number.isInteger(p.risk_level) && Number.isInteger(p.term_days) && Number.isInteger(p.min_purchase_minor) && Number.isInteger(p.redemption_delay_days);
      result.push({ id: p.id, source_scope: 'bank_product', version: p.version, name: p.name, raw: p, risk: risk(p.risk_level), manager: '虚构产品，无真实发行人', term: has(p.term_days) ? p.term_days + ' 天锁定' : '期限未披露', minimum: money(p.min_purchase_minor), fees: '未模拟费用，不能推定真实产品免费', liquidity: has(p.redemption_delay_days) ? '赎回后 ' + p.redemption_delay_days + ' 个模拟自然日到账' : '到账规则未披露', returns: '本金记账；未模拟市场收益', as_of: session?.as_of ?? '业务资料日期未知', source: '本地银行业务目录 · 第 ' + value(p.version, '未知') + ' 版', eligibility: p.profile_eligible === true ? '风险与期限已匹配；金额、资金与权限在准备时核对' : (p.exclusion_reasons || []).join('；') || '条件尚未核对', gaps: p.risk_notice || '虚构产品，不对应真实发行方。', executable: p.source === 'simulation' && known && p.profile_eligible === true });
    }
    for (const p of published?.products || []) {
      if (!p.code) continue;
      result.push({ id: p.code, source_scope: 'public_catalog', version: published.catalog_version || published.sha256 || p.version || null, name: p.name, raw: p, risk: risk(p.risk), manager: value(p.publish), term: '期限未披露', minimum: '起购未披露', fees: value(p.charge), liquidity: '申赎与到账规则未披露', returns: has(p.annual_return) ? String(p.annual_return) + '（指标类型/期间待核验）' : '收益指标与期间未披露', as_of: p.source_date || published.source_date, source: '微众银行官方公开目录 · ' + value(p.source_date || published.source_date, '日期未知'), eligibility: value(p.source_date || published.source_date, '日期未知') + ' 快照销售状态：' + value(p.sale_status, '未披露') + '；当前可申购条件未知，仅供资料研究。', gaps: published.stale ? '目录已过期；不作为当前在售或交易依据。' : '目录公开不代表当前在售；关键交易条件尚未核验。', executable: false });
    }
    return [...new Map(result.map(p => [key(p), p])).values()];
  }
  function live(token, epoch) { const current = B().identity?.(); return token === generation && epoch === privacyEpoch && (!active.workspace_id || !session?.workspace_id || active.workspace_id === session.workspace_id) && (!current || !session || current.workspace_id === session.workspace_id && current.privacy_revision === session.privacy_revision); }
  async function loadCatalogue(force = false) {
    if (!force && catalogue.length) return catalogue;
    if (catalogueFlight) return catalogueFlight;
    const epoch = privacyEpoch;
    catalogueFlight = (async () => {
      const s = await B().ensure(); if (epoch !== privacyEpoch) return [];
      session = s;
      const reads = await Promise.allSettled([B().get('/products'), B().get('/public-products?limit=500&offset=0')]);
      if (epoch !== privacyEpoch) return [];
      const bank = reads[0].status === 'fulfilled' ? reads[0].value : [], pub = reads[1].status === 'fulfilled' ? reads[1].value : { products: [] };
      // 分页始终要求同一目录内容版本，避免把中途修订拼接成一份目录。
      const publicRows = [], expected = pub.catalog_version || pub.sha256;
      if (reads[1].status === 'fulfilled') {
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
          if (epoch !== privacyEpoch) return [];
          offset = pageOffset(next, offset);
        }
        if (publicRows.length !== matched) throw new Error('产品目录数量未齐，请重新读取。');
      }
      const normalized = normalize(bank, { ...pub, products: publicRows });
      catalogue = normalized;
      if (reads.every(r => r.status === 'rejected')) throw new Error('产品资料暂时无法读取，请重新读取。');
      return catalogue;
    })().finally(() => { catalogueFlight = null; });
    return catalogueFlight;
  }
  const routeMetadata = p => ({ object_kind: 'product', object_id: p.id, source_scope: p.source_scope, object_version: p.version, as_of: p.as_of });
  function href(view, metadata = {}) {
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
  function preview(p) {
    const card = viewLink(null, 'product', routeMetadata(p), 'discover-preview'); card.id = 'discover-preview-' + p.source_scope + '-' + encodeURIComponent(p.id); card.append(node('span', names[p.source_scope], 'discover-identity'), node('h3', p.name), node('p', p.risk + ' · ' + p.term, 'discover-meta'), node('small', p.source_scope === 'bank_product' ? '本地模拟业务；条件需再次核对' : '公开资料 · 只读', 'discover-note')); return card;
  }
  function title(text, intro) { const h = document.getElementById('discoverHeading'), p = document.getElementById('discoverIntro'), header = document.querySelector('.header-location'); if (h) h.textContent = text; if (p) p.textContent = intro; if (header) header.textContent = text; }
  function resetSubview() { const root = document.getElementById('discoverSubview'); root.replaceChildren(action('‹ 返回', () => window.NavrenViews?.back())); return root; }
  function error(root, e, retry) { root.replaceChildren(node('p', e.message || '资料暂不可用', 'discover-error')); if (retry) root.append(action('重新读取', retry)); }
  function selectionLoad() {
    if (!session?.workspace_id) return;
    try { const data = JSON.parse(sessionStorage.getItem('navren_research_selection') || 'null'); if (data?.workspace_id === session.workspace_id && data.privacy_epoch === privacyEpoch && data.privacy_revision === session.privacy_revision) selected = data.refs.filter(r => r && Object.hasOwn(names, r.source_scope) && typeof r.id === 'string' && r.id.length <= 128 && r.version != null).slice(0, 3); } catch { selected = []; }
    reconcileSelection();
  }
  function reconcileSelection() {
    const valid = selected.filter(s => catalogue.some(p => key(p) === key(s) && String(p.version) === String(s.version)));
    const removed = selected.filter(s => !valid.includes(s));
    invalidSelection = [...new Map([...invalidSelection, ...removed].map(s => [key(s) + ':' + s.version, s])).values()];
    selected = valid;
    if (removed.length) selectionSave();
  }
  function selectionSave() {
    if (!session?.workspace_id) return;
    try { sessionStorage.setItem('navren_research_selection', JSON.stringify({ workspace_id: session.workspace_id, privacy_epoch: privacyEpoch, privacy_revision: session.privacy_revision, refs: selected.map(p => ({ source_scope: p.source_scope, id: p.id, version: p.version })) })); } catch { /* 临时选择仍保留在本页。 */ }
  }
  function select(p, checked) {
    if (checked && p.version == null) { showToast('资料版本尚未核验，暂不能加入比较。'); return false; }
    if (checked && selected.length >= 3 && !selected.some(x => key(x) === key(p))) { showToast('已选 3 款，先移除一款再添加。'); return false; }
    selected = selected.filter(x => key(x) !== key(p)); if (checked) { selected.push({ source_scope: p.source_scope, id: p.id, version: p.version }); invalidSelection = invalidSelection.filter(x => key(x) !== key(p)); } selectionSave(); return true;
  }
  function selectionBar(root, redraw) {
    const bar = node('section', undefined, 'discover-selection'); bar.setAttribute('aria-label', '产品比较选择');
    const count = node('strong', '已选 ' + selected.length + ' / 3'), notice = node('p', selected.length < 2 ? '还需选择 ' + (2 - selected.length) + ' 款；至少 2 款才能比较。' : selected.length === 3 ? '已选满 3 款，可先移除再添加。' : '可开始比较，最多 3 款。', 'discover-note');
    const chips = node('div', undefined, 'discover-actions');
    for (const s of selected) { const p = catalogue.find(x => key(x) === key(s)), label = p?.name || '资料已不可用：' + s.id, stale = !p || String(p.version) !== String(s.version); const b = action(label + (stale ? '（需重核）' : '') + ' ×', () => { select(s, false); redraw(); }, 'discover-chip'); b.setAttribute('aria-label', '移除 ' + label); chips.append(b); }
    const controls = node('div', undefined, 'discover-actions'), compare = action('开始比较', () => open('compare', { selected, filters: active.filters || {} } ), 'primary'); compare.disabled = selected.length < 2;
    controls.append(compare, action('清空', () => { selected = []; invalidSelection = []; selectionSave(); redraw(); }), action('退出比较', () => { comparisonMode = false; redraw(); })); bar.append(count, notice, chips, controls); root.append(bar);
    if (invalidSelection.length) { const invalid = node('aside', undefined, 'discover-error'); invalid.append(node('strong', '以下产品资料已变化或不可用，已退出选择；需重新核对后加入。')); for (const s of invalidSelection) invalid.append(node('p', (names[s.source_scope] || s.source_scope) + ' · ' + s.id + ' · 原版本 ' + value(s.version, '未知'))); root.append(invalid); }
  }
  async function research(context, token, epoch) {
    title('产品研究', '查看资料与具体缺项；默认顺序不代表最适合你的产品。'); const root = resetSubview(); root.append(node('p', '正在核对产品身份与资料日期…', 'discover-note'));
    try { await loadCatalogue(); if (!live(token, epoch)) return; selectionLoad();
      root.replaceChildren(action('‹ 返回', () => window.NavrenViews?.back()));
      if (context.filters?.source === 'public_nav') root.append(node('p', '历史管理人净值已移出产品研究；原来源筛选已失效，下面显示当前可研究的资料。', 'discover-note'));
      const toolbar = node('form', undefined, 'discover-toolbar'), search = field('搜索产品或机构'), source = field('资料身份', 'select', [['', '全部身份'], ['public_catalog', '微众公开目录'], ['bank_product', '模拟业务产品']]), riskFilter = field('风险', 'select', [['', '全部风险'], ['R1', 'R1'], ['R2', 'R2'], ['R3', 'R3'], ['R4', 'R4'], ['R5', 'R5'], ['unknown', '未披露 R 等级']]), termFilter = field('期限', 'select', [['', '全部期限'], ['flex', '无锁定期'], ['short', '90 天及以内'], ['long', '超过 90 天'], ['unknown', '未披露']]), amount = field('起购上限（元）', 'number');
      search.box.classList.add('grow'); search.input.value = context.filters?.q || ''; source.input.value = context.filters?.source || ''; riskFilter.input.value = context.filters?.risk || ''; termFilter.input.value = context.filters?.term || ''; amount.input.value = context.filters?.maximum || ''; amount.input.min = '0';
      const filterButton = action('应用筛选', null); filterButton.type = 'submit'; toolbar.append(search.box, source.box, riskFilter.box, termFilter.box, amount.box, filterButton);
      let visibleLimit = 100;
      const compareMode = action('比较产品', () => { comparisonMode = !comparisonMode; redraw(false); }); compareMode.setAttribute('aria-pressed', comparisonMode); toolbar.append(compareMode);
      const status = node('p', '', 'discover-note'); status.setAttribute('role', 'status'); const results = node('div', undefined, 'discover-results'), bar = node('div'); root.append(toolbar, status, results, bar);
      const filters = () => ({ q: search.input.value.trim(), source: source.input.value, risk: riskFilter.input.value, term: termFilter.input.value, maximum: amount.input.value });
      function redraw(resetLimit = true) {
        if (resetLimit) visibleLimit = 100;
        const f = filters(); results.replaceChildren(); bar.replaceChildren(); compareMode.setAttribute('aria-pressed', comparisonMode);
        const rows = catalogue.filter(p => {
          if (f.q && !(p.name + ' ' + p.manager + ' ' + p.id).toLowerCase().includes(f.q.toLowerCase())) return false;
          if (f.source && p.source_scope !== f.source) return false;
          if (f.risk && (f.risk === 'unknown' ? riskCode(p.risk) !== null : riskCode(p.risk) !== f.risk)) return false;
          const days = p.source_scope === 'bank_product' ? p.raw.term_days : null;
          if (f.term && (f.term === 'unknown' ? days != null : f.term === 'flex' ? days !== 0 : f.term === 'short' ? days == null || days > 90 : days == null || days <= 90)) return false;
          if (f.maximum && (p.source_scope !== 'bank_product' || p.raw.min_purchase_minor > Number(f.maximum) * 100)) return false;
          return true;
        });
        compareMode.textContent = comparisonMode ? '退出比较模式' : selected.length ? '比较产品（已选 ' + selected.length + '）' : '比较产品';
        const shown = rows.slice(0, visibleLimit);
        status.textContent = rows.length + ' 项资料' + (shown.length < rows.length ? '，已显示 ' + shown.length + ' 项' : '') + ' · ' + (Object.values(f).some(Boolean) ? '已应用筛选；未知字段不猜测，金额筛选只含已知起购条件。' : '未应用筛选；公开目录与业务产品分开标明。');
        shown.forEach(p => { const row = node('article', undefined, 'discover-product-row'), choice = node('label', undefined, 'discover-select-cell'), check = node('input'); check.id = 'discover-select-' + p.source_scope + '-' + encodeURIComponent(p.id); check.type = 'checkbox'; check.checked = selected.some(s => key(s) === key(p)); check.setAttribute('aria-label', '比较 ' + p.name); check.disabled = p.version == null || !check.checked && selected.length >= 3; check.onchange = () => { if (!select(p, check.checked)) check.checked = false; redraw(false); }; choice.hidden = !comparisonMode; choice.append(check);
          const a = viewLink(null, 'product', { ...routeMetadata(p), filters: filters() }, 'discover-product-link'); a.id = 'discover-product-' + p.source_scope + '-' + encodeURIComponent(p.id); a.append(node('h3', p.name), node('span', names[p.source_scope], 'discover-identity'), node('p', [p.risk, p.term, '起购 ' + p.minimum, p.liquidity].join(' · '), 'discover-meta')); row.append(choice, a); results.append(row); });
        if (!rows.length) results.append(node('p', '当前条件没有匹配资料。可以清除筛选继续研究。', 'discover-empty'));
        if (shown.length < rows.length) results.append(action('加载更多（还有 ' + (rows.length - shown.length) + ' 项）', () => { if (!live(token, epoch)) return; visibleLimit += 100; redraw(false); }, 'discover-load-more'));
        if (comparisonMode) selectionBar(bar, () => redraw(false));
      }
      toolbar.onsubmit = e => { e.preventDefault(); active = { ...active, filters: filters() }; window.NavrenViews?.update({ filters: filters() }); redraw(); }; redraw();
    } catch (e) { if (live(token, epoch)) error(root, e, () => refresh(true)); }
  }
  const specRows = p => [['产品身份 / 管理人', names[p.source_scope] + '\n' + p.manager], ['风险', p.risk], ['期限', p.term], ['起购', p.minimum], ['费用', p.fees], ['申赎与到账', p.liquidity], ['收益指标类型与期间', p.returns], ['资料日期', date(p.as_of)], ['资料来源', p.source], ['条件与缺项', p.eligibility + '\n' + p.gaps]];
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
    try { await loadCatalogue(); if (!live(token, epoch)) return; const p = lookup(context); if (!p) { error(root, new Error(context.source_scope === 'public_nav' ? '管理人净值资料已移出当前产品研究。历史模拟组合仍可在账户中回看，请返回研究列表查看微众披露。' : '没有找到这份产品资料。可能已删除或目录变化，请返回研究列表。'), () => open('research')); return; }
      root.replaceChildren(action('‹ 返回', () => window.NavrenViews?.back())); const box = node('article', undefined, 'discover-detail'); box.append(node('span', names[p.source_scope], 'discover-identity'), node('h2', p.name), specs(p));
      if (p.source_scope !== 'bank_product') recordReading('public_product', p.id, p.version, p.name, p.source_scope);
      const changed = mismatch(p, context); if (changed) box.prepend(node('p', '所引用版本已变化，下面为当前资料。请重新核对后再选择操作。', 'discover-error'));
      const controls = node('div', undefined, 'discover-actions'), cite = action('引用到提问', () => referenceProduct(p)), add = action(selected.some(s => key(s) === key(p)) ? '已加入比较' : '加入比较', () => { if (select(p, true)) { comparisonMode = true; open('research'); } }); cite.disabled = add.disabled = p.version == null; controls.append(cite, add);
      if (p.version == null) box.append(node('p', '资料缺少可核验版本，暂不能作为比较或对话中的稳定引用。', 'discover-note'));
      if (p.source_scope === 'bank_product') { const buy = action('准备申购', () => purchase(p, box), 'primary'); buy.disabled = changed || !p.executable; controls.append(buy); }
      else box.append(node('p', '这份公开资料只用于查看和比较，不能据此创建申购草案。', 'discover-note'));
      if (p.source_scope !== 'bank_product') box.append(productEvidence(p)); box.append(controls); root.append(box);
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
      const body = node('tbody'), fields = rows.map(specRows); fields[0].forEach(([label], i) => { const row = node('tr'), h = node('th', label); h.scope = 'row'; row.append(h); for (const f of fields) row.append(node('td', f[i][1])); body.append(row); });
      const operations = node('tr'), heading = node('th', '明确操作'); heading.scope = 'row'; operations.append(heading);
      for (const p of rows) { const td = node('td'); td.append(viewLink('查看完整资料', 'product', routeMetadata(p))); if (p.source_scope === 'bank_product') { const buy = action('准备申购', () => purchase(p, root)); buy.disabled = !p.executable; td.append(buy); } else td.append(node('p', '公开资料 · 只读')); operations.append(td); } body.append(operations); table.append(body); wrap.append(table);
      root.append(node('p', rows.length === 3 ? '共 3 款。手机可在表格内横向查看第三款，左侧字段保持对应。' : '手机可在表格内横向查看，左侧字段保持对应。', 'discover-compare-help'), wrap);
    } catch (e) { if (live(token, epoch)) error(root, e, () => refresh(true)); }
  }
  function fingerprint(plan) { let h = 2166136261; for (const c of JSON.stringify(plan)) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); } return 'legacy-' + (h >>> 0).toString(16); }
  function legacyPlans() { if (!legacyVisible) return []; const identity = B().identity?.() || session; if (identity?.workspace_id) { try { const owner = localStorage.getItem('navren_legacy_plan_workspace'); if (owner && owner !== identity.workspace_id) return []; if (!owner) localStorage.setItem('navren_legacy_plan_workspace', identity.workspace_id); } catch { return []; } } const data = Storage.get('qingzhou_allocation'); if (!data?.allocation?.length) return []; return [data, ...(data._history || []).slice().reverse()].map(p => ({ ...p, id: fingerprint({ generated_at: p.generated_at, summary: p.summary, allocation: p.allocation, based_on: p.based_on }), source_scope: 'browser_history' })); }
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
  function recordReading(kind, id, version, label, productSource, archive) { if (!id || version == null) return; const rows = (Storage.get('navren_reading_history') || []).filter(r => !(r.kind === kind && r.id === id)); rows.unshift({ kind, id, version, source_scope: 'public', ...(productSource ? { product_source: productSource } : {}), ...(archive?{archive:{edition_date:archive.edition_date,revision:Number(archive.revision),event_version:Number(archive.event_version)}}:{}), label, updated_at: new Date().toISOString() }); Storage.set('navren_reading_history', rows.slice(0, 20)); }
  function recordMarketReading(item){recordReading('event',item.id,item.archive?.event_version??item.version,item.display_title||item.headline,undefined,item.archive);}
  async function reading(context, token, epoch) {
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
  function saveWeekly(w, b) { const saved = Storage.get('qingzhou_bookmarks') || [], index = saved.findIndex(x => x.object_kind === 'weekly' && x.object_id === w.period && x.version === w.version); if (index >= 0) saved.splice(index, 1); else saved.push({ category: 'market_weekly', object_kind: 'weekly', object_id: w.period, text: w.title, source: 'Navren周报', version: w.version, timestamp: new Date().toISOString() }); Storage.set('qingzhou_bookmarks', saved); b.setAttribute('aria-pressed', index < 0); b.textContent = index < 0 ? '取消收藏' : '收藏周报'; }
  function weeklyCover(data) { const w = data.report, cover = viewLink(null, 'weekly', { period: w.period, object_kind: 'weekly', object_id: w.period, object_version: w.version, source_scope: 'public_market' }, 'discover-weekly-cover'); cover.dataset.discoveryCover = 'true'; cover.append(node('span', MarketUI.periodLabel(w) + ' · 第 ' + w.version + ' 版', 'discover-meta'), node('h3', w.title)); const items = node('ul'); for (const change of (w.changes || []).slice(0, 3)) items.append(node('li', change.heading)); if (items.childElementCount) cover.append(items); if (w.period !== data.expected_period) cover.append(node('p', '本期尚在整理，先阅读最近已发布周报。', 'discover-note')); cover.append(node('span', '每周一更新 · 北京时间 · 阅读完整周报 ›', 'discover-cover-read')); return cover; }
  async function adaptWeeklyCover() {
    const root = document.getElementById('weeklyReport'), original = root?.querySelector('.market-weekly-card:not([data-discovery-cover])'); if (!original || coverFlight) return;
    coverFlight = (async () => { try { const data = await MarketUI.get('weeklies/latest'); if (!original.isConnected || data.report?.status !== 'published' || !data.report.source_articles?.every(readable)) return; const version = original.querySelector('.market-weekly-meta')?.textContent; if (version && !version.includes('第' + data.report.version + '版')) return; weeklyHistory = data.history || []; root.replaceChildren(weeklyCover(data)); } catch { /* 原有已核验卡片保留，可继续阅读。 */ } finally { coverFlight = null; if (!original.isConnected && root.isConnected && root.querySelector('.market-weekly-card:not([data-discovery-cover])')) queueMicrotask(adaptWeeklyCover); } })(); await coverFlight;
  }
  async function weekly(context, token, epoch) {
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
      const controls = node('div', undefined, 'discover-actions'), save = action('收藏周报', () => saveWeekly(w, save)); const saved = (Storage.get('qingzhou_bookmarks') || []).some(x => x.object_kind === 'weekly' && x.object_id === w.period && x.version === w.version); save.textContent = saved ? '取消收藏' : '收藏周报'; save.setAttribute('aria-pressed', saved); controls.append(action('引用到提问', () => marketReference('weekly', w.period, w.version, w.title, w.period)), save); reader.append(controls);
      const history = data.history || weeklyHistory; if (history.length) { const s = section('历史期号'); s.classList.add('discover-weekly-history'); for (const h of history) s.append(viewLink(h.period + ' · 第 ' + h.version + ' 版' + (h.status === 'published' ? '' : ' · 等待修订'), 'weekly', { period: h.period, object_version: h.version, object_kind: 'weekly', object_id: h.period, source_scope: 'public_market' })); }
      root.append(reader); recordReading('weekly', w.period, w.version, w.title);
    } catch (e) { if (live(token, epoch)) error(root, e, () => render(context)); }
  }
  async function home(token, epoch) { title('发现', '了解市场变化，研究产品，再核对你的安排。'); const restoredId=active.listposition;planPreview(); MarketUI.recommend().then(()=>{if(!live(token,epoch))return;adaptWeeklyCover();if(restoredId)MarketUI.restoreHotspot?.(restoredId);}).catch(() => {}); const target = document.getElementById('productPreview'); try { await loadCatalogue(); if (!live(token, epoch)) return; selectionLoad(); target.replaceChildren(); const publicFirst = [...catalogue].sort((a, b) => Number(b.source_scope === 'public_catalog') - Number(a.source_scope === 'public_catalog')); for (const p of publicFirst.slice(0, 3)) target.append(preview(p)); if (!target.childElementCount) target.append(node('p', '暂无可读取产品资料。公开资料不可用时不会使用虚构产品冒充。', 'discover-empty')); } catch (e) { if (live(token, epoch)) error(target, e, () => refresh(true)); } }
  async function render(context = {}) {
    active = context; const token = ++generation, epoch = privacyEpoch, view = context.view || 'home';
    document.querySelectorAll('.market-panel-layer[open],.market-dialog[open]').forEach(dialog=>dialog.close());
    const h = document.getElementById('discoverHome'), s = document.getElementById('discoverSubview'); if (!h || !s) return; h.hidden = view !== 'home'; s.hidden = view === 'home';
    if(view!=='market_history')window.MarketHistory?.pause();
    if (view === 'home') return home(token, epoch);
    if(view==='market_history'){title('历史热点');return window.MarketHistory?.render(context);}
    if (view === 'research' || view === 'public') return research(context, token, epoch);
    if (view === 'product') return product(context, token, epoch);
    if (view === 'compare') return compare(context, token, epoch);
    if (view === 'plans' || view === 'plan') return planView(context, token, epoch);
    if (view === 'weekly') return weekly(context, token, epoch);
    if (view === 'reading') return reading(context, token, epoch);
    title('发现', '这个视图不存在，当前资料没有被替换为其他对象。'); error(resetSubview(), new Error('找不到指定视图，请返回发现。'), () => open('home'));
  }
  function refresh(force = false) { if (force) catalogue = []; return render(active); }
  function openCurrentPlan(report) { const p = serverPlans[0] || legacyPlans()[0]; return open(p ? 'plan' : 'plans', p ? { object_kind: 'research_plan', object_id: p.id, source_scope: p.source_scope || 'server_research', object_version: p.object_version, filters: { report: !!report } } : {}); }
  function resetPrivacy() { legacyVisible = false; ++privacyEpoch; ++generation; catalogue = []; selected = []; invalidSelection = []; serverPlans = []; session = null; catalogueFlight = null; plansFlight = null; try { sessionStorage.removeItem('navren_research_selection'); } catch {} document.getElementById('planPreview')?.replaceChildren(node('p', '旧账户研究记录已隐藏。', 'discover-note')); document.getElementById('productPreview')?.replaceChildren(node('p', '账户权限已变化，请重新读取资料。', 'discover-note')); const root = document.getElementById('discoverSubview'); if (root && !root.hidden) error(root, new Error('旧账户资料已失效，请重新读取。'), () => refresh(true)); }
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
    const originalAsk = MarketUI.ask; MarketUI.ask = (kind, id, text) => window.NavrenViews?.ask ? window.NavrenViews.ask(text, { kind, id, source_scope: 'market', label: '公开市场资料' }) : originalAsk(kind, id, text);
  });
  window.addEventListener('navren:privacy-reset', resetPrivacy);
  window.addEventListener('storage', e => { if (e.key === 'qingzhou_allocation') planPreview(); });
  return { render, refresh, normalize, select, selected: () => selected.slice(), weeklyCover, adaptWeeklyCover, openCurrentPlan, routeMetadata,recordMarketReading };
})();
