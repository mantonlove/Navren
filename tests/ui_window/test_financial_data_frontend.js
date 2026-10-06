/* 实际独立库返回的DOM/身份检查。夹具添加HTTP身份封套，不宣称实际HTTP或实屏。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require('jsdom');
const fixture=JSON.parse(fs.readFileSync('outputs/window-progress/ui/financial-data/fixture.json','utf8'));
const dom=new JSDOM(fs.readFileSync('account.html','utf8'),{url:'http://127.0.0.1:60291/account.html',runScripts:'outside-only'}),w=dom.window;
const subject=fixture.subjects.find(s=>s.subject_id==='working_landlady');
let identity={account_id:subject.subject_id,workspace_id:subject.workspace_id,privacy_revision:42},identityEpoch=0,viewEpoch=0,renderer,context={view:'home'},delay=null;
const calls=[],writes=[];
const clone=value=>JSON.parse(JSON.stringify(value));
const lookup=path=>{const url=new URL(path,'http://fixture'),op=url.pathname.split('/').at(-1),query=[...url.searchParams.entries()].sort((a,b)=>a[0].localeCompare(b[0])),key=identity.account_id+'/'+op+'?'+new URLSearchParams(query);const response=fixture.responses[key];assert(response,'缺少实际返回夹具 '+key);return clone(response);};
w.BankClient={ensure:async()=>identity,identity:()=>({...identity}),guard(){const epoch=identityEpoch,view=viewEpoch,before={...identity};return {valid(data){return epoch===identityEpoch&&view===viewEpoch&&before.workspace_id===identity.workspace_id&&before.privacy_revision===identity.privacy_revision&&(!data||data.workspace_id===before.workspace_id&&data.privacy_revision===before.privacy_revision);}};},get:async path=>{calls.push(path);const value=lookup(path);if(delay&&path.includes('presentation')){const hold=delay;delay=null;await hold;}return value;},post:async(...args)=>{writes.push(args);throw Error('只读检查不得提交操作');},money:minor=>(minor/100).toFixed(2)+' 元',key:()=> 'test-key'};
w.NavrenViews={register:(page,fn)=>{renderer=fn;},current:()=>context,revision:()=>viewEpoch,url:meta=>{const params=new URLSearchParams();for(const [key,value]of Object.entries(meta))if(value!=null&&typeof value!=='object')params.set(key,value);return 'account.html?'+params;},open:async(view,meta={})=>{viewEpoch++;context={...meta,view};await renderer(context);},back:()=>{},update:meta=>{context={...context,...meta};}};
const execution={workspace_id:subject.workspace_id,version:777,as_of:'2026-10-03',accounts:[{id:'current-operation-account',name:'当前操作账户',balance_minor:999999999,currency:'CNY'}],holdings:[],cards:[],split_bills:[],profile:{risk_level:3}};
w.BankUI={node:(tag,text,cls)=>{const e=w.document.createElement(tag);if(text!=null)e.textContent=text;if(cls)e.className=cls;return e;},button:(text,fn)=>{const b=w.document.createElement('button');b.textContent=text;b.onclick=fn;return b;},formatMoney:minor=>w.NavrenFinancialData.money(minor),state:()=>execution,products:()=>[],load:async()=>execution,showError:(host,e)=>host.textContent=e.message,openDraft:async payload=>writes.push(payload)};
w.CalendarUI={destroyAccount:()=>{}};
for(const file of ['financial_data','calendar_client','account_home','account_workspace'])w.eval(fs.readFileSync('js/'+file+'.js','utf8'));
const settle=async()=>{for(let i=0;i<14;i++)await new Promise(r=>setTimeout(r,0));};
let assertions=0;const check=(value,label)=>{assert(value,label);assertions++;};
(async()=>{
 for(const s of fixture.subjects){identity={account_id:s.subject_id,workspace_id:s.workspace_id,privacy_revision:42};identityEpoch++;w.NavrenFinancialData.clear();const data=await w.NavrenFinancialData.overview();check(data.subject_id===s.subject_id,'四主体各自关联');check(data.data.summary.total_assets_minor===data.data.summary.cash_minor+data.data.summary.investment_minor+data.data.summary.real_assets_minor,'总资产未加授信/保额');}
 identity={account_id:subject.subject_id,workspace_id:subject.workspace_id,privacy_revision:42};identityEpoch++;w.NavrenFinancialData.clear();
 w.dispatchEvent(new w.CustomEvent('navren:account-rendered',{detail:{state:execution,products:[]}}));await settle();
 const overview=await w.NavrenFinancialData.overview(),data=overview.data;
 check(w.document.querySelector('#assetOverview .ah-total .ah-amount').title===w.NavrenFinancialData.money(data.summary.total_assets_minor),'首页取独立库总资产');
 check(!w.document.getElementById('assetOverview').textContent.includes('9,999,999.99'),'未叠加旧账户余额');
 check(w.document.querySelectorAll('#accountCardsPreview .ah-bank-card').length===data.cards.filter(c=>c.status==='active').length,'首页仅有效卡');
 check(w.document.querySelectorAll('#accountMonthlyReport polyline').length===1,'真实每周快照绘制资产曲线');
 check(w.document.getElementById('accountMonthlyReport').textContent.includes('每周及期末快照'),'曲线采样口径明确');
 check(w.document.getElementById('accountRecentTransactions').textContent.includes('交易明细')||w.document.querySelector('#accountRecentTransactions tbody tr'),'读取真实分页流水');
 check(w.document.getElementById('remittance-account_id').value==='current-operation-account','付款账户保留执行账本ID');
 check(!w.document.querySelector('#accountRemittanceForm option')?.value.includes('::'),'历史ID不传入操作表单');
 check(!w.document.querySelector('#remittance-bank_region')&&!w.document.querySelector('#remittance-bank_code'),'保留之前删除的地区和联行号');
 for(const [view,kind,id]of [['account-detail','account',data.accounts[0].account_id],['card-detail','card',data.cards[0].card_id],['holding-detail','holding',data.positions[0].position_id],['record-detail','asset',data.assets[0].asset_id]]){
   await w.NavrenViews.open(view,{source_scope:w.NavrenFinancialData.scope,object_kind:kind,object_id:id,object_version:overview.dataset_version});await settle();const host=w.document.getElementById('accountSubview');check(!host.textContent.includes('正在读取'),'详情完成取数 '+view);check(!/从此账户转账|挂失|取消代扣|申请卡片|赎回 /.test(host.textContent),'历史详情只读 '+view);
 }
 await w.NavrenViews.open('archive',{source_scope:w.NavrenFinancialData.scope});await settle();let host=w.document.getElementById('accountSubview');check(host.textContent.includes('保险档案'),'保险档案恢复');const select=host.querySelector('select');select.value='expired';select.dispatchEvent(new w.Event('change'));await settle();check(host.textContent.includes('已过期'),'过期保单单独筛选');let policyLink=host.querySelector('a[href*=policy]');check(policyLink,'历史保单可打开');policyLink.click();await settle();host=w.document.getElementById('accountSubview');check(host.textContent.includes('保额（不计资产）'),'保额边界');check(host.textContent.includes('现金价值未知'),'未知现金价值未填零');
 await w.NavrenViews.open('statements',{source_scope:w.NavrenFinancialData.scope,object_kind:'account',object_id:data.accounts[0].account_id,period:'2026-10'});await settle();host=w.document.getElementById('accountSubview');check(host.textContent.includes('本人期初现金'),'主体快照与账户区分');check(host.textContent.includes('账户期初余额'),'账户筛选余额');
 await w.NavrenViews.open('calendar',{source_scope:w.NavrenFinancialData.scope,period:'2026-10'});await settle();host=w.document.getElementById('accountSubview');check(host.querySelectorAll('.calendar-day').length===31,'日历保留整月布局');host.querySelector('#account-calendar-day-2026-10-03').click();await settle();check(host.textContent.includes('当日明细'),'新库日明细');check(host.textContent.includes('投资收益未知'),'未知收益未造零');
 await w.NavrenViews.open('payments',{source_scope:w.NavrenFinancialData.scope,object_kind:'account',object_id:data.accounts[0].account_id});check(w.document.getElementById('accountSubview').textContent.includes('不能用于当前账户操作'),'拒绝历史对象进入操作');check(writes.length===0,'历史流程无POST');
 const originalGet=w.BankClient.get;w.NavrenFinancialData.clear();w.BankClient.get=async path=>{const response=await originalGet(path);response.data.accounts[0].subject_id='other';return response;};await assert.rejects(w.NavrenFinancialData.overview(),/其他主体/);assertions++;w.BankClient.get=originalGet;
 w.NavrenFinancialData.clear();let release;delay=new Promise(r=>release=r);const pending=w.NavrenFinancialData.overview();await new Promise(r=>setTimeout(r,0));identity={account_id:fixture.subjects[0].subject_id,workspace_id:fixture.subjects[0].workspace_id,privacy_revision:43};identityEpoch++;w.dispatchEvent(new w.Event('navren:privacy-reset'));release();await assert.rejects(pending,/账户或资料权限已变化/);assertions++;
 check(!w.document.getElementById('assetOverview').textContent,'撤销后清除历史金额');
 identity={account_id:subject.subject_id,workspace_id:subject.workspace_id,privacy_revision:42};identityEpoch++;w.NavrenFinancialData.clear();w.BankClient.get=async path=>{if(path.startsWith('/data/'))throw Error('独立库读取失败');return originalGet(path);};await w.NavrenViews.open('home');await settle();check(w.document.getElementById('assetOverview').textContent.includes('独立库读取失败'),'异常不回退旧余额');check(w.document.querySelector('#assetOverview button'),'显式重试');
 check(calls.every(path=>path.startsWith('/data/')),'展示新增取数只用独立入口');
 const result={passed:true,assertions,reads:calls.length,writes:writes.length,dataset_version:fixture.dataset_version,evidence:'只读数据库真实返回夹具 + DOM；非HTTP或实屏'};
 fs.writeFileSync('outputs/window-progress/ui/financial-data/checks.json',JSON.stringify(result,null,2));console.log(JSON.stringify(result));dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1;});
