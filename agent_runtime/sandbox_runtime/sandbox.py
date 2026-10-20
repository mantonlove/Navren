"""Isolated prototype API. All Python in this file is fixed trusted host code.

The caller scope and dataset registry MUST come from the trusted application,
never from a model or a browser payload. Production authentication is absent.
There is intentionally no HTTP, bank tool, or arbitrary interpreter endpoint.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import selectors
import signal
import struct
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
WORKER = ROOT / "worker.py"
PYTHON = ROOT.parents[1] / ".venv" / "bin" / "python"
MAX_CODE = 65536
MAX_INPUT = 8192
MAX_VALUES = 256
MAX_OUTPUT = 256
MAX_REQUEST = 131072
MAX_STDOUT = 8192
MAX_STDERR = 8192


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class Scope:
    workspace: str
    session: str


@dataclass(frozen=True)
class Limits:
    memory_bytes: int = 1048576
    fuel: int = 2000000
    total_seconds: float = 5.0
    rss_bytes: int = 134217728

    def checked(self) -> "Limits":
        if type(self.memory_bytes) is not int or not 65536 <= self.memory_bytes <= 1048576:
            raise ValueError("memory_limit_invalid")
        if type(self.fuel) is not int or not 1 <= self.fuel <= 100000000:
            raise ValueError("fuel_limit_invalid")
        if type(self.rss_bytes) is not int or not 1048576 <= self.rss_bytes <= 134217728:
            raise ValueError("rss_limit_invalid")
        if type(self.total_seconds) not in (int, float) or not math.isfinite(self.total_seconds) or not 0 < self.total_seconds <= 5.0:
            raise ValueError("total_deadline_invalid")
        return self


@dataclass(frozen=True)
class Dataset:
    handle: str
    scope: Scope
    version: str
    unit: str
    values: tuple[int, ...]
    input_sha256: str


class TrustedDatasetRegistry:
    """A trusted caller admits an already authorized, deidentified data COPY.

    This class implements identity binding, not login, customer authorization,
    retrieval, account-fact validation, or operation MFA.
    """
    def __init__(self) -> None:
        self._datasets: dict[str, Dataset] = {}
        self._lock = threading.Lock()

    def admit_copy(self, scope: Scope, version: str, values: list[int], *, unit: str = "minor") -> str:
        if not isinstance(scope, Scope) or any(not isinstance(s, str) or not 1 <= len(s) <= 128 for s in (scope.workspace, scope.session, version)):
            raise ValueError("dataset_identity_invalid")
        if unit not in ("minor", "integer"):
            raise ValueError("dataset_unit_invalid")
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_VALUES:
            raise ValueError("input_shape")
        if any(type(v) is not int or not -(1 << 63) <= v < (1 << 63) for v in values):
            raise ValueError("input_i64_range")
        copied = tuple(values)
        raw = b"".join(struct.pack("<q", v) for v in copied)
        if len(raw) > MAX_INPUT:
            raise ValueError("input_too_large")
        handle = str(uuid.uuid4())
        dataset = Dataset(handle, scope, version, unit, copied, sha(raw))
        with self._lock:
            self._datasets[handle] = dataset
        return handle

    def resolve(self, scope: Scope, handle: str) -> Dataset | None:
        with self._lock:
            item = self._datasets.get(handle)
        return item if item is not None and item.scope == scope else None


class SandboxExecutor:
    def __init__(self, registry: TrustedDatasetRegistry, audit_dir: Path, *, limits: Limits = Limits()) -> None:
        self.registry = registry
        self.audit_dir = Path(audit_dir)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.limits = limits.checked()
        self._slot = threading.Lock()
        self._receipts: dict[str, tuple[str, bool]] = {}
        self._receipt_lock = threading.Lock()

    def _audit(self, receipt: dict) -> dict:
        # New immutable event per execution; no shared append file or raw input/code.
        target = self.audit_dir / (receipt["execution_id"] + ".json")
        raw = canonical(receipt)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # An audit failure is an execution failure, never successful return.
            raise
        with self._receipt_lock:
            self._receipts[receipt["execution_id"]] = (sha(raw), False)
        return receipt

    @staticmethod
    def _rss(pid: int) -> int | None:
        probe = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "rss="], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=0.15,
                               env={"LC_ALL": "C"}, check=False)
        text = probe.stdout.strip()
        if probe.returncode != 0 or not text or not text.isdigit():
            return None
        return int(text) * 1024

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=1)

    def execute(self, scope: Scope, handle: str, wat: str, *, cancel: threading.Event | None = None,
                observe=None) -> dict:
        """Untrusted WAT is the only generated executable material.

        `scope`, `handle`, cancellation, limits and observer are trusted host
        controls. `observe` receives fixed phase names, never runs guest code.
        """
        started = time.monotonic()
        receipt = {"schema": "navren.sandbox-execution/1", "execution_id": str(uuid.uuid4()),
                   "status": "REJECTED", "reason": None, "code_sha256": None,
                   "input_sha256": None, "dataset_version": None, "binding_sha256": None,
                   "scope_sha256": sha(canonical(asdict(scope))) if isinstance(scope, Scope) else None,
                   "candidate": None, "limits": asdict(self.limits), "allowed_imports": [],
                   "financial_execution_authorized": False, "account_fact_verified": False,
                   "worker_pid": None, "worker_returncode": None, "worker_reaped": True,
                   "worker_group_absent": True, "temporary_directory_removed": True,
                   "rss_samples": 0, "rss_peak_observed_bytes": 0,
                   "rss_monitor_is_sampled_not_kernel_hard_limit": True,
                   "worker_phases": [], "worker_report": None,
                   "host_source_sha256": sha(Path(__file__).read_bytes()),
                   "worker_source_sha256": sha(WORKER.read_bytes()),
                   "runtime": {"name": "Wasmtime", "version": importlib.metadata.version("wasmtime")},
                   "monotonic_started": started}
        def finish(reason=None, status=None):
            if reason is not None:
                receipt["reason"] = reason
            if status is not None:
                receipt["status"] = status
            receipt["elapsed_seconds"] = round(time.monotonic() - started, 6)
            return self._audit(receipt)
        if not isinstance(scope, Scope) or not isinstance(handle, str):
            return finish("trusted_binding_invalid")
        dataset = self.registry.resolve(scope, handle)
        if dataset is None:
            return finish("dataset_scope_or_handle_denied")
        receipt.update(input_sha256=dataset.input_sha256, dataset_version=dataset.version)
        if not isinstance(wat, str):
            return finish("only_wat_text_accepted")
        if len(wat) > MAX_CODE:
            return finish("code_too_large")
        try:
            code_bytes = wat.encode("utf-8")
        except UnicodeError:
            return finish("code_utf8_invalid")
        if len(code_bytes) > MAX_CODE:
            return finish("code_too_large")
        receipt["code_sha256"] = sha(code_bytes)
        receipt["binding_sha256"] = sha(canonical({"execution_id": receipt["execution_id"], "scope": asdict(scope),
                                                   "dataset_handle": handle, "dataset_version": dataset.version,
                                                   "input_sha256": dataset.input_sha256,
                                                   "code_sha256": receipt["code_sha256"]}))
        if cancel is not None and cancel.is_set():
            return finish("user_cancelled", "CANCELLED")
        if not self._slot.acquire(blocking=False):
            return finish("executor_busy")
        proc = None
        temp_dir = None
        selector = selectors.DefaultSelector()
        stdout = bytearray()
        stderr = bytearray()
        stderr_parsed = 0
        request = canonical({"wat": wat, "values": list(dataset.values), "input_sha256": dataset.input_sha256,
                             "memory_bytes": self.limits.memory_bytes, "fuel": self.limits.fuel})
        written = 0
        deadline = started + self.limits.total_seconds
        failure = None
        try:
            if len(request) > MAX_REQUEST:
                failure = "worker_request_too_large"
            else:
                temp_dir = tempfile.TemporaryDirectory(prefix="navren-wasm-", dir=self.audit_dir)
                proc = subprocess.Popen([str(PYTHON), "-I", str(WORKER)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=temp_dir.name,
                                        env={"LC_ALL": "C", "PYTHONHASHSEED": "0"},
                                        close_fds=True, start_new_session=True)
                receipt["worker_pid"] = proc.pid
                receipt["worker_reaped"] = False
                receipt["temporary_directory_removed"] = False
                if observe is not None:
                    observe("spawned")
                for pipe, kind in ((proc.stdin, "stdin"), (proc.stdout, "stdout"), (proc.stderr, "stderr")):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_WRITE if kind == "stdin" else selectors.EVENT_READ, kind)
                next_rss = 0.0
                while selector.get_map() or proc.poll() is None:
                    now = time.monotonic()
                    if cancel is not None and cancel.is_set():
                        failure = "user_cancelled"
                        break
                    if now >= deadline:
                        failure = "total_deadline_exceeded"
                        break
                    if proc.poll() is None and now >= next_rss:
                        try:
                            rss = self._rss(proc.pid)
                        except (OSError, subprocess.TimeoutExpired):
                            rss = None
                        next_rss = time.monotonic() + 0.025
                        if rss is None and proc.poll() is None:
                            failure = "rss_monitor_unavailable"
                            break
                        if rss is not None:
                            receipt["rss_samples"] += 1
                            receipt["rss_peak_observed_bytes"] = max(receipt["rss_peak_observed_bytes"], rss)
                            if rss > self.limits.rss_bytes:
                                failure = "worker_rss_exceeded"
                                break
                    if time.monotonic() >= deadline:
                        failure = "total_deadline_exceeded"
                        break
                    events = selector.select(timeout=min(0.01, max(0, deadline - time.monotonic())))
                    for key, _ in events:
                        pipe, kind = key.fileobj, key.data
                        if kind == "stdin":
                            try:
                                count = os.write(pipe.fileno(), request[written:written + 8192])
                                written += count
                            except BrokenPipeError:
                                written = len(request)
                            if written == len(request):
                                selector.unregister(pipe)
                                pipe.close()
                        else:
                            try:
                                chunk = os.read(pipe.fileno(), 4096)
                            except BlockingIOError:
                                continue
                            if not chunk:
                                selector.unregister(pipe)
                                pipe.close()
                                continue
                            target = stdout if kind == "stdout" else stderr
                            target.extend(chunk)
                            if len(target) > (MAX_STDOUT if kind == "stdout" else MAX_STDERR):
                                failure = "worker_" + kind + "_exceeded"
                                break
                            if kind == "stderr":
                                while b"\n" in stderr[stderr_parsed:]:
                                    end = stderr.index(10, stderr_parsed)
                                    line = bytes(stderr[stderr_parsed:end])
                                    stderr_parsed = end + 1
                                    try:
                                        item = json.loads(line)
                                    except ValueError:
                                        failure = "worker_progress_invalid"
                                        break
                                    if item.get("phase") not in ("parse_wat", "compile", "instantiate", "execute") or set(item) != {"phase"}:
                                        failure = "worker_progress_invalid"
                                        break
                                    receipt["worker_phases"].append(item["phase"])
                                    if observe is not None:
                                        observe(item["phase"])
                        if failure is not None:
                            break
                    if failure is not None:
                        break
                if failure is None:
                    proc.wait(timeout=max(0.001, deadline - time.monotonic()))
                    if proc.returncode != 0:
                        failure = "worker_nonzero_exit"
                    else:
                        try:
                            worker_report = json.loads(stdout)
                        except ValueError:
                            failure = "worker_result_invalid"
                        else:
                            receipt["worker_report"] = worker_report
                            receipt["status"] = worker_report.get("status", "REJECTED")
                            receipt["reason"] = worker_report.get("reason")
                            if receipt["status"] == "COMPLETED_CANDIDATE":
                                result = worker_report.get("candidate_values")
                                if not isinstance(result, list) or not 1 <= len(result) <= MAX_OUTPUT // 8 or any(type(v) is not int or not -(1 << 63) <= v < (1 << 63) for v in result):
                                    failure = "worker_candidate_shape_invalid"
                                else:
                                    receipt["candidate"] = {"values": result, "unit": dataset.unit,
                                                             "input_sha256": dataset.input_sha256,
                                                             "dataset_version": dataset.version,
                                                             "financial_execution_authorized": False,
                                                             "account_fact_verified": False}
        except subprocess.TimeoutExpired:
            failure = "total_deadline_exceeded"
        except Exception as exc:
            failure = "trusted_parent_error"
            receipt["error_type"] = type(exc).__name__
        finally:
            if proc is not None:
                self._stop(proc)
                receipt["worker_returncode"] = proc.returncode
                receipt["worker_reaped"] = proc.poll() is not None
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    receipt["worker_group_absent"] = True
                else:
                    receipt["worker_group_absent"] = False
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None and not pipe.closed:
                        pipe.close()
            selector.close()
            if temp_dir is not None:
                path = Path(temp_dir.name)
                temp_dir.cleanup()
                receipt["temporary_directory_removed"] = not path.exists()
            self._slot.release()
        if failure is not None:
            receipt["candidate"] = None
            return finish(failure, "CANCELLED" if failure == "user_cancelled" else "TERMINATED" if failure in ("total_deadline_exceeded", "worker_rss_exceeded") else "REJECTED")
        if not receipt["worker_reaped"] or not receipt["worker_group_absent"] or not receipt["temporary_directory_removed"]:
            receipt["candidate"] = None
            return finish("worker_cleanup_failed", "REJECTED")
        return finish()

    def consume_candidate(self, receipt: dict, scope: Scope, handle: str, wat: str) -> dict:
        """Return a once-consumed CANDIDATE only; no assertion of account truth.

        Trusted application must separately validate units, period, source,
        arithmetic and business purpose before using this in a response.
        """
        dataset = self.registry.resolve(scope, handle)
        if dataset is None:
            raise ValueError("dataset_scope_or_handle_denied")
        execution_id = receipt.get("execution_id")
        with self._receipt_lock:
            saved = self._receipts.get(execution_id)
            if saved is None or saved[0] != sha(canonical(receipt)):
                raise ValueError("receipt_unknown_or_tampered")
            if saved[1]:
                raise ValueError("receipt_already_consumed")
            if receipt.get("status") != "COMPLETED_CANDIDATE":
                raise ValueError("receipt_not_completed")
            if receipt.get("scope_sha256") != sha(canonical(asdict(scope))) or receipt.get("input_sha256") != dataset.input_sha256 or receipt.get("dataset_version") != dataset.version or receipt.get("code_sha256") != sha(wat.encode("utf-8")):
                raise ValueError("receipt_binding_changed")
            expected = sha(canonical({"execution_id": execution_id, "scope": asdict(scope),
                                      "dataset_handle": handle, "dataset_version": dataset.version,
                                      "input_sha256": dataset.input_sha256, "code_sha256": receipt["code_sha256"]}))
            if receipt.get("binding_sha256") != expected:
                raise ValueError("receipt_binding_changed")
            if time.monotonic() - receipt["monotonic_started"] > 60:
                raise ValueError("receipt_expired")
            self._receipts[execution_id] = (saved[0], True)
        return {"kind": "sandbox_calculation_candidate", "execution_id": execution_id,
                **receipt["candidate"], "verified_account_or_transaction_fact": False}
