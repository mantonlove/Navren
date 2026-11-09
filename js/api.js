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

const Api = {
  async sendMessage(text, mode, imageBase64 = null, sentiment = null, onProgress = null, requestOptions = {}) {
    if(imageBase64!==null&&imageBase64!==undefined&&imageBase64!==''){
      const error=Error('请先上传原图片，再使用已保存的媒体发送。');error.code='LEGACY_IMAGE_REJECTED';throw error;
    }
    const mediaIds=agentMediaIds(requestOptions?.media_ids);
    try {
      // 在任何 await 前固定原会话。浏览器旧画像／历史不能成为新会话上下文。
      const identity=typeof BankClient!=='undefined'?BankClient.identity():null;
      const sourceGuard=typeof BankClient!=='undefined'?BankClient.guard():null;
      if(!identity||!/^[a-f0-9]{32}$/.test(identity.registered_user_id||'')||!sourceGuard?.valid(identity))
        throw new Error('请登录并选择本人画像后再发送。');
      // 完整归档与模型近期上下文都由当前受权服务端读取；不回传旧缓存。
      const profile={};
      const requestBody={text,mode:mode||'classic'};
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
        const response = await this._fetchAgent(requestBody, onProgress, sourceGuard);
        return this._handleResponse(response, text, profile);
      } catch (err) {
        const msg = err.message || '';
        console.warn('Agent API failed:', msg);
        if (msg.includes('429') || msg.includes('QPM') || msg.includes('限流')) {
          if (typeof showToast === 'function') showToast('请求过于频繁，请稍后重试');
        }
        return {bank:true,reply:'对话服务暂不可用。本次对话没有确认执行操作，请到「账户」查询原操作状态后再试。',isFallback:true};
      }
    } catch (fatalErr) {
      console.error('Fatal error in sendMessage:', fatalErr.message, fatalErr.stack);
      return { reply: '系统错误，请稍后重试。\n\n⚠️ 理财非存款，产品有风险，投资须谨慎。', isFallback: true };
    }
  },

  async _fetchAgent(body, onProgress = null, sourceGuard = null) {
    const controller = new AbortController();
    activeAgentRequests.add(controller);
    const timer = setTimeout(() => controller.abort(), API_CONFIG.timeout);

    try {
      const guard=sourceGuard||(typeof BankClient!=='undefined'?BankClient.guard?.():null);
      const current=()=>!controller.signal.aborted&&!!guard&&guard.valid();
      if(!current())throw new Error('账户或页面已变化，已停止旧请求。');
      const bodyJSON=JSON.stringify(body);
      if(new TextEncoder().encode(bodyJSON).byteLength>1048576){const error=Error('对话内容超过允许大小，请缩短文字后重试。');error.code='AGENT_JSON_TOO_LARGE';throw error;}
      const headers=await BankClient.agentHeaders();
      if(!current())throw new Error('账户或页面已变化，已停止旧请求。');
      const res = await fetch('/api/agent', {
        method: 'POST',
        headers: {...headers,
          ...(onProgress ? {Accept:'text/event-stream'} : {})},
        body: bodyJSON,
        signal: controller.signal
      });
      if(!current())throw new Error('账户或页面已变化，已停止旧请求。');
      if (!res.ok) {
        const errText = await res.text();
        throw new Error(`HTTP ${res.status}: ${errText.slice(0, 200)}`);
      }

      if (!onProgress) { const result = await res.json();if(!current())throw new Error('账户或页面已变化，已停止旧请求。'); clearTimeout(timer); return result; }
      const reader = res.body.getReader(), decoder = new TextDecoder();
      let pending = '', result = null;
      while (true) {
        const {value,done} = await reader.read();
        if(!current()){controller.abort();await reader.cancel();throw new Error('账户或页面已变化，已停止旧请求。');}
        pending += decoder.decode(value || new Uint8Array(), {stream:!done});
        let end;
        while ((end = pending.indexOf('\n\n')) >= 0) {
          const block = pending.slice(0,end); pending = pending.slice(end+2);
          const data = block.split('\n').filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trimStart()).join('\n');
          if (!data) continue;
          const event = JSON.parse(data);
          if (event.type === 'CUSTOM' && event.name === 'navren.result') result = event.value;
          else onProgress(event);
        }
        if (done) break;
      }
      clearTimeout(timer);
      if (!result) throw new Error('运行连接中断，未收到最终成果');
      return result;
    } catch (e) {
      clearTimeout(timer);
      throw e;
    } finally{activeAgentRequests.delete(controller);clearTimeout(timer);}
  },

  _handleResponse(data, userText, profile) {
    // data = { reply, intent, slots, products[], isFallback, compliance_pass, allocation? }
    const reply = data?.reply || '';

    const baseResult = {
      reply,
      isFallback: data?.isFallback || false,
      intent: data?.intent || '',
      slots: data?.slots || {},
      products: data?.products || [],
      allocation: data?.allocation || null,
      ui_actions: data?.ui_actions || [],
      follow_up_questions: Array.isArray(data?.follow_up_questions) ? data.follow_up_questions : [],
      followup_memory: data?.followup_memory || null,
      agent_chain: data?.agent_chain || null,
      highlight_names: data?.highlight_names || [],
      image_analysis: data?.image_analysis || null,
      speech_receipt: data?.speech_receipt || '',
      personal_revision: Number.isSafeInteger(data?.personal_revision) ? data.personal_revision : null,
      knowledge_snapshot: data?.knowledge_snapshot || null,
      task_result: data?.task_result ?? null,
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
    if (data.bank) return {...baseResult,bank:true,bank_actions:data.bank_actions||[],budget:data.budget,
      widgets:data.widgets||[],
      market_sources:data.market_sources||[],
      workspace_id:data.workspace_id,version:data.version,bank_response_id:data.bank_response_id,
      cashflow_timeline:data.cashflow_timeline||null,agent_workflow:data.agent_workflow||null,
      user_memory_context:data.user_memory_context||null,
      service_negotiation_request:data.service_negotiation_request||null,
      memory_proposals:data.memory_proposals||[],
      agent_review:data.agent_review||null,agent_runtime:data.agent_runtime||null,
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
