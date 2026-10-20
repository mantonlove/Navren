const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// 浏览器前置夹具：每个 VM 独立且初始为空，与 localStorage 分离。
function navrenSessionStorageFixture() {
  const values = new Map();
  return {
    getItem(key) { return values.get(String(key)) ?? null; },
    setItem(key, value) { values.set(String(key), String(value)); },
    removeItem(key) { values.delete(String(key)); },
    clear() { values.clear(); }
  };
}

const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');
const bankClientSource = fs.readFileSync(path.join(__dirname, '..', 'js', 'bank_client.js'), 'utf8');

test('bank response normalization preserves cashflow and review evidence for UI cards', () => {
  const context = vm.createContext({});
  vm.runInContext(source, context, { filename: 'js/api.js' });
  context.response = {
    bank: true,
    reply: '已完成只读资金推演。',
    bank_actions: [],
    cashflow_timeline: { status: 'approved', accounts: [{ currency: 'CNY' }], timeline: [] },
    agent_workflow: { status: 'approved', evidence: [{ evidence_id: 'evidence-1' }] },
    user_memory_context: [{ node_id: 'memory-1', category: 'goal' }],
  };

  const result = vm.runInContext('Api._handleResponse(response, "", {})', context);

  assert.equal(result.bank, true);
  assert.deepEqual(JSON.parse(JSON.stringify(result.cashflow_timeline)), context.response.cashflow_timeline);
  assert.deepEqual(JSON.parse(JSON.stringify(result.agent_workflow)), context.response.agent_workflow);
  assert.deepEqual(JSON.parse(JSON.stringify(result.user_memory_context)), context.response.user_memory_context);
});

test('response normalization preserves speech receipt, personal revision, knowledge snapshot and candidate binding', () => {
  const context = vm.createContext({});
  vm.runInContext(source, context, { filename: 'js/api.js' });
  const candidate = { proposal_id: 'proposal-1', source_ref: 'source-1',
    confirmation_token: 'confirmation-1', expected_revision: 7, category: 'goal', value: '明年学费' };
  context.response = { bank: true, reply: '请核对本人陈述。', speech_receipt: 'speech-1',
    personal_revision: 7, knowledge_snapshot: { release_id: 'public-release-1' }, memory_proposals: [candidate] };
  const result = vm.runInContext('Api._handleResponse(response,"",{})', context);
  assert.equal(result.speech_receipt, 'speech-1');
  assert.equal(result.personal_revision, 7);
  assert.deepEqual(JSON.parse(JSON.stringify(result.knowledge_snapshot)), context.response.knowledge_snapshot);
  assert.deepEqual(JSON.parse(JSON.stringify(result.memory_proposals)), [candidate]);
  context.response = { bank: true, isFallback: true, reply: '当前资料未取得。', speech_receipt: 'fallback-speech', personal_revision: '7' };
  const fallback = vm.runInContext('Api._handleResponse(response,"",{})', context);
  assert.equal(fallback.speech_receipt, 'fallback-speech');
  assert.equal(fallback.personal_revision, null);
});

test('colloquial personal balance questions are recognized as bank requests', () => {
  const context = vm.createContext({});
  context.window = context;
  context.addEventListener=()=>{};
  vm.runInContext(bankClientSource, context, { filename: 'js/bank_client.js' });

  assert.equal(vm.runInContext('BankClient.isBankRequest("我还有多少钱")', context), true);
  assert.equal(vm.runInContext('BankClient.isBankRequest("账户里还有多少钱")', context), true);
  assert.equal(vm.runInContext('BankClient.isBankRequest("这款产品多少钱")', context), false);
});

test('every Agent failure stays in the unified error path regardless of wording', async () => {
  const context = vm.createContext({
    Storage: { get: () => null }, console: { warn() {}, error() {} }
  });
  vm.runInContext(source, context);
  vm.runInContext('Api._fetchAgent = async () => { throw new Error("offline"); }; Api._fallbackResponse = () => { throw new Error("legacy fallback called"); };', context);
  for (const text of ['你好', '房租交完还够吗', '刚才那个改两千']) {
    context.question = text;
    const result = await vm.runInContext('Api.sendMessage(question, "youth")', context);
    assert.equal(result.bank, true);
    assert.equal(result.isFallback, true);
    assert.match(result.reply, /对话服务暂不可用/);
    assert.doesNotMatch(result.reply, /测评|推荐产品/);
  }
});

test('reviewed operation ID and Agent status survive response normalization', () => {
  const context = vm.createContext({});
  vm.runInContext(source, context);
  context.response = {bank:true, reply:'草案待确认',
    bank_actions:[{operation_id:'op-1',draft:{operation_id:'op-1'}}],
    agent_review:{status:'approved'},agent_runtime:{architecture:'native_tool_agent'},
    answer_contract:{kind:'reviewed_text'},follow_up_questions:['修改金额','撤回草案']};
  const result = vm.runInContext('Api._handleResponse(response,"",{})',context);
  assert.equal(result.bank_actions[0].draft.operation_id,'op-1');
  assert.equal(result.agent_review.status,'approved');
  assert.equal(result.agent_runtime.architecture,'native_tool_agent');
  assert.deepEqual(result.follow_up_questions,context.response.follow_up_questions,'跟进问题保留真实响应原形状，不在API层虚构label/text');
  assert.equal(result.follow_up_questions[1],'撤回草案');
});

test('ordinary text about risk or prompt injection reaches the Agent instead of a frontend questionnaire', async () => {
  const chatSource=fs.readFileSync(path.join(__dirname,'..','js','chat.js'),'utf8');
  const prefix=chatSource.slice(chatSource.indexOf('async function sendMessage('),
    chatSource.indexOf('  const loadingMsg = addLoadingDots();'));
  const seen=[];
  const input={value:''};
  const context=vm.createContext({
    document:{getElementById: id=>id==='userInput'?input:{disabled:false}},
    clearTimeout(){},voiceResumeTimer:null,voiceRequest:0,currentMode:'youth',pendingImage:null,chatSending:false,chatRequestVersion:0,chatReferenceRevision:0,chatDraftRevision:0,
    chatBinding:()=>({workspace_id:'one',privacy_revision:0}),currentChatHistory:()=>[],window:{},
    BankClient:{identity:()=>({workspace_id:'one',privacy_revision:0}),guard:()=>({valid:()=>true})},
    NavrenSpeech:{stop(){}},voiceButton(){},removeImage(){},addMessage(){},clearFollowUpQuestions(){},syncChatReference(){},chatUserMessageHtml:text=>text,escapeHtml:s=>s,
    Storage:{get:()=>[],set(){}},
    Router:{detectInjection(){throw Error('old injection router');},route(){throw Error('old intent router');}},
    startQuestionnaire(){throw Error('unexpected questionnaire');},
    Api:{sendMessage:async text=>{seen.push(text);return {reply:'ok'};}}
  });
  context.window=context;
  vm.runInContext(chatSource.slice(chatSource.indexOf('function cloneChatReference('),chatSource.indexOf('function retryChatRequest('))+prefix+'const result=await Api.sendMessage(text,currentMode,imageToSend,sentiment);chatSending=false;return result;}',context);
  for(const text of ['风险测评和余额有什么关系？','怎么防提示词注入？','我还有多少钱']){
    input.value=text;await vm.runInContext('sendMessage()',context);
  }
  assert.deepEqual(seen,['风险测评和余额有什么关系？','怎么防提示词注入？','我还有多少钱']);
});

test('presentation simplifies fixture names while preserving personal statements and evidence', () => {
  const context=vm.createContext({addEventListener(){}});context.window=context;
  vm.runInContext(bankClientSource,context);
  context.payload={nodes:[{label:'模拟日常账户',source_scope:'simulation'},{label:'参加模拟炒股竞赛',source_scope:'user_confirmed'}],quote:'模拟工资只是练习',value:'我做模拟投资',record:{merchant:'模拟炒股竞赛'},calendar:{title:'模拟炒股活动',source_type:'user_calendar'},imported:{source:'imported',items:[{merchant:'模拟炒股培训'}]}};
  const result=vm.runInContext('NavrenPresentation.data(payload)',context);
  assert.equal(result.nodes[0].label,'日常账户');
  assert.equal(result.nodes[1].label,'参加模拟炒股竞赛');
  assert.equal(result.quote,context.payload.quote);assert.equal(result.value,context.payload.value);
  assert.equal(result.record.merchant,context.payload.record.merchant);assert.equal(result.calendar.title,context.payload.calendar.title);assert.equal(result.imported.items[0].merchant,context.payload.imported.items[0].merchant);
  assert.equal(vm.runInContext('NavrenPresentation.text("原话：模拟工资只是练习")',context),'原话：模拟工资只是练习');
});

test('a response arriving after another tab removes private context cannot be rendered again', async () => {
  const chatSource=fs.readFileSync(path.join(__dirname,'..','js','chat.js'),'utf8');
  const prefix=chatSource.slice(chatSource.indexOf('async function sendMessage('),chatSource.indexOf('  result.reply=NavrenPresentation.text'));
  const input={value:'继续分析'},context=vm.createContext({document:{getElementById:id=>id==='userInput'?input:{disabled:false}},clearTimeout(){},voiceResumeTimer:null,voiceRequest:0,currentMode:'youth',pendingImage:null,chatSending:false,chatRequestVersion:0,chatReferenceRevision:0,chatDraftRevision:0,
    chatBinding:()=>({workspace_id:'one',privacy_revision:0}),currentChatHistory:()=>[],window:{},BankClient:{identity:()=>({workspace_id:'one',privacy_revision:0})},
    NavrenSpeech:{stop(){}},voiceButton(){},removeImage(){},addMessage(){},clearFollowUpQuestions(){},syncChatReference(){},chatUserMessageHtml:text=>text,escapeHtml:s=>s,Storage:{get:()=>[],set(){}},addLoadingDots:()=>({remove(){}}),scrollToBottom(){},Api:{}});
  context.window=context;
  context.NavrenChatReference=null;context.NavrenChatReferenceBinding=null;context.NavrenChatReturnView=null;
  vm.runInContext('Api.sendMessage=async()=>{globalThis.NavrenChatPrivacyVersion=1;return {reply:"旧私人资料"};};',context);
  vm.runInContext(chatSource.slice(chatSource.indexOf('function cloneChatReference('),chatSource.indexOf('function retryChatRequest('))+prefix+'throw Error("stale response reached renderer");}',context);
  await vm.runInContext('sendMessage()',context);
  assert.equal(context.NavrenChatReference,null,'失效回合不能恢复旧引用');assert.equal(input.value,'','失效回合不能回填旧问题');
});

test('welcome cannot revive old private browser profiles or claim formal assessment',()=>{
  const chatSource=fs.readFileSync(path.join(__dirname,'..','js','chat.js'),'utf8');
  const welcome={textContent:''};const writes=[];
  const context=vm.createContext({document:{addEventListener(){},getElementById:()=>welcome},window:{addEventListener(){}},
    Storage:{get:()=>({risk_level:'R5',nickname:'旧账户私密姓名',amount:999999}),set:(key,value)=>writes.push(key)},
    assembleProfile(){throw Error('旧浏览器档案不能参与欢迎语');}});
  vm.runInContext(chatSource,context);vm.runInContext('personalizeWelcome()',context);
  assert.match(welcome.textContent,/查账户/);assert.doesNotMatch(welcome.textContent,/旧账户私密姓名|999999|风险评估已完成|画像已就绪/);
  assert(!writes.includes('qingzhou_chatHistory'));
});

test('legacy array responses retain shape but reject another workspace using authoritative headers',async()=>{
  const state={workspace_id:'one',privacy_revision:0,csrf:'test'};const storage=new Map();let resets=0;
  const context=vm.createContext({AbortController,setTimeout,clearTimeout,Event:class{constructor(type){this.type=type;}},
    sessionStorage:navrenSessionStorageFixture(),
    localStorage:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    addEventListener(){},dispatchEvent:()=>resets++,
    fetch:async(url)=>({ok:true,json:async()=>url.endsWith('/session')?state:[{id:'not-one'}],
      headers:{get:key=>url.endsWith('/session')?null:key==='X-Navren-Workspace'?'two':key==='X-Navren-Privacy-Revision'?'0':null}})});
  context.window=context;vm.runInContext(bankClientSource,context);await vm.runInContext('BankClient.ensure()',context);
  await assert.rejects(()=>vm.runInContext('BankClient.get("/products")',context),error=>error.code==='STALE_IDENTITY');
  assert.equal(vm.runInContext('BankClient.identity()',context),null);assert.equal(resets,1);
});

test('an imported-file preview arriving after authorization reset is discarded',async()=>{
  const state={workspace_id:'one',privacy_revision:0,csrf:'test'},storage=new Map(),listeners=new Map();
  let resolve;const delayed=new Promise(r=>resolve=r);
  const context=vm.createContext({AbortController,setTimeout,clearTimeout,Event:class{},
    sessionStorage:navrenSessionStorageFixture(),
    localStorage:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    addEventListener:(kind,handler)=>listeners.set(kind,handler),dispatchEvent(){},
    fetch:async(url)=>url.endsWith('/session')?{ok:true,json:async()=>state}:{ok:true,json:async()=>delayed}});
  context.window=context;vm.runInContext(bankClientSource,context);await vm.runInContext('BankClient.ensure()',context);
  const pending=vm.runInContext('BankClient.postText("/statements/preview","私人账单")',context);
  await new Promise(r=>setTimeout(r,0));listeners.get('storage')({key:'navren_privacy_one',newValue:'1'});
  resolve({...state,sample:[{merchant:'旧私人商户'}]});
  await assert.rejects(()=>pending,error=>error.code==='STALE_RESPONSE');
});

test('page references attach material without rewriting the composer or auto-sending',async()=>{
  const chatSource=fs.readFileSync(path.join(__dirname,'..','js','chat.js'),'utf8');
  const first=chatSource.indexOf('async function receivePageQuestion('),receive=chatSource.slice(first,chatSource.indexOf('\nfor(const type of ',first));
  let sends=0,focuses=0;const input={value:'',dispatchEvent(){},focus(){focuses++;}};
  const binding={workspace_id:'one',privacy_revision:0},claim={...binding,question:'原来的建议问题',reference:{kind:'account',id:'one'},return_view:{page:'account',view:'account-detail'},open_once:true,send_once:true};
  const context=vm.createContext({chatReferenceRevision:0,chatBinding:()=>binding,document:{getElementById:()=>input},location:{href:'https://navren.test/chat.html?context=ticket'},URL,Event:class{},history:{state:null,replaceState(){}},BankClient:{guard:()=>({valid:data=>!data||data.workspace_id===binding.workspace_id&&data.privacy_revision===binding.privacy_revision}),post:async()=>claim},syncChatReference(){},sendMessage:async()=>sends++,addMessage(){},escapeHtml:s=>s});context.window=context;
  vm.runInContext(chatSource.slice(chatSource.indexOf('function cloneChatReference('),chatSource.indexOf('function currentChatReference('))+receive,context);await vm.runInContext('receivePageQuestion("ticket")',context);
  assert.equal(input.value,'');assert.equal(context.NavrenChatReference.id,'one');assert.deepEqual(JSON.parse(JSON.stringify(context.NavrenChatReferenceBinding)),binding);assert.equal(context.NavrenPageContextId,null);assert.equal(sends,0);assert.equal(focuses,1);
  input.value='用户改过的问题';claim.open_once=false;await vm.runInContext('receivePageQuestion("ticket")',context);assert.equal(input.value,'用户改过的问题');assert.equal(sends,0);
  claim.open_once=true;claim.reference=null;input.value='';await vm.runInContext('receivePageQuestion("ticket")',context);assert.equal(input.value,claim.question,'无引用旧票据仅兼容空且未变化的输入框');assert.equal(context.NavrenChatReference,null);assert.equal(sends,0);
  input.value='本人新问题';await vm.runInContext('receivePageQuestion("ticket")',context);assert.equal(input.value,'本人新问题','旧票据不改本人文字');assert.equal(sends,0);
  claim.reference={kind:'account',id:'other'};claim.workspace_id='other';await vm.runInContext('receivePageQuestion("ticket")',context);assert.equal(context.NavrenChatReference,null,'异主体响应拒绝挂引用');assert.equal(input.value,'本人新问题');
});

test('sending an edited referenced draft creates a new final-question ticket',async()=>{
  const chatSource=fs.readFileSync(path.join(__dirname,'..','js','chat.js'),'utf8');
  const first=chatSource.indexOf('async function sendMessage('),last=chatSource.indexOf('  if (!unifiedTask&&typeof BankUI',first);
  const posts=[],calls=[],retries=[],binding={workspace_id:'one',privacy_revision:0};const input={value:'用户修改后只想查询余额',dispatchEvent(){}};
  const context=vm.createContext({Event:class{},document:{getElementById:id=>id==='userInput'?input:{disabled:false},createElement:()=>({})},clearTimeout(){},voiceResumeTimer:null,voiceRequest:0,currentMode:'youth',pendingImage:null,chatSending:false,chatRequestVersion:0,chatReferenceRevision:0,chatDraftRevision:0,
    chatBinding:()=>binding,currentChatHistory:()=>[],NavrenSpeech:{stop(){}},voiceButton(){},removeImage(){},addMessage:()=>({dataset:{},querySelector:()=>null}),clearFollowUpQuestions(){},syncChatReference(){},chatUserMessageHtml:text=>text,updateChatSendState(){},escapeHtml:s=>s,Storage:{set(){}},addLoadingDots:()=>({remove(){}}),scrollToBottom(){},renderRuntimeEvent(){},NavrenPresentation:{text:text=>text},verifiedReplyProjection:()=>null,renderReplyText:text=>text,highlightProductNames:text=>text,newChatMessageId:()=> 'fixture-message',appendMessageRetry:(message,button)=>retries.push(button),
    BankClient:{identity:()=>binding,guard:()=>({valid:data=>!data||data.workspace_id===binding.workspace_id&&data.privacy_revision===binding.privacy_revision}),post:async(path,body)=>{posts.push({path,body});return {context_id:'final-ticket-'+posts.length,...binding};}},Api:{sendMessage:async(text,mode,image,sentiment,event,options)=>{calls.push({text,options});return {reply:'ok'};}}});context.window=context;
  context.NavrenChatReference={kind:'account',id:'one',version:4,execution_authority:false};context.NavrenChatReferenceBinding=binding;context.NavrenPageContextId=null;
  vm.runInContext(chatSource.slice(chatSource.indexOf('function cloneChatReference('),chatSource.indexOf('function retryChatRequest('))+chatSource.slice(first,last)+'return result;}',context);
  await vm.runInContext('sendMessage()',context);
  assert.equal(posts.length,1);assert.equal(posts[0].path,'/page-contexts');assert.equal(posts[0].body.question,'用户修改后只想查询余额');assert.equal(posts[0].body.reference.id,'one');assert(!('execution_authority' in posts[0].body.reference));assert(!posts[0].body.draft_only);assert.equal(context.NavrenPageContextId,null);assert.equal(calls[0].text,'用户修改后只想查询余额');assert.equal(calls[0].options.page_context_id,'final-ticket-1');assert.equal(input.value,'');assert.equal(context.NavrenChatReference,null);
  input.value='失败后本人查询';context.NavrenChatReference={kind:'account',id:'one',version:4};context.NavrenChatReferenceBinding=binding;context.NavrenChatReturnView={page:'account',view:'home'};
  context.Api.sendMessage=async(text,mode,image,sentiment,event,options)=>{calls.push({text,options});return {reply:'读取失败',isFallback:true};};await vm.runInContext('sendMessage()',context);
  assert.equal(posts.length,2,'本人再次发送只准备一个新票据');assert.equal(calls.length,2,'失败不自动重试');assert.equal(calls[1].options.page_context_id,'final-ticket-2');assert.equal(input.value,'失败后本人查询');assert.equal(context.NavrenChatReference.id,'one');assert.equal(context.NavrenChatReference.version,4);assert.deepEqual(JSON.parse(JSON.stringify(context.NavrenChatReferenceBinding)),binding);assert.equal(retries.length,1,'失败仅提供本人可点击重试入口');
  context.Api.sendMessage=async()=>{input.value='等待期间本人新写的原话';context.chatDraftRevision++;return {reply:'读取失败',isFallback:true};};await vm.runInContext('sendMessage()',context);
  assert.equal(input.value,'等待期间本人新写的原话','失败不得覆盖新草稿');assert.equal(context.NavrenChatReference,null,'失败不得把旧引用附到新草稿');assert.equal(posts.length,3,'只产生3次本人明确发送的票据');
});

test('a late claimed reference cannot revive a draft after privacy reset',async()=>{
  const chatSource=fs.readFileSync(path.join(__dirname,'..','js','chat.js'),'utf8'),first=chatSource.indexOf('async function receivePageQuestion('),receive=chatSource.slice(first,chatSource.indexOf('\nfor(const type of ',first));
  let finish,valid=true;const response=new Promise(resolve=>finish=resolve),input={value:'',dispatchEvent(){throw Error('late draft rendered');},focus(){throw Error('late focus');}};
  const context=vm.createContext({chatReferenceRevision:0,document:{getElementById:()=>input},location:{href:'https://navren.test/chat.html?context=ticket'},URL,Event:class{},history:{state:null,replaceState(){}},BankClient:{guard:()=>({valid:()=>valid}),post:()=>response},syncChatReference(){throw Error('late reference rendered');},addMessage(){throw Error('late error rendered');},escapeHtml:s=>s});context.window=context;
  vm.runInContext(receive,context);const pending=vm.runInContext('receivePageQuestion("ticket")',context);
  valid=false;context.NavrenChatPrivacyVersion=1;context.NavrenChatReference=null;context.NavrenChatReturnView=null;
  finish({question:'旧私人问题',reference:{id:'old'},return_view:{page:'account'},open_once:true});await pending;
  assert.equal(input.value,'');assert.equal(context.NavrenChatReference,null);assert.equal(context.NavrenChatReturnView,null);
});
