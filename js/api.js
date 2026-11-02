/**
 * Navren — API 调用封装
 * POST /api/agent → 智能体编排（意图→槽位→RAG→合规→LLM→日志）
 * 限流 5次/分钟，内置队列调度
 */

let lastCallTime = 0;
let queueRunning = false;
const callQueue = [];
const activeAgentRequests=new Set();
function cancelAgentRequests(){for(const controller of activeAgentRequests)controller.abort();activeAgentRequests.clear();}
if(typeof window!=='undefined')for(const type of ['navren:privacy-reset','navren:identity-revoked','navren:account-change','navren:account-switched','pagehide'])window.addEventListener(type,cancelAgentRequests);

/** 限流调度器 */
async function rateLimiter() {
  const now = Date.now();
  const elapsed = now - lastCallTime;
  if (elapsed < API_CONFIG.rateLimitDelay) {
    await new Promise(r => setTimeout(r, API_CONFIG.rateLimitDelay - elapsed));
  }
  lastCallTime = Date.now();
}

function agentMediaIds(value) {
  if(value===undefined)return null;
  if(!Array.isArray(value)||value.length>8||value.some(id=>typeof id!=='string'||!/^[a-f0-9]{64}$/.test(id))){
    const error=Error('媒体引用无效，请重新上传后再发送。');error.code='INVALID_MEDIA_IDS';throw error;
  }
  return [...new Set(value)];
}

function agentConversationScope(value) {
  if(value===undefined||value===null)return null;
  if(typeof value!=='string'||!value.startsWith('pd1:')||value.length<=4||value.length>8192||/[\s\u0000-\u001f\u007f]/u.test(value)){
    const error=Error('对话范围无法核验，请开始新对话后重试。');error.code='INVALID_CONVERSATION_SCOPE';throw error;
  }
  return value;
}

// Error descriptions use fixed local codes; verified same-origin reply facts may be preserved.
const AGENT_SAFE_ERROR_MESSAGES=Object.freeze({
  AGENT_IDENTITY_REQUIRED:'请先登录并选择体验画像，再发送问题。',
  REGISTERED_SESSION_EXPIRED:'当前登录会话已失效，请重新登录后继续。',
  BANK_RELOGIN_REQUIRED:'银行访问会话需要重新验证，请先完成账户验证。',
  AGENT_CONTEXT_CHANGED:'账户或页面已变化，本次结果未显示；请求是否已处理或保存尚未确认，请核对账户和聊天记录。',
  AGENT_CONTEXT_CHANGED_PREPARE:'账户或页面已变化，本次请求尚未发送，请核对当前账户后再试。',
  INVALID_MEDIA_IDS:'附件引用无法核对，请重新选择附件。',
  INVALID_CONVERSATION_SCOPE:'对话范围无法核验，请开始新对话后再提问。',
  CONVERSATION_SCOPE_REQUIRED:'对话尚未建立，请点击“新对话”后再发送；历史记录仍可查看。',
  CONVERSATION_SCOPE_UPGRADE_REQUIRED:'对话范围已升级，请点击“新对话”后再发送；历史记录仍可查看。',
  CONVERSATION_SCOPE_STALE:'对话范围已失效，请点击“新对话”后再发送；历史记录仍可查看。',
  CONVERSATION_SCOPE_INVALID:'对话范围无法核验，请点击“新对话”后再发送；历史记录仍可查看。',
  CONVERSATION_SCOPE_CURRENT_TURN_REQUIRED:'当前消息与对话范围无法核对，请点击“新对话”后再发送；历史记录仍可查看。',
  CONVERSATION_SCOPE_MEMBERSHIP_INVALID:'对话记录归属无法核验，请点击“新对话”后再发送；历史记录仍可查看。',
  CONVERSATION_SCOPE_CONFLICT:'当前消息与对话范围冲突，请点击“新对话”后再发送；历史记录仍可查看。',
  CONVERSATION_SCOPE_STORAGE_REQUIRED:'对话范围暂时无法读取，请稍后重试。',
  CONVERSATION_SCOPE_UNAVAILABLE:'对话服务暂时不可用，请稍后重试。',
  LEGACY_IMAGE_REJECTED:'图片需要先正常上传，再发送问题。',
  INVALID_ATTACHMENT_RECEIPT:'附件保存结果无法核对，请重新读取聊天。',
  AGENT_JSON_TOO_LARGE:'对话内容超过允许大小，请缩短文字后重试。',
  AGENT_HTTP_401:'登录会话已失效，请重新登录后继续。',
  AGENT_HTTP_403:'当前会话没有访问权限，请核对登录账号和画像。',
  AGENT_HTTP_413:'对话内容超过允许大小，请缩短文字后重试。',
  AGENT_HTTP_429:'请求过于频繁，请稍后再试。',
  AGENT_HTTP_ERROR:'服务未能完成请求，处理结果暂无法确认。',
  AGENT_TIMEOUT:'本次处理超时，结果暂无法确认，请查看相关记录后再继续。',
  AGENT_CONNECTION_INTERRUPTED:'连接中断，未能确认本次结果，请查看相关记录后再继续。',
  AGENT_STREAM_INVALID:'答复传输未完成，结果暂无法确认，请查看相关记录后再继续。',
  AGENT_PREPARE_FAILED:'本次请求未能准备完成，请检查账户状态后再试。',
  AGENT_UNKNOWN:'本次处理未完成，原因暂无法确定，请查看相关记录后再继续。',
  BOUNDARY_INVALID_INPUT:'本次回答在资料安全校验时未能完成，未展示该回答。',
  BOUNDARY_OUTPUT:'本次回答未通过资料安全校验，未展示该回答。',
  BOUNDARY_RAW_RECORDS:'输入资料超出当前安全处理范围，请缩小范围后再试。',
  AGENT_REVIEW_BLOCKED:'本次回答未通过业务审核，未展示该回答。',
  AGENT_REPEATED_REJECTED_FINAL:'答复重复未通过核验，本次已停止，未展示该回答。',
  AGENT_BUDGET_EXCEEDED:'本次回答未能在处理限制内完成，请调整问题后再试。',
  request_budget_exceeded:'本次回答未能在处理限制内完成，请调整问题后再试。',
  request_deadline_exceeded:'本次回答未能在处理时限内完成。',
  MODEL_USAGE_UNVERIFIED:'本轮模型用量无法核验，后续调用已停止。',
  provider_unavailable:'模型服务本轮暂不可用，回答未完成。',
  provider_timeout:'模型服务本轮响应超时，回答未完成。',
  provider_http_error:'模型服务本轮返回错误，回答未完成。',
  response_truncated:'模型返回内容不完整，回答未完成。',
  invalid_model_contract:'模型返回格式未通过校验，回答未完成。',
  missing_api_key:'对话模型尚未配置，暂时无法完成分析。',
  AGENT_OUTPUT_INVALID:'回答的金额或证据校验未通过，未展示该回答。',
  INVALID_FINAL_CONTRACT:'回答格式未通过校验，未展示该回答。',
  METRIC_MONEY_SYNTAX_REQUIRED:'回答中的金额写法未通过校验，未展示该回答。',
  OPERATION_STATUS_EVIDENCE_REQUIRED:'回答中的操作状态缺少可核实证据，未展示该回答。',
  STALE_EVIDENCE:'账户或资料发生变化，请重新查询。',
  STALE_KNOWLEDGE_EVIDENCE:'引用资料发生变化，请重新查询。',
  STALE_MARKET_EVIDENCE:'市场资料已修订或撤回，请重新查阅。',
  STALE_SERVICE_EVIDENCE:'服务规则或回执已变化，请重新查询。',
  STALE_METRIC_SOURCE:'金融指标来源已变化，请重新查询。',
  METRIC_VERSION_CONFLICT:'金融指标来源已变化，请重新查询。',
  READ_COMPLETION_REQUIRED:'本次查询未完成，暂时无法给出可靠答复。',
  READ_RESULT_REQUIRED:'查询结果未整理成完整回答。',
  BOUNDARY_PUBLIC_QUERY:'资料来源尚未确定，请明确产品名称和资料来源。',
  AGENT_LOCAL_REPAIR_SCOPE_INCOMPLETE:'本次回答未完成核验，未展示相关分析。',
  STATISTIC_BINDING_INVALID:'资料校验未完成，暂时无法给出可靠答复。',
  AGENT_BUDGET_ESTIMATOR_UNAVAILABLE:'预算核验暂不可用，回答未完成。'
});
function agentSafeCode(value){return typeof value==='string'&&Object.hasOwn(AGENT_SAFE_ERROR_MESSAGES,value)?value:'AGENT_UNKNOWN';}
function agentSafeError(code){const error=new Error('AGENT_REQUEST_FAILED');error.code=agentSafeCode(code);return error;}
function agentFailureResponse(value){const code=agentSafeCode(value?.code);return {bank:true,reply:AGENT_SAFE_ERROR_MESSAGES[code],isFallback:true,compliance_pass:false,error:{code}};}
function agentHttpError(status){return agentSafeError(({401:'AGENT_HTTP_401',403:'AGENT_HTTP_403',408:'AGENT_TIMEOUT',413:'AGENT_HTTP_413',429:'AGENT_HTTP_429',504:'AGENT_TIMEOUT'})[status]||'AGENT_HTTP_ERROR');}
// Non-stream HTTP errors may carry a scope code; consume at most 8 KiB and
// accept only fixed scope codes for the expected status. Never show raw bodies.
async function agentHttpScopeError(res){
  const allowed={409:new Set(['CONVERSATION_SCOPE_REQUIRED','CONVERSATION_SCOPE_UPGRADE_REQUIRED','CONVERSATION_SCOPE_STALE','CONVERSATION_SCOPE_CURRENT_TURN_REQUIRED','CONVERSATION_SCOPE_MEMBERSHIP_INVALID','CONVERSATION_SCOPE_CONFLICT']),
    422:new Set(['CONVERSATION_SCOPE_INVALID']),503:new Set(['CONVERSATION_SCOPE_STORAGE_REQUIRED','CONVERSATION_SCOPE_UNAVAILABLE'])};
  if(!allowed[res.status]||!res.body||typeof res.body.getReader!=='function')return null;
  const length=res.headers?.get?.('content-length');
  if(length!==null&&length!==undefined&&(!/^\d+$/.test(length)||Number(length)>8192))return null;
  const reader=res.body.getReader(),decoder=new TextDecoder();let total=0,raw='';
  try{
    while(true){
      const part=await reader.read();if(part.done)break;
      if(!(part.value instanceof Uint8Array))return null;
      total+=part.value.byteLength;if(total>8192){await reader.cancel();return null;}
      raw+=decoder.decode(part.value,{stream:true});
    }
    raw+=decoder.decode();const value=JSON.parse(raw);const code=value?.error?.code;
    return typeof code==='string'&&allowed[res.status].has(code)?agentSafeError(code):null;
  }catch(_){return null;}
}

// Only accept the same-origin runtime's bounded fallback reply shape; never render
// raw HTTP bodies, provider exception messages or unstructured HTML as errors.
function agentBackendFallbackReply(data,code){
  const raw=data?.reply;
  const tail=' 本次对话没有确认执行任何金融操作。';
  const generic='对话或审核服务暂时不可用，请稍后重试。';
  if(data?.bank!==true||data?.compliance_pass!==false||typeof raw!=='string'||raw.length>6000||/[<>]/u.test(raw)||!raw.endsWith(tail))return AGENT_SAFE_ERROR_MESSAGES[code];
  const body=raw.slice(0,-tail.length);
  if(body.endsWith(generic))return body.slice(0,-generic.length)+AGENT_SAFE_ERROR_MESSAGES[code]+tail;
  // The remaining text is generated by the same-origin runtime after its
  // outgoing safety check; retain its validated facts and specific explanation.
  return raw;
}

// Snapshot precisely what this turn will submit; never compare against mutable UI state.
function agentFrozenRequest(body){
  const copy=JSON.parse(JSON.stringify(body));
  function freeze(value){if(value&&typeof value==='object'){Object.values(value).forEach(freeze);Object.freeze(value);}return value;}
  return freeze(copy);
}

const Api = {
  async sendMessage(text, mode, imageBase64 = null, sentiment = null, onProgress = null, requestOptions = {}) {
    if(imageBase64!==null&&imageBase64!==undefined&&imageBase64!==''){
      return agentFailureResponse({code:'LEGACY_IMAGE_REJECTED'});
    }
    try {
      const mediaIds=agentMediaIds(requestOptions?.media_ids);
      const conversationScope=agentConversationScope(requestOptions?.conversation_scope);
      // 在任何 await 前固定原会话。浏览器旧画像／历史不能成为新会话上下文。
      const identity=typeof BankClient!=='undefined'?BankClient.identity():null;
      const sourceGuard=typeof BankClient!=='undefined'?BankClient.guard():null;
      if(!identity||!/^[a-f0-9]{32}$/.test(identity.registered_user_id||'')||!sourceGuard?.valid(identity))
        throw agentSafeError('AGENT_IDENTITY_REQUIRED');
      // 完整归档与模型近期上下文都由当前受权服务端读取；不回传旧缓存。
      const profile={};
      const requestBody={text,mode:mode||'classic'};
      if(conversationScope!==null)requestBody.conversation_scope=conversationScope;
      if(mediaIds!==null)requestBody.media_ids=mediaIds;
      // 票据来自本轮冻结请求，不读取发送等待期间可变的全局引用。
      if(typeof requestOptions.page_context_id==='string'&&requestOptions.page_context_id){
        requestBody.page_context_id=requestOptions.page_context_id;
        // 正式关联票据已经固定对象；不混入遗留入口的可变上下文。
        globalThis.NavrenMarketContext=null;globalThis.NavrenGraphContext=null;
      }else if(requestOptions.isolated_text!==true){
        if(globalThis.NavrenMarketContext){requestBody.market_context=globalThis.NavrenMarketContext;globalThis.NavrenMarketContext=null;}
        if(globalThis.NavrenGraphContext){requestBody.graph_context=globalThis.NavrenGraphContext;globalThis.NavrenGraphContext=null;}
      }
      try {
        const frozenRequest=agentFrozenRequest(requestBody);
        const response = await this._fetchAgent(frozenRequest, onProgress, sourceGuard);
        return this._handleResponse(response, text, profile, frozenRequest);
      } catch (err) {
        return agentFailureResponse(err);
      }
    } catch (fatalErr) {
      return agentFailureResponse(fatalErr);
    }
  },

  async _fetchAgent(body, onProgress = null, sourceGuard = null) {
    const controller = new AbortController();
    activeAgentRequests.add(controller);
    let timedOut=false,submitted=false;
    const timer = setTimeout(() => {timedOut=true;controller.abort();}, API_CONFIG.timeout);

    try {
      const guard=sourceGuard||(typeof BankClient!=='undefined'?BankClient.guard?.():null);
      const current=()=>!controller.signal.aborted&&!!guard&&guard.valid();
      if(!current())throw agentSafeError(timedOut?'AGENT_TIMEOUT':'AGENT_CONTEXT_CHANGED_PREPARE');
      const bodyJSON=JSON.stringify(body);
      if(new TextEncoder().encode(bodyJSON).byteLength>1048576){const error=Error('对话内容超过允许大小，请缩短文字后重试。');error.code='AGENT_JSON_TOO_LARGE';throw error;}
      const headers=await BankClient.agentHeaders();
      if(!current())throw agentSafeError(timedOut?'AGENT_TIMEOUT':'AGENT_CONTEXT_CHANGED_PREPARE');
      submitted=true;
      const res = await fetch('/api/agent', {
        method: 'POST',
        headers: {...headers,
          ...(onProgress ? {Accept:'text/event-stream'} : {})},
        body: bodyJSON,
        signal: controller.signal
      });
      if(!current())throw agentSafeError(timedOut?'AGENT_TIMEOUT':'AGENT_CONTEXT_CHANGED');
      if (!res.ok) {const scopedError=await agentHttpScopeError(res);if(!current())throw agentSafeError(timedOut?'AGENT_TIMEOUT':'AGENT_CONTEXT_CHANGED');throw scopedError||agentHttpError(res.status);}

      if (!onProgress) { let result;try{result=await res.json();}catch(_){throw agentSafeError('AGENT_STREAM_INVALID');}if(!current())throw agentSafeError(timedOut?'AGENT_TIMEOUT':'AGENT_CONTEXT_CHANGED'); clearTimeout(timer); return result; }
      const reader = res.body.getReader(), decoder = new TextDecoder();
      let pending = '', result = null;
      while (true) {
        const {value,done} = await reader.read();
        if(!current()){controller.abort();await reader.cancel();throw agentSafeError(timedOut?'AGENT_TIMEOUT':'AGENT_CONTEXT_CHANGED');}
        pending += decoder.decode(value || new Uint8Array(), {stream:!done});
        let end;
        while ((end = pending.indexOf('\n\n')) >= 0) {
          const block = pending.slice(0,end); pending = pending.slice(end+2);
          const data = block.split('\n').filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trimStart()).join('\n');
          if (!data) continue;
          let event;try{event=JSON.parse(data);}catch(_){throw agentSafeError('AGENT_STREAM_INVALID');}
          if (event.type === 'CUSTOM' && event.name === 'navren.result') result = event.value;
          else onProgress(event);
        }
        if (done) break;
      }
      clearTimeout(timer);
      if (!result) throw agentSafeError('AGENT_STREAM_INVALID');
      return result;
    } catch (e) {
      clearTimeout(timer);
      if(agentSafeCode(e?.code)!=='AGENT_UNKNOWN')throw agentSafeError(e.code);
      throw agentSafeError(timedOut?'AGENT_TIMEOUT':submitted?'AGENT_CONNECTION_INTERRUPTED':'AGENT_PREPARE_FAILED');
    } finally{activeAgentRequests.delete(controller);clearTimeout(timer);}
  },

  _handleResponse(data, userText, profile, frozenRequest = null) {
    // data = { reply, intent, slots, products[], isFallback, compliance_pass, allocation? }
    const fallback=data?.isFallback===true;
    const fallbackCode=fallback?agentSafeCode(data?.error?.code):null;
    const reply = fallback?agentBackendFallbackReply(data,fallbackCode):(data?.reply || '');

    const baseResult = {
      reply,
      isFallback: data?.isFallback || false,
      ...(fallback?{error:{code:fallbackCode}}:{}),
      intent: data?.intent || '',
      slots: data?.slots || {},
      products: data?.products || [],
      allocation: data?.allocation || null,
      ui_actions: fallback?[]:(data?.ui_actions || []),
      follow_up_questions: Array.isArray(data?.follow_up_questions) ? data.follow_up_questions : [],
      followup_memory: data?.followup_memory || null,
      agent_chain: data?.agent_chain || null,
      highlight_names: data?.highlight_names || [],
      image_analysis: data?.image_analysis || null,
      speech_receipt: data?.speech_receipt || '',
      personal_revision: Number.isSafeInteger(data?.personal_revision) ? data.personal_revision : null,
      knowledge_snapshot: data?.knowledge_snapshot || null,
      task_result: fallback?null:(data?.task_result ?? null),
      reply_presentation: data?.reply_presentation ?? null,
      reply_interaction: data?.reply_interaction ?? null,
      environment: data?.environment ?? null,
      privacy_revision: Number.isSafeInteger(data?.privacy_revision) ? data.privacy_revision : null,
      compliance_pass: data?.compliance_pass,
      compliance_flags: data?.compliance_flags || []
    };
    // Only the newly frozen media-receipt branch supplies these fields. Do not
    // manufacture them for ordinary/model/social responses that lack them.
    if(Array.isArray(data?.user_attachments)){
      if(data.user_attachments.length>8||typeof globalThis.NavrenChatMedia?.validate!=='function'){
        const e=Error('附件保存结果无法核验，请重新读取聊天。');e.code='INVALID_ATTACHMENT_RECEIPT';throw e;
      }
      const attachmentIdentity=typeof BankClient!=='undefined'?BankClient.identity():null;
      const attachmentGuard=typeof BankClient!=='undefined'?BankClient.guard():null;
      if(!attachmentIdentity||attachmentGuard?.valid(attachmentIdentity)!==true||
        data.workspace_id!==attachmentIdentity.workspace_id||data.privacy_revision!==attachmentIdentity.privacy_revision){
        const e=Error('附件回执所属账户或授权无法核对，请重新读取聊天。');e.code='INVALID_ATTACHMENT_RECEIPT';throw e;
      }
      baseResult.user_attachments=data.user_attachments.map(media=>globalThis.NavrenChatMedia.validate(media,{boundOnly:true}));
      if(data.attachment_receipt!==undefined){
        const receipt=data.attachment_receipt;
        if(!receipt||typeof receipt!=='object'||Array.isArray(receipt)||Object.keys(receipt).sort().join(',')!=='request_id,status'||
          receipt.status!=='archived'||typeof receipt.request_id!=='string'||!receipt.request_id||receipt.request_id.length>160||
          receipt.request_id!==data.bank_response_id||baseResult.user_attachments.length===0){
          const e=Error('附件保存回执与当前消息不一致，请重新读取聊天。');e.code='INVALID_ATTACHMENT_RECEIPT';throw e;
        }
        baseResult.attachment_receipt=Object.freeze({status:'archived',request_id:receipt.request_id});
      }
    }
    // A host-only receipt for an attachment-only request is not a model answer.
    // Confirm the exact frozen media set and existing identity/receipt checks first.
    const requestedIds=frozenRequest?.media_ids;
    const noContext=frozenRequest&&['page_context_id','market_context','graph_context','image'].every(k=>!Object.hasOwn(frozenRequest,k));
    const archivedIds=baseResult.user_attachments?.map(item=>item.media_id);
    const attachmentOnlyReceipt=fallback&&data?.bank===true&&!Object.hasOwn(data,'error')&&
      typeof userText==='string'&&userText.trim()===''&&typeof frozenRequest?.text==='string'&&frozenRequest.text.trim()===''&&noContext&&
      Array.isArray(requestedIds)&&requestedIds.length>0&&Array.isArray(archivedIds)&&requestedIds.length===archivedIds.length&&
      new Set(requestedIds).size===requestedIds.length&&new Set(archivedIds).size===archivedIds.length&&
      requestedIds.every(id=>archivedIds.includes(id))&&baseResult.attachment_receipt?.status==='archived';
    if(attachmentOnlyReceipt){
      return {reply:'附件已保存。你可以继续补充希望了解的问题。',isFallback:true,compliance_pass:false,bank:true,attachment_only_receipt:true,
        workspace_id:data.workspace_id,privacy_revision:baseResult.privacy_revision,bank_response_id:data.bank_response_id,version:data.version,
        user_attachments:baseResult.user_attachments,attachment_receipt:baseResult.attachment_receipt};
    }
    // A failed response may contain stale, unreviewed structured fields. Keep
    // only the guarded reply, failure code, binding and independently checked
    // attachment receipt; never let failed content activate UI or memory.
    if(fallback){
      const safe={reply,isFallback:true,compliance_pass:false,error:{code:fallbackCode},bank:data?.bank===true,
        workspace_id:data?.workspace_id,privacy_revision:baseResult.privacy_revision,
        bank_response_id:data?.bank_response_id,version:data?.version};
      if(baseResult.user_attachments)safe.user_attachments=baseResult.user_attachments;
      if(baseResult.attachment_receipt)safe.attachment_receipt=baseResult.attachment_receipt;
      return safe;
    }
    if (data.bank) return {...baseResult,bank:true,bank_actions:fallback?[]:(data.bank_actions||[]),budget:fallback?null:data.budget,
      widgets:fallback?[]:(data.widgets||[]),
      market_sources:data.market_sources||[],
      workspace_id:data.workspace_id,version:data.version,bank_response_id:data.bank_response_id,
      cashflow_timeline:data.cashflow_timeline||null,agent_workflow:data.agent_workflow||null,
      user_memory_context:data.user_memory_context||null,
      service_negotiation_request:fallback?null:(data.service_negotiation_request||null),
      memory_proposals:fallback?[]:(data.memory_proposals||[]),
      agent_review:fallback?(fallbackCode==='AGENT_REVIEW_BLOCKED'?{status:'blocked'}:null):(data.agent_review||null),agent_runtime:data.agent_runtime||null,
      answer_contract:data.answer_contract||null};

    // 尝试从 slots 中提取档案更新
    const slots = data?.slots || {};
    const updates = [];
    if (slots.amount && !profile.amount) {
      updates.push({ field: 'amount', value: slots.amount, confidence: 0.8, evidence: userText });
    }
    if (slots.horizon && !profile.horizon) {
      updates.push({ field: 'horizon', value: slots.horizon, confidence: 0.8, evidence: userText });
    }
    if (slots.goal && !profile.goal) {
      updates.push({ field: 'goal', value: slots.goal, confidence: 0.7, evidence: userText });
    }
    if (updates.length > 0 && typeof handleProfileUpdate === 'function') {
      const result = handleProfileUpdate(updates);
      return {
        ...baseResult,
        toast: result?.toast || null
      };
    }

    return baseResult;
  }
};

/** 私有聊天媒体：受信会话 + 原件完整性；不承担助手上传或发布权限。 */
(function(global){
  'use strict';
  const PREFIX='/api/finance/chat-media';
  const HEX64=/^[a-f0-9]{64}$/;
  const HEX32=/^[a-f0-9]{32}$/;
  const DOCUMENT_MIME=Object.freeze({
    'application/pdf':'pdf','application/vnd.openxmlformats-officedocument.wordprocessingml.document':'docx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':'xlsx',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation':'pptx',
    'text/plain':'txt','text/markdown':'md','text/csv':'csv'});
  const DOCUMENT_KEYS=['file_name','document_format','text_chars','extraction_status','extraction_warning'];
  const MIME=new Set(['image/png','image/jpeg','image/webp','image/gif','audio/wav','audio/webm','audio/ogg','audio/mpeg','audio/mp4',
    ...Object.keys(DOCUMENT_MIME)]);
  const PCM_RATES=new Set([8000,16000,22050,24000,44100,48000]);
  const LIMITS=Object.freeze({image_bytes:10485760,audio_bytes:26214400,document_bytes:10485760,document_text_chars:120000,audio_duration_ms_if_embedded:600000,
    image_pixels:40000000,media_per_user_turn:8,pending_per_workspace:32,pending_bytes_per_workspace:104857600,
    total_bytes_per_workspace:536870912,pending_ttl_seconds:86400,concurrent_binary_uploads_per_service:2});
  const DESCRIPTOR_KEYS=['contract','media_id','kind','mime_type','bytes','sha256','duration_ms','storage_status',
    'content_url','historical_readonly','execution_authority','audio_format'];
  const DEADLINE_MS=45000;
  const operations=new Set(),urls=new Set();
  let epoch=0;
  function error(code,message,status){const e=Error(message);e.name='ChatMediaError';e.code=code;if(status)e.status=status;return e;}
  function invalid(){return error('INVALID_MEDIA_DESCRIPTOR','媒体资料无法核验，请重新读取。');}
  function own(value,key){return Object.prototype.hasOwnProperty.call(value,key);}
  function keys(value,expected){
    if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).length!==expected.length||
      expected.some(key=>!own(value,key)))throw invalid();
  }
  function clone(value,seen=new Set(),depth=0){
    if(value===null||['string','boolean'].includes(typeof value))return value;
    if(typeof value==='number'&&Number.isFinite(value))return value;
    if(!value||typeof value!=='object'||depth>64||seen.has(value)||Object.getOwnPropertySymbols(value).length||
      (!Array.isArray(value)&&Object.prototype.toString.call(value)!=='[object Object]'))throw invalid();
    seen.add(value);const result=Array.isArray(value)?[]:Object.create(null);
    for(const key of Object.keys(value)){
      const d=Object.getOwnPropertyDescriptor(value,key);if(!d||!own(d,'value'))throw invalid();
      Object.defineProperty(result,key,{value:clone(d.value,seen,depth+1),enumerable:true,writable:true,configurable:true});
    }
    if(Array.isArray(value)&&Object.keys(value).length!==value.length)throw invalid();
    seen.delete(value);return result;
  }
  function freeze(value){if(value&&typeof value==='object'){Object.keys(value).forEach(k=>freeze(value[k]));Object.freeze(value);}return value;}
  function canonical(value){
    if(value===null||typeof value!=='object')return JSON.stringify(value);
    if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
    return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
  }
  function nonnegative(value){return Number.isSafeInteger(value)&&value>=0;}
  function documentName(value,format){
    if(typeof value!=='string'||Array.from(value).length<1||Array.from(value).length>180||value!==value.trim()||
      /[\/\\]/u.test(value)||/\p{C}/u.test(value)||value.lastIndexOf('.')<=0||
      value.slice(value.lastIndexOf('.')).toLowerCase()!=='.'+format)throw invalid();
    return value;
  }
  function documentFields(media){
    const format=DOCUMENT_MIME[media.mime_type];documentName(media.file_name,format);
    if(media.document_format!==format||!nonnegative(media.text_chars)||media.text_chars>LIMITS.document_text_chars||
      !(media.extraction_status==='text'&&media.text_chars>0&&media.extraction_warning===null||
        media.extraction_status==='no_text'&&media.text_chars===0&&typeof media.extraction_warning==='string'&&
        Array.from(media.extraction_warning).length>0&&Array.from(media.extraction_warning).length<=200))throw invalid();
  }
  function documentDisposition(name){
    // RFC 5987: the server percent-encodes these characters as well.
    return "attachment; filename*=UTF-8''"+encodeURIComponent(name).replace(/[!'()*]/g,c=>'%'+c.charCodeAt(0).toString(16).toUpperCase());
  }
  function validateDescriptor(raw,boundOnly){
    const media=clone(raw),document=media?.kind==='document';keys(media,document?DESCRIPTOR_KEYS.concat(DOCUMENT_KEYS):DESCRIPTOR_KEYS);
    if(media.contract!=='navren.chat-media/1'||typeof media.media_id!=='string'||!HEX64.test(media.media_id)||
      !['image','audio','document'].includes(media.kind)||typeof media.mime_type!=='string'||
      !(MIME.has(media.mime_type)||media.mime_type==='audio/pcm')||
      !Number.isSafeInteger(media.bytes)||media.bytes<=0||media.bytes>LIMITS[media.kind+'_bytes']||
      typeof media.sha256!=='string'||!HEX64.test(media.sha256)||
      !(media.duration_ms===null||Number.isSafeInteger(media.duration_ms)&&media.duration_ms>0&&media.duration_ms<=LIMITS.audio_duration_ms_if_embedded)||
      !['stored','bound'].includes(media.storage_status)||boundOnly&&media.storage_status!=='bound'||
      media.content_url!==PREFIX+'/'+media.media_id||media.historical_readonly!==true||media.execution_authority!==false||
      (media.kind==='image')!==media.mime_type.startsWith('image/')||
      (media.kind==='audio')!==media.mime_type.startsWith('audio/')||document!==own(DOCUMENT_MIME,media.mime_type))throw invalid();
    if(document){
      if(media.duration_ms!==null||media.audio_format!==null)throw invalid();
      documentFields(media);
    }
    if(media.kind==='image'&&(media.duration_ms!==null||media.audio_format!==null))throw invalid();
    if(media.mime_type==='audio/pcm'){
      keys(media.audio_format,['encoding','sample_rate','channels']);
      const format=media.audio_format;
      if(media.kind!=='audio'||format.encoding!=='pcm_s16le'||!PCM_RATES.has(format.sample_rate)||
        ![1,2].includes(format.channels)||format.sample_rate*format.channels*2>0xffffffff||
        media.bytes%(format.channels*2)!==0)throw invalid();
    }else if(media.audio_format!==null)throw invalid();
    return freeze(media);
  }
  function clear(){
    epoch+=1;for(const op of operations){finish(op);op.controller.abort();}operations.clear();
    for(const url of urls){try{global.URL.revokeObjectURL(url);}catch(_){}}
    urls.clear();
  }
  function boundIdentity(value){
    if(!value||typeof value!=='object'||Array.isArray(value))throw error('MEDIA_LOGIN_REQUIRED','请登录并核验当前画像后再使用媒体。');
    const identity=clone(value);
    if(typeof identity.registered_user_id!=='string'||!HEX32.test(identity.registered_user_id)||
      typeof identity.workspace_id!=='string'||!HEX32.test(identity.workspace_id)||
      !['tycoon','grain_farmer','working_landlady','retired_entrepreneur'].includes(identity.account_id)||
      typeof identity.auth_subject!=='string'||!(/^[A-Za-z0-9:_-]{16,192}$/).test(identity.auth_subject)||
      !nonnegative(identity.privacy_revision)||!nonnegative(identity.session_generation)||typeof identity.csrf!=='string'||!identity.csrf||
      !Number.isFinite(identity.auth_expires_at)||!Number.isFinite(identity.lease_expires_at)||
      identity.lease_expires_at>identity.auth_expires_at||Date.now()/1000>=identity.lease_expires_at)
      throw error('MEDIA_LOGIN_REQUIRED','请登录并核验当前画像后再使用媒体。');
    return freeze(identity);
  }
  function capture(signal){
    const client=global.BankClient;
    if(!client||typeof client.identity!=='function'||typeof client.guard!=='function'||typeof client.get!=='function')
      throw error('MEDIA_LOGIN_REQUIRED','请登录并核验当前画像后再使用媒体。');
    let identity,guard;
    try{
      identity=boundIdentity(client.identity());guard=client.guard();
      if(!guard||typeof guard.valid!=='function'||guard.valid(identity)!==true)throw error('MEDIA_STALE','账户或页面已变化，请重新读取媒体。');
    }catch(e){clear();throw safeFailure(e);}
    const op={client,identity,signature:canonical(identity),guard,epoch,controller:new AbortController(),external:signal,onAbort:null,timer:null,timedOut:false};
    if(signal!==undefined){
      if(!signal||typeof signal.aborted!=='boolean'||typeof signal.addEventListener!=='function'||typeof signal.removeEventListener!=='function')throw invalid();
      op.onAbort=()=>op.controller.abort();
      if(signal.aborted)op.controller.abort();else signal.addEventListener('abort',op.onAbort,{once:true});
    }
    operations.add(op);
    op.timer=global.setTimeout(()=>{op.timer=null;op.timedOut=true;op.controller.abort();},DEADLINE_MS);
    try{check(op);}catch(e){finish(op);throw e;}return op;
  }
  function check(op){
    if(op.epoch!==epoch)throw error('MEDIA_STALE','账户或页面已变化，已停止旧媒体请求。');
    if(op.controller.signal.aborted)throw op.timedOut?error('MEDIA_TIMEOUT','媒体操作超时，请重试。'):error('MEDIA_ABORTED','媒体操作已取消。');
    let valid=false;
    try{valid=global.BankClient===op.client&&op.guard.valid(op.identity)===true&&
      canonical(boundIdentity(op.client.identity()))===op.signature&&op.epoch===epoch;}catch(_){valid=false;}
    if(!valid){clear();throw error('MEDIA_STALE','账户、授权或页面已变化，请重新读取媒体。');}
  }
  function finish(op){
    operations.delete(op);if(op.timer!==null){global.clearTimeout(op.timer);op.timer=null;}
    if(op.external&&op.onAbort)op.external.removeEventListener('abort',op.onAbort);
  }
  async function wait(op,action){
    check(op);let onAbort;
    try{
      const source=Promise.resolve(action());
      const aborted=new Promise((_,reject)=>{onAbort=()=>reject(error('MEDIA_ABORTED','媒体操作已取消。'));
        if(op.controller.signal.aborted)onAbort();else op.controller.signal.addEventListener('abort',onAbort,{once:true});});
      const value=await Promise.race([source,aborted]);check(op);return value;
    }catch(e){check(op);throw e;}
    finally{if(onAbort)op.controller.signal.removeEventListener('abort',onAbort);}
  }
  function safeFailure(e){
    if(e?.name==='ChatMediaError')return e;
    return error('MEDIA_NETWORK','媒体服务暂不可用，请稍后重试。');
  }
  function httpFailure(status){
    const values={400:['MEDIA_INVALID_FILE','文件内容无法读取，请检查原件或换为支持的格式。'],
      422:['MEDIA_INVALID_FILE','文件内容无法读取，请检查原件或换为支持的格式。'],
      401:['MEDIA_LOGIN_REQUIRED','登录已失效，请重新核验。'],404:['MEDIA_UNAVAILABLE','媒体不存在、未绑定或当前无权读取。'],
      409:['MEDIA_STALE','媒体所属账户或授权已变化，请重新读取。'],413:['MEDIA_TOO_LARGE','媒体超过允许的大小、像素或时长。'],
      415:['MEDIA_UNSUPPORTED','媒体类型不受支持，或文件内容与类型不符。'],429:['MEDIA_LIMITED','媒体上传繁忙或容量不足，请稍后重试。'],
      503:['MEDIA_CORRUPT','媒体暂时无法完整读取，请稍后重试。']};
    const item=values[status]||['MEDIA_NETWORK','媒体服务暂不可用，请稍后重试。'];return error(item[0],item[1],status);
  }
  function financeEnvelope(value,required,op,privacyRevision){
    // 通用 HTTP200 封装补入身份字段；只接这两个已知字段，不能放宽原件描述符。
    const extra=['workspace_id','privacy_revision'].filter(key=>!required.includes(key)&&own(value,key));
    keys(value,required.concat(extra));
    if(own(value,'workspace_id')&&(value.workspace_id!==op.identity.workspace_id||value.workspace_id!==value.workspace_binding)||
      own(value,'privacy_revision')&&value.privacy_revision!==privacyRevision){
      clear();throw error('MEDIA_STALE','媒体所属账户或授权已变化，请重新读取。');
    }
  }
  async function context(op,previous){
    const result=clone(await wait(op,()=>op.client.get('/chat-media/context')));
    financeEnvelope(result,['contract','workspace_binding','registered_user_id','privacy_revision','archive_epoch','limits'],op,op.identity.privacy_revision);
    keys(result.limits,['image_bytes','audio_bytes','document_bytes','document_text_chars','media_per_user_turn','mime_types']);
    if(result.contract!=='navren.chat-media-context/1'||result.registered_user_id!==op.identity.registered_user_id||result.workspace_binding!==op.identity.workspace_id||
      result.privacy_revision!==op.identity.privacy_revision||!nonnegative(result.archive_epoch)||
      ['image_bytes','audio_bytes','document_bytes','document_text_chars','media_per_user_turn'].some(k=>result.limits[k]!==LIMITS[k])||
      !Array.isArray(result.limits.mime_types)||result.limits.mime_types.length!==MIME.size||
      new Set(result.limits.mime_types).size!==MIME.size||result.limits.mime_types.some(type=>!MIME.has(type))||
      previous&&(result.archive_epoch!==previous.archive_epoch||result.privacy_revision!==previous.privacy_revision)){
      clear();throw error('MEDIA_STALE','媒体上下文或授权已变化，请重新读取。');
    }
    check(op);return freeze(result);
  }
  async function digest(op,blob){
    const bytes=await wait(op,()=>blob.arrayBuffer());
    if(!global.crypto?.subtle?.digest)throw error('MEDIA_HASH_UNAVAILABLE','当前环境无法校验媒体完整性。');
    const hash=await wait(op,()=>global.crypto.subtle.digest('SHA-256',bytes));
    return Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('');
  }
  function options(value,allowed){
    if(value===undefined)return {};
    if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).some(k=>!allowed.includes(k)))throw invalid();
    return value;
  }
  function uploadKey(raw){
    if(raw!==undefined){if(typeof raw!=='string'||!HEX32.test(raw))throw invalid();return raw;}
    if(typeof global.crypto?.getRandomValues!=='function')throw error('MEDIA_RANDOM_UNAVAILABLE','当前环境无法安全创建上传请求。');
    return Array.from(global.crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
  }
  async function upload(blob,settings){
    const setting=options(settings,['uploadKey','signal']),op=capture(setting.signal);
    try{
      if(!(blob instanceof global.Blob)||!Number.isSafeInteger(blob.size)||blob.size<=0)throw invalid();
      const mime=blob.type.split(';',1)[0].trim().toLowerCase();
      if(!MIME.has(mime))throw error('MEDIA_UNSUPPORTED','请选择支持的原图片、音频或文档文件。');
      const kind=own(DOCUMENT_MIME,mime)?'document':mime.split('/')[0];
      const fileName=typeof blob.name==='string'?blob.name:null;
      if(kind==='document')documentName(fileName,DOCUMENT_MIME[mime]);
      let encodedName;
      if(fileName!==null){try{encodedName=encodeURIComponent(fileName);}catch(_){throw invalid();}}
      if(blob.size>LIMITS[kind+'_bytes'])throw httpFailure(413);
      const key=uploadKey(setting.uploadKey),sourceHash=await digest(op,blob),ctx=await context(op);
      const response=await wait(op,()=>global.fetch(PREFIX,{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',
        headers:{'Accept':'application/json','Content-Type':mime,'X-QZ-CSRF':op.identity.csrf,
          'X-Navren-Expected-Workspace':op.identity.workspace_id,'X-Navren-Privacy-Revision':String(ctx.privacy_revision),
          'X-Navren-Archive-Epoch':String(ctx.archive_epoch),'X-Navren-Upload-Key':key,
          ...(encodedName===undefined?{}:{'X-Navren-File-Name':encodedName})},
        // A known-size Blob lets the browser set Content-Length. It is forbidden
        // for browser code to set that header or wrap the bytes as multipart/JSON.
        body:blob,signal:op.controller.signal}));
      if(!response.ok)throw httpFailure(response.status);
      const body=clone(await wait(op,()=>response.json()));
      financeEnvelope(body,['contract','media','workspace_binding','privacy_revision','archive_epoch'],op,ctx.privacy_revision);
      const media=validateDescriptor(body.media,false);
      if(body.contract!=='navren.chat-media-upload/1'||body.workspace_binding!==op.identity.workspace_id||
        body.privacy_revision!==ctx.privacy_revision||body.archive_epoch!==ctx.archive_epoch||
        media.kind!==kind||media.mime_type!==mime||media.bytes!==blob.size||media.sha256!==sourceHash||media.audio_format!==null||
        kind==='document'&&media.file_name!==fileName)throw invalid();
      await context(op,ctx);check(op);return media;
    }catch(e){throw safeFailure(e);}finally{finish(op);}
  }
  async function bind(requestId,descriptors,settings){
    const setting=options(settings,['signal']),op=capture(setting.signal);
    try{
      if(typeof requestId!=='string'||!(/^[A-Za-z0-9_-]{1,160}$/).test(requestId))throw invalid();
      const inputs=clone(descriptors);
      if(!Array.isArray(inputs)||inputs.length>LIMITS.media_per_user_turn)throw invalid();
      const expected=inputs.map(media=>validateDescriptor(media,false)),ids=expected.map(media=>media.media_id);
      if(new Set(ids).size!==ids.length)throw invalid();
      const ctx=await context(op);
      const response=await wait(op,()=>global.fetch(PREFIX+'/bind',{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',
        headers:{'Accept':'application/json','Content-Type':'application/json','X-QZ-CSRF':op.identity.csrf,
          'X-Navren-Expected-Workspace':op.identity.workspace_id,'X-Navren-Privacy-Revision':String(ctx.privacy_revision),
          'X-Navren-Archive-Epoch':String(ctx.archive_epoch)},
        body:JSON.stringify({request_id:requestId,media_ids:ids}),signal:op.controller.signal}));
      if(response.status!==200)throw httpFailure(response.status);
      const body=clone(await wait(op,()=>response.json()));
      financeEnvelope(body,['contract','request_id','attachments','workspace_binding','archive_epoch'],op,ctx.privacy_revision);
      if(body.contract!=='navren.chat-media-bind/1'||body.request_id!==requestId||
        body.workspace_binding!==op.identity.workspace_id||body.archive_epoch!==ctx.archive_epoch||
        !Array.isArray(body.attachments)||body.attachments.length!==expected.length)throw invalid();
      const attachments=body.attachments.map((raw,index)=>{
        const media=validateDescriptor(raw,true);
        if(canonical(media)!==canonical({...expected[index],storage_status:'bound'}))throw invalid();
        return media;
      });
      await context(op,ctx);check(op);return freeze({...body,attachments});
    }catch(e){throw safeFailure(e);}finally{finish(op);}
  }
  async function readIn(op,raw){
    const media=validateDescriptor(raw,true),ctx=await context(op);
    const response=await wait(op,()=>global.fetch(PREFIX+'/'+media.media_id,{method:'GET',credentials:'same-origin',cache:'no-store',redirect:'error',
      headers:{'Accept':media.mime_type},signal:op.controller.signal}));
    if(response.status!==200)throw httpFailure(response.status);
    const header=name=>response.headers?.get?.(name)?.trim()||'';
    if(header('Content-Type').toLowerCase()!==media.mime_type||!(/^[1-9]\d*$/).test(header('Content-Length'))||
      Number(header('Content-Length'))!==media.bytes||header('X-Content-Type-Options').toLowerCase()!=='nosniff'||
      header('Cache-Control').toLowerCase()!=='no-store'||
      (media.kind==='document'?header('Content-Disposition')!==documentDisposition(media.file_name):header('Content-Disposition').toLowerCase()!=='inline')||
      header('Accept-Ranges').toLowerCase()!=='none')throw error('MEDIA_INTEGRITY','媒体响应无法完整核验，已停止读取。');
    const blob=await wait(op,()=>response.blob());
    if(!(blob instanceof global.Blob)||blob.type!==media.mime_type||blob.size!==media.bytes||await digest(op,blob)!==media.sha256)
      throw error('MEDIA_INTEGRITY','媒体内容与保存记录不一致，已停止读取。');
    await context(op,ctx);check(op);return {blob,media};
  }
  async function read(raw,settings){
    const setting=options(settings,['signal']),op=capture(setting.signal);
    try{check(op);const result=await readIn(op,raw);check(op);return result.blob;}catch(e){throw safeFailure(e);}finally{finish(op);}
  }
  function wav(blob,media){
    const format=media.audio_format;if(format===null)return blob;
    const header=new ArrayBuffer(44),view=new DataView(header);
    const ascii=(offset,value)=>{for(let i=0;i<value.length;i++)view.setUint8(offset+i,value.charCodeAt(i));};
    ascii(0,'RIFF');view.setUint32(4,36+media.bytes,true);ascii(8,'WAVE');ascii(12,'fmt ');view.setUint32(16,16,true);
    view.setUint16(20,1,true);view.setUint16(22,format.channels,true);view.setUint32(24,format.sample_rate,true);
    view.setUint32(28,format.sample_rate*format.channels*2,true);view.setUint16(32,format.channels*2,true);
    view.setUint16(34,16,true);ascii(36,'data');view.setUint32(40,media.bytes,true);
    return new global.Blob([header,blob],{type:'audio/wav'});
  }
  async function open(raw,settings){
    const setting=options(settings,['signal']),op=capture(setting.signal);let url=null;
    try{
      const {blob,media}=await readIn(op,raw);check(op);
      const playbackBlob=wav(blob,media);check(op);
      url=global.URL.createObjectURL(playbackBlob);urls.add(url);check(op);
      let disposed=false;
      const dispose=()=>{if(disposed)return;disposed=true;urls.delete(url);try{global.URL.revokeObjectURL(url);}catch(_){}};
      return Object.freeze({url,blob,playbackBlob,media,dispose});
    }catch(e){if(url){urls.delete(url);try{global.URL.revokeObjectURL(url);}catch(_){}}throw safeFailure(e);}
    finally{finish(op);}
  }
  function validate(raw,settings){
    const setting=options(settings,['boundOnly']);if(own(setting,'boundOnly')&&typeof setting.boundOnly!=='boolean')throw invalid();
    const op=capture();try{const media=validateDescriptor(raw,setting.boundOnly===true);check(op);return media;}finally{finish(op);}
  }
  global.NavrenChatMedia=Object.freeze({upload,bind,read,open,validate,clear});
  if(typeof global.addEventListener==='function')for(const event of ['navren:privacy-reset','navren:identity-revoked','navren:account-change','navren:account-switched','pagehide'])
    global.addEventListener(event,clear);
})(typeof window==='object'?window:globalThis);
