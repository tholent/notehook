"""flock-based pass lock (spec §1 orphan recovery: one sync engine per config dir)."""

from pathlib import Path

import pytest

from notehook_cli.lock import LockError, file_lock


def test_lock_acquire_release_sequential(tmp_path: Path) -> None:
    lock_file = tmp_path / "events.db.lock"
    with file_lock(lock_file):
        pass
    # Released cleanly -- acquiring again must not block or raise.
    with file_lock(lock_file):
        pass


def test_lock_second_holder_fails_clearly(tmp_path: Path) -> None:
    lock_file = tmp_path / "events.db.lock"
    with file_lock(lock_file):
        with pytest.raises(LockError):
            with file_lock(lock_file, timeout_seconds=0.3, retry_interval=0.05):
                pass  # pragma: no cover - never reached


def test_lock_available_again_after_release(tmp_path: Path) -> None:
    lock_file = tmp_path / "events.db.lock"
    with file_lock(lock_file):
        pass
    # Now free -- a short timeout is still enough.
    with file_lock(lock_file, timeout_seconds=0.3, retry_interval=0.05):
        pass
