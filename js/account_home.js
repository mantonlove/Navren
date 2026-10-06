/* 账户首页：只读取当前工作区资料，复用银行确认表单，不保存金融状态。 */
window.NavrenAccountHome = (() => {
  let current = null, epoch = 0, owner = null, reportMonth = '', transactionMonth = '';
  const snapshots = new Map();
  let remittanceDraft = {}, actionCards = { remittance: false, split: false }, remittanceReview = null;
  const en = {
    '我的资产':'My assets','银行卡':'Bank cards','转账':'Transfers','资产月报':'Monthly assets','交易记录':'Transactions',
    '总资产':'Total assets','资产':'Assets','现金':'Cash','净资产':'Net assets','现金余额':'Cash balance','持仓本金':'Investment principal','待结算赎回':'Pending redemption',
    '负债':'Liabilities','可安排资金':'Arrangeable funds','本金口径':'Principal basis','资金口径':'Funds basis',
    '查看全部资产':'All assets','管理卡片':'Manage cards','借记卡':'Debit card','信用卡':'Credit card','银行卡类型待核实':'Card type unverified',
    '尾号':'Ending in','本期待还':'Outstanding balance','账户现金':'Account cash','关联持仓':'Linked investments','关联账户尚未取得':'Linked account unavailable',
    '尚未取得银行卡资料':'Card records unavailable','未关联银行卡的资金账户':'Accounts without a linked debit card',
    '查看持仓':'View investments','查看账户':'View account','查看完整月报':'Full monthly report','全部交易':'All transactions',
    '正在读取月报…':'Loading monthly report…','正在读取交易…':'Loading transactions…','重新读取':'Reload',
    '月末总资产':'Month-end assets','月末净资产':'Month-end net assets','月末现金':'Month-end cash','本月收入':'Monthly income',
    '消费支出':'Expenses','收支结余':'Income less expenses','历史资产估值':'Historical asset valuation',
    '历史估值资料暂未取得':'Historical valuation unavailable','历史估值与当前本金分别记账。':'Historical valuations and current principal are recorded separately.',
    '现金月结':'Cash statement','期初现金':'Opening cash','期末现金':'Closing cash','现金净变动':'Net cash movement',
    '月份':'Month','时间':'Date','交易对象':'Merchant / recipient','交易类型':'Type','金额':'Amount','收入':'Income','消费':'Expense','退款':'Refund',
    '转出':'Transfer out','转入':'Transfer in','转账方向待核实':'Transfer direction unverified','投资买入':'Investment purchase','投资赎回':'Investment redemption',
    '交易对象未取得':'Transaction party unavailable','暂无已记录交易':'No recorded transactions','币种未知':'Currency unknown','未知':'Unknown',
    '定时转账与AA收款':'Scheduled transfers and split payments','查看精确金额':'Exact amount',
    '银行卡仅作为关联入口；多张卡共用账户时，余额不重复计入总资产。':'Cards link to accounts; balances shared by several cards are counted once.',
    '持仓按账户关联，不推断由哪一张卡购买。':'Investments are linked to accounts, without inferring the purchasing card.',
    '现金账户月结资料暂未取得。':'Cash statement unavailable.', '所选月份没有已记录流水，不代表没有发生交易。':'No transactions recorded for this month; this does not imply no activity.',
    '历史合成记录':'Synthetic historical records','截至':'Through','本月':'This month','笔交易':'transactions','有效':'Active','已挂失':'Lost',
    '已过期':'Expired','状态待核实':'Status unverified','报告月份':'Report month','交易月份':'Transaction month',
    '资产与负债按当前账本本金展示，不包含市场估值。':'Current assets and liabilities use ledger principal, excluding market valuation.',
    '可安排资金已扣除应急保留、近期到期款和生效预留，不与总资产相加。':'Arrangeable funds exclude reserves and due payments; they are not added to total assets.'
  };
  const t = value => window.NavrenI18n?.language === 'en' ? (en[value] || window.NavrenI18n?.t?.(value) || value) : value;
  Object.assign(en, {
    '转账汇款':'Transfers and remittances','境内人民币账户汇款':'Domestic CNY account transfers','AA收款':'Split payments','共同支出，分摊收款':'Share expenses and collect payments',
    '展开':'Expand','收起':'Collapse','付款账户':'Payment account','汇款人姓名':'Sender name','联系电话':'Contact phone','收款人姓名':'Recipient name','收款账号':'Recipient account number',
    '开户银行':'Recipient bank','开户支行':'Bank branch','汇款金额（元）':'Amount (CNY)',
    '汇款用途':'Payment purpose','附言（选填）':'Message (optional)','汇款方式':'Transfer method','实时汇款':'Real-time','非实时汇款':'Deferred',
    '普通汇款':'Ordinary transfer','次日汇款':'Next-day transfer','非实时处理方式':'Deferred method','请填写开户姓名':'Enter the registered account name',
    '请填写本人联系电话':'Enter your contact phone','与收款账户开户姓名一致':'As registered with the recipient bank','请输入完整银行卡号或账号':'Enter the full card or account number',
    '输入或选择银行名称':'Enter or select the bank','请输入开户支行全称':'Enter the full branch name',
    '请选择用途':'Select a purpose','生活支出':'Living expenses','家庭往来':'Family payment','货款／服务费':'Goods / services','借款归还':'Loan repayment','其他':'Other',
    '大写':'Chinese uppercase','请输入金额':'Enter an amount','金额格式有误':'Invalid amount','核对汇款信息':'Review remittance','清空':'Clear','返回修改':'Edit details',
    '本地模拟环境 · 银行汇款渠道未接入':'Local simulation · banking transfer service is not connected',
    '处理方式需由银行确认，实际到账时间以收款行为准。':'The bank must confirm processing; arrival depends on the recipient bank.',
    '请填写有效金额，最多两位小数。':'Enter a valid amount with up to two decimal places.','金额必须大于零。':'Amount must be greater than zero.',
    '金额超出可填写范围。':'Amount is outside the supported input range.','账号须为8至32位数字。':'Account numbers must contain 8–32 digits.',
    '请输入有效联系电话。':'Enter a valid contact phone.',
    '当前账户已变化，请重新核对。':'The current account has changed. Review again.','暂无可用人民币付款账户':'No CNY payment account is available',
    '请填写有效信息。':'Enter valid information.',
    '这些信息仅供核对，尚未向银行提交；收款账户未完成银行核验。':'For review only. Nothing has been submitted to a bank, and the recipient account has not been verified.',
    '人民币':'CNY','尚未填写':'Not provided','暂无AA收款账单':'No split-payment bills','参与人':'Participant','应收':'Due','已收':'Received','状态':'Status',
    '发起收款':'Request payment','登记收款':'Record payment','本地模拟收款':'Local simulated collection','只记录本地AA请求与收款，不向联系人发送消息。':'Records local requests and collections without messaging contacts.',
    '待发起':'Draft','待收款':'Awaiting payment','已完成':'Completed','已取消':'Cancelled','已发起':'Requested'
  });
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
  function route(text,view,metadata={},cls='') { const a=current.link(t(text),view,metadata);if(window.NavrenViews?.url)a.href=window.NavrenViews.url({page:'account',view,...metadata});if(cls)a.classList.add(cls);return a; }
  function metric(label,value,currency,cls='') { const box=n('div',null,'ah-stat '+cls);box.append(n('span',label),amount(value,currency,true));return box; }
  function historicalRef(kind,id,extra={}) { return NavrenFinancialData.reference(current.financial,kind,id,extra); }
  function assets() {
    const envelope=current.financial,host=document.getElementById('assetOverview');host.replaceChildren();if(!envelope)return;
    const s=envelope.data.summary,currency='CNY';
    const hero=n('div',null,'ah-total');hero.append(n('span','总资产'),amount(s.total_assets_minor,currency,true),n('small','合成历史估值 · '+envelope.as_of));
    const grid=n('div',null,'ah-stats');for(const [label,key]of [['净资产','net_worth_minor'],['现金余额','cash_minor'],['投资估值','investment_minor'],['负债','liabilities_minor']])grid.append(metric(label,s[key],currency));
    const other=n('div',null,'ah-available');other.append(n('span','其他资产'),amount(s.real_assets_minor,currency,true));host.append(hero,grid,other);
  }
  function cards() {
    const envelope=current.financial,host=document.getElementById('accountCardsPreview');host.replaceChildren();if(!envelope)return;
    const s=envelope.data,status={active:'有效',lost:'已挂失',expired:'已过期',replaced:'已换卡',closed:'已关闭'};
    const active=s.cards.filter(card=>card.status==='active');
    for(const card of active) {
      const account=s.accounts.find(a=>a.account_id===card.account_id),currency=account.currency;
      const face=route('', 'card-detail',historicalRef('card',card.card_id),'ah-bank-card');face.dataset.cardType=card.card_type;
      const head=n('div',null,'ah-card-head');head.append(n('strong',card.card_type==='credit'?'信用卡':'借记卡'),n('span',status[card.status]||'状态待核实','ah-card-status'));
      const identity=n('div',null,'ah-card-identity');identity.append(n('span',NavrenFinancialUI.institution(s,account)),n('span',card.masked_number));face.append(head,identity);
      if(card.card_type==='credit') {const facility=s.credit_facilities.find(item=>item.facility_id===card.facility_id);face.append(metric('本期待还',facility?.outstanding_minor,currency,'ah-card-primary'));}
      else {
        face.append(metric('账户现金',account.balance_minor,currency,'ah-card-primary'));
        const positions=s.positions.filter(position=>position.account_id===account.account_id),linked=n('div',null,'ah-card-holdings');linked.append(n('span','关联持仓'),amount(sum(positions.map(position=>position.book_value_minor)),currency,true));face.append(linked);
        if(positions.length){const list=n('div',null,'ah-card-products');for(const position of positions.slice(0,2)){const row=n('div');row.append(n('span',NavrenFinancialUI.assetName(s,position.asset_id)),n('span',money(position.book_value_minor,currency)));list.append(row);}face.append(list);}
      }
      face.append(n('small',account.name,'ah-card-account'),n('span','↗','ah-card-arrow'));host.append(face);
    }
    if(!active.length)host.append(n('div','未取得有效银行卡记录','ah-card-empty'));
    const unmatched=s.accounts.filter(a=>a.account_type!=='credit_card'&&!active.some(c=>c.card_type==='debit'&&c.account_id===a.account_id));
    if(unmatched.length) {const group=n('div',null,'ah-unlinked');group.append(n('h4','未关联银行卡的资金账户'));for(const a of unmatched){const row=route('','account-detail',historicalRef('account',a.account_id),'ah-unlinked-row');row.append(n('span',a.name),amount(a.balance_minor,a.currency,true),n('span','›'));group.append(row);}host.append(group);}
    if(s.cards.length>active.length)host.append(route('历史卡片','cards',{source_scope:NavrenFinancialData.scope},'ah-text-link'));
    host.append(n('p','银行卡关联所属账户，多卡共享的余额只计一次。','ah-report-basis'));
  }
  function remittanceAmount(value) {
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
  function closeRemittanceReview() { if(remittanceReview){remittanceReview.close();remittanceReview.remove();remittanceReview=null;} }
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
  function reviewRemittance(captured,form,trigger) {
    if(current!==captured||!captured.isCurrent())throw Error(t('当前账户已变化，请重新核对。'));
    if(document.querySelector('dialog[open]'))return;
    const fields=Object.fromEntries(new FormData(form).entries()),value=remittanceAmount(fields.amount),payer=captured.state.accounts.find(a=>a.id===fields.account_id);
    if(!payer||payer.currency!=='CNY'||!value.minor)throw Error(t('当前账户已变化，请重新核对。'));
    const mode=fields.method==='realtime'?t('实时汇款'):t('非实时汇款')+' · '+t(fields.deferred==='next_day'?'次日汇款':'普通汇款');
    const dialog=n('dialog',null,'ah-remittance-dialog');remittanceReview=dialog;
    const title=n('h2','核对汇款信息');title.id='remittanceReviewHeading';dialog.setAttribute('aria-labelledby',title.id);dialog.append(title);
    const banner=n('div',null,'ah-review-amount');banner.append(n('strong',money(value.minor,'CNY')),n('span',value.uppercase));dialog.append(banner);
    const rows=n('dl',null,'ah-review-fields');
    const entries=[['付款账户',payer.name],['汇款人姓名',fields.sender_name],['联系电话',fields.sender_phone],['收款人姓名',fields.recipient_name],['收款账号',fields.recipient_account.replace(/\s/g,'')],['开户银行',fields.bank_name],['开户支行',fields.bank_branch],['汇款方式',mode],['汇款用途',t(fields.purpose)],['附言（选填）',fields.message||t('尚未填写')]];
    for(const [label,text]of entries){const row=n('div');row.append(n('dt',label),n('dd',text));rows.append(row);}dialog.append(rows);
    const notice=n('p','这些信息仅供核对，尚未向银行提交；收款账户未完成银行核验。','ah-form-note');notice.id='remittanceReviewNotice';dialog.setAttribute('aria-describedby',notice.id);dialog.append(notice);
    const edit=n('button','返回修改','ah-action-button is-primary');edit.type='button';edit.onclick=()=>dialog.close();dialog.append(edit);
    dialog.addEventListener('close',()=>{dialog.remove();if(remittanceReview===dialog)remittanceReview=null;if(trigger.isConnected)trigger.focus();},{once:true});
    dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}});
    document.body.append(dialog);dialog.showModal();
  }
  function transfer() {
    const host=document.getElementById('accountTransferForm'),captured=current;if(!host)return;host.replaceChildren();
    const card=document.getElementById('accountRemittanceCard');card.open=actionCards.remittance;card.ontoggle=()=>{actionCards.remittance=card.open;};
    host.append(n('p','以下表单使用当前本地操作账户，与上方独立历史资产分开。','ah-form-note'));
    const form=n('form',null,'ah-remittance-form');form.id='accountRemittanceForm';const controls={};
    const field=(label,name,type,placeholder,required=true)=>{const item=remittanceField(label,name,type,placeholder,required);item.input.required=required;controls[name]=item.input;form.append(item.wrap);return item;};
    const payerWrap=n('label',null,'ah-remittance-field'),payer=n('select');payer.id='remittance-account_id';payer.name='account_id';payer.required=true;controls.account_id=payer;const payerCaption=remittanceCaption('付款账户',true);payerCaption.id=payer.id+'-label';payer.setAttribute('aria-labelledby',payerCaption.id);payerWrap.append(payerCaption,payer);
    if(!captured.state){host.append(n('p','当前操作账户暂不可用，请重新读取操作账户。','ah-form-note'),BankUI.button('重新读取',()=>BankUI.load().catch(()=>{})));return;}
    const accounts=(captured.state.accounts||[]).filter(a=>a.currency==='CNY');
    for(const a of accounts){const option=n('option',a.name+' · '+money(a.balance_minor,a.currency));option.value=a.id;payer.append(option);}
    if(!accounts.length){const option=n('option','暂无可用人民币付款账户');option.value='';payer.append(option);}
    payer.value=accounts.some(a=>a.id===remittanceDraft.account_id)?remittanceDraft.account_id:accounts.find(a=>a.id.endsWith('-main'))?.id||accounts[0]?.id||'';form.append(payerWrap);
    field('汇款人姓名','sender_name','text','请填写开户姓名').input.maxLength=60;
    const phone=field('联系电话','sender_phone','tel','请填写本人联系电话').input;phone.maxLength=24;
    field('收款人姓名','recipient_name','text','与收款账户开户姓名一致').input.maxLength=60;
    const account=field('收款账号','recipient_account','text','请输入完整银行卡号或账号').input;account.inputMode='numeric';account.maxLength=40;
    const bank=field('开户银行','bank_name','text','输入或选择银行名称').input;bank.setAttribute('list','remittanceBanks');
    const banks=n('datalist');banks.id='remittanceBanks';for(const name of ['中国工商银行','中国农业银行','中国银行','中国建设银行','交通银行','中国邮政储蓄银行','招商银行','中信银行','中国光大银行','浦发银行','兴业银行','杭州银行']){const option=n('option');option.value=name;banks.append(option);}form.append(banks);
    field('开户支行','bank_branch','text','请输入开户支行全称');
    const amount=field('汇款金额（元）','amount','text','0.00');amount.input.inputMode='decimal';amount.input.maxLength=22;amount.wrap.classList.add('ah-money-field');
    const shell=n('div',null,'ah-money-shell'),upperWrap=n('div',null,'ah-money-uppercase'),upper=n('output','请输入金额');upper.id='remittanceUppercase';upper.setAttribute('for',amount.input.id);upper.setAttribute('aria-live','polite');upperWrap.append(n('span','大写'),upper);amount.input.setAttribute('aria-describedby',upper.id);shell.append(amount.input,upperWrap);amount.wrap.append(shell);
    const purposeWrap=n('label',null,'ah-remittance-field'),purpose=n('select');purpose.name='purpose';purpose.id='remittance-purpose';purpose.required=true;controls.purpose=purpose;
    for(const value of ['','生活支出','家庭往来','货款／服务费','借款归还','其他']){const option=n('option',value||'请选择用途');option.value=value;purpose.append(option);}purpose.value=remittanceDraft.purpose||'';const purposeCaption=remittanceCaption('汇款用途',true);purposeCaption.id=purpose.id+'-label';purpose.setAttribute('aria-labelledby',purposeCaption.id);purposeWrap.append(purposeCaption,purpose);form.append(purposeWrap);
    const method=n('fieldset',null,'ah-remittance-method');method.append(remittanceCaption('汇款方式',true,'legend'));const choices=n('div',null,'ah-method-choices');
    for(const [value,label]of [['realtime','实时汇款'],['deferred','非实时汇款']]){const wrap=n('label',null,'ah-method-choice'),input=n('input');input.type='radio';input.name='method';input.required=true;input.value=value;input.checked=value===(remittanceDraft.method||'realtime');wrap.append(input,n('span',label));choices.append(wrap);}method.append(choices);
    const delay=n('select');delay.name='deferred';delay.required=true;delay.id='remittance-deferred';delay.setAttribute('aria-label',t('非实时处理方式'));for(const [value,label]of [['ordinary','普通汇款'],['next_day','次日汇款']]){const option=n('option',label);option.value=value;delay.append(option);}delay.value=remittanceDraft.deferred||'ordinary';method.append(delay);form.append(method);
    field('附言（选填）','message','text','',false).input.maxLength=128;
    const hint=n('p','处理方式需由银行确认，实际到账时间以收款行为准。','ah-form-note ah-full-row');form.append(hint);
    const error=n('p','', 'ah-form-error');error.setAttribute('role','alert');
    const footer=n('div',null,'ah-form-footer'),buttons=n('div',null,'ah-form-buttons'),reset=n('button','清空','ah-action-button'),submit=n('button','核对汇款信息','ah-action-button is-primary');reset.type='button';submit.type='submit';submit.disabled=!accounts.length;buttons.append(reset,submit);footer.append(n('span','本地模拟环境 · 银行汇款渠道未接入','ah-form-note'),buttons);form.append(error,footer);host.append(form);
    function updateAmount(cursor=false) {
      const input=amount.input,start=input.selectionStart,before=start==null?null:input.value.slice(0,start).replaceAll(',','').length,result=remittanceAmount(input.value);
      if(!result.error){input.value=result.formatted;if(cursor&&before!==null){let pos=0,count=0;while(pos<input.value.length&&count<before){if(input.value[pos]!==',')count++;pos++;}input.setSelectionRange(pos,pos);}}
      input.setCustomValidity(result.error?t(result.error):result.minor===0?t('金额必须大于零。'):'');upper.textContent=result.error?t('金额格式有误'):result.uppercase||t('请输入金额');
    }
    function updateFields() {
      for(const name of ['sender_name','recipient_name','bank_name','bank_branch'])controls[name].setCustomValidity(controls[name].value&&!controls[name].value.trim()?t('请填写有效信息。'):'');
      const cleanAccount=account.value.replace(/\s/g,'');account.setCustomValidity(cleanAccount&&!/^\d{8,32}$/.test(cleanAccount)?t('账号须为8至32位数字。'):'');
      const cleanPhone=phone.value.replace(/[\s()-]/g,'');phone.setCustomValidity(cleanPhone&&!/^\+?\d{7,15}$/.test(cleanPhone)?t('请输入有效联系电话。'):'');
      const deferred=form.querySelector('[name=method]:checked')?.value==='deferred';delay.hidden=!deferred;delay.disabled=!deferred;
    }
    const save=()=>{updateFields();remittanceDraft=Object.fromEntries(new FormData(form).entries());if(delay.disabled)remittanceDraft.deferred=delay.value;error.textContent='';};
    form.addEventListener('input',e=>{if(e.target===amount.input)updateAmount(true);save();});form.addEventListener('change',save);
    amount.input.addEventListener('blur',()=>{const value=remittanceAmount(amount.input.value);if(value.minor!==null&&!value.error){const minor=BigInt(value.minor);amount.input.value=String(minor/100n)+'.'+String(minor%100n).padStart(2,'0');updateAmount();save();}});
    reset.onclick=()=>{remittanceDraft={};form.reset();for(const input of Object.values(controls))if(input!==payer)input.value='';payer.value=accounts.find(a=>a.id.endsWith('-main'))?.id||accounts[0]?.id||'';form.querySelector('[name=method][value=realtime]').checked=true;delay.value='ordinary';updateAmount();save();delete form.dataset.navrenDirty;amount.input.focus();};
    form.addEventListener('submit',event=>{event.preventDefault();updateAmount();updateFields();if(!form.reportValidity())return;save();try{reviewRemittance(captured,form,submit);}catch(failure){error.textContent=failure.message;}});
    updateAmount();updateFields();
  }
  function splitPayments() {
    const host=document.getElementById('accountSplitForm'),captured=current;if(!host)return;host.replaceChildren();const card=document.getElementById('accountSplitCard');card.open=actionCards.split;card.ontoggle=()=>{actionCards.split=card.open;};
    const status={draft:'待发起',open:'待收款',requested:'已发起',paid:'已完成',completed:'已完成',cancelled:'已取消'};
    const operation=(button,payload)=>{if(current!==captured||!captured.isCurrent())return;button.disabled=true;BankUI.openDraft({...payload,idempotency_key:BankClient.key()},button).catch(error=>BankUI.showError(host,error)).finally(()=>{if(button.isConnected)button.disabled=false;});};
    host.append(n('p','当前本地操作账户 · AA请求与独立历史记录分开。','ah-form-note'));
    if(!captured.state){host.append(n('p','当前操作账户暂不可用。','ah-form-note'));return;}
    for(const split of captured.state.split_bills||[]){
      const item=n('section',null,'ah-aa-bill'),head=n('div',null,'ah-aa-heading'),identity=n('div');identity.append(n('h3',split.title),n('span',status[split.status]||split.status,'ah-status-badge'));head.append(identity,amount(split.total_minor,'CNY'));item.append(head);
      const people=(captured.state.split_participants||[]).filter(person=>person.split_id===split.id),table=n('table',null,'ah-aa-table'),row=n('tr');for(const label of ['参与人','应收','已收','状态'])row.append(n('th',label));const thead=n('thead');thead.append(row);table.append(thead);const tbody=n('tbody');
      for(const person of people){const row=n('tr');row.append(n('td',person.display_name),n('td',money(person.share_minor,'CNY')),n('td',money(person.paid_minor,'CNY')));const action=n('td');action.append(n('span',status[person.status]||person.status));if(split.status!=='draft'&&person.status==='requested'){const button=n('button','登记收款','ah-action-button');button.type='button';button.title=t('本地模拟收款');button.onclick=()=>operation(button,{action:'collect_split_payment',account_id:split.account_id,split_id:split.id,participant_id:person.id});action.append(button);}row.append(action);tbody.append(row);}table.append(tbody);const scroll=n('div',null,'ah-table-scroll');scroll.append(table);item.append(scroll);
      if(split.status==='draft'){const button=n('button','发起收款','ah-action-button is-primary');button.type='button';button.onclick=()=>operation(button,{action:'request_split',account_id:split.account_id,split_id:split.id});const footer=n('div',null,'ah-aa-footer');footer.append(button);item.append(footer);}host.append(item);
    }
    if(!(captured.state.split_bills||[]).length)host.append(n('p','暂无AA收款账单','ah-muted'));
    host.append(n('p','只记录本地AA请求与收款，不向联系人发送消息。','ah-form-note'));
  }
  function previousMonth(date) { const match=/^(\d{4})-(\d{2})-\d{2}$/.exec(date||'');if(!match)return '';let year=Number(match[1]),month=Number(match[2])-1;if(month===0){month=12;year--;}return year+'-'+String(month).padStart(2,'0'); }
  function configureMonths() {
    const envelope=current.financial;
    reportMonth=reportMonth||previousMonth(envelope.as_of);transactionMonth=transactionMonth||envelope.as_of.slice(0,7);
    for(const [id,value,callback]of [['accountReportMonth',reportMonth,v=>{reportMonth=v;renderMonthly();}],['accountTransactionMonth',transactionMonth,v=>{transactionMonth=v;renderTransactions();}]]){
      const input=document.getElementById(id);input.value=value;input.max=envelope.as_of.slice(0,7);input.min=envelope.evidence?.coverage_start?.slice(0,7)||'2016-10';
      input.onchange=()=>{if(/^\d{4}-(?:0[1-9]|1[0-2])$/.test(input.value)&&input.value<=input.max&&input.value>=input.min)callback(input.value);};
    }
  }
  function alive(token,captured) { return token===epoch && current===captured && captured.isCurrent() && !document.getElementById('accountHome').hidden; }
  function loadError(host,error,retry) {host.replaceChildren(n('p',error.message,'ah-muted'));const button=n('button','重新读取','ah-small-button');button.type='button';button.onclick=retry;host.append(button);}
  function chart(rows) {
    if(rows.length<2)return null;
    const fig=n('figure',null,'ah-history-chart');fig.append(n('figcaption','历史资产估值 · 每周及期末快照'));
    const values=rows.map(r=>r.total_assets_minor),low=Math.min(...values),high=Math.max(...values),range=high-low||1;
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 680 128');svg.setAttribute('role','img');svg.setAttribute('aria-label',t('历史资产估值')+' '+rows[0].date+' — '+rows.at(-1).date);svg.setAttribute('preserveAspectRatio','none');
    const title=document.createElementNS(svg.namespaceURI,'title');title.textContent=rows.map(r=>r.date+': '+money(r.total_assets_minor,'CNY')).join('；');svg.append(title);
    const poly=document.createElementNS(svg.namespaceURI,'polyline');poly.setAttribute('points',values.map((v,i)=>(12+i*656/(values.length-1)).toFixed(2)+','+(108-(v-low)/range*84).toFixed(2)).join(' '));poly.setAttribute('fill','none');poly.setAttribute('stroke','currentColor');poly.setAttribute('stroke-width','2.5');poly.setAttribute('vector-effect','non-scaling-stroke');svg.append(poly);fig.append(svg);
    const axis=n('div',null,'ah-chart-axis');axis.append(n('span',rows[0].date),n('span',rows.at(-1).date));fig.append(axis);return fig;
  }
  async function renderMonthly(fresh=false) {
    const host=document.getElementById('accountMonthlyReport'),captured=current,token=epoch,month=reportMonth;if(!month||!captured?.financial)return;host.replaceChildren(n('p','正在读取月报…','ah-muted'));
    try {
      const envelope=await NavrenFinancialData.month(month,null,{fresh}),report=envelope.data;if(!alive(token,captured)||month!==reportMonth)return;
      const closing=report.closing;let curveError=null,rows=[];try{rows=await NavrenFinancialData.assetCurve(envelope,{fresh});}catch(error){curveError=error;}if(!alive(token,captured)||month!==reportMonth)return;host.replaceChildren();
      const grid=n('div',null,'ah-month-stats');for(const [label,key]of [['期末总资产','total_assets_minor'],['期末净资产','net_worth_minor'],['期末现金','cash_minor']])grid.append(metric(label,closing?.[key],'CNY'));host.append(grid);
      const fig=chart(rows);if(fig)host.append(fig);else {host.append(n('p',curveError?.message||'所选期间的资产曲线资料未取得。','ah-report-basis'));if(curveError)host.append(BankUI.button('重新读取资产曲线',()=>renderMonthly(true)));}
      const cash=n('div',null,'ah-cash-month');cash.append(n('h3','现金月结'));const totals=n('div',null,'ah-flow-stats');for(const [label,key]of [['本月收入','income_minor'],['消费支出','expense_minor'],['退款','refund_minor'],['现金净变动','net_cash_change_minor']])totals.append(metric(label,report.totals?.[key],'CNY'));cash.append(totals);
      const endpoints=n('div',null,'ah-cash-endpoints');endpoints.append(metric('期初现金',report.opening?.cash_minor,'CNY'),metric('期末现金',closing?.cash_minor,'CNY'));cash.append(endpoints,route('查看完整月报','statements',{source_scope:NavrenFinancialData.scope,period:month},'ah-text-link'));host.append(cash,NavrenFinancialUI.basis(envelope));if(report.period?.coverage_partial)host.append(n('p','所选月份仅展示已覆盖期间，期末金额不是完整月底金额。','ah-report-basis'));
    }catch(error){if(alive(token,captured)&&month===reportMonth)loadError(host,error,()=>renderMonthly(true));}
  }
  async function renderTransactions(fresh=false) {
    const host=document.getElementById('accountRecentTransactions'),captured=current,token=epoch,month=transactionMonth;if(!month||!captured?.financial)return;host.replaceChildren(n('p','正在读取交易…','ah-muted'));
    try {
      const envelope=await NavrenFinancialData.latest(month,8,{fresh}),rows=envelope.data.items;if(!alive(token,captured)||month!==transactionMonth)return;host.replaceChildren();
      if(!rows.length)host.append(n('p','所选月份没有已记录流水，不代表没有发生交易。','ah-muted'));else NavrenFinancialUI.transactionTable(host,envelope,rows,captured.financial.data);
      const foot=n('div',null,'ah-transactions-footer');foot.append(n('span',envelope.data.total+' '+t('笔交易')),route('全部交易','statements',{source_scope:NavrenFinancialData.scope,period:month},'ah-text-link'));host.append(foot);
    }catch(error){if(alive(token,captured)&&month===transactionMonth)loadError(host,error,()=>renderTransactions(true));}
  }
  function clear() {epoch++;current=null;owner=null;reportMonth=transactionMonth='';remittanceDraft={};actionCards={remittance:false,split:false};closeRemittanceReview();snapshots.clear();for(const id of ['accountRemittanceCard','accountSplitCard']){const card=document.getElementById(id);if(card)card.open=false;}for(const id of ['assetOverview','accountCardsPreview','accountMonthlyReport','accountRecentTransactions','accountTransferForm','accountSplitForm'])document.getElementById(id)?.replaceChildren();}
  async function render(options) {
    if(options.state&&options.state.workspace_id!==BankClient.identity()?.workspace_id)options={...options,state:null};
    const identity=BankClient.identity()?.workspace_id||options.state?.workspace_id;
    if(owner!==identity){snapshots.clear();closeRemittanceReview();reportMonth=transactionMonth='';remittanceDraft={};actionCards={remittance:false,split:false};owner=identity;}
    current=options;const token=++epoch;snapshots.clear();transfer();splitPayments();
    for(const id of ['assetOverview','accountCardsPreview','accountMonthlyReport','accountRecentTransactions'])document.getElementById(id)?.replaceChildren(n('p','正在读取独立财务资料…','ah-muted'));
    document.querySelectorAll('#accountHome [data-ah-text]').forEach(el=>el.textContent=t(el.dataset.ahText));
    try{const envelope=await NavrenFinancialData.overview();if(!alive(token,options))return;current.financial=envelope;assets();cards();configureMonths();renderMonthly();renderTransactions();}
    catch(error){if(alive(token,options))for(const id of ['assetOverview','accountCardsPreview','accountMonthlyReport','accountRecentTransactions'])loadError(document.getElementById(id),error,()=>{NavrenFinancialData.clear();render(options);});}
  }
  window.addEventListener('navren:privacy-reset',clear);
  return {render,refreshAssets(){if(current){assets();cards();}},clear,remittanceAmount};
})();
