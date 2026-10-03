/**
 * Navren — L1 前端意图路由
 * 关键词 + 正则加权匹配，<1ms，离线可用
 */

const L1_ROUTES = [
  // ═══════ 特殊规则 ═══════
  {
    intent: 'complaint',
    priority: 1,
    keywords: ['坑', '骗', '投诉', '起诉', '银保监', '律师', '坑人', '害人', '太坑', '上当'],
    regex: /(亏|跌|赔).{0,5}(好多|太多|惨|死|完|光)|(怎么办|好慌|好怕|受不了|顶不住)/,
    weight: 5,
    action: 'handoff'
  },
  {
    intent: 'compliance_redline',
    priority: 2,
    keywords: ['保本保息', '稳赚不赔', '绝对安全', '无风险'],
    regex: /(保证|承诺).{0,3}(收益|赚钱|盈利)|(推荐|告诉我).{0,5}(股票代码|涨停|翻倍)/,
    weight: 5,
    action: 'compliance_block'
  },
  {
    intent: 'sentiment_anxiety',
    priority: 3,
    keywords: ['好慌', '担心', '害怕', '跌了', '亏了', '睡不着', '焦虑', '紧张', '后悔', '难受', '不敢'],
    regex: /(跌|亏|赔).{0,5}(慌|怕|担心|怎么办|睡不着|受不了)|(最近|这几天).{0,3}(一直|老).{0,3}(跌|亏)/,
    weight: 4,
    action: 'sentiment_care'
  },
  {
    intent: 'chitchat',
    priority: 4,
    keywords: [],
    regex: /(天气|吃饭|电影|游戏|追剧|明星|八卦|体育|足球|篮球|恋爱|分手)/,
    weight: 4,
    action: 'polite_reject'
  },

  // ═══════ 常规意图 ═══════
  {
    intent: 'purchase',
    priority: 4,
    keywords: ['帮我买', '我要买', '下单', '帮我推荐', '帮我看看', '推荐一下', '建议', '怎么配'],
    regex: /(帮我|我要|给我|马上).{0,3}(买(?!卖)|投(?!诉)|申购|下单|推荐|看看|建议|配置)/,
    weight: 5,
    action: 'l2_state_required'
  },
  {
    intent: 'transaction',
    priority: 5,
    keywords: ['怎么买', '在哪里买', '如何购买', '怎么操作', '申购', '赎回', '取出来', '到账', '怎么卖', '怎么转'],
    regex: /(怎么|如何|在哪).{0,5}(买|卖|操作|赎回|取|转账)|(申购|赎回).{0,3}(流程|步骤|方法)/,
    weight: 4,
    action: 'route_to_rule_db'
  },
  {
    intent: 'market',
    priority: 6,
    keywords: ['市场', '行情', '经济', '走势', '宏观', '利率'],
    regex: /(最近|现在|当前).{0,3}(市场|行情|经济|股市|债市).{0,3}(怎么样|如何|怎么看)/,
    weight: 3,
    action: 'route_to_market_db'
  },
  {
    intent: 'risk',
    priority: 7,
    keywords: ['风险等级', 'R1', 'R2', 'R3', 'R4', 'R5', '保本', '安全', '合法', '合规'],
    regex: /(什么|啥|的意思).{0,3}(风险|等级|R\d)/,
    weight: 3,
    action: 'route_to_regulation_db'
  },
  {
    intent: 'portfolio',
    priority: 8,
    keywords: ['亏了', '要不要卖', '要不要赎回', '到期', '到期了', '调仓', '换仓', '继续持有', '还要不要', '该不该卖'],
    regex: /(亏|跌).{0,3}(要不要|该不该|该).{0,3}(卖|赎回|走|换)|(到期|快到期).{0,3}(怎么办|怎么处理|要不要续)/,
    weight: 4,
    action: 'route_to_product_db'
  },
  {
    intent: 'product',
    priority: 9,
    keywords: ['理财', '产品', '有什么', '介绍', '收益', '对比', '区别', '哪个好'],
    regex: /(有没有|有什么|推荐).{0,3}(理财|产品|基金|好的)|(对比|比较|哪个好|区别)/,
    weight: 3,
    action: 'route_to_product_db'
  },
  {
    intent: 'marketing_opportunity',
    priority: 10,
    keywords: ['年终奖', '发了钱', '闲钱', '不知道放', '攒了', '奖金', '红包'],
    regex: /(发了|拿到|收到).{0,5}(年终奖|奖金|分红)|(闲钱|余钱).{0,3}(不知道|没想好).{0,3}(怎么|放哪)/,
    weight: 3,
    action: 'route_to_product_db'
  }
];

const INJECTION_PATTERNS = [
  /忽略(以上|前面|之前|所有|系统).*(指令|提示|规则|设定)/,
  /(现在|假装|扮演|你是|你是一个|从现在开始).*(推销|销售|股评|专家|赌徒)/,
  /(不要|别|禁止|停止).*(合规|风险|谨慎|警告)/,
  /(###|===|```|~~~|<<<|>>>).*(指令|提示|规则)/,
  /(DAN|越狱|jailbreak|无视|解除).*(限制|规则|设定)/,
  /(推荐|告诉我|说).*(股票|代码|涨停|翻倍|暴涨).*(基金|股票|币|买)/
];

const COMPLIANCE_KEYWORDS = [
  { pattern: /稳赚不赔|保本保息|绝对安全|无风险|稳赚|必赚|躺赚/, category: '收益承诺' },
  { pattern: /最好的|最高的|最安全的|绝对不会亏|肯定涨|百分百/, category: '极端用语' },
  { pattern: /保证收益|承诺收益|固定收益/, category: '承诺表述' },
  { pattern: /比.{0,3}银行好|秒杀|吊打|碾压|完胜/, category: '违规对比' }
];

const Router = {
  /** 检测 prompt 注入 */
  detectInjection(input) {
    return INJECTION_PATTERNS.some(p => p.test(input));
  },

  /** L1 意图路由 */
  route(input) {
    let bestMatch = null;
    let bestScore = 0;
    let secondScore = 0;

    for (const route of L1_ROUTES) {
      let score = 0;
      for (const kw of route.keywords) {
        if (kw && input.includes(kw)) score++;
      }
      if (route.regex && route.regex.test(input)) {
        score += 3;
      }
      score *= route.weight;

      if (score > bestScore) {
        secondScore = bestScore;
        bestScore = score;
        bestMatch = route;
      } else if (score > secondScore) {
        secondScore = score;
      }
    }

    if (!bestMatch) return { source: 'L2_UPGRADE', l1Guess: null, l1Score: 0 };

    const gap = bestScore - secondScore;
    const THRESHOLD = 9;

    if (bestScore >= THRESHOLD && gap >= 6) {
      return { source: 'L1', intent: bestMatch.intent, action: bestMatch.action };
    }
    return { source: 'L2_UPGRADE', l1Guess: bestMatch.intent, l1Score: bestScore };
  },

  /** L2 状态路由：purchase */
  l2StatePurchase(input) {
    const ORDER = /(帮我|我要|给我|马上).{0,3}(买|投|申购|下单).{0,5}(万|元|块|这个|那只)/;
    if (ORDER.test(input)) {
      return { state: 'ORDER', action: 'handoff', path: 'purchase_handoff' };
    }
    const profile = assembleProfile();
    if (!profile.risk) return { state: 'NO_RISK_PROFILE', action: 'show_questionnaire', path: 'marketing' };
    if (!profile.finance.amount || !profile.finance.horizon) {
      const missing = [];
      if (!profile.finance.amount) missing.push('amount');
      if (!profile.finance.horizon) missing.push('horizon');
      return { state: 'HAS_RISK_NO_AMOUNT', action: 'ask_followup', missing, path: 'marketing' };
    }
    return { state: 'PROFILE_COMPLETE', action: 'generate_plan', path: 'marketing' };
  },

  /** L2 状态路由：product */
  l2StateProduct(input) {
    const names = extractProductNames(input);
    if (names.length >= 2 || /(对比|比较|哪个好|区别|vs)/.test(input)) {
      return { state: 'COMPARISON', products: names };
    }
    if (/(什么意思|什么是|啥意思|解释|定义|概念)/.test(input)) {
      return { state: 'EDUCATION' };
    }
    if (names.length === 1) {
      return { state: 'SINGLE_PRODUCT', product: names[0] };
    }
    return { state: 'DISCOVERY' };
  },

  /** 关键词合规检查（输出端） */
  checkCompliance(text) {
    for (const rule of COMPLIANCE_KEYWORDS) {
      if (rule.pattern.test(text)) {
        return { pass: false, category: rule.category };
      }
    }
    return { pass: true };
  }
};

