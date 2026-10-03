/** 同源模拟银行客户端。身份在HttpOnly Cookie中；不保存金融状态或验证码到本地存储。 */
window.BankClient = (() => {
  let session = null, boot = null;
  const prefix = '/api/finance';
  async function raw(path, method = 'GET', body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const headers = { 'Accept': 'application/json' };
      if (method !== 'GET') { headers['Content-Type'] = 'application/json'; if (session) headers['X-QZ-CSRF'] = session.csrf; }
      const response = await fetch(prefix + path, { method, credentials: 'same-origin', cache: 'no-store', headers,
        body: method === 'GET' ? undefined : JSON.stringify(body || {}), signal: controller.signal });
      const data = await response.json();
      if (!response.ok) {
        const error = new Error(data.error?.message || '银行请求失败');
        error.code = data.error?.code; error.status = response.status;
        if (error.code === 'CSRF_DENIED' || error.status === 401) session = boot = null;
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('银行请求超时。请查询操作状态，暂勿重复提交。');
      throw error;
    } finally { clearTimeout(timer); }
  }
  async function ensure() {
    if (session) return session;
    if (!boot) boot = (async () => {
      session = await raw('/session', 'POST', {});
      return session;
    })().catch(e => { boot = null; throw e; });
    return boot;
  }
  return {
    ensure,
    async get(path) { await ensure(); return raw(path); },
    async post(path, body) { await ensure(); return raw(path, 'POST', body); },
    async agentHeaders() { const s = await ensure(); return { 'Content-Type': 'application/json', 'X-QZ-CSRF': s.csrf }; },
    isBankRequest(text) { return /账单|流水|消费|支出|收入|余额|持仓|资产|理财|配置|可投资|应急金|保留资金|风险等级|风险画像|投资期限|申购|购买|买入|赎回|代扣|订阅|会员|周期扣费|转账|缴费|挂失|额度|模拟.*产品|SIM-/i.test(text); },
    key() { return crypto.randomUUID().replaceAll('-', ''); },
    money(n) { return (n / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' 元'; },
    minor(rawValue) {
      const s = String(rawValue).trim();
      if (!/^\d{1,7}(?:\.\d{1,2})?$/.test(s)) throw new Error('金额请输入正数，最多两位小数。');
      const [yuan, cents = ''] = s.split('.');
      const value = Number(yuan) * 100 + Number(cents.padEnd(2, '0'));
      if (!Number.isSafeInteger(value) || value < 0 || value > 100000000) throw new Error('金额超出模拟业务范围。');
      return value;
    }
  };
})();
