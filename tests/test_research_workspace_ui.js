/* 正式模块与隔离DOM/API合同。保单、金额和身份均为测试夹具；不读现役数据，不调用模型。 */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {JSDOM}=require('jsdom');
const source=fs.readFileSync(path.join(__dirname,'../js/research_workspace.js'),'utf8');
const sha=value=>crypto.createHash('sha256').update(value).digest('hex');
const tick=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};
const until=async(predicate)=>{const deadline=Date.now()+1000;while(!predicate()){if(Date.now()>deadline)throw new Error('等待界面结果超时');await new Promise(r=>setTimeout(r,5));}};
const identity=()=>({registered_user_id:'a'.repeat(32),workspace_id:'b'.repeat(32),privacy_revision:1,auth_subject:'fixture-registered-subject',csrf:'fixture-csrf'});
function entity(id){return {entity_id:id,name:'测试产品 '+id,code:id,customer_group:'individual',audience:'personal',currency:'CNY',capabilities:['field_evidence','manager_standard_cost_scenario'],evidence_version:'d'.repeat(64),facts:[{entity_id:id,field:'channel.WEBANK.risk_rating',value:{label:'R4中高风险'},state:'verified',source_scope:'channel_catalog',source_version:'d'.repeat(64),document_id:'test-document',locator:{page:3,clause:'费用条款'}},{entity_id:id,field:'fees',state:'verified',value:{subscription:[{min_amount:'0',max_amount:null,rate:'0.01'}],ongoing:[{kind:'management',annual_rate:'0.005',included_in_nav:true}],assumptions:['隔离测试费用情景，不是实际产品。']},source_scope:'manager_disclosure',source_version:'d'.repeat(64),document_id:'test-document',locator:{page:3,clause:'费用条款'}}],documents:[{document_id:'test-document',title:'隔离测试原文',url:'https://example.invalid/fixture.pdf',sha256:'d'.repeat(64),published_at:'2026-10-14'}]};}
function compositeEntity(id='A'){
 const data=entity(id),summaryId=id+':summary',prospectusId=id+':prospectus',summaryVersion='1'.repeat(64),prospectusVersion='2'.repeat(64);
 data.documents=[{document_id:summaryId,title:'隔离产品资料概要',url:'https://example.invalid/summary.pdf',sha256:summaryVersion,source_version:'sha256:'+summaryVersion},{document_id:prospectusId,document_type:'prospectus',url:'https://example.invalid/prospectus.pdf',sha256:prospectusVersion,source_version:'sha256:'+prospectusVersion}];
 const bindings=[['fees.manager_standard.subscription',summaryId,3,'三（一）申购费'],['fees.manager_standard.redemption',summaryId,3,'三（一）赎回费'],['fees.annual.management',summaryId,3,'三（二）运作相关费用'],['fees.annual.custody',summaryId,3,'三（二）运作相关费用'],['fees.annual.sales_service',prospectusId,115,'基金的费用与税收：销售服务费'],['fees.operating_deducted_from_assets',summaryId,3,'三（二）运作相关费用'],['fees.annual.accrual_basis',prospectusId,115,'基金的费用与税收：销售服务费']].map(([field,document_id,page,clause])=>({field,document_id,locator:{kind:'pdf',page,clause}}));
 Object.assign(data.facts.find(f=>f.field==='fees'),{source_version:'sha256:'+summaryVersion,document_id:summaryId,source_url:data.documents[0].url,locator:{kind:'composite',pages:[3],bindings}});return data;
}
async function assertCompositePanel(c){
 const panel=c.root.querySelector('.rw-evidence');assert.ok(panel);assert.match(panel.querySelector('.rw-evidence-overview').textContent,/概要原文位置：第3页/);assert.doesNotMatch(panel.textContent,/定位待补/);assert.equal(panel.querySelector('.rw-evidence-overview a').href,'https://example.invalid/summary.pdf');
 const items=[...panel.querySelectorAll('.rw-evidence-binding')];assert.equal(items.length,7);
 for(const item of items){assert.equal(item.open,false);item.querySelector('summary').click();await tick();assert.equal(item.open,true);const prospectus=['fees.annual.sales_service','fees.annual.accrual_basis'].includes(item.dataset.evidenceField);assert.match(item.textContent,prospectus?/招募说明书.*第115页/s:/隔离产品资料概要.*第3页/s);assert.match(item.textContent,new RegExp('版本：sha256:'+(prospectus?'2':'1').repeat(64)));assert.equal(item.querySelector('a').href,'https://example.invalid/'+(prospectus?'prospectus':'summary')+'.pdf');assert.equal(item.querySelector('a').rel,'noopener noreferrer');}
 return panel;
}
function setup(overrides={}){
 const dom=new JSDOM('<!doctype html><body><main id="root"></main></body>',{url:'https://navren.invalid/recommend.html',runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window;
 Object.defineProperty(w,'crypto',{value:crypto.webcrypto});w.AbortController=AbortController;w.fetch=async()=>{throw new Error('测试未登记的网络请求');};
 let current=identity();w.BankClient={identity:()=>current,ensure:async()=>current,guard:()=>{const expected=JSON.stringify(current);return {valid:()=>JSON.stringify(current)===expected};}};
 w.NavrenFormControls={enhance(){}};w.eval(source);
 const calls=[],items=['A','C','B','D'].map(entity);
 const api={
  catalog:async input=>{calls.push(['catalog',input]);return {entities:items,total:items.length,source_date:'2026-10-14'};},
  entity:async id=>{calls.push(['entity',id]);return entity(id);},
  compare:async input=>{calls.push(['compare',input]);return {id:'e'.repeat(32),calculation_id:'e'.repeat(32),version:1,status:'complete',rows:[{label:'隔离费用',values:Object.fromEntries(input.entity_ids.map(id=>[id,{value:'10.00',unit:'元',state:'verified'}]))}],missing:[],exclusions:[],calculations:['仅测试'],algorithm_version:'fixture/1'};},
  saveResearch:async input=>{calls.push(['saveResearch',input]);return {record:{id:'f'.repeat(32)}};},
  workspace:async()=>{calls.push(['workspace']);return {research:[]};},policies:async()=>({items:[]}),tasks:async()=>({items:[]}),...overrides
 };
 const root=w.document.getElementById('root'),ui=w.ResearchWorkspace.create({root,api,identity:()=>current,navigate:()=>{}});
 const findButton=(text,host=root)=>[...host.querySelectorAll('button')].find(b=>b.textContent.trim()===text);
 const click=async(text,host=root)=>{const b=findButton(text,host);assert.ok(b,'缺少按钮 '+text);b.click();await tick();return b;};
 const change=(label,value)=>{const input=root.querySelector(`[aria-label="${label}"]`);assert.ok(input,'缺少控件 '+label);input.value=value;input.dispatchEvent(new w.Event('input',{bubbles:true}));return input;};
 return {w,root,ui,api,calls,items,click,change,findButton,setIdentity:value=>{current=value;},close:()=>{ui.dispose();dom.window.close();}};
}
function reserveFixture(c,{needs=[180000],held=0,bankProtected=[]}={}){
 const copy=v=>JSON.parse(JSON.stringify(v)),policies=needs.map((_,i)=>({id:(i?'b':'a').repeat(32),record_revision:1,value:{label:i?'本人寿险保费':'本人医疗险保费',premium:{amount:i?'700.00':'1800.00',currency:'CNY',next_due_on:'2026-10-20'}}})),bank={version:7,as_of:'2026-10-16',commitment_window_end:'2026-11-15',profile:{reserve_minor:500000},accounts:[{id:'account-self',name:'本人结算账户',currency:'CNY',balance_minor:2500000},{id:'account-other',name:'本人其他账户',currency:'CNY',balance_minor:0}],commitments:[{kind:'mandate',account_id:'account-self',amount_minor:350000},{kind:'mandate',account_id:'account-self',amount_minor:50000}],fund_holds:held?[{id:'held-original',account_id:'account-self',amount_minor:held,status:'held'}]:[],remittances:[]},records=new Map(),opened=[];
 let revision=policies.length,saved=null;const operationId='e'.repeat(32);c.w.BankUI={openDraft:async draft=>opened.push(copy(draft))};
 c.api.overview=async()=>copy(bank);c.api.policies=async()=>({items:copy(policies)});c.api.workspace=async()=>({revision,research:[]});c.api.tasks=async()=>({items:saved?[copy(saved)]:[]});
 c.api.calculate=async input=>{c.calls.push(['calculate',copy(input)]);const id='c'.repeat(32),rows=policies.map((p,i)=>({obligation_id:'policy:'+p.id,amount_minor:i?70000:180000,due_date:'2026-10-20',additional_reserve_minor:needs[i],already_held_minor:i?0:held,bank_protected_minor:bankProtected[i]?180000:0,protection_basis:bankProtected[i]?'original_bank_commitment':'explicit_private_obligation_and_actual_allocations'})),additional=needs.reduce((sum,v)=>sum+v,0),record={id,record_revision:1,workspace_revision:++revision,value:{method:'reserve_obligations',algorithm_version:'navren.research-protection-binding/1',source_scope:{account_id:input.account_id,bank_version:bank.version,selected_policy_records:policies.map(p=>({record_id:p.id,record_revision:p.record_revision})),policy_obligation_aliases:Object.fromEntries(policies.map(p=>['policy:'+p.id,'policy:'+p.id]))},result:{status:'complete',calculations:{cash_minor:2500000,required_reserve_minor:900000+held+additional,already_protected_minor:900000+held,additional_reserve_minor:additional,uncommitted_cash_minor:1600000-held-additional},rows:[{obligation_id:'mandate:rent',amount_minor:350000,bank_protected_minor:350000,additional_reserve_minor:0},...rows],missing:[],exclusions:[]}}};records.set(id,record);return {revision,record:copy(record)};};
 c.api.saveTask=async input=>{c.calls.push(['saveTask',copy(input)]);const value=copy(input.value);value.conditions=Object.fromEntries(Object.entries(value.conditions).sort(([a],[b])=>a.localeCompare(b)));saved={id:'d'.repeat(32),kind:'task',record_revision:1,workspace_revision:++revision,value,operation_links:[]};records.set(saved.id,saved);return {revision,record:copy(saved)};};
 c.api.record=async id=>{c.calls.push(['record',id]);return {revision,record:copy(records.get(id))};};c.api.prepareReserve=async input=>{c.calls.push(['prepareReserve',copy(input)]);return {operation:{id:operationId,state:'pending_confirmation'},public_share_execution:false};};
 const open=async()=>{await c.ui.showCatalog({goal:'cash'});await c.click('核对用款与预留安排');c.change('资金账户','account-self');for(const p of policies)c.root.querySelector('input[value="'+p.id+'"]').checked=true;c.root.querySelector('form > .rw-consent input').checked=true;await c.click('核对用款安排');await until(()=>!!c.root.querySelector('[data-reserve-confirm]'));};
 const save=async()=>{c.root.querySelector('[data-reserve-confirm]').checked=true;await c.click('保存安排并核对操作');};return {bank,policies,opened,operationId,records,open,save,get saved(){return saved;}};
}
const passed=[],sourceCompositeOnly=process.argv.includes('--source-composite-only'),reserveSuccessorOnly=process.argv.includes('--reserve-successor-only');
async function check(name,fn){if(sourceCompositeOnly&&!name.startsWith('组合来源')&&!['详情六层保留原文字段来源、版本和页码','公开文字作为文本显示，危险链接不进入原文按钮'].includes(name))return;if(reserveSuccessorOnly&&!name.startsWith('资金保护后继')&&!['资金安排实际调用测算和保存任务，准备后只进入原确认组件','既有预留事项保留类型和原操作，编辑不重新准备'].includes(name))return;await fn();passed.push(name);}
async function withUI(fn,overrides){const c=setup(overrides);try{await fn(c);}finally{c.close();}}

async function run(){
 await check('首页提供资金用途和三类保险入口，零API调用',()=>withUI(async c=>{
  const products=c.w.document.createElement('div'),insurance=c.w.document.createElement('div');c.root.append(products,insurance);const links=[];
  c.w.ResearchWorkspace.renderHome({products,insurance,navigate:(...args)=>links.push(args)});
  assert.equal(products.querySelectorAll('button').length,3);assert.equal(insurance.querySelectorAll('button').length,3);assert.match(products.textContent,/现金安排/);assert.ok(!products.textContent.includes('前三'));
  products.querySelector('button').click();assert.equal(links[0][0],'research');assert.equal(links[0][1].filters.goal,'cash');assert.equal(c.calls.length,0);
 }));
 await check('筛选只提交公开条件，机构客群显式分离',()=>withUI(async c=>{
  await c.ui.showCatalog();c.change('产品、机构或代码','测试');const audience=c.change('适用客群','institutional');audience.dispatchEvent(new c.w.Event('change',{bubbles:true}));await c.click('应用筛选');
  const input=c.calls.filter(x=>x[0]==='catalog').at(-1)[1];assert.equal(input.audience,'institutional');assert.equal(input.q,'测试');assert.equal(input.owner,undefined);
 }));
 await check('最多选择三项，选中后撤出原选择按钮并可移除',()=>withUI(async c=>{
  await c.ui.showCatalog();for(const row of [...c.root.querySelectorAll('.rw-entity')].slice(0,3))await c.click('加入比较',row);
  assert.equal(c.ui.selection.length,3);const chosen=c.root.querySelector('.rw-entity');assert.equal(c.findButton('加入比较',chosen),undefined);assert.ok(c.findButton('移除',chosen));
  await c.click('加入比较',c.root.querySelectorAll('.rw-entity')[3]);assert.equal(c.ui.selection.length,3);assert.match(c.root.textContent,/最多比较3项/);
  await c.click('移除',c.root.querySelector('.rw-entity'));assert.equal(c.ui.selection.length,2);
 }));
 await check('详情六层保留原文字段来源、版本和页码',()=>withUI(async c=>{
  await c.ui.showDetail('A');assert.equal(c.root.querySelectorAll('[role="tab"]').length,6);await c.click('费用与退出');assert.match(c.root.textContent,/1%/);assert.match(c.root.textContent,/已体现在净值/);
  await c.click('来源');assert.match(c.root.querySelector('.rw-evidence').textContent,/第3页/);assert.equal(c.root.querySelector('.rw-evidence a').href,'https://example.invalid/fixture.pdf');
 }));
 await check('公开文字作为文本显示，危险链接不进入原文按钮',()=>withUI(async c=>{
  const data=entity('A');data.name='<img src=x onerror=alert(1)>';data.documents[0].url='javascript:alert(1)';c.api.entity=async()=>data;await c.ui.showDetail('A');assert.equal(c.root.querySelector('img'),null);assert.match(c.root.textContent,/<img/);await c.click('公告与资料');assert.equal(c.root.querySelector('.rw-document a'),null);
 }));
 await check('组合来源概要页码与七项原文逐一对应文档、版本和链接',()=>withUI(async c=>{
  c.api.entity=async()=>compositeEntity();await c.ui.showDetail('A');await c.click('费用与退出');await c.click('来源');await assertCompositePanel(c);
 }));
 await check('组合来源缺失文档或版本不符不借用概要链接，定位文本不执行',()=>withUI(async c=>{
  const data=compositeEntity(),fees=data.facts.find(f=>f.field==='fees');fees.locator.bindings=[{field:'fees.annual.sales_service',document_id:'not-in-pinned-documents',source_version:fees.source_version,locator:{pages:[115,116],clause:'<img src=x onerror=alert(1)>',table:'费用表',pointer:'/fees/sales_service'}},{field:'fees.annual.custody',document_id:data.documents[0].document_id,source_version:'sha256:'+'3'.repeat(64),locator:{page:3}},{field:'fees.annual.management',source_url:'javascript:alert(1)',locator:{clause:'仅有条款定位'}}];
  c.api.entity=async()=>data;await c.ui.showDetail('A');await c.click('费用与退出');await c.click('来源');const panel=c.root.querySelector('.rw-evidence');for(const item of panel.querySelectorAll('.rw-evidence-binding')){item.querySelector('summary').click();assert.equal(item.querySelector('a'),null);assert.match(item.textContent,/该项原件链接尚未取得/);}assert.match(panel.textContent,/第115、116页/);assert.match(panel.textContent,/费用表 · \/fees\/sales_service/);assert.match(panel.textContent,/<img/);assert.equal(panel.querySelector('img'),null);assert.equal(panel.querySelectorAll('a').length,1);
 }));
 await check('组合来源测算结果复用各产品固定文档依据并保留测算说明',()=>withUI(async c=>{
  c.api.entity=async id=>compositeEntity(id);c.api.compare=async input=>({id:'e'.repeat(32),version:1,status:'complete',rows:[{label:'申购费情景',values:Object.fromEntries(input.entity_ids.map(id=>[id,{...compositeEntity(id).facts.find(f=>f.field==='fees'),value:'10.00',unit:'元',evidence_kind:'calculation_input'}]))}],missing:[],exclusions:[],calculations:[],algorithm_version:'fixture/1'});
  await c.ui.showCatalog();for(const row of [...c.root.querySelectorAll('.rw-entity')].slice(0,2))await c.click('加入比较',row);await c.click('比较 2 项');c.change('研究金额（元）','10000');await c.click('重新计算');await c.click('来源',c.root.querySelector('[data-comparison-result]'));const panel=await assertCompositePanel(c);assert.match(panel.textContent,/测算采用的原始费用依据/);assert.match(panel.textContent,/不是原文承诺的未来收益/);
 }));
 await check('比较使用十进制金额与真实选择，改变期限即撤销旧结果',()=>withUI(async c=>{
  await c.ui.showCatalog();await c.click('加入比较',c.root.querySelectorAll('.rw-entity')[0]);await c.click('加入比较',c.root.querySelectorAll('.rw-entity')[1]);await c.click('比较 2 项');
  c.change('研究金额（元）','10,000.00');await c.click('重新计算');const input=c.calls.find(x=>x[0]==='compare')[1];assert.equal(input.amount,'10000.00');assert.equal(input.holding_days,365);assert.ok(c.findButton('保存这份研究'));
  c.change('持有天数','30');assert.equal(c.findButton('保存这份研究'),undefined);assert.match(c.root.textContent,/条件已改变/);
 }));
 await check('保存研究仅经服务端接口，成功后不保留旧提交按钮',()=>withUI(async c=>{
  await c.ui.showCatalog();await c.click('加入比较',c.root.querySelectorAll('.rw-entity')[0]);await c.click('加入比较',c.root.querySelectorAll('.rw-entity')[1]);await c.click('比较 2 项');c.change('研究金额（元）','1000');await c.click('重新计算');await c.click('保存这份研究');assert.equal(c.calls.filter(x=>x[0]==='saveResearch').length,1);assert.equal(c.findButton('保存这份研究'),undefined);assert.match(c.root.textContent,/已保存/);
 }));
 await check('身份切换后丢弃晚到私人响应并卸载旧资料',()=>withUI(async c=>{
  let resolve;c.api.workspace=()=>new Promise(r=>{resolve=r;});const pending=c.ui.showSaved();await tick();c.setIdentity({...identity(),workspace_id:'c'.repeat(32)});c.w.dispatchEvent(new c.w.Event('navren:account-switched'));resolve({research:[{id:'secret',title:'旧账户私有研究'}]});await pending;assert.ok(!c.root.textContent.includes('旧账户私有研究'));assert.match(c.root.textContent,/账户已变化/);
 }));
 await check('目录响应缺失不是空目录，失败可保留条件重试',()=>withUI(async c=>{
  c.api.catalog=async()=>({});await c.ui.showCatalog();assert.match(c.root.textContent,/目录响应不完整/);assert.ok(!c.root.textContent.includes('没有符合当前条件'));assert.ok(c.findButton('重新读取'));
 }));
 await check('未取得保单资料不宣称没有保险或保障充分',()=>withUI(async c=>{
  c.api.policies=async()=>{throw new Error('保单读取失败');};await c.ui.showInsurance('policies');assert.match(c.root.textContent,/保单读取失败/);assert.ok(!c.root.textContent.includes('保障充分'));
 }));
 await check('空白账户可建立主附险责任，未知限额保留null并明确授权',()=>withUI(async c=>{
  let payload;c.api.savePolicy=async input=>{payload=input;return {record:{id:'p'.repeat(32)}};};await c.ui.showInsurance('policies');await c.click('添加保单档案');
  for(const [label,value]of [['保单名称','本人核对医疗保单'],['具体保障对象','本人'],['适用合同版本','2026合同原件'],['生效日期','2026-01-01'],['到期日期','2026-12-31'],['保费金额','500.00']])c.change(label,value);
  await c.click('添加保障责任');const first=c.root.querySelector('.rw-coverage-row');first.querySelector('[aria-label="责任名称"]').value='主医疗责任';const relation=first.querySelector('[aria-label="主险或附加险"]');relation.value='main';relation.dispatchEvent(new c.w.Event('change',{bubbles:true}));
  await c.click('添加保障责任');const second=c.root.querySelectorAll('.rw-coverage-row')[1];second.querySelector('[aria-label="责任名称"]').value='附加特药';const rider=second.querySelector('[aria-label="主险或附加险"]');rider.value='rider';rider.dispatchEvent(new c.w.Event('change',{bubbles:true}));const parent=second.querySelector('[aria-label="关联主险"]');parent.value=parent.options[1].value;
  c.root.querySelector('form>.rw-consent input').checked=true;await c.click('确认保存档案');assert.ok(payload);assert.equal(payload.value.coverages.length,2);assert.equal(payload.value.coverages[1].parent_id,payload.value.coverages[0].id);assert.equal(payload.value.coverages[0].annual_limit,null);assert.equal(payload.value.coverages[0].waiting_period_days,null);assert.equal(payload.value.coverages[0].evidence_state,'unverified');assert.equal(payload.value.consent.health_data_authorized,true);
 }));
 await check('既有角色保单只读可见，补资料显式关联原ID与版本',()=>withUI(async c=>{
  let payload;c.api.policies=async()=>({revision:3,items:[],existing_policy_records:{source_scope:'navren_synthetic',dataset_version:'fixture-role-v1',as_of:'2026-10-18',items:[{policy_id:'role:policy:1',insurance_type:'medical',status:'active',terms_version:'historical-2024',start_date:'2026-01-01',end_date:'2026-12-31',premium_minor:50000,sum_insured_minor:10000000,insured_id:'self',details:{policy_number:'fixture-P1'}}]}});c.api.savePolicy=async input=>{payload=input;return {record:{id:'p'.repeat(32)}};};await c.ui.showInsurance('policies');assert.match(c.root.textContent,/fixture-P1/);assert.ok(!c.root.textContent.includes('尚未取得该账户的保单档案'));await c.click('补资料');c.change('具体保障对象','本人');c.root.querySelector('form>.rw-consent input').checked=true;await c.click('确认保存档案');assert.equal(payload.id,undefined);assert.equal(payload.expected_revision,3);assert.equal(payload.value.existing_policy_id,'role:policy:1');assert.equal(payload.value.existing_policy_version,'fixture-role-v1');assert.equal(payload.value.contract_version,'historical-2024');
 }));
 await check('原件列表展示SHA且完整字节核对失败时不给出下载成功',()=>withUI(async c=>{
  const raw=Buffer.from('%PDF-1.7 fixture original'),hash=sha(raw),id='a'.repeat(32);c.api.policies=async()=>({items:[{id:'p'.repeat(32),value:{label:'原件档案',insured_object:{label:'本人'},premium:{amount:'500.00'},coverages:[],attachment_ids:[id]}}]});c.api.materials=async()=>({items:[{id,mime_type:'application/pdf',byte_count:raw.length,sha256:hash,created_at:'2026-10-18'}]});c.api.downloadMaterial=async()=>({bytes:Uint8Array.from(raw).buffer});await c.ui.showInsurance();await c.click('查看与编辑档案');assert.match(c.root.textContent,new RegExp(hash));await c.click('核对原件');await until(()=>c.root.textContent.includes('完整原件字节已核对'));assert.match(c.root.textContent,/完整原件字节已核对/);c.api.downloadMaterial=async()=>({bytes:new Uint8Array([0]).buffer});await c.click('核对原件');await until(()=>c.root.textContent.includes('原件SHA或字节数不一致'));assert.match(c.root.textContent,/原件SHA或字节数不一致/);
 }));
 await check('资金安排实际调用测算和保存任务，准备后只进入原确认组件',()=>withUI(async c=>{
  const f=reserveFixture(c);await f.open();assert.equal(c.calls.find(r=>r[0]==='calculate')[1].method,'reserve_obligations');await f.save();assert.equal(f.saved.value.kind,'reserve');assert.deepEqual(f.saved.value.conditions.obligation_ids,['policy:'+f.policies[0].id]);assert.equal(f.saved.value.conditions.amount,'1800.00');assert.ok(c.calls.some(r=>r[0]==='record'));await c.click('进入资金预留确认');assert.equal(f.opened[0].operation_id,f.operationId);assert.deepEqual(Object.keys(f.opened[0]),['operation_id']);assert.ok(!c.calls.some(r=>r[0]==='confirm'));assert.match(c.root.textContent,/已准备，尚未确认执行/);
 }));
 await check('资金保护后继四项指标与可选窗口直接使用报价，单项允许部分金额',()=>withUI(async c=>{
  const f=reserveFixture(c);await f.open();const metrics=Object.fromEntries([...c.root.querySelectorAll('.rw-facts .rw-fact')].map(row=>[row.querySelector('dt').textContent,row.querySelector('dd').textContent]));assert.equal(metrics['总需保护'],'10,800.00 元');assert.equal(metrics['已有保护'],'9,000.00 元');assert.equal(metrics['新增所需预留'],'1,800.00 元');assert.equal(metrics['保护后可安排'],'14,200.00 元');assert.equal(c.root.querySelector('[aria-label="用款核对截止日"]').max,'2026-11-15');assert.match(c.root.textContent,/超出此范围须补齐用款来源/);c.change('拟新增预留金额（元）','900.05');await f.save();assert.equal(f.saved.value.conditions.amount,'900.05');assert.equal(f.saved.value.conditions.obligation_ids.length,1);
 }));
 await check('资金保护后继多项只支持剩余需求整笔合计并在界面阻止错误总额',()=>withUI(async c=>{
  const f=reserveFixture(c,{needs:[180000,70000]});await f.open();const cash=c.root.querySelector('[aria-label="拟新增预留金额（元）"]');assert.equal(cash.value,'2,500.00');assert.equal(cash.readOnly,true);c.change('拟新增预留金额（元）','2400.00');await f.save();assert.equal(f.saved,null);assert.match(c.root.textContent,/多项须等于所选剩余需求之和/);c.change('拟新增预留金额（元）','2500.00');await f.save();assert.equal(f.saved.value.conditions.amount,'2500.00');assert.equal(f.saved.value.conditions.obligation_ids.length,2);
 }));
 await check('资金保护后继取消一项后可部分预留，超额单项在保存前阻止',()=>withUI(async c=>{
  const f=reserveFixture(c,{needs:[180000,70000]});await f.open();const picks=c.root.querySelectorAll('[data-reserve-obligation]');picks[1].checked=false;picks[1].dispatchEvent(new c.w.Event('change',{bubbles:true}));assert.equal(c.root.querySelector('[aria-label="拟新增预留金额（元）"]').readOnly,false);c.change('拟新增预留金额（元）','1800.01');await f.save();assert.equal(f.saved,null);c.change('拟新增预留金额（元）','1000.00');await f.save();assert.deepEqual(f.saved.value.conditions.obligation_ids,['policy:'+f.policies[0].id]);
 }));
 await check('资金保护后继已有保护的私人保单不能作为新增冻结选择',()=>withUI(async c=>{
  const f=reserveFixture(c,{needs:[0,70000],bankProtected:[true,false]});await f.open();const picks=[...c.root.querySelectorAll('[data-reserve-obligation]')];assert.equal(picks.length,1);assert.equal(picks[0].value,'policy:'+f.policies[1].id);await f.save();assert.equal(f.saved.value.conditions.amount,'700.00');assert.deepEqual(f.saved.value.conditions.obligation_ids,['policy:'+f.policies[1].id]);
 }));
 await check('资金保护后继主动不选新增保费时只保存安排，不宣称已满足保护需求',()=>withUI(async c=>{
  const f=reserveFixture(c);await f.open();const pick=c.root.querySelector('[data-reserve-obligation]');pick.checked=false;pick.dispatchEvent(new c.w.Event('change',{bubbles:true}));await f.save();assert.equal(f.saved.value.conditions.amount,'0.00');assert.deepEqual(f.saved.value.conditions.obligation_ids,[]);assert.match(c.root.textContent,/本次不新增预留/);assert.doesNotMatch(c.root.textContent,/本次无需新增预留/);assert.equal(c.calls.filter(r=>r[0]==='prepareReserve').length,0);
 }));
 await check('资金保护后继零新增保存提醒且明确读回，列表也不准备零额操作',()=>withUI(async c=>{
  const f=reserveFixture(c,{needs:[0],held:180000});await f.open();assert.match(c.root.textContent,/10,800.00 元/);assert.equal(c.root.querySelector('[aria-label="拟新增预留金额（元）"]').value,'0.00');for(const input of c.root.querySelectorAll('[data-reminder-channel]'))input.checked=true;await f.save();assert.equal(f.saved.value.conditions.amount,'0.00');assert.deepEqual(f.saved.value.conditions.obligation_ids,[]);assert.deepEqual(f.saved.value.reminder,{enabled:true,calendar:true,message:true});assert.equal(c.calls.filter(r=>r[0]==='record').length,1);assert.match(c.root.textContent,/安排与提醒已保存并读回/);assert.equal(c.findButton('进入资金预留确认'),undefined);await c.click('查看已保存安排');assert.equal(c.findButton('进入资金预留确认'),undefined);assert.equal(c.calls.filter(r=>r[0]==='prepareReserve').length,0);
 }));
 await check('资金保护后继保单变化或银行底线无版本变化也使旧报价失效',async()=>{
  for(const change of ['policy','floor'])await withUI(async c=>{const f=reserveFixture(c);await f.open();if(change==='policy')f.policies[0].record_revision++;else f.bank.profile.reserve_minor++;await f.save();assert.equal(f.saved,null);assert.match(c.root.textContent,/报价已失效/);assert.ok(c.findButton('重新核对用款安排'));assert.equal(c.calls.filter(r=>r[0]==='prepareReserve').length,0);});
 });
 await check('资金保护后继报价中改账户丢弃晚到响应，不显示旧金额或旧安排',()=>withUI(async c=>{
  const f=reserveFixture(c),calculate=c.api.calculate;let resolve;c.api.calculate=input=>new Promise(done=>{resolve=()=>done(calculate(input));});await c.ui.showCatalog({goal:'cash'});await c.click('核对用款与预留安排');c.change('资金账户','account-self');c.root.querySelector('form > .rw-consent input').checked=true;await c.click('核对用款安排');await until(()=>!!resolve);c.change('资金账户','account-other');resolve();await tick();assert.equal(c.root.querySelector('[data-reserve-confirm]'),null);assert.match(c.root.textContent,/报价条件已改变/);
 }));
 await check('资金保护后继保存后账户变化不prepare，服务端拒绝也不自动重试',async()=>{
  for(const mode of ['preflight','server'])await withUI(async c=>{const f=reserveFixture(c);await f.open();await f.save();if(mode==='preflight')f.bank.version++;else c.api.prepareReserve=async input=>{c.calls.push(['prepareReserve',input]);throw Object.assign(new Error('原保护已变化'),{status:409,code:'RESEARCH_VERSION_CONFLICT'});};await c.click('进入资金预留确认');assert.equal(c.calls.filter(r=>r[0]==='prepareReserve').length,mode==='server'?1:0);assert.equal(f.opened.length,0);assert.equal(c.findButton('进入资金预留确认'),undefined);assert.ok(c.findButton('重新核对用款安排'));await tick();assert.equal(c.calls.filter(r=>r[0]==='prepareReserve').length,mode==='server'?1:0);});
 });
 await check('资金保护后继原操作成功后读回同一安排再提供重新报价',()=>withUI(async c=>{
  const f=reserveFixture(c);await f.open();await f.save();await c.click('进入资金预留确认');f.saved.operation_links=[{operation_id:f.operationId,state:'succeeded'}];c.api.operation=async()=>({operation:{id:f.operationId,state:'succeeded',receipt_id:'original-receipt'}});await c.click('读取原操作状态');assert.match(c.root.textContent,/同一原操作及关联安排已读回/);assert.ok(c.findButton('重新核对用款安排'));assert.equal(c.calls.filter(r=>r[0]==='prepareReserve').length,1);
 }));
 await check('机构筛选适配真实枚举并逐实体校验目录发布版本',()=>withUI(async c=>{
  const requests=[],revision='v'.repeat(64);c.w.fetch=async(url,options)=>{requests.push([url,options]);let data;if(url.includes('/catalog?'))data={contract:c.w.ResearchWorkspace.contract,data_scope:'public',provider_revision:revision,items:[{...entity('A'),provider_revision:revision}],total:1,limit:12};else data={contract:c.w.ResearchWorkspace.contract,data_scope:'public',provider_revision:revision,evidence_version:entity('A').evidence_version,entity:{...entity('A'),provider_revision:revision,customer_group:'institution'}};return {ok:true,headers:new Headers(),json:async()=>data};};const api=c.w.ResearchWorkspace.createHttpApi();const data=await api.catalog({audience:'institutional',kind:'insurance_offer'});assert.match(requests[0][0],/customer_group=institution(?:&|$)/);assert.match(requests[0][0],/kind=insurance_offer/);assert.equal(data.entities[0].audience,'institutional');assert.ok(data.entities[0].facts.length);
 }));
 await check('私人HTTP归属、CAS和金额合同，用户不能从JSON指定owner',()=>withUI(async c=>{
  const requests=[],scope={contract:c.w.ResearchWorkspace.contract,...identity(),workspace_binding:identity().workspace_id,revision:0,records:[]};c.w.fetch=async(url,options)=>{requests.push([url,options]);const body=options.body&&JSON.parse(options.body);const data=options.method==='POST'?{...scope,revision:1,record:{id:'r'.repeat(32),record_revision:1,kind:'research',value:body.value}}:scope;return {ok:true,headers:new Headers(),json:async()=>data};};const api=c.w.ResearchWorkspace.createHttpApi();await api.saveResearch({name:'本人研究',goal:'条款核对',entity_ids:[],conditions:{amount:'1000.01',owner:'forged'}});const request=requests.find(r=>r[1].method==='POST'),body=JSON.parse(request[1].body);assert.equal(body.expected_revision,0);assert.equal(body.value.conditions.amount,'1000.01');assert.equal(body.owner,undefined);assert.equal(body.value.conditions.owner,undefined);assert.equal(request[1].headers['X-Navren-Workspace'],identity().workspace_id);assert.equal(request[1].headers['X-QZ-CSRF'],identity().csrf);
  c.w.fetch=async()=>({ok:true,headers:new Headers(),json:async()=>({...scope,workspace_id:'forged'})});await assert.rejects(()=>api.workspace(),/归属或授权版本不一致/);
 }));
 await check('旧后端能力探针失败安全回退，ensure不返回时仍按期限退出',()=>withUI(async c=>{
  c.w.BankClient.ensure=()=>new Promise(()=>{});const started=Date.now(),ready=await c.w.ResearchWorkspace.available({timeout:35});assert.equal(ready,false);assert.ok(Date.now()-started<300);assert.equal(c.w.ResearchWorkspace.configured,false);
 }));
 await check('启动仅视图变化时丢弃旧响应并用完整新守卫重读一次',()=>withUI(async c=>{
  let version=1,publicReads=0;c.w.NavrenViews={revision:()=>version};c.w.BankClient.guard=()=>{const expected=version,scope=JSON.stringify(c.w.BankClient.identity());return {valid:()=>version===expected&&JSON.stringify(c.w.BankClient.identity())===scope};};
  c.w.fetch=async url=>{let data;if(url.includes('/catalog?')){publicReads++;if(publicReads===1){await new Promise(r=>setTimeout(r,5));version++;}data={contract:c.w.ResearchWorkspace.contract,data_scope:'public',provider_revision:'provider-v1',items:[]};}else data={contract:c.w.ResearchWorkspace.contract,...identity(),workspace_binding:identity().workspace_id,revision:0,records:[],capabilities:{private_persistence:true,public_provider_loaded:true}};return {ok:true,headers:new Headers(),json:async()=>data};};
  assert.equal(await c.w.ResearchWorkspace.available({timeout:500}),true);const status=c.w.ResearchWorkspace.probeStatus;assert.equal(publicReads,2);assert.equal(status.state,'ready');assert.equal(status.attempts[0].code,'RESEARCH_STALE_RESPONSE');assert.equal(status.attempts[0].view_changed,true);assert.equal(status.attempts[0].identity_changed,false);
 }));
 await check('暂不可用不缓存为未支持，立即重试可启用且保留失败诊断',()=>withUI(async c=>{
  let fail=true,count=0;c.w.fetch=async url=>{count++;const data=url.includes('/catalog?')?fail?{error:{code:'RESEARCH_DEPENDENCY_UNAVAILABLE',message:'临时资料不可用'}}:{contract:c.w.ResearchWorkspace.contract,data_scope:'public',provider_revision:'provider-v1',items:[]}:{contract:c.w.ResearchWorkspace.contract,...identity(),workspace_binding:identity().workspace_id,revision:0,records:[],capabilities:{private_persistence:true,public_provider_loaded:true}};return {ok:!(url.includes('/catalog?')&&fail),status:fail?503:200,headers:new Headers(),json:async()=>data};};
  assert.equal(await c.w.ResearchWorkspace.available({timeout:300}),false);assert.equal(c.w.ResearchWorkspace.probeStatus.state,'unavailable');assert.equal(c.w.ResearchWorkspace.probeStatus.failure.http_status,503);const before=count;fail=false;assert.equal(await c.w.ResearchWorkspace.available({timeout:300}),true);assert.ok(count>before);
 }));
 await check('原件适配提交真实CAS及授权头，回读全字节后才关联保单',()=>withUI(async c=>{
  const bytes=Uint8Array.from(Buffer.from('%PDF-1.7 full-original\0\0')),hash=sha(bytes),aid='a'.repeat(32),pid='b'.repeat(32),requests=[];let revision=0;
  const record={id:pid,kind:'policy',record_revision:1,value:{label:'本人原件档案',premium:{amount:'500.00',currency:'CNY'},attachment_ids:[],consent:{purpose:'资料核对',health_data_authorized:true}}};
  c.w.fetch=async(url,options)=>{requests.push([url,options]);const headers=new Headers({'X-Navren-Workspace':identity().workspace_id,'X-Navren-Privacy-Revision':'1','X-Navren-Content-SHA256':hash,'Content-Type':'application/pdf'});if(url.endsWith('/attachments/'+aid))return {ok:true,headers,arrayBuffer:async()=>bytes.buffer};let extra={};if(url.endsWith('/attachments')){revision++;extra.attachment={id:aid,sha256:hash,byte_count:bytes.byteLength,mime_type:'application/pdf'};}else if(url.endsWith('/policies')&&options.method==='POST'){const input=JSON.parse(options.body);assert.equal(input.expected_revision,1);assert.equal(input.value.attachment_ids[0],aid);revision++;extra.record={...record,record_revision:2,value:input.value};}else if(url.includes('/records/'))extra.record=record;else extra={records:[record],attachments:[]};return {ok:true,headers,json:async()=>({contract:c.w.ResearchWorkspace.contract,...identity(),workspace_binding:identity().workspace_id,revision,...extra})};};
  const api=c.w.ResearchWorkspace.createHttpApi();const result=await api.uploadMaterial({file:{type:'application/pdf',arrayBuffer:async()=>bytes.buffer},record_id:pid,record_revision:1,purpose:'policy_review',health_data_authorized:true});const upload=requests.find(r=>r[0].endsWith('/attachments'));assert.equal(upload[1].headers['X-Navren-Expected-Revision'],'0');assert.equal(upload[1].headers['X-Navren-Health-Consent'],'true');assert.equal(upload[1].headers['X-Navren-Confirmed'],'true');assert.equal(result.verified_original,true);assert.equal(result.record.value.attachment_ids[0],aid);assert.equal(result.workspace_revision,2);
 }));
 await check('新增跟进事项实际提交并读回稳定ID，非引用未定义修订变量',()=>withUI(async c=>{
  let saved;c.api.saveTask=async input=>{saved=input;return {record:{id:'a'.repeat(32)}};};await c.ui.showInsurance('tasks');await c.click('添加跟进事项');c.change('事项名称','本人核对发票与材料');c.root.querySelector('[data-task-confirm]').checked=true;await c.click('确认保存事项');assert.equal(saved.value.title,'本人核对发票与材料');assert.ok(c.findButton('返回事项列表'));assert.equal(saved.value.kind,'renewal');
 }));
 await check('已有预留事项编辑保持kind和原操作状态，提醒可独立选择且不再准备',()=>withUI(async c=>{
  let saved;c.api.tasks=async()=>({items:[{id:'r'.repeat(32),workspace_revision:3,operation_links:[{operation_id:'o'.repeat(32),state:'succeeded'}],execution:{status:'succeeded'},value:{title:'本人资金安排',kind:'reserve',status:'ready',due_on:'2026-11-01',record_ids:[],conditions:{account_id:'account-self',amount:'100.00',currency:'CNY'},reminder:{enabled:true,calendar:false,message:true}}}]});c.api.saveTask=async input=>{saved=input;return {record:{id:'r'.repeat(32)}};};await c.ui.showInsurance('tasks');assert.ok(!c.findButton('进入资金预留确认'));await c.click('查看与编辑事项');assert.equal(c.root.querySelector('[aria-label="事项类型"]').value,'reserve');assert.equal(c.root.querySelector('[aria-label="事项类型"]').disabled,true);assert.equal(c.root.querySelector('[aria-label="本人跟进状态"]').disabled,true);c.change('事项名称','更正本人资金安排标题');c.root.querySelector('[data-task-confirm]').checked=true;await c.click('确认保存事项');assert.equal(saved.value.kind,'reserve');assert.equal(saved.value.status,'ready');assert.equal(saved.value.reminder.calendar,false);assert.equal(saved.value.reminder.message,true);assert.equal(saved.value.conditions.amount,'100.00');assert.equal(c.calls.filter(r=>r[0]==='prepareReserve').length,0);
 }));
 await check('服务端身份或隐私撤销立即卸载私人内容，拒绝晚到旧账户显示',()=>withUI(async c=>{
  c.api.workspace=async()=>{throw Object.assign(new Error('授权已撤销'),{status:401,code:'REGISTERED_CONTEXT_REQUIRED'});};await c.ui.showSaved();assert.match(c.root.textContent,/账户已变化/);assert.ok(!c.root.textContent.includes('恢复研究'));assert.equal(c.ui.selection.length,0);
 }));
 console.log(JSON.stringify({source_sha256:sha(source),scope:sourceCompositeOnly?'composite_source_only':reserveSuccessorOnly?'reserve_successor_only':'full_module',passed,model_calls:0,limitation:'禁网DOM与注入API合同；不等于实际入口或真实模型验收。'},null,2));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
