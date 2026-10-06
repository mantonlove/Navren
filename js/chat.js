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
let pendingImage = null;
let handoffActive = false;
let chatSending=false;
function chatBinding(){const s=BankClient.identity();return s?{workspace_id:s.workspace_id,privacy_revision:s.privacy_revision}:{};}
function currentChatHistory(){const s=BankClient.identity();return (Storage.get('qingzhou_chatHistory')||[]).filter(m=>m.workspace_id===s?.workspace_id&&m.privacy_revision===s?.privacy_revision);}

function resetPrivateChat(){
  globalThis.NavrenChatPrivacyVersion=(globalThis.NavrenChatPrivacyVersion||0)+1;
  cancelVoice();
  const container=document.getElementById('chatMessages');
  if(container){container.replaceChildren();addMessage('ai','个人资料已更新，旧对话上下文已清除。');}
}
window.addEventListener('navren:privacy-reset',resetPrivateChat);
window.addEventListener('storage',e=>{if(e.key==='qingzhou_chatHistory'&&e.newValue===null)resetPrivateChat();});

// ── Init ──
document.addEventListener('DOMContentLoaded', async () => {
  const params = new URLSearchParams(window.location.search);
  try{await BankClient.refreshIdentity();}catch(e){
    const welcome=document.getElementById('welcomeMsg');if(welcome)welcome.textContent='账户身份暂时无法核对，请刷新后重试。';return;
  }
  // 旧URL只作预填。图谱对象必须由服务器重新核对，不能信任URL里的个人JSON。
  window.NavrenGraphContext=null;
  if(['event','article','weekly'].includes(params.get('market_kind'))&&params.get('market_id')){
    window.NavrenMarketContext={kind:params.get('market_kind'),id:params.get('market_id')};
  }

  // 优先使用 localStorage 记住用户最后选择
  // URL 参数优先（用户显式选择），localStorage 兜底
  currentMode = getInterfaceMode();
  Storage.set('qingzhou_mode', currentMode);

  document.body.className = 'mode-' + currentMode;
  // mode badge removed from chat header

  // 智能欢迎语
  personalizeWelcome();

  // 应用字体：优先用户保存的，否则按模式默认
  const savedFontSize = getModeFont(currentMode);
  const defaultFont = savedFontSize || MODE_DEFAULT_FONT[currentMode] || 'medium';
  applyFontSize(defaultFont);

  renderPresets();
  setupVoice();
  checkPrivacyGuide();
  checkVoiceGuide();
  const welcomeText = document.getElementById('welcomeMsg');
  if (welcomeText) attachMessageSpeech(welcomeText.closest('.message'), welcomeText.textContent || '');

  // 恢复聊天记录（如果存在）
  const savedHistory = currentChatHistory();
  if (savedHistory && savedHistory.length > 0) {
    const msgContainer = document.getElementById('chatMessages');
    msgContainer.innerHTML = ''; // 清空初始欢迎消息
    let historyMutated = false;
    savedHistory.forEach(msg => {
      // 兼容旧数据：'assistant' → 'ai'
      const role = msg.role === 'assistant' ? 'ai' : msg.role;

      // AI 消息优先使用保存的渲染后 HTML（保留产品高亮/追问/按钮等）
      if ((role === 'ai' || role === 'assistant') && msg.htmlContent) {
        const el = addMessage(role, '', msg.isFallback || false);
        const restoredHtml = msg.showAgentChain
          ? msg.htmlContent
          : stripAgentChainPanels(msg.htmlContent);
        if (restoredHtml !== msg.htmlContent) {
          msg.htmlContent = restoredHtml;
          historyMutated = true;
        }
        el.querySelector('.bubble').innerHTML = restoredHtml;
        el.querySelectorAll('.calendar-widget').forEach(n=>n.remove());
        if(msg.widgets?.length&&window.CalendarUI)CalendarUI.response(el,msg.widgets);
        // 重新绑定事件（收藏按钮、追问按钮、UI 动作按钮）
        rebindBubbleEvents(el, msg);
        el.dataset.bankResponseId=msg.bank_response_id||'';
        el.dataset.speechReceipt=msg.speech_receipt||'';
        attachMessageSpeech(el, msg.content || '');
      } else if (role === 'user') {
        const el=addMessage(role, escapeHtml(msg.content), msg.isFallback || false);
        if(msg.recording_id)VoiceMessages.restore(el,msg);
      } else {
        // 旧文字使用当前安全排版器；不恢复旧业务动作。
        const escaped = renderReplyText(msg.content);
        // 旧数据无 htmlContent + 无 highlight_names，跳过产品名高亮
        const el=addMessage(role, escaped, msg.isFallback || false);
        el.dataset.speechReceipt=msg.speech_receipt||'';
      }
    });
    if (historyMutated) {
      Storage.set('qingzhou_chatHistory', savedHistory);
    }
    scrollToBottom();
  }

  const input = document.getElementById('userInput');
  input.addEventListener('input', () => {
    document.getElementById('sendBtn').disabled = !input.value.trim();
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  });
  input.focus();

  const queuedQuestion=params.get('q') || (params.get('action')==='reassess'?'了解我的风险偏好与当前业务风险配置':params.get('action')==='handoff'?'我想联系人工顾问':'');
  if(queuedQuestion){input.value=queuedQuestion;input.dispatchEvent(new Event('input'));}
  // 清理旧私密查询参数，不赋予任何自动执行语义。
  const clean=new URL(location.href);for(const key of ['q','action','reason','graph_context'])clean.searchParams.delete(key);
  history.replaceState(history.state,'',clean.pathname+clean.search+clean.hash);
  if(params.get('context'))await receivePageQuestion(params.get('context'));

  // 回访变化摘要
  showReengagementSummary();

  // 场景引导提示
  showContextualTip('chat');
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

function setupVoice() {
  const btn = document.getElementById('voiceBtn');
  btn.dataset.idleContent = btn.innerHTML;
  btn.classList.remove('hidden');
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
}

function cancelVoice() {
  voiceConversation = false;
  clearTimeout(voiceResumeTimer);
  voiceRequest++;
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
  voiceConversation = true;
  const request = ++voiceRequest;
  const input = document.getElementById('userInput');
  const before = input.value;
  let voiceMessage=null,recording=null;
  try {
    const text = await NavrenSpeech.startASR({ finishOnSentence: false,
      onState: voiceButton,
      onRecording: async clip=>{
        if(request!==voiceRequest)return;
        recording=await VoiceMessages.save(clip);
        if(request!==voiceRequest)return;
        voiceMessage=addMessage('user','');VoiceMessages.mount(voiceMessage,recording);
        const history=currentChatHistory();
        history.push({role:'user',content:'',recording_id:recording.id,...chatBinding(),timestamp:new Date().toISOString()});
        Storage.set('qingzhou_chatHistory',history);scrollToBottom();
      },
      onTranscript: text => {
        if (request !== voiceRequest) return;
        input.value = text;
        input.dispatchEvent(new Event('input'));
      }
    });
    if (request !== voiceRequest) return;
    voiceButton();
    if(voiceMessage)VoiceMessages.update(voiceMessage,text);
    if(recording){const history=Storage.get('qingzhou_chatHistory')||[];const entry=history.find(m=>m.recording_id===recording.id);if(entry)entry.content=text;Storage.set('qingzhou_chatHistory',history);}
    if (text.trim()) { input.value = text; sendMessage(recording?{recording_id:recording.id}:null); }
    else { voiceConversation = false; showToast('没有识别到文字，已保留音频供你回放。'); }
  } catch (error) {
    if (request !== voiceRequest) return;
    voiceButton();
    voiceConversation = false;
    if (error.name === 'AbortError') { input.value = before; input.dispatchEvent(new Event('input')); }
    else showToast(error.message);
  }
}

function stopListening() { NavrenSpeech.finishRecording(); }

// ── Privacy Guide ──
function checkPrivacyGuide() {
  renderPrivacyGuide();
  if (!Storage.get('qingzhou_privacyRead')) {
    document.getElementById('privacyGuide').classList.remove('hidden');
  }
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
    'When you use voice features, recordings and text to be read aloud are sent to Alibaba Cloud Model Studio for processing. Voice messages are stored only in your current browser for playback.',
    'Your data will not be used for other purposes without your confirmation.',
    'Manage conversation permissions in Profile. Change demo risk and funding settings in Accounts.'
  ] : [
    '为提供更贴合你的银行与生活财务服务，Navren会在对话中了解您的投资偏好和资产情况，并建立个人档案。',
    '对话与模拟账户记录保存在本机；回答问题所需的账户摘要会发送至 DeepSeek 和 GLM',
    '使用语音时，录音和待朗读文字发送至阿里云百炼处理；语音消息仅保存在当前浏览器，供你回放',
    '未经您确认，不会用于其他用途',
    '聊天授权在「我的」中管理；模拟风险与资金设置请在「账户」中修改'
  ];
  guide.lang = english ? 'en' : 'zh-CN';
  guide.querySelector('#privacyTitle').textContent = titles[currentMode] || titles.classic;
  guide.querySelectorAll('p,li').forEach((node,index) => { node.textContent = copy[index]; });
  guide.querySelector('button').textContent = english ? 'Got it' : '我知道了';
}
window.addEventListener('navren:language', renderPrivacyGuide);

function acceptPrivacy() {
  Storage.set('qingzhou_privacyRead', true);
  document.getElementById('privacyGuide').classList.add('hidden');
}

// ── Voice Guide ──
function checkVoiceGuide() {
  if (Storage.get('qingzhou_voiceGuideRead')) return;
  // 关怀版首次默认开启语音播报
  if (currentMode === 'senior') {
    Storage.set('qingzhou_voiceEnabled', true);
  }
  Storage.set('qingzhou_voiceGuideRead', true);

  if (currentMode === 'senior') {
    setTimeout(() => showToast('点击麦克风说出问题，Navren 会显示听到的内容并为您回答。'), 1000);
  }
}

// ── Image Upload ──
function triggerUpload() {
  document.getElementById('imageInput').click();
}

function handleImageUpload(event) {
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => {
    pendingImage = e.target.result;
    document.getElementById('previewImg').src = pendingImage;
    document.getElementById('imagePreview').classList.remove('hidden');
  };
  reader.readAsDataURL(file);
}

function removeImage() {
  pendingImage = null;
  document.getElementById('imagePreview').classList.add('hidden');
  document.getElementById('imageInput').value = '';
}

// ── Send Message ──
async function sendMessage(voiceEntry = null) {
  const input = document.getElementById('userInput');
  const text = input.value.trim();
  if (!text||chatSending) return;
  chatSending=true;
  clearFollowUpQuestions();
  const binding=chatBinding();
  const turnGuard=BankClient.guard?.();
  const retryReference=window.NavrenChatReference?JSON.parse(JSON.stringify(window.NavrenChatReference)):null;
  const privacyTurn=globalThis.NavrenChatPrivacyVersion||0;
  const turnCurrent=()=>privacyTurn===(globalThis.NavrenChatPrivacyVersion||0)&&
    binding.workspace_id===BankClient.identity()?.workspace_id&&binding.privacy_revision===BankClient.identity()?.privacy_revision&&
    (!turnGuard||turnGuard.valid(binding));
  clearTimeout(voiceResumeTimer);
  voiceRequest++;
  const voiceTurn = voiceRequest;
  NavrenSpeech.stop();
  voiceButton();

  const userContent = pendingImage
    ? `<img src="${pendingImage}" style="max-width:200px;border-radius:8px;margin-bottom:6px;display:block;"><div>${escapeHtml(text)}</div>`
    : escapeHtml(text);
  const imageToSend = pendingImage;
  if(!voiceEntry?.recording_id)addMessage('user', userContent);
  input.value = '';
  document.getElementById('sendBtn').disabled = true;
  removeImage();

  // 保存用户消息到历史（在所有 early return 之前）
  if(!voiceEntry?.recording_id)Storage.set('qingzhou_chatHistory', [
    ...currentChatHistory(),
    { role: 'user', content: text, ...binding, timestamp: new Date().toISOString() }
  ]);

  // 所有自然语言统一交给后端Agent理解；前端不按关键词弹问卷或模板拦截。
  const sentiment = null;

  const loadingMsg = addLoadingDots();
  const taskRequest=window.NavrenTaskChat?.begin();
  scrollToBottom();

  let result;
  try {
    if(window.NavrenChatReference&&!window.NavrenPageContextId){
      const reference=JSON.parse(JSON.stringify(window.NavrenChatReference));delete reference.execution_authority;
      const ticket=await BankClient.post('/page-contexts',{question:text,reference,return_view:window.NavrenChatReturnView||{page:'chat',view:'home'}});
      if(!turnCurrent()){loadingMsg.remove();chatSending=false;taskRequest?.component.destroy();return;}
      window.NavrenPageContextId=ticket.context_id;
    }
    if(!turnCurrent()){loadingMsg.remove();chatSending=false;taskRequest?.component.destroy();return;}
    result = await Api.sendMessage(text, currentMode, imageToSend, sentiment, event => renderRuntimeEvent(loadingMsg,event));
  } catch (e) {
    console.error('sendMessage error:', e);
    result = { reply: '抱歉，系统遇到了一个小问题，请稍后再试。\n\n⚠️ 理财非存款，产品有风险，投资须谨慎。', isFallback: true };
  }

  loadingMsg.remove();
  chatSending=false;
  if(!turnCurrent()){taskRequest?.component.destroy();return;}
  result.reply=NavrenPresentation.text(result.reply);
  let replyHtml = renderReplyText(result.reply);

  // 金融产品名蓝色加粗高亮
  const highlightNames = result.highlight_names && result.highlight_names.length > 0
    ? result.highlight_names
    : (result.products || []).map(p => typeof p === 'string' ? p : p.name).filter(Boolean);
  replyHtml = highlightProductNames(replyHtml, highlightNames);

  const msgEl = addMessage('ai', replyHtml, result.isFallback);
  msgEl.dataset.bankResponseId=result.bank_response_id||'';
  msgEl.dataset.speechReceipt=result.speech_receipt||'';
  const unifiedTask=window.NavrenTaskChat?.render(msgEl,result,taskRequest)||false;
  if(unifiedTask)attachMessageSpeech(msgEl,result.reply);
  if(result.isFallback){
    input.value=text;input.dispatchEvent(new Event('input'));
    const retry=document.createElement('button');retry.type='button';retry.className='chat-retry';retry.textContent='重试这个问题';
    retry.onclick=()=>{window.NavrenChatReference=retryReference;window.NavrenPageContextId=null;input.value=text;sendMessage();};appendMessageRetry(msgEl, retry);
  }else{window.NavrenChatReference=null;syncChatReference();}
  if (!unifiedTask&&typeof BankUI !== 'undefined') BankUI.renderResponse(msgEl, result);
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
  if (!result.task_result_rejected&&(voiceEnabled || voiceConversation) && voiceTurn === voiceRequest) {
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
  if (result.follow_up_questions && result.follow_up_questions.length > 0) {
    renderFollowUpQuestions(msgEl, result.follow_up_questions);
  } else if (!result.bank) {
    addFollowUpQuestions(msgEl, result.reply);
  }

  // 风险重评提醒：超6个月或人生阶段变化
  checkRiskReassessmentNudge(msgEl);

  // 重新读取 history（并发调用时避免覆盖其他 sendMessage 写入的数据）
  finishRuntimeProgress(loadingMsg, msgEl);
  const latestHistory = currentChatHistory();
  // 保存完整渲染后的 HTML，刷新恢复时直接使用，避免排版变化
  const renderedBubble = msgEl.querySelector('.bubble');
  const savedBubble = renderedBubble?.cloneNode(true);
  savedBubble?.querySelectorAll('.runtime-panel,.message-audio-control').forEach(button => button.remove());
  savedBubble?.querySelectorAll('.calendar-widget,.message-suggestions').forEach(widget => widget.remove());
  if(unifiedTask&&savedBubble){savedBubble.replaceChildren();const body=document.createElement('div');body.className='message-body message-prose';body.innerHTML=renderReplyText(result.reply);savedBubble.append(body);const p=document.createElement('p');p.className='task-history-note';p.textContent='结构化任务依据与动作需重新查询核验。';savedBubble.append(p);}
  const localSocialReply = result.answer_contract?.kind === 'social_reply' &&
    result.answer_contract?.status === 'not_required' && result.agent_review?.status === 'not_required';
  latestHistory.push({
    role: 'ai',
    presentation_ref:localSocialReply ? null : (window.NavrenPresentationBridge?.descriptor?.(result)||null),
    ...binding,
    content: result.reply,
    highlight_names: highlightNames.filter(name=>typeof name==='string'&&name.trim()),
    htmlContent: savedBubble ? savedBubble.innerHTML : null,
    showAgentChain: shouldShowAgentChain(result),
    intent: result.intent || '',
    bank_response_id: result.bank_response_id || '',
    speech_receipt: result.speech_receipt || '',
    widgets:result.widgets||[],
    timestamp: new Date().toISOString(),
    isFallback: result.isFallback
  });
  Storage.set('qingzhou_chatHistory', latestHistory);

  scrollToBottom();
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

function runtimeActivitySummary(kind, summary) {
  if (!summary || typeof summary !== 'object' || Array.isArray(summary)) return [];
  const parts = [];
  for (const key of RUNTIME_ACTIVITY_FIELDS[kind] || []) {
    const value = summary[key];
    if (key === 'record_count') {
      if (Number.isSafeInteger(value) && value >= 0 && value <= 10000000) parts.push(value + ' 条记录');
    } else if (key === 'round') {
      if (Number.isSafeInteger(value) && value > 0 && value <= 1000) parts.push('第 ' + value + ' 轮');
    } else if (key === 'period') {
      if (typeof value === 'string' && value.length <= 64 && /^\d{4}-\d{2}(?:-\d{2})?(?:\s*(?:至|—|~|\/|\.\.)\s*\d{4}-\d{2}(?:-\d{2})?)?$/.test(value)) parts.push(value);
    } else if (key === 'verdict') {
      const verdicts = {approve: '审核通过', revise: '需要修订', reject: '审核未通过', insufficient_evidence: '审核证据不足'};
      if (Object.hasOwn(verdicts, value)) parts.push(verdicts[value]);
    } else {
      const text = key === 'source' ? runtimeSafeText(value, 64) : runtimeSafeName(value);
      if (text) parts.push(({source: '来源：', skill: '技能：', provider: '提供方：', model: '模型：', failure_type: '失败类型：'}[key] || '') + text);
    }
  }
  return parts;
}

function runtimeActivityIdentity(activity) {
  const summary = activity.summary;
  let identity = activity.name;
  if (activity.kind === 'skill') identity = runtimeSafeName(summary?.skill) || identity;
  else if (['model', 'review'].includes(activity.kind)) {
    const model = runtimeSafeName(summary?.model), provider = runtimeSafeName(summary?.provider);
    identity = model ? (provider && provider !== model ? provider + ' / ' + model : model) : identity;
  }
  const count = activity.kind === 'data' && Number.isSafeInteger(summary?.record_count) && summary.record_count >= 0 && summary.record_count <= 10000000 ? ' · ' + summary.record_count + ' 条记录' : '';
  return (activity.label.includes(identity) ? activity.label : activity.label + ' · ' + identity) + count;
}

function runtimeActivityPanel(message) {
  let state = runtimeActivityStates.get(message);
  if (state) return state;
  const bubble = message.querySelector('.bubble') || message;
  bubble.replaceChildren();
  const panel = document.createElement('details'); panel.className = 'runtime-panel runtime-activity-panel';
  const summary = document.createElement('summary'); summary.className = 'runtime-activity-current';
  const latest = document.createElement('span'); latest.className = 'runtime-activity-latest';
  latest.setAttribute('role', 'status'); latest.setAttribute('aria-live', 'polite'); latest.setAttribute('aria-atomic', 'true');
  const count = document.createElement('span'); count.className = 'runtime-activity-count';
  summary.append(latest, count);
  const list = document.createElement('ol'); list.className = 'runtime-steps runtime-activity-list';
  panel.append(summary, list); bubble.append(panel);
  state = {panel, latest, count, list, records: new Map(), attempts: new Map(), steps: new Map(), sequence: 0, requestId: '', typedSeen: false, finished: false};
  runtimeActivityStates.set(message, state);
  return state;
}

function updateRuntimeActivity(state, activity) {
  let record = state.records.get(activity.callId);
  if (record?.typed && !activity.typed) return;
  if (!record) {
    const row = document.createElement('li'); row.className = 'runtime-step';
    const title = document.createElement('span'); title.className = 'runtime-activity-label';
    const name = document.createElement('code'); name.className = 'runtime-activity-name';
    const status = document.createElement('span'); status.className = 'runtime-activity-state';
    const details = document.createElement('span'); details.className = 'runtime-activity-data';
    row.append(title, name, status, details); state.list.append(row);
    const key = activity.kind + ':' + activity.name;
    const attempt = (state.attempts.get(key) || 0) + 1; state.attempts.set(key, attempt);
    record = {row, title, name, status, details, attempt}; state.records.set(activity.callId, record);
  }
  // 兼容事件重复到达不能把已经结束的同一次调用重新变成“进行中”。
  if (record.activity?.status !== 'started' && record.activity && activity.status === 'started') return;
  record.typed = record.typed || activity.typed;
  if (Number.isSafeInteger(activity.attempt) && activity.attempt > 0 && activity.attempt <= 1000) record.attempt = activity.attempt;
  record.activity = activity;
  state.latestRecord = record;
  const statuses = {started: '进行中', completed: '已完成', failed: '失败'};
  record.row.className = 'runtime-step ' + ({started: 'running', completed: 'done', failed: 'rejected'}[activity.status]);
  record.title.textContent = activity.label;
  record.name.textContent = activity.name; record.name.hidden = activity.label.includes(activity.name);
  record.status.textContent = statuses[activity.status];
  const details = runtimeActivitySummary(activity.kind, activity.summary);
  if (record.attempt > 1) details.unshift('第 ' + record.attempt + ' 次调用');
  record.details.textContent = details.join(' · '); record.details.hidden = !details.length;
  state.latest.textContent = runtimeActivityIdentity(activity) + (record.attempt > 1 ? ' · 第 ' + record.attempt + ' 次调用' : '') + ' · ' + statuses[activity.status];
  state.count.textContent = state.records.size + ' 项调用';
  state.panel.dataset.status = activity.status;
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
    activity = {callId, kind: 'model', name, label: '调用模型 ' + name, status: 'started', typed: false};
  } else if (event.type === 'STEP_FINISHED') {
    const state = runtimeActivityStates.get(message), record = state?.records.get(state.steps.get(String(event.stepName || '')));
    if (!record || record.typed) return;
    activity = {...record.activity, status: 'completed'};
  } else if (event.type === 'RUN_FINISHED' || event.type === 'RUN_ERROR') {
    const state = runtimeActivityStates.get(message);
    if (state) { state.finished = true; state.panel.open = false; }
    return;
  } else return;
  const state = runtimeActivityPanel(message);
  if (state.finished) return;
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

function finishRuntimeProgress(loadingMessage, replyMessage) {
  const state = runtimeActivityStates.get(loadingMessage);
  if (!state || !state.records.size) return;
  state.finished = true; state.panel.open = false;
  const latest = state.latestRecord;
  if (latest?.activity.status === 'started') state.latest.textContent = runtimeActivityIdentity(latest.activity) + ' · 未收到结束状态';
  replyMessage.querySelector('.bubble')?.append(state.panel);
  runtimeActivityStates.delete(loadingMessage);
  runtimeActivityStates.set(replyMessage, state);
}

// ── Add Messages ──
function addMessage(role, content, isFallback = false) {
  clearFollowUpQuestions();
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = 'message ' + role;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  if (role === 'ai' || role === 'assistant') {
    avatar.innerHTML = '<img class="brand-icon" src="assets/brand/navren-icon.png" width="1254" height="1254" alt="">';
  } else {
    const userInfo = Storage.get('qingzhou_userInfo');
    if (userInfo?.avatar) {
      avatar.innerHTML = `<img src="${userInfo.avatar}" style="width:100%;height:100%;border-radius:10px;object-fit:cover;">`;
    } else {
      avatar.textContent = '👤';
    }
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
  if (role === 'ai' || role === 'assistant') attachMessageSpeech(div, contentDiv.innerText || contentDiv.textContent || '');
  return div;
}

function renderReplyText(text) {
  return window.NavrenMarkdown?.render(text) ?? escapeHtml(text).replace(/\n/g, '<br>');
}

function appendMessageRetry(message, retry) {
  const bubble = message.querySelector('.bubble');
  if (!bubble) return;
  let tools = bubble.querySelector(':scope > .message-tools');
  if (!tools) { tools = document.createElement('div'); tools.className = 'message-tools'; }
  tools.classList.add('has-retry');
  tools.prepend(retry);
  bubble.querySelectorAll(':scope > .message-audio-control').forEach(button => tools.append(button));
  bubble.append(tools);
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
    privacyTurn === (globalThis.NavrenChatPrivacyVersion || 0) &&
    (!guard || guard.valid(binding)) && (!msgEl.__navrenTaskCurrent || msgEl.__navrenTaskCurrent());
  const label = document.createElement('span');
  label.className = 'follow-up-label'; label.textContent = getFollowUpLabel();
  div.append(label); msgEl.append(div);
  return div;
}
window.addEventListener('navren:account-change', clearFollowUpQuestions);
window.addEventListener('navren:privacy-reset', clearFollowUpQuestions);

let activeSpeechControl = null;
let speechControlRun = 0;

function attachMessageSpeech(message, initialText) {
  document.querySelectorAll('.message.ai .message-audio-control,.message.assistant .message-audio-control').forEach(button=>{
    if(button.closest('.message')===message)return;
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
    if (activeSpeechControl === button && NavrenSpeech.activeKind === 'tts') {
      button.dataset.muted = 'true';
      NavrenSpeech.stop();
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
  bubble.appendChild(button);
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
    idle: '<path d="M11 5 6 9H3v6h3l5 4V5Z"/><path d="M15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/>',
    muted: '<path d="M11 5 6 9H3v6h3l5 4V5Z"/><path d="m3 3 18 18"/>',
    preparing: '<circle cx="5" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.5" fill="currentColor" stroke="none"/>'
  };
  button.dataset.state = muted ? 'muted' : state;
  button.setAttribute('aria-pressed', state === 'idle' ? 'false' : 'true');
  button.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (muted ? icons.muted : state === 'speaking' ? icons.idle : icons[state] || icons.preparing) + '</svg>';
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
  const preset = getVoicePreset();
  const rate = currentMode === 'senior' ? 0.85 : preset.rate;
  return NavrenSpeech.speak(text, { voice: preset.id, rate, speechReceipt: message.dataset.speechReceipt || '',
    onState: state => { if (run === speechControlRun) setSpeechControl(button, state === 'speaking' ? 'speaking' : 'preparing'); }
  }).then(() => { if (onEnd) onEnd(); }).catch(error => {
    if (error.name !== 'AbortError') {
      voiceConversation = false;
      showToast('语音播报未完成：' + error.message);
    }
  }).finally(() => {
    if (run === speechControlRun) {
      setSpeechControl(button, 'idle');
      if (activeSpeechControl === button) activeSpeechControl = null;
    }
  });
}

// “我的”页面关闭语音播报后，已打开的聊天页同步停止并显示划线图标。
window.addEventListener('storage', event => {
  if (event.key !== 'qingzhou_voiceEnabled') return;
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
  button.style.color='';button.style.borderColor='';button.style.background='';
  button.innerHTML='<svg aria-hidden="true" viewBox="0 0 24 24" fill="'+(saved?'currentColor':'none')+'" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 2.8 5.7 6.3.9-4.55 4.45 1.07 6.28L12 17.36l-5.62 2.97 1.07-6.28L2.9 9.6l6.3-.9Z"/></svg>';
  button.title=saved?'取消收藏':'收藏此回复';
  button.setAttribute('aria-label',button.title);button.setAttribute('aria-pressed',String(saved));
  button.closest('.bubble')?.classList.add('has-message-actions');
}
function addBookmarkButton(msgEl, text, result) {
  const bubble=msgEl.querySelector('.bubble');
  const btn=document.createElement('button');btn.type='button';btn.className='tts-btn message-bookmark-control';
  bookmarkIcon(btn,(Storage.get('qingzhou_bookmarks')||[]).some(b=>b.text===text.slice(0,200)));
  btn.onclick=e=>{
    e.stopPropagation();const items=Storage.get('qingzhou_bookmarks')||[];
    const index=items.findIndex(b=>b.text===text.slice(0,200));
    if(index>=0){items.splice(index,1);bookmarkIcon(btn,false);showToast('已取消收藏');}
    else{items.push({text:text.slice(0,200),...chatBinding(),timestamp:new Date().toISOString()});bookmarkIcon(btn,true);showToast('已收藏');}
    Storage.set('qingzhou_bookmarks',items.slice(-20));
    if(index<0 && !result?.bank && (result?.products?.length || result?.allocation?.allocation?.length))saveRecommendationSnapshot(result,text);
  };
  bubble.classList.add('has-message-actions');bubble.appendChild(btn);
}

/** 资产配置方案专用"收藏方案"按钮 — 居中，放在方案正文下方 */
function addSaveAllocationButton(msgEl, result) {
  const bubble = msgEl.querySelector('.bubble');
  const btn = document.createElement('button');
  btn.textContent = '收藏方案';
  btn.style.cssText = 'display:block;margin:14px auto 0;padding:10px 0;width:180px;text-align:center;'
    + 'border:1.5px solid var(--gold);border-radius:var(--r-sm);background:var(--surface-card);'
    + 'color:var(--gold-dark);font-size:var(--font-size-sm);font-weight:700;cursor:pointer;'
    + 'transition:all var(--transition);line-height:1.5';

  btn.onmouseover = () => { btn.style.background = 'var(--primary-deep)'; btn.style.color = 'var(--on-primary)'; };
  btn.onmouseout = () => {
    btn.style.background = btn.dataset.saved === '1' ? 'var(--gold-light)' : 'var(--surface-card)';
    btn.style.color = btn.dataset.saved === '1' ? 'var(--gold-dark)' : 'var(--gold-dark)';
  };

  btn.onclick = (e) => {
    e.stopPropagation();
    saveRecommendationSnapshot(result, '用户主动收藏资产配置方案');
    btn.textContent = '已收藏至推荐页';
    btn.dataset.saved = '1';
    btn.style.background = 'var(--gold-light)';
    btn.style.borderColor = 'var(--gold)';
    btn.style.cursor = 'default';
    btn.title = '方案已保存，可在「推荐」页查看';
    if (typeof showToast === 'function') showToast('已收藏，可在「推荐」页继续查看和追问');
  };

  bubble.appendChild(btn);
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
    sendMessage();
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
function rebindBubbleEvents(msgEl, historyEntry) {
  if (typeof BankUI !== 'undefined') BankUI.rebind(msgEl);
  const bubble = msgEl.querySelector('.bubble');
  if (!bubble) return;
  // 历史中的旧追问及业务按钮不重新激活。
  bubble.querySelectorAll('.message-suggestions').forEach(node=>node.remove());
  bubble.querySelectorAll('div > span').forEach(label=>{
    if(!/^(下一步|继续看|您可以继续|您可能想问|Next steps|Keep exploring|You can continue)$/.test(label.textContent.trim()))return;
    const region=label.parentElement;
    if(region.firstElementChild===label&&region.querySelector('[style*="cursor"],.follow-up-chip'))region.remove();
  });
  let body=bubble.querySelector('.message-body') || [...bubble.children].find(node=>node.tagName==='DIV'&&!node.classList.contains('sender'));
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
    const text=historyEntry.content||'';
    bookmarkIcon(btn,(Storage.get('qingzhou_bookmarks')||[]).some(b=>b.text===text.slice(0,200)));
    btn.onclick=e=>{
      e.stopPropagation();const items=Storage.get('qingzhou_bookmarks')||[];
      const idx=items.findIndex(b=>b.text===text.slice(0,200));
      if(idx>=0)items.splice(idx,1);else items.push({text:text.slice(0,200),...chatBinding(),timestamp:new Date().toISOString()});
      Storage.set('qingzhou_bookmarks',items.slice(-20));bookmarkIcon(btn,idx<0);
      showToast(idx<0?'已收藏':'已取消收藏');
    };
  });

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
function renderUIActions(msgEl, actions) {
  const bubble = msgEl.querySelector('.bubble');
  const div = document.createElement('div');
  div.style.cssText = 'margin-top:12px;padding:10px 0 0;border-top:1px solid var(--border);display:flex;flex-wrap:wrap;gap:8px';

  actions.forEach(action => {
    const btn = document.createElement('button');
    btn.textContent = action.label;
    btn.style.cssText = 'padding:8px 16px;border-radius:8px;border:1.5px solid var(--ink);font-size:var(--font-size-sm);font-weight:600;cursor:pointer;background:var(--surface-card);color:var(--ink);transition:all var(--transition);line-height:1.4';
    btn.onmouseover = () => { btn.style.background = 'var(--primary-deep)'; btn.style.color = 'var(--on-primary)'; };
    btn.onmouseout = () => { btn.style.background = 'var(--surface-card)'; btn.style.color = 'var(--ink)'; };
    btn.onclick = (e) => {
      e.stopPropagation();
      switch (action.type) {
        case 'start_risk_quiz': startQuestionnaire(); break;
        case 'show_safe_plan':
          document.getElementById('userInput').value = '查看当前风险等级内的稳健方案'; sendMessage(); break;
        case 'explain_risk_gap':
          document.getElementById('userInput').value = '了解达到该收益目标需要承担什么风险'; sendMessage(); break;
        case 'handoff': triggerHandoff('用户主动请求转人工（风险拦截触发）'); break;
      }
    };
    div.appendChild(btn);
  });
  bubble.appendChild(div);
}

/** 渲染后端返回的追问按钮（优先于前端兜底） */
function renderFollowUpQuestions(msgEl, questions) {
  const div = createFollowUpRegion(msgEl);
  if (!div) return;

  questions.slice(0, 4).forEach(q => {
    // 动作型 follow-up 加图标
    const actionIcons = { 'start_risk_quiz': '🔄 ', 'handoff': '📞 ' };
    const icon = actionIcons[q.type] || '';
    const chip = document.createElement('button');
    chip.type='button';chip.className='follow-up-chip';
    chip.textContent = icon + q.label;
    chip.onclick = (e) => {
      e.stopPropagation();
      if(!div.__navrenIsCurrent())return;
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
    addMessage('ai',`<strong>本地顾问交接摘要已准备</strong><p>摘要仅供本机顾问演示视角回看，未连接真实人工服务。业务配置与问卷参考分别保留。</p><div>${syncedItems.map(i=>'<p>'+escapeHtml(i)+'</p>').join('')}</div><button class="retry-btn" onclick="goTo('copilot.html')">查看顾问摘要</button>`);
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

function syncChatReference(){
  let bar=document.getElementById('navrenChatContext');
  if(!bar){bar=document.createElement('div');bar.id='navrenChatContext';bar.className='chat-context-bar';document.querySelector('.chat-input-area')?.prepend(bar);}
  bar.replaceChildren();bar.hidden=!window.NavrenChatReference&&!window.NavrenChatReturnView;
  if(window.NavrenChatReference){const tag=document.createElement('span');tag.className='chat-reference-chip';tag.textContent='关联：'+(NavrenChatReference.label||NavrenChatReference.id);const remove=document.createElement('button');remove.type='button';remove.textContent='×';remove.setAttribute('aria-label','移除提问引用');remove.onclick=()=>{window.NavrenChatReference=null;window.NavrenPageContextId=null;syncChatReference();};tag.append(remove);bar.append(tag);}
  if(window.NavrenChatReturnView){const back=document.createElement('a');back.href=NavrenViews.url(NavrenChatReturnView);back.textContent='返回原页面';back.onclick=()=>sessionStorage.setItem('navren_return_view',JSON.stringify({...NavrenChatReturnView,...chatBinding()}));bar.append(back);}
}
async function receivePageQuestion(id){
  const input=document.getElementById('userInput');
  const guard=BankClient.guard(),privacyTurn=globalThis.NavrenChatPrivacyVersion||0;
  try{
    const data=await BankClient.post('/page-contexts/'+encodeURIComponent(id)+'/claim',{});
    if(!guard.valid(data)||privacyTurn!==(globalThis.NavrenChatPrivacyVersion||0))return;
    const clean=new URL(location.href);clean.searchParams.delete('context');history.replaceState(history.state,'',clean.pathname+clean.search+clean.hash);
    if(data.open_once===false)return;
    window.NavrenChatReference=data.reference;window.NavrenChatReturnView=data.return_view;
    window.NavrenPageContextId=null;
    input.value=data.question||'';input.dispatchEvent(new Event('input'));syncChatReference();
    input.focus({preventScroll:true});
  }catch(e){if(guard.valid()&&privacyTurn===(globalThis.NavrenChatPrivacyVersion||0))addMessage('ai',escapeHtml(e.message));}
}
window.addEventListener('navren:privacy-reset',()=>{window.NavrenChatReference=null;window.NavrenChatReturnView=null;window.NavrenPageContextId=null;window.NavrenMarketContext=null;window.NavrenGraphContext=null;syncChatReference();});
