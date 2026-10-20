/* 付款银行卡选择器的隔离 DOM 验收；仅假身份/假接入，不提交银行操作。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),{JSDOM}=require('jsdom');
const source=fs.readFileSync(path.resolve(__dirname,'../../js/account_home.js'),'utf8');
const sourceHash=crypto.createHash('sha256').update(source).digest('hex');
const clone=value=>JSON.parse(JSON.stringify(value));
let assertions=0,scenarios=0,reads=0,writes=0,network=0;
function check(value,label){assert.ok(value,label);assertions++;}
function equal(actual,expected,label){assert.deepEqual(actual,expected,label);assertions++;}
const settle=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setTimeout(resolve,0));};

function fixture(){
  const account=(id,type,connection,balance=100000)=>({account_id:id,id,name:id+'结算账户',account_type:type,connection_id:connection,currency:'CNY',status:'active',balance_minor:balance});
  const accounts=[account('acct-main','bank_settlement','bank-main'),account('acct-other','bank_settlement','bank-other',250000),account('acct-wallet','payment_wallet','wallet'),account('acct-broker','securities_cash','broker'),account('acct-credit','credit_card','bank-main',-50000),account('acct-unknown',null,'bank-main'),account('acct-savings','savings','bank-main'),account('acct-closed','bank_settlement','bank-main'),account('acct-noexecution','bank_settlement','bank-main')];
  accounts.find(a=>a.account_id==='acct-closed').status='closed';
  const cards=[],raw=[];
  function add(id,accountId,tail,extra={},rawExtra={}){
    cards.push({card_id:id,account_id:accountId,card_type:'debit',masked_number:'**** **** **** '+tail,status:'active',issued_on:'2021-10-08',expires_on:'2031-10-08',role:'primary',...extra});
    raw.push({id,account_id:accountId,type:'debit',status:'active',last_four:tail,issued_on:'2021-10-08',expires_on:'2031-10-08',workspace_id:'ws-remittance-fixture',...rawExtra});
  }
  add('card-main','acct-main','2001');add('card-main-second','acct-main','2002');add('card-other','acct-other','2003');
  add('card-wallet','acct-wallet','3001');add('card-broker','acct-broker','3002');add('card-credit','acct-credit','3003',{card_type:'credit',facility_id:'credit-facility'},{type:'credit'});
  add('card-unknown','acct-unknown','3004');add('card-savings','acct-savings','3005');add('card-closed','acct-closed','3006');add('card-noexecution','acct-noexecution','3007');
  add('card-lost','acct-main','3010',{status:'lost'},{status:'lost'});add('card-expired','acct-main','3011',{expires_on:'2026-10-08'},{expires_on:'2026-10-08'});
  add('card-raw-lost','acct-main','3012',{}, {status:'lost'});add('card-raw-credit','acct-main','3013',{}, {type:'credit'});
  add('card-bad-tail','acct-main','3014',{}, {last_four:'9999'});add('card-bad-account','acct-main','3015',{}, {account_id:'acct-other'});
  add('card-no-raw','acct-main','3016');raw.splice(raw.findIndex(c=>c.id==='card-no-raw'),1);
  add('card-unknown-type','acct-main','3017',{card_type:null});add('card-unknown-status','acct-main','3018',{status:null});
  add('card-no-expiry','acct-main','3019',{expires_on:null},{expires_on:null});add('card-invalid-expiry','acct-main','3020',{expires_on:'2031-02-30'},{expires_on:'2031-02-30'});
  add('card-expiry-disagrees','acct-main','3021',{}, {expires_on:'2032-10-08'});add('card-raw-no-expiry','acct-main','3022',{}, {expires_on:null});
  const state={environment:'simulation',workspace_id:'ws-remittance-fixture',as_of:'2026-10-09',version:7,accounts:accounts.filter(a=>a.account_id!=='acct-noexecution').map(a=>({id:a.account_id,name:a.name,currency:a.currency,balance_minor:Math.max(0,a.balance_minor)})),cards:raw,split_bills:[],split_participants:[],profile:{reserve_minor:0},holdings:[],operations:[]};
  state.accounts.push({id:'acct-unlinked',name:'没有接入关联的执行账户',currency:'CNY',balance_minor:777777});
  const connected={schema_version:'navren.connected-assets.v1',source:'navren_simulated_bank',environment:'simulation',read_only:true,runtime_ledger_included:true,workspace_id:state.workspace_id,workspace_binding:state.workspace_id,subject_id:'remittance-owner',current_account_id:'remittance-owner',privacy_revision:42,version:7,dataset_version:'connected:7:cards',as_of:state.as_of,observed_at:'2026-10-09T04:00:00Z',valuation_at:'2026-10-09T04:00:00Z',data:{view:'overview',accounts,cards,connections:[{connection_id:'bank-main',institution_name:'中国工商银行',institution_type:'bank',status:'connected'},{connection_id:'bank-other',institution_name:'中国建设银行',institution_type:'bank',status:'connected'},{connection_id:'wallet',institution_name:'支付宝',institution_type:'payment',status:'connected'},{connection_id:'broker',institution_name:'中信证券',institution_type:'securities',status:'connected'}],summary:{total_assets_minor:1050000,net_worth_minor:1000000,cash_minor:1050000,investment_minor:0,real_assets_minor:0,liabilities_minor:50000,pending_redemption_minor:0},assets:[],positions:[],contracts:[],credit_facilities:[{facility_id:'credit-facility',account_id:'acct-credit',outstanding_minor:50000,limit_minor:100000}],policies:[],obligations:[]}};
  return {state,connected};
}

function harness(data=fixture()){
  const dom=new JSDOM('<!doctype html><main><div id="accountHome"><div id="assetOverview"></div><div id="accountCardsPreview"></div><div id="accountAssetsSupplement"></div><details id="accountRemittanceCard" open><summary tabindex="0">转账汇款</summary><div id="accountTransferForm"></div></details><details id="accountSplitCard"><summary>AA收款</summary><div id="accountSplitForm"></div></details><input id="accountReportMonth" type="month"><div id="accountMonthlyReport"></div><input id="accountTransactionMonth" type="month"><div id="accountRecentTransactions"></div></div><button id="outside">其他按钮</button></main>',{url:'http://fixture/account.html',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window;let identity={workspace_id:data.state.workspace_id,account_id:'remittance-owner',privacy_revision:42},identityEpoch=0,connectedDelay=null,failConnected=false,outsideActions=0;
  const node=(tag,text,cls)=>{const n=w.document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  w.fetch=()=>{network++;throw Error('禁止真实网络');};
  w.BankClient={identity:()=>({...identity}),ensure:async()=>({...identity}),guard(){const captured=identityEpoch;return {valid:()=>captured===identityEpoch};},money:minor=>(minor/100).toFixed(2)+' 元',get:()=>{network++;throw Error('未授权接口');}};
  for(const method of ['post','put','patch','delete'])w.BankClient[method]=()=>{writes++;throw Error('禁止金融写入');};
  w.BankUI={node,button:(text,fn)=>{const b=node('button',text);b.type='button';b.onclick=fn;return b;},formatMoney:(minor,currency)=>currency==='CNY'?(minor/100).toFixed(2)+' 元':'未知',load:async()=>{reads++;return {state:data.state,products:[]};},openDraft:()=>{writes++;throw Error('禁止金融草案');}};
  w.NavrenI18n={language:'zh-CN',t:text=>text};
  w.NavrenViews={current:()=>({view:'home'}),update:()=>{},url:meta=>'account.html?'+new URLSearchParams(Object.entries(meta).filter(([,v])=>typeof v!=='object'))};
  const institution=(records,account)=>records?.connections.find(c=>c.connection_id===account?.connection_id)?.institution_name||'关联机构未知';
  w.NavrenFinancialUI={institution,basis:()=>node('p','历史资料只读'),transactionTable:host=>host.append(node('table'))};
  w.NavrenConnectedAssetsUI={institution,paymentPlatforms:[{id:'alipay',name:'支付宝'},{id:'wechat',name:'微信支付'}]};
  w.NavrenFinancialData={scope:'financial_history',overview:async()=>{reads++;throw Error('本测试未载入历史月报');}};
  w.NavrenConnectedAssets={scope:'connected_bank',overview:async()=>{reads++;const pause=connectedDelay;connectedDelay=null;if(pause)await pause;if(failConnected)throw Error('接入资料未取得');return data.connected;},reference:(envelope,kind,id,extra={})=>({source_scope:'connected_bank',object_version:envelope.dataset_version,object_kind:kind,object_id:id,...extra}),transactions:async()=>{reads++;return {...data.connected,data:{items:[],total:0,has_more:false}};}};
  w.matchMedia=()=>({matches:true});
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
  w.document.getElementById('outside').onclick=()=>outsideActions++;
  w.eval(source);
  const H=w.NavrenAccountHome;
  const trigger=()=>w.document.querySelector('.ah-payer-trigger'),input=()=>w.document.getElementById('remittance-account_id'),menu=()=>w.document.querySelector('.ah-payer-menu'),options=()=>[...w.document.querySelectorAll('.ah-payer-option')],form=()=>w.document.getElementById('accountRemittanceForm');
  async function render(){const epoch=identityEpoch;await H.render({state:data.state,products:[],isCurrent:()=>identityEpoch===epoch,link:(text,view,meta)=>{const a=node('a',text);a.href=w.NavrenViews.url({view,...meta});return a;}});await settle();return menu();}
  function open(){w.document.getElementById('accountRemittanceCard').open=true;trigger().click();return menu();}
  function key(target,key,shiftKey=false){target.dispatchEvent(new w.KeyboardEvent('keydown',{key,shiftKey,bubbles:true,cancelable:true}));}
  function choose(id){open();const option=options().find(o=>o.dataset.cardId===id);assert.ok(option,'候选卡存在 '+id);option.click();}
  function fill(){for(const [name,value]of Object.entries({sender_name:'汇款人',sender_phone:'13800138000',recipient_name:'收款人',recipient_account:'6222000012345678',bank_name:'中国银行',bank_branch:'杭州支行',amount:'123.45',purpose:'生活支出'})){const control=form().elements.namedItem(name);control.value=value;control.dispatchEvent(new w.Event('input',{bubbles:true}));}}
  function submit(){form().dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));}
  return {w,H,data,render,trigger,input,menu,options,form,open,key,choose,fill,submit,close(){H.clear();dom.window.close();},pause(){let release;connectedDelay=new Promise(resolve=>release=resolve);return release;},invalidate(){identityEpoch++;identity={...identity,privacy_revision:43};w.dispatchEvent(new w.Event('navren:privacy-reset'));},fail(){failConnected=true;},get outsideActions(){return outsideActions;}};
}

(async()=>{
  let h;
  try{
    scenarios++;h=harness();await h.render();
    equal(h.options().map(o=>o.dataset.cardId),['card-main','card-main-second','card-other'],'严格候选交集只留下三张有效银行借记卡');
    equal(h.input().type,'hidden','表单账户字段保持隐藏执行ID');equal(h.input().name,'account_id','表单仍提交执行账户字段');equal(h.input().value,'acct-main','默认卡关联确切运行账户');
    equal(h.w.document.querySelector('#remittance-account_id select'),null,'隐藏账户中没有旧原生选择器');
    equal(h.trigger().getAttribute('aria-haspopup'),'menu','触发器说明菜单类型');equal(h.menu().getAttribute('role'),'menu','菜单提供正确角色');
    check(h.options().every(o=>o.getAttribute('role')==='menuitemradio'),'卡片均为单选菜单项');
    check(h.trigger().textContent.includes('中国工商银行')&&h.trigger().textContent.includes('2001'),'默认卡展示银行与准确尾号');
    check(!h.menu().textContent.includes('支付宝')&&!h.menu().textContent.includes('中信证券'),'钱包和证券没有伪装成银行卡');
    h.choose('card-other');equal(h.input().value,'acct-other','选卡映射执行账户');check(h.trigger().textContent.includes('中国建设银行')&&h.trigger().textContent.includes('2003'),'选中卡身份更新');
    equal(h.options().filter(o=>o.getAttribute('aria-checked')==='true').map(o=>o.dataset.cardId),['card-other'],'选中标记准确且唯一');
    check(h.menu().hidden,'选择后关闭菜单');equal(h.w.document.activeElement,h.trigger(),'选择后焦点回到触发器');
    equal(new h.w.FormData(h.form()).get('account_id'),'acct-other','FormData携带运行账户而非卡片ID');equal(new h.w.FormData(h.form()).get('card_id'),null,'卡片ID只保留在选择器内存');
    h.choose('card-main-second');equal(h.input().value,'acct-main','同账户另一张卡仍映射同一执行ID');check(h.trigger().textContent.includes('2002'),'同账户多卡身份不串');
    h.fill();h.submit();const review=h.w.document.querySelector('dialog[open]');check(review,'完整资料可以进入只读核对');check(review.textContent.includes('中国工商银行')&&review.textContent.includes('2002'),'核对展示实际选中卡');
    check(review.textContent.includes('尚未向银行提交'),'核对仍明确未提交');review.close();
    h.input().value='acct-wallet';h.submit();check(!h.w.document.querySelector('dialog[open]'),'篡改隐藏账户为钱包不能核对');check(h.form().textContent.includes('请选择有效的付款银行卡'),'篡改账户有明确拒绝');
    h.input().value='acct-main';h.data.state.cards.find(c=>c.id==='card-main-second').status='lost';h.submit();check(!h.w.document.querySelector('dialog[open]'),'核对时重读候选拒绝刚挂失的卡');
    h.data.state.cards.find(c=>c.id==='card-main-second').status='active';h.submit();check(h.w.document.querySelector('dialog[open]'),'恢复有效状态后可重新核对');h.w.document.querySelector('dialog[open]').close();
    h.open();h.data.state.cards.find(c=>c.id==='card-other').status='lost';h.options().find(o=>o.dataset.cardId==='card-other').click();equal(h.input().value,'acct-main','点击时重核候选拒绝刚挂失的另一张卡');check(h.trigger().textContent.includes('2002'),'无效旧候选不能替换当前卡身份');
    h.close();h=null;

    scenarios++;h=harness();await h.render();h.open();equal(h.w.document.activeElement,h.options()[0],'打开菜单焦点进入所选卡');
    h.key(h.menu(),'ArrowDown');equal(h.w.document.activeElement,h.options()[1],'向下到下一张卡');h.key(h.menu(),'ArrowUp');equal(h.w.document.activeElement,h.options()[0],'向上回上一张');
    h.key(h.menu(),'ArrowUp');equal(h.w.document.activeElement,h.options().at(-1),'向上环绕末项');h.key(h.menu(),'Home');equal(h.w.document.activeElement,h.options()[0],'Home到首项');h.key(h.menu(),'End');equal(h.w.document.activeElement,h.options().at(-1),'End到末项');
    h.key(h.menu(),'Escape');check(h.menu().hidden,'Esc关闭');equal(h.w.document.activeElement,h.trigger(),'Esc还原焦点');equal(h.trigger().getAttribute('aria-expanded'),'false','关闭状态准确');
    h.key(h.trigger(),'ArrowDown');check(!h.menu().hidden,'触发器向下键可以打开');h.key(h.menu(),'Tab');check(h.menu().hidden,'Tab关闭');equal(h.w.document.activeElement,h.w.document.getElementById('remittance-sender_name'),'Tab到下一个表单控件');
    h.open();h.key(h.menu(),'Tab',true);check(h.menu().hidden,'Shift Tab关闭');equal(h.w.document.activeElement,h.w.document.querySelector('#accountRemittanceCard>summary'),'Shift Tab回汇款入口');
    h.open();h.menu().querySelector('.ah-payer-menu-title').click();check(!h.menu().hidden,'菜单内部非选项点击不误关');
    const outside=h.w.document.getElementById('outside');outside.dispatchEvent(new h.w.MouseEvent('pointerdown',{bubbles:true}));check(h.menu().hidden,'外部pointerdown关闭');outside.click();equal(h.outsideActions,1,'外部按钮正常动作');
    h.open();outside.click();check(h.menu().hidden,'外部click也关闭');equal(h.outsideActions,2,'外部click不被吞');
    h.open();outside.focus();check(h.menu().hidden,'焦点离开关闭');equal(h.w.document.activeElement,outside,'外部焦点不抢回');
    h.open();h.w.dispatchEvent(new h.w.Event('resize'));check(h.menu().hidden,'窗口尺寸变化关闭');h.open();h.w.document.dispatchEvent(new h.w.Event('scroll'));check(h.menu().hidden,'页面滚动关闭');
    h.open();h.menu().dispatchEvent(new h.w.Event('scroll',{bubbles:true}));check(!h.menu().hidden,'菜单自身滚动不误关');h.key(h.menu(),'Escape');h.close();h=null;

    scenarios++;h=harness();const release=h.pause();const pending=h.render();await settle();check(h.trigger().disabled,'接入响应尚未取得时不能选择旧执行账户');equal(h.input().value,'','未知关联不会回退全CNY');equal(h.options().length,0,'未知关联不虚构银行卡');release();await pending;
    check(!h.trigger().disabled,'接入响应取得后解除禁用');h.choose('card-other');h.fill();const oldMenu=h.menu(),oldOption=h.options()[0];await h.render();check(!oldMenu.isConnected,'重渲染销毁body旧菜单');equal(h.w.document.querySelectorAll('.ah-payer-menu').length,1,'同轮仅保留一个新菜单');
    equal(h.input().value,'acct-other','同身份重渲染保留付款选择');check(h.trigger().textContent.includes('2003'),'重渲染保留卡片身份');equal(h.form().elements.namedItem('sender_name').value,'汇款人','同身份刷新保留填写资料');
    oldOption.click();equal(h.input().value,'acct-other','旧脱离菜单不能改新表单');h.open();h.w.dispatchEvent(new h.w.Event('pagehide'));equal(h.w.document.querySelectorAll('.ah-payer-menu').length,0,'pagehide销毁body菜单');h.close();h=null;

    scenarios++;for(const [label,mutate]of [
      ['版本不一致',f=>f.connected.version=8],['工作区不一致',f=>f.connected.workspace_id='other'],['银行类型缺失',f=>delete f.connected.data.connections.find(c=>c.connection_id==='bank-main').institution_type],
      ['银行标成支付机构',f=>f.connected.data.connections.find(c=>c.connection_id==='bank-main').institution_type='payment'],['机构状态未知',f=>delete f.connected.data.connections.find(c=>c.connection_id==='bank-main').status],
      ['执行币种未知',f=>delete f.state.accounts.find(a=>a.id==='acct-main').currency],['关联币种不是人民币',f=>f.connected.data.accounts.find(a=>a.account_id==='acct-main').currency='USD'],
      ['未知业务日期',f=>f.state.as_of='2026-02-30'],['原始卡币种关系不存在',f=>f.state.cards=[]]
    ]){const f=fixture();mutate(f);h=harness(f);await h.render();check(!h.options().some(o=>['card-main','card-main-second'].includes(o.dataset.cardId)),label+'时主账户卡不得入选');h.close();h=null;}
    for(const [label,asOf,expiry,eligible]of [['较晚运行日使卡过期','2031-10-09','2031-10-08',false],['较晚运行日与卡有效期相等','2031-10-09','2031-10-09',true]]){const f=fixture();f.state.as_of=asOf;for(const c of f.connected.data.cards.filter(c=>c.card_id==='card-main'))c.expires_on=expiry;for(const c of f.state.cards.filter(c=>c.id==='card-main'))c.expires_on=expiry;h=harness(f);await h.render();equal(h.options().some(o=>o.dataset.cardId==='card-main'),eligible,label);h.close();h=null;}
    h=harness();h.fail();await h.render();check(h.trigger().disabled,'接入失败禁止付款选择');equal(h.input().value,'','接入失败不回退旧余额账户');check(!h.options().length,'接入失败不沿用旧候选');h.close();h=null;

    scenarios++;h=harness();await h.render();h.open();const oldOption2=h.options().find(o=>o.dataset.cardId==='card-other'),oldTrigger=h.trigger(),oldForm=h.form(),oldInput=h.input();h.invalidate();equal(h.w.document.querySelectorAll('.ah-payer-menu').length,0,'隐私失效立即清除菜单');equal(h.w.document.getElementById('accountTransferForm').textContent,'','隐私失效立即清除表单');
    const prior=oldInput.value;oldOption2.click();oldTrigger.click();oldForm.dispatchEvent(new h.w.Event('submit',{bubbles:true,cancelable:true}));equal(oldInput.value,prior,'隐私失效后旧控件不能改隐藏账户');check(!h.w.document.querySelector('dialog[open]'),'隐私失效后旧表单不能核对');equal(h.w.document.querySelectorAll('.ah-payer-menu').length,0,'隐私失效后旧控件不复活菜单');
    const releaseLate=h.pause(),late=h.render();await settle();h.invalidate();releaseLate();await late;equal(h.w.document.getElementById('accountTransferForm').textContent,'','迟到接入不能恢复隐私失效表单');equal(h.w.document.querySelectorAll('.ah-payer-menu').length,0,'迟到接入不能重建菜单');h.close();h=null;

    equal(writes,0,'所有测试0金融写入或草案');equal(network,0,'所有测试0真实网络');
    console.log(JSON.stringify({passed:true,scenarios,assertions,fake_reads:reads,financial_writes:writes,network_requests:network,source_sha256:sourceHash,evidence:'付款银行卡隔离假数据与DOM；未HTTP、浏览器或金融提交'}));
  }catch(error){console.error(error);process.exitCode=1;}finally{h?.close();}
})();
