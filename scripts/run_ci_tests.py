#!/usr/bin/env python3
"""运行 CI 门禁使用的 unittest 子集。

全量测试在干净检出下并非全绿：部分模块依赖封存的输出夹具、真实模型密钥、
短信 MFA 或网络放行，属于本地环境测试。CI 只跑 `tests/ci_green.txt` 中
已确认可在无状态、无密钥环境下通过的模块，避免门禁因环境缺失长期变红；
全量结果由工作流里的 report 任务单独统计，不阻塞合并。

用法：
    python3 scripts/run_ci_tests.py                # 跑清单内全部模块
    python3 scripts/run_ci_tests.py -v             # 详细输出
    python3 scripts/run_ci_tests.py --verify       # 逐模块单独跑，重新核对清单
    python3 scripts/run_ci_tests.py --list FILE    # 指定清单
    python3 scripts/run_ci_tests.py --all          # 跑 tests/ 下全部模块（等价 discover）
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
DEFAULT_LIST = TESTS_DIR / "ci_green.txt"


def _ensure_import_path() -> None:
    # 与 `python -m unittest discover -s tests` 一致：仓库根（cwd）与 tests/ 都在导入路径上，
    # 因此测试模块名是 `test_xxx`，而被测模块名是 `agent_runtime.xxx` 这类顶层包名。
    for path in (str(TESTS_DIR), str(ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)


def read_list(path: Path) -> list[str]:
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def all_modules() -> list[str]:
    return sorted(p.stem for p in TESTS_DIR.glob("test_*.py"))


def load_suite(names: list[str]) -> tuple[unittest.TestSuite, list[str]]:
    _ensure_import_path()
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    broken: list[str] = []
    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 导入失败要作为门禁失败上报
            print(f"IMPORT-FAIL {name}: {type(exc).__name__}: {exc}", flush=True)
            broken.append(name)
            continue
        suite.addTests(loader.loadTestsFromModule(module))
    return suite, broken


def run(names: list[str], verbose: bool) -> int:
    started = time.monotonic()
    suite, broken = load_suite(names)
    count = suite.countTestCases()
    print(f"载入 {len(names) - len(broken)} 个模块、{count} 个测试", flush=True)
    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1)
    result = runner.run(suite)
    elapsed = time.monotonic() - started
    print(
        f"\nRan {result.testsRun} tests in {elapsed:.1f}s: "
        f"failures={len(result.failures)} errors={len(result.errors)} "
        f"skipped={len(result.skipped)}",
        flush=True,
    )
    return 0 if result.wasSuccessful() and not broken else 1


def verify(names: list[str]) -> int:
    """逐个模块单独执行，打印仍可通过的模块，便于刷新 ci_green.txt。"""
    ok: list[str] = []
    failed: list[str] = []
    for name in names:
        _ensure_import_path()
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name} import: {type(exc).__name__}: {exc}", flush=True)
            failed.append(name)
            continue
        suite = unittest.TestLoader().loadTestsFromModule(module)
        if suite.countTestCases() == 0:
            print(f"EMPTY {name}", flush=True)
            failed.append(name)
            continue
        result = unittest.TextTestRunner(verbosity=0, stream=open("/dev/null", "w")).run(suite)
        if result.wasSuccessful():
            ok.append(name)
        else:
            print(
                f"FAIL {name}: failures={len(result.failures)} errors={len(result.errors)}",
                flush=True,
            )
            failed.append(name)
    print(f"\n通过 {len(ok)} / {len(names)}")
    for name in ok:
        print(name)
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 CI 门禁测试子集")
    parser.add_argument("--list", type=Path, default=DEFAULT_LIST, help="清单文件")
    parser.add_argument("--verify", action="store_true", help="逐模块核对清单")
    parser.add_argument("--all", action="store_true", help="忽略清单，跑全部模块")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    names = all_modules() if args.all else read_list(args.list)
    if not names:
        print(f"清单为空：{args.list}")
        return 1
    return verify(names) if args.verify else run(names, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
