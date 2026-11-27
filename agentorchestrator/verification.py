from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

_EXCLUDED_DIRS = {
    ".git", "data", "logs", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", "node_modules", "dist", "build",
}


def _tree_fingerprint(root: Path) -> str:
    parts: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _EXCLUDED_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            parts.append(
                f"{path.relative_to(root).as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}"
            )
    parts.sort()
    return hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest()


def _git(workspace: Path, args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
    )


def capture_workspace(workspace: str | Path | None) -> dict[str, Any]:
    """采集工作区的独立证据（git HEAD / status / diff stat）。"""
    if not workspace:
        return {"available": False, "reason": "no_workspace"}
    path = Path(workspace).expanduser()
    if not path.is_dir():
        return {"available": False, "reason": "missing_directory"}
    if shutil.which("git") is None:
        return {"available": False, "reason": "git_not_found"}
    try:
        inside = _git(path, ["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return {"available": False, "reason": "not_git"}
        head = _git(path, ["rev-parse", "HEAD"])
        status = _git(path, ["status", "--porcelain=v1"])
        numstat = _git(path, ["diff", "--numstat"])
        stat = _git(path, ["diff", "--stat"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": str(exc)}

    fingerprint = "\n".join([
        head.stdout.strip(), status.stdout.strip(), numstat.stdout.strip(),
        _tree_fingerprint(path),
    ])
    return {
        "available": True,
        "workspace": str(path.resolve()),
        "head": head.stdout.strip(),
        "status": status.stdout.strip(),
        "diff_stat": stat.stdout.strip(),
        "fingerprint": hashlib.sha256(fingerprint.encode("utf-8", "replace")).hexdigest(),
    }


def workspace_changed(before: dict[str, Any] | None,
                      after: dict[str, Any] | None) -> bool | None:
    if not before or not after:
        return None
    if not before.get("available") or not after.get("available"):
        return None
    return (before.get("head") != after.get("head")
            or before.get("fingerprint") != after.get("fingerprint"))


def verification_prompt(before: dict[str, Any] | None,
                        after: dict[str, Any] | None) -> str:
    if not before or not before.get("available"):
        reason = (before or {}).get("reason", "unknown")
        return f"（协调器无法独立核对工作区：{reason}）\n"
    changed = workspace_changed(before, after)
    before_status = (before.get("status") or "").strip() or "clean"
    after_status = (after or {}).get("status") or ""
    after_status = after_status.strip() or "clean"
    return (
        "【协调器实测工作区（独立证据）】\n"
        f"- 运行前 HEAD: {before.get('head') or '(no commit)'}\n"
        f"- 运行后 HEAD: {(after or {}).get('head') or '(no commit)'}\n"
        f"- 运行前 status: {before_status[:1200]}\n"
        f"- 运行后 status: {after_status[:1200]}\n"
        f"- 是否发生变化: {'是' if changed else ('否' if changed is False else '无法判断')}\n"
    )
