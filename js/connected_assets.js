/* 当前账本读取与本地体验关联；不缓存完成结果，不回退历史余额。 */
window.NavrenConnectedAssets = (() => {
  const scope = 'connected_bank', pending = new Map();
  let generation = 0, owner = '', latestVersion = -1;
  const minor = Number.isSafeInteger;
  const fail = message => { throw Error(message); };
  const identifier = value => typeof value === 'string' && value.length > 0 && value.length <= 240 && !/[\u0000-\u001f\u007f]/.test(value);
  const iso = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
  const day = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value+'T00:00:00Z')) && new Date(value+'T00:00:00Z').toISOString().slice(0,10) === value;
  function clear() { generation++; owner = ''; latestVersion = -1; pending.clear(); }
  function total(values) {
    if (!values.every(minor)) fail('金额不是有效的整数分，已停止展示。');
    const result = values.reduce((a,b) => a + BigInt(b), 0n);
    if (result > BigInt(Number.MAX_SAFE_INTEGER) || result < BigInt(Number.MIN_SAFE_INTEGER)) fail('金额超出可核对范围。');
    return Number(result);
  }
  function binding(value, identity) {
    if (!identity || !identifier(identity.workspace_id) || !identifier(identity.account_id) || !Number.isSafeInteger(identity.privacy_revision) ||
        !value || value.schema_version !== 'navren.connected-assets.v1' || value.source !== 'navren_simulated_bank' ||
        value.environment !== 'simulation' || value.read_only !== true || value.runtime_ledger_included !== true ||
        value.workspace_binding !== identity.workspace_id || value.workspace_id != null && value.workspace_id !== identity.workspace_id ||
        value.subject_id !== identity.account_id || value.current_account_id !== identity.account_id || value.privacy_revision !== identity.privacy_revision ||
        !minor(value.version) || value.version < 0 || !identifier(value.dataset_version) || !day(value.as_of) ||
        !iso(value.observed_at) || !iso(value.valuation_at) || !value.data || typeof value.data !== 'object') fail('当前资产的身份、授权、来源或时间无法核对。');
    function walk(item) {
      if (!item || typeof item !== 'object') return;
      if (item.subject_id != null && item.subject_id !== identity.account_id) fail('资料含其他主体对象，已停止展示。');
      for (const [key,child] of Object.entries(item)) {
        if (key.endsWith('_minor') && child != null && !minor(child)) fail('金额不是有效的整数分，已停止展示。');
        if (child && typeof child === 'object') walk(child);
      }
    }
    walk(value.data);
    return value;
  }
  function validate(value, identity = window.BankClient?.identity()) {
    binding(value, identity);
    const data = value.data, index = {};
    if (data.view !== 'overview') fail('当前资产展示合同不兼容。');
    for (const [table,key] of [['connections','connection_id'],['accounts','account_id'],['cards','card_id'],['positions','position_id'],['assets','asset_id'],['contracts','contract_id'],['credit_facilities','facility_id'],['policies','policy_id'],['obligations','obligation_id']]) {
      const rows = data[table];
      if (!Array.isArray(rows) || rows.length > 200 || rows.some(row => !row || !identifier(row[key]))) fail('当前资产目录未完整取得：'+table);
      index[table] = new Map(rows.map(row => [row[key],row]));
      if (index[table].size !== rows.length) fail('当前财务对象编号重复。');
    }
    const association = (rows,key,table,optional=false) => rows.forEach(row => {
      if (optional && row[key] == null) return;
      if (!index[table].has(row[key])) fail('当前财务对象关联未取得：'+key);
    });
    association(data.accounts,'connection_id','connections');
    association(data.cards,'account_id','accounts'); association(data.cards,'facility_id','credit_facilities',true);
    association(data.credit_facilities,'account_id','accounts');
    association(data.positions,'account_id','accounts'); association(data.positions,'asset_id','assets'); association(data.positions,'contract_id','contracts');
    association(data.contracts,'account_id','accounts',true); association(data.contracts,'asset_id','assets',true);
    association(data.policies,'payment_account_id','accounts',true); association(data.policies,'insurer_connection_id','connections'); association(data.policies,'contract_id','contracts');
    association(data.obligations,'account_id','accounts');
    for (const account of data.accounts) {
      if (account.account_id.includes('::') || account.id != null && account.id !== account.account_id || account.currency !== 'CNY' || !minor(account.balance_minor) ||
          (account.account_type === 'credit_card' ? account.balance_minor > 0 : account.balance_minor < 0)) fail('当前账户编号、币种或余额无法核对。');
    }
    for (const card of data.cards) {
      if (!['credit','debit','virtual'].includes(card.card_type) || !/^\*{4} \*{4} \*{4} \d{4}$/.test(card.masked_number || '')) fail('卡片类型或16位脱敏卡号无法核对。');
      const account=index.accounts.get(card.account_id),facility=index.credit_facilities.get(card.facility_id);
      if(card.card_type==='virtual'&&(card.network!=='SIM'||account.account_type!=='bank_settlement'||card.facility_id!=null||card.registration_only===true))fail('虚拟卡来源、账户或无授信条件无法核对。');
      if (card.card_type === 'credit' && (!facility || account.account_type !== 'credit_card') || card.card_type === 'debit' && account.account_type === 'credit_card' ||
          facility && facility.account_id !== account.account_id) fail('银行卡与账户或授信关联不一致。');
    }
    for (const facility of data.credit_facilities) if (!minor(facility.outstanding_minor) || facility.outstanding_minor < 0 || !minor(facility.limit_minor) || facility.limit_minor < 0 || index.accounts.get(facility.account_id).account_type !== 'credit_card') fail('授信金额或所属账户无法核对。');
    for (const account of data.accounts.filter(row => row.account_type === 'credit_card')) if (-account.balance_minor !== total(data.credit_facilities.filter(row => row.account_id === account.account_id).map(row => row.outstanding_minor))) fail('信用卡账务余额与授信欠款不一致。');
    for (const position of data.positions) {
      const contract=index.contracts.get(position.contract_id);
      if (contract.account_id !== position.account_id || contract.asset_id !== position.asset_id || !minor(position.book_value_minor) || position.book_value_minor < 0 || !minor(position.cost_basis_minor) || position.cost_basis_minor < 0) fail('持仓金额、账户与合同关联不一致。');
    }
    for (const asset of data.assets) {
      const excluded=asset.asset_type==='crop_exposure' && asset.included_in_total_assets===false && asset.details?.not_added_to_assets===true && asset.book_value_minor===null;
      if (!identifier(asset.asset_type) || !excluded && (!minor(asset.book_value_minor) || asset.book_value_minor < 0 || asset.included_in_total_assets===false)) fail('资产类型或估值未完整取得。');
    }
    const summary=data.summary,keys=['cash_minor','investment_minor','real_assets_minor','liabilities_minor','loan_principal_minor','credit_card_outstanding_minor','pending_redemption_minor','total_assets_minor','net_worth_minor'];
    if (!summary || keys.some(key => !minor(summary[key]) || key !== 'net_worth_minor' && summary[key] < 0)) fail('当前资产汇总未完整取得。');
    const cash=total(data.accounts.filter(row => row.account_type !== 'credit_card').map(row => row.balance_minor));
    const credit=total(data.credit_facilities.map(row => row.outstanding_minor));
    if (cash !== summary.cash_minor || credit !== summary.credit_card_outstanding_minor || total([summary.loan_principal_minor,credit]) !== summary.liabilities_minor ||
        total([summary.cash_minor,summary.investment_minor,summary.real_assets_minor,summary.pending_redemption_minor]) !== summary.total_assets_minor ||
        total([summary.total_assets_minor,-summary.liabilities_minor]) !== summary.net_worth_minor || total(data.positions.map(row => row.book_value_minor)) !== summary.investment_minor ||
        total(data.assets.filter(row => row.asset_type.startsWith('fund_')).map(row => row.book_value_minor)) !== summary.investment_minor ||
        total(data.assets.filter(row => !row.asset_type.startsWith('fund_') && row.included_in_total_assets!==false).map(row => row.book_value_minor)) !== summary.real_assets_minor) fail('账户、授信、持仓与资产汇总无法核对。');
    return value;
  }
  async function read(path, validator, {fresh=false}={}) {
    await BankClient.ensure();
    const identity=BankClient.identity(),guard=BankClient.guard();
    if (!identity || !guard.valid()) fail('账户或资料权限已变化，请重新读取。');
    const keyOwner=identity.workspace_id+':'+identity.account_id+':'+identity.privacy_revision;
    if (owner !== keyOwner) { clear(); owner=keyOwner; }
    const epoch=generation,key=keyOwner+path;
    let request=fresh ? null : pending.get(key);
    if (!request) { request=BankClient.get(path); if (!fresh) pending.set(key,request); }
    try {
      const response=await request;
      if (epoch !== generation || !guard.valid({workspace_id:response.workspace_id || response.workspace_binding,privacy_revision:response.privacy_revision})) fail('账户或资料权限已变化，已丢弃旧响应。');
      validator(response,identity);
      if (response.version < latestVersion) fail('当前账本已更新，已丢弃旧版本。');
      latestVersion=Math.max(latestVersion,response.version);
      return response;
    } finally { if (pending.get(key) === request) pending.delete(key); }
  }
  function overview(options) { return read('/connected-assets',validate,options); }
  function transactions(parameters={},options) {
    const allowed=['month','account_id','limit','offset','kind'];
    if (Object.keys(parameters).some(key => !allowed.includes(key))) fail('当前流水参数无效。');
    const {month,account_id,limit=50,offset=0,kind=null}=parameters;
    if(kind!==null&&!transactionKinds.includes(kind))fail('当前流水类型无效。');
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month || '') || !minor(limit) || limit < 1 || limit > 100 || !minor(offset) || offset < 0 || offset > 10000 || account_id != null && !identifier(account_id)) fail('当前流水月份、账户或分页无效。');
    const query=new URLSearchParams({month,limit:String(limit),offset:String(offset),...(account_id ? {account_id} : {}),...(kind ? {kind} : {})});
    return read('/connected-assets/transactions?'+query,(response,identity) => {
      binding(response,identity); const data=response.data;
      if((kind!==null || data.kind!==undefined)&&data.kind!==kind || data.available_kinds!==undefined&&(!Array.isArray(data.available_kinds)||data.available_kinds.some(value=>!transactionKinds.includes(value))||new Set(data.available_kinds).size!==data.available_kinds.length))fail('当前流水类型筛选无法核对。');
      if (!Array.isArray(data.items) || data.items.length > limit || !minor(data.total) || data.total < 0 || data.items.length!==Math.min(limit,Math.max(0,data.total-offset)) || data.offset !== offset || data.limit !== limit || data.month !== month || data.account_id !== (account_id || null) || typeof data.has_more !== 'boolean' || data.has_more !== (offset+data.items.length < data.total)) fail('当前流水分页未完整取得。');
      const ids=new Set();
      for (const row of data.items) {
        if (!identifier(row.transaction_id || row.id) || ids.has(row.transaction_id || row.id) || !identifier(row.account_id) || account_id && row.account_id !== account_id ||
            !minor(row.signed_minor) || row.currency !== 'CNY' || !iso(row.occurred_at) || row.occurred_at.slice(0,7) !== month || kind!==null&&(row.kind==='external_transfer'?'transfer':row.kind)!==kind) fail('当前流水的金额、时间、类型或账户无法核对。');
        ids.add(row.transaction_id || row.id);
      }
      return response;
    },options);
  }
  function reference(envelope,kind,id,extra={}) {
    if (!identifier(id) || !['account','card','holding','asset','contract','policy','institution'].includes(kind) || kind==='institution' && !['alipay','wechat'].includes(id)) fail('当前资产引用无效。');
    return {...extra,source_scope:scope,object_version:envelope.dataset_version,object_kind:kind,object_id:id};
  }
  const connectionChannels=Object.freeze({payment:Object.freeze({wechat:'微信支付',alipay:'支付宝'}),securities:Object.freeze({citic:'中信证券',huatai:'华泰证券'})});
  function connectionPair(kind,institution) {
    if(!Object.hasOwn(connectionChannels,kind) || institution!=null&&!Object.hasOwn(connectionChannels[kind],institution))fail('平台关联渠道无效。');
  }
  function connectionBinding(value,kind,institution) {
    connectionPair(kind,institution);
    if(!value || !/^[a-f0-9]{32}$/.test(value.id||'') || value.kind!==kind || !Object.hasOwn(connectionChannels[kind],value.institution) || institution!=null&&value.institution!==institution ||
       value.institution_name!==connectionChannels[kind][value.institution] || !identifier(value.connection_id) || !['bound','disconnected'].includes(value.status) || value.active!==(value.status==='bound') ||
       !['migrated','local_initialized'].includes(value.source) || value.environment!=='simulation' || value.real_connection!==false || !Array.isArray(value.accounts) || !value.accounts.length || value.accounts.length>200)fail('平台关联资料无法核对。');
    const ids=new Set();
    for(const account of value.accounts) {
      if(!identifier(account?.account_id) || account.account_id.includes('::') || ids.has(account.account_id) || !identifier(account.name) || account.account_type!==(kind==='payment'?'payment_wallet':'securities_cash') ||
         account.currency!=='CNY' || !minor(account.balance_minor) || account.balance_minor<0 || account.connection_status!==value.status || account.connection_active!==value.active)fail('平台账户资料无法核对。');
      ids.add(account.account_id);
    }
    return value;
  }
  function validateConnections(value,kind,institution=null) {
    connectionPair(kind,institution);
    if(!value || value.environment!=='simulation' || value.kind!==kind || value.institution!==institution || !Array.isArray(value.channels) || !Array.isArray(value.bindings) || !Array.isArray(value.active_bindings))fail('平台关联目录未完整取得。');
    const expected=Object.keys(connectionChannels[kind]).filter(key=>institution===null||key===institution),byInstitution=new Map(),ids=new Set();
    for(const item of value.bindings) {connectionBinding(item,kind,institution);if(byInstitution.has(item.institution)||ids.has(item.id))fail('平台关联编号重复。');byInstitution.set(item.institution,item);ids.add(item.id);}
    if(value.channels.length!==expected.length || new Set(value.channels.map(row=>row?.institution)).size!==expected.length)fail('平台渠道目录未完整取得。');
    for(const channel of value.channels) {
      const bound=byInstitution.get(channel?.institution);
      if(!expected.includes(channel?.institution) || channel.kind!==kind || channel.institution_name!==connectionChannels[kind][channel.institution] || channel.mode!=='local_simulation' || channel.can_bind!==true ||
         channel.status!==(bound?.status||'unbound') || channel.binding_id!==(bound?.id||null) || channel.needs_initialization!==!bound)fail('平台关联状态无法核对。');
    }
    if(value.bindings.length>expected.length || value.active_bindings.length!==value.bindings.filter(row=>row.active).length || new Set(value.active_bindings.map(row=>row?.id)).size!==value.active_bindings.length)fail('有效平台关联无法核对。');
    for(const active of value.active_bindings) {connectionBinding(active,kind,institution);const item=byInstitution.get(active.institution),same=item&&['id','connection_id','kind','institution','institution_name','status','active','source','environment','real_connection'].every(key=>active[key]===item[key])&&active.accounts.length===item.accounts.length&&active.accounts.every(account=>{const source=item.accounts.find(row=>row.account_id===account.account_id);return source&&['name','account_type','currency','balance_minor','connection_status','connection_active'].every(key=>account[key]===source[key]);});if(!active.active || !same)fail('有效平台关联与账户目录不一致。');}
    return value;
  }
  async function connectionRequest(path,body,validator) {
    await BankClient.ensure();const identity=BankClient.identity(),guard=BankClient.guard(),epoch=generation;
    if(!identity || !identifier(identity.workspace_id) || !identifier(identity.account_id) || !minor(identity.privacy_revision) || !guard.valid())fail('当前体验身份无法核对。');
    const value=await (body?BankClient.post(path,body):BankClient.get(path));
    const current=BankClient.identity();
    if(epoch!==generation || !guard.valid() || current?.workspace_id!==identity.workspace_id || current?.account_id!==identity.account_id || current?.privacy_revision!==identity.privacy_revision || value?.workspace_id!=null&&value.workspace_id!==identity.workspace_id || value?.workspace_binding!=null&&value.workspace_binding!==identity.workspace_id ||
       value?.privacy_revision!=null&&value.privacy_revision!==identity.privacy_revision || value?.current_account_id!=null&&value.current_account_id!==identity.account_id || value?.subject_id!=null&&value.subject_id!==identity.account_id)fail('账户或资料权限已变化，已丢弃旧响应。');
    validator(value);return value;
  }
  function connections(parameters={}) {
    if(!parameters||Object.keys(parameters).some(key=>!['kind','institution'].includes(key)))fail('平台关联查询参数无效。');const {kind='payment',institution=null}=parameters;
    connectionPair(kind,institution);const query=new URLSearchParams({kind,...(institution===null?{}:{institution})});
    return connectionRequest('/account-connections?'+query,null,value=>validateConnections(value,kind,institution));
  }
  async function changeConnection(kind,institution,confirmed,bindingId=null) {
    connectionPair(kind,institution);if(institution==null || confirmed!==true || bindingId!==null&&!/^[a-f0-9]{32}$/.test(bindingId))fail('请明确核对并确认平台关联。');
    const value=await connectionRequest(bindingId===null?'/account-connections':'/account-connections/'+bindingId+'/disconnect',bindingId===null?{kind,institution,confirmed:true}:{confirmed:true},value=>{
      if(!value || value.environment!=='simulation' || typeof value.changed!=='boolean' || typeof value.account_created!=='boolean')fail('尚未取得可核对的关联操作结果。');
      const result=connectionBinding(value.binding,kind,institution);
      if(result.status!==(bindingId===null?'bound':'disconnected') || bindingId!==null&&(result.id!==bindingId||value.account_created) || value.account_created&&(!value.changed||result.source!=='local_initialized'||result.accounts.some(row=>row.balance_minor!==0)))fail('关联操作结果与已确认内容不一致。');
    });
    clear();return value;
  }
  async function bindCard(parameters) {
    const fields=['account_id','card_number','confirmed','expected_version','expected_workspace_id','expected_subject_id','expected_privacy_revision','idempotency_key'];
    if(!parameters||Object.keys(parameters).length!==fields.length||Object.keys(parameters).some(key=>!fields.includes(key))||!identifier(parameters.account_id)||!identifier(parameters.idempotency_key)||typeof parameters.card_number!=='string'||!/^[\d ]+$/.test(parameters.card_number)||!/^\d{16,19}$/.test(parameters.card_number.replace(/ /g,''))||parameters.confirmed!==true||!minor(parameters.expected_version)||parameters.expected_version<0||!minor(parameters.expected_privacy_revision))fail('请完整核对本人借记卡资料。');
    const identity=BankClient.identity();if(parameters.expected_workspace_id!==identity?.workspace_id||parameters.expected_subject_id!==identity?.account_id||parameters.expected_privacy_revision!==identity?.privacy_revision)fail('当前身份或授权已变化，请重新核对银行卡。');
    const value=await connectionRequest('/card-bindings',parameters,result=>{
      const card=result?.card;
      if(!result||result.environment!=='simulation'||result.real_connection!==false||typeof result.changed!=='boolean'||typeof result.replayed!=='boolean'||result.workspace_id!==identity.workspace_id||result.workspace_binding!==identity.workspace_id||result.subject_id!==identity.account_id||result.current_account_id!==identity.account_id||result.privacy_revision!==identity.privacy_revision||!minor(result.version)||result.version<parameters.expected_version||!identifier(card?.card_id)||card.account_id!==parameters.account_id||card.card_type!=='debit'||card.status!=='active'||card.masked_number!=='**** **** **** '+parameters.card_number.replace(/ /g,'').slice(-4))fail('绑定回执的身份、卡片或版本无法核对，请核对原登记。');
    });
    clear();return value;
  }
  function securitiesSummary(envelope) {
    validate(envelope);const data=envelope.data,accounts=data.accounts.filter(row=>row.account_type==='securities_cash');
    const rows=accounts.map(account=>{const positions=data.positions.filter(row=>row.account_id===account.account_id),investment=total(positions.map(row=>row.book_value_minor));return {account,positions,cash_minor:account.balance_minor,investment_minor:investment,total_minor:total([account.balance_minor,investment])};});
    return {accounts:rows,broker_count:new Set(accounts.map(row=>data.connections.find(connection=>connection.connection_id===row.connection_id)?.institution_name||row.connection_id)).size,cash_minor:total(rows.map(row=>row.cash_minor)),investment_minor:total(rows.map(row=>row.investment_minor)),total_minor:total(rows.map(row=>row.total_minor)),valuation_at:envelope.valuation_at};
  }
  for (const event of ['navren:privacy-reset','navren:identity-revoked','navren:account-switched','pagehide']) window.addEventListener(event,clear);
  const transactionKinds=Object.freeze(['income','expense','refund','internal_transfer','investment_purchase','investment_redemption','loan_repayment','insurance_premium','transfer']);
  return {overview,transactions,reference,scope,clear,validate,transactionKinds,connections,changeConnection,bindCard,validateConnections,securitiesSummary};
})();

/* 当前资产详情与本地平台关联，金融余额和记录仍由账本读取。 */
window.NavrenConnectedAssetsUI = (() => {
  const F=NavrenConnectedAssets,mounts=new Map();
  const paymentPlatforms=Object.freeze([{id:'alipay',name:'支付宝',logo:'alipay.png'},{id:'wechat',name:'微信支付',logo:'wechat-pay-symbol.svg'}].map(Object.freeze));
  const securitiesBrands=Object.freeze([{id:'citic',name:'中信证券',logo:'citic-securities.png'},{id:'huatai',name:'华泰证券',logo:'huatai-securities.png'}].map(Object.freeze));
  const words={'正在同步账户…':'Synchronizing account…','重新读取':'Reload','银行卡详情':'Card details','账户详情':'Account details','持仓详情':'Investment details','信用卡':'Credit card','当前虚拟卡不提供授信或对外支付能力。':'This virtual card does not provide credit or external payments.','虚拟卡':'Virtual card','借记卡':'Debit card','有效':'Active','已挂失':'Lost','已换卡':'Replaced','已关闭':'Closed','已过期':'Expired','未知':'Unknown','状态待核实':'Status unverified','账户现金':'Account cash','信用卡未还余额':'Credit card outstanding balance','所属账户':'Linked account','余额同步':'Balance synchronized','估值更新':'Valuation updated','账户与卡片信息':'Account and card information','币种':'Currency','账户状态':'Account status','脱敏卡号':'Masked card number','发行日期':'Issued on','停止使用日期':'Closed on','未记录':'Not recorded','卡片角色':'Card role','主卡':'Primary card','附属卡':'Supplementary card','角色待核实':'Role unverified','信用额度（不计资产）':'Credit limit (excluded from assets)','查看所属账户':'View account','所属账户流水':'Account transactions','没有已记录的交易。':'No recorded transactions.','交易记录':'Transactions','月份':'Month','上一页':'Previous','下一页':'Next','时间':'Date','交易对象':'Counterparty','类型':'Type','金额':'Amount','交易对象未取得':'Counterparty unavailable','已结算现金':'Settled cash','从此账户汇款':'Remit from this account','关联卡片':'Linked cards','关联持仓':'Linked investments','持仓估值':'Investment value','成本本金':'Cost basis','份额':'Units','估值来源':'Valuation source','账户估值':'Account valuation','承保机构':'Insurer','资产名称未取得':'Asset name unavailable','机构未取得':'Institution unavailable','当前账本记录':'Current ledger record','历史导入记录':'Imported historical record','查看历史档案':'View historical record'};
  const t=text => window.NavrenI18n?.language === 'en' ? (words[text] || window.NavrenI18n?.t?.(text) || text) : text;
  Object.assign(words,{'我的资金':'My funds','银行卡':'Bank cards','当前持仓':'Current investments','当前没有资金账户。':'No current funding accounts.','当前没有银行卡。':'No current bank cards.','当前没有持仓。':'No current investments.'});
  Object.assign(words,{'我的银行卡':'My bank cards','申请银行卡':'Apply for a bank card','卡片类型':'Card type','卡片状态':'Card status','全部卡片':'All cards','全部状态':'All statuses','有效卡片':'Active cards','其他状态':'Other statuses','查看详情':'View details','没有符合筛选条件的银行卡。':'No bank cards match these filters.','选择申请账户':'Choose an application account','所属银行账户':'Linked bank account','继续申请':'Continue application','申请记录':'Applications','申请类型':'Card type','申请日期':'Application date','申请状态':'Application status','已受理':'Received','已发卡':'Issued','被拒绝':'Declined','未核实':'Unverified','账户已更新，请重新读取后再申请。':'The account has changed. Reload before applying.','当前没有可申请的银行结算账户。':'No eligible bank settlement account is available.','申请提交后等待审核，不代表银行卡已核发。':'The application awaits review; submission does not mean the card has been issued.'});
  Object.assign(words,{'绑定银行卡':'Link a bank card','登记本人借记卡':'Register your debit card','登记本人已有的借记卡；账户关联不代表已取得银行授权。':'Register an existing debit card; linking it does not grant bank authorization.','银行卡号':'Bank card number','请输入本人借记卡号':'Enter your debit card number','核对银行卡':'Review bank card','返回修改':'Edit details','确认绑定':'Confirm link','核对原登记':'Check original registration','重新读取银行卡':'Reload bank cards','我确认这是本人的借记卡':'I confirm this is my own debit card','所属账户':'Linked account','开户银行':'Bank','请输入16～19位数字银行卡号。':'Enter a bank card number of 16–19 digits.','已绑定，正在重新读取银行卡…':'Linked; reloading bank cards…','结果尚未核实，将使用原编号核对登记。':'The result is unverified. The original registration reference will be used.','绑定已完成，银行卡列表暂未读到，请重新读取。':'The card was linked, but the list could not be reloaded. Please reload it.','当前没有可绑定的银行结算账户。':'No eligible bank settlement account is available for linking.'});
  Object.assign(words,{'本人登记':'Self-registered','银行资料待核验':'Bank details unverified','登记日期':'Registered on','交易已停用':'Transactions disabled'});
  Object.assign(words,{'支付宝':'Alipay','微信支付':'WeChat Pay','未绑定':'Not linked','已绑定':'Linked','无法核实':'Unable to verify','当前用户尚未绑定该支付账户。':'You have not linked an account with this payment platform.','绑定后可查看余额与交易记录。':'Once linked, you can view its balance and transaction history.','已绑定的支付账户':'Linked payment accounts','账户余额':'Account balance','返回我的账户':'Back to my accounts','暂时无法核实该支付平台的绑定状态。':'The payment platform link status could not be verified.','支付平台引用无效，请重新选择。':'The payment platform reference is invalid. Please select it again.'});
  Object.assign(words,{'已解除':'Unlinked','绑定支付账户':'Link payment account','重新绑定':'Link again','解除绑定':'Unlink','核对绑定信息':'Review account link','核对解除信息':'Review unlinking','确认绑定':'Confirm link','确认解除':'Confirm unlinking','取消':'Cancel','关闭':'Close','处理中…':'Processing…','重新读取状态':'Check link status','当前体验身份':'Current demo identity','读取范围':'Data scope','账户余额与交易记录':'Account balance and transactions','证券现金、持仓与交易记录':'Securities cash, investments and transactions','仅在体验环境建立关联':'Link within the demo environment','实际平台授权尚未接入。':'Real platform authorization is not connected.','仅解除关联，账户资金、流水与持仓保留。':'Unlinking preserves account balances, transactions and investments.','初始账户余额':'Initial account balance','创建独立体验账户，不生成持仓与历史记录。':'Create a separate demo account without investments or historical records.','我的证券':'My securities','现金与持仓合计':'Cash and investments total','证券现金':'Securities cash','证券投资':'Securities investments','绑定证券账户':'Link securities account','券商':'Broker','券商账户':'Brokerage accounts','中信证券':'CITIC Securities','华泰证券':'Huatai Securities','查看账户详情':'View account details','当前没有证券账户。':'No securities accounts.','关联已解除，账户资金与记录仍保留。':'The link has been removed; balances and records are preserved.','尚未取得关联状态，请重新读取。':'Link status is unavailable. Please check again.'});
  const n=(tag,text,cls) => { const node=document.createElement(tag);if(text!=null)node.textContent=t(String(text));if(cls)node.className=cls;return node; };
  const kinds={income:'收入',expense:'消费',refund:'退款',transfer:'转账',external_transfer:'转账',internal_transfer:'本人账户调拨',investment_purchase:'投资买入',investment_redemption:'投资赎回',loan_repayment:'贷款还款',insurance_premium:'保费'};
  Object.assign(words,{'全部类型':'All types','每页':'Per page','收入':'Income','消费':'Expense','退款':'Refund','转账':'Transfer','本人账户调拨':'Own-account transfer','投资买入':'Investment purchase','投资赎回':'Investment redemption','贷款还款':'Loan repayment','保费':'Insurance premium'});
  function choice(record,label,items,value,onChange,cls='') {
    const button=n('button',label,'cd-choice-button '+cls),menu=n('div',null,'cd-choice-menu'),abort=new AbortController();
    button.type='button';button.setAttribute('aria-haspopup','menu');button.setAttribute('aria-expanded','false');button.setAttribute('aria-label',label);menu.setAttribute('role','menu');menu.setAttribute('aria-label',label);menu.hidden=true;document.body.append(menu);
    const options=items.map(item=>{const option=n('button',item.label,'cd-choice-option');option.type='button';option.setAttribute('role','menuitemradio');option.setAttribute('aria-checked',String(item.value===value));option.dataset.value=String(item.value);option.onclick=()=>{close(true);if(item.value!==value)onChange(item.value);};menu.append(option);return option;});
    function close(focus=false){menu.hidden=true;button.setAttribute('aria-expanded','false');if(focus&&button.isConnected)button.focus();}
    function open(){if(!button.isConnected)return;for(const existing of document.querySelectorAll('.cd-choice-menu:not([hidden])'))existing.dispatchEvent(new Event('cd:close'));const box=button.getBoundingClientRect();menu.hidden=false;menu.style.left=Math.max(12,Math.min(box.left,window.innerWidth-menu.offsetWidth-12))+'px';menu.style.top=(box.bottom+8+menu.offsetHeight>window.innerHeight?Math.max(12,box.top-menu.offsetHeight-8):box.bottom+8)+'px';button.setAttribute('aria-expanded','true');(options.find(option=>option.getAttribute('aria-checked')==='true')||options[0])?.focus();}
    menu.addEventListener('cd:close',()=>close(),{signal:abort.signal});button.onclick=()=>menu.hidden?open():close(true);button.addEventListener('keydown',event=>{if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();open();}},{signal:abort.signal});
    menu.addEventListener('keydown',event=>{const index=options.indexOf(document.activeElement);if(event.key==='Escape'){event.preventDefault();close(true);}else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();options[event.key==='Home'?0:event.key==='End'?options.length-1:(index+(event.key==='ArrowDown'?1:-1)+options.length)%options.length]?.focus();}else if(event.key==='Tab')close();},{signal:abort.signal});
    document.addEventListener('pointerdown',event=>{if(!menu.hidden&&!menu.contains(event.target)&&!button.contains(event.target))close();},{signal:abort.signal});window.addEventListener('resize',()=>close(),{signal:abort.signal});document.addEventListener('scroll',event=>{if(!menu.contains(event.target))close();},{capture:true,signal:abort.signal});
    const destroy=()=>{abort.abort();menu.remove();record.cleanups.delete(destroy);};record.cleanups.add(destroy);return {button,destroy};
  }
  const money=(value,currency='CNY') => { if (!Number.isSafeInteger(value) || currency !== 'CNY') return t('未知');const absolute=BigInt(value)<0n?-BigInt(value):BigInt(value);return (value<0?'-':'')+(absolute/100n).toLocaleString('en-US')+'.'+String(absolute%100n).padStart(2,'0')+(window.NavrenI18n?.language==='en'?' CNY':' 元'); };
  const formatTime=value => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString(window.NavrenI18n?.language==='en'?'en-GB':'zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : t('未知');
  const status=value => t(({active:'有效',lost:'已挂失',replaced:'已换卡',closed:'已关闭',expired:'已过期'}[value] || '状态待核实'));
  const official={'工行合成连接':'中国工商银行','建行合成连接':'中国建设银行','农行合成连接':'中国农业银行','邮储合成连接':'中国邮政储蓄银行','招行合成连接':'招商银行','中行合成连接':'中国银行','农商行合成连接':'农村商业银行','中信证券合成连接':'中信证券','微信支付合成连接':'微信支付','支付宝合成连接':'支付宝','Navren合成承保机构':'承保机构','Navren 合成承保机构':'承保机构'};
  function valuationSource(value) { return ['mock_nav','navren_simulated_bank','synthetic_not_real_NAV'].includes(value)?t('账户估值'):value; }
  function institution(data,account) { const name=data.connections.find(row => row.connection_id===account.connection_id)?.institution_name;return official[name] || name || t('机构未取得'); }
  function link(text,view,envelope,kind,id,extra={}) {
    const metadata=kind ? F.reference(envelope,kind,id,extra) : {...extra,source_scope:F.scope},a=n('a',text,'account-view-link');
    a.href=NavrenViews.url({page:'account',view,...metadata});a.onclick=event => {if(event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)return;event.preventDefault();NavrenViews.open(view,metadata);};return a;
  }
  function fields(host,pairs) { const dl=n('dl',null,'account-definition-list');for(const [label,value] of pairs)dl.append(n('dt',label),n('dd',value==null?t('未知'):value));host.append(dl); }
  function panel(title,note) { const section=n('section',null,'cd-panel'),head=n('div',null,'cd-panel-heading');head.append(n('h3',title));if(note)head.append(n('p',note));section.append(head);return section; }
  function objectRow(host,envelope,title,amount,note,kind,id,view) {
    const row=link('',view,envelope,kind,id);row.className='account-object';const copy=n('span',null,'account-object-identity'),value=n('span',null,'account-object-amount');copy.append(n('strong',title),n('small',note));value.append(n('strong',amount));row.append(copy,value,n('span','›','account-object-arrow'));host.append(row);
  }
  function platformHeading(root,platform,state) {
    root.classList.add('cd-payment-platform');root.dataset.platform=platform.id;
    const heading=n('header',null,'cd-platform-heading'),logo=n('div',null,'cd-platform-logo'),symbol=n('span',null,'cd-platform-symbol'),image=n('img'),copy=n('div',null,'cd-platform-copy'),status=n('span',{bound:'已绑定',unbound:'未绑定',disconnected:'已解除',unverified:'无法核实'}[state],'cd-platform-status');
    image.src='assets/institutions/'+platform.logo;image.alt=t(platform.name);image.decoding='async';status.dataset.state=state;symbol.append(image);logo.append(symbol);copy.append(n('h2',platform.name),status);heading.append(logo,copy);root.append(heading);
  }
  function times(envelope) { const box=n('div',null,'cd-source');box.append(n('p',t('余额同步')+' '+formatTime(envelope.observed_at),'ah-report-basis'),n('p',t('估值更新')+' '+formatTime(envelope.valuation_at),'ah-report-basis'));return box; }
  function icon(name) { const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');for(const [key,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.5','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true'}))svg.setAttribute(key,value);for(const d of ({bank:['M3 10h18M4 10V8l8-5 8 5v2M6 10v9m6-9v9m6-9v9M3 21h18'],nfc:['M8 9a5 5 0 0 1 0 6','M11 6a9 9 0 0 1 0 12','M14 3a13 13 0 0 1 0 18']}[name])){const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',d);svg.append(path);}return svg; }
  function cardVisual(data,card) {
    const account=data.accounts.find(row => row.account_id===card.account_id),credit=card.card_type==='credit',facility=data.credit_facilities.find(row => row.facility_id===card.facility_id),bank=institution(data,account),type=card.card_type==='virtual'?'虚拟卡':credit?'信用卡':'借记卡';
    const face=n('article',null,'cd-bank-card');face.dataset.status=card.status;face.dataset.bankTheme=[[/中国银行/,'boc'],[/建设银行/,'ccb'],[/农业银行/,'abc'],[/工商银行/,'icbc'],[/招商银行/,'cmb'],[/邮政储蓄/,'psbc']].find(([pattern]) => pattern.test(bank))?.[1] || 'neutral';
    const wave=n('span',null,'cd-card-wave');wave.setAttribute('aria-hidden','true');const identity=n('div',null,'cd-card-bank');identity.append(icon('bank'),n('span',bank));const head=n('div',null,'cd-card-head'),typeLabel=n('span',type,'cd-card-type');if(card.status==='lost')typeLabel.append(n('span','已挂失','cd-card-lost-badge'));head.append(identity,typeLabel);
    const number=n('div',null,'cd-card-number-row'),chips=n('span',null,'cd-chip-group'),chip=n('span',null,'cd-chip');chip.setAttribute('aria-hidden','true');chips.append(chip,icon('nfc'));number.append(chips,n('span',card.masked_number,'cd-card-number'));
    const balance=n('div',null,'cd-card-balance');balance.append(n('span',t(credit?'信用卡未还余额':'账户现金')+(card.status==='lost'?' · '+t('交易已停用'):'')),n('strong',money(credit?facility.outstanding_minor:account.balance_minor,account.currency)));
    const foot=n('div',null,'cd-card-foot');foot.append(n('span','所属账户'),n('strong',account.name));face.append(wave,head,number,balance,foot);return face;
  }
  function cardApplicationAccounts(envelope) {
    const state=window.BankUI?.state?.(),identity=BankClient.identity(),data=envelope.data;
    if(!state||state.workspace_id!==identity?.workspace_id||state.workspace_id!==envelope.workspace_binding||state.version!==envelope.version||state.as_of!==envelope.as_of||!Array.isArray(state.accounts))return [];
    return data.accounts.filter(account=>{
      const current=state.accounts.find(row=>row.id===account.account_id),connection=data.connections.find(row=>row.connection_id===account.connection_id);
      return account.account_type==='bank_settlement'&&account.status==='active'&&account.currency==='CNY'&&current?.currency==='CNY'&&connection?.institution_type==='bank'&&connection.status==='connected';
    });
  }
  function showCardApplication(record,envelope,valid,trigger,notice) {
    if(!valid()||document.querySelector('dialog[open]'))return;
    const accounts=cardApplicationAccounts(envelope);
    if(!accounts.length||typeof window.BankUI?.cardApplicationForm!=='function'){notice.textContent=t('账户已更新，请重新读取后再申请。');return;}
    const dialog=n('dialog',null,'cd-connection-dialog cd-card-application-dialog'),shell=n('div',null,'cd-connection-shell'),header=n('header',null,'cd-connection-dialog-heading'),title=n('h2','选择申请账户'),body=n('div',null,'cd-connection-dialog-body'),label=n('label',null,'cd-connection-field'),select=n('select'),error=n('p','', 'cd-connection-error'),footer=n('footer',null,'cd-connection-actions');
    title.id='cdCardApplicationHeading';dialog.setAttribute('aria-labelledby',title.id);dialog.dataset.i18nSkip='true';header.append(title,n('p','申请提交后等待审核，不代表银行卡已核发。','cd-connection-caption'));label.append(n('span','所属银行账户'));select.required=true;select.setAttribute('aria-label',t('所属银行账户'));
    for(const account of accounts){const option=n('option',institution(envelope.data,account)+' · '+account.name);option.value=account.account_id;select.append(option);}label.append(select);body.append(label,error);error.setAttribute('role','alert');let disposed=false;
    const active=()=>!disposed&&valid()&&dialog.isConnected&&dialog.open;
    function dispose(restore=false){if(disposed)return;disposed=true;record.cleanups.delete(dispose);if(dialog.open)dialog.close();dialog.remove();if(restore&&valid()&&trigger.isConnected)trigger.focus();}
    const cancel=connectionButton('取消',()=>dispose(true)),next=connectionButton('继续申请',()=>{
      if(!active()||!select.reportValidity())return;
      const selected=cardApplicationAccounts(envelope).find(account=>account.account_id===select.value);
      if(!selected){error.textContent=t('账户已更新，请重新读取后再申请。');return;}
      dispose(false);if(valid())BankUI.cardApplicationForm(selected.account_id);
    },true);footer.append(cancel,next);shell.append(header,body,footer);dialog.append(shell);dialog.addEventListener('cancel',event=>{event.preventDefault();dispose(true);});dialog.addEventListener('click',event=>{if(event.target===dialog)dispose(true);});dialog.addEventListener('close',()=>dispose(false),{once:true});record.cleanups.add(dispose);document.body.append(dialog);dialog.showModal();select.focus();
  }
  function showCardBinding(record,host,envelope,valid,trigger,notice) {
    if(!valid()||document.querySelector('dialog[open]'))return;
    const accounts=cardApplicationAccounts(envelope);if(!accounts.length){notice.textContent=t('当前没有可绑定的银行结算账户。');return;}
    const dialog=n('dialog',null,'cd-connection-dialog cd-card-binding-dialog'),shell=n('div',null,'cd-connection-shell'),header=n('header',null,'cd-connection-dialog-heading'),title=n('h2','登记本人借记卡'),body=n('div',null,'cd-connection-dialog-body'),form=n('form'),accountLabel=n('label',null,'cd-connection-field'),select=n('select'),numberLabel=n('label',null,'cd-connection-field'),number=n('input'),review=n('div',null,'cd-connection-review'),ownership=n('label',null,'cd-card-ownership'),checkbox=n('input'),statusLine=n('p','', 'cd-connection-notice'),error=n('p','', 'cd-connection-error'),footer=n('footer',null,'cd-connection-actions');
    title.id='cdCardBindingHeading';dialog.setAttribute('aria-labelledby',title.id);dialog.dataset.i18nSkip='true';header.append(title,n('p','登记本人已有的借记卡；账户关联不代表已取得银行授权。','cd-connection-caption'));select.required=true;select.setAttribute('aria-label',t('所属银行账户'));accountLabel.append(n('span','所属银行账户'),select);
    for(const account of accounts){const option=n('option',institution(envelope.data,account)+' · '+account.name);option.value=account.account_id;select.append(option);}number.type='text';number.name='card_number';number.inputMode='numeric';number.autocomplete='off';number.required=true;number.maxLength=23;number.placeholder=t('请输入本人借记卡号');number.setAttribute('aria-label',t('银行卡号'));numberLabel.append(n('span','银行卡号'),number);form.append(accountLabel,numberLabel);
    checkbox.type='checkbox';checkbox.required=true;checkbox.setAttribute('aria-label',t('我确认这是本人的借记卡'));ownership.append(checkbox,n('span','我确认这是本人的借记卡'));review.hidden=ownership.hidden=true;statusLine.setAttribute('role','status');error.setAttribute('role','alert');body.append(form,review,ownership,statusLine,error);
    let disposed=false,busy=false,draft=null,unknown=false,registered=false;
    const active=()=>!disposed&&valid()&&dialog.isConnected&&dialog.open;
    function dispose(restore=false){if(disposed)return;disposed=true;draft=null;number.value='';record.cleanups.delete(dispose);if(dialog.open)dialog.close();dialog.remove();if(restore&&valid()&&trigger.isConnected)trigger.focus();}
    const cancel=connectionButton('取消',()=>{if(!busy)dispose(true);}),back=connectionButton('返回修改',()=>{
      if(!active()||busy||unknown||registered)return;number.value=draft?.card_number||'';draft=null;checkbox.checked=false;review.hidden=ownership.hidden=true;form.hidden=false;back.hidden=true;confirm.textContent=t('核对银行卡');title.textContent=t('登记本人借记卡');error.textContent=statusLine.textContent='';number.focus();
    }),confirm=connectionButton('核对银行卡',()=>{if(draft)submit();else form.requestSubmit();},true);back.hidden=true;footer.append(cancel,back,confirm);
    function lock(value){busy=value;cancel.disabled=back.disabled=confirm.disabled=select.disabled=number.disabled=checkbox.disabled=value;confirm.textContent=t(value?'处理中…':registered?'重新读取银行卡':unknown?'核对原登记':draft?'确认绑定':'核对银行卡');}
    form.onsubmit=event=>{
      event.preventDefault();if(!active()||busy)return;error.textContent='';const digits=number.value.replace(/ /g,''),selected=cardApplicationAccounts(envelope).find(account=>account.account_id===select.value),identity=BankClient.identity();
      if(!/^\d{16,19}$/.test(digits)){error.textContent=t('请输入16～19位数字银行卡号。');number.focus();return;}
      if(!selected){error.textContent=t('账户已更新，请重新读取后再申请。');return;}
      draft={account_id:selected.account_id,card_number:digits,confirmed:true,expected_version:envelope.version,expected_workspace_id:identity.workspace_id,expected_subject_id:identity.account_id,expected_privacy_revision:identity.privacy_revision,idempotency_key:BankClient.key()};
      review.replaceChildren();fields(review,[['开户银行',institution(envelope.data,selected)],['所属账户',selected.name],['脱敏卡号','**** **** **** '+digits.slice(-4)],['卡片类型','借记卡']]);number.value='';form.hidden=true;review.hidden=ownership.hidden=false;back.hidden=false;title.textContent=t('核对银行卡');confirm.textContent=t('确认绑定');checkbox.focus();
    };
    async function refreshCards(){
      try{if(typeof window.BankUI?.load==='function')await BankUI.load();if(!active())return;dispose(false);if(valid())await render(host,record.context,record.isCurrent);}
      catch(failure){if(active()){error.textContent=t('绑定已完成，银行卡列表暂未读到，请重新读取。');statusLine.textContent='';}}
    }
    async function submit(){
      if(!active()||busy||!draft)return;
      if(registered){lock(true);await refreshCards();if(active())lock(false);return;}
      if(!checkbox.checked){checkbox.reportValidity();checkbox.focus();return;}
      if(!unknown&&!cardApplicationAccounts(envelope).some(account=>account.account_id===draft.account_id)){error.textContent=t('账户已更新，请重新读取后再申请。');return;}
      lock(true);error.textContent='';
      try{await F.bindCard(draft);if(!active())return;registered=true;unknown=false;draft.card_number='';back.hidden=true;ownership.hidden=true;statusLine.textContent=t('已绑定，正在重新读取银行卡…');await refreshCards();}
      catch(failure){if(active()){unknown=!(Number.isInteger(failure.status)&&failure.status>=400&&failure.status<500&&![408,425,429].includes(failure.status));back.hidden=unknown;error.textContent=failure.message;statusLine.textContent=unknown?t('结果尚未核实，将使用原编号核对登记。'):'';}}
      finally{if(active())lock(false);}
    }
    shell.append(header,body,footer);dialog.append(shell);dialog.addEventListener('cancel',event=>{event.preventDefault();if(!busy)dispose(true);});dialog.addEventListener('click',event=>{if(event.target===dialog&&!busy)dispose(true);});dialog.addEventListener('close',()=>dispose(false),{once:true});record.cleanups.add(dispose);document.body.append(dialog);dialog.showModal();select.focus();
  }
  function bankCards(record,root,envelope,valid) {
    const data=envelope.data,rows=data.cards,heading=n('header',null,'cd-heading cd-cards-heading'),copy=n('div'),actions=n('div',null,'cd-cards-actions'),notice=n('p','', 'cd-cards-notice'),filters=n('div',null,'cd-cards-filters'),grid=n('div',null,'cd-card-grid'),result=n('p','', 'cd-cards-result'),activeCard=card=>card.status==='active'&&!card.registration_only,activeCount=rows.filter(activeCard).length;
    root.classList.add('cd-cards');copy.append(n('h2','我的银行卡'),n('p',window.NavrenI18n?.language==='en'?`${rows.length} cards · ${activeCount} active`:`${rows.length} 张银行卡 · ${activeCount} 张有效卡`,'cd-cards-count'));
    const binding=connectionButton('绑定银行卡',()=>showCardBinding(record,root.parentElement,envelope,valid,binding,notice)),application=connectionButton('申请银行卡',()=>showCardApplication(record,envelope,valid,application,notice),true);actions.append(binding,application);heading.append(copy,actions);root.append(heading,filters,notice,result,grid);
    notice.setAttribute('role','status');result.setAttribute('aria-live','polite');
    const options={type:[['all','全部卡片'],['debit','借记卡'],['credit','信用卡'],['virtual','虚拟卡']],status:[['all','全部状态'],['active','有效卡片'],['other','其他状态']]},saved=record.context.filters||{},selection={type:options.type.some(([value])=>value===saved.card_type)?saved.card_type:'all',status:options.status.some(([value])=>value===saved.card_status)?saved.card_status:'all'},buttons=[];
    for(const [key,title]of [['type','卡片类型'],['status','卡片状态']]){const group=n('div',null,'cd-card-filter-group');group.setAttribute('role','group');group.setAttribute('aria-label',t(title));group.append(n('span',title,'cd-card-filter-label'));for(const [value,text]of options[key]){const button=connectionButton(text,()=>{if(!valid())return;selection[key]=value;record.context.filters={...record.context.filters,card_type:selection.type,card_status:selection.status};NavrenViews.update?.({filters:record.context.filters});paint();});button.dataset.cardFilter=key;button.dataset.value=value;buttons.push(button);group.append(button);}filters.append(group);}
    function paint(){
      for(const button of buttons)button.setAttribute('aria-pressed',String(selection[button.dataset.cardFilter]===button.dataset.value));
      const shown=rows.filter(card=>(selection.type==='all'||card.card_type===selection.type)&&(selection.status==='all'||selection.status==='active'&&activeCard(card)||selection.status==='other'&&!activeCard(card)));
      result.textContent=window.NavrenI18n?.language==='en'?`${shown.length} of ${rows.length} cards`:`显示 ${shown.length} / ${rows.length} 张银行卡`;grid.replaceChildren();
      if(!shown.length){grid.append(n('p',rows.length?'没有符合筛选条件的银行卡。':'当前没有银行卡。','cd-empty'));return;}
      for(const card of shown){const account=data.accounts.find(row=>row.account_id===card.account_id),tile=link('', 'card-detail',envelope,'card',card.card_id),meta=n('div',null,'cd-card-tile-meta'),badge=n('span',card.registration_only?'本人登记':status(card.status),'cd-status'),role=n('span',card.registration_only?'银行资料待核验':t(card.role==='primary'?'主卡':card.role==='supplementary'?'附属卡':'角色待核实'),'cd-card-role'),details=n('span',t('查看详情')+' ›','cd-card-details');tile.className='cd-card-tile';tile.dataset.cardId=card.card_id;tile.dataset.status=card.status;tile.setAttribute('aria-label',institution(data,account)+' · '+t(card.card_type==='virtual'?'虚拟卡':card.card_type==='credit'?'信用卡':'借记卡')+' · '+card.masked_number+' · '+(card.registration_only?t('本人登记')+' · '+t('银行资料待核验'):status(card.status)));badge.dataset.status=card.registration_only?'registered':card.status;meta.append(badge,role,details);tile.append(cardVisual(data,card),meta);grid.append(tile);}
    }
    paint();root.append(n('p',t('余额同步')+' '+formatTime(envelope.observed_at),'cd-cards-time'));
    const state=window.BankUI?.state?.();if(state?.workspace_id===envelope.workspace_binding&&state.version===envelope.version&&Array.isArray(state.card_applications)&&state.card_applications.length){const applications=panel('申请记录'),list=n('div',null,'cd-card-applications');for(const item of state.card_applications){const row=n('article',null,'cd-card-application'),type=item.requested_type==='virtual'?'虚拟卡':item.requested_type==='credit'?'信用卡':'借记卡',label={submitted:'已受理',issued:'已发卡',declined:'被拒绝'}[item.status]||'未核实';row.append(n('strong',type),n('span',label,'cd-status'),n('span',item.requested_on,'cd-card-application-date'));if(item.decision_reason)row.append(n('p',item.decision_reason));list.append(row);}applications.append(list);root.append(applications);}
  }
  function connectionSnapshot(value,envelope) {
    for(const binding of value.bindings) {
      const connection=envelope.data.connections.find(row=>row.connection_id===binding.connection_id);
      if(!connection || institution(envelope.data,{connection_id:binding.connection_id})!==binding.institution_name || connection.binding_id!=null&&connection.binding_id!==binding.id || connection.connection_active!=null&&connection.connection_active!==binding.active)throw Error('平台关联与当前账户目录不一致，请重新读取。');
      for(const row of binding.accounts) {
        const account=envelope.data.accounts.find(item=>item.account_id===row.account_id);
        if(!account || account.connection_id!==binding.connection_id || account.account_type!==row.account_type || account.balance_minor!==row.balance_minor || account.currency!==row.currency || account.binding_id!=null&&account.binding_id!==binding.id || account.connection_active!=null&&account.connection_active!==binding.active || account.connection_status!=null&&account.connection_status!==binding.status)throw Error('平台账户资料已更新，请重新读取。');
      }
    }
    for(const channel of value.channels.filter(row=>row.status==='unbound'))if(envelope.data.accounts.some(account=>account.account_type===(value.kind==='payment'?'payment_wallet':'securities_cash')&&institution(envelope.data,account)===channel.institution_name))throw Error('平台状态与已有账户无法核对，请重新读取。');
    return value;
  }
  function connectionButton(label,onClick,primary=false) {const button=n('button',label,'cd-connection-button'+(primary?' is-primary':''));button.type='button';button.onclick=onClick;return button;}
  async function currentActor(record,valid) {
    const identity=record.identity,data=await BankClient.get('/demo/accounts');if(!valid())throw Error('体验身份已变化。');
    const account=data?.accounts?.find(row=>row.user_id===identity.account_id);
    if(data?.environment!=='simulation'||data?.schema_version!==1||data?.data_scope!=='navren_synthetic'||data?.workspace_binding!==identity.workspace_id||data?.current_account_id!==identity.account_id||data?.privacy_revision!==identity.privacy_revision||account?.workspace_id!==identity.workspace_id||typeof account?.display_name!=='string'||!account.display_name.trim())throw Error('当前体验身份名称无法核对。');
    return account.display_name;
  }
  function showConnection(record,host,envelope,valid,{kind,institution:selected=null,bindingId=null},trigger) {
    if(!valid()||document.querySelector('dialog[open]'))return;
    const dialog=n('dialog',null,'cd-connection-dialog'),shell=n('div',null,'cd-connection-shell'),header=n('header',null,'cd-connection-dialog-heading'),title=n('h2',bindingId?'核对解除信息':'核对绑定信息'),caption=n('p','仅在体验环境建立关联','cd-connection-caption'),body=n('div',null,'cd-connection-dialog-body'),chooser=n('div'),review=n('div',null,'cd-connection-review'),notice=n('p','', 'cd-connection-notice'),error=n('p','', 'cd-connection-error'),actions=n('footer',null,'cd-connection-actions');
    title.id='cdConnectionHeading';dialog.setAttribute('aria-labelledby',title.id);dialog.dataset.i18nSkip='true';header.append(title,caption);body.append(chooser,review,notice,error);shell.append(header,body,actions);dialog.append(shell);error.setAttribute('role','alert');notice.setAttribute('role','status');
    let disposed=false,busy=false,unknown=false,actor='',directory=null,channel=null,select=null;
    const active=()=>!disposed&&valid()&&dialog.isConnected&&dialog.open;
    const cancel=connectionButton('取消',()=>finish()),query=connectionButton('重新读取状态',()=>loadState(true)),confirm=connectionButton(bindingId?'确认解除':'确认绑定',submit,true);query.hidden=true;confirm.disabled=true;actions.append(cancel,query,confirm);
    function dispose(restore=false) {if(disposed)return;disposed=true;record.cleanups.delete(dispose);if(dialog.open)dialog.close();dialog.remove();if(restore&&valid()&&trigger?.isConnected)trigger.focus();}
    function finish(){if(!busy)dispose(true);}
    function lock(value){busy=value;cancel.disabled=query.disabled=value;if(select)select.disabled=value;confirm.disabled=value||!channel||!actor||unknown||(bindingId?channel.status!=='bound':channel.status==='bound');confirm.textContent=t(value?'处理中…':bindingId?'确认解除':'确认绑定');}
    function paint() {
      review.replaceChildren();if(!channel)return;
      fields(review,[['当前体验身份',actor],['券商',channel.institution_name],['读取范围',kind==='payment'?'账户余额与交易记录':'证券现金、持仓与交易记录']]);review.querySelector('dt:nth-of-type(2)').textContent=t(kind==='payment'?'支付平台':'券商');
      notice.textContent=t(bindingId?'仅解除关联，账户资金、流水与持仓保留。':'实际平台授权尚未接入。');
      if(!bindingId&&channel.needs_initialization){fields(review,[['初始账户余额',money(0)]]);notice.textContent=t('创建独立体验账户，不生成持仓与历史记录。')+' '+t('实际平台授权尚未接入。');}
      if(bindingId&&channel.status!=='bound'||!bindingId&&channel.status==='bound')notice.textContent=t(bindingId?'关联已解除，账户资金与记录仍保留。':'已绑定');
      lock(false);
    }
    async function refreshPage() {dispose(false);if(valid()){await render(host,record.context,record.isCurrent);window.dispatchEvent(new CustomEvent('navren:account-connections-changed',{detail:{kind,institution:selected}}));}}
    async function loadState(afterUnknown=false) {
      if(!active()||busy)return;lock(true);error.textContent='';
      try {
        const values=await Promise.all([currentActor(record,active),F.connections({kind})]);if(!active())return;actor=values[0];directory=values[1];
        if(bindingId){const binding=directory.bindings.find(row=>row.id===bindingId);if(!binding)throw Error('原关联已不可用，请关闭后重新读取。');selected=binding.institution;}
        const candidates=directory.channels.filter(row=>row.can_bind&&row.status!=='bound');
        if(!selected)selected=candidates[0]?.institution||directory.channels[0]?.institution;
        channel=directory.channels.find(row=>row.institution===selected);if(!channel)throw Error('平台关联渠道尚未取得。');
        if(afterUnknown&&(bindingId?channel.status==='disconnected':channel.status==='bound')){await refreshPage();return;}
        if(kind==='securities'&&!bindingId&&!select){const label=n('label',null,'cd-connection-field');label.append(n('span','券商'));select=n('select');select.setAttribute('aria-label',t('券商'));for(const item of directory.channels){const option=n('option',item.institution_name+(item.status==='bound'?' · '+t('已绑定'):''));option.value=item.institution;option.disabled=item.status==='bound';select.append(option);}select.value=selected;select.onchange=()=>{if(!active()||busy)return;selected=select.value;channel=directory.channels.find(row=>row.institution===selected);unknown=false;error.textContent='';paint();};label.append(select);chooser.append(label);}
        unknown=false;query.hidden=true;confirm.hidden=false;paint();
      } catch(failure) {if(active()){unknown=true;error.textContent=failure.message;query.hidden=false;confirm.hidden=true;}}finally{if(active())lock(false);}
    }
    async function submit() {
      if(!active()||busy||unknown||!channel||!actor||confirm.disabled)return;lock(true);error.textContent='';
      const previous=directory.bindings.find(row=>row.institution===selected);
      try {
        const result=await F.changeConnection(kind,selected,true,bindingId);if(!active())return;
        if(previous&&(result.binding.id!==previous.id||result.binding.accounts.map(row=>row.account_id).sort().join('|')!==previous.accounts.map(row=>row.account_id).sort().join('|')))throw Error('关联操作未保留原账户，请重新读取结果。');
        await refreshPage();
      } catch(failure) {if(active()){unknown=true;confirm.hidden=true;query.hidden=false;error.textContent='尚未核实操作结果，请重新读取关联状态。'+failure.message;}}finally{if(active())lock(false);}
    }
    dialog.addEventListener('cancel',event=>{event.preventDefault();finish();});dialog.addEventListener('click',event=>{if(event.target===dialog)finish();});dialog.addEventListener('close',()=>dispose(false),{once:true});record.cleanups.add(dispose);document.body.append(dialog);dialog.showModal();cancel.focus();loadState();
  }
  function securitiesStats(host,values,compact=false) {
    const stats=n('dl',null,'cd-securities-stats'+(compact?' is-compact':''));
    for(const [label,key] of [['现金与持仓合计','total_minor'],['证券现金','cash_minor'],['证券投资','investment_minor']]){const item=n('div',null,key==='total_minor'?'is-total':'');item.dataset.metric=key;item.append(n('dt',label),n('dd',money(values[key])));stats.append(item);}host.append(stats);
  }
  function securitiesIdentity(host,name,accountName) {
    const brand=securitiesBrands.find(row=>row.name===name),heading=host.querySelector('.cd-panel-heading'),title=heading.querySelector('h3'),identity=n('div',null,'cd-broker-identity'),copy=n('div',null,'cd-broker-copy');
    host.dataset.brokerTheme=brand?.id||'neutral';title.classList.add('cd-broker-title');
    if(brand){const logo=n('div',null,'cd-broker-logo'),image=n('img');image.src='assets/institutions/'+brand.logo;image.alt=t(name);image.decoding='async';title.classList.add('is-logo-title');image.onerror=()=>{logo.hidden=true;title.classList.remove('is-logo-title');};logo.append(image);identity.append(logo);}
    copy.append(title,n('p',accountName,'cd-broker-account-name'));identity.append(copy);heading.replaceChildren(identity);
  }
  function historyLink(host,envelope,row,kind,view) {
    const sourceId=row['source_'+(kind==='holding'?'position':kind)+'_id'],version=envelope.historical_dataset_version || envelope.evidence?.historical_dataset_version;
    if (!sourceId || !version) return;
    const a=n('a','查看历史档案','account-view-link'),metadata={source_scope:'financial_history',object_kind:kind,object_id:sourceId,object_version:version};a.href=NavrenViews.url({page:'account',view,...metadata});a.onclick=event=>{if(event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)return;event.preventDefault();NavrenViews.open(view,metadata);};host.append(a);
  }
  function transactionsTable(host,envelope,rows,typeButton) {
    const scroll=n('div',null,'ah-table-scroll'),table=n('table',null,'ah-transactions'),thead=n('thead'),head=n('tr');for(const label of ['时间','交易对象','类型','金额']){const cell=n('th',typeButton&&label==='类型'?null:label);cell.scope='col';if(typeButton&&label==='类型')cell.append(typeButton);head.append(cell);}thead.append(head);table.append(thead);const body=n('tbody');
    for(const row of rows){const tr=n('tr'),date=n('td',formatTime(row.occurred_at)),party=n('td',row.counterparty || t('交易对象未取得')),type=n('td',t(kinds[row.kind] || row.kind || '未知')),value=n('td',money(row.signed_minor,row.currency),'ah-transaction-amount');date.title=t(row.historical_import===true?'历史导入记录':'当前账本记录');if(row.signed_minor>0)value.classList.add('is-inflow');tr.append(date,party,type,value);body.append(tr);}if(rows.length)table.append(body);scroll.append(table);host.append(scroll);
  }
  async function loadTransactions(host,envelope,parameters,valid,reload,typeControl) {
    host.replaceChildren(n('p','正在同步账户…','cd-loading'));
    try {
      const response=await F.transactions(parameters,{fresh:true});if(!valid())return;
      if(response.version !== envelope.version){const error=Error('账户账本已更新，请重新读取当前资料。');error.code='CONNECTED_VERSION_CHANGED';throw error;}
      if(response.data.items.some(row => !envelope.data.accounts.some(account => account.account_id===row.account_id)))throw Error('流水含当前目录之外的账户。');
      host.replaceChildren();if(response.data.items.length||typeControl)transactionsTable(host,envelope,response.data.items,typeControl?.(response.data));if(!response.data.items.length)host.append(n('p','没有已记录的交易。','cd-empty'));
      return response.data;
    } catch(error) { if(valid()){host.replaceChildren(n('p',error.message,'cd-empty'));const retry=n('button','重新读取','ah-small-button');retry.type='button';retry.onclick=()=>reload?reload():loadTransactions(host,envelope,parameters,valid,reload,typeControl);host.append(retry);} }
  }
  async function render(host,context={},isCurrent=()=>true) {
    for(const cleanup of mounts.get(host)?.cleanups||[])cleanup();const record={context:{...context},isCurrent,guard:null,cleanups:new Set()};mounts.set(host,record);host.replaceChildren(n('p','正在同步账户…','account-empty'));
    const valid=()=>{const identity=BankClient.identity();return mounts.get(host)===record && host.isConnected && isCurrent() && (!record.guard || record.guard.valid()) && (!record.identity||identity?.workspace_id===record.identity.workspace_id&&identity?.account_id===record.identity.account_id&&identity?.privacy_revision===record.identity.privacy_revision);};
    const platformView=context.view==='payment-platform',platform=platformView?paymentPlatforms.find(item=>item.id===context.object_id):null;
    try {
      if(platformView && (!platform || context.object_kind!=='institution' || context.source_scope!==F.scope)){const error=Error(t('支付平台引用无效，请重新选择。'));error.code='INVALID_PAYMENT_PLATFORM';throw error;}
      const expectedKind={'account-detail':'account','card-detail':'card','holding-detail':'holding'}[context.view];
      if(expectedKind&&context.object_kind&&context.object_kind!==expectedKind)throw Error('对象类型与当前详情不一致，请重新选择。');
      await BankClient.ensure();record.identity=BankClient.identity();record.guard=BankClient.guard();if(!valid())return;
      const envelope=await F.overview({fresh:true});if(!valid())return;
      const data=envelope.data,id=context.object_id,view=context.view;const root=n('div',null,'cd-detail');root.dataset.i18nSkip='true';host.replaceChildren(root);
      if(platformView) {
        const directory=connectionSnapshot(await F.connections({kind:'payment',institution:platform.id}),envelope);if(!valid())return;
        const channel=directory.channels[0],binding=directory.bindings[0],accounts=binding?binding.accounts.map(row=>data.accounts.find(account=>account.account_id===row.account_id)):[];platformHeading(root,platform,channel.status);
        const trigger=connectionButton(channel.status==='bound'?'解除绑定':channel.status==='disconnected'?'重新绑定':'绑定支付账户',()=>showConnection(record,host,envelope,valid,{kind:'payment',institution:platform.id,bindingId:channel.status==='bound'?binding.id:null},trigger),channel.status!=='bound');
        if(!accounts.length){const empty=n('section',null,'cd-payment-state');empty.dataset.state='unbound';empty.append(n('h3','当前用户尚未绑定该支付账户。'),n('p','绑定后可查看余额与交易记录。'),trigger);root.append(empty);}else{
          root.querySelector('.cd-platform-heading').append(trigger);const section=panel(channel.status==='bound'?'已绑定的支付账户':'支付账户');section.classList.add('cd-payment-accounts');for(const account of accounts)objectRow(section,envelope,account.name,money(account.balance_minor,account.currency),'账户余额','account',account.account_id,'account-detail');root.append(section);
          if(channel.status==='disconnected')root.append(n('p','关联已解除，账户资金与记录仍保留。','cd-connection-notice'));
          for(const account of accounts){const transactions=panel(accounts.length>1?account.name+' · '+t('交易记录'):'交易记录'),body=n('div',null,'cd-transactions-body');transactions.append(body);root.append(transactions);await loadTransactions(body,envelope,{account_id:account.account_id,month:envelope.as_of.slice(0,7),limit:8,offset:0},valid,()=>render(host,record.context,isCurrent));if(!valid())return;}
          root.append(n('p',t('余额同步')+' '+formatTime(envelope.observed_at),'cd-visual-note'));
        }
      } else if(view==='securities') {
        if(context.source_scope!=null&&context.source_scope!==F.scope)throw Error('证券账户资料来源无效，请重新选择。');
        const summary=F.securitiesSummary(envelope),heading=n('header',null,'cd-heading cd-securities-heading'),copy=n('div');copy.append(n('h2','我的证券'),n('p',window.NavrenI18n?.language==='en'?`${summary.broker_count} brokers · ${summary.accounts.length} accounts`:`${summary.broker_count} 家券商 · ${summary.accounts.length} 个账户`,'cd-securities-count'));heading.append(copy);root.append(heading);
        const overview=n('section',null,'cd-securities-overview');overview.setAttribute('aria-label',t('现金与持仓合计'));securitiesStats(overview,summary);overview.append(n('p',t('估值更新')+' '+formatTime(envelope.valuation_at),'cd-securities-time'));root.append(overview);
        let directory=null;try{directory=connectionSnapshot(await F.connections({kind:'securities'}),envelope);if(!valid())return;}catch(failure){if(!valid())return;const warning=n('div',null,'cd-connection-read-error');warning.append(n('p','尚未取得关联状态，请重新读取。'),n('p',failure.message));const retry=connectionButton('重新读取',()=>render(host,record.context,isCurrent));warning.append(retry);root.append(warning);}
        if(directory){const bound=n('span',window.NavrenI18n?.language==='en'?`${directory.active_bindings.length} linked brokers`:`已绑定 ${directory.active_bindings.length} 家`,'cd-securities-linked');copy.querySelector('.cd-securities-count').append(n('span',' · '),bound);const available=directory.channels.some(channel=>channel.can_bind&&channel.status!=='bound'),bind=connectionButton('绑定证券账户',()=>showConnection(record,host,envelope,valid,{kind:'securities'},bind),true);bind.disabled=!available;if(!available)bind.title=t('已绑定全部支持券商');heading.append(bind);}
        const grid=n('div',null,'cd-broker-grid');root.append(grid);if(!summary.accounts.length)grid.append(n('p','当前没有证券账户。','cd-empty'));
        for(const row of summary.accounts){const brokerName=institution(data,row.account),broker=panel(brokerName),binding=directory?.bindings.find(item=>item.accounts.some(account=>account.account_id===row.account.account_id));broker.classList.add('cd-broker-card');broker.dataset.accountId=row.account.account_id;securitiesIdentity(broker,brokerName,row.account.name);const badge=n('span',binding?binding.active?'已绑定':'已解除':'无法核实','cd-platform-status');badge.dataset.state=binding?binding.status:'unverified';broker.querySelector('.cd-panel-heading').append(badge);securitiesStats(broker,row,true);const footer=n('div',null,'cd-broker-actions');footer.append(link('查看账户详情','account-detail',envelope,'account',row.account.account_id));if(binding){const action=connectionButton(binding.active?'解除绑定':'重新绑定',()=>showConnection(record,host,envelope,valid,{kind:'securities',institution:binding.institution,bindingId:binding.active?binding.id:null},action));footer.append(action);}broker.append(footer);grid.append(broker);}
      } else if(view==='cards') {
        bankCards(record,root,envelope,valid);
      } else if(['funds','holdings'].includes(view)) {
        const title={funds:'我的资金',holdings:'当前持仓'}[view],rows={funds:data.accounts,holdings:data.positions}[view],heading=n('header',null,'cd-heading'),list=n('div',null,'account-object-list');heading.append(n('h2',title));root.append(heading,list);
        if(!rows.length)list.append(n('p',{funds:'当前没有资金账户。',holdings:'当前没有持仓。'}[view],'cd-empty'));
        for(const row of rows) {
          if(view==='funds') {
            const credit=row.account_type==='credit_card';objectRow(list,envelope,row.name,money(credit?-row.balance_minor:row.balance_minor,row.currency),institution(data,row)+' · '+t(credit?'信用卡未还余额':'已结算现金'),'account',row.account_id,'account-detail');
          } else {
            const asset=data.assets.find(item=>item.asset_id===row.asset_id),account=data.accounts.find(item=>item.account_id===row.account_id);objectRow(list,envelope,asset.name || t('资产名称未取得'),money(row.book_value_minor,account.currency),account.name+' · '+t('持仓估值'),'holding',row.position_id,'holding-detail');
          }
        }
        root.append(times(envelope));
      } else if(view==='card-detail') {
        const card=data.cards.find(row=>row.card_id===id);if(!card)throw Error('当前卡片不存在或不属于此账户。');const account=data.accounts.find(row=>row.account_id===card.account_id),credit=card.card_type==='credit',facility=data.credit_facilities.find(row=>row.facility_id===card.facility_id);
        const heading=n('header',null,'cd-heading'),copy=n('div'),badges=n('div',null,'cd-badges'),badge=n('span',card.registration_only?'本人登记':status(card.status),'cd-status');badge.dataset.status=card.registration_only?'registered':card.status;copy.append(n('p','银行卡详情','cd-eyebrow'),n('h2',institution(data,account)));badges.append(n('span',credit?'信用卡':'借记卡','cd-type'),badge);heading.append(copy,badges);root.append(heading);if(card.registration_only)root.append(n('p','银行资料待核验','cd-cards-notice'));
        const hero=n('div',null,'cd-hero'),visual=n('div',null,'cd-visual');visual.append(cardVisual(data,card),n('p',t('余额同步')+' '+formatTime(envelope.observed_at),'cd-visual-note'));hero.append(visual);
        const info=panel('账户与卡片信息');fields(info,[['所属账户',account.name],['币种',account.currency],['账户状态',status(account.status)],['脱敏卡号',card.masked_number],['卡片角色',t(card.registration_only?'角色待核实':card.role==='primary'?'主卡':card.role==='supplementary'?'附属卡':'角色待核实')],[card.registration_only?'登记日期':'发行日期',card.registration_only?card.registered_on:card.issued_on],['停止使用日期',card.closed_on || t('未记录')]]);if(credit)fields(info,[['信用额度（不计资产）',money(facility.limit_minor,account.currency)]]);info.append(link('查看所属账户','account-detail',envelope,'account',account.account_id));historyLink(info,envelope,card,'card','card-detail');if(card.card_type==='virtual')info.append(n('p','当前虚拟卡不提供授信或对外支付能力。','bank-note'));hero.append(info);root.append(hero);
        const transactions=panel('所属账户流水'),body=n('div',null,'cd-transactions-body');transactions.append(body);root.append(transactions,times(envelope));await loadTransactions(body,envelope,{account_id:account.account_id,month:envelope.as_of.slice(0,7),limit:8,offset:0},valid,()=>render(host,record.context,isCurrent));
      } else if(view==='account-detail') {
        const account=data.accounts.find(row=>row.account_id===id);if(!account)throw Error('当前资金账户不存在或不属于此账户。');const heading=n('header',null,'cd-heading'),copy=n('div');copy.append(n('p','账户详情','cd-eyebrow'),n('h2',account.name));heading.append(copy);root.append(heading);
        const details=panel(institution(data,account));if(account.account_type==='securities_cash'){securitiesStats(details,F.securitiesSummary(envelope).accounts.find(row=>row.account.account_id===id));fields(details,[['币种',account.currency],['账户状态',status(account.status)]]);}else fields(details,[[account.account_type==='credit_card'?'信用卡未还余额':'已结算现金',money(account.account_type==='credit_card'?-account.balance_minor:account.balance_minor,account.currency)],['币种',account.currency],['账户状态',status(account.status)]]);
        if(account.account_type!=='credit_card'){const metadata={source_scope:'simulation',object_kind:'account',object_id:account.account_id},payment=n('a','从此账户汇款','account-view-link');payment.href=NavrenViews.url({page:'account',view:'payments',...metadata});payment.onclick=event=>{if(event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)return;event.preventDefault();NavrenViews.open('payments',metadata);};details.append(payment);}historyLink(details,envelope,account,'account','account-detail');root.append(details);
        const cards=data.cards.filter(row=>row.account_id===id),positions=data.positions.filter(row=>row.account_id===id);if(cards.length){const section=panel('关联卡片');for(const card of cards)objectRow(section,envelope,card.masked_number,'',status(card.status),'card',card.card_id,'card-detail');root.append(section);}if(positions.length){const section=panel('关联持仓');for(const position of positions)objectRow(section,envelope,data.assets.find(row=>row.asset_id===position.asset_id)?.name || t('资产名称未取得'),money(position.book_value_minor),'持仓估值','holding',position.position_id,'holding-detail');root.append(section);}
        const transactions=panel('所属账户流水'),body=n('div',null,'cd-transactions-body');transactions.append(body);root.append(transactions,times(envelope));await loadTransactions(body,envelope,{account_id:id,month:envelope.as_of.slice(0,7),limit:8,offset:0},valid,()=>render(host,record.context,isCurrent));
      } else if(view==='holding-detail') {
        const position=data.positions.find(row=>row.position_id===id);if(!position)throw Error('当前持仓不存在或不属于此账户。');const asset=data.assets.find(row=>row.asset_id===position.asset_id),account=data.accounts.find(row=>row.account_id===position.account_id),heading=n('header',null,'cd-heading'),copy=n('div');copy.append(n('p','持仓详情','cd-eyebrow'),n('h2',asset.name));heading.append(copy);root.append(heading);
        const info=panel('持仓估值');fields(info,[['持仓估值',money(position.book_value_minor)],['成本本金',money(position.cost_basis_minor)],['份额',position.quantity],['估值来源',valuationSource(position.valuation_source)],['估值更新',formatTime(envelope.valuation_at)],['所属账户',account.name]]);info.append(link('查看所属账户','account-detail',envelope,'account',account.account_id));historyLink(info,envelope,position,'holding','holding-detail');root.append(info,times(envelope));
      } else if(view==='statements') {
        const filter=context.filters?.account_id || (context.object_kind==='account'?context.object_id:null);if(filter && !data.accounts.some(row=>row.account_id===filter))throw Error('流水账户不属于当前主体。');
        let coverage=null;try{coverage=await window.NavrenFinancialData?.overview();}catch(_){}if(!valid())return;
        let month=context.period || envelope.as_of.slice(0,7),offset=0;const heading=n('div',null,'ah-section-heading'),picker=n('label',null,'ah-month-picker'),input=n('input');input.type='month';input.id='accountStatementMonth';input.max=envelope.as_of.slice(0,7);input.min=coverage?.evidence?.coverage_start?.slice(0,7)||input.max;if(!/^\d{4}-(0[1-9]|1[0-2])$/.test(month))month=input.max;else if(month<input.min)month=input.min;else if(month>input.max)month=input.max;input.value=month;if(context.period&&context.period!==month){record.context.period=month;NavrenViews.update?.({period:month});}input.setAttribute('aria-label',t('月份'));picker.append(n('span','月份'),input);heading.append(n('h2','交易记录'),picker);if(!coverage?.evidence?.coverage_start){const retry=n('button','重新读取','ah-small-button');retry.type='button';retry.setAttribute('aria-label',window.NavrenI18n?.language==='en'?'Reload month range':'重新读取月份范围');retry.onclick=()=>render(host,record.context,isCurrent);heading.append(retry);}root.append(heading);
        const body=n('div',null,'cd-transactions-body'),footer=n('div',null,'cd-table-footer cd-statement-footer');root.append(body,footer);let requestEpoch=0,typeControl=null,sizeControl=null;
        let kind=F.transactionKinds.includes(context.filters?.transaction_kind)?context.filters.transaction_kind:null,limit=[10,20,50,100].includes(context.filters?.page_size)?context.filters.page_size:50;
        if(context.period===month&&Number.isSafeInteger(context.filters?.page_offset)&&context.filters.page_offset>=0&&context.filters.page_offset<=10000&&context.filters.page_offset%limit===0)offset=context.filters.page_offset;
        function persist(){record.context.period=month;record.context.filters={...record.context.filters,transaction_kind:kind,page_size:limit,page_offset:offset};NavrenViews.update?.({period:month,filters:record.context.filters});}
        async function load() {
          if(!valid())return;
          const token=++requestEpoch;persist();typeControl?.destroy();sizeControl?.destroy();footer.replaceChildren();
          const page=await loadTransactions(body,envelope,{month,limit,offset,kind,...(filter?{account_id:filter}:{})},()=>valid()&&token===requestEpoch,()=>render(host,record.context,isCurrent),page=>{
            const available=page.available_kinds||F.transactionKinds,choices=[{value:null,label:t('全部类型')},...F.transactionKinds.filter(value=>available.includes(value)||value===kind).map(value=>({value,label:t(kinds[value])}))];
            typeControl=choice(record,t('类型')+(kind?' · '+t(kinds[kind]):''),choices,kind,value=>{kind=value;offset=0;load().then(()=>{if(valid())typeControl?.button.focus();});},'cd-type-filter');typeControl.button.classList.toggle('is-filtered',kind!==null);typeControl.button.setAttribute('aria-label',(window.NavrenI18n?.language==='en'?'Filter transaction type: ':'筛选交易类型：')+t(kind?kinds[kind]:'全部类型'));return typeControl.button;
          });
          if(!valid()||token!==requestEpoch||!page)return;
          if(offset>0&&offset>=page.total){offset=page.total?Math.floor((page.total-1)/limit)*limit:0;return load();}
          const previous=n('button','上一页','ah-small-button'),next=n('button','下一页','ah-small-button'),total=n('span',window.NavrenI18n?.language==='en'?`${page.total} transactions`:`共 ${page.total} 条`,'cd-page-total'),size=n('div',null,'cd-page-size'),number=n('span',`${page.total?Math.floor(offset/limit)+1:0} / ${Math.ceil(page.total/limit)}`,'cd-page-number');number.setAttribute('aria-live','polite');
          sizeControl=choice(record,window.NavrenI18n?.language==='en'?String(limit):`${limit} 条`,[10,20,50,100].map(value=>({value,label:window.NavrenI18n?.language==='en'?String(value):`${value} 条`})),limit,value=>{limit=value;offset=0;load().then(()=>{if(valid())sizeControl?.button.focus();});});sizeControl.button.setAttribute('aria-label',window.NavrenI18n?.language==='en'?`Rows per page: ${limit}`:`每页条数：${limit}`);size.append(n('span','每页'),sizeControl.button);
          previous.type=next.type='button';previous.disabled=offset===0;next.disabled=!page.has_more||offset+limit>10000;previous.onclick=()=>{offset=Math.max(0,offset-limit);load();};next.onclick=()=>{if(offset+limit<=10000){offset+=limit;load();}};footer.append(total,size,previous,number,next);
          if(page.has_more&&offset+limit>10000)footer.append(n('span',window.NavrenI18n?.language==='en'?'Narrow the month or account range to view more transactions.':'请缩小月份或账户范围后查看更多交易。','cd-empty'));
        }
        input.onchange=()=>{if(/^\d{4}-(0[1-9]|1[0-2])$/.test(input.value) && input.value>=input.min && input.value<=input.max){month=input.value;record.context.period=month;NavrenViews.update?.({period:month});offset=0;load();}};window.NavrenMonthPicker?.mount(input,{currentMonth:envelope.as_of.slice(0,7)});await load();
      } else throw Error('此当前资产详情暂不可用。');
    } catch(error) {if(valid()){
      if(platformView && platform && error.code!=='INVALID_PAYMENT_PLATFORM'){const root=n('div',null,'cd-detail');root.dataset.i18nSkip='true';platformHeading(root,platform,'unverified');const state=n('section',null,'cd-payment-state');state.dataset.state='unverified';state.append(n('h3','暂时无法核实该支付平台的绑定状态。'),n('p',error.message));const retry=n('button','重新读取','ah-small-button');retry.type='button';retry.onclick=()=>render(host,context,isCurrent);state.append(retry);root.append(state);host.replaceChildren(root);}else{host.replaceChildren(n('p',error.message,'account-empty'));if(error.code!=='INVALID_PAYMENT_PLATFORM'){const retry=n('button','重新读取','ah-small-button');retry.type='button';retry.onclick=()=>render(host,context,isCurrent);host.append(retry);}}
    }}
  }
  function refresh() {for(const [host,record] of mounts){if(!host.isConnected || !record.isCurrent()){for(const cleanup of record.cleanups)cleanup();mounts.delete(host);continue;}if(!document.hidden&&!document.querySelector('dialog[open],.navren-month-popup,.cd-choice-menu:not([hidden])'))render(host,record.context,record.isCurrent);}}
  for(const event of ['navren:view','popstate','hashchange'])window.addEventListener(event,()=>{for(const record of mounts.values())for(const cleanup of record.cleanups)cleanup();mounts.clear();});
  for(const event of ['navren:privacy-reset','navren:identity-revoked','navren:account-switched','pagehide'])window.addEventListener(event,()=>{for(const [host,record] of mounts){for(const cleanup of record.cleanups)cleanup();if(host.isConnected && record.isCurrent())host.replaceChildren();}mounts.clear();});
  window.addEventListener('focus',refresh);window.addEventListener('bank:ready',refresh);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  return {render,cardVisual,institution,money,paymentPlatforms,cardApplicationAccounts};
})();
