/** 展示名称简化，不修改数据库、枚举、来源证据或凭据。 */
window.NavrenPresentation={
  name:v=>String(v??'').replace(/^模拟/,''),
  text:v=>String(v??'').replace(/^模拟银行 ·/gm,'账户 ·'),
  data(value,key='',preserve=false){
    if(Array.isArray(value))return value.map(v=>this.data(v,key,preserve));
    if(value&&typeof value==='object'){
      const verbatim=preserve||['user_confirmed','conversation','imported'].includes(value.source_scope)||value.source==='imported'||['user_calendar','user_entered','imported'].includes(value.source_type)||key==='record';
      return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,this.data(v,k,verbatim||['value','quote','notice','evidence_refs'].includes(k))]));
    }
    if(preserve)return value;
    if(typeof value==='string'&&['name','label','title','plan_name','merchant','display_name','occupation','product_name','account_name'].includes(key))return this.name(value);
    return value;
  }
};
/** 同源模拟银行客户端。身份在HttpOnly Cookie中；不保存金融状态或验证码到本地存储。 */
window.BankClient = (() => {
  let session = null, boot = null, generation=0;
  const reads=new Set();
  const identityChannel=typeof BroadcastChannel==='function'?new BroadcastChannel('navren-demo-identity'):null;
  if(identityChannel)identityChannel.onmessage=()=>{session=boot=null;clearHistory();window.dispatchEvent(new Event('navren:identity-revoked'));};
  function invalidate(){generation++;for(const c of reads)c.abort();reads.clear();}
  function clearHistory(){
    invalidate();
    for(const key of ['qingzhou_chatHistory','qingzhou_chatKeyMoments'])localStorage.removeItem(key);
    window.dispatchEvent(new Event('navren:privacy-reset'));
  }
  window.addEventListener('storage',e=>{
    if(session&&e.key==='navren_privacy_'+session.workspace_id&&e.newValue!==String(session.privacy_revision)){
      session=boot=null;clearHistory();
    }
  });
  function checkPrivacy(data,path,method){
    if(data.privacy_revision!=null&&data.workspace_id){
      const key='navren_privacy_'+data.workspace_id,old=localStorage.getItem(key);
      if(old!=null&&Number(old)!==data.privacy_revision)clearHistory();
      localStorage.setItem(key,String(data.privacy_revision));
      if(session?.workspace_id===data.workspace_id)session.privacy_revision=data.privacy_revision;
    }
    if(method==='POST'&&(/\/memories\/[^/]+\/(delete|correct)$|\/memory-relations\/[^/]+\/delete$|\/profile-observations\//.test(path)||path==='/memories/settings'))clearHistory();
  }
  const prefix = '/api/finance';
  function assertRequestCurrent(requestGeneration,requestWorkspace,requestCsrf){
    if(requestGeneration!==generation||requestWorkspace!==session?.workspace_id||requestCsrf!==session?.csrf){
      const error=new Error('页面、账户或资料授权已变化，已丢弃旧响应。');error.code='STALE_RESPONSE';throw error;
    }
  }
  async function raw(path, method = 'GET', body) {
    const requestGeneration=generation,requestWorkspace=session?.workspace_id,requestCsrf=session?.csrf;
    const controller = new AbortController();
    if(method==='GET')reads.add(controller);
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const headers = { 'Accept': 'application/json' };
      if (method !== 'GET') { headers['Content-Type'] = 'application/json'; if (session) headers['X-QZ-CSRF'] = session.csrf; }
      const response = await fetch(prefix + path, { method, credentials: 'same-origin', cache: 'no-store', headers,
        body: method === 'GET' ? undefined : JSON.stringify(body || {}), signal: controller.signal });
      assertRequestCurrent(requestGeneration,requestWorkspace,requestCsrf);
      const data = await response.json();
      assertRequestCurrent(requestGeneration,requestWorkspace,requestCsrf);
      if (!response.ok) {
        const error = new Error(data.error?.message || '银行请求失败');
        error.code = data.error?.code; error.status = response.status;
        if (['CSRF_DENIED','SESSION_CHANGED','WORKSPACE_CHANGED'].includes(error.code) || error.status === 401) {
          session = boot = null;clearHistory();
        }
        throw error;
      }
      const headerRevision=response.headers?.get?.('X-Navren-Privacy-Revision');
      const headerWorkspace=response.headers?.get?.('X-Navren-Workspace');
      if ((headerWorkspace && data.workspace_id && headerWorkspace!==data.workspace_id) ||
          (headerRevision!=null && data.privacy_revision!=null && Number(headerRevision)!==data.privacy_revision)) {
        session=boot=null;clearHistory();const e=new Error('响应的账户或授权版本不一致，已丢弃。');e.code='UNBOUND_RESPONSE';throw e;
      }
      const binding={workspace_id:headerWorkspace||data.workspace_id,
        privacy_revision:headerRevision!=null?Number(headerRevision):data.privacy_revision};
      if(binding.workspace_id&&requestWorkspace&&binding.workspace_id!==requestWorkspace&&path!=='/session'&&path!=='/session/reset'){
        session=boot=null;clearHistory();const e=new Error('账户身份已变化，请重新载入视图。');e.code='STALE_IDENTITY';throw e;
      }
      if(path==='/session/reset'){clearHistory();session=data;boot=null;}
      if(Array.isArray(data)&&requestWorkspace&&(!binding.workspace_id||!Number.isInteger(binding.privacy_revision))){const e=new Error('资料身份或授权版本缺失，请重新核对。');e.code='UNBOUND_RESPONSE';throw e;}
      checkPrivacy(binding,path,method);
      return path.startsWith('/graph/evidence')?data:NavrenPresentation.data(data);
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('银行请求超时。请查询操作状态，暂勿重复提交。');
      throw error;
    } finally { clearTimeout(timer);reads.delete(controller); }
  }
  async function rawText(path, text, contentType = 'text/csv; charset=utf-8') {
    await ensure();
    const requestGeneration=generation,requestWorkspace=session.workspace_id,requestCsrf=session.csrf;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(prefix + path, { method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'Accept': 'application/json', 'Content-Type': contentType, 'X-QZ-CSRF': requestCsrf },
        body: text, signal: controller.signal });
      assertRequestCurrent(requestGeneration,requestWorkspace,requestCsrf);
      const data = await response.json();
      assertRequestCurrent(requestGeneration,requestWorkspace,requestCsrf);
      if (!response.ok) {
        const error = new Error(data.error?.message || '银行请求失败');
        error.code = data.error?.code; error.status = response.status;
        if (['CSRF_DENIED','SESSION_CHANGED','WORKSPACE_CHANGED'].includes(error.code) || error.status === 401) {
          session = boot = null;clearHistory();
        }
        throw error;
      }
      if(requestGeneration!==generation||data.workspace_id!==requestWorkspace||session?.workspace_id!==requestWorkspace){const error=new Error('账户或授权已变化，旧导入预览已失效。');error.code='STALE_RESPONSE';throw error;}
      checkPrivacy(data,path,'POST');
      return NavrenPresentation.data(data);
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('账单上传超时，请重新预览文件。');
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
    beginAccountSwitch(){invalidate();session=boot=null;identityChannel?.postMessage({type:'invalidate'});},
    acceptSelectedSession(data){
      if(!data||typeof data.workspace_id!=='string'||typeof data.csrf!=='string'||!Number.isInteger(data.privacy_revision))throw Error('账户身份响应无效。');
      clearHistory();session={...data};boot=null;return {...session};
    },
    identity(){return session?{...session}:null;},
    async refreshIdentity(){
      await ensure();const next=await raw('/session');
      if(session&&session.workspace_id!==next.workspace_id)clearHistory();
      session=next;return {...session};
    },
    guard(){
      const snapshot=session?{...session}:null,gen=generation,view=window.NavrenViews?.revision();
      return {workspace_id:snapshot?.workspace_id,privacy_revision:snapshot?.privacy_revision,generation:gen,
        valid(data){return !!snapshot&&gen===generation&&(!window.NavrenViews||view===NavrenViews.revision())&&
          snapshot.workspace_id===session?.workspace_id&&snapshot.privacy_revision===session?.privacy_revision&&
          (!data||data.workspace_id===snapshot.workspace_id&&data.privacy_revision===snapshot.privacy_revision);}};
    },
    async get(path) { await ensure(); return raw(path); },
    async post(path, body) { await ensure(); return raw(path, 'POST', body); },
    async postText(path, text) { return rawText(path, text); },
    async agentHeaders() { const s = await ensure(); return { 'Content-Type': 'application/json', 'X-QZ-CSRF': s.csrf }; },
    isBankRequest(text) {
      return /账单|流水|消费|支出|收入|余额|持仓|资产|理财|配置|可投资|应急金|保留资金|风险等级|风险画像|投资期限|申购|购买|买入|赎回|代扣|订阅|会员|周期扣费|转账|缴费|挂失|额度|卡片|模拟.*产品|SIM-|现金流|资金时间轴|未来\s*\d{1,3}\s*(?:天|日)|协商|服务优惠|费用减免|工资迁入/i.test(text) ||
        /我.{0,8}(?:还有|有|还剩|剩下|能用|可用|能动用|可以动用).{0,8}(?:多少钱|多少(?:现金|资金|存款|余额|可用金额)?)|(?:账户|银行卡|卡里|账上|手头|手里).{0,8}(?:还有|有|还剩|剩下|可用|能用)?.{0,6}(?:多少钱|多少(?:现金|资金|存款|余额|可用金额)?)/i.test(text);
    },
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
