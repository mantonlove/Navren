/** 保险资料展示。只接收发现工作区已加载的官方目录，不请求数据或准备交易。 */
window.DiscoverInsurance = (() => {
  const mounts = new Map();
  const words = {
    insurance: ['保险', 'Insurance'], all: ['全部', 'All'], personal: ['个人', 'Individuals'],
    myCoverage: ['我的保障', 'My coverage'], officialCatalog: ['微众官网保险目录', 'WeBank insurance catalogue'], allInsurance: ['全部保险', 'All insurance'], preferences: ['设置保障偏好', 'Set protection interests'],
    checked: ['最近校对', 'Last checked'],
    officialFailed: ['校对失败，沿用上次资料', 'Check failed; previous disclosure retained'],
    officialChecking: ['正在校对官网', 'Checking official disclosure'],
    officialUnavailable: ['官网资料暂未取得', 'Official disclosure unavailable'], officialOrigin: ['微众官网 ↗', 'WeBank source ↗'],
    institution: ['机构', 'Institutions'], unknown: ['未明确', 'Unspecified'],
    source: ['微众官方披露', 'WeBank official disclosure'], date: ['披露日期', 'Disclosure date'],
    multipleDates: ['多期披露', 'Multiple disclosure dates'], missing: ['未披露', 'Not disclosed'],
    current: ['当前销售状态待核验', 'Current availability unverified'],
    back: ['‹ 返回', '‹ Back'], search: ['搜索保险名称、公司或编码', 'Search insurance, insurer or code'],
    view: ['查看资料', 'View details'], empty: ['暂无可读取的保险资料', 'No insurance disclosures available'],
    noMatch: ['没有匹配的保险资料', 'No matching insurance disclosures'],
    reset: ['清除筛选', 'Clear filters'], insurer: ['保险公司', 'Insurer'],
    audience: ['适用客户', 'Customer eligibility'], requirements: ['投保 / 投资者要求', 'Applicant / investor requirements'],
    code: ['产品编码', 'Product code'], information: ['保险信息', 'Insurance information'],
    costs: ['费用与风险', 'Fees and risk'], fees: ['费用说明', 'Fee disclosure'],
    chargeWay: ['收费方式', 'Charging method'], risk: ['官方风险原文', 'Official risk disclosure'],
    status: ['快照销售状态', 'Sales status in this snapshot'], official: ['官方原文 ↗', 'Official source ↗'],
    ask: ['问 Navren', 'Ask Navren'], noVersion: ['目录版本未取得，暂不能引用到提问。', 'Catalogue version unavailable; reference is disabled.'],
    failed: ['页面暂时无法打开，请重试。', 'Unable to open this page. Please try again.'],
    items: ['项资料', 'disclosures'], unnamed: ['保险资料', 'Insurance disclosure'],
    health: ['健康与医疗', 'Health and medical'], property: ['房产保障', 'Property protection'],
    agriculture: ['农业保障', 'Agricultural protection'], equipment: ['设备保障', 'Equipment protection'],
    liability: ['经营责任', 'Business liability'], pension: ['养老规划', 'Retirement planning'], accident: ['意外保障', 'Accident protection'],
    adjust: ['调整需求', 'Adjust interests'], interests: ['你关注的保障方向', 'Your protection interests'],
    choose: ['选择最多 3 项，按选择顺序展示', 'Choose up to 3, shown in selection order'],
    apply: ['保存', 'Save'], cancel: ['取消', 'Cancel'], automatic: ['按已有资料展示', 'Use existing records'],
    selected: ['你的关注', 'Your interest'], memory: ['已确认需求', 'Confirmed interest'], records: ['已有保障', 'Existing coverage'],
    setup: ['补充你的保障需求', 'Choose your protection interests'], setupNote: ['选择关注方向，让这里更贴合你的需求。', 'Choose the protection topics that matter to you.'],
    policyCount: ['份已接入保单', 'linked policies'], checkCoverage: ['查看保障', 'View coverage'], research: ['相关保险', 'Related insurance'],
    chosenNote: ['根据你选择的关注方向展示', 'Based on your selected interests'], memoryNote: ['根据本人已确认的需求展示', 'Based on your confirmed interests'],
    unrecorded: ['尚未取得该方向的保单记录', 'No policy records retrieved for this topic'], unavailable: ['保单资料暂未取得', 'Policy records unavailable'],
    noProduct: ['暂无该方向公开资料', 'No public disclosures for this topic'], policyList: ['相关保单', 'Related policies'],
    close: ['收起', 'Close'], localOnly: ['仅本次有效，浏览器未能保存。', 'Applies for this visit; browser storage unavailable.'],
    object: ['保障对象', 'Protection subject'], review: ['核对保障责任与保障期限', 'Review coverage and policy dates'], policyAsOf:['保单资料截至','Policy records as of'],
    help: ['展示依据', 'Display basis'], helpNote: ['优先展示你选择或确认的需求，其次整理已有保单。方向与公开资料不代表适合投保；可在详情核对保障责任与条件。', 'Your selected or confirmed interests come first, followed by existing policies. Topics and public disclosures do not establish suitability; check coverage and eligibility in the details.']
  };
  const english = () => window.NavrenI18n?.language === 'en';
  const t = key => words[key][english() ? 1 : 0];
  const text = value => value === null || value === undefined || String(value).trim() === '' ? t('missing') : String(value);
  const node = (tag, value, cls) => { const n = document.createElement(tag); if (value != null) n.textContent = value; if (cls) n.className = cls; return n; };
  const original = (tag, value, cls) => { const n = node(tag, text(value), cls); n.setAttribute('translate', 'no'); return n; };
  const validRow = p => p && p.source_scope === 'public_catalog' && p.raw?.type === '保险' && typeof p.id === 'string' && !!p.id.trim();
  const rowsOnly = rows => Array.isArray(rows) ? [...new Map(rows.filter(validRow).map(p => [p.id, p])).values()] : [];
  const active = record => mounts.get(record.host) === record && record.host.isConnected && (!record.box || record.box.parentElement === record.host && record.box.isConnected) && (!record.context?.identity || ['workspace_id','account_id','privacy_revision'].every(k => record.context.identity[k] === window.BankClient?.identity?.()?.[k]));
  const metadata = p => ({ object_kind: 'product', object_id: p.id, source_scope: 'public_catalog', object_version: p.version });
  function audience(p) {
    // 只认明确披露的客户类型；不从产品名、公司或风险态度推断。
    const value = String(p.raw.customer_group_type || '').trim();
    if (['符合资质的个人投资者', '个人', '个人客户', '个人投资者'].includes(value)) return 'personal';
    if (['符合资质的机构投资者', '机构', '机构客户', '机构投资者'].includes(value)) return 'institution';
    return 'unknown';
  }
  function shield() {
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('viewBox', '0 0 24 24'); icon.setAttribute('aria-hidden', 'true'); icon.setAttribute('focusable', 'false');
    icon.innerHTML = '<path d="M12 3 4 6v5c0 5 3.5 8 8 10 4.5-2 8-5 8-10V6z"/><path d="m8.5 12 2.3 2.3 4.7-4.7"/>';
    return icon;
  }
  function button(label, callback, cls = '') {
    const b = node('button', label, 'insurance-button ' + cls); b.type = 'button'; b.onclick = callback; return b;
  }
  function fail(error) { if (typeof window.showToast === 'function') window.showToast(error?.message || t('failed')); }
  function openProduct(record, p, event) {
    if (!active(record) || !event.currentTarget.isConnected) { event.preventDefault(); return; }
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || !window.NavrenViews?.open) return;
    event.preventDefault(); Promise.resolve(window.NavrenViews.open('product', metadata(p))).catch(fail);
  }
  function productHref(p) {
    if (window.NavrenViews?.url) return window.NavrenViews.url({ page: 'recommend', view: 'product', ...metadata(p) });
    const u = new URL('recommend.html', location.href); u.searchParams.set('view', 'product');
    for (const [key, value] of Object.entries(metadata(p))) if (value != null) u.searchParams.set(key, value);
    return window.NavrenApp?.href ? window.NavrenApp.href(u.pathname + u.search) : u.pathname + u.search;
  }
  function card(record, p) {
    const a = node('a', null, 'insurance-card'); a.href = productHref(p); a.setAttribute('aria-label', t('view') + ' · ' + (p.name || p.id));
    a.onclick = event => openProduct(record, p, event);
    const top = node('div', null, 'insurance-card-top'), emblem = node('span', null, 'insurance-emblem'); emblem.append(shield());
    top.append(emblem, node('span', t(audience(p)), 'insurance-audience'));
    const foot = node('div', null, 'insurance-card-foot'), arrow = node('span', '›', 'insurance-arrow'); arrow.setAttribute('aria-hidden', 'true');
    foot.append(original('span', p.raw.code || p.id, 'insurance-code'), arrow);
    a.append(top, original('h3', p.name || t('unnamed')), original('p', p.raw.publish, 'insurance-company'), foot); return a;
  }
  function sourceLine(rows) {
    const dates = [...new Set(rows.map(p => p.raw.source_date || p.as_of).filter(v => /^\d{4}-\d{2}-\d{2}$/.test(String(v))))].sort();
    const date = dates.length === 1 ? dates[0] : dates.length ? t('multipleDates') : t('missing');
    return t('source') + ' · ' + date + ' · ' + t('current');
  }
  function syncLine(record) {
    const meta = record.context?.sync_context, line = node('div', null, 'insurance-catalog-sync');
    const checked = meta?.catalog_sync?.checked_at;
    const when = typeof checked === 'string' && Number.isFinite(Date.parse(checked)) ? new Date(checked).toLocaleString(english() ? 'en-GB' : 'zh-CN', {timeZone:'Asia/Shanghai',hour12:false}) : t('missing');
    line.append(node('span', t('checked')+' '+when));
    const a = node('a', t('officialOrigin')); a.href = 'https://www.webank.com/inquiryCenter/query'; a.target = '_blank'; a.rel = 'noopener noreferrer'; line.append(a);
    if (!['ok','unchanged'].includes(meta?.catalog_sync?.fetch_status)) line.append(node('span', t(meta?.catalog_sync?.fetch_status === 'fetching' ? 'officialChecking' : meta?.catalog_version ? 'officialFailed' : 'officialUnavailable'), meta?.catalog_sync?.fetch_status === 'fetching' ? 'insurance-source' : 'insurance-sync-warning'));
    return line;
  }
  function publicDirectory(record) {
    const box = node('div', null, 'insurance-public-directory');
    const all = node('a', t('allInsurance')+' ›', 'insurance-all-link');
    all.href = window.NavrenViews?.url?.({page:'recommend',view:'insurance'}) || 'recommend.html?view=insurance';
    all.onclick = e => { if(!active(record)){e.preventDefault();return;} if(e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)return; e.preventDefault(); Promise.resolve(window.NavrenViews?.open('insurance')).catch(fail); };
    box.append(syncLine(record), all); return box;
  }
  function clear(host) {
    if (!host || host.id === 'insurancePreview') document.getElementById('insuranceHelpHost')?.replaceChildren();
    if (host) { const record = mounts.get(host); mounts.delete(host); if (record) host.replaceChildren(); return; }
    for (const target of mounts.keys()) target.replaceChildren(); mounts.clear();
  }
  function mount(host, type, products) {
    if (!host?.replaceChildren) return null;
    clear(host); const record = { host, type, rows: rowsOnly(products), query: '', audience: 'all' };
    mounts.set(host, record); return record;
  }
  function root(record, cls) {
    // 语言重绘只更新模块自身，保留工作区在模块之后追加的版本变更提示。
    if (!record.box || record.box.parentElement !== record.host) {
      record.box = node('div', null, 'insurance-root ' + cls); record.box.dataset.i18nSkip = ''; record.host.replaceChildren(record.box);
    } else record.box.replaceChildren();
    return record.box;
  }
  const topics = ['health','property','agriculture','equipment','liability','pension','accident'];
  function readPreferences(record) {
    if (!Storage.accountReady()) return [];
    if (window.NavrenInsurancePreferences) return window.NavrenInsurancePreferences.read(record.context?.identity);
    const value = Storage.get('navren_insurancePreferences');
    return Array.isArray(value) ? value.filter(key => topics.includes(key)) : [];
  }
  function preferencesLink() {
    const a = node('a', t('preferences'), 'insurance-button insurance-need-cta');
    const path = 'mine.html?view=home&focustarget=mineInsurancePreferences';
    a.href = window.NavrenApp?.href ? window.NavrenApp.href(path) : path; return a;
  }
  function topicIcon(key) {
    const icon = shield();
    const paths = {health:'<path d="M12 21s-8-5-8-11a4 4 0 0 1 8-2 4 4 0 0 1 8 2c0 6-8 11-8 11Z"/><path d="M12 9v6m-3-3h6"/>',property:'<path d="m3 10 9-7 9 7M5 9v12h14V9M10 21v-7h4v7"/>',agriculture:'<path d="M12 21V5m0 8c-6 0-8-3-8-7 5 0 8 2 8 7Zm0 4c6 0 8-3 8-7-5 0-8 2-8 7Z"/>',equipment:'<path d="m14 3 3 3-7 7-3-3ZM4 17l3-4 4 4-4 4H4Zm11-5 6 6-3 3-6-6"/>',liability:'<path d="M4 7h16v13H4ZM9 7V4h6v3M4 12h16m-10 0v3h4v-3"/>',pension:'<path d="M6 21V8a6 6 0 0 1 12 0M3 21h6M18 8h3"/>',accident:'<path d="m7 3 14 14-4 4L3 7ZM9 13l4-4m-3 2h.01m3 3h.01"/>',setup:'<path d="M12 4v16M4 12h16"/>'};
    icon.innerHTML = paths[key] || icon.innerHTML; return icon;
  }
  function policyLink(record, policy) {
    const envelope = record.context.overview, metadata = {object_kind:'policy',object_id:policy.policy_id,source_scope:'financial_history',object_version:envelope.dataset_version};
    const contract = envelope.data.contracts.find(c => c.contract_id === policy.contract_id);
    const asset = envelope.data.assets.find(a => a.asset_id === policy.covered_asset_id);
    const a = original('a', asset?.name || contract?.name || t(record.focusKey), 'insurance-policy-link');
    a.href = window.NavrenViews?.url?.({page:'account',view:'record-detail',...metadata}) || '#';
    a.onclick = e => { if (!active(record)) { e.preventDefault(); return; } if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || !window.NavrenViews?.open) return; e.preventDefault(); Promise.resolve(window.NavrenViews.open('record-detail',{page:'account',...metadata})).catch(fail); };
    const date = node('small', [policy.start_date,policy.end_date].filter(Boolean).join(' — ')); a.append(date,node('span','›','insurance-arrow')); return a;
  }
  function directionCard(record, item) {
    const box = node('article',null,'insurance-need-card'); box.dataset.need = item.key;
    const top = node('div',null,'insurance-card-top'), emblem = node('span',null,'insurance-emblem'); emblem.append(topicIcon(item.key));
    top.append(emblem,node('span',item.key === 'setup' ? '' : t(item.reasonKind==='records' && !item.policies?.length ? 'object' : item.reasonKind), 'insurance-need-badge'));
    box.append(top,node('h3',t(item.key)));
    if (item.key === 'setup') {
      box.append(node('p',t('setupNote'),'insurance-need-reason'),preferencesLink());
      return box;
    }
    const count = item.policies?.length || 0;
    const metric = node('p',null,'insurance-need-metric');
    if (count) metric.append(node('strong',String(count)),node('span',t('policyCount')));
    else metric.append(node('span',record.selection.dataAvailable ? t('unrecorded') : t('unavailable')));
    box.append(metric);
    let reason = item.reasonKind === 'selected' ? t('chosenNote') : item.reasonKind === 'memory' ? t('memoryNote') : (item.assets || []).map(a=>a.name).slice(0,2).join(' · ');
    if (!reason) reason = t('review');
    box.append(original('p',reason,'insurance-need-reason'));
    const foot = node('div',null,'insurance-need-foot');
    if (count) { const show = button(t('checkCoverage')+' ›',() => { if(!active(record))return; record.focusKey = record.focusKey === item.key ? null : item.key; record.editing=false; drawPreview(record); },'insurance-text-action'); show.setAttribute('aria-expanded',String(record.focusKey===item.key)); foot.append(show); }
    if (item.productIds?.length) foot.append(button(t('research')+' ›',() => { if(!active(record))return; Promise.resolve(window.NavrenViews?.open('insurance',{filters:{insurance_need:item.key,insurance_audience:'personal'}})).catch(fail); },'insurance-text-action'));
    else foot.append(node('span',t('noProduct'),'insurance-no-product'));
    box.append(foot); return box;
  }
  function drawPreview(record) {
    if(!active(record)){if(mounts.get(record.host)===record)clear(record.host);return;}
    const box=root(record,'insurance-preview');
    record.selection = window.NavrenInsuranceNeeds?.build({...record.context,priorities:record.priorities,products:record.rows});
    if(!record.selection){box.append(node('p',t('failed'),'insurance-empty'));return;}
    const help=node('details',null,'insurance-help'), summary=node('summary','?'); summary.setAttribute('aria-label',t('help'));help.append(summary,node('p',t('helpNote')));
    const helpHost = record.host.id === 'insurancePreview' && document.getElementById('insuranceHelpHost');
    if (helpHost) helpHost.replaceChildren(help); else box.append(help);
    box.append(publicDirectory(record));
    if(record.selection.asOf)help.lastChild.append(node('small',t('policyAsOf')+' '+record.selection.asOf));
    const grid=node('div',null,'insurance-grid insurance-needs-grid');record.selection.items.forEach(item=>grid.append(directionCard(record,item)));box.append(grid);
    const focus=record.selection.items.find(item=>item.key===record.focusKey);
    if(focus?.policies?.length){const panel=node('section',null,'insurance-policy-panel');panel.setAttribute('aria-label',t('policyList'));const heading=node('div',null,'insurance-editor-heading');heading.append(node('h3',t(focus.key)+' · '+t('policyList')),button(t('close'),()=>{if(!active(record))return;record.focusKey=null;drawPreview(record);},'insurance-text-action'));panel.append(heading);focus.policies.forEach(p=>panel.append(policyLink(record,p)));box.append(panel);}
    if(record.selection.error)box.append(node('p',t('unavailable'),'insurance-source'));
    if(record.storageFailed)box.append(node('p',t('localOnly'),'insurance-source'));
  }
  function saveFilters(record) {
    const current = window.NavrenViews?.current?.();
    if (current?.view === 'insurance') window.NavrenViews.update({ filters: { ...current.filters, insurance_query: record.query, insurance_audience: record.audience, insurance_need:record.need || '' } });
  }
  function drawList(record) {
    const oldInput = record.host.querySelector('input[type="search"]'), focused = document.activeElement === oldInput, position = oldInput?.selectionStart;
    const box = root(record, 'insurance-list');
    box.append(button(t('back'), () => { if (active(record)) window.NavrenViews?.back(); }, 'insurance-back'));
    const heading = node('header', null, 'insurance-list-heading'); heading.append(node('h2', t('officialCatalog')), syncLine(record), node('p', sourceLine(record.rows), 'insurance-source')); box.append(heading);
    if(record.need)heading.append(button(t(record.need)+' · '+t('reset'),()=>{if(!active(record))return;record.need=null;saveFilters(record);drawList(record);},'insurance-filter insurance-need-filter'));
    const toolbar = node('div', null, 'insurance-toolbar'), tabs = node('div', null, 'insurance-filters'); tabs.setAttribute('role', 'group'); tabs.setAttribute('aria-label', t('audience'));
    const search = node('input'); search.type = 'search'; search.maxLength = 160; search.value = record.query; search.placeholder = t('search'); search.setAttribute('aria-label', t('search'));
    const label = node('label', null, 'insurance-search'); label.append(node('span', t('search'), 'sr-only'), search);
    const status = node('p', null, 'insurance-count'); status.setAttribute('role', 'status');
    const results = node('div', null, 'insurance-grid insurance-list-results'), filters = [];
    const redraw = () => {
      if (!active(record)) return;
      const query = record.query.toLocaleLowerCase();
      const shown = record.rows.filter(p => (!record.need || window.NavrenInsuranceNeeds?.productCategory(p) === record.need) && (record.audience === 'all' || audience(p) === record.audience) && (!query || [p.name, p.raw.publish, p.raw.code || p.id].join(' ').toLocaleLowerCase().includes(query)));
      for (const [key, b] of filters) b.setAttribute('aria-pressed', String(key === record.audience));
      status.textContent = shown.length + ' ' + t('items'); results.replaceChildren(); shown.forEach(p => results.append(card(record, p)));
      if (!shown.length) {
        const empty = node('div', null, 'insurance-empty'); empty.append(node('p', record.rows.length ? t('noMatch') : t('empty')));
        if (record.rows.length) empty.append(button(t('reset'), () => { if (!active(record)) return; record.need=null;record.query = ''; record.audience = 'all'; search.value = ''; saveFilters(record); redraw(); search.focus(); }));
        results.append(empty);
      }
    };
    for (const key of ['all', 'personal', 'institution', 'unknown']) {
      const count = key === 'all' ? record.rows.length : record.rows.filter(p => audience(p) === key).length;
      const b = button(t(key) + ' ' + count, () => { if (!active(record)) return; record.audience = key; saveFilters(record); redraw(); }, 'insurance-filter');
      filters.push([key, b]); tabs.append(b);
    }
    search.oninput = () => { if (!active(record)) return; record.query = search.value.trim(); saveFilters(record); redraw(); };
    toolbar.append(tabs, label); box.append(toolbar, status, results); redraw();
    if (focused) { search.focus({ preventScroll: true }); if (position != null) search.setSelectionRange(position, position); }
  }
  function disclosureSection(heading, fields) {
    const section = node('section', null, 'insurance-disclosure'); section.append(node('h3', heading));
    const list = node('dl'); for (const [key, value] of fields) list.append(node('dt', t(key)), original('dd', value)); section.append(list); return section;
  }
  function officialLink(url) {
    try {
      const parsed = new URL(url); if (parsed.protocol !== 'https:' || parsed.username || parsed.password) return null;
      const a = node('a', t('official'), 'insurance-button insurance-source-link'); a.href = parsed.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;
    } catch { return null; }
  }
  function drawDetail(record) {
    const p = record.rows[0], box = root(record, 'insurance-detail');
    box.append(button(t('back'), () => { if (active(record)) window.NavrenViews?.back(); }, 'insurance-back'));
    if (!p) { box.append(node('p', t('empty'), 'insurance-empty')); return; }
    const raw = p.raw, article = node('article', null, 'insurance-detail-card'), header = node('header', null, 'insurance-detail-heading');
    const emblem = node('span', null, 'insurance-emblem insurance-detail-emblem'); emblem.append(shield());
    const identity = node('div', null, 'insurance-detail-identity'); identity.append(node('span', t('source') + ' · ' + t(audience(p)), 'insurance-eyebrow'), original('h2', p.name || t('unnamed')), original('p', raw.publish, 'insurance-company')); header.append(emblem, identity);
    const grid = node('div', null, 'insurance-detail-grid');
    grid.append(disclosureSection(t('information'), [['insurer', raw.publish], ['audience', raw.customer_group_type], ['requirements', raw.investor], ['code', raw.code || p.id]]), disclosureSection(t('costs'), [['fees', raw.charge], ['chargeWay', raw.charge_way], ['risk', raw.risk]]));
    const provenance = node('div', null, 'insurance-provenance');
    provenance.append(syncLine(record), node('p', sourceLine(record.rows), 'insurance-source'), node('p', t('status') + ' · ' + text(raw.sale_status), 'insurance-snapshot-status'));
    const controls = node('div', null, 'insurance-detail-actions'), link = officialLink(raw.source_url);
    if (link) controls.append(link);
    const ask = button(t('ask'), () => {
      if (!active(record) || !ask.isConnected || ask.disabled || !window.NavrenViews?.ask) return;
      record.askPending = true; ask.disabled = true;
      Promise.resolve(window.NavrenViews.ask('', { kind: 'product', id: p.id, source_scope: 'public_catalog', version: p.version, label: p.name }))
        .catch(fail).finally(() => { record.askPending = false; const current = record.box.querySelector('.insurance-primary'); if (active(record) && current) current.disabled = p.version == null || p.version === ''; });
    }, 'insurance-primary');
    const missingVersion = p.version == null || p.version === '';
    ask.disabled = missingVersion || record.askPending === true; controls.append(ask);
    article.append(header, grid, provenance, controls);
    if (missingVersion) article.append(node('p', t('noVersion'), 'insurance-source'));
    box.append(article);
  }
  function preview(host, rows, context = {}) { const record = mount(host, 'preview', rows); if(record){record.context=context; if(Storage.accountReady()){record.priorities=readPreferences(record);drawPreview(record);}else{record.box.replaceChildren(node('p','正在恢复账号偏好…'));Storage.ready().then(()=>{if(active(record)){record.priorities=readPreferences(record);drawPreview(record);}}).catch(()=>{if(active(record))record.box.replaceChildren(node('p','账号偏好暂未读取，请刷新重试。'));});}} }
  function render(host, rows, context = {}) {
    const record = mount(host, 'list', rows); if (!record) return;
    record.context = context;
    const filters = window.NavrenViews?.current?.()?.filters || {};
    record.query = typeof filters.insurance_query === 'string' ? filters.insurance_query.slice(0, 160).trim() : '';
    if (['all', 'personal', 'institution', 'unknown'].includes(filters.insurance_audience)) record.audience = filters.insurance_audience;
    if(topics.includes(filters.insurance_need))record.need=filters.insurance_need;
    drawList(record);
  }
  function detail(host, p, context = {}) { const record = mount(host, 'detail', [p]); if (record) { record.context = context; drawDetail(record); } }
  function sync(host, rows, sync_context) {
    const record = mounts.get(host); if (!record || !active(record)) return false;
    const changed = record.context?.sync_context?.catalog_version !== sync_context?.catalog_version;
    record.context = { ...record.context, sync_context };
    if (Array.isArray(rows)) record.rows = rowsOnly(rows);
    else if (changed) record.rows = [];
    // 同版本只替换同步行，保留展开详情、需求草稿和焦点。
    const line = record.box?.querySelector('.insurance-catalog-sync'); if (line) line.replaceWith(syncLine(record));
    if (changed && record.type === 'preview') {
      const focused = record.box?.contains(document.activeElement) ? document.activeElement : null;
      const need = focused?.dataset.need, label = focused?.textContent;
      // 换版重算公开产品关联；保单、展开方向和尚未保存的需求草稿仍属于原账户。
      drawPreview(record);
      const controls = [...record.box.querySelectorAll('button')];
      const replacement = need ? controls.find(b => b.dataset.need === need) : controls.find(b => b.textContent === label);
      if (focused) replacement?.focus({preventScroll:true});
    }
    if (record.type === 'list' && changed) drawList(record);
    return true;
  }
  window.addEventListener('navren:language', () => {
    for (const record of mounts.values()) {
      if (!active(record)) { mounts.delete(record.host); continue; }
      ({ preview: drawPreview, list: drawList, detail: drawDetail })[record.type](record);
    }
  });
  function refreshPreferences() {
    for (const record of mounts.values()) {
      if (record.type !== 'preview' || !active(record)) continue;
      const priorities = readPreferences(record);
      if (JSON.stringify(priorities) === JSON.stringify(record.priorities)) continue;
      record.priorities = priorities; record.focusKey = null; drawPreview(record);
    }
  }
  window.addEventListener('navren:insurance-preferences-updated', refreshPreferences);
  window.addEventListener('pageshow', refreshPreferences);
  window.addEventListener('navren:account-state', () => {
    if (Storage.accountReady()) refreshPreferences();
    else for (const record of mounts.values()) if (record.type === 'preview') { record.priorities = []; record.box.replaceChildren(node('p','正在恢复账号偏好…')); }
  });
  for(const event of ['navren:privacy-reset','navren:identity-revoked','navren:account-switched','pagehide'])window.addEventListener(event, () => clear());
  return { preview, render, detail, clear, sync };
})();
