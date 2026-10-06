/* 真实聊天/API/任务组件/银行组件源代码的离线接口验收；并非现役Agent投影或服务验收。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const project=path.resolve(__dirname,'../..'),source=file=>fs.readFileSync(path.join(project,file),'utf8'),tick=()=>new Promise(r=>setTimeout(r,0));
const clone=value=>JSON.parse(JSON.stringify(value)),deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
function session(id='A',privacy_revision=1){return {workspace_id:id,privacy_revision,csrf:'fixture-only-csrf-'+id};}
function response(data,binding=data){return {ok:true,status:200,headers:{get:key=>key==='X-Navren-Workspace'?binding.workspace_id:key==='X-Navren-Privacy-Revision'?String(binding.privacy_revision):null},json:async()=>clone(data)};}
function streamed(data){let i=0;const bytes=new TextEncoder().encode('data: '+JSON.stringify({type:'CUSTOM',name:'navren.result',value:data})+'\n\n');return {ok:true,status:200,body:{getReader:()=>({read:async()=>i++===0?{value:bytes,done:false}:{done:true},cancel:async()=>{}})}};}
function reference(binding,kind='financial_metric',id='metric-cash',scope='finance_service',version=7){return {kind,id,source_scope:scope,version,workspace_id:binding.workspace_id,privacy_revision:binding.privacy_revision};}
function projection(binding,{kind='analysis',version=7}={}){
 const p={contract_version:'navren.task-result.v1',result_id:'result-'+binding.workspace_id,result_version:1,result_kind:kind,subject_id:'customer-'+binding.workspace_id,workspace_id:binding.workspace_id,privacy_revision:binding.privacy_revision,source_scope:'finance_service',source_version:7,conclusion:{zh:'基座已核验的分析结论',en:'Verified basis conclusion'},conditions:[{zh:'仅含已记录事项，不把未来工资当作余额'}],metrics:[],evidence:[],business_refs:[],actions:[]};
 if(kind==='analysis')p.metrics=[{metric_id:'cash-'+binding.workspace_id,subject_id:p.subject_id,workspace_id:p.workspace_id,privacy_revision:p.privacy_revision,metric_name:'已记录余额',currency:'CNY',unit:'minor',minor_unit_scale:2,value_minor:12345,as_of:'2026-10-06',data_scope:'settled_bank_records',calculation_status:'calculated',basis:{kind:'settled_balance',label:'已结算账本余额',included:['已结算现金'],deducted:[]},formula_id:'settled_balance.v1',input_refs:[reference(binding,'account','account-1','bank_ledger')],source_versions:[reference(binding,'account','account-1','bank_ledger')],excluded_items:[],coverage_gaps:[],assumed_inputs:[],source_scope:'finance_service',source_version:7}];
 else{const ref=reference(binding,kind==='operation'?'operation':'receipt','op-existing','bank_operation',version);p.business_refs=[ref];p.actions=[{action_id:'business',type:kind==='operation'?'review_operation':'view_receipt',reference:clone(ref)}];}return p;
}
async function setup(savedHistory=[]){
 const html=source('chat.html').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,''),dom=new JSDOM(html,{url:'https://navren.test/chat.html',runScripts:'outside-only'}),w=dom.window,d=w.document;await tick();
 let cookie=session(),agentData=null,agentHandler=null,contextHandler=null,operationHandler=null,operationState='pending_confirmation',operationVersion=7,init;const calls=[],sequence=[],navigation=[],refs=[],store=new Map([['qingzhou_chatHistory',clone(savedHistory)],['qingzhou_privacyRead',true],['qingzhou_voiceGuideRead',true]]);
 w.TextDecoder=TextDecoder;w.TextEncoder=TextEncoder;w.structuredClone=structuredClone;w.API_CONFIG={timeout:3000,rateLimitDelay:0};w.MODE_DEFAULT_FONT={classic:'medium'};
 w.Storage={get:key=>store.has(key)?clone(store.get(key)):null,set:(key,value)=>store.set(key,clone(value)),addKeyMoment:()=>{},getKeyMoments:()=>[]};
 w.eval(source('js/storage.js').slice(source('js/storage.js').indexOf('function escapeHtml')));
 w.NavrenSpeech={stop(){},activeKind:null};w.VoiceMessages={restore(){}};w.getInterfaceMode=()=> 'classic';w.getModeFont=()=>null;w.applyFontSize=()=>{};w.showContextualTip=()=>{};w.showToast=()=>{};
 w.NavrenApp={navigate:url=>navigation.push(url)};w.NavrenViews={revision:()=>0,reference:ref=>{refs.push(clone(ref));return Promise.resolve();},url:()=>'/chat.html'};
 w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');this.dispatchEvent(new w.Event('close'));};
 function operationView(binding){return {...binding,operation:{id:'op-existing',version:operationVersion,state:operationState,action:'transfer',payload:{amount_minor:12345},confirmation_digest:'fixture-digest'},account:{name:'账户-'+binding.workspace_id},beneficiary:{display_name:'收款人-'+binding.workspace_id},liquidity_floor_minor:0,verification_required:false};}
 w.fetch=async (url,options={})=>{
   assert.equal(typeof url,'string');assert(url.startsWith('/api/'),'禁止真实网络');const bound={...cookie};calls.push({url,method:options.method||'GET',options,binding:bound});sequence.push('fetch:'+url);
   if(url==='/api/finance/session')return response(bound);
   if(url==='/api/finance/page-contexts')return contextHandler?contextHandler({bound,options}):response({...bound,context_id:'context-'+bound.workspace_id});
   if(url==='/api/agent'){assert.equal(options.method,'POST');assert.equal(options.headers.Accept,'text/event-stream');return agentHandler?agentHandler({bound,options}):streamed(agentData);}
   if(url==='/api/finance/operations/op-existing'&&(options.method||'GET')==='GET')return operationHandler?operationHandler({bound,options,view:operationView(bound)}):response(operationView(bound),bound);
   throw Error('未登记接口：'+(options.method||'GET')+' '+url);
 };
 for(const file of ['assets/vendor/chat-markdown/marked.umd.js','assets/vendor/chat-markdown/purify.min.js','js/chat_markdown.js','js/bank_client.js','js/task_components.js','js/task_chat.js','js/bank_ui.js','js/api.js'])w.eval(source(file)+(file==='js/api.js'?'\nwindow.Api=Api;':'')); // eval的lexical导出桥；生产脚本正文保持原样。
 const originalAdd=d.addEventListener.bind(d);d.addEventListener=(type,callback,...args)=>{if(type==='DOMContentLoaded'){init=callback;return;}return originalAdd(type,callback,...args);};w.eval(source('js/chat.js'));d.addEventListener=originalAdd;
 const begin=w.NavrenTaskChat.begin;w.NavrenTaskChat.begin=()=>{sequence.push('begin');return begin();};await w.BankClient.ensure();
 return {dom,w,d,calls,sequence,navigation,refs,store,identity:()=>w.BankClient.identity(),setAgent(data){agentData=data;agentHandler=null;},setAgentHandler(fn){agentHandler=fn;},setContextHandler(fn){contextHandler=fn;},setOperationHandler(fn){operationHandler=fn;},setOperation({state='pending_confirmation',version=7}={}){operationState=state;operationVersion=version;operationHandler=null;},switchAccount(id='B',revision=2){cookie=session(id,revision);w.BankClient.acceptSelectedSession(cookie);},async send(data){this.setAgent(data);d.getElementById('userInput').value='核对这次任务';await w.sendMessage();return d.querySelector('.message.ai:last-child');},async initialize(){await init();},close(){w.NavrenTaskComponents.invalidate();w.close();}};
}
const results=[];async function test(name,run){let env;try{env=await setup();await run(env);results.push({name,passed:true});console.log('通过：'+name);}catch(error){results.push({name,passed:false,error:error.message});console.error('失败：'+name+'\n'+error.stack);}finally{env?.close();}}
const oldActions=[{label:'不得回退执行的旧草案',draft:{action:'transfer',amount_minor:99999}}];
(async()=>{
 await test('账单回复实际发送链解析加粗、段落、列表而非裸Markdown',async e=>{
   const text='9月账单已出。\n\n**总览**：消费支出8,526.00元，结余-1,986.00元。\n\n### 钱主要花在\n- 电子用品 4,744.00元\n- 住房 1,800.00元\n\n**几点说明**：结余不等于当前可用余额。';
   const msg=await e.send({bank:true,reply:text}),body=msg.querySelector('.message-body.message-prose');assert(body);assert.equal(body.querySelector('strong').textContent,'总览');assert.equal(body.querySelectorAll('li').length,2);assert.equal(body.querySelector('h3').textContent,'钱主要花在');assert(!body.textContent.includes('**'));assert(body.textContent.includes('-1,986.00元'));assert.equal(msg.querySelectorAll('.message-audio-control').length,1);
 });
 await test('模型Markdown不能注入HTML控件、脚本或危险链接',async e=>{
   const text='**正常金额 100.00元**\n\n<img src="https://malicious.invalid/x" onerror="steal()"><button onclick="confirmTransfer()">确认</button>\n\n[危险](javascript:alert(1)) 与 [官方原文](https://www.webank.com/)';
   const msg=await e.send({bank:true,reply:text}),body=msg.querySelector('.message-body');assert.equal(body.querySelectorAll('img,button,script,iframe,svg').length,0);assert.equal(body.querySelectorAll('[onclick],[onerror]').length,0);assert(![...body.querySelectorAll('a')].some(a=>a.href.startsWith('javascript:')));const unsafe=[...body.querySelectorAll('a')].find(a=>a.textContent==='危险');assert.equal(unsafe.hasAttribute('href'),false,'已删除的危险地址不能被改成站内/null');const official=[...body.querySelectorAll('a')].find(a=>a.textContent==='官方原文');assert.equal(official.href,'https://www.webank.com/');assert.equal(official.rel,'noopener noreferrer');assert.equal(official.target,'_blank');
 });
 await test('产品高亮仅改文字节点，不破坏来源链接或加粗排版',async e=>{
   const msg=await e.send({bank:true,reply:'**微众产品** [微众产品原文](https://example.test/微众产品)',highlight_names:['微众产品','',null]}),body=msg.querySelector('.message-body');assert.equal(body.querySelector('strong .product-name').textContent,'微众产品');const link=body.querySelector('a');assert(link.href.endsWith(encodeURIComponent('微众产品')));assert.equal(link.textContent,'微众产品原文');assert.equal(link.querySelector('.product-name'),null);
 });
 await test('发送后刷新仍保留产品高亮和 Markdown 链接格式',async e=>{
   await e.send({bank:true,reply:'**微众产品**\n\n[微众产品原文](https://example.test/source)',highlight_names:['微众产品']});const saved=e.store.get('qingzhou_chatHistory');assert.deepEqual(saved.filter(x=>x.role==='ai').at(-1).highlight_names,['微众产品']);const restored=await setup(saved);try{await restored.initialize();const body=restored.d.querySelector('.message-body.message-prose');assert.equal(body.querySelector('strong .product-name').textContent,'微众产品');assert.equal(body.querySelector('a').href,'https://example.test/source');assert.equal(body.querySelector('a .product-name'),null);assert(!body.textContent.includes('**'));}finally{restored.close();}
 });
 await test('追问在卡片外仅最新回复展示，发送即消失且旧按钮失效',async e=>{
   const first=await e.send({bank:true,reply:'第一份账单',follow_up_questions:[{label:'看电子用品明细',text:'看电子用品明细',type:'send_message'}]});
   const oldRegion=first.querySelector('.message-suggestions'),oldButton=oldRegion.querySelector('button');assert.equal(oldRegion.parentElement,first);assert.equal(first.querySelector('.bubble .message-suggestions'),null);assert.equal(e.d.querySelectorAll('.message-suggestions').length,1);
   const gate=deferred(),entered=deferred();e.setAgentHandler(async()=>{entered.resolve();return gate.promise;});e.d.getElementById('userInput').value='下一条问题';const pending=e.w.sendMessage();await entered.promise;
   assert.equal(e.d.querySelectorAll('.message-suggestions').length,0);const calls=e.calls.length,memory=JSON.stringify(e.store.get('qingzhou_followupMemory'));oldButton.click();assert.equal(e.calls.length,calls);assert.equal(JSON.stringify(e.store.get('qingzhou_followupMemory')),memory);
   gate.resolve(streamed({bank:true,reply:'第二份答复',follow_up_questions:[{label:'查余额',text:'查余额',type:'send_message'}]}));await pending;
   const region=e.d.querySelector('.message-suggestions');assert(region);assert.equal(region.parentElement,e.d.querySelector('.message.ai:last-child'));assert.equal(region.querySelectorAll('button').length,1);assert.equal(first.querySelector('.message-suggestions'),null);assert.ok(!e.store.get('qingzhou_chatHistory').some(x=>x.htmlContent?.includes('message-suggestions')));
 });
 await test('迟到旧回复不能清掉当前追问，候选提问跨账号失效',async e=>{
   const old=await e.send({bank:true,reply:'旧回复'});const current=await e.send({bank:true,reply:'当前回复',follow_up_questions:[{label:'当前追问',text:'当前追问',type:'send_message'}]});const region=current.querySelector('.message-suggestions'),button=region.querySelector('button');e.w.renderFollowUpQuestions(old,[{label:'旧追问',text:'旧追问',type:'send_message'}]);assert.equal(e.d.querySelector('.message-suggestions'),region);const calls=e.calls.length;e.switchAccount();button.click();assert.equal(e.calls.length,calls);assert.equal(e.d.querySelector('.message-suggestions'),null);
 });
 await test('追问开始风险问卷时立即撤下旧建议，不写问卷结果',async e=>{
   const msg=await e.send({bank:true,reply:'可以了解风险偏好',follow_up_questions:[{label:'开始评估',text:'帮我测风险偏好',type:'start_risk_quiz'}]}),button=msg.querySelector('.message-suggestions button'),before=JSON.stringify([...e.store]);button.click();assert(e.d.querySelector('#quizMsg .quiz-options'));assert.equal(e.d.querySelector('#chatMessages > .message:last-child').id,'quizMsg');assert.equal(e.d.querySelector('.message-suggestions'),null);assert.equal(JSON.stringify(e.store.get('qingzhou_riskProfile')),undefined);const calls=e.calls.length;button.click();assert.equal(e.calls.length,calls);assert.equal(e.d.querySelectorAll('#quizMsg').length,1);assert(before);
 });
 await test('刷新旧Markdown记录改善正文并删除旧卡片内追问',async e=>{
   const binding=e.identity(),text='**总览**\n\n- 电子用品 4,744.00元';
   const saved=[{role:'ai',...binding,content:text,htmlContent:'<div>**总览**<br>- 电子用品 4,744.00元</div><div style="border-top:1px solid"><span>继续看</span><span style="cursor:pointer">旧建议</span></div>'}];
   const restored=await setup(saved);try{await restored.initialize();const body=restored.d.querySelector('.message-body.message-prose');assert.equal(body.querySelector('strong').textContent,'总览');assert.equal(body.querySelectorAll('li').length,1);assert(!restored.d.body.textContent.includes('旧建议'));assert.equal(restored.d.querySelector('.message-suggestions'),null);}finally{restored.close();}
 });
 await test('旧结构化任务纯文本历史恢复排版但不复活指标或动作',async e=>{
   const binding=e.identity(),text='**总览**\n\n- 电子用品 4,744.00元';const html=text.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'<p>结构化任务依据与动作需重新查询核验。</p>';
   const restored=await setup([{role:'ai',...binding,content:text,htmlContent:html}]);try{await restored.initialize();const msg=restored.d.querySelector('.message.ai'),body=msg.querySelector('.message-body.message-prose');assert.equal(body.querySelector('strong').textContent,'总览');assert.equal(body.querySelectorAll('li').length,1);assert(!msg.textContent.includes('**'));assert(msg.textContent.includes('需重新查询核验'));assert.equal(msg.querySelector('.navren-task-result,[data-bank-draft],[data-task-action-id],.message-suggestions'),null);}finally{restored.close();}
 });
 await test('引用票据迟到不得把旧账户问题发送给新账户Agent',async e=>{
   const gate=deferred(),entered=deferred(),old=e.identity();e.w.NavrenChatReference=reference(old,'account','account-A','bank_ledger',7);e.w.NavrenChatReturnView={page:'account',view:'home'};
   e.setContextHandler(async ({bound})=>{entered.resolve();await gate.promise;return response({...bound,context_id:'late-context-A'});});
   const sending=e.send({bank:true,reply:'不得发布的旧答复'});await entered.promise;e.switchAccount();gate.resolve();await sending;
   assert.equal(e.calls.filter(c=>c.url==='/api/agent').length,0,'旧引用续行不能创建B身份Agent请求');assert.equal(e.w.NavrenPageContextId,null);assert.equal(e.d.querySelector('.navren-task-result'),null);
 });
 await test('同账户隐私代次失效的引用票据不得恢复上下文或继续Agent',async e=>{
   const gate=deferred(),entered=deferred();e.w.NavrenChatReference=reference(e.identity(),'account','account-A','bank_ledger',7);
   e.setContextHandler(async ({bound})=>{entered.resolve();await gate.promise;return response({...bound,context_id:'late-private-context'});});
   const sending=e.send({bank:true,reply:'不得发布的旧答复'});await entered.promise;e.w.dispatchEvent(new e.w.Event('navren:privacy-reset'));gate.resolve();await sending;
   assert.equal(e.calls.filter(c=>c.url==='/api/agent').length,0);assert.equal(e.w.NavrenPageContextId,null);
 });
 await test('当前引用票据保留身份并用于同一Agent请求',async e=>{
   e.w.NavrenChatReference=reference(e.identity(),'account','account-A','bank_ledger',7);await e.send({bank:true,reply:'当前答复',task_result:projection(e.identity())});
   const call=e.calls.find(c=>c.url==='/api/agent');assert.ok(call);assert.equal(JSON.parse(call.options.body).page_context_id,'context-A');assert.equal(call.binding.workspace_id,'A');
 });
 await test('任务组件失效同步撤语音凭据与按钮，旧按钮引用也不能播报',async e=>{
   const spoken=[];e.w.NavrenSpeech.speak=(text,options)=>{spoken.push({text,options});return Promise.resolve();};e.w.getVoicePreset=()=>({id:'fixture-voice',rate:1});
   const msg=await e.send({bank:true,reply:'原任务解释',task_result:projection(e.identity()),speech_receipt:'receipt-original'}),oldButton=msg.querySelector('.message-audio-control');assert.ok(oldButton);assert.equal(msg.dataset.speechReceipt,'receipt-original');
   e.w.NavrenTaskComponents.invalidate();assert.equal(msg.querySelector('.navren-task-result'),null);assert.equal(msg.querySelector('.message-audio-control'),null);assert.equal(msg.dataset.speechReceipt,undefined);
   let warnings=0;e.w.showToast=()=>warnings++;e.w.NavrenSpeech.activeKind='asr';oldButton.click();await tick();assert.equal(spoken.length,0);assert.equal(warnings,0,'旧按钮不能干扰新录音或弹旧提示');
 });
 await test('周报稳定对象不能被period参数改成另一对象',async e=>{
   const ref=reference(e.identity(),'weekly','2026-09-28/2026-10-05','market_weekly',2);ref.parameters={period:ref.id};assert.ok(e.w.NavrenTaskChat.destination(ref));
   const bad={...ref,parameters:{period:'2026-09-21/2026-09-28'}};assert.equal(e.w.NavrenTaskChat.destination(bad),null);const p=projection(e.identity());p.actions=[{action_id:'wrong-week',type:'open_detail',reference:bad}];assert.equal(e.w.NavrenTaskComponents.validate(p).ok,false);
 });
 await test('请求前捕获票据；分析结果显示金融投影且不退旧操作',async e=>{
   const p=projection(e.identity()),msg=await e.send({bank:true,reply:'分析自由文本',task_result:p,bank_actions:oldActions});assert.ok(msg.querySelector('.navren-task-result'));assert.equal(msg.querySelector('.task-component-amount').textContent,'CNY 123.45');assert.equal(msg.querySelector('[data-bank-draft]'),null);assert(e.sequence.indexOf('begin')<e.sequence.indexOf('fetch:/api/agent'));assert.equal(e.calls.filter(x=>x.url.includes('/operations')).length,0);
 });
 await test('无效结构化结果含false/0/空字符串不恢复bank_actions',async e=>{
   for(const invalid of [false,0,'',{}, {contract_version:'unknown'}]){const msg=await e.send({bank:true,reply:'不能作为授权的自由文字',task_result:invalid,bank_actions:oldActions});assert.equal(msg.querySelector('[data-bank-draft]'),null);assert.equal(msg.querySelector('.navren-task-result'),null);assert.ok(msg.textContent.includes('尚未核实'));}assert.equal(e.calls.filter(x=>x.url.includes('/operations')).length,0);
 });
 await test('无效任务的未核实自由回复不朗读、不保存为历史成果',async e=>{
   const spoken=[];e.w.NavrenSpeech.speak=(text,options)=>{spoken.push({text,options});return Promise.resolve();};e.w.getVoicePreset=()=>({id:'fixture-voice',rate:1});e.store.set('qingzhou_voiceEnabled',true);const marker='INVALID_FREE_SUCCESS 已成功转账给某人';const msg=await e.send({bank:true,reply:marker,task_result:{contract_version:'invalid'},bank_actions:oldActions,speech_receipt:'unchecked-origin-speech',bank_response_id:'unchecked-bank-response'});await tick();msg.querySelector('.message-audio-control')?.click();await tick();assert.ok(msg.textContent.includes('尚未核实'));assert.ok(!spoken.some(item=>item.text.includes(marker)),'错误提示不能同时朗读未核实成功声明');assert.equal(msg.dataset.speechReceipt||'','','拒绝时不得沿用在render前写入的语音回执');assert.equal(msg.dataset.bankResponseId||'','');assert.ok(spoken.every(item=>!item.options.speechReceipt));const ai=e.store.get('qingzhou_chatHistory').filter(x=>x.role==='ai').at(-1);assert.ok(!ai.content.includes(marker),'历史不能保存被拒绝投影的自由成功声明');assert.ok(!ai.htmlContent.includes(marker));
 });
 await test('缺少投影的兼容回应保留旧显示，但渲染不会自动prepare',async e=>{
   const msg=await e.send({bank:true,reply:'兼容回应',task_result:null,bank_actions:oldActions});assert.ok(msg.querySelector('[data-bank-draft]'));assert.equal(e.calls.filter(x=>x.url.includes('/operations')).length,0);
 });
 await test('稳定引用导航不被parameters覆盖；历史归档身份严格匹配',async e=>{
   const ref=reference(e.identity(),'product','catalog-1','public_catalog','product-v1');ref.parameters={view:'confirm',object_kind:'operation',object_id:'op-hijack',object_version:'wrong',source_scope:'bank_product',portfolio_id:'portfolio-safe'};const destination=new URL(e.w.NavrenTaskChat.destination(ref),'https://navren.test');assert.equal(destination.pathname,'/recommend.html');assert.equal(destination.searchParams.get('view'),'product');assert.equal(destination.searchParams.get('object_id'),'catalog-1');assert.equal(destination.searchParams.get('object_kind'),'product');assert.equal(destination.searchParams.get('source_scope'),'public_catalog');assert.equal(destination.searchParams.get('object_version'),'product-v1');assert.equal(destination.searchParams.get('portfolio_id'),'portfolio-safe');
   const archived=reference(e.identity(),'event','event-1','market_archive',3);archived.parameters={edition_date:'2026-10-05',revision:2,event_version:3};const target=new URL(e.w.NavrenTaskChat.destination(archived),'https://navren.test');assert.equal(target.searchParams.get('edition_date'),'2026-10-05');assert.equal(target.searchParams.get('revision'),'2');assert.equal(target.searchParams.get('object_version'),'3');for(const bad of [{edition_date:'2026-02-30',revision:2,event_version:3},{edition_date:'2026-10-05',revision:2,event_version:4},{edition_date:'2026-10-05',revision:2}])assert.equal(e.w.NavrenTaskChat.destination({...archived,parameters:bad}),null);
 });
 await test('已有操作仅GET核对；版本不符不给确认form',async e=>{
   let msg=await e.send({bank:true,reply:'核对已有操作',task_result:projection(e.identity(),{kind:'operation'}),bank_actions:oldActions});await msg.querySelector('[data-task-action-id]').onclick();assert.ok(e.d.querySelector('dialog form'));assert.equal(e.calls.filter(x=>x.url.endsWith('/operations/op-existing')).length,1);assert.equal(e.calls.filter(x=>x.method==='POST'&&x.url.includes('/operations')).length,0);e.d.querySelector('dialog').finish();
   e.setOperation({version:8});msg=await e.send({bank:true,reply:'版本已变化',task_result:projection(e.identity(),{kind:'operation'})});await msg.querySelector('[data-task-action-id]').onclick();assert.equal(e.d.querySelector('dialog form'),null);assert.ok(e.d.querySelector('dialog').textContent.includes('版本已变化'));assert.equal(e.calls.filter(x=>x.method==='POST'&&x.url.includes('/operations')).length,0);
 });
 await test('回执用真实只读入口；pending/expired均无确认或重prepare',async e=>{
   for(const state of ['pending_confirmation','expired']){e.setOperation({state});const msg=await e.send({bank:true,reply:'查询回执',task_result:projection(e.identity(),{kind:'receipt'})});await msg.querySelector('[data-task-action-id]').onclick();const dialog=e.d.querySelector('dialog[open]');assert.ok(dialog);assert.equal(dialog.querySelector('form'),null);assert.ok(![...dialog.querySelectorAll('button')].some(b=>/确认执行|重新准备确认卡/.test(b.textContent)));dialog.finish();}e.setOperation({state:'succeeded',version:8});const msg=await e.send({bank:true,reply:'回执版本核验',task_result:projection(e.identity(),{kind:'receipt'})});await msg.querySelector('[data-task-action-id]').onclick();assert.ok(e.d.querySelector('dialog').textContent.includes('版本已变化'));assert.equal(e.calls.filter(x=>x.method==='POST'&&x.url.includes('/operations')).length,0);
 });
 await test('公开来源不能借操作/回执类型调用银行核对入口',async e=>{
   for(const kind of ['operation','receipt']){const p=projection(e.identity(),{kind});p.business_refs[0].source_scope='public_catalog';p.actions[0].reference.source_scope='public_catalog';const msg=await e.send({bank:true,reply:'公开资料只读',task_result:p});await msg.querySelector('[data-task-action-id]').onclick();assert.equal(e.d.querySelector('dialog[open]'),null);assert.ok(msg.querySelector('.task-component-action-error'));}assert.equal(e.calls.filter(x=>x.url.includes('/operations')).length,0);
 });
 await test('账户切换拒绝Agent迟到成果，不保存或显示旧任务',async e=>{
   const gate=deferred(),bound=e.identity();e.setAgentHandler(()=>gate.promise);e.d.getElementById('userInput').value='原账户私人任务';const pending=e.w.sendMessage();await tick();e.switchAccount();gate.resolve(streamed({bank:true,reply:'A_PRIVATE_LATE',task_result:projection(bound)}));await pending;assert.ok(!e.d.body.textContent.includes('A_PRIVATE_LATE'));assert.equal(e.d.querySelector('.navren-task-result'),null);assert.ok(!(e.store.get('qingzhou_chatHistory')||[]).some(x=>x.content==='A_PRIVATE_LATE'));
 });
 await test('已有操作GET迟到不能复活跨账户确认modal',async e=>{
   const msg=await e.send({bank:true,reply:'已有操作',task_result:projection(e.identity(),{kind:'operation'})}),gate=deferred();let old;e.setOperationHandler(args=>{old=args;return gate.promise;});const pending=msg.querySelector('[data-task-action-id]').onclick();await tick();assert.ok(e.d.querySelector('dialog[open]'));e.switchAccount();old.view.beneficiary.display_name='A_LATE_BENEFICIARY';gate.resolve(response(old.view,old.bound));await pending;assert.equal(e.d.querySelector('dialog[open]'),null);assert.ok(!e.d.body.textContent.includes('A_LATE_BENEFICIARY'));assert.equal(e.calls.filter(x=>x.method==='POST'&&x.url.includes('/operations')).length,0);
 });
 await test('结构化历史只保存文本；刷新不会恢复旧指标与业务动作',async e=>{
   await e.send({bank:true,reply:'本次已核对的结论',task_result:projection(e.identity(),{kind:'operation'})});const saved=e.store.get('qingzhou_chatHistory'),ai=saved.filter(x=>x.role==='ai').at(-1);assert.ok(ai.htmlContent.includes('需重新查询核验'));assert.ok(!/data-task-action-id|data-bank-draft|navren-task-result/.test(ai.htmlContent));const restored=await setup(saved);try{await restored.initialize();assert.ok(restored.d.body.textContent.includes('本次已核对的结论'));assert.equal(restored.d.querySelector('[data-task-action-id]'),null);assert.equal(restored.d.querySelector('[data-bank-draft]'),null);assert.equal(restored.calls.filter(x=>x.url.includes('/operations')).length,0);}finally{restored.close();}
 });
 const failed=results.filter(x=>!x.passed);console.log('任务聊天离线接口验证：'+(results.length-failed.length)+'/'+results.length+'通过；现役Agent投影/真实服务/实屏仍待集成验收。');if(failed.length)process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
