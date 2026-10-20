/* 账户首页：只读取当前工作区资料，复用银行确认表单，不保存金融状态。 */
window.NavrenAccountHome = (() => {
  let current = null, epoch = 0, owner = null, accountMonth = '', monthlyRequest = 0, transactionsRequest = 0;
  const snapshots = new Map();
  let remittanceDraft = {}, actionCards = { remittance: false, split: false }, remittanceReview = null;
  let remittanceAttempt = null;
  let selectedCardId = null, disposeCardStack = null, disposePayer = null, disposeContacts = null, disposeHistoryChart = null, syncTimer = null;
  let renderRequest = 0, renderLanguage = '', cardStructure = '', transactionSnapshot = '';
  let refreshPayer = null, aaOptions = null;
  const linkMetadata = new WeakMap();
  const en = {
    '本月支出':'Monthly outgoings',
    '我的资产':'My assets','银行卡':'Bank cards','转账':'Transfers','资产月报':'Monthly assets','交易记录':'Transactions',
    '总资产':'Total assets','资产':'Assets','现金':'Cash','净资产':'Net assets','现金余额':'Cash balance','持仓本金':'Investment principal','待结算赎回':'Pending redemption',
    '负债':'Liabilities','可安排资金':'Arrangeable funds','本金口径':'Principal basis','资金口径':'Funds basis',
    '查看全部资产':'All assets','管理卡片':'Manage cards','借记卡':'Debit card','信用卡':'Credit card','SIM 虚拟卡':'SIM virtual card','银行卡类型待核实':'Card type unverified',
    '尾号':'Ending in','本期待还':'Outstanding balance','账户现金':'Account cash','关联持仓':'Linked investments','关联账户尚未取得':'Linked account unavailable',
    '尚未取得银行卡资料':'Card records unavailable','未关联银行卡的资金账户':'Accounts without a linked debit card',
    '查看持仓':'View investments','查看账户':'View account','查看完整月报':'Full monthly report','全部交易':'All transactions',
    '正在读取月报…':'Loading monthly report…','正在读取交易…':'Loading transactions…','重新读取':'Reload',
    '月末总资产':'Month-end assets','月末净资产':'Month-end net assets','月末现金':'Month-end cash','本月收入':'Monthly income',
    '消费支出':'Expenses','收支结余':'Income less expenses','历史资产估值':'Historical asset valuation',
    '历史估值资料暂未取得':'Historical valuation unavailable','历史估值与当前本金分别记账。':'Historical valuations and current principal are recorded separately.',
    '现金月结':'Cash statement','期初现金':'Opening cash','期末现金':'Closing cash','现金净变动':'Net cash movement',
    '截至日总资产':'Assets at cutoff','截至日净资产':'Net assets at cutoff','截至日现金':'Cash at cutoff','本期收入':'Period income','本期支出':'Period expenses','期间现金变动':'Period cash movement','期间现金收支':'Period cash statement',
    '月份':'Month','时间':'Date','交易对象':'Merchant / recipient','交易类型':'Type','金额':'Amount','收入':'Income','消费':'Expense','退款':'Refund',
    '转出':'Transfer out','转入':'Transfer in','转账方向待核实':'Transfer direction unverified','投资买入':'Investment purchase','投资赎回':'Investment redemption',
    '交易对象未取得':'Transaction party unavailable','暂无已记录交易':'No recorded transactions','币种未知':'Currency unknown','未知':'Unknown',
    '定时转账与AA收款':'Scheduled transfers and split payments','查看精确金额':'Exact amount',
    '银行卡仅作为关联入口；多张卡共用账户时，余额不重复计入总资产。':'Cards link to accounts; balances shared by several cards are counted once.',
    '持仓按账户关联，不推断由哪一张卡购买。':'Investments are linked to accounts, without inferring the purchasing card.',
    '现金账户月结资料暂未取得。':'Cash statement unavailable.', '所选月份没有已记录流水，不代表没有发生交易。':'No transactions recorded for this month; this does not imply no activity.',
    '历史记录':'Historical records','截至':'Through','本月':'This month','笔交易':'transactions','有效':'Active','已挂失':'Lost',
    '已过期':'Expired','状态待核实':'Status unverified','报告月份':'Report month','交易月份':'Transaction month',
    '资产与负债按当前账本本金展示，不包含市场估值。':'Current assets and liabilities use ledger principal, excluding market valuation.',
    '可安排资金已扣除应急保留、近期到期款和生效预留，不与总资产相加。':'Arrangeable funds exclude reserves and due payments; they are not added to total assets.'
  };
  const t = value => window.NavrenI18n?.language === 'en' ? (en[value] || window.NavrenI18n?.t?.(value) || value) : value;
  Object.assign(en, {
    '正在同步账户资产…':'Syncing account assets…','更新于':'Updated','投资估值':'Investment value','其他资产':'Other assets',
    '付款账户与上方资产共用同一账本。':'Payment accounts and assets use the same ledger.','AA收款使用当前付款账户，到账后同步更新资产。':'Split payments use the current account; assets update when funds arrive.',
    '点击露出的银行卡切换当前卡片':'Select an exposed card to bring it forward','切换到':'Switch to','查看':'View','卡片详情':'Card details','当前银行卡':'Current bank card','我的银行卡':'My bank cards','上一张银行卡':'Previous bank card','下一张银行卡':'Next bank card','张银行卡':'bank cards',
    '所属账户':'Linked account','关联持仓 · 估值':'Linked investments · valuation',
    '其他资金账户':'Other cash accounts','银行卡资料待补齐':'Bank card records incomplete','未取得有效银行卡记录':'Active card records unavailable',
    '证券资金账户':'Securities cash account','账户余额':'Account balance','经营收款余额':'Business payment balance',
    '支付宝':'Alipay','微信支付':'WeChat Pay','支付账户':'Payment account','未绑定':'Not linked','绑定状态待核实':'Link status unavailable','正在读取…':'Loading…',
    '信用卡未还余额':'Credit card outstanding balance',
    '期末总资产':'Period-end assets','期末净资产':'Period-end net assets','期末现金':'Closing cash','期初现金':'Opening cash',
    '悬停或点选查看区间详情':'Hover or select to view interval details','相邻快照区间':'Between adjacent snapshots','期初快照':'Opening snapshot','当日资产快照':'Daily asset snapshot','期初':'Opening','起点总资产':'Opening assets','终点总资产':'Closing assets','资产变动':'Asset change','变化幅度未取得':'Percentage unavailable','没有可比较的前一快照':'No comparable previous snapshot','区间收支':'Interval cash flows','支出':'Expenses','现金变动':'Cash movement','未取得':'Unavailable','总资产走势':'Asset trend','每周及期末快照':'Weekly and period-end snapshots','金额（元）':'Amount (CNY)','金额（万元）':'Amount (10,000 CNY)','金额（亿元）':'Amount (100 million CNY)',
    '转账汇款':'Transfers and remittances','境内人民币账户汇款':'Domestic CNY account transfers','AA收款':'Split payments','共同支出，分摊收款':'Share expenses and collect payments',
    '展开':'Expand','收起':'Collapse','付款账户':'Payment account','汇款人姓名':'Sender name','联系电话':'Contact phone','收款人姓名':'Recipient name','收款账号':'Recipient account number',
    '开户银行':'Recipient bank','开户支行':'Bank branch','汇款金额（元）':'Amount (CNY)',
    '汇款用途':'Payment purpose','附言（选填）':'Message (optional)','汇款方式':'Transfer method','实时汇款':'Real-time','非实时汇款':'Deferred',
    '普通汇款':'Ordinary transfer','次日汇款':'Next-day transfer','非实时处理方式':'Deferred method','请填写开户姓名':'Enter the registered account name',
    '请填写本人联系电话':'Enter your contact phone','与收款账户开户姓名一致':'As registered with the recipient bank','请输入完整银行卡号或账号':'Enter the full card or account number',
    '输入或选择银行名称':'Enter or select the bank','请输入开户支行全称':'Enter the full branch name',
    '请选择用途':'Select a purpose','生活支出':'Living expenses','家庭往来':'Family payment','货款／服务费':'Goods / services','借款归还':'Loan repayment','其他':'Other',
    '大写':'Chinese uppercase','请输入金额':'Enter an amount','金额格式有误':'Invalid amount','核对汇款信息':'Review remittance','清空':'Clear','返回修改':'Edit details',
    '银行汇款渠道未接入':'Banking transfer service is not connected',
    '模拟处理：实时汇款确认后处理，普通汇款 2 小时后处理，次日汇款次日 09:00 处理（北京时间）。':'Simulation: real-time remittances process after confirmation, ordinary remittances after 2 hours, and next-day remittances at 09:00 the next day (Beijing time).',
    '请填写有效金额，最多两位小数。':'Enter a valid amount with up to two decimal places.','金额必须大于零。':'Amount must be greater than zero.',
    '金额超出可填写范围。':'Amount is outside the supported input range.','账号须为10至32位数字。':'Account numbers must contain 10–32 digits.',
    '请输入有效联系电话。':'Enter a valid contact phone.',
    '当前账户已变化，请重新核对。':'The current account has changed. Review again.','暂无可用人民币付款账户':'No CNY payment account is available',
    '请填写有效信息。':'Enter valid information.',
    '这些信息仅供核对，尚未向银行提交；收款账户未完成银行核验。':'For review only. Nothing has been submitted to a bank, and the recipient account has not been verified.',
    '人民币':'CNY','尚未填写':'Not provided','暂无AA收款账单':'No split-payment bills','参与人':'Participant','应收':'Due','已收':'Received','状态':'Status',
    '发起收款':'Request payment','登记收款':'Record payment','只记录本地AA请求与收款，不向联系人发送消息。':'Records local requests and collections without messaging contacts.',
    '待发起':'Draft','待收款':'Awaiting payment','已完成':'Completed','已取消':'Cancelled','已发起':'Requested'
  });
  Object.assign(en,{'付款银行卡':'Payment bank card','选择付款银行卡':'Choose a payment card','暂无可选借记卡':'No eligible debit cards','正在核对银行卡…':'Checking bank cards…','借记卡资料暂未取得':'Debit card records unavailable','请选择有效的付款银行卡。':'Select an eligible payment card.'});
  Object.assign(en,{'模拟汇款':'Simulated remittance','确认汇款':'Confirm remittance','取消汇款':'Cancel remittance','正在准备汇款…':'Preparing remittance…','处理中…':'Processing…','查询汇款状态':'Check remittance status','查看汇款回执':'View remittance receipt','汇款记录':'Remittance history','查看回执':'View receipt','关闭':'Close','尚未取得汇款状态，请查询原操作，勿重复提交。':'Remittance status unavailable. Check the original operation before submitting again.','待本人确认，尚未扣款':'Awaiting confirmation; no debit','预约已受理，尚未扣款':'Scheduled; not yet debited','汇款已完成':'Remittance completed','已取消，未扣款':'Cancelled; no debit','汇款失败，未扣款':'Failed; no debit','确认已过期':'Confirmation expired','账户已变化，请重新核对':'Account changed; review again','执行日期':'Execution date','付款后账户余额':'Account balance after remittance','业务编号':'Receipt number','交易流水编号':'Transaction number','模拟额外验证码':'Simulation verification code','查看演示验证码':'View simulation code','本次只更新模拟账本，不会向真实银行发起汇款。':'Updates the simulation ledger only; no transfer is sent to a real bank.','尚未取得汇款记录':'Remittance history unavailable'});
  function n(tag, text, cls) { const el = document.createElement(tag); if (text != null) el.textContent = t(String(text)); if (cls) el.className = cls; return el; }
  const validMinor = Number.isSafeInteger;
  function sum(values) { if (!values.every(validMinor)) return null; const value = values.reduce((a,b) => a + BigInt(b), 0n); return value <= BigInt(Number.MAX_SAFE_INTEGER) && value >= BigInt(Number.MIN_SAFE_INTEGER) ? Number(value) : null; }
  function money(value, currency) {
    if(!/^[A-Z]{3}$/.test(currency||''))return t('币种未知');
    if(!validMinor(value))return t('未知');
    if(BankUI.formatMoney)return BankUI.formatMoney(value,currency);
    return currency==='CNY'?BankClient.money(value):(value/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})+' '+currency;
  }
  function amount(value,currency,large=false) {
    const el=n('strong',money(value,currency),'ah-amount');el.title=money(value,currency);
    if(large && currency==='CNY' && validMinor(value) && Math.abs(value)>=1000000000) {
      const v=BigInt(value),abs=v<0n?-v:v,enMode=window.NavrenI18n?.language==='en';
      const unit=enMode?(abs>=100000000000n?100000000000n:100000000n):(abs>=10000000000n?10000000000n:1000000n);
      const rounded=(abs*100n+unit/2n)/unit;
      el.textContent=(v<0n?'-':'')+(rounded/100n).toLocaleString(enMode?'en-US':'zh-CN')+'.'+String(rounded%100n).padStart(2,'0')+(enMode?(unit===100000000000n?' B CNY':' M CNY'):(unit===10000000000n?' 亿元':' 万元'));
    }
    return el;
  }
  function route(text,view,metadata={},cls='') { const reference={...metadata},a=current.link(t(text),view,reference);linkMetadata.set(a,{view,reference});if(window.NavrenViews?.url)a.href=window.NavrenViews.url({page:'account',view,...reference});if(cls)a.classList.add(cls);return a; }
  // Preserve the nodes and handlers: a valuation tick only changes text and reference versions.
  function updateNumber(target,replacement) {
    if(!target)return;
    if(target.title!==replacement.title)target.title=replacement.title;
    const before=[...target.childNodes],after=[...replacement.childNodes];
    if(before.length!==after.length||before.some((node,i)=>node.nodeType!==after[i].nodeType||node.nodeName!==after[i].nodeName)){target.replaceChildren(...after);return;}
    before.forEach((node,i)=>{if(node.nodeType===Node.TEXT_NODE){if(node.nodeValue!==after[i].nodeValue)node.nodeValue=after[i].nodeValue;}else updateNumber(node,after[i]);});
  }
  function refreshReferences() {
    document.querySelectorAll('#accountHome a').forEach(a=>{
      const saved=linkMetadata.get(a);if(!saved||saved.reference.source_scope!==NavrenConnectedAssets.scope||!saved.reference.object_id)return;
      Object.assign(saved.reference,connectedRef(saved.reference.object_kind,saved.reference.object_id));
      if(window.NavrenViews?.url)a.href=window.NavrenViews.url({page:'account',view:saved.view,...saved.reference});
      else {const url=new URL(a.href);url.searchParams.set('object_version',saved.reference.object_version);a.href=url.href;}
    });
  }
  function metric(label,value,currency,cls='') { const box=n('div',null,'ah-stat '+cls);box.append(n('span',label),amount(value,currency,true));return box; }
  function connectedRef(kind,id,extra={}) { return NavrenConnectedAssets.reference(current.connected,kind,id,extra); }
  function syncedAt(value) { const date=new Date(value);return Number.isFinite(date.getTime())?date.toLocaleString(window.NavrenI18n?.language==='en'?'en-GB':'zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):t('未知'); }
  function assets() {
    const envelope=current.connected,host=document.getElementById('assetOverview');if(!envelope){host.replaceChildren();return;}
    const s=envelope.data.summary,currency='CNY';
    host.dataset.sourceScope=NavrenConnectedAssets.scope;host.dataset.ledgerVersion=String(envelope.version);host.dataset.snapshotVersion=envelope.dataset_version;
    const values=host.querySelectorAll('[data-asset-value]');
    if(values.length===(s.pending_redemption_minor>0?7:6)&&host.dataset.language===(window.NavrenI18n?.language||'zh')){
      values.forEach(el=>updateNumber(el,amount(s[el.dataset.assetValue],currency,true)));
      const timestamp=host.querySelector('.ah-total>small'),text=t('更新于')+' '+syncedAt(envelope.observed_at);if(timestamp.textContent!==text)timestamp.textContent=text;return;
    }
    host.replaceChildren();host.dataset.language=window.NavrenI18n?.language||'zh';
    const hero=n('div',null,'ah-total');hero.append(n('span','总资产'),amount(s.total_assets_minor,currency,true),n('small',t('更新于')+' '+syncedAt(envelope.observed_at)));
    const grid=n('div',null,'ah-stats');for(const [label,key]of [['净资产','net_worth_minor'],['现金余额','cash_minor'],['投资估值','investment_minor'],['负债','liabilities_minor']])grid.append(metric(label,s[key],currency));
    const other=n('div',null,'ah-available');other.append(n('span','其他资产'),amount(s.real_assets_minor,currency,true));host.append(hero,grid,other);
    if(s.pending_redemption_minor>0){const pending=n('div',null,'ah-available');pending.append(n('span','待结算赎回'),amount(s.pending_redemption_minor,currency,true));host.append(pending);}
    host.querySelectorAll('.ah-amount').forEach((el,i)=>el.dataset.assetValue=['total_assets_minor','net_worth_minor','cash_minor','investment_minor','liabilities_minor','real_assets_minor','pending_redemption_minor'][i]);
  }
  function stopCardStack(reset=false) {
    disposeCardStack?.();disposeCardStack=null;if(reset)selectedCardId=null;
  }
  function cardIcon(name) {
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    for(const [key,value]of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.5','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true'}))svg.setAttribute(key,value);
    const paths={bank:['M3 10h18M4 10V8l8-5 8 5v2M6 10v9m6-9v9m6-9v9M3 21h18'],nfc:['M8 9a5 5 0 0 1 0 6','M11 6a9 9 0 0 1 0 12','M14 3a13 13 0 0 1 0 18'],arrow:['M7 17 17 7M7 7h10v10']};
    for(const d of paths[name]){const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',d);svg.append(path);}return svg;
  }
  function cardTheme(bank) {
    for(const [pattern,theme]of [[/中国银行|中行/,'boc'],[/建设银行|建行/,'ccb'],[/农业银行|农行/,'abc'],[/工商银行|工行/,'icbc'],[/招商银行|招行/,'cmb'],[/邮政储蓄|邮储/,'psbc']])if(pattern.test(bank))return theme;
    return 'neutral';
  }
  function cardBalance(value,currency) {
    const el=n('div',null,'ah-card-value');el.title=money(value,currency);
    if(!validMinor(value)||!/^[A-Z]{3}$/.test(currency||'')){el.append(n('strong',money(value,currency)));return el;}
    const minor=BigInt(value),absolute=minor<0n?-minor:minor;
    const digits=n('strong',(minor<0n?'-':'')+(absolute/100n).toLocaleString('en-US'));
    digits.append(n('span','.'+String(absolute%100n).padStart(2,'0'),'ah-card-decimal'));
    el.append(n('span',currency==='CNY'?'¥':currency,'ah-card-unit'),digits);return el;
  }
  function fundsAccountName(data,account) {
    const institution=NavrenFinancialUI.institution(data,account);
    if(account.account_type==='securities_cash')return institution+'资金账户';
    if(account.account_type==='payment_wallet')return institution+(account.scope==='self_employed'||account.source_pool_id?.endsWith('::cash:operating')?'经营收款余额':'余额');
    return institution+' · '+account.name;
  }
  function fundsBanner(data,account,platform=null,binding='unbound') {
    const institution=account?NavrenFinancialUI.institution(data,account):platform.name,identity=account?fundsAccountName(data,account):t(platform.name);
    const brands=[[/中信证券/,'citic','citic-securities.png'],[/支付宝/,'alipay','alipay.png'],[/微信支付/,'wechat','wechat-pay-symbol.svg']];
    const brand=brands.find(([pattern])=>pattern.test(institution));
    const banner=platform?route('','payment-platform',{source_scope:NavrenConnectedAssets.scope,object_kind:'institution',object_id:platform.id},'ah-funds-banner'):route('','account-detail',connectedRef('account',account.account_id),'ah-funds-banner');
    banner.dataset.fundsTheme=brand?.[1]||'neutral';
    const state=account?(account.connection_status==='bound'&&account.connection_active===true?'bound':account.connection_status==='disconnected'&&account.connection_active===false?'disconnected':'unknown'):binding;
    const bindingLabel={bound:'已绑定',disconnected:'已解除绑定',unbound:'未绑定',loading:'正在读取…',unknown:'绑定状态待核实'}[state];
    banner.dataset.bindingState=state;
    if(account)banner.dataset.fundsAccountId=account.account_id;
    if(platform)banner.dataset.paymentPlatform=platform.id;
    banner.setAttribute('aria-label',identity+' · '+(account?money(account.balance_minor,account.currency)+' · '+bindingLabel:t(bindingLabel)));
    const head=n('span',null,'ah-funds-banner-head'),logo=n('span',null,'ah-funds-logo'),symbol=n('span',null,'ah-funds-symbol');
    if(brand){const image=n('img');image.src='assets/institutions/'+brand[2];image.alt='';image.decoding='async';symbol.append(image);}else symbol.append(cardIcon('bank'));logo.append(symbol);
    const copy=n('span',null,'ah-funds-brand-copy');
    const purpose=!account?'支付账户':account.account_type==='securities_cash'?'证券资金账户':account.account_type==='payment_wallet'?(account.scope==='self_employed'||account.source_pool_id?.endsWith('::cash:operating')?'经营收款余额':'账户余额'):account.name;
    copy.append(n('strong',institution,'ah-funds-brand-name'),n('span',purpose+(platform&&account&&state!=='bound'?' · '+bindingLabel:''),'ah-funds-purpose'));head.append(logo,copy);
    const detail=n('span',null,'ah-funds-banner-detail'),arrow=n('span','›','ah-funds-arrow');arrow.setAttribute('aria-hidden','true');
    detail.append(account?amount(account.balance_minor,account.currency,true):n('span',bindingLabel,'ah-funds-binding'),arrow);
    banner.append(head,detail);return banner;
  }
  function paymentDirectory(data=null,binding='unbound') {
    const group=n('div',null,'ah-unlinked ah-funds-banners');group.append(n('h4','其他资金账户'));
    const platforms=NavrenConnectedAssetsUI.paymentPlatforms;
    const wallets=data?.accounts.filter(account=>account.account_type==='payment_wallet')||[];
    for(const platform of platforms){
      const accounts=wallets.filter(account=>NavrenConnectedAssetsUI.institution(data,account)===platform.name);
      if(accounts.length)for(const account of accounts)group.append(fundsBanner(data,account,platform));
      else group.append(fundsBanner(data,null,platform,binding));
    }
    return group;
  }
  function securitiesBanner(envelope) {
    const banner=route('','securities',{source_scope:NavrenConnectedAssets.scope},'ah-funds-banner');banner.dataset.fundsTheme='neutral';banner.dataset.securitiesAggregate='true';
    const head=n('span',null,'ah-funds-banner-head'),logo=n('span',null,'ah-funds-logo'),symbol=n('span',null,'ah-funds-symbol');symbol.append(cardIcon('bank'));logo.append(symbol);
    const copy=n('span',null,'ah-funds-brand-copy'),caption=n('span','证券资料待核实','ah-funds-purpose');copy.append(n('strong','我的证券','ah-funds-brand-name'),caption);head.append(logo,copy);
    const detail=n('span',null,'ah-funds-banner-detail');
    try {const summary=NavrenConnectedAssets.securitiesSummary(envelope);caption.textContent=summary.accounts.length+'个证券账户 · 现金＋投资';detail.append(amount(summary.total_minor,'CNY',true));}catch {detail.append(n('span','金额待核实','ah-funds-binding'));}
    const arrow=n('span','›','ah-funds-arrow');arrow.setAttribute('aria-hidden','true');detail.append(arrow);banner.append(head,detail);return banner;
  }
  function showPaymentDirectory(binding) {
    const host=document.getElementById('accountAssetsSupplement');if(host)host.replaceChildren(paymentDirectory(null,binding));
  }
  function cards() {
    const captured=current,token=epoch,envelope=captured.connected,host=document.getElementById('accountCardsPreview'),supplement=document.getElementById('accountAssetsSupplement')||host;
    if(!envelope){stopCardStack();cardStructure='';host.replaceChildren();if(supplement!==host)supplement.replaceChildren();return;}
    const s=envelope.data,active=s.cards.filter(card=>card.status==='active'&&!card.registration_only);
    let securitiesKnown=false;try{NavrenConnectedAssets.securitiesSummary(envelope);securitiesKnown=true;}catch{}
    const structure=JSON.stringify({language:window.NavrenI18n?.language||'zh',cards:s.cards.map(card=>[card.card_id,card.account_id,card.facility_id,card.card_type,card.masked_number,card.status,card.registration_only]),accounts:s.accounts.map(account=>[account.account_id,account.connection_id,account.account_type,account.name,account.currency,account.status,account.connection_status,account.connection_active,account.scope,account.source_pool_id]),connections:s.connections.map(connection=>[connection.connection_id,connection.institution_name,connection.institution_type,connection.status]),credit:s.credit_facilities?.map(item=>[item.facility_id,validMinor(item.outstanding_minor)]),securities:securitiesKnown});
    if(cardStructure===structure&&host.querySelector('.ah-cards-heading')){
      host.querySelectorAll('[data-card-id]').forEach(frame=>{const card=active.find(item=>item.card_id===frame.dataset.cardId),account=s.accounts.find(item=>item.account_id===card.account_id),facility=s.credit_facilities?.find(item=>item.facility_id===card.facility_id);updateNumber(frame.querySelector('.ah-card-value'),cardBalance(card.card_type==='credit'?facility?.outstanding_minor:account?.balance_minor,account?.currency));});
      supplement.querySelectorAll('[data-funds-account-id]').forEach(banner=>{const account=s.accounts.find(item=>item.account_id===banner.dataset.fundsAccountId);updateNumber(banner.querySelector('.ah-amount'),amount(account.balance_minor,account.currency,true));if(banner.classList.contains('ah-funds-banner'))banner.setAttribute('aria-label',fundsAccountName(s,account)+' · '+money(account.balance_minor,account.currency)+' · '+t({bound:'已绑定',disconnected:'已解除绑定',unknown:'绑定状态待核实'}[banner.dataset.bindingState]));});
      const securities=supplement.querySelector('[data-securities-aggregate] .ah-amount');if(securities){try{updateNumber(securities,amount(NavrenConnectedAssets.securitiesSummary(envelope).total_minor,'CNY',true));}catch{cardStructure='';cards();return;}}
      refreshReferences();return;
    }
    stopCardStack();host.replaceChildren();if(supplement!==host)supplement.replaceChildren();
    const heading=n('div',null,'ah-cards-heading'),label=n('span',null,'ah-cards-label'),all=route('我的银行卡','cards',{source_scope:NavrenConnectedAssets.scope},'ah-cards-all');
    label.append(n('h3','银行卡'),n('span',active.length+' '+t('张银行卡'),'ah-cards-count'));all.append(n('span','›'));heading.append(label,all);host.append(heading);
    if(active.length) {
      const stack=n('div',null,'ah-card-stack'),announcement=n('span',null,'ah-card-live'),visibleCount=Math.min(active.length,3);
      stack.id='accountHomeCardStack';stack.style.setProperty('--ah-card-peeks',String(visibleCount-1));stack.setAttribute('aria-label',t('点击露出的银行卡切换当前卡片'));
      announcement.setAttribute('role','status');announcement.setAttribute('aria-live','polite');host.append(stack,announcement);
      let activeIndex=active.findIndex(card=>card.card_id===selectedCardId);if(activeIndex<0)activeIndex=0;
      let switching=false,queuedIndex=null,disposed=false,motions=[];
      const navigation=n('div',null,'ah-card-navigation'),counter=n('span',null,'ah-card-counter'),previous=n('button','‹','ah-card-nav-button'),next=n('button','›','ah-card-nav-button');
      for(const [button,text] of [[previous,'上一张银行卡'],[next,'下一张银行卡']]){button.type='button';button.setAttribute('aria-label',t(text));button.setAttribute('aria-controls',stack.id);button.disabled=active.length<2;}
      previous.onclick=()=>requestSelect(((queuedIndex??activeIndex)-1+active.length)%active.length);next.onclick=()=>requestSelect(((queuedIndex??activeIndex)+1)%active.length);
      navigation.append(previous,counter,next);navigation.hidden=active.length<2;host.append(navigation);
      const frames=active.map((card,index)=>{
        const account=s.accounts.find(a=>a.account_id===card.account_id),currency=account?.currency,bank=NavrenFinancialUI.institution(s,account);
        const tail=String(card.masked_number||'').match(/(\d{4})$/)?.[1]||'****';
        const frame=n('article',null,'ah-bank-card');frame.dataset.cardId=card.card_id;frame.dataset.bankTheme=cardTheme(bank);frame.dataset.cardType=card.card_type;
        const physical=n('div',null,'ah-card-physical'),wave=n('span',null,'ah-card-wave');wave.setAttribute('aria-hidden','true');physical.append(wave);
        const head=n('div',null,'ah-card-head'),identity=n('div',null,'ah-card-bank');identity.append(cardIcon('bank'),n('span',bank));
        head.append(identity,n('span',card.card_type==='virtual'?'SIM 虚拟卡':card.card_type==='credit'?'信用卡':card.card_type==='debit'?'借记卡':'银行卡类型待核实','ah-card-type'),n('span',t('尾号')+' '+tail,'ah-card-peek'));physical.append(head);
        const body=n('div',null,'ah-card-body'),chipRow=n('div',null,'ah-card-chip-row'),chipGroup=n('span',null,'ah-card-chip-group'),chip=n('span',null,'ah-card-chip');chip.setAttribute('aria-hidden','true');chipGroup.append(chip,cardIcon('nfc'));
        const number=n('span','**** **** **** '+tail,'ah-card-number');number.setAttribute('aria-label',t('尾号')+' '+tail);chipRow.append(chipGroup,number);body.append(chipRow);
        const credit=card.card_type==='credit',facility=credit?s.credit_facilities?.find(item=>item.facility_id===card.facility_id):null;
        const balance=n('div',null,'ah-card-balance');balance.append(n('span',credit?'信用卡未还余额':'账户现金','ah-card-balance-label'),cardBalance(credit?facility?.outstanding_minor:account?.balance_minor,currency));body.append(balance);
        const foot=n('div',null,'ah-card-foot'),footValue=n('span',null,'ah-card-foot-value');
        foot.append(n('span','所属账户','ah-card-foot-label'));footValue.append(n('span',account?.name||'关联账户尚未取得'));
        footValue.append(cardIcon('arrow'));foot.append(footValue);body.append(foot);physical.append(body);
        const select=n('button',null,'ah-card-select');select.type='button';select.setAttribute('aria-label',t('切换到')+' '+bank+' · '+t('尾号')+' '+tail);select.onclick=()=>requestSelect(index);
        const open=route('','card-detail',connectedRef('card',card.card_id),'ah-card-open');open.setAttribute('aria-label',t('查看')+' '+bank+' · '+t('尾号')+' '+tail+' · '+t('卡片详情'));
        open.addEventListener('click',event=>{if(switching||!alive(token,captured)){event.preventDefault();event.stopImmediatePropagation();}},true);
        frame.append(physical,select,open);stack.append(frame);return {frame,body,bank,tail,select,open};
      });
      const live=()=>!disposed&&stack.isConnected&&alive(token,captured);
      function arrange() {
        const order=[activeIndex,...active.map((_,index)=>index).filter(index=>index!==activeIndex)];
        frames.forEach(({frame,body,select,open},index)=>{
          const rank=order.indexOf(index);frame.dataset.rank=String(rank);frame.hidden=rank>=visibleCount;frame.style.zIndex=String(active.length-rank);
          frame.style.setProperty('--ah-card-offset','calc(var(--ah-card-step) * '+Math.max(0,visibleCount-1-rank)+')');
          frame.style.setProperty('--ah-card-inset',Math.min(rank*12,48)+'px');
          body.setAttribute('aria-hidden',String(rank!==0));select.hidden=rank===0||rank>=visibleCount;open.hidden=rank!==0;
        });selectedCardId=active[activeIndex].card_id;counter.textContent=(activeIndex+1)+' / '+active.length;
      }
      function requestSelect(index) {
        if(!live())return;
        if(switching){queuedIndex=index;return;}if(index!==activeIndex)void switchTo(index);
      }
      async function switchTo(index) {
        if(!live()||index===activeIndex)return;switching=true;
        const focusSelected=document.activeElement===frames[index].select;
        const before=frames.map(({frame})=>{const style=getComputedStyle(frame);return {transform:style.transform,left:style.left,right:style.right,rank:frame.dataset.rank,hidden:frame.hidden};});
        activeIndex=index;arrange();motions=[];
        try {
          if(!window.matchMedia('(prefers-reduced-motion: reduce)').matches&&typeof frames[0].frame.animate==='function') {
            frames.forEach(({frame,body},cardIndex)=>{
              if(frame.hidden)return;
              const after=getComputedStyle(frame),entering=cardIndex===index,leaving=before[cardIndex].rank==='0';
              motions.push(frame.animate([{transform:before[cardIndex].transform,left:before[cardIndex].left,right:before[cardIndex].right,opacity:entering ? .72 : 1},{transform:after.transform,left:after.left,right:after.right,opacity:1}],{duration:560,easing:'cubic-bezier(.22,.75,.18,1)',fill:'both'}));
              if(entering||leaving){body.style.visibility='visible';motions.push(body.animate(entering?[{opacity:0},{opacity:1,offset:.72},{opacity:1}]:[{opacity:1},{opacity:0,offset:.45},{opacity:0}],{duration:560,easing:'ease-out',fill:'both'}));}
            });
            await Promise.allSettled(motions.map(motion=>motion.finished));
          }
        } finally {
          motions.forEach(motion=>motion.cancel());frames.forEach(({body})=>body.style.visibility='');switching=false;
          if(live()){arrange();const active=frames[activeIndex];announcement.textContent=t('当前银行卡')+'：'+active.bank+' · '+t('尾号')+' '+active.tail;const next=queuedIndex;queuedIndex=null;if(next!==null&&next!==activeIndex)void switchTo(next);else if(focusSelected)active.open.focus({preventScroll:true});}
        }
      }
      disposeCardStack=()=>{disposed=true;queuedIndex=null;motions.forEach(motion=>motion.cancel());};arrange();
    }
    if(!active.length)host.append(n('div','未取得有效银行卡记录','ah-card-empty'));
    const unmatched=s.accounts.filter(a=>a.account_type!=='credit_card'&&!active.some(c=>c.account_id===a.account_id));
    const incomplete=unmatched.filter(account=>account.account_type==='bank_settlement');
    if(incomplete.length){
      const group=n('div',null,'ah-unlinked');group.append(n('h4','银行卡资料待补齐'));
      for(const account of incomplete){const row=route('','account-detail',connectedRef('account',account.account_id),'ah-unlinked-row'),identity=n('span',null,'ah-funds-identity');row.dataset.fundsAccountId=account.account_id;identity.append(n('span',fundsAccountName(s,account)),n('small','未取得有效银行卡记录'));row.append(identity,amount(account.balance_minor,account.currency,true),n('span','›'));group.append(row);}supplement.append(group);
    }
    const payments=paymentDirectory(s),knownPayments=new Set(NavrenConnectedAssetsUI.paymentPlatforms.map(platform=>platform.name));
    const other=unmatched.filter(account=>account.account_type!=='bank_settlement'&&account.account_type!=='securities_cash'&&!(account.account_type==='payment_wallet'&&knownPayments.has(NavrenConnectedAssetsUI.institution(s,account))));
    for(const account of other.reverse())payments.querySelector('h4').after(fundsBanner(s,account));
    payments.querySelector('h4').after(securitiesBanner(envelope));
    supplement.append(payments);
    if(s.cards.some(card=>card.status!=='active')){
      const footer=n('div',null,'ah-assets-footnote');
      footer.append(route('历史卡片','cards',{source_scope:NavrenFinancialData.scope},'ah-text-link'));supplement.append(footer);
    }
    cardStructure=structure;
  }
  function remittanceAmount(value) {
    if(window.NavrenMoneyInput)return NavrenMoneyInput.format(value);
    const normalized=String(value).trim().replace(/[０-９]/g,c=>String(c.charCodeAt(0)-0xff10)).replace(/[，]/g,',').replace(/[．]/g,'.');
    if(!normalized)return {raw:'',formatted:'',minor:null,uppercase:''};
    const raw=normalized.replaceAll(',','');
    if(!/^\d*(?:\.\d{0,2})?$/.test(raw)||!/[0-9]/.test(raw))return {formatted:normalized,minor:null,error:'请填写有效金额，最多两位小数。'};
    let [yuan,cents]=raw.split('.');yuan=(yuan||'0').replace(/^0+(?=\d)/,'');
    if(yuan.length>12)return {formatted:normalized,minor:null,error:'金额超出可填写范围。'};
    const minor=BigInt(yuan)*100n+BigInt((cents||'').padEnd(2,'0'));
    const digits='零壹贰叁肆伍陆柒捌玖',units=['','拾','佰','仟'],large=['','万','亿'];
    function group(value) {
      let result='',zero=false;
      const chars=String(value).padStart(4,'0');
      for(let i=0;i<4;i++){const digit=Number(chars[i]);if(digit){if(zero&&result)result+='零';result+=digits[digit]+units[3-i];zero=false;}else if(result)zero=true;}
      return result;
    }
    const groups=[];let integer=BigInt(yuan);
    while(integer){groups.push(Number(integer%10000n));integer/=10000n;}
    let upper='',gap=false;
    for(let i=groups.length-1;i>=0;i--){const part=groups[i];if(!part){if(upper)gap=true;continue;}if(upper&&(gap||part<1000))upper+='零';upper+=group(part)+large[i];gap=false;}
    upper=(upper||'零')+'元';
    const fraction=Number(minor%100n),jiao=Math.floor(fraction/10),fen=fraction%10;
    if(!fraction)upper+='整';else {if(jiao)upper+=digits[jiao]+'角';else if(minor>=100n)upper+='零';if(fen)upper+=digits[fen]+'分';else upper+='整';}
    return {raw:yuan+(cents!==undefined?'.'+cents:''),formatted:yuan.replace(/\B(?=(\d{3})+(?!\d))/g,',')+(cents!==undefined?'.'+cents:''),minor:Number(minor),uppercase:upper};
  }
  function closeRemittanceReview() { if(remittanceReview){remittanceReview.disposeVerification?.();remittanceReview.close();remittanceReview.remove();remittanceReview=null;} }
  function remittanceCaption(label,required=false,tag='span') {
    const caption=n(tag,label);
    if(required){const star=n('span','*','ah-required');star.setAttribute('aria-hidden','true');caption.append(star);}
    return caption;
  }
  function remittanceField(label,name,type='text',placeholder='',required=true) {
    const wrap=n('label',null,'ah-remittance-field'),caption=remittanceCaption(label,required),input=n('input');input.type=type;input.name=name;input.id='remittance-'+name;
    caption.id=input.id+'-label';input.setAttribute('aria-labelledby',caption.id);
    input.autocomplete='off';input.maxLength=80;if(placeholder)input.placeholder=t(placeholder);input.value=remittanceDraft[name]||'';wrap.append(caption,input);return {wrap,input};
  }
  function remittanceCards(captured) {
    const state=captured.state,envelope=captured.connected,data=envelope?.data;
    if(!state||!data||envelope.workspace_id!==state.workspace_id||envelope.version!==state.version)return [];
    const day=value=>typeof value==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(value)&&Number.isFinite(Date.parse(value+'T00:00:00Z'))&&new Date(value+'T00:00:00Z').toISOString().slice(0,10)===value;
    if(!day(envelope.as_of)||!day(state.as_of))return [];
    const seen=new Set(),items=[];
    for(const card of data.cards||[]) {
      const account=data.accounts?.find(a=>a.account_id===card.account_id),runtime=state.accounts?.find(a=>a.id===card.account_id),record=state.cards?.find(c=>c.id===card.card_id),connection=data.connections?.find(c=>c.connection_id===account?.connection_id);
      const tail=card.masked_number?.match(/(\d{4})$/)?.[1];
      if(seen.has(card.card_id)||card.card_type!=='debit'||card.status!=='active'||!tail||account?.account_type!=='bank_settlement'||account.status!=='active'||account.currency!=='CNY'||runtime?.currency!=='CNY'||!connection?.institution_name||connection.institution_type!=='bank'||connection.status!=='connected'||record?.type!=='debit'||record.status!=='active'||record.account_id!==runtime.id||record.last_four!==tail)continue;
      if(!day(card.expires_on)||!day(record.expires_on)||card.expires_on!==record.expires_on||card.expires_on<envelope.as_of||card.expires_on<state.as_of)continue;
      seen.add(card.card_id);items.push({card,account:runtime,bank:NavrenFinancialUI.institution(data,account),tail});
    }
    return items;
  }
  function payerIdentity(item) {
    const identity=n('span',null,'ah-payer-identity'),symbol=n('span',null,'ah-payer-symbol'),copy=n('span',null,'ah-payer-copy');
    symbol.dataset.bankTheme=cardTheme(item.bank);symbol.append(cardIcon('bank'));copy.append(n('strong',item.bank),n('small',t('借记卡')+' · '+t('尾号')+' '+item.tail));identity.append(symbol,copy);return identity;
  }
  function payerPicker(captured,items,input) {
    const wrap=n('div',null,'ah-remittance-field ah-payer-field'),caption=remittanceCaption('付款银行卡',true),trigger=n('button',null,'ah-payer-trigger'),balance=n('small',null,'ah-payer-balance'),menu=n('div',null,'ah-payer-menu'),abort=new AbortController();
    caption.id=input.id+'-label';trigger.type='button';trigger.id='remittance-card-trigger';trigger.setAttribute('aria-labelledby',caption.id+' remittance-card-copy');trigger.setAttribute('aria-haspopup','menu');trigger.setAttribute('aria-expanded','false');trigger.setAttribute('aria-controls','remittance-card-menu');menu.id='remittance-card-menu';menu.setAttribute('role','menu');menu.setAttribute('aria-label',t('选择付款银行卡'));menu.hidden=true;
    menu.append(n('div','选择付款银行卡','ah-payer-menu-title'));document.body.append(menu);
    let selected=items.find(item=>item.card.card_id===remittanceDraft.card_id&&item.account.id===remittanceDraft.account_id)||items.find(item=>item.account.id===remittanceDraft.account_id)||items.find(item=>item.account.id.endsWith('-main'))||items[0];
    function valid(){return current===captured&&captured.isCurrent()&&trigger.isConnected;}
    function close(focus=false){menu.hidden=true;trigger.setAttribute('aria-expanded','false');if(focus&&trigger.isConnected)trigger.focus();}
    function paint(){
      input.value=selected?.account.id||'';trigger.replaceChildren();balance.replaceChildren();
      if(selected){const identity=payerIdentity(selected);identity.id='remittance-card-copy';trigger.append(identity);balance.append(n('span','账户余额'),n('strong',money(selected.account.balance_minor,'CNY')));remittanceDraft.account_id=selected.account.id;remittanceDraft.card_id=selected.card.card_id;}
      else{const empty=n('span',captured.connected?'暂无可选借记卡':captured.connectedError?'借记卡资料暂未取得':'正在核对银行卡…');empty.id='remittance-card-copy';trigger.append(empty);}
      const arrow=n('span',null,'ah-payer-chevron');arrow.setAttribute('aria-hidden','true');arrow.textContent='⌄';trigger.append(arrow);trigger.disabled=!selected;
      for(const option of options)option.setAttribute('aria-checked',String(option.dataset.cardId===selected?.card.card_id));
    }
    const options=items.map(item=>{
      const option=n('button',null,'ah-payer-option'),value=n('span',null,'ah-payer-option-balance'),check=n('span','✓','ah-payer-check');option.type='button';option.setAttribute('role','menuitemradio');option.dataset.cardId=item.card.card_id;option.dataset.bankTheme=cardTheme(item.bank);value.append(n('span','账户余额'),n('strong',money(item.account.balance_minor,'CNY')));check.setAttribute('aria-hidden','true');option.append(payerIdentity(item),value,check);
      option.onclick=()=>{if(!valid()||!remittanceCards(captured).some(row=>row.card.card_id===item.card.card_id&&row.account.id===item.account.id)){close();return;}selected=item;paint();close(true);input.dispatchEvent(new Event('change',{bubbles:true}));};menu.append(option);return option;
    });
    function open(){
      if(!valid()||!items.length)return;
      menu.hidden=false;const box=trigger.getBoundingClientRect();menu.style.width=Math.min(Math.max(box.width,390),430,Math.max(0,window.innerWidth-24))+'px';menu.style.maxHeight=Math.max(120,window.innerHeight-24)+'px';menu.style.left=Math.max(12,Math.min(box.left,window.innerWidth-menu.offsetWidth-12))+'px';menu.style.top=(box.bottom+8+menu.offsetHeight>window.innerHeight?Math.max(12,box.top-menu.offsetHeight-8):box.bottom+8)+'px';trigger.setAttribute('aria-expanded','true');(options.find(option=>option.dataset.cardId===selected?.card.card_id)||options[0]).focus();
    }
    trigger.onclick=()=>menu.hidden?open():close(true);
    trigger.addEventListener('keydown',event=>{if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();open();}},{signal:abort.signal});
    menu.addEventListener('keydown',event=>{const index=options.indexOf(document.activeElement);if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close(true);}else if(event.key==='Tab'){event.preventDefault();close(true);if(event.shiftKey)document.querySelector('#accountRemittanceCard>summary')?.focus();else document.getElementById('remittance-recipient_name')?.focus();}else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();options[event.key==='Home'?0:event.key==='End'?options.length-1:(index+(event.key==='ArrowDown'?1:-1)+options.length)%options.length]?.focus();}},{signal:abort.signal});
    document.addEventListener('pointerdown',event=>{if(!menu.hidden&&!menu.contains(event.target)&&!trigger.contains(event.target))close();},{signal:abort.signal});
    document.addEventListener('click',event=>{if(!menu.hidden&&!menu.contains(event.target)&&!trigger.contains(event.target))close();},{signal:abort.signal});
    document.addEventListener('focusin',event=>{if(!menu.hidden&&!menu.contains(event.target)&&!trigger.contains(event.target))close();},{signal:abort.signal});
    window.addEventListener('resize',()=>close(),{signal:abort.signal});document.addEventListener('scroll',event=>{if(!menu.contains(event.target))close();},{capture:true,signal:abort.signal});
    document.getElementById('accountRemittanceCard')?.addEventListener('toggle',()=>{if(!document.getElementById('accountRemittanceCard').open)close();},{signal:abort.signal});
    refreshPayer=()=>{const latest=remittanceCards(captured);items.forEach(item=>{const next=latest.find(row=>row.card.card_id===item.card.card_id);if(next)Object.assign(item,next);});if(selected){const value=balance.querySelector('strong');if(value)value.textContent=money(selected.account.balance_minor,'CNY');}options.forEach(option=>{const item=items.find(row=>row.card.card_id===option.dataset.cardId),value=option.querySelector('.ah-payer-option-balance strong');if(item&&value)value.textContent=money(item.account.balance_minor,'CNY');});};
    disposePayer=()=>{abort.abort();menu.remove();refreshPayer=null;};paint();wrap.append(caption,input,trigger,balance);
    return {wrap,reset(){selected=items.find(item=>item.account.id.endsWith('-main'))||items[0];paint();close();}};
  }
  Object.assign(en,{'未提供':'Not provided','汇款未受理':'Remittance rejected','汇款已受理，资金已冻结，等待处理':'Accepted; funds held, awaiting processing','预计处理时间':'Estimated processing time','账本记账日':'Ledger posting date','已冻结金额':'Funds held','当前可用余额':'Current available balance','处理到期汇款':'Process due remittances','确认处理':'Confirm processing','取消':'Cancel'});
  const remittanceStatuses={pending_confirmation:'待本人确认，尚未扣款',scheduled:'汇款已受理，资金已冻结，等待处理',succeeded:'汇款已完成',cancelled:'已取消，未扣款',failed:'汇款失败，未扣款',expired:'确认已过期',invalidated:'账户已变化，请重新核对'};
  function remittanceDay(value){return typeof value==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(value)&&Number.isFinite(Date.parse(value+'T00:00:00Z'))&&new Date(value+'T00:00:00Z').toISOString().slice(0,10)===value;}
  function remittanceTime(record){
    if(typeof record.execute_at==='number'&&Number.isFinite(record.execute_at)&&record.execute_at>0&&Number.isFinite(new Date(record.execute_at*1000).getTime()))return new Intl.DateTimeFormat(window.NavrenI18n?.language==='en'?'en-GB':'zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(record.execute_at*1000));
    return remittanceDay(record.execute_on)?record.execute_on:t('未知');
  }
  function remittancePayload(fields,payer,value) {
    if(!['realtime','ordinary','next_day'].includes(fields.method))throw Error(t('请选择有效的汇款方式。'));
    return {account_id:payer.account.id,card_id:payer.card.card_id,recipient_name:fields.recipient_name.trim(),recipient_account:fields.recipient_account.replace(/\s/g,''),recipient_bank:fields.bank_name.trim(),recipient_branch:fields.bank_branch.trim(),amount_minor:value.minor,purpose:fields.purpose,message:fields.message.trim(),mode:fields.method};
  }
  function showRemittance(captured,attempt,trigger,preview=null) {
    if(current!==captured||!captured.isCurrent()||document.querySelector('dialog[open]'))return;
    const guard=BankClient.guard(),dialog=n('dialog',null,'ah-remittance-dialog');remittanceReview=dialog;
    const active=()=>current===captured&&captured.isCurrent()&&guard.valid()&&dialog.isConnected&&dialog.open;
    const title=n('h2',preview?'核对汇款信息':'查看汇款回执'),content=n('div'),status=n('p','正在准备汇款…','ah-form-note'),error=n('p','', 'ah-form-error'),actions=n('div',null,'ah-form-buttons');
    title.id='remittanceReviewHeading';dialog.setAttribute('aria-labelledby',title.id);status.setAttribute('role','status');error.setAttribute('role','alert');dialog.append(title,content,status,error,actions);
    let view=attempt.view,record=null,busy=false,unknown=false,rejected=false,verification=null;
    dialog.disposeVerification=()=>verification?.dispose();
    const confirm=n('button','确认汇款','ah-action-button is-primary'),cancel=n('button','取消汇款','ah-action-button'),query=n('button','查询汇款状态','ah-action-button'),edit=n('button',preview?'返回修改':'关闭','ah-action-button');
    for(const button of [confirm,cancel,query,edit])button.type='button';confirm.disabled=cancel.disabled=true;query.hidden=true;actions.append(edit,cancel,query,confirm);
    function setBusy(value){busy=value;for(const button of [confirm,cancel,query,edit])button.disabled=value;confirm.disabled=value||!!verification&&!verification.canConfirm();confirm.textContent=t(value?'处理中…':'确认汇款');}
    function rows(entries){const fields=n('dl',null,'ah-review-fields');for(const [label,text]of entries){const row=n('div');row.append(n('dt',label),n('dd',text));fields.append(row);}content.append(fields);}
    function paintPreview(){
      content.replaceChildren();if(!preview){const op=view?.operation,r=record||op?.payload?.remittance;if(!r)return;const card=captured.state?.cards?.find(item=>item.id===r.card_id),account=captured.connected?.data.accounts?.find(item=>item.account_id===(record?.account_id||op?.payload?.account_id));const bank=account?NavrenFinancialUI.institution(captured.connected.data,account):t('关联账户尚未取得');rows([['付款银行卡',bank+' · '+t('尾号')+' '+(card?.last_four||t('未知'))],['汇款人姓名',r.sender_name],['联系电话',r.sender_phone||t('未提供')],['收款人姓名',r.recipient_name],['收款账号',r.recipient_account],['开户银行',r.recipient_bank],['开户支行',r.recipient_branch],['汇款金额（元）',money(record?.amount_minor||op?.payload?.amount_minor,'CNY')],['汇款方式',t(r.mode==='realtime'?'实时汇款':r.mode==='next_day'?'次日汇款':'普通汇款')],['汇款用途',r.purpose],['附言（选填）',r.message||t('尚未填写')]]);return;}
      const {fields,payer,value}=preview,banner=n('div',null,'ah-review-amount');banner.append(n('strong',money(value.minor,'CNY')),n('span',value.uppercase));content.append(banner);
      const sender=view?.operation?.payload?.remittance;
      rows([['付款银行卡',payer.bank+' · '+t('尾号')+' '+payer.tail],...(sender?[['汇款人姓名',sender.sender_name],['联系电话',sender.sender_phone||t('未提供')]]:[]),['收款人姓名',fields.recipient_name],['收款账号',fields.recipient_account.replace(/\s/g,'')],['开户银行',fields.bank_name],['开户支行',fields.bank_branch],['汇款方式',t(fields.method==='realtime'?'实时汇款':fields.method==='next_day'?'次日汇款':'普通汇款')],['汇款用途',t(fields.purpose)],['附言（选填）',fields.message||t('尚未填写')]]);
    }
    function paint(){
      verification?.dispose();verification=null;
      if(rejected){dialog.dataset.operationState='failed';status.textContent=t('汇款未受理');confirm.hidden=cancel.hidden=query.hidden=true;edit.textContent=t('返回修改');return;}
      const op=view?.operation;if(!op)return;
      const state=record?.status||op.result?.remittance?.status||op.state;dialog.dataset.operationState=state;
      const pending=op.state==='pending_confirmation'&&!unknown;status.textContent=t(unknown?'尚未取得汇款状态，请查询原操作，勿重复提交。':remittanceStatuses[state]||'尚未取得汇款状态，请查询原操作，勿重复提交。');
      confirm.hidden=cancel.hidden=!pending;confirm.disabled=cancel.disabled=busy||!pending;query.hidden=op.state==='pending_confirmation'&&!unknown;edit.textContent=t(pending&&preview?'返回修改':'关闭');
      if(pending){
        paintPreview();
        verification=BankUI.operationVerification(content,view,{isCurrent:()=>active()&&!busy,onError:failure=>{error.textContent=failure.message;},onChange:()=>{confirm.disabled=busy||!verification?.canConfirm();}});
        confirm.disabled=busy||!verification.canConfirm();
      }else if(record){
        content.replaceChildren();const banner=n('div',null,'ah-review-amount');banner.append(n('strong',money(record.amount_minor,'CNY')));content.append(banner);
        const entries=[['收款人姓名',record.recipient_name],['收款账号',record.recipient_account],['开户银行',record.recipient_bank]];
        if(record.mode)entries.push(['汇款方式',t(record.mode==='realtime'?'实时汇款':record.mode==='next_day'?'次日汇款':'普通汇款')]);
        if(state==='scheduled')entries.push(['预计处理时间',remittanceTime(record)]);else if(remittanceDay(record.business_on))entries.push(['账本记账日',record.business_on]);
        if(state==='scheduled'&&validMinor(record.held_minor))entries.push(['已冻结金额',money(record.held_minor,'CNY')]);
        if(validMinor(record.available_minor))entries.push(['当前可用余额',money(record.available_minor,'CNY')]);
        if(record.remittance_id)entries.push(['业务编号',record.remittance_id]);if(record.transaction_id)entries.push(['交易流水编号',record.transaction_id]);
        if(validMinor(record.balance_after_minor))entries.push([state==='succeeded'?'付款后账户余额':'账户余额',money(record.balance_after_minor,'CNY')]);rows(entries);
        if(record.failure_reason)content.append(n('p',record.failure_reason,'ah-form-error'));
        if(state==='scheduled'){
          const advance=n('button','处理到期汇款','ah-action-button'),advanceConfirm=n('div',null,'ah-form-note');advance.type='button';
          advance.onclick=()=>{
            if(busy||unknown||!active())return;advance.disabled=true;
            const from=captured.state?.as_of,businessDay=record.execute_business_on;
            const days=remittanceDay(from)&&remittanceDay(businessDay)?Math.max(1,Math.round((Date.parse(businessDay+'T00:00:00Z')-Date.parse(from+'T00:00:00Z'))/86400000)):null;
            if(!Number.isSafeInteger(days)||days>30){advanceConfirm.textContent='请先查询当前模拟日期与执行日期。';advance.disabled=false;return;}
            const day=new Date(Date.parse(from+'T00:00:00Z')+days*86400000).toISOString().slice(0,10);
            advanceConfirm.replaceChildren(n('p','将模拟日期推进至 '+day+'，处理全部到期汇款及其他到期模拟业务。'));
            const yes=n('button','确认处理','ah-action-button is-primary'),no=n('button','取消','ah-action-button');yes.type=no.type='button';
            no.onclick=()=>{advanceConfirm.replaceChildren();advance.disabled=false;};
            yes.onclick=async()=>{if(busy||unknown||!active())return;setBusy(true);yes.disabled=no.disabled=true;try{await BankClient.post('/simulation/advance',{days,confirmed:true});if(!active())return;await readStatus();await refresh();}catch(failure){await recover(failure);}finally{if(active()){setBusy(false);paint();}}};
            advanceConfirm.append(no,yes);
          };content.append(advance,advanceConfirm);
        }
      }
    }
    async function refresh(){
      if(!active())return;
      try{const next=await BankUI.load();if(!active())return;if(!next?.state||next.state.workspace_id!==captured.state.workspace_id)throw Error('更新账户的身份无法核对。');captured.state=next.state;NavrenConnectedAssets.clear();const connected=await NavrenConnectedAssets.overview({fresh:true});if(!active())return;captured.connected=connected;assets();cards();renderTransactions(true);await loadRemittances(captured);}catch(failure){if(active())error.textContent='回执已保留，账户资料更新暂未完成：'+failure.message;}
    }
    function checkView(next){
      if(next.operation?.id!==attempt.operationId)throw Error('汇款操作编号不一致，已停止展示。');
      const sender=next.operation.payload?.remittance;
      if(typeof sender?.sender_name!=='string'||!sender.sender_name.trim()||typeof sender.sender_phone!=='string')throw Error('尚未取得当前客户的汇款资料，请重新核对。');
      if(attempt.payload){const core=next.operation.payload,expected=attempt.payload;
        if(!core||core.account_id!==expected.account_id||core.amount_minor!==expected.amount_minor||Object.entries(expected).some(([key,value])=>!['account_id','amount_minor','idempotency_key'].includes(key)&&core.remittance?.[key]!==value))throw Error('汇款草稿与已核对的信息不一致，不能确认。');
      }
    }
    function preparedView(next){
      if(!next.operation?.id||!next.operation.confirmation_digest)throw Error('汇款草稿缺少确认信息。');
      attempt.operationId=next.operation.id;checkView(next);view=attempt.view=next;
    }
    async function readStatus(){
      const next=await BankClient.get('/operations/'+encodeURIComponent(attempt.operationId));if(!active())return;checkView(next);
      const receipt=await BankClient.get('/remittances/'+encodeURIComponent(attempt.operationId));if(!active())return;
      const nextRecord=receipt.remittance||receipt;
      if(nextRecord.operation_id!==attempt.operationId||nextRecord.environment!=='simulation'||!remittanceStatuses[nextRecord.status]||!validMinor(nextRecord.amount_minor)||nextRecord.amount_minor<1)throw Error('汇款单据的编号、环境或状态无法核对。');
      const core=next.operation.payload;
      if(!core?.remittance||nextRecord.account_id!==core.account_id||nextRecord.amount_minor!==core.amount_minor||Object.entries(core.remittance).some(([key,value])=>nextRecord[key]!==value))throw Error('汇款单据与原操作内容不一致，已停止展示。');
      if(nextRecord.status==='succeeded'&&(!nextRecord.payment_id||!nextRecord.transaction_id||!validMinor(nextRecord.balance_before_minor)||!validMinor(nextRecord.balance_after_minor)||BigInt(nextRecord.balance_before_minor)-BigInt(nextRecord.balance_after_minor)!==BigInt(nextRecord.amount_minor)))throw Error('成功汇款的流水与余额证据尚未完整取得。');
      view=attempt.view=next;record=nextRecord;unknown=false;attempt.unknown=false;
      if(trigger?.isConnected&&nextRecord.status!=='pending_confirmation'){trigger.textContent=t('查看汇款回执');trigger.dataset.operationId=attempt.operationId;}paint();
    }
    async function recover(failure){
      if(!active())return;
      if(!attempt.operationId&&!attempt.unknown&&[400,403,404,405,413,415,422].includes(failure.status)&&typeof failure.code==='string'&&failure.code){
        rejected=true;unknown=false;attempt.unknown=false;if(remittanceAttempt===attempt)remittanceAttempt=null;error.textContent=failure.message;paint();return;
      }
      unknown=true;attempt.unknown=true;confirm.disabled=cancel.disabled=true;query.hidden=false;status.textContent=t('尚未取得汇款状态，请查询原操作，勿重复提交。');error.textContent=failure.message;
      if(attempt.operationId)try{await readStatus();await refresh();}catch(_){if(active()){unknown=true;confirm.hidden=cancel.hidden=true;query.hidden=false;status.textContent=t('尚未取得汇款状态，请查询原操作，勿重复提交。');}}
    }
    confirm.onclick=async()=>{
      if(busy||unknown||!active()||view?.operation?.state!=='pending_confirmation')return;
      if(!verification?.canConfirm()){error.textContent=verification?.reason()||'请先完成短信验证。';return;}
      const payload={confirmed:true,confirmation_digest:view.operation.confirmation_digest,...verification.payload()};setBusy(true);
      try{const result=await BankClient.post('/operations/'+encodeURIComponent(attempt.operationId)+'/confirm',payload);if(!active())return;const op=result.operation||result;if(op.id!==attempt.operationId)throw Error('确认回执编号不一致，已停止展示。');view=attempt.view={...view,operation:op};await readStatus();await refresh();}catch(failure){await recover(failure);}finally{delete payload.verification_code;delete payload.challenge_id;verification?.clear();if(active()){setBusy(false);paint();}}
    };
    cancel.onclick=async()=>{if(busy||unknown||!active()||view?.operation?.state!=='pending_confirmation')return;setBusy(true);try{const result=await BankClient.post('/operations/'+encodeURIComponent(attempt.operationId)+'/cancel',{});if(!active())return;const op=result.operation||result;if(op.id!==attempt.operationId||op.state!=='cancelled')throw Error('尚未取得原汇款的取消回执。');view=attempt.view={...view,operation:op};await readStatus();await refresh();}catch(failure){await recover(failure);}finally{if(active()){setBusy(false);paint();}}};
    query.onclick=async()=>{if(busy||rejected||!active())return;setBusy(true);try{if(!attempt.operationId){const prepared=await BankClient.post('/remittances/prepare',attempt.payload);if(!active())return;preparedView(prepared);}await readStatus();await refresh();error.textContent='';}catch(failure){await recover(failure);}finally{if(active()){setBusy(false);paint();}}};
    edit.onclick=()=>{if(!busy)dialog.close();};
    dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
    dialog.addEventListener('close',()=>{verification?.dispose();dialog.remove();if(remittanceReview===dialog)remittanceReview=null;if(trigger?.isConnected)trigger.focus();if(current===captured&&captured.isCurrent())BankUI.load().catch(()=>{});},{once:true});
    dialog.addEventListener('click',event=>{if(event.target===dialog&&!busy){const rect=dialog.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)dialog.close();}});
    document.body.append(dialog);dialog.showModal();paintPreview();dialog.append(n('p','本次只更新模拟账本，不会向真实银行发起汇款。','ah-form-note'));
    (async()=>{setBusy(true);try{if(!attempt.operationId){const prepared=await BankClient.post('/remittances/prepare',attempt.payload);if(!active())return;preparedView(prepared);}else{await readStatus();}if(active()){unknown=false;attempt.unknown=false;paint();}}catch(failure){await recover(failure);}finally{if(active()){setBusy(false);paint();if(unknown){confirm.hidden=cancel.hidden=true;query.hidden=false;edit.disabled=false;}}}})();
  }
  function reviewRemittance(captured,form,trigger) {
    if(current!==captured||!captured.isCurrent())throw Error(t('当前账户已变化，请重新核对。'));
    if(document.querySelector('dialog[open]'))return;
    const fields=Object.fromEntries(new FormData(form).entries()),value=remittanceAmount(fields.amount),payer=remittanceCards(captured).find(item=>item.account.id===fields.account_id&&item.card.card_id===remittanceDraft.card_id);
    if(!payer||!value.minor)throw Error(t('请选择有效的付款银行卡。'));
    const payload=remittancePayload(fields,payer,value),fingerprint=JSON.stringify(payload);
    if(!remittanceAttempt||remittanceAttempt.fingerprint!==fingerprint){if(remittanceAttempt?.unknown)throw Error(t('尚未取得汇款状态，请查询原操作，勿重复提交。'));remittanceAttempt={fingerprint,payload:{...payload,idempotency_key:BankClient.key()},operationId:null,view:null};}
    showRemittance(captured,remittanceAttempt,trigger,{fields,payer,value});
  }
  async function loadRemittances(captured){
    const host=document.getElementById('accountRemittanceReceipts');if(!host)return;const guard=BankClient.guard();
    try{const result=await BankClient.get('/remittances');if(current!==captured||!captured.isCurrent()||!guard.valid()||!host.isConnected)return;const rows=Array.isArray(result)?result:result.remittances;if(!Array.isArray(rows))throw Error('汇款列表格式未取得。');host.replaceChildren();if(!rows.length)return;
      host.append(n('h3','汇款记录'));const list=n('div',null,'account-object-list');
      for(const receipt of rows.slice(0,5)){
        if(!receipt?.operation_id||receipt.environment!=='simulation'||!remittanceStatuses[receipt.status]||!validMinor(receipt.amount_minor)||receipt.amount_minor<1||(receipt.workspace_id&&receipt.workspace_id!==captured.state.workspace_id))throw Error('汇款记录无法核对。');
        const row=n('div',null,'account-object'),identity=n('div',null,'account-object-identity'),button=n('button','查看回执','ah-action-button');
        identity.append(n('strong',receipt.recipient_name),n('small',t(remittanceStatuses[receipt.status])+(receipt.status==='scheduled'?' · '+remittanceTime(receipt):remittanceDay(receipt.business_on)?' · '+receipt.business_on:'')));
        button.type='button';button.onclick=()=>showRemittance(captured,{operationId:receipt.operation_id,view:null},button);row.append(identity,amount(receipt.amount_minor,'CNY'),button);list.append(row);
      }host.append(list);
    }catch(failure){if(current===captured&&captured.isCurrent()&&guard.valid()&&host.isConnected)host.replaceChildren(n('p','尚未取得汇款记录','ah-form-note'));}
  }
  function transfer() {
    disposePayer?.();disposePayer=null;disposeContacts?.();disposeContacts=null;
    const host=document.getElementById('accountTransferForm'),captured=current;if(!host)return;host.replaceChildren();
    const card=document.getElementById('accountRemittanceCard');card.open=actionCards.remittance;card.ontoggle=()=>{actionCards.remittance=card.open;};
    const form=n('form',null,'ah-remittance-form');form.id='accountRemittanceForm';const controls={};
    const field=(label,name,type,placeholder,required=true)=>{const item=remittanceField(label,name,type,placeholder,required);item.input.required=required;controls[name]=item.input;form.append(item.wrap);return item;};
    const payer=n('input');payer.type='hidden';payer.id='remittance-account_id';payer.name='account_id';controls.account_id=payer;
    if(!captured.state){host.append(n('p','当前操作账户暂不可用，请重新读取操作账户。','ah-form-note'),BankUI.button('重新读取',()=>BankUI.load().catch(()=>{})));return;}
    const accounts=remittanceCards(captured),picker=payerPicker(captured,accounts,payer);form.append(picker.wrap);
    const contactHost=n('div',null,'ah-full-row');form.append(contactHost);let contactPicker=null;
    field('收款人姓名','recipient_name','text','与收款账户开户姓名一致').input.maxLength=60;
    const account=field('收款账号','recipient_account','text','请输入完整银行卡号或账号').input;account.inputMode='numeric';account.maxLength=40;
    const bank=field('开户银行','bank_name','text','输入或选择银行名称').input;bank.setAttribute('list','remittanceBanks');
    const banks=n('datalist');banks.id='remittanceBanks';for(const name of ['中国工商银行','中国农业银行','中国银行','中国建设银行','交通银行','中国邮政储蓄银行','招商银行','中信银行','中国光大银行','上海浦东发展银行','兴业银行','杭州银行']){const option=n('option');option.value=name;banks.append(option);}form.append(banks);
    field('开户支行','bank_branch','text','请输入开户支行全称');
    const amount=field('汇款金额（元）','amount','text','0.00');amount.input.inputMode='decimal';amount.input.maxLength=22;amount.wrap.classList.add('ah-money-field');
    const shell=n('div',null,'ah-money-shell'),upperWrap=n('div',null,'ah-money-uppercase'),upper=n('output','请输入金额');upper.id='remittanceUppercase';upper.setAttribute('for',amount.input.id);upper.setAttribute('aria-live','polite');upperWrap.append(n('span','大写'),upper);amount.input.setAttribute('aria-describedby',upper.id);shell.append(amount.input,upperWrap);amount.wrap.append(shell);
    const purposeWrap=n('label',null,'ah-remittance-field'),purpose=n('select');purpose.name='purpose';purpose.id='remittance-purpose';purpose.required=true;controls.purpose=purpose;
    for(const value of ['','生活支出','家庭往来','货款／服务费','借款归还','其他']){const option=n('option',value||'请选择用途');option.value=value;purpose.append(option);}purpose.value=remittanceDraft.purpose||'';const purposeCaption=remittanceCaption('汇款用途',true);purposeCaption.id=purpose.id+'-label';purpose.setAttribute('aria-labelledby',purposeCaption.id);purposeWrap.append(purposeCaption,purpose);form.append(purposeWrap);
    const method=n('fieldset',null,'ah-remittance-method');method.append(remittanceCaption('汇款方式',true,'legend'));const choices=n('div',null,'ah-method-choices');
    const savedMethod=remittanceDraft.method==='deferred'?remittanceDraft.deferred||'ordinary':remittanceDraft.method||'realtime';
    for(const [value,label,timing]of [['realtime','实时汇款','确认后处理'],['ordinary','普通汇款','2小时后处理'],['next_day','次日汇款','次日09:00处理']]){const wrap=n('label',null,'ah-method-choice'),input=n('input'),copy=n('span',null,'ah-method-copy');input.type='radio';input.name='method';input.required=true;input.value=value;input.checked=value===savedMethod;copy.append(n('span',label),n('small',timing));wrap.append(input,copy);choices.append(wrap);}method.append(choices);form.append(method);
    field('附言（选填）','message','text','',false).input.maxLength=128;
    const hint=n('p','处理时间为北京时间；当前使用模拟汇款。','ah-form-note ah-full-row');form.append(hint);
    const error=n('p','', 'ah-form-error');error.setAttribute('role','alert');
    const footer=n('div',null,'ah-form-footer'),buttons=n('div',null,'ah-form-buttons'),reset=n('button','清空','ah-action-button'),submit=n('button','核对汇款信息','ah-action-button is-primary');reset.type='button';submit.type='submit';submit.disabled=!accounts.length;if(remittanceAttempt?.view?.operation&&remittanceAttempt.view.operation.state!=='pending_confirmation')submit.textContent=t('查看汇款回执');buttons.append(reset,submit);footer.append(n('span','模拟汇款','ah-form-note'),buttons);form.append(error,footer);host.append(form);
    function updateAmount(cursor=false) {
      const input=amount.input,start=input.selectionStart,before=start==null?null:input.value.slice(0,start).replaceAll(',','').length,result=remittanceAmount(input.value);
      if(!result.error){input.value=result.formatted;if(cursor&&before!==null){let pos=0,count=0;while(pos<input.value.length&&count<before){if(input.value[pos]!==',')count++;pos++;}input.setSelectionRange(pos,pos);}}
      input.setCustomValidity(result.error?t(result.error):result.minor===0?t('金额必须大于零。'):'');upper.textContent=result.error?t('金额格式有误'):result.uppercase||t('请输入金额');
    }
    function updateFields() {
      for(const name of ['recipient_name','bank_name','bank_branch'])controls[name].setCustomValidity(controls[name].value&&!controls[name].value.trim()?t('请填写有效信息。'):'');
      const cleanAccount=account.value.replace(/\s/g,'');account.setCustomValidity(cleanAccount&&!/^\d{10,32}$/.test(cleanAccount)?t('账号须为10至32位数字。'):'');
    }
    const save=()=>{updateFields();remittanceDraft={...Object.fromEntries(new FormData(form).entries()),card_id:remittanceDraft.card_id};error.textContent='';if(remittanceAttempt){const selected=remittanceCards(captured).find(item=>item.card.card_id===remittanceDraft.card_id&&item.account.id===payer.value),value=remittanceAmount(controls.amount.value);const matching=selected&&value.minor&&JSON.stringify(remittancePayload(Object.fromEntries(new FormData(form).entries()),selected,value))===remittanceAttempt.fingerprint;submit.textContent=t(matching&&remittanceAttempt.view?.operation?.state!=='pending_confirmation'?'查看汇款回执':'核对汇款信息');}};
    form.addEventListener('input',e=>{if(e.target===amount.input)updateAmount(true);save();});form.addEventListener('change',save);
    amount.input.addEventListener('blur',()=>{const value=remittanceAmount(amount.input.value);if(value.minor!==null&&!value.error){const minor=BigInt(value.minor);amount.input.value=String(minor/100n)+'.'+String(minor%100n).padStart(2,'0');updateAmount();save();}});
    reset.onclick=()=>{if(remittanceAttempt?.unknown){error.textContent=t('尚未取得汇款状态，请查询原操作，勿重复提交。');return;}remittanceAttempt=null;remittanceDraft={};contactPicker?.reset();form.reset();for(const input of Object.values(controls))if(input!==payer)input.value='';picker.reset();form.querySelector('[name=method][value=realtime]').checked=true;updateAmount();save();delete form.dataset.navrenDirty;amount.input.focus();};
    form.addEventListener('submit',event=>{event.preventDefault();updateAmount();updateFields();if(!form.reportValidity())return;save();try{reviewRemittance(captured,form,submit);}catch(failure){error.textContent=failure.message;}});
    updateAmount();updateFields();
    if(window.NavrenPaymentContacts){contactPicker=NavrenPaymentContacts.mountPicker(contactHost,{
      isCurrent:()=>current===captured&&captured.isCurrent()&&form.isConnected,
      canPick:()=>{if(remittanceAttempt?.unknown){error.textContent=t('尚未取得汇款状态，请查询原操作，勿重复提交。');return false;}return true;},
      onPick:contact=>{if(!contact||current!==captured||!captured.isCurrent())return;
        controls.recipient_name.value=contact.recipient_name;controls.recipient_account.value=contact.recipient_account;
        controls.bank_name.value=contact.recipient_bank;controls.bank_branch.value=contact.recipient_branch;
        save();for(const name of ['recipient_name','recipient_account','bank_name','bank_branch'])controls[name].dispatchEvent(new Event('input',{bubbles:true}));
      }
    });disposeContacts=()=>contactPicker?.destroy();}
    const receipts=n('section',null,'ah-remittance-receipts');receipts.id='accountRemittanceReceipts';host.append(receipts);loadRemittances(captured);
  }
  function splitPayments() {
    const host=document.getElementById('accountSplitForm'),captured=current;if(!host)return;
    const card=document.getElementById('accountSplitCard');card.open=actionCards.split;card.ontoggle=()=>{actionCards.split=card.open;if(card.open&&current===captured&&captured.isCurrent()&&!document.getElementById('accountHome').hidden)splitPayments();};
    if(!window.NavrenAAPayments){host.replaceChildren(n('p','AA收款暂不可用，请稍后重新读取。','ah-muted'));return;}
    aaOptions={
      state:captured.state,connected:captured.connected,
      isCurrent:()=>current===captured&&captured.isCurrent()&&!document.getElementById('accountHome').hidden,
      onChange:()=>{if(current!==captured||!captured.isCurrent())return;NavrenConnectedAssets.clear();BankUI.load().catch(()=>{});}
    };
    const controller=NavrenAAPayments.mount(host,aaOptions);
    controller?.ready?.catch(error=>{if(current===captured&&captured.isCurrent()&&!host.querySelector('.aa-error'))host.append(n('p',error.message||'AA收款资料暂未取得。','aa-error'));});
  }
  function previousMonth(date) { const match=/^(\d{4})-(\d{2})-\d{2}$/.exec(date||'');if(!match)return '';let year=Number(match[1]),month=Number(match[2])-1;if(month===0){month=12;year--;}return year+'-'+String(month).padStart(2,'0'); }
  const validMonth=value=>/^\d{4}-(?:0[1-9]|1[0-2])$/.test(value||'');
  function monthBounds() {
    const history=current.financial,connected=current.connected;
    const max=[history&&(history.evidence?.coverage_end||history.as_of)?.slice(0,7),connected?.as_of?.slice(0,7)].filter(validMonth).sort().at(-1);
    const min=history?.evidence?.coverage_start?.slice(0,7)||max;
    const asOf=(history?.evidence?.coverage_end||history?.as_of||connected?.as_of),lastDay=asOf&&new Date(Number(asOf.slice(0,4)),Number(asOf.slice(5,7)),0).getDate();
    const complete=asOf&&Number(asOf.slice(8,10))===lastDay?asOf.slice(0,7):previousMonth(asOf);
    return {min,max,fallback:validMonth(complete)?complete:max};
  }
  function rememberMonth() {
    NavrenViews.update?.({filters:{...NavrenViews.current?.()?.filters,account_month:accountMonth,report_month:accountMonth,transaction_month:accountMonth}});
  }
  function selectMonth(value) {
    const {min,max}=monthBounds();if(!validMonth(value)||value<min||value>max)return false;
    accountMonth=value;rememberMonth();configureMonths();renderMonthly();renderTransactions(true);return true;
  }
  function configureMonths() {
    const {min,max,fallback}=monthBounds();if(!validMonth(max))return;
    const context=NavrenViews.current?.(),identity=BankClient.identity(),contextMatches=(!context?.workspace_id||context.workspace_id===identity?.workspace_id)&&(context?.privacy_revision==null||context.privacy_revision===identity?.privacy_revision);
    const filters=contextMatches?context?.filters:{};
    const candidate=[accountMonth,filters?.account_month,filters?.report_month,filters?.transaction_month].find(value=>validMonth(value)&&value>=min&&value<=max);
    accountMonth=candidate||(!validMonth(fallback)||fallback<min?min:fallback>max?max:fallback);
    rememberMonth();const captured=current,token=epoch;
    for(const [id,enabled]of [['accountReportMonth',!!current.financial],['accountTransactionMonth',!!current.connected]]){
      const input=document.getElementById(id);input.value=accountMonth;input.min=min;input.max=max;input.disabled=!enabled;
      input.onchange=()=>{if(!alive(token,captured)||input.disabled)return;if(!selectMonth(input.value)){input.value=accountMonth;window.NavrenMonthPicker?.refresh(input);}};
      window.NavrenMonthPicker?.mount(input,{currentMonth:max});
    }
  }
  function alive(token,captured) { return token===epoch && current===captured && captured.isCurrent() && !document.getElementById('accountHome').hidden; }
  function loadError(host,error,retry) {host.replaceChildren(n('p',error.message,'ah-muted'));const button=n('button','重新读取','ah-small-button');button.type='button';button.onclick=retry;host.append(button);}
  function chart(rows,partial=false,report={}) {
    const samples=rows.map(row=>({...row,time:Date.parse(row.date+'T00:00:00Z')}));
    const isPoint=row=>!!row&&validMinor(row.total_assets_minor)&&Number.isFinite(row.time)&&new Date(row.time).toISOString().slice(0,10)===row.date;
    const points=samples.filter(isPoint);if(!points.length)return null;
    const fig=n('figure',null,'ah-history-chart'),caption=n('figcaption'),frame=n('div',null,'ah-chart-frame');
    caption.append(n('strong','总资产走势'),n('span',partial?'期初及期间快照':'期初、每周及期末快照'));fig.append(caption,frame);
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 680 300');svg.setAttribute('role','group');svg.setAttribute('aria-label',t('历史资产估值')+' '+points[0].date+' — '+points.at(-1).date);
    const shape=(tag,attributes={},text)=>{const el=document.createElementNS(svg.namespaceURI,tag);for(const [key,value]of Object.entries(attributes))el.setAttribute(key,String(value));if(text!=null)el.textContent=text;return el;};
    svg.append(shape('title',{},points.map(row=>row.date+': '+money(row.total_assets_minor,'CNY')).join('；')));
    const values=points.map(row=>row.total_assets_minor),low=Math.min(...values),high=Math.max(...values),padding=Math.max((high-low)*.26,10000),floor=low-padding,ceiling=high+padding;
    const left=88,right=654,top=76,bottom=250,first=Math.min(...points.map(row=>row.time)),last=Math.max(...points.map(row=>row.time));
    const x=row=>last===first?(left+right)/2:left+(row.time-first)/(last-first)*(right-left),y=row=>bottom-(row.total_assets_minor-floor)/(ceiling-floor)*(bottom-top);
    const magnitude=Math.max(Math.abs(low),Math.abs(high)),unit=magnitude>=10000000000?10000000000:magnitude>=1000000?1000000:100,label=unit===10000000000?'金额（亿元）':unit===1000000?'金额（万元）':'金额（元）';
    const locale=window.NavrenI18n?.language==='en'?'en-US':'zh-CN',tickPrecision=Math.max(0,Math.min(8,Math.ceil(-Math.log10((ceiling-floor)/4/unit))+1));
    const compact=value=>(value/unit).toLocaleString(locale,{minimumFractionDigits:2,maximumFractionDigits:2});
    const gradientId='ah-asset-area-'+monthlyRequest,defs=shape('defs'),gradient=shape('linearGradient',{id:gradientId,x1:0,y1:0,x2:0,y2:1});gradient.append(shape('stop',{offset:'0%',class:'ah-chart-gradient-top'}),shape('stop',{offset:'100%',class:'ah-chart-gradient-bottom'}));defs.append(gradient);svg.append(defs,shape('text',{x:left,y:24,class:'ah-chart-unit'},t(label)));
    for(let index=0;index<=4;index++){
      const position=top+index*(bottom-top)/4,value=ceiling-index*(ceiling-floor)/4;
      svg.append(shape('line',{x1:left,y1:position,x2:right,y2:position,class:'ah-chart-grid'}),shape('text',{x:left-14,y:position+4,'text-anchor':'end',class:'ah-chart-tick'},(value/unit).toLocaleString(locale,{minimumFractionDigits:tickPrecision,maximumFractionDigits:tickPrecision})));
    }
    const rangeBand=shape('rect',{class:'ah-chart-range',x:left,y:top,width:0,height:bottom-top,rx:5,hidden:true}),cursor=shape('line',{class:'ah-chart-cursor',x1:left,x2:left,y1:top,y2:bottom,hidden:true});svg.append(rangeBand);
    const segments=[];let segment=[];
    for(const row of samples){if(isPoint(row))segment.push(row);else if(segment.length){segments.push(segment);segment=[];}}if(segment.length)segments.push(segment);
    for(const line of segments){const coordinates=line.map(row=>x(row).toFixed(2)+','+y(row).toFixed(2)).join(' ');if(line.length>1)svg.append(shape('polygon',{points:x(line[0]).toFixed(2)+','+bottom+' '+coordinates+' '+x(line.at(-1)).toFixed(2)+','+bottom,class:'ah-chart-fill',fill:'url(#'+gradientId+')'}),shape('polyline',{points:coordinates,class:'ah-chart-line','vector-effect':'non-scaling-stroke'}));}
    svg.append(cursor);
    const placed=[],nodes=[],tip=n('div',null,'ah-chart-tooltip'),tipId='ah-asset-detail-'+monthlyRequest;tip.id=tipId;tip.hidden=true;tip.setAttribute('role','status');tip.setAttribute('aria-live','polite');
    points.forEach((row,index)=>{
      const cx=x(row),cy=y(row),text=compact(row.total_assets_minor),width=Math.max(60,text.length*7.2+12),labelX=Math.min(Math.max(cx-width/2,left-8),right-width+8);
      let labelY=cy-30;
      for(const candidate of [cy-30,cy-56,cy+14,cy-82,cy+40]){const box={x:labelX,y:Math.max(33,Math.min(candidate,bottom-20)),width,height:21};if(!placed.some(prior=>box.x<prior.x+prior.width+6&&box.x+box.width+6>prior.x&&box.y<prior.y+prior.height+4&&box.y+box.height+4>prior.y)){labelY=box.y;break;}}
      placed.push({x:labelX,y:labelY,width,height:21});
      const point=shape('g',{class:'ah-chart-point',tabindex:0,role:'button','aria-label':(row.opening?t('期初')+' · ':'')+row.date+' · '+money(row.total_assets_minor,'CNY'),'aria-describedby':tipId,'data-last':String(index===points.length-1),'data-opening':String(!!row.opening)});
      point.append(shape('circle',{cx,cy,r:15,class:'ah-chart-hit'}),shape('circle',{cx,cy,r:8,class:'ah-chart-halo'}),shape('circle',{cx,cy,r:4,class:'ah-chart-dot'}));
      const value=shape('g',{class:'ah-chart-value'});if(Math.abs(labelY-(cy-30))>2)value.append(shape('line',{x1:cx,y1:cy-7,x2:labelX+width/2,y2:labelY+19,class:'ah-chart-leader'}));value.append(shape('text',{x:labelX+width/2,y:labelY+15,'text-anchor':'middle'},text));point.append(value);svg.append(point);nodes.push(point);
      point.addEventListener('focus',()=>show(index));point.addEventListener('click',()=>show(index));point.addEventListener('keydown',event=>{if(event.key==='Escape'){hide();return;}let target;if(event.key==='ArrowLeft')target=Math.max(0,index-1);if(event.key==='ArrowRight')target=Math.min(points.length-1,index+1);if(event.key==='Home')target=0;if(event.key==='End')target=points.length-1;if(target!=null){event.preventDefault();nodes[target].focus();}if(event.key==='Enter'||event.key===' '){event.preventDefault();show(index);}});
    });
    let priorLabel=-Infinity;
    points.forEach((row,index)=>{const position=x(row),isLast=index===points.length-1;if(index&&!isLast&&(position-priorLabel<76||right-position<76))return;svg.append(shape('text',{x:position,y:281,'text-anchor':index===0&&points.length>1?'start':isLast&&points.length>1?'end':'middle',class:'ah-chart-tick'},row.date.slice(5).replace('-','/')));priorLabel=position;});
    frame.append(svg);fig.append(n('p','悬停或点选查看区间详情','ah-chart-hint'),tip);
    function intervalFlow(previous,row){
      if(!Array.isArray(report.daily)||!/^\d{4}-\d{2}-\d{2}$/.test(report.period?.start_date||'')||!/^\d{4}-\d{2}-\d{2}$/.test(report.period?.end_date||''))return null;
      const day=new Date(previous.time);day.setUTCDate(day.getUTCDate()+1);const start=day.toISOString().slice(0,10),end=row.date,keys=['income_minor','expense_minor','refund_minor','net_cash_change_minor'],sums=Object.fromEntries(keys.map(key=>[key,0n]));
      if(start<report.period.start_date||end>report.period.end_date)return null;
      const dates=new Map();for(const item of report.daily){if(item.date>=start&&item.date<=end){if(dates.has(item.date))return null;dates.set(item.date,item);}}
      for(;day.getTime()<=row.time;day.setUTCDate(day.getUTCDate()+1)){const item=dates.get(day.toISOString().slice(0,10));if(!item||item.covered!==true||keys.some(key=>!validMinor(item[key])))return null;for(const key of keys)sums[key]+=BigInt(item[key]);}
      if(Object.values(sums).some(value=>value>BigInt(Number.MAX_SAFE_INTEGER)||value<BigInt(Number.MIN_SAFE_INTEGER)))return null;
      return {start,end,...Object.fromEntries(keys.map(key=>[key,Number(sums[key])]))};
    }
    function field(list,label,value){const item=n('div');item.append(n('dt',label),n('dd',money(value,'CNY')));list.append(item);}
    let shownIndex=-1;
    function hide(){
      shownIndex=-1;
      if(!tip.hidden)tip.hidden=true;
      if(tip.firstChild)tip.replaceChildren();
      if('rangeStart' in tip.dataset)delete tip.dataset.rangeStart;
      if('rangeEnd' in tip.dataset)delete tip.dataset.rangeEnd;
      if(!rangeBand.hasAttribute('hidden'))rangeBand.setAttribute('hidden','');
      if(!cursor.hasAttribute('hidden'))cursor.setAttribute('hidden','');
      nodes.forEach(point=>{if(point.hasAttribute('data-active'))point.removeAttribute('data-active');});
    }
    function show(index,event){
      if(!fig.isConnected)return;if(shownIndex===index&&!tip.hidden){place(index,event);return;}shownIndex=index;const row=points[index],previous=samples[samples.indexOf(row)-1],hasInterval=isPoint(previous)&&previous.time<row.time;
      tip.replaceChildren();tip.hidden=false;tip.dataset.rangeStart=hasInterval?previous.date:row.date;tip.dataset.rangeEnd=row.date;nodes.forEach((point,i)=>point.dataset.active=String(i===index));cursor.removeAttribute('hidden');cursor.setAttribute('x1',x(row));cursor.setAttribute('x2',x(row));
      if(hasInterval){rangeBand.removeAttribute('hidden');rangeBand.setAttribute('x',x(previous));rangeBand.setAttribute('width',Math.max(1,x(row)-x(previous)));}else rangeBand.setAttribute('hidden','');
      const header=n('div',null,'ah-chart-tip-heading');header.append(n('strong',hasInterval?previous.date.replaceAll('-','/')+' — '+row.date.replaceAll('-','/'):row.date.replaceAll('-','/')),n('span',hasInterval?'相邻快照区间':row.opening?'期初快照':'当日资产快照'));tip.append(header);
      const assets=n('dl',null,'ah-chart-tip-assets');if(hasInterval)field(assets,'起点总资产',previous.total_assets_minor);field(assets,hasInterval?'终点总资产':'总资产',row.total_assets_minor);tip.append(assets);
      if(hasInterval){const difference=BigInt(row.total_assets_minor)-BigInt(previous.total_assets_minor),delta=Number(difference),change=n('div',null,'ah-chart-tip-change');change.append(n('span','资产变动'),n('strong',validMinor(delta)?(delta>0?'+':'')+money(delta,'CNY'):t('未取得')));const percent=n('span',previous.total_assets_minor>0?((row.total_assets_minor-previous.total_assets_minor)/previous.total_assets_minor*100).toLocaleString(locale,{minimumFractionDigits:2,maximumFractionDigits:2})+'%':t('变化幅度未取得'),'ah-chart-tip-percent');change.append(percent);tip.append(change);}else if(!row.opening)tip.append(n('p','没有可比较的前一快照','ah-chart-tip-note'));
      const composition=n('dl',null,'ah-chart-tip-composition');for(const [label,key]of [['净资产','net_worth_minor'],['现金余额','cash_minor'],['投资估值','investment_minor'],['其他资产','real_assets_minor'],['负债','liabilities_minor']])if(validMinor(row[key]))field(composition,label,row[key]);if(composition.children.length)tip.append(composition);
      const flow=hasInterval?intervalFlow(previous,row):null;if(flow){const section=n('section',null,'ah-chart-tip-flow'),list=n('dl');section.append(n('h4',(flow.start===flow.end?flow.start.slice(5).replace('-','/'):flow.start.slice(5).replace('-','/')+' — '+flow.end.slice(5).replace('-','/'))+' '+t('区间收支')));for(const [label,key]of [['收入','income_minor'],['支出','expense_minor'],['退款','refund_minor'],['现金变动','net_cash_change_minor']])field(list,label,flow[key]);section.append(list);tip.append(section);}
      place(index,event);
    }
    function place(index,event){
      const rect=fig.getBoundingClientRect(),pointRect=nodes[index].getBoundingClientRect(),anchorX=event?event.clientX-rect.left:pointRect.left-rect.left+pointRect.width/2,anchorY=event?event.clientY-rect.top:pointRect.bottom-rect.top;
      tip.style.left=Math.max(0,Math.min(anchorX+16,rect.width-tip.offsetWidth))+'px';tip.style.top=Math.max(64-rect.top,Math.min(anchorY-tip.offsetHeight-16,innerHeight-tip.offsetHeight-12-rect.top))+'px';
    }
    svg.addEventListener('pointermove',event=>{if(event.pointerType==='touch')return;const rect=svg.getBoundingClientRect(),px=(event.clientX-rect.left)*680/rect.width,py=(event.clientY-rect.top)*300/rect.height;if(px<left-18||px>right+18||py<top-35||py>bottom+16){hide();return;}const closest=points.reduce((best,row,i)=>Math.abs(x(row)-px)<Math.abs(x(points[best])-px)?i:best,0),index=Math.abs(x(points[closest])-px)<12?closest:points.findIndex(row=>x(row)>=px);show(index===-1?points.length-1:index,event);});
    fig.addEventListener('pointerleave',hide);fig.addEventListener('mouseleave',hide);fig.addEventListener('focusout',event=>{if(!fig.contains(event.relatedTarget))hide();});fig.addEventListener('keydown',event=>{if(event.key==='Escape')hide();});
    const abort=new AbortController();document.addEventListener('pointerdown',event=>{if(!fig.contains(event.target))hide();},{signal:abort.signal});document.addEventListener('keydown',event=>{if(event.key==='Escape')hide();},{signal:abort.signal});disposeHistoryChart=()=>{hide();abort.abort();};
    return fig;
  }
  async function renderMonthly(fresh=false) {
    const host=document.getElementById('accountMonthlyReport'),captured=current,token=epoch,month=accountMonth,request=++monthlyRequest;if(!month||!captured?.financial)return;const active=()=>alive(token,captured)&&month===accountMonth&&request===monthlyRequest;disposeHistoryChart?.();disposeHistoryChart=null;host.replaceChildren(n('p','正在读取月报…','ah-muted'));
    const coverage=captured.financial.evidence;if(coverage&&(month<coverage.coverage_start?.slice(0,7)||month>coverage.coverage_end?.slice(0,7))){host.replaceChildren(n('p','该月份暂无历史月报；新增操作可在下方账户流水查看。','ah-muted'));return;}
    try {
      const envelope=await NavrenFinancialData.month(month,null,{fresh}),report=envelope.data;if(!active())return;if(report.month&&report.month!==month)throw Error('月报月份无法核对，请重新读取。');
      const closing=report.closing,partial=!!report.period?.coverage_partial,endDate=report.period?.end_date;let curveError=null,rows=[];try{rows=await NavrenFinancialData.assetCurve(envelope,{fresh});}catch(error){curveError=error;}if(!active())return;host.replaceChildren();
      if(rows.length&&validMinor(report.opening?.total_assets_minor)&&/^\d{4}-\d{2}-\d{2}$/.test(report.opening?.date||'')&&report.opening.date<rows[0].date)rows=[{...report.opening,opening:true},...rows];
      const summary=n('div',null,'ah-month-summary'),grid=n('div',null,'ah-month-stats');if(partial&&endDate)summary.append(n('p',window.NavrenI18n?.language==='en'?'Through '+endDate:'截至'+Number(endDate.slice(5,7))+'月'+Number(endDate.slice(8,10))+'日','ah-month-period'));for(const [label,key]of [[partial?'截至日总资产':'期末总资产','total_assets_minor'],[partial?'截至日净资产':'期末净资产','net_worth_minor'],[partial?'截至日现金':'期末现金','cash_minor']])grid.append(metric(t(label),closing?.[key],'CNY',key==='total_assets_minor'?'ah-month-primary':''));summary.append(grid);if(report.period?.start_date&&endDate)summary.append(n('p',report.period.start_date+' — '+endDate,'ah-month-period'));host.append(summary);
      const content=n('div',null,'ah-month-content'),plot=n('div',null,'ah-month-plot'),fig=chart(rows,partial,report);if(fig)plot.append(fig);else {plot.append(n('p',curveError?.message||'所选期间的资产曲线资料未取得。','ah-report-basis'));if(curveError)plot.append(BankUI.button('重新读取资产曲线',()=>renderMonthly(true)));}
      const cash=n('div',null,'ah-cash-month');cash.append(n('h3',t(partial?'期间现金收支':'现金月结')));const totals=n('div',null,'ah-flow-stats');for(const [label,key]of [[partial?'本期收入':'本月收入','income_minor'],[partial?'本期支出':'本月支出','expense_minor'],['退款','refund_minor'],[partial?'期间现金变动':'现金净变动','net_cash_change_minor']])totals.append(metric(t(label),report.totals?.[key],'CNY'));cash.append(totals);content.append(plot,cash);host.append(content);
      const footer=n('div',null,'ah-month-footer'),endpoints=n('div',null,'ah-cash-endpoints');endpoints.append(metric(t('期初现金'),report.opening?.cash_minor,'CNY'),metric(t(partial?'截至日现金':'期末现金'),closing?.cash_minor,'CNY'));footer.append(endpoints,route(partial?'查看期间明细':'查看完整月报','statements',{source_scope:NavrenFinancialData.scope,period:month},'ah-report-link'));host.append(footer);
      const notes=n('div',null,'ah-month-notes');notes.append(NavrenFinancialUI.basis(envelope));if(partial)notes.append(n('p','所选月份仅展示已覆盖期间。','ah-report-basis'));host.append(notes);
    }catch(error){if(active())loadError(host,error,()=>renderMonthly(true));}
  }
  async function renderTransactions(fresh=false,background=false) {
    const host=document.getElementById('accountRecentTransactions'),captured=current,token=epoch,month=accountMonth,request=++transactionsRequest;if(!month||!captured?.connected)return;const active=()=>alive(token,captured)&&month===accountMonth&&request===transactionsRequest;if(!background)host.replaceChildren(n('p','正在读取交易…','ah-muted'));
    try {
      const envelope=await NavrenConnectedAssets.transactions({month,limit:8,offset:0},{fresh}),rows=envelope.data.items;if(!active())return;
      if(envelope.version!==captured.connected.version){if(!background)host.replaceChildren(n('p','正在同步账户资产…','ah-muted'));BankUI.load().catch(error=>{if(alive(token,captured))loadError(host,error,()=>renderTransactions(true));});return;}
      const snapshot=JSON.stringify([month,window.NavrenI18n?.language,envelope.data,captured.connected.data.accounts.map(a=>[a.account_id,a.name]),captured.connected.data.connections.map(c=>[c.connection_id,c.institution_name])]);
      if(background&&snapshot===transactionSnapshot&&host.querySelector('.ah-transactions-footer'))return;
      host.replaceChildren();
      host.append(n('p','全部账户流水（含新增操作） · 所选月份 '+month,'ah-report-basis'));
      if(!rows.length)host.append(n('p','所选月份没有已记录流水，不代表没有发生交易。','ah-muted'));else NavrenFinancialUI.transactionTable(host,envelope,rows,captured.connected.data);
      if(rows.length){const dates=rows.map(row=>row.occurred_at.slice(0,10)).sort();host.append(n('p','当前显示记录 '+dates[0]+'—'+dates.at(-1),'ah-report-basis'));}
      const foot=n('div',null,'ah-transactions-footer');foot.append(n('span',window.NavrenI18n?.language==='en'?'Showing '+rows.length+' / '+envelope.data.total+' transactions':'显示 '+rows.length+' / 共 '+envelope.data.total+' 笔'),route('全部交易','statements',{source_scope:NavrenConnectedAssets.scope,period:month},'ah-text-link'));host.append(foot);
      transactionSnapshot=snapshot;
    }catch(error){if(active())loadError(host,error,()=>renderTransactions(true));}
  }
  function stopSync(){clearTimeout(syncTimer);syncTimer=null;}
  function scheduleSync(){stopSync();const captured=current,token=epoch;if(!captured||!alive(token,captured)||document.hidden)return;syncTimer=setTimeout(()=>{syncTimer=null;if(!alive(token,captured)||document.hidden)return;if(document.querySelector('dialog[open],.navren-month-popup,.nv-control-popup,.ah-payer-menu:not([hidden])')){scheduleSync();return;}BankUI.load().catch(()=>{if(alive(token,captured))scheduleSync();});},30000);}
  function clear() {disposeHistoryChart?.();disposeHistoryChart=null;window.NavrenAAPayments?.clear?.();disposePayer?.();disposePayer=null;disposeContacts?.();disposeContacts=null;window.NavrenMonthPicker?.close(false);for(const id of ['accountReportMonth','accountTransactionMonth']){const input=document.getElementById(id);if(input){input.value='';input.disabled=true;window.NavrenMonthPicker?.refresh(input);}}stopSync();stopCardStack(true);epoch++;current=null;owner=null;accountMonth='';remittanceDraft={};remittanceAttempt=null;actionCards={remittance:false,split:false};closeRemittanceReview();snapshots.clear();for(const id of ['accountRemittanceCard','accountSplitCard']){const card=document.getElementById(id);if(card)card.open=false;}for(const id of ['assetOverview','accountCardsPreview','accountAssetsSupplement','accountMonthlyReport','accountRecentTransactions','accountTransferForm','accountSplitForm'])document.getElementById(id)?.replaceChildren();}
  async function render(options) {
    stopSync();
    if(options.state&&options.state.workspace_id!==BankClient.identity()?.workspace_id)options={...options,state:null};
    const binding=BankClient.identity(),identity=[binding?.workspace_id||options.state?.workspace_id,binding?.account_id,binding?.privacy_revision].join('|');
    const language=window.NavrenI18n?.language||'zh',incremental=owner===identity&&current?.connected&&current?.financial&&current.isCurrent()&&renderLanguage===language;
    if(incremental){await refresh(options);return;}
    stopCardStack(owner!==identity);
    if(owner!==identity){window.NavrenAAPayments?.clear?.();snapshots.clear();closeRemittanceReview();accountMonth='';remittanceDraft={};remittanceAttempt=null;actionCards={remittance:false,split:false};owner=identity;}
    cardStructure='';transactionSnapshot='';renderLanguage=language;renderRequest++;
    current=options;const token=++epoch;snapshots.clear();transfer();splitPayments();showPaymentDirectory('loading');
    for(const id of ['accountReportMonth','accountTransactionMonth']){const input=document.getElementById(id);input.value=accountMonth;input.disabled=true;window.NavrenMonthPicker?.mount(input);}
    for(const id of ['assetOverview','accountCardsPreview','accountRecentTransactions'])document.getElementById(id)?.replaceChildren(n('p','正在同步账户资产…','ah-muted'));
    document.getElementById('accountMonthlyReport')?.replaceChildren(n('p','正在读取月报…','ah-muted'));
    document.querySelectorAll('#accountHome [data-ah-text]').forEach(el=>el.textContent=t(el.dataset.ahText));
    const connected=async()=>{try{const envelope=await NavrenConnectedAssets.overview({fresh:true});if(!alive(token,options))return;if(options.validateConnectedEnvelope&&!options.validateConnectedEnvelope(envelope))return;current.connected=envelope;current.connectedError=false;assets();cards();transfer();splitPayments();if(options.state&&options.state.version!==envelope.version){BankUI.load().catch(()=>{});}}catch(error){if(alive(token,options)){current.connected=null;current.connectedError=true;transfer();for(const id of ['assetOverview','accountCardsPreview','accountRecentTransactions'])loadError(document.getElementById(id),error,()=>{NavrenConnectedAssets.clear();BankUI.load().catch(()=>{});});showPaymentDirectory('unknown');}}};
    const history=async()=>{try{const envelope=await NavrenFinancialData.overview();if(!alive(token,options))return;current.financial=envelope;}catch(error){if(alive(token,options)){current.financial=null;loadError(document.getElementById('accountMonthlyReport'),error,()=>{NavrenFinancialData.clear();render(options);});}}};
    await Promise.allSettled([connected(),history()]);if(alive(token,options)){configureMonths();if(current.financial)renderMonthly();if(current.connected)renderTransactions(true);scheduleSync();}
  }
  async function refresh(options) {
    const captured=current,token=epoch,request=++renderRequest,previousState=captured.state,previous=captured.connected,history=captured.financial,month=accountMonth;
    Object.assign(captured,options);
    const active=()=>request===renderRequest&&alive(token,captured);
    try {
      const [connected,financial]=await Promise.allSettled([NavrenConnectedAssets.overview({fresh:true}),NavrenFinancialData.overview()]);
      if(!active())return;
      if(connected.status==='rejected'){
        stopCardStack();cardStructure='';captured.connected=null;captured.connectedError=true;transfer();splitPayments();
        for(const id of ['assetOverview','accountCardsPreview','accountRecentTransactions'])loadError(document.getElementById(id),connected.reason,()=>{NavrenConnectedAssets.clear();BankUI.load().catch(()=>{});});
        showPaymentDirectory('unknown');
      }else {
        if(captured.validateConnectedEnvelope&&!captured.validateConnectedEnvelope(connected.value))return;
        captured.connected=connected.value;captured.connectedError=false;assets();cards();
        // Rebuild business controls only when their ledger or eligibility has changed.
        const eligibility=envelope=>JSON.stringify([envelope?.as_of,
          envelope?.data.accounts.map(a=>[a.account_id,a.account_type,a.name,a.currency,a.status,a.connection_id,a.connection_status,a.connection_active]),
          envelope?.data.cards.map(c=>[c.card_id,c.account_id,c.card_type,c.status,c.masked_number,c.expires_on,c.registration_only]),
          envelope?.data.connections.map(c=>[c.connection_id,c.institution_name,c.institution_type,c.status])]);
        const runtimeEligibility=state=>JSON.stringify([state?.as_of,state?.accounts?.map(a=>[a.id,a.currency,a.status]),state?.cards?.map(c=>[c.id,c.account_id,c.type,c.status,c.last_four,c.expires_on])]);
        if(runtimeEligibility(previousState)!==runtimeEligibility(captured.state)||eligibility(previous)!==eligibility(connected.value)){transfer();splitPayments();}
        else {refreshPayer?.();if(aaOptions){aaOptions.state=captured.state;aaOptions.connected=captured.connected;}}
        if(captured.state&&captured.state.version!==connected.value.version)BankUI.load().catch(()=>{});
      }
      if(financial.status==='rejected'){
        captured.financial=null;disposeHistoryChart?.();disposeHistoryChart=null;loadError(document.getElementById('accountMonthlyReport'),financial.reason,()=>{NavrenFinancialData.clear();render(captured);});
      }else captured.financial=financial.value;
      const bounds=monthBounds(),monthControls=['accountReportMonth','accountTransactionMonth'].map(id=>document.getElementById(id));
      if(monthControls.some((input,i)=>input.min!==bounds.min||input.max!==bounds.max||input.value!==accountMonth||input.disabled!==(i===0?!captured.financial:!captured.connected)))configureMonths();
      if(captured.financial&&(history?.dataset_version!==captured.financial.dataset_version||month!==accountMonth))renderMonthly();
      if(captured.connected&&(previous?.version!==captured.connected.version||previous?.as_of!==captured.connected.as_of||month!==accountMonth))renderTransactions(true,month===accountMonth);
    }finally{if(active())scheduleSync();}
  }
  window.addEventListener('navren:privacy-reset',clear);
  window.addEventListener('navren:account-connections-changed',()=>{const captured=current;if(captured&&alive(epoch,captured))BankUI.load().catch(()=>{});});
  window.addEventListener('pagehide',()=>{window.NavrenAAPayments?.clear?.();stopSync();disposePayer?.();disposePayer=null;disposeContacts?.();disposeContacts=null;});
  document.addEventListener('visibilitychange',()=>{if(document.hidden){stopSync();return;}const captured=current;if(captured&&alive(epoch,captured))BankUI.load().catch(()=>scheduleSync());});
  return {render,refreshAssets(){if(current){assets();cards();}},clear,remittanceAmount};
})();
