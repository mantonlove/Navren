/** 演示账户入口。目录和切换由受信服务适配器提供；界面不建立账户、不保存业务状态。 */
window.NavrenAccountSwitcher = (() => {
  const ids = new Set(['tycoon','grain_farmer','working_landlady','retired_entrepreneur']);
  const words = {
    '演示账户':'Demo account','选择账户':'Choose account','账户目录尚未接通':'Account directory is not connected yet',
    '正在核对账户…':'Verifying account…','正在切换账户…':'Switching account…','重试':'Retry','取消':'Cancel',
    '继续切换':'Continue switching','切换账户':'Switch account','切换账号':'Switch account','关闭':'Close','当前账户':'Current account',
    '账户身份暂时无法核对':'Account identity could not be verified',
    '账户目录身份不一致':'The account directory identity does not match',
    '账户目录不完整':'The account directory is incomplete',
    '切换结果尚未核实，请重试身份核对':'The switch result is unverified. Retry identity verification.',
    '正在处理当前任务，请完成后切换':'Finish the current task before switching accounts',
    '正在核对操作状态，请取得结果后切换':'Wait for the operation status before switching accounts',
    '录音将中止，未发送音频不会自动提交。':'Recording will stop. Unsent audio will not be submitted.',
    '当前播放将停止。':'Current audio playback will stop.',
    '输入框中的未发送内容会清除。':'Unsent input will be cleared.',
    '未确认操作仍归原账户，不会执行或迁移。':'Unconfirmed operations stay with the original account and will not execute or move.',
    '切换后重新核对身份、资料和权限。':'Identity, data and permissions will be verified again.',
    '四账户目录来自基座服务；不是生产实名登录。':'The four-account directory comes from the demo service; it is not a production login.',
    '已切换账户，旧上下文已清除':'Account switched. Previous context has been cleared',
    '账户已在其他页面切换，请重新核对身份':'The account changed in another page. Verify identity again.',
    '目录资料来源':'Directory source',
    '体验用户为虚拟身份，账户与财务数据均为模拟数据，资金操作仅在体验环境内生效；公开资料以标注来源为准。':'Experience users are fictional. Account and financial data are simulated, and financial actions only affect the experience environment. Public information retains its stated sources.'
  };
  const t = s => window.NavrenI18n?.language === 'en' ? words[s] || window.NavrenI18n.t(s) : s;
  function n(tag,text,cls){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null){e.textContent=t(text);e.dataset.i18nSkip='';}return e;}
  function button(text,fn){const e=n('button',text);e.type='button';e.onclick=fn;return e;}
  let configured=null,pageInstance=null;
  const instances=new Set();
  function activity(){
    const media=[...document.querySelectorAll('.voice-message audio')].some(a=>!a.paused);
    return {recording:window.NavrenSpeech?.activeKind==='asr',playing:media||window.NavrenSpeech?.activeKind==='tts',
      sending:typeof chatSending!=='undefined'&&chatSending,operation:window.BankUI?.switchState?.()||{},
      input:!!document.getElementById('userInput')?.value.trim()};
  }
  function quiesce(){
    if(typeof cancelVoice==='function')cancelVoice();else window.NavrenSpeech?.stop();
    document.querySelectorAll('.voice-message audio').forEach(a=>a.pause());
    for(const id of ['userInput','imageInput']){const e=document.getElementById(id);if(e)e.value='';}
    if(typeof removeImage==='function')removeImage();
    for(const key of ['navren_return_view','navren_research_selection','navren_calendar_question'])sessionStorage.removeItem(key);
    window.dispatchEvent(new Event('navren:privacy-reset'));
  }
  function confirmSwitch(state){
    return new Promise(resolve=>{
      const d=n('dialog',null,'account-switch-dialog'),title=n('h2','切换账户');
      title.id='accountSwitchHeading';d.setAttribute('aria-labelledby',title.id);d.append(title);
      const notes=n('ul');
      if(state.recording)notes.append(n('li','录音将中止，未发送音频不会自动提交。'));
      if(state.playing)notes.append(n('li','当前播放将停止。'));
      if(state.input)notes.append(n('li','输入框中的未发送内容会清除。'));
      notes.append(n('li','未确认操作仍归原账户，不会执行或迁移。'),n('li','切换后重新核对身份、资料和权限。'));
      const actions=n('div',null,'account-switch-actions');let finished=false;
      const finish=value=>{if(finished)return;finished=true;d.close();d.remove();resolve(value);};
      actions.append(button('取消',()=>finish(false)),button('继续切换',()=>finish(true)));d.append(notes,actions);
      d.addEventListener('cancel',e=>{e.preventDefault();finish(false);});
      document.body.append(d);d.showModal();actions.firstChild.focus();
    });
  }
  function mount(root,adapter=configured){
    let directory=null,identity=null,epoch=0,destroyed=false,busy=false,dialog=null,selectionDialog=null,selectionTrigger=null;
    root.classList.add('account-switcher');
    const live=token=>!destroyed&&root.isConnected&&token===epoch;
    function settingsStatus(text){const status=document.getElementById('accountAccessStatus');if(status)status.textContent=t(text);}
    function message(text,isError=false){root.hidden=root.id==='accountSwitcher';root.replaceChildren(n('span','演示账户','account-switch-label'),n('span',text,'account-switch-status'));root.lastChild.role=isError?'alert':'status';settingsStatus(text);}
    function gate(value){document.documentElement.classList.toggle('navren-switch-pending',value);document.documentElement.classList.toggle('navren-private-locked',value);root.setAttribute('aria-busy',String(value));document.querySelectorAll('.chat-input-area,.page-scroll,.chat-messages').forEach(e=>{e.inert=value;});}
    function validate(data,fresh){
      if(data?.schema_version!==1||data.data_scope!=='navren_synthetic'||!Array.isArray(data.accounts)||data.accounts.length!==4||new Set(data.accounts.map(a=>a.account_id)).size!==4||data.accounts.some(a=>!ids.has(a.account_id)||typeof a.label!=='string'||!a.label||!a.source_version))throw Error(t('账户目录不完整'));
      if(!fresh?.workspace_id||!Number.isInteger(fresh.privacy_revision)||data.workspace_binding!==fresh.workspace_id||data.privacy_revision!==fresh.privacy_revision||data.current_account_id!==fresh.account_id||(fresh.account_id!=null&&!ids.has(fresh.account_id)))throw Error(t('账户目录身份不一致'));
    }
    function paint(){
      if(!directory||!identity)return;
      root.hidden=root.id==='accountSwitcher';settingsStatus('');
      const current=directory.accounts.find(a=>a.account_id===identity.account_id);
      root.replaceChildren(n('span','演示账户','account-switch-label'));
      const chooser=button('',()=>{dialog.hidden=!dialog.hidden;chooser.setAttribute('aria-expanded',String(!dialog.hidden));if(!dialog.hidden)dialog.querySelector('[aria-current="true"]')?.focus();});
      chooser.className='account-switch-trigger';chooser.textContent=current?.label||t('当前账户');chooser.dataset.i18nSkip='';chooser.setAttribute('aria-label',t('选择账户')+' · '+(current?.label||t('当前账户')));chooser.setAttribute('aria-expanded','false');root.append(chooser);
      dialog=n('div',null,'account-switch-menu');dialog.hidden=true;dialog.setAttribute('role','group');dialog.setAttribute('aria-label',t('选择账户'));
      for(const item of directory.accounts){const b=button('',()=>choose(item.account_id));b.textContent=item.label;b.dataset.i18nSkip='';b.dataset.accountId=item.account_id;b.setAttribute('aria-current',String(item.account_id===identity.account_id));b.disabled=busy;dialog.append(b);}
      const source=n('div',null,'account-switch-source');source.append(n('p','体验用户为虚拟身份，账户与财务数据均为模拟数据，资金操作仅在体验环境内生效；公开资料以标注来源为准。'));
      dialog.append(source);root.append(dialog);
    }
    async function refresh(){
      const token=++epoch;if(!adapter||adapter.safe_switch!==true){root.replaceChildren();root.hidden=true;return false;}
      message('正在核对账户…');
      try{const fresh=await adapter.identity();const data=await adapter.catalogue();if(!live(token))return false;validate(data,fresh);identity=fresh;directory=data;paint();return true;}
      catch(e){if(!live(token))return false;message(e.message||t('账户身份暂时无法核对'),true);root.append(button('重试',refresh));return false;}
    }
    function closeSelection(restore=true){
      if(!selectionDialog)return;
      selectionDialog.close();selectionDialog.remove();selectionDialog=null;
      if(restore)selectionTrigger?.focus();
    }
    async function openSelection(trigger){
      if(destroyed||busy)return false;
      if(selectionDialog){selectionDialog.focus();return true;}
      selectionTrigger=trigger;
      if(!adapter||adapter.safe_switch!==true){message('账户目录尚未接通',true);return false;}
      if(!await refresh()||destroyed)return false;
      const d=n('dialog',null,'account-switch-dialog account-select-dialog');
      const head=n('div',null,'account-select-heading'),title=n('h2','切换账号');
      title.id='experienceAccountSelectionHeading';d.setAttribute('aria-labelledby',title.id);
      const close=button('×',()=>closeSelection());close.className='account-select-close';close.setAttribute('aria-label',t('关闭'));
      head.append(title,close);d.append(head);
      const list=n('div',null,'account-select-list');list.setAttribute('role','group');list.setAttribute('aria-label',t('选择账户'));
      for(const item of directory.accounts){
        const current=item.account_id===identity.account_id;
        const choice=button('',()=>{closeSelection(current);if(!current)choose(item.account_id);});
        choice.className='account-select-choice';choice.dataset.accountId=item.account_id;choice.setAttribute('aria-current',String(current));
        choice.append(n('span',item.label));if(current)choice.append(n('span','当前账户','account-select-current'));
        list.append(choice);
      }
      const source=n('div',null,'account-switch-source');source.append(n('p','体验用户为虚拟身份，账户与财务数据均为模拟数据，资金操作仅在体验环境内生效；公开资料以标注来源为准。'));
      d.append(list,source);d.addEventListener('cancel',e=>{e.preventDefault();closeSelection();});
      selectionDialog=d;document.body.append(d);d.showModal();list.querySelector('[aria-current="true"]')?.focus();return true;
    }
    async function choose(id){
      if(busy||!directory||!ids.has(id)||id===identity?.account_id)return false;
      const state=(adapter.activity||activity)();
      if(state.sending){message('正在处理当前任务，请完成后切换',true);root.append(button('重试',refresh));return false;}
      if(state.operation?.busy||state.operation?.unknown){message('正在核对操作状态，请取得结果后切换',true);root.append(button('重试',refresh));return false;}
      busy=true;const before={...identity},token=++epoch;
      try{
        if(!await (adapter.confirm||confirmSwitch)(state)){if(live(token)){busy=false;paint();(selectionTrigger||root.querySelector('.account-switch-trigger'))?.focus();}return false;}
        if(!live(token))return false;
        gate(true);(adapter.quiesce||quiesce)();message('正在切换账户…');
        // 适配器在请求发出前撤销客户端旧请求；服务端验证原工作区及权限版本。
        const selected=await adapter.select(id,{expected_workspace_id:before.workspace_id,expected_privacy_revision:before.privacy_revision});
        const fresh=await adapter.identity();const data=await adapter.catalogue();
        if(!live(token))return false;
        validate(data,fresh);
        if(selected.account_id!==id||fresh.account_id!==id||selected.workspace_id!==fresh.workspace_id||selected.privacy_revision!==fresh.privacy_revision)throw Error(t('切换结果尚未核实，请重试身份核对'));
        identity=fresh;directory=data;busy=false;paint();
        window.dispatchEvent(new CustomEvent('navren:account-switched',{detail:{account_id:id,workspace_id:fresh.workspace_id,privacy_revision:fresh.privacy_revision}}));
        // 只在已核验成功后重载当前页；不让旧表单/请求/快照跨账户复用。
        (adapter.reload||(()=>location.reload()))();return true;
      }catch(e){if(!live(token))return false;busy=false;message(e.message||t('切换结果尚未核实，请重试身份核对'),true);
        root.append(button('重试',async()=>{if(await refresh())(adapter.reload||(()=>location.reload()))();}));return false;}
    }
    const language=()=>{if(directory)paint();else refresh();};
    const revoked=()=>{
      closeSelection(false);
      epoch++;busy=false;identity=directory=null;dialog=null;
      gate(true);(adapter?.quiesce||quiesce)();
      message('账户已在其他页面切换，请重新核对身份',true);
      root.append(button('重试',async()=>{if(await refresh())(adapter?.reload||(()=>location.reload()))();}));
    };
    const hide=e=>{if(dialog&&!root.contains(e.target)){dialog.hidden=true;root.querySelector('.account-switch-trigger')?.setAttribute('aria-expanded','false');}};
    const key=e=>{if(e.key==='Escape'&&dialog&&!dialog.hidden){dialog.hidden=true;const trigger=root.querySelector('.account-switch-trigger');trigger?.setAttribute('aria-expanded','false');trigger?.focus();}};
    window.addEventListener('navren:language',language);window.addEventListener('navren:identity-revoked',revoked);document.addEventListener('click',hide);root.addEventListener('keydown',key);
    const controller={refresh,choose,openSelection,setAdapter(next){adapter=next;return refresh();},destroy(){closeSelection(false);destroyed=true;epoch++;instances.delete(controller);if(pageInstance===controller)pageInstance=null;window.removeEventListener('navren:language',language);window.removeEventListener('navren:identity-revoked',revoked);document.removeEventListener('click',hide);root.removeEventListener('keydown',key);}};
    instances.add(controller);if(root.id==='accountSwitcher')pageInstance=controller;refresh();return controller;
  }
  function configure(adapter){if(!adapter||!['identity','catalogue','select'].every(k=>typeof adapter[k]==='function')||adapter.safe_switch!==true)throw Error('invalid_account_switch_adapter');configured=adapter;instances.forEach(c=>c.setAdapter(adapter));}
  document.addEventListener('DOMContentLoaded',()=>{const root=document.getElementById('accountSwitcher');if(root)mount(root);});
  return {mount,configure,activity,openSelection:trigger=>pageInstance?.openSelection(trigger)||Promise.resolve(false)};
})();
