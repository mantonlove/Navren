#!/usr/bin/env python3
"""服务器变更前核对 GitHub 已推送提交；不写 Git 索引、工作区或服务器。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from urllib.parse import urlsplit


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, timeout=30
    )
    if result.returncode:
        raise ValueError("git_command_failed:" + args[0])
    return result.stdout


def relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in (".", "..") for p in value.split("/")):
        raise ValueError("invalid_repository_path")
    return str(path)


def verify(root: Path, commit: str, branch: str, manifest_path: str, remote: str = "origin") -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("full_commit_required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch:
        raise ValueError("invalid_branch")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
        raise ValueError("invalid_remote")
    url = git(root, "remote", "get-url", remote).decode().strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password:
        raise ValueError("github_https_remote_required")
    if parsed.query or parsed.fragment or parsed.port not in (None, 443):
        raise ValueError("invalid_github_remote")
    repository = "https://github.com/" + parsed.path.strip("/").removesuffix(".git")
    reference = "refs/heads/" + branch
    lines = git(root, "ls-remote", "--exit-code", remote, reference).decode().splitlines()
    if len(lines) != 1 or lines[0].split() != [commit, reference]:
        raise ValueError("remote_branch_commit_mismatch")
    manifest_path = relative_file(manifest_path)
    raw_manifest = git(root, "show", commit + ":" + manifest_path)
    manifest = json.loads(raw_manifest)
    if manifest.get("repository") != repository or manifest.get("branch") != branch:
        raise ValueError("deployment_repository_or_branch_mismatch")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("deployment_files_missing")
    checked = {}
    for path, digest in expected.items():
        path = relative_file(path)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid_file_digest")
        actual = hashlib.sha256(git(root, "show", commit + ":" + path)).hexdigest()
        if actual != digest:
            raise ValueError("committed_file_digest_mismatch:" + path)
        checked[path] = actual
    return {
        "schema": "navren.github-before-deploy/1",
        "verified": True,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "repository": repository,
        "branch": branch,
        "commit": commit,
        "remote_commit": commit,
        "manifest_path": manifest_path,
        "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
        "files": checked,
        "server_changed": False,
        "business_accepted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--commit", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args()
    try:
        result = verify(args.repo_root, args.commit, args.branch, args.manifest, args.remote)
    except (ValueError, OSError, subprocess.SubprocessError, UnicodeError) as error:
        print(json.dumps({"verified": False, "reason": str(error), "server_changed": False}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
