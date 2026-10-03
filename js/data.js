/**
 * Navren —— 预设 fallback 对话数据
 * 当元景万悟 API 不可用（超时 / 网络错误）时，按关键词匹配返回预设回复。
 * 每条回复均含合规标签，三模式（classic / senior / youth）各有对应话术。
 */

const FALLBACK_DATA = [
  {
    id: "product_inquiry",
    keywords: ["什么是", "理财", "产品", "稳健", "推荐", "有什么", "介绍", "哪些"],
    reply: {
      classic: "我行理财产品按风险等级分为 R1（低风险）至 R5（高风险）。稳健型产品通常指 R2-R3 等级，主要投向高等级债券、同业存单等资产，波动较小。例如「稳享固收增强 6 个月」（R2，业绩比较基准 2.8%-3.5%）和「稳盈固收+ 12 个月」（R3，业绩比较基准 3.5%-4.5%）。您可以根据自己的资金使用计划选择合适期限。",
      senior: "叔叔/阿姨，我给您简单说说——理财产品就像把钱交给专业的人去打理，他们会帮您买一些稳妥的东西，比如借给信誉好的大公司（债券），到期了连本带利还回来。风险等级越低越稳当，R1 最安全，R5 波动最大。咱可以先从 R2 的看起，6 个月就能取，心里踏实。",
      youth: "银行理财 R1-R5 风险分级，稳健型 = R2-R3。底层一般是高等级信用债 + 同业存单 + 少量可转债增厚。以稳享固收增强 6 个月为例：R2，年化 3.2%，最大回撤不到 0.5%。如果你对波动容忍度低，R2 是甜蜜点。"
    }
  },
  {
    id: "asset_allocation",
    keywords: ["配置", "怎么配", "分配", "组合", "投什么", "买什么", "建议", "方案", "怎么投"],
    reply: {
      classic: "资产配置的核心是根据您的风险偏好、资金规模和投资期限，分散到不同类型的资产中。以稳健型 R3 为例，建议采用「固收打底 + 权益增强」策略：\n\n• 固收类（70%）：稳享固收增强 6 个月 + 安鑫短债 30 天，提供稳定票息\n• 权益类（30%）：沪深 300 指数增强，分享市场长期增长\n\n调整后的组合预期年化 3.5%-5.5%，最大回撤控制在 3% 以内。具体比例需结合您的实际情况调整。\n\n⚠️ 以上为参考建议，历史业绩不代表未来收益，投资需谨慎。",
      senior: "叔叔/阿姨，我给您盘算一下——咱们把钱分成三份：大头（七成）放稳妥的地方，买债券类产品，稳稳当当拿利息；小头（三成）买点大公司的指数基金，长期看涨。这样万一有一边波动了，另一边还能兜着，心里踏实。您觉得这个思路行不行？",
      youth: "以 R3 稳健型、20 万为例，最优配置：\n📊 稳享固收增强 6 个月 — 40%（R2，年化 3.2%）\n📊 沪深 300 指数增强 — 30%（R4，年化 6.5%）\n📊 安鑫短债 30 天 — 20%（R2，年化 2.3%）\n📊 活期盈 — 10%（R1，灵活备用）\n预期年化 3.5%-5.5%，最大回撤 -2.8%。\n\n⚠️ 以上为参考建议，历史不代表未来。"
    }
  },
  {
    id: "compliance_reject",
    keywords: ["保本", "保息", "稳赚", "绝对安全", "无风险", "一定涨", "肯定不会亏", "保证收益", "承诺收益"],
    reply: {
      classic: "根据《理财公司理财产品销售管理暂行办法》，非存款理财产品不得承诺保本保息。任何理财产品都存在不同程度的风险，这是监管的明确要求。如果您对本金安全有较高要求，建议了解我行大额存单产品——它是存款产品，受存款保险条例保护，50 万元以内本息全额保障。",
      senior: "叔叔/阿姨，我得跟您说实话——除了银行存款，其他理财产品按国家规定是不能保证本金不亏的。这不是咱们一家银行的规定，是所有银行都一样。不过您别担心，有存款保险的大额存单 50 万以内是实实在在安全的，您要是担心，可以先从这个开始看看。",
      youth: "监管规定：理财≠存款，不能承诺保本保息。不存在「保本高收益」这种产品——金融里收益和风险是一枚硬币的两面。如果你要本金零风险，只有两类：①活期/定期存款 ②大额存单（50 万以内存款保险兜底，2.5%）。其他任何说保本的，直接视为违规。"
    }
  },
  {
    id: "market_update",
    keywords: ["市场", "行情", "经济", "走势", "最近", "怎么样", "变化", "宏观", "利率"],
    reply: {
      classic: "近期市场有几个关键变化：① 货币政策维持宽松基调，市场利率整体处于下行通道，利好债券类资产；② A 股市场估值处于历史中低位，长期配置价值逐步显现；③ 信用债利差收窄至近年低位，纯债产品票息空间有限但资本利得仍有支撑。综合来看，当前时点「固收打底 + 权益增厚」的配置策略性价比突出。\n\n⚠️ 以上为市场分析，不构成投资建议。市场有风险，投资需谨慎。",
      senior: "叔叔/阿姨，最近市场整体还算平稳。简单来说——存款的利息虽然低了些，但债券类产品表现还不错。股票市场最近有一些起伏，不过长期来看大趋势是好的。咱们不用天天盯着，把专业的事交给专业的人做就行。",
      youth: "当前宏观画像：货币宽松 + 信用利差历史低位 + A 股估值中低分位。对债市而言，票息空间收窄但久期策略仍有可为。股市方面，沪深 300 PE 约 12 倍，处于近 5 年 40% 分位，定投窗口打开。结论：纯债中规中矩，固收+性价比提升，权益定投正当时。\n\n⚠️ 市场分析不构成投资建议。投资需谨慎。"
    }
  },
  {
    id: "risk_education",
    keywords: ["风险等级", "R1", "R2", "R3", "R4", "R5", "啥意思", "什么意思", "怎么分", "区别", "风险"],
    reply: {
      classic: "理财产品风险等级分为五级：\n\n• R1（低风险）：本金安全性极高，收益稳定，如货币基金、存款\n• R2（中低风险）：本金风险较低，波动很小，如短债基金\n• R3（中等风险）：本金有一定风险，可能出现小幅亏损\n• R4（中高风险）：本金风险较大，波动明显\n• R5（高风险）：本金风险极大，可能出现较大亏损\n\nR1-R2 适合保守型及以上客户，R3 适合稳健型及以上，R4 适合进取型及以上，R5 仅适合激进型客户。",
      senior: "叔叔/阿姨，风险等级就像是给理财产品贴的“安全标签”，从 R1 到 R5，数字越小越安全。R1 就像是把钱放在稳妥的匣子里，基本不会少；R2 偶尔会晃一晃，但幅度很小；R3 开始有起有落了，但大趋势还是好的。您要是第一次接触，我建议从 R1、R2 开始，先看看再说。",
      youth: "R1-R5 风险五级，核心看两个变量：①底层资产波动率 ②最大回撤。R1 货币基金≈无回撤；R2 短债基金最大回撤<0.5%；R3 混合型最大回撤 3%-5%；R4 权益类最大回撤 15%-20%；R5 行业主题最大回撤>30%。选风险等级就是选你能接受的最大亏损幅度。"
    }
  },
  {
    id: "product_comparison",
    keywords: ["对比", "比较", "哪个好", "区别", "选哪个", "这只", "那只", "基金"],
    reply: {
      classic: "比较两只产品，建议从以下维度看：① 风险等级（R1-R5）——决定波动幅度；② 业绩比较基准——反映预期收益区间（非承诺）；③ 封闭期——决定资金灵活性；④ 底层资产——决定收益来源和风险特征；⑤ 费率——影响实际到手收益。两只产品各有优势，没有绝对的好坏，关键看与您自身情况的匹配程度。\n\n如果您愿意，可以告诉我您的资金规模和投资期限，我帮您分析哪只更适合。",
      senior: "叔叔/阿姨，选产品不是看哪个收益高就买哪个，得看哪个更适合您——就像买鞋，不是越贵越好，合适的才舒服。我帮您看看这两个产品的安全程度、放多久能取、收多少费用，咱们一起比比看。您方便跟我说说这钱您打算放多久吗？",
      youth: "比较两只产品的硬指标：① 风险等级 ② 历史年化 + 最大回撤（夏普比率）③ 封闭期 vs 你的投资期限 ④ 费率结构（管理费+托管费+申赎费）。给我产品名，我帮你跑一遍对比。记住：选产品不是为了找「最好的」，而是找「最适合你的」。"
    }
  },
  {
    id: "transaction_guide",
    keywords: ["买", "购买", "怎么操作", "在哪里买", "我要买", "下单", "交易", "申购", "赎回", "卖出", "取出来"],
    reply: {
      classic: "您可以通过我行手机银行 APP 完成购买：\n\n① 打开 APP → 首页 →「理财」\n② 搜索产品名称或登记编码\n③ 点击「立即购买」\n④ 仔细阅读并确认风险揭示书\n⑤ 输入购买金额 → 提交\n\n⚠️ 首次购买需在 APP 内完成风险评估问卷。任何涉及交易的操作，请务必通过官方 APP 完成，切勿委托他人代操作。",
      senior: "叔叔/阿姨，我一步一步跟您说：打开手机银行 APP，首页有个「理财」按钮，点进去，搜产品名字，找到之后点「购买」，然后系统会让您看一段风险提示，看完了确认，输金额，最后提交就行了。如果不放心操作，可以让家人帮忙，或者去咱们银行的网点，工作人员当面帮您。",
      youth: "购买流程：APP → 理财板块 → 搜产品名/编码 → 确认风险揭示 → 输入金额 → 提交。首次购买前需完成风险评估问卷（大约 5 分钟）。注意：通过官方 APP 操作，任何代买代操作都是违规的。"
    }
  },
  {
    id: "emotion_comfort",
    keywords: ["亏", "跌", "赔", "担心", "害怕", "慌", "后悔", "怎么办", "难受", "坑", "骗"],
    reply: {
      classic: "我理解您的心情，看到账户亏损确实让人焦虑。市场短期波动是正常现象——回顾过去 20 年，A 股经历过多次超过 20% 的回调，但长期趋势是向上的。建议您不要因为短期波动做出冲动决策。如果您的财务状况或投资目标发生了变化，我们可以重新评估您的配置方案。\n\n我也可以帮您转接专业投资顾问，由他/她为您做更深入的持仓分析。您觉得需要吗？",
      senior: "叔叔/阿姨，我完全理解您的心情，看到亏钱谁都不好受。但是市场就是这样，有涨有跌，就像天气有晴有雨。咱们不要着急做决定——慌的时候最容易出错。这笔钱如果不是急用，咱先放着看看；如果实在不放心，我可以帮您转接专业的投资顾问，让人家当面给您分析分析。",
      youth: "完全理解。市场波动是常态——沪深 300 历史上有过 6 次超 20% 的回调，每次都涨回来了。现在的浮亏不是实际亏损，除非你此时卖出。建议：① 不要恐慌性赎回 ② 审视自己的风险承受力是否匹配当前持仓 ③ 如果睡不着觉，你的持仓风险可能超了。需要我帮你重新评估配置吗？"
    }
  },
  {
    id: "risk_assessment",
    keywords: ["怕亏", "害怕", "保守", "不敢", "胆小", "安全第一", "本金", "稳妥", "不敢冒险"],
    reply: {
      classic: "从您的表述来看，您对本金的安全性比较看重，风险偏好可能偏保守或稳健型。为了更准确地评估，建议您完成一份简短的问卷：\n\n① 您的投资经验：A.几乎没有 B.1-3年 C.3年以上\n② 能接受的最大亏损：A.不能接受亏损 B.可接受 5% 以内 C.可接受 10% 以上\n③ 投资目标：A.保本为主 B.稳健增值 C.追求高收益\n\n请告诉我您的选择，我帮您判断对应的风险等级。",
      senior: "叔叔/阿姨，我理解，谁的钱都来之不易，小心一点没错。咱们来看看您适合什么类型的产品——您告诉我：您以前买过理财产品吗？要是亏了 5%，您能接受吗？还有这笔钱您是打算怎么用的呢？根据您的回答，我帮您选最合适的产品。",
      youth: "理解。风险厌恶是理性的，关键是把厌恶程度量化。快速自测：① 你投资多久了？② 账户跌多少你就睡不着？（5% / 10% / 20%）③ 你的钱 3 年内会用到吗？这三个问题的答案基本锁死了你该选 R 几的产品。"
    }
  },
  {
    id: "portfolio_management",
    keywords: ["亏了", "要不要卖", "要不要赎回", "到期", "到期了", "调仓", "换仓", "继续持有", "还要不要", "该不该卖"],
    reply: {
      classic: "我理解您对持仓变化的担忧。关于是否调整，我不能替您做决策，但可以帮您分析几个方面：① 产品基本面是否有变化 ② 您的风险承受力是否变化 ③ 当前市场环境如何。如果您持有的具体产品名称方便告诉我，我可以帮您做更详细的分析。如果担心较大，建议咨询我行专业投资顾问。",
      senior: "叔叔/阿姨，看到账户有变化确实让人心里不踏实。我没办法直接替您决定要不要卖掉，但我可以帮您看看这只产品最近的表现怎么样，跟其他差不多的产品比情况如何。您方便告诉我您买的是哪只产品吗？",
      youth: "持仓管理的核心不是择时，是纪律。帮你分析三个维度：① 产品基本面是否恶化（不是短期波动）② 你的风险承受力是否变了（不是市场变了）③ 是否达到了你预设的止损线。告诉我产品名，我帮你拉数据。"
    }
  },
  {
    id: "marketing_opportunity",
    keywords: ["年终奖", "发了钱", "闲钱", "不知道放", "攒了", "奖金", "红包", "多了笔钱"],
    reply: {
      classic: "恭喜！有新增资金是好事。建议不要一次性全部投入，可分 2-3 批配置。首先建议预留 3-6 个月的生活开支作为流动资金（可放入活期盈等灵活产品），其余部分可根据您的风险偏好分批配置到固收+或混合型产品中。方便告诉我这笔钱的金额和您打算持有多久吗？",
      senior: "叔叔/阿姨，发了奖金是好事呀！我的建议是先留出一部分日常要用的钱放活期，剩下的如果不急着用，可以看看比较稳当的产品。您方便跟我说说大概有多少、打算放多久吗？我帮您参谋参谋。",
      youth: "新资金入账？先说原则：① 别 all-in，分 2-3 批建仓 ② 先预留 3-6 个月生活费（活期盈）③ 剩余的按你 R 等级配置。告诉我金额和时间线，我跑一下推荐组合。"
    }
  }
];

/**
 * 按关键词匹配查找 fallback 回复
 * @param {string} userInput - 用户输入文本
 * @param {string} mode - 当前模式 "classic" | "senior" | "youth"
 * @returns {object|null} 匹配到的回复对象，或 null
 */
function findFallback(userInput, mode) {
  if (!userInput || !userInput.trim()) return null;

  const input = userInput.toLowerCase();
  let bestMatch = null;
  let bestScore = 0;

  // ══ 第一优先级：检测情绪 → 无论如何先安抚 ══
  const sentimentKeywords = ['好慌', '担心', '害怕', '跌了', '亏了', '睡不着', '焦虑', '紧张', '后悔', '难受', '不敢', '太坑', '坑人'];
  const hasSentiment = sentimentKeywords.some(k => input.includes(k));
  if (hasSentiment) {
    let profile, risk;
    try { profile = typeof assembleProfile === 'function' ? assembleProfile() : null; risk = profile?.risk; }
    catch(e) { risk = null; }
    if (!risk) { const r = Storage.get('qingzhou_riskProfile'); if (r?.level) risk = r; }
    let comfortText = '';
    if (mode === 'senior') {
      comfortText = '叔叔/阿姨，看到市场波动心里不踏实，我完全理解。钱是辛苦攒的，谁都会担心。咱们先别急——市场有涨有跌是正常的，慌的时候做决定最容易出错。';
    } else if (mode === 'youth') {
      comfortText = '完全理解。市场波动是常态——沪深300 历史上 6 次超 20% 回调都涨回来了。浮亏不是实际亏损，除非现在卖出。先冷静，我帮你分析一下。';
    } else {
      comfortText = '我理解您的心情，看到市场波动确实让人不安。市场短期起伏是正常的，建议不要因为短期波动做出冲动决策。';
    }
    if (risk) {
      comfortText += `\n\n您的风险等级是 ${risk.level} ${risk.label}，可承受最大回撤 ${risk.maxDrawdown}。当前波动在您的承受范围内。`;
    }
    comfortText += '\n\n如果您愿意，我可以帮您：① 看看您持有的产品受影响大不大 ② 重新评估是否需调整配置 ③ 聊聊更保守的选项。';
    return { id: 'sentiment_comfort', text: comfortText, isFallback: true };
  }

  // ══ 第二优先级：推荐/配置请求 / 用户提供了金额期限信息 → 检查画像完整度 ══
  const isRecommendation = /推荐|配置|怎么配|建议|方案|投什么|买什么/.test(input);
  const hasAmountHorizon = /\d+\s*(?:万|w|万元|个?月|年|天)/.test(input);
  if (isRecommendation || hasAmountHorizon) {
    let profile, risk, finance;
    try {
      profile = typeof assembleProfile === 'function' ? assembleProfile() : null;
      risk = profile?.risk;
      finance = profile?.finance;
    } catch(e) { profile = null; risk = null; finance = {}; }
    // 兜底：直接从 localStorage 再读一次
    if (!risk) {
      const rawRisk = Storage.get('qingzhou_riskProfile');
      if (rawRisk?.level) {
        risk = { level: rawRisk.level, score: rawRisk.score, label: rawRisk.label, maxDrawdown: rawRisk.maxDrawdown, maxEquityRatio: rawRisk.maxEquityRatio };
        if (!profile) profile = {};
        profile.risk = risk;
      }
    }
    if (!finance) {
      const rawProfile = Storage.get('qingzhou_userProfile') || {};
      finance = { amount: rawProfile.amount || null, horizon: rawProfile.horizon || null, goal: rawProfile.goal || null };
    }

    // 没有风险评估 → 引导先评估
    if (!risk) {
      const guideTexts = {
        classic: '在为您推荐产品之前，我需要先了解您的风险偏好。您可以跟我说"重新测试我的风险评估结果"，完成 8 道简单题目（约 2 分钟），我就能为您精准匹配产品。',
        senior: '叔叔/阿姨，推荐产品之前，我先帮您做个小测评——就 8 道题，看看您的钱适合哪种风险等级。测完了，我帮您找最合适的产品，这样心里有底。',
        youth: '推荐之前先跑个风险画像——8 题，2 分钟。有了 R 等级才能精准匹配，不然都是盲推。试试说"重新测试我的风险评估结果"。'
      };
      return { id: 'profile_guide', text: guideTexts[mode] || guideTexts.classic, isFallback: true };
    }

    // 有风险但缺金额/期限 → 追问
    if (!finance?.amount || !finance?.horizon) {
      const missing = [];
      if (!finance?.amount) missing.push('可投金额');
      if (!finance?.horizon) missing.push('投资期限');
      const askTexts = {
        classic: `您的风险评估结果是 ${risk.level} ${risk.label}。为了给您更精准的建议，还需要了解：${missing.join(' 和 ')}。您方便告诉我吗？\n\n例如："我有 20 万，3 年不用"`,
        senior: `叔叔/阿姨，风险测评出来了——您是 ${risk.label}。接下来我想知道：您大概有多少钱可以拿来理财？这笔钱多久不会用到？您告诉我，我帮您算得准准的。`,
        youth: `风险画像：${risk.level} ${risk.label}。还差两个关键参数：① 可投金额 ② 投资期限。报一下，我直接跑配置。格式："20 万，3 年不用"`
      };
      return { id: 'profile_incomplete', text: askTexts[mode] || askTexts.classic, isFallback: true };
    }

    // 画像完整 → 基于风险等级生成个性化推荐
    const amount = finance.amount >= 10000 ? (finance.amount/10000).toFixed(0) + '万' : finance.amount + '元';
    const template = profile.getTemplate ? profile.getTemplate() : null;
    const riskLevel = parseInt(risk.level?.replace('R','') || '3');
    const maxProductRisk = Math.min(5, riskLevel + 1);

    // 按风险等级生成不同的配置
    let equityRatio, bondRatio, cashRatio;
    let products, drawdown;
    if (riskLevel <= 1) {
      // R1保守型：几乎全部现金/存款
      equityRatio=0; bondRatio=20; cashRatio=80;
      products = ['安心存单（R1，存款保险保障，年利率 1.8%）','活期盈（R1，灵活存取，七日年化 1.5%）'];
      drawdown = '<0.1%';
    } else if (riskLevel <= 2) {
      // R2稳健型：少量固收
      equityRatio=0; bondRatio=70; cashRatio=30;
      products = ['安鑫短债 30 天（R2，基准 2.0%-2.5%）','稳享固收增强 6 个月（R2，基准 2.8%-3.5%）','活期盈（R1，灵活备用）'];
      drawdown = '<0.5%';
    } else if (riskLevel <= 3) {
      // R3平衡型：固收+少量权益
      equityRatio=30; bondRatio=50; cashRatio=20;
      products = ['稳享固收增强 6 个月（R2，基准 2.8%-3.5%）','沪深 300 指数增强（R3，基准 4.0%-6.5%）','安鑫短债 30 天（R2，基准 2.0%-2.5%）','活期盈（R1，灵活备用）'];
      drawdown = '<3%';
    } else if (riskLevel <= 4) {
      // R4进取型：权益为主
      equityRatio=60; bondRatio=30; cashRatio=10;
      products = ['沪深 300 指数增强（R3，基准 4.0%-6.5%）','科技主题混合（R4，基准 6.0%-10.0%）','稳享固收增强 6 个月（R2，基准 2.8%-3.5%）'];
      drawdown = '<15%';
    } else {
      // R5激进型
      equityRatio=80; bondRatio=10; cashRatio=10;
      products = ['科技主题混合（R4，基准 6.0%-10.0%）','新能源行业精选（R4，基准 8.0%-12.0%）','全球配置（R4，基准 5.0%-8.0%）'];
      drawdown = '<30%';
    }

    let recText = '';
    if (mode === 'classic') {
      recText = `基于您的画像——${risk.level} ${risk.label} · ${amount} · ${finance.horizon}，为您推荐以下配置：\n\n`;
      products.forEach((p, i) => { recText += `📊 ${p}\n`; });
      recText += `\n组合最大回撤控制在 ${drawdown}。`;
      if (template) recText += ` 业绩比较基准参考区间：${template.expectedReturn}`;
      recText += '\n\n⚠️ 以上为参考建议，历史业绩不代表未来收益，投资需谨慎。';
    } else if (mode === 'senior') {
      recText = `叔叔/阿姨，根据您是${risk.label}、有 ${amount}、打算放 ${finance.horizon}，我帮您盘算了一下：\n\n`;
      if (riskLevel <= 1) {
        recText += '您是非常稳妥的类型，我建议大部分放存款——有存款保险保障，50万以内本息无忧。小部分放活期理财，随时能用。\n\n';
      } else {
        recText += `大头放稳妥的产品，波动很小，稳稳当当拿利息。${equityRatio > 0 ? '小头买点指数基金，长期看是涨的，能多赚一点。' : ''}\n\n`;
      }
      recText += `这样搭配下来，整体波动不大（最多跌 ${drawdown}），您心里踏实。您觉得这个思路行吗？\n\n⚠️ 理财非存款，产品有风险，投资须谨慎。`;
    } else {
      recText = `基于 ${risk.level} ${risk.label} · ${amount} · ${finance.horizon}，最优配置：\n\n`;
      products.forEach(p => { recText += `📊 ${p}\n`; });
      recText += `\n最大回撤 ${drawdown}`;
      if (template) recText += ` · 基准参考 ${template.expectedReturn}`;
      recText += '\n\n⚠️ 历史不代表未来。DYOR.';
    }

    return { id: 'personalized_recommendation', text: recText, isFallback: true };
  }

  // ══ 第三优先级：常规关键词匹配 ══
  for (const entry of FALLBACK_DATA) {
    let score = 0;
    for (const keyword of entry.keywords) {
      if (input.includes(keyword.toLowerCase())) {
        score++;
      }
    }
    if (score > bestScore) {
      bestScore = score;
      bestMatch = entry;
    }
  }

  if (bestMatch && bestScore > 0) {
    const text = bestMatch.reply[mode] || bestMatch.reply.classic;
    return { id: bestMatch.id, text: text, isFallback: true };
  }

  // ══ 无匹配 → 上下文感知的通用回复 ══
  let profile;
  try { profile = typeof assembleProfile === 'function' ? assembleProfile() : null; }
  catch(e) { profile = null; }

  // 简单问候 → 友好回复
  if (/^(你好|hi|hello|嗨|在吗|您好|早上好|下午好|晚上好)[\s!！。.,，]*$/.test(input.trim())) {
    const greetings = {
      classic: '您好！我是 Navren，您的智慧银行理财顾问。请问有什么可以帮您？',
      senior: '您好！我是 Navren，您慢慢说，想问什么都可以。',
      youth: 'Hi！Navren 在线。有什么理财问题直接说。'
    };
    return { id: 'greeting', text: greetings[mode] || greetings.classic, isFallback: true };
  }

  if (profile?.risk && !profile?.finance?.amount) {
    return {
      id: 'contextual_fallback',
      text: mode === 'senior'
        ? '叔叔/阿姨，我听着呢。您想了解什么？比如看看产品、问问市场，或者跟我说说您有多少钱想理财？'
        : '我在这里。您可以问我产品推荐、市场解读、风险评估，或者告诉我您的资金情况，我帮您规划。',
      isFallback: true
    };
  }

  return {
    id: 'generic_fallback',
    text: mode === 'senior'
      ? '网络好像有点慢，您别急。要不您再试一次，或者换个方式问我？'
      : '网络似乎不太稳定，请稍后重试。您也可以换个方式提问。',
    isFallback: true
  };
}
