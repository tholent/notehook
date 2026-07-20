"""EventLog: schema, append/settle semantics, concurrency (spec §1, decision D1)."""

import threading
from pathlib import Path

from notehook_cli.workflows.events import EventLog


def test_schema_created_idempotently(tmp_path: Path) -> None:
    db_file = tmp_path / "events.db"
    EventLog(db_file)
    # Re-opening must not fail (CREATE TABLE IF NOT EXISTS, migration-free).
    log2 = EventLog(db_file)
    assert log2.all_events() == []


def test_append_defaults_unsettled(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    log.append("created", "a.txt", "abc123", 3, "sync-upload", "CLI-1", "pass-1")
    rows = log.all_events()
    assert len(rows) == 1
    row = rows[0]
    assert row.type == "created"
    assert row.rel_path == "a.txt"
    assert row.content_hash == "abc123"
    assert row.size == 3
    assert row.source == "sync-upload"
    assert row.origin_equipment == "CLI-1"
    assert row.sync_pass == "pass-1"
    assert row.settled is False
    assert row.target_install == ""
    assert row.created_at > 0


def test_settle_pass_only_settles_matching_pass(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    log.append("created", "a.txt", "h1", 1, "sync-upload", "CLI-1", "pass-1")
    log.append("created", "b.txt", "h2", 1, "sync-upload", "CLI-1", "pass-2")
    log.settle_pass("pass-1")
    rows = {r.rel_path: r for r in log.all_events()}
    assert rows["a.txt"].settled is True
    assert rows["b.txt"].settled is False


def test_settle_orphans_settles_everything_unsettled(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    log.append("created", "a.txt", "h1", 1, "sync-upload", "CLI-1", "dead-pass-1")
    log.append("created", "b.txt", "h2", 1, "sync-upload", "CLI-1", "dead-pass-2")
    log.settle_orphans()
    assert all(r.settled for r in log.all_events())


def test_append_settled_is_settled_at_insert(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    log.append_settled(
        "created", "backfilled.txt", "h1", 1, "backfill", "", "backfill-pass", target_install="x4"
    )
    row = log.all_events()[0]
    assert row.settled is True
    assert row.source == "backfill"
    assert row.target_install == "x4"


def test_events_since_returns_only_newer_rows(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    first_id = log.append("created", "a.txt", "h1", 1, "sync-upload", "CLI-1", "p1")
    log.append("created", "b.txt", "h2", 1, "sync-upload", "CLI-1", "p1")
    newer = log.events_since(first_id)
    assert [r.rel_path for r in newer] == ["b.txt"]


def test_concurrent_writers_all_land(tmp_path: Path) -> None:
    """Two threads appending to one EventLog: all rows land, busy_timeout
    absorbs SQLITE_BUSY instead of it escaping as an error."""
    log = EventLog(tmp_path / "events.db")
    errors: list[Exception] = []

    def writer(prefix: str, count: int) -> None:
        try:
            for i in range(count):
                log.append(
                    "created",
                    f"{prefix}-{i}.txt",
                    "h",
                    1,
                    "sync-upload",
                    "CLI-1",
                    f"{prefix}-pass",
                )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=("daemon", 40)),
        threading.Thread(target=writer, args=("backfill", 40)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(log.all_events()) == 80
