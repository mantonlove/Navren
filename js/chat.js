/**
 * Navren — chat.html 页面逻辑
 */

const PRESET_QUESTIONS = {
  classic: ['分析9月账单和异常消费', '我有多少可投资资金？', '对比活钱和30天产品', '关闭视频会员的银行代扣'],
  senior: ['帮我看9月账单', '应急的钱留好后，还能投多少？', '对比活钱和30天产品', '查看银行代扣'],
  youth: ['9月钱花在哪了？', '我有多少可投资资金？', '活钱和30天产品怎么选？', '查看银行代扣']
};

let currentMode = 'classic';
let isListening = false;
const chatAttachmentUploadKeys=new WeakMap();
const chatAttachmentUploads=new WeakMap();
let handoffActive = false;
let chatSending=false;
let newConversationStarting=false;
let conversationIssueRun=0;
let conversationScope=null;
let conversationScopeBinding=null;
let conversationScopeInvalid=false;
const conversationScopeStorageKey='navren:conversation-scope:v1';
let chatRequestVersion=0;
let chatReferenceRevision=0;
let chatDraftRevision=0;
document.addEventListener('input',event=>{if(event.target?.id==='userInput')chatDraftRevision++;});
function chatBinding(){const s=BankClient.identity();return s?{registered_user_id:s.registered_user_id,workspace_id:s.workspace_id,privacy_revision:s.privacy_revision}:{};}
function currentChatHistory(){const s=BankClient.identity();return (Storage.get('qingzhou_chatHistory')||[]).filter(m=>m.registered_user_id===s?.registered_user_id&&m.workspace_id===s?.workspace_id&&m.privacy_revision===s?.privacy_revision);}
function validConversationScopeToken(value){
  return typeof value==='string'&&value.startsWith('pd1:')&&value.length>4&&value.length<=8192&&!/[\s\u0000-\u001f\u007f]/u.test(value);
}
function sameConversationIdentity(a,b){
  return !!a&&!!b&&a.registered_user_id===b.registered_user_id&&a.workspace_id===b.workspace_id&&a.privacy_revision===b.privacy_revision;
}
function clearConversationScope({preserveStorage=false}={}){
  conversationScope=null;conversationScopeBinding=null;conversationScopeInvalid=false;
  if(!preserveStorage)try{sessionStorage.removeItem(conversationScopeStorageKey);}catch(_){}
}
function restoreConversationScope(identity=BankClient.identity()){
  clearConversationScope({preserveStorage:true});
  let raw;
  try{raw=sessionStorage.getItem(conversationScopeStorageKey);}catch(_){conversationScopeInvalid=true;return false;}
  if(raw===null)return true;
  try{
    const stored=JSON.parse(raw);
    if(!stored||Object.keys(stored).sort().join(',')!=='contract,conversation_scope,execution_authority,privacy_revision,registered_user_id,workspace_id'||
      stored.contract!=='navren.conversation-scope/1'||stored.execution_authority!==false||!validConversationScopeToken(stored.conversation_scope))throw Error('invalid_scope');
    if(!sameConversationIdentity(stored,identity)){clearConversationScope();return true;}
    conversationScope=stored.conversation_scope;conversationScopeBinding=Object.freeze(chatBinding());return true;
  }catch(_){clearConversationScope();conversationScopeInvalid=true;return false;}
}
function currentConversationScope(){
  if(conversationScopeBinding&&!sameConversationIdentity(conversationScopeBinding,BankClient.identity()))clearConversationScope();
  return conversationScope;
}
// A fresh browser tab has no session scope. Issue a server-authenticated
// range before enabling its first send; never fall back to workspace history.
async function ensureInitialConversationScope(identity){
  if(conversationScopeInvalid)return false;
  if(currentConversationScope()!==null)return true;
  if(chatSending||newConversationStarting)return false;
  newConversationStarting=true;const issueRun=++conversationIssueRun,version=chatRequestVersion;updateChatSendState();
  try{
    const guard=BankClient.guard();
    if(!guard.valid(identity))throw Error('账户身份已变化，请重新核对。');
    const receipt=await BankClient.post('/dialogue/new-conversation',{});
    if(issueRun!==conversationIssueRun||version!==chatRequestVersion||!guard.valid(identity))throw Error('账户或页面已变化，新对话未建立。');
    if(!receipt||typeof receipt!=='object'||Array.isArray(receipt)||receipt.contract!=='navren.conversation-scope/1'||
      receipt.execution_authority!==false||!validConversationScopeToken(receipt.conversation_scope))throw Error('新对话范围尚未核实，请点击新对话重试。');
    const stored={contract:receipt.contract,conversation_scope:receipt.conversation_scope,execution_authority:false,
      registered_user_id:identity.registered_user_id,workspace_id:identity.workspace_id,privacy_revision:identity.privacy_revision};
    sessionStorage.setItem(conversationScopeStorageKey,JSON.stringify(stored));
    conversationScope=receipt.conversation_scope;conversationScopeBinding=Object.freeze({...chatBinding()});conversationScopeInvalid=false;
    return true;
  }catch(error){
    if(issueRun===conversationIssueRun&&version===chatRequestVersion){
      conversationScopeInvalid=true;
      if(typeof showToast==='function')showToast(error.message||'新对话暂时无法建立，请点击新对话重试。');
    }
    return false;
  }finally{
    if(issueRun===conversationIssueRun){newConversationStarting=false;updateChatSendState();}
  }
}
async function collapseConversationArchive(run,guard){
  if(run!==chatArchiveRun||!guard.valid())return false;
  const view=historyView(),toggle=document.querySelector('#chatMessages .chat-history-toggle');
  if(!toggle)return run===chatArchiveRun&&guard.valid();
  if(view?.isExpanded()){
    if(!toggle)throw Error('对话历史入口尚未就绪，请重新读取。');
    toggle.click();
  }
  const panel=document.querySelector('#chatMessages #chatHistoryPanel');
  const deadline=Date.now()+1200;
  while(panel&&!panel.hidden){
    if(run!==chatArchiveRun||!guard.valid())return false;
    if(Date.now()>=deadline)throw Error('对话历史尚未折叠，请重新读取。');
    await new Promise(resolve=>setTimeout(resolve,16));
  }
  return run===chatArchiveRun&&guard.valid()&&(!view||!view.isExpanded());
}
async function startNewConversation(){
  if(chatSending||newConversationStarting)return false;
  newConversationStarting=true;const issueRun=++conversationIssueRun,version=chatRequestVersion;updateChatSendState();
  try{
    const identity=await BankClient.refreshIdentity(),guard=BankClient.guard();
    if(chatSending||issueRun!==conversationIssueRun||version!==chatRequestVersion||!guard.valid(identity))throw Error('账户身份已变化，请重新核对。');
    const receipt=await BankClient.post('/dialogue/new-conversation',{});
    if(chatSending||issueRun!==conversationIssueRun||version!==chatRequestVersion||!guard.valid(identity))throw Error('账户或页面已变化，新对话未切换。');
    if(!receipt||typeof receipt!=='object'||Array.isArray(receipt)||receipt.contract!=='navren.conversation-scope/1'||
      receipt.execution_authority!==false||!validConversationScopeToken(receipt.conversation_scope))throw Error('新对话范围尚未核实，请重试。');
    const stored={contract:receipt.contract,conversation_scope:receipt.conversation_scope,execution_authority:false,
      registered_user_id:identity.registered_user_id,workspace_id:identity.workspace_id,privacy_revision:identity.privacy_revision};
    sessionStorage.setItem(conversationScopeStorageKey,JSON.stringify(stored));
    conversationScope=receipt.conversation_scope;conversationScopeBinding=Object.freeze({...chatBinding()});conversationScopeInvalid=false;
    chatRequestVersion++;window.NavrenReplyInteractions?.clear();cancelVoice();NavrenSpeech.stop();
    const input=document.getElementById('userInput');if(input)input.value='';
    try{removeImage();clearChatReference();}catch(_){}
    const loaded=await initializeChatArchive({identity});
    if(!loaded)throw Error('新对话已建立，聊天历史暂时无法读取，请点击重新读取。');
    const host=document.getElementById('chatMessages');
    if(host&&!host.querySelector('#welcomeMsg')&&chatWelcomeTemplate){
      host.append(chatWelcomeTemplate.cloneNode(true));personalizeWelcome();renderPresets();
    }
    input?.focus();return true;
  }catch(error){
    if(typeof showToast==='function')showToast(error.message||'新对话尚未建立，请重试。');
    return false;
  }finally{
    if(issueRun===conversationIssueRun){newConversationStarting=false;updateChatSendState();}
  }
}
function newChatMessageId(){return globalThis.crypto?.randomUUID?.()||'chat-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);}
function consumeReplyControl(message,kind,binding=chatBinding()){
  if(!message||binding.workspace_id!==BankClient.identity()?.workspace_id||binding.privacy_revision!==BankClient.identity()?.privacy_revision)return false;
  message.__navrenConsumedReplyControls={...message.__navrenConsumedReplyControls,[kind]:true};
  if(kind==='retry'){
    message.querySelectorAll('.chat-retry').forEach(button=>button.remove());
    message.querySelector('.message-tools')?.classList.remove('has-retry');
  }
  if(kind==='followups')message.querySelectorAll('.message-suggestions').forEach(region=>region.remove());
  const history=currentChatHistory(),entry=history.findLast(item=>['ai','assistant'].includes(item.role)&&
    (message.dataset.clientMessageId?item.client_message_id===message.dataset.clientMessageId:
      !!message.dataset.bankResponseId&&item.bank_response_id===message.dataset.bankResponseId));
  if(entry){
    entry.consumed_reply_controls={...entry.consumed_reply_controls,[kind]:true};
    if(kind==='followups')entry.follow_up_questions=[];
    Storage.set('qingzhou_chatHistory',history);
  }
  return true;
}

function approvedReplyInteraction(result,binding){
  if(!window.NavrenReplyInteractions||result?.isFallback||result?.bank!==true||result.environment!=='simulation'||
    result.workspace_id!==binding.workspace_id||result.privacy_revision!==binding.privacy_revision||
    result.answer_contract?.kind!=='reviewed_text'||result.agent_review?.status!=='approved'||
    (result.bank_actions||[]).length||typeof result.bank_response_id!=='string'||!result.bank_response_id)return null;
  return NavrenReplyInteractions.normalize(result.reply_interaction,{...binding,response_id:result.bank_response_id});
}
function saveReplyInteractionState(binding,responseId,snapshot){
  if(!snapshot||binding.workspace_id!==BankClient.identity()?.workspace_id||binding.privacy_revision!==BankClient.identity()?.privacy_revision)return;
  const history=currentChatHistory(),entry=history.findLast(item=>['ai','assistant'].includes(item.role)&&item.bank_response_id===responseId);
  if(entry){entry.reply_interaction=snapshot;Storage.set('qingzhou_chatHistory',history);}
}
function renderReplyInteraction(message,result,binding,requestVersion){
  const spec=approvedReplyInteraction(result,binding);if(!spec)return false;
  const guard=BankClient.guard?.(),privacyTurn=globalThis.NavrenChatPrivacyVersion||0;
  const isCurrent=()=>message.isConnected&&requestVersion===chatRequestVersion&&
    [...document.querySelectorAll('#chatMessages .message')].at(-1)===message&&
    privacyTurn===(globalThis.NavrenChatPrivacyVersion||0)&&binding.workspace_id===BankClient.identity()?.workspace_id&&
    binding.privacy_revision===BankClient.identity()?.privacy_revision&&(!guard||guard.valid(binding));
  return NavrenReplyInteractions.render(message,spec,{...binding,response_id:result.bank_response_id,isCurrent,isBusy:()=>chatSending,
    onState:snapshot=>saveReplyInteractionState(binding,result.bank_response_id,snapshot),send:async(text,interaction)=>{
      if(chatSending||!isCurrent()||Date.now()>=Date.parse(interaction.expires_at))return false;
      await BankClient.refreshIdentity();
      if(chatSending||!isCurrent()||Date.now()>=Date.parse(interaction.expires_at))return false;
      // 独立纯文本请求，不使用或修改输入框、待发送附件及关联资料。
      const promise=sendMessage(null,{text,reference:null,return_view:{page:'chat',view:'home'},internal_choice:true},
        {internalChoice:true,keepInteractionId:interaction.interaction_id});
      Promise.resolve(promise).catch(error=>{if(typeof showToast==='function')showToast(error.message);});
      return chatSending===true;
    }});
}
function restoreReplyInteraction(message,entry){
  if(!entry?.reply_interaction||!window.NavrenReplyInteractions)return;
  NavrenReplyInteractions.render(message,entry.reply_interaction,{workspace_id:entry.workspace_id,
    privacy_revision:entry.privacy_revision,response_id:entry.bank_response_id,historical:true});
}

function verifiedReplyProjection(result,binding){
  const p=result?.reply_presentation, ids=result?.answer_contract?.fact_ids;
  if(!window.NavrenReplyComponents||result?.isFallback||result?.bank!==true||
    result.environment!=='simulation'||result.workspace_id!==binding.workspace_id||
    (Number.isSafeInteger(result.privacy_revision)&&result.privacy_revision!==binding.privacy_revision)||
    result.answer_contract?.kind!=='verified_facts'||!Array.isArray(ids)||!ids.length||
    ids.some(id=>typeof id!=='string'||!id)||new Set(ids).size!==ids.length||
    !p||typeof p.lead!=='string'||!p.lead.trim()||p.lead.length>1600||
    !Array.isArray(p.fact_ids)||p.fact_ids.length!==ids.length||p.fact_ids.some(id=>!ids.includes(id)))return null;
  return NavrenReplyComponents.snapshot(p,{workspace_id:binding.workspace_id,
    response_id:result.bank_response_id,answer_kind:'verified_facts',isFallback:false});
}

function renderReplyProjection(message,presentation,binding,{historical=false}={}){
  if(!window.NavrenReplyComponents)return false;
  const guard=BankClient.guard?.(), privacyTurn=globalThis.NavrenChatPrivacyVersion||0;
  const isCurrent=()=>message.isConnected&&binding.workspace_id===BankClient.identity()?.workspace_id&&
    binding.privacy_revision===BankClient.identity()?.privacy_revision&&
    privacyTurn===(globalThis.NavrenChatPrivacyVersion||0)&&(!guard||guard.valid(binding));
  const context={workspace_id:binding.workspace_id,response_id:presentation.response_id,
    answer_kind:'verified_facts',historical,isCurrent,onQuestion:question=>{
      if(historical||!isCurrent()||chatSending||typeof question!=='string'||!question.trim())return;
      clearChatReference();
      const input=document.getElementById('userInput');input.value=question;sendMessage();
    }};
  if(!NavrenReplyComponents.render(message,presentation,context))return false;
  const body=message.querySelector('.message-body');
  if(body){
    // A verified lead is plain data, not model-provided HTML or Markdown.
    if (window.NavrenMarkdown?.renderPlain) body.innerHTML=NavrenMarkdown.renderPlain(presentation.lead);
    else body.textContent=presentation.lead;
    body.classList.add('reply-lead');
  }
  message.__navrenReplyProjection=presentation;
  message.__navrenReplyCurrent=isCurrent;
  return true;
}

function restoreReplyProjection(message,entry){
  if(!entry?.reply_presentation)return;
  const projection=verifiedReplyProjection({...entry,reply_presentation:entry.reply_presentation},chatBinding());
  if(projection&&renderReplyProjection(message,projection,chatBinding(),{historical:true}))return;
  const p=document.createElement('p');p.className='task-history-note';
  p.textContent='这条结构化记录的身份或来源版本无法确认，请重新查询。';
  message.querySelector('.bubble')?.append(p);
}

let chatArchive=null;
let chatHistoryView=null;
function historyView(){return chatHistoryView||=(window.NavrenChatHistoryView?.create());}
let chatArchiveRun=0;
let chatArchiveReady=false;
let chatArchiveBusy=false;
let chatArchiveNodes=new Map();
const chatArchiveMediaTasks=new Set();
const chatWelcomeTemplate=document.getElementById('welcomeMsg')?.closest('.message')?.cloneNode(true)||null;

function unloadChatArchive(){
  chatArchiveRun++;
  historyView()?.reset();
  window.VoiceMessages?.clear?.();
  window.NavrenChatMedia?.clear();
  const old=chatArchive;chatArchive=null;
  chatArchiveReady=false;chatArchiveBusy=false;chatArchiveNodes.clear();chatArchiveMediaTasks.clear();
  old?.unload();
  document.getElementById('chatMessages')?.replaceChildren();
  updateChatSendState();
}
function archiveHistoryEntry(row,binding){
  return {role:row.role==='user'?'user':'ai',content:row.text,...binding,
    client_message_id:row.message_id,bank_response_id:row.request_id,
    timestamp:row.created_at,client_origin:'archive',historical_readonly:true,
    archive_status:row.status,attachments:row.attachments||[],isFallback:row.kind==='safe-status'};
}
function archivedUserMessageHtml(row){
  // 归档引用只展示受控标签，不将历史参数交给当前 resolver 或操作入口。
  const english=window.NavrenI18n?.language==='en';
  const references=(row.references||[]).map(ref=>'<span class="chat-user-reference" role="group">'+
    '<span class="chat-user-reference-label">'+(english?'Reference':'关联资料')+'</span>'+
    '<span class="chat-user-reference-title" translate="no" data-i18n-skip>'+escapeHtml(ref.label)+'</span></span>').join('');
  const attachment=(row.attachments||[]).some(item=>item.storage_status==='not_archived')?'<span class="chat-user-text">'+
    (english?'The original image was not archived.':'原图片未存档。')+'</span>':'';
  return references+attachment+(row.text?'<span class="chat-user-text">'+escapeHtml(row.text)+'</span>':'');
}
function mountArchivedMedia(message,row,run,guard){
  const tasks=[];
  const current=()=>run===chatArchiveRun&&guard.valid()&&message.isConnected;
  for(const raw of row.attachments||[]){
    if(raw?.storage_status==='not_archived')continue;
    let media;
    try{media=window.NavrenChatMedia?.validate(raw,{boundOnly:true});}catch(_){continue;}
    if(!media)continue;
    if(media.kind==='audio'){
      if(row.kind==='safe-status')continue;
      tasks.push(Promise.resolve(window.VoiceMessages?.restoreMedia(message,media,row.text,{assistant:row.role==='assistant'})).catch(error=>{
        if(!current()||error?.name==='AbortError'||['MEDIA_ABORTED','MEDIA_STALE'].includes(error?.code))return;
        const note=document.createElement('span');note.className='chat-user-text';note.textContent='语音暂时无法读取。';message.querySelector('.bubble')?.append(note);
      }));
      continue;
    }
    if(row.role!=='user')continue;
    const host=message.querySelector('.message-body');if(!host)continue;
    if(media.kind==='document'){window.NavrenChatAttachments?.mountStoredDocument(host,media,current);continue;}
    const note=document.createElement('span');note.className='chat-user-text';note.textContent='正在读取图片…';host.prepend(note);
    tasks.push(window.NavrenChatMedia.open(media).then(handle=>{
      if(!current()){handle.dispose();return;}
      const image=document.createElement('img');image.alt='聊天图片';image.style.cssText='max-width:200px;border-radius:8px;margin-bottom:6px;display:block';
      image.onload=()=>{if(!current()){handle.dispose();image.remove();}};
      image.onerror=()=>{handle.dispose();if(current()){image.remove();note.textContent='图片暂时无法读取。';host.prepend(note);}};
      image.src=handle.url;note.replaceWith(image);
    }).catch(()=>{if(current())note.textContent='图片暂时无法读取。';}));
  }
  return Promise.allSettled(tasks);
}
function renderArchivedComponents(message,row,guard){
  const display=row.readonly_components;
  if(!display||row.role!=='assistant'||row.kind==='safe-status')return;
  const current=()=>message.isConnected&&guard.valid();
  const binding=chatBinding(),projection=display.reply_presentation;
  if(projection&&window.NavrenReplyComponents){
    // 只消费归档 reader 的静态投影；historical 禁止发送问题和当前数据查询。
    NavrenReplyComponents.render(message,projection,{workspace_id:binding.workspace_id,
      response_id:row.request_id,historical:true,isCurrent:current});
  }
  const presentations=[display.presentation,...(display.widgets||[]).map(widget=>widget.presentation)].filter(Boolean);
  const seen=new Set(),bubble=message.querySelector('.bubble');if(!bubble)return;
  const node=(tag,text,cls)=>{const item=document.createElement(tag);if(text!=null)item.textContent=text;if(cls)item.className=cls;return item;};
  for(const presentation of presentations){
    if(presentation.contract!=='navren.presentation/1'||presentation.layout!=='funding_blocks'||
      presentation.workspace_binding!==binding.workspace_id||presentation.message_id!==row.request_id||
      !Array.isArray(presentation.points)||seen.has(presentation.view_id))continue;
    seen.add(presentation.view_id);
    const root=node('section',null,'financial-presentation-widget');root.dataset.historyReadonly='true';
    root.append(node('h3',presentation.title||'资金快照'),node('p',window.NavrenI18n?.language==='en'?'Historical snapshot':'历史快照','calendar-note'));
    const grid=node('div',null,'calendar-summary');
    for(const point of presentation.points){
      if(!point||typeof point.label!=='string'||!point.value)continue;
      const value=point.value,card=node('article',null,'calendar-metric');
      const digits=Number.isSafeInteger(value.value_minor)?String(Math.abs(value.value_minor)).padStart(3,'0'):null;
      const amount=digits&&typeof value.currency==='string'
        ?(value.value_minor<0?'−':'')+digits.slice(0,-2).replace(/\B(?=(\d{3})+(?!\d))/g,',')+'.'+digits.slice(-2)+' '+(value.currency==='CNY'?'元':value.currency)
        :window.NavrenI18n?.language==='en'?'Not provided':'未提供合计';
      card.append(node('span',point.label),node('strong',amount));
      if(Array.isArray(point.missing)&&point.missing.length)card.append(node('p',window.NavrenI18n?.language==='en'?'The snapshot has incomplete coverage.':'快照资料覆盖不完整。','calendar-note'));
      grid.append(card);
    }
    root.append(grid);
    // 依据沿用原快照文字；不重新计算金额、比例或请求当前批准原件。
    if(Array.isArray(presentation.important_states))for(const note of presentation.important_states)if(typeof note==='string')root.append(node('p',note,'calendar-note'));
    bubble.append(root);
  }
}
function renderChatArchiveState(state,run,guard){
  if(run!==chatArchiveRun||!guard.valid())return;
  const container=document.getElementById('chatMessages');if(!container)return;
  const first=!chatArchiveReady,oldHeight=container.scrollHeight,oldTop=container.scrollTop;
  chatArchiveBusy=state.phase==='loading';
  if(state.phase==='detached'){container.replaceChildren();chatArchiveReady=false;updateChatSendState();return;}
  if(state.phase==='error'&&state.error?.recovery==='restart'){
    window.VoiceMessages?.clear?.();window.NavrenChatMedia?.clear();
    container.replaceChildren();chatArchiveNodes.clear();chatArchiveReady=false;
  }
  const live=[...container.children].filter(node=>!node.matches('[data-chat-archive-row],.chat-archive-page-control,[data-chat-history]')&&!node.querySelector('#welcomeMsg'));
  const ordered=[];
  for(const row of state.messages){
    let message=chatArchiveNodes.get(row.message_id);
    if(!message){
      const role=row.role==='user'?'user':'ai';
      message=addMessage(role,role==='user'?archivedUserMessageHtml(row):renderReplyText(row.text),row.kind==='safe-status',{historical:true});
      message.dataset.chatArchiveRow=row.message_id;
      message.dataset.historyReadonly='true';
      message.dataset.bankResponseId=row.request_id;
      renderArchivedComponents(message,row,guard);
      chatArchiveNodes.set(row.message_id,message);
      // 下一微任务时消息已在本次排序后的容器内；旧页/旧身份不允许挂载原件。
      queueMicrotask(()=>{
        if(run!==chatArchiveRun||!guard.valid()||!message.isConnected)return;
        const mediaTask=mountArchivedMedia(message,row,run,guard);
        chatArchiveMediaTasks.add(mediaTask);mediaTask.finally(()=>chatArchiveMediaTasks.delete(mediaTask));
      });
    }
    ordered.push(message);
  }
  const control=document.createElement('div');control.className='chat-archive-page-control';
  const button=document.createElement('button');button.type='button';button.className='btn btn-ghost';
  const english=window.NavrenI18n?.language==='en';
  if(state.phase==='error'){
    const note=document.createElement('p');note.role='alert';note.textContent=english?'Chat history could not be loaded. Please retry.':'聊天历史暂时无法读取，请重试。';
    control.append(note);button.textContent=english?'Retry':'重新读取';button.onclick=()=>state.error?.recovery==='restart'?initializeChatArchive():loadEarlierChatArchive();control.append(button);
  }else if(state.phase==='idle'||state.phase==='loading'){
    const note=document.createElement('p');note.role='status';note.textContent=english?'Loading chat history…':'正在读取聊天历史…';control.append(note);
  }else if(state.has_more){
    button.textContent=english?'Load earlier messages':'加载更早消息';button.onclick=()=>loadEarlierChatArchive();button.disabled=chatSending;control.append(button);
  }
  if(state.phase==='ready'&&state.is_empty===true&&!live.length&&chatWelcomeTemplate){
    const welcome=chatWelcomeTemplate.cloneNode(true);ordered.push(welcome);
  }
  const archivedNodes=[...(control.childNodes.length?[control]:[]),...ordered];
  const history=state.messages.length?historyView()?.render(archivedNodes):null;
  if(history){
    history.style.setProperty('--chat-history-gap',getComputedStyle(container).rowGap);
    container.replaceChildren(history,...live);
  }else container.replaceChildren(...archivedNodes,...live);
  if(state.phase==='ready'){
    chatArchiveReady=true;
    const binding=chatBinding(),archived=state.messages.map(row=>archiveHistoryEntry(row,binding));
    const known=new Set(state.messages.map(row=>row.request_id));
    const liveEntries=currentChatHistory().filter(row=>row.client_origin==='live'&&!known.has(row.bank_response_id));
    Storage.set('qingzhou_chatHistory',[...archived,...liveEntries]);
    if(state.is_empty===true){
      personalizeWelcome();renderPresets();
      const welcomeText=document.getElementById('welcomeMsg');
      if(welcomeText)attachMessageSpeech(welcomeText.closest('.message'),welcomeText.textContent||'');
    }
    if(first){
      if(state.messages.length&&historyView()&&!historyView().isExpanded())container.scrollTop=0;
      else scrollToBottom();
    }else container.scrollTop=oldTop+Math.max(0,container.scrollHeight-oldHeight);
  }
  updateChatSendState();
}
async function initializeChatArchive({identity:verifiedIdentity=null}={}){
  unloadChatArchive();
  const run=chatArchiveRun;
  try{
    const identity=verifiedIdentity||await BankClient.refreshIdentity();if(run!==chatArchiveRun)return false;
    const guard=BankClient.guard();if(!guard.valid(identity)||!window.NavrenChatArchive||!window.NavrenChatHistoryView?.create)throw Error('archive_unavailable');
    const archive=NavrenChatArchive.create({identityProvider:()=>BankClient.identity(),
      readPage:async({cursor,limit,signal})=>{
        if(signal.aborted||run!==chatArchiveRun||!guard.valid())throw Error('stale_archive');
        const query=new URLSearchParams({limit:String(limit)});if(cursor!==null)query.set('cursor',cursor);
        const page=await BankClient.get('/dialogue/history?'+query);
        if(signal.aborted||run!==chatArchiveRun||!guard.valid())throw Error('stale_archive');
        return page;
      },onChange:state=>renderChatArchiveState(state,run,guard)});
    chatArchive=archive;archive.attach();
    // 仅迁移当前私有分区仍有原录音、且具有真实服务端回合ID的旧记录。
    // 无父回合的旧录音继续保留在本机，不伪造过去的发送或审核。
    if(window.VoiceMessages?.migrateOwned){
      const legacy=currentChatHistory().filter(entry=>entry.role==='user'&&entry.recording_id&&entry.bank_response_id&&
        !(entry.attachments||[]).some(media=>media.contract==='navren.chat-media/1'&&media.storage_status==='bound'));
      for(const entry of legacy){
        if(run!==chatArchiveRun||!guard.valid())return false;
        try{
          const migrated=await VoiceMessages.migrateOwned(entry);
          if(run!==chatArchiveRun||!guard.valid())return false;
          if(migrated?.status==='migrated'){
            const history=currentChatHistory(),saved=history.find(row=>row.role==='user'&&row.recording_id===entry.recording_id&&row.bank_response_id===entry.bank_response_id);
            if(saved){saved.attachments=[migrated.media];Storage.set('qingzhou_chatHistory',history);}
          }
        }catch(_){if(run!==chatArchiveRun||!guard.valid())return false;}
      }
    }
    await archive.loadNext();
    if(run!==chatArchiveRun||!guard.valid())return false;
    await Promise.allSettled([...chatArchiveMediaTasks]);
    if(currentConversationScope()!==null&&!await collapseConversationArchive(run,guard))return false;
    return run===chatArchiveRun&&guard.valid();
  }catch(_){
    if(run!==chatArchiveRun)return false;
    chatArchiveBusy=false;chatArchiveReady=false;
    const host=document.getElementById('chatMessages');if(host&&!host.querySelector('[role="alert"]')){
      host.replaceChildren();const note=document.createElement('p');note.role='alert';note.textContent='聊天历史暂时无法读取，请重试。';
      const retry=document.createElement('button');retry.type='button';retry.className='btn btn-ghost';retry.textContent='重新读取';retry.onclick=()=>initializeChatArchive();host.append(note,retry);
      if(BankClient.bankReauthenticationRequired?.()){
        const select=document.createElement('button');select.type='button';select.className='btn btn-ghost';
        select.textContent=window.NavrenI18n?.language==='en'?'Select this experience again':'重新选择当前体验画像';
        select.onclick=async()=>{select.disabled=true;try{await BankClient.reselectExperience();await initializeChatArchive();}
          catch(_){if(select.isConnected){select.disabled=false;note.textContent=window.NavrenI18n?.language==='en'?'The experience could not be verified. Please retry.':'体验画像暂时无法核对，请重试。';}}};
        host.append(select);
      }
    }
    updateChatSendState();return false;
  }
}
async function loadEarlierChatArchive(){
  if(!chatArchive||chatSending||chatArchiveBusy)return false;
  try{await chatArchive.loadNext();return true;}catch(_){return false;}
}
function resetPrivateChat(event){
  conversationIssueRun++;newConversationStarting=false;
  clearConversationScope({preserveStorage:event?.type==='pagehide'});
  chatRequestVersion++;chatSending=false;
  window.NavrenReplyInteractions?.clear();
  globalThis.NavrenChatPrivacyVersion=(globalThis.NavrenChatPrivacyVersion||0)+1;
  cancelVoice();
  const input=document.getElementById('userInput');if(input)input.value='';
  try{removeImage();clearChatReference();}catch(_){}
  unloadChatArchive();
}
for(const event of ['navren:privacy-reset','navren:identity-revoked','navren:account-change','navren:account-switched','pagehide'])window.addEventListener(event,resetPrivateChat);
window.addEventListener('storage',event=>{
  const identity=Storage.privateIdentity?.();
  if(identity&&event.key==='navren:private-cache:provisional:v1:'+identity.registered_user_id+':'+identity.workspace_id+':meta'){
    window.NavrenPageLoad?.invalidate();resetPrivateChat();
  }
});
window.addEventListener('pageshow',async event=>{
  if(!event.persisted||document.body.dataset.page!=='chat')return;
  let pageLoad=window.NavrenPageLoad?.join();let run;
  try{
    const identity=await BankClient.refreshIdentity();await Storage.ready();
    if(!BankClient.guard().valid(identity))return;
    restoreConversationScope(identity);
    await ensureInitialConversationScope(identity);
    if(!BankClient.guard().valid(identity))return;
    pageLoad=window.NavrenPageLoad?.resume();
    const restoration=initializeChatArchive({identity});run=chatArchiveRun;
    const restored=await restoration;if(run!==chatArchiveRun)return;
    if(!restored){pageLoad?.fail(new Error('聊天历史暂时无法读取，请重试。'));return;}
    await pageLoad?.ready();
  }catch(error){if(run==null||run===chatArchiveRun)pageLoad?.fail(error);}
});

// ── Init ──
document.addEventListener('DOMContentLoaded', async () => {
  if(document.body.dataset.page!=='chat')return;
  let pageLoad=window.NavrenPageLoad?.join();
  try{
  const params = new URLSearchParams(window.location.search);
  let initialIdentity;
  try{initialIdentity=await BankClient.refreshIdentity();}catch(e){
    const welcome=document.getElementById('welcomeMsg');if(welcome)welcome.textContent='账户身份暂时无法核对，请刷新后重试。';pageLoad?.fail(e);return;
  }
  try { await Storage.ready(); } catch (error) {
    const welcome=document.getElementById('welcomeMsg');if(welcome)welcome.textContent='账号设置暂时无法恢复，请刷新后重试。';pageLoad?.fail(error);return;
  }
  const pageGuard=BankClient.guard(),privacyTurn=globalThis.NavrenChatPrivacyVersion||0;
  if(!pageGuard.valid(initialIdentity))return;
  restoreConversationScope(initialIdentity);
  await ensureInitialConversationScope(initialIdentity);
  if(!pageGuard.valid(initialIdentity)||privacyTurn!==(globalThis.NavrenChatPrivacyVersion||0))return;
  pageLoad=window.NavrenPageLoad?.resume();
  const pageCurrent=()=>pageGuard.valid()&&privacyTurn===(globalThis.NavrenChatPrivacyVersion||0);
  // 旧URL只作预填。图谱对象必须由服务器重新核对，不能信任URL里的个人JSON。
  window.NavrenGraphContext=null;
  if(['event','article','weekly'].includes(params.get('market_kind'))&&params.get('market_id')){
    window.NavrenMarketContext={kind:params.get('market_kind'),id:params.get('market_id')};
  }

  currentMode = getInterfaceMode();

  document.body.className = 'mode-' + currentMode;
  // mode badge removed from chat header

  // 欢迎内容只在服务端确认空归档后一次构建。

  // 应用字体：优先用户保存的，否则按模式默认
  const savedFontSize = getModeFont(currentMode);
  const defaultFont = savedFontSize || MODE_DEFAULT_FONT[currentMode] || 'medium';
  applyFontSize(defaultFont);

  setupVoice();
  checkPrivacyGuide();
  await checkVoiceGuide();

  // 恢复当前注册用户实例的服务端完整归档；浏览器缓存不作为恢复真源。
  const archiveLoaded=await initializeChatArchive({identity:initialIdentity});
  if(!pageCurrent())return;

  const input = document.getElementById('userInput');
  input.addEventListener('input', () => {
    updateChatSendState();
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  });

  const queuedQuestion=params.get('context')?'':params.get('q') || (params.get('action')==='reassess'?'了解我的风险偏好与当前业务风险配置':params.get('action')==='handoff'?'我想联系人工顾问':'');
  if(queuedQuestion){input.value=queuedQuestion;input.dispatchEvent(new Event('input'));}
  // 清理旧私密查询参数，不赋予任何自动执行语义。
  const clean=new URL(location.href);for(const key of ['q','action','reason','graph_context'])clean.searchParams.delete(key);
  history.replaceState(history.state,'',clean.pathname+clean.search+clean.hash);
  if(params.get('context'))await receivePageQuestion(params.get('context'),{focus:false});

  // 回访变化摘要
  showReengagementSummary();

  // 场景引导提示
  await showContextualTip('chat');
  if(!pageCurrent())return;
  if(!archiveLoaded){pageLoad?.fail(new Error('聊天历史暂时无法读取，请重试。'));return;}
  const revealed=await pageLoad?.ready();
  if(revealed!==false&&pageCurrent()&&input.isConnected&&!document.hidden)input.focus({preventScroll:true});
  }catch(error){pageLoad?.fail(error);throw error;}
});

// ── Presets ──
function renderPresets() {
  const bar = document.getElementById('presetBar');
  if (!bar) return;
  bar.innerHTML = '';
  (PRESET_QUESTIONS[currentMode] || PRESET_QUESTIONS.classic).forEach(q => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'preset-chip';
    chip.textContent = q;
    chip.onclick = () => { document.getElementById('userInput').value = q; sendMessage(); };
    bar.appendChild(chip);
  });
}

// ── Voice ──
let voiceConversation = false;
let voiceResumeTimer = null;
let voiceRequest = 0;
let voiceInputState = 'idle';

let pendingVoiceNameReview = null;
const VOICE_NAME_REVIEW_NOTICE='请核对产品名称及份额后发送';

function voiceNameReviewCurrent(review) {
  return !!review&&review.request===voiceRequest&&review.privacyTurn===(globalThis.NavrenChatPrivacyVersion||0)&&
    review.guard.valid(review.binding);
}
function clearVoiceNameReview({clearDraft=false}={}) {
  const review=pendingVoiceNameReview;pendingVoiceNameReview=null;
  if(!review)return;
  const input=document.getElementById('userInput');
  if(clearDraft&&input?.value===review.raw_text&&chatDraftRevision===review.draftRevision){
    input.value='';input.dispatchEvent(new Event('input'));
  }
  const status=document.getElementById('voiceStatus');
  if(status){status.textContent='';status.classList.remove('visible');}
  updateChatSendState();
}
function renderVoiceNameReview() {
  const review=pendingVoiceNameReview;
  if(!voiceNameReviewCurrent(review)){clearVoiceNameReview({clearDraft:true});return;}
  const status=document.getElementById('voiceStatus');
  if(status){
    status.textContent=VOICE_NAME_REVIEW_NOTICE+(review.names.length?'。参考名称：'+review.names.join('；'):'');
    status.classList.add('visible');
  }
  updateChatSendState();
}
async function checkedVoiceNameReview(raw,metadata) {
  // 兼容没有该字段的原服务；有字段但无法核对时只保留 raw 供本人编辑，不信任候选名。
  if(metadata===undefined)return null;
  const manual={names:[]};
  const keys=['schema_version','status','reason','notice','names','raw_sha256','public_release_id','hint_only','identity_authority'];
  if(!metadata||typeof metadata!=='object'||Array.isArray(metadata)||Object.keys(metadata).some(key=>!keys.includes(key))||
    metadata.schema_version!=='navren.asr-name-review.v1'||!['review_required','not_required'].includes(metadata.status)||
    metadata.hint_only!==true||metadata.identity_authority!==false||
    !['possible_public_name_mismatch','no_public_name_mismatch','public_name_hints_unavailable','candidate_assist_limit'].includes(metadata.reason)||
    typeof metadata.notice!=='string'||metadata.notice.length>512||
    (metadata.status==='review_required'&&metadata.notice!==VOICE_NAME_REVIEW_NOTICE)||
    !Array.isArray(metadata.names)||metadata.names.length>3||new Set(metadata.names).size!==metadata.names.length||metadata.names.some(name=>
      typeof name!=='string'||!name||name!==name.trim()||name.length>160||/[\u0000-\u001f\u007f]/.test(name))||
    (metadata.status==='not_required'&&(metadata.names.length!==0||metadata.notice!==''||metadata.reason!=='no_public_name_mismatch'))||
    typeof metadata.raw_sha256!=='string'||!/^[a-f0-9]{64}$/.test(metadata.raw_sha256)||
    !(typeof metadata.public_release_id==='string'&&/^[A-Za-z0-9:_-]{1,128}$/.test(metadata.public_release_id)||
      metadata.public_release_id===null&&metadata.names.length===0))return manual;
  try {
    const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(raw));
    const hex=Array.from(new Uint8Array(digest),byte=>byte.toString(16).padStart(2,'0')).join('');
    if(hex!==metadata.raw_sha256)return manual;
  }catch(_){return manual;}
  return metadata.status==='review_required'?{names:[...new Set(metadata.names)]}:null;
}

function setupVoice() {
  const btn = document.getElementById('voiceBtn');
  btn.dataset.idleContent = btn.innerHTML;
  btn.classList.remove('hidden');
  // 沿用原发送键；这个标记只区分本人提交输入框，不是业务授权。
  const send=document.getElementById('sendBtn');
  if(send)send.onclick=()=>sendMessage(null,null,{manualVoiceReview:true});
  voiceButton();
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') cancelVoice();
  });
  window.addEventListener('pagehide', cancelVoice);
  document.addEventListener('visibilitychange', () => { if (document.hidden) cancelVoice(); });
}

function voiceButton(state = 'idle') {
  const btn = document.getElementById('voiceBtn');
  voiceInputState = state;
  isListening = ['connecting', 'listening', 'finishing'].includes(state);
  btn.classList.toggle('listening', isListening);
  if (state === 'idle') btn.innerHTML = btn.dataset.idleContent || '🎤';
  else btn.textContent = state === 'listening' ? '■' : '…';
  const labels = { idle: '语音输入', connecting: '正在连接语音，点击取消',
    listening: '正在听，再次点击结束录音；Esc 取消',
    finishing: '正在整理语音内容，点击取消；Esc 取消' };
  btn.title = labels[state] || labels.idle;
  btn.setAttribute('aria-label', btn.title);
  btn.setAttribute('aria-pressed', String(isListening));
  const status = document.getElementById('voiceStatus');
  if (status) {
    status.textContent = state === 'connecting' ? '正在连接语音服务…' :
      state === 'listening' ? '正在聆听，识别内容会显示在输入框。点击结束后发送并语音回答。' :
      state === 'finishing' ? '正在整理识别内容…' : '';
    status.classList.toggle('visible', Boolean(status.textContent));
  }
  // 正常收起录音器不等于本人取消，仍保留这一轮已绑定的待核对草稿。
  if(state==='idle'&&voiceNameReviewCurrent(pendingVoiceNameReview))renderVoiceNameReview();
}

function cancelVoice() {
  voiceConversation = false;
  clearTimeout(voiceResumeTimer);
  voiceRequest++;
  clearVoiceNameReview({clearDraft:true});
  NavrenSpeech.stop();
  voiceButton();
}

async function toggleVoice() {
  if (isListening) {
    if (voiceInputState === 'listening' && NavrenSpeech.activeKind === 'asr') NavrenSpeech.finishRecording();
    else cancelVoice();
    return;
  }
  clearTimeout(voiceResumeTimer);
  clearVoiceNameReview({clearDraft:true});
  voiceConversation = true;
  const request = ++voiceRequest;
  const binding=chatBinding(),guard=BankClient.guard(),privacyTurn=globalThis.NavrenChatPrivacyVersion||0;
  const current=()=>request===voiceRequest&&privacyTurn===(globalThis.NavrenChatPrivacyVersion||0)&&guard.valid(binding);
  if(!binding.workspace_id||!current()){voiceConversation=false;voiceButton();return;}
  const input = document.getElementById('userInput');
  const before = input.value;
  let voiceMessage=null,recording=null,nameReview;
  try {
    const text = await NavrenSpeech.startASR({ finishOnSentence: false,
      onState: state=>{if(current())voiceButton(state);},
      onRecording: async clip=>{
        if(!current())return;
        recording=await VoiceMessages.save(clip);
        if(!current())return;
        voiceMessage=addMessage('user','');voiceMessage.dataset.recordingId=recording.id;VoiceMessages.mount(voiceMessage,recording);
        const history=currentChatHistory();
        history.push({role:'user',client_origin:'live',content:'',recording_id:recording.id,...chatBinding(),timestamp:new Date().toISOString()});
        Storage.set('qingzhou_chatHistory',history);scrollToBottom();
      },
      onTranscript: (text,final,review) => {
        if (!current()) return;
        if(review!==undefined)nameReview=review;
        input.value = text;
        input.dispatchEvent(new Event('input'));
      }
    });
    if (!current()) return;
    voiceButton();
    if(voiceMessage)VoiceMessages.update(voiceMessage,text);
    if(recording){const history=Storage.get('qingzhou_chatHistory')||[];const entry=history.find(m=>m.recording_id===recording.id);if(entry)entry.content=text;Storage.set('qingzhou_chatHistory',history);}
    if (text.trim()||recording?.media) {
      const draftValue=input.value,draftRevision=chatDraftRevision;
      const review=nameReview===undefined?null:await checkedVoiceNameReview(text,nameReview);
      if(!current())return;
      const voiceEntry=recording?{recording_id:recording.id,message:voiceMessage,media:recording.media,clip:recording}:null;
      const editedWhileChecking=input.value!==draftValue||chatDraftRevision!==draftRevision;
      if(review||editedWhileChecking){
        if(!editedWhileChecking)input.value=text;
        pendingVoiceNameReview={raw_text:text,names:review?.names||[],voiceEntry,request,guard,binding,privacyTurn,
          draftRevision:chatDraftRevision};
        input.dispatchEvent(new Event('input'));renderVoiceNameReview();input.focus({preventScroll:true});return;
      }
      input.value = text;
      await sendMessage(voiceEntry);
    } else { voiceConversation = false; showToast('未取得可保存的录音，请重新录音。'); }
  } catch (error) {
    if (!current()) return;
    voiceButton();
    voiceConversation = false;
    if (error.name === 'AbortError') { input.value = before; input.dispatchEvent(new Event('input')); }
    else if(recording?.media){
      // 转写失败与原始录音保存分开；不伪造转写文字，也不补发语音识别请求。
      input.value='';VoiceMessages.update(voiceMessage,'');
      await sendMessage({recording_id:recording.id,message:voiceMessage,media:recording.media,clip:recording});
    }else showToast(error.message);
  }
}

function stopListening() { NavrenSpeech.finishRecording(); }

// ── Privacy Guide ──
function checkPrivacyGuide() {
  if (!Storage.accountReady()) { document.getElementById('privacyGuide')?.classList.add('hidden'); return; }
  renderPrivacyGuide();
  document.getElementById('privacyGuide')?.classList.toggle('hidden', Storage.get('qingzhou_privacyRead') === true);
}

function renderPrivacyGuide() {
  const guide = document.getElementById('privacyGuide');
  if (!guide) return;
  const english = window.NavrenI18n?.language === 'en';
  const titles = english
    ? {classic:'Privacy & personalization',senior:'Your personal financial profile',youth:'Your data, your choice'}
    : {classic:'🔒 隐私与个性化服务说明',senior:'您的专属理财小档案',youth:'你的数据，你做主'};
  const copy = english ? [
    'To tailor its banking and everyday financial services to you, Navren learns about your investment preferences and assets during conversations and builds a personal profile.',
    'Conversations and demo account records are stored on this computer. Account summaries needed to answer your questions are sent to DeepSeek and GLM.',
    'Voice recordings are transcribed locally by SenseVoice, with cloud transcription disabled. Text approved for reading aloud is sent to the official Zhipu GLM TTS service. Original chat attachments are stored privately with your account; extracted document text needed to answer is sent to DeepSeek and GLM as user-provided material.',
    'Your data will not be used for other purposes without your confirmation.',
    'Manage conversation permissions in Profile. Change demo risk and funding settings in Accounts.'
  ] : [
    '为提供更贴合你的银行与生活财务服务，Navren会在对话中了解您的投资偏好和资产情况，并建立个人档案。',
    '对话与模拟账户记录保存在本机；回答问题所需的账户摘要会发送至 DeepSeek 和 GLM',
    '录音仅在本机通过 SenseVoice 转写，不启用云端转写；经核准的待朗读正文发送至智谱官方 GLM TTS。聊天附件随本人账户私密保存；回答问题所需的文档提取正文以本人提供资料的身份发送至 DeepSeek 和 GLM',
    '未经您确认，不会用于其他用途',
    '聊天授权在「我的」中管理；模拟风险与资金设置请在「账户」中修改'
  ];
  guide.lang = english ? 'en' : 'zh-CN';
  guide.querySelector('#privacyTitle').textContent = titles[currentMode] || titles.classic;
  guide.querySelectorAll('p,li').forEach((node,index) => { node.textContent = copy[index]; });
  guide.querySelector('button').textContent = english ? 'Got it' : '我知道了';
}
window.addEventListener('navren:language', renderPrivacyGuide);

async function acceptPrivacy() {
  const guide=document.getElementById('privacyGuide'),button=guide?.querySelector('button');
  if (!Storage.accountReady() || button?.disabled) return;
  if (button) button.disabled=true;
  try { await Storage.set('qingzhou_privacyRead', true); if(Storage.accountReady())guide?.classList.add('hidden'); }
  catch (_) { showToast('说明已读状态未保存，请重试。'); }
  finally { if(button?.isConnected)button.disabled=false; }
}

// ── Voice Guide ──
async function checkVoiceGuide() {
  if (!Storage.accountReady()) return;
  if (Storage.get('qingzhou_voiceGuideRead')) return;
  try { await Storage.set('qingzhou_voiceGuideRead', true); } catch (_) { return; }

  if (currentMode === 'senior') {
    setTimeout(() => showToast('点击麦克风说出问题，Navren 会显示听到的内容并为您回答。'), 1000);
  }
}

// 文件选择与剪贴板共用原件队列；点击发送前不上传。
function triggerUpload() {document.getElementById('imageInput').click();}
function handleImageUpload(event) {
  window.NavrenChatAttachments?.addFiles(Array.from(event.target.files||[]));event.target.value='';
}
function removeImage() {
  window.NavrenChatAttachments?.clear();
  const input=document.getElementById('imageInput');if(input)input.value='';
  updateChatSendState();
}
window.NavrenChatAttachments?.init({onChange:()=>{chatDraftRevision++;updateChatSendState();},notify:message=>showToast(message)});

// ── Send Message ──
async function sendMessage(voiceEntry = null, retrySnapshot = null, options = {}) {
  const input = document.getElementById('userInput');
  const text = retrySnapshot ? retrySnapshot.text : input.value.trim();
  if(chatSending){showToast('当前问题正在处理，请稍候。');return;}
  if(newConversationStarting){showToast('新对话正在准备，请稍候。');return;}
  if(conversationScopeInvalid){showToast('对话范围已失效，请开始新对话。');return;}
  if(!chatArchiveReady||chatArchiveBusy){showToast('聊天记录尚未准备完成，请稍候或重新读取。');return;}
  if(typeof text!=='string'){showToast('问题内容无法读取，请重新输入。');return;}
  const requestScope=currentConversationScope();
  if(requestScope===null){showToast('对话尚未建立，请点击新对话后再试。');return;}
  if(retrySnapshot&&retrySnapshot.internal_choice!==true&&(retrySnapshot.conversation_scope??null)!==requestScope){
    showToast('这个问题属于之前的对话，请在当前对话重新提问。');return;
  }
  let voiceReview=null;
  if(!voiceEntry&&!retrySnapshot&&options.internalChoice!==true&&pendingVoiceNameReview){
    if(!voiceNameReviewCurrent(pendingVoiceNameReview)){clearVoiceNameReview({clearDraft:true});return;}
    if(options.manualVoiceReview!==true){renderVoiceNameReview();return;}
    if(!text.trim())return;
    voiceReview=pendingVoiceNameReview;voiceEntry=voiceReview.voiceEntry;voiceConversation=true;
  }
  const reference = retrySnapshot ? cloneChatReference(retrySnapshot.reference) : currentChatReference();
  const internalChoice=options.internalChoice===true;
  const attachmentFiles=internalChoice?[]:retrySnapshot?(retrySnapshot.attachmentFiles|| (retrySnapshot.imageBlob?[retrySnapshot.imageBlob]:[])):(window.NavrenChatAttachments?.files()||[]);
  const voiceClip=internalChoice?null:voiceEntry?.clip||retrySnapshot?.voiceClip||null;
  if(!text.trim()&&!reference&&!attachmentFiles.length&&!voiceEntry?.recording_id&&!voiceClip?.blob)return;
  if((retrySnapshot?.had_attachments||retrySnapshot?.had_image)&&!attachmentFiles.length){
    showToast('原问题包含附件，请重新添加原附件后发送。');return;
  }
  const requestSnapshot = Object.freeze({text,conversation_scope:requestScope,reference:reference?JSON.parse(JSON.stringify(reference)):null,
    internal_choice:internalChoice,attachmentFiles:Object.freeze([...attachmentFiles]),had_attachments:attachmentFiles.length>0,
    had_image:attachmentFiles.some(file=>file.type.startsWith('image/')),voiceClip,had_audio:Boolean(voiceClip?.blob||voiceEntry?.recording_id),
    return_view:JSON.parse(JSON.stringify(retrySnapshot?.return_view||window.NavrenChatReturnView||{page:'chat',view:'home'}))});
  // 显式重试只使用冻结请求；即使新草稿内容相同，也不消费输入区。
  let consumesComposer=!internalChoice&&!retrySnapshot;
  const frozenDraftRevision=chatDraftRevision,frozenReferenceRevision=chatReferenceRevision;
  window.NavrenPageContextId=null;
  chatSending=true;
  const requestVersion=++chatRequestVersion;
  window.NavrenReplyInteractions?.invalidateAll(options.keepInteractionId||null);
  syncChatReference();
  clearFollowUpQuestions();
  const binding=chatBinding();
  const turnGuard=BankClient.guard?.();
  const privacyTurn=globalThis.NavrenChatPrivacyVersion||0;
  // 回合引用已经冻结；清空输入附件或准备下一条草稿不会使本轮失效。
  const turnCurrent=()=>requestVersion===chatRequestVersion&&privacyTurn===(globalThis.NavrenChatPrivacyVersion||0)&&
    binding.workspace_id===BankClient.identity()?.workspace_id&&binding.privacy_revision===BankClient.identity()?.privacy_revision&&
    (!turnGuard||turnGuard.valid(binding));
  clearTimeout(voiceResumeTimer);
  voiceRequest++;
  if(voiceReview)voiceReview.request=voiceRequest;else clearVoiceNameReview();
  const voiceTurn = voiceRequest;
  const userMessageId=voiceEntry?.recording_id||newChatMessageId();
  NavrenSpeech.stop();
  voiceButton();

  // 原件先完整上传，成功前保留输入草稿、附件和本机预览；普通 Agent JSON 只接收 ID。
  const media=[];
  updateChatSendState();
  try{
    for(const file of attachmentFiles){
      if(!window.NavrenChatMedia)throw Error('附件保存服务暂不可用，请稍后重试。');
      let descriptor=chatAttachmentUploads.get(file);
      if(!descriptor){
        let uploadKey=chatAttachmentUploadKeys.get(file);
        if(!uploadKey){uploadKey=Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');chatAttachmentUploadKeys.set(file,uploadKey);}
        descriptor=await NavrenChatMedia.upload(file,{uploadKey});
        if(!turnCurrent())return;
        chatAttachmentUploads.set(file,descriptor);
      }
      media.push(NavrenChatMedia.validate(descriptor));
    }
    if(voiceEntry?.recording_id){
      if(!voiceEntry.media||!window.NavrenChatMedia)throw Error('录音原件尚未保存，请重新录音。');
      media.push(NavrenChatMedia.validate(voiceEntry.media));
    }else if(voiceClip?.blob){
      if(!window.NavrenChatMedia)throw Error('录音保存服务暂不可用，请稍后重试。');
      const descriptor=await NavrenChatMedia.upload(voiceClip.blob);
      if(!turnCurrent())return;
      media.push(descriptor);
    }
    if(!turnCurrent())return;
  }catch(error){
    if(turnCurrent())showToast(error.name==='AbortError'?'附件保存已停止。':error.message||'附件未保存，请重试。');
    return;
  }finally{
    // 成功后由本轮继续持有发送锁；取消/失败时立即释放，不留下虚假的已发送消息。
    if(!turnCurrent()||media.length!==(attachmentFiles.length+Number(Boolean(voiceEntry?.recording_id||voiceClip?.blob)))){
      if(requestVersion===chatRequestVersion){
        chatSending=false;syncChatReference();updateChatSendState();
        if(voiceReview&&pendingVoiceNameReview===voiceReview&&voiceNameReviewCurrent(voiceReview))renderVoiceNameReview();
      }
    }
  }
  consumesComposer=consumesComposer&&chatDraftRevision===frozenDraftRevision&&chatReferenceRevision===frozenReferenceRevision&&
    (!attachmentFiles.length||window.NavrenChatAttachments?.matches(attachmentFiles));
  if(voiceReview&&pendingVoiceNameReview===voiceReview)clearVoiceNameReview();

  const userContent = chatUserMessageHtml(text, requestSnapshot.reference,requestSnapshot.return_view);
  let userMessage=voiceEntry?.message||null;
  if(!voiceEntry?.recording_id){userMessage=addMessage('user', userContent);userMessage.dataset.clientMessageId=userMessageId;}
  if(!voiceEntry?.recording_id&&voiceClip?.blob){VoiceMessages.mount(userMessage,{...voiceClip,media:media.find(item=>item.kind==='audio')},text);renderChatVoiceReference(userMessage,requestSnapshot.reference,requestSnapshot.return_view);}
  window.NavrenChatAttachments?.mountFiles(userMessage,attachmentFiles,media);
  if(consumesComposer){input.value='';if(requestSnapshot.reference)clearChatReference();removeImage();}
  else if(!internalChoice&&!retrySnapshot)window.NavrenChatAttachments?.consume(attachmentFiles);
  updateChatSendState();

  // 保存用户消息到历史（在所有 early return 之前）
  if(voiceEntry?.recording_id){
    VoiceMessages.update(voiceEntry.message,text);
    const history=currentChatHistory(),entry=history.findLast(item=>item.recording_id===voiceEntry.recording_id);
    if(entry){entry.content=text;entry.attachments=media;entry.reference_snapshot=requestSnapshot.reference;entry.request_snapshot={...(entry.request_snapshot||{}),return_view:requestSnapshot.return_view};Storage.set('qingzhou_chatHistory',history);}
    const message=voiceEntry.message||[...document.querySelectorAll('#chatMessages .message.user')].findLast(item=>item.dataset.recordingId===voiceEntry.recording_id);
    renderChatVoiceReference(message,requestSnapshot.reference,requestSnapshot.return_view);
  }else Storage.set('qingzhou_chatHistory', [
    ...currentChatHistory(),
    { role: 'user',client_origin:'live',client_message_id:userMessageId,content: text,attachments:media, reference_snapshot: requestSnapshot.reference, request_snapshot:{return_view:requestSnapshot.return_view}, ...binding, timestamp: new Date().toISOString() }
  ]);

  // 所有自然语言统一交给后端Agent理解；前端不按关键词弹问卷或模板拦截。
  const sentiment = null;

  const loadingMsg = addLoadingDots();
  const taskRequest=window.NavrenTaskChat?.begin();
  scrollToBottom();

  let result;
  try {
    let pageContextId=null;
    if(requestSnapshot.reference){
      // 每次发送及重试都重新解析同一对象版本并创建新票据；不提交 request_mode。
      const ticket=await BankClient.post('/page-contexts',{question:requestSnapshot.text,
        reference:chatReferencePacket(requestSnapshot.reference),return_view:requestSnapshot.return_view});
      if(!turnCurrent()){loadingMsg.remove();if(requestVersion===chatRequestVersion)chatSending=false;syncChatReference();taskRequest?.component.destroy();return;}
      if(typeof ticket.context_id!=='string'||!ticket.context_id)throw new Error('关联资料票据暂时无法核对。');
      pageContextId=ticket.context_id;
    }
    if(!turnCurrent()){loadingMsg.remove();if(requestVersion===chatRequestVersion)chatSending=false;syncChatReference();taskRequest?.component.destroy();return;}
    // 一旦提交回合，上传ID可能已被服务端绑定；显式重试重新上传原Blob，不跨请求重绑旧ID。
    for(const file of attachmentFiles){chatAttachmentUploads.delete(file);chatAttachmentUploadKeys.delete(file);}
    result = await Api.sendMessage(requestSnapshot.text, currentMode, null, sentiment,
      event => renderRuntimeEvent(loadingMsg,event),{page_context_id:pageContextId,isolated_text:internalChoice,media_ids:media.map(item=>item.media_id),conversation_scope:requestSnapshot.conversation_scope});
  } catch (e) {
    result = typeof agentFailureResponse==='function' ? agentFailureResponse(e) : {bank:true,reply:'本次处理未完成，原因暂无法确定，请查看相关记录后再继续。',isFallback:true};
  }

  // 最终答复与确认组件真正插入后，再由 finishRuntimeProgress 移除本轮进度。
  if(requestVersion===chatRequestVersion)chatSending=false;
  syncChatReference();
  if(!turnCurrent()){loadingMsg.remove();taskRequest?.component.destroy();return;}
  // Keep an obsolete scope visible but unsendable until the user opens a new
  // conversation. Never clear or silently fall back to workspace history.
  if(result?.isFallback===true&&['CONVERSATION_SCOPE_REQUIRED','CONVERSATION_SCOPE_UPGRADE_REQUIRED',
      'CONVERSATION_SCOPE_STALE','CONVERSATION_SCOPE_INVALID','CONVERSATION_SCOPE_CURRENT_TURN_REQUIRED',
      'CONVERSATION_SCOPE_MEMBERSHIP_INVALID','CONVERSATION_SCOPE_CONFLICT'].includes(result.error?.code)&&
      requestSnapshot.conversation_scope===currentConversationScope()){
    conversationScopeInvalid=true;updateChatSendState();
  }
  try {
  result.reply=NavrenPresentation.text(result.reply);
  const attachmentReceived=result.attachment_receipt?.status==='archived'&&result.attachment_receipt.request_id===result.bank_response_id&&
    result.workspace_id===binding.workspace_id&&result.privacy_revision===binding.privacy_revision;
  const reviewedVoiceReply=!!voiceReview&&result.isFallback!==true&&typeof result.speech_receipt==='string'&&!!result.speech_receipt.trim();
  if(attachmentReceived&&!reviewedVoiceReply)voiceConversation=false;
  const replyProjection=verifiedReplyProjection(result,binding);
  if(replyProjection?.text)result.reply=replyProjection.text;
  let replyHtml = renderReplyText(result.reply);

  // 金融产品名蓝色加粗高亮
  const highlightNames = result.highlight_names && result.highlight_names.length > 0
    ? result.highlight_names
    : (result.products || []).map(p => typeof p === 'string' ? p : p.name).filter(Boolean);
  replyHtml = highlightProductNames(replyHtml, highlightNames);

  const msgEl = addMessage('ai', replyHtml, result.isFallback);
  msgEl.dataset.clientMessageId=newChatMessageId();
  msgEl.dataset.bankResponseId=result.bank_response_id||'';
  msgEl.dataset.speechReceipt=result.speech_receipt||'';
  if(attachmentReceived&&!reviewedVoiceReply){msgEl.querySelector('.message-audio-control')?.remove();msgEl.querySelector('.bubble')?.classList.remove('has-audio-control');}
  const unifiedTask=window.NavrenTaskChat?.render(msgEl,result,taskRequest)||false;
  if(!unifiedTask&&replyProjection)renderReplyProjection(msgEl,replyProjection,binding);
  if(unifiedTask)attachMessageSpeech(msgEl,result.reply);
  if(result.isFallback&&!attachmentReceived){
    if(['AGENT_TIMEOUT','AGENT_CONNECTION_INTERRUPTED','AGENT_STREAM_INVALID','AGENT_HTTP_401','AGENT_HTTP_403','AGENT_HTTP_413','AGENT_HTTP_429','AGENT_HTTP_ERROR','AGENT_UNKNOWN'].includes(result.error?.code)){
      const note=document.createElement('p');note.className='task-history-note';note.textContent='本次消息或答复是否已保存尚未确认，可重新读取聊天记录核对。';msgEl.querySelector('.bubble')?.append(note);
    }
    if(internalChoice){
      const note=document.createElement('p');note.className='task-history-note';note.textContent=window.NavrenI18n?.language==='en'?'This choice could not be processed. Please ask again.':'此选择未继续处理，请重新查询。';
      msgEl.querySelector('.bubble')?.append(note);
    }else{
      // 用户消息已进入历史；答复失败只提供冻结请求的显式重试，不回填旧草稿。
      const retry=document.createElement('button');retry.type='button';retry.className='chat-retry';retry.textContent='重试这个问题';
      retry.onclick=()=>retryChatRequest(requestSnapshot,binding,msgEl);appendMessageRetry(msgEl, retry);
    }
  }
  if (!unifiedTask&&typeof BankUI !== 'undefined') BankUI.renderResponse(msgEl, result);
  renderReplyInteraction(msgEl,result,binding,requestVersion);
  if(window.CalendarUI)CalendarUI.response(msgEl,result.widgets);
  if(window.MarketUI)MarketUI.sources(msgEl,result.market_sources);
  renderMemoryContextCard(msgEl, result.user_memory_context);
  renderMemoryProposals(msgEl, result.memory_proposals);
  renderServiceNegotiationPrompt(msgEl, result.service_negotiation_request);

  // 推荐历史记录（无 UI 卡片，仅 audit trail）

  // 收藏按钮（小星标）：仅产品推荐场景显示，资产配置方案另有专用收藏按钮
  const isWorthBookmarking = !result.isFallback && result.intent !== 'asset_allocation';
  if (isWorthBookmarking) {
    addBookmarkButton(msgEl, result.reply, result);
  }

  // 语音播报与每条回复的朗读按钮共用控制器。
  const voiceEnabled = Storage.get('qingzhou_voiceEnabled');
  if ((!attachmentReceived||reviewedVoiceReply)&&!result.task_result_rejected&&(voiceEnabled || voiceConversation) && voiceTurn === voiceRequest) {
    speakMessage(msgEl, result.reply, scheduleVoiceResume);
  }

  if (result.toast && !result.isFallback) {
    showToast(result.toast);
  }

  // 多模态图片分析结果卡片
  if (result.image_analysis && result.image_analysis.status === 'success') {
    const analysisDiv = document.createElement('div');
    analysisDiv.style.cssText = 'margin-top:8px;padding:10px 14px;background:var(--surface);border-radius:var(--r-sm);font-size:var(--font-size-xs);color:var(--ink-70);line-height:1.8;border-left:3px solid var(--gold)';
    const result_text = result.image_analysis.result || '';
    const has_warning = /误导|保本|夸大|不适合/.test(result_text);
    const warning_style = has_warning ? 'color:var(--state-danger);font-weight:700' : 'color:var(--ink)';
    analysisDiv.innerHTML = '<div style="font-weight:700;color:var(--ink);margin-bottom:4px">🖼️ 图片分析</div>' +
      '<div style="' + warning_style + '">' + escapeHtml(result_text || '分析完成').replace(/\n/g, '<br>').slice(0, 500) + '</div>';
    msgEl.querySelector('.bubble').appendChild(analysisDiv);
  }

  // 智能体链路可视化面板：只在推荐/配置/对比等需要解释依据的场景展示。
  // 后端仍返回 agent_chain 用于日志与评分，普通概念解释不打扰用户。
  if (!result.bank && shouldShowAgentChain(result)) {
    renderAgentChain(msgEl, result.agent_chain);
  }

  // 保存 follow-up 会话记忆（阶段机驱动下次追问选择）
  if (result.followup_memory) {
    Storage.set('qingzhou_followupMemory', result.followup_memory);
  }

  // 保存资产配置/推荐方案到推荐中心，recommend.html 会读取完整上下文
  if (!result.bank) saveRecommendationSnapshot(result, text);

  // 资产配置方案专用"收藏方案"按钮（居中，替代小星标）
  if (result.intent === 'asset_allocation' && result.allocation?.allocation?.length > 0) {
    addSaveAllocationButton(msgEl, result);
  }

  // UI 动作按钮：后端返回结构化 action（重评测评/软拦截/转人工）
  if (result.ui_actions && result.ui_actions.length > 0) {
    renderUIActions(msgEl, result.ui_actions);
  }

  // 智能追问：优先使用后端生成的问题，前端兜底
  const followUpDescriptors=result.isFallback||result.reply_interaction?[]:normalizeFollowUpQuestions(result.follow_up_questions);
  renderFollowUpQuestions(msgEl,followUpDescriptors);

  // 风险重评提醒：超6个月或人生阶段变化
  checkRiskReassessmentNudge(msgEl);

  // 重新读取 history（并发调用时避免覆盖其他 sendMessage 写入的数据）
  finishRuntimeProgress(loadingMsg, msgEl, result, attachmentReceived && result.attachment_only_receipt === true);
  const latestHistory = currentChatHistory();
  const originalQuestion=latestHistory.findLast(row=>row.role==='user'&&(row.client_message_id===userMessageId||voiceEntry?.recording_id&&row.recording_id===voiceEntry.recording_id));
  if(originalQuestion&&result.bank_response_id){originalQuestion.bank_response_id=result.bank_response_id;if(userMessage)userMessage.dataset.bankResponseId=result.bank_response_id;}
  if(originalQuestion&&Array.isArray(result.user_attachments)&&result.user_attachments.length===media.length&&
    result.user_attachments.every((item,i)=>item.media_id===media[i].media_id&&item.sha256===media[i].sha256))originalQuestion.attachments=result.user_attachments;
  // 保存完整渲染后的 HTML，刷新恢复时直接使用，避免排版变化
  const renderedBubble = msgEl.querySelector('.bubble');
  const savedBubble = renderedBubble?.cloneNode(true);
  savedBubble?.querySelectorAll('.runtime-panel,.message-audio-control').forEach(button => button.remove());
  savedBubble?.querySelectorAll('.calendar-widget,.message-suggestions').forEach(widget => widget.remove());
  savedBubble?.querySelectorAll('.reply-components,.reply-interaction').forEach(widget=>widget.remove());
  if(unifiedTask&&savedBubble){savedBubble.replaceChildren();const body=document.createElement('div');body.className='message-body message-prose';body.innerHTML=renderReplyText(result.reply);savedBubble.append(body);const p=document.createElement('p');p.className='task-history-note';p.textContent='结构化任务依据与动作需重新查询核验。';savedBubble.append(p);}
  const localSocialReply = result.answer_contract?.kind === 'social_reply' &&
    result.answer_contract?.status === 'not_required' && result.agent_review?.status === 'not_required';
  latestHistory.push({
    role: 'ai',
    client_origin:'live',
    client_message_id:msgEl.dataset.clientMessageId,
    consumed_reply_controls:msgEl.__navrenConsumedReplyControls||{},
    request_snapshot: {text:requestSnapshot.text,conversation_scope:requestSnapshot.conversation_scope,reference:requestSnapshot.reference,return_view:requestSnapshot.return_view,internal_choice:requestSnapshot.internal_choice,had_image:requestSnapshot.had_image,had_audio:requestSnapshot.had_audio},
    presentation_ref:localSocialReply ? null : (window.NavrenPresentationBridge?.descriptor?.(result)||null),
    ...binding,
    content: result.reply,
    highlight_names: highlightNames.filter(name=>typeof name==='string'&&name.trim()),
    htmlContent: savedBubble ? savedBubble.innerHTML : null,
    showAgentChain: shouldShowAgentChain(result),
    intent: result.intent || '',
    bank: result.bank===true,
    environment: result.environment || null,
    read_only: result.bank===true&&!result.isFallback&&!(result.bank_actions||[]).length,
    answer_contract: result.answer_contract?.kind==='verified_facts'
      ? {kind:'verified_facts',fact_ids:[...(result.answer_contract.fact_ids||[])]}:result.answer_contract?.kind==='reviewed_text'?{kind:'reviewed_text'}:null,
    agent_review:result.agent_review?{status:result.agent_review.status}:null,
    reply_interaction:window.NavrenReplyInteractions?.snapshot(msgEl)||null,
    reply_presentation: !unifiedTask&&msgEl.__navrenReplyProjection
      ? window.NavrenReplyComponents?.snapshot(msgEl.__navrenReplyProjection,{workspace_id:binding.workspace_id,response_id:result.bank_response_id,answer_kind:'verified_facts'}):null,
    follow_up_questions: followUpDescriptors,
    bank_response_id: result.bank_response_id || '',
    speech_receipt: result.speech_receipt || '',
    attachments:msgEl.__navrenSavedSpeech?[msgEl.__navrenSavedSpeech]:[],
    widgets:result.widgets||[],
    timestamp: new Date().toISOString(),
    isFallback: result.isFallback
  });
  Storage.set('qingzhou_chatHistory', latestHistory);

  scrollToBottom();
  } finally { loadingMsg.remove(); }
}

// 仅渲染本轮实际收到的活动；不读取工具参数、原始结果或模型推理。
const runtimeActivityStates = new WeakMap();
const RUNTIME_ACTIVITY_FIELDS = {
  tool: ['source', 'period', 'record_count'], data: ['source', 'period', 'record_count'],
  skill: ['skill'], model: ['provider', 'model', 'round', 'failure_type'],
  review: ['provider', 'model', 'round', 'failure_type', 'verdict'], reply: ['source']
};

function runtimeSafeText(value, limit = 96) {
  if (typeof value !== 'string') return '';
  const text = value.trim();
  if (!text || text.length > limit || /[<>\r\n\u0000-\u001f]/.test(text) ||
      /https?:|file:|\/Users\/|[\w.+-]+@[\w.-]+|\d{7,}|(?:^|\s)(?:Bearer|sk-)|api[_-]?key|secret|token|[¥￥]|\b(?:CNY|USD|EUR|RMB)\b|\d[\d,.]*\s*(?:元|万元|亿元)/i.test(text)) return '';
  return text;
}

function runtimeSafeName(value) {
  const name = runtimeSafeText(value);
  return /^[A-Za-z][A-Za-z0-9_.:/-]{0,95}$/.test(name) ? name : '';
}

// 名称来自实际能力目录；未知能力保留真实标识，不猜测用途或补造调用。
const RUNTIME_TOOL_LABELS = Object.freeze({
  "load_skill": "加载专业技能",
  "knowledge_status": "检查知识资料接入",
  "knowledge_search": "检索公开知识与条款",
  "market_hotspots": "查询市场热点",
  "market_status": "核对市场来源与更新",
  "market_event": "读取市场事件与分析",
  "market_article": "核对报道原文",
  "market_weekly": "读取市场周报",
  "calendar_query": "查询财务日历",
  "calendar_day": "读取当日事项",
  "calendar_item": "核对日历事项",
  "calendar_budget_scenario": "计算日历预算情景",
  "calendar_prepare_event": "准备日历事项草案",
  "wealth_product": "核对基金与组合数据",
  "wealth_versions": "核对组合版本与来源",
  "personal_graph": "读取财务关系资料",
  "graph_scenario": "计算资金规划情景",
  "remember_profile": "整理待确认资料",
  "public_products": "查询公开产品资料",
  "research_plans": "读取已保存研究方案",
  "customer_records": "读取相关业务档案",
  "account_summary": "查询账户概览",
  "budget": "核对可安排资金",
  "statement_source": "核对账单来源",
  "monthly_statement": "查询月度账单",
  "annual_statement": "查询年度账单",
  "statement_details": "核对账单明细",
  "anomalies": "核对异常收支",
  "periodic_charges": "查询周期扣费",
  "cashflow": "分析现金流",
  "money_calculation": "进行金额计算",
  "products": "核对模拟产品与适配条件",
  "service_offer_rules": "查询服务条件",
  "service_offer_preview": "核对服务条件预览",
  "service_application_status": "查询服务申请回执",
  "business_records": "读取相关业务记录",
  "operation_status": "核对操作回执",
  "user_memory": "读取已确认资料",
  "prepare_operation": "准备待确认操作草案",
  "withdraw_draft": "撤回待确认草案",
  "answer_facts": "组织已核验事实",
  "answer_reviewed": "提交答复审核",
  "presentation_query": "查询图表依据",
  "presentation_detail": "核对图表明细",
  "run_generated_code": "执行受限计算"
});
const RUNTIME_SKILL_LABELS = Object.freeze({
  "goal-planning": "目标规划",
  "financial-diagnosis": "财务诊断",
  "reviewer": "独立审核",
  "payments": "支付与转账",
  "wealth": "理财与投资",
  "calendar": "财务日历",
  "asset-allocation": "资产配置",
  "product-research": "产品研究",
  "cards": "卡片与银行业务",
  "portfolio-review": "持仓复核",
  "code-sandbox": "受限代码计算",
  "cashflow": "收支与现金流",
  "subscriptions": "订阅与代扣",
  "banking": "银行业务",
  "profile": "财务资料与偏好",
  "knowledge-retrieval": "知识检索",
  "conversation-components": "对话内容编排",
  "market": "市场研究",
  "bank-service": "银行服务条件"
});
const RUNTIME_SOURCE_LABELS = Object.freeze({
  "public_knowledge": "公开知识资料",
  "local_knowledge_status": "知识资料接入状态",
  "verified_public_market": "已核验市场资料",
  "webank_public_disclosure": "银行公开资料",
  "profile_sources": "已登记资料",
  "maintainer_skill": "专业技能资料",
  "conversation_candidate": "待核实资料",
  "simulation": "模拟账户数据",
  "imported": "导入资料",
  "local_rule": "本地规则"
});

function runtimeActivityPresentation(activity) {
  if (activity.kind === 'skill' && activity.name === 'intent_route')
    return {category: '任务识别', label: '识别任务范围', code: ''};
  if (activity.kind === 'skill' || activity.name === 'load_skill') {
    const skill = runtimeSafeName(activity.summary?.skill);
    return {category: '技能调用', label: Object.hasOwn(RUNTIME_SKILL_LABELS, skill)
      ? RUNTIME_SKILL_LABELS[skill] : '加载专业技能', code: skill || 'load_skill'};
  }
  if (activity.kind === 'review') {
    const labels = {independent_review: '独立审核答复', answer_boundary: '核对回答范围',
      amount_contract: '核对金额依据', evidence_reference: '核对引用来源'};
    return {category: '结果核验', label: Object.hasOwn(labels, activity.name)
      ? labels[activity.name] : '核验答复', code: ''};
  }
  if (activity.kind === 'model') return {category: '分析处理', label: '分析当前问题与已有资料', code: ''};
  return {category: '工具调用', label: Object.hasOwn(RUNTIME_TOOL_LABELS, activity.name)
    ? RUNTIME_TOOL_LABELS[activity.name] : '调用工具', code: activity.name};
}

function runtimeActivitySummary(kind, summary) {
  if (!summary || typeof summary !== 'object' || Array.isArray(summary)) return [];
  const parts = [];
  for (const key of RUNTIME_ACTIVITY_FIELDS[kind] || []) {
    const value = summary[key];
    // 调用轮次、供应商、失败代码属于审计信息，保留原事件，不在用户进度中混排。
    if (['provider', 'model', 'round', 'failure_type', 'skill'].includes(key)) continue;
    if (key === 'record_count') {
      if (Number.isSafeInteger(value) && value >= 0 && value <= 10000000) parts.push(value + ' 条记录');
    } else if (key === 'period') {
      if (typeof value === 'string' && value.length <= 64 && /^\d{4}-\d{2}(?:-\d{2})?(?:\s*(?:至|—|~|\/|\.\.)\s*\d{4}-\d{2}(?:-\d{2})?)?$/.test(value)) parts.push(value);
    } else if (key === 'verdict') {
      const verdicts = {approve: '审核通过', revise: '需要修订', reject: '审核未通过', insufficient_evidence: '审核证据不足'};
      if (Object.hasOwn(verdicts, value)) parts.push(verdicts[value]);
    } else if (key === 'source' && Object.hasOwn(RUNTIME_SOURCE_LABELS, value)) {
      parts.push(RUNTIME_SOURCE_LABELS[value]);
    }
  }
  return parts;
}

function runtimeActivityPanel(message) {
  let state = runtimeActivityStates.get(message);
  if (state) return state;
  const bubble = message.querySelector('.bubble') || message;
  bubble.replaceChildren();
  const panel = document.createElement('section'); panel.className = 'runtime-panel runtime-activity-panel';
  panel.setAttribute('aria-label', '本次处理进度');
  const heading = document.createElement('div'); heading.className = 'runtime-activity-current';
  const latest = document.createElement('span'); latest.className = 'runtime-activity-latest';
  latest.setAttribute('role', 'status'); latest.setAttribute('aria-live', 'polite'); latest.setAttribute('aria-atomic', 'true');
  heading.append(latest);
  const list = document.createElement('ol'); list.className = 'runtime-steps runtime-activity-list';
  list.setAttribute('aria-label', '本次实际调用');
  list.tabIndex = 0;
  panel.append(heading, list); bubble.append(panel);
  state = {panel, latest, list, records: new Map(), steps: new Map(), sequence: 0,
    requestId: '', typedSeen: false, finished: false, transportFinished: false, runError: false};
  runtimeActivityStates.set(message, state);
  return state;
}

function updateRuntimeActivity(state, activity) {
  const followLatest = state.list.scrollHeight - state.list.scrollTop - state.list.clientHeight <= 24;
  let record = state.records.get(activity.callId);
  if (record?.typed && !activity.typed) return;
  if (!record) {
    const row = document.createElement('li'); row.className = 'runtime-step';
    const category = document.createElement('span'); category.className = 'runtime-activity-kind';
    const title = document.createElement('span'); title.className = 'runtime-activity-label';
    const name = document.createElement('code'); name.className = 'runtime-activity-name';
    const status = document.createElement('span'); status.className = 'runtime-activity-state';
    const details = document.createElement('span'); details.className = 'runtime-activity-data';
    row.append(category, title, status, name, details); state.list.append(row);
    record = {row, category, title, name, status, details}; state.records.set(activity.callId, record);
  }
  // 同一调用只更新这一行；重复或兼容事件不能重新打开已结束的调用。
  if (record.activity?.status !== 'started' && record.activity && activity.status === 'started') return;
  record.typed = record.typed || activity.typed;
  record.activity = activity;
  state.latestRecord = record;
  const view = runtimeActivityPresentation(activity);
  const statuses = {started: '进行中', completed: '已完成', failed: '未完成'};
  record.row.className = 'runtime-step ' + ({started: 'running', completed: 'done', failed: 'rejected'}[activity.status]);
  record.category.textContent = view.category;
  record.title.textContent = view.label;
  record.name.textContent = view.code; record.name.hidden = !view.code;
  record.status.textContent = statuses[activity.status];
  const details = runtimeActivitySummary(activity.kind, activity.summary);
  record.details.textContent = details.join(' · '); record.details.hidden = !details.length;
  state.latest.textContent = activity.status === 'started' ? '正在' + view.label : '正在处理';
  state.panel.dataset.status = 'running';
  if (followLatest) state.list.scrollTop = state.list.scrollHeight;
}

function renderRuntimeEvent(message, event) {
  if (!message?.isConnected || !event || typeof event !== 'object') return;
  let activity;
  if (event.type === 'CUSTOM' && event.name === 'navren.activity') {
    const value = event.value;
    if (!value || value.schema_version !== 1 || !Object.hasOwn(RUNTIME_ACTIVITY_FIELDS, value.kind) ||
        !['started', 'completed', 'failed'].includes(value.status) || !runtimeSafeName(value.name) ||
        typeof value.request_id !== 'string' || !value.request_id || value.request_id.length > 160 ||
        typeof value.call_id !== 'string' || !value.call_id || value.call_id.length > 160) return;
    if (value.kind === 'reply') return; // 本地问候无需模型、技能或工具进度。
    const state = runtimeActivityStates.get(message);
    if (state?.requestId && state.requestId !== value.request_id) return;
    activity = {callId: value.call_id, kind: value.kind, name: value.name,
      label: runtimeSafeText(value.label) || ({tool: '调用工具 ', skill: '加载技能 ', data: '读取数据 ', model: '调用模型 ', review: '审核接口 '}[value.kind] + value.name),
      status: value.status, attempt: value.attempt, summary: value.summary, typed: true, requestId: value.request_id};
  } else if (event.type === 'TOOL_CALL_START') {
    const name = runtimeSafeName(event.toolCallName);
    if (!name || typeof event.toolCallId !== 'string' || !event.toolCallId || event.toolCallId.length > 160) return;
    activity = {callId: event.toolCallId, kind: 'tool', name, label: '调用工具 ' + name, status: 'started', typed: false};
  } else if (event.type === 'TOOL_CALL_END') {
    const record = runtimeActivityStates.get(message)?.records.get(event.toolCallId);
    if (!record || record.typed) return;
    activity = {...record.activity, status: ['rejected', 'failed'].includes(event.metadata?.status) ? 'failed' : 'completed'};
  } else if (event.type === 'STEP_STARTED') {
    // 旧接口只确认模型被调用；不把旧“理解任务”等阶段模板当作实际活动。
    const step = String(event.stepName || '');
    const name = /^(DeepSeek|GLM)(?:\s|·|$)/i.exec(step)?.[1];
    if (!name || runtimeActivityStates.get(message)?.typedSeen) return;
    const state = runtimeActivityPanel(message), callId = 'legacy-model-' + (++state.sequence);
    state.steps.set(step, callId);
    activity = {callId, kind: /^GLM$/i.test(name)?'review':'model', name, label: /^GLM$/i.test(name)?'审核':'主模型', status: 'started', typed: false};
  } else if (event.type === 'STEP_FINISHED') {
    const state = runtimeActivityStates.get(message), record = state?.records.get(state.steps.get(String(event.stepName || '')));
    if (!record || record.typed) return;
    activity = {...record.activity, status: 'completed'};
  } else if (event.type === 'RUN_FINISHED' || event.type === 'RUN_ERROR') {
    const state = runtimeActivityStates.get(message);
    if (state) {
      state.transportFinished = true;
      state.runError = event.type === 'RUN_ERROR';
      if (state.runError) {
        state.latest.textContent = '本次处理未完成';
        state.panel.dataset.status = 'failed';
      }
    }
    return;
  } else return;
  const state = runtimeActivityPanel(message);
  if (state.finished || state.transportFinished) return;
  if (activity.typed && !state.typedSeen) {
    state.typedSeen = true;
    // typed活动有实际调用ID；移除旧模型阶段兼容项，避免同次模型调用显示两遍。
    for (const [id, record] of state.records) {
      if (id.startsWith('legacy-model-')) { record.row.remove(); state.records.delete(id); }
    }
    state.steps.clear();
  }
  if (activity.requestId) state.requestId = activity.requestId;
  updateRuntimeActivity(state, activity);
  scrollToBottom();
}

function finishRuntimeProgress(loadingMessage, replyMessage, result = {}, attachmentOnlyReceiptVerified = false) {
  const state = runtimeActivityStates.get(loadingMessage);
  if (state) {
    state.finished = true; state.panel.remove();
    runtimeActivityStates.delete(loadingMessage);
    // 失败只保留简短状态；具体原因和重试沿用真实答复，不从参数或异常文本拼接。
    if (state.runError || (result.isFallback === true && attachmentOnlyReceiptVerified !== true) || result.task_result_rejected === true) {
      const notice = document.createElement('p'); notice.className = 'runtime-panel runtime-activity-notice';
      notice.textContent = '本次处理未完成';
      (replyMessage.querySelector('.bubble') || replyMessage).prepend(notice);
    }
  }
  loadingMessage.remove();
  ensureMessageTools(replyMessage);
}

// ── Add Messages ──
function addMessage(role, content, isFallback = false, options = {}) {
  if(!options.historical)clearFollowUpQuestions();
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = 'message ' + role;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  if (role === 'ai' || role === 'assistant') {
    avatar.innerHTML = '<img class="brand-icon" src="assets/brand/navren-icon.png" width="1254" height="1254" alt="">';
  } else {
    if(window.NavrenPersonalProfile)NavrenPersonalProfile.decorateAvatar(avatar);
    else avatar.textContent = '👤';
  }

  const bubble = document.createElement('div');
  bubble.className = 'bubble';

  const contentDiv = document.createElement('div');
  contentDiv.className = role === 'ai' || role === 'assistant' ? 'message-body message-prose' : 'message-body';
  contentDiv.innerHTML = content;
  bubble.appendChild(contentDiv);

  div.appendChild(avatar);
  div.appendChild(bubble);
  container.appendChild(div);
  if (!options.historical&&(role === 'ai' || role === 'assistant')) attachMessageSpeech(div, contentDiv.innerText || contentDiv.textContent || '');
  return div;
}

function renderReplyText(text) {
  return window.NavrenMarkdown?.render(text) ?? escapeHtml(text).replace(/\n/g, '<br>');
}

function ensureMessageTools(message) {
  const bubble = message.querySelector('.bubble');
  if (!bubble) return null;
  let tools = bubble.querySelector(':scope > .message-tools');
  if (!tools) {
    tools = document.createElement('div'); tools.className = 'message-tools';
    bubble.append(tools);
  }
  // 只归组直接导航按钮；银行说明、草案和托管任务动作留在原容器。
  bubble.querySelectorAll(':scope > .bank-actions > button:not([data-bank-draft]):not([data-calendar-action])').forEach(button => {
    const original = button.parentElement;
    const group = document.createElement('div'); group.className = 'bank-actions message-action-group';
    if (original.dataset.bankResponse) group.dataset.bankResponse = original.dataset.bankResponse;
    button.classList.add('message-action'); group.append(button);
    messageToolActions(message).append(group);
    if (!original.childElementCount) original.remove();
  });
  bubble.querySelectorAll(':scope > .message-bookmark-control, :scope > .message-audio-control').forEach(button => tools.append(button));
  bubble.querySelectorAll(':scope > .chat-retry, :scope > .message-tools > .chat-retry').forEach(button => {
    button.classList.add('message-action'); messageToolActions(message).append(button);
  });
  bubble.append(tools);
  return tools;
}

function messageToolActions(message) {
  const bubble = message.querySelector('.bubble');
  if (!bubble) return null;
  let tools = bubble.querySelector(':scope > .message-tools');
  if (!tools) {
    tools = document.createElement('div'); tools.className = 'message-tools'; bubble.append(tools);
  }
  let actions = tools.querySelector(':scope > .message-tool-actions');
  if (!actions) {
    actions = document.createElement('div'); actions.className = 'message-tool-actions'; tools.prepend(actions);
  }
  return actions;
}

function appendMessageRetry(message, retry) {
  const tools = ensureMessageTools(message);
  if (!tools) return;
  tools.classList.add('has-retry'); retry.classList.add('message-action');
  messageToolActions(message).prepend(retry);
}

function clearFollowUpQuestions() {
  document.querySelectorAll('#chatMessages .message-suggestions').forEach(node => node.remove());
}

function createFollowUpRegion(msgEl) {
  const messages = [...document.querySelectorAll('#chatMessages .message')];
  if (!msgEl?.isConnected || messages.at(-1) !== msgEl) return null;
  clearFollowUpQuestions();
  const binding = chatBinding(), guard = BankClient.guard?.();
  const privacyTurn = globalThis.NavrenChatPrivacyVersion || 0;
  const div = document.createElement('div');
  div.className = 'message-suggestions';
  div.__navrenIsCurrent = () => div.isConnected && msgEl.isConnected &&
    [...document.querySelectorAll('#chatMessages .message')].at(-1) === msgEl &&
    binding.workspace_id===BankClient.identity()?.workspace_id&&binding.privacy_revision===BankClient.identity()?.privacy_revision&&
    privacyTurn === (globalThis.NavrenChatPrivacyVersion || 0) &&
    (!guard || guard.valid(binding)) && (!msgEl.__navrenTaskCurrent || msgEl.__navrenTaskCurrent())&&
    (!msgEl.__navrenReplyCurrent||msgEl.__navrenReplyCurrent());
  const label = document.createElement('span');
  label.className = 'follow-up-label'; label.textContent = getFollowUpLabel();
  div.append(label); msgEl.append(div);
  return div;
}
window.addEventListener('navren:account-change', clearFollowUpQuestions);
window.addEventListener('navren:privacy-reset', clearFollowUpQuestions);
window.addEventListener('navren:identity-revoked', clearFollowUpQuestions);
window.addEventListener('navren:account-switched', clearFollowUpQuestions);
window.addEventListener('pagehide', clearFollowUpQuestions);

let activeSpeechControl = null;
let speechControlRun = 0;

function attachMessageSpeech(message, initialText) {
  document.querySelectorAll('.message.ai .message-audio-control,.message.assistant .message-audio-control').forEach(button=>{
    if(button.closest('.message')===message)return;
    if(button.closest('.message')?.__navrenSavedSpeech)return;
    button.closest('.bubble')?.classList.remove('has-audio-control');button.remove();
    if(button===activeSpeechControl){if(NavrenSpeech.activeKind==='tts')NavrenSpeech.stop();activeSpeechControl=null;}
  });
  if (!message || !initialText.trim()) return;
  const bubble = message.querySelector('.bubble');
  const button = document.createElement('button');
  const staleButton = bubble.querySelector('.message-audio-control');
  if (staleButton) staleButton.replaceWith(button);
  button.type = 'button';
  button.className = 'message-audio-control';
  bubble.classList.add('has-audio-control');
  setSpeechControl(button, 'idle');
  button.addEventListener('click', () => {
    if(!button.isConnected||!message.isConnected||(message.__navrenTaskCurrent&&!message.__navrenTaskCurrent()))return;
    clearTimeout(voiceResumeTimer);
    voiceResumeTimer = null;
    if (activeSpeechControl === button && (NavrenSpeech.activeKind === 'tts'||message.__navrenSavedSpeech)) {
      button.dataset.muted = 'true';
      NavrenSpeech.stop();
      window.VoiceMessages?.stopSaved?.();
      speechControlRun++;
      activeSpeechControl=null;
      setSpeechControl(button, 'idle');
      scheduleVoiceResume();
      return;
    }
    if (NavrenSpeech.activeKind === 'asr') {
      showToast('请先结束语音输入，再朗读回复。');
      return;
    }
    if (activeSpeechControl && activeSpeechControl !== button) setSpeechControl(activeSpeechControl, 'idle');
    speakMessage(message, initialText, scheduleVoiceResume);
  });
  ensureMessageTools(message).append(button);
}

function renderMemoryContextCard(message, memories) {
  if (!Array.isArray(memories) || !memories.length) return;
  const labels = { goal: '财务目标', planned_commitment: '已确认的大额支出',
    liquidity_preference: '流动性偏好', product_interest: '关注的产品类型',
    knowledge_preference: '信息说明偏好' };
  const card = document.createElement('section');
  card.className = 'memory-context-card';
  const title = document.createElement('h3');
  title.textContent = '本次参考的财务记忆';
  const note = document.createElement('p');
  note.textContent = '以下内容由你主动保存，仅作背景参考，不改变账户余额或现金流计算。';
  const list = document.createElement('ul');
  memories.forEach(item => {
    if (!item || typeof item.value !== 'string') return;
    const row = document.createElement('li');
    const category = labels[item.category] || '财务记忆';
    row.textContent = category + '：' + item.value;
    list.append(row);
  });
  if (!list.children.length) return;
  card.append(title, note, list);
  message.querySelector('.bubble')?.append(card);
}

function renderMemoryProposals(message, proposals) {
  if (!Array.isArray(proposals) || !proposals.length) return;
  const labels = { goal: '财务目标', planned_commitment: '已确认的大额支出',
    liquidity_preference: '流动性偏好', product_interest: '关注的产品类型',
    knowledge_preference: '信息说明偏好' };
  const card = document.createElement('section');
  card.className = 'memory-proposal-card';
  const title = document.createElement('strong');
  title.textContent = '可保存为财务记忆';
  const notice = document.createElement('p');
  notice.textContent = '这是根据你明确表达的长期偏好整理出的候选，当前尚未保存。';
  card.append(title, notice);
  proposals.forEach(proposal => {
    const confirmation = {
      proposal_id: proposal.proposal_id, source_ref: proposal.source_ref,
      confirmation_token: proposal.confirmation_token, expected_revision: proposal.expected_revision
    };
    const row = document.createElement('div');
    row.className = 'memory-proposal-row';
    const details = document.createElement('div');
    const category = document.createElement('span');
    category.textContent = labels[proposal.category] || '财务记忆';
    const value = document.createElement('p');
    value.textContent = proposal.value;
    details.append(category, value);
    const actions = document.createElement('div');
    actions.className = 'memory-proposal-actions';
    const save = document.createElement('button');
    save.type = 'button'; save.className = 'p-btn'; save.textContent = '确认保存';
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        if (!confirmation.proposal_id || !confirmation.source_ref || !confirmation.confirmation_token
            || !Number.isSafeInteger(confirmation.expected_revision)) {
          throw new Error('候选确认凭据不完整，请重新核对本人原话。');
        }
        await BankClient.post('/memories', confirmation);
        row.replaceChildren(details);
        const saved = document.createElement('span');
        saved.className = 'memory-proposal-saved';
        saved.textContent = '已确认并保存到本机记忆';
        row.append(saved);
      } catch (error) {
        save.disabled = false;
        showToast('记忆未保存：' + error.message);
      }
    });
    const dismiss = document.createElement('button');
    dismiss.type = 'button'; dismiss.className = 'p-btn'; dismiss.textContent = '不保存';
    dismiss.addEventListener('click', () => row.remove());
    actions.append(save, dismiss);
    row.append(details, actions);
    card.append(row);
  });
  message.querySelector('.bubble')?.append(card);
}

function renderServiceNegotiationPrompt(message, request) {
  if (!request || typeof request.text !== 'string') return;
  const card = document.createElement('section');
  card.className = 'service-negotiation-card';
  const title = document.createElement('h3'); title.textContent = '模拟银行服务协商';
  const intro = document.createElement('p');
  intro.textContent = '先核对你的条件。Navren 只会从已批准的合成优惠规则中匹配方案，不会生成贷款、外部商家报价或真实合同。';
  const text = document.createElement('textarea');
  text.value = request.text; text.rows = 3; text.maxLength = 1200;
  text.setAttribute('aria-label', '本次协商条件');
  const shareLabel = document.createElement('label');
  shareLabel.className = 'service-memory-share';
  const share = document.createElement('input'); share.type = 'checkbox';
  const shareText = document.createElement('span');
  shareText.textContent = '本次向模拟银行代理分享已确认的财务目标、流动性偏好和产品兴趣（默认关闭；不分享消费明细、风险测评或资金承诺）。';
  shareLabel.append(share, shareText);
  const preview = document.createElement('button');
  preview.type = 'button'; preview.className = 'p-btn'; preview.textContent = '按当前条件匹配方案';
  const result = document.createElement('div'); result.className = 'service-negotiation-result';
  preview.addEventListener('click', async () => {
    preview.disabled = true; preview.textContent = '正在核验模拟规则…'; result.replaceChildren();
    try {
      const view = await BankClient.post('/service-negotiations/preview', {
        text: text.value.trim(), share_memory: share.checked
      });
      result.replaceChildren();
      const messageLine = document.createElement('p'); messageLine.textContent = view.message || view.notice;
      result.append(messageLine);
      if (view.shared_memory?.length) {
        const disclosure = document.createElement('p');
        disclosure.textContent = '本次实际分享给模拟银行代理：' + view.shared_memory.map(item => item.value).join('；');
        result.append(disclosure);
      } else if (share.checked) {
        const noMemory = document.createElement('p');
        noMemory.textContent = '当前没有可分享的已启用财务记忆；本次没有披露记忆内容。';
        result.append(noMemory);
      }
      if (view.offers?.length) {
        const participants = document.createElement('p');
        participants.textContent = '流程：用户利益代理 → 模拟银行服务代理 → 规则独立审核。';
        result.append(participants);
        view.offers.forEach(offer => result.append(renderServiceOffer(view.draft_id, offer)));
      }
      const notice = document.createElement('small'); notice.textContent = view.notice || '';
      result.append(notice);
    } catch (error) {
      const failed = document.createElement('p'); failed.textContent = '方案核验失败：' + error.message; result.append(failed);
    } finally { preview.disabled = false; preview.textContent = '重新匹配方案'; }
  });
  card.append(title, intro, text, shareLabel, preview, result);
  message.querySelector('.bubble')?.append(card);
}

function renderServiceOffer(draftId, offer) {
  const row = document.createElement('article'); row.className = 'service-offer';
  const title = document.createElement('h4'); title.textContent = offer.title;
  const amount = document.createElement('p');
  amount.textContent = '每月最多减免 ' + formatServiceMoney(offer.monthly_value_minor)
    + '，期限 ' + offer.duration_months + ' 个月，模拟成本上限 ' + formatServiceMoney(offer.max_cost_minor) + '。';
  const conditions = document.createElement('ul');
  (offer.conditions || []).forEach(item => { const li = document.createElement('li'); li.textContent = item; conditions.append(li); });
  const investment = document.createElement('p');
  investment.textContent = '是否要求购买理财：' + (offer.requires_investment_purchase ? '是' : '否');
  const status = document.createElement('p'); status.textContent = offer.status_label || '条件申请待核验';
  const accept = document.createElement('button'); accept.type = 'button'; accept.className = 'p-btn';
  accept.textContent = '我接受这些条件，提交模拟申请';
  accept.addEventListener('click', async () => {
    accept.disabled = true;
    try {
      const application = await BankClient.post('/service-negotiations/' + draftId + '/accept', {
        offer_id: offer.offer_id, confirmed: true
      });
      row.replaceChildren(title, amount, conditions, investment);
      const receipt = document.createElement('p');
      receipt.textContent = '模拟申请已提交，状态：' + application.status + '；编号 ' + application.application_id;
      const notice = document.createElement('small'); notice.textContent = application.notice;
      row.append(receipt, notice);
    } catch (error) {
      accept.disabled = false;
      status.textContent = '未提交：' + error.message;
    }
  });
  row.append(title, amount, conditions, investment, status, accept);
  return row;
}

function formatServiceMoney(minor) {
  return Number(minor / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' 元';
}

function setSpeechControl(button, state) {
  const muted = state === 'idle' && (button.dataset.muted === 'true' || !Storage.get('qingzhou_voiceEnabled'));
  const icons = {
    idle: '<path d="M11 5.5 6.8 9H4.5v6h2.3l4.2 3.5z"/><path d="M15 9a4.5 4.5 0 0 1 0 6m2.7-9a8.5 8.5 0 0 1 0 12"/>',
    muted: '<path d="M11 5.5 6.8 9H4.5v6h2.3l4.2 3.5z"/><path d="M15 9a4.5 4.5 0 0 1 0 6"/>',
    speaking: '<rect x="7" y="7" width="10" height="10" rx="2.5" fill="currentColor" stroke="none"/>',
    preparing: '<circle cx="6" cy="12" r="1.35" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.35" fill="currentColor" stroke="none"/><circle cx="18" cy="12" r="1.35" fill="currentColor" stroke="none"/>'
  };
  button.dataset.state = muted ? 'muted' : state;
  button.setAttribute('aria-pressed', state === 'idle' ? 'false' : 'true');
  button.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (muted ? icons.muted : icons[state] || icons.preparing) + '</svg>';
  if (state === 'idle') {
    button.setAttribute('aria-label', '朗读 Navren 的回复');
    button.title = muted ? '当前不朗读，点击朗读这条回复' : '朗读这条回复';
    button.removeAttribute('aria-busy');
    button.classList.remove('speaking');
  } else if (state === 'speaking') {
    button.setAttribute('aria-label', '停止朗读');
    button.title = '停止朗读';
    button.removeAttribute('aria-busy');
    button.classList.add('speaking');
  } else {
    button.setAttribute('aria-label', '正在准备语音，点击停止');
    button.title = '正在准备语音，点击停止';
    button.setAttribute('aria-busy', 'true');
    button.classList.add('speaking');
  }
}

function speakMessage(message, text, onEnd) {
  const button = message?.querySelector('.message-audio-control');
  if (!button || !text.trim()) return Promise.resolve();
  if(message.__navrenTaskCurrent&&!message.__navrenTaskCurrent()){
    delete message.dataset.speechReceipt;delete message.dataset.bankResponseId;
    button.remove();message.querySelector('.bubble')?.classList.remove('has-audio-control');return Promise.resolve();
  }
  const run = ++speechControlRun;
  button.dataset.muted = 'false';
  activeSpeechControl = button;
  setSpeechControl(button, 'preparing');
  const guard=BankClient.guard(),binding=chatBinding();
  const current=()=>run===speechControlRun&&message.isConnected&&guard.valid();
  const saved=message.__navrenSavedSpeech;
  const preset = getVoicePreset();
  const rate = currentMode === 'senior' ? 0.85 : preset.rate;
  const onState=state=>{if(current())setSpeechControl(button,state==='speaking'?'speaking':'preparing');};
  const playback=saved?VoiceMessages.playSaved(saved,{onState}):NavrenSpeech.speak(text, {
    voice:preset.id,rate,speechReceipt:message.dataset.speechReceipt||'',onState,
    onSavedMedia:raw=>{
      if(!current())return;
      const media=NavrenChatMedia.validate(raw,{boundOnly:true});
      if(media.kind!=='audio')return;
      message.__navrenSavedSpeech=media;
      const history=currentChatHistory(),entry=history.findLast(item=>['ai','assistant'].includes(item.role)&&item.bank_response_id===message.dataset.bankResponseId);
      if(entry){entry.attachments=[media];Storage.set('qingzhou_chatHistory',history);}
    }
  });
  return playback.then(() => { if (current()&&onEnd) onEnd(); }).catch(error => {
    if (current()&&error.name !== 'AbortError') {
      voiceConversation = false;
      showToast('语音播报未完成：' + error.message);
    }
  }).finally(() => {
    if (current()) {
      setSpeechControl(button, 'idle');
      if (activeSpeechControl === button) activeSpeechControl = null;
    }
  });
}

// “我的”页面关闭语音播报后，已打开的聊天页同步停止并显示划线图标。
let lastAccountVoicePreset = null;
window.addEventListener('navren:account-state', () => {
  if (document.body.dataset.page !== 'chat') return;
  currentMode = getInterfaceMode();
  if (Storage.accountReady()) renderPresets();
  checkPrivacyGuide();
  const preset = Storage.get('qingzhou_voicePreset');
  if (lastAccountVoicePreset !== null && lastAccountVoicePreset !== preset && NavrenSpeech.activeKind === 'tts') NavrenSpeech.stop();
  lastAccountVoicePreset = preset;
  const enabled = Boolean(Storage.get('qingzhou_voiceEnabled'));
  if (!enabled) {
    voiceConversation = false;
    clearTimeout(voiceResumeTimer);
    voiceResumeTimer = null;
    if (NavrenSpeech.activeKind === 'tts') NavrenSpeech.stop();
  }
  document.querySelectorAll('.message-audio-control').forEach(button => {
    button.dataset.muted = enabled ? 'false' : 'true';
    if (!enabled || button !== activeSpeechControl) setSpeechControl(button, 'idle');
  });
  document.querySelectorAll('.message-bookmark-control').forEach(button => button.__syncAccountBookmark?.());
});

function stripAgentChainPanels(html) {
  const box = document.createElement('div');
  box.innerHTML = html || '';

  // 移除新版 .agent-chain-panel 卡片
  box.querySelectorAll('.agent-chain-panel').forEach(panel => {
    const wrapper = panel.parentElement && panel.parentElement !== box
      && panel.parentElement.textContent.trim() === panel.textContent.trim()
      ? panel.parentElement : panel;
    wrapper.remove();
  });

  // 移除旧版固定五行链路面板（文本特征识别）
  const legacyPanels = Array.from(box.querySelectorAll('div'))
    .filter(el => {
      const text = (el.innerText || el.textContent || '').trim();
      return text.includes('智能体链路')
        && (text.includes('意图：') || text.includes('工具：') || text.includes('合规：'));
    })
    .sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);

  legacyPanels.forEach(el => {
    if (!box.contains(el)) return;
    const wrapper = el.parentElement && el.parentElement !== box
      && el.parentElement.textContent.trim() === el.textContent.trim()
      ? el.parentElement : el;
    wrapper.remove();
  });

  // 清理旧版"画像：R2""R3画像"等用户可见表达（不在链路面板内时也应避免残留）
  box.querySelectorAll('*').forEach(el => {
    if (el.children.length > 0) return; // 只处理叶子节点
    if (!el.textContent || !el.textContent.trim()) return;
    const txt = el.textContent;
    // 替换"画像：R2 · 10万" → "用户画像：稳健型 · 10万"
    if (/画像[：:]\s*R\d/.test(txt)) {
      const riskMap = { R1: '保守型', R2: '稳健型', R3: '平衡型', R4: '进取型', R5: '激进型' };
      let fixed = txt.replace(/画像[：:]\s*(R\d)/g, (_, r) => '用户画像：' + (riskMap[r] || r));
      fixed = fixed.replace(/\b(R\d)画像\b/g, (_, r) => (riskMap[r] || r) + '用户画像');
      if (fixed !== txt && el.childNodes.length === 1 && el.childNodes[0].nodeType === 3) {
        el.textContent = fixed;
      }
    }
  });

  // 清理空容器
  // 旧版把字符串追问渲染成了 undefined；只移除这类失效按钮，保留消息正文和有效操作卡。
  box.querySelectorAll('span').forEach(span => {
    if (span.textContent.trim() !== 'undefined' || span.style.cursor !== 'pointer') return;
    const parent = span.parentElement;
    span.remove();
    if (parent?.children.length === 1 && /^(下一步|继续看|您可以继续|您可能想问)$/.test(parent.textContent.trim())) parent.remove();
  });
  box.querySelectorAll('div').forEach(el => {
    if (!el.textContent.trim() && el.children.length === 0) el.remove();
  });

  return box.innerHTML;
}

function saveRecommendationSnapshot(result, userText) {
  const isRecommendationIntent = ['product_recommend', 'asset_allocation'].includes(result.intent);
  const hasAllocation = result.allocation?.allocation?.length > 0;
  const hasProducts = result.products?.length > 0 && isRecommendationIntent;
  if (!hasAllocation && !hasProducts) return;

  const riskProfile = Storage.get('qingzhou_riskProfile') || {};
  const userProfile = Storage.get('qingzhou_userProfile') || {};
  const previous = Storage.get('qingzhou_allocation');
  const previousHistory = previous?._history || [];
  const previousSnapshot = previous ? JSON.parse(JSON.stringify({ ...previous, _history: undefined })) : null;

  let allocationItems = [];
  let summary = '';
  let expectedReturn = '';
  let maxDrawdown = '';
  let riskLevel = riskProfile.level || result.agent_chain?.profile_snapshot?.risk_level || '';

  if (hasAllocation) {
    allocationItems = result.allocation.allocation.map(a => ({
      name: a.name,
      risk: a.risk || a.risk_level || '',
      ratio: Number(a.ratio) || 0,
      benchmark: a.benchmark || '',
      lock_period: a.lock_period || '',
      min_amount: a.min_amount || 0,
      underlying: a.underlying || '',
      reason: a.reason || '',
      caveat: a.caveat || ''
    }));
    expectedReturn = result.allocation.expected_return || '';
    maxDrawdown = result.allocation.max_drawdown || '';
    riskLevel = result.allocation.risk_level || riskLevel;
    summary = `${riskLevel || '专属'}资产配置${expectedReturn ? ' · 基准' + expectedReturn : ''}`;
  } else {
    const total = result.products.length;
    allocationItems = result.products.map((p, i) => {
      const ratio = i === total - 1
        ? 100 - Math.floor(100 / total) * (total - 1)
        : Math.floor(100 / total);
      return {
        name: p.name,
        risk: p.risk_level || p.risk || '',
        ratio,
        benchmark: p.benchmark || '',
        lock_period: p.lock_period || '',
        min_amount: p.min_amount || 0,
        product_id: p.product_id || ''
      };
    });
    summary = `来自对话的产品推荐 · ${total}只产品`;
  }

  const snapshot = {
    allocation: allocationItems,
    summary,
    generated_at: new Date().toISOString(),
    based_on: {
      risk_level: riskLevel || '',
      amount: userProfile.amount || result.agent_chain?.profile_snapshot?.amount || null,
      horizon: userProfile.horizon || result.agent_chain?.profile_snapshot?.horizon || '',
      goal: userProfile.goal || ''
    },
    expected_return: expectedReturn,
    max_drawdown: maxDrawdown,
    source: {
      type: 'chat',
      mode: currentMode,
      user_text: userText,
      assistant_reply: result.reply,
      intent: result.intent,
      follow_up_questions: result.follow_up_questions || [],
      agent_chain: result.agent_chain || null
    },
    _history: [
      ...(previousSnapshot ? [previousSnapshot] : []),
      ...previousHistory
    ].slice(0, 8)
  };

  Storage.set('qingzhou_allocation', snapshot);
}

function addLoadingDots() {
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = 'message ai';
  div.id = 'loadingMsg';
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.innerHTML = '<img class="brand-icon" src="assets/brand/navren-icon.png" width="1254" height="1254" alt="">';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  const dots = document.createElement('div');
  dots.className = 'loading-dots';
  dots.innerHTML = '<span></span><span></span><span></span>';
  bubble.appendChild(dots);
  div.appendChild(avatar);
  div.appendChild(bubble);
  container.appendChild(div);
  return div;
}

// ── Bookmark ──
function bookmarkIcon(button, saved) {
  button.classList.add('message-bookmark-control');
  button.classList.remove('tts-btn');
  button.style.color='';button.style.borderColor='';button.style.background='';
  button.innerHTML='<svg aria-hidden="true" viewBox="0 0 24 24" fill="'+(saved?'currentColor':'none')+'" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7.5 4.25h9a1.75 1.75 0 0 1 1.75 1.75v13.7L12 16.05 5.75 19.7V6A1.75 1.75 0 0 1 7.5 4.25Z"/></svg>';
  button.title=saved?'取消收藏':'收藏此回复';
  button.setAttribute('aria-label',button.title);button.setAttribute('aria-pressed',String(saved));
  button.closest('.bubble')?.classList.add('has-message-actions');
}
function configureAccountBookmark(button, message, text, entry = {}, result) {
  const origin=chatBinding(), snippet=text.slice(0,200);
  const messageId=entry.client_message_id||entry.bank_response_id||message.dataset.clientMessageId||message.dataset.bankResponseId||message.dataset.chatArchiveRow||newChatMessageId();
  const valid=()=>Storage.accountReady()&&origin.registered_user_id===BankClient.identity()?.registered_user_id&&origin.workspace_id===BankClient.identity()?.workspace_id&&origin.privacy_revision===BankClient.identity()?.privacy_revision;
  const matches=row=>(!row.category||row.category==='chat_message')&&row.workspace_id===origin.workspace_id&&row.privacy_revision===origin.privacy_revision&&(row.message_id?row.message_id===messageId:row.text===snippet);
  button.__syncAccountBookmark=()=>{bookmarkIcon(button,valid()&&(Storage.get('qingzhou_bookmarks')||[]).some(matches));button.disabled=!valid();};
  button.__syncAccountBookmark();
  button.onclick=async event=>{
    event.stopPropagation();if(button.disabled||!valid())return;
    const removing=(Storage.get('qingzhou_bookmarks')||[]).some(matches);
    button.disabled=true;
    try {
      await Storage.update('qingzhou_bookmarks', rows=>{
        const index=rows.findIndex(matches);
        if(removing){if(index<0)throw Error('收藏已变化，请重新读取。');rows.splice(index,1);}
        else if(index<0){if(rows.length>=500)throw Error('收藏数量已达上限，请先整理已有收藏。');rows.push({category:'chat_message',message_id:messageId,text:snippet,workspace_id:origin.workspace_id,privacy_revision:origin.privacy_revision,timestamp:new Date().toISOString()});}
        return rows;
      });
      if(!valid())return;
      button.__syncAccountBookmark();showToast(removing?'已取消收藏':'已收藏');
      if(!removing&&!result?.bank&&(result?.products?.length||result?.allocation?.allocation?.length))saveRecommendationSnapshot(result,text);
    } catch(error) { if(valid())showToast(error.message||'收藏未保存，请重试。'); }
    finally { if(button.isConnected)button.__syncAccountBookmark(); }
  };
}
function addBookmarkButton(msgEl, text, result) {
  const bubble=msgEl.querySelector('.bubble');
  const btn=document.createElement('button');btn.type='button';btn.className='message-bookmark-control';
  configureAccountBookmark(btn,msgEl,text,{},result);
  bubble.classList.add('has-message-actions');ensureMessageTools(msgEl).append(btn);
}

/** 资产配置方案专用收藏动作，与其他消息操作共用工具行。 */
function addSaveAllocationButton(msgEl, result) {
  const bubble = msgEl.querySelector('.bubble');
  const btn = document.createElement('button');
  btn.type = 'button'; btn.className = 'message-action message-save-control';
  btn.textContent = '收藏方案';

  btn.onclick = (e) => {
    e.stopPropagation();
    saveRecommendationSnapshot(result, '用户主动收藏资产配置方案');
    btn.textContent = '已收藏至推荐页';
    btn.dataset.saved = '1';
    btn.title = '方案已保存，可在「推荐」页查看';
    if (typeof showToast === 'function') showToast('已收藏，可在「推荐」页继续查看和追问');
  };

  messageToolActions(msgEl).append(btn);
}


function scheduleVoiceResume() {
  clearTimeout(voiceResumeTimer);
  voiceResumeTimer = setTimeout(() => {
    if (voiceConversation && !document.hidden && Storage.get('qingzhou_voiceEnabled') && !isListening) toggleVoice();
  }, 500);
}


// ── 回访变化摘要 ──
function showReengagementSummary() {
  // 返回只恢复已核验会话；旧浏览器人生标签和配置不作为当前私人事实。
}

// ── 智能欢迎语 ──
function personalizeWelcome() {
  const welcomeEl=document.getElementById('welcomeMsg');if(!welcomeEl)return;
  const hours=Number(new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'numeric',hourCycle:'h23'}).format(new Date()));
  const greeting=hours<12?'早上好':hours<18?'下午好':'晚上好';
  welcomeEl.textContent=greeting+'。我是 Navren，可以帮你查账户、了解收付款与卡片、研究产品、整理目标和财务日历。你想先了解哪件事？';
  Storage.set('qingzhou_lastVisit',new Date().toISOString().slice(0,10));
}

// ── 从用户消息中自动提取画像字段 ──
function extractProfileFromMessage(text) {
  const profile = Storage.get('qingzhou_userProfile') || {};
  let changed = false;

  // 提取金额：支持 "20万" "20w" "200000" "20万元"
  const amountMatch = text.match(/(\d+\.?\d*)\s*(?:万|w|万元|W)/);
  if (amountMatch && !profile.amount) {
    const amount = parseFloat(amountMatch[1]) * 10000;
    profile.amount = amount;
    changed = true;
    Storage.addKeyMoment(`聊天提取：可投金额 ${(amount/10000).toFixed(0)} 万`);
  }
  // 提取纯数字金额 >1000
  const numMatch = text.match(/(?:有|投|放|买)\s*(\d{4,6})\s*(?:元|块|钱|的)?/);
  if (numMatch && !profile.amount) {
    const amount = parseInt(numMatch[1]);
    if (amount >= 1000) {
      profile.amount = amount;
      changed = true;
      Storage.addKeyMoment(`聊天提取：可投金额 ${amount} 元`);
    }
  }

  // 提取期限：支持 "半年" "6个月" "3年" "1年" "30天"
  const horizonMatch = text.match(/(\d+\.?\d*)\s*(?:个?月|年|天)/);
  if (horizonMatch && !profile.horizon) {
    const num = horizonMatch[1];
    const unit = horizonMatch[0].includes('年') ? '年' : horizonMatch[0].includes('天') ? '天' : '个月';
    profile.horizon = num + unit;
    changed = true;
    Storage.addKeyMoment(`聊天提取：投资期限 ${profile.horizon}`);
  }
  // "半年" "一年"
  if (/半年/.test(text) && !profile.horizon) { profile.horizon = '6个月'; changed = true; }
  if (/一年/.test(text) && !profile.horizon) { profile.horizon = '1年'; changed = true; }

  // 提取投资目标
  if (/教育|孩子|上学|学费/.test(text) && !profile.goal) { profile.goal = '子女教育金'; changed = true; }
  if (/养老|退休/.test(text) && !profile.goal) { profile.goal = '退休养老'; changed = true; }
  if (/买房|购房|首付/.test(text) && !profile.goal) { profile.goal = '购房置业'; changed = true; }
  if (/增值|赚钱|收益|投资/.test(text) && !profile.goal) { profile.goal = '财富增值'; changed = true; }

  if (changed) {
    Storage.set('qingzhou_userProfile', profile);
    // 触发人生阶段检测
    if (typeof detectLifeEvent === 'function') detectLifeEvent();
  }
}

// ── Utils ──
function handleKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage(null,null,{manualVoiceReview:true});
  }
}

function scrollToBottom() {
  const container = document.getElementById('chatMessages');
  setTimeout(() => { container.scrollTop = container.scrollHeight; }, 50);
}

/** 获取当前模式下的追问区域标题 */
function getFollowUpLabel() {
  const labels = { classic: '下一步', senior: '您可以继续', youth: '继续看' };
  return labels[currentMode] || labels.classic;
}

/** 将回复中的金融产品名用蓝色加粗高亮 */
function highlightProductNames(html, names) {
  // names 优先来自后端 highlight_names（已扫描全库并按长度降序）
  if (!names || names.length === 0) return html;

  // 转义正则特殊字符
  const escapeRe = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const pattern = [...new Set(names.filter(name=>typeof name==='string'&&name.trim()))].sort((a,b)=>b.length-a.length).map(escapeRe).join('|');
  if (!pattern) return html;

  const re = new RegExp(`(${pattern})`, 'g');
  const template=document.createElement('template');template.innerHTML=html;
  const walker=document.createTreeWalker(template.content,NodeFilter.SHOW_TEXT),nodes=[];
  while(walker.nextNode())if(!walker.currentNode.parentElement?.closest('a,code,pre,.product-name'))nodes.push(walker.currentNode);
  for(const node of nodes){
    re.lastIndex=0;const text=node.nodeValue;let match,start=0,changed=false;const fragment=document.createDocumentFragment();
    while((match=re.exec(text))){
      changed=true;fragment.append(document.createTextNode(text.slice(start,match.index)));
      const span=document.createElement('span');span.className='product-name';span.textContent=match[0];fragment.append(span);start=re.lastIndex;
    }
    if(changed){fragment.append(document.createTextNode(text.slice(start)));node.replaceWith(fragment);}
  }
  return template.innerHTML;
}

/** 刷新页面恢复历史消息后，重新绑定气泡内的点击事件 */
function rebindBubbleEvents(msgEl, historyEntry, retryQuestion = '') {
  const bubble = msgEl.querySelector('.bubble');
  if (!bubble) return;
  bubble.querySelectorAll('.reply-components,.reply-interaction').forEach(node=>node.remove());
  if(historyEntry.request_snapshot?.internal_choice===true||historyEntry.consumed_reply_controls?.retry===true){
    bubble.querySelectorAll('.chat-retry').forEach(node=>node.remove());
    bubble.querySelector('.message-tools')?.classList.remove('has-retry');
  }
  const hadDraft=!!bubble.querySelector('[data-bank-draft]');
  if(historyEntry.read_only===true||(!historyEntry.isFallback&&historyEntry.bank_response_id&&!hadDraft))
    bubble.querySelectorAll('.bank-actions button').forEach(button=>{
      if(['查看账户与操作记录','View account & activity'].includes(button.textContent.trim()))button.remove();
    });
  if (typeof BankUI !== 'undefined') BankUI.rebind(msgEl,{historical:true});
  bubble.querySelectorAll('.runtime-panel').forEach(panel => panel.remove());
  // 历史中的旧追问及业务按钮不重新激活。
  bubble.querySelectorAll('.message-suggestions').forEach(node=>node.remove());
  bubble.querySelectorAll('div > span').forEach(label=>{
    if(!/^(下一步|继续看|您可以继续|您可能想问|Next steps|Keep exploring|You can continue)$/.test(label.textContent.trim()))return;
    const region=label.parentElement;
    if(region.firstElementChild===label&&region.querySelector('[style*="cursor"],.follow-up-chip'))region.remove();
  });
  let body=bubble.querySelector('.message-body') || [...bubble.children].find(node=>node.tagName==='DIV'&&!node.matches('.sender,.message-tools,.bank-actions,.message-ui-actions'));
  if(!body&&historyEntry?.content){
    [...bubble.childNodes].filter(node=>node.nodeType===Node.TEXT_NODE).forEach(node=>node.remove());
    body=document.createElement('div');bubble.prepend(body);
  }
  if(body&&historyEntry?.content){
    const names=historyEntry.highlight_names || [...body.querySelectorAll('.product-name')].map(node=>node.textContent);
    body.classList.add('message-body','message-prose');body.innerHTML=highlightProductNames(renderReplyText(historyEntry.content),names);
  }

  // 将历史收藏控件统一升级，去掉旧外圈和金色内联样式。
  bubble.querySelectorAll('.tts-btn,.message-bookmark-control').forEach(btn=>{
    configureAccountBookmark(btn,msgEl,historyEntry.content||'',historyEntry);
  });

  bubble.querySelectorAll('[data-chat-action]').forEach(button => {
    button.onclick = event => { event.stopPropagation(); runMessageAction(button.dataset.chatAction); };
  });
  bubble.querySelectorAll('.chat-retry').forEach(button => {
    const snapshot=historyEntry.request_snapshot||{text:retryQuestion,reference:null,return_view:{page:'chat',view:'home'}};
    button.disabled=typeof snapshot.text!=='string'||(!snapshot.text.trim()&&!cloneChatReference(snapshot.reference));
    button.onclick=()=>retryChatRequest(snapshot,{workspace_id:historyEntry.workspace_id,privacy_revision:historyEntry.privacy_revision},msgEl);
  });
  ensureMessageTools(msgEl);

  // 追问按钮 / UI 动作按钮 — 需通过文本内容判断行为
  bubble.querySelectorAll('span').forEach(span => {
    const txt = span.textContent.trim();
    if (!txt || /^(下一步|继续看|您可以继续|您可能想问)$/.test(txt)) return;

    // 判断是否为可点击的追问/动作按钮（有 cursor:pointer 或 border）
    const style = span.style || {};
    const isButton = style.cursor === 'pointer' || span.getAttribute('style')?.includes('cursor');
    // 或用 computed style
    const cs = getComputedStyle(span);
    const looksClickable = cs.cursor === 'pointer' && (cs.border.includes('solid') || cs.border.includes('1px'));

    if (looksClickable || (span.getAttribute('style') || '').includes('cursor:pointer')) {
      span.onclick = (e) => {
        e.stopPropagation();
        if (txt === '重新风险测评' || txt === '开始风险测评' || txt === '开始测评') {
          if (typeof startQuestionnaire === 'function') startQuestionnaire();
        } else if (txt.includes('转接人工')) {
          if (typeof triggerHandoff === 'function') triggerHandoff('用户点击历史追问');
        } else {
          document.getElementById('userInput').value = txt;
          if (typeof sendMessage === 'function') sendMessage();
        }
      };
    }
  });
}

/** 链路展示决策：由后端 agent_chain.display 决定，前端不自行推断 */
function shouldShowAgentChain(result) {
  if (!result || !result.agent_chain) return false;

  const params = new URLSearchParams(window.location.search);
  if (params.get('debug_chain') === '1') return true;

  // 后端明确告知是否展示
  if (result.agent_chain.display === true) {
    // 防御：display=true 但 steps 为空 → warning + 不渲染空卡片
    if (!result.agent_chain.steps || result.agent_chain.steps.length === 0) {
      console.warn('agent_chain.display=true 但 steps 为空，跳过渲染');
      return false;
    }
    return true;
  }

  // 开发期调试日志
  if (result.intent === 'asset_allocation' && result.allocation?.allocation?.length > 0
      && result.agent_chain.display !== true) {
    console.warn('资产配置已生成但 agent_chain.display 未开启', result);
  }

  return false;
}

/** 渲染智能体链路卡片 — 动态步骤 + 点击展开/收起 */
function renderAgentChain(msgEl, chain) {
  const bubble = msgEl.querySelector('.bubble');
  if (!chain || !chain.steps || chain.steps.length === 0) return;

  // 步骤 → 用户可见文案
  const stepHtml = chain.steps.map(s => {
    const icon = { intent: '→', slots: '↳', profile: '👤', search: '🔍',
      allocation: '📊', comparison: '↔', recommend: '✓', image_risk: '🖼️',
      image_analysis: '🖼️', compliance: '⚖', holdings: '📋' }[s.type] || '·';
    const statusColors = { pass: 'var(--gold-dark)', soft_block: 'var(--state-warning)',
      hard_block: 'var(--state-danger)', tx_block: 'var(--state-warning)', handoff: 'var(--state-warning)',
      info_insufficient: 'var(--ink-40)' };
    const color = s.type === 'compliance' ? (statusColors[s.status] || 'var(--ink-40)') : 'var(--ink-50)';
    return `<div style="display:flex;gap:6px;align-items:baseline;font-size:var(--font-size-xs);color:${color};line-height:1.7;word-break:break-all">
      <span style="flex-shrink:0;width:14px;text-align:center">${icon}</span>
      <span>${escapeHtml(s.detail)}</span>
    </div>`;
  }).join('');

  const totalProducts = chain.products_total || 1260;
  const summary = `${chain.intent_label || '分析'} · 合规${chain.compliance_status === 'pass' ? '通过' : '拦截'}`;

  const card = document.createElement('div');
  card.className = 'agent-chain-panel';
  card.style.cssText = 'margin-top:8px;padding:8px 12px;background:var(--surface);border-radius:6px;border-left:3px solid var(--gold);cursor:pointer;overflow:hidden;transition:max-height 0.3s ease;max-height:128px;user-select:none';
  card.title = '点击展开/收起智能体链路';

  card.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span style="font-weight:700;color:var(--ink-50);font-size:var(--font-size-xs)">智能体链路<span style="font-weight:400;color:var(--ink-30);margin-left:4px;font-size:var(--font-size-xs)">· 点击展开</span></span>
      <span style="font-size:var(--font-size-xs);color:var(--ink-30)">${escapeHtml(summary)}</span>
    </div>
    <div class="agent-chain-steps" style="display:flex;flex-direction:column;gap:2px;overflow-y:auto;max-height:320px">
      ${stepHtml}
    </div>
  `;

  // 点击展开/收起
  let expanded = false;
  card.onclick = (e) => {
    // 不阻止步骤区域内的文本选择，但优先处理点击
    if (e.target.tagName === 'A' || e.target.closest('button')) return;
    e.stopPropagation();
    expanded = !expanded;
    if (expanded) {
      card.style.maxHeight = '420px';
      card.querySelector('.agent-chain-steps').style.maxHeight = '380px';
      card.querySelector('span').innerHTML = '智能体链路<span style="font-weight:400;color:var(--ink-30);margin-left:4px;font-size:var(--font-size-xs)">· 点击收起</span>';
    } else {
      card.style.maxHeight = '128px';
      card.querySelector('.agent-chain-steps').style.maxHeight = '88px';
      card.querySelector('span').innerHTML = '智能体链路<span style="font-weight:400;color:var(--ink-30);margin-left:4px;font-size:var(--font-size-xs)">· 点击展开</span>';
    }
  };
  // 阻止步骤区域内点击冒泡到消息气泡
  card.querySelector('.agent-chain-steps').onclick = (e) => { e.stopPropagation(); };

  bubble.appendChild(card);
}

/** 渲染后端返回的 UI 动作按钮（风险测评/软拦截/转人工） */
function runMessageAction(type) {
  switch (type) {
    case 'start_risk_quiz': startQuestionnaire(); break;
    case 'show_safe_plan':
      document.getElementById('userInput').value = '查看当前风险等级内的稳健方案'; sendMessage(); break;
    case 'explain_risk_gap':
      document.getElementById('userInput').value = '了解达到该收益目标需要承担什么风险'; sendMessage(); break;
    case 'handoff': triggerHandoff('用户主动请求转人工（风险拦截触发）'); break;
  }
}

function renderUIActions(msgEl, actions) {
  const div = document.createElement('div');
  div.className = 'message-ui-actions message-action-group';

  actions.forEach(action => {
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'message-action'; btn.dataset.chatAction = action.type;
    btn.textContent = action.label;
    btn.onclick = (e) => {
      e.stopPropagation();
      runMessageAction(action.type);
    };
    div.appendChild(btn);
  });
  messageToolActions(msgEl).append(div);
}

/** 渲染后端返回的追问按钮（优先于前端兜底） */
function normalizeFollowUpQuestions(questions){
  if(!Array.isArray(questions))return [];
  const normalized=[], seen=new Set(), types=new Set(['question','send_message','start_risk_quiz','handoff']);
  for(const raw of questions){
    const q=typeof raw==='string'?{label:raw,text:raw,type:'question'}:raw;
    if(!q||typeof q!=='object'||Array.isArray(q))continue;
    const type=q.type||'question';if(!types.has(type))continue;
    const text=typeof q.text==='string'?q.text.trim():typeof q.question==='string'?q.question.trim():'';
    const label=typeof q.label==='string'?q.label.trim():text;
    if(!label||label.length>160||text.length>1200||(['question','send_message'].includes(type)&&!text))continue;
    const key=type+':'+(text||label);if(seen.has(key))continue;seen.add(key);
    normalized.push({type,label,text:text||label,
      ...(typeof q.description==='string'&&q.description.trim()?{description:q.description.trim().slice(0,400)}:{})});
    if(normalized.length===4)break;
  }
  return normalized;
}

function renderFollowUpQuestions(msgEl, questions) {
  if(msgEl.__navrenConsumedReplyControls?.followups===true)return;
  questions=normalizeFollowUpQuestions(questions);
  if(!questions.length)return;
  const div = createFollowUpRegion(msgEl);
  if (!div) return;

  questions.slice(0, 4).forEach(q => {
    // 动作型 follow-up 加图标
    const actionIcons = { 'start_risk_quiz': '🔄 ', 'handoff': '📞 ' };
    const icon = actionIcons[q.type] || '';
    const chip = document.createElement('button');
    chip.type='button';chip.className='follow-up-chip';
    chip.textContent = icon + q.label;
    if(q.description)chip.title=q.description;
    chip.onclick = (e) => {
      e.stopPropagation();
      if(!div.__navrenIsCurrent()||chatSending)return;
      consumeReplyControl(msgEl,'followups');
      // 记录点击到会话记忆，推进阶段
      const mem = Storage.get('qingzhou_followupMemory') || {};
      mem.clicked = mem.clicked || [];
      mem.clicked.push({'label': q.label, 'time': new Date().toISOString()});
      if (mem.clicked.length > 20) mem.clicked = mem.clicked.slice(-20);
      // 推进阶段
      const ikMap = { '回撤':'explain_drawdown','收益':'expected_return','稳健':'safer_alternative',
        '进取':'more_aggressive','费用':'fee_detail','买':'how_to_buy','详情':'view_detail',
        '测评':'reassess_risk','重评':'reassess_risk','评估':'reassess_risk',
        '稳健方案':'view_safe_plan','市场':'market_impact','对比':'which_fits_me' };
      for (const [kw, ik] of Object.entries(ikMap)) {
        if (q.label.includes(kw)) {
          const topic = mem.current_topic_key || 'general';
          const stages = {'allocation_plan':['overview','understand_risk','compare_options','execution_ready'],
            'market_analysis':['overview','personal_impact','allocation_response'],
            'risk_mismatch':['explain_gap','reassess_or_safe_plan','safe_plan'],
            'product_recommend':['overview','understand_risk','compare_options','execution_ready']};
          const topicStages = stages[topic] || ['explore','deepen','decide'];
          const advance = {'explain_drawdown':'understand_risk','expected_return':'understand_risk',
            'safer_alternative':'compare_options','more_aggressive':'compare_options',
            'fee_detail':'compare_options','how_to_buy':'execution_ready','view_detail':'understand_risk',
            'reassess_risk':'reassess_or_safe_plan','view_safe_plan':'safe_plan',
            'market_impact':'personal_impact','which_fits_me':'fit_user'};
          const newStage = advance[ik];
          if (newStage && topicStages.includes(newStage)) {
            mem.topic_stages = mem.topic_stages || {};
            mem.topic_stages[topic] = newStage;
          }
          mem.completed_intents = mem.completed_intents || {};
          mem.completed_intents[topic] = mem.completed_intents[topic] || [];
          if (!mem.completed_intents[topic].includes(ik)) mem.completed_intents[topic].push(ik);
          break;
        }
      }
      Storage.set('qingzhou_followupMemory', mem);

      switch (q.type) {
        case 'start_risk_quiz':
          startQuestionnaire();
          break;
        case 'send_message':
          document.getElementById('userInput').value = q.text;
          sendMessage();
          break;
        case 'handoff':
          triggerHandoff(q.text || '用户通过追问请求转人工');
          break;
        default:
          document.getElementById('userInput').value = q.text;
          sendMessage();
      }
    };
    div.appendChild(chip);
  });

  window.NavrenI18n?.apply(div);
}

function addFollowUpQuestions(msgEl, reply) {
  // 问候/通用回复/错误提示不显示追问
  if (/^(您好|你好|Hi|Hello|网络|抱歉|系统错误)/.test(reply.trim())) return;

  const profile = typeof assembleProfile === 'function' ? (() => { try { return assembleProfile(); } catch(e) { return null; } })() : null;
  const risk = profile?.risk;
  const finance = profile?.finance;
  const questions = [];

  // ══ 风险错配检测（前端兜底，优先级最高） ══
  const isRiskMismatch = /风险等级不匹配|目标收益偏高|不能.*直接推荐.*高波动|通常需要承担.*R[3-5]|不能.*超.*风险|重新.*风险测评/.test(reply)
    || (/\d+[%％]/.test(reply) && /目标收益.*偏高|不能.*推荐.*高/.test(reply))
    || (/R\d.*不能|不能.*R\d/.test(reply) && /推荐|配置/.test(reply));

  if (isRiskMismatch) {
    const riskLevel = risk?.level || '';
    const targetMatch = reply.match(/(\d+\.?\d*)\s*[%％]/);
    const targetReturn = targetMatch ? targetMatch[1] : '';
    const questions = [
      '重新风险测评',
      riskLevel ? `查看${riskLevel}稳健方案` : '查看稳健方案',
    ];
    if (targetReturn) {
      questions.push(`了解${targetReturn}%目标风险`);
    } else {
      questions.push('了解目标收益需要承担的风险');
    }
    renderFallbackQuestions(msgEl, questions);
    return;
  }

  // 基于画像状态 + 回复内容动态生成追问
  if (risk && finance?.amount && finance?.horizon) {
    // 画像完整 → 追问配置细节
    if (/固收|债券|债/.test(reply) && /权益|指数|股票/.test(reply)) {
      questions.push('固收和权益的比例可以调整吗', '哪个部分风险更大', '预期收益分别多少');
    } else if (/R\d/.test(reply)) {
      const rLevel = reply.match(/R(\d)/);
      if (rLevel) {
        questions.push(`R${rLevel[1]}的产品我适合买多少`, '历史最大回撤是多少', '同风险等级还有哪些产品');
      }
    } else {
      questions.push('这个方案的预期收益是多少', '最大可能会亏多少', '有没有替代产品');
    }
  } else if (risk && !finance?.amount) {
    // 有风险缺金额 → 引导补金额
    const riskLabel = risk.label || '稳健型';
    questions.push(`我是${riskLabel}，有20万怎么配`, '先告诉我最少要多少钱起投', '能不能先推荐再补充信息');
  } else if (risk && !finance?.horizon) {
    questions.push('短期和长期推荐有什么区别', '我先按3年计划吧', '如果随时要用怎么配置');
  } else if (!risk) {
    // 无画像 → 引导评估
    questions.push('帮我测一下风险偏好', '风险评估要多久', '不评估能推荐吗');
  }

  // 通用追问兜底
  if (questions.length < 3) {
    if (/市场|行情|走势|利率/.test(reply)) {
      questions.push('对债市有什么影响', '现在适合入场吗');
    } else if (/保本|安全|稳健/.test(reply)) {
      questions.push('有没有存款类产品', '大额存单利率多少');
    } else if (/期限|封闭|多久/.test(reply)) {
      questions.push('有随时能取的产品吗', '封闭期内急用钱怎么办');
    }
    // 补充至3个：优先用风险/画像相关，不用泛化兜底
    while (questions.length < 3) {
      if (risk && !questions.includes('重新风险测评')) {
        questions.push('重新风险测评');
      } else if (risk && !questions.includes('帮我看看现在适合买什么')) {
        questions.push('帮我看看现在适合买什么');
      } else if (!questions.includes('最近市场怎么样')) {
        questions.push('最近市场怎么样');
      }
    }
  }

  // 渲染兜底追问（区分 start_risk_quiz 和 send_message）
  renderFallbackQuestions(msgEl, questions.slice(0, 3));
}

/** 渲染前端兜底追问按钮 */
function renderFallbackQuestions(msgEl, questions) {
  const div = createFollowUpRegion(msgEl);
  if (!div) return;

  questions.forEach(q => {
    const chip = document.createElement('button');
    chip.type='button';chip.className='follow-up-chip';
    chip.textContent = q;
    chip.onclick = (e) => {
      e.stopPropagation();
      if(!div.__navrenIsCurrent())return;
      const mem = Storage.get('qingzhou_followupMemory') || {};
      mem.clicked = mem.clicked || [];
      mem.clicked.push({'label': q, 'time': new Date().toISOString()});
      if (mem.clicked.length > 20) mem.clicked = mem.clicked.slice(-20);
      Storage.set('qingzhou_followupMemory', mem);

      if (q === '重新风险测评') {
        startQuestionnaire();
      } else {
        document.getElementById('userInput').value = q;
        sendMessage();
      }
    };
    div.appendChild(chip);
  });

  window.NavrenI18n?.apply(div);
}

// ── 风险重评提醒 ──
function checkRiskReassessmentNudge(msgEl) {
  const risk = Storage.get('qingzhou_riskProfile');
  if (!risk) return;

  const assessedAt = new Date(risk.assessedAt);
  const monthsSince = (Date.now() - assessedAt.getTime()) / (1000*60*60*24*30);
  const stages = Storage.get('qingzhou_lifeStages') || [];
  const hasStageChange = stages.length > 0;

  if (monthsSince > 6 || hasStageChange) {
    const bubble = msgEl.querySelector('.bubble');
    const nudge = document.createElement('div');
    nudge.style.cssText = 'margin-top:10px;padding:8px 12px;background:var(--gold-light);border-radius:var(--r-sm);font-size:var(--font-size-xs);color:var(--gold-dark);line-height:1.6';
    nudge.innerHTML = monthsSince > 6
      ? `💡 您的风险评估已超过 ${Math.floor(monthsSince)} 个月，建议<a style="color:var(--ink);cursor:pointer;text-decoration:underline" onclick="startQuestionnaire()">重新评估</a>以获得更精准的建议。`
      : '💡 您的人生阶段发生了变化，建议<a style="color:var(--ink);cursor:pointer;text-decoration:underline" onclick="startQuestionnaire()">重新评估风险</a>以确保推荐方案匹配当前状况。';
    bubble.appendChild(nudge);
  }
}

// ── Handoff ──
async function triggerHandoff(reason) {
  handoffActive = true;
  try {
    await BankClient.refreshIdentity();const guard=BankClient.guard();
    const state=await BankClient.get('/overview');if(!guard.valid(state))return;
    const risk=Storage.get('qingzhou_riskProfile');
    const boundRisk=risk?.workspace_id===state.workspace_id&&risk?.privacy_revision===state.privacy_revision;
    const syncedItems=['业务参考日期：'+state.as_of,'业务风险配置：R'+state.profile.risk_level+'（演示配置，非正式测评）'];
    if(boundRisk)syncedItems.push('本地问卷参考：'+risk.level+' '+risk.label+'（不替代正式测评）');
    const recentMessages=currentChatHistory().slice(-4).map(m=>({role:m.role,text:String(m.content||'').replace(/<[^>]*>/g,'').slice(0,180)}));
    const packet={...chatBinding(),reason:String(reason),syncedItems,recentMessages,productCtx:'产品身份与购买条件请在发现逐项重新核对',timestamp:new Date().toISOString()};
    Storage.set('qingzhou_handoffPackage',packet);
    Storage.addKeyMoment('已准备本地顾问交接摘要');
    addMessage('ai',`<strong>本地顾问交接摘要已准备</strong><p>摘要保存在本机，可在本次对话中回看，未连接真实人工服务。业务配置与问卷参考分别保留。</p><div>${syncedItems.map(i=>'<p>'+escapeHtml(i)+'</p>').join('')}</div>`);
    scrollToBottom();
  } catch(e) {addMessage('ai','交接摘要暂时无法核对：'+escapeHtml(e.message)+'。请稍后重新准备。');}
}

// ── Risk Questionnaire ──
const RISK_QUESTIONS = [
  { q: '您的投资经验？', opts: ['A. 几乎没有', 'B. 1年以内', 'C. 1-3年', 'D. 3年以上'], scores: [0, 2, 4, 6] },
  { q: '您对理财产品的了解？', opts: ['A. 完全不了解', 'B. 知道一些基本概念', 'C. 比较熟悉', 'D. 非常了解'], scores: [0, 2, 4, 6] },
  { q: '您能接受的最大本金亏损？', opts: ['A. 不能接受亏损', 'B. 5%以内', 'C. 10%以内', 'D. 20%以上'], scores: [0, 3, 6, 9] },
  { q: '这笔钱的投资期限？', opts: ['A. 随时要用', 'B. 1年以内', 'C. 1-3年', 'D. 3年以上'], scores: [0, 2, 4, 6] },
  { q: '您的收入稳定性？', opts: ['A. 不稳定', 'B. 一般', 'C. 比较稳定', 'D. 非常稳定'], scores: [0, 2, 4, 6] },
  { q: '您的投资目标？', opts: ['A. 保本为主', 'B. 稳健增值', 'C. 追求较高收益', 'D. 追求高收益'], scores: [0, 3, 6, 9] },
  { q: '市场下跌10%时您会？', opts: ['A. 立刻赎回', 'B. 有点慌但观望', 'C. 继续持有', 'D. 逢低加仓'], scores: [0, 2, 4, 6] },
  { q: '可投金额占总资产比例？', opts: ['A. 大部分(>50%)', 'B. 约30-50%', 'C. 约10-30%', 'D. 小部分(<10%)'], scores: [0, 2, 4, 6] }
];

function startQuestionnaire(callback) {
  let currentQ = 0;
  let totalScore = 0;
  const answers = [];

  function showQuestion() {
    if (currentQ >= RISK_QUESTIONS.length) {
      finishQuestionnaire(totalScore, answers, callback);
      return;
    }
    const q = RISK_QUESTIONS[currentQ];
    const quizDiv = document.createElement('div');
    quizDiv.className = 'message ai';
    quizDiv.id = 'quizMsg';
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = '📋';
    const bubble = document.createElement('div');
    bubble.className = 'bubble quiz-inline';
    bubble.innerHTML = `
      <div class="quiz-progress">📋 风险偏好评估 · ${currentQ + 1}/${RISK_QUESTIONS.length}</div>
      <div class="quiz-question">${q.q}</div>
      <div class="quiz-options">${q.opts.map((o, i) => `<div class="quiz-option" data-idx="${i}"><span style="display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:50%;border:2px solid #C8A45C;font-size:var(--font-size-base);font-weight:700;color:#C8A45C;flex-shrink:0;">${'ABCD'[i]}</span><span>${o.replace(/^[A-D][.、]\s*/, '')}</span></div>`).join('')}</div>
    `;
    quizDiv.appendChild(avatar);
    quizDiv.appendChild(bubble);
    clearFollowUpQuestions();
    document.getElementById('chatMessages').appendChild(quizDiv);
    scrollToBottom();

    bubble.querySelectorAll('.quiz-option').forEach(opt => {
      opt.onclick = () => {
        const idx = parseInt(opt.dataset.idx);
        totalScore += q.scores[idx];
        answers.push(idx);
        quizDiv.remove();
        currentQ++;
        showQuestion();
      };
    });
  }

  function finishQuestionnaire(score, ans, cb) {
    let level, label, maxDrawdown, maxEquity;
    if (score <= 12) { level = 'R1'; label = '保守型'; maxDrawdown = '<0.5%'; maxEquity = 0; }
    else if (score <= 24) { level = 'R2'; label = '稳健型'; maxDrawdown = '<1.5%'; maxEquity = 0.10; }
    else if (score <= 36) { level = 'R3'; label = '平衡型'; maxDrawdown = '<5%'; maxEquity = 0.25; }
    else if (score <= 45) { level = 'R4'; label = '进取型'; maxDrawdown = '<15%'; maxEquity = 0.60; }
    else { level = 'R5'; label = '激进型'; maxDrawdown = '<30%'; maxEquity = 0.90; }

    const result = {
      ...chatBinding(),source_scope:'browser_questionnaire',formal_assessment:false,
      level, score, label, maxDrawdown, maxEquityRatio: maxEquity,
      answers, assessedAt: new Date().toISOString(),
      expiresAt: new Date(Date.now() + 365 * 86400000).toISOString()
    };
    Storage.set('qingzhou_riskProfile', result);
    Storage.addProfileHistory({
      field: 'risk_level', oldValue: null, newValue: level,
      source: 'risk_assessment', timestamp: new Date().toISOString(),
      confidence: 1, context: '风险评估问卷', confirmed: true
    });
    Storage.addKeyMoment(`用户完成了风险评估问卷（${level}-${label}）`);

    const msgs = {
      classic: `问卷参考结果为 <strong>${level} ${label}</strong>（评分 ${score}/54）。这是本地问卷记录，不能替代银行正式风险测评，也不代表已经取得申购资格。`,
      senior: `您的问卷参考结果为<strong>${label}</strong>。这份结果保存在本机，银行正式风险测评和购买条件还需要另行核对。`,
      youth: `问卷参考画像：<strong>${level} ${label}</strong>，评分 ${score}/54。它是本机参考记录，正式测评与购买资格仍需核对。`
    };
    addMessage('ai', msgs[currentMode] || msgs.classic);

    showToast('问卷结果已保存，聊天记录保留');

    scrollToBottom();
    if (cb) cb(result);
  }

  showQuestion();
}

// 关联卡是资料附件；所有对象版本由新票据在服务器再次核对。
function cloneChatReference(ref){
  if(!ref||typeof ref!=="object"||Array.isArray(ref)||typeof ref.kind!=="string"||
    typeof ref.id!=="string"||!ref.kind||!ref.id||ref.execution_authority===true)return null;
  try{return JSON.parse(JSON.stringify(ref));}catch(_){return null;}
}
function currentChatReference(){
  const b=window.NavrenChatReferenceBinding,s=chatBinding();
  if(!b||b.registered_user_id!==s.registered_user_id||b.workspace_id!==s.workspace_id||b.privacy_revision!==s.privacy_revision)return null;
  return cloneChatReference(window.NavrenChatReference);
}
function chatReferencePacket(ref){
  return Object.fromEntries(['kind','id','source_scope','version','label','parameters']
    .filter(key=>ref[key]!==undefined).map(key=>[key,JSON.parse(JSON.stringify(ref[key]))]));
}
function updateChatSendState(){
  const button=document.getElementById('sendBtn'),input=document.getElementById('userInput');
  if(button)button.disabled=chatSending||newConversationStarting||conversationScopeInvalid||currentConversationScope()===null||!chatArchiveReady||chatArchiveBusy||(!input?.value.trim()&&!currentChatReference()&&!(window.NavrenChatAttachments?.files().length));
  const newButton=document.getElementById('newConversationBtn');
  if(newButton){newButton.disabled=chatSending||newConversationStarting;newButton.setAttribute('aria-busy',String(newConversationStarting));}
}
function clearChatReference(){
  chatReferenceRevision++;
  window.NavrenChatReference=null;window.NavrenChatReferenceBinding=null;window.NavrenChatReturnView=null;
  window.NavrenPageContextId=null;window.NavrenMarketContext=null;window.NavrenGraphContext=null;
  syncChatReference();
}
function retryChatRequest(snapshot,binding,message=null){
  const identity=chatBinding();
  if(chatSending||newConversationStarting||binding.workspace_id!==identity.workspace_id||binding.privacy_revision!==identity.privacy_revision)return;
  if((snapshot.conversation_scope??null)!==currentConversationScope()){
    showToast('这个问题属于之前的对话，请在当前对话重新提问。');return;
  }
  if(snapshot.internal_choice===true){
    if(typeof showToast==='function')showToast(window.NavrenI18n?.language==='en'?'This choice has expired. Please ask again.':'此选择已失效，请重新查询。');
    return;
  }
  const ref=cloneChatReference(snapshot.reference);
  if(typeof snapshot.text!=='string')return;
  if(snapshot.had_image&&(!snapshot.imageBlob||typeof snapshot.image!=='string')){
    showToast('原问题包含图片，请重新附上图片后使用发送按钮。');return;
  }
  if(snapshot.had_audio&&!snapshot.voiceClip?.blob){showToast('原问题包含录音，请重新录音后发送。');return;}
  if(!snapshot.text.trim()&&!ref&&!snapshot.imageBlob&&!snapshot.voiceClip?.blob)return;
  if(message?.__navrenConsumedReplyControls?.retry===true)return;
  if(message&&!consumeReplyControl(message,'retry',binding))return;
  // 重试直接使用冻结请求；不会先把旧引用放进另写的草稿。
  sendMessage(null,{text:snapshot.text,conversation_scope:snapshot.conversation_scope??null,reference:ref,return_view:snapshot.return_view,internal_choice:snapshot.internal_choice===true,
    image:snapshot.image,imageBlob:snapshot.imageBlob,had_image:snapshot.had_image===true,
    voiceClip:snapshot.voiceClip,had_audio:snapshot.had_audio===true},
    {internalChoice:snapshot.internal_choice===true});
}
function chatReferenceTitle(ref){
  return String(ref?.title||ref?.label||ref?.id||'关联资料').slice(0,200);
}
function chatReferenceUi(text){return window.NavrenI18n?.t(text)??text;}
function chatReferenceHref(view){
  const pages={account:'account.html',mine:'mine.html',recommend:'recommend.html',chat:'chat.html',copilot:'copilot.html'};
  const fields=['page','view','object_kind','object_id','source_scope','portfolio_id','object_version','as_of','date','period','edition_date','revision','time_view','filters','selected','return_context_id','workspace_id','privacy_revision','scrollposition','focustarget','listposition'];
  const bounded=(value,depth=0)=>{
    if(depth>4)return false;
    if(value===null||typeof value==='boolean')return true;
    if(typeof value==='number')return Number.isFinite(value);
    if(typeof value==='string')return value.length<=512;
    if(Array.isArray(value))return value.length<=10&&value.every(item=>bounded(item,depth+1));
    return !!value&&typeof value==='object'&&Object.keys(value).length<=20&&Object.entries(value).every(([key,item])=>key.length<=80&&bounded(item,depth+1));
  };
  try{
    if(!view||typeof view!=='object'||Array.isArray(view)||!Object.hasOwn(pages,view.page)||
      typeof view.view!=='string'||!/^[a-z][a-z0-9_-]{0,63}$/.test(view.view)||
      Object.keys(view).some(key=>!fields.includes(key))||!bounded(view)||typeof window.NavrenViews?.url!=='function')return null;
    const href=NavrenViews.url(JSON.parse(JSON.stringify(view))),url=new URL(href,location.href);
    if(url.origin!==location.origin||url.pathname!==new URL(pages[view.page],location.href).pathname)return null;
    return url.pathname+url.search+url.hash;
  }catch(_){return null;}
}
function chatUserMessageHtml(text,ref,returnView=null){
  const snapshot=cloneChatReference(ref),body=typeof text==='string'?escapeHtml(text):'';
  if(!snapshot)return body;
  const href=chatReferenceHref(returnView);
  const summary='<span class="chat-user-reference" role="group" aria-label="'+escapeHtml(chatReferenceUi('随此消息发送的关联资料'))+'">'+
    '<span class="chat-user-reference-label">'+escapeHtml(chatReferenceUi('关联资料'))+'</span><span class="chat-user-reference-title" translate="no" data-i18n-skip>'+ 
    escapeHtml(chatReferenceTitle(snapshot))+'</span>'+(href?'<a class="chat-user-reference-open" href="'+
      escapeHtml(href).replace(/"/g,'&quot;')+'">'+escapeHtml(chatReferenceUi('查看资料'))+'</a>':'')+'</span>';
  return summary+(body?'<span class="chat-user-text">'+body+'</span>':'');
}
function renderChatVoiceReference(message,ref,returnView=null){
  if(!message?.isConnected)return;
  const bubble=message.querySelector('.bubble');if(!bubble)return;
  bubble.querySelectorAll('.chat-user-reference').forEach(item=>item.remove());
  const template=document.createElement('template');template.innerHTML=chatUserMessageHtml('',ref,returnView);
  const summary=template.content.firstElementChild;
  if(summary)bubble.insertBefore(summary,bubble.querySelector('.voice-transcript')||bubble.firstChild);
}
function chatReferenceIcon(kind){
  const paths={card:'M3 6h18v13H3z M3 10h18 M15 15h3',account:'M4 5h16v15H4z M7 9h10 M7 13h6',
    calendar:'M4 5h16v15H4z M8 3v4 M16 3v4 M4 10h16',graph:'M4 17l5-6 5 3 6-8 M3 21h18',
    event:'M5 4h14v16H5z M8 8h8 M8 12h8 M8 16h5',weekly:'M5 4h14v16H5z M8 8h8 M8 12h8 M8 16h5'};
  const key=kind==='calendar_item'?'calendar':kind;
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');
  const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',paths[key]||paths.event);svg.append(path);return svg;
}
function syncChatReference(){
  const area=document.querySelector('.chat-input-area');if(!area)return;
  let card=document.getElementById('navrenChatContext');
  if(!card){card=document.createElement('section');card.id='navrenChatContext';card.className='chat-reference-card';area.prepend(card);}
  card.setAttribute('aria-label',chatReferenceUi('关联资料'));
  card.replaceChildren();const ref=currentChatReference();card.hidden=!ref;
  if(!ref){updateChatSendState();return;}
  const icon=document.createElement('span');icon.className='chat-reference-icon';icon.append(chatReferenceIcon(ref.kind));
  const copy=document.createElement('div');copy.className='chat-reference-copy';
  const title=document.createElement('strong');title.className='chat-reference-title';title.textContent=chatReferenceTitle(ref);title.title=title.textContent;title.setAttribute('translate','no');title.dataset.i18nSkip='';
  const metadata=document.createElement('span');metadata.className='chat-reference-meta';metadata.setAttribute('translate','no');metadata.dataset.i18nSkip='';
  const source=ref.source_label||chatReferenceUi({market:'公开市场资料',imported:'本地账单',imported_local_csv:'本地账单',simulation:'账户资料',bank_product:'银行产品',public_catalog:'公开产品资料',public_nav:'公开净值资料',server_research:'研究资料'}[ref.source_scope]||'关联资料');
  const view=window.NavrenChatReturnView;
  const date=ref.edition_date||ref.parameters?.edition_date||ref.period||ref.as_of||view?.edition_date||view?.period||view?.as_of||
    (ref.kind==='weekly'&&/^\d{4}-\d{2}-\d{2}$/.test(ref.id)?ref.id:null);
  const revision=ref.revision??ref.parameters?.revision??(ref.kind==='weekly'?ref.version:null);
  const version=ref.parameters?.event_version??ref.version;
  const versionLabel=version!=null&&ref.kind!=='weekly'?chatReferenceUi('版本')+' '+String(version).slice(0,12):null;
  metadata.textContent=[source,date,revision!=null?chatReferenceUi('第 '+revision+' 版'):null,versionLabel].filter(Boolean).join(' · ');
  metadata.title=version!=null?source+' · '+String(date||'')+' · '+chatReferenceUi('版本')+' '+version:metadata.textContent;
  copy.append(title,metadata);
  const actions=document.createElement('div');actions.className='chat-reference-actions';
  if(window.NavrenChatReturnView&&window.NavrenViews){
    const back=document.createElement('a');back.className='chat-reference-back';back.href=NavrenViews.url(NavrenChatReturnView);back.textContent=chatReferenceUi('返回');back.setAttribute('aria-label',chatReferenceUi('返回关联资料'));
    back.onclick=e=>{if(chatSending){e.preventDefault();return;}sessionStorage.setItem('navren_return_view',JSON.stringify({...NavrenChatReturnView,...chatBinding()}));};actions.append(back);
  }
  const remove=document.createElement('button');remove.type='button';remove.className='chat-reference-remove';remove.setAttribute('aria-label',chatReferenceUi('移除关联资料'));remove.title=chatReferenceUi('移除关联资料');remove.disabled=chatSending;
  const close=document.createElementNS('http://www.w3.org/2000/svg','svg');close.setAttribute('viewBox','0 0 24 24');close.setAttribute('aria-hidden','true');const path=document.createElementNS(close.namespaceURI,'path');path.setAttribute('d','M6 6l12 12 M18 6L6 18');close.append(path);remove.append(close);remove.onclick=()=>clearChatReference();actions.append(remove);
  card.setAttribute('aria-busy',String(chatSending));card.append(icon,copy,actions);updateChatSendState();
}
window.addEventListener('navren:language',()=>{
  if(document.getElementById('navrenChatContext'))syncChatReference();
  // 仅改固定摘要槽；用户气泡、引用原名和语音转写仍保持原文。
  document.querySelectorAll('.chat-user-reference').forEach(summary=>{
    summary.setAttribute('aria-label',chatReferenceUi('随此消息发送的关联资料'));
    const label=summary.querySelector('.chat-user-reference-label'),link=summary.querySelector('.chat-user-reference-open');
    if(label)label.textContent=chatReferenceUi('关联资料');if(link)link.textContent=chatReferenceUi('查看资料');
  });
});
async function receivePageQuestion(id,{focus=true}={}){
  const input=document.getElementById('userInput'),draftAtClaim=input.value;
  const guard=BankClient.guard(),privacyTurn=globalThis.NavrenChatPrivacyVersion||0,referenceTurn=chatReferenceRevision;
  try{
    const data=await BankClient.post('/page-contexts/'+encodeURIComponent(id)+'/claim',{});
    if(!guard.valid(data)||privacyTurn!==(globalThis.NavrenChatPrivacyVersion||0)||referenceTurn!==chatReferenceRevision)return;
    const clean=new URL(location.href);clean.searchParams.delete('context');history.replaceState(history.state,'',clean.pathname+clean.search+clean.hash);
    if(data.open_once===false)return;
    const ref=cloneChatReference(data.reference);
    chatReferenceRevision++;window.NavrenChatReference=ref;window.NavrenChatReferenceBinding=ref?chatBinding():null;
    window.NavrenChatReturnView=ref?data.return_view:null;window.NavrenPageContextId=null;
    // 关联跳转只添加附件。无引用旧文字票据仅兼容空且未变化的输入框。
    if(!ref&&!draftAtClaim&&!input.value&&typeof data.question==='string')input.value=data.question;
    input.dispatchEvent(new Event('input'));syncChatReference();if(focus)input.focus({preventScroll:true});
  }catch(e){if(guard.valid()&&privacyTurn===(globalThis.NavrenChatPrivacyVersion||0))addMessage('ai',escapeHtml(e.message));}
}
for(const type of ['navren:privacy-reset','navren:identity-revoked','navren:account-change','navren:account-switched','pagehide'])
  window.addEventListener(type,()=>{chatRequestVersion++;clearChatReference();});
