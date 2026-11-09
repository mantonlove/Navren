#!/usr/bin/env python3
"""按清单获取 dependency-resources 下的大文件（tokenizer.json）并校验 sha256。

背景：三个 tokenizer.json 合计约 32.8 MB（glm53-flash 约 19 MB，两个 DeepSeek
各约 6.1 MB），2026-11-09 起从 Git 索引移除，改为按需获取。运行时代码
（agent_runtime/request_budget.py）会按固定 sha256 读取这些文件，缺失或被改动
都会 fail-closed 报错，因此克隆仓库后需要先跑一次本脚本。

清单里每一项都是可核对的事实：仓库内路径、上游 URL（固定到具体 revision）、
字节数与 sha256。sha256 取自本地文件实际 `shasum -a 256` 的结果，并于 2026-11-09
按下列 URL 重新下载、逐字节比对一致。

用法：
    python3 scripts/fetch_dependency_resources.py --check
        只做本地校验，不联网：文件存在则核 sha256；缺失则打印获取方式并以非零退出。
    python3 scripts/fetch_dependency_resources.py --fetch
        缺失或哈希不符时按清单 URL 下载，校验通过后原子替换。清单里没有可靠 URL
        （来源待核实）的条目会直接报错退出，不会静默跳过。

退出码：
    0  清单内文件全部存在且 sha256 一致，或获取成功
    1  存在缺失、哈希不符或下载失败
    2  清单或参数错误（含来源未核实、没有可用 URL）
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHUNK = 1024 * 1024
USER_AGENT = "navren-dependency-fetcher/1"

# 只收录已从 Git 索引移除的大文件。清单中的 sha256 必须与本地文件实际
# `shasum -a 256` 一致，并且能在 url 指向的固定 revision 上复现。
MANIFEST = (
    {
        "path": "dependency-resources/deepseek-v4/tokenizer.json",
        "bytes": 6367146,
        "sha256": "8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf",
        "url": (
            "https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/resolve/"
            "60d8d70770c6776ff598c94bb586a859a38244f1/tokenizer.json"
        ),
        "source": "deepseek-ai/DeepSeek-V4-Flash @ 60d8d70770c6776ff598c94bb586a859a38244f1",
    },
    {
        "path": "dependency-resources/deepseek-v41-1017/tokenizer.json",
        "bytes": 6367257,
        "sha256": "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b",
        "url": (
            "https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/resolve/"
            "1bae0e1088993b9b82de08f59de5be5a34e42170/tokenizer.json"
        ),
        "source": "deepseek-ai/DeepSeek-V4.1-Flash-1017 @ 1bae0e1088993b9b82de08f59de5be5a34e42170",
    },
    {
        "path": "dependency-resources/glm53-flash/tokenizer.json",
        "bytes": 20217442,
        "sha256": "19e773648cb4e65de8660ea6365e10acca112d42a854923df93db4a6f333a82d",
        "url": (
            "https://huggingface.co/zai-org/GLM-5.3-Flash/resolve/"
            "46a050bec13002ad29ee795c16e61cf4fa3c4a9c/tokenizer.json"
        ),
        "source": "zai-org/GLM-5.3-Flash @ 46a050bec13002ad29ee795c16e61cf4fa3c4a9c",
    },
)


def entry_path(entry: dict) -> Path:
    path = Path(entry["path"])
    return path if path.is_absolute() else REPO_ROOT / path


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _print_howto(entry: dict) -> None:
    if entry.get("url"):
        print("    获取：python3 scripts/fetch_dependency_resources.py --fetch")
        print(f"    上游：{entry['url']}")
    else:
        print("    获取：来源待核实，清单里没有可靠上游 URL；请先确认来源再运行 --fetch。")


def check(manifest=MANIFEST) -> int:
    """本地校验：不联网。返回退出码。"""
    failures = []
    for entry in manifest:
        path = entry_path(entry)
        print(f"- {entry['path']}")
        if not path.is_file():
            print("    状态：缺失")
            _print_howto(entry)
            failures.append(entry["path"])
            continue
        actual = sha256_of(path)
        size = path.stat().st_size
        if actual != entry["sha256"]:
            print("    状态：sha256 不符")
            print(f"    期望：{entry['sha256']}（{entry['bytes']} bytes）")
            print(f"    实际：{actual}（{size} bytes）")
            _print_howto(entry)
            failures.append(entry["path"])
            continue
        print(f"    状态：存在且 sha256 一致（{size} bytes）")
    print()
    if failures:
        print(f"失败：{len(failures)}/{len(manifest)} 个文件不可用：{'、'.join(failures)}")
        print("修复：python3 scripts/fetch_dependency_resources.py --fetch")
        return 1
    print(f"通过：{len(manifest)}/{len(manifest)} 个文件存在且 sha256 与清单一致。")
    return 0


def _download(entry: dict, dest: Path) -> None:
    """下载到临时文件，校验通过后再原子替换；任何失败都清理临时文件。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(entry["url"], headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as out:
            for chunk in iter(lambda: response.read(CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
        actual = digest.hexdigest()
        if actual != entry["sha256"]:
            raise ValueError(f"下载内容 sha256 不符：实际 {actual}，期望 {entry['sha256']}")
        if size != entry["bytes"]:
            raise ValueError(f"下载字节数不符：实际 {size}，期望 {entry['bytes']}")
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def fetch(manifest=MANIFEST) -> int:
    """按清单下载缺失或哈希不符的文件。返回退出码。"""
    pending = []
    for entry in manifest:
        path = entry_path(entry)
        if path.is_file() and sha256_of(path) == entry["sha256"]:
            print(f"[已就绪] {entry['path']}")
            continue
        pending.append(entry)

    if not pending:
        print("全部文件已就绪，无需下载。")
        return 0

    without_url = [entry for entry in pending if not entry.get("url")]
    if without_url:
        for entry in without_url:
            print(
                f"[错误] {entry['path']}：清单没有可靠上游 URL（来源待核实），拒绝下载。",
                file=sys.stderr,
            )
        print("未开始任何下载：请先补齐清单中的上游 URL 与 sha256，再运行 --fetch。", file=sys.stderr)
        return 2

    failures = []
    for entry in pending:
        path = entry_path(entry)
        print(f"[下载] {entry['path']}")
        print(f"       来源：{entry['source']}")
        try:
            _download(entry, path)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"[失败] {entry['path']}：{exc}", file=sys.stderr)
            failures.append(entry["path"])
            continue
        print(f"[完成] {entry['path']}：{path.stat().st_size} bytes，sha256 一致")

    print()
    if failures:
        print(f"失败：{len(failures)}/{len(manifest)} 个文件未获取成功：{'、'.join(failures)}")
        return 1
    print(f"通过：{len(manifest)}/{len(manifest)} 个文件存在且 sha256 与清单一致。")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="获取并校验 dependency-resources 下的大文件（tokenizer.json）。",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只做本地校验，不联网（默认）")
    group.add_argument("--fetch", action="store_true", help="缺失或哈希不符时按清单下载")
    args = parser.parse_args(argv)

    if args.fetch:
        return fetch()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
