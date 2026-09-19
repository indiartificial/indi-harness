"""
locking.py

A single-instance run lock that works on Windows, macOS and Linux.

The previous version imported `fcntl` at module scope, which does not
exist on Windows — the script failed at import with ModuleNotFoundError
before doing anything at all.

Both backends use a real OS-level lock rather than a "does the lock file
exist?" check, which matters: an OS lock is released automatically when
the process dies, so a crashed or killed run doesn't leave a stale file
that blocks every subsequent run until someone deletes it by hand.

  POSIX   -> fcntl.flock, whole file, exclusive, non-blocking
  Windows -> msvcrt.locking, one byte at offset 0, non-blocking
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    import msvcrt

    def _try_lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    _BUSY_ERRORS = (OSError,)
else:
    import fcntl

    def _try_lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    _BUSY_ERRORS = (BlockingIOError, OSError)


class LockBusy(RuntimeError):
    """Another process already holds the lock."""


class FileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_RDWR|O_CREAT rather than mode "w": truncating the file while
        # another process holds a lock on it would be rude and, on Windows,
        # would fail anyway.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(fd, "r+")
        try:
            _try_lock(handle)
        except _BUSY_ERRORS as exc:
            handle.close()
            raise LockBusy(f"lock held by another process ({exc})") from exc

        # Record the PID for anyone looking at the file, then return the
        # cursor to 0 so the Windows unlock covers the same byte we locked.
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except OSError:
            pass
        handle.seek(0)

        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        except OSError:
            pass
        try:
            self._handle.close()
        except OSError:
            pass
        self._handle = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc_info) -> None:
        self.release()
