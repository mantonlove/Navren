/* 原录音先上传服务器；IDB 仅为可选本机缓存，不能充当归档或媒体授权。 */
window.VoiceMessages=(()=>{
  let database,epoch=0,activeSaved=null;
  const pending=new Set(),mounts=new Set();
  const t=text=>window.NavrenI18n?.t(text)||text;
  const cancelled=()=>new DOMException('语音所属账户或页面已变化','AbortError');
  function mediaClient(){if(!window.NavrenChatMedia)throw new Error('音频保存服务尚未就绪。');return window.NavrenChatMedia;}
  function operation(signal){
    const identity=BankClient.identity(),guard=BankClient.guard(),version=epoch,controller=new AbortController();
    if(!identity||!guard.valid(identity)||signal?.aborted)throw cancelled();
    const abort=()=>controller.abort();signal?.addEventListener('abort',abort,{once:true});pending.add(controller);
    return {identity,guard,signal:controller.signal,
      current:()=>version===epoch&&!controller.signal.aborted&&guard.valid(identity),
      check(){if(!this.current())throw cancelled();},
      close(){pending.delete(controller);signal?.removeEventListener('abort',abort);},cancel:abort};
  }
  function labels(bubble){
    const title=bubble.querySelector('.voice-message-title'),audio=bubble.querySelector('audio'),toggle=bubble.querySelector('.voice-transcript-toggle'),transcript=bubble.querySelector('.voice-transcript');
    if(title){const titleText=bubble.dataset.voiceAssistant==='true'?'回复朗读':'语音消息';title.textContent=t(bubble.dataset.voiceSeconds?titleText+' · '+bubble.dataset.voiceSeconds+' 秒':titleText);}
    audio?.setAttribute('aria-label',t(bubble.dataset.voiceAssistant==='true'?'回放已保存的回复朗读':'回放我的语音消息'));
    if(toggle)toggle.textContent=t(transcript?.hidden?'查看文字':'收起文字');
    if(transcript?.dataset.voicePlaceholder)transcript.textContent=t(transcript.dataset.voicePlaceholder);
  }
  function db(){
    if(!database)database=new Promise((resolve,reject)=>{
      const request=indexedDB.open('navren-voice-messages',1);
      request.onupgradeneeded=()=>request.result.createObjectStore('clips',{keyPath:'id'});
      request.onsuccess=()=>resolve(request.result);request.onerror=()=>{database=null;reject(request.error);};
    });
    return database;
  }
  async function cache(clip,op){
    const store=await db();op.check();
    await new Promise((resolve,reject)=>{
      const tx=store.transaction('clips','readwrite');tx.objectStore('clips').put(clip);
      tx.oncomplete=resolve;tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||cancelled());
    });
  }
  async function save(recording){
    const op=operation(recording?.signal);
    try{
      if(!(recording?.blob instanceof Blob)||!recording.blob.size)throw new Error('录音文件为空，请重试。');
      const client=mediaClient();
      const media=await client.upload(recording.blob,{signal:op.signal,...(recording.uploadKey?{uploadKey:recording.uploadKey}:{})});op.check();
      client.validate(media,{boundOnly:false});if(media.kind!=='audio')throw new Error('返回的媒体不是录音。');
      const clip={id:crypto.randomUUID(),registered_user_id:op.identity.registered_user_id,
        workspace:op.identity.workspace_id,privacy_revision:op.identity.privacy_revision,
        blob:recording.blob,duration:Number.isFinite(recording.duration)?Math.max(0,recording.duration):0,
        media,created_at:Date.now()};
      // 上传已成功；IDB 不可用不能把成功原件误报为上传失败。
      cache(clip,op).catch(()=>{});
      return clip;
    }finally{op.close();}
  }
  async function get(id){
    const op=operation();
    try{
      if(typeof id!=='string'||!id)return null;
      const store=await db();op.check();
      const clip=await new Promise((resolve,reject)=>{const request=store.transaction('clips').objectStore('clips').get(id);request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});op.check();
      return clip?.registered_user_id===op.identity.registered_user_id&&clip?.workspace===op.identity.workspace_id&&clip.privacy_revision===op.identity.privacy_revision?clip:null;
    }finally{op.close();}
  }
  async function migrateOwned(entry){
    const op=operation();
    try{
      if(entry?.role!=='user'||entry.registered_user_id!==op.identity.registered_user_id||
        entry.workspace_id!==op.identity.workspace_id||entry.privacy_revision!==op.identity.privacy_revision)
        return {status:'not_eligible',reason:'entry_binding'};
      const recordingId=entry.recording_id,requestId=entry.bank_response_id;
      if(typeof recordingId!=='string'||!recordingId)return {status:'not_eligible',reason:'recording_missing'};
      let clip;
      op.check();
      try{clip=await get(recordingId);}catch(error){op.check();return {status:'not_eligible',reason:'local_unavailable'};}
      op.check();
      if(clip?.id!==recordingId||clip.registered_user_id!==op.identity.registered_user_id||
        clip.workspace!==op.identity.workspace_id||clip.privacy_revision!==op.identity.privacy_revision||
        !(clip.blob instanceof Blob)||!clip.blob.size||!clip.blob.type.toLowerCase().startsWith('audio/'))
        return {status:'not_eligible',reason:'owned_original_missing'};
      if(requestId==null||requestId==='')return {status:'local_unsent'};
      // 与真实 bind HTTP 入口同一格式；真实父回合存在性仍必须由服务器核验。
      if(typeof requestId!=='string'||!(/^[A-Za-z0-9_-]{1,160}$/).test(requestId))
        return {status:'not_eligible',reason:'parent_request_invalid'};
      const client=mediaClient(),blob=clip.blob,mime=blob.type.split(';',1)[0].trim().toLowerCase();
      op.check();const bytes=await blob.arrayBuffer();op.check();
      op.check();const digest=await crypto.subtle.digest('SHA-256',bytes);op.check();
      const sha=[...new Uint8Array(digest)].map(value=>value.toString(16).padStart(2,'0')).join('');
      const sameOriginal=media=>media.kind==='audio'&&media.mime_type===mime&&media.audio_format===null&&media.bytes===blob.size&&media.sha256===sha;
      let media=null;
      if(clip.media){
        try{const existing=client.validate(clip.media,{boundOnly:false});op.check();if(sameOriginal(existing))media=existing;}
        catch(error){op.check();}
      }
      async function optionalCache(next){
        op.check();try{await cache(next,op);}catch(error){op.check();}op.check();
      }
      if(!media){
        op.check();media=await client.upload(blob,{signal:op.signal});op.check();
        media=client.validate(media,{boundOnly:false});op.check();
        if(!sameOriginal(media))throw new Error('上传的录音与本机原件不一致。');
        // 先留存已成功上传的 descriptor；失败重试不会把原 bytes 或旧记录置空。
        op.check();await optionalCache({...clip,media});op.check();
      }
      if(media.storage_status!=='bound'||clip.bank_response_id!==requestId){
        op.check();const result=await client.bind(requestId,[media],{signal:op.signal});op.check();
        if(result?.request_id!==requestId||!Array.isArray(result.attachments))throw new Error('旧录音关联结果无法核验。');
        const matches=result.attachments.filter(item=>item?.media_id===media.media_id);
        if(matches.length!==1)throw new Error('旧录音关联结果无法核验。');
        media=client.validate(matches[0],{boundOnly:true});op.check();
        if(!sameOriginal(media))throw new Error('关联的录音与本机原件不一致。');
      }else{media=client.validate(media,{boundOnly:true});op.check();}
      op.check();const original=await client.read(media,{signal:op.signal});op.check();
      if(!(original instanceof Blob)||original.size!==blob.size||original.type!==media.mime_type)
        throw new Error('旧录音原件读回未能完整核验。');
      op.check();await optionalCache({...clip,media,bank_response_id:requestId});op.check();
      return {status:'migrated',media};
    }finally{op.close();}
  }
  function pauseOthers(audio){
    window.NavrenSpeech?.stop();
    if(activeSaved?.audio!==audio)activeSaved?.cancel();
    for(const record of mounts.values())if(record.audio!==audio)record.audio.pause();
  }
  function disposeRecord(record){
    if(!mounts.delete(record))return;
    record.attempt++;record.controller?.abort();record.audio.pause();record.audio.removeAttribute('src');record.audio.load?.();
    record.resource?.dispose();record.localURL&&URL.revokeObjectURL(record.localURL);
    if(record.host!==record.bubble)record.host.remove();
  }
  function dispose(message){for(const record of [...mounts])if(record.message===message)disposeRecord(record);}
  function failure(record,error){
    if(error?.name==='AbortError'||!record.current())return;
    let note=record.host.querySelector('.voice-media-error');
    if(!note){note=document.createElement('div');note.className='voice-media-error';record.host.append(note);}
    note.textContent=t('音频暂时无法回放。');
  }
  function player(message,{clip=null,media=null,resource=null,text='',assistant=false,op}){
    op.check();if(!message?.isConnected){resource?.dispose();throw cancelled();}
    const bubble=message.querySelector('.bubble');if(!bubble){resource?.dispose();throw cancelled();}
    if(media){for(const old of [...mounts])if(old.message===message&&old.mediaId===media.media_id)disposeRecord(old);}
    else dispose(message);
    let host=bubble;
    if(media||assistant){host=document.createElement('div');host.className='voice-message';host.dataset.mediaId=media?.media_id||'';bubble.append(host);}
    else{bubble.textContent='';bubble.classList.add('voice-message');}
    const duration=clip?.duration??(Number.isFinite(media?.duration_ms)?media.duration_ms/1000:0);
    host.dataset.voiceSeconds=duration>0?String(Math.max(1,Math.round(duration))):'';host.dataset.voiceAssistant=String(assistant);
    const title=document.createElement('div');title.className='voice-message-title';
    const audio=document.createElement('audio');audio.controls=true;audio.preload='metadata';
    const record={message,bubble,host,audio,resource,mediaId:media?.media_id||null,localURL:null,attempt:0,controller:null,playingGate:false,
      current:()=>op.current()&&message.isConnected&&mounts.has(record)};
    mounts.add(record);
    if(resource)audio.src=resource.url;
    else{record.localURL=URL.createObjectURL(clip.blob);audio.src=record.localURL;}
    host.append(title,audio);
    audio.addEventListener('loadedmetadata',()=>{if(record.current()&&Number.isFinite(audio.duration)&&audio.duration>0){host.dataset.voiceSeconds=String(Math.max(1,Math.round(audio.duration)));labels(host);}});
    if(!assistant){
      const toggle=document.createElement('button');toggle.type='button';toggle.className='voice-transcript-toggle';toggle.setAttribute('aria-expanded','false');
      const transcript=document.createElement('div');transcript.className='voice-transcript';transcript.hidden=true;transcript.textContent=text||'正在识别文字…';transcript.setAttribute('translate','no');transcript.dataset.i18nSkip='';if(!text)transcript.dataset.voicePlaceholder='正在识别文字…';
      toggle.onclick=()=>{transcript.hidden=!transcript.hidden;labels(host);toggle.setAttribute('aria-expanded',String(!transcript.hidden));};host.append(toggle,transcript);
    }
    audio.addEventListener('play',()=>{
      if(!record.current()){disposeRecord(record);return;}
      if(!media||record.playingGate){pauseOthers(audio);return;}
      // Blob URL 不是授权：每次开始/恢复播放前重新受控读取。
      audio.pause();const attempt=++record.attempt;record.controller?.abort();
      const controller=new AbortController();record.controller=controller;pending.add(controller);
      (async()=>{
        let next;
        try{
          next=await mediaClient().open(media,{signal:controller.signal});
          if(controller.signal.aborted||attempt!==record.attempt||!record.current())throw cancelled();
          record.resource?.dispose();record.resource=next;next=null;audio.src=record.resource.url;
          record.playingGate=true;pauseOthers(audio);await audio.play();
          if(!record.current()){audio.pause();throw cancelled();}
        }catch(error){audio.pause();record.resource?.dispose();record.resource=null;audio.removeAttribute('src');audio.load?.();failure(record,error);}
        finally{record.playingGate=false;next?.dispose();pending.delete(controller);if(record.controller===controller)record.controller=null;}
      })();
    });
    audio.addEventListener('timeupdate',()=>{if(!record.current())disposeRecord(record);});
    labels(host);return record;
  }
  function mount(message,clip,text=''){
    const op=operation();
    try{
      if(!(clip?.blob instanceof Blob)||!clip.blob.size||clip.registered_user_id!==op.identity.registered_user_id||clip.workspace!==op.identity.workspace_id||clip.privacy_revision!==op.identity.privacy_revision)throw cancelled();
      // 当次未 bound 的原件只作本地回放，不把 stored descriptor 冒充已归档。
      player(message,{clip,text,op});
    }finally{op.close();}
  }
  function update(message,text){const el=message?.querySelector('.voice-transcript');if(el){if(text){delete el.dataset.voicePlaceholder;el.textContent=text;}else{el.dataset.voicePlaceholder='没有识别到文字，你仍可以回放音频或重新录音。';el.textContent=t(el.dataset.voicePlaceholder);}}}
  async function restoreMedia(message,descriptor,text='',{assistant=false}={}){
    const op=operation();let resource;
    try{
      const client=mediaClient();descriptor=client.validate(descriptor,{boundOnly:true});if(descriptor.kind!=='audio')throw new Error('媒体不是音频。');
      resource=await client.open(descriptor,{signal:op.signal});op.check();
      for(const note of message.querySelectorAll('.voice-media-error'))if(note.dataset.mediaId===descriptor.media_id)note.remove();
      player(message,{media:descriptor,resource,text,assistant,op});resource=null;
      if(!assistant&&!text)update(message,'');
      return true;
    }catch(error){
      if(op.current()&&message?.isConnected){
        const bubble=message.querySelector('.bubble');
        let note=[...(bubble?.querySelectorAll('.voice-media-error')||[])].find(item=>item.dataset.mediaId===descriptor.media_id);
        if(!note){note=document.createElement('div');note.className='voice-media-error';note.dataset.mediaId=descriptor.media_id;bubble?.append(note);}
        note.textContent=t(assistant?'已保存的朗读暂时无法回放。':'语音暂时无法读取。');
      }
      if(error.name==='AbortError')throw error;
      return false;
    }finally{resource?.dispose();op.close();}
  }
  async function restore(message,entry){
    if(entry?.media)return restoreMedia(message,entry.media,entry.content||'');
    const op=operation();
    try{const clip=await get(entry.recording_id);op.check();if(!message.isConnected)return false;if(clip){mount(message,clip,entry.content);return true;}message.querySelector('.bubble').textContent=t('语音文件在此浏览器中不可用。')+(entry.content||'');return false;}
    catch(error){if(op.current()&&message.isConnected)message.querySelector('.bubble').textContent=t('语音暂时无法读取。')+(entry.content||'');return false;}
    finally{op.close();}
  }
  function stopSaved(){activeSaved?.cancel();}
  function playSaved(descriptor,{onState,onEnd}={}){
    stopSaved();window.NavrenSpeech?.stop();
    for(const record of mounts.values())record.audio.pause();
    return new Promise((resolve,reject)=>{
      let op;try{op=operation();descriptor=mediaClient().validate(descriptor,{boundOnly:true});if(descriptor.kind!=='audio')throw new Error('媒体不是音频。');}catch(error){op?.close();reject(error);return;}
      const audio=document.createElement('audio');let resource,done=false;
      const finish=error=>{
        if(done)return;done=true;audio.pause();audio.removeAttribute('src');audio.load?.();resource?.dispose();
        op.signal.removeEventListener('abort',abort);op.close();if(activeSaved?.audio===audio)activeSaved=null;
        if(error)reject(error);else{try{onEnd?.();resolve();}catch(callbackError){reject(callbackError);}}
      };
      const abort=()=>finish(cancelled());
      activeSaved={audio,cancel:()=>{op.cancel();finish(cancelled());}};op.signal.addEventListener('abort',abort,{once:true});
      audio.onended=()=>finish(op.current()?null:cancelled());audio.onerror=()=>finish(new Error('已保存的朗读暂时无法回放。'));
      audio.ontimeupdate=()=>{if(!op.current())finish(cancelled());};
      audio.onplay=()=>{if(!op.current())finish(cancelled());else try{onState?.('speaking');}catch(error){finish(error);}};
      (async()=>{
        let opened;
        try{
          onState?.('preparing');opened=await mediaClient().open(descriptor,{signal:op.signal});op.check();
          if(done)throw cancelled();resource=opened;opened=null;audio.src=resource.url;op.check();await audio.play();op.check();
        }catch(error){finish(error);}finally{opened?.dispose();}
      })();
    });
  }
  function clear(){
    epoch++;activeSaved?.cancel();activeSaved=null;
    for(const controller of pending)controller.abort();pending.clear();
    for(const record of [...mounts])disposeRecord(record);
    window.NavrenChatMedia?.clear();
  }
  for(const type of ['pagehide','navren:privacy-reset','navren:identity-revoked','navren:account-change','navren:account-switched'])window.addEventListener(type,clear);
  window.addEventListener('navren:language',()=>document.querySelectorAll('.voice-message').forEach(labels));
  return {save,get,migrateOwned,mount,update,restore,restoreMedia,playSaved,stopSaved,clear,dispose};
})();
