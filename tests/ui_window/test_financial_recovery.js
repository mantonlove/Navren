/* S04 新增原生恢复接线；test_double 禁网，不发送真实短信、不访问现役身份。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'../..'),ui=fs.readFileSync(path.join(root,'js/bank_ui.js'),'utf8');
const workspaceSource=fs.readFileSync(path.join(root,'js/account_workspace.js'),'utf8');
const PURPOSE='financial_write_unlock',RID='a'.repeat(32),CID='b'.repeat(32),TOKEN='T'.repeat(43),TEST_ONLY_CODE='246810';
const groups=[];let assertions=0,fixtures=0;
function check(value,message){assert.ok(value,message);assertions++;}
function equal(a,b,message){assert.deepEqual(a,b,message);assertions++;}
const settle=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
function fixture(options={}){
  fixtures++;
  const dom=new JSDOM('<!doctype html><span class="header-location"></span><h1 id="accountViewTitle"></h1><div><a id="accountBack"></a></div><div id="accountHome"><div><h2 id="accountAssetsHeading">我的资产</h2></div></div><div id="accountSubview"></div><div id="accountLegacyViews"></div>',{url:'https://test-double.invalid/account.html',runScripts:'outside-only'});
  const w=dom.window,d=w.document,calls=[],storage=[],network=[],logs=[],timers=new Map();let clock=Date.now(),live=true,view={view:'home'},timerId=0;
  // 本轮只运行新增原生接线；旧账户启动及旧 UI 验收不重复执行。
  const add=d.addEventListener.bind(d);d.addEventListener=(name,...args)=>{if(name!=='DOMContentLoaded')add(name,...args);};
  w.Date.now=()=>clock;w.setInterval=fn=>{timers.set(++timerId,fn);return timerId;};w.clearInterval=id=>timers.delete(id);
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
  w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'));};
  w.Storage.prototype.setItem=function(){storage.push('write');throw Error('persistent storage forbidden');};
  w.fetch=()=>{network.push('fetch');throw Error('network forbidden');};
  for(const name of ['log','info','warn','error','debug'])w.console[name]=()=>logs.push(name);
  w.NavrenPresentation={name:v=>String(v??''),text:v=>String(v??'')};
  const identity={workspace_id:'test-double-workspace',privacy_revision:1};
  const status=()=>({contract:'navren.security-event/1',workspace_id:identity.workspace_id,financial_write_locked:true,recovery_configured:true,failures:3,threshold:3,reads_and_original_receipt_recovery_allowed:true,login_or_wait_unlocks:false,...options.status});
  const challenge=()=>({purpose:PURPOSE,recovery_id:RID,challenge_id:CID,source:'test_double',expires_at:clock/1000+120,real_SMS_acceptance:false,...options.challenge});
  const proof=()=>({purpose:PURPOSE,recovery_id:RID,recovery_token:TOKEN,source:'test_double',expires_at:clock/1000+100,real_SMS_acceptance:false,...options.proof});
  const success=()=>({purpose:PURPOSE,recovery_id:RID,state:'recovered',financial_write_locked:false,failures:0,source:'test_double',real_SMS_acceptance:false,...options.success});
  w.BankClient={
    identity:()=>live?identity:null,bankReauthenticationWorkspace:()=>null,
    guard:()=>({workspace_id:identity.workspace_id,valid:()=>live}),
    get:async route=>{calls.push({method:'GET',route});if(options.get)return options.get(route,status);return status();},
    post:async(route,body)=>{calls.push({method:'POST',route,body:JSON.parse(JSON.stringify(body))});if(options.post)return options.post(route,body,{challenge,proof,success});
      if(route==='/security/recovery/sms')return challenge();
      if(route==='/security/recovery/'+RID+'/verify')return proof();
      if(route==='/security/recovery/confirm')return success();
      throw Error('unrelated write forbidden');}
  };
  w.NavrenViews={revision:()=>1,current:()=>view,register:()=>{},back:()=>{},open:()=>{}};
  w.NavrenAccountHome={render:async()=>{},clear:()=>{}};
  w.NavrenConnectedAssets={scope:'connected_bank',clear:()=>{}};
  w.NavrenConnectedAssetsUI={render:async host=>host.append(d.createElement('h2'))};
  w.eval(ui);
  const button=text=>[...d.querySelectorAll('button')].find(b=>b.textContent===text);
  const dialog=()=>d.querySelector('dialog');
  async function open(){w.BankUI.financialRecoveryForm();await settle();}
  async function send(){await button('发送恢复验证码').onclick();await settle();}
  async function verify(){const input=d.querySelector('input');input.value=TEST_ONLY_CODE;input.dispatchEvent(new w.Event('input'));await button('验证验证码').onclick();await settle();}
  function noLeaks(){
    equal(storage,[],'验证码和证明不持久化');equal(network,[],'不访问网络');equal(logs,[],'不向控制台输出证明');
    const attributes=[...d.querySelectorAll('*')].flatMap(n=>[...n.attributes].map(a=>a.value)).join('|');
    check(!attributes.includes(TOKEN)&&!attributes.includes(TEST_ONLY_CODE),'DOM 属性没有验证码或恢复证明');
    check(!d.body.textContent.includes(TOKEN)&&!d.body.textContent.includes(TEST_ONLY_CODE),'可见正文没有验证码或恢复证明');
    check(!w.location.href.includes(TOKEN)&&!w.location.href.includes(TEST_ONLY_CODE),'URL 无验证资料');
  }
  return {w,d,calls,options,button,dialog,open,send,verify,noLeaks,setLive:v=>live=v,setView:v=>view=v,
    advance:ms=>{clock+=ms;for(const fn of [...timers.values()])fn();},close:()=>{dialog()?.finish();noLeaks();dom.window.close();}};
}
async function group(name,fn){await fn();groups.push(name);}
(async()=>{
  await group('FR01 明确点击及单次消费',async()=>{
    const f=fixture();await f.open();equal(f.calls.map(x=>x.method),['GET'],'打开只查询，不发短信');
    check(f.button('确认恢复操作权限').hidden,'验证前隐藏恢复确认');await f.send();
    equal(f.calls[1].body,{purpose:PURPOSE,confirmed:true},'短信请求仅有固定用途和本人确认');
    check(f.d.body.textContent.includes('本地替身通道'),'替身通道明确标注');
    const input=f.d.querySelector('input');input.value='123';input.dispatchEvent(new f.w.Event('input'));
    check(f.button('验证验证码').disabled,'不完整验证码不能验证');await f.verify();
    equal(Object.keys(f.calls[2].body).sort(),['challenge_id','purpose','verification_code'],'验证体不增加主体或其他字段');
    equal(f.calls[2].route,'/security/recovery/'+RID+'/verify','精确挑战对象路径');
    check(input.value===''&&f.d.body.textContent.includes('操作限制尚未解除'),'验证后清空输入且尚未解锁');
    equal(f.calls.length,3,'验证不自动确认');f.noLeaks();
    await f.button('确认恢复操作权限').onclick();await settle();
    equal(Object.keys(f.calls[3].body).sort(),['confirmed','purpose','recovery_id','recovery_token'],'最终确认精确字段');
    check(f.d.body.textContent.includes('操作权限已恢复，未执行任何交易'),'只在完整确认响应后显示恢复');
    await f.button('确认恢复操作权限').onclick();equal(f.calls.length,4,'确认不重复消费或执行原交易');f.close();
  });
  await group('FR02 未配置、后端未加载与未锁定',async()=>{
    for(const options of [{status:{recovery_configured:false}},{status:{financial_write_locked:false}},{get:()=>{throw Object.assign(Error('unavailable'),{code:'NOT_FOUND',status:404});}}]){
      const f=fixture(options);await f.open();check(f.button('发送恢复验证码').disabled,'不可用状态不能发短信');
      check(!f.d.body.textContent.includes('操作权限已恢复'),'不把不可用猜成成功');equal(f.calls.length,1,'仅原查询');f.close();
    }
  });
  await group('FR03 状态结构和工作区绑定',async()=>{
    for(const status of [{workspace_id:'other-workspace'},{contract:'wrong'},{financial_write_locked:1},{recovery_configured:'true'},{failures:-1},{reads_and_original_receipt_recovery_allowed:false}]){
      const f=fixture({status});await f.open();check(f.button('发送恢复验证码').disabled,'未绑定状态保持不可办理');equal(f.calls.length,1,'不调用写入口');f.close();
    }
    const f=fixture();f.setLive(false);await f.open();check(!f.dialog(),'失效会话不打开本人弹窗');equal(f.calls.length,0,'失效会话不查询恢复');f.close();
  });
  await group('FR04 挑战用途、来源及期限拒绝',async()=>{
    for(const challenge of [{purpose:'operation'},{recovery_id:'wrong'},{challenge_id:'wrong'},{source:'unknown'},{workspace_id:'other'},{expires_at:1}]){
      const f=fixture({challenge});await f.open();await f.send();check(f.button('确认恢复操作权限').hidden,'错误挑战不能进入确认');
      check(f.d.querySelector('input').value==='','错误挑战无验证码残留');equal(f.calls.length,2,'没有后续验证或交易');f.close();
    }
  });
  await group('FR05 错码、证明串用和格式拒绝',async()=>{
    for(const proof of [{purpose:'operation'},{recovery_id:'c'.repeat(32)},{source:'provider'},{recovery_token:'bad'},{expires_at:1}]){
      const f=fixture({proof});await f.open();await f.send();await f.verify();check(f.button('确认恢复操作权限').hidden,'不接受不匹配证明');
      equal(f.calls.length,3,'错误证明不确认');check(f.d.querySelector('input').value==='','验证输入及时清空');f.close();
    }
    const f=fixture({post:(route,body,r)=>{if(route.endsWith('/verify'))throw Object.assign(Error(TEST_ONLY_CODE),{code:'RECOVERY_VERIFICATION_FAILED'});return r.challenge();}});
    await f.open();await f.send();await f.verify();f.noLeaks();check(f.d.body.textContent.includes('本次验证未通过'),'显示静态拒绝信息，不回显错误原件');f.close();
  });
  await group('FR06 到期立即失效且不自动续发',async()=>{
    for(const verified of [false,true]){
      const f=fixture();await f.open();await f.send();if(verified)await f.verify();const before=f.calls.length;
      f.advance(121000);check(f.d.body.textContent.includes('已过期'),'到期显示失效');
      check(f.d.querySelector('input').value==='','到期清空输入');check(f.button('确认恢复操作权限').hidden,'到期无可消费证明');equal(f.calls.length,before,'到期不自动发短信或确认');f.close();
    }
  });
  await group('FR07 关闭及身份、隐私、视图离开清理',async()=>{
    for(const event of ['close','navren:privacy-reset','navren:identity-revoked','navren:account-switched','navren:view','navren:bank-session-revoked','navren:bank-reauthentication-required','pagehide','beforeunload','popstate','hashchange']){
      const f=fixture();await f.open();await f.send();await f.verify();const oldConfirm=f.button('确认恢复操作权限'),count=f.calls.length;
      if(event==='close')f.dialog().finish();else f.w.dispatchEvent(new f.w.Event(event));
      check(!f.dialog(),'离开事件关闭弹窗');await oldConfirm.onclick();equal(f.calls.length,count,'旧确认按钮不能消费证明');f.close();
    }
  });
  await group('FR08 异步旧响应不会复活已关闭会话',async()=>{
    for(const stage of ['GET','sms','verify','confirm']){
      let release;const waiting=new Promise(resolve=>release=resolve);
      const f=fixture({get:(route,status)=>stage==='GET'?waiting:status(),post:(route,body,r)=>{
        if(route.endsWith('/sms'))return stage==='sms'?waiting:r.challenge();
        if(route.endsWith('/verify'))return stage==='verify'?waiting:r.proof();
        return stage==='confirm'?waiting:r.success();}});
      f.w.BankUI.financialRecoveryForm();await settle();
      if(stage!=='GET'){f.button('发送恢复验证码').onclick();await settle();}
      if(['verify','confirm'].includes(stage)){const pin=f.d.querySelector('input');pin.value=TEST_ONLY_CODE;pin.dispatchEvent(new f.w.Event('input'));f.button('验证验证码').onclick();await settle();}
      if(stage==='confirm'){f.button('确认恢复操作权限').onclick();await settle();}
      const count=f.calls.length;f.dialog().finish();release({purpose:PURPOSE,recovery_id:RID,challenge_id:CID,recovery_token:TOKEN,source:'test_double',expires_at:Date.now()/1000+60,real_SMS_acceptance:false});await settle();
      check(!f.dialog(),'旧响应不能重新打开弹窗');equal(f.calls.length,count,'旧响应不触发下一请求');f.close();
    }
  });
  await group('FR09 无事件的工作区守卫失效',async()=>{
    const f=fixture();await f.open();await f.send();await f.verify();const old=f.button('确认恢复操作权限'),count=f.calls.length;
    f.setLive(false);f.advance(1);check(!f.dialog(),'守卫失效主动清理');await old.onclick();equal(f.calls.length,count,'失效守卫不能确认');f.close();
  });
  await group('FR10 待处理防重及确认结果未知',async()=>{
    let release;const pending=new Promise(resolve=>release=resolve);
    const f=fixture({post:(route,body,r)=>route.endsWith('/sms')?pending:route.endsWith('/verify')?r.proof():Promise.reject(Error('lost response'))});
    await f.open();const first=f.button('发送恢复验证码').onclick();await f.button('发送恢复验证码').onclick();
    equal(f.calls.filter(c=>c.method==='POST').length,1,'发送过程中不重复提交');
    release({purpose:PURPOSE,recovery_id:RID,challenge_id:CID,source:'test_double',expires_at:Date.now()/1000+120,real_SMS_acceptance:false});await first;await f.verify();
    await f.button('确认恢复操作权限').onclick();await settle();check(f.d.body.textContent.includes('恢复结果尚未核对'),'丢失确认响应保持未知');
    check(!f.d.body.textContent.includes('操作权限已恢复'),'未知结果不能冒充成功');const count=f.calls.length;
    await f.button('确认恢复操作权限').onclick();equal(f.calls.length,count,'未知结果不自动重放证明');await f.button('重新核对').onclick();
    equal(f.calls.at(-1).method,'GET','唯一恢复动作是重新查询');f.close();
  });
  await group('FR11 确认响应必须完整同对象',async()=>{
    for(const success of [{state:'verified'},{financial_write_locked:true},{failures:1},{recovery_id:'c'.repeat(32)},{purpose:'operation'},{source:'provider'}]){
      const f=fixture({success});await f.open();await f.send();await f.verify();await f.button('确认恢复操作权限').onclick();await settle();
      check(!f.d.body.textContent.includes('操作权限已恢复'),'错误确认响应不显示恢复');check(f.d.body.textContent.includes('恢复结果尚未核对'),'错误确认响应要求人工重新查询');f.close();
    }
  });
  await group('FR12 账户原生入口及挂失入口保留',async()=>{
    const f=fixture();f.w.eval(workspaceSource);
    const state={workspace_id:'test-double-workspace',accounts:[{id:'account-exact'}],cards:[{id:'card-exact',account_id:'account-exact',status:'active'}],as_of:'2026-10-20'};
    f.setView({view:'card-detail',object_id:'card-exact',source_scope:'connected_bank'});
    f.w.dispatchEvent(new f.w.CustomEvent('navren:account-rendered',{detail:{state,products:[]}}));await settle();
    check(f.button('挂失此卡'),'原精确卡片挂失入口保留');check(f.button('验证并恢复操作权限'),'银行卡详情具有本人恢复入口');
    f.setView({view:'home'});await f.w.AccountWorkspace.render({view:'home'});await f.w.AccountWorkspace.render({view:'home'});
    equal(f.d.querySelectorAll('[data-financial-recovery-entry]').length,1,'首页入口只创建一次，不随数字刷新堆叠');
    f.d.querySelector('[data-financial-recovery-entry]').click();await settle();check(f.dialog(),'首页按钮进入同一原生弹窗');equal(f.calls.length,1,'入口只查询一次恢复状态');f.close();
  });
  console.log(JSON.stringify({status:'PASS_LOCAL_TEST_DOUBLE_ONLY',groups,fixtures,assertions,model_api_calls:0,live_api_calls:0,real_sms_sent:0,current_entry_accepted:false}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
