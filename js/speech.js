/** 百炼语音共享控制器：实时 PCM 录音、实时 PCM 播放、取消和资源回收。 */
window.NavrenSpeech = (() => {
  let active = null;
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;

  function abortError() { return new DOMException('语音操作已取消', 'AbortError'); }
  function stop() {
    if (active) active.cancel();
  }
  function finishRecording() {
    if (active?.kind === 'asr') {
      if (!active.ready) active.cancel();
      else if (!active.finished) active.finish();
    }
  }

  function run(kind, options = {}) {
    stop();
    const identity=BankClient.identity(),guard=BankClient.guard();
    if(!identity||!guard.valid(identity))return Promise.reject(abortError());
    return new Promise((resolve, reject) => {
      const op = { kind, ended: false, finished: false, ready: false, asrReady:false, serverDone: false,
        socket: null, stream: null, capture: null, context: null, sources: new Set(),
        controller: new AbortController(), networkController:new AbortController(), sentences: new Map(), nextTime: 0, timers: [], tail: null, audioBytes: 0,
        capturedBytes:0, flushing:false, input:null, silence:null, localOnly:false, discarded:false, settling:false,
        recorder:null, recorded:[], recordingReady:Promise.resolve(), recordingStart:0, recordingTimer:null, rejectRecording:null, savedMedia:null };
      active = op;
      const current=()=>active===op&&!op.ended;
      const alive=()=>{if(!current())return false;if(guard.valid(identity))return true;op.cancel?.();return false;};
      const state = value => { if (alive()) options.onState?.(value); };
      const transcript=()=>[...op.sentences.entries()].sort((a,b)=>Number(a[0])-Number(b[0])).map(([,value])=>value.text).join('');
      function disconnectCapture(){if(op.capture){op.capture.port.onmessage=null;op.capture.disconnect();}}
      function closeSocket(){if(op.socket){op.socket.onclose=op.socket.onerror=op.socket.onmessage=op.socket.onopen=null;op.socket.close();}}
      function release() {
        op.controller.abort();op.networkController.abort();
        op.timers.forEach(clearTimeout);clearTimeout(op.recordingTimer);
        op.stream?.getTracks().forEach(track => track.stop());
        if(op.recorder){op.recorder.ondataavailable=op.recorder.onstop=op.recorder.onerror=null;if(op.recorder.state==='recording')op.recorder.stop();}
        if(op.discarded)op.rejectRecording?.(abortError());
        disconnectCapture();
        op.sources.forEach(source => { source.onended = null; try { source.stop(); } catch (_) {} });
        op.sources.clear();closeSocket();
        if (op.context && op.context.state !== 'closed') op.context.close().catch(() => {});
        if (active === op) active = null;
      }
      function complete(error,discard=false){
        if(!current())return;
        if(!guard.valid(identity)){error=abortError();discard=true;}
        const text=transcript();op.discarded=discard;op.ended=true;release();
        if(error)reject(error);else resolve(text);
      }
      function disableASR(error){
        if(!alive())return;
        if(op.localOnly){if(op.finished)settle();return;}
        op.localOnly=true;op.asrReady=false;op.sentences.clear();
        op.networkController.abort();op.timers.forEach(clearTimeout);disconnectCapture();closeSocket();
        options.onTranscript?.('',false);
        // 服务错误与显式取消分开：继续录原件，不把最后一个 MediaRecorder 分片丢掉。
        if(error)options.onRecognitionError?.(error);
        if(op.finished)settle();else state('listening');
      }
      function settle(error) {
        if(!current()||op.settling)return;
        if(error?.name==='AbortError'||!guard.valid(identity)){complete(abortError(),true);return;}
        if(error&&kind==='asr'&&op.recorder){disableASR(error);return;}
        op.settling=true;
        if(kind==='asr'&&op.recorder){
          op.finished=true;
          if(op.recorder.state==='recording')op.recorder.stop();
          op.stream?.getTracks().forEach(track=>track.stop());disconnectCapture();
          op.recordingReady.then(()=>{if(alive())complete();},failure=>complete(failure,true));
        }else complete(error);
      }
      op.cancel = () => {
        if(op.socket?.readyState===WebSocket.OPEN)try{op.socket.send(JSON.stringify({type:'cancel'}));}catch(_){}
        complete(abortError(),true);
      };
      function startOriginalRecording(){
        if(!options.onRecording)return;
        if(typeof MediaRecorder==='undefined')throw new Error('当前浏览器不支持语音消息录制，请使用新版 Chrome。');
        const mime=['audio/webm;codecs=opus','audio/mp4','audio/webm'].find(m=>MediaRecorder.isTypeSupported(m));
        op.recorder=new MediaRecorder(op.stream,mime?{mimeType:mime}:{});
        op.recordingReady=new Promise((resolveRecording,rejectRecording)=>{
          op.rejectRecording=rejectRecording;
          op.recorder.ondataavailable=e=>{if(!op.discarded&&e.data.size)op.recorded.push(e.data);};
          op.recorder.onerror=()=>{const error=new Error('录音文件未能完成，请重试。');rejectRecording(error);complete(error,true);};
          op.recorder.onstop=()=>{
            if(op.discarded||!alive()){rejectRecording(abortError());return;}
            const blob=new Blob(op.recorded,{type:op.recorder.mimeType});
            if(!blob.size){rejectRecording(new Error('录音文件为空，请重试。'));return;}
            let saved;
            try{saved=options.onRecording({blob,duration:(performance.now()-op.recordingStart)/1000,signal:op.controller.signal});}
            catch(error){rejectRecording(error);return;}
            Promise.resolve(saved).then(()=>alive()?resolveRecording():rejectRecording(abortError()),rejectRecording);
          };
        });
        op.recordingReady.catch(()=>{});op.recordingStart=performance.now();op.recorder.start(250);
      }
      function finishUpload() {
        if(!alive()||!op.flushing)return;
        op.flushing=false;
        if(op.recorder?.state==='recording')op.recorder.stop();
        op.stream?.getTracks().forEach(track=>track.stop());disconnectCapture();
        if(op.localOnly||!op.asrReady||!op.capturedBytes||op.socket?.readyState!==WebSocket.OPEN){
          if(op.recorder){op.localOnly=true;op.sentences.clear();op.networkController.abort();closeSocket();settle();}
          else settle(new Error('麦克风没有传入音频，请检查系统录音权限和输入设备。'));
          return;
        }
        op.socket.send(JSON.stringify({type:'finish'}));
        op.timers.push(setTimeout(()=>settle(new Error('语音转写超时，已保留原录音。')),16000));
      }
      op.finish = () => {
        if(!alive()||op.finished||!op.ready||kind!=='asr')return;
        op.finished=true;op.flushing=true;state('finishing');
        if(op.localOnly||!op.asrReady){finishUpload();return;}
        op.capture?.port.postMessage({type:'flush'});op.timers.push(setTimeout(finishUpload,250));
      };
      function maybeDone() {
        if(op.serverDone&&kind==='asr'&&!op.finished){disableASR();return;}
        if(op.serverDone&&(!op.sources.size||kind==='asr'))settle();
      }
      function playPCM(data) {
        if (!alive()) return;
        op.audioBytes += data.byteLength;
        let bytes = new Uint8Array(data);
        if (op.tail !== null) {
          const joined = new Uint8Array(bytes.length + 1);
          joined[0] = op.tail; joined.set(bytes, 1); bytes = joined; op.tail = null;
        }
        if (bytes.length % 2) { op.tail = bytes[bytes.length - 1]; bytes = bytes.subarray(0, bytes.length - 1); }
        if (!bytes.length) return;
        const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
        const buffer = op.context.createBuffer(1, bytes.length / 2, 24000);
        const samples = buffer.getChannelData(0);
        for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
        const source = op.context.createBufferSource();
        source.buffer = buffer; source.connect(op.context.destination);
        op.sources.add(source);
        source.onended = () => { op.sources.delete(source); source.disconnect(); maybeDone(); };
        op.nextTime = Math.max(op.nextTime, op.context.currentTime + 0.04);
        source.start(op.nextTime); op.nextTime += buffer.duration;
        state('speaking');
      }
      (async () => {
        try {
          state('connecting');
          // MediaRecorder 原件不依赖 ASR 状态、AudioWorklet 或 WS ready。
          if(AudioContextClass){
            try{op.context=new AudioContextClass();await op.context.resume();if(!alive())throw abortError();}
            catch(error){if(kind!=='asr'||error.name==='AbortError')throw error;op.context?.close().catch(()=>{});op.context=null;}
          }
          if(kind==='asr'){
            if(!navigator.mediaDevices?.getUserMedia)throw new Error('录音需要安全连接或本机地址。');
            const stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
            if(!alive()){stream.getTracks().forEach(track=>track.stop());throw abortError();}
            op.stream=stream;startOriginalRecording();op.ready=true;state('listening');
            op.recordingTimer=setTimeout(op.finish,60000);
            const statusTimer=setTimeout(()=>settle(new Error('语音状态响应超时，仍可保存原录音。')),15000);op.timers.push(statusTimer);
            const statusResponse=await fetch('/api/speech/status',{credentials:'same-origin',signal:op.networkController.signal});
            const status=await statusResponse.json();clearTimeout(statusTimer);if(!alive())throw abortError();
            if(op.finished||op.localOnly)return;
            if(!statusResponse.ok||status.asr_available===false){
              if(op.recorder){disableASR(new Error('语音识别暂不可用，仍可保存原录音。'));return;}
              throw new Error('语音识别暂不可用。');
            }
            if(!op.context?.audioWorklet||op.context.state!=='running'){
              if(op.recorder){disableASR(new Error('当前浏览器不能识别语音，仍可保存原录音。'));return;}
              throw new Error('当前浏览器不支持语音处理。');
            }
            const revision=document.querySelector('meta[name="navren-release"]')?.content||'';
            await op.context.audioWorklet.addModule('/js/pcm_worklet.js?v='+encodeURIComponent(revision));
          }else if(!op.context||op.context.state!=='running')throw new Error('请先点击页面，再开始语音。');
          if(!alive())throw abortError();if(op.finished||op.localOnly)return;
          const headers=await BankClient.agentHeaders();
          if(!alive())throw abortError();if(op.finished||op.localOnly)return;
          const sessionTimer = setTimeout(() => settle(new Error('语音服务响应超时，请重试。')), 15000);
          op.timers.push(sessionTimer);
          const sessionPayload = kind === 'tts'
            ? { kind, speech_receipt: options.speechReceipt || '' } : { kind };
          if (kind === 'tts' && options.purpose === 'voice_preview') sessionPayload.purpose = 'voice_preview';
          const response = await fetch('/api/speech/session', { method: 'POST', credentials: 'same-origin',
            headers, body: JSON.stringify(sessionPayload), signal: op.networkController.signal });
          const session = await response.json();
          clearTimeout(sessionTimer);
          if (!response.ok) throw new Error(session.error?.message || '语音服务无法启动。');
          if (!alive()) throw abortError();
          const url = new URL(session.path, location.href); url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
          op.socket = new WebSocket(url.href); op.socket.binaryType = 'arraybuffer';
          const startupTimer = setTimeout(() => settle(new Error('语音连接超时，请检查网络后重试。')), 15000);
          op.timers.push(startupTimer);
          op.socket.onopen = () => {
            if (alive()) op.socket.send(JSON.stringify({ type: 'start', text: options.text,
              voice: options.voice || 'female_sweet', rate: options.rate ?? 1.0 }));
          };
          op.socket.onerror = () => settle(new Error('语音连接失败，请检查服务后重试。'));
          op.socket.onclose = () => {
            if (!op.serverDone) settle(new Error('语音连接中断，请重试。'));
          };
          op.socket.onmessage = event => {
            if (!alive()) return;
            try {
              if (event.data instanceof ArrayBuffer) { playPCM(event.data); return; }
              const message = JSON.parse(event.data);
              if (message.type === 'ready') {
                clearTimeout(startupTimer); op.ready = true;
                if(kind==='asr'&&(op.finished||op.localOnly))return;
                if (kind === 'asr') {
                  op.input = op.context.createMediaStreamSource(op.stream);
                  op.capture = new AudioWorkletNode(op.context, 'navren-pcm');
                  op.silence = op.context.createGain(); op.silence.gain.value = 0;
                  op.capture.port.onmessage = packet => {
                    if (!alive()) return;
                    if (packet.data?.type==='flushed') { finishUpload();return; }
                    if (!(packet.data instanceof ArrayBuffer) || (op.finished && !op.flushing)) return;
                    if (op.socket.bufferedAmount > 64000) { settle(new Error('录音上传过慢，请检查网络后重试。')); return; }
                    op.capturedBytes+=packet.data.byteLength;
                    op.socket.send(packet.data);
                  };
                  op.input.connect(op.capture); op.capture.connect(op.silence); op.silence.connect(op.context.destination);
                  op.asrReady=true;
                  state('listening');
                  op.timers.push(setTimeout(()=>{if(alive()&&!op.capturedBytes)settle(new Error('录音设备没有输出音频，请检查麦克风权限和系统输入设备。'));},4000));
                } else {
                  state('generating');
                  op.timers.push(setTimeout(() => {
                    if (!op.serverDone) settle(new Error('语音合成超时，请分段朗读。'));
                  }, 120000));
                }
              } else if (message.type === 'transcript') {
                const previous = op.sentences.get(message.sentence_id);
                if (!previous?.final || message.final) op.sentences.set(message.sentence_id, { text: message.text, final: message.final });
                const text = [...op.sentences.entries()].sort((a, b) => Number(a[0]) - Number(b[0]))
                  .map(([, sentence]) => sentence.text).join('');
                options.onTranscript?.(text, message.final, message.name_review);
                if (message.final && message.text && options.finishOnSentence) op.finish();
              } else if (message.type === 'done') {
                op.serverDone = true;
                if (kind === 'tts' && !op.audioBytes) throw new Error('语音服务未返回音频，请重试。');
                if (op.tail !== null) throw new Error('返回的音频数据不完整，请重试。');
                if(kind==='tts'&&message.media&&!op.savedMedia){
                  const media=window.NavrenChatMedia?.validate(message.media,{boundOnly:true});
                  if(!media||media.kind!=='audio')throw new Error('已生成的朗读原件无法核验。');
                  if(!alive())return;op.savedMedia=media;options.onSavedMedia?.(media);
                }
                maybeDone();
              } else if (message.type === 'error') {
                settle(new Error(message.message || '语音服务暂不可用。'));
              }
            } catch (error) { settle(error); }
          };
        } catch (error) {
          if (error.name === 'NotAllowedError') error = new Error('麦克风权限未开启，请在地址栏的权限设置中允许录音。');
          if(kind==='asr'&&op.recorder&&alive()&&!op.finished){disableASR(error);}
          else if(kind==='asr'&&op.localOnly){if(op.finished)settle();}
          else settle(error);
        }
      })();
    });
  }
  for(const type of ['pagehide','navren:privacy-reset','navren:identity-revoked','navren:account-change','navren:account-switched'])window.addEventListener(type,stop);
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
  return { startASR: options => run('asr', options), speak: (text, options) => run('tts', { ...options, text }),
    stop, finishRecording, get activeKind() { return active?.kind || null; } };
})();
