"""Sync-engine event emission (spec workflow-spec.md §1) against the real server.

Full-pass matrix, no-op cases, the durability guarantees the settled-flag
design exists for, per-device attribution, and the pass lock.
"""

import os
from pathlib import Path

import httpx
import pytest

from notehook_cli.engine import SyncEngine
from notehook_cli.lock import LockError, file_lock
from notehook_cli.scan import file_md5
from tests.conftest import make_api, make_engine, make_engine_with_events

# --- full-pass matrix ---


def test_event_download_new(http: httpx.Client, tmp_path: Path, sync_root: Path) -> None:
    api = make_api(http, "CLI-dl-new")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)

    other = make_api(http, "SN-device-1")
    other_root = tmp_path / "other"
    (other_root / "Note").mkdir(parents=True)
    (other_root / "Note" / "device.note").write_bytes(b"from device")
    make_engine(other, tmp_path, other_root).run_once()

    result = engine.run_once()
    assert result.downloaded == ["Note/device.note"]

    events = event_log.all_events()
    assert len(events) == 1
    row = events[0]
    assert row.type == "created"
    assert row.rel_path == "Note/device.note"
    assert row.source == "sync-download"
    assert row.content_hash == file_md5(sync_root / "Note" / "device.note")
    assert row.size == len(b"from device")
    assert row.origin_equipment == "SN-device-1"
    assert row.settled is True
    assert row.sync_pass


def test_event_download_changed(http: httpx.Client, tmp_path: Path, sync_root: Path) -> None:
    api = make_api(http, "CLI-dl-chg")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)

    other = make_api(http, "SN-device-2")
    other_root = tmp_path / "other"
    other_root.mkdir(parents=True, exist_ok=True)
    (other_root / "shared.txt").write_bytes(b"v1")
    other_engine = make_engine(other, tmp_path, other_root)
    other_engine.run_once()
    engine.run_once()  # initial download, seeds item.known

    before_ids = {e.id for e in event_log.all_events()}
    (other_root / "shared.txt").write_bytes(b"v2 changed")
    other_engine.run_once()
    result = engine.run_once()
    assert result.downloaded == ["shared.txt"]

    new_events = [e for e in event_log.all_events() if e.id not in before_ids]
    assert len(new_events) == 1
    row = new_events[0]
    assert row.type == "updated"
    assert row.source == "sync-download"
    assert row.content_hash == file_md5(sync_root / "shared.txt")
    assert row.origin_equipment == "SN-device-2"
    assert row.settled is True


def test_event_upload_new(http: httpx.Client, tmp_path: Path, sync_root: Path) -> None:
    api = make_api(http, "CLI-up-new")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)
    f = sync_root / "doc.txt"
    f.write_bytes(b"hello")

    result = engine.run_once()
    assert result.uploaded == ["doc.txt"]

    events = event_log.all_events()
    assert len(events) == 1
    row = events[0]
    assert row.type == "created"
    assert row.rel_path == "doc.txt"
    assert row.source == "sync-upload"
    assert row.content_hash == file_md5(f)
    assert row.size == 5
    assert row.origin_equipment == "CLI-up-new"
    assert row.settled is True


def test_event_upload_changed(http: httpx.Client, tmp_path: Path, sync_root: Path) -> None:
    api = make_api(http, "CLI-up-chg")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)
    f = sync_root / "doc.txt"
    f.write_bytes(b"v1")
    engine.run_once()

    before_ids = {e.id for e in event_log.all_events()}
    f.write_bytes(b"v2 with more content")
    result = engine.run_once()
    assert result.uploaded == ["doc.txt"]

    new_events = [e for e in event_log.all_events() if e.id not in before_ids]
    assert len(new_events) == 1
    row = new_events[0]
    assert row.type == "updated"
    assert row.source == "sync-upload"
    assert row.content_hash == file_md5(f)
    assert row.origin_equipment == "CLI-up-chg"


def test_event_delete_local(http: httpx.Client, tmp_path: Path, sync_root: Path) -> None:
    api = make_api(http, "CLI-del-loc")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)
    f = sync_root / "gone.txt"
    f.write_bytes(b"here today")
    engine.run_once()

    other = make_api(http, "SN-device-3")
    other_root = tmp_path / "other"
    other_engine = make_engine(other, tmp_path, other_root)
    other_engine.run_once()
    (other_root / "gone.txt").unlink()
    other_engine.run_once()  # deletes on server

    before_ids = {e.id for e in event_log.all_events()}
    result = engine.run_once()
    assert result.deleted_local == ["gone.txt"]

    new_events = [e for e in event_log.all_events() if e.id not in before_ids]
    assert len(new_events) == 1
    row = new_events[0]
    assert row.type == "deleted"
    assert row.rel_path == "gone.txt"
    assert row.content_hash == ""
    assert row.size == 0
    assert row.settled is True


def test_event_delete_remote(http: httpx.Client, tmp_path: Path, sync_root: Path) -> None:
    api = make_api(http, "CLI-del-rem")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)
    f = sync_root / "temp.txt"
    f.write_bytes(b"delete me")
    engine.run_once()

    before_ids = {e.id for e in event_log.all_events()}
    f.unlink()
    result = engine.run_once()
    assert result.deleted_remote == ["temp.txt"]

    new_events = [e for e in event_log.all_events() if e.id not in before_ids]
    assert len(new_events) == 1
    row = new_events[0]
    assert row.type == "deleted"
    assert row.rel_path == "temp.txt"
    assert row.content_hash == ""
    assert row.size == 0
    assert row.settled is True


def test_event_conflict_keep_both(http: httpx.Client, tmp_path: Path, sync_root: Path) -> None:
    api = make_api(http, "CLI-conflict")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)
    f = sync_root / "clash.txt"
    f.write_bytes(b"base")
    engine.run_once()

    other = make_api(http, "SN-device-4")
    other_root = tmp_path / "other"
    other_engine = make_engine(other, tmp_path, other_root)
    other_engine.run_once()
    (other_root / "clash.txt").write_bytes(b"device edit")
    other_engine.run_once()

    before_ids = {e.id for e in event_log.all_events()}
    f.write_bytes(b"local edit")
    result = engine.run_once()
    assert result.conflicts == ["clash.txt"]

    new_events = [e for e in event_log.all_events() if e.id not in before_ids]
    # Exactly two events: the conflicted-copy upload and the original-name
    # download -- no double emission.
    assert len(new_events) == 2
    by_type = {e.rel_path: e for e in new_events}

    original = by_type["clash.txt"]
    assert original.type == "updated"
    assert original.source == "sync-download"
    assert original.origin_equipment == "SN-device-4"
    assert original.content_hash == file_md5(sync_root / "clash.txt")

    copy_rows = [e for e in new_events if e.rel_path != "clash.txt"]
    assert len(copy_rows) == 1
    copy = copy_rows[0]
    assert copy.rel_path.startswith("clash (conflicted copy")
    assert copy.type == "created"
    assert copy.source == "sync-upload"
    assert copy.origin_equipment == "CLI-conflict"

    # Both actions of one pass share a single sync_pass.
    assert original.sync_pass == copy.sync_pass


def test_events_share_one_sync_pass_per_run(
    http: httpx.Client, tmp_path: Path, sync_root: Path
) -> None:
    api = make_api(http, "CLI-onepass")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)
    (sync_root / "mine.txt").write_bytes(b"mine")
    engine.run_once()

    other = make_api(http, "SN-device-5")
    other_root = tmp_path / "other"
    other_engine = make_engine(other, tmp_path, other_root)
    other_engine.run_once()
    (other_root / "theirs.txt").write_bytes(b"theirs")
    other_engine.run_once()

    before_ids = {e.id for e in event_log.all_events()}
    (sync_root / "mine.txt").write_bytes(b"mine v2")
    engine.run_once()  # uploads mine.txt v2, downloads theirs.txt in one pass

    new_events = [e for e in event_log.all_events() if e.id not in before_ids]
    assert len(new_events) == 2
    assert len({e.sync_pass for e in new_events}) == 1


# --- no-op cases ---


def test_hash_equal_touch_emits_nothing(
    http: httpx.Client, tmp_path: Path, sync_root: Path
) -> None:
    api = make_api(http, "CLI-touch")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)
    f = sync_root / "doc.txt"
    f.write_bytes(b"unchanged content")
    engine.run_once()

    before_ids = {e.id for e in event_log.all_events()}
    # Touch: change mtime, keep content identical.
    stat = f.stat()
    os.utime(f, ns=(stat.st_atime_ns + 1_000_000_000, stat.st_mtime_ns + 1_000_000_000))
    result = engine.run_once()
    assert result.changed == 0

    new_events = [e for e in event_log.all_events() if e.id not in before_ids]
    assert new_events == []


def test_folder_create_emits_nothing(http: httpx.Client, tmp_path: Path, sync_root: Path) -> None:
    api = make_api(http, "CLI-folder-create")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)
    (sync_root / "Folder").mkdir()
    engine.run_once()
    assert event_log.all_events() == []


def test_folder_delete_remote_side_emits_nothing(
    http: httpx.Client, tmp_path: Path, sync_root: Path
) -> None:
    api = make_api(http, "CLI-folder-del-rem")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)
    (sync_root / "Folder").mkdir()
    engine.run_once()

    (sync_root / "Folder").rmdir()
    engine.run_once()  # local dir gone, remote still has it -> DELETE_REMOTE
    assert event_log.all_events() == []


def test_folder_delete_local_side_emits_nothing(
    http: httpx.Client, tmp_path: Path, sync_root: Path
) -> None:
    api = make_api(http, "CLI-folder-del-loc")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)
    (sync_root / "Folder").mkdir()
    engine.run_once()

    other = make_api(http, "SN-device-6")
    other_root = tmp_path / "other"
    other_engine = make_engine(other, tmp_path, other_root)
    other_engine.run_once()  # pulls Folder
    (other_root / "Folder").rmdir()
    other_engine.run_once()  # pushes delete to remote

    engine.run_once()  # local Folder deleted -> DELETE_LOCAL (dir branch)
    assert not (sync_root / "Folder").exists()
    assert event_log.all_events() == []


# --- durability (the reason this phase exists) ---


def test_durability_partial_pass_settles_completed_files(
    http: httpx.Client, tmp_path: Path, sync_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler raising partway through a pass must still leave events for
    the files that *did* complete, settled -- via the `finally` path."""
    api = make_api(http, "CLI-partial")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)

    other = make_api(http, "SN-device-7")
    other_root = tmp_path / "other"
    other_root.mkdir(parents=True, exist_ok=True)
    (other_root / "a.txt").write_bytes(b"A")
    (other_root / "b.txt").write_bytes(b"B")
    make_engine(other, tmp_path, other_root).run_once()

    original_download = SyncEngine._do_download
    calls = {"n": 0}

    def flaky_download(self: SyncEngine, item: object, result: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("network drop on file 2")
        original_download(self, item, result)  # type: ignore[arg-type]

    monkeypatch.setattr(SyncEngine, "_do_download", flaky_download)

    with pytest.raises(RuntimeError, match="network drop"):
        engine.run_once()

    events = event_log.all_events()
    # classify() processes DOWNLOAD items in rel_path order: a.txt then b.txt.
    assert len(events) == 1
    assert events[0].rel_path == "a.txt"
    assert events[0].settled is True


def test_durability_orphan_recovery_on_next_pass(
    http: httpx.Client, tmp_path: Path, sync_root: Path
) -> None:
    """A hard crash leaves settled=0 rows behind (the action executed, but
    the process died before settle_pass ran). The next run_once() must
    settle them as orphans, alongside settling its own pass."""
    api = make_api(http, "CLI-crash")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)

    orphan_id = event_log.append(
        "created", "orphan.txt", "deadbeef", 3, "sync-upload", api.equipment_no, "dead-pass"
    )

    (sync_root / "new.txt").write_bytes(b"fresh")
    result = engine.run_once()
    assert result.uploaded == ["new.txt"]

    all_rows = {e.id: e for e in event_log.all_events()}
    assert all_rows[orphan_id].settled is True
    assert all_rows[orphan_id].sync_pass == "dead-pass"

    new_rows = [e for e in all_rows.values() if e.rel_path == "new.txt"]
    assert len(new_rows) == 1
    assert new_rows[0].settled is True
    assert new_rows[0].sync_pass != "dead-pass"


# --- two-equipment attribution ---


def test_two_equipment_attribution(http: httpx.Client, tmp_path: Path, sync_root: Path) -> None:
    device_a = make_api(http, "SN-A")
    device_a_root = tmp_path / "device-a"
    device_a_root.mkdir(parents=True, exist_ok=True)
    (device_a_root / "note.txt").write_bytes(b"from A")
    make_engine(device_a, tmp_path, device_a_root).run_once()

    client_b = make_api(http, "CLI-B")
    engine_b, event_log_b = make_engine_with_events(client_b, tmp_path, sync_root)
    result = engine_b.run_once()
    assert result.downloaded == ["note.txt"]

    events = event_log_b.all_events()
    assert len(events) == 1
    assert events[0].type == "created"
    assert events[0].origin_equipment == "SN-A"

    # B's own upload carries B's own equipment_no.
    before_ids = {e.id for e in event_log_b.all_events()}
    (sync_root / "mine.txt").write_bytes(b"from B")
    engine_b.run_once()
    new_events = [e for e in event_log_b.all_events() if e.id not in before_ids]
    assert len(new_events) == 1
    assert new_events[0].origin_equipment == "CLI-B"


# --- lock ---


def test_engine_run_once_fails_clearly_when_lock_held(
    http: httpx.Client, tmp_path: Path, sync_root: Path
) -> None:
    """Spec §1: one sync engine per config dir at a time. A concurrent pass
    (e.g. the daemon and a one-shot `notehook sync` racing) must fail
    clearly rather than corrupt the event log."""
    api = make_api(http, "CLI-locked")
    engine, event_log = make_engine_with_events(api, tmp_path, sync_root)
    lock_path = tmp_path / f"events-{api.equipment_no}.db.lock"
    (sync_root / "f.txt").write_bytes(b"x")

    with file_lock(lock_path):
        with pytest.raises(LockError):
            engine.run_once()

    assert event_log.all_events() == []
