/* 使用浏览器 MediaRecorder 和 IndexedDB。音频仅存此浏览器，不写入服务器或LocalStorage。 */
window.VoiceMessages=(()=>{
  let database;
  const urls=new Set();
  const t=text=>window.NavrenI18n?.t(text)||text;
  function labels(bubble){
    const title=bubble.querySelector('.voice-message-title'),audio=bubble.querySelector('audio'),toggle=bubble.querySelector('.voice-transcript-toggle'),transcript=bubble.querySelector('.voice-transcript');
    if(title)title.textContent=t('语音消息 · '+bubble.dataset.voiceSeconds+' 秒');
    audio?.setAttribute('aria-label',t('回放我的语音消息'));
    if(toggle)toggle.textContent=t(transcript?.hidden?'查看文字':'收起文字');
    if(transcript?.dataset.voicePlaceholder)transcript.textContent=t(transcript.dataset.voicePlaceholder);
  }
  function db(){
    if(!database)database=new Promise((resolve,reject)=>{
      const request=indexedDB.open('navren-voice-messages',1);
      request.onupgradeneeded=()=>request.result.createObjectStore('clips',{keyPath:'id'});
      request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
    });
    return database;
  }
  async function save(recording){
    const session=await BankClient.ensure(),guard=BankClient.guard();
    if(!guard.valid(session))throw new DOMException('语音所属账户已变化','AbortError');
    const id=crypto.randomUUID(),database=await db();
    if(!guard.valid(session))throw new DOMException('语音所属账户已变化','AbortError');
    const clip={id,workspace:session.workspace_id,privacy_revision:session.privacy_revision,blob:recording.blob,duration:recording.duration,created_at:Date.now()};
    await new Promise((resolve,reject)=>{
      const tx=database.transaction('clips','readwrite');tx.objectStore('clips').put(clip);
      tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);
    });
    if(!guard.valid(session))throw new DOMException('语音所属账户已变化','AbortError');
    return clip;
  }
  async function get(id){
    const session=await BankClient.ensure(),guard=BankClient.guard();
    if(!guard.valid(session))return null;
    const database=await db();
    if(!guard.valid(session))return null;
    const clip=await new Promise((resolve,reject)=>{const request=database.transaction('clips').objectStore('clips').get(id);request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});
    return guard.valid(session)&&clip?.workspace===session.workspace_id&&clip.privacy_revision===session.privacy_revision?clip:null;
  }
  function mount(message,clip,text=''){
    const bubble=message.querySelector('.bubble');bubble.textContent='';bubble.classList.add('voice-message');
    bubble.dataset.voiceSeconds=String(Math.max(1,Math.round(clip.duration)));
    const title=document.createElement('div');title.className='voice-message-title';title.textContent='语音消息 · '+Math.max(1,Math.round(clip.duration))+' 秒';
    const audio=document.createElement('audio');audio.controls=true;audio.preload='metadata';audio.setAttribute('aria-label','回放我的语音消息');
    const url=URL.createObjectURL(clip.blob);urls.add(url);audio.src=url;
    audio.addEventListener('play',()=>{NavrenSpeech.stop();document.querySelectorAll('.voice-message audio').forEach(other=>{if(other!==audio)other.pause();});});
    const toggle=document.createElement('button');toggle.type='button';toggle.className='voice-transcript-toggle';toggle.textContent='查看文字';toggle.setAttribute('aria-expanded','false');
    const transcript=document.createElement('div');transcript.className='voice-transcript';transcript.hidden=true;transcript.textContent=text||'正在识别文字…';transcript.setAttribute('translate','no');transcript.dataset.i18nSkip='';if(!text)transcript.dataset.voicePlaceholder='正在识别文字…';
    toggle.onclick=()=>{transcript.hidden=!transcript.hidden;labels(bubble);toggle.setAttribute('aria-expanded',String(!transcript.hidden));};
    bubble.append(title,audio,toggle,transcript);
    labels(bubble);
  }
  function update(message,text){const el=message?.querySelector('.voice-transcript');if(el){if(text){delete el.dataset.voicePlaceholder;el.textContent=text;}else{el.dataset.voicePlaceholder='没有识别到文字，你仍可以回放音频或重新录音。';el.textContent=t(el.dataset.voicePlaceholder);}}}
  async function restore(message,entry){
    const guard=BankClient.guard();
    try{const clip=await get(entry.recording_id);if(!guard.valid()||!message.isConnected)return;if(clip)mount(message,clip,entry.content);else message.querySelector('.bubble').textContent='语音文件在此浏览器中不可用。'+(entry.content||'');}
    catch(e){if(guard.valid()&&message.isConnected)message.querySelector('.bubble').textContent='语音暂时无法读取。'+(entry.content||'');}
  }
  function clear(){document.querySelectorAll('.voice-message audio').forEach(audio=>{audio.pause();audio.removeAttribute('src');});urls.forEach(URL.revokeObjectURL);urls.clear();}
  window.addEventListener('pagehide',clear);
  window.addEventListener('navren:privacy-reset',clear);
  window.addEventListener('navren:identity-revoked',clear);
  window.addEventListener('navren:language',()=>document.querySelectorAll('.voice-message').forEach(labels));
  return {save,mount,update,restore};
})();
