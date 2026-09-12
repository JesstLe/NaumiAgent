"""Small cross-process directory lock for runtime initialization."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import psutil


class InterprocessLockTimeoutError(TimeoutError):
    """Raised when another live process retains the lock beyond the deadline."""


class InterprocessDirectoryLock:
    """Coordinate short critical sections without platform-specific file APIs."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = 120.0,
        poll_seconds: float = 0.05,
    ) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self._held = False

    def __enter__(self) -> InterprocessDirectoryLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.path.mkdir()
                (self.path / "owner").write_text(
                    f"{os.getpid()}\n{time.time()}\n",
                    encoding="utf-8",
                )
                self._held = True
                return
            except FileExistsError:
                self._recover_abandoned_lock()
                if time.monotonic() >= deadline:
                    raise InterprocessLockTimeoutError(
                        f"等待运行时初始化锁超时：{self.path}"
                    )
                time.sleep(self.poll_seconds)

    def release(self) -> None:
        if not self._held:
            return
        owner_path = self.path / "owner"
        for attempt in range(100):
            try:
                owner_path.unlink(missing_ok=True)
                self.path.rmdir()
                self._held = False
                return
            except FileNotFoundError:
                self._held = False
                return
            except OSError:
                if attempt == 99:
                    raise
                time.sleep(0.01)

    def _recover_abandoned_lock(self) -> None:
        owner_path = self.path / "owner"
        try:
            lines = owner_path.read_text(encoding="utf-8").splitlines()
            owner_pid = int(lines[0])
        except FileNotFoundError:
            try:
                if time.time() - self.path.stat().st_mtime > 1.0:
                    shutil.rmtree(self.path)
            except (FileNotFoundError, OSError):
                pass
            return
        except (OSError, ValueError, IndexError):
            return
        if psutil.pid_exists(owner_pid):
            return
        try:
            shutil.rmtree(self.path)
        except (FileNotFoundError, OSError):
            pass
