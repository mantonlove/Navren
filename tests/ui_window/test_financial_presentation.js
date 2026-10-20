/* 真实历史形状的合成 DOM 验收；0 网络、0 模型 API、0 现役服务或数据库。 */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),{JSDOM}=require('jsdom');
const project=path.resolve(__dirname,'../..');
const inputDir=path.join(project,'outputs/window-progress/agent/baseline-remediation-20261014/contract-tests/final-HTTP-after-cap');
const outputDir=path.join(project,'outputs/window-progress/ui/financial-presentation-20261014/dom-acceptance');
const sourceFile=path.join(project,'js/calendar_ui.js'),code=fs.readFileSync(sourceFile,'utf8');
const bodyFile=path.join(inputDir,'HTTP-response.json'),historyFile=path.join(inputDir,'approved-history-readback.json');
const originalBody=JSON.parse(fs.readFileSync(bodyFile,'utf8')).body,originalHistory=JSON.parse(fs.readFileSync(historyFile,'utf8'));
fs.mkdirSync(outputDir,{recursive:true});
const copy=x=>structuredClone(x),tick=()=>new Promise(r=>setTimeout(r,0));
const sha=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const results=[];let networkAttempts=0,businessWrites=0;
function pair(){const body=copy(originalBody),history=copy(originalHistory);return {body,history,widget:body.widgets[0]};}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function updatePresentation(fixture,mutate){
  const p=copy(fixture.widget.presentation);mutate(p);
  function replace(value){
    if(!value||typeof value!=='object')return;
    for(const key of Object.keys(value)){
      if(value[key]?.contract==='navren.presentation/1')value[key]=copy(p);
      else replace(value[key]);
    }
  }
  replace(fixture.body);replace(fixture.history);fixture.widget=fixture.body.widgets[0];
  return p;
}
function updateDisplaySlot(fixture,index,patch){
  function walk(value){if(!value||typeof value!=='object')return;
    for(const [key,v] of Object.entries(value)){
      if(key==='display_amount_occurrences'&&Array.isArray(v))Object.assign(v[index],patch);
      else walk(v);
    }
  }walk(fixture.history);
}
function amountNodes(root){return [...root.querySelectorAll('[data-financial-amount]')];}
async function settle(){for(let i=0;i<4;i++)await tick();}
async function setup(fixture=pair(),options={}){
  const dom=new JSDOM('<!doctype html><html><body><main id="root"></main><article class="message ai" id="message"><div class="bubble"></div></article></body></html>',{url:'https://navren.invalid/chat.html',runScripts:'outside-only'}),w=dom.window;
  // 先完成 DOMContentLoaded，避免首页自动读取历史干扰固定 consumer 入口。
  await tick();w.document.body.dataset.page='chat';
  let identity={workspace_id:originalHistory.workspace_id,privacy_revision:originalHistory.privacy_revision,personal_revision:originalBody.personal_revision},generation=0,viewRevision=0;
  if(options.identity!==undefined)identity=options.identity;
  const calls={ensure:0,get:[],post:[],fetch:0};
  w.fetch=()=>{calls.fetch++;networkAttempts++;throw Error('隔离验收禁止网络');};
  w.NavrenI18n={language:options.language||'zh',locale:'zh-CN',t:x=>x};
  w.NavrenViews={revision:()=>viewRevision};w.NavrenHelp={button:()=>w.document.createElement('span')};
  w.BankClient={identity:()=>identity?copy(identity):null,guard(){const captured=copy(identity),g=generation;return {valid:()=>generation===g&&identity?.workspace_id===captured?.workspace_id&&identity?.privacy_revision===captured?.privacy_revision};},ensure:async()=>{calls.ensure++;return identity?copy(identity):null;},get:async endpoint=>{calls.get.push(endpoint);const id=encodeURIComponent(fixture.widget.presentation.message_id);if(endpoint!=='/approved-responses/'+id+'?language='+(options.language==='en'?'en':'zh'))throw Error('非预期只读接口：'+endpoint);return copy(fixture.history);},post:async endpoint=>{calls.post.push(endpoint);businessWrites++;throw Error('隔离验收禁止业务写入');}};
  w.eval(code);
  const root=w.document.getElementById('root'),message=w.document.getElementById('message'),bubble=message.querySelector('.bubble');
  message.dataset.bankResponseId=fixture.body.bank_response_id;bubble.textContent=fixture.body.reply;
  return {dom,w,root,message,bubble,calls,fixture,
    async mount(widget=fixture.widget){await w.CalendarUI.mount(root,copy(widget));return root;},
    async response(widgets=fixture.body.widgets){w.CalendarUI.response(message,copy(widgets));await settle();return bubble;},
    changeIdentity(next={workspace_id:'synthetic-other',privacy_revision:1,personal_revision:0},event=false){identity=next;generation++;if(event)w.dispatchEvent(new w.Event('navren:privacy-reset'));},
    changeView(){viewRevision++;},
    close(){w.close();}
  };
}
async function using(fixture,fn,options={}){const e=await setup(fixture,options);try{await fn(e);}finally{e.close();}}
async function test(id,name,fn){try{await fn();results.push({id,name,status:'passed'});console.log('通过 '+id+'：'+name);}catch(error){results.push({id,name,status:'failed',error:error.stack});console.error('失败 '+id+'：'+name+'\n'+error.message);}}
function assertRejected(root,reason){assert.equal(amountNodes(root).length,0,reason||'拒绝时不得残留金额槽');assert.equal(root.__navrenFinancialPacket,undefined,'拒绝时不得保留原始资金数据');assert.equal(root.querySelectorAll('button,a,input,select').length,0,'失败结果不得给出业务操作');}
function assertSixSlots(root){const amounts=amountNodes(root);assert.equal(amounts.length,6);assert.deepEqual(amounts.map(n=>n.dataset.financialAmount),['2439000','1439000','1186600','1000000','null','null']);assert.deepEqual(amounts.map(n=>n.textContent),['24,390.00元','14,390.00元','11,866.00元','10,000.00元','未提供合计','未提供合计']);}
function assertMetadata(root,p,batch){
  const basis=[...root.querySelectorAll('details')].map(n=>n.textContent).join('\n');
  for(const m of batch.metrics){
    const card=root.querySelector('[data-financial-slot="'+p.points.find(x=>x.value.metric_id===m.metric_id).block+'"]');assert(card,'每个签发指标有卡片');
    for(const v of [m.metric_id,m.subject_id,m.currency,m.data_scope,m.calculation_status,m.basis,m.formula_id,...m.input_refs,...m.assumed_inputs,...m.excluded_items,...m.coverage_gaps])assert(card.textContent.includes(v),'卡片保留指标元数据：'+v);
    for(const [key,value] of Object.entries(m.source_versions)){assert(card.textContent.includes(key),'来源版本键保留：'+key);assert(card.textContent.includes(value),'来源版本值保留：'+key);}
  }
  for(const [key,value] of Object.entries(p.source_versions)){assert(basis.includes(key));assert(basis.includes(value));}
  for(const v of [p.workspace_binding,p.request_id,p.message_id,p.as_of,p.timezone,p.clock_basis,p.measure_basis,p.coverage,p.scope.funding_query.coverage_scope,p.funding_reference.snapshot_id,p.funding_reference.snapshot_hash])assert(root.textContent.includes(v),'总视图元数据保留：'+v);
}
async function negative(id,name,mutate,options={}){await test(id,name,async()=>{const f=pair();mutate(f);await using(f,async e=>{await e.mount();assertRejected(e.root);},options);});}
(async()=>{
  assert.equal(originalBody.widgets.length,1);assert.equal(originalBody.widgets[0].type,'financial_presentation');
  await test('FP-001','实际新格式经正式 approved-responses 读回呈现全部 6 槽、保留 null 和来源',async()=>using(pair(),async e=>{
    await e.mount();assertSixSlots(e.root);assertMetadata(e.root,e.fixture.widget.presentation,e.fixture.history.response.metric_batch);
    assert(e.root.classList.contains('financial-presentation-widget'),'新格式有独立样式范围');
    const visibleCard=e.root.cloneNode(true);visibleCard.querySelectorAll('details').forEach(n=>n.remove());
    for(const point of e.fixture.widget.presentation.points)for(const missing of point.missing)assert(!visibleCard.textContent.includes(missing),'机器缺项代码只保留在来源折叠中');
    assert(visibleCard.textContent.includes('输入尚未完整覆盖，具体缺项见来源依据。'));
    assert.equal(e.calls.ensure,1);assert.deepEqual(e.calls.get,['/approved-responses/'+originalBody.bank_response_id+'?language=zh']);assert.equal(e.root.querySelectorAll('button,a,input,select,svg,canvas').length,0,'只读块不制造交易动作、比例或图形');
    assert(amountNodes(e.root).filter(n=>n.dataset.financialAmount==='null').every(n=>n.textContent==='未提供合计'),'来源未签发的 null 合计不渲染为 0');assert(e.root.textContent.includes('不能按事项数量推算'));assert(e.root.textContent.includes('未评估日内偿付能力'));assert(e.root.textContent.includes('不能据此认定应急储备充足'));
    fs.mkdirSync(outputDir,{recursive:true});fs.writeFileSync(path.join(outputDir,'positive-dom.html'),e.root.outerHTML);
  }));
  await test('FP-002','实际 response 入口保留 2 正文＋6展示槽，不重复正文、不增加金额',async()=>using(pair(),async e=>{
    await e.response();const component=e.bubble.querySelector('.calendar-widget');assert(component);assertSixSlots(component);
    const textBefore=[...e.bubble.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('');assert.equal(textBefore,e.fixture.body.reply);
    assert.equal((textBefore.match(/24390元/g)||[]).length,2);assert.equal(e.bubble.querySelectorAll('[data-financial-amount]').length,6);
    assert.equal(e.fixture.history.original_agent_final_proof.body_amount_occurrence_count,2);assert.equal(e.fixture.history.original_agent_final_proof.total_amount_occurrence_count,8);
    fs.writeFileSync(path.join(outputDir,'response-dom.html'),e.message.outerHTML);
  }));
  await test('FP-003','原生 null 合计与真实计算零值区分',async()=>{const f=pair();const index=3,m=f.history.response.metric_batch.metrics[index];m.value_minor=0;
    updatePresentation(f,p=>p.points[index].value.value_minor=0);updateDisplaySlot(f,index,{value_minor:0});await using(f,async e=>{await e.mount();const n=amountNodes(e.root);assert.equal(n.length,6);assert.equal(n[index].textContent,'0.00元');assert.equal(n[4].textContent,'未提供合计');assert.equal(n[5].textContent,'未提供合计');});
  });
  await test('FP-004','额外输入引用、条件、排除项和缺项完整保留，不当成金额',async()=>{const f=pair(),m=f.history.response.metric_batch.metrics[2];m.assumed_inputs=['仅用于已记录事项范围，不承诺未来资金'];m.excluded_items=['未来工资不预支'];m.coverage_gaps=['其他未记录缴费尚未覆盖'];m.input_refs.push('synthetic/input-extra');await using(f,async e=>{await e.mount();assertSixSlots(e.root);assertMetadata(e.root,f.widget.presentation,f.history.response.metric_batch);});});
  await test('FP-005','历史只读快照保留原数据，同时明确历史只读状态',async()=>{const f=pair();f.history.status='historical_readonly';await using(f,async e=>{await e.mount();assertSixSlots(e.root);assert(e.root.textContent.includes('历史已发布快照'));assert.equal(e.root.querySelectorAll('button,a').length,0);});});
  await negative('FP-006','其他账户的 widget 不读取私人原件',f=>{f.widget.presentation.workspace_binding='synthetic-other';});
  await negative('FP-007','widget 授权版本过期拒绝展示',f=>{f.widget.presentation.authorization_revision.privacy++;});
  await negative('FP-008','当前资料版本与 widget 不一致拒绝展示',f=>{f.widget.presentation.authorization_revision.personal++;});
  await negative('FP-009','readback 账户不匹配拒绝展示',f=>{f.history.workspace_id='synthetic-other';});
  await negative('FP-010','readback 隐私版本不匹配拒绝展示',f=>{f.history.privacy_revision++;});
  await negative('FP-011','widget view 与 widget_id 不一致拒绝展示',f=>{f.widget.widget_id='wrong-view';});
  await negative('FP-012','widget request/message 错配拒绝展示',f=>{f.widget.presentation.request_id='wrong-request';});
  await negative('FP-013','正式 readback message 与 widget 错配拒绝展示',f=>{f.history.response.bank_response_id='wrong-message';});
  await negative('FP-014','本地 widget 金额篡改不能代替正式原件',f=>{f.widget.presentation.points[0].value.value_minor++;});
  await negative('FP-015','本地 widget 标签或条件篡改不能代替正式原件',f=>{f.widget.presentation.important_states=['保证可以随时支取'];});
  await negative('FP-016','已过期 readback 不以历史标记继续展示',f=>{f.history.status='expired';});
  for(const [id,key]of [['FP-017','original_message_verified'],['FP-018','identity_verified'],['FP-019','language_verified'],['FP-020','source_versions_verified']])await negative(id,'正式原件核验字段 '+key+' 缺失时拒绝',f=>{delete f.history.verification[key];});
  await negative('FP-021','语义审核仍候选时拒绝展示',f=>{f.history.verification.semantic_review_status='candidate';});
  await negative('FP-022','最终守卫未通过时拒绝展示',f=>{f.history.verification.final_guard_status='pending';});
  await negative('FP-023','只读接口出现执行权限时拒绝展示',f=>{f.history.execution_authority=true;});
  await negative('FP-024','旧批准允许重新签发时拒绝展示',f=>{f.history.can_reissue_old_approval=true;});
  await negative('FP-025','终审裁决拒绝时不消费金额',f=>{f.history.original_agent_final_proof.verdict.verdict='reject';});
  await negative('FP-026','终审证明消息绑定错配时拒绝展示',f=>{f.history.original_agent_final_proof.message_id='wrong-message';});
  await negative('FP-027','终审证明正式响应哈希错配时拒绝展示',f=>{f.history.original_agent_final_proof.approved_response_hash='wrong-hash';});
  await negative('FP-028','发布回执尚未发送完成时拒绝展示',f=>{f.history.original_publication_receipt.publication_state='prepared';});
  await negative('FP-029','来源版本缺失时拒绝展示',f=>{delete f.history.response.metric_batch.source_versions.presentation_code;});
  await negative('FP-030','来源版本冲突时拒绝展示',f=>{f.history.response.metric_batch.source_versions.bank='wrong-version';});
  await negative('FP-031','单个指标的来源版本冲突不可被自身覆盖伪装通过',f=>{f.history.response.metric_batch.metrics[0].source_versions.bank='wrong-version';});
  await negative('FP-032','缺少一个签发指标拒绝六块展示',f=>{f.history.response.metric_batch.metrics.pop();});
  await negative('FP-033','重复指标 ID 拒绝展示',f=>{f.history.response.metric_batch.metrics[1].metric_id=f.history.response.metric_batch.metrics[0].metric_id;});
  await negative('FP-034','指标主体错配拒绝展示',f=>{f.history.response.metric_batch.metrics[1].subject_id='synthetic-other';});
  await negative('FP-035','指标币种与已批准槽位不同拒绝展示',f=>{f.history.response.metric_batch.metrics[1].currency='USD';});
  await negative('FP-036','指标金额与已批准槽位不同拒绝展示',f=>{f.history.response.metric_batch.metrics[1].value_minor++;});
  await negative('FP-037','缺失合计伪装成 calculated 拒绝展示',f=>{f.history.response.metric_batch.metrics[4].calculation_status='calculated';});
  await negative('FP-038','没有输入引用结构的指标拒绝展示',f=>{delete f.history.response.metric_batch.metrics[0].input_refs;});
  await negative('FP-039','没有条件与缺项结构的指标拒绝展示',f=>{delete f.history.response.metric_batch.metrics[2].assumed_inputs;});
  await negative('FP-040','终审展示槽位缺一项拒绝展示',f=>{f.history.original_agent_final_proof.approved_review_projection.presentation_review.display_amount_occurrences.pop();});
  await negative('FP-041','终审展示槽位路径错配拒绝展示',f=>{f.history.original_agent_final_proof.approved_review_projection.presentation_review.display_amount_occurrences[1].path='points/2/value';});
  await negative('FP-042','终审正文金额计数不符拒绝展示',f=>{f.history.original_agent_final_proof.body_amount_occurrence_count=1;});
  await negative('FP-043','终审总金额计数不符拒绝展示',f=>{f.history.verification.total_amount_occurrence_count=7;});
  await negative('FP-044','正文金额声明位置错配拒绝展示',f=>{f.history.original_agent_final_proof.approved_review_projection.presentation_review.body_amount_contract.amount_claims[0].claim.occurrence_start++;});
  await negative('FP-045','正文被改写但证明保持原文时拒绝展示',f=>{f.history.response.reply='本地伪造正文 999999元';});
  await negative('FP-046','六块重复或少块即使原件一致也拒绝展示',f=>{updatePresentation(f,p=>p.points[1].block=p.points[0].block);});
  await negative('FP-047','未来新增的不支持布局明确拒绝，不假装旧日历成功',f=>{updatePresentation(f,p=>p.layout='pie');});
  await negative('FP-048','输入金额不为安全整数即使各处一致也拒绝展示',f=>{const bad=Number.MAX_SAFE_INTEGER+1;f.history.response.metric_batch.metrics[1].value_minor=bad;updatePresentation(f,p=>p.points[1].value.value_minor=bad);updateDisplaySlot(f,1,{value_minor:bad});});
  await negative('FP-049','币种不是三位货币代码时各处一致仍拒绝',f=>{for(const m of f.history.response.metric_batch.metrics)m.currency='123';updatePresentation(f,p=>{p.scope.currency='123';p.points.forEach(x=>x.value.currency='123');});for(let i=0;i<6;i++)updateDisplaySlot(f,i,{currency:'123'});const claims=f.history.original_agent_final_proof.approved_review_projection.presentation_review.body_amount_contract.amount_claims;for(const c of claims)c.claim.currency='123';});
  await test('FP-050','readback 读取失败不展示浏览器缓存中的金额',async()=>using(pair(),async e=>{e.w.BankClient.get=async()=>{throw Error('原件不可用');};await e.mount();assertRejected(e.root);assert(e.root.textContent.includes('原件不可用'));}));
  await test('FP-051','ensure 等待期间换身份不复活旧金额',async()=>using(pair(),async e=>{const d=deferred();e.w.BankClient.ensure=()=>d.promise;const waiting=e.mount();e.changeIdentity();d.resolve({workspace_id:originalHistory.workspace_id,privacy_revision:0,personal_revision:0});await waiting;assertRejected(e.root);assert.equal(e.calls.get.length,0);}));
  await test('FP-052','GET 等待期间无事件换身份也阻断迟到原件',async()=>using(pair(),async e=>{const d=deferred();e.w.BankClient.get=()=>d.promise;const waiting=e.mount();await settle();e.changeIdentity();d.resolve(copy(e.fixture.history));await waiting;assertRejected(e.root);}));
  await test('FP-053','GET 等待期间切换视图阻断迟到原件',async()=>using(pair(),async e=>{const d=deferred();e.w.BankClient.get=()=>d.promise;const waiting=e.mount();await settle();e.changeView();d.resolve(copy(e.fixture.history));await waiting;assertRejected(e.root);}));
  await test('FP-054','GET 等待期间容器脱离 DOM 不插入旧数据',async()=>using(pair(),async e=>{const d=deferred();e.w.BankClient.get=()=>d.promise;const waiting=e.mount();await settle();e.root.remove();d.resolve(copy(e.fixture.history));await waiting;assertRejected(e.root);}));
  await test('FP-055','同一容器最新请求优先，旧原件不能覆盖新拒绝结果',async()=>using(pair(),async e=>{const d=deferred();let reads=0;e.w.BankClient.get=()=>++reads===1?d.promise:Promise.reject(Error('较新请求失败'));const first=e.mount();await settle();await e.mount();const current=e.root.textContent;d.resolve(copy(e.fixture.history));await first;assertRejected(e.root);assert.equal(e.root.textContent,current);}));
  await test('FP-056','已经渲染的旧账户值在隐私重置时清除',async()=>using(pair(),async e=>{await e.mount();assertSixSlots(e.root);e.changeIdentity(undefined,true);assertRejected(e.root);}));
  await test('FP-057','重复 response 调用仅保留一次资金组件',async()=>using(pair(),async e=>{await e.response();await e.response();assert.equal(e.bubble.querySelectorAll('.calendar-widget').length,1);assert.equal(amountNodes(e.bubble).length,6);const text=[...e.bubble.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('');assert.equal(text,e.fixture.body.reply);}));
  await test('FP-058','response消息ID与widget不一致不读取原件',async()=>using(pair(),async e=>{e.message.dataset.bankResponseId='other-message';await e.response();assert.equal(e.calls.get.length,0);assertRejected(e.bubble.querySelector('.calendar-widget'));}));
  await test('FP-059','本地正文篡改不能和官方资金值槽组合展示',async()=>using(pair(),async e=>{e.bubble.textContent='伪造本地正文：9000元';await e.response();assertRejected(e.bubble.querySelector('.calendar-widget'));}));
  await test('FP-060','标签、来源引用包含 HTML 时只呈现文本，不插入元素',async()=>{const f=pair();updatePresentation(f,p=>{p.title='<img src=x onerror=bad()>资金拆解';p.points[0].label='<script>bad()</script>已结算现金';});f.history.response.metric_batch.metrics[0].input_refs.push('<img src=x onerror=bad()>');await using(f,async e=>{await e.mount();assertSixSlots(e.root);assert.equal(e.root.querySelector('img,script'),null);assert(e.root.textContent.includes('<script>bad()</script>'));});});
  const report={contract:'navren.financial-presentation.dom-acceptance/1',verified_at:new Date().toISOString(),scope:'Actual calendar_ui.js in isolated jsdom; immutable actual HTTP/history shapes plus explicitly mutated synthetic negative controls. No live HTTP, browser screen, database, model API, or production restart.',passed:results.filter(r=>r.status==='passed').length,failed:results.filter(r=>r.status==='failed').length,cases:results,source:{path:path.relative(project,sourceFile),sha256:sha(sourceFile)},styles:[{path:'css/bank.css',sha256:sha(path.join(project,'css/bank.css')),visual_verification:false}],fixtures:[bodyFile,historyFile].map(file=>({path:path.relative(project,file),sha256:sha(file)})),network_attempts:networkAttempts,business_writes:businessWrites,model_api_calls:0,limitations:['Readback and BankClient boundaries are explicit in-memory doubles; no real final reviewer semantics or current-entry acceptance is claimed.','Positive approved evidence comes from preset-model temporary HTTP output; it is not actual GLM semantic acceptance.','jsdom cannot verify visual rendering, pointer behavior, or responsive screen geometry. CSS hash records the associated source only; layout acceptance requires the separately owned isolated screen check.']};
  fs.mkdirSync(outputDir,{recursive:true});fs.writeFileSync(path.join(outputDir,'validation.json'),JSON.stringify(report,null,2)+'\n');
  console.log('资金展示隔离 DOM：'+report.passed+'通过，'+report.failed+'失败；网络'+networkAttempts+'，业务写入'+businessWrites+'，模型 API 0。');
  assert.equal(networkAttempts,0);assert.equal(businessWrites,0);if(report.failed)process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
