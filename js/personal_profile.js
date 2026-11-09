/** 普通个人资料独立保存；不修改登录凭据或银行实名资料。 */
window.NavrenPersonalProfile = (() => {
  'use strict';
  const keys=['nickname','full_name','phone','email','city','occupation','company','birthday','gender','bio','avatar'];
  const words={'个人资料':'Personal profile','编辑资料':'Edit profile','编辑个人资料':'Edit personal profile','基本资料':'Basic information','联系信息':'Contact information','工作与简介':'Work and biography','昵称':'Display name','姓名':'Full name','生日':'Birthday','性别':'Gender','联系邮箱':'Contact email','联系手机':'Contact phone','所在城市':'City','职业':'Occupation','公司 / 学校':'Company / school','个人简介':'About you','未填写':'Not provided','不设置':'Not set','女':'Female','男':'Male','不便透露':'Prefer not to say','更换头像':'Change photo','移除头像':'Remove photo','取消':'Cancel','保存修改':'Save changes','保存中…':'Saving…','处理图片…':'Processing image…','关闭':'Close','重新读取':'Reload','放弃修改并读取最新':'Discard changes and reload','沿用原昵称和头像':'Use previous name and photo','除了昵称，其余信息均可按需填写。':'All fields except the display name are optional.','联系信息不会修改登录凭据。':'Contact details do not change your sign-in credentials.','资料暂时无法读取，请重试。':'Your profile could not be loaded. Please retry.','个人资料已保存':'Profile saved','请输入昵称。':'Enter a display name.','请输入有效的联系邮箱。':'Enter a valid contact email.','请输入有效的联系手机，允许国际区号。':'Enter a valid phone number; international prefixes are supported.','生日不能晚于今天。':'Your birthday cannot be in the future.','请选择 5 MB 以内的 PNG、JPEG、WebP 或 GIF 图片。':'Choose a PNG, JPEG, WebP or GIF image smaller than 5 MB.','图片无法读取，请换一张图片。':'This image could not be read. Try another image.','图片处理失败，请换一张图片。':'This image could not be processed. Try another image.','资料已在其他页面更新，请读取最新资料后重新编辑。':'Your profile changed elsewhere. Reload the latest profile before editing.','保存失败，请稍后重试。':'Changes could not be saved. Please retry.','账户已变化，请重新打开资料。':'Your account changed. Reopen your profile.','正在读取资料…':'Loading profile…','资料响应无法核对，请重新读取。':'Profile data could not be verified. Please reload.'};
  words['旧资料待确认']='Previous profile needs confirmation';words['旧资料存在冲突，需本人确认']='Previous profile data conflicts. Confirm your own information.';
  Object.assign(words,{'账户资料':'Account details','本人资料':'Your personal profile','联系邮箱':'Contact email','联系手机':'Contact phone','以下为虚构体验角色资料，模拟联系方式仅用于展示。':'These are fictional persona details. Simulated contacts are for display only.','本人资料可通过“编辑资料”自行填写。':'You can enter your own details using Edit profile.'});
  const t=value=>window.NavrenI18n?.language==='en'?(words[value]||value):value;
  const node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!=null)e.textContent=t(text);if(cls)e.className=cls;return e;};
  const copy=(tag,text,cls)=>{const e=node(tag,text,cls);e.dataset.ppCopy=text;return e;};
  const blank=()=>Object.fromEntries(keys.map(key=>[key,'']));
  const validAvatar=value=>value===''||typeof value==='string'&&value.length<=546176&&/^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+=*$/.test(value);
  let profile=null,identity=null,simulationProfile=null,epoch=0,accountCheck=0,readPromise=null,readCompletion=null,dialog=null,editor=null,hidden=false,needsConfirmation=false;
  const channel=typeof BroadcastChannel==='function'?new BroadcastChannel('navren-personal-profile'):null;
  const bindingFields=['registered_user_id','workspace_id','account_id','privacy_revision','auth_subject','session_generation','csrf','auth_expires_at','lease_expires_at'];
  const registered=value=>typeof value==='string'&&/^[a-f0-9]{32}$/.test(value);
  const sameBinding=(a,b)=>!!a&&!!b&&registered(a.registered_user_id)&&registered(b.registered_user_id)&&bindingFields.every(key=>a[key]===b[key]);
  const sameProfile=(a,b)=>!!a&&!!b&&keys.every(key=>a[key]===b[key]);
  function live(){return profile&&sameBinding(identity,window.BankClient?.identity())?profile:null;}
  function same(token,snapshot,data){const current=BankClient.identity();return !hidden&&token===epoch&&sameBinding(current,snapshot)&&(!data||data.registered_user_id===snapshot.registered_user_id&&data.workspace_binding===snapshot.workspace_id&&data.workspace_id===snapshot.workspace_id&&data.privacy_revision===snapshot.privacy_revision);}
  function validate(data){const p=data?.profile;if(!p||keys.some(key=>typeof p[key]!=='string')||!p.nickname.trim()||!Number.isSafeInteger(p.revision)||p.revision<0||data.account_revision!==p.revision||!validAvatar(p.avatar)||p.updated_at!=null&&typeof p.updated_at!=='string')throw Error(t('资料响应无法核对，请重新读取。'));return {...Object.fromEntries(keys.map(key=>[key,p[key]])),revision:p.revision,updated_at:p.updated_at};}
  function accountProfile(state,snapshot){
    if(!state||state.contract!=='navren.account-state/1'||state.registered_user_id!==snapshot.registered_user_id||state.workspace_binding!==snapshot.workspace_id||state.persona_id!==snapshot.account_id||!Number.isSafeInteger(state.revision)||state.revision<0||!Array.isArray(state.initialized_sections)||!state.sections?.profile)throw Error(t('资料响应无法核对，请重新读取。'));
    return validate({profile:{...Object.fromEntries(keys.map(key=>[key,state.sections.profile[key]])),revision:state.revision,updated_at:state.updated_at},account_revision:state.revision});
  }
  function simulatedProfile(state,snapshot){
    const value=state?.simulated_persona_profile,p=value?.profile;
    if(value?.contract!=='navren.simulated-persona-profile/1'||value.status!=='available'||!['tycoon','grain_farmer','working_landlady','retired_entrepreneur'].includes(value.persona_id)||
      value.registered_user_id!==snapshot.registered_user_id||value.workspace_binding!==snapshot.workspace_id||value.persona_id!==snapshot.account_id||
      value.data_origin!=='navren_synthetic'||value.is_simulated!==true||value.is_real_identity!==false||value.contact_use!=='display_only'||value.editable!==false||
      typeof value.template_version!=='string'||!/^[a-f0-9]{64}$/.test(value.template_version)||
      !p||p.persona_id!==value.persona_id||p.data_origin!=='navren_synthetic'||p.is_simulated!==true||
      ['nickname','full_name','email','simulation_contact_phone','city','occupation','birthday','bio'].some(key=>typeof p[key]!=='string'||!p[key].trim())||
      !p.email.endsWith('@navren.example.invalid')||!/^SIM-PHONE-\*{4}-100[1-4]$/.test(p.simulation_contact_phone))return null;
    return JSON.parse(JSON.stringify(value));
  }
  function sameSimulation(a,b){return a===b||!!a&&!!b&&a.template_version===b.template_version&&a.registered_user_id===b.registered_user_id&&a.workspace_binding===b.workspace_binding&&a.persona_id===b.persona_id;}
  function summaryGrid(rows){
    const dl=node('dl',null,'pp-summary-grid');
    for(const [label,value]of rows){const field=node('div',null,'pp-summary-item'),dd=node('dd');dd.textContent=value||t('未填写');if(value){dd.setAttribute('translate','no');dd.dataset.i18nSkip='';}dd.classList.toggle('pp-unfilled',!value);field.append(copy('dt',label),dd);dl.append(field);}
    return dl;
  }
  // Only the selected account summary uses this display projection; source values remain unchanged.
  function personaSummaryDisplay(value){
    const fullName=String(value.full_name||'').replace(/（模拟）$/, '').replace(/\(模拟\)$/, '');
    const phoneTail=/(\d{4})$/.exec(String(value.simulation_contact_phone||''));
    const email=String(value.email||''),at=email.lastIndexOf('@');
    const bio=String(value.bio||'').replace(/^虚构体验角色。/, '')
      .replace('演示中区分', '区分')
      .replace('演示工资、租金和物业支出的不同用途', '分别安排工资、租金和物业支出')
      .replace('演示养老金、家庭支出与门店周转资金的分别管理', '分别管理养老金、家庭支出与门店周转资金');
    return {fullName,email:at>0?email.slice(0,at)+'@••••':email,phone:phoneTail?'•••• '+phoneTail[1]:'未填写',bio};
  }
  function renderSimulatedSummary(host){
    if(!live()||!simulationProfile||!sameBinding(identity,window.BankClient?.identity()))return false;
    const value=simulationProfile.profile,display=personaSummaryDisplay(value),section=node('section');section.dataset.profileScope='simulated-persona';
    const heading=node('div',null,'pp-summary-bio');heading.append(copy('span','账户资料'),node('p',value.nickname));section.append(heading);
    section.append(summaryGrid([['姓名',display.fullName],['联系邮箱',display.email],['联系手机',display.phone],['所在城市',value.city],['职业',value.occupation],['生日',value.birthday]]));
    const bio=node('div',null,'pp-summary-bio');bio.append(copy('span','个人简介'),node('p',display.bio));section.append(bio);host.append(section);return true;
  }
  function confirmationRequired(state){return state.migration?.legacy_profile==='conflict'&&!state.initialized_sections.includes('profile');}
  function ownedLegacyInfo(snapshot){
    if(typeof Storage==='undefined'||typeof Storage.legacyOwned!=='function'||typeof Storage.privateIdentity!=='function'||!sameBinding(snapshot,BankClient.identity()))return null;
    const binding=Storage.privateIdentity();
    if(!binding||['registered_user_id','workspace_id','privacy_revision','auth_subject'].some(key=>binding[key]!==snapshot[key]))return null;
    try{return Storage.legacyOwned('qingzhou_userInfo');}catch(_){return null;}
  }
  function legacyImportAllowed(state,snapshot,baseline){
    const current=accountProfile(state,snapshot);
    return !confirmationRequired(state)&&!state.initialized_sections.includes('profile')&&current.revision===baseline.revision&&sameProfile(current,baseline);
  }
  function drawAvatar(host,p,alt='头像'){
    if(!host)return;
    const current=window.BankClient?.identity();
    // 默认头像属于注册用户；体验画像名称不能充当用户身份种子。
    const seed=current?.registered_user_id||current?.workspace_id||'navren-guest';
    const uploaded=p?.avatar&&validAvatar(p.avatar);
    const img=node('img');
    img.src=uploaded?p.avatar:window.NavrenDefaultAvatars?.render(seed)||'assets/brand/navren-icon.png';
    img.alt=t(alt);img.width=img.height=160;img.draggable=false;
    img.style.cssText='width:100%;height:100%;object-fit:cover;border-radius:inherit;display:block';
    host.replaceChildren(img);
  }
  function decorateAvatar(host){drawAvatar(host,needsConfirmation?null:live());}
  function renderSummary(){
    const p=live(),name=document.getElementById('profileName'),subtitle=document.getElementById('profileSubtitle'),summary=document.getElementById('personalProfileSummary');
    if(name)name.textContent=p&&needsConfirmation?t('旧资料待确认'):p?.nickname||(profile?t('资料暂时无法读取，请重试。'):t('正在读取资料…'));
    if(subtitle){subtitle.textContent=p?(needsConfirmation?t('旧资料存在冲突，需本人确认'):[p.occupation,p.company,p.city].filter(Boolean).join(' · ')||t('个人资料')):'';subtitle.setAttribute('translate','no');subtitle.dataset.i18nSkip='';}
    drawAvatar(document.getElementById('profileAvatar'),needsConfirmation?null:p);
    const edit=document.getElementById('editPersonalProfile'),avatar=document.getElementById('profileAvatar');if(edit){edit.textContent=t('编辑资料');edit.disabled=!p;}if(avatar)avatar.disabled=!p;
    if(summary){summary.replaceChildren();const hasSimulation=renderSimulatedSummary(summary);if(p&&needsConfirmation){const notice=copy('p','旧资料存在冲突，需本人确认','pp-read-error');notice.setAttribute('role','status');summary.append(notice);}else if(p){
      const hasPersonalDetails=['full_name','email','phone','city','occupation','birthday','company','bio','gender'].some(key=>p[key]);
      if(hasSimulation){const heading=node('div',null,'pp-summary-bio');heading.append(copy('span','本人资料'),copy('p',hasPersonalDetails?'个人资料':'本人资料可通过“编辑资料”自行填写。'));summary.append(heading);}
      if(!hasSimulation||hasPersonalDetails)summary.append(summaryGrid([['姓名',p.full_name],['联系邮箱',p.email],['联系手机',p.phone],['所在城市',p.city],['职业',p.occupation],['生日',p.birthday]]));
      if(p.bio){const bio=node('div',null,'pp-summary-bio');bio.append(copy('span','个人简介'),node('p',null));bio.lastElementChild.textContent=p.bio;bio.lastElementChild.setAttribute('translate','no');bio.lastElementChild.dataset.i18nSkip='';summary.append(bio);}
    }}
    document.querySelectorAll('.message.user .avatar').forEach(decorateAvatar);
  }
  function readError(error){const summary=document.getElementById('personalProfileSummary');renderSummary();if(!summary)return;const row=node('div',null,'pp-read-error'),text=node('p','资料暂时无法读取，请重试。'),retry=copy('button','重新读取','pp-text-button');retry.type='button';retry.onclick=()=>refresh();row.append(text,retry);summary.replaceChildren(row);const name=document.getElementById('profileName');if(name)name.textContent=t('个人资料');}
  async function refresh(){
    if(readPromise)return readPromise;if(hidden)return null;
    const token=++epoch,completion={token,binding:window.BankClient?.identity()?{...BankClient.identity()}:null,promise:null};
    // 先安装本次 promise，再进入可能同步抛错的依赖，确保失败后可重新读取。
    const task=Promise.resolve().then(async()=>{try{
      const session=await BankClient.ensure();if(token!==epoch||hidden)return null;completion.binding={...session};
      const snapshot={...session},guard=BankClient.guard(),state=await Storage.ready();
      if(!same(token,snapshot)||!guard.valid()||!Storage.accountReady())return null;
      const canonical=accountProfile(state,snapshot),confirmation=confirmationRequired(state),data=await BankClient.get('/personal-profile');
      if(!same(token,snapshot,data)||!guard.valid(data))return null;
      const next=validate(data);
      if(next.revision!==canonical.revision||!sameProfile(next,canonical)||profile&&identity?.registered_user_id===snapshot.registered_user_id&&next.revision<profile.revision)throw Error(t('资料已在其他页面更新，请读取最新资料后重新编辑。'));
      profile=canonical;simulationProfile=simulatedProfile(state,snapshot);identity=snapshot;needsConfirmation=confirmation;renderSummary();
      window.dispatchEvent(new CustomEvent('navren:personal-profile-loaded',{detail:{registered_user_id:identity.registered_user_id,workspace_id:identity.workspace_id,account_revision:profile.revision}}));
      return needsConfirmation?null:{...profile};
    }catch(error){if(token===epoch&&!hidden){profile=identity=simulationProfile=null;needsConfirmation=false;readError(error);}return null;}finally{if(token===epoch)readPromise=null;}});
    completion.promise=task;readCompletion=completion;readPromise=task;return task;
  }
  function whenReady(){
    if(hidden||!document.getElementById('personalProfileSummary'))return Promise.resolve(null);
    const completion=readCompletion;
    if(!completion||completion.token!==epoch||completion.binding&&!sameBinding(completion.binding,window.BankClient?.identity()))return Promise.resolve(null);
    return completion.promise;
  }
  function dateToday(){const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());const get=type=>parts.find(part=>part.type===type).value;return get('year')+'-'+get('month')+'-'+get('day');}
  function draftValue(){const draft={...editor.draft};for(const [key,input]of editor.inputs)draft[key]=input.value.trim();return draft;}
  function changed(){return editor&&keys.some(key=>draftValue()[key]!==editor.baseline[key]);}
  function updateSave(){if(!editor)return;editor.save.disabled=editor.saving||editor.avatarBusy||!changed();editor.save.textContent=t(editor.saving?'保存中…':editor.avatarBusy?'处理图片…':'保存修改');editor.dialog.querySelectorAll('input,textarea,select').forEach(input=>input.disabled=editor.saving);editor.dialog.querySelectorAll('[data-pp-avatar-action]').forEach(button=>button.disabled=editor.saving||editor.avatarBusy);editor.dialog.querySelectorAll('[data-pp-close]').forEach(button=>button.disabled=editor.saving);editor.remove.hidden=!editor.draft.avatar;editor.counter.textContent=String(editor.inputs.get('bio').value.length)+' / 500';}
  function setEditorStatus(text,error=false){if(!editor)return;editor.status.textContent=t(text);editor.status.dataset.error=String(error);}
  function close(force=false){if(!dialog||editor?.saving&&!force)return;const trigger=editor?.trigger;dialog.close();dialog.remove();dialog=null;editor=null;if(trigger?.isConnected&&!trigger.disabled)trigger.focus({preventScroll:true});}
  function field(form,key,label,type,max,placeholder){const wrapper=node('label',null,'pp-field'),caption=copy('span',label);const input=node(type==='textarea'?'textarea':type==='select'?'select':'input');input.id='personalProfile-'+key;input.name=key;if(input.tagName==='INPUT')input.type=type;if(max)input.maxLength=max;if(placeholder)input.placeholder=placeholder;input.value=editor.draft[key];
    if(key==='nickname'){input.required=true;input.autocomplete='nickname';}if(key==='full_name')input.autocomplete='name';if(key==='phone')input.autocomplete='tel';if(key==='email')input.autocomplete='email';if(key==='city'){input.autocomplete='address-level2';wrapper.classList.add('pp-field-wide');}if(key==='company')input.autocomplete='organization';if(key==='occupation')input.autocomplete='organization-title';if(key==='birthday'){input.max=dateToday();input.autocomplete='bday';}if(key==='bio'){input.rows=3;wrapper.classList.add('pp-field-wide');}
    if(key==='gender'){for(const [value,text]of [['','不设置'],['female','女'],['male','男'],['undisclosed','不便透露']]){const option=copy('option',text);option.value=value;input.append(option);}input.value=editor.draft[key];}
    input.addEventListener('input',()=>{input.setCustomValidity('');editor?.reload.setAttribute('hidden','');updateSave();});input.addEventListener('change',updateSave);wrapper.append(caption,input);form.append(wrapper);editor.inputs.set(key,input);return wrapper;
  }
  async function upload(event){
    const selected=event.target.files?.[0],context=editor;if(!selected||!context)return;event.target.value='';
    if(!['image/png','image/jpeg','image/webp','image/gif'].includes(selected.type)||selected.size>5*1024*1024){setEditorStatus('请选择 5 MB 以内的 PNG、JPEG、WebP 或 GIF 图片。',true);return;}
    context.avatarBusy=true;updateSave();setEditorStatus('');const token=epoch,snapshot={...identity};
    try{const dataUrl=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(Error('图片无法读取，请换一张图片。'));reader.readAsDataURL(selected);});
      const image=await new Promise((resolve,reject)=>{const img=new Image();img.onload=()=>resolve(img);img.onerror=()=>reject(Error('图片无法读取，请换一张图片。'));img.src=dataUrl;});if(editor!==context||!same(token,snapshot))return;
      if(!image.naturalWidth||!image.naturalHeight||image.naturalWidth*image.naturalHeight>40000000)throw Error('图片处理失败，请换一张图片。');
      const canvas=document.createElement('canvas'),size=Math.min(image.naturalWidth,image.naturalHeight),edge=Math.min(512,size);canvas.width=canvas.height=edge;canvas.getContext('2d').drawImage(image,(image.naturalWidth-size)/2,(image.naturalHeight-size)/2,size,size,0,0,edge,edge);const avatar=canvas.toDataURL('image/webp',.86);if(!validAvatar(avatar))throw Error('图片处理失败，请换一张图片。');context.draft.avatar=avatar;drawAvatar(context.avatar,context.draft);
    }catch(error){if(editor===context&&same(token,snapshot))setEditorStatus(error.message||'图片处理失败，请换一张图片。',true);}finally{if(editor===context){context.avatarBusy=false;updateSave();}}
  }
  function validateDraft(draft){const fail=(key,message)=>{const input=editor.inputs.get(key);input.setCustomValidity(t(message));input.reportValidity();input.focus();return false;};if(!draft.nickname)return fail('nickname','请输入昵称。');if(draft.email&&!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(draft.email))return fail('email','请输入有效的联系邮箱。');if(draft.phone&&(!/^\+?[0-9() -]+$/.test(draft.phone)||draft.phone.replace(/\D/g,'').length<6||draft.phone.replace(/\D/g,'').length>20))return fail('phone','请输入有效的联系手机，允许国际区号。');if(draft.birthday&&draft.birthday>dateToday())return fail('birthday','生日不能晚于今天。');return editor.form.reportValidity();}
  async function save(event){event.preventDefault();const context=editor;if(!context||context.saving||context.avatarBusy||!changed())return;const draft=draftValue();if(!validateDraft(draft))return;
    const snapshot={...identity},guard=BankClient.guard();if(!same(epoch,snapshot)||!guard.valid()){setEditorStatus('账户已变化，请重新打开资料。',true);return;}const token=++epoch;readPromise=null;accountCheck++;context.saving=true;setEditorStatus('');updateSave();
    try{
      const result=await BankClient.post('/personal-profile',{expected_revision:context.baseline.revision,profile:draft});
      if(editor!==context||!same(token,snapshot,result)||!guard.valid(result))return;
      const saved=validate(result),state=await Storage.ready({refresh:true});
      if(editor!==context||!same(token,snapshot)||!guard.valid())return;
      const confirmed=accountProfile(state,snapshot);
      if(!state.initialized_sections.includes('profile')||confirmed.revision<saved.revision||!sameProfile(confirmed,saved))throw Object.assign(Error(t('资料已在其他页面更新，请读取最新资料后重新编辑。')),{code:'ACCOUNT_STATE_VERSION_CONFLICT'});
      profile=confirmed;identity=snapshot;needsConfirmation=false;renderSummary();
      channel?.postMessage({registered_user_id:identity.registered_user_id,account_revision:profile.revision});
      close(true);if(typeof showToast==='function')showToast(t('个人资料已保存'));
    }
    catch(error){if(editor!==context||!same(token,snapshot))return;const conflict=['PROFILE_VERSION_CONFLICT','PERSONAL_PROFILE_VERSION_CONFLICT','PERSONAL_VERSION_CONFLICT','ACCOUNT_STATE_VERSION_CONFLICT'].includes(error.code)||error.status===409;setEditorStatus(conflict?'资料已在其他页面更新，请读取最新资料后重新编辑。':error.message||'保存失败，请稍后重试。',true);context.reload.hidden=!conflict;}
    finally{if(editor===context){context.saving=false;updateSave();}}
  }
  async function open(options={}){
    if(dialog)return;const trigger=document.activeElement;if(!live())await refresh();const p=live();if(!p)return;
    const legacySnapshot={...identity},legacyToken=epoch;let importAllowed=false;
    try{const state=await Storage.ready();if(!same(legacyToken,legacySnapshot))return;importAllowed=legacyImportAllowed(state,legacySnapshot,p);}catch(_){}
    if(dialog||!same(legacyToken,legacySnapshot))return;
    dialog=node('dialog',null,'pp-dialog');dialog.dataset.i18nSkip='';dialog.setAttribute('aria-labelledby','personalProfileTitle');const form=node('form',null,'pp-form');form.noValidate=true;form.onsubmit=save;
    const head=node('header',null,'pp-dialog-head'),title=copy('h2','编辑个人资料');title.id='personalProfileTitle';const closeButton=copy('button','×','pp-close');closeButton.type='button';closeButton.dataset.ppClose='';closeButton.setAttribute('aria-label',t('关闭'));closeButton.onclick=()=>close();head.append(node('div'),closeButton);head.firstElementChild.append(title,copy('p','除了昵称，其余信息均可按需填写。'));form.append(head);
    const body=node('div',null,'pp-dialog-body'),photo=node('div',null,'pp-photo-row'),avatar=node('span',null,'pp-edit-avatar'),photoControls=node('div',null,'pp-photo-controls'),input=node('input');input.type='file';input.accept='image/png,image/jpeg,image/webp,image/gif';input.hidden=true;input.id='personalProfileAvatarFile';input.onchange=upload;
    const choose=node('button',null,'pp-photo-picker'),remove=copy('button','移除头像','pp-text-button');choose.type=remove.type='button';choose.dataset.ppAvatarAction=remove.dataset.ppAvatarAction='';choose.setAttribute('aria-label',t('更换头像'));choose.onclick=()=>input.click();
    const camera=node('span',null,'pp-photo-camera');camera.setAttribute('aria-hidden','true');camera.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M8 5.5 9.4 3.5h5.2l1.4 2H19a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-10a2 2 0 0 1 2-2Z"/><circle cx="12" cy="12" r="3.5"/></svg>';
    choose.append(avatar,camera);remove.onclick=()=>{editor.draft.avatar='';drawAvatar(avatar,editor.draft);updateSave();};photoControls.append(remove);photo.append(choose,photoControls,input);body.append(photo);
    const status=node('p',null,'pp-editor-status');status.role='status';status.setAttribute('aria-live','polite');const footer=node('footer',null,'pp-dialog-foot'),reload=copy('button','放弃修改并读取最新','pp-text-button'),cancel=copy('button','取消','pp-secondary'),submit=copy('button','保存修改','pp-primary');reload.type=cancel.type='button';cancel.dataset.ppClose='';reload.hidden=true;reload.onclick=async()=>{close();await refresh();await open();};cancel.onclick=()=>close();submit.type='submit';footer.append(status,reload,cancel,submit);
    editor={dialog,form,trigger,baseline:{...p},draft:{...blank(),...Object.fromEntries(keys.map(key=>[key,p[key]]))},inputs:new Map(),avatar,remove,counter:null,status,save:submit,reload,saving:false,avatarBusy:false};
    for(const [heading,fields]of [['基本资料',[['nickname','昵称','text',40,'输入希望显示的昵称'],['full_name','姓名','text',80,'输入姓名'],['birthday','生日','date'],['gender','性别','select']]],['联系信息',[['email','联系邮箱','email',254,'name@example.com'],['phone','联系手机','tel',32,'输入联系手机'],['city','所在城市','text',80,'省 / 市']]],['工作与简介',[['occupation','职业','text',80,'例如：设计师、学生'],['company','公司 / 学校','text',120,'输入公司或学校'],['bio','个人简介','textarea',500,'简单介绍一下自己']]]]){const section=node('section',null,'pp-editor-section'),h=copy('h3',heading),grid=node('div',null,'pp-field-grid');section.append(h,grid);for(const args of fields)field(grid,...args);if(heading==='联系信息')section.append(copy('p','联系信息不会修改登录凭据。','pp-field-note'));body.append(section);}
    const counter=node('span',null,'pp-bio-counter');editor.inputs.get('bio').parentElement.append(counter);editor.counter=counter;drawAvatar(avatar,editor.draft);
    const legacy=importAllowed?ownedLegacyInfo(legacySnapshot):null;
    if(importAllowed&&(legacy?.nickname||validAvatar(legacy?.avatar||'')&&legacy?.avatar)){
      const importButton=copy('button','沿用原昵称和头像','pp-text-button pp-legacy-import');importButton.type='button';
      importButton.onclick=async()=>{
        const context=editor,token=epoch;if(!context||context.saving||!same(token,legacySnapshot))return;
        try{
          const state=await Storage.ready();
          if(editor!==context||!same(token,legacySnapshot)||!legacyImportAllowed(state,legacySnapshot,context.baseline))return;
          const owned=ownedLegacyInfo(legacySnapshot);if(!owned)return;
          if(typeof owned.nickname==='string')context.inputs.get('nickname').value=owned.nickname.trim().slice(0,40);
          if(validAvatar(owned.avatar||''))context.draft.avatar=owned.avatar||'';
          drawAvatar(avatar,context.draft);updateSave();importButton.remove();
        }catch(error){if(editor===context&&same(token,legacySnapshot))setEditorStatus(error.message||'资料响应无法核对，请重新读取。',true);}
      };
      photoControls.append(importButton);
    }
    form.append(body,footer);dialog.append(form);document.body.append(dialog);dialog.addEventListener('cancel',event=>{event.preventDefault();close();});dialog.addEventListener('click',event=>{if(event.target===dialog){const bounds=dialog.getBoundingClientRect();if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom)close();}});dialog.showModal();window.NavrenFormControls?.enhance(dialog);if(needsConfirmation)setEditorStatus('旧资料存在冲突，需本人确认');updateSave();editor.inputs.get(options.field||'nickname')?.focus({preventScroll:true});
  }
  function invalidate(){epoch++;accountCheck++;readPromise=null;readCompletion=null;profile=identity=simulationProfile=null;needsConfirmation=false;close(true);renderSummary();}
  async function syncAccountProfile(){
    const token=++accountCheck;let snapshot;
    if(hidden||!Storage.accountReady())return;
    try{
      snapshot=window.BankClient?.identity?.();if(!snapshot)return;const guard=BankClient.guard();
      const state=await Storage.ready();
      if(hidden||token!==accountCheck||!sameBinding(snapshot,BankClient.identity())||!guard.valid()||!Storage.accountReady())return;
      const current=accountProfile(state,snapshot),confirmation=confirmationRequired(state),nextSimulation=simulatedProfile(state,snapshot);
      if(live()&&current.revision<profile.revision)return;
      if(live()&&profile.revision===current.revision&&sameProfile(profile,current)&&needsConfirmation===confirmation&&sameSimulation(simulationProfile,nextSimulation))return;
      if(editor?.saving)return;
      if(dialog){if(confirmation){needsConfirmation=true;renderSummary();}dialog.querySelector('.pp-legacy-import')?.remove();setEditorStatus('资料已在其他页面更新，请读取最新资料后重新编辑。',true);editor.reload.hidden=false;return;}
      // 只采用适配器已核验的同一 canonical profile；不再发 GET 或触发 ready 循环。
      epoch++;readPromise=null;readCompletion=null;profile=current;simulationProfile=nextSimulation;identity={...snapshot};needsConfirmation=confirmation;renderSummary();
      window.dispatchEvent(new CustomEvent('navren:personal-profile-loaded',{detail:{registered_user_id:identity.registered_user_id,workspace_id:identity.workspace_id,account_revision:profile.revision}}));
    }catch(error){if(token===accountCheck&&!hidden&&sameBinding(snapshot,window.BankClient?.identity?.())){invalidate();readError(error);}}
  }
  function init(){hidden=false;renderSummary();refresh();}
  window.addEventListener('navren:identity-revoked',invalidate);window.addEventListener('navren:privacy-reset',()=>{invalidate();queueMicrotask(()=>{if(!hidden&&BankClient.identity())refresh();});});window.addEventListener('navren:account-switched',()=>{invalidate();refresh();});
  window.addEventListener('navren:view',()=>{if(dialog)close(true);if(!live()&&!hidden)refresh();});
  window.addEventListener('pagehide',()=>{hidden=true;invalidate();});window.addEventListener('pageshow',event=>{if(event.persisted){hidden=false;refresh();}});
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&!dialog&&!hidden)refresh();});
  window.addEventListener('navren:language',()=>{renderSummary();if(editor){dialog.querySelectorAll('[data-pp-copy]').forEach(e=>e.textContent=t(e.dataset.ppCopy));dialog.querySelector('.pp-photo-picker')?.setAttribute('aria-label',t('更换头像'));updateSave();}});
  window.addEventListener('navren:account-state',event=>{if(event.detail?.status==='ready')syncAccountProfile();else invalidate();});
  if(channel)channel.onmessage=event=>{
    const current=BankClient.identity(),message=event.data;
    if(hidden||!current||message?.registered_user_id!==current.registered_user_id||!Number.isSafeInteger(message.account_revision)||message.account_revision<0)return;
    // 同账号其他画像/标签的保存也更新统一 CAS；只有广播才主动重新读取账号状态。
    Storage.ready({refresh:true}).then(()=>syncAccountProfile()).catch(()=>{});
  };
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
  return {refresh,whenReady,open,upload,renderSummary,decorateAvatar,invalidate,current:()=>!needsConfirmation&&live()?{...live()}:null};
})();
