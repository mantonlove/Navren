/* 独立财务历史的只读入口。历史对象不会转换成现行执行账本 ID。 */
window.NavrenFinancialData = (() => {
  let generation = 0, owner = '', version = null;
  const cache = new Map(), scope = 'financial_history';
  function stale() { const e = Error('账户或资料权限已变化，请重新读取。'); e.code = 'STALE_FINANCIAL_DATA'; return e; }
  function clear() { generation++; owner = ''; version = null; cache.clear(); }
  for (const event of ['navren:privacy-reset', 'navren:identity-revoked', 'navren:account-switched', 'pagehide']) window.addEventListener(event, clear);
  function validate(value, identity) {
    if (!value || value.schema_version !== 'navren.financial.v1' || value.source !== 'navren_synthetic' ||
        value.read_only !== true || value.runtime_ledger_included !== false || value.workspace_binding !== identity.workspace_id ||
        value.subject_id !== identity.account_id || !value.dataset_version || value.data == null ||
        !/^\d{4}-\d{2}-\d{2}$/.test(value.as_of || '')) throw Error('独立财务资料的身份、来源或版本无法核对。');
    function walk(item) {
      if (!item || typeof item !== 'object') return;
      if (item.subject_id != null && item.subject_id !== value.subject_id) throw Error('资料含其他主体对象，已停止展示。');
      for (const [key, child] of Object.entries(item)) {
        if (key.endsWith('_minor') && child != null && !Number.isSafeInteger(child)) throw Error('金额不是有效的整数分，已停止展示。');
        if (child && typeof child === 'object') walk(child);
      }
    }
    walk(value.data);
    return value;
  }
  async function read(operation, parameters = {}, {fresh = false} = {}) {
    await BankClient.ensure();
    const identity = BankClient.identity(), guard = BankClient.guard();
    if (!identity?.account_id || !guard.valid()) throw stale();
    const keyOwner = identity.workspace_id + ':' + identity.privacy_revision;
    if (owner !== keyOwner) { clear(); owner = keyOwner; }
    const epoch = generation, query = new URLSearchParams(parameters).toString(), key = operation + '?' + query;
    if (fresh) cache.delete(key);
    let pending = cache.get(key);
    if (!pending) {
      pending = BankClient.get('/data/' + operation + (query ? '?' + query : ''));
      if (cache.size >= 24) cache.delete(cache.keys().next().value);
      cache.set(key, pending);
    }
    try {
      const response = await pending;
      if (epoch !== generation || !guard.valid(response)) throw stale();
      validate(response, identity);
      if (version != null && version !== response.dataset_version) { clear(); throw Error('独立财务库版本已变化，请重新读取。'); }
      version = response.dataset_version;
      return response;
    } catch (error) { if (cache.get(key) === pending) cache.delete(key); throw error; }
  }
  function associations(envelope) {
    const data = envelope.data, index = {};
    for (const [table, field] of [['connections','connection_id'], ['accounts','account_id'], ['cards','card_id'], ['contracts','contract_id'], ['credit_facilities','facility_id'], ['assets','asset_id'], ['positions','position_id'], ['policies','policy_id'], ['obligations','obligation_id']]) {
      if (!Array.isArray(data[table])) throw Error('独立财务目录未完整取得：' + table);
      index[table] = new Map(data[table].map(row => [row[field], row]));
      if (index[table].size !== data[table].length || data[table].some(row => !row[field])) throw Error('财务对象编号重复或缺失。');
    }
    const check = (rows, field, target, optional = false) => rows.forEach(row => {
      if (optional && row[field] == null) return;
      if (!index[target].has(row[field])) throw Error('财务对象关联未取得：' + field);
    });
    check(data.accounts, 'connection_id', 'connections');
    check(data.cards, 'account_id', 'accounts'); check(data.cards, 'facility_id', 'credit_facilities', true);
    check(data.credit_facilities, 'account_id', 'accounts');
    check(data.positions, 'account_id', 'accounts'); check(data.positions, 'asset_id', 'assets'); check(data.positions, 'contract_id', 'contracts');
    check(data.policies, 'payment_account_id', 'accounts'); check(data.policies, 'insurer_connection_id', 'connections');
    check(data.policies, 'contract_id', 'contracts'); check(data.policies, 'covered_asset_id', 'assets', true);
    check(data.obligations, 'account_id', 'accounts');
    for (const card of data.cards) if (card.facility_id && index.credit_facilities.get(card.facility_id).account_id !== card.account_id) throw Error('卡片与授信不属于同一账户。');
    for (const position of data.positions) {
      const contract = index.contracts.get(position.contract_id);
      if (contract.account_id !== position.account_id || contract.asset_id !== position.asset_id) throw Error('持仓与合同关联不一致。');
    }
    const cash=data.accounts.filter(account=>account.account_type!=='credit_card');
    if(!cash.every(account=>Number.isSafeInteger(account.balance_minor))||
       cash.reduce((total,account)=>total+BigInt(account.balance_minor),0n)!==BigInt(data.summary.cash_minor))throw Error('账户余额与历史现金总额无法核对。');
    return envelope;
  }
  async function overview(options) { return associations(await read('presentation', {view:'overview'}, options)); }
  function range(month) {
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month || '')) throw Error('请选择有效月份。');
    return {start_date:month + '-01', end_date:month + '-' + new Date(Date.UTC(Number(month.slice(0,4)), Number(month.slice(5,7)), 0)).getUTCDate()};
  }
  async function month(month, account_id, options) {
    const response = await read('presentation', {view:'month_report',month,...(account_id ? {account_id} : {})}, options);
    if (response.data.view !== 'month_report' || response.data.account_id !== (account_id || null)) throw Error('月报账户来源无法核对。');
    return response;
  }
  async function transactions(parameters, options) {
    const response = await read('list_transactions', parameters, options), data = response.data;
    if (!Array.isArray(data.items) || !Number.isSafeInteger(data.total) || !Number.isSafeInteger(data.offset) || data.offset !== Number(parameters.offset || 0)) throw Error('流水分页未完整取得。');
    for (const row of data.items) if (parameters.account_id && row.account_id !== parameters.account_id ||
        parameters.start_date && row.booked_on < parameters.start_date || parameters.end_date && row.booked_on > parameters.end_date) throw Error('流水与所选范围不一致。');
    return response;
  }
  async function latest(month, count = 8, options) {
    const parameters = range(month), first = await transactions({...parameters,limit:1,offset:0},options);
    if (!first.data.total) return first;
    const last = await transactions({...parameters,limit:count,offset:Math.max(0,first.data.total-count)},options);
    if (first.dataset_version !== last.dataset_version || first.data.total !== last.data.total) throw Error('流水版本已变化，请重新读取。');
    return {...last,data:{...last.data,items:[...last.data.items].reverse()}};
  }
  async function assetCurve(envelope, options) {
    const period=envelope.data.period,dates=[],day=new Date(period.start_date+'T00:00:00Z');
    while(day.toISOString().slice(0,10)<=period.end_date){dates.push(day.toISOString().slice(0,10));day.setUTCDate(day.getUTCDate()+7);}
    if(dates.at(-1)!==period.end_date)dates.push(period.end_date);
    const points=await Promise.all(dates.map(as_of=>read('get_financial_summary',{as_of},options)));
    if(points.some((point,i)=>point.dataset_version!==envelope.dataset_version||point.as_of!==dates[i]))throw Error('资产曲线日期或版本无法核对。');
    return points.map(point=>({date:point.as_of,...point.data}));
  }
  function reference(envelope, kind, id, extra = {}) { return {source_scope:scope,object_version:envelope.dataset_version,object_kind:kind,object_id:id,...extra}; }
  function money(value, currency = 'CNY') {
    if (!Number.isSafeInteger(value)) return '未知';
    if (!/^[A-Z]{3}$/.test(currency || '')) return '币种未知';
    const cents = BigInt(value), absolute = cents < 0n ? -cents : cents;
    return (cents < 0n ? '-' : '') + (absolute / 100n).toLocaleString('zh-CN') + '.' + String(absolute % 100n).padStart(2,'0') + (currency === 'CNY' ? ' 元' : ' ' + currency);
  }
  return {read,overview,month,transactions,latest,assetCurve,range,reference,money,clear,scope};
})();

/* 沿用账户卡片、月报与日历组件的布局；详情全部为只读历史。 */
window.NavrenFinancialUI = (() => {
  const F = NavrenFinancialData;
  const labels = {active:'有效',research_only:'研究记录',expired:'已过期',replaced:'已换卡',cancelled:'已取消',closed:'已关闭',lost:'已挂失',pending:'待确认',booked:'已入账',forecast_not_paid:'预计 · 未支付',paid:'已支付',completed:'已完成',unknown:'未知'};
  const kinds = {income:'收入',expense:'消费',refund:'退款',transfer:'转账汇款',insurance_premium:'保费',internal_transfer:'同主体调拨',investment_purchase:'投资买入',investment_redemption:'投资赎回',loan_repayment:'贷款还款'};
  const el = (tag, text, cls) => { const node=document.createElement(tag);if(text!=null)node.textContent=String(text);if(cls)node.className=cls;return node; };
  const button = (text, action) => { const node=el('button',text,'ah-small-button');node.type='button';node.onclick=action;return node; };
  const details = value => { try{return typeof value==='string'?JSON.parse(value):value||{};}catch{return {};} };
  const status = value => labels[value] || '状态待核实';
  function link(text, view, envelope, kind, id, extra = {}) {
    const metadata=F.reference(envelope,kind,id,extra), node=el('a',text,'account-view-link');
    node.href=NavrenViews.url({page:'account',view,...metadata});
    node.onclick=event=>{if(event.button||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;event.preventDefault();NavrenViews.open(view,metadata);};return node;
  }
  function basis(envelope) { return el('p','历史资料 · 截至 '+envelope.as_of+' · 只读','ah-report-basis'); }
  function valuationSource(value) { return ['mock_nav','navren_simulated_bank','synthetic_not_real_NAV'].includes(value)?'账户估值':value; }
  function error(host, failure, retry) { host.replaceChildren(el('p',failure.message||'独立财务资料暂未取得。','account-empty'));if(retry)host.append(button('重新读取',retry)); }
  function fields(host, pairs) { const list=el('dl',null,'account-definition-list');for(const [label,value]of pairs)list.append(el('dt',label),el('dd',value==null?'未知':value));host.append(list); }
  function row(host, title, amount, note, anchor) { const node=anchor||el('article');node.classList.add('account-object');const identity=el('span',null,'account-object-identity');identity.append(el('strong',title),el('small',note));const value=el('span',null,'account-object-amount');value.append(el('strong',amount));node.append(identity,value);if(anchor)node.append(el('span','›','account-object-arrow'));host.append(node);return node; }
  function accountName(data,id) { return data.accounts.find(account=>account.account_id===id)?.name||'关联账户未取得'; }
  const institutionNames = {
    '工行合成连接':'中国工商银行',
    '建行合成连接':'中国建设银行',
    '农行合成连接':'中国农业银行',
    '邮储合成连接':'中国邮政储蓄银行',
    '招行合成连接':'招商银行',
    '中行合成连接':'中国银行',
    '农商行合成连接':'农村商业银行',
    '中信证券合成连接':'中信证券',
    '微信支付合成连接':'微信支付',
    '支付宝合成连接':'支付宝',
    'Navren合成承保机构':'承保机构',
    'Navren 合成承保机构':'承保机构'
  };
  function institutionLabel(name) { return institutionNames[name]||name; }
  function institution(data,account) {
    const name=data.connections.find(item=>item.connection_id===account?.connection_id)?.institution_name;
    return institutionLabel(name)||'机构未取得';
  }
  function assetName(data,id) { return data.assets.find(item=>item.asset_id===id)?.name||'资产名称未取得'; }
  function group(host,title) {const box=el('section',null,'account-detail-block');box.append(el('h2',title));host.append(box);return box;}
  function policyName(data,policy) {
    const contract=data.contracts.find(item=>item.contract_id===policy.contract_id&&item.subject_id===policy.subject_id);
    const types={health:'健康保险',life:'人寿保险',property:'财产保险',equipment:'设备保险',agriculture:'农业保险',crop:'农作物保险',shop_property:'营业场所财产保险',public_liability:'公众责任保险'};
    const name=contract?.name?.trim()||types[policy.insurance_type]||'保险档案';
    return name+' · '+(details(policy.details).policy_number||'保险档案');
  }
  function policyRows(host,envelope,items) {for(const policy of items)row(host,policyName(envelope.data,policy),F.money(policy.premium_minor),'保费 · '+status(policy.status)+' · '+policy.start_date+' — '+policy.end_date,link('','record-detail',envelope,'policy',policy.policy_id));if(!items.length)host.append(el('p','未取得所选状态的保单记录；不能据此推断没有保障。'));}
  function transactionTable(host,envelope,items,data) {
    const wrap=el('div',null,'ah-table-scroll'),table=el('table',null,'ah-transactions'),head=el('tr');
    for(const text of ['时间','账户 / 卡片','交易对象','交易类型','金额'])head.append(el('th',text));const thead=el('thead');thead.append(head);table.append(thead);const body=el('tbody');
    for(const item of items){const tr=el('tr'),account=data?.accounts.find(a=>a.account_id===item.account_id),card=data?.cards.find(c=>c.card_id===item.card_id);tr.append(el('td',item.occurred_at.replace('T',' ').slice(0,16)),el('td',(account?.name||'账户资料未取得')+(card?' · '+card.masked_number:'')),el('td',item.counterparty||'交易对象未取得'),el('td',kinds[item.kind]||'交易类型待核实'));const amount=el('td',F.money(item.signed_minor,item.currency),'ah-transaction-amount');if(item.signed_minor>0)amount.classList.add('is-inflow');tr.append(amount);body.append(tr);}table.append(body);wrap.append(table);host.append(wrap);
  }
  function cardTail(card) { return String(card?.masked_number||'').match(/(\d{4})$/)?.[1]||'****'; }
  function cardDetailIcon(name) {
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    for(const [key,value]of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.5','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true'}))svg.setAttribute(key,value);
    const paths={bank:['M3 10h18M4 10V8l8-5 8 5v2M6 10v9m6-9v9m6-9v9M3 21h18'],nfc:['M8 9a5 5 0 0 1 0 6','M11 6a9 9 0 0 1 0 12','M14 3a13 13 0 0 1 0 18']};
    for(const d of paths[name]){const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',d);svg.append(path);}return svg;
  }
  function cardDetailPanel(title,description) {
    const panel=el('section',null,'cd-panel'),head=el('div',null,'cd-panel-heading');head.append(el('h3',title));if(description)head.append(el('p',description));panel.append(head);return panel;
  }
  async function cardDetail(host,envelope,id,valid) {
    const data=envelope.data,card=data.cards.find(item=>item.card_id===id);if(!card)throw Error('卡片不存在或不属于当前主体。');
    const account=data.accounts.find(item=>item.account_id===card.account_id);if(!account)throw Error('卡片的所属账户未取得。');
    const credit=card.card_type==='credit',facility=credit?data.credit_facilities.find(item=>item.facility_id===card.facility_id):null,bank=institution(data,account),type=credit?'信用卡':card.card_type==='debit'?'借记卡':'银行卡类型待核实';
    const root=el('div',null,'cd-detail'),header=el('header',null,'cd-heading'),identity=el('div');identity.append(el('p','银行卡详情 · 尾号 '+cardTail(card),'cd-eyebrow'),el('h2',bank));const badges=el('div',null,'cd-badges'),state=el('span',status(card.status),'cd-status');state.dataset.status=card.status;badges.append(el('span',type,'cd-type'),state);header.append(identity,badges);root.append(header);host.replaceChildren(root);
    const hero=el('div',null,'cd-hero'),visual=el('div',null,'cd-visual'),face=el('article',null,'cd-bank-card');
    face.dataset.bankTheme=[[/中国银行|中行/,'boc'],[/建设银行|建行/,'ccb'],[/农业银行|农行/,'abc'],[/工商银行|工行/,'icbc'],[/招商银行|招行/,'cmb'],[/邮政储蓄|邮储/,'psbc']].find(([pattern])=>pattern.test(bank))?.[1]||'neutral';
    const wave=el('span',null,'cd-card-wave');wave.setAttribute('aria-hidden','true');const bankIdentity=el('div',null,'cd-card-bank');bankIdentity.append(cardDetailIcon('bank'),el('span',bank));const head=el('div',null,'cd-card-head');head.append(bankIdentity,el('span',type,'cd-card-type'));
    const numberRow=el('div',null,'cd-card-number-row'),chipGroup=el('span',null,'cd-chip-group'),chip=el('span',null,'cd-chip');chip.setAttribute('aria-hidden','true');chipGroup.append(chip,cardDetailIcon('nfc'));numberRow.append(chipGroup,el('span','**** **** **** '+cardTail(card),'cd-card-number'));
    const balance=el('div',null,'cd-card-balance');balance.append(el('span',credit?'信用卡未还余额':'账户现金'),el('strong',F.money(credit?facility?.outstanding_minor:account.balance_minor,account.currency)));
    const foot=el('div',null,'cd-card-foot');foot.append(el('span','所属账户'),el('strong',account.name));face.append(wave,head,numberRow,balance,foot);visual.append(face,el('p','余额截至 '+envelope.as_of+' · '+(credit?'信用卡欠款计入负债':'同账户余额按账户计入资产'),'cd-visual-note'));hero.append(visual);
    const info=cardDetailPanel('账户与卡片信息');
    fields(info,[['所属账户',account.name],['币种',account.currency==='CNY'?'人民币 · CNY':account.currency],['账户用途',({personal:'个人资金',self_employed:'个人经营资金'}[account.scope]||'用途待核实')],['账户状态',status(account.status)]]);
    const cardInfo=el('div',null,'cd-card-info');cardInfo.append(el('h4','卡片档案'));fields(cardInfo,[['脱敏卡号','**** **** **** '+cardTail(card)],['卡片角色',card.role==='primary'?'主卡':card.role==='supplementary'?'附属卡':'角色待核实'],['发行日期',card.issued_on],['停止使用日期',card.closed_on||'未记录']]);info.append(cardInfo);
    if(credit){const creditInfo=el('div',null,'cd-credit-info');fields(creditInfo,[['信用额度（不计资产）',F.money(facility?.limit_minor,account.currency)]]);info.append(creditInfo);}
    const extraState=el('div',null,'cd-extra-state'),accountLink=link('查看所属账户','account-detail',envelope,'account',account.account_id);accountLink.classList.add('cd-info-link');info.append(extraState,accountLink);hero.append(info);root.append(hero);
    const transactions=cardDetailPanel('所属账户流水','展示「'+account.name+'」的最近记录；历史卡片尾号按流水原始关联显示。'),transactionBody=el('div',null,'cd-transactions-body');transactions.classList.add('cd-transactions');transactions.append(transactionBody);root.append(transactions);
    const extras=el('div',null,'cd-extra-grid'),history=cardDetailPanel('卡片沿革'),timeline=el('ol',null,'cd-timeline');
    for(const record of data.cards.filter(item=>item.account_id===account.account_id).sort((a,b)=>String(a.issued_on).localeCompare(String(b.issued_on)))){
      const item=el('li'),recordLink=record.card_id===card.card_id?el('div',null,'cd-timeline-record'):link('','card-detail',envelope,'card',record.card_id);recordLink.classList.add('cd-timeline-record');if(record.card_id===card.card_id)item.dataset.current='true';
      const recordHead=el('div',null,'cd-timeline-head');recordHead.append(el('strong','尾号 '+cardTail(record)),el('span',status(record.status)));recordLink.append(recordHead,el('small',record.issued_on+(record.closed_on?' — '+record.closed_on:' 发行')));
      const replaced=data.cards.find(previous=>previous.card_id===record.replaces_card_id);if(replaced)recordLink.append(el('small','替换尾号 '+cardTail(replaced)));if(record.card_id===card.card_id)recordLink.append(el('span','当前查看','cd-current-label'));item.append(recordLink);timeline.append(item);
    }history.append(timeline);const associated=el('div',null,'cd-associated-panels');extras.append(history,associated);root.append(extras);
    const related=el('div',null,'cd-related');root.append(related);const source=el('footer',null,'cd-source');source.append(basis(envelope));root.append(source);
    const consistent=response=>{if(response.dataset_version!==envelope.dataset_version||response.as_of!==envelope.as_of)throw Error('附加记录与这张卡的资料版本或日期不一致，请重新读取。');};
    const localError=(target,failure,retry)=>{target.replaceChildren(el('p',failure.message||'附加记录暂未取得。','cd-empty'));if(retry)target.append(button('重新读取',retry));};
    let recordsEpoch=0,transactionsEpoch=0;
    async function loadRecords(fresh=false) {
      const token=++recordsEpoch;extraState.replaceChildren(el('p','正在读取关联档案…','cd-loading'));
      try {
        const response=await F.read('get_account_overview',{account_id:account.account_id},{fresh});if(!valid()||token!==recordsEpoch)return;consistent(response);
        const result=response.data;if(result.account?.account_id!==account.account_id||result.account.balance_minor!==account.balance_minor||!Array.isArray(result.cards)||!Array.isArray(result.contracts)||!Array.isArray(result.recent_card_statements)||!Array.isArray(result.credit_facilities)||!result.cards.some(item=>item.card_id===card.card_id))throw Error('所属账户的附加档案未完整取得或余额关联无法核对。');
        for(const item of [...result.cards,...result.contracts,...result.recent_card_statements,...result.credit_facilities])if(item.account_id!==account.account_id)throw Error('附加档案包含其他账户记录，已停止展示。');
        extraState.replaceChildren();related.replaceChildren();associated.replaceChildren();
        if(credit&&result.recent_card_statements.length){
          const statements=cardDetailPanel('近期信用卡账单','账单金额按各账期记录，与卡片当前未还余额分别展示。'),scroll=el('div',null,'ah-table-scroll'),table=el('table',null,'cd-statements'),thead=el('thead'),header=el('tr');
          for(const label of ['账期','账单金额','已还金额','还款日','状态'])header.append(el('th',label));thead.append(header);table.append(thead);const tbody=el('tbody');
          for(const statement of result.recent_card_statements.slice(0,4)){const row=el('tr'),statementStatus=({open:'待还款',adjusted_by_refund:'退款调整'}[statement.status]||status(statement.status));for(const value of [statement.period_start+' — '+statement.period_end,F.money(statement.statement_minor,account.currency),F.money(statement.paid_minor,account.currency),statement.due_on,statementStatus])row.append(el('td',value));tbody.append(row);}table.append(tbody);scroll.append(table);statements.append(scroll);related.append(statements);
        }
        const groupedRecords=(panel,items,historyLabel,paint)=>{const current=items.filter(item=>['active','research_only'].includes(item.status)),past=items.filter(item=>!['active','research_only'].includes(item.status));if(current.length)paint(panel,current);if(past.length){const disclosure=el('details',null,'cd-history-docs'),summary=el('summary',historyLabel+' · '+past.length+' 条'),body=el('div',null,'cd-history-body');paint(body,past);disclosure.append(summary,body);panel.append(disclosure);}};
        if(result.contracts.length){const contracts=cardDetailPanel('关联合同');groupedRecords(contracts,result.contracts,'历史合同',(target,items)=>{for(const contract of items)row(target,contract.name,contract.principal_minor==null?'':F.money(contract.principal_minor,account.currency),status(contract.status)+' · '+contract.start_date+(contract.end_date?' — '+contract.end_date:''),link('','record-detail',envelope,'contract',contract.contract_id));});associated.append(contracts);}
        const obligations=data.obligations.filter(item=>item.account_id===account.account_id);if(obligations.length){const arrangements=cardDetailPanel('已记录缴费安排');for(const obligation of obligations.slice(0,6))row(arrangements,obligation.category,F.money(obligation.amount_minor,account.currency),obligation.due_on+' · '+status(obligation.status));arrangements.append(link('财务日历','calendar',envelope));associated.append(arrangements);}
        const policies=data.policies.filter(policy=>policy.payment_account_id===account.account_id);if(policies.length){const insurance=cardDetailPanel('关联保单');groupedRecords(insurance,policies,'历史保单',(target,items)=>policyRows(target,envelope,items));associated.append(insurance);}
      }catch(failure){if(valid()&&token===recordsEpoch)localError(extraState,failure,()=>loadRecords(true));}
    }
    async function loadTransactions(fresh=false) {
      const token=++transactionsEpoch;transactionBody.replaceChildren(el('p','正在读取所属账户流水…','cd-loading'));
      try {
        const parameters={account_id:account.account_id,end_date:envelope.as_of},first=await F.transactions({...parameters,limit:1,offset:0},{fresh});if(!valid()||token!==transactionsEpoch)return;consistent(first);
        let page=first;if(first.data.total){page=await F.transactions({...parameters,limit:8,offset:Math.max(0,first.data.total-8)},{fresh});if(!valid()||token!==transactionsEpoch)return;consistent(page);if(page.data.total!==first.data.total)throw Error('所属账户流水数量已变化，请重新读取。');}
        transactionBody.replaceChildren();const items=[...page.data.items].reverse();
        if(items.length){const displayData={...data,cards:data.cards.map(record=>({...record,masked_number:'尾号 '+cardTail(record)}))};transactionTable(transactionBody,page,items,displayData);}else transactionBody.append(el('p','当前覆盖期间未取得该账户流水。','cd-empty'));
        const footer=el('div',null,'cd-table-footer');footer.append(el('span','已记录 '+page.data.total+' 笔 · 当前展示 '+items.length+' 笔'),link('按月查看账户流水','statements',envelope,'account',account.account_id,{period:envelope.as_of.slice(0,7),filters:{account_id:account.account_id}}));transactionBody.append(footer);
      }catch(failure){if(valid()&&token===transactionsEpoch)localError(transactionBody,failure,()=>loadTransactions(true));}
    }
    await Promise.allSettled([loadRecords(),loadTransactions()]);
  }
  function accountDetail(host,envelope,records) {
    const data=envelope.data,account=records.account,bank=institution(data,account),credit=account.account_type==='credit_card';
    const type=({bank_settlement:'现金账户',securities_cash:'证券资金账户',payment_wallet:'支付账户',credit_card:'信用卡账务账户'}[account.account_type]||'账户类型待核实');
    const root=el('div',null,'ad-detail'),heading=el('header',null,'ad-heading'),identity=el('div',null,'ad-identity');
    root.dataset.institutionTheme=/中信证券/.test(bank)?'citic':'neutral';
    identity.append(el('p',bank,'ad-eyebrow'),el('h2',account.name),basis(envelope));
    const badges=el('div',null,'ad-badges'),state=el('span',status(account.status),'ad-status');state.dataset.status=account.status;
    badges.append(el('span',type,'ad-type'),state);heading.append(identity,badges);root.append(heading);
    const overview=el('section',null,'ad-overview'),balance=el('div',null,'ad-balance');
    balance.append(el('span',credit?'账户账务余额':'账户现金余额','ad-balance-label'),el('strong',F.money(account.balance_minor,account.currency||'')),el('p','余额截至 '+envelope.as_of,'ad-balance-date'));
    const info=el('div',null,'ad-overview-info');info.append(el('h3','基本信息'));
    const list=el('dl',null,'ad-facts');
    for(const [label,value]of [['机构',bank],['账户类型',type],['币种',account.currency==='CNY'?'人民币 · CNY':account.currency||'币种未取得'],['历史覆盖起始日',account.opened_on]]){
      const fact=el('div',null,'ad-fact');fact.append(el('dt',label),el('dd',value==null?'未知':value));list.append(fact);
    }
    info.append(list);overview.append(balance,info);root.append(overview);
    const section=(title,items)=>{
      const panel=el('section',null,'ad-panel'),head=el('div',null,'ad-section-heading'),grid=el('div',null,'ad-record-grid');
      head.append(el('h3',title),el('span',items.length+' 项','ad-section-count'));panel.append(head,grid);root.append(panel);return grid;
    };
    const cards=records.cards||[];
    if(cards.length){const grid=section('关联银行卡',cards);for(const card of cards)row(grid,card.card_type==='credit'?'信用卡':'借记卡','',card.masked_number+' · '+status(card.status),link('','card-detail',envelope,'card',card.card_id));}
    const facilities=records.credit_facilities||[];
    if(facilities.length){const grid=section('账户授信',facilities);for(const facility of facilities){const box=el('div',null,'ad-credit-record');fields(box,[['信用额度（不计资产）',F.money(facility.limit_minor,account.currency||'')],['未还金额',F.money(facility.outstanding_minor,account.currency||'')]]);grid.append(box);}}
    const positions=data.positions.filter(item=>item.account_id===account.account_id);
    if(positions.length){const grid=section('资产持仓',positions);for(const position of positions)row(grid,assetName(data,position.asset_id),F.money(position.book_value_minor,account.currency||''),'持仓估值 · '+position.valuation_date,link('','holding-detail',envelope,'holding',position.position_id));}
    const contracts=records.contracts||[];
    if(contracts.length){const grid=section('关联合同',contracts);for(const contract of contracts)row(grid,contract.name,F.money(contract.principal_minor,account.currency||''),'合同 · '+status(contract.status)+' · '+contract.start_date+(contract.end_date?' — '+contract.end_date:''),link('','record-detail',envelope,'contract',contract.contract_id));}
    const footer=el('footer',null,'ad-footer'),statementLink=link('此账户流水','statements',envelope,'account',account.account_id,{period:envelope.as_of.slice(0,7),filters:{account_id:account.account_id}});
    statementLink.classList.add('ad-statement-link');footer.append(statementLink);root.append(footer);host.replaceChildren(root);
  }
  async function render(host, context, isCurrent) {
    let guard;const view=context.view||'home',valid=()=>host.isConnected&&isCurrent()&&(!guard||guard.valid());host.replaceChildren(el('p','正在读取独立财务资料…','account-empty'));
    try {
      await BankClient.ensure();guard=BankClient.guard();if(!valid())return;
      const envelope=await F.overview();if(!valid())return;
      if(context.source_scope===F.scope&&context.object_version&&context.object_version!==envelope.dataset_version)throw Error('这份历史对象版本已变化，请返回账户重新选择。');
      const data=envelope.data,id=context.object_id;host.replaceChildren(basis(envelope));
      if(view==='funds') {
        const accounts=group(host,'资金账户');for(const account of data.accounts)row(accounts,account.name,F.money(account.balance_minor,account.currency),institution(data,account)+' · '+(account.account_type==='credit_card'?'信用卡账务，不计现金资产':'已结算余额'),link('','account-detail',envelope,'account',account.account_id));
        host.append(link('资产持仓','holdings',envelope),link('保险与合同档案','archive',envelope),link('财务日历','calendar',envelope));
      } else if(view==='account-detail') {
        const result=await F.read('get_account_overview',{account_id:id});if(!valid())return;const account=result.data.account;
        if(account?.account_id!==id)throw Error('账户详情关联无法核对。');accountDetail(host,envelope,result.data);
      } else if(view==='card-detail') { await cardDetail(host,envelope,id,valid); }
      else if(view==='cards') {
        const cards=data.cards;
        for(const card of cards){const account=data.accounts.find(a=>a.account_id===card.account_id),box=group(host,institution(data,account)+' · '+(card.card_type==='credit'?'信用卡':'借记卡'));fields(box,[['卡号',card.masked_number],['状态',status(card.status)],['发行日期',card.issued_on],['停止使用日期',card.closed_on||'未记录'],['关联账户',accountName(data,card.account_id)]]);box.append(link('查看所属账户','account-detail',envelope,'account',card.account_id));
          if(card.facility_id){const facility=data.credit_facilities.find(f=>f.facility_id===card.facility_id);fields(box,[['信用额度（不计资产）',F.money(facility?.limit_minor,account.currency)],['未还金额',F.money(facility?.outstanding_minor,account.currency)]]);}else fields(box,[['账户余额（多卡共享，仅计一次）',F.money(account.balance_minor,account.currency)]]);
          box.append(link('卡片详情','card-detail',envelope,'card',card.card_id));}
      } else if(view==='holdings'||view==='holding-detail') {
        const positions=view==='holding-detail'?data.positions.filter(position=>position.position_id===id):data.positions;
        if(view==='holding-detail'&&!positions.length)throw Error('持仓不存在或不属于当前主体。');
        for(const position of positions){const box=group(host,assetName(data,position.asset_id));fields(box,[['持仓估值',F.money(position.book_value_minor)],['成本本金',F.money(position.cost_basis_minor)],['份额',position.quantity],['估值日',position.valuation_date],['估值来源',valuationSource(position.valuation_source)],['所属账户',accountName(data,position.account_id)]]);box.append(link('关联账户','account-detail',envelope,'account',position.account_id),link('持仓合同','record-detail',envelope,'contract',position.contract_id));}
        if(view==='holdings'){const real=group(host,'其他资产');for(const asset of data.assets.filter(asset=>!asset.asset_type.startsWith('fund_')))row(real,asset.name,F.money(asset.book_value_minor),'资产估值 · '+(asset.valuation_date||envelope.as_of),link('','record-detail',envelope,'asset',asset.asset_id));}
      } else if(view==='archive') {
        const policies=group(host,'保险档案'),select=el('select');select.setAttribute('aria-label','保单状态');for(const [value,label]of [['active','有效保单'],['expired','过期保单'],['cancelled','已取消保单'],['pending','待确认保单']]){const option=el('option',label);option.value=value;select.append(option);}policies.append(select);const list=el('div');policies.append(list);let policyEpoch=0;
        const load=async()=>{const token=++policyEpoch;list.replaceChildren(el('p','正在读取保险档案…'));try{const response=await F.read('list_insurance',{status:select.value});if(!valid()||token!==policyEpoch)return;list.replaceChildren();if(!Array.isArray(response.data.items))throw Error('保单列表未完整取得。');policyRows(list,envelope,response.data.items);}catch(failure){if(valid()&&token===policyEpoch)error(list,failure,load);}};select.onchange=load;await load();if(!valid())return;
        const contracts=group(host,'合同档案');for(const contract of data.contracts)row(contracts,contract.name,F.money(contract.principal_minor),status(contract.status)+' · '+contract.start_date+(contract.end_date?' — '+contract.end_date:''),link('','record-detail',envelope,'contract',contract.contract_id));
      } else if(view==='record-detail') {
        if(context.object_kind==='policy') {
          const response=await F.read('get_policy',{policy_id:id});if(!valid())return;const {policy,premium_funding,claims}=response.data;if(policy?.policy_id!==id)throw Error('保险详情关联无法核对。');host.append(el('h2',policyName(data,policy)));
          fields(host,[['承保机构',institutionLabel(data.connections.find(c=>c.connection_id===policy.insurer_connection_id)?.institution_name)],['保单状态',status(policy.status)],['保障期间',policy.start_date+' — '+policy.end_date],['保额（不计资产）',F.money(policy.sum_insured_minor)],['保费',F.money(policy.premium_minor)],['本人支付保费',F.money(policy.farmer_paid_minor)],['补贴',F.money(policy.subsidy_minor)],['免赔额',F.money(policy.deductible_minor)],['现金价值',F.money(policy.cash_value_minor)],['受益人',policy.beneficiary_id],['缴费账户',accountName(data,policy.payment_account_id)]]);
          host.append(link('缴费账户','account-detail',envelope,'account',policy.payment_account_id));
          const funding=group(host,'保费来源');for(const source of premium_funding||[])fields(funding,[['支付方',({policyholder:'投保人',central:'中央财政',provincial:'省级财政',local:'地方财政',personal:'本人',government:'政府补贴',employer:'单位'}[source.payer_type]||'未知')],['金额',F.money(source.amount_minor)],['个人现金影响',source.personal_cash_effect?'有':'无']]);
          const claimBox=group(host,'理赔记录');if(!claims?.length)claimBox.append(el('p','未提供可核验的理赔记录。'));for(const claim of claims||[])fields(claimBox,[['状态',status(claim.status)],['发生日',claim.occurred_on],['赔付金额',F.money(claim.paid_minor)]]);
        } else if(context.object_kind==='asset') {
          const asset=data.assets.find(a=>a.asset_id===id);if(!asset)throw Error('资产记录未取得。');host.append(el('h2',asset.name));fields(host,[['资产估值',F.money(asset.book_value_minor)],['成本本金',F.money(asset.cost_basis_minor)],['估值日',asset.valuation_date||envelope.as_of],['归属',asset.ownership_scope==='personal'?'本人':'归属待核实']]);
        } else if(context.object_kind==='contract') {
          let contract=data.contracts.find(c=>c.contract_id===id);if(!contract){const records=await Promise.all(data.accounts.map(account=>F.read('get_account_overview',{account_id:account.account_id})));if(!valid())return;contract=records.flatMap(record=>record.data.contracts||[]).find(c=>c.contract_id===id);}if(!contract)throw Error('历史合同不存在或不属于当前主体。');host.append(el('h2',contract.name));fields(host,[['状态',status(contract.status)],['开始日期',contract.start_date],['结束日期',contract.end_date||'未记录'],['合同本金',F.money(contract.principal_minor)],['关联账户',accountName(data,contract.account_id)]]);host.append(link('关联账户','account-detail',envelope,'account',contract.account_id));
        } else throw Error('此历史对象类型未开放。');
      } else if(view==='statements') { await reports(host,envelope,context,valid); }
      else if(view==='calendar'||view==='calendar-day') { await calendar(host,envelope,context,valid); }
      else throw Error('此视图不支持独立财务历史。');
    } catch (failure) { if(valid())error(host,failure,()=>render(host,context,isCurrent)); }
  }
  function monthlyReport(report,month,data) {
    const header=el('header',null,'fd-month-report-head'),title=el('div');title.append(el('h2',month+' 资产月报'));
    header.append(title,el('span',data.selected_account?'所选账户收支':'本人账户','fd-month-report-scope'));
    const cash=el('section',null,'fd-month-cash');cash.setAttribute('aria-label','现金概览');
    for(const [label,field,amount]of [['本人期末现金','closing_cash_minor',data.closing?.cash_minor],['本人期初现金','opening_cash_minor',data.opening?.cash_minor],[data.selected_account?'所选账户现金净变动':'现金净变动','net_cash_change_minor',data.totals.net_cash_change_minor]]) {
      const metric=el('div',null,'fd-month-metric');metric.dataset.field=field;
      metric.dataset.direction=Number.isSafeInteger(amount)?amount>0?'positive':amount<0?'negative':'zero':'unknown';
      metric.append(el('span',label,'fd-month-metric-label'),el('strong',F.money(amount),'fd-month-metric-value'));cash.append(metric);
    }
    const groups=el('section',null,'fd-month-groups');groups.setAttribute('aria-label',data.selected_account?'所选账户分类收支':'分类收支');
    const fieldList=pairs=>{const list=el('dl',null,'fd-month-fields');for(const [label,field,amount,emphasis]of pairs){const row=el('div',null,'fd-month-field');row.dataset.field=field;if(emphasis)row.dataset.emphasis='true';row.append(el('dt',label),el('dd',amount));list.append(row);}return list;};
    const icons={spending:['M4 7h16v13H4z','M4 7V4h13v3','M15 11h5v5h-5z'],investment:['M4 20V4','M4 20h16','M7 14l4-4 4 2 5-7'],debt:['M3 9l9-6 9 6','M4 10h16','M6 10v8','M12 10v8','M18 10v8','M3 21h18']};
    for(const [kind,label,rows]of [
      ['spending','日常收支',[['收入','income_minor'],['消费','expense_minor'],['退款','refund_minor'],['净消费','net_expense_minor',true]]],
      ['investment','投资变动',[['投资买入','investment_purchase_minor'],['投资赎回','investment_redemption_minor']]],
      ['debt','贷款',[['贷款还款','loan_repayment_minor']]]
    ]) {
      const group=el('section',null,'fd-month-group');group.dataset.kind=kind;
      const heading=el('header',null,'fd-month-group-head'),icon=el('span',null,'fd-month-group-icon');icon.setAttribute('aria-hidden','true');
      const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');for(const [attribute,value]of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.7','stroke-linecap':'round','stroke-linejoin':'round'}))svg.setAttribute(attribute,value);
      for(const d of icons[kind]){const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',d);svg.append(path);}icon.append(svg);
      heading.append(icon,el('h3',label));group.append(heading,fieldList(rows.map(([name,field,emphasis])=>[name,field,F.money(data.totals[field]),emphasis])));groups.append(group);
    }
    report.replaceChildren(header,cash,groups);
    if(data.selected_account) {
      const account=data.selected_account,section=el('section',null,'fd-month-selected');section.append(el('h3','所选账户'),fieldList([
        ['所选账户','selected_account_name',account.account.name],['账户期初余额','selected_opening_balance_minor',F.money(account.opening_balance_minor)],
        ['账户期末余额','selected_closing_balance_minor',F.money(account.closing_balance_minor)],['账户净变动','selected_balance_change_minor',F.money(account.balance_change_minor)]
      ]));report.append(section);
      const note=el('p','期初、期末现金为全户快照；收支为所选账户。','fd-month-report-note');note.dataset.variant='scope';report.append(note);
    }
    if(data.period?.coverage_partial){const note=el('p','所选月份仅展示已覆盖期间，月底金额尚未完整取得。','fd-month-report-note');note.dataset.variant='partial';report.append(note);}
  }
  async function reports(host,envelope,context,valid) {
    let month=context.period||envelope.as_of.slice(0,7),offset=0,epoch=0;
    const accountId=context.object_kind==='account'?context.object_id:context.filters?.account_id;
    if(accountId&&!envelope.data.accounts.some(a=>a.account_id===accountId))throw Error('流水账户不属于当前主体。');
    const controls=el('div',null,'calendar-main-controls'),input=el('input');input.type='month';input.id='accountHistoryStatementMonth';input.max=envelope.evidence?.coverage_end?.slice(0,7)||envelope.as_of.slice(0,7);input.min=envelope.evidence?.coverage_start?.slice(0,7)||input.max;if(!/^\d{4}-(0[1-9]|1[0-2])$/.test(month))month=input.max;else if(month<input.min)month=input.min;else if(month>input.max)month=input.max;input.value=month;if(context.period&&context.period!==month)NavrenViews.update?.({period:month});input.setAttribute('aria-label','流水月份');controls.append(input);host.append(controls);const report=el('section',null,'account-detail-block fd-month-report'),transactions=el('section',null,'account-detail-block');host.append(report,transactions);
    const load=async()=>{const token=++epoch;const current=()=>valid()&&epoch===token;report.replaceChildren(el('p','正在读取月报…'));transactions.replaceChildren();try {
      const snapshot=await F.month(month,accountId),data=snapshot.data;if(!current())return;monthlyReport(report,month,data);
      const page=await F.transactions({...F.range(month),...(accountId?{account_id:accountId}:{}),limit:50,offset});if(!current())return;transactions.append(el('h2','交易明细'));transactionTable(transactions,page,page.data.items,envelope.data);const pager=el('div',null,'calendar-main-controls'),previous=button('上一页',()=>{offset=Math.max(0,offset-50);load();}),next=button('下一页',()=>{offset+=50;load();});previous.disabled=offset===0;next.disabled=!page.data.has_more;pager.append(previous,el('span',page.data.total+' 笔 · 第 '+(Math.floor(offset/50)+1)+' 页'),next);transactions.append(pager);
    }catch(failure){if(current())error(report,failure,load);}};
    input.onchange=()=>{if(input.value&&input.value>=input.min&&input.value<=input.max){month=input.value;offset=0;NavrenViews.update?.({period:month});load();}};window.NavrenMonthPicker?.mount(input,{currentMonth:envelope.as_of.slice(0,7)});await load();
  }
  async function calendar(host,envelope,context,valid) {
    let month=(context.date||context.period||envelope.as_of).slice(0,7),epoch=0;
    const heading=el('div',null,'calendar-heading'),controls=el('div',null,'calendar-main-controls'),input=el('input');input.type='month';input.value=month;input.setAttribute('aria-label','日历月份');heading.append(el('h2','财务日历'),controls);host.append(heading);const view=el('section',null,'calendar-widget'),detail=el('section',null,'account-detail-block');host.append(view,detail);
    const showDay=async(response,date)=>{const token=++epoch;detail.replaceChildren(el('h2',date+' 当日明细'));const daily=response.data.daily.find(d=>d.date===date);if(daily)fields(detail,[['收入',F.money(daily.income_minor)],['消费',F.money(daily.expense_minor)],['退款',F.money(daily.refund_minor)],['现金净变动',F.money(daily.net_cash_change_minor)],['投资收益',F.money(daily.investment_pnl_minor)]]);
      for(const event of response.data.events.filter(e=>(e.date||e.due_on||e.occurred_at?.slice(0,10))===date)){const name=event.label||event.title||event.category||'财务事项';row(detail,name,F.money(event.amount_minor,event.currency||'CNY'),status(event.status));const policyId=event.policy_id;if(policyId)detail.append(link('保险档案','record-detail',envelope,'policy',policyId));}
      try{const page=await F.transactions({start_date:date,end_date:date,limit:100,offset:0});if(!valid()||epoch!==token)return;transactionTable(detail,page,page.data.items,envelope.data);if(page.data.has_more)detail.append(link('查看完整流水','statements',envelope,null,null,{period:date.slice(0,7)}));if(page.data.coverage_partial)detail.append(el('p','此日期不在已记录流水覆盖期内；预计事项不代表已支付。'));}catch(failure){if(valid()&&epoch===token)detail.append(el('p',failure.message),button('重新读取当日明细',()=>showDay(response,date)));}};
    const load=async()=>{const token=++epoch;view.replaceChildren(el('p','正在读取财务日历…'));detail.replaceChildren();try{const response=await CalendarClient.financial(F.range(month));if(!valid()||token!==epoch)return;input.value=month;view.replaceChildren();const grid=el('div',null,'calendar-grid');for(const day of ['一','二','三','四','五','六','日'])grid.append(el('div','星期'+day,'calendar-weekday'));const first=F.range(month).start_date,offset=(new Date(first+'T00:00:00Z').getUTCDay()+6)%7;for(let i=0;i<offset;i++)grid.append(el('span'));
      const daily=new Map(response.data.daily.map(day=>[day.date,day]));const end=Number(F.range(month).end_date.slice(-2));for(let i=1;i<=end;i++){const date=month+'-'+String(i).padStart(2,'0'),day=daily.get(date),events=response.data.events.filter(e=>(e.date||e.due_on||e.occurred_at?.slice(0,10))===date),cell=button('',()=>showDay(response,date));cell.className='calendar-day';cell.id='account-calendar-day-'+date;cell.append(el('strong',i));if(day?.transaction_count)cell.append(el('small',day.transaction_count+' 笔交易','calendar-day-cash'));cell.append(el('small',day?.covered?'收益未知':'无已覆盖流水','calendar-day-pnl pnl-unknown'));for(const event of events.slice(0,2))cell.append(el('span',(event.label||event.title||event.category||'事项')+' · '+status(event.status),'calendar-day-title'));grid.append(cell);}view.append(grid);if(response.data.events_truncated||response.data.truncated)view.append(el('p','部分事项未在日历预览列出。'));if(context.view==='calendar-day'&&context.date)await showDay(response,context.date);
    }catch(failure){if(valid()&&token===epoch)error(view,failure,load);}};
    const shift=value=>{const d=new Date(month+'-01T00:00:00Z');d.setUTCMonth(d.getUTCMonth()+value);month=d.toISOString().slice(0,7);NavrenViews.update?.({period:month});load();};controls.append(button('‹',()=>shift(-1)),input,button('›',()=>shift(1)));input.onchange=()=>{if(input.value){month=input.value;NavrenViews.update?.({period:month});load();}};await load();
  }
  return {render,transactionTable,institution,assetName,basis,error};
})();
