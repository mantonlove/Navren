/* 实际 AccountWorkspace + 最小内存 DOM；金额只取夹具，不调用金融服务。 */
const fs=require('node:fs'),assert=require('node:assert/strict'),crypto=require('node:crypto'),{JSDOM}=require('jsdom');
const source=fs.readFileSync('js/account_workspace.js','utf8'),i18n=fs.readFileSync('js/i18n.js','utf8'),tick=()=>new Promise(r=>setTimeout(r,5));
const results=[];
async function test(name,fn){try{await fn();results.push({name,passed:true});console.log('通过：'+name);}catch(e){results.push({name,passed:false,error:e.message});console.error('失败：'+name+'\n'+e.stack);}}
async function fixture({commitment={},timeline=[],language='zh-CN'}={}){
  const ids=['accountSubview','accountHome','accountViewTitle','accountBack','accountSourceLabel','assetOverview','accountFundsPreview','accountUpcomingPreview','accountFundsAll','accountCalendarLink','accountLegacyViews'];
  const dom=new JSDOM('<body data-page="account"><span class="header-location"></span>'+ids.map(id=>'<section id="'+id+'"></section>').join('')+'</body>',{url:'https://navren.test/account.html',runScripts:'outside-only'}),w=dom.window;
  const state={workspace_id:'isolated-fixture',version:7,as_of:'2026-10-03',commitment_window_end:'2026-11-02',total_assets_principal_minor:0,liabilities_minor:0,investable_minor:null,total_cash_minor:0,holding_principal_minor:0,pending_redemption_minor:0,accounts:[],cards:[],holdings:[],operations:[],orders:[],notice:'只读合同夹具',commitments:[{kind:'subscription',evidence_id:'one',source_table:'subscriptions',name:'用户自定义：现金余额',due_on:'2026-10-10',amount_minor:0,...commitment}],cashflow_timeline:{timeline:[{date:'2026-10-10',events:timeline}]}};
  w.fetch=()=>{throw Error('forbidden_network');};w.Storage={get:()=>language,set(){}};w.STORAGE_KEYS={LANGUAGE:'language'};w.matchMedia=()=>({matches:false,addEventListener(){}});
  w.eval(i18n);await tick();
  const node=(tag,text,cls)=>{const e=w.document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)w.NavrenI18n.setText(e,text);return e;};
  const calls=[];w.BankClient={money:n=>(n/100).toFixed(2)+' 元',get:async p=>{calls.push(p);assert.equal(p,'/financial-records');return {workspace_id:state.workspace_id,version:state.version,records:{}};}};
  w.BankUI={node,state:()=>state,products:()=>[],button:(text,fn)=>{const e=node('button',text);e.onclick=fn;return e;},operationStatus:op=>op.state,actions:{}};
  w.NavrenViews={register(){},current:()=>({view:'home'}),open(){},back(){}};
  w.eval(source);w.document.dispatchEvent(new w.Event('DOMContentLoaded'));await w.AccountWorkspace.render({view:'home'});await tick();
  return {dom,w,state,calls,row:()=>w.document.querySelector('#accountUpcomingPreview .account-object'),data:()=>w.AccountWorkspace.arrangements()[0],close:()=>w.close()};
}
async function withFixture(options,fn){const e=await fixture(options);try{await fn(e);}finally{e.close();}}
function assertLabel(e,label){assert.equal(e.data().status,label);assert(e.row());assert.equal(e.row().querySelector('.account-object-amount small').textContent,label);assert.equal(e.row().querySelector('.account-object-amount strong').textContent,'0.00 元','已知零金额仍显示，不另算');assert(e.calls.every(p=>p==='/financial-records'));}
(async()=>{
  await test('commitment明确candidate显示候选安排',()=>withFixture({commitment:{certainty:'candidate'}},async e=>assertLabel(e,'候选安排')));
  await test('commitment明确confirmed显示确认安排',()=>withFixture({commitment:{certainty:'confirmed'}},async e=>assertLabel(e,'确认安排')));
  await test('certainty缺失保持待核实，不能提升为确认',()=>withFixture({},async e=>assertLabel(e,'安排确定性待核实')));
  await test('certainty显式unknown保持待核实',()=>withFixture({commitment:{certainty:'unknown'}},async e=>assertLabel(e,'安排确定性待核实')));
  await test('同evidence同source的timeline可补candidate标签',()=>withFixture({timeline:[{evidence_id:'one',source_table:'subscriptions',certainty:'candidate'}]},async e=>assertLabel(e,'候选安排')));
  await test('同evidence同source的timeline可补confirmed标签',()=>withFixture({timeline:[{evidence_id:'one',source_table:'subscriptions',certainty:'confirmed'}]},async e=>assertLabel(e,'确认安排')));
  await test('commitment与timeline的candidate/confirmed冲突保持待核实',()=>withFixture({commitment:{certainty:'candidate'},timeline:[{evidence_id:'one',source_table:'subscriptions',certainty:'confirmed'}]},async e=>assertLabel(e,'安排确定性待核实')));
  await test('多个timeline确定性冲突保持待核实',()=>withFixture({timeline:[{evidence_id:'one',source_table:'subscriptions',certainty:'candidate'},{evidence_id:'one',source_table:'subscriptions',certainty:'confirmed'}]},async e=>assertLabel(e,'安排确定性待核实')));
  await test('其它来源/其它evidence不能给当前安排提升确定性',()=>withFixture({timeline:[{evidence_id:'one',source_table:'mandates',certainty:'confirmed'},{evidence_id:'other',source_table:'subscriptions',certainty:'confirmed'}]},async e=>assertLabel(e,'安排确定性待核实')));
  await test('来源缺失或空白不能借其它来源的同ID确认安排',async()=>{
    for(const source_table of [undefined,null,'',' '])await withFixture({commitment:{source_table},timeline:[{evidence_id:'one',source_table:'mandates',certainty:'confirmed'}]},async e=>assertLabel(e,'安排确定性待核实'));
  });
  await test('未知确定性的过往日期不能标为已确认逾期',async()=>{
    for(const certainty of [undefined,'unknown'])await withFixture({commitment:{certainty,due_on:'2026-10-02'}},async e=>assertLabel(e,'安排确定性待核实'));
  });
  await test('candidate日期已过只说候选日期已过，不当成已确认逾期',()=>withFixture({commitment:{certainty:'candidate',due_on:'2026-10-02'}},async e=>assertLabel(e,'候选日期已过，待核实')));
  await test('confirmed日期已过标逾期待核查',()=>withFixture({commitment:{certainty:'confirmed',due_on:'2026-10-02'}},async e=>assertLabel(e,'逾期待核查')));
  await test('due等于参考日不提前标逾期',()=>withFixture({commitment:{certainty:'confirmed',due_on:'2026-10-03'}},async e=>assertLabel(e,'确认安排')));
  await test('candidate英文标签/日期注释完整且中英往返保留用户标题',()=>withFixture({language:'en',commitment:{certainty:'candidate'}},async e=>{
    assert.equal(e.row().querySelector('.account-object-amount small').textContent,'Candidate arrangement');assert.equal(e.row().querySelector('.account-object-identity small').textContent,'2026-10-10 · Candidate arrangement');assert.equal(e.row().querySelector('.account-object-identity strong').textContent,'用户自定义：现金余额');e.w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(e.row().querySelector('.account-object-amount small').textContent,'候选安排');e.w.NavrenI18n.setLanguage('en',false);assert.equal(e.row().querySelector('.account-object-amount small').textContent,'Candidate arrangement');
  }));
  await test('逾期英文标签与日期注释一致且往返不改原日期',()=>withFixture({language:'en',commitment:{certainty:'confirmed',due_on:'2026-10-02'}},async e=>{
    assert.equal(e.row().querySelector('.account-object-amount small').textContent,'Overdue; verification needed');assert.equal(e.row().querySelector('.account-object-identity small').textContent,'2026-10-02 · Overdue; verification needed');e.w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(e.row().querySelector('.account-object-identity small').textContent,'2026-10-02 · 逾期待核查');
  }));
  await test('confirmed、unknown与冲突标签英文完整且不把冲突说成确认',async()=>{
    for(const [options,expected]of [[{commitment:{certainty:'confirmed'}},'Confirmed arrangement'],[{commitment:{certainty:'unknown'}},'Arrangement certainty is unverified'],[{commitment:{certainty:'candidate'},timeline:[{evidence_id:'one',source_table:'subscriptions',certainty:'confirmed'}]},'Arrangement certainty is unverified']])await withFixture({...options,language:'en'},async e=>{assert.equal(e.row().querySelector('.account-object-amount small').textContent,expected);assert.equal(e.row().querySelector('.account-object-identity small').textContent,'2026-10-10 · '+expected);e.w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(e.row().querySelector('.account-object-amount small').textContent,e.data().status);});
  });
  const passed=results.filter(r=>r.passed).length;console.log(JSON.stringify({passed,total:results.length,network:'forbidden',source_sha256:crypto.createHash('sha256').update(source).digest('hex'),i18n_sha256:crypto.createHash('sha256').update(i18n).digest('hex'),failures:results.filter(r=>!r.passed)},null,2));if(passed!==results.length)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1;});
