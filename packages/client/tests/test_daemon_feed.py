"""Change-feed long-poll trigger (workflow-spec.md §7, Phase 6).

Client tests run against the real server app in-process; a dedicated
SupernoteApiClient (sharing the engine's equipment_no but its own connection)
plays the daemon's feed client, matching how notehook_cli.cli wires it.
"""

import logging
import threading
import time
from pathlib import Path

import httpx
import pytest

from notehook_cli.api_client import ApiError, EndpointUnsupported, SupernoteApiClient
from notehook_cli.daemon import SyncDaemon, _has_foreign_change
from notehook_cli.engine import SyncResult
from tests.conftest import make_api, make_engine


def _wait_for(predicate: "callable[[], bool]", timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _feed_client(http: httpx.Client, equipment_no: str) -> SupernoteApiClient:
    """A second, independently-authenticated connection for the same
    equipment_no as the daemon's own engine — mirrors cli.py's wiring of a
    dedicated httpx.Client for the long-poll."""
    return make_api(http, equipment_no)


def test_feed_row_filtering_ignores_own_equipment() -> None:
    """Pure unit test of the echo-suppression rule (spec §7): a client's own
    upload rows must never be treated as a wake trigger."""
    own = "CLI-self"
    assert not _has_foreign_change([], own)
    assert not _has_foreign_change([{"equipment_no": own}], own)
    assert not _has_foreign_change([{"equipment_no": own}, {"equipment_no": own}], own)
    assert _has_foreign_change([{"equipment_no": own}, {"equipment_no": "SN-other"}], own)
    assert _has_foreign_change([{"equipment_no": "SN-other"}], own)


def test_feed_wakes_daemon_on_remote_change(
    http: httpx.Client, sync_root: Path, tmp_path: Path
) -> None:
    """A device-side upload should wake the daemon via the change feed, not
    the poll timer: poll_interval is set huge so the timer cannot be the
    trigger and the file still shows up within a short bounded wait."""
    api = make_api(http, "CLI-feedwake")
    engine = make_engine(api, tmp_path, sync_root)
    # Seed the server's change log *before* the daemon starts: since=0 is
    # always bootstrap semantics (spec §7 — no wait, no rows), so the feed
    # can only genuinely long-poll once the cursor has left 0. Real installs
    # clear this the moment anything first syncs; do it explicitly here so
    # the test exercises the steady-state long-poll rather than racing the
    # one-time "nothing has ever changed yet" warm-up.
    (sync_root / "seed.note").write_bytes(b"seed")
    engine.run_once()

    feed_api = _feed_client(http, "CLI-feedwake")
    results: list[SyncResult] = []
    daemon = SyncDaemon(
        engine,
        poll_interval_seconds=3600,
        on_result=results.append,
        feed_api=feed_api,
    )

    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: len(results) >= 1)  # initial pass + feed bootstrap
        time.sleep(0.3)  # let the feed's bootstrap long-poll request land

        other = make_api(http, "SN-device")
        other_root = tmp_path / "other"
        other_root.mkdir()
        (other_root / "pushed.note").write_bytes(b"pushed by device")
        make_engine(other, tmp_path, other_root).run_once()

        assert _wait_for(
            lambda: any("pushed.note" in r.downloaded for r in results), timeout=15.0
        ), f"results: {[r.downloaded for r in results]}"
        assert (sync_root / "pushed.note").read_bytes() == b"pushed by device"
    finally:
        daemon.stop()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_feed_endpoint_absent_falls_back_to_poll(
    http: httpx.Client,
    sync_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A server without /api/notehook/changes must not break sync: the feed
    thread logs once and exits permanently; the poll timer keeps working."""
    api = make_api(http, "CLI-nofeed")
    engine = make_engine(api, tmp_path, sync_root)
    feed_api = _feed_client(http, "CLI-nofeed")

    def _unsupported(*_args: object, **_kwargs: object) -> tuple[int, list[dict[str, object]]]:
        raise EndpointUnsupported("9999", "not implemented")

    monkeypatch.setattr(feed_api, "changes", _unsupported)

    results: list[SyncResult] = []
    daemon = SyncDaemon(
        engine,
        poll_interval_seconds=1,
        on_result=results.append,
        feed_api=feed_api,
    )

    other = make_api(http, "SN-writer2")
    other_root = tmp_path / "other2"
    other_root.mkdir()
    (other_root / "remote.note").write_bytes(b"pushed by device")
    make_engine(other, tmp_path, other_root).run_once()

    with caplog.at_level(logging.INFO, logger="notehook_cli.daemon"):
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()
        try:
            # Sync still happens via the plain poll timer.
            assert _wait_for(lambda: any("remote.note" in r.downloaded for r in results))
        finally:
            daemon.stop()
            thread.join(timeout=10)
        assert not thread.is_alive()

    unsupported_logs = [r for r in caplog.records if "change-feed endpoint" in r.message]
    assert len(unsupported_logs) == 1


def test_feed_transport_errors_back_off_and_keep_retrying(
    sync_root: Path, tmp_path: Path
) -> None:
    """Transport/ApiErrors (not EndpointUnsupported) must not disable the
    feed permanently — the thread backs off and keeps trying."""

    class FlakyFeed:
        equipment_no = "CLI-flaky"
        calls = 0

        def changes(
            self, since: int, limit: int = 500, wait_seconds: int = 0
        ) -> tuple[int, list[dict[str, object]]]:
            FlakyFeed.calls += 1
            if FlakyFeed.calls < 3:
                raise ApiError("0001", "temporary failure")
            return since + 1, []

    class StubEngine:
        root = sync_root

        def run_once(self) -> SyncResult:
            return SyncResult()

    daemon = SyncDaemon(
        StubEngine(),  # type: ignore[arg-type]
        poll_interval_seconds=3600,
        feed_api=FlakyFeed(),  # type: ignore[arg-type]
    )
    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: FlakyFeed.calls >= 3, timeout=10.0)
    finally:
        daemon.stop()
        thread.join(timeout=10)
    assert not thread.is_alive()
