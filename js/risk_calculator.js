/**
 * Navren — 理财量化计算引擎
 * 夏普比率 / 最大回撤 / 波动率 / Sortino / VaR / 蒙特卡洛模拟
 */

const RiskCalc = {
  /**
   * 年化收益率
   * @param {number[]} returns - 日/月收益率序列
   * @param {number} periods - 年化周期数 (252交易日 or 12月)
   */
  annualizedReturn(returns, periods = 12) {
    if (!returns.length) return 0;
    const total = returns.reduce((s, r) => s * (1 + r), 1);
    const years = returns.length / periods;
    return Math.pow(total, 1 / years) - 1;
  },

  /** 年化波动率 */
  annualizedVolatility(returns, periods = 12) {
    if (returns.length < 2) return 0;
    const mean = returns.reduce((s, r) => s + r, 0) / returns.length;
    const variance = returns.reduce((s, r) => s + Math.pow(r - mean, 2), 0) / (returns.length - 1);
    return Math.sqrt(variance) * Math.sqrt(periods);
  },

  /** 夏普比率 (假设无风险利率 2.5%) */
  sharpeRatio(returns, riskFree = 0.025, periods = 12) {
    const annRet = this.annualizedReturn(returns, periods);
    const annVol = this.annualizedVolatility(returns, periods);
    if (annVol === 0) return 0;
    return (annRet - riskFree) / annVol;
  },

  /** 最大回撤 */
  maxDrawdown(prices) {
    if (!prices.length) return 0;
    let peak = prices[0];
    let maxDd = 0;
    for (const p of prices) {
      if (p > peak) peak = p;
      const dd = (peak - p) / peak;
      if (dd > maxDd) maxDd = dd;
    }
    return maxDd;
  },

  /** Sortino比率 (下行风险调整) */
  sortinoRatio(returns, riskFree = 0.025, periods = 12) {
    const annRet = this.annualizedReturn(returns, periods);
    const downside = returns.filter(r => r < 0);
    if (!downside.length) return annRet > riskFree ? 999 : 0;
    const meanDown = downside.reduce((s, r) => s + r, 0) / downside.length;
    const downVar = downside.reduce((s, r) => s + Math.pow(r - meanDown, 2), 0) / (downside.length - 1);
    const downDev = Math.sqrt(downVar) * Math.sqrt(periods);
    return downDev === 0 ? 0 : (annRet - riskFree) / downDev;
  },

  /** 历史VaR (95%置信) */
  valueAtRisk(returns, confidence = 0.95) {
    const sorted = [...returns].sort((a, b) => a - b);
    const idx = Math.floor(returns.length * (1 - confidence));
    return -sorted[idx];
  },

  /** 条件VaR / Expected Shortfall */
  cVaR(returns, confidence = 0.95) {
    const var95 = this.valueAtRisk(returns, confidence);
    const tail = returns.filter(r => r <= -var95);
    if (!tail.length) return var95;
    return -tail.reduce((s, r) => s + r, 0) / tail.length;
  },

  /** 卡尔玛比率 (年化收益/最大回撤) */
  calmarRatio(returns, prices, periods = 12) {
    const annRet = this.annualizedReturn(returns, periods);
    const mdd = this.maxDrawdown(prices);
    return mdd === 0 ? 0 : annRet / mdd;
  },

  /** 蒙特卡洛模拟 */
  monteCarlo(initialValue, annualReturn, annualVol, years, simulations = 10000) {
    const monthlyReturn = annualReturn / 12;
    const monthlyVol = annualVol / Math.sqrt(12);
    const months = years * 12;
    const results = [];

    for (let s = 0; s < simulations; s++) {
      let value = initialValue;
      const path = [value];
      for (let m = 0; m < months; m++) {
        // Box-Muller变换生成正态分布随机数
        const u1 = Math.random();
        const u2 = Math.random();
        const z = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
        const monthlyRet = monthlyReturn + monthlyVol * z;
        value *= (1 + monthlyRet);
        path.push(value);
      }
      results.push({ finalValue: value, path });
    }

    // 排序取分位数
    results.sort((a, b) => a.finalValue - b.finalValue);
    const p5 = results[Math.floor(simulations * 0.05)].finalValue;
    const p50 = results[Math.floor(simulations * 0.50)].finalValue;
    const p95 = results[Math.floor(simulations * 0.95)].finalValue;
    const p5Path = results[Math.floor(simulations * 0.05)].path;
    const p50Path = results[Math.floor(simulations * 0.50)].path;
    const p95Path = results[Math.floor(simulations * 0.95)].path;

    return {
      initialValue,
      years,
      simulations,
      conservative: { value: p5, annualizedReturn: Math.pow(p5 / initialValue, 1 / years) - 1 },
      base: { value: p50, annualizedReturn: Math.pow(p50 / initialValue, 1 / years) - 1 },
      optimistic: { value: p95, annualizedReturn: Math.pow(p95 / initialValue, 1 / years) - 1 },
      paths: { p5: p5Path, p50: p50Path, p95: p95Path }
    };
  },

  /**
   * 组合指标综合计算
   * 输入组合配置 → 输出完整量化报告
   */
  analyzePortfolio(allocation, returnsData) {
    const { returns, prices } = returnsData;
    const periods = 12;

    return {
      annualizedReturn: this.annualizedReturn(returns, periods),
      annualizedVolatility: this.annualizedVolatility(returns, periods),
      sharpeRatio: this.sharpeRatio(returns, 0.025, periods),
      sortinoRatio: this.sortinoRatio(returns, 0.025, periods),
      maxDrawdown: this.maxDrawdown(prices),
      valueAtRisk95: this.valueAtRisk(returns, 0.95),
      cVaR95: this.cVaR(returns, 0.95),
      calmarRatio: this.calmarRatio(returns, prices, periods),
      monteCarlo: this.monteCarlo(prices[prices.length - 1] || 100000,
        this.annualizedReturn(returns, periods),
        this.annualizedVolatility(returns, periods), 3, 5000)
    };
  },

  /**
   * 基于 R1-R5 等级生成模拟月收益序列
   * 参数基于A股+债券实际历史数据校准
   */
  generateSimulatedReturns(riskLevel, months = 36) {
    // 校准参数 (年化收益, 年化波动) - 基于近5年实际市场数据
    const params = {
      R1: { annualReturn: 0.020, annualVol: 0.005 },
      R2: { annualReturn: 0.030, annualVol: 0.015 },
      R3: { annualReturn: 0.045, annualVol: 0.050 },
      R4: { annualReturn: 0.065, annualVol: 0.150 },
      R5: { annualReturn: 0.090, annualVol: 0.250 }
    };

    const p = params[riskLevel] || params.R3;
    const monthlyReturn = p.annualReturn / 12;
    const monthlyVol = p.annualVol / Math.sqrt(12);

    const returns = [];
    let price = 100;
    const prices = [price];

    for (let i = 0; i < months; i++) {
      const u1 = Math.random();
      const u2 = Math.random();
      const z = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
      const ret = monthlyReturn + monthlyVol * z;
      returns.push(ret);
      price *= (1 + ret);
      prices.push(price);
    }

    return { returns, prices };
  }
};
