/* 交易筛选、分页和菜单的隔离假响应检查；禁网且不接触现役银行库。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<!doctype html><main><div id="detail"></div><button id="outside">其他控件</button></main>',{url:'http://fixture/account.html',runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window;
const clone=value=>JSON.parse(JSON.stringify(value));
let assertions=0;
function check(value,label){assert.ok(value,label);assertions++;}
function equal(actual,expected,label){assert.deepEqual(actual,expected,label);assertions++;}
async function rejects(action,pattern,label){await assert.rejects(action,pattern,label);assertions++;}
function throws(action,pattern,label){assert.throws(action,pattern,label);assertions++;}
let identity={workspace_id:'ws-statement-fixture',account_id:'statement-owner',privacy_revision:42},identityEpoch=0,mutatePage=null,nextTransactionDelay=null;
const calls=[],writes=[],updates=[],networkAttempts=[];
const canonical=['income','expense','refund','internal_transfer','investment_purchase','investment_redemption','loan_repayment','insurance_premium','transfer'];
const envelope={schema_version:'navren.connected-assets.v1',source:'navren_simulated_bank',environment:'simulation',read_only:true,runtime_ledger_included:true,workspace_id:identity.workspace_id,workspace_binding:identity.workspace_id,subject_id:identity.account_id,current_account_id:identity.account_id,privacy_revision:42,version:7,dataset_version:'connected:7:statement-fixture',as_of:'2026-10-09',observed_at:'2026-10-09T12:00:00Z',valuation_at:'2026-10-09T11:59:00Z',data:{view:'overview',summary:{cash_minor:150000,investment_minor:0,real_assets_minor:0,liabilities_minor:0,loan_principal_minor:0,credit_card_outstanding_minor:0,pending_redemption_minor:0,total_assets_minor:150000,net_worth_minor:150000},connections:[{connection_id:'icbc',institution_name:'中国工商银行'}],accounts:[{account_id:'acct-main',name:'个人结算账户',account_type:'bank_settlement',connection_id:'icbc',balance_minor:100000,currency:'CNY',status:'active'},{account_id:'acct-other',name:'另一资金账户',account_type:'bank_settlement',connection_id:'icbc',balance_minor:50000,currency:'CNY',status:'active'}],cards:[],positions:[],assets:[],contracts:[],credit_facilities:[],policies:[],obligations:[]}};
const rows=[];
function add(kind,count,month='2026-10',account_id='acct-main'){
  for(let i=0;i<count;i++){const index=rows.length;rows.push({transaction_id:'tx-'+index,account_id,occurred_at:month+'-07T10:'+String(index%60).padStart(2,'0')+':00+08:00',signed_minor:kind==='income'?100:-100,currency:'CNY',counterparty:'商户'+index,kind});}
}
add('expense',60);add('income',23);add('refund',5);add('internal_transfer',2);add('transfer',2);add('external_transfer',2);add('investment_purchase',2);add('investment_redemption',2);add('loan_repayment',2);add('insurance_premium',2);
add('insurance_premium',1,'2026-10','acct-other');add('expense',1,'2026-09');add('income',1,'2026-09');
const normalize=kind=>kind==='external_transfer'?'transfer':kind;
w.BankClient={ensure:async()=>({...identity}),identity:()=>({...identity}),guard(){const before={...identity},epoch=identityEpoch;return {valid(response){return epoch===identityEpoch&&before.workspace_id===identity.workspace_id&&before.account_id===identity.account_id&&before.privacy_revision===identity.privacy_revision&&(!response||response.workspace_id===before.workspace_id&&response.privacy_revision===before.privacy_revision);}};},get:async request=>{
  calls.push(request);const url=new URL(request,'http://fixture');
  if(url.pathname==='/connected-assets')return clone(envelope);
  assert.equal(url.pathname,'/connected-assets/transactions','只允许假交易读取');
  const pause=nextTransactionDelay;nextTransactionDelay=null;if(pause)await pause;
  const limit=Number(url.searchParams.get('limit')),offset=Number(url.searchParams.get('offset')),account_id=url.searchParams.get('account_id'),month=url.searchParams.get('month'),kind=url.searchParams.get('kind');
  const wholeMonth=rows.filter(row=>(!account_id||row.account_id===account_id)&&row.occurred_at.slice(0,7)===month),filtered=wholeMonth.filter(row=>!kind||normalize(row.kind)===kind);
  const page={...clone(envelope),data:{items:clone(filtered.slice(offset,offset+limit)),total:filtered.length,offset,limit,month,account_id,kind,available_kinds:canonical.filter(value=>wholeMonth.some(row=>normalize(row.kind)===value)),has_more:offset+limit<filtered.length}};
  if(mutatePage)mutatePage(page);return page;
}};
for(const method of ['post','put','patch','delete'])w.BankClient[method]=(...args)=>{writes.push({method,args});throw Error('只读测试不得提交银行动作');};
w.fetch=(...args)=>{networkAttempts.push(args);throw Error('隔离测试禁止网络');};
w.NavrenViews={url:metadata=>'account.html?'+new URLSearchParams(metadata),open:()=>{throw Error('交易控件不应导航到其他视图');},update:metadata=>updates.push(clone(metadata))};
w.NavrenFinancialData={overview:async()=>({evidence:{coverage_start:'2026-09-01'}})};
w.NavrenI18n={language:'zh-CN',t:value=>value};
w.eval(fs.readFileSync(path.resolve(__dirname,'../../js/connected_assets.js'),'utf8'));
const F=w.NavrenConnectedAssets,UI=w.NavrenConnectedAssetsUI,host=w.document.getElementById('detail');
const settle=async()=>{for(let i=0;i<6;i++)await new Promise(resolve=>setTimeout(resolve,0));};
const transactionCalls=()=>calls.filter(request=>request.startsWith('/connected-assets/transactions?'));
const lastQuery=()=>new URL(transactionCalls().at(-1),'http://fixture').searchParams;
const typeButton=()=>host.querySelector('thead .cd-type-filter');
const sizeButton=()=>host.querySelector('.cd-page-size .cd-choice-button');
const openMenu=()=>w.document.querySelector('.cd-choice-menu:not([hidden])');
const menuValues=menu=>[...menu.querySelectorAll('[role=menuitemradio]')].map(option=>option.dataset.value);
const pageButton=label=>[...host.querySelectorAll('.cd-table-footer button')].find(button=>button.textContent===label);
const bodyKinds=()=>[...host.querySelectorAll('tbody tr')].map(row=>row.children[2].textContent);
function key(target,name){target.dispatchEvent(new w.KeyboardEvent('keydown',{key:name,bubbles:true,cancelable:true}));}
async function choose(button,value){assert.ok(button,'选择器必须已显示');button.click();const menu=openMenu();assert.ok(menu,'选择菜单必须打开');const option=[...menu.querySelectorAll('[role=menuitemradio]')].find(item=>item.dataset.value===String(value));assert.ok(option,'选择值必须存在：'+value);option.click();await settle();}
async function render(context={}){await UI.render(host,{view:'statements',period:'2026-10',...context},()=>true);}

(async()=>{
  equal([...F.transactionKinds],canonical,'公开的类型合同提供九个规范类型');
  await render({filters:{account_id:'acct-main'}});
  equal(host.querySelectorAll('tbody tr').length,50,'默认首屏按接口读取50条');
  check(bodyKinds().every(kind=>kind==='消费'),'首屏故意只含消费，收入位于第60条以后');
  check(!host.querySelector('.cd-source')&&!host.textContent.includes('余额同步')&&!host.textContent.includes('估值更新'),'交易明细不再插入两行时间或空说明容器');
  equal(host.querySelectorAll('thead th').length,4,'交易表保留四列表头');
  typeButton().click();
  equal(menuValues(openMenu()),['null',...canonical],'类型选项覆盖整月，而非首屏50条');
  key(openMenu(),'Escape');
  await choose(typeButton(),'income');
  equal(lastQuery().get('kind'),'income','选择收入向接口传规范类型');
  equal(lastQuery().get('account_id'),'acct-main','筛选继续保留所属账户');
  equal(lastQuery().get('offset'),'0','筛选从第一页读取');
  equal(host.querySelectorAll('tbody tr').length,23,'整月筛选取得首屏之外的全部23条收入');
  check(bodyKinds().every(kind=>kind==='收入'),'收入筛选无消费混入');
  equal(host.querySelector('.cd-page-total').textContent,'共 23 条','总数来自筛选后接口结果');
  typeButton().click();
  equal(menuValues(openMenu()),['null',...canonical],'当前选择收入时仍可切换到整月其他类型');
  key(openMenu(),'Escape');

  await choose(typeButton(),null);
  pageButton('下一页').click();await settle();
  equal(lastQuery().get('offset'),'50','下一页采用当前每页条数推进offset');
  equal(host.querySelector('.cd-page-number').textContent,'2 / 3','分页显示真实页数');
  await choose(typeButton(),'income');
  equal(lastQuery().get('offset'),'0','从第二页改类型重置offset');
  await choose(typeButton(),null);pageButton('下一页').click();await settle();
  await choose(sizeButton(),10);
  equal(lastQuery().get('limit'),'10','每页10条传给接口');
  equal(lastQuery().get('offset'),'0','从第二页改条数重置offset');
  equal(host.querySelectorAll('tbody tr').length,10,'每页10条实际只展示10条');
  equal(host.querySelector('.cd-page-number').textContent,'1 / 11','每页10条据总量重算页数');
  pageButton('下一页').click();await settle();
  equal(lastQuery().get('offset'),'10','每页10条后的下一页读取offset10');
  pageButton('上一页').click();await settle();
  equal(lastQuery().get('offset'),'0','上一页按当前limit返回第一页');
  check(pageButton('上一页').disabled,'第一页禁用上一页');
  for(const limit of [20,50,100]){
    await choose(sizeButton(),limit);
    equal(lastQuery().get('limit'),String(limit),'每页'+limit+'条请求使用所选条数');
    equal(host.querySelectorAll('tbody tr').length,limit,'每页'+limit+'条渲染所选条数');
  }
  pageButton('下一页').click();await settle();
  equal(lastQuery().get('offset'),'100','每页100条的下一页读取offset100');
  equal(host.querySelectorAll('tbody tr').length,2,'末页显示真实剩余2条');
  check(pageButton('下一页').disabled,'末页禁用下一页');

  await render({filters:{account_id:'acct-other'}});typeButton().click();
  equal(menuValues(openMenu()),['null','insurance_premium'],'可用类型依照整月及所选账户限定');
  key(openMenu(),'Escape');
  await render({filters:{account_id:'acct-other',transaction_kind:'income',page_size:20}});
  check(host.textContent.includes('没有已记录的交易。')&&!host.querySelector('tbody'),'空筛选结果不伪造记录');
  check(typeButton()&&host.querySelectorAll('thead th').length===4,'空筛选仍保留表头中的恢复入口');
  equal(host.querySelector('.cd-page-number').textContent,'0 / 0','空结果显示0页');
  check(pageButton('上一页').disabled&&pageButton('下一页').disabled,'空结果禁用两个分页按钮');
  typeButton().click();equal(menuValues(openMenu()),['null','income','insurance_premium'],'空结果菜单保留当前已选类型和真实可用类型');key(openMenu(),'Escape');
  await choose(typeButton(),null);
  equal(host.querySelectorAll('tbody tr').length,1,'空结果可通过全部类型恢复交易');
  equal(lastQuery().get('account_id'),'acct-other','恢复全部类型继续限定当前账户');

  await render({filters:{account_id:'acct-main',transaction_kind:'transfer'}});
  equal(host.querySelectorAll('tbody tr').length,4,'规范转账筛选同时接收旧external_transfer别名');
  check(bodyKinds().every(kind=>kind==='转账'),'规范转账与旧别名显示统一名称');
  equal(lastQuery().get('kind'),'transfer','筛选请求只发送规范transfer');
  const apiTransfer=await F.transactions({month:'2026-10',account_id:'acct-main',kind:'transfer',limit:10});
  check(apiTransfer.data.items.some(row=>row.kind==='external_transfer'),'公共API接受响应中的转账别名');
  for(const invalid of ['external_transfer','unknown','',123])throws(()=>F.transactions({month:'2026-10',kind:invalid}),/流水类型无效/,'非法请求类型在读取前拒绝：'+invalid);
  const beforeInvalid=transactionCalls().length;
  throws(()=>F.transactions({month:'2026-10',kind:'income',unrecognized:true}),/流水参数无效/,'未知请求参数拒绝');
  equal(transactionCalls().length,beforeInvalid,'无效请求不进入假服务读取');
  const mismatches=[
    ['响应类型不匹配',page=>page.data.kind='expense',/类型筛选无法核对/],
    ['筛选响应缺少kind',page=>delete page.data.kind,/类型筛选无法核对/],
    ['筛选响应混入消费',page=>page.data.items[0].kind='expense',/类型或账户无法核对/],
    ['可用类型含非法值',page=>page.data.available_kinds=['unknown'],/类型筛选无法核对/],
    ['可用类型重复',page=>page.data.available_kinds=['income','income'],/类型筛选无法核对/],
    ['可用类型不是数组',page=>page.data.available_kinds='income',/类型筛选无法核对/],
    ['响应分页不匹配',page=>page.data.offset=10,/分页未完整取得/],
    ['响应账户不匹配',page=>page.data.account_id='acct-other',/分页未完整取得/],
    ['响应记录跨账户',page=>page.data.items[0].account_id='acct-other',/类型或账户无法核对/]
  ];
  for(const [label,mutate,pattern] of mismatches){mutatePage=mutate;await rejects(()=>F.transactions({month:'2026-10',account_id:'acct-main',kind:'income',limit:10}),pattern,label);mutatePage=null;}
  mutatePage=page=>page.data.kind='income';await rejects(()=>F.transactions({month:'2026-10',limit:10}),/类型筛选无法核对/,'未筛选请求拒绝服务端私自筛选');mutatePage=null;
  await render({filters:{account_id:'acct-main',transaction_kind:'unknown',page_size:7,page_offset:5}});
  check(!lastQuery().has('kind')&&lastQuery().get('limit')==='50'&&lastQuery().get('offset')==='0','非法保存类型、条数和offset恢复为全部类型及第一页50条');
  for(const invalid of [-10,1,10.5,'10',10010,Number.MAX_SAFE_INTEGER+1]){
    await render({filters:{account_id:'acct-main',page_size:10,page_offset:invalid}});
    equal(lastQuery().get('offset'),'0','保存offset必须是范围内整数且为当前limit倍数：'+invalid);
  }
  mutatePage=page=>delete page.data.available_kinds;await render();typeButton().click();
  equal(menuValues(openMenu()),['null',...canonical],'缺少可用类型元数据的旧响应仍提供九种筛选');key(openMenu(),'Escape');mutatePage=null;

  await render({filters:{account_id:'acct-main',transaction_kind:'income',page_size:10}});
  equal(updates.at(-1).filters,{account_id:'acct-main',transaction_kind:'income',page_size:10,page_offset:0},'路由状态保存账户、类型、每页条数和当前offset');
  pageButton('下一页').click();await settle();typeButton().click();
  const menu=openMenu(),readsBeforeMenuFocus=calls.length;
  w.dispatchEvent(new w.Event('focus'));await settle();
  equal(calls.length,readsBeforeMenuFocus,'打开菜单时焦点返回不刷新或打断选择');
  check(menu.isConnected&&!menu.hidden&&typeButton().getAttribute('aria-expanded')==='true','焦点返回期间菜单仍保持打开');
  key(menu,'Escape');w.dispatchEvent(new w.Event('focus'));await settle();
  check(calls.length>readsBeforeMenuFocus,'关闭菜单后焦点返回刷新交易');
  equal(lastQuery().get('kind'),'income','焦点刷新保留所选类型');
  equal(lastQuery().get('limit'),'10','焦点刷新保留每页条数');
  equal(lastQuery().get('account_id'),'acct-main','焦点刷新保留账户范围');
  equal(lastQuery().get('offset'),'10','翻至第二页后焦点刷新保留offset10');
  equal(host.querySelector('.cd-page-number').textContent,'2 / 3','翻页后焦点刷新保留第二页页码');
  equal(updates.at(-1).filters.page_offset,10,'翻页后的offset保存在路由状态');
  equal(typeButton().getAttribute('aria-label'),'筛选交易类型：收入','刷新后类型按钮继续显示所选类型');
  equal(sizeButton().getAttribute('aria-label'),'每页条数：10','刷新后条数按钮继续显示所选条数');
  equal(w.document.querySelectorAll('.cd-choice-menu').length,2,'反复加载和焦点刷新不会累积菜单');
  pageButton('下一页').click();await settle();
  const month=host.querySelector('input[type=month]');month.value='2026-09';month.dispatchEvent(new w.Event('change'));await settle();
  equal(lastQuery().get('offset'),'0','切换月份重置offset');
  equal(lastQuery().get('month'),'2026-09','切换月份读取所选月');
  equal(lastQuery().get('kind'),'income','切换月份保留类型选择');
  equal(host.querySelectorAll('tbody tr').length,1,'切换月份按新月及类型显示真实结果');
  w.dispatchEvent(new w.Event('focus'));await settle();
  equal(host.querySelector('input[type=month]').value,'2026-09','焦点刷新保留用户选择月份');

  await render({filters:{account_id:'acct-main',transaction_kind:'income',page_size:10,page_offset:20}});
  equal(host.querySelector('.cd-page-number').textContent,'3 / 3','合法保存offset恢复原第三页');
  const smallerRows=rows.filter(row=>row.account_id==='acct-main'&&row.occurred_at.slice(0,7)==='2026-10'&&row.kind==='income').slice(0,15);
  mutatePage=page=>{page.data.total=smallerRows.length;page.data.items=clone(smallerRows.slice(page.data.offset,page.data.offset+page.data.limit));page.data.has_more=page.data.offset+page.data.items.length<page.data.total;};
  w.dispatchEvent(new w.Event('focus'));await settle();
  equal(lastQuery().get('offset'),'10','交易总数缩小后自动读取最后有效页');
  equal(host.querySelector('.cd-page-number').textContent,'2 / 2','总数缩小后显示有效末页页码');
  equal(host.querySelectorAll('tbody tr').length,5,'总数缩小后末页显示真实剩余5条');
  equal(updates.at(-1).filters.page_offset,10,'自动恢复的有效offset同步保存');
  mutatePage=page=>{page.data.total=0;page.data.items=[];page.data.has_more=false;};
  w.dispatchEvent(new w.Event('focus'));await settle();
  equal(lastQuery().get('offset'),'0','交易总数归零时自动恢复offset0');
  equal(host.querySelector('.cd-page-number').textContent,'0 / 0','交易总数归零时显示0页');
  check(pageButton('上一页').disabled&&pageButton('下一页').disabled,'总数归零后分页按钮禁用');
  equal(updates.at(-1).filters.page_offset,0,'归零后保存offset0避免反复读取越界页');mutatePage=null;

  await render();const trigger=typeButton();trigger.focus();key(trigger,'ArrowDown');
  let keyboardMenu=openMenu(),options=[...keyboardMenu.querySelectorAll('[role=menuitemradio]')];
  check(trigger.getAttribute('aria-haspopup')==='menu'&&trigger.getAttribute('aria-expanded')==='true','键盘打开按钮有菜单语义及展开状态');
  equal(w.document.activeElement,options[0],'键盘打开将焦点置于当前选项');
  key(w.document.activeElement,'ArrowDown');equal(w.document.activeElement,options[1],'向下键移至下一选项');
  key(w.document.activeElement,'End');equal(w.document.activeElement,options.at(-1),'End键移至最后选项');
  key(w.document.activeElement,'ArrowDown');equal(w.document.activeElement,options[0],'向下键从末尾环回首项');
  key(w.document.activeElement,'ArrowUp');equal(w.document.activeElement,options.at(-1),'向上键从首项环回末项');
  key(w.document.activeElement,'Home');equal(w.document.activeElement,options[0],'Home键返回第一项');
  key(w.document.activeElement,'Escape');
  check(keyboardMenu.hidden&&trigger.getAttribute('aria-expanded')==='false','Escape关闭菜单并更新展开状态');
  equal(w.document.activeElement,trigger,'Escape将焦点还给触发按钮');
  trigger.click();w.document.getElementById('outside').dispatchEvent(new w.Event('pointerdown',{bubbles:true}));
  check(!openMenu(),'点击菜单外关闭菜单');
  trigger.click();key(openMenu(),'Tab');check(!openMenu(),'Tab离开菜单时关闭菜单');
  trigger.click();sizeButton().click();
  equal(w.document.querySelectorAll('.cd-choice-menu:not([hidden])').length,1,'条数菜单打开时关闭类型菜单');
  equal(menuValues(openMenu()),['10','20','50','100'],'条数菜单仅提供四个授权条数');
  check([...openMenu().querySelectorAll('[role=menuitemradio]')].filter(option=>option.getAttribute('aria-checked')==='true').length===1,'条数菜单只有当前值选中');
  w.dispatchEvent(new w.Event('resize'));check(!openMenu(),'窗口尺寸变化关闭浮层');
  trigger.click();w.document.dispatchEvent(new w.Event('scroll'));check(!openMenu(),'页面滚动关闭浮层');
  trigger.click();const staleTrigger=trigger;
  w.dispatchEvent(new w.Event('navren:privacy-reset'));
  check(!host.textContent,'撤销隐私授权清空交易视图');
  equal(w.document.querySelectorAll('.cd-choice-menu').length,0,'撤销隐私授权移除所有body菜单');
  const readsAfterPrivacy=calls.length;staleTrigger.click();w.dispatchEvent(new w.Event('focus'));await settle();
  equal(calls.length,readsAfterPrivacy,'撤销授权后失效控件和焦点事件不触发读取');

  await render();typeButton().click();
  const oldOption=[...openMenu().querySelectorAll('[role=menuitemradio]')].find(option=>option.dataset.value==='income');
  const readsBeforeView=calls.length,updatesBeforeView=updates.length;
  w.dispatchEvent(new w.CustomEvent('navren:view',{detail:{page:'account',view:'home'}}));
  equal(w.document.querySelectorAll('.cd-choice-menu').length,0,'返回首页立即移除所有body挂载菜单');
  host.textContent='首页已显示';oldOption.click();await settle();
  equal(updates.length,updatesBeforeView,'返回首页后旧菜单选项不再写入filters');
  equal(calls.length,readsBeforeView,'返回首页后旧菜单选项不再发起GET');
  equal(host.textContent,'首页已显示','返回首页后旧菜单选项不改变新页面内容');
  w.dispatchEvent(new w.Event('focus'));await settle();
  equal(calls.length,readsBeforeView,'返回首页清除挂载记录后焦点事件不重读旧交易');

  let releaseView;nextTransactionDelay=new Promise(resolve=>releaseView=resolve);const pendingView=render();await settle();
  check(host.textContent.includes('正在同步账户'),'页面切换的迟到响应场景已进入交易读取');
  w.dispatchEvent(new w.CustomEvent('navren:view',{detail:{page:'account',view:'home'}}));
  host.textContent='新的首页内容';const updatesAfterView=updates.length;
  releaseView();await pendingView;await settle();
  equal(host.textContent,'新的首页内容','页面切换后迟到交易响应不能覆盖新首页');
  equal(w.document.querySelectorAll('.cd-choice-menu').length,0,'页面切换后迟到交易响应不能重新创建菜单');
  equal(updates.length,updatesAfterView,'页面切换后迟到交易响应不能再写入路由筛选状态');

  let release;nextTransactionDelay=new Promise(resolve=>release=resolve);const pending=render();await settle();
  check(host.textContent.includes('正在同步账户'),'迟到响应场景已进入交易读取');
  identityEpoch++;identity={...identity,privacy_revision:43};w.dispatchEvent(new w.Event('navren:privacy-reset'));release();await pending;await settle();
  check(!host.textContent&&!host.querySelector('tbody'),'隐私变化后迟到交易响应不恢复资料');
  equal(w.document.querySelectorAll('.cd-choice-menu').length,0,'迟到交易响应不重建旧菜单');
  check(calls.every(request=>request.startsWith('/connected-assets')),'全部银行请求只读取接入目录或交易接口');
  equal(writes.length,0,'所有测试执行0次银行写入');
  equal(networkAttempts.length,0,'所有测试执行0次真实网络请求');
  console.log(JSON.stringify({passed:true,assertions,reads:calls.length,writes:writes.length,network_requests:networkAttempts.length,evidence:'交易筛选、分页及菜单隔离假响应与DOM；未运行浏览器、HTTP或现役库'}));
  dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1;});
