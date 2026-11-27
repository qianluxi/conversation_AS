from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

DEFAULT_EXCLUDE_DIRS = (
    ".git", "data", "logs", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", "node_modules", "dist", "build",
)
DEFAULT_MAX_FILES = 50_000
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_SCAN_TIMEOUT = 30.0


def _git(workspace: Path, args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
    )


def _tree_fingerprint(root: Path, exclude_dirs: set[str], *,
                      max_files: int, max_total_bytes: int,
                      max_file_bytes: int,
                      scan_timeout: float) -> tuple[str | None, list[str], dict[str, Any], str | None]:
    parts: list[str] = []
    errors: list[str] = []
    stats: dict[str, Any] = {"files": 0, "bytes": 0, "elapsed_seconds": 0.0}
    limit: str | None = None
    started = time.monotonic()

    def onerror(exc: OSError) -> None:
        errors.append(str(exc))

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        if time.monotonic() - started > scan_timeout:
            limit = "scan_timeout"
            break
        kept_dirs: list[str] = []
        for name in dirnames:
            path = Path(dirpath) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                try:
                    parts.append(f"L\0{relative}\0{os.readlink(path)}")
                except OSError as exc:
                    errors.append(f"{relative}: {exc}")
                continue
            if name not in exclude_dirs:
                kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in filenames:
            path = Path(dirpath) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                try:
                    parts.append(f"L\0{relative}\0{os.readlink(path)}")
                except OSError as exc:
                    errors.append(f"{relative}: {exc}")
                continue
            try:
                stat = path.stat()
                if stat.st_size > max_file_bytes:
                    limit = "file_too_large"
                    break
                if stats["files"] + 1 > max_files:
                    limit = "too_many_files"
                    break
                if stats["bytes"] + stat.st_size > max_total_bytes:
                    limit = "too_many_bytes"
                    break
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                parts.append(f"{relative}\0{stat.st_size}\0{digest.hexdigest()}")
                stats["files"] += 1
                stats["bytes"] += stat.st_size
            except OSError as exc:
                errors.append(f"{relative}: {exc}")
            if time.monotonic() - started > scan_timeout:
                limit = "scan_timeout"
                break
        if limit is not None:
            break

    stats["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if limit is not None:
        return None, errors, stats, limit
    if errors:
        return None, errors, stats, "tree_walk_failed"
    parts.sort()
    return (
        hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest(),
        errors, stats, None,
    )


def capture_workspace(workspace: str | Path | None,
                      exclude_dirs: list[str] | None = None, *,
                      max_files: int = DEFAULT_MAX_FILES,
                      max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
                      max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                      scan_timeout: float = DEFAULT_SCAN_TIMEOUT) -> dict[str, Any]:
    """采集工作区的独立证据（git 快照 + 文件内容指纹 + 扫描统计）。"""
    excluded = set(DEFAULT_EXCLUDE_DIRS) | set(exclude_dirs or [])
    base = {"exclude_dirs": sorted(excluded)}
    if not workspace:
        return {**base, "available": False, "reason": "no_workspace"}
    path = Path(workspace).expanduser()
    if not path.is_dir():
        return {**base, "available": False, "reason": "missing_directory"}
    if shutil.which("git") is None:
        return {**base, "available": False, "reason": "git_not_found"}
    try:
        inside = _git(path, ["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return {**base, "available": False, "reason": "not_git"}
        head = _git(path, ["rev-parse", "HEAD"])
        status = _git(path, ["status", "--porcelain=v1"])
        numstat = _git(path, ["diff", "--numstat"])
        stat = _git(path, ["diff", "--stat"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {**base, "available": False, "reason": str(exc)}

    if status.returncode != 0 or numstat.returncode != 0 or stat.returncode != 0:
        return {
            **base,
            "available": False,
            "reason": "git_command_failed",
            "detail": (status.stderr or numstat.stderr or stat.stderr).strip()[:500],
        }
    if head.returncode != 0:
        count = _git(path, ["rev-list", "--count", "--all"])
        if count.returncode == 0 and count.stdout.strip() == "0":
            head_text = ""
        else:
            return {
                **base,
                "available": False,
                "reason": "head_unresolved",
                "detail": (head.stderr or count.stderr).strip()[:500],
            }
    else:
        head_text = head.stdout.strip()

    tree_hash, tree_errors, stats, limit = _tree_fingerprint(
        path, excluded, max_files=max_files, max_total_bytes=max_total_bytes,
        max_file_bytes=max_file_bytes, scan_timeout=scan_timeout,
    )
    if tree_hash is None:
        reason = limit or "tree_walk_failed"
        return {
            **base,
            **stats,
            "available": False,
            "reason": reason,
            "detail": tree_errors[0] if tree_errors else reason,
            "errors": tree_errors[:10],
        }

    fingerprint = "\n".join([
        head_text, status.stdout.strip(), numstat.stdout.strip(), tree_hash,
    ])
    return {
        **base,
        **stats,
        "available": True,
        "workspace": str(path.resolve()),
        "head": head_text,
        "status": status.stdout.strip(),
        "diff_stat": stat.stdout.strip(),
        "tree_hash": tree_hash,
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
        f"- 扫描统计: {before.get('files', 0)} 个文件 / "
        f"{before.get('bytes', 0)} bytes / {before.get('elapsed_seconds', 0)}s\n"
        f"- 是否发生变化: {'是' if changed else ('否' if changed is False else '无法判断')}\n"
        f"- 未纳入目录: {excludes}\n"
    )
