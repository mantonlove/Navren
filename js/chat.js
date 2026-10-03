/**
 * Navren — chat.html 页面逻辑
 */

const PRESET_QUESTIONS = {
  classic: ['分析9月账单和异常消费', '我有多少可投资资金？', '对比模拟活钱和30天产品', '关闭视频会员的银行代扣'],
  senior: ['帮我看9月账单', '应急的钱留好后，还能投多少？', '对比活钱和30天产品', '查看银行代扣'],
  youth: ['9月钱花在哪了？', '我有多少可投资资金？', '活钱和30天产品怎么选？', '查看银行代扣']
};

let currentMode = 'classic';
let isListening = false;
let pendingImage = null;
let handoffActive = false;

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
  const params = new URLSearchParams(window.location.search);

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

  // 恢复聊天记录（如果存在）
  const savedHistory = Storage.get('qingzhou_chatHistory');
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
        // 重新绑定事件（收藏按钮、追问按钮、UI 动作按钮）
        rebindBubbleEvents(el, msg);
      } else if (role === 'user') {
        addMessage(role, escapeHtml(msg.content), msg.isFallback || false);
      } else {
        // AI 消息无 htmlContent（旧数据）→ 用纯文本渲染并做高亮
        const escaped = escapeHtml(msg.content).replace(/\n/g, '<br>');
        // 旧数据无 htmlContent + 无 highlight_names，跳过产品名高亮
        addMessage(role, escaped, msg.isFallback || false);
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

  // 如果是从 mine.html 来的重新评估请求
  if (params.get('action') === 'reassess') {
    setTimeout(() => startQuestionnaire(), 800);
  }
  // 如果是从账户页来的人工客服请求
  if (params.get('action') === 'handoff') {
    setTimeout(() => { if (typeof triggerHandoff === 'function') triggerHandoff('用户从账户页发起人工客服请求'); }, 800);
  }

  const queuedQuestion = params.get('q');
  if (queuedQuestion) {
    input.value = queuedQuestion;
    document.getElementById('sendBtn').disabled = !queuedQuestion.trim();
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    setTimeout(() => sendMessage(), 500);
  }

  // 回访变化摘要
  showReengagementSummary();

  // 场景引导提示
  showContextualTip('chat');
});

// ── Presets ──
function renderPresets() {
  const bar = document.getElementById('presetBar');
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
function setupVoice() {
  const btn = document.getElementById('voiceBtn');
  btn.classList.remove('hidden');
}

function toggleVoice() {
  if (isListening) { stopListening(); return; }

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) { showToast('您的浏览器不支持语音识别'); return; }

  const recognition = new SpeechRecognition();
  recognition.lang = 'zh-CN';
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;

  const btn = document.getElementById('voiceBtn');
  btn.classList.add('listening');
  btn.textContent = '🔴';
  isListening = true;

  recognition.start();

  recognition.onresult = (event) => {
    const text = event.results[0][0].transcript;
    document.getElementById('userInput').value = text;
    btn.classList.remove('listening');
    btn.classList.add('success');
    btn.textContent = '🎤';
    isListening = false;
    setTimeout(() => btn.classList.remove('success'), 1000);
    sendMessage();
  };

  recognition.onerror = () => {
    btn.classList.remove('listening');
    btn.textContent = '🎤';
    isListening = false;
    if (currentMode === 'senior') {
      setTimeout(() => showToast('没听清，您再试一次？'), 300);
    }
  };

  recognition.onend = () => {
    if (isListening && currentMode === 'senior') {
      try { recognition.start(); } catch (e) { isListening = false; btn.textContent = '🎤'; }
    }
  };

  setTimeout(() => {
    if (isListening) {
      try { recognition.stop(); } catch (e) {}
      isListening = false;
      btn.classList.remove('listening');
      btn.textContent = '🎤';
    }
  }, 8000);
}

function stopListening() {
  isListening = false;
  const btn = document.getElementById('voiceBtn');
  btn.classList.remove('listening');
  btn.textContent = '🎤';
}

// ── Privacy Guide ──
function checkPrivacyGuide() {
  if (!Storage.get('qingzhou_privacyRead')) {
    document.getElementById('privacyGuide').classList.remove('hidden');
    document.getElementById('privacyTitle').textContent =
      currentMode === 'senior' ? '您的专属理财小档案' :
      currentMode === 'youth' ? '你的数据，你做主' :
      '🔒 隐私与个性化服务说明';
  }
}

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

  if (currentMode === 'senior' && 'speechSynthesis' in window) {
    setTimeout(() => {
      const utterance = new SpeechSynthesisUtterance(
        '您好，我是 Navren，您的专属理财顾问。您可以直接对我说话，我会用语音回复您。试试问我：什么是理财产品？'
      );
      utterance.lang = 'zh-CN';
      utterance.rate = 0.85;
      utterance.onend = () => {
        if (Storage.get('qingzhou_voiceEnabled')) toggleVoice();
      };
      speechSynthesis.speak(utterance);
    }, 1000);
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
async function sendMessage() {
  const input = document.getElementById('userInput');
  const text = input.value.trim();
  if (!text) return;

  if (Router.detectInjection(text)) {
    addMessage('ai', '⚠️ 抱歉，您的输入包含不符合理财顾问服务规范的内容，我无法处理该请求。作为合规的银行理财顾问，我只能基于您的财务状况和风险偏好，提供客观的产品信息和配置建议。如有具体理财需求，欢迎重新描述。');
    input.value = '';
    return;
  }

  const userContent = pendingImage
    ? `<img src="${pendingImage}" style="max-width:200px;border-radius:8px;margin-bottom:6px;display:block;"><div>${escapeHtml(text)}</div>`
    : escapeHtml(text);
  const imageToSend = pendingImage;
  addMessage('user', userContent);
  input.value = '';
  document.getElementById('sendBtn').disabled = true;
  removeImage();

  // 保存用户消息到历史（在所有 early return 之前）
  Storage.set('qingzhou_chatHistory', [
    ...(Storage.get('qingzhou_chatHistory') || []),
    { role: 'user', content: text, timestamp: new Date().toISOString() }
  ]);

  // 核心机制：从用户消息中自动提取画像字段（金额/期限/目标）
  extractProfileFromMessage(text);

  // 重新测评 —— 先显示用户消息+清空输入框，再弹问卷
  if (/重新.*(测|评估)|再.*(测|评估|做题)|风险.*(评估|测评|问卷)|做.*(题|测评|评估)|测.*(风险|评估|问卷)/.test(text)) {
    startQuestionnaire();
    return;
  }

  const route = Router.route(text);
  if (route.action === 'compliance_block') {
    const replyText = '⚠️ 理财非存款，产品有风险，投资须谨慎。我不能对产品收益做出任何保证或承诺。您可以通过我行 APP 查看产品的完整风险说明书和过往业绩后再做判断。如有疑问，欢迎随时咨询。';
    addMessage('ai', replyText);
    Storage.set('qingzhou_chatHistory', [
      ...(Storage.get('qingzhou_chatHistory') || []),
      { role: 'ai', content: replyText, timestamp: new Date().toISOString(), isFallback: true }
    ]);
    return;
  }

  // 情绪检测：焦虑/恐慌 → 先安抚，调整推荐策略偏保守
  const sentiment = route.intent === 'sentiment_anxiety' ? 'anxiety' : null;
  if (sentiment) {
    Storage.addKeyMoment('检测到用户焦虑情绪');
  }

  const loadingMsg = addLoadingDots();
  scrollToBottom();

  let result;
  try {
    result = await Api.sendMessage(text, currentMode, imageToSend, sentiment);
  } catch (e) {
    console.error('sendMessage error:', e);
    result = { reply: '抱歉，系统遇到了一个小问题，请稍后再试。\n\n⚠️ 理财非存款，产品有风险，投资须谨慎。', isFallback: true };
  }

  loadingMsg.remove();

  let replyHtml = escapeHtml(result.reply).replace(/\n/g, '<br>');

  // 金融产品名蓝色加粗高亮
  const highlightNames = result.highlight_names && result.highlight_names.length > 0
    ? result.highlight_names
    : (result.products || []).map(p => typeof p === 'string' ? p : p.name).filter(Boolean);
  replyHtml = highlightProductNames(replyHtml, highlightNames);

  const msgEl = addMessage('ai', replyHtml, result.isFallback);
  if (typeof BankUI !== 'undefined') BankUI.renderResponse(msgEl, result);

  // 推荐历史记录（无 UI 卡片，仅 audit trail）

  // 收藏按钮（小星标）：仅产品推荐场景显示，资产配置方案另有专用收藏按钮
  const isWorthBookmarking = (result.products && result.products.length > 0)
    && result.intent !== 'asset_allocation';
  if (isWorthBookmarking) {
    addBookmarkButton(msgEl, result.reply, result);
  }

  // 语音播报（仅在mine页开启时自动朗读，不显示播放按钮）
  const voiceEnabled = Storage.get('qingzhou_voiceEnabled');
  if (voiceEnabled) {
    speakText(result.reply, () => {
      setTimeout(() => {
        if (Storage.get('qingzhou_voiceEnabled') && !isListening) {
          toggleVoice();
        }
      }, 1500);
    });
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
    const warning_style = has_warning ? 'color:#C53030;font-weight:700' : 'color:var(--ink)';
    analysisDiv.innerHTML = '<div style="font-weight:700;color:var(--ink);margin-bottom:4px">🖼️ 图片分析</div>' +
      '<div style="' + warning_style + '">' + escapeHtml(result_text || '分析完成').replace(/\n/g, '<br>').slice(0, 500) + '</div>';
    msgEl.querySelector('.bubble').appendChild(analysisDiv);
  }

  // 智能体链路可视化面板：只在推荐/配置/对比等需要解释依据的场景展示。
  // 后端仍返回 agent_chain 用于日志与评分，普通概念解释不打扰用户。
  if (shouldShowAgentChain(result)) {
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
  const latestHistory = Storage.get('qingzhou_chatHistory') || [];
  // 保存完整渲染后的 HTML，刷新恢复时直接使用，避免排版变化
  const renderedBubble = msgEl.querySelector('.bubble');
  latestHistory.push({
    role: 'ai',
    content: result.reply,
    htmlContent: renderedBubble ? renderedBubble.innerHTML : null,
    showAgentChain: shouldShowAgentChain(result),
    intent: result.intent || '',
    bank_response_id: result.bank_response_id || '',
    timestamp: new Date().toISOString(),
    isFallback: result.isFallback
  });
  Storage.set('qingzhou_chatHistory', latestHistory);

  scrollToBottom();
}

// ── Add Messages ──
function addMessage(role, content, isFallback = false) {
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = 'message ' + role;

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  if (role === 'ai' || role === 'assistant') {
    avatar.innerHTML = '<img src="assets/logo.png" style="width:100%;height:100%;border-radius:10px;object-fit:cover;">';
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
  contentDiv.innerHTML = content;
  bubble.appendChild(contentDiv);

  div.appendChild(avatar);
  div.appendChild(bubble);
  container.appendChild(div);
  return div;
}

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
  avatar.innerHTML = '<img src="assets/logo.png" style="width:100%;height:100%;border-radius:10px;object-fit:cover;">';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  const dots = document.createElement('div');
  dots.className = 'loading-dots';
  dots.innerHTML = '<span></span><span></span><span></span>';
  bubble.appendChild(dots);
  div.appendChild(avatar);
  div.appendChild(bubble);
  container.appendChild(div);
  return { remove: () => div.remove() };
}

// ── Bookmark ──
function addBookmarkButton(msgEl, text, result) {
  const bubble = msgEl.querySelector('.bubble');
  const btn = document.createElement('button');
  btn.className = 'tts-btn';
  const bookmarks = Storage.get('qingzhou_bookmarks') || [];
  const alreadyBookmarked = bookmarks.some(b => b.text === text.slice(0, 200));

  function updateIcon(bookmarked) {
    btn.innerHTML = bookmarked
      ? '<svg viewBox="0 0 24 24" fill="var(--gold)" stroke="var(--gold)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="12" height="12"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="12" height="12"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>';
  }

  updateIcon(alreadyBookmarked);
  if (alreadyBookmarked) {
    btn.style.color = 'var(--gold)';
    btn.style.borderColor = 'var(--gold)';
  }
  btn.title = alreadyBookmarked ? '取消收藏' : '收藏此回复';

  btn.onclick = (e) => {
    e.stopPropagation();
    const currentBookmarks = Storage.get('qingzhou_bookmarks') || [];
    const idx = currentBookmarks.findIndex(b => b.text === text.slice(0, 200));
    if (idx >= 0) {
      currentBookmarks.splice(idx, 1);
      Storage.set('qingzhou_bookmarks', currentBookmarks);
      updateIcon(false);
      btn.style.color = '';
      btn.style.borderColor = '';
      btn.title = '收藏此回复';
      showToast('已取消收藏');
    } else {
      currentBookmarks.push({ text: text.slice(0, 200), timestamp: new Date().toISOString() });
      Storage.set('qingzhou_bookmarks', currentBookmarks.slice(-20));
      updateIcon(true);
      btn.style.color = 'var(--gold)';
      btn.style.borderColor = 'var(--gold)';
      btn.title = '取消收藏';
      // 收藏时同步推荐方案到推荐页
      if (result && (result.products?.length > 0 || result.allocation?.allocation?.length > 0)) {
        saveRecommendationSnapshot(result, text);
        showToast('已收藏 · 方案已同步到推荐中心');
      } else {
        showToast('已收藏');
      }
    }
  };
  bubble.appendChild(btn);
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

  btn.onmouseover = () => { btn.style.background = 'var(--gold)'; btn.style.color = '#fff'; };
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


let _ttsAudio = null;

function speakText(text, onEnd) {
  // 停止正在播放的音频
  if (_ttsAudio) {
    _ttsAudio.pause();
    _ttsAudio = null;
  }

  const cleanText = text.replace(/⚠️[^]*/g, '').replace(/\n\n/g, '。').replace(/\n/g, '').trim();
  if (!cleanText) { if (onEnd) onEnd(); return; }

  const preset = typeof getVoicePreset === 'function' ? getVoicePreset() : { id: 'female_sweet', rate: 1.3 };
  const voiceId = preset.id || 'gentle-female';

  // 优先使用 server.py TTS（macOS say 命令，音质好）
  fetch('/api/tts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: cleanText, voice: voiceId, rate: preset.rate })
  })
  .then(res => {
    if (!res.ok) throw new Error('TTS server error');
    return res.blob();
  })
  .then(blob => {
    _ttsAudio = new Audio(URL.createObjectURL(blob));
    if (onEnd) _ttsAudio.onended = () => { _ttsAudio = null; onEnd(); };
    _ttsAudio.play().catch(() => { _ttsAudio = null; if (onEnd) onEnd(); });
  })
  .catch(() => {
    // Fallback: 浏览器 Web Speech API
    if (!('speechSynthesis' in window)) { if (onEnd) onEnd(); return; }
    speechSynthesis.cancel();
    const doSpeak = (voices) => {
      const utterance = new SpeechSynthesisUtterance(cleanText);
      utterance.lang = 'zh-CN';
      utterance.rate = preset.rate;
      if (typeof matchVoice === 'function') {
        const voice = matchVoice(preset, voices || []);
        if (voice) utterance.voice = voice;
      }
      if (onEnd) utterance.onend = onEnd;
      speechSynthesis.speak(utterance);
    };
    if (typeof loadVoices === 'function') {
      loadVoices().then(doSpeak);
    } else {
      doSpeak([]);
    }
  });
}


// ── 回访变化摘要 ──
function showReengagementSummary() {
  const lastVisit = Storage.get('qingzhou_lastVisit');
  if (!lastVisit) return;
  const days = Math.floor((Date.now() - new Date(lastVisit).getTime()) / (1000*60*60*24));
  if (days < 1) return; // 同日不提示

  // 收集期间变化
  const changes = [];
  const keyMoments = Storage.getKeyMoments();
  const recentMoments = keyMoments.filter(m => new Date(m.time) > new Date(lastVisit));
  if (recentMoments.length > 0) {
    changes.push(`期间发生 ${recentMoments.length} 个关键事件`);
  }

  const stages = Storage.get('qingzhou_lifeStages') || [];
  const newStages = stages.filter(s => !s.detectedAt || new Date(s.detectedAt) > new Date(lastVisit));
  if (newStages.length > 0) {
    changes.push(`检测到新的人生阶段：${newStages.map(s=>s.label).join('、')}`);
  }

  const allocation = Storage.get('qingzhou_allocation');
  if (allocation?.generated_at && new Date(allocation.generated_at) > new Date(lastVisit)) {
    changes.push('已生成新的资产配置方案');
  }

  if (changes.length === 0) return;

  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = 'message ai';
  div.innerHTML = `
    <div class="avatar"><img src="assets/logo.png" style="width:100%;height:100%;border-radius:10px;object-fit:cover"></div>
    <div class="bubble" style="background:var(--gold-light);color:var(--gold-dark);font-size:var(--font-size-sm);line-height:1.8">
      <div style="font-weight:700;margin-bottom:4px">📌 距上次来访已过 ${days} 天，期间变化：</div>
      ${changes.map(c => '<div>· ' + c + '</div>').join('')}
    </div>
  `;
  container.insertBefore(div, container.firstChild);
}

// ── 智能欢迎语 ──
function personalizeWelcome() {
  const welcomeEl = document.getElementById('welcomeMsg');
  if (!welcomeEl) return;

  const profile = typeof assembleProfile === 'function' ? assembleProfile() : null;
  const hours = new Date().getHours();
  const timeGreet = hours < 12 ? '早上好' : hours < 18 ? '下午好' : '晚上好';
  const userInfo = Storage.get('qingzhou_userInfo') || {};
  const name = userInfo.nickname || '';

  // 检查画像完整度
  let greeting = '';
  if (profile?.risk && profile?.finance?.amount && profile?.finance?.horizon) {
    // 画像完整：个性化问候
    greeting = `${timeGreet}${name ? '，'+name : ''}。您的${profile.risk.level} ${profile.risk.label}画像已就绪，可以直接问我配置建议。`;
  } else if (profile?.risk) {
    // 仅有风险评估
    const missing = profile.missingFields ? profile.missingFields().join('、') : '金额和期限';
    greeting = `${timeGreet}${name ? '，'+name : ''}。您的风险评估已完成，补充${missing}后我就能为您精准推荐。`;
  } else {
    // 新用户或无画像
    const lastVisit = Storage.get('qingzhou_lastVisit');
    const today = new Date().toISOString().slice(0, 10);
    if (lastVisit && lastVisit !== today) {
      const days = Math.floor((Date.now() - new Date(lastVisit).getTime()) / (1000*60*60*24));
      greeting = `欢迎回来${name ? '，'+name : ''}！距您上次来访已过 ${days} 天。请问有什么可以帮您？`;
    } else {
      greeting = `${timeGreet}${name ? '，'+name : ''}。我是 Navren，可以帮您看懂账单、比较产品和安排资金。先从哪件事开始？`;
    }
  }

  welcomeEl.textContent = greeting;
  Storage.set('qingzhou_lastVisit', new Date().toISOString().slice(0, 10));
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
  const pattern = names.map(escapeRe).join('|');
  if (!pattern) return html;

  const re = new RegExp(`(${pattern})`, 'g');
  return html.replace(re, '<span class="product-name">$1</span>');
}

/** 刷新页面恢复历史消息后，重新绑定气泡内的点击事件 */
function rebindBubbleEvents(msgEl, historyEntry) {
  if (typeof BankUI !== 'undefined') BankUI.rebind(msgEl);
  const bubble = msgEl.querySelector('.bubble');
  if (!bubble) return;

  // 收藏按钮（星标 toggle）
  bubble.querySelectorAll('.tts-btn').forEach(btn => {
    btn.onclick = (e) => {
      e.stopPropagation();
      const text = historyEntry.content || '';
      const bookmarks = Storage.get('qingzhou_bookmarks') || [];
      const idx = bookmarks.findIndex(b => b.text === text.slice(0, 200));
      if (idx >= 0) {
        bookmarks.splice(idx, 1);
        Storage.set('qingzhou_bookmarks', bookmarks);
        btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="12" height="12"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>';
        btn.style.color = ''; btn.style.borderColor = '';
        btn.title = '收藏此回复';
        if (typeof showToast === 'function') showToast('已取消收藏');
      } else {
        bookmarks.push({ text: text.slice(0, 200), timestamp: new Date().toISOString() });
        Storage.set('qingzhou_bookmarks', bookmarks.slice(-20));
        btn.innerHTML = '<svg viewBox="0 0 24 24" fill="var(--gold)" stroke="var(--gold)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="12" height="12"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>';
        btn.style.color = 'var(--gold)'; btn.style.borderColor = 'var(--gold)';
        btn.title = '取消收藏';
        if (typeof showToast === 'function') showToast('已收藏');
      }
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
    const statusColors = { pass: 'var(--gold-dark)', soft_block: '#C04A1A',
      hard_block: '#C53030', tx_block: '#C04A1A', handoff: '#C04A1A',
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
    btn.onmouseover = () => { btn.style.background = 'var(--ink)'; btn.style.color = '#fff'; };
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
  const bubble = msgEl.querySelector('.bubble');
  const div = document.createElement('div');
  div.style.cssText = 'margin-top:12px;padding:10px 0 0;border-top:1px solid var(--border);display:flex;flex-wrap:wrap;gap:6px';
  div.innerHTML = `<span style="font-size:var(--font-size-xs);color:var(--ink-30);width:100%;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.05em;font-weight:600">${getFollowUpLabel()}</span>`;

  questions.slice(0, 4).forEach(q => {
    // 动作型 follow-up 加图标
    const actionIcons = { 'start_risk_quiz': '🔄 ', 'handoff': '📞 ' };
    const icon = actionIcons[q.type] || '';
    const chip = document.createElement('span');
    chip.textContent = icon + q.label;
    chip.style.cssText = 'padding:9px 16px;border-radius:10px;border:1px solid var(--border);font-size:var(--font-size-base);cursor:pointer;color:var(--ink);transition:all 0.2s ease;background:var(--surface-card);line-height:1.4;box-shadow:0 1px 3px rgba(0,0,0,0.04)';
    chip.onmouseover = () => { chip.style.borderColor = 'var(--ink)'; chip.style.background = 'var(--ink)'; chip.style.color = '#fff'; chip.style.transform = 'translateY(-2px)'; chip.style.boxShadow = '0 4px 12px rgba(0,0,0,0.1)'; };
    chip.onmouseout = () => { chip.style.borderColor = 'var(--border)'; chip.style.background = 'var(--surface-card)'; chip.style.color = 'var(--ink)'; chip.style.transform = ''; chip.style.boxShadow = '0 1px 3px rgba(0,0,0,0.04)'; };
    chip.onclick = (e) => {
      e.stopPropagation();
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

  bubble.appendChild(div);
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
  const bubble = msgEl.querySelector('.bubble');
  const div = document.createElement('div');
  div.style.cssText = 'margin-top:12px;padding:10px 0 0;border-top:1px solid var(--border);display:flex;flex-wrap:wrap;gap:6px';
  div.innerHTML = `<span style="font-size:var(--font-size-xs);color:var(--ink-30);width:100%;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.05em;font-weight:600">${getFollowUpLabel()}</span>`;

  questions.forEach(q => {
    const chip = document.createElement('span');
    chip.textContent = q;
    chip.style.cssText = 'padding:9px 16px;border-radius:10px;border:1px solid var(--border);font-size:var(--font-size-base);cursor:pointer;color:var(--ink);transition:all 0.2s ease;background:var(--surface-card);line-height:1.4;box-shadow:0 1px 3px rgba(0,0,0,0.04)';
    chip.onmouseover = () => { chip.style.borderColor = 'var(--ink)'; chip.style.background = 'var(--ink)'; chip.style.color = '#fff'; chip.style.transform = 'translateY(-2px)'; chip.style.boxShadow = '0 4px 12px rgba(0,0,0,0.1)'; };
    chip.onmouseout = () => { chip.style.borderColor = 'var(--border)'; chip.style.background = 'var(--surface-card)'; chip.style.color = 'var(--ink)'; chip.style.transform = ''; chip.style.boxShadow = '0 1px 3px rgba(0,0,0,0.04)'; };
    chip.onclick = (e) => {
      e.stopPropagation();
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

  bubble.appendChild(div);
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
function triggerHandoff(reason) {
  handoffActive = true;
  Storage.addKeyMoment('触发 Handoff：' + reason);

  const profile = assembleProfile();
  const syncedItems = [];
  if (profile.risk?.level) syncedItems.push('风险偏好：' + profile.risk.label + '（' + profile.risk.level + '）');
  if (profile.finance.amount) syncedItems.push('可投金额：约 ' + profile.finance.amount + ' 元');
  if (profile.finance.horizon) syncedItems.push('投资期限：' + profile.finance.horizon);
  if (profile.finance.goal) syncedItems.push('投资目标：' + profile.finance.goal);

  // 打包最近对话摘要
  const chatHistory = Storage.get('qingzhou_chatHistory') || [];
  const recentChat = chatHistory.slice(-4).map(m => (m.role==='user'?'客户':'Navren')+'：'+m.content.slice(0,60)).join('<br>');

  // 打包产品推荐上下文
  const allocation = Storage.get('qingzhou_allocation');
  const productCtx = allocation?.allocation?.map(a => a.name+'（'+a.ratio+'%）').join('、') || '无';

  // 打包情绪标签
  const stages = Storage.get('qingzhou_lifeStages') || [];
  const stageLabel = stages.length > 0 ? stages.map(s=>s.label).join('、') : '无特殊标记';

  // 存入 handoff 记录供 Copilot 读取
  Storage.set('qingzhou_handoffPackage', {
    reason, syncedItems, recentChat, productCtx, stageLabel,
    timestamp: new Date().toISOString()
  });

  addMessage('ai', `
    <div style="text-align:center;">
      <div style="font-size:40px;margin-bottom:12px;">📞</div>
      <strong>正在为您转接人工顾问……</strong>
      <p style="font-size:var(--font-size-sm);color:var(--ink-40);margin-top:8px;">已同步以下信息，顾问接听后可直接查看：</p>
      <div style="text-align:left;margin-top:12px;background:var(--surface);padding:14px;border-radius:var(--r-sm);font-size:var(--font-size-xs);line-height:2;color:var(--ink-70)">
        ${syncedItems.map(i => '<div>✅ ' + i + '</div>').join('')}
        <div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">💬 最近对话：<br>${recentChat || '无'}</div>
        <div>📊 推荐产品：${productCtx}</div>
        <div>🏷️ 客户阶段：${stageLabel}</div>
      </div>
      <div style="display:flex;gap:8px;justify-content:center;margin-top:12px;">
        <button class="retry-btn" onclick="showToast('预约已提交，顾问将在工作时间回电')">预约回电</button>
        <button class="retry-btn" onclick="goTo('chat.html')">返回</button>
      </div>
    </div>
  `);
  scrollToBottom();
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
      classic: `评估完成。您的风险等级为 <strong>${level} ${label}</strong>（评分 ${score}/54）。可承受最大回撤 ${maxDrawdown}。接下来我可以为您推荐适合的产品。`,
      senior: `叔叔/阿姨，测评结果出来了——您是<strong>${label}</strong>。您可以承受 ${maxDrawdown} 以内的波动。接下来我帮您看看适合您的产品吧？`,
      youth: `风险画像出炉：<strong>${level} ${label}</strong>，评分 ${score}/54，可接受回撤 ${maxDrawdown}。接下来跑推荐？`
    };
    addMessage('ai', msgs[currentMode] || msgs.classic);

    // 清除对话历史——强制后续对话基于最新画像，不被旧数据污染
    Storage.set('qingzhou_chatHistory', []);
    showToast('对话已刷新，后续回复将基于最新画像');

    scrollToBottom();
    if (cb) cb(result);
  }

  showQuestion();
}
