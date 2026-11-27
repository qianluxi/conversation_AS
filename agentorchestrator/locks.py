from __future__ import annotations

import os
import time
from pathlib import Path


class CrossProcessLockError(RuntimeError):
    """另一个进程持有同一个锁。"""


class CrossProcessFileLock:
    """跨进程文件锁（Windows msvcrt / POSIX flock），用于本地助理互斥。"""

    def __init__(self, path: str | Path, *, timeout: float = 0.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._fd: int | None = None

    @property
    def locked(self) -> bool:
        return self._fd is not None

    def _lock(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            try:
                self._lock(fd)
            except OSError as exc:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise CrossProcessLockError(
                        f"另一个协调器进程正在使用本地助理（锁文件：{self.path}）"
                    ) from exc
                time.sleep(0.05)
                continue
            self._fd = fd
            return

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            self._unlock(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "CrossProcessFileLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
