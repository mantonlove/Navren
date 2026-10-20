"""Fixed trusted worker; generated material is parsed exclusively by Wasmtime.

No WASI, linker, host functions, shell, eval, exec, file/path argument, or
serialized native module is accepted. This file is not an Agent entry point.
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
import time

import wasmtime

MAX_REQUEST = 131072
MAX_CODE = 65536
MAX_INPUT = 8192
MAX_VALUES = 256
MAX_OUTPUT = 256
MAX_MEMORY = 1048576
MAX_FUEL = 100000000
INPUT_PTR = 4096


class Refusal(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def phase(name: str) -> None:
    # Only fixed strings from trusted code reach stderr, never guest text.
    sys.stderr.write(json.dumps({"phase": name}, separators=(",", ":")) + "\n")
    sys.stderr.flush()


def main() -> None:
    started = time.monotonic()
    store = None
    report = {"schema": "navren.sandbox-worker/1", "status": "REJECTED", "reason": None,
              "imports_count": None, "linear_memory_bytes": None, "fuel_consumed": None,
              "candidate_values": None, "bank_execution_authorized": False,
              "account_fact_verified": False, "wasi_configured": False,
              "host_imports_registered": 0}
    try:
        payload = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(payload) > MAX_REQUEST:
            raise Refusal("worker_request_too_large")
        req = json.loads(payload)
        if set(req) != {"wat", "values", "input_sha256", "memory_bytes", "fuel"}:
            raise Refusal("worker_request_shape")
        wat = req["wat"]
        if not isinstance(wat, str) or len(wat) > MAX_CODE or len(wat.encode("utf-8")) > MAX_CODE:
            raise Refusal("code_too_large")
        values = req["values"]
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_VALUES:
            raise Refusal("input_shape")
        if any(type(v) is not int or not -(1 << 63) <= v < (1 << 63) for v in values):
            raise Refusal("input_i64_range")
        input_bytes = b"".join(struct.pack("<q", v) for v in values)
        if len(input_bytes) > MAX_INPUT:
            raise Refusal("input_too_large")
        if hashlib.sha256(input_bytes).hexdigest() != req["input_sha256"]:
            raise Refusal("input_hash_mismatch")
        memory_limit = req["memory_bytes"]
        fuel = req["fuel"]
        if type(memory_limit) is not int or not 65536 <= memory_limit <= MAX_MEMORY:
            raise Refusal("memory_limit_invalid")
        if type(fuel) is not int or not 1 <= fuel <= MAX_FUEL:
            raise Refusal("fuel_limit_invalid")
        config = wasmtime.Config()
        config.consume_fuel = True
        config.wasm_threads = False
        config.wasm_memory64 = False
        config.wasm_multi_memory = False
        config.wasm_component_model = False
        # Wasmtime 49's default component-model-async engine feature depends on
        # reference types even when component modules are disabled. The Python
        # API exposes no async feature switch. Keep reference types enabled;
        # this grants no host capability: core Module only, every import denied,
        # no host function/linker/WASI, and Store limits forbid every table.
        config.wasm_reference_types = True
        config.wasm_function_references = False
        config.wasm_gc = False
        engine = wasmtime.Engine(config)
        phase("parse_wat")
        binary = wasmtime.wat2wasm(wat)
        phase("compile")
        module = wasmtime.Module(engine, binary)
        report["imports_count"] = len(module.imports)
        if module.imports:
            raise Refusal("all_imports_forbidden")
        exports = {item.name: item.type for item in module.exports}
        if set(exports) != {"memory", "run"}:
            raise Refusal("export_contract_invalid")
        if not isinstance(exports["memory"], wasmtime.MemoryType):
            raise Refusal("memory_export_invalid")
        run_type = exports["run"]
        if not isinstance(run_type, wasmtime.FuncType):
            raise Refusal("run_export_invalid")
        if [str(v) for v in run_type.params] != ["i32", "i32"] or [str(v) for v in run_type.results] != ["i64"]:
            raise Refusal("run_signature_invalid")
        store = wasmtime.Store(engine)
        store.set_limits(memory_size=memory_limit, table_elements=0, instances=1, tables=0, memories=1)
        store.set_fuel(fuel)
        phase("instantiate")
        instance = wasmtime.Instance(store, module, [])
        memory = instance.exports(store)["memory"]
        run = instance.exports(store)["run"]
        if memory.data_len(store) < INPUT_PTR + len(input_bytes):
            raise Refusal("input_memory_too_small")
        memory.write(store, input_bytes, INPUT_PTR)
        phase("execute")
        packed = run(store, INPUT_PTR, len(values)) & ((1 << 64) - 1)
        ptr, length = packed & 0xffffffff, packed >> 32
        report["linear_memory_bytes"] = memory.data_len(store)
        if length == 0 or length > MAX_OUTPUT or length % 8:
            raise Refusal("output_size_invalid")
        if ptr > memory.data_len(store) or length > memory.data_len(store) - ptr:
            raise Refusal("output_out_of_bounds")
        result_bytes = bytes(memory.read(store, ptr, ptr + length))
        report["candidate_values"] = list(struct.unpack("<" + "q" * (length // 8), result_bytes))
        report["output_sha256"] = hashlib.sha256(result_bytes).hexdigest()
        report["status"] = "COMPLETED_CANDIDATE"
    except Refusal as exc:
        report["reason"] = exc.reason
    except wasmtime.Trap as exc:
        report["status"] = "TRAPPED"
        report["reason"] = "fuel_exhausted" if exc.trap_code == wasmtime.TrapCode.OUT_OF_FUEL else "wasm_trap"
        report["trap_code"] = None if exc.trap_code is None else exc.trap_code.name
    except wasmtime.WasmtimeError:
        report["reason"] = "wasm_validation_compile_or_resource_error"
    except (ValueError, TypeError, KeyError, OverflowError, UnicodeError):
        report["reason"] = "invalid_request"
    except Exception as exc:
        # Never emit raw error messages: generated strings can contain secrets or terminal escapes.
        report["reason"] = "trusted_worker_error"
        report["error_type"] = type(exc).__name__
    finally:
        if store is not None:
            try:
                report["fuel_consumed"] = fuel - store.get_fuel()
            except Exception:
                pass
        report["worker_elapsed_seconds"] = round(time.monotonic() - started, 6)
        sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
