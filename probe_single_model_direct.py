"""Single-model public-fixture memory probe for a capped Linux systemd unit.

Never opens a business database, sends a network request, reads credentials,
or accepts user text. The parent samples its own cgroup; only the child loads
one current encoder. No real model is run unless --backend real is explicit.
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
from pathlib import Path
import sys
import time

PUBLIC_QUERY = '公开测试：基金赎回到账时间'
PUBLIC_DOCUMENTS = (
    '公开测试材料：赎回申请后两个工作日到账。',
    'Public test material: a redemption request may settle later.',
)
SECRET_NAMES = (
    'DEEPSEEK_API_KEY', 'GLM_API_KEY', 'NAVREN_SMS_ACCESS_KEY_ID',
    'NAVREN_SMS_ACCESS_KEY_SECRET', 'NAVREN_PERSONAL_DATA_KEY',
)
ALLOWED_MODEL_CODES = frozenset({
    'vector_model_missing', 'vector_model_invalid', 'vector_dependency_missing',
    'vector_inference_failed', 'vector_input_invalid', 'vector_query_too_long',
    'vector_text_too_long',
})
MAX_CGROUP_LIMIT_BYTES = 1280 * 1024 * 1024
HOST_HEADROOM_BYTES = 128 * 1024 * 1024


class ProbeError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _current_cgroup() -> Path:
    try:
        lines = Path('/proc/self/cgroup').read_text().splitlines()
        relative = next(line.split('::', 1)[1] for line in lines if line.startswith('0::/'))
        path = (Path('/sys/fs/cgroup') / relative.lstrip('/')).resolve(strict=True)
        if not path.is_relative_to(Path('/sys/fs/cgroup').resolve(strict=True)):
            raise ProbeError('cgroup_path_invalid')
        return path
    except (OSError, StopIteration, IndexError, ValueError):
        raise ProbeError('cgroup_path_unavailable') from None


def _read_int(path: Path):
    try:
        value = path.read_text().strip()
        return int(value) if value != 'max' else None
    except (OSError, ValueError):
        return None


def _events(path: Path) -> dict[str, int]:
    try:
        return {parts[0]:int(parts[1]) for line in path.read_text().splitlines()
                if len(parts := line.split()) == 2}
    except (OSError, ValueError):
        return {}


def _host_memory() -> dict[str, int | None]:
    try:
        values = {name:int(rest.split()[0]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines()
                  if ':' in line for name, rest in [line.split(':', 1)]}
        return {'MemAvailable':values.get('MemAvailable'), 'SwapFree':values.get('SwapFree')}
    except (OSError, ValueError):
        return {'MemAvailable':None, 'SwapFree':None}


def _private_network() -> bool:
    try:
        return os.readlink('/proc/self/ns/net') != os.readlink('/proc/1/ns/net')
    except OSError:
        return False


def _clean_env() -> None:
    for name in tuple(os.environ):
        upper = name.upper()
        if (name in SECRET_NAMES or any(part in upper for part in
                ('SECRET', 'TOKEN', 'API_KEY', 'PASSWORD', 'CREDENTIAL', 'AUTH_'))):
            os.environ.pop(name, None)


def _verify_vector(value, *, profile: str, documents: bool = False) -> None:
    import numpy as np
    array = np.asarray(value, dtype='<f4')
    maximum = 8 if profile == 'e5' else 16
    if ((array.ndim != 2 if documents else array.ndim != 1)
            or array.shape[-1] != 384
            or (documents and not 1 <= array.shape[0] <= maximum)
            or not np.isfinite(array).all()):
        raise ProbeError('vector_shape_invalid')
    norms = np.linalg.norm(array, axis=-1)
    if np.any(np.abs(norms - 1.0) > 0.002):
        raise ProbeError('vector_norm_invalid')


def _child(conn, profile: str, backend: str) -> None:
    # Suppress third-party progress output so the parent emits one JSON object.
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    os.close(null)
    _clean_env()
    try:
        if backend == 'simulate':
            from worker_proto.child import SimulatedEncoder
            encoder = SimulatedEncoder(profile)
        elif backend == 'real':
            if profile == 'e5':
                from local_knowledge.multilingual_embeddings import get_multilingual_encoder
                factory = get_multilingual_encoder
            else:
                from local_knowledge.aligned_embeddings import get_aligned_encoder
                factory = get_aligned_encoder
            started = time.monotonic()
            encoder = factory()
            load_seconds = time.monotonic() - started
            from worker_proto.profile_spec import profile_spec
            expected = profile_spec(profile, 'real')
            if (encoder.profile_id != expected['profile_id']
                    or encoder.dimension != expected['dimension']):
                raise ProbeError('profile_binding_mismatch')
            conn.send({'stage':'loaded', 'seconds':round(load_seconds, 6),
                       'profile_id':expected['profile_id']})
        else:
            raise ProbeError('backend_invalid')
        if backend == 'simulate':
            conn.send({'stage':'loaded', 'seconds':0.0,
                       'profile_id':encoder.profile_id})
        phases = (
            ('query_cold', lambda:encoder.encode_query(PUBLIC_QUERY), False),
            ('query_warm', lambda:encoder.encode_query(PUBLIC_QUERY), False),
            ('documents_cold', lambda:encoder.encode_documents(list(PUBLIC_DOCUMENTS)), True),
            ('documents_warm', lambda:encoder.encode_documents(list(PUBLIC_DOCUMENTS)), True),
        )
        for name, action, documents in phases:
            started = time.monotonic()
            result = action()
            seconds = time.monotonic() - started
            if documents:
                if type(result) is not list or len(result) != len(PUBLIC_DOCUMENTS):
                    raise ProbeError('vector_shape_invalid')
                for value in result:_verify_vector(value, profile=profile, documents=True)
            else:
                _verify_vector(result, profile=profile)
            conn.send({'stage':name, 'seconds':round(seconds, 6)})
        conn.send({'stage':'done'})
    except BaseException as error:
        code = getattr(error, 'code', None)
        if code not in ALLOWED_MODEL_CODES | {'profile_binding_mismatch','vector_shape_invalid',
                                              'vector_norm_invalid','backend_invalid'}:
            code = 'model_or_inference_error'
        try:conn.send({'stage':'failed','code':code})
        except (OSError, BrokenPipeError):pass
    finally:
        conn.close()


def run_probe(profile: str, backend: str, cgroup: Path, *, deadline_seconds: float,
              require_linux: bool = True, child_target=None) -> tuple[dict, int]:
    receipt = {'schema':'navren.single-model-public-probe/1', 'profile':profile,
               'backend':backend, 'fixture':'fixed-public-short-text',
               'status':'REJECTED', 'timings_seconds':{},
               'model_quality_accepted':False, 'production_memory_accepted':False}
    if profile not in {'e5','aligned'} or backend not in {'real','simulate'}:
        return {**receipt,'error_code':'arguments_invalid'}, 5
    if require_linux and (not sys.platform.startswith('linux') or not _private_network()):
        return {**receipt,'error_code':'linux_private_network_required'}, 5
    limit = _read_int(cgroup/'memory.max')
    if limit is None or limit <= 0 or limit > MAX_CGROUP_LIMIT_BYTES:
        return {**receipt,'error_code':'bounded_cgroup_required'}, 5
    if _read_int(cgroup/'memory.swap.max') != 0:
        return {**receipt,'error_code':'swap_must_be_disabled_for_unit'}, 5
    if (_read_int(cgroup/'memory.current') is None
            or _read_int(cgroup/'memory.peak') is None
            or not {'max','oom','oom_kill'}.issubset(_events(cgroup/'memory.events'))):
        return {**receipt,'error_code':'cgroup_metrics_unavailable'}, 5
    host_before = _host_memory()
    if any(value is None for value in host_before.values()):
        return {**receipt,'error_code':'host_metrics_unavailable'}, 5
    if backend == 'real' and host_before['MemAvailable'] < limit + HOST_HEADROOM_BYTES:
        return {**receipt,'error_code':'host_headroom_insufficient',
                'host_before_bytes':host_before,'cgroup_limit_bytes':limit}, 5
    _clean_env()
    before = _events(cgroup/'memory.events')
    receipt.update(status='RUNNING', cgroup_limit_bytes=limit,
                   host_before_bytes=host_before)
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=child_target or _child,
                              args=(child, profile, backend), daemon=True)
    process.start()
    child.close()
    peak = _read_int(cgroup/'memory.current') or 0
    host_minimum = dict(host_before)
    started = time.monotonic()
    last_stage = 'starting'
    code = 4
    try:
        while time.monotonic() - started < deadline_seconds:
            current = _read_int(cgroup/'memory.current')
            if current is not None:peak = max(peak,current)
            host_now = _host_memory()
            for key, value in host_now.items():
                if value is not None:host_minimum[key] = min(host_minimum[key],value)
            if parent.poll(0.02):
                try:message = parent.recv()
                except EOFError:
                    receipt.update(status='CHILD_EXITED',error_code='child_pipe_closed')
                    break
                stage = message.get('stage')
                if stage == 'failed':
                    receipt.update(status='MODEL_FAILED',error_code=message.get('code','model_or_inference_error'))
                    code = 2
                    break
                if stage == 'done':
                    receipt['status'] = 'MEASURED_NOT_ACCEPTED'
                    code = 0
                    break
                if stage in {'loaded','query_cold','query_warm','documents_cold','documents_warm'}:
                    last_stage = stage
                    receipt['timings_seconds'][stage] = message.get('seconds')
                    if stage == 'loaded':receipt['profile_id'] = message.get('profile_id')
                else:
                    receipt.update(status='PROTOCOL_FAILED',error_code='child_message_invalid')
                    code = 4
                    break
            if not process.is_alive():
                receipt.update(status='CHILD_EXITED',error_code='child_exited_before_receipt')
                break
        else:
            receipt.update(status='TIMEOUT',error_code='probe_deadline_exceeded')
            code = 3
    finally:
        if code == 0:
            process.join(timeout=1)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        parent.close()
    after = _events(cgroup/'memory.events')
    oom_delta = after.get('oom_kill',0) - before.get('oom_kill',0)
    if oom_delta > 0:
        receipt.update(status='CHILD_OOM_KILLED',error_code='cgroup_oom_kill')
        code = 4
    receipt.update(last_stage=last_stage, child_exit_code=process.exitcode,
                   sampled_cgroup_current_peak_bytes=peak,
                   cgroup_peak_cumulative_bytes=_read_int(cgroup/'memory.peak'),
                   cgroup_events_delta={key:after.get(key,0)-before.get(key,0)
                                        for key in ('max','oom','oom_kill')},
                   host_minimum_observed_bytes=host_minimum,
                   host_after_bytes=_host_memory())
    return receipt, code


def main() -> int:
    class JsonArgumentParser(argparse.ArgumentParser):
        def error(self, message):
            raise ProbeError('arguments_invalid')

    parser = JsonArgumentParser(add_help=False)
    parser.add_argument('--profile')
    parser.add_argument('--backend', default='simulate')
    parser.add_argument('--deadline-seconds', type=float, default=180.0)
    parser.add_argument('--public-fixture-only', action='store_true')
    args = None
    try:
        args = parser.parse_args()
        if not args.public_fixture_only or args.profile not in ('e5','aligned') or args.backend not in ('simulate','real'):
            raise ProbeError('arguments_invalid')
        if not math.isfinite(args.deadline_seconds) or args.deadline_seconds <= 0 or args.deadline_seconds > 300:
            raise ProbeError('deadline_invalid')
        cgroup = _current_cgroup() if args.backend == 'real' else Path(os.environ.get('NAVREN_PROBE_TEST_CGROUP','/sys/fs/cgroup'))
        receipt, code = run_probe(args.profile,args.backend,cgroup,
                                  deadline_seconds=args.deadline_seconds,
                                  require_linux=args.backend == 'real')
    except ProbeError as error:
        receipt, code = {'schema':'navren.single-model-public-probe/1',
                         'profile':args.profile if args else None,
                         'backend':args.backend if args else None,
                         'status':'REJECTED','error_code':error.code,
                         'model_quality_accepted':False,
                         'production_memory_accepted':False}, 5
    except BaseException:
        receipt, code = {'schema':'navren.single-model-public-probe/1',
                         'profile':args.profile if args else None,
                         'backend':args.backend if args else None,
                         'status':'FAILED','error_code':'internal_error',
                         'model_quality_accepted':False,
                         'production_memory_accepted':False}, 6
    print(json.dumps(receipt,ensure_ascii=False,separators=(',',':')))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
