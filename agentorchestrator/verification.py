from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_EXCLUDE_DIRS = (
    ".git", "data", "logs", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", "node_modules", "dist", "build",
)


def _git(workspace: Path, args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
    )


def _tree_fingerprint(root: Path, exclude_dirs: set[str]) -> tuple[str | None, list[str]]:
    """对纳入范围的文件计算内容 SHA-256；返回 (fingerprint, errors)。"""
    parts: list[str] = []
    errors: list[str] = []

    def onerror(exc: OSError) -> None:
        errors.append(str(exc))

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        dirnames[:] = [name for name in dirnames if name not in exclude_dirs]
        for name in filenames:
            path = Path(dirpath) / name
            relative = path.relative_to(root).as_posix()
            try:
                stat = path.stat()
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                parts.append(f"{relative}\0{stat.st_size}\0{digest.hexdigest()}")
            except OSError as exc:
                errors.append(f"{relative}: {exc}")
    if errors:
        return None, errors
    parts.sort()
    return hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest(), []


def capture_workspace(workspace: str | Path | None,
                      exclude_dirs: list[str] | None = None) -> dict[str, Any]:
    """采集工作区的独立证据（git 快照 + 文件内容指纹）。"""
    excluded = set(DEFAULT_EXCLUDE_DIRS) | set(exclude_dirs or [])
    if not workspace:
        return {"available": False, "reason": "no_workspace", "exclude_dirs": sorted(excluded)}
    path = Path(workspace).expanduser()
    if not path.is_dir():
        return {"available": False, "reason": "missing_directory",
                "exclude_dirs": sorted(excluded)}
    if shutil.which("git") is None:
        return {"available": False, "reason": "git_not_found",
                "exclude_dirs": sorted(excluded)}
    try:
        inside = _git(path, ["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return {"available": False, "reason": "not_git",
                    "exclude_dirs": sorted(excluded)}
        head = _git(path, ["rev-parse", "HEAD"])
        status = _git(path, ["status", "--porcelain=v1"])
        numstat = _git(path, ["diff", "--numstat"])
        stat = _git(path, ["diff", "--stat"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": str(exc),
                "exclude_dirs": sorted(excluded)}

    if status.returncode != 0 or numstat.returncode != 0 or stat.returncode != 0:
        return {
            "available": False,
            "reason": "git_command_failed",
            "detail": (status.stderr or numstat.stderr or stat.stderr).strip()[:500],
            "exclude_dirs": sorted(excluded),
        }
    if head.returncode != 0:
        # 空仓库（尚无 commit）允许继续；其他错误视为不可用。
        head_text = ""
    else:
        head_text = head.stdout.strip()

    tree_hash, tree_errors = _tree_fingerprint(path, excluded)
    if tree_hash is None:
        return {
            "available": False,
            "reason": "tree_walk_failed",
            "detail": tree_errors[0] if tree_errors else "unknown",
            "errors": tree_errors[:10],
            "exclude_dirs": sorted(excluded),
        }

    fingerprint = "\n".join([
        head_text, status.stdout.strip(), numstat.stdout.strip(), tree_hash,
    ])
    return {
        "available": True,
        "workspace": str(path.resolve()),
        "head": head_text,
        "status": status.stdout.strip(),
        "diff_stat": stat.stdout.strip(),
        "tree_hash": tree_hash,
        "fingerprint": hashlib.sha256(fingerprint.encode("utf-8", "replace")).hexdigest(),
        "exclude_dirs": sorted(excluded),
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
        excludes = ", ".join(before.get("exclude_dirs") or []) if before else ""
        return (
            "（协调器无法独立核对工作区："
            f"{reason}{'；排除目录：' + excludes if excludes else ''}）\n"
        )
    changed = workspace_changed(before, after)
    before_status = (before.get("status") or "").strip() or "clean"
    after_status = (after or {}).get("status") or ""
    after_status = after_status.strip() or "clean"
    excludes = ", ".join(before.get("exclude_dirs") or [])
    return (
        "【协调器实测工作区（独立证据）】\n"
        f"- 运行前 HEAD: {before.get('head') or '(no commit)'}\n"
        f"- 运行后 HEAD: {(after or {}).get('head') or '(no commit)'}\n"
        f"- 运行前 status: {before_status[:1200]}\n"
        f"- 运行后 status: {after_status[:1200]}\n"
        f"- 文件内容指纹（SHA-256）: {'已计算' if before.get('tree_hash') else '不可用'}\n"
        f"- 是否发生变化: {'是' if changed else ('否' if changed is False else '无法判断')}\n"
        f"- 未纳入目录: {excludes}\n"
    )
