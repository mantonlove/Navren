/* 实际 VoiceMessages + BankClient；内存 fake IndexedDB，禁止网络和真实录音。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),crypto=require('node:crypto'),{JSDOM}=require('jsdom');
const files=['js/bank_client.js','js/voice_messages.js'],source=Object.fromEntries(files.map(p=>[p,fs.readFileSync(p,'utf8')]));
const tick=()=>new Promise(r=>setTimeout(r,5)),results=[];
const identity=(name='old',revision=0)=>({workspace_id:'fixture-'+name,privacy_revision:revision,csrf:'fixture-csrf-'+name});
function fakeIDB({holdOpen=false,holdGet=false,holdPut=false}={}){
  const records=new Map(),pending={open:[],get:[],put:[]},counts={open:0,read:0,write:0};
  const db={createObjectStore(){},transaction(storeName,mode){
    assert.equal(storeName,'clips');assert(mode===undefined||mode==='readwrite');
    const tx={objectStore(name){assert.equal(name,'clips');return {
      put(value){counts.write++;records.set(value.id,{...value});pending.put.push(tx);if(!holdPut)queueMicrotask(()=>finishPut(tx));},
      get(id){counts.read++;const request={id};pending.get.push(request);if(!holdGet)queueMicrotask(()=>finishGet(request));return request;}
    };}};return tx;
  }};
  function finishOpen(request=pending.open[0]){assert(request,'没有等待中的open');pending.open.splice(pending.open.indexOf(request),1);request.result=db;request.onupgradeneeded?.();request.onsuccess?.();}
  function finishGet(request=pending.get[0],error=null){assert(request,'没有等待中的get');pending.get.splice(pending.get.indexOf(request),1);if(error){request.error=error;request.onerror?.();}else{const record=records.get(request.id);request.result=record?{...record}:undefined;request.onsuccess?.();}}
  function finishPut(tx=pending.put[0],error=null){assert(tx,'没有等待中的put');pending.put.splice(pending.put.indexOf(tx),1);if(error){tx.error=error;tx.onerror?.();}else tx.oncomplete?.();}
  return {records,pending,counts,finishOpen,finishGet,finishPut,api:{open(name,version){assert.equal(name,'navren-voice-messages');assert.equal(version,1);counts.open++;const request={};pending.open.push(request);if(!holdOpen)queueMicrotask(()=>finishOpen(request));return request;}}};
}
async function setup(options={}){
  const dom=new JSDOM('<body><main id="messages"></main></body>',{url:'https://navren.test/chat.html',runScripts:'outside-only'}),w=dom.window,idb=fakeIDB(options),media={created:[],revoked:[],paused:0,stopped:0,network:0};let view=0;
  w.fetch=()=>{media.network++;throw Error('forbidden_network');};w.indexedDB=idb.api;w.NavrenI18n={t:text=>text};w.NavrenViews={revision:()=>view};w.NavrenSpeech={stop(){media.stopped++;}};
  w.HTMLMediaElement.prototype.pause=function(){media.paused++;};w.URL.createObjectURL=blob=>{assert(blob instanceof w.Blob);const url='blob:https://navren.test/voice-'+(media.created.length+1);media.created.push(url);return url;};w.URL.revokeObjectURL=url=>media.revoked.push(url);
  const getExposure='return {save,mount,update,restore};';assert.equal(source['js/voice_messages.js'].split(getExposure).length,2,'只给现有内部get增加测试入口，不改其执行体');
  for(const path of files)w.eval(path==='js/voice_messages.js'?source[path].replace(getExposure,'return {save,mount,update,restore,__getForTest:get};'):source[path]);w.BankClient.acceptSelectedSession(identity());
  const message=()=>{const el=w.document.createElement('div');el.className='message user';el.innerHTML='<div class="bubble">原始消息占位</div>';w.document.getElementById('messages').append(el);return el;};
  const clip=(overrides={})=>({id:'clip-old',workspace:'fixture-old',privacy_revision:0,blob:new w.Blob(['isolated fixture'],{type:'audio/webm'}),duration:3,created_at:1,...overrides});
  return {w,idb,media,message,clip,change(name='new',revision=0){w.BankClient.acceptSelectedSession(identity(name,revision));},changeView(){view++;},close(){assert.equal(media.network,0);w.close();}};
}
async function withEnv(options,fn){const e=await setup(options);try{await fn(e);}finally{e.close();}}
async function test(name,fn){try{await fn();results.push({name,passed:true});console.log('通过：'+name);}catch(e){results.push({name,passed:false,error:e.message});console.error('失败：'+name+'\n'+e.stack);}}
(async()=>{
  await test('实际save/get保留当前工作区、0隐私版本及音频本体',()=>withEnv({},async e=>{
    const recording={blob:e.clip().blob,duration:3.25},saved=await e.w.VoiceMessages.save(recording);assert.equal(saved.workspace,'fixture-old');assert.equal(saved.privacy_revision,0);assert.equal(saved.blob,recording.blob);assert.equal(saved.duration,3.25);assert.equal(e.idb.counts.write,1);const read=await e.w.VoiceMessages.__getForTest(saved.id);assert.equal(read.id,saved.id);assert.equal(read.blob,recording.blob);assert.equal(e.idb.counts.open,1);
  }));
  for(const [description,overrides]of [['缺少隐私版本',{privacy_revision:undefined}],['错误隐私版本',{privacy_revision:1}],['其它工作区',{workspace:'fixture-other'}]])await test(description+'音频不能被当前账户get读取',()=>withEnv({},async e=>{
    e.idb.records.set('clip-old',e.clip(overrides));assert.equal(await e.w.VoiceMessages.__getForTest('clip-old'),null);assert.equal(e.media.created.length,0);
  }));
  await test('save等待打开DB时切换身份，拒绝写入和返回音频',()=>withEnv({holdOpen:true},async e=>{
    const pending=e.w.VoiceMessages.save({blob:e.clip().blob,duration:3}).catch(error=>error);await tick();assert.equal(e.idb.pending.open.length,1);e.change();e.idb.finishOpen();const result=await pending;assert.equal(result.name,'AbortError');assert.equal(e.idb.counts.write,0);
  }));
  await test('save事务迟到完成不能向新身份返回旧音频',()=>withEnv({holdPut:true},async e=>{
    const pending=e.w.VoiceMessages.save({blob:e.clip().blob,duration:3}).catch(error=>error);await tick();assert.equal(e.idb.pending.put.length,1);assert.equal([...e.idb.records.values()][0].workspace,'fixture-old');e.change();e.idb.finishPut();const result=await pending;assert.equal(result.name,'AbortError');assert.equal([...e.idb.records.values()][0].workspace,'fixture-old','已提交夹具仍绑定旧账户，不迁到新账户');
  }));
  await test('get等待打开DB时身份改变，迟到旧音频不可返回',()=>withEnv({holdOpen:true},async e=>{
    e.idb.records.set('clip-old',e.clip());const pending=e.w.VoiceMessages.__getForTest('clip-old');await tick();e.change();e.idb.finishOpen();assert.equal(await pending,null);
  }));
  await test('get读取迟到且隐私版本改变，旧音频不可返回',()=>withEnv({holdGet:true},async e=>{
    e.idb.records.set('clip-old',e.clip());const pending=e.w.VoiceMessages.__getForTest('clip-old');await tick();e.change('old',1);e.idb.finishGet();assert.equal(await pending,null);
  }));
  await test('restore读取迟到且账户已变，旧消息不挂载也不写fallback',()=>withEnv({holdGet:true},async e=>{
    e.idb.records.set('clip-old',e.clip());const message=e.message(),pending=e.w.VoiceMessages.restore(message,{recording_id:'clip-old',content:'旧账户原始转写'});await tick();e.change();e.idb.finishGet();await pending;assert.equal(message.querySelector('audio'),null);assert.equal(message.querySelector('.bubble').textContent,'原始消息占位');assert.equal(e.media.created.length,0);
  }));
  await test('restore读取迟到且旧元素已移除，不能生成音频URL',()=>withEnv({holdGet:true},async e=>{
    e.idb.records.set('clip-old',e.clip());const message=e.message(),pending=e.w.VoiceMessages.restore(message,{recording_id:'clip-old',content:'原始转写'});await tick();message.remove();e.idb.finishGet();await pending;assert.equal(e.media.created.length,0);assert.equal(message.querySelector('.bubble').textContent,'原始消息占位');
  }));
  await test('restore当前身份有效时真实挂载音频且保留原始转写',()=>withEnv({},async e=>{
    e.idb.records.set('clip-old',e.clip());const message=e.message();await e.w.VoiceMessages.restore(message,{recording_id:'clip-old',content:'原始识别：现金余额 USD 12.34'});assert.equal(message.querySelector('audio').src,e.media.created[0]);assert.equal(message.querySelector('.voice-transcript').textContent,'原始识别：现金余额 USD 12.34');
  }));
  await test('restore缺隐私版本只有不可用提示，没有旧音频',()=>withEnv({},async e=>{
    e.idb.records.set('clip-old',e.clip({privacy_revision:undefined}));const message=e.message();await e.w.VoiceMessages.restore(message,{recording_id:'clip-old',content:'当前历史原始转写'});assert.equal(message.querySelector('audio'),null);assert.equal(message.querySelector('.bubble').textContent,'语音文件在此浏览器中不可用。当前历史原始转写');assert.equal(e.media.created.length,0);
  }));
  await test('restore旧请求失败不能在新身份或新视图中插入错误提示',()=>withEnv({holdGet:true},async e=>{
    const message=e.message(),pending=e.w.VoiceMessages.restore(message,{recording_id:'clip-old',content:'旧转写'});await tick();e.changeView();e.idb.finishGet(undefined,new e.w.Error('isolated read failure'));await pending;assert.equal(message.querySelector('.bubble').textContent,'原始消息占位');assert.equal(e.media.created.length,0);
  }));
  await test('privacy-reset停播所有本地音频、清src并撤销URL',()=>withEnv({},async e=>{
    const first=e.message(),second=e.message();e.w.VoiceMessages.mount(first,e.clip(),'原始一');e.w.VoiceMessages.mount(second,e.clip({id:'clip-two'}),'原始二');e.w.dispatchEvent(new e.w.Event('navren:privacy-reset'));assert.equal(e.media.paused,2);assert.deepEqual(e.media.revoked,e.media.created);assert([first,second].every(message=>!message.querySelector('audio').hasAttribute('src')));assert.equal(e.idb.counts.open,0);
  }));
  await test('save在ensure续行前切换，旧session不能借新guard返回',()=>withEnv({},async e=>{
    const pending=e.w.VoiceMessages.save({blob:e.clip().blob,duration:3}).catch(error=>error);e.change();const result=await pending;assert(result instanceof e.w.Error||result instanceof e.w.DOMException||result.workspace==='fixture-new','ensure返回旧session、新guard却有效，不能返回旧账户音频');
  }));
  await test('get在ensure续行前切换，旧clip不能借新guard返回',()=>withEnv({},async e=>{
    e.idb.records.set('clip-old',e.clip());const pending=e.w.VoiceMessages.__getForTest('clip-old');e.change();assert.equal(await pending,null,'ensure得到的旧session必须与guard绑定同一身份');
  }));
  const passed=results.filter(r=>r.passed).length;console.log(JSON.stringify({passed,total:results.length,network:'forbidden',indexeddb:'isolated-memory-only',source_hashes:Object.fromEntries(files.map(path=>[path,crypto.createHash('sha256').update(source[path]).digest('hex')])),failures:results.filter(r=>!r.passed)},null,2));if(passed!==results.length)process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
