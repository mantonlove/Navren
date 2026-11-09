"""Fixed local SenseVoice candidate worker. PCM is stdin-only and never written."""
from pathlib import Path
import hashlib,json,os,resource,sys,time

MAX_AUDIO_BYTES = 16000 * 2 * 120
WEIGHT_BYTES = 239233841
WEIGHT_SHA256 = 'c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51'
TOKENS_BYTES = 315894
TOKENS_SHA256 = 'f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc'

def digest(path):
    h=hashlib.sha256()
    with path.open('rb')as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def main():
    started=time.monotonic();stage={}
    if len(sys.argv)!=2 or os.environ.get('HF_HUB_OFFLINE')!='1':
        raise ValueError('LOCAL_ASR_CONFIGURATION_INVALID')
    model=Path(sys.argv[1]).resolve(strict=True)
    weight,tokens=model/'model.int8.onnx',model/'tokens.txt'
    for p,n,h in [(weight,WEIGHT_BYTES,WEIGHT_SHA256),(tokens,TOKENS_BYTES,TOKENS_SHA256)]:
        if not p.is_file()or p.is_symlink()or p.stat().st_size!=n or digest(p)!=h:
            raise ValueError('LOCAL_ASR_MODEL_NOT_APPROVED')
    stage['fixed_assets_seconds']=time.monotonic()-started
    mark=time.monotonic()
    raw=sys.stdin.buffer.read(MAX_AUDIO_BYTES+1)
    if not raw or len(raw)>MAX_AUDIO_BYTES or len(raw)%2:
        raise ValueError('INVALID_AUDIO')
    stage['stdin_seconds']=time.monotonic()-mark
    # Retain the existing Python audit guard. Native-network isolation is not claimed.
    def offline(event,args):
        if event in ('socket.connect','socket.connect_ex','socket.getaddrinfo','socket.bind',
                     'subprocess.Popen','os.system','os.posix_spawn','os.posix_spawnp'):
            raise RuntimeError('LOCAL_ASR_OFFLINE_ONLY')
    sys.addaudithook(offline)
    resource.setrlimit(resource.RLIMIT_CPU,(12,13))
    mark=time.monotonic()
    import numpy as np
    import sherpa_onnx
    stage['imports_seconds']=time.monotonic()-mark
    pcm=np.frombuffer(raw,dtype='<i2').astype(np.float32)/32768.0
    mark=time.monotonic()
    recognizer=sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(weight),tokens=str(tokens),num_threads=1,sample_rate=16000,
        feature_dim=80,decoding_method='greedy_search',debug=False,provider='cpu',
        language='zh',use_itn=False,rule_fsts='',rule_fars='',
        hr_dict_dir='',hr_rule_fsts='',hr_lexicon='')
    stage['recognizer_load_seconds']=time.monotonic()-mark
    mark=time.monotonic()
    stream=recognizer.create_stream()
    stream.accept_waveform(16000,pcm)
    recognizer.decode_stream(stream)
    stage['decode_seconds']=time.monotonic()-mark
    text=stream.result.text.strip()
    if not text or len(text)>8000:raise ValueError('LOCAL_ASR_NO_TEXT')
    usage=resource.getrusage(resource.RUSAGE_SELF)
    sys.stdout.write(json.dumps({'text':text,'language':'zh',
        'candidate_diagnostic':{'stage_seconds':stage,'total_seconds':time.monotonic()-started,
        'cpu_user_seconds':usage.ru_utime,'cpu_system_seconds':usage.ru_stime,
        'macOS_ru_maxrss_bytes':usage.ru_maxrss}},ensure_ascii=False))
    sys.stdout.flush()

if __name__=='__main__':
    try:main()
    except Exception:
        sys.stderr.write('LOCAL_ASR_FAILED\n')
        raise SystemExit(1)
