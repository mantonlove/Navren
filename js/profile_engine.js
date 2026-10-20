/**
 * Navren — 画像与资产配置引擎
 * assembleProfile / getTemplate / handleProfileUpdate
 */

const ALLOCATION_TEMPLATES = {
  R1: { cash: 0.80, bond: 0.20, plus: 0, equity: 0, theme: 0, expectedReturn: '1.5%-2.5%', maxDrawdown: '<0.5%' },
  R2: { cash: 0.20, bond: 0.60, plus: 0.15, equity: 0.05, theme: 0, expectedReturn: '2.5%-3.5%', maxDrawdown: '<1.5%' },
  R3: { cash: 0.10, bond: 0.40, plus: 0.25, equity: 0.20, theme: 0.05, expectedReturn: '3.5%-5.5%', maxDrawdown: '<5%' },
  R4: { cash: 0.05, bond: 0.20, plus: 0.20, equity: 0.40, theme: 0.15, expectedReturn: '5.0%-8.0%', maxDrawdown: '<15%' },
  R5: { cash: 0.05, bond: 0.10, plus: 0.10, equity: 0.40, theme: 0.35, expectedReturn: '7.0%-12.0%', maxDrawdown: '<30%' }
};

function assembleProfile() {
  const risk = Storage.get('qingzhou_riskProfile');
  const userInfo = window.NavrenPersonalProfile?.current?.();
  const profile = Storage.get('qingzhou_userProfile');
  const preferences = Storage.get('qingzhou_preferences');
  const allocation = Storage.get('qingzhou_allocation');

  return {
    risk: risk ? {
      level: risk.level,
      score: risk.score,
      label: risk.label,
      maxDrawdown: risk.maxDrawdown,
      maxEquityRatio: risk.maxEquityRatio,
      assessedAt: risk.assessedAt
    } : null,

    finance: {
      amount: profile?.amount || null,
      horizon: profile?.horizon || null,
      income: profile?.income || null,
      goal: profile?.goal || null,
      interests: profile?.interests || []
    },

    account: userInfo ? {
      nickname: userInfo.nickname
    } : null,

    preferences: preferences || DEFAULTS[STORAGE_KEYS.PREFERENCES],

    allocation: allocation,

    isComplete() {
      return !!(this.risk && this.finance.amount && this.finance.horizon);
    },

    missingFields() {
      const m = [];
      if (!this.risk) m.push('风险评估');
      if (!this.finance.amount) m.push('可投金额');
      if (!this.finance.horizon) m.push('投资期限');
      if (!this.finance.goal) m.push('投资目标');
      return m;
    },

    getTemplate() {
      if (!this.risk) return ALLOCATION_TEMPLATES['R3'];
      return ALLOCATION_TEMPLATES[this.risk.level] || ALLOCATION_TEMPLATES['R3'];
    }
  };
}



/** 一键生成结构化资产配置建议书 */
function generateAllocationReport() {
  const profile = assembleProfile();
  const allocation = Storage.get('qingzhou_allocation');
  const risk = profile.risk;
  const finance = profile.finance;

  if (!risk || !finance.amount) return null;

  const template = profile.getTemplate();
  const holdings = Storage.get('qingzhou_holdings') || [];
  const stages = Storage.get('qingzhou_lifeStages') || [];

  let report = '';

  // 一、客户画像
  report += '一、客户画像\n';
  report += `风险等级：${risk.level} ${risk.label}（评分 ${risk.score}/54）\n`;
  report += `可投金额：${finance.amount >= 10000 ? (finance.amount/10000).toFixed(0)+'万' : finance.amount+'元'}\n`;
  if (finance.horizon) report += `投资期限：${finance.horizon}\n`;
  if (finance.goal) report += `投资目标：${finance.goal}\n`;
  if (stages.length) report += `人生阶段：${stages.map(s=>s.label).join('、')}\n`;

  // 二、配置方案
  report += '\n二、推荐配置方案\n';
  if (allocation?.allocation) {
    allocation.allocation.forEach(a => {
      const underlying = typeof getUnderlyingAssets === 'function' ? getUnderlyingAssets(a.name) : null;
      report += `【${a.name}】配置 ${a.ratio}%\n`;
      report += `  风险等级：${a.risk || 'R3'}\n`;
      if (underlying) {
        report += `  底层资产：${underlying.assets.join('、')}\n`;
        if (underlying.risk_note) report += `  风险提示：${underlying.risk_note}\n`;
      }
    });
  }

  // 三、预期表现
  report += '\n三、预期表现（基于历史回测模拟）\n';
  report += `预期年化区间：${template.expectedReturn}\n`;
  report += `最大回撤：${template.maxDrawdown}\n`;
  report += `建议权益仓位：≤${parseInt(risk.maxEquityRatio*100)}%\n`;

  // 四、当前持仓诊断
  if (holdings.length > 0) {
    report += '\n四、当前持仓诊断\n';
    holdings.forEach(h => {
      const match = allocation?.allocation?.find(a => a.name === h.name);
      if (match) {
        report += `✅ ${h.name}：已在推荐方案中，配置 ${match.ratio}%，当前持有 ${h.amount} 元${h.amount > match.ratio / 100 * finance.amount ? '（偏高，建议调整）' : '（匹配）'}\n`;
      } else {
        report += `⚠️ ${h.name}：不在本次推荐方案中，建议评估是否保留\n`;
      }
    });
  }

  // 五、合规声明
  report += '\n五、合规声明\n';
  report += '⚠️ 理财非存款，产品有风险，投资须谨慎。\n';
  report += '以上为参考建议，历史回测不代表未来收益。\n';
  report += `生成时间：${new Date().toLocaleString('zh-CN')}\n`;

  return report;
}

/** 推荐白盒解释：生成「为什么推荐这个」的可审计说明 */
function buildExplanation(products = []) {
  const profile = assembleProfile();
  const risk = profile.risk;
  const finance = profile.finance;
  const reasons = [];

  if (risk?.level) {
    reasons.push(`您的风险等级为 <strong>${risk.level} ${risk.label}</strong>${risk.score ? '（评分 ' + risk.score + '/54）' : ''}，可承受最大回撤 ${risk.maxDrawdown}`);
  }
  if (finance?.horizon) {
    reasons.push(`投资期限 <strong>${finance.horizon}</strong>`);
  }
  if (finance?.amount) {
    reasons.push(`可投金额约 <strong>${finance.amount >= 10000 ? (finance.amount / 10000).toFixed(0) + ' 万' : finance.amount + ' 元'}</strong>`);
  }
  if (finance?.goal) {
    reasons.push(`投资目标为 <strong>${finance.goal}</strong>`);
  }

  const productNotes = [];
  products.forEach(p => {
    const riskOk = risk?.level && p.risk_level ?
      parseInt(p.risk_level.replace('R','')) <= parseInt(risk.level.replace('R','')) + 1 : true;
    const lockOk = finance?.horizon ?
      (p.lock_period || '').includes('天') || (p.lock_period || '') === '—' : true;
    productNotes.push(`${p.name}（${p.risk_level}${riskOk ? ' ✅风险匹配' : ''}${lockOk ? ' ✅期限合适' : ''}）`);
  });

  return {
    summary: reasons.join(' · '),
    products: productNotes,
    timestamp: new Date().toISOString()
  };
}



function mergeProfileField(field, newValue, source, confidence) {
  const history = Storage.getProfileHistory();
  const fieldHistory = history.filter(h => h.field === field);
  const lastRecord = fieldHistory[fieldHistory.length - 1];

  if (lastRecord?.source === 'manual_edit' && source === 'chat_extraction') {
    return { merged: false, reason: 'manual_edit_protected' };
  }
  if (lastRecord?.newValue === newValue) {
    return { merged: false, reason: 'unchanged' };
  }
  if (confidence < 0.6) {
    return { merged: true, confirmed: false, needsConfirm: true };
  }
  return { merged: true, confirmed: source !== 'chat_extraction' };
}

function handleProfileUpdate(updates) {
  let changed = false;
  const needsConfirm = [];

  for (const update of updates) {
    const result = mergeProfileField(update.field, update.value, 'chat_extraction', update.confidence);

    if (result.merged) {
      changed = true;
      const profile = Storage.get('qingzhou_userProfile') || {};
      profile[update.field] = update.value;
      Storage.set('qingzhou_userProfile', profile);

      Storage.addProfileHistory({
        field: update.field,
        oldValue: profile[update.field] || null,
        newValue: update.value,
        source: 'chat_extraction',
        timestamp: new Date().toISOString(),
        confidence: update.confidence,
        context: update.evidence || '',
        confirmed: result.confirmed
      });

      if (result.needsConfirm) {
        needsConfirm.push(`${update.field}=${update.value}`);
      }
    }
  }

  if (needsConfirm.length > 0) {
    return { toast: `Navren猜测: ${needsConfirm.join('、')}，对吗？` };
  }

  if (changed) {
    Storage.set('qingzhou_chatHistory', []);
    if (typeof showToast === 'function') showToast('档案已更新，对话上下文已刷新');
    return { toast: `已记录: ${updates.map(u => u.field + '=' + u.value).join('、')}` };
  }
  return null;
}

// 画像变更时清空对话上下文，强制后续对话以新数据为准
// 风险评估完成、mine.html 编辑档案时都调用此函数
/** 全生命周期检测：自动识别用户人生节点 */
function detectLifeEvent() {
  const profile = Storage.get('qingzhou_userProfile') || {};
  const history = Storage.getProfileHistory();
  const events = [];

  // 规则1: 新晋家庭 — 档案中出现子女教育/母婴相关
  if (profile.goal && /教育|子女|孩子|上学|幼儿园/.test(profile.goal)) {
    events.push({ stage: 'new_family', label: '新晋家庭', advice: '转向稳健医疗险+长期教育金规划', priority: 'high' });
  }

  // 规则2: 财富上升期 — 连续3条档案更新显示资金增长
  const amountChanges = history.filter(h => h.field === 'amount' && h.newValue);
  if (amountChanges.length >= 2) {
    const latest = amountChanges[amountChanges.length - 1];
    const previous = amountChanges[amountChanges.length - 2];
    if (latest.newValue > previous.newValue * 1.3) {
      events.push({ stage: 'wealth_growth', label: '财富上升期', advice: '建议增配权益类，适度增加风险敞口', priority: 'medium' });
    }
  }

  // 规则3: 退休规划 — 档案中年龄>50或出现养老关键词
  if (profile.age && /[5-9][0-9]/.test(profile.age)) {
    events.push({ stage: 'retirement', label: '退休规划期', advice: '转向保守配置，注重现金流和本金安全', priority: 'high' });
  }
  if (profile.goal && /养老|退休|养老金/.test(profile.goal)) {
    events.push({ stage: 'retirement', label: '退休规划期', advice: '转向保守配置，注重现金流和本金安全', priority: 'high' });
  }

  // 规则4: 购房计划 — 出现大额支出+期限缩短
  if (profile.horizon && /1年|短期|随时/.test(profile.horizon) && profile.amount && profile.amount > 500000) {
    events.push({ stage: 'home_purchase', label: '购房筹备期', advice: '推荐短期高流动性产品，避免锁定长期资金', priority: 'high' });
  }

  // 去重并保存
  const existing = Storage.get('qingzhou_lifeStages') || [];
  const newEvents = events.filter(e => !existing.find(x => x.stage === e.stage));
  if (newEvents.length > 0) {
    Storage.set('qingzhou_lifeStages', [...existing, ...newEvents]);
    newEvents.forEach(e => {
      Storage.addKeyMoment(`人生节点检测：${e.label} → ${e.advice}`);
    });
  }

  return events;
}

function onProfileChanged() {
  Storage.set('qingzhou_chatHistory', []);
  detectLifeEvent(); // 档案变更时自动检测人生节点
}


/** 产品底层资产穿透数据（模拟银行投研系统） */
const UNDERLYING_ASSETS = {
  '安鑫短债': { assets: ['国债', 'AAA级同业存单', '高等级信用债'], risk_tags: [], risk_note: '' },
  '稳享固收增强': { assets: ['国债', 'AAA级信用债', '可转债(≤10%)'], risk_tags: [], risk_note: '' },
  '沪深300指数增强': { assets: ['沪深300成分股', '股指期货(对冲用)'], risk_tags: ['权益类'], risk_note: '含权益敞口，市场下跌时可能产生较大回撤' },
  '活期盈': { assets: ['国债逆回购', '银行同业存款'], risk_tags: [], risk_note: '' },
  '新能源行业精选': { assets: ['新能源产业链股票', '光伏/锂电/储能'], risk_tags: ['行业集中', '权益类'], risk_note: '行业集中度高，受新能源政策影响大' },
  '科技主题混合': { assets: ['半导体', 'AI/大模型', '消费电子'], risk_tags: ['行业集中', '权益类'], risk_note: '科技板块波动较大，建议控制仓位' },
  '全球配置': { assets: ['美股ETF', '港股通', '发达市场债券'], risk_tags: ['汇率风险', '跨境'], risk_note: '含跨境投资，受汇率波动影响' },
  '大额存单': { assets: ['银行存款'], risk_tags: [], risk_note: '受存款保险条例保护，50万以内本息有保障' },
  '天利同业存单': { assets: ['AAA级同业存单'], risk_tags: [], risk_note: '' }
};

/** 查询产品底层资产 */
function getUnderlyingAssets(productName) {
  for (const [key, val] of Object.entries(UNDERLYING_ASSETS)) {
    if (productName.includes(key)) return val;
  }
  return null;
}

