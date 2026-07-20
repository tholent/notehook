"""POSIX file lock: one sync engine per config dir at a time (spec §1).

`fcntl.flock`-based, exclusive, non-blocking with a short retry loop before
giving up with a clear error. No Windows shims — the daemon story is already
POSIX-only (see docs/workflow-implementation-plan.md, "Risks / watch items").
"""

import fcntl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

_RETRY_INTERVAL_SECONDS = 0.1


class LockError(RuntimeError):
    """Raised when the lock could not be acquired before the deadline."""


@contextmanager
def file_lock(
    lock_file: Path,
    *,
    timeout_seconds: float = 2.0,
    retry_interval: float = _RETRY_INTERVAL_SECONDS,
) -> Iterator[None]:
    """Hold an exclusive `flock` on `lock_file` for the duration of the block.

    Non-blocking under the hood (`LOCK_EX | LOCK_NB`), retried at
    `retry_interval` until `timeout_seconds` elapses, at which point
    `LockError` is raised — e.g. another sync pass (daemon or a concurrent
    one-shot `notehook sync`) is already running.
    """
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh: IO[str] = lock_file.open("a+")
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockError(
                        f"could not acquire lock {lock_file} "
                        f"(another notehook process is already syncing this config dir)"
                    ) from None
                time.sleep(retry_interval)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        fh.close()
