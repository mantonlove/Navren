/* 有限消费者 DOM 检查。真实 UI 模块；账号存储/BankClient 为受控异步替身。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {JSDOM, VirtualConsole} = require('jsdom');
const root = path.resolve(__dirname, '../..');
const files = ['js/storage.js','js/i18n.js','js/design.js','js/mine.js','js/insurance_preferences.js','js/personal_profile.js','js/chat.js'];
const sources = Object.fromEntries(files.map(file => [file, fs.readFileSync(path.join(root,file),'utf8')]));
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const pins = Object.fromEntries(files.map(file => [file,sha(sources[file])]));
const clone = value => value == null ? value : JSON.parse(JSON.stringify(value));
const fields = ['nickname','full_name','phone','email','city','occupation','company','birthday','gender','bio','avatar'];
const output = process.argv[2];
const selected = new Set((process.argv[3] || '').split(',').filter(Boolean));
const results = [];
function deferred() {let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function extractFunction(name) {
  const source=sources['js/chat.js'],start=source.indexOf('function '+name+'(');
  assert.ok(start>=0,'actual named chat helper exists: '+name);
  const lineEnd=source.indexOf('\n',start);
  const end=['chatBinding','newChatMessageId'].includes(name)?lineEnd:source.indexOf('\n}',start)+2;
  assert.ok(end>start,'actual helper end: '+name);
  return source.slice(start,end);
}
const chatNames=['chatBinding','newChatMessageId','bookmarkIcon','configureAccountBookmark'];
const chatHelpers=chatNames.map(extractFunction).join('\n');
const tick = () => new Promise(resolve=>setImmediate(resolve));
async function settle() {for(let i=0;i<5;i++)await tick();}

async function fixture(options={}) {
  const errors=[],calls=[],writes=[],readyCalls=[],channels=[],navigation=[],effects={speech:0,snapshots:0};
  const virtualConsole=new VirtualConsole();virtualConsole.on('jsdomError',error=>errors.push(String(error)));
  const dom=new JSDOM(`<!doctype html><html><body data-page="mine"><div class="app">
    <header class="header"><a class="brand" href="mine.html">Navren</a></header>
    <main><section id="settingsThemeSelector"></section><div id="fontSizeRow"></div>
    <button id="voiceToggle" role="switch"></button><div id="voicePresetRow"></div><div id="prefsBody"></div>
    <select id="interfaceMode"><option value="classic">classic</option><option value="senior">senior</option><option value="youth">youth</option></select>
    <div id="mineInsurancePreferences"></div><button id="profileAvatar"></button><h2 id="profileName"></h2><p id="profileSubtitle"></p>
    <button id="editPersonalProfile"></button><div id="personalProfileSummary"></div><div id="bookmarksList"></div>
    <div id="modalOverlay" class="hidden"><div class="modal-foot"><button>取消</button></div></div>
    <section id="chatFixture"><article class="message assistant" data-client-message-id="fixture-message"><div class="bubble"><button id="chatBookmark"></button></div></article></section>
    </main></div></body></html>`,{url:'https://navren.invalid/mine.html?view=home',runScripts:'outside-only',pretendToBeVisual:true,virtualConsole});
  const w=dom.window,d=w.document;
  // 先结束真实 DOMContentLoaded/load；目标模块原样求值，避免无关全页启动与真实 adapter 初始化。
  if(d.readyState!=='complete')await new Promise(resolve=>w.addEventListener('load',resolve,{once:true}));
  const now=Date.now()/1000;
  const service={ready:options.ready!==false,status:options.ready===false?'loading':'ready',revision:options.revision??9,
    initialized:options.initialized||['profile','settings','preferences','insurance','favorites','reading','markers'],legacy:options.legacy??null,
    identity:{registered_user_id:'aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa',workspace_id:'11111111111141118111111111111111',account_id:'tycoon',privacy_revision:0,
      auth_subject:'welcome-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',session_generation:1,csrf:'fixture-a',auth_expires_at:now+7200,lease_expires_at:now+3600},
    values:{navren_theme:{id:'light'},navren_language:'zh-CN',qingzhou_mode:'classic',qingzhou_fontSize:16,
      qingzhou_modeFontSizes:{classic:16,senior:20,youth:15},qingzhou_voiceEnabled:false,qingzhou_voicePreset:'male_podcast',
      qingzhou_preferences:{recommendByRisk:false,recommendByHorizon:false,marketHotPush:false,marketHotVisible:false,weeklyReport:false},
      navren_insurancePreferences:['health'],qingzhou_bookmarks:[],navren_reading_history:[],qingzhou_privacyRead:false,qingzhou_voiceGuideRead:false},
    profile:{nickname:'默认昵称',full_name:'张三',phone:'13800138000',email:'person@example.com',city:'杭州',occupation:'模拟交易研究员',company:'示例学校',birthday:'2000-01-02',gender:'undisclosed',bio:'本人简介',avatar:''},
    migration:clone(options.migration||{legacy_profile:'none',conflicts:[]}),deferReady:false,deferGet:false,deferPost:true,deferRefresh:false,readyQueue:[],getQueue:[],postQueue:[],refreshQueue:[]};
  Object.assign(service.values,clone(options.values||{}));Object.assign(service.profile,clone(options.profile||{}));
  const binding=()=>clone(service.identity);
  function accountState(){const v=service.values;return {contract:'navren.account-state/1',registered_user_id:service.identity.registered_user_id,
    workspace_binding:service.identity.workspace_id,persona_id:service.identity.account_id,revision:service.revision,updated_at:'2026-10-17T00:00:00Z',
    initialized_sections:clone(service.initialized),migration:clone(service.migration),sections:{profile:{...clone(service.profile),avatar_preset:'navren-flow-3'},settings:{theme:v.navren_theme.id,language:v.navren_language,
      mode:v.qingzhou_mode,font_size:v.qingzhou_fontSize,mode_font_sizes:clone(v.qingzhou_modeFontSizes),voice_enabled:v.qingzhou_voiceEnabled,voice_preset:v.qingzhou_voicePreset},
      preferences:clone(v.qingzhou_preferences),insurance:{topics:clone(v.navren_insurancePreferences)},favorites:clone(v.qingzhou_bookmarks),reading:clone(v.navren_reading_history),
      markers:{onboarded:false,privacy_read:v.qingzhou_privacyRead,voice_guide_read:v.qingzhou_voiceGuideRead,tips:{chat:false,recommend:false,account:false,mine:false}}}};}
  function profileDTO(override={}){return {registered_user_id:service.identity.registered_user_id,workspace_id:service.identity.workspace_id,workspace_binding:service.identity.workspace_id,
    privacy_revision:service.identity.privacy_revision,account_revision:service.revision,profile:{...clone(service.profile),revision:service.revision,updated_at:'2026-10-17T00:00:00Z'},...clone(override)};}
  const notify=(status=service.status)=>w.dispatchEvent(new w.CustomEvent('navren:account-state',{detail:{status,keys:[]}}));
  function stage(values,kind){if(!service.ready)return Promise.reject(Error('ACCOUNT_NOT_READY'));const wait=deferred();const row={kind,values:clone(values),...wait,settled:false};writes.push(row);return wait.promise;}
  const fixtureStorage={accountReady:()=>service.ready,accountStatus:()=>service.status,
    ready:async(options={})=>{readyCalls.push(clone(options));if(!service.ready)throw Error('ACCOUNT_NOT_READY');if(service.deferReady){const wait=deferred();service.readyQueue.push(wait);return wait.promise;}if(options.refresh&&service.deferRefresh){const wait=deferred();service.refreshQueue.push(wait);return wait.promise;}return accountState();},
    get:key=>service.ready?clone(service.values[key]??null):null,
    set:(key,value)=>stage({[key]:value},'set'),setMany:values=>stage(values,'setMany'),
    update:(key,mutate)=>{try{return stage({[key]:mutate(clone(service.values[key]))},'update');}catch(error){return Promise.reject(error);}},
    legacyOwned:key=>key==='qingzhou_userInfo'?clone(service.legacy):null,privateIdentity:()=>binding()};
  w.fixtureStorage=fixtureStorage;
  w.BankClient={identity:()=>binding(),ensure:async()=>binding(),guard:()=>{const captured=binding();return {valid:data=>
    Object.keys(captured).every(key=>captured[key]===service.identity[key])&&(!data||(data.workspace_id===captured.workspace_id&&data.privacy_revision===captured.privacy_revision))};},
    get:async url=>{calls.push({method:'GET',url});if(url!=='/personal-profile')throw Error('Forbidden fixture GET '+url);if(service.deferGet){const wait=deferred();service.getQueue.push(wait);return wait.promise;}return profileDTO();},
    post:async(url,body)=>{calls.push({method:'POST',url,body:clone(body)});if(url!=='/personal-profile')throw Error('Forbidden fixture POST '+url);const wait=deferred();service.postQueue.push({...wait,body:clone(body)});return wait.promise;}};
  w.BroadcastChannel=class {constructor(name){this.name=name;this.messages=[];channels.push(this);}postMessage(message){this.messages.push(clone(message));}close(){}emit(message){this.onmessage?.({data:clone(message)});}};
  w.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
  w.NavrenViews={current:()=>({page:'mine',view:'home'}),revision:()=>1};
  w.NavrenApp={href:href=>href,navigate:href=>navigation.push(href)};
  w.NavrenSpeech={activeKind:()=>null,stop:()=>{},speak:()=>{effects.speech++;throw Error('Paid speech forbidden');}};
  w.NavrenDefaultAvatars={render:seed=>'data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg"><title>'+seed+'</title></svg>')};
  w.saveRecommendationSnapshot=()=>{effects.snapshots++;};
  w.fetch=()=>{throw Error('Real network forbidden');};
  w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
  w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');this.dispatchEvent(new w.Event('close'));};
  w.eval(sources['js/storage.js']+'\n;Object.assign(Storage,window.fixtureStorage);\n'+
    ['js/i18n.js','js/design.js','js/mine.js','js/insurance_preferences.js','js/personal_profile.js'].map(file=>sources[file]).join('\n;\n')+
    '\n;'+chatHelpers+'\n;window.__consumer={Storage,refreshAccountPreferences,getModeFont,getInterfaceMode,configureAccountBookmark};');
  w.NavrenDesign.mountSettings(d.querySelector('header'));
  notify();await settle();
  function pending(){const row=writes.find(item=>!item.settled);assert.ok(row,'a controlled pending storage write exists');return row;}
  async function acceptWrite(){const row=pending();row.settled=true;Object.assign(service.values,clone(row.values));service.revision++;notify('ready');row.resolve(accountState());await settle();return row;}
  async function rejectWrite(){const row=pending();row.settled=true;row.reject(Error('合成保存失败'));await settle();return row;}
  const toasts=()=>[...d.querySelectorAll('.toast')].map(node=>node.textContent);
  const clearToast=()=>d.querySelectorAll('.toast').forEach(node=>node.remove());
  return {w,d,dom,errors,calls,writes,readyCalls,channels,navigation,effects,service,accountState,profileDTO,notify,pending,acceptWrite,rejectWrite,toasts,clearToast,
    async ready(){service.ready=true;service.status='ready';notify();await settle();},
    async notReady(status='loading'){service.ready=false;service.status=status;notify();await settle();},
    async openProfile(){await w.NavrenPersonalProfile.open();assert.ok(d.querySelector('.pp-dialog'));},
    input(key,value){const input=d.getElementById('personalProfile-'+key);assert.ok(input);input.value=value;input.dispatchEvent(new w.Event('input',{bubbles:true}));},
    submitProfile(){d.querySelector('.pp-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));},
    async acceptPost(override={}){const request=service.postQueue.shift();assert.ok(request);assert.deepEqual(Object.keys(request.body).sort(),['expected_revision','profile']);
      assert.equal(request.body.expected_revision,service.revision);service.profile=clone(request.body.profile);service.revision++;if(!service.initialized.includes('profile'))service.initialized.push('profile');request.resolve(profileDTO(override));await settle();return request;},
    async finish(){await settle();assert.equal(effects.speech,0);assert.deepEqual(errors,[],'unexpected JSDOM errors');dom.window.close();}};
}

async function check(id,name,operation,options){if(selected.size&&!selected.has(id))return;let f;try{f=await fixture(options);await operation(f);await f.finish();results.push({id,name,passed:true});}
catch(error){f?.dom.window.close();results.push({id,name,passed:false,error:error.stack||String(error)});}}

(async()=>{
  await check('AC01','未核验账号不展示默认开关且不能提交',async f=>{
    const toggle=f.d.getElementById('voiceToggle');assert.equal(toggle.disabled,true);assert.equal(toggle.hidden,true);assert.equal(toggle.hasAttribute('aria-checked'),false);
    assert.equal(f.d.querySelector('#fontSizeSlider'),null);assert.equal(f.d.querySelector('#prefsBody button'),null);assert.equal(f.d.querySelector('.settings-voice-trigger'),null);
    assert.equal(f.d.getElementById('navrenThemeButton').disabled,true);assert.equal(f.d.getElementById('navrenLanguageButton').disabled,true);
    await f.w.toggleVoice();await f.w.selectVoice('female_sweet');await f.w.togglePref('weeklyReport',{disabled:false});
    assert.equal(await f.w.NavrenDesign.applyTheme({id:'dark'}),false);assert.equal(f.writes.length,0);
    await f.notReady('unavailable');assert.match(f.d.getElementById('prefsBody').textContent,/暂未读取/);
  },{ready:false});
  await check('AC02','完整恢复 false、音色、各模式字号、深色及英文',async f=>{
    assert.equal(f.d.getElementById('voiceToggle').getAttribute('aria-checked'),'false');
    assert.ok([...f.d.querySelectorAll('#prefsBody [role=switch]')].every(node=>node.getAttribute('aria-checked')==='false'));
    assert.equal(f.d.getElementById('settings-voice-option-male_podcast').getAttribute('aria-selected'),'true');
    assert.equal(f.d.getElementById('fontSizeSlider').value,'21');assert.equal(f.d.documentElement.style.fontSize,'21px');
    assert.equal(f.d.documentElement.dataset.theme,'dark');assert.equal(f.w.NavrenI18n.language,'en');assert.equal(f.d.documentElement.lang,'en');
    assert.equal(f.w.__consumer.getModeFont('senior'),23);assert.equal(f.writes.length,0);
    f.service.values.qingzhou_fontSize=23;f.service.values.qingzhou_modeFontSizes={};f.notify('ready');await settle();
    assert.equal(f.d.getElementById('fontSizeSlider').value,'23');assert.equal(f.d.documentElement.style.fontSize,'23px');
    assert.equal(f.w.__consumer.getModeFont('youth'),23,'current mode restores server global legacy font');
    assert.equal(f.w.__consumer.getModeFont('classic'),'medium');assert.equal(f.w.__consumer.getModeFont('senior'),'large','other modes keep own defaults');assert.equal(f.writes.length,0);
  },{values:{navren_theme:{id:'dark'},navren_language:'en',qingzhou_mode:'youth',qingzhou_modeFontSizes:{classic:17,senior:23,youth:21}}});
  await check('AC03','自动朗读待保存不乐观、失败不成功、确认后开启',async f=>{
    let task=f.w.toggleVoice();assert.equal(f.pending().values.qingzhou_voiceEnabled,true);assert.equal(f.d.getElementById('voiceToggle').getAttribute('aria-checked'),'false');
    assert.equal(f.d.getElementById('voiceToggle').disabled,true);await f.w.toggleVoice();assert.equal(f.writes.length,1);
    await f.rejectWrite();await task;assert.equal(f.d.getElementById('voiceToggle').getAttribute('aria-checked'),'false');assert.ok(f.toasts().some(text=>text.includes('未保存')));
    f.clearToast();task=f.w.toggleVoice();await f.acceptWrite();await task;assert.equal(f.d.getElementById('voiceToggle').getAttribute('aria-checked'),'true');
  });
  await check('AC04','偏好与音色保存失败不假成功、成功采用确认快照',async f=>{
    let button=f.d.querySelector('#prefsBody button');let task=f.w.togglePref('recommendByRisk',button);
    assert.equal(button.getAttribute('aria-checked'),'false');assert.equal(f.service.values.qingzhou_preferences.recommendByRisk,false);await f.rejectWrite();await task;
    assert.equal(f.d.querySelector('#prefsBody button').getAttribute('aria-checked'),'false');assert.ok(f.toasts().some(text=>text.includes('未保存')));
    f.clearToast();button=f.d.querySelector('#prefsBody button');task=f.w.togglePref('recommendByRisk',button);await f.acceptWrite();await task;
    assert.equal(f.d.querySelector('#prefsBody button').getAttribute('aria-checked'),'true');
    f.clearToast();f.d.getElementById('settings-voice-option-female_sweet').click();assert.equal(f.service.values.qingzhou_voicePreset,'male_podcast');
    assert.equal(f.d.getElementById('settings-voice-option-male_podcast').getAttribute('aria-selected'),'true');await f.rejectWrite();assert.ok(!f.toasts().some(text=>text==='语音音色已切换'));
    f.clearToast();f.d.getElementById('settings-voice-option-female_sweet').click();await f.acceptWrite();assert.equal(f.service.values.qingzhou_voicePreset,'female_sweet');
    assert.equal(f.d.getElementById('settings-voice-option-female_sweet').getAttribute('aria-selected'),'true');assert.ok(f.toasts().includes('语音音色已切换'));
  });
  await check('AC05','字号即时预览、失败回滚、分模式原子保存',async f=>{
    let slider=f.d.getElementById('fontSizeSlider');slider.value='22';slider.dispatchEvent(new f.w.Event('input',{bubbles:true}));assert.equal(f.d.documentElement.style.fontSize,'22px');assert.equal(f.service.values.qingzhou_modeFontSizes.classic,16);
    slider.dispatchEvent(new f.w.Event('change',{bubbles:true}));assert.equal(slider.disabled,true);assert.deepEqual(f.pending().values,{qingzhou_modeFontSizes:{classic:22,senior:20,youth:15},qingzhou_fontSize:22});
    await f.rejectWrite();slider=f.d.getElementById('fontSizeSlider');assert.equal(slider.value,'16');assert.equal(f.d.documentElement.style.fontSize,'16px');
    slider.value='19';slider.dispatchEvent(new f.w.Event('change',{bubbles:true}));await f.acceptWrite();assert.equal(f.service.values.qingzhou_modeFontSizes.classic,19);assert.equal(f.service.values.qingzhou_modeFontSizes.senior,20);
    f.service.values.qingzhou_fontSize=23;f.service.values.qingzhou_modeFontSizes={};f.notify('ready');await settle();
    slider=f.d.getElementById('fontSizeSlider');assert.equal(slider.value,'23');slider.value='24';slider.dispatchEvent(new f.w.Event('change',{bubbles:true}));
    assert.equal(f.pending().kind,'setMany');assert.deepEqual(f.pending().values,{qingzhou_modeFontSizes:{classic:24},qingzhou_fontSize:24});
    await f.rejectWrite();assert.equal(f.d.getElementById('fontSizeSlider').value,'23');assert.equal(f.d.documentElement.style.fontSize,'23px');
  });
  await check('AC06','外观、语言和模式等待保存确认且失败不跳页',async f=>{
    f.d.querySelector('#settingsThemeSelector [data-theme-choice=dark]').click();assert.equal(f.d.documentElement.dataset.theme,'light');assert.equal(f.d.getElementById('navrenThemeButton').disabled,true);
    await f.rejectWrite();assert.equal(f.d.documentElement.dataset.theme,'light');f.d.querySelector('#settingsThemeSelector [data-theme-choice=dark]').click();await f.acceptWrite();assert.equal(f.d.documentElement.dataset.theme,'dark');
    f.clearToast();let task=f.w.NavrenI18n.setLanguage('en');assert.equal(f.w.NavrenI18n.language,'zh-CN');await f.rejectWrite();assert.equal(await task,false);assert.equal(f.w.NavrenI18n.language,'zh-CN');
    task=f.w.NavrenI18n.setLanguage('en');await f.acceptWrite();assert.equal(await task,true);assert.equal(f.w.NavrenI18n.language,'en');
    task=f.w.NavrenDesign.switchMode('senior');assert.equal(f.navigation.length,0);await f.rejectWrite();assert.equal(await task,false);assert.equal(f.navigation.length,0);assert.equal(f.service.values.qingzhou_fontSize,23);
    const before=f.writes.length;task=f.w.NavrenDesign.switchMode('senior');assert.equal(f.writes.length,before+1,'mode switch stages one settings write');assert.equal(f.pending().kind,'setMany');
    assert.deepEqual(f.pending().values,{qingzhou_mode:'senior',qingzhou_fontSize:'large',qingzhou_modeFontSizes:{classic:23}});
    assert.equal(f.d.documentElement.style.fontSize,'23px','pending mode switch keeps old confirmed font');
    await f.acceptWrite();assert.equal(await task,true);assert.equal(f.navigation.length,1);assert.match(f.navigation[0],/mode=senior/);assert.equal(f.d.documentElement.style.fontSize,'20px');assert.equal(f.service.values.qingzhou_modeFontSizes.classic,23);
    const back=f.writes.length;task=f.w.NavrenDesign.switchMode('classic');assert.equal(f.writes.length,back+1);assert.equal(f.pending().kind,'setMany');
    assert.deepEqual(f.pending().values,{qingzhou_mode:'classic',qingzhou_fontSize:23,qingzhou_modeFontSizes:{classic:23,senior:'large'}});
    await f.acceptWrite();assert.equal(await task,true);assert.equal(f.d.documentElement.style.fontSize,'23px');
  },{values:{qingzhou_fontSize:23,qingzhou_modeFontSizes:{}}});
  await check('AC07','保险失败保留草稿、成功后确认展示、失去就绪清空',async f=>{
    await f.w.NavrenInsurancePreferences.refresh();f.d.querySelector('[data-insurance-action=edit]').click();f.d.querySelector('[data-insurance-need=property]').click();
    f.d.querySelector('[data-insurance-action=save]').click();assert.equal(f.d.getElementById('mineInsurancePreferences').getAttribute('aria-busy'),'true');assert.deepEqual(f.service.values.navren_insurancePreferences,['health']);
    await f.rejectWrite();assert.equal(f.d.querySelector('[data-insurance-need=property]').getAttribute('aria-pressed'),'true');assert.equal(f.d.getElementById('mineInsurancePreferences').classList.contains('is-editing'),true);
    assert.match(f.d.querySelector('.insurance-preferences-status').textContent,/失败/);f.d.querySelector('[data-insurance-action=save]').click();await f.acceptWrite();
    assert.deepEqual(f.service.values.navren_insurancePreferences,['health','property']);assert.ok(f.d.querySelector('[data-insurance-summary=property]'));assert.equal(f.d.querySelector('[data-insurance-action=save]'),null);
    await f.notReady();assert.equal(f.d.querySelector('[data-insurance-summary]'),null);assert.equal(f.d.querySelector('[data-insurance-action]'),null);
  });
  await check('AC08','个人资料完整 POST 保留用户原文并等待 canonical 重读',async f=>{
    assert.equal(f.w.NavrenPersonalProfile.current().occupation,'模拟交易研究员');
    f.service.profile.city='全部';f.service.profile.company='全部';f.service.profile.bio='全部';await f.w.NavrenPersonalProfile.refresh();
    f.w.NavrenI18n.setLanguage('en',false);f.w.NavrenI18n.apply();
    assert.equal(f.d.querySelector('.pp-summary-bio p').textContent,'全部');assert.equal(f.d.querySelector('.pp-summary-bio p').getAttribute('translate'),'no');
    assert.ok([...f.d.querySelectorAll('.pp-summary-grid dd')].some(node=>node.textContent==='全部'&&node.dataset.i18nSkip===''));
    assert.match(f.d.getElementById('profileSubtitle').textContent,/全部/);assert.equal(f.d.getElementById('profileSubtitle').getAttribute('translate'),'no');
    f.w.NavrenI18n.setLanguage('zh-CN',false);await f.openProfile();f.input('nickname','新的昵称');f.input('company','新的学校');
    f.service.deferRefresh=true;f.submitProfile();await settle();assert.equal(f.calls.filter(row=>row.method==='POST').length,1);assert.equal(f.w.NavrenPersonalProfile.current().nickname,'默认昵称');
    const body=f.calls.find(row=>row.method==='POST').body;assert.deepEqual(Object.keys(body.profile).sort(),fields.slice().sort());assert.equal(body.profile.occupation,'模拟交易研究员');assert.equal(body.profile.avatar,'');
    await f.acceptPost();assert.ok(f.d.querySelector('.pp-dialog'));assert.equal(f.w.NavrenPersonalProfile.current().nickname,'默认昵称');assert.ok(!f.toasts().includes('个人资料已保存'));
    assert.equal(f.service.refreshQueue.length,1);f.service.refreshQueue.shift().resolve(f.accountState());await settle();assert.equal(f.d.querySelector('.pp-dialog'),null);assert.equal(f.w.NavrenPersonalProfile.current().nickname,'新的昵称');
    assert.equal(f.w.NavrenPersonalProfile.current().company,'新的学校');assert.ok(f.toasts().includes('个人资料已保存'));assert.equal(f.channels.find(row=>row.name==='navren-personal-profile').messages.length,1);
    assert.equal(f.writes.length,0,'profile path never writes finance or account-display keys');
  });
  await check('AC09','个人资料 GET/POST 拒绝不同注册 owner 响应',async f=>{
    f.service.deferGet=true;const read=f.w.NavrenPersonalProfile.refresh();await settle();assert.equal(f.service.getQueue.length,1);
    f.service.getQueue.shift().resolve(f.profileDTO({registered_user_id:'bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb'}));await read;assert.equal(f.w.NavrenPersonalProfile.current().nickname,'默认昵称');
    f.service.deferGet=false;await f.openProfile();f.input('nickname','不应被采用');f.submitProfile();await settle();await f.acceptPost({registered_user_id:'bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb'});
    assert.equal(f.w.NavrenPersonalProfile.current().nickname,'默认昵称');assert.ok(!f.toasts().includes('个人资料已保存'));
    f.service.identity.registered_user_id='bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb';f.w.dispatchEvent(new f.w.Event('navren:identity-revoked'));await settle();
    assert.equal(f.w.NavrenPersonalProfile.current(),null);assert.equal(f.d.querySelector('.pp-dialog'),null);
  });
  await check('AC10','跨标签 canonical revision 更新、编辑草稿冲突保留且无 GET 循环',async f=>{
    const channel=f.channels.find(row=>row.name==='navren-personal-profile'),gets=f.calls.filter(row=>row.method==='GET').length;
    f.service.revision=10;f.service.profile.nickname='其他标签昵称';channel.emit({registered_user_id:f.service.identity.registered_user_id,account_revision:10});await settle();
    assert.equal(f.w.NavrenPersonalProfile.current().nickname,'其他标签昵称');assert.equal(f.w.NavrenPersonalProfile.current().revision,10);assert.ok(f.readyCalls.some(row=>row.refresh));assert.equal(f.calls.filter(row=>row.method==='GET').length,gets);
    await f.openProfile();f.input('nickname','本地未保存草稿');f.service.revision=11;f.service.profile.nickname='更新后的服务端昵称';f.notify('ready');await settle();
    assert.equal(f.d.getElementById('personalProfile-nickname').value,'本地未保存草稿');assert.match(f.d.querySelector('.pp-editor-status').textContent,/其他页面更新/);
    assert.equal(f.d.querySelector('.pp-dialog-foot .pp-text-button').hidden,false);assert.equal(f.calls.filter(row=>row.method==='GET').length,gets);assert.equal(f.calls.filter(row=>row.method==='POST').length,0);
  });
  await check('AC11','仅可信未初始化 profile 可显式导入，其他 section revision 不误禁',async f=>{
    await f.openProfile();const button=f.d.querySelector('.pp-legacy-import');assert.ok(button,'revision > 0 with uninitialized profile permits owned import');
    button.click();await settle();assert.equal(f.d.getElementById('personalProfile-nickname').value,'已归属的旧昵称');assert.equal(f.calls.filter(row=>row.method==='POST').length,0);assert.equal(f.writes.length,0);
    f.d.querySelector('.pp-secondary').click();f.service.initialized.push('profile');await f.openProfile();assert.equal(f.d.querySelector('.pp-legacy-import'),null,'initialized empty profile cannot import');
  },{revision:8,initialized:['settings'],legacy:{nickname:'已归属的旧昵称'},profile:{full_name:'',phone:'',email:'',city:'',occupation:'',company:'',birthday:'',gender:'',bio:''}});
  await check('AC12','收藏删除按稳定对象匹配，失败不误提示且不删错行',async f=>{
    const a={category:'market_news',text:'A',object_id:'a',timestamp:'2026-10-17T01:00:00Z'},b={category:'market_news',text:'B',object_id:'b'},c={category:'market_news',text:'C',object_id:'c'};
    f.service.values.qingzhou_bookmarks=[a,b];f.w.renderBookmarks();const row=[...f.d.querySelectorAll('.mine-saved-item')].find(node=>node.querySelector('p').textContent==='A');const button=row.querySelector('button');
    f.service.values.qingzhou_bookmarks=[c,b,a];button.click();assert.deepEqual(f.pending().values.qingzhou_bookmarks,[c,b]);assert.deepEqual(f.service.values.qingzhou_bookmarks,[c,b,a]);await f.rejectWrite();assert.deepEqual(f.service.values.qingzhou_bookmarks,[c,b,a]);assert.ok(!f.toasts().includes('已删除'));
    f.clearToast();button.click();await f.acceptWrite();assert.deepEqual(f.service.values.qingzhou_bookmarks,[c,b]);assert.ok(f.toasts().includes('已删除'));
  });
  await check('AC13','真实具名聊天收藏 helper 的 DOM 点击等待确认并防重复',async f=>{
    const message=f.d.querySelector('.message.assistant'),button=f.d.getElementById('chatBookmark');f.w.__consumer.configureAccountBookmark(button,message,'需要收藏的回复');
    button.click();assert.equal(button.disabled,true);assert.equal(button.getAttribute('aria-pressed'),'false');assert.deepEqual(f.service.values.qingzhou_bookmarks,[]);button.click();assert.equal(f.writes.length,1);
    await f.rejectWrite();assert.equal(button.getAttribute('aria-pressed'),'false');assert.ok(!f.toasts().includes('已收藏'));f.clearToast();button.click();await f.acceptWrite();
    assert.equal(button.getAttribute('aria-pressed'),'true');assert.equal(f.service.values.qingzhou_bookmarks[0].message_id,'fixture-message');assert.ok(f.toasts().includes('已收藏'));
    f.clearToast();button.click();await f.rejectWrite();assert.equal(button.getAttribute('aria-pressed'),'true');assert.ok(!f.toasts().includes('已取消收藏'));
    await f.notReady();button.__syncAccountBookmark();assert.equal(button.disabled,true);
  });
  await check('AC14','可信旧资料冲突保持待本人确认，读回已初始化 profile 后才公开',async f=>{
    assert.equal(f.w.NavrenPersonalProfile.current(),null);const before=f.calls.filter(row=>row.method==='GET').length;
    f.service.ready=true;f.service.status='ready';f.service.deferReady=true;
    const read=f.w.NavrenPersonalProfile.refresh();await settle();assert.equal(f.calls.filter(row=>row.method==='GET').length,before,'profile GET waits for verified account state');
    assert.equal(f.w.NavrenPersonalProfile.current(),null);assert.notEqual(f.d.getElementById('profileName').textContent,'默认昵称');
    f.service.deferReady=false;for(const wait of f.service.readyQueue.splice(0))wait.resolve(f.accountState());await read;await settle();
    assert.equal(f.w.NavrenPersonalProfile.current(),null,'unconfirmed defaults are not public profile facts');assert.equal(f.d.getElementById('profileName').textContent,'旧资料待确认');
    assert.match(f.d.getElementById('personalProfileSummary').textContent,/旧资料存在冲突，需本人确认/);assert.equal(f.d.getElementById('editPersonalProfile').disabled,false);
    assert.equal(f.d.querySelector('.pp-summary-grid'),null);await f.openProfile();assert.equal(f.d.querySelector('.pp-legacy-import'),null,'conflict never chooses one owned persona import');
    assert.equal(f.d.getElementById('personalProfile-nickname').value,'默认昵称');assert.notEqual(f.d.getElementById('personalProfile-nickname').value,'某画像的旧昵称');
    f.input('nickname','本人确认的新昵称');f.service.deferRefresh=true;f.submitProfile();await settle();await f.acceptPost();
    assert.equal(f.w.NavrenPersonalProfile.current(),null,'POST alone does not clear conflict');assert.ok(f.d.querySelector('.pp-dialog'));assert.ok(!f.toasts().includes('个人资料已保存'));
    assert.equal(f.service.refreshQueue.length,1);f.service.refreshQueue.shift().resolve(f.accountState());await settle();
    assert.equal(f.w.NavrenPersonalProfile.current().nickname,'本人确认的新昵称');assert.equal(f.d.querySelector('.pp-dialog'),null);assert.equal(f.d.getElementById('profileName').textContent,'本人确认的新昵称');assert.ok(f.toasts().includes('个人资料已保存'));
    assert.equal(f.service.migration.legacy_profile,'conflict','initialized profile overrides historical migration flag only after verified readback');
  },{ready:false,initialized:['settings'],migration:{legacy_profile:'conflict',conflicts:['profile']},legacy:{nickname:'某画像的旧昵称'},profile:{full_name:'',phone:'',email:'',city:'',occupation:'',company:'',birthday:'',gender:'',bio:''}});
  await check('AC15','依赖同步抛错不锁死 readPromise，可恢复后重新读取',async f=>{
    f.service.ready=true;f.service.status='ready';const ensure=f.w.BankClient.ensure;
    f.w.BankClient.ensure=()=>{throw Error('EARLY_DEPENDENCY_NOT_READY');};await f.w.NavrenPersonalProfile.refresh();assert.equal(f.w.NavrenPersonalProfile.current(),null);
    f.w.BankClient.ensure=ensure;await f.w.NavrenPersonalProfile.refresh();assert.equal(f.w.NavrenPersonalProfile.current().nickname,'默认昵称');
    const ready=f.w.__consumer.Storage.ready;f.w.__consumer.Storage.ready=()=>{throw Error('EARLY_STORAGE_NOT_READY');};await f.w.NavrenPersonalProfile.refresh();assert.equal(f.w.NavrenPersonalProfile.current(),null);
    f.w.__consumer.Storage.ready=ready;await f.w.NavrenPersonalProfile.refresh();assert.equal(f.w.NavrenPersonalProfile.current().nickname,'默认昵称');
    const guard=f.w.BankClient.guard;f.w.BankClient.guard=()=>{throw Error('EARLY_GUARD_NOT_READY');};f.notify('ready');await settle();assert.equal(f.w.NavrenPersonalProfile.current(),null);
    f.w.BankClient.guard=guard;await f.w.NavrenPersonalProfile.refresh();assert.equal(f.w.NavrenPersonalProfile.current().nickname,'默认昵称');
  },{ready:false});
  const drift=files.filter(file=>sha(fs.readFileSync(path.join(root,file)))!==pins[file]);
  const report={status:results.every(row=>row.passed)&&!drift.length?'PASS_SCOPED_ACCOUNT_CONSUMERS':'FAIL',passed:results.filter(row=>row.passed).length,failed:results.filter(row=>!row.passed).length,
    requested_cases:[...selected],cases:results,source_sha256:pins,source_drift:drift,
    boundaries:{actual_complete_modules:files.filter(file=>file!=='js/chat.js'),chat_only_named_helpers:chatNames,chat_helpers_sha256:sha(chatHelpers),
      replaced_after_actual_initialization:['Storage.accountReady/accountStatus/ready/get/set/setMany/update/legacyOwned/privateIdentity'],
      synthesized:['minimal matching settings/profile/insurance DOM','controlled asynchronous confirmed Storage snapshot and failure queue','BankClient identity/guard and personal-profile DTO/deferred calls','BroadcastChannel transport','NavrenApp navigation','default avatar renderer','showModal/close dialog events','matchMedia'],
      not_covered:['real Storage adapter/BankClient HTTP guard/server persistence (separate sibling scope)','unrelated DOMContentLoaded whole-page initialization','chat whole-page initialization/media/voice lifecycle','browser CSS layout and visual acceptance','avatar upload pixel processing'],
      real_network_calls:0,database_writes:0,paid_calls:0}};
  if(output){fs.mkdirSync(output,{recursive:true});fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2)+'\n');}
  for(const row of results)process.stdout.write((row.passed?'PASS ':'FAIL ')+row.id+' '+row.name+'\n'+(row.error?row.error+'\n':''));
  process.stdout.write(JSON.stringify({status:report.status,passed:report.passed,failed:report.failed,source_drift:drift})+'\n');process.exitCode=report.status.startsWith('PASS')?0:1;
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
