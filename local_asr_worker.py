"""受宿主限时管理的本地 Whisper 子进程；PCM 只经 stdin 留在内存。"""
from pathlib import Path
import hashlib
import json
import os
import resource
import sys

MAX_AUDIO_BYTES = 16000 * 2 * 120
ALLOWED_MODELS = {
    'd5a3b8671ac7aab11a2c9d0f16e7da94bad5500d785856f438c6bd44c3723944':
        (74418182, 'aaff20ce8f69beddee3fe0cc1e08f4e92f58586cb9f12ba00a6f73cbfec1cb1c'),
    '55b6674c9b339702d486e2b1573839a66f8ec8f821ed2886993ef717a86b09f5':
        (481307592, 'e8f58e638208af66d5d5d67801259dc7a12d199e971967a9f9d33a8e3635668e'),
}


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    if len(sys.argv) != 2 or os.environ.get('HF_HUB_OFFLINE') != '1':
        raise ValueError('LOCAL_ASR_CONFIGURATION_INVALID')
    model = Path(sys.argv[1]).resolve(strict=True)
    if not model.is_dir() or (model/'weights.safetensors').exists():
        raise ValueError('LOCAL_ASR_MODEL_NOT_APPROVED')
    weight = model/'weights.npz'
    config = model/'config.json'
    digest = _sha256_file(weight)
    allowed = ALLOWED_MODELS.get(digest)
    if not allowed or weight.stat().st_size != allowed[0] or _sha256_file(config) != allowed[1]:
        raise ValueError('LOCAL_ASR_MODEL_NOT_APPROVED')
    raw = sys.stdin.buffer.read(MAX_AUDIO_BYTES + 1)
    if not raw or len(raw) > MAX_AUDIO_BYTES or len(raw) % 2:
        raise ValueError('INVALID_AUDIO')
    # 不允许 Python 依赖退回网络或另启下载/解码程序。音频直接作为16k浮点数组传入。
    def offline(event, args):
        if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                     'socket.bind', 'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.posix_spawnp'):
            raise RuntimeError('LOCAL_ASR_OFFLINE_ONLY')
    sys.addaudithook(offline)
    resource.setrlimit(resource.RLIMIT_CPU, (12, 13))
    import numpy as np
    import mlx.core as mx
    import mlx_whisper
    mx.set_cache_limit(16 * 1024 * 1024)
    mx.set_memory_limit(1536 * 1024 * 1024)
    pcm = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
    result = mlx_whisper.transcribe(pcm, path_or_hf_repo=str(model), language='zh',
        temperature=0, condition_on_previous_text=False, word_timestamps=False, verbose=None)
    text = result.get('text', '').strip()
    if not text or len(text) > 8000:
        raise ValueError('LOCAL_ASR_NO_TEXT')
    # 不修词、不改变金额或简繁字形；返回实际模型文字供原聊天链路消费。
    sys.stdout.write(json.dumps({'text': text, 'language': result.get('language')}, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # 不将可能含音频、转写或本机路径的第三方异常输出到HTTP/日志。
        sys.stderr.write('LOCAL_ASR_FAILED\n')
        raise SystemExit(1)
