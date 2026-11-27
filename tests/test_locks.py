from __future__ import annotations

from pathlib import Path

import pytest

from agentorchestrator.locks import CrossProcessFileLock, CrossProcessLockError


def test_cross_process_lock_excludes_second_holder(tmp_path: Path) -> None:
    path = tmp_path / "assistant.lock"
    first = CrossProcessFileLock(path)
    second = CrossProcessFileLock(path)
    first.acquire()
    try:
        assert first.locked
        with pytest.raises(CrossProcessLockError):
            second.acquire()
        assert not second.locked
    finally:
        first.release()
    second.acquire()
    try:
        assert second.locked
    finally:
        second.release()


def test_cross_process_lock_can_be_reacquired(tmp_path: Path) -> None:
    lock = CrossProcessFileLock(tmp_path / "assistant.lock")
    lock.acquire()
    lock.release()
    lock.acquire()
    lock.release()
    assert not lock.locked
