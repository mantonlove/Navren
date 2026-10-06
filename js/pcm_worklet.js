/* 连续重采样为单声道 16kHz / PCM16；每包 100ms，无录音文件落盘。 */
class NavrenPCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.phase = this.sum = this.count = this.offset = 0;
    this.buffer = new ArrayBuffer(3200);
    this.view = new DataView(this.buffer);
    this.flushed=false;
    this.port.onmessage=event=>{
      if(event.data?.type==='flush') {
        this.flushed=true;
        if(this.offset){const tail=this.buffer.slice(0,this.offset*2);this.port.postMessage(tail,[tail]);}
        this.port.postMessage({type:'flushed'});
      }
    };
  }
  process(inputs) {
    if(this.flushed)return true;
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    for (const value of channel) {
      this.sum += value;
      this.count++;
      this.phase += 16000;
      if (this.phase >= sampleRate) {
        this.phase -= sampleRate;
        const sample = Math.max(-1, Math.min(1, this.sum / this.count));
        this.view.setInt16(this.offset * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true);
        this.sum = this.count = 0;
        if (++this.offset === 1600) {
          this.port.postMessage(this.buffer, [this.buffer]);
          this.buffer = new ArrayBuffer(3200);
          this.view = new DataView(this.buffer);
          this.offset = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('navren-pcm', NavrenPCM);
