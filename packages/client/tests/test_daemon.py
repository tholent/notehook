"""Daemon loop: initial pass, FS-event trigger, poll trigger, clean stop."""

import threading
import time
from pathlib import Path

import httpx

from noted_cli.daemon import SyncDaemon
from noted_cli.engine import SyncResult
from tests.conftest import make_api, make_engine


def _wait_for(predicate: "callable[[], bool]", timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_daemon_syncs_on_fs_change(
    http: httpx.Client, sync_root: Path, tmp_path: Path
) -> None:
    api = make_api(http, "CLI-daemon")
    engine = make_engine(api, tmp_path, sync_root)
    results: list[SyncResult] = []
    daemon = SyncDaemon(engine, poll_interval_seconds=60, on_result=results.append)

    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: len(results) >= 1)  # initial pass

        (sync_root / "new.note").write_bytes(b"created while running")
        assert _wait_for(
            lambda: any("new.note" in r.uploaded for r in results)
        ), f"results: {[r.uploaded for r in results]}"
    finally:
        daemon.stop()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_daemon_polls_remote(http: httpx.Client, sync_root: Path, tmp_path: Path) -> None:
    api = make_api(http, "CLI-poller")
    engine = make_engine(api, tmp_path, sync_root)
    results: list[SyncResult] = []
    daemon = SyncDaemon(engine, poll_interval_seconds=1, on_result=results.append)

    other = make_api(http, "SN-writer")
    other_root = tmp_path / "other"
    (other_root).mkdir()
    (other_root / "remote.note").write_bytes(b"pushed by device")
    make_engine(other, tmp_path, other_root).run_once()

    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: any("remote.note" in r.downloaded for r in results))
        assert (sync_root / "remote.note").read_bytes() == b"pushed by device"
    finally:
        daemon.stop()
        thread.join(timeout=10)


def test_daemon_survives_sync_errors(sync_root: Path, tmp_path: Path) -> None:
    class ExplodingEngine:
        root = sync_root
        calls = 0

        def run_once(self) -> SyncResult:
            ExplodingEngine.calls += 1
            raise RuntimeError("server unreachable")

    daemon = SyncDaemon(ExplodingEngine(), poll_interval_seconds=1)  # type: ignore[arg-type]
    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: ExplodingEngine.calls >= 2)  # kept retrying
    finally:
        daemon.stop()
        thread.join(timeout=10)
    assert not thread.is_alive()
