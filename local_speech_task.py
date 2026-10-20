"""VoiceGateway 的可选本地 ASR task_factory 适配；默认关闭，不读云密钥。"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import uuid
import math
import shutil
import tempfile
import wave

from simulated_bank import BankError

# 同机最多一个识别子进程，避免8GiB演示机同时加载多个small模型。
_LOCAL_SLOT = threading.BoundedSemaphore(1)
_MODEL_PINS = {
    74418182: ('whisper-tiny', 'd5a3b8671ac7aab11a2c9d0f16e7da94bad5500d785856f438c6bd44c3723944',
               'aaff20ce8f69beddee3fe0cc1e08f4e92f58586cb9f12ba00a6f73cbfec1cb1c'),
    481307592: ('whisper-small-mlx', '55b6674c9b339702d486e2b1573839a66f8ec8f821ed2886993ef717a86b09f5',
               'e8f58e638208af66d5d5d67801259dc7a12d199e971967a9f9d33a8e3635668e'),
}


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class LocalASRConfig:
    python: str
    model_dir: str
    model_id: str
    worker: str
    error_message: str = ''

    @classmethod
    def from_environment(cls):
        if os.environ.get('NAVREN_LOCAL_ASR_ENABLED') != '1':
            return None
        try:
            return cls._validated_environment()
        except Exception:
            return cls('', '', 'local-asr-unavailable', '', '本地语音识别运行环境或模型校验未就绪，请使用文字输入。')

    @classmethod
    def _validated_environment(cls):
        python = Path(os.environ.get('NAVREN_LOCAL_ASR_PYTHON', ''))
        model = Path(os.environ.get('NAVREN_LOCAL_ASR_MODEL_DIR', ''))
        worker = Path(__file__).with_name('local_asr_worker.py')
        if not python.is_absolute() or not model.is_absolute() or not os.access(python, os.X_OK) or not worker.is_file():
            raise BankError('LOCAL_ASR_NOT_CONFIGURED', '本地语音识别运行环境尚未配置。', 503)
        runtime = python.parent.parent
        sites = list((runtime/'lib').glob('python*/site-packages'))
        modules = ('mlx', 'mlx_whisper', 'numpy', 'numba', 'scipy', 'tiktoken', 'huggingface_hub', 'tqdm')
        if not (runtime/'pyvenv.cfg').is_file() or not any(all((site/name).is_dir() for name in modules) for site in sites):
            raise ValueError('LOCAL_ASR_RUNTIME_MISSING')
        model = model.resolve(strict=True)
        weight, config = model/'weights.npz', model/'config.json'
        if not weight.is_file() or not config.is_file() or (model/'weights.safetensors').exists():
            raise BankError('LOCAL_ASR_NOT_CONFIGURED', '本地语音识别模型不受支持。', 503)
        pin = _MODEL_PINS.get(weight.stat().st_size)
        if not pin or _sha256_file(weight) != pin[1] or _sha256_file(config) != pin[2]:
            raise BankError('LOCAL_ASR_NOT_CONFIGURED', '本地语音识别模型校验未通过。', 503)
        return cls(str(python), str(model), pin[0], str(worker))

    def ready(self):
        try:
            return not self.error_message and os.access(self.python, os.X_OK) and Path(self.worker).is_file() and Path(self.model_dir).is_dir()
        except OSError:
            return False


class LocalSpeechTask:
    """只接受16k单声道PCM16；停止录音后识别，不伪称逐帧流式转写。"""
    local_only = True
    MAX_AUDIO_BYTES = 16000 * 2 * 120
    # 原后端finish后15s、前端16s保持；12s超时+两段0.5s回收，小于14s。
    WORKER_SECONDS = 12

    def __init__(self, kind, options, key, url, *, config):
        if kind != 'asr' or not config.ready():
            raise BankError('LOCAL_ASR_NOT_CONFIGURED', '本地语音识别暂不可用。', 503)
        self.kind, self.id, self.config = kind, str(uuid.uuid4()), config
        self.done = False
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._events = queue.Queue()
        self._audio = bytearray()
        self._process = None
        self._runner = None
        self.native_exit_code = None

    def audio(self, data):
        with self._lock:
            if self.done or self._closed.is_set() or not isinstance(data, bytes) or not data or len(data) > 65536 or len(data) % 2:
                raise BankError('INVALID_AUDIO', '录音数据无效。')
            if len(self._audio) + len(data) > self.MAX_AUDIO_BYTES:
                raise BankError('SPEECH_TIMEOUT', '录音达到两分钟上限，请分段说。', 408)
            self._audio.extend(data)

    def finish(self):
        with self._lock:
            if self.done:
                return
            if self._closed.is_set() or not self._audio:
                raise BankError('MICROPHONE_NO_AUDIO', '没有收到麦克风音频，请检查输入设备与系统录音权限。')
            if not _LOCAL_SLOT.acquire(blocking=False):
                raise BankError('SPEECH_BUSY', '本地语音识别正在处理另一段录音，请稍后重试。', 429)
            self.done = True
            self._runner = threading.Thread(target=self._run, name='navren-local-asr', daemon=True)
            try:
                self._runner.start()
            except Exception:
                self.done = False
                _LOCAL_SLOT.release()
                raise BankError('LOCAL_ASR_FAILED', '本地语音识别无法启动，请使用文字输入。', 503)

    @staticmethod
    def _stop(process):
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=0.5)

    def _run(self):
        process = None
        try:
            env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'zh_CN.UTF-8',
                   'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
                   'PYTHONDONTWRITEBYTECODE': '1', 'NUMBA_DISABLE_JIT': '1'}
            process = subprocess.Popen([self.config.python, '-B', self.config.worker, self.config.model_dir],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, start_new_session=True)
            with self._lock:
                self._process = process
                raw = bytes(self._audio)
                self._audio.clear()
                cancelled = self._closed.is_set()
            if cancelled:
                return
            try:
                output, _ = process.communicate(input=raw, timeout=self.WORKER_SECONDS)
            except subprocess.TimeoutExpired:
                self._stop(process)
                raise BankError('SPEECH_TIMEOUT', '本地语音识别超时，请分段说或使用文字输入。', 408)
            finally:
                raw = None
            self.native_exit_code = process.returncode
            if self._closed.is_set():
                return
            if process.returncode != 0 or len(output) > 40000:
                raise BankError('LOCAL_ASR_FAILED', '本地语音识别失败，请重试或使用文字输入。', 503)
            result = json.loads(output)
            text = result.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 8000:
                raise BankError('LOCAL_ASR_NO_TEXT', '没有识别到文字，请重试或使用文字输入。')
            self._events.put({'header': {'event': 'result-generated', 'task_id': self.id},
                'payload': {'output': {'sentence': {'sentence_id': 0, 'text': text, 'sentence_end': True}}}})
            self._events.put({'header': {'event': 'task-finished', 'task_id': self.id}})
        except Exception as error:
            if not self._closed.is_set():
                self._events.put(error if isinstance(error, BankError) else BankError(
                    'LOCAL_ASR_FAILED', '本地语音识别暂不可用，请使用文字输入。', 503))
        finally:
            self._stop(process)
            if process is not None:
                self.native_exit_code = process.returncode
            if process is not None:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
            with self._lock:
                self._audio.clear()
            _LOCAL_SLOT.release()

    def receive(self):
        if self._closed.is_set():
            raise BankError('SPEECH_DISCONNECTED', '语音会话已结束。', 502)
        try:
            event = self._events.get(timeout=1)
        except queue.Empty:
            raise TimeoutError()
        if isinstance(event, Exception):
            raise event
        return event

    def close(self):
        self._closed.set()
        with self._lock:
            process = self._process
            self._audio.clear()
        self._stop(process)
        if self._runner and self._runner is not threading.current_thread():
            self._runner.join(1.5)
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                break


_TTS_SLOT = threading.BoundedSemaphore(1)
_LOCAL_VOICES = {
    'female_sweet': 'Tingting',
    'male_warm': 'Eddy (中文（中国大陆）)',
    'male_podcast': 'Reed (中文（中国大陆）)',
}


@dataclass(frozen=True)
class LocalTTSConfig:
    tmp_root: str
    error_message: str = ''

    @classmethod
    def from_environment(cls):
        if os.environ.get('NAVREN_LOCAL_TTS_ENABLED') != '1':
            return None
        value = os.environ.get('NAVREN_LOCAL_TTS_TMP_ROOT', '')
        try:
            path = Path(value)
            if not path.is_absolute() or not path.is_dir() or (path.stat().st_mode & 0o777) != 0o700 or not os.access('/usr/bin/say', os.X_OK):
                raise ValueError('LOCAL_TTS_NOT_CONFIGURED')
            return cls(str(path.resolve()))
        except Exception:
            return cls('', '本地朗读运行目录或系统语音未就绪，暂不能播报。')

    def ready(self):
        try:
            root = Path(self.tmp_root)
            return not self.error_message and root.is_dir() and (root.stat().st_mode & 0o777) == 0o700 and os.access('/usr/bin/say', os.X_OK)
        except OSError:
            return False

    @property
    def voices(self):
        return dict(_LOCAL_VOICES) if self.ready() else {}


class LocalTTSTask:
    """仅合成后端获准文字；派生WAVE在0600临时文件生成，读后立即回收。"""
    local_only = True
    MAX_PCM_BYTES = 24000 * 2 * 120

    def __init__(self, kind, options, key, url, *, config):
        from voice_gateway import speech_text
        if kind != 'tts' or not config.ready():
            raise BankError('LOCAL_TTS_NOT_CONFIGURED', '本地朗读暂不可用。', 503)
        voice = config.voices.get(options.get('voice', 'female_sweet'))
        if not voice:
            raise BankError('INVALID_VOICE', '请选择当前本地服务支持的音色。')
        try:
            rate = float(options.get('rate', 1.0))
        except (TypeError, ValueError):
            rate = float('nan')
        if not math.isfinite(rate) or not 0.5 <= rate <= 2:
            raise BankError('INVALID_RATE', '语速应在0.5至2倍之间。')
        self.id, self.kind, self.done = str(uuid.uuid4()), kind, True
        self._text = speech_text(options.get('text'))
        self._config, self._voice, self._rate = config, voice, round(175 * rate)
        self._events = queue.Queue()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._process = None
        self.native_exit_code = None
        self.temporary_directory = None
        if not _TTS_SLOT.acquire(blocking=False):
            raise BankError('SPEECH_BUSY', '本地朗读正在处理另一段文字，请稍后重试。', 429)
        self._runner = threading.Thread(target=self._run, name='navren-local-tts', daemon=True)
        try:
            self._runner.start()
        except Exception:
            _TTS_SLOT.release()
            raise BankError('LOCAL_TTS_FAILED', '本地朗读无法启动。', 503)

    def _run(self):
        process, root, output_path = None, None, None
        try:
            root = Path(tempfile.mkdtemp(prefix='navren-tts-', dir=self._config.tmp_root))
            os.chmod(root, 0o700)
            self.temporary_directory = str(root)
            fd, output = tempfile.mkstemp(prefix='speech-', suffix='.wav', dir=root)
            os.fchmod(fd, 0o600)
            os.close(fd)
            output_path = Path(output)
            process = subprocess.Popen(['/usr/bin/say', '-v', self._voice, '-r', str(self._rate),
                '--file-format=WAVE', '--data-format=LEI16@24000', '--channels=1', '-o', output, '-f', '-'],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'zh_CN.UTF-8'}, start_new_session=True)
            with self._lock:
                self._process = process
                text = self._text
                self._text = None
            if self._closed.is_set():
                return
            try:
                process.communicate(input=(text+'\n').encode('utf-8'), timeout=20)
            except subprocess.TimeoutExpired:
                raise BankError('SPEECH_TIMEOUT', '本地朗读生成超时，请稍后重试。', 408)
            finally:
                text = None
            self.native_exit_code = process.returncode
            if self._closed.is_set():
                return
            if process.returncode != 0 or output_path.stat().st_size > self.MAX_PCM_BYTES + 65536:
                raise BankError('LOCAL_TTS_FAILED', '本地朗读未能完整生成，请缩短文字后重试。', 503)
            with wave.open(str(output_path), 'rb') as stream:
                if (stream.getframerate(), stream.getnchannels(), stream.getsampwidth(), stream.getcomptype()) != (24000, 1, 2, 'NONE'):
                    raise BankError('LOCAL_TTS_FAILED', '本地朗读音频格式无效。', 503)
                pcm = stream.readframes(24000 * 120 + 1)
            if not pcm or len(pcm) > self.MAX_PCM_BYTES or len(pcm) % 2:
                raise BankError('LOCAL_TTS_FAILED', '本地朗读音频不完整。', 503)
            # 删除派生文件后才开始向原WS发布；金额及风险文字未做截断。
            output_path.unlink()
            output_path = None
            shutil.rmtree(root)
            root = None
            for offset in range(0, len(pcm), 4800):
                if self._closed.is_set():
                    return
                self._events.put(pcm[offset:offset+4800])
            self._events.put({'header': {'event': 'task-finished', 'task_id': self.id}})
        except Exception as error:
            if not self._closed.is_set():
                self._events.put(error if isinstance(error, BankError) else BankError(
                    'LOCAL_TTS_FAILED', '本地朗读暂不可用，请稍后重试。', 503))
        finally:
            LocalSpeechTask._stop(process)
            if process is not None:
                self.native_exit_code = process.returncode
                for pipe in (process.stdin, process.stderr):
                    if pipe is not None:
                        pipe.close()
            try:
                if output_path is not None:
                    output_path.unlink(missing_ok=True)
                if root is not None:
                    shutil.rmtree(root)
            finally:
                self._text = None
                _TTS_SLOT.release()

    def receive(self):
        if self._closed.is_set():
            raise BankError('SPEECH_DISCONNECTED', '语音会话已结束。', 502)
        try:
            event = self._events.get(timeout=1)
        except queue.Empty:
            raise TimeoutError()
        if isinstance(event, Exception):
            raise event
        return event

    def finish(self):
        return

    def audio(self, data):
        raise BankError('INVALID_AUDIO', '朗读任务不接受录音。')

    def close(self):
        self._closed.set()
        self._text = None
        with self._lock:
            process = self._process
        LocalSpeechTask._stop(process)
        if self._runner is not threading.current_thread():
            self._runner.join(1.5)
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                break
