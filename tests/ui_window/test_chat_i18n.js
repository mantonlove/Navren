/* 实际chat.js/i18n.js，屏蔽chat的DOMContentLoaded业务初始化；纯内存DOM、禁止网络。 */
const fs=require('node:fs'),assert=require('node:assert/strict'),crypto=require('node:crypto'),{JSDOM,VirtualConsole}=require('jsdom');
const chat=fs.readFileSync('js/chat.js','utf8'),i18n=fs.readFileSync('js/i18n.js','utf8'),voiceMessages=fs.readFileSync('js/voice_messages.js','utf8'),tick=()=>new Promise(r=>setTimeout(r,5));
const privacyMarkup=fs.readFileSync('chat.html','utf8').match(/<div class="privacy-guide[\s\S]*?\n    <\/div>/)[0];
const results=[];
async function test(name,fn){try{await fn();results.push({name,passed:true});console.log('通过：'+name);}catch(e){results.push({name,passed:false,error:e.message});console.error('失败：'+name+'\n'+e.stack);}}
async function setup(language='en'){
  const errors=[],observers=[],vc=new VirtualConsole();vc.on('jsdomError',error=>errors.push(error.message));
  const dom=new JSDOM('<body data-page="chat"><main id="chatMessages"><div class="message ai"><div class="bubble"><span id="welcomeMsg"></span><div id="presetBar"></div></div></div></main><div class="chat-input-area"><textarea id="userInput" placeholder="查账单、安排转账、管理卡片或聊聊你的计划…"></textarea><button id="voiceBtn"><svg aria-hidden="true"></svg></button><div id="voiceStatus" role="status"></div></div></body>',{url:'https://navren.test/chat.html',runScripts:'outside-only',virtualConsole:vc}),w=dom.window,store=new Map([['language',language],['qingzhou_voiceEnabled',true]]),calls={network:0,initialize:0,send:0};
  const NativeObserver=w.MutationObserver;w.MutationObserver=class extends NativeObserver{constructor(callback){super(callback);observers.push(this);}};
  w.document.body.insertAdjacentHTML('beforeend',privacyMarkup);
  w.Storage={get:k=>store.get(k),set:(k,v)=>store.set(k,v)};w.STORAGE_KEYS={LANGUAGE:'language'};w.matchMedia=()=>({matches:false,addEventListener(){}});
  w.fetch=()=>{calls.network++;throw Error('forbidden_network');};w.BankClient={identity:()=>({workspace_id:'isolated-chat',privacy_revision:1}),refreshIdentity:()=>{calls.initialize++;throw Error('业务初始化不得执行');}};
  w.NavrenSpeech={activeKind:null,stop(){}};w.NavrenViews={url:()=>'/account.html?view=home'};w.showToast=()=>{};w.escapeHtml=value=>{const node=w.document.createElement('span');node.textContent=String(value??'');return node.innerHTML;};
  w.eval(i18n);await tick();
  const add=w.document.addEventListener.bind(w.document);let skipped=0;
  w.document.addEventListener=(type,callback,options)=>{if(type==='DOMContentLoaded'){skipped++;return;}return add(type,callback,options);};
  w.eval(chat+'\nwindow.__chatI18nTest={setMode:m=>{currentMode=m;},useSender:fn=>{sendMessage=fn;},setReference:(ref,view=null)=>{window.NavrenChatReference=ref;window.NavrenChatReferenceBinding=chatBinding();window.NavrenChatReturnView=view;},chatUserMessageHtml,renderChatVoiceReference,renderPresets,personalizeWelcome,voiceButton,setupVoice,setSpeechControl,addMessage,syncChatReference,checkPrivacyGuide,acceptPrivacy};');
  w.document.addEventListener=add;w.__chatI18nTest.useSender(()=>calls.send++);assert.equal(skipped,1,'只屏蔽聊天业务DOMContentLoaded初始化');
  return {dom,w,store,calls,api:w.__chatI18nTest,async close(){await tick();observers.forEach(observer=>observer.disconnect());w.close();assert.deepEqual(errors,[],'存活DOM无未处理错误，关闭前才断开真实observer');}};
}
async function withEnv(language,fn){const e=await setup(language);try{await fn(e);}finally{await e.close();}}
function enableVoiceMessages(e){
  const {w}=e,media={created:[],revoked:[],paused:0,database:0};
  w.URL.createObjectURL=blob=>{assert(blob instanceof w.Blob);const url='blob:https://navren.test/isolated-'+(media.created.length+1);media.created.push(url);return url;};
  w.URL.revokeObjectURL=url=>media.revoked.push(url);
  w.HTMLMediaElement.prototype.pause=function(){media.paused++;};
  w.indexedDB={open(){media.database++;throw Error('forbidden_voice_database');}};
  w.eval(voiceMessages);
  return media;
}
const q={classic:['分析9月账单和异常消费','我有多少可投资资金？','对比活钱和30天产品','关闭视频会员的银行代扣'],senior:['帮我看9月账单','应急的钱留好后，还能投多少？','对比活钱和30天产品','查看银行代扣'],youth:['9月钱花在哪了？','我有多少可投资资金？','活钱和30天产品怎么选？','查看银行代扣']};
(async()=>{
  const privacyEnglish=[
    'To tailor its banking and everyday financial services to you, Navren learns about your investment preferences and assets during conversations and builds a personal profile.',
    'Conversations and demo account records are stored on this computer. Account summaries needed to answer your questions are sent to DeepSeek and GLM.',
    'When you use voice features, recordings and text to be read aloud are sent to Alibaba Cloud Model Studio for processing. Voice messages are stored only in your current browser for playback.',
    'Your data will not be used for other purposes without your confirmation.',
    'Manage conversation permissions in Profile. Change demo risk and funding settings in Accounts.'
  ];
  const privacyChinese=[
    '为提供更贴合你的银行与生活财务服务，Navren会在对话中了解您的投资偏好和资产情况，并建立个人档案。',
    '对话与模拟账户记录保存在本机；回答问题所需的账户摘要会发送至 DeepSeek 和 GLM',
    '使用语音时，录音和待朗读文字发送至阿里云百炼处理；语音消息仅保存在当前浏览器，供你回放',
    '未经您确认，不会用于其他用途',
    '聊天授权在「我的」中管理；模拟风险与资金设置请在「账户」中修改'
  ];
  for(const [mode,chineseTitle,englishTitle] of [['classic','🔒 隐私与个性化服务说明','Privacy & personalization'],['senior','您的专属理财小档案','Your personal financial profile'],['youth','你的数据，你做主','Your data, your choice']])await test(mode+'隐私说明完整英文初始化、往返与接受流程',()=>withEnv('en',async e=>{
    const {w,api}=e;api.setMode(mode);api.checkPrivacyGuide();await tick();
    const guide=w.document.getElementById('privacyGuide'),title=w.document.getElementById('privacyTitle'),button=guide.querySelector('button'),copy=()=>[...guide.querySelectorAll('p,li')].map(n=>n.textContent.trim());
    assert(!guide.classList.contains('hidden'));assert.equal(title.textContent,englishTitle);assert.deepEqual(copy(),privacyEnglish);assert.equal(button.textContent,'Got it');
    w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(title.textContent,chineseTitle);assert.deepEqual(copy(),privacyChinese);assert.equal(button.textContent,'我知道了');
    w.NavrenI18n.setLanguage('en',false);assert.equal(title.textContent,englishTitle);assert.deepEqual(copy(),privacyEnglish);
    api.acceptPrivacy();assert.equal(e.store.get('qingzhou_privacyRead'),true);assert(guide.classList.contains('hidden'));
    w.NavrenI18n.setLanguage('zh-CN',false);assert(guide.classList.contains('hidden'),'切语言不得重新打开已经接受的隐私说明');assert.equal(e.calls.network,0);assert.equal(e.calls.initialize,0);
  }));
  await test('已接受的隐私说明初始化与切语言均不重新要求接受',()=>withEnv('en',async e=>{
    e.store.set('qingzhou_privacyRead',true);e.api.checkPrivacyGuide();const guide=e.w.document.getElementById('privacyGuide');assert(guide.classList.contains('hidden'));e.w.NavrenI18n.setLanguage('zh-CN',false);e.w.NavrenI18n.setLanguage('en',false);assert(guide.classList.contains('hidden'));assert.equal(e.store.get('qingzhou_privacyRead'),true);
  }));
  for(const mode of ['classic','senior','youth'])await test(mode+'预设英文显示且中文往返保留语义和输入',()=>withEnv('en',async e=>{
    const {w,api}=e;api.setMode(mode);api.renderPresets();await tick();const chips=[...w.document.querySelectorAll('.preset-chip')];assert.equal(chips.length,4);assert.deepEqual(chips.map(c=>c.textContent),q[mode].map(x=>w.NavrenI18n.t(x)));assert(chips.every((c,i)=>c.textContent!==q[mode][i]));
    w.document.getElementById('userInput').value='我输入的现金余额 USD 20.00';w.NavrenI18n.setLanguage('zh-CN',false);assert.deepEqual(chips.map(c=>c.textContent),q[mode]);assert.equal(w.document.getElementById('userInput').value,'我输入的现金余额 USD 20.00');w.NavrenI18n.setLanguage('en',false);assert.deepEqual(chips.map(c=>c.textContent),q[mode].map(x=>w.NavrenI18n.t(x)));chips[0].click();assert.equal(e.calls.send,1);assert.equal(w.document.getElementById('userInput').value,q[mode][0],'预设原问题没有改写成另一业务动作');assert.equal(e.calls.network,0);
  }));
  for(const [hour,greeting,enGreeting]of [[7,'早上好','Good morning'],[14,'下午好','Good afternoon'],[21,'晚上好','Good evening']])await test(greeting+'欢迎语英文初始化与往返恢复',()=>withEnv('en',async e=>{
    const {w,api}=e,Original=w.Intl.DateTimeFormat;w.Intl.DateTimeFormat=function(locale,options){assert.equal(options.timeZone,'Asia/Shanghai');return {format:()=>String(hour)};};api.personalizeWelcome();w.Intl.DateTimeFormat=Original;const node=w.document.getElementById('welcomeMsg'),raw=node.textContent;assert(raw.startsWith(greeting));await tick();assert(node.textContent.startsWith(enGreeting));assert(node.textContent.includes('accounts, payments, cards, product research, goals and your financial calendar'));w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(node.textContent,raw);w.NavrenI18n.setLanguage('en',false);assert(node.textContent.startsWith(enGreeting));assert.equal(e.calls.initialize,0);
  }));
  for(const [state,label,status]of [['idle','Voice input',''],['connecting','Connecting voice; click to cancel','Connecting to voice service…'],['listening','Listening; click again to finish, or press Esc to cancel','Listening. Recognized text appears in the input. Finish to send your audio and receive a spoken reply.'],['finishing','Processing your recording; click or press Esc to cancel','Processing recognized speech…']])await test('语音'+state+'属性、状态和中英往返',()=>withEnv('en',async e=>{
    const {w,api}=e;api.setupVoice();api.voiceButton(state);await tick();const button=w.document.getElementById('voiceBtn'),node=w.document.getElementById('voiceStatus');assert.equal(button.title,label);assert.equal(button.getAttribute('aria-label'),label);assert.equal(node.textContent,status);assert.equal(button.getAttribute('aria-pressed'),String(state!=='idle'));w.NavrenI18n.setLanguage('zh-CN',false);if(state!=='idle')assert(/[\u3400-\u9fff]/.test(node.textContent));const chineseTitle=button.title;w.NavrenI18n.setLanguage('en',false);assert.equal(button.title,label);assert.notEqual(chineseTitle,label);assert.equal(node.textContent,status);assert.equal(e.calls.network,0);
  }));
  await test('朗读控件状态和静音图标说明中英往返',()=>withEnv('en',async e=>{
    const {w,api}=e,button=w.document.createElement('button');w.document.body.append(button);for(const [state,title]of [['idle','Read this reply aloud'],['preparing','Preparing audio; click to stop'],['speaking','Stop reading']]){api.setSpeechControl(button,state);await tick();assert.equal(button.title,title);assert(button.querySelector('svg'));w.NavrenI18n.setLanguage('zh-CN',false);assert(/[\u3400-\u9fff]/.test(button.title));w.NavrenI18n.setLanguage('en',false);assert.equal(button.title,title);}button.dataset.muted='true';api.setSpeechControl(button,'idle');await tick();assert.equal(button.dataset.state,'muted');assert.equal(button.title,'Reading is off; click to read this reply');assert.equal(button.textContent,'','图标按钮无额外文字');
  }));
  await test('实际addMessage保护用户正文与标明原件的资料',()=>withEnv('en',async e=>{
    const {w,api}=e;const user=api.addMessage('user','现金余额 · 我自己的原话 12.34 元');const original=api.addMessage('ai','<p translate="no">总资产（本金） · 原始资料 12.34 元</p>');await tick();assert.equal(user.querySelector('.bubble').textContent,'现金余额 · 我自己的原话 12.34 元');assert.equal(original.querySelector('[translate="no"]').textContent,'总资产（本金） · 原始资料 12.34 元');w.NavrenI18n.setLanguage('zh-CN',false);w.NavrenI18n.setLanguage('en',false);assert.equal(user.querySelector('.bubble').textContent,'现金余额 · 我自己的原话 12.34 元');assert.equal(original.querySelector('[translate="no"]').textContent,'总资产（本金） · 原始资料 12.34 元');
  }));
  await test('仅最后回复保留朗读控件且用户消息不增加该控件',()=>withEnv('en',async e=>{
    const {w,api}=e,first=api.addMessage('ai','第一份原始答复');api.addMessage('user','用户原话');const last=api.addMessage('ai','第二份原始答复');await tick();assert.equal(first.querySelectorAll('.message-audio-control').length,0);assert.equal(last.querySelectorAll('.message-audio-control').length,1);assert.equal(w.document.querySelectorAll('.message.user .message-audio-control').length,0);assert.equal(last.querySelector('.message-audio-control').title,'Read this reply aloud');
  }));
  await test('引用工具栏标签英文、来源对象名称原文保留',()=>withEnv('en',async e=>{
    const {w,api}=e;api.setReference({kind:'account',id:'original-one',label:'现金余额',source_scope:'simulation'},{page:'account',view:'home'});api.syncChatReference();await tick();const bar=w.document.getElementById('navrenChatContext');assert(!bar.hidden);assert.equal(bar.getAttribute('aria-label'),'Reference material');assert.equal(bar.querySelector('a').textContent,'Back');assert.equal(bar.querySelector('a').getAttribute('aria-label'),'Return to reference material');assert.equal(bar.querySelector('button').getAttribute('aria-label'),'Remove reference material');assert.equal(bar.querySelector('.chat-reference-title').textContent,'现金余额');assert.equal(bar.querySelector('.chat-reference-title').title,'现金余额');assert.equal(bar.querySelector('.chat-reference-meta').textContent,'Account details');w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(bar.querySelector('a').textContent,'返回');assert.equal(bar.querySelector('.chat-reference-title').textContent,'现金余额');assert.equal(bar.querySelector('.chat-reference-meta').textContent,'账户资料');assert.equal(e.calls.network,0);
  }));
  await test('引用移除按钮的可访问名称英文及中文往返',()=>withEnv('en',async e=>{
    const {w,api}=e;api.setReference({kind:'account',id:'original-one',label:'现金余额'});api.syncChatReference();await tick();const button=()=>w.document.querySelector('#navrenChatContext button');assert.equal(button().getAttribute('aria-label'),'Remove reference material');assert.equal(button().title,'Remove reference material');w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(button().getAttribute('aria-label'),'移除关联资料');w.NavrenI18n.setLanguage('en',false);assert.equal(button().getAttribute('aria-label'),'Remove reference material');w.document.getElementById('userInput').value='我另外输入的原文';button().click();assert(w.document.getElementById('navrenChatContext').hidden);assert.equal(w.document.getElementById('userInput').value,'我另外输入的原文');assert.equal(e.calls.send,0);
  }));
  await test('来源固定标签及版本可本地化，原source_label和标题不变',()=>withEnv('en',async e=>{
    const {w,api}=e;api.setReference({kind:'event',id:'original-one',label:'现金余额',source_scope:'market',edition_date:'2026-10-09',revision:2,version:'event-v3'});api.syncChatReference();await tick();const meta=()=>w.document.querySelector('.chat-reference-meta');assert.equal(meta().textContent,'Public market material · 2026-10-09 · Edition 2 · Version event-v3');assert.equal(meta().title,'Public market material · 2026-10-09 · Version event-v3');w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(meta().textContent,'公开市场资料 · 2026-10-09 · 第 2 版 · 版本 event-v3');w.NavrenI18n.setLanguage('en',false);assert.equal(w.document.querySelector('.chat-reference-title').textContent,'现金余额');
    api.setReference({kind:'event',id:'original-one',title:'公开市场资料',source_scope:'market',source_label:'现金余额',edition_date:'2026-10-09',version:'原版本号'});api.syncChatReference();await tick();assert.equal(w.document.querySelector('.chat-reference-title').textContent,'公开市场资料');assert.equal(w.document.querySelector('.chat-reference-title').title,'公开市场资料');assert.equal(meta().textContent,'现金余额 · 2026-10-09 · Version 原版本号');w.NavrenI18n.setLanguage('zh-CN',false);w.NavrenI18n.setLanguage('en',false);await tick();assert.equal(meta().textContent,'现金余额 · 2026-10-09 · Version 原版本号');assert.equal(e.calls.network,0);
  }));
  await test('已发送文本及语音摘要仅翻译固定槽，原话和标题往返不变',()=>withEnv('en',async e=>{
    const {w,api}=e,ref={kind:'account',id:'original-one',label:'现金余额'},view={page:'account',view:'home'},original='公开市场资料 · 我的用户原话 USD 12.34';
    const message=api.addMessage('user',api.chatUserMessageHtml(original,ref,view)),summary=message.querySelector('.chat-user-reference'),title=summary.querySelector('.chat-user-reference-title'),text=message.querySelector('.chat-user-text');
    assert.equal(summary.getAttribute('aria-label'),'Reference material sent with this message');assert.equal(summary.querySelector('.chat-user-reference-label').textContent,'Reference material');assert.equal(summary.querySelector('a').textContent,'View material');assert.equal(title.textContent,'现金余额');assert.equal(text.textContent,original);
    const voice=api.addMessage('user',''),transcript=w.document.createElement('div');transcript.className='voice-transcript';transcript.textContent='现金余额 · 用户语音转写';voice.querySelector('.bubble').append(transcript);api.renderChatVoiceReference(voice,ref,view);const voiceSummary=voice.querySelector('.chat-user-reference');
    w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(summary.querySelector('.chat-user-reference-label').textContent,'关联资料');assert.equal(summary.querySelector('a').textContent,'查看资料');assert.equal(summary.getAttribute('aria-label'),'随此消息发送的关联资料');assert.equal(voiceSummary.querySelector('a').textContent,'查看资料');
    w.NavrenI18n.setLanguage('en',false);await tick();assert.equal(summary.querySelector('a').textContent,'View material');assert.equal(voiceSummary.querySelector('.chat-user-reference-label').textContent,'Reference material');assert.strictEqual(summary.querySelector('.chat-user-reference-title'),title,'语言切换不重建原引用标题节点');assert.strictEqual(message.querySelector('.chat-user-text'),text,'语言切换不重建用户正文');assert.equal(title.textContent,'现金余额');assert.equal(text.textContent,original);assert.equal(transcript.textContent,'现金余额 · 用户语音转写');assert.equal(e.calls.send,0);assert.equal(e.calls.network,0);
  }));
  await test('缺绑定或异主体引用不能因语言刷新复活',()=>withEnv('en',async e=>{
    const {w,api}=e;api.setReference({kind:'account',id:'original-one',label:'现金余额'});w.NavrenChatReferenceBinding={workspace_id:'other',privacy_revision:1};api.syncChatReference();assert(w.document.getElementById('navrenChatContext').hidden);w.NavrenI18n.setLanguage('zh-CN',false);w.NavrenI18n.setLanguage('en',false);assert(w.document.getElementById('navrenChatContext').hidden);assert(!w.document.querySelector('.chat-reference-title'));w.NavrenChatReferenceBinding=null;api.syncChatReference();assert(w.document.getElementById('navrenChatContext').hidden);assert.equal(e.calls.network,0);
  }));
  await test('真实语音消息在用户气泡中初始化英文控件且转写原文往返保持',()=>withEnv('en',async e=>{
    const {w,api}=e,media=enableVoiceMessages(e),message=api.addMessage('user',''),original='现金余额 · 我的原始转写 USD 12.34';
    w.VoiceMessages.mount(message,{blob:new w.Blob(['isolated audio fixture'],{type:'audio/webm'}),duration:2.4},original);await tick();
    const title=message.querySelector('.voice-message-title'),audio=message.querySelector('audio'),toggle=message.querySelector('.voice-transcript-toggle'),transcript=message.querySelector('.voice-transcript');
    assert.equal(title.textContent,'Voice message · 2 s');assert.equal(audio.getAttribute('aria-label'),'Play my voice message');assert.equal(toggle.textContent,'View transcript');assert.equal(toggle.getAttribute('aria-expanded'),'false');assert(transcript.hidden);assert.equal(transcript.textContent,original);assert.equal(transcript.getAttribute('translate'),'no');assert.equal(transcript.dataset.i18nSkip,'');assert.equal(audio.src,media.created[0]);
    toggle.click();assert.equal(toggle.textContent,'Hide transcript');assert.equal(toggle.getAttribute('aria-expanded'),'true');assert(!transcript.hidden);
    w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(title.textContent,'语音消息 · 2 秒');assert.equal(audio.getAttribute('aria-label'),'回放我的语音消息');assert.equal(toggle.textContent,'收起文字');assert.equal(transcript.textContent,original);
    toggle.click();assert.equal(toggle.textContent,'查看文字');assert(transcript.hidden);w.NavrenI18n.setLanguage('en',false);assert.equal(toggle.textContent,'View transcript');assert.equal(title.textContent,'Voice message · 2 s');assert.equal(transcript.textContent,original);assert.equal(media.database,0);assert.equal(e.calls.network,0);
  }));
  await test('语音识别占位状态随语言改变且真实识别文本没有被改写',()=>withEnv('en',async e=>{
    const {w,api}=e,media=enableVoiceMessages(e),message=api.addMessage('user','');
    w.VoiceMessages.mount(message,{blob:new w.Blob(['fixture']),duration:0.2});await tick();const transcript=message.querySelector('.voice-transcript');
    assert.equal(message.querySelector('.voice-message-title').textContent,'Voice message · 1 s');assert.equal(transcript.textContent,'Recognizing speech…');w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(transcript.textContent,'正在识别文字…');
    const original='查账单 · 识别出的用户原话 0 元';w.VoiceMessages.update(message,original);assert.equal(transcript.dataset.voicePlaceholder,undefined);w.NavrenI18n.setLanguage('en',false);assert.equal(transcript.textContent,original);w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(transcript.textContent,original);
    w.VoiceMessages.update(message,'');assert.equal(transcript.textContent,'没有识别到文字，你仍可以回放音频或重新录音。');w.NavrenI18n.setLanguage('en',false);assert.equal(transcript.textContent,'No speech was recognized. You can replay the audio or record again.');w.NavrenI18n.setLanguage('zh-CN',false);assert.equal(transcript.textContent,'没有识别到文字，你仍可以回放音频或重新录音。');assert.equal(media.database,0);assert.equal(e.calls.network,0);
  }));
  await test('语音消息身份失效时停播并撤销隔离音频地址',()=>withEnv('en',async e=>{
    const {w,api}=e,media=enableVoiceMessages(e);for(let n=0;n<2;n++)w.VoiceMessages.mount(api.addMessage('user',''),{blob:new w.Blob(['fixture '+n]),duration:2},'原始语音转写 '+n);
    w.dispatchEvent(new w.CustomEvent('navren:identity-revoked'));assert.equal(media.paused,2);assert.deepEqual(media.revoked,media.created);assert([...w.document.querySelectorAll('.voice-message audio')].every(audio=>!audio.hasAttribute('src')));assert.equal(media.database,0);assert.equal(e.calls.network,0);
  }));
  const passed=results.filter(r=>r.passed).length;console.log(JSON.stringify({passed,total:results.length,network:'forbidden',chat_sha256:crypto.createHash('sha256').update(chat).digest('hex'),i18n_sha256:crypto.createHash('sha256').update(i18n).digest('hex'),voice_messages_sha256:crypto.createHash('sha256').update(voiceMessages).digest('hex'),failures:results.filter(r=>!r.passed)},null,2));if(passed!==results.length)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1;});
