/** 基座HTTP适配：身份来自Cookie，界面只能选择固定演示账户，不能指定工作区授权。 */
(() => {
  async function select(userId, expected) {
    const current=await BankClient.ensure();
    if(current.workspace_id!==expected.expected_workspace_id)throw Error('账户已变化，请刷新后再试。');
    BankClient.beginAccountSwitch();
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
    try {
      const response=await fetch('/api/finance/demo/select',{method:'POST',credentials:'same-origin',cache:'no-store',
        headers:{'Accept':'application/json','Content-Type':'application/json','X-QZ-CSRF':current.csrf},
        body:JSON.stringify({user_id:userId,...expected}),signal:controller.signal});
      const result=await response.json();
      if(!response.ok)throw Error(result.error?.message||'账户切换失败，请重新核对身份。');
      if(result.account_id!==userId)throw Error('账户选择结果与服务端身份不一致。');
      return BankClient.acceptSelectedSession(result);
    } finally {clearTimeout(timer);}
  }
  const adapter={safe_switch:true,identity:()=>BankClient.refreshIdentity(),
    catalogue:async()=>{
      const data=await BankClient.get('/demo/accounts');
      return {...data,schema_version:1,accounts:data.accounts.map(a=>({...a,account_id:a.user_id,label:a.display_name}))};
    },select};
  window.NavrenBaseAccountAdapter=adapter;
  const start=()=>{if(window.NavrenAccountSwitcher&&window.BankClient)NavrenAccountSwitcher.configure(adapter);};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start);else start();
})();
