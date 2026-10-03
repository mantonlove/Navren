/**
 * Navren — localStorage 读写封装
 * 统一管理所有本地存储的键值读写与默认值
 */

const STORAGE_KEYS = {
  MODE: 'qingzhou_mode',
  FONT_SIZE: 'qingzhou_fontSize',
  MODE_FONT_SIZES: 'qingzhou_modeFontSizes',
  VOICE_ENABLED: 'qingzhou_voiceEnabled',
  VOICE_PRESET: 'qingzhou_voicePreset',
  RISK_PROFILE: 'qingzhou_riskProfile',
  USER_INFO: 'qingzhou_userInfo',
  USER_PROFILE: 'qingzhou_userProfile',
  PREFERENCES: 'qingzhou_preferences',
  CHAT_HISTORY: 'qingzhou_chatHistory',
  PROFILE_HISTORY: 'qingzhou_profileHistory',
  CHAT_KEY_MOMENTS: 'qingzhou_chatKeyMoments',
  ALLOCATION: 'qingzhou_allocation',
  PRIVACY_READ: 'qingzhou_privacyRead',
  VOICE_GUIDE_READ: 'qingzhou_voiceGuideRead'
};

const DEFAULTS = {
  [STORAGE_KEYS.MODE]: 'classic',
  [STORAGE_KEYS.FONT_SIZE]: 'medium',
  [STORAGE_KEYS.VOICE_ENABLED]: false,
  [STORAGE_KEYS.PREFERENCES]: {
    recommendByRisk: true,
    recommendByHorizon: true,
    marketHotPush: false,
    weeklyReport: true
  }
};

const Storage = {
  get(key) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : (DEFAULTS[key] ?? null);
    } catch {
      return DEFAULTS[key] ?? null;
    }
  },

  set(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (e) {
      console.warn('localStorage write failed:', key, e);
    }
  },

  remove(key) {
    localStorage.removeItem(key);
  },

  /** 清除所有Navren相关数据（保留 profileHistory 作为审计日志） */
  clearAll() {
    Object.values(STORAGE_KEYS).forEach(key => {
      if (key !== STORAGE_KEYS.PROFILE_HISTORY) {
        localStorage.removeItem(key);
      }
    });
  },

  /** 退出登录：保留档案+评估，清除对话 */
  logout() {
    const keep = [
      STORAGE_KEYS.RISK_PROFILE,
      STORAGE_KEYS.USER_PROFILE,
      STORAGE_KEYS.PROFILE_HISTORY,
      STORAGE_KEYS.ALLOCATION
    ];
    const kept = {};
    keep.forEach(k => { kept[k] = localStorage.getItem(k); });

    this.clearAll();

    keep.forEach(k => {
      if (kept[k]) localStorage.setItem(k, kept[k]);
    });
  },

  /** 获取档案历史 */
  getProfileHistory() {
    return this.get(STORAGE_KEYS.PROFILE_HISTORY) || [];
  },

  /** 追加档案变更记录 */
  addProfileHistory(record) {
    const history = this.getProfileHistory();
    history.push(record);
    this.set(STORAGE_KEYS.PROFILE_HISTORY, history);
  },

  /** 获取关键节点 */
  getKeyMoments() {
    return this.get(STORAGE_KEYS.CHAT_KEY_MOMENTS) || [];
  },

  /** 追加关键节点 */
  addKeyMoment(event) {
    const moments = this.getKeyMoments();
    moments.push({ time: new Date().toISOString(), event });
    this.set(STORAGE_KEYS.CHAT_KEY_MOMENTS, moments);
  }
};

// ══════ 共享字体工具（chat.js & mine.js 共用，消除重复定义） ══════
const FONT_SIZE_MAP = { small:'15px', medium:'16px', large:'20px' };
const MODE_DEFAULT_FONT = { classic:'medium', senior:'large', youth:'small' };

/** 模式和字号统一入口；每个模式保留自己的字号选择。 */
function getInterfaceMode() {
  const candidate = new URLSearchParams(location.search).get('mode') || Storage.get(STORAGE_KEYS.MODE);
  return Object.hasOwn(MODE_DEFAULT_FONT, candidate) ? candidate : 'classic';
}
function getModeFont(mode = getInterfaceMode()) {
  const sizes = Storage.get('qingzhou_modeFontSizes') || {};
  return Object.hasOwn(FONT_SIZE_MAP, sizes[mode]) ? sizes[mode] : MODE_DEFAULT_FONT[mode] || 'medium';
}
function setModeFont(mode, size) {
  if (!Object.hasOwn(MODE_DEFAULT_FONT, mode) || !Object.hasOwn(FONT_SIZE_MAP, size)) return;
  Storage.set('qingzhou_modeFontSizes', {...(Storage.get('qingzhou_modeFontSizes') || {}), [mode]: size});
  Storage.set(STORAGE_KEYS.FONT_SIZE, size);
}
// 迁移已有字号到原模式，避免旧的全局字号把关怀版也缩成小字。
if (!Storage.get('qingzhou_modeFontSizes')) {
  let previousSize;
  try { previousSize = JSON.parse(localStorage.getItem(STORAGE_KEYS.FONT_SIZE)); } catch (_) {}
  if (Object.hasOwn(FONT_SIZE_MAP, previousSize)) setModeFont(Storage.get(STORAGE_KEYS.MODE), previousSize);
}

function applyFontSize(size) {
  const px = FONT_SIZE_MAP[size] || '16px';
  const pxi = parseInt(px);
  document.documentElement.style.fontSize = px;
  // chat.html 使用的变量名
  document.documentElement.style.setProperty('--font-size-base', px);
  document.documentElement.style.setProperty('--font-size-msg', px);
  document.documentElement.style.setProperty('--font-size-sm', (pxi-1)+'px');
  document.documentElement.style.setProperty('--font-size-xs', Math.max(13,pxi-3)+'px');
  document.documentElement.style.setProperty('--font-size-lg', (pxi+4)+'px');
  document.documentElement.style.setProperty('--font-size-xl', (pxi+10)+'px');
  // mine.html 使用的变量名
  document.documentElement.style.setProperty('--fs-base', px);
  document.documentElement.style.setProperty('--fs-sm', (pxi-1)+'px');
  document.documentElement.style.setProperty('--fs-lg', (pxi+4)+'px');
  document.documentElement.style.setProperty('--fs-xl', (pxi+10)+'px');
}

// ══════ 语音预设（5种可选声音，映射到系统真实语音）══════
const VOICE_PRESETS = [
  { id: 'female_sweet',   name: '甜美女生', icon: '🎙️', desc: '亲切温暖，自然流畅', rate: 1.3, pitch: 1.05, voiceName: 'female_sweet' },
  { id: 'male_warm',      name: '温暖男生', icon: '🎧', desc: '低沉稳重，专业可信', rate: 1.3, pitch: 0.90, voiceName: 'male_warm' },
  { id: 'male_podcast',   name: '播客男生', icon: '🎵', desc: '磁性醇厚，娓娓道来', rate: 1.3, pitch: 0.95, voiceName: 'male_podcast' }
];

function getVoicePreset() {
  const id = Storage.get(STORAGE_KEYS.VOICE_PRESET) || 'female_sweet';
  return VOICE_PRESETS.find(v => v.id === id) || VOICE_PRESETS[0];
}

/** 异步加载系统语音列表（voices 在首次调用后才异步就绪） */
let _cachedVoices = null;
function loadVoices() {
  if (_cachedVoices && _cachedVoices.length > 0) return Promise.resolve(_cachedVoices);
  return new Promise((resolve) => {
    if (typeof speechSynthesis === 'undefined') { resolve([]); return; }
    const voices = speechSynthesis.getVoices();
    if (voices && voices.length > 0) {
      _cachedVoices = voices;
      resolve(voices);
      return;
    }
    speechSynthesis.onvoiceschanged = () => {
      _cachedVoices = speechSynthesis.getVoices();
      resolve(_cachedVoices);
    };
    // Timeout fallback
    setTimeout(() => {
      if (!_cachedVoices) { _cachedVoices = speechSynthesis.getVoices(); }
      resolve(_cachedVoices);
    }, 2000);
  });
}

/** 根据预设找到最匹配的系统语音 */
function matchVoice(preset, voices) {
  if (!voices || voices.length === 0) return null;
  const zhVoices = voices.filter(v => v.lang === 'zh-CN');
  if (zhVoices.length === 0) return null;

  // 精确匹配预设指定的语音名
  if (preset.voiceName) {
    const exact = zhVoices.find(v => v.name === preset.voiceName);
    if (exact) return exact;
    const partial = zhVoices.find(v => v.name.includes(preset.voiceName));
    if (partial) return partial;
  }

  // 回退：第一个zh-CN语音
  return zhVoices[0];
}

// ── 全局键盘快捷键 ──
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    // 关闭所有打开的弹窗
    document.querySelectorAll('.modal-overlay:not(.hidden)').forEach(m => m.classList.add('hidden'));
  }
});

/** 场景引导提示：首次访问页面时显示 */
function showContextualTip(page) {
  const key = 'qingzhou_tipSeen_' + page;
  if (Storage.get(key)) return;

  const tips = {
    chat: '💡 试试点击预设问题快速开始，或直接说出您的理财需求。左侧图标可切换推荐/账户/我的。',
    recommend: '💡 这里展示配置方案和市场动态。在「我的」中可开关市场热点和周报。',
    account: '查看资产、持仓和账单，或管理银行代扣与资金设置。',
    mine: '可在这里调整阅读方式，也可以补充个人档案。蓝色边框表示已填写。'
  };

  const msg = tips[page];
  if (!msg) return;

  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.style.cssText = 'bottom:100px;max-width:360px;text-align:center;line-height:1.6;font-size:12px';
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 6000);
  Storage.set(key, true);
}

// ══════ 跨页面共享工具函数 ══════

/** 带模式参数的页面跳转 */
function goTo(url) {
  const mode = Storage.get('qingzhou_mode') || 'classic';
  NavrenApp.navigate(url, mode);
}

/** Toast 消息提示 */
function showToast(msg) {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3000);
  return toast;
}

/** HTML 转义 */
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}
