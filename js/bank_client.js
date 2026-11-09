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
  let session = null, boot = null, generation=0, reselecting=false;
  // 仅供本机显式G01验收观察，不认证、不请求接口、不赋予任何权限。
  const identityProjectionEnabled=(()=>{
    try{return ['http:','https:'].includes(window.location.protocol)&&
      ['127.0.0.1','localhost','[::1]'].includes(window.location.hostname)&&
      new URLSearchParams(window.location.search).get('navren_g01_identity')==='1';}
    catch(_){return false;}
  })();
  const identityProjectionAttributes=['data-navren-g01-identity-status','data-navren-g01-registered-user-sha256',
    'data-navren-g01-workspace-sha256','data-navren-g01-persona'];
  let identityProjectionEpoch=0,identityProjectionTimer=null,identityProjectionSession=null;
  function clearIdentityProjection(){
    identityProjectionEpoch++;clearTimeout(identityProjectionTimer);identityProjectionTimer=null;identityProjectionSession=null;
    for(const name of identityProjectionAttributes)document.documentElement?.removeAttribute(name);
  }
  function projectVerifiedIdentity(current){
    if(!identityProjectionEnabled||!current||identityProjectionSession===current)return;
    clearIdentityProjection();
    try{registeredSession(current);}catch(_){return;}
    if(!window.crypto?.subtle||typeof TextEncoder!=='function')return;
    const epoch=identityProjectionEpoch,started=generation;
    identityProjectionSession=current;
    const stillCurrent=()=>{
      if(epoch!==identityProjectionEpoch||started!==generation||session!==current)return false;
      try{registeredSession(current);return session===current;}catch(_){return false;}
    };
    const clearCurrent=()=>{if(epoch===identityProjectionEpoch)clearIdentityProjection();};
    identityProjectionTimer=setTimeout(clearCurrent,Math.max(0,Math.min(2147483647,
      Math.ceil((Math.min(current.auth_expires_at,current.lease_expires_at)-Date.now()/1000)*1000))));
    const fingerprint=async(field,value)=>{
      const bytes=new TextEncoder().encode('navren.g01.client-identity/1\0'+field+'\0'+value);
      const hash=await window.crypto.subtle.digest('SHA-256',bytes);
      return Array.from(new Uint8Array(hash),byte=>byte.toString(16).padStart(2,'0')).join('');
    };
    void Promise.all([fingerprint('registered_user_id',current.registered_user_id),fingerprint('workspace_id',current.workspace_id)])
      .then(([owner,workspace])=>{
        if(!stillCurrent()){clearCurrent();return;}
        const root=document.documentElement;if(!root){clearCurrent();return;}
        root.setAttribute(identityProjectionAttributes[1],owner);root.setAttribute(identityProjectionAttributes[2],workspace);
        root.setAttribute(identityProjectionAttributes[3],current.account_id);
        root.setAttribute(identityProjectionAttributes[0],'verified');
      }).catch(clearCurrent);
  }
  clearIdentityProjection();
  if(identityProjectionEnabled){
    window.addEventListener('navren:identity-revoked',clearIdentityProjection);
    window.addEventListener('pagehide',clearIdentityProjection);
  }
  const reauthenticationKey='navren_bank_reauthentication';
  let reauthentication=null,legacyReauthentication=null,reauthenticationOwner=null;
  try {
    const stored=sessionStorage.getItem(reauthenticationKey);
    if(stored!==null){const value=JSON.parse(stored);legacyReauthentication=value&&Object.keys(value).sort().join(',')==='type,workspace_id'&&value.type==='bank_password'&&typeof value.workspace_id==='string'&&/^[a-f0-9]{32}$/.test(value.workspace_id)?value:{type:'bank_password',workspace_id:null};}
  }catch(_){legacyReauthentication={type:'bank_password',workspace_id:null};}
  const reads=new Set();
  const identityChannel=typeof BroadcastChannel==='function'?new BroadcastChannel('navren-demo-identity'):null;
  if(identityChannel)identityChannel.onmessage=event=>{
    if(event.data?.type==='bank_password_reauthentication'){
      const workspace=event.data.workspace_id,owner=event.data.registered_user_id;
      const trusted=session||welcomeBinding();
      if(!trusted||typeof workspace!=='string'||!/^[a-f0-9]{32}$/.test(workspace)||
        (owner!=null&&owner!==trusted.registered_user_id)||
        (session&&session.workspace_id!==workspace)||
        (owner==null&&(!session||session.workspace_id!==workspace)))return;
      requireBankReauthentication(workspace,false,trusted.registered_user_id);return;
    }
    session=boot=null;clearHistory();window.dispatchEvent(new Event('navren:identity-revoked'));
  };
  function invalidate(){generation++;clearIdentityProjection();for(const c of reads)c.abort();reads.clear();}
  function clearHistory({purgeCache=false,reason='identity_unloaded'}={}){
    // 退出／切号只卸载；显式删除或授权变更仅清当前缓存，归档由服务端管理。
    if(purgeCache){try{Storage.invalidatePrivateCache?.();}catch(_){} }
    try{Storage.unloadPrivateContext?.();}catch(_){}
    invalidate();
    window.dispatchEvent(new CustomEvent('navren:privacy-reset',{detail:{reason,persistent_history_deleted:false}}));
  }
  const registeredId=value=>typeof value==='string'&&/^[a-f0-9]{32}$/.test(value);
  const smsVerificationReasons=new Set(['SMS_CODE_REJECTED','SMS_CODE_INVALID','SMS_CHALLENGE_INVALID','SMS_EXPIRED','SMS_TIMEOUT','SMS_VERIFY_UNAVAILABLE','FINANCIAL_WRITE_LOCKED']);
  function smsVerificationFailure(data,path,method){
    if(method!=='POST'||!/^\/operations\/[a-f0-9]{32}\/confirm$/.test(path)||data?.error?.code!=='SMS_VERIFICATION_FAILED')return null;
    const meta=data.error.sms_verification;
    if(!meta||typeof meta!=='object'||Array.isArray(meta)||Object.keys(meta).sort().join(',')!=='reason_code,same_challenge_retry'||
      !smsVerificationReasons.has(meta.reason_code)||typeof meta.same_challenge_retry!=='boolean'||meta.same_challenge_retry&&meta.reason_code!=='SMS_CODE_REJECTED')return null;
    return Object.freeze({reason_code:meta.reason_code,same_challenge_retry:meta.same_challenge_retry});
  }
  const personaIds=new Set(['tycoon','grain_farmer','working_landlady','retired_entrepreneur']);
  const subjectId=value=>typeof value==='string'&&/^[A-Za-z0-9:_-]{16,192}$/.test(value);
  function leaseCurrent(data){
    const now=Date.now()/1000;
    return Number.isSafeInteger(data?.session_generation)&&data.session_generation>=0&&
      Number.isFinite(data.auth_expires_at)&&Number.isFinite(data.lease_expires_at)&&
      now<data.lease_expires_at&&data.lease_expires_at<=data.auth_expires_at;
  }
  function welcomeBinding(){
    const value=window.NavrenWelcomeSession?.getState?.();
    return value?.authenticated===true&&!value.revoked&&!value.business_revoked&&
      registeredId(value.registered_user_id)&&subjectId(value.auth_subject)?value:null;
  }
  function registeredSession(data){
    if(data?.selection_required===true){const e=Error('请先选择本人体验画像。');e.code='SELECTION_REQUIRED';throw e;}
    if(!data||!registeredId(data.registered_user_id)||!registeredId(data.workspace_id)||
      !personaIds.has(data.account_id)||!subjectId(data.auth_subject)||!leaseCurrent(data)||
      !Number.isSafeInteger(data.privacy_revision)||data.privacy_revision<0||typeof data.csrf!=='string'||!data.csrf)
      {const e=Error('注册用户与体验实例尚未核对。');e.code='UNBOUND_REGISTERED_SESSION';throw e;}
    const welcome=welcomeBinding();
    if(!welcome||welcome.registered_user_id!==data.registered_user_id||welcome.auth_subject!==data.auth_subject)
      {const e=Error('欢迎账户与当前实例不一致。');e.code='UNBOUND_REGISTERED_SESSION';throw e;}
    return data;
  }
  const privacyKey=value=>value?.registered_user_id&&value?.workspace_id
    ?'navren_privacy_'+value.registered_user_id+'_'+value.workspace_id:null;
  const ownerLockKey=owner=>reauthenticationKey+':'+owner;
  function loadOwnerLock(owner){
    reauthenticationOwner=owner;reauthentication=null;
    try{
      const stored=sessionStorage.getItem(ownerLockKey(owner));
      if(stored!==null){const value=JSON.parse(stored);reauthentication=value&&Object.keys(value).sort().join(',')==='registered_user_id,type,workspace_id'&&value.type==='bank_password'&&value.registered_user_id===owner&&registeredId(value.workspace_id)?value:{type:'bank_password',registered_user_id:owner,workspace_id:null};}
    }catch(_){reauthentication={type:'bank_password',registered_user_id:owner,workspace_id:null};}
  }
  function denialLock(){
    const trusted=session||welcomeBinding();
    if(trusted&&registeredId(trusted.registered_user_id)&&reauthenticationOwner!==trusted.registered_user_id)loadOwnerLock(trusted.registered_user_id);
    return legacyReauthentication||reauthentication;
  }
  function requireBankReauthentication(workspace,broadcast=true,owner){
    const trusted=session||welcomeBinding();
    owner=owner||trusted?.registered_user_id;
    if(!trusted||!registeredId(owner)||owner!==trusted.registered_user_id||!registeredId(workspace)||session&&session.workspace_id!==workspace)throw Error('银行重验证工作区无法核对。');
    reauthenticationOwner=owner;reauthentication={type:'bank_password',registered_user_id:owner,workspace_id:workspace};
    // 仅保存阻断元信息，不能据此认证；损坏记录须拒绝默认初始化。
    try{sessionStorage.setItem(ownerLockKey(owner),JSON.stringify(reauthentication));}catch(_){}
    session=boot=null;clearHistory();
    if(broadcast)identityChannel?.postMessage({type:'bank_password_reauthentication',registered_user_id:owner,workspace_id:workspace});
    window.dispatchEvent(new CustomEvent('navren:bank-reauthentication-required',{detail:{workspace_id:workspace}}));
  }
  function assertBankAccess(){const lock=denialLock();if(lock){const error=new Error(lock.workspace_id?'银行访问会话已失效，请验证银行访问密码或明确选择账户。':'银行重验证记录无法核对，请明确选择账户。');error.code='BANK_RELOGIN_REQUIRED';throw error;}}
  function liveIdentity(){
    if(!session)return null;
    try{const current=session;registeredSession(current);if(session!==current)return null;projectVerifiedIdentity(current);return {...current};}
    catch(_){session=boot=null;clearHistory({reason:'registered_session_invalid'});return null;}
  }
  window.addEventListener('storage',e=>{
    if(session&&e.key===privacyKey(session)&&e.newValue!==String(session.privacy_revision)){
      session=boot=null;clearHistory();
    }
  });
  function checkPrivacy(data,path,method){
    if(data.privacy_revision!=null&&data.workspace_id){
      const key=privacyKey(data);if(!key)return;const old=localStorage.getItem(key);
      if(old!=null&&Number(old)!==data.privacy_revision)clearHistory({purgeCache:true,reason:'privacy_changed'});
      localStorage.setItem(key,String(data.privacy_revision));
      if(session?.workspace_id===data.workspace_id)session.privacy_revision=data.privacy_revision;
    }
    if(method==='POST'&&(/\/memories\/[^/]+\/(delete|correct)$|\/memory-relations\/[^/]+\/delete$|\/profile-observations\//.test(path)||path==='/memories/settings'))clearHistory({purgeCache:true,reason:'private_data_changed'});
  }
  const prefix = '/api/finance';
  function assertRequestCurrent(requestGeneration,requestWorkspace,requestCsrf,requestOwner,requestSubject,requestServerGeneration,requestAuthExpiry,requestLeaseExpiry){
    if(requestGeneration!==generation||requestWorkspace!==session?.workspace_id||requestCsrf!==session?.csrf||requestOwner!==session?.registered_user_id||requestSubject!==session?.auth_subject||
      requestServerGeneration!==session?.session_generation||requestAuthExpiry!==session?.auth_expires_at||requestLeaseExpiry!==session?.lease_expires_at){
      const error=new Error('页面、账户或资料授权已变化，已丢弃旧响应。');error.code='STALE_RESPONSE';throw error;
    }
    if(requestWorkspace&&!leaseCurrent({session_generation:requestServerGeneration,auth_expires_at:requestAuthExpiry,lease_expires_at:requestLeaseExpiry})){
      session=boot=null;clearHistory({reason:'registered_lease_expired'});const error=Error('当前体验会话已到期，请重新核验。');error.code='REGISTERED_SESSION_EXPIRED';throw error;
    }
  }
  async function raw(path, method = 'GET', body) {
    const requestGeneration=generation,requestWorkspace=session?.workspace_id,requestCsrf=session?.csrf,requestOwner=session?.registered_user_id,requestSubject=session?.auth_subject,
      requestServerGeneration=session?.session_generation,requestAuthExpiry=session?.auth_expires_at,requestLeaseExpiry=session?.lease_expires_at;
    const controller = new AbortController();
    if(method==='GET')reads.add(controller);
    // 公开业绩可能先刷新目录再读取管理人；不占用其他请求的等待预算。
    const timeout = method === 'GET' && ['/public-product-performance','/public-research-products'].includes(path.split('?')[0]) ? 45000 : 15000;
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const headers = { 'Accept': 'application/json' };
      if (method !== 'GET') { headers['Content-Type'] = 'application/json'; if (session) headers['X-QZ-CSRF'] = session.csrf; }
      if (method === 'POST' && path === '/account-state' && session) {
        headers['X-Navren-Workspace'] = session.workspace_id;
        headers['X-Navren-Privacy-Revision'] = String(session.privacy_revision);
      }
      const response = await fetch(prefix + path, { method, credentials: 'same-origin', cache: 'no-store', headers,
        body: method === 'GET' ? undefined : JSON.stringify(body || {}), signal: controller.signal });
      assertRequestCurrent(requestGeneration,requestWorkspace,requestCsrf,requestOwner,requestSubject,requestServerGeneration,requestAuthExpiry,requestLeaseExpiry);
      const data = await response.json();
      assertRequestCurrent(requestGeneration,requestWorkspace,requestCsrf,requestOwner,requestSubject,requestServerGeneration,requestAuthExpiry,requestLeaseExpiry);
      if (!response.ok) {
        const error = new Error(data.error?.code==='SMS_VERIFICATION_FAILED'?'验证码未通过验证，请检查或重新获取。':data.error?.message || '银行请求失败');
        error.code = data.error?.code; error.status = response.status;
        const smsFailure=smsVerificationFailure(data,path,method);if(smsFailure)error.sms_verification=smsFailure;
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
      if(data.registered_user_id!=null&&requestOwner&&data.registered_user_id!==requestOwner){session=boot=null;clearHistory();const e=Error('注册用户已变化，已丢弃旧响应。');e.code='STALE_IDENTITY';throw e;}
      const binding={registered_user_id:data.registered_user_id||requestOwner,workspace_id:headerWorkspace||data.workspace_id,
        privacy_revision:headerRevision!=null?Number(headerRevision):data.privacy_revision};
      if(binding.workspace_id&&requestWorkspace&&binding.workspace_id!==requestWorkspace&&path!=='/session'&&path!=='/session/reset'){
        session=boot=null;clearHistory();const e=new Error('账户身份已变化，请重新载入视图。');e.code='STALE_IDENTITY';throw e;
      }
      if(path==='/session/reset'){registeredSession(data);clearHistory();session=data;boot=null;}
      if(Array.isArray(data)&&requestWorkspace&&(!binding.workspace_id||!Number.isInteger(binding.privacy_revision))){const e=new Error('资料身份或授权版本缺失，请重新核对。');e.code='UNBOUND_RESPONSE';throw e;}
      checkPrivacy(binding,path,method);
      if(path==='/session/reset')projectVerifiedIdentity(session);
      return path.startsWith('/graph/evidence')||['/account-state','/personal-profile'].includes(path)?data:NavrenPresentation.data(data);
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('银行请求超时。请查询操作状态，暂勿重复提交。');
      throw error;
    } finally { clearTimeout(timer);reads.delete(controller); }
  }
  async function rawText(path, text, contentType = 'text/csv; charset=utf-8') {
    await ensure();
    const requestGeneration=generation,requestWorkspace=session.workspace_id,requestCsrf=session.csrf,requestOwner=session.registered_user_id,requestSubject=session.auth_subject,
      requestServerGeneration=session.session_generation,requestAuthExpiry=session.auth_expires_at,requestLeaseExpiry=session.lease_expires_at;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(prefix + path, { method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'Accept': 'application/json', 'Content-Type': contentType, 'X-QZ-CSRF': requestCsrf },
        body: text, signal: controller.signal });
      assertRequestCurrent(requestGeneration,requestWorkspace,requestCsrf,requestOwner,requestSubject,requestServerGeneration,requestAuthExpiry,requestLeaseExpiry);
      const data = await response.json();
      assertRequestCurrent(requestGeneration,requestWorkspace,requestCsrf,requestOwner,requestSubject,requestServerGeneration,requestAuthExpiry,requestLeaseExpiry);
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
    if(session){const current=liveIdentity();assertBankAccess();if(current)return session;const error=Error('当前体验会话已失效，请重新核验。');error.code='UNBOUND_REGISTERED_SESSION';throw error;}
    if (!boot) {
      const started=generation,pending=(async()=>{
        if(typeof window.NavrenWelcomeSession?.refresh!=='function'){const error=Error('欢迎注册会话尚未核对。');error.code='UNBOUND_REGISTERED_SESSION';throw error;}
        await window.NavrenWelcomeSession.refresh();
        const welcome=welcomeBinding();
        if(!welcome){const error=Error('欢迎注册会话尚未核对。');error.code='UNBOUND_REGISTERED_SESSION';throw error;}
        if(started!==generation){const error=Error('旧会话初始化已失效。');error.code='STALE_RESPONSE';throw error;}
        loadOwnerLock(welcome.registered_user_id);assertBankAccess();
        // The welcome gateway already signed the lease; cold start only reads it.
        const next=await raw('/session');
        assertBankAccess();if(started!==generation){const error=Error('旧会话初始化已失效。');error.code='STALE_RESPONSE';throw error;}
        registeredSession(next);session=next;projectVerifiedIdentity(session);return session;
      })();
      const guarded=pending.catch(e=>{if(boot===guarded)boot=null;throw e;});boot=guarded;
    }
    return boot;
  }
  return {
    ensure,
    async reselectExperience(){
      if(reselecting){const error=Error('正在重新核对体验画像，请稍候。');error.code='EXPERIENCE_RESELECT_BUSY';throw error;}
      reselecting=true;
      const started=generation,controller=new AbortController();
      let timer;
      const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();const error=Error('体验画像重新核对超时，请重试。');error.code='EXPERIENCE_RESELECT_TIMEOUT';reject(error);},15000);});
      const wait=promise=>Promise.race([promise,deadline]);
      reads.add(controller);
      try{
        const initial=welcomeBinding();
        const welcomeApi=window.NavrenWelcomeSession;
        if(typeof welcomeApi?.refresh!=='function'){const error=Error('请返回欢迎页核验注册账户。');error.code='EXPERIENCE_SELECTION_REQUIRED';throw error;}
        await wait(welcomeApi.refresh());
        const welcome=welcomeBinding(),identity=welcome?.current_business_identity;
        if(started!==generation||(initial&&(!welcome||initial.registered_user_id!==welcome.registered_user_id||initial.auth_subject!==welcome.auth_subject))){const error=Error('账户已变化，已停止重新选择。');error.code='STALE_RESPONSE';throw error;}
        if(!welcome||welcome.business_identity_status!=='verified'||!identity||identity.environment!=='simulation'||
          identity.registered_user_id!==welcome.registered_user_id||identity.auth_subject!==welcome.auth_subject||
          !personaIds.has(identity.account_id)||!registeredId(identity.workspace_id)||
          !Number.isSafeInteger(identity.privacy_revision)||identity.privacy_revision<0||!leaseCurrent(identity)||
          typeof welcome.csrf!=='string'||!welcome.csrf){const error=Error('当前体验画像尚未核验，请返回欢迎页选择。');error.code='EXPERIENCE_SELECTION_REQUIRED';throw error;}
        const binding={registered_user_id:welcome.registered_user_id,auth_subject:welcome.auth_subject,account_id:identity.account_id,csrf:welcome.csrf};
        const check=()=>{
          const current=welcomeBinding();
          if(started!==generation||controller.signal.aborted||!current||current.registered_user_id!==binding.registered_user_id||
            current.auth_subject!==binding.auth_subject||current.csrf!==binding.csrf||!leaseCurrent(identity)){
            const error=Error('账户或授权已变化，已丢弃重新选择结果。');error.code='STALE_RESPONSE';throw error;
          }
        };
        check();
        const response=await wait(fetch('/api/welcome/auth/experience',{method:'POST',credentials:'same-origin',cache:'no-store',
          headers:{'Accept':'application/json','Content-Type':'application/json','X-Welcome-CSRF':binding.csrf},
          body:JSON.stringify({user_id:binding.account_id}),signal:controller.signal}));
        check();const selected=await wait(response.json());check();
        if(!response.ok){const error=Error(selected?.error?.message||'重新选择体验画像失败。');error.code=selected?.error?.code;throw error;}
        const business=selected?.current_business_identity;
        if(!selected||selected.authenticated!==true||selected.redirect!=='/chat.html'||selected.registered_user_id!==binding.registered_user_id||selected.auth_subject!==binding.auth_subject||
          selected.business_identity_status!=='verified'||selected.selection_required!==false||!business||business.environment!=='simulation'||
          business.registered_user_id!==binding.registered_user_id||business.auth_subject!==binding.auth_subject||business.account_id!==binding.account_id||
          !registeredId(business.workspace_id)||selected.workspace_id!==business.workspace_id||selected.account_id!==binding.account_id||
          !Number.isSafeInteger(business.privacy_revision)||business.privacy_revision<0||!leaseCurrent(business)){
          const error=Error('重新选择响应与当前注册画像不一致。');error.code='UNBOUND_REGISTERED_SESSION';throw error;
        }
        check();
        const sessionResponse=await wait(fetch(prefix+'/session',{method:'GET',credentials:'same-origin',cache:'no-store',headers:{'Accept':'application/json'},signal:controller.signal}));
        check();const fresh=await wait(sessionResponse.json());check();
        if(!sessionResponse.ok){const error=Error(fresh?.error?.message||'体验租约尚未核实。');error.code=fresh?.error?.code;throw error;}
        registeredSession(fresh);check();
        if(fresh.environment!=='simulation'||fresh.registered_user_id!==binding.registered_user_id||fresh.auth_subject!==binding.auth_subject||
          fresh.account_id!==binding.account_id||fresh.workspace_id!==business.workspace_id||fresh.privacy_revision!==business.privacy_revision||
          fresh.session_generation!==business.session_generation||fresh.auth_expires_at!==business.auth_expires_at||fresh.lease_expires_at!==business.lease_expires_at){
          const error=Error('体验租约与明确选择结果不一致。');error.code='UNBOUND_REGISTERED_SESSION';throw error;
        }
        check();
        // Only this explicit, fully read-back selection may clear an unknown lock.
        return window.BankClient.acceptSelectedSession(fresh);
      }finally{clearTimeout(timer);reads.delete(controller);reselecting=false;}
    },
    requireBankReauthentication,
    bankReauthenticationWorkspace:()=>denialLock()?.workspace_id||null,
    bankReauthenticationRequired:()=>!!denialLock(),
    beginAccountSwitch(){invalidate();session=boot=null;identityChannel?.postMessage({type:'invalidate'});},
    acceptSelectedSession(data,{bank_reauthentication=false}={}){
      registeredSession(data);
      // Reload only this verified owner's denial record; never claim a legacy lock.
      loadOwnerLock(data.registered_user_id);
      if(bank_reauthentication&&((!legacyReauthentication&&!reauthentication)||
        (legacyReauthentication&&legacyReauthentication.workspace_id!==data.workspace_id)||
        (reauthentication&&(reauthentication.registered_user_id!==data.registered_user_id||reauthentication.workspace_id!==data.workspace_id))))throw Error('银行恢复会话与原工作区不一致。');
      try{sessionStorage.removeItem(ownerLockKey(data.registered_user_id));if(legacyReauthentication)sessionStorage.removeItem(reauthenticationKey);}catch(_){throw Error('银行重验证锁无法清除，请重试。');}
      reauthentication=legacyReauthentication=null;reauthenticationOwner=data.registered_user_id;
      clearHistory();session={...data};boot=null;projectVerifiedIdentity(session);return {...session};
    },
    identity(){return liveIdentity();},
    async refreshIdentity(){
      await ensure();const next=await raw('/session');
      registeredSession(next);
      if(session&&['workspace_id','registered_user_id','auth_subject','session_generation','csrf','auth_expires_at','lease_expires_at'].some(key=>session[key]!==next[key]))clearHistory();
      session=next;projectVerifiedIdentity(session);return {...session};
    },
    guard(){
      const snapshot=liveIdentity(),gen=generation,view=window.NavrenViews?.revision();
      return {registered_user_id:snapshot?.registered_user_id,workspace_id:snapshot?.workspace_id,privacy_revision:snapshot?.privacy_revision,generation:gen,
        valid(data){const current=liveIdentity();return !!snapshot&&!!current&&leaseCurrent(snapshot)&&gen===generation&&(!window.NavrenViews||view===NavrenViews.revision())&&
          snapshot.registered_user_id===session?.registered_user_id&&snapshot.auth_subject===session?.auth_subject&&
          snapshot.session_generation===session?.session_generation&&snapshot.csrf===session?.csrf&&
          snapshot.workspace_id===session?.workspace_id&&snapshot.privacy_revision===session?.privacy_revision&&
          (!data||data.workspace_id===snapshot.workspace_id&&data.privacy_revision===snapshot.privacy_revision&&
            (data.registered_user_id==null||data.registered_user_id===snapshot.registered_user_id)&&
            (data.auth_subject==null||data.auth_subject===snapshot.auth_subject)&&
            (data.session_generation==null||data.session_generation===snapshot.session_generation)&&
            (data.auth_expires_at==null||data.auth_expires_at===snapshot.auth_expires_at)&&
            (data.lease_expires_at==null||data.lease_expires_at===snapshot.lease_expires_at));}};
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
      if (!Number.isSafeInteger(value) || value < 0 || value > 100000000) throw new Error('金额超出当前业务范围。');
      return value;
    }
  };
})();
