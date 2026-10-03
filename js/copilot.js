/**
 * Navren — copilot.html 理财经理 AI Copilot
 * 逆向赋能：为一线客户经理提供客户洞察 + 推荐话术 + 合规提示
 */

document.addEventListener('DOMContentLoaded', () => {
  renderClientProfile();
  renderTalkingPoints();
  renderProductHighlights();
  renderHandoffContext();
  renderBriefing();
  renderComplianceNotes();
});

// ── 客户画像摘要 ──
function renderClientProfile() {
  const el = document.getElementById('clientProfile');
  if (!el) return;

  const profile = assembleProfile();
  const userInfo = Storage.get('qingzhou_userInfo') || {};
  const risk = profile.risk;
  const finance = profile.finance;
  const stages = Storage.get('qingzhou_lifeStages') || [];

  const items = [];
  if (userInfo.nickname) items.push({ label: '客户', value: userInfo.nickname });
  if (risk) items.push({ label: '风险等级', value: `${risk.level} ${risk.label}（${risk.score}/54）` });
  if (finance.amount) items.push({ label: '可投金额', value: finance.amount >= 10000 ? (finance.amount/10000).toFixed(0)+' 万' : finance.amount+' 元' });
  if (finance.horizon) items.push({ label: '投资期限', value: finance.horizon });
  if (finance.goal) items.push({ label: '投资目标', value: finance.goal });
  if (finance.income) items.push({ label: '收入来源', value: finance.income });
  if (stages.length > 0) items.push({ label: '人生阶段', value: stages.map(s=>s.label).join('、'), highlight: true });

  if (items.length === 0) {
    el.innerHTML = '<div style="color:var(--ink-40);font-size:var(--font-size-sm)">暂无客户画像数据。引导客户完成风险评估和基本档案填写。</div>';
    return;
  }

  el.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px">
      ${items.map(i => `
        <div>
          <div style="font-size:var(--font-size-xs);color:var(--ink-40);margin-bottom:3px">${i.label}</div>
          <div style="font-size:var(--font-size-base);font-weight:600;${i.highlight ? 'color:var(--gold-dark)' : ''}">${i.value}</div>
        </div>
      `).join('')}
    </div>
  `;
}

// ── 对话洞察 + 推荐话术 ──
function renderTalkingPoints() {
  const el = document.getElementById('talkingPoints');
  if (!el) return;

  const profile = assembleProfile();
  const keyMoments = Storage.getKeyMoments();
  const stages = Storage.get('qingzhou_lifeStages') || [];
  const points = [];

  // 从人生阶段生成话术
  stages.forEach(s => {
    points.push({ type: '洞察', text: `客户处于「${s.label}」阶段，建议话术方向：${s.advice}` });
  });

  // 从关键节点生成话术
  const recent = keyMoments.slice(-3);
  recent.forEach(m => {
    points.push({ type: '节点', text: m.event });
  });

  // 风险评估话术
  if (profile.risk) {
    points.push({
      type: '话术',
      text: `推荐话术："根据您${profile.risk.level}${profile.risk.label}的风险评估结果，可承受最大回撤${profile.risk.maxDrawdown}，我为您筛选了匹配的产品。"`
    });
  }

  // 金额话术
  if (profile.finance.amount) {
    const amount = profile.finance.amount >= 10000 ? (profile.finance.amount/10000).toFixed(0)+'万' : profile.finance.amount+'元';
    points.push({
      type: '话术',
      text: `推荐话术："您有${amount}可投资金，我建议采用'固收打底+权益增强'的策略，分散配置能有效控制回撤。"`
    });
  }

  if (points.length === 0) {
    el.innerHTML = '<div style="color:var(--ink-40);font-size:var(--font-size-sm)">暂无对话数据。建议引导客户多与 Navren 对话，AI 会自动提取洞察。</div>';
    return;
  }

  el.innerHTML = points.map(p => `
    <div style="padding:8px 0;border-bottom:1px solid var(--border);display:flex;gap:10px">
      <span style="font-size:var(--font-size-xs);font-weight:700;color:${p.type==='话术'?'var(--gold-dark)':p.type==='洞察'?'var(--ink)':'var(--ink-40)'};flex-shrink:0;min-width:32px">[${p.type}]</span>
      <span style="font-size:var(--font-size-sm);color:var(--ink-70);line-height:1.6">${p.text}</span>
    </div>
  `).join('');
}

// ── 产品推荐亮点 ──
function renderProductHighlights() {
  const el = document.getElementById('productHighlights');
  if (!el) return;

  const allocation = Storage.get('qingzhou_allocation');
  if (!allocation || !allocation.allocation) {
    el.innerHTML = '<div style="text-align:center;padding:var(--s4);color:var(--ink-40);font-size:var(--font-size-sm)">Navren 尚未为客户生成推荐方案</div>';
    return;
  }

  el.innerHTML = allocation.allocation.map(a => `
    <div style="padding:12px 0;border-bottom:1px solid var(--border)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
        <span style="font-weight:700;font-size:var(--font-size-sm)">${a.name}</span>
        <span style="font-size:var(--font-size-xs);color:var(--ink-40)">配置 ${a.ratio}%</span>
      </div>
      <div style="font-size:var(--font-size-xs);color:var(--ink-40);line-height:1.6">
        <strong>亮点</strong>：${a.risk || 'R3'} 等级，适配客户风险偏好。
        建议话术："这只产品${a.ratio}%的配置比例是基于您的${allocation.based_on?.risk_level || '风险'}评估结果。"
      </div>
    </div>
  `).join('');
}

// ── Handoff 上下文 ──
function renderHandoffContext() {
  const pkg = Storage.get('qingzhou_handoffPackage');
  if (!pkg) return;

  // 在 talkingPoints 下方追加
  const tp = document.getElementById('productHighlights');
  if (!tp) return;
  const section = tp.closest('.mine-section');
  if (!section) return;

  const div = document.createElement('div');
  div.className = 'mine-section';
  div.innerHTML = `
    <div class="sec-label">最近人工转接记录</div>
    <div class="rec-current">
      <div style="font-size:var(--font-size-xs);color:var(--ink-40);margin-bottom:8px">${pkg.timestamp?.slice(0,16) || ''} · 原因：${pkg.reason || '未指定'}</div>
      <div style="font-size:var(--font-size-sm);color:var(--ink-70);line-height:2">
        ${(pkg.syncedItems || []).map(i => '<div>' + i + '</div>').join('')}
        <div style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border)">💬 ${pkg.recentChat?.replace(/<br>/g, ' | ') || '无'}</div>
        <div>📊 ${pkg.productCtx || '无'}</div>
        <div>🏷️ ${pkg.stageLabel || '无'}</div>
      </div>
    </div>
  `;
  section.parentNode.insertBefore(div, section.nextSibling);
}

// ── 客户简报生成器 ──
function renderBriefing() {
  const profile = assembleProfile();
  const risk = profile.risk;
  const finance = profile.finance;
  const stages = Storage.get('qingzhou_lifeStages') || [];
  const allocation = Storage.get('qingzhou_allocation');
  const keyMoments = Storage.getKeyMoments();
  const handoff = Storage.get('qingzhou_handoffPackage');


  const lines = [];
  lines.push('【客户简报】' + (Storage.get('qingzhou_userInfo')?.nickname || '客户'));
  lines.push('');

  // 画像
  if (risk) lines.push(`风险等级：${risk.level} ${risk.label}（${risk.score}/54）`);
  if (finance.amount) lines.push(`可投金额：${finance.amount >= 10000 ? (finance.amount/10000).toFixed(0)+'万' : finance.amount+'元'}`);
  if (finance.horizon) lines.push(`投资期限：${finance.horizon}`);
  if (finance.goal) lines.push(`投资目标：${finance.goal}`);
  if (stages.length) lines.push(`人生阶段：${stages.map(s=>s.label).join('、')}`);

  // 推荐方案
  if (allocation?.allocation) {
    lines.push('');
    lines.push('【推荐方案】');
    allocation.allocation.forEach(a => lines.push(`  ${a.name} · ${a.ratio}% · ${a.risk || 'R3'}`));
  }

  // 合规提醒
  lines.push('');
  lines.push('【合规须知】');
  lines.push('  禁止使用"保本""稳赚"等承诺用语');
  lines.push('  必须声明"理财非存款，产品有风险，投资须谨慎"');
  if (risk) lines.push(`  推荐产品风险等级不得超过 ${risk.level}+1`);
  lines.push('');
  lines.push(`生成时间：${new Date().toLocaleString('zh-CN')}`);

  const briefing = lines.join('\n');

  const el = document.getElementById('productHighlights');
  if (!el) return;
  const section = el.closest('.mine-section');
  if (!section) return;

  const div = document.createElement('div');
  div.className = 'mine-section';
  div.innerHTML = `
    <div class="sec-label">客户简报</div>
    <div class="card">
      <pre style="font-size:var(--font-size-sm);color:var(--ink-70);line-height:1.8;white-space:pre-wrap;font-family:var(--body);max-height:300px;overflow-y:auto;margin-bottom:12px">${briefing}</pre>
      <button class="btn-outline" style="padding:6px 16px;font-size:var(--font-size-xs)" onclick="copyBriefing()">复制简报</button>
    </div>
  `;
  section.parentNode.insertBefore(div, section.nextSibling);

  window._briefingText = briefing;
}

window.copyBriefing = function() {
  if (window._briefingText) {
    navigator.clipboard.writeText(window._briefingText).then(() => {
      alert('简报已复制到剪贴板');
    }).catch(() => {
      alert('复制失败，请手动选择文本');
    });
  }
};

// ── 合规提示 ──
function renderComplianceNotes() {
  const el = document.getElementById('complianceNotes');
  if (!el) return;

  const profile = assembleProfile();
  const risk = profile.risk;

  const notes = [
    '⚠️ 必须向客户明确说明：理财非存款，产品有风险，投资须谨慎',
    '⚠️ 推荐产品的风险等级不得超过客户评估等级 +1',
    '⚠️ 禁止使用"保本""稳赚""绝对安全"等承诺性用语',
    '⚠️ 收益描述只能使用"业绩比较基准"或"历史年化区间"'
  ];

  if (risk) {
    notes.push(`⚠️ 该客户风险等级为 ${risk.level}，最大建议权益仓位 ${parseInt(risk.maxEquityRatio*100)}%。推荐的权益类产品占比不得超过此限。`);
  }

  el.innerHTML = `
    <div style="background:#FFF5F5;padding:14px;border-radius:var(--r-sm);border:1px solid #FDD">
      <div style="font-size:var(--font-size-xs);font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#C53030;margin-bottom:10px">合规红线 · 必须遵守</div>
      ${notes.map(n => `<div style="font-size:var(--font-size-sm);color:#742A2A;line-height:1.8;margin-bottom:4px">${n}</div>`).join('')}
    </div>
  `;
}
