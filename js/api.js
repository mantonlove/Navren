/**
 * Navren — API 调用封装
 * POST /api/agent → 智能体编排（意图→槽位→RAG→合规→LLM→日志）
 * 限流 5次/分钟，内置队列调度
 */

let lastCallTime = 0;
let queueRunning = false;
const callQueue = [];

/** 限流调度器 */
async function rateLimiter() {
  const now = Date.now();
  const elapsed = now - lastCallTime;
  if (elapsed < API_CONFIG.rateLimitDelay) {
    await new Promise(r => setTimeout(r, API_CONFIG.rateLimitDelay - elapsed));
  }
  lastCallTime = Date.now();
}

const Api = {
  async sendMessage(text, mode, imageBase64 = null, sentiment = null) {
    try {
      // 收集画像数据（不在前端构造 prompt，只传原始数据给 /api/agent）
      let profile = {};
      try {
        const assembled = typeof assembleProfile === 'function' ? assembleProfile() : null;
        if (assembled) {
          profile = {
            risk_level: assembled.risk?.level || '',
            risk_label: assembled.risk?.label || '',
            risk_score: assembled.risk?.score || 0,
            amount: assembled.finance?.amount || null,
            horizon: assembled.finance?.horizon || null,
            goal: assembled.finance?.goal || null,
            income: assembled.finance?.income || null,
            interests: assembled.finance?.interests || [],
            nickname: assembled.account?.nickname || '',
          };
        }
      } catch (e) {
        console.warn('Profile assembly error:', e.message);
      }
      // 兜底：直接从 localStorage 读取
      if (!profile.risk_level) {
        const rawRisk = Storage.get('qingzhou_riskProfile');
        if (rawRisk?.level) {
          profile.risk_level = rawRisk.level;
          profile.risk_label = rawRisk.label;
          profile.risk_score = rawRisk.score;
        }
      }
      if (!profile.amount) {
        const rawProfile = Storage.get('qingzhou_userProfile') || {};
        profile.amount = rawProfile.amount || null;
        profile.horizon = rawProfile.horizon || null;
        profile.goal = rawProfile.goal || null;
      }

      // 读取对话历史（最近10轮）
      const chatHistory = Storage.get('qingzhou_chatHistory') || [];
      const recentHistory = chatHistory.slice(-20);

      // 构建 agent 请求体（不再构造 system prompt）
      const requestBody = {
        text: text,
        mode: mode || 'classic',
        profile: profile,
        history: recentHistory.map(m => ({
          role: m.role === 'assistant' ? 'ai' : m.role,
          content: m.content
        })),
      };
      // 传入 follow-up 会话记忆
      const followupMemory = Storage.get('qingzhou_followupMemory');
      if (followupMemory) requestBody.followup_memory = followupMemory;

      // 传入用户持仓（只读，辅助推荐）
      const holdings = Storage.get('qingzhou_holdings');
      if (holdings) requestBody.holdings = holdings;

      if (imageBase64) {
        requestBody.image = imageBase64;
      }

      try {
        const bankRequest = typeof BankClient !== 'undefined' && BankClient.isBankRequest(text);
        if (!bankRequest) await rateLimiter();
        const response = await this._fetchAgent(requestBody);
        return this._handleResponse(response, text, profile);
      } catch (err) {
        const msg = err.message || '';
        console.warn('Agent API failed, using fallback:', msg);
        if (msg.includes('429') || msg.includes('QPM') || msg.includes('限流')) {
          if (typeof showToast === 'function') showToast('请求过于频繁，请稍后重试');
        }
        if (typeof BankClient !== 'undefined' && BankClient.isBankRequest(text)) {
          return {bank:true,reply:'银行状态暂不可用。本次对话没有确认执行操作，请到「账户」查询订单核实状态后再试。',isFallback:true};
        }
        return this._fallbackResponse(text, mode);
      }
    } catch (fatalErr) {
      console.error('Fatal error in sendMessage:', fatalErr.message, fatalErr.stack);
      return { reply: '系统错误，请稍后重试。\n\n⚠️ 理财非存款，产品有风险，投资须谨慎。', isFallback: true };
    }
  },

  async _fetchAgent(body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), API_CONFIG.timeout);

    try {
      const res = await fetch('/api/agent', {
        method: 'POST',
        headers: typeof BankClient !== 'undefined' ? await BankClient.agentHeaders() : { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal
      });
      clearTimeout(timer);

      if (!res.ok) {
        const errText = await res.text();
        throw new Error(`HTTP ${res.status}: ${errText.slice(0, 200)}`);
      }

      return await res.json();
    } catch (e) {
      clearTimeout(timer);
      throw e;
    }
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
      follow_up_questions: data?.follow_up_questions || [],
      followup_memory: data?.followup_memory || null,
      agent_chain: data?.agent_chain || null,
      highlight_names: data?.highlight_names || [],
      image_analysis: data?.image_analysis || null,
      compliance_pass: data?.compliance_pass,
      compliance_flags: data?.compliance_flags || []
    };
    if (data.bank) return {...baseResult,bank:true,bank_actions:data.bank_actions||[],budget:data.budget,
      workspace_id:data.workspace_id,version:data.version,bank_response_id:data.bank_response_id};

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
  },

  /** 仅用于真正的 API 异常（网络错误/超时）的 fallback */
  _fallbackResponse(text, mode) {
    const fallback = typeof findFallback === 'function' ? findFallback(text, mode) : null;
    if (fallback) {
      return {
        reply: fallback.text,
        isFallback: true,
        fallbackId: fallback.id
      };
    }
    return {
      reply: '抱歉，系统暂时无法处理您的请求，请稍后重试。\n\n⚠️ 理财非存款，产品有风险，投资须谨慎。',
      isFallback: true
    };
  }
};
