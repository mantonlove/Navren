/**
 * Navren — mine.html 页面逻辑
 */

let currentMode = 'classic';
// FONT_SIZE_MAP, MODE_DEFAULT_FONT, applyFontSize 定义在 storage.js（共享模块）

document.addEventListener('DOMContentLoaded', () => {
  const params = new URLSearchParams(window.location.search);
  // 优先使用 localStorage 记住用户最后选择
  // URL 参数优先（用户显式选择），localStorage 兜底
  currentMode = getInterfaceMode();
  Storage.set('qingzhou_mode', currentMode);
  document.body.className = 'mode-' + currentMode;

  // 应用字体大小
  const savedFontSize = getModeFont(currentMode);
  applyFontSize(savedFontSize);

  renderModeSelector();
  renderFontSize();
  renderVoiceToggle();
  renderVoicePresets();
  renderPrefs();

  // 私人资料由当前视图按需读取；不提前挂载旧画像或后台图谱。

  // 恢复昵称
  const userInfo = Storage.get('qingzhou_userInfo');
  if (userInfo?.nickname) {
    const name=document.getElementById('profileName');if(name)name.textContent=userInfo.nickname;
  }
  // 恢复头像
  if (userInfo?.avatar) {
    renderLocalAvatar(userInfo.avatar);
  }

  // 场景引导
  if (typeof showContextualTip === 'function') showContextualTip('mine');

  // 恢复已保存的账户安全信息(已移至account.html)
  const phoneEl = document.getElementById('accountPhone');
  if (phoneEl) {
    const savedPhone = Storage.get('qingzhou_accountPhone');
    if (savedPhone) phoneEl.textContent = savedPhone.slice(0, 3) + '****' + savedPhone.slice(-4);
  }
  const emailEl = document.getElementById('accountEmail');
  if (emailEl) {
    const savedEmail = Storage.get('qingzhou_accountEmail');
    if (savedEmail) {
      const parts = savedEmail.split('@');
      emailEl.textContent = parts[0].slice(0, 3) + '***@' + (parts[1] || '');
    }
  }
});



// ── Mode Selector ──
const MODE_ICONS = {
  classic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 18v-7"/><path d="M11.119 2.205a2 2 0 0 1 1.762 0l7.84 3.846A.5.5 0 0 1 20.5 7h-17a.5.5 0 0 1-.22-.949z"/><path d="M14 18v-7"/><path d="M18 18v-7"/><path d="M3 22h18"/><path d="M6 18v-7"/></svg>',
  senior: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19.414 14.414C21 12.828 22 11.5 22 9.5a5.5 5.5 0 0 0-9.591-3.676.6.6 0 0 1-.818.001A5.5 5.5 0 0 0 2 9.5c0 2.3 1.5 4 3 5.5l5.535 5.362a2 2 0 0 0 2.879.052 2.12 2.12 0 0 0-.004-3 2.124 2.124 0 1 0 3-3 2.124 2.124 0 0 0 3.004 0 2 2 0 0 0 0-2.828l-1.881-1.882a2.41 2.41 0 0 0-3.409 0l-1.71 1.71a2 2 0 0 1-2.828 0 2 2 0 0 1 0-2.828l2.823-2.762"/></svg>',
  youth: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z"/><path d="M20 2v4"/><path d="M22 4h-4"/><circle cx="4" cy="20" r="2"/></svg>'
};

// ── 画像完善度 ──
function renderProfileCompletion() {
  const profile = typeof assembleProfile === 'function' ? assembleProfile() : null;
  if (!profile) return;

  const fields = [
    { key: 'risk', label: '风险评估', done: !!profile.risk },
    { key: 'amount', label: '可投金额', done: !!profile.finance?.amount },
    { key: 'horizon', label: '投资期限', done: !!profile.finance?.horizon },
    { key: 'goal', label: '投资目标', done: !!profile.finance?.goal },
    { key: 'income', label: '收入来源', done: !!profile.finance?.income },
    { key: 'interests', label: '关注领域', done: !!(profile.finance?.interests?.length > 0) }
  ];
  const done = fields.filter(f => f.done).length;
  const pct = Math.round((done / fields.length) * 100);

  const container = document.getElementById('profileCompletion');
  if (!container) return;

  const bar = document.createElement('div');
  bar.className = 'profile-completion';
  bar.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span style="font-size:var(--font-size-xs);font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--ink-40)">画像完善度</span>
      <span style="font-size:var(--font-size-xs);font-weight:700;color:${pct >= 80 ? '#10B981' : pct >= 50 ? 'var(--gold-dark)' : 'var(--ink-40)'}">${pct}% · ${done}/${fields.length} 项</span>
    </div>
    <div style="height:4px;border-radius:2px;background:var(--ink-15)">
      <div style="height:4px;border-radius:2px;background:${pct >= 80 ? '#10B981' : pct >= 50 ? 'var(--gold)' : 'var(--ink-40)'};width:${pct}%;transition:width 0.3s"></div>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:6px">
      ${fields.filter(f => !f.done).map(f => `<span style="font-size:var(--font-size-xs);color:var(--ink-40);background:var(--surface);padding:2px 6px;border-radius:4px">+ ${f.label}</span>`).join('')}
    </div>
  `;
  container.replaceChildren(bar);
}

// ── Mode Selector ──
function renderModeSelector() {
  const container = document.getElementById('modeSelector');
  const modes = [
    { id: 'classic', name: '经典版' },
    { id: 'senior', name: '关怀版' },
    { id: 'youth', name: '青春版' }
  ];
  container.innerHTML = '';
  modes.forEach(m => {
    const div = document.createElement('button');
    div.type = 'button';
    div.className = 'mode-opt' + (currentMode === m.id ? ' active' : '');
    div.setAttribute('aria-pressed', String(currentMode === m.id));
    div.innerHTML = `<div class="m-icon">${MODE_ICONS[m.id]}</div><div class="m-label">${m.name}</div>`;
    div.onclick = () => {
      NavrenDesign.switchMode(m.id);
    };
    container.appendChild(div);
  });
}

// ── Font Size ──
function renderFontSize() {
  const row = document.getElementById('fontSizeRow');
  const sizes = [
    { id: 'small', label: '小' },
    { id: 'medium', label: '中' },
    { id: 'large', label: '大' }
  ];
  const current = getModeFont(currentMode);
  row.innerHTML = '';
  sizes.forEach(s => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'font-opt' + (current === s.id ? ' active' : '');
    btn.setAttribute('aria-pressed', String(current === s.id));
    btn.textContent = s.label;
    btn.onclick = () => {
      setModeFont(currentMode, s.id);
      applyFontSize(s.id);
      renderFontSize();
    };
    row.appendChild(btn);
  });
}

// ── Voice Toggle ──
function renderVoiceToggle() {
  const toggle = document.getElementById('voiceToggle');
  if (!toggle) return;
  const enabled = Boolean(Storage.get('qingzhou_voiceEnabled'));
  toggle.classList.toggle('on', enabled);
  toggle.setAttribute('aria-checked', String(enabled));
}
function toggleVoice() {
  const toggle = document.getElementById('voiceToggle');
  const enabled = !Storage.get('qingzhou_voiceEnabled');
  Storage.set('qingzhou_voiceEnabled', enabled);
  renderVoiceToggle();
}

// ── 用户主动维护的本地财务记忆 ──
const MEMORY_CATEGORIES = {
  goal: '财务目标',
  planned_commitment: '已确认的大额支出',
  liquidity_preference: '流动性偏好',
  product_interest: '关注的产品类型',
  knowledge_preference: '信息说明偏好'
};
let financialMemoryState = { enabled: false, items: [] };

let memoryReadSequence = 0;
async function loadFinancialMemories() {
  const list = document.getElementById('financialMemoryList');
  if (!list) return;
  const sequence = ++memoryReadSequence;
  list.replaceChildren(memoryMessage('正在核对记忆来源…'));
  try {
    await BankClient.ensure();
    const guard = await BankClient.guard();
    const result = await BankClient.get('/memories');
    if (sequence !== memoryReadSequence || !guard.valid(result)) return;
    financialMemoryState = result;
    renderFinancialMemories();
    return result;
  } catch (error) {
    if (sequence !== memoryReadSequence) return;
    list.replaceChildren(memoryMessage('记忆暂未取得。已保存内容的状态尚未核实。'));
    const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'p-btn';
    retry.textContent = '重新读取记忆'; retry.onclick = loadFinancialMemories; list.append(retry);
    const status = document.getElementById('memoryStatus');
    if (status) status.textContent = error.message;
  }
}
async function acceptMemoryMutation(result) {
  await BankClient.refreshIdentity();
  const identity = BankClient.identity();
  if (!identity || result.workspace_id !== identity.workspace_id || result.privacy_revision !== identity.privacy_revision) return false;
  financialMemoryState = result;
  renderFinancialMemories();
  window.dispatchEvent(new Event('navren:memory-updated'));
  return true;
}
window.NavrenMineMemory = { state: () => financialMemoryState, load: loadFinancialMemories };
window.addEventListener('navren:privacy-reset', () => {
  memoryReadSequence++;
  financialMemoryState = { enabled: false, items: [] };
  document.getElementById('financialMemoryList')?.replaceChildren(memoryMessage('资料权限已变化，正在重新核验。'));
  document.getElementById('archiveContent')?.replaceChildren();
  document.getElementById('riskDetail')?.replaceChildren();
  document.getElementById('bookmarksList')?.replaceChildren();
  closeModal();
});

function memoryMessage(text) {
  const message = document.createElement('p');
  message.className = 'memory-empty';
  message.textContent = text;
  return message;
}

function renderFinancialMemories() {
  const expectedRevision = financialMemoryState.personal_revision;
  const toggle = document.getElementById('memoryToggle');
  const status = document.getElementById('memoryStatus');
  const list = document.getElementById('financialMemoryList');
  if (!toggle || !status || !list) return;
  toggle.classList.toggle('on', Boolean(financialMemoryState.enabled));
  toggle.setAttribute('aria-checked', String(Boolean(financialMemoryState.enabled)));
  status.textContent = financialMemoryState.enabled
    ? '已开启：只在已接入的资金时间轴分析中参考匹配内容；不会改变余额或计算结果。'
    : '仅保存：对话读取已关闭。你仍可主动查看、修改或删除已保存内容。';
  const consent = document.getElementById('mineConsentStatus');
  if (consent) consent.textContent = status.textContent;
  list.replaceChildren();
  if (!financialMemoryState.items?.length) {
    list.append(memoryMessage('暂未保存财务记忆。你可以手动添加目标、已确认的大额支出或资金偏好；Navren 不会从消费流水自动推断。'));
    return;
  }
  financialMemoryState.items.forEach(item => {
    const card = document.createElement('article');
    card.className = 'memory-item';
    const content = document.createElement('div');
    const heading = document.createElement('div');
    heading.className = 'memory-item-heading';
    const category = document.createElement('span');
    category.className = 'memory-category';
    category.textContent = MEMORY_CATEGORIES[item.category] || item.category_label || '财务记忆';
    const origin = document.createElement('span');
    origin.className = 'memory-origin';
    origin.textContent = item.source_type === 'conversation_user_statement'
      ? '对话中主动陈述 · 已确认保存' : '本人填写';
    const expiry = document.createElement('span');
    expiry.className = 'memory-expiry' + (item.expired ? ' is-expired' : '');
    expiry.textContent = item.expired
      ? '已过期 · 不参与分析'
      : item.expires_on ? '有效至 ' + item.expires_on : '未设置有效期';
    heading.append(category, origin, expiry);
    const value = document.createElement('p');
    value.className = 'memory-value';
    value.textContent = item.value;
    content.append(heading, value);
    const actions = document.createElement('div');
    actions.className = 'memory-actions';
    const edit = document.createElement('button');
    edit.type = 'button'; edit.className = 'p-btn'; edit.textContent = '修改';
    edit.setAttribute('aria-label', '修改' + category.textContent);
    edit.addEventListener('click', () => openFinancialMemoryEditor(item, expectedRevision));
    const remove = document.createElement('button');
    remove.type = 'button'; remove.className = 'p-btn'; remove.textContent = '删除';
    remove.setAttribute('aria-label', '删除' + category.textContent);
    remove.addEventListener('click', () => deleteFinancialMemory(item, expectedRevision));
    actions.append(edit, remove);
    const meta = document.createElement('p');
    meta.className = 'memory-meta';
    const created = Number(item.created_at), confirmed = Number(item.confirmed_at);
    const dates = [];
    if (item.created_at != null && Number.isFinite(created)) dates.push('记录于 ' + new Date(created * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'}));
    if (item.confirmed_at != null && Number.isFinite(confirmed)) dates.push('本人确认于 ' + new Date(confirmed * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'}));
    meta.textContent = '来源：' + (item.source_type === 'conversation_user_statement' ? '对话中的主动陈述' : '本人填写')
      + (dates.length ? ' · ' + dates.join(' · ') : '');
    card.append(content, actions, meta);
    if (item.relations?.length) {
      const relations = document.createElement('div');
      relations.className = 'memory-relations';
      item.relations.forEach(relation => {
        const row = document.createElement('div');
        row.className = 'memory-relation-row';
        const relationText = document.createElement('span');
        const relationLabels = { supports: '支持', conflicts_with: '与之冲突',
          depends_on: '依赖', related_to: '关联' };
        relationText.textContent = (relationLabels[relation.relation] || '关联') + '：' + relation.value;
        const removeRelation = document.createElement('button');
        removeRelation.type = 'button'; removeRelation.className = 'p-btn'; removeRelation.textContent = '解除';
        removeRelation.addEventListener('click', async () => {
          try {
            requireMemoryRevision(expectedRevision);
            await acceptMemoryMutation(await BankClient.post('/memory-relations/' + relation.relation_id + '/delete', { expected_revision: expectedRevision }));
          } catch (error) { showToast('记忆关联未解除：' + error.message); }
        });
        row.append(relationText, removeRelation);
        relations.append(row);
      });
      card.append(relations);
    }
    if (financialMemoryState.items.length > 1) {
      const relate = document.createElement('button');
      relate.type = 'button'; relate.className = 'p-btn'; relate.textContent = '关联另一条记忆';
      relate.addEventListener('click', () => openMemoryRelationEditor(item, expectedRevision));
      const advanced = document.createElement('details'); const summary = document.createElement('summary'); summary.textContent = '高级关联'; advanced.append(summary, relate); card.append(advanced);
    }
    list.append(card);
  });
}

window.toggleFinancialMemory = async function() {
  const next = !financialMemoryState.enabled;
  const expectedRevision = financialMemoryState.personal_revision;
  if (next && !confirm('允许 Navren 在已接入的资金分析中读取匹配记忆，并整理对话中的本人陈述为待核实资料？内容保存在本机工作区；关闭后保留保存内容，可在“我的”修改或删除，旧对话上下文立即失效。')) return;
  const toggle = document.getElementById('memoryToggle');
  toggle.disabled = true;
  window.dispatchEvent(new Event('navren:privacy-reset'));
  try {
    requireMemoryRevision(expectedRevision);
    const result = await BankClient.post('/memories/settings', { enabled: next, expected_revision: expectedRevision });
    if (await acceptMemoryMutation(result)) showToast(next ? '已允许在已接入的资金分析中参考记忆' : '已撤销读取授权，保存内容仍可管理');
  } catch (error) {
    showToast('授权设置未更新：' + error.message);
    await loadFinancialMemories();
  } finally { toggle.disabled = false; }
};

function requireMemoryRevision(revision) {
  if (!Number.isSafeInteger(revision)) throw new Error('个人资料版本尚未取得，请重新读取后再操作。');
}

function openMemoryRelationEditor(item, expectedRevision = financialMemoryState.personal_revision) {
  const body = document.getElementById('modalBody');
  const overlay = document.getElementById('modalOverlay');
  document.getElementById('modalTitle').textContent = '关联财务记忆';
  body.replaceChildren();
  const targetLabel = document.createElement('label'); targetLabel.textContent = '选择另一条记忆';
  const target = document.createElement('select');
  financialMemoryState.items.filter(other => other.node_id !== item.node_id).forEach(other => {
    const option = document.createElement('option');
    option.value = other.node_id;
    option.textContent = (MEMORY_CATEGORIES[other.category] || '财务记忆') + ' · ' + other.value.slice(0, 60);
    target.append(option);
  });
  const relationLabel = document.createElement('label'); relationLabel.textContent = '关系';
  const relation = document.createElement('select');
  [['related_to','相关'],['supports','支持'],['conflicts_with','与之冲突'],['depends_on','依赖']].forEach(([value,label]) => {
    const option = document.createElement('option'); option.value = value; option.textContent = label; relation.append(option);
  });
  body.append(targetLabel, target, relationLabel, relation);
  overlay.classList.remove('hidden');
  document.getElementById('modalConfirm').onclick = async () => {
    try {
      requireMemoryRevision(expectedRevision);
      const result = await BankClient.post('/memories/' + item.node_id + '/relations', {
        target_node_id: target.value, relation: relation.value, expected_revision: expectedRevision
      });
      closeModal(); if (await acceptMemoryMutation(result)) showToast('记忆关联已保存');
    } catch (error) { showToast('记忆关联未保存：' + error.message); }
  };
}

function openFinancialMemoryEditor(item = null, expectedRevision = financialMemoryState.personal_revision) {
  const body = document.getElementById('modalBody');
  const overlay = document.getElementById('modalOverlay');
  document.getElementById('modalTitle').textContent = item ? '修改财务记忆' : '添加财务记忆';
  body.replaceChildren();
  if (!item) {
    const categoryLabel = document.createElement('label');
    categoryLabel.textContent = '记忆类别';
    categoryLabel.htmlFor = 'memoryCategory';
    const category = document.createElement('select');
    category.id = 'memoryCategory';
    Object.entries(MEMORY_CATEGORIES).forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value; option.textContent = label; category.append(option);
    });
    body.append(categoryLabel, category);
  } else {
    const categoryLabel = document.createElement('p');
    categoryLabel.className = 'memory-category';
    categoryLabel.textContent = MEMORY_CATEGORIES[item.category] || '财务记忆';
    body.append(categoryLabel);
  }
  const valueLabel = document.createElement('label');
  valueLabel.htmlFor = 'memoryValue';
  valueLabel.textContent = '内容（最多240字）';
  const value = document.createElement('textarea');
  value.id = 'memoryValue'; value.maxLength = 240; value.rows = 4;
  value.placeholder = '如：明年10月需要留出学费；未来两个月希望保留充足周转资金。';
  value.value = item?.value || '';
  const expiryLabel = document.createElement('label');
  expiryLabel.htmlFor = 'memoryExpiresOn';
  expiryLabel.textContent = '参考有效期（可留空）';
  const expiry = document.createElement('input');
  expiry.id = 'memoryExpiresOn'; expiry.type = 'date';
  expiry.min = financialMemoryState.as_of || '';
  expiry.value = item?.expires_on || '';
  const hint = document.createElement('p');
  hint.className = 'memory-meta';
  hint.textContent = '到期日按参考日期计算，到期后仍可管理，不进入分析。保存不会启用读取授权或执行资金操作。请勿填写密码、验证码、证件号、手机号或银行卡号。';
  body.append(valueLabel, value, expiryLabel, expiry, hint);
  overlay.classList.remove('hidden');
  value.focus();
  document.getElementById('modalConfirm').textContent = financialMemoryState.enabled ? '保存记忆' : '仅保存';
  document.getElementById('modalConfirm').onclick = async () => {
    const text = value.value.trim();
    if (!text) { showToast('请填写记忆内容'); value.focus(); return; }
    const path = item ? '/memories/' + item.node_id + '/correct' : '/memories';
    const payload = item
      ? { value: text, expires_on: expiry.value || null, expected_revision: expectedRevision }
      : { category: document.getElementById('memoryCategory').value, value: text, expires_on: expiry.value || null, expected_revision: expectedRevision };
    try {
      requireMemoryRevision(expectedRevision);
      const result = await BankClient.post(path, payload);
      closeModal(); if (await acceptMemoryMutation(result)) showToast(item ? '记忆已修改，旧上下文已失效' : '记忆已保存到本机工作区');
    } catch (error) {
      showToast('财务记忆未保存：' + error.message);
    }
  };
}

async function deleteFinancialMemory(item, expectedRevision = financialMemoryState.personal_revision) {
  if (!confirm('删除这条财务记忆？删除后 Navren 不会再在分析中参考它。')) return;
  try {
    requireMemoryRevision(expectedRevision);
    const result = await BankClient.post('/memories/' + item.node_id + '/delete', { expected_revision: expectedRevision });
    if (await acceptMemoryMutation(result)) showToast('记忆及其派生关系已删除，银行交易仍保留');
  } catch (error) {
    showToast('财务记忆未删除：' + error.message);
  }
}

// ── Risk Profile ──
function renderRiskProfile() {
  const stored = Storage.get('qingzhou_riskProfile');
  const identity = BankClient.identity?.();
  const risk = stored && stored.workspace_id === identity?.workspace_id && stored.privacy_revision === identity?.privacy_revision ? stored : null;
  const detail = document.getElementById('riskDetail');
  if (!detail) return;
  detail.replaceChildren();
  const p = document.createElement('p');
  p.textContent = risk ? '浏览器聊天问卷记录：' + (risk.label || '未命名') + ' ' + (risk.level || '未知等级') : stored ? '历史聊天问卷的身份或授权版本无法核验，内容已隐藏。' : '未保存聊天问卷结果；这不代表已完成正式风险测评。';
  detail.append(p);
  const source = document.createElement('p'); source.className = 'mine-source';
  source.textContent = '来源：当前浏览器的聊天问卷。仅用于沟通，不改银行业务等级或交易资格。';
  detail.append(source);
  if(risk){const date=new Date(risk.assessedAt||''),expires=new Date(risk.expiresAt||''),record=document.createElement('p');record.className='mine-source';record.textContent='聊天问卷记录时间：'+(!isNaN(date)?date.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}):'未知')+'；'+(!isNaN(expires)?expires.getTime()<=Date.now()?'浏览器问卷记录已过期':'浏览器记录标记有效至 '+expires.toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'}):'记录有效期未知')+'。正式测评有效性仍未知。';detail.append(record);}
  if (risk?.maxDrawdown != null) { const p = document.createElement('p'); p.textContent = '问卷记录的回撤意愿：' + risk.maxDrawdown; detail.append(p); }
}

// ── Archive with Timeline ──
function renderArchive() {
  const container = document.getElementById('archiveContent');
  if (!container) return;
  const profile = Storage.get('qingzhou_userProfile') || {};
  const history = Storage.getProfileHistory();
  const labels = {age:'年龄段',occupation:'职业',income:'本人陈述的收入',family:'家庭情况',amount:'本人陈述的可投金额',horizon:'投资期限',liquidity:'流动性需要',existingAssets:'本人陈述的资产',liabilities:'本人陈述的负债',goal:'旧目标陈述',experience:'投资经验',interests:'金融兴趣',riskComfort:'聊天风险态度'};
  container.replaceChildren();
  const identity = BankClient.identity?.();
  for (const [key,label] of Object.entries(labels)) {
    const latest = history.filter(h => h.field === key).at(-1);
    // 旧聊天提取、模型默认值没有当前服务端确认链，不能复活为新画像。
    if (profile[key] == null || !latest || latest.source !== 'manual_edit' || latest.confirmed !== true) continue;
    if (!latest.workspace_id || latest.workspace_id !== identity?.workspace_id) continue;
    const row = document.createElement('article'); row.className = 'mine-legacy-item';
    const title = document.createElement('h3'); title.textContent = label;
    const value = document.createElement('p'); value.textContent = Array.isArray(profile[key]) ? profile[key].join('、') : String(profile[key]);
    const source = document.createElement('p'); source.className = 'mine-source'; source.textContent = '历史本人手动填写 · 当前浏览器 · 不参与新画像分析';
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'p-btn'; remove.textContent = '清除本机旧资料';
    remove.onclick = () => { if (!confirm('清除这项浏览器旧资料？不会删除银行交易、服务端目标或已保存记忆。')) return; const next = Storage.get('qingzhou_userProfile') || {}; delete next[key]; Storage.set('qingzhou_userProfile', next); Storage.addProfileHistory({field:key,oldValue:null,newValue:null,source:'manual_edit',confirmed:true,timestamp:new Date().toISOString(),workspace_id:identity?.workspace_id}); renderArchive(); };
    const edit = document.createElement('button'); edit.type='button'; edit.className='p-btn'; edit.textContent='修改本机旧资料'; edit.onclick=()=>editArchiveField(key,label,Array.isArray(profile[key])?profile[key].join('、'):String(profile[key]));row.append(title,value,source,edit,remove);container.append(row);
  }
  if (!container.children.length) { const p = document.createElement('p'); p.textContent = '没有能核对当前身份的本人手动旧资料。历史原记录仍保留；对话提取与模型默认值不会补回已删除记忆。'; container.append(p); }
}

window.toggleTimeline = function(btn, key) {
  const list = document.getElementById('timeline-' + key);
  if (!list) return;
  list.style.display = list.style.display === 'none' ? 'block' : 'none';
};

// ── Preferences ──
const PREF_ITEMS = [
  { key: 'recommendByRisk', label: '根据风险偏好推荐' },
  { key: 'recommendByHorizon', label: '根据投资期限筛选' },
  { key: 'marketHotVisible', label: '展示市场热点' },
  { key: 'weeklyReport', label: '展示市场周报' }
];

function renderPrefs() {
  const container = document.getElementById('prefsBody');
  if (!container) return;
  const prefs = {marketHotVisible:true,weeklyReport:true,...(Storage.get('qingzhou_preferences') || {})};
  container.innerHTML = PREF_ITEMS.map(p => `
    <div class="trow">
      <div class="tl">${p.label}</div>
      <button type="button" role="switch" aria-label="${p.label}" aria-checked="${!!prefs[p.key]}" class="toggle${prefs[p.key] ? ' on' : ''}" onclick="togglePref('${p.key}',this)"></button>
    </div>
  `).join('');
  const note=document.createElement('p');note.className='market-limit';note.textContent='展示开关不订阅推送。市场提醒服务尚未接入。';container.append(note);
}

window.togglePref = function(key, el) {
  const prefs = {marketHotVisible:true,weeklyReport:true,...(Storage.get('qingzhou_preferences') || {})};
  prefs[key] = !prefs[key];
  Storage.set('qingzhou_preferences', prefs);
  el.classList.toggle('on', prefs[key]); el.setAttribute('aria-checked', String(prefs[key]));
};

// ── Account Actions ──
async function switchAccount() {
  if (!confirm('创建一个独立的本机演示账户？原工作区的数据仍保留，新账户不继承原账户的权限、目标或记忆。')) return;
  try { await BankClient.post('/session/reset', {confirmed:true}); window.dispatchEvent(new Event('navren:privacy-reset')); NavrenApp.navigate('chat.html'); }
  catch (error) { showToast('独立演示账户未创建：' + error.message); }
}

// 旧本机档案只维护自己的历史，不清空有效对话，也不写入新画像。
window._notifyProfileChanged = function() {};

function logout() {
  showToast('本机演示没有真实银行登录。可创建独立演示账户，或在数据授权中撤销记忆读取。');
}

// ── Modal Helpers ──
let modalCallback = null;
function openModal(title, fields, cb) {
  document.getElementById('modalTitle').textContent = title;
  const body = document.getElementById('modalBody'); body.replaceChildren();
  fields.forEach((field,index) => {const label=document.createElement('label'); label.textContent=field.label;label.htmlFor='modalField'+index;const input=document.createElement('input');input.type=field.type||'text';input.id=label.htmlFor;input.value=field.value||'';input.placeholder=field.placeholder||'';input.autocomplete='off';body.append(label,input);});
  document.getElementById('modalOverlay').classList.remove('hidden');
  document.getElementById('modalField0')?.focus();
  document.getElementById('modalConfirm').textContent = '确认';
  modalCallback = cb;
  document.getElementById('modalConfirm').onclick = () => {
    const values = fields.map((_, i) => document.getElementById('modalField' + i)?.value || '');
    closeModal();
    if (cb) cb(values.length === 1 ? values[0] : values);
  };
}
function closeModal() {
  document.getElementById('modalOverlay').classList.add('hidden');
  document.getElementById('profileFieldClear')?.remove();
  document.getElementById('goalFormDiscard')?.remove();
  const cancel=document.querySelector('.modal-foot [onclick="closeModal()"]');if(cancel)cancel.textContent='取消';
  modalCallback = null;
}

function renderLocalAvatar(value) {
  if (typeof value !== 'string' || !/^data:image\/(?:png|jpeg|webp|gif);base64,[a-z0-9+/]+=*$/i.test(value)) return;
  const avatar = document.getElementById('profileAvatar'); if(!avatar)return;
  const image = document.createElement('img'); image.src=value; image.alt='本机头像';avatar.replaceChildren(image);
}
function handleAvatarUpload(event) {
  const file = event.target.files[0];
  if (!file) return;
  if (!['image/png','image/jpeg','image/webp','image/gif'].includes(file.type) || file.size > 5 * 1024 * 1024) {showToast('请选择5MB以内的PNG、JPEG、WebP或GIF图片。');event.target.value='';return;}
  const reader = new FileReader();
  reader.onload = (e) => {const dataUrl=e.target.result;const info=Storage.get('qingzhou_userInfo')||{};info.avatar=dataUrl;Storage.set('qingzhou_userInfo',info);renderLocalAvatar(dataUrl);showToast('本机头像已更新');};
  reader.readAsDataURL(file);
}

function editProfile() {
  const currentName = document.getElementById('profileName').textContent;
  openModal('编辑个人资料', [
    { label: '昵称', value: currentName, placeholder: '输入您的昵称' }
  ], (name) => {
    if (name && name.trim()) {
      const info = Storage.get('qingzhou_userInfo') || {};
      info.nickname = name.trim();
      Storage.set('qingzhou_userInfo', info);
      document.getElementById('profileName').textContent = name.trim();
      showToast('昵称已更新');
    }
  });
}




function editArchiveField(key, label, currentValue) {
  openModal('编辑' + label, [
    { label: label, value: currentValue, placeholder: '输入新的' + label }
  ], (newValue) => {
    if (!newValue || !newValue.trim()) return;
    const val = newValue.trim();
    const profile = Storage.get('qingzhou_userProfile') || {};
    const oldValue = profile[key] || null;

    // 格式化：amount 去掉"元""万"等，存数字
    if (key === 'amount') {
      const num = parseInt(val.replace(/[^0-9]/g, ''));
      if (isNaN(num)) { showToast('请输入有效数字'); return; }
      profile[key] = num;
    } else if (key === 'interests') {
      profile[key] = val.split(/[,，、\s]+/).filter(Boolean);
    } else {
      profile[key] = val;
    }

    Storage.set('qingzhou_userProfile', profile);
    Storage.addProfileHistory({
      field: key, oldValue, newValue: profile[key],
      source: 'manual_edit', timestamp: new Date().toISOString(),
      confidence: 1, context: '用户在"我的"页面手动编辑本机旧资料', confirmed: true,workspace_id:BankClient.identity()?.workspace_id
    });
    renderArchive();
    showToast('本机旧资料已更新；不改变银行记录或服务端记忆');
  });
  if (currentValue) {
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.id = 'profileFieldClear';
    clear.className = 'p-btn';
    clear.textContent = '清除此项';
    clear.addEventListener('click', () => {
      const profile = Storage.get('qingzhou_userProfile') || {};
      const oldValue = profile[key] ?? null;
      delete profile[key];
      Storage.set('qingzhou_userProfile', profile);
      Storage.addProfileHistory({
        field: key, oldValue, newValue: null, source: 'manual_edit',
        timestamp: new Date().toISOString(), confidence: 1,
        context: '用户在“我的”页面清除此项', confirmed: true, workspace_id:BankClient.identity()?.workspace_id,
      });
      closeModal();
        renderProfileCompletion();
      renderArchive();
      showToast(label + '已清除');
    });
    document.querySelector('.modal-foot')?.insertBefore(clear, document.getElementById('modalConfirm'));
  }
}



// ── Bookmarks ──
function renderBookmarks() {
  const list = document.getElementById('bookmarksList');
  if (!list) return;
  const bookmarks = Storage.get('qingzhou_bookmarks') || [];
  const identity = BankClient.identity?.();
  const categories = {chat_message:'对话收藏',market_news:'市场资讯',market_weekly:'市场周报',recommendation:'备选方案'};
  list.replaceChildren();
  if (!bookmarks.length) { list.append(memoryMessage('尚未收藏内容。可在对话或发现页收藏，再从这里继续阅读。')); return; }
  for (const [category,label] of Object.entries(categories)) {
    const rows = bookmarks.map((item,index) => ({item,index})).filter(({item}) => (item.category || 'chat_message') === category);
    if (!rows.length) continue;
    const section = document.createElement('section'); const h = document.createElement('h2'); h.textContent = label + ' · ' + rows.length; section.append(h);
    for (const {item,index} of rows.reverse()) {
      const publicItem = ['market_news','market_weekly'].includes(category);
      const readable = publicItem || (identity && item.workspace_id === identity.workspace_id && item.privacy_revision === identity.privacy_revision);
      const row = document.createElement('article'); row.className = 'mine-saved-item';
      const text = document.createElement('p'); text.textContent = readable ? item.text || '未命名收藏' : '历史收藏身份或授权版本无法核验，正文已隐藏。';
      if(readable){text.setAttribute('translate','no');text.dataset.i18nSkip='';}
      const meta = document.createElement('p'); meta.className = 'mine-source';
      const at = item.timestamp ? new Date(item.timestamp) : null;
      meta.textContent = (at && !isNaN(at) ? at.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}) : '记录日期未知') + (item.version != null ? ' · 保存版本 ' + item.version : ' · 版本未记录');
      const actions = document.createElement('div'); actions.className = 'mine-row-actions';
      if (publicItem && item.object_id) {
        const href=window.NavrenMine?.readingHref?.({...item,object_kind:category==='market_weekly'?'weekly':'event'});
        if(href){const open=document.createElement('a');open.className='p-btn';open.textContent='查看保存对象与版本';open.id='mine-bookmark-'+index;open.href=href;actions.append(open);}
        else actions.append(memoryMessage('保存的来源或历史版本不完整，请重新核对。'));
      }
      const remove = document.createElement('button'); remove.type='button'; remove.className='p-btn'; remove.textContent='删除收藏'; remove.onclick=()=>deleteBookmark(index); actions.append(remove);
      row.append(text,meta,actions);section.append(row);
    }
    list.append(section);
  }
}

window.deleteBookmark = function(idx) {
  const bookmarks = Storage.get('qingzhou_bookmarks') || [];
  bookmarks.splice(idx, 1);
  Storage.set('qingzhou_bookmarks', bookmarks);
  renderBookmarks();
  showToast('已删除');
};

// ── Voice Preset Selection ──
function renderVoicePresets() {
  const host=document.getElementById('voicePresetRow');if(!host)return;host.replaceChildren();
  const presets=typeof VOICE_PRESETS!=='undefined'?VOICE_PRESETS:[],current=Storage.get('qingzhou_voicePreset')||'female_sweet';
  const select=document.createElement('select');select.setAttribute('aria-label','选择语音音色');
  for(const preset of presets){const option=document.createElement('option');option.value=preset.id;option.textContent=window.NavrenI18n?.t(preset.name)||preset.name;select.append(option);}select.value=current;select.onchange=()=>selectVoice(select.value);
  const preview=document.createElement('button');preview.type='button';preview.className='p-btn';preview.textContent=window.NavrenI18n?.t('试听')||'试听';preview.setAttribute('aria-label','试听当前音色');preview.onclick=()=>previewVoice(select.value);host.append(select,preview);
}

function selectVoice(id) {
  Storage.set('qingzhou_voicePreset', id);
  renderVoicePresets();
  showToast('语音音色已切换');
}

// 试听与聊天共用相同的百炼音色和取消逻辑。
function previewVoice(id) {
  const preset = VOICE_PRESETS.find(voice => voice.id === id) || VOICE_PRESETS[0];
  const rate = getInterfaceMode() === 'senior' ? 0.85 : preset.rate;
  showToast('正在生成试听，点击其他音色可切换。');
  NavrenSpeech.speak('您好，我是 Navren，可以帮您看懂账单、比较产品和安排资金。理财有风险，请仔细阅读产品说明。',
    { voice: preset.id, rate, purpose: 'voice_preview' }).catch(error => {
      if (error.name !== 'AbortError') showToast('试听未完成：' + error.message);
    });
}

// 编辑弹层只提供临时填写；键盘可以关闭并在各控件间循环。
document.addEventListener('keydown',event=>{
  const overlay=document.getElementById('modalOverlay');if(!overlay||overlay.classList.contains('hidden'))return;
  if(event.key==='Escape'){event.preventDefault();closeModal();document.querySelector('[data-view-heading]')?.focus();return;}
  if(event.key!=='Tab')return;const items=[...overlay.querySelectorAll('button,input,select,textarea,a[href]')].filter(x=>!x.disabled&&!x.hidden);if(!items.length)return;const first=items[0],last=items.at(-1);
  if(event.shiftKey&&(document.activeElement===first||!overlay.contains(document.activeElement))){event.preventDefault();last.focus();}else if(!event.shiftKey&&(document.activeElement===last||!overlay.contains(document.activeElement))){event.preventDefault();first.focus();}
});
