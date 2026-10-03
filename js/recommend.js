/**
 * Navren — recommend.html 推荐中心页面逻辑
 */

let currentMode = 'classic';

document.addEventListener('DOMContentLoaded', () => {
  const params = new URLSearchParams(window.location.search);
  currentMode = getInterfaceMode();
  document.body.className = 'mode-' + currentMode;
  // mode badge removed from header

  // 应用字体
  const savedFontSize = getModeFont(currentMode);
  applyFontSize(savedFontSize);

  if (typeof showContextualTip === 'function') showContextualTip('recommend');
  renderRecommendations();
});

function goTo(url) {
  NavrenApp.navigate(url, currentMode);
}

function renderRecommendations() {
  const allocation = Storage.get('qingzhou_allocation');
  const emptyState = document.getElementById('emptyState');
  const recContent = document.getElementById('recContent');

  // 市场热点和周报始终显示
  renderMarketHot();
  renderWeeklyReport();
  renderMotEducation();
  renderKypAlignment();

  if (!allocation || !allocation.allocation || allocation.allocation.length === 0) {
    emptyState.classList.remove('hidden');
    recContent.classList.add('hidden');
    // Enhance empty state with guidance
    const emptyDiv = document.getElementById('emptyState');
    if (emptyDiv) {
      const riskP = Storage.get('qingzhou_riskProfile');
      const userP = Storage.get('qingzhou_userProfile') || {};
      const prog = [!!riskP?.level, !!userP.amount, !!userP.horizon].filter(Boolean).length;
      const guides = [
        ['去聊天测风险偏好', '在聊天中完成风险评估后，Navren 才能为您生成千人千面的配置方案。'],
        ['去聊天补充信息', '已完成风险评估，补充金额和期限后即可生成专属方案。'],
        ['去聊天完成画像', '还差一步——告诉 Navren您的投资期限。'],
        ['去聊天获取方案', '画像已齐全，直接让 Navren为您配置即可。']
      ];
      const g = guides[Math.min(prog, 3)];
      const bar = prog < 3 ? '<div style="margin-bottom:12px"><span style="display:inline-block;width:'+(prog*33)+'%;height:4px;background:var(--gold);border-radius:2px"></span><span style="display:inline-block;width:'+(100-prog*33)+'%;height:4px;background:var(--ink-15);border-radius:2px"></span></div><div style="font-size:var(--font-size-xs);color:var(--ink-40);margin-bottom:12px">画像完整度 '+prog+'/3</div>' : '';
      emptyDiv.replaceChildren();
      const referenceNote = document.createElement('p');
      referenceNote.textContent = '暂无市场研究参考方案。模拟银行产品与申购请使用上方目录。';
      emptyDiv.append(referenceNote);
    }
    return;
  }

  emptyState.classList.add('hidden');
  recContent.classList.remove('hidden');

  // Render current recommendation
  const colors = ['#FF8C42', '#6C5CE7', '#10B981', '#F59E0B', '#3B82F6', '#EC4899'];
  const pieSegments = allocation.allocation.map((a, i) => {
    const prev = allocation.allocation.slice(0, i).reduce((s, x) => s + x.ratio, 0);
    return `${colors[i]} ${prev}% ${prev + a.ratio}%`;
  }).join(', ');

  document.getElementById('currentRec').innerHTML = `
    <div style="display:flex;gap:var(--s3);align-items:flex-start;margin-bottom:var(--s3)">
      <div style="width:100px;height:100px;border-radius:50%;background:conic-gradient(${pieSegments});flex-shrink:0"></div>
      <div style="flex:1">
        <div style="font-family:var(--display);font-size:var(--font-size-base);font-weight:600;margin-bottom:4px">${allocation.summary ? allocation.summary.slice(0,20) : '推荐配置方案'}</div>
        <div style="font-size:var(--font-size-xs);color:var(--ink-40);margin-bottom:12px">${allocation.generated_at ? new Date(allocation.generated_at).toLocaleDateString('zh-CN') : ''} · 基于 ${allocation.based_on?.risk_level || 'R3'}</div>
        <div style="font-size:var(--font-size-xs);line-height:2;margin-bottom:8px">
          ${allocation.allocation.map((a,i) => `<div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${colors[i]};margin-right:6px"></span>${a.name} · ${a.ratio}%</div>`).join('')}
        </div>
      </div>
    </div>
    <div class="product-grid">
      ${allocation.allocation.map((a,i) => {
        return `
        <div class="product-card">
          <div class="pc-header">
            <span class="pc-name">${a.name || '产品'}</span>
            <span class="risk-badge risk-r${(a.risk||'R3').replace('R','')}">${a.risk || 'R3'}</span>
          </div>
          <div class="pc-actions">
            <span class="pc-detail" onclick="showProductDetail('${(a.name||'').replace(/'/g,"\\'")}')">查看详情 →</span>
            <span class="pc-send" data-product="${(a.name||'')}" onclick="event.stopPropagation();sendToChat(this.getAttribute('data-product'))">发送至聊天 ↗</span>
          </div>
        </div>
      `}).join('')}
    </div>
    <div style="text-align:center;font-size:var(--font-size-xs);color:var(--ink-40);margin-top:var(--s2);padding-top:var(--s2);border-top:1px solid var(--border)">
      ⚠️ 以上为参考建议，历史回测不代表未来收益。投资需谨慎。
    </div>
  `;

  // Render history
  const history = (allocation._history || []).slice(-5).reverse();
  document.getElementById('historyList').innerHTML = history.length === 0
    ? '<div style="font-size:var(--font-size-xs);color:var(--ink-40);padding:8px">暂无历史推荐</div>'
    : history.map(h => `
        <div style="padding:12px;background:var(--surface);border-radius:var(--r-sm);margin-bottom:8px;cursor:pointer">
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-weight:700;font-size:var(--font-size-sm)">${h.summary || '历史方案'}</span>
            <span style="font-size:var(--font-size-xs);color:var(--ink-40)">${h.generated_at ? new Date(h.generated_at).toLocaleDateString('zh-CN') : ''}</span>
          </div>
          <div style="font-size:var(--font-size-xs);color:var(--ink-40)">${(h.allocation||[]).map(a=>a.name+' '+a.ratio+'%').join(' · ') || ''}</div>
        </div>
      `).join('');
}

function renderMarketHot() {
  const el = document.getElementById('marketHot');
  if (!el) return;
  const section = el.closest('.mine-section');

  // 市场热点作为核心模块，默认显示；用户设置仅控制推送通知
  if (section) section.classList.remove('hidden');

  // 读取新闻池（优先 MARKET_NEWS，回退 MARKET_DATA）
  const newsData = typeof MARKET_NEWS !== 'undefined' ? MARKET_NEWS : null;
  const marketData = typeof MARKET_DATA !== 'undefined' ? MARKET_DATA : null;

  // 无新闻池时 fallback 到旧版 MARKET_DATA 摘要
  if (!newsData || !newsData.news || newsData.news.length === 0) {
    if (marketData && marketData.market_brief) {
      el.innerHTML = `<div style="padding:14px;font-size:var(--font-size-sm);color:var(--ink-40);line-height:1.8">
        <div style="font-weight:600;color:var(--ink);margin-bottom:8px">市场摘要</div>
        ${escapeHtml(marketData.market_brief.slice(0,200))}
        <div style="margin-top:8px;font-size:var(--font-size-xs);color:var(--ink-30)">更新于 ${(marketData.update_time||'').slice(0,10)}</div>
        <div style="margin-top:4px;color:var(--ink-30)">市场数据仅供参考，不构成投资建议。</div>
      </div>`;
    } else {
      el.innerHTML = '<div style="padding:14px;font-size:var(--font-size-sm);color:var(--ink-40)">今日热点暂未更新，请稍后再试。</div>';
    }
    return;
  }

  // 检查新闻池时效性
  const updateTime = new Date(newsData.update_time);
  const hoursSince = (Date.now() - updateTime.getTime()) / (1000 * 60 * 60);
  const isStale = hoursSince > 24;

  // ── 收集用户上下文用于个性化排序 ──
  const riskP = Storage.get('qingzhou_riskProfile');
  const riskLevel = riskP?.level || 'R3';
  const rn = parseInt(riskLevel.replace('R', '')) || 3;
  const userProfile = Storage.get('qingzhou_userProfile') || {};
  const holdings = Storage.get('qingzhou_holdings') || [];
  const allocation = Storage.get('qingzhou_allocation');
  const allocProducts = allocation?.allocation || [];
  const chatHistory = Storage.get('qingzhou_chatHistory') || [];
  const userInterests = userProfile.interests || [];
  const prefs = Storage.get('qingzhou_preferences') || {};
  const currentMode = Storage.get('qingzhou_mode') || 'classic';

  // 从最近聊天提取关键词
  const recentChat = chatHistory.slice(-6).map(m => m.content || '').join(' ');
  const chatKeywords = [];
  if (/年化|收益|回报/.test(recentChat)) chatKeywords.push('收益');
  if (/市场|行情|走势/.test(recentChat)) chatKeywords.push('市场');
  if (/债券|债基|固收/.test(recentChat)) chatKeywords.push('债券');
  if (/股票|权益|指数|ETF/.test(recentChat)) chatKeywords.push('权益');
  if (/黄金|金价/.test(recentChat)) chatKeywords.push('黄金');
  if (/汇率|美元|人民币/.test(recentChat)) chatKeywords.push('汇率');
  if (/养老|退休/.test(recentChat)) chatKeywords.push('养老');

  // 持仓/方案中的资产类型
  const holdingAssets = new Set();
  allocProducts.forEach(p => {
    const underlying = (p.underlying || '').toString();
    if (/债|固收|信用/.test(underlying)) holdingAssets.add('bond');
    if (/权益|股票|指数/.test(underlying)) holdingAssets.add('equity');
    if (/货[币币]|现金|存款/.test(underlying)) holdingAssets.add('cash');
  });
  // 从持仓名称推测
  holdings.forEach(h => {
    const name = (h.name || '') + (h.category || '');
    if (/债|固收|信用/.test(name)) holdingAssets.add('bond');
    if (/股|权益|指数|ETF/.test(name)) holdingAssets.add('equity');
    if (/黄金|金/.test(name)) holdingAssets.add('gold');
    if (/货[币币]|现金|存款/.test(name)) holdingAssets.add('cash');
  });

  // ── 个性化打分 ──
  const scored = newsData.news.map((item, idx) => {
    let score = 0;
    const reasons = [];

    // 1. 时效性：当天+5，近3天+3，近7天+1
    const daysAgo = (Date.now() - new Date(item.published_at).getTime()) / (1000*60*60*24);
    if (daysAgo < 1) score += 5;
    else if (daysAgo < 3) score += 3;
    else if (daysAgo < 7) score += 1;

    // 2. 资产相关性
    const assets = item.related_assets || [];
    if (holdingAssets.size > 0) {
      const overlap = assets.filter(a => holdingAssets.has(a));
      if (overlap.length > 0) {
        score += overlap.length * 4;
        reasons.push('与您的配置方案相关');
      }
    }

    // 3. 风险适配
    if (rn <= 2) { // 保守/稳健
      if (assets.some(a => ['bond', 'cash', 'wealth_product'].includes(a))) score += 3;
      if (item.category === '利率' || item.category === '监管' || item.category === '银行理财') score += 2;
    } else if (rn >= 4) { // 进取/激进
      if (assets.some(a => ['equity', 'fund'].includes(a))) score += 3;
      if (item.category === '权益' || item.category === '基金') score += 2;
    }

    // 4. 聊天相关性
    chatKeywords.forEach(kw => {
      const text = item.title + (item.summary||'') + (item.tags||[]).join(' ');
      if (text.includes(kw)) score += 3;
    });

    // 5. 影响级别
    if (item.impact_level === 'high') score += 3;
    else if (item.impact_level === 'medium') score += 1;

    // 6. 合规降权：标题党/过度预测
    if (/暴涨|暴跌|崩盘|翻倍|稳赚|抄底/.test(item.title)) score -= 5;

    return { ...item, idx, score, reasons, daysAgo };
  });

  scored.sort((a, b) => b.score - a.score);

  // ── 多样性：取前3条，避免全同类 ──
  const selected = [];
  const usedCategories = new Set();
  for (const item of scored) {
    if (selected.length >= 3) break;
    if (selected.length === 0) {
      selected.push(item);
      usedCategories.add(item.category);
    } else if (selected.length === 1) {
      // 第2条优先选不同类别
      if (!usedCategories.has(item.category) || item.score > selected[0].score * 0.7) {
        selected.push(item);
        usedCategories.add(item.category);
      }
    } else {
      // 第3条尽量与前两条不同
      if (usedCategories.size < 2 || !usedCategories.has(item.category)) {
        selected.push(item);
        usedCategories.add(item.category);
      }
    }
  }

  // ── 渲染 ──
  const timeDisplay = updateTime.toLocaleString('zh-CN', { month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit' });
  const staleWarning = isStale ? '<div style="margin-bottom:10px;padding:6px 12px;background:#FFF8F0;border-radius:var(--r-sm);font-size:var(--font-size-xs);color:#C04A1A">市场热点数据可能不是最新（更新于 ' + timeDisplay + '）</div>' : '';

  el.innerHTML = staleWarning + selected.map((item, n) => {
    const whyHTML = item.reasons.length > 0
      ? `<div style="margin-top:6px;font-size:var(--font-size-xs);color:var(--gold-dark)">📌 ${item.reasons.join(' · ')}</div>`
      : '';
    const riskHTML = item.risk_note
      ? `<div style="margin-top:6px;font-size:var(--font-size-xs);color:var(--ink-40)">⚠ ${escapeHtml(item.risk_note)}</div>`
      : '';

    return `
    <div class="market-news-item" style="padding:14px 0;border-bottom:1px solid var(--border);cursor:pointer;transition:all var(--transition);text-align:left"
         onmouseover="this.style.background='var(--surface-raised)'" onmouseout="this.style.background='none'">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
        <div style="flex:1;min-width:0">
          ${n===0 && item.score > 3 ? '<span style="font-size:var(--font-size-xs);color:var(--gold-dark);font-weight:700;margin-right:6px">为你推荐</span>' : ''}
          <span style="font-size:var(--font-size-sm);font-weight:600;color:var(--ink);line-height:1.5">${escapeHtml(item.title)}</span>
          <div style="margin-top:4px;font-size:var(--font-size-xs);color:var(--ink-40)">${escapeHtml(item.source)} · ${item.published_at || ''}</div>
        </div>
        <span style="font-size:var(--font-size-xs);color:var(--ink-30);flex-shrink:0">▸</span>
      </div>
      ${whyHTML}
      <div class="m-detail" style="display:none;margin-top:10px;padding:12px;background:var(--surface);border-radius:var(--r-sm);font-size:var(--font-size-sm);color:var(--ink-70);line-height:1.8">
        <div style="margin-bottom:6px">${escapeHtml(item.summary || '')}</div>
        <div style="margin-bottom:6px;padding:6px 10px;background:var(--gold-light);border-radius:4px;font-size:var(--font-size-xs);color:var(--gold-dark)">
          ${escapeHtml(item.why_it_matters || '')}
        </div>
        ${riskHTML}
        <div style="margin-top:6px;font-size:var(--font-size-xs);color:var(--ink-30)">${escapeHtml(item.compliance_note || '')}</div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="retry-btn market-news-discuss" style="padding:6px 14px;font-size:var(--font-size-xs)">
            问 Navren这对我有什么影响 ↗
          </button>
          <button class="retry-btn" style="padding:6px 14px;font-size:var(--font-size-xs)"
            onclick="event.stopPropagation();bookmarkNews('${item.id}')">⭐ 收藏</button>
        </div>
      </div>
    </div>`;
  }).join('') + `
  <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;font-size:var(--font-size-xs);color:var(--ink-30)">
    <span>更新于 ${timeDisplay} · ${newsData.source || '公开市场数据'}</span>
    <span style="cursor:pointer;text-decoration:underline" onclick="NavrenApp.navigate('chat.html?mode=${currentMode}&q=最近市场有什么值得关注的热点')">查看更多 →</span>
  </div>
  <div style="font-size:var(--font-size-xs);color:var(--ink-15);padding-bottom:8px">以上内容仅供参考，不构成投资建议。市场有风险，投资须谨慎。</div>`;

  // 标题通过 URL 参数传递，避免嵌入 onclick 时引号破坏脚本或属性。
  el.querySelectorAll('.market-news-discuss').forEach((button, index) => {
    button.addEventListener('click', event => {
      event.stopPropagation();
      const params = new URLSearchParams({
        mode: currentMode,
        q: '帮我分析这条市场热点对我的配置有什么影响：' + selected[index].title
      });
      NavrenApp.navigate('chat.html?' + params.toString());
    });
  });

  // 点击展开/收起详情
  el.querySelectorAll('.market-news-item').forEach(item => {
    item.onclick = function(e) {
      if (e.target.tagName === 'BUTTON' || e.target.tagName === 'A') return;
      const detail = this.querySelector('.m-detail');
      if (detail) detail.style.display = detail.style.display === 'none' ? 'block' : 'none';
    };
  });
}

/** 收藏市场热点新闻 */
function bookmarkNews(newsId) {
  const bookmarks = JSON.parse(localStorage.getItem('qingzhou_bookmarks') || '[]');
  const newsData = typeof MARKET_NEWS !== 'undefined' ? MARKET_NEWS : null;
  const item = newsData?.news?.find(n => n.id === newsId);
  if (!item) return;
  const exists = bookmarks.some(b => b.text === item.title);
  if (!exists) {
    bookmarks.push({ text: item.title, timestamp: new Date().toISOString(), category: 'market_news', id: newsId });
    localStorage.setItem('qingzhou_bookmarks', JSON.stringify(bookmarks.slice(-30)));
    if (typeof showToast === 'function') showToast('已收藏');
  } else {
    if (typeof showToast === 'function') showToast('已收藏过');
  }
}

function renderWeeklyReport() {
  const el = document.getElementById('weeklyReport');
  if (!el) return;
  const section = el.closest('.mine-section');
  const prefs = Storage.get('qingzhou_preferences') || {};
  if (!prefs.weeklyReport) {
    if (section) section.classList.add('hidden');
    return;
  }
  if (section) section.classList.remove('hidden');

  const marketData = typeof MARKET_DATA !== 'undefined' ? MARKET_DATA : null;

  el.innerHTML = `
    <div style="font-size:var(--font-size-sm);color:var(--ink-70);line-height:2">
      <div style="margin-bottom:12px">
        <div style="font-weight:700;color:var(--ink);margin-bottom:4px">宏观一览</div>
        <div>· LPR：1年期 3.10% / 5年期 3.60%（连续 6 月不变）</div>
        <div>· 国债 10Y：2.75%（周环比 -5bp）</div>
        <div>· 人民币汇率：7.24（周环比 -0.3%）</div>
      </div>
      <div style="margin-bottom:12px">
        <div style="font-weight:700;color:var(--ink);margin-bottom:4px">资产表现（本周）</div>
        <div>· 固收类：+0.12% · 权益类：+1.80% · 商品类：-0.45%</div>
      </div>
      <div style="margin-bottom:12px">
        <div style="font-weight:700;color:var(--ink);margin-bottom:4px">与你相关</div>
        <div>· 您关注的固收+产品本周表现稳健，组合预估 +0.3%</div>
        <div>· 指数增强类产品受益于权益反弹，周涨幅 +1.5%</div>
      </div>
      <div>
        <div style="font-weight:700;color:var(--ink);margin-bottom:4px">下周关注</div>
        <div>· 6月 LPR 报价窗口 · 美联储议息会议纪要发布</div>
      </div>
    </div>
    <div style="text-align:center;font-size:var(--font-size-xs);color:var(--ink-40);margin-top:var(--s2);padding-top:var(--s2);border-top:1px solid var(--border)">
      📅 周报 · ${marketData ? (marketData.update_time || '').slice(0,10) : '2026-05-24'} · 数据来源：央行/中债登/沪深交易所
    </div>
  `;
}

/** MoT 投资者教育：市场波动时主动解释原因 */
function renderMotEducation() {
  const marketData = typeof MARKET_DATA !== 'undefined' ? MARKET_DATA : null;
  if (!marketData || !marketData.key_indicators) return;

  const container = document.getElementById('weeklyReport');
  if (!container) return;
  const section = container.closest('.mine-section');
  if (!section) return;

  // 检测是否已有波动解读（存在 storage 中，同一天不重复）
  const lastMot = Storage.get('qingzhou_lastMotDate');
  const today = new Date().toISOString().slice(0, 10);
  if (lastMot === today) return;

  // 判断是否需要推送：市场有显著变化时
  const ki = marketData.key_indicators;
  let motHtml = '';
  const holdings = Storage.get('qingzhou_holdings') || [];

  if (ki.bond_10y && parseFloat(ki.bond_10y) < 2.8) {
    motHtml += '<div style="margin-bottom:8px">📉 <strong>国债收益率下行</strong>：10年期国债收益率降至 ' + ki.bond_10y + '，利好固收类产品。如果您持有债券基金或固收+，净值可能小幅上升。</div>';
  }

  if (holdings.length > 0) {
    const hasEquity = holdings.some(h => h.risk === 'R3' || h.risk === 'R4');
    if (hasEquity) {
      motHtml += '<div style="margin-bottom:8px">📊 <strong>权益市场关注</strong>：您持有权益类产品，建议关注本周沪深 300 走势。短期波动是正常现象，不建议恐慌赎回。</div>';
    }
  }

  if (motHtml) {
    // 在周报下方追加波动解读
    const motDiv = document.createElement('div');
    motDiv.style.cssText = 'margin-top:12px;padding:14px;background:var(--gold-light);border-radius:var(--r-sm);font-size:var(--font-size-xs);color:var(--ink-70);line-height:1.8';
    motDiv.innerHTML = '<div style="font-size:var(--font-size-xs);font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:var(--gold-dark);margin-bottom:8px">📡 市场波动解读</div>' + motHtml;
    container.appendChild(motDiv);
    Storage.set('qingzhou_lastMotDate', today);
  }
}

/** KYP 投研对齐：市场事件 x 用户持仓 = 精准影响分析 */
function renderKypAlignment() {
  const holdings = Storage.get('qingzhou_holdings') || [];
  if (holdings.length === 0) return;

  const marketData = typeof MARKET_DATA !== 'undefined' ? MARKET_DATA : null;
  if (!marketData || !marketData.key_indicators) return;

  const alerts = [];
  const ki = marketData.key_indicators;

  // 债券收益率下行 → 利好固收
  if (ki.bond_10y && parseFloat(ki.bond_10y) < 2.8) {
    const affected = holdings.filter(h => h.risk === 'R2' || (h.name && /债|固收|短债/.test(h.name)));
    if (affected.length > 0) {
      alerts.push({
        type: 'positive',
        title: '国债收益率下行，利好固收类持仓',
        detail: `10年期国债收益率 ${ki.bond_10y}，处于低位。您持有的 ${affected.map(h=>h.name).join('、')} 可能受益于利率下行，净值有望小幅上升。`,
        products: affected
      });
    }
  }

  // 权益反弹 → 利好指数/权益
  if (ki.pe_csi300 && parseFloat(ki.pe_csi300) < 13) {
    const affected = holdings.filter(h => h.risk === 'R3' || h.risk === 'R4');
    if (affected.length > 0) {
      alerts.push({
        type: 'info',
        title: '沪深300估值处于历史中低位',
        detail: `当前PE约${ki.pe_csi300}倍，低于近5年中位数。您持有的 ${affected.map(h=>h.name).join('、')} 当前估值合理，定投窗口打开。`,
        products: affected
      });
    }
  }

  // 汇率波动 → 影响跨境产品
  if (ki.usdcny && parseFloat(ki.usdcny) > 7.2) {
    const affected = holdings.filter(h => typeof getUnderlyingAssets === 'function' && getUnderlyingAssets(h.name)?.risk_tags?.includes('跨境'));
    if (affected.length > 0) {
      alerts.push({
        type: 'warning',
        title: '人民币汇率波动，影响跨境产品',
        detail: `美元兑人民币 ${ki.usdcny}，汇率偏高。您持有的 ${affected.map(h=>h.name).join('、')} 含有跨境资产，需关注汇率风险。`,
        products: affected
      });
    }
  }

  // 无事件时静默
  if (alerts.length === 0) return;

  // 渲染
  const container = document.getElementById('weeklyReport');
  if (!container) return;

  const div = document.createElement('div');
  div.style.cssText = 'margin-top:12px';
  div.innerHTML = `
    <div class="sec-label">🔍 投研对齐 · 持仓影响分析</div>
    <div class="rec-current">
      ${alerts.map(a => `
        <div style="padding:12px 0;border-bottom:1px solid var(--border)">
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
            <span style="font-size:var(--font-size-xs)">${a.type === 'positive' ? '🟢' : a.type === 'warning' ? '🟡' : '🔵'}</span>
            <span style="font-weight:700;font-size:var(--font-size-xs)">${a.title}</span>
          </div>
          <div style="font-size:var(--font-size-xs);color:var(--ink-70);line-height:1.7">${a.detail}</div>
        </div>
      `).join('')}
    </div>
  `;
  container.parentNode.insertBefore(div, container.nextSibling);
}

window.generateReport = function() {
  if (typeof generateAllocationReport === 'function') {
    const report = generateAllocationReport();
    if (report) {
      Storage.set('qingzhou_allocationReport', { content: report, generatedAt: new Date().toISOString() });
      showToast('报告已生成！可在聊天中查看');
      NavrenApp.navigate('chat.html?q=' + encodeURIComponent('请根据我的资产配置报告，帮我分析一下'), currentMode);
    } else {
      showToast('请先完成风险评估和档案填写');
    }
  }
};

function sendToChat(productName) {
  NavrenApp.navigate('chat.html?q=' + encodeURIComponent('帮我看看' + productName), currentMode);
}

// ── 产品详情弹窗 ──
window.showProductDetail = function(productName) {
  const modal = document.getElementById('productModal');
  if (!modal) return;
  modal.classList.remove('hidden');

  // 从产品数据中查找完整信息
  let product = null;
  if (typeof getProductData === 'function') {
    const all = getProductData();
    product = all?.find(p => p.name === productName || p.product_id === productName);
  }

  // 底层资产
  const underlying = typeof getUnderlyingAssets === 'function' ? getUnderlyingAssets(productName) : null;

  document.getElementById('productModalTitle').textContent = productName;
  document.getElementById('productModalBody').innerHTML = product ? `
    <div style="line-height:2.2;font-size:var(--font-size-sm)">
      <div style="display:flex;gap:16px;margin-bottom:12px">
        <div><span style="color:var(--ink-40)">风险等级</span><br><span class="risk-badge risk-r${(product.risk_level||'R3').replace('R','')}" style="margin-top:4px">${product.risk_level || 'R3'}</span></div>
        <div><span style="color:var(--ink-40)">业绩基准</span><br><strong>${product.benchmark || '—'}</strong></div>
        <div><span style="color:var(--ink-40)">封闭期</span><br><strong>${product.lock_period || '—'}</strong></div>
      </div>
      <div style="margin-bottom:12px"><span style="color:var(--ink-40)">起购金额</span><br><strong>${product.min_amount ? product.min_amount.toLocaleString() : '—'} 元</strong></div>
      ${underlying ? `
        <div style="margin-bottom:12px"><span style="color:var(--ink-40)">底层资产</span><br><strong>${underlying.assets.join('、')}</strong></div>
        ${underlying.risk_note ? `<div style="margin-bottom:12px;padding:8px 12px;background:#FFF5F5;border-radius:6px;font-size:var(--font-size-xs);color:#C53030">⚠️ ${underlying.risk_note}</div>` : ''}
        ${underlying.risk_tags.length > 0 ? `<div style="margin-bottom:12px"><span style="color:var(--ink-40)">风险标签</span><br>${underlying.risk_tags.map(t=>'<span style="display:inline-block;margin:2px;padding:2px 8px;border-radius:4px;background:var(--ink-15);font-size:var(--font-size-xs)">'+t+'</span>').join('')}</div>` : ''}
      ` : ''}
      ${product.fee ? `<div style="margin-bottom:12px"><span style="color:var(--ink-40)">费率</span><br><strong>${product.fee}</strong></div>` : ''}
      ${product.suitable_for?.length ? `<div><span style="color:var(--ink-40)">适合客群</span><br><strong>${product.suitable_for.join('、')}</strong></div>` : ''}
    </div>
  ` : `
    <div style="line-height:2.2;font-size:var(--font-size-sm)">
      <div style="margin-bottom:12px"><span style="color:var(--ink-40)">风险等级</span><br><span class="risk-badge risk-r3">R3</span></div>
      ${underlying ? `
        <div style="margin-bottom:12px"><span style="color:var(--ink-40)">底层资产</span><br><strong>${underlying.assets.join('、')}</strong></div>
        ${underlying.risk_note ? `<div style="margin-bottom:12px;padding:8px 12px;background:#FFF5F5;border-radius:6px;font-size:var(--font-size-xs);color:#C53030">⚠️ ${underlying.risk_note}</div>` : ''}
      ` : '<div style="color:var(--ink-40)">详细产品信息请通过手机银行查看完整说明书</div>'}
    </div>
  `;

  document.getElementById('productModalSend').onclick = () => {
    closeProductModal();
    sendToChat(productName);
  };
};

window.closeProductModal = function() {
  const modal = document.getElementById('productModal');
  if (modal) modal.classList.add('hidden');
};

function showToast(msg) {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3000);
}
