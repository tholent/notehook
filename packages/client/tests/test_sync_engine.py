"""Bidirectional sync scenarios against the real server, in-process."""

from pathlib import Path

import httpx

from noted_cli.engine import SyncEngine
from tests.conftest import make_api, make_engine


def test_initial_upload(engine: SyncEngine, sync_root: Path) -> None:
    (sync_root / "Note").mkdir()
    (sync_root / "Note" / "a.note").write_bytes(b"note a")
    (sync_root / "readme.txt").write_bytes(b"top-level file")

    result = engine.run_once()
    assert sorted(result.uploaded) == ["Note/a.note", "readme.txt"]
    assert result.conflicts == []

    # Second pass is a no-op.
    result2 = engine.run_once()
    assert result2.changed == 0


def test_download_from_server(
    http: httpx.Client, engine: SyncEngine, sync_root: Path, tmp_path: Path
) -> None:
    # Another equipment (the "device") uploads first.
    other = make_api(http, "SN99999999")
    other_root = tmp_path / "other"
    (other_root / "Note").mkdir(parents=True)
    (other_root / "Note" / "device.note").write_bytes(b"from device")
    make_engine(other, tmp_path, other_root).run_once()

    result = engine.run_once()
    assert result.downloaded == ["Note/device.note"]
    assert (sync_root / "Note" / "device.note").read_bytes() == b"from device"


def test_local_edit_propagates(engine: SyncEngine, sync_root: Path) -> None:
    f = sync_root / "doc.txt"
    f.write_bytes(b"v1")
    engine.run_once()
    f.write_bytes(b"v2 with more content")
    result = engine.run_once()
    assert result.uploaded == ["doc.txt"]


def test_remote_edit_propagates(
    http: httpx.Client, engine: SyncEngine, sync_root: Path, tmp_path: Path
) -> None:
    f = sync_root / "shared.txt"
    f.write_bytes(b"v1")
    engine.run_once()

    other = make_api(http, "SN2")
    other_root = tmp_path / "other"
    other_engine = make_engine(other, tmp_path, other_root)
    other_engine.run_once()  # pulls shared.txt
    (other_root / "shared.txt").write_bytes(b"v2 from device")
    other_engine.run_once()  # pushes edit

    result = engine.run_once()
    assert result.downloaded == ["shared.txt"]
    assert f.read_bytes() == b"v2 from device"


def test_local_delete_propagates(engine: SyncEngine, sync_root: Path) -> None:
    f = sync_root / "temp.txt"
    f.write_bytes(b"delete me")
    engine.run_once()
    f.unlink()
    result = engine.run_once()
    assert result.deleted_remote == ["temp.txt"]
    # And it doesn't come back on the next pass.
    result2 = engine.run_once()
    assert result2.changed == 0
    assert not f.exists()


def test_remote_delete_propagates(
    http: httpx.Client, engine: SyncEngine, sync_root: Path, tmp_path: Path
) -> None:
    f = sync_root / "gone.txt"
    f.write_bytes(b"here today")
    engine.run_once()

    other = make_api(http, "SN2")
    other_root = tmp_path / "other"
    other_engine = make_engine(other, tmp_path, other_root)
    other_engine.run_once()
    (other_root / "gone.txt").unlink()
    other_engine.run_once()  # deletes on server

    result = engine.run_once()
    assert result.deleted_local == ["gone.txt"]
    assert not f.exists()


def test_local_edit_beats_remote_delete(
    http: httpx.Client, engine: SyncEngine, sync_root: Path, tmp_path: Path
) -> None:
    f = sync_root / "precious.txt"
    f.write_bytes(b"v1")
    engine.run_once()

    other = make_api(http, "SN2")
    other_root = tmp_path / "other"
    other_engine = make_engine(other, tmp_path, other_root)
    other_engine.run_once()
    (other_root / "precious.txt").unlink()
    other_engine.run_once()

    f.write_bytes(b"v2 edited after remote delete")
    result = engine.run_once()
    # Data safety: the edit survives and is re-uploaded, not deleted.
    assert result.uploaded == ["precious.txt"]
    assert f.exists()


def test_conflict_keep_both(
    http: httpx.Client, engine: SyncEngine, sync_root: Path, tmp_path: Path
) -> None:
    f = sync_root / "clash.txt"
    f.write_bytes(b"base")
    engine.run_once()

    other = make_api(http, "SN2")
    other_root = tmp_path / "other"
    other_engine = make_engine(other, tmp_path, other_root)
    other_engine.run_once()
    (other_root / "clash.txt").write_bytes(b"device edit")
    other_engine.run_once()

    f.write_bytes(b"local edit")
    result = engine.run_once()

    assert result.conflicts == ["clash.txt"]
    # Original name now holds the remote version; the local edit is preserved
    # as a conflicted copy — nothing was silently lost.
    assert f.read_bytes() == b"device edit"
    copies = list(sync_root.glob("clash (conflicted copy *"))
    assert len(copies) == 1
    assert copies[0].read_bytes() == b"local edit"

    # The conflicted copy also made it to the server.
    remote_names = {e.name for e in engine._api.list_all()}
    assert any(n and n.startswith("clash (conflicted copy") for n in remote_names)


def test_conflict_newest_wins_local(
    http: httpx.Client, sync_root: Path, tmp_path: Path
) -> None:
    api = make_api(http, "CLI-newest")
    engine = make_engine(api, tmp_path, sync_root, policy="newest-wins")
    f = sync_root / "n.txt"
    f.write_bytes(b"base")
    engine.run_once()

    other = make_api(http, "SN2")
    other_root = tmp_path / "other"
    other_engine = make_engine(other, tmp_path, other_root)
    other_engine.run_once()
    (other_root / "n.txt").write_bytes(b"older remote edit")
    other_engine.run_once()

    f.write_bytes(b"newer local edit")  # mtime is now >= remote lastUpdateTime
    result = engine.run_once()
    assert result.conflicts == ["n.txt"]
    assert result.uploaded == ["n.txt"]
    assert f.read_bytes() == b"newer local edit"


def test_conflict_remote_wins(http: httpx.Client, sync_root: Path, tmp_path: Path) -> None:
    api = make_api(http, "CLI-rw")
    engine = make_engine(api, tmp_path, sync_root, policy="remote-wins")
    f = sync_root / "r.txt"
    f.write_bytes(b"base")
    engine.run_once()

    other = make_api(http, "SN2")
    other_root = tmp_path / "other"
    other_engine = make_engine(other, tmp_path, other_root)
    other_engine.run_once()
    (other_root / "r.txt").write_bytes(b"remote version")
    other_engine.run_once()

    f.write_bytes(b"local version")
    result = engine.run_once()
    assert f.read_bytes() == b"remote version"
    assert result.downloaded == ["r.txt"]


def test_empty_folders_sync_both_ways(
    http: httpx.Client, engine: SyncEngine, sync_root: Path, tmp_path: Path
) -> None:
    (sync_root / "EmptyLocal").mkdir()
    engine.run_once()

    other = make_api(http, "SN2")
    other_root = tmp_path / "other"
    other_engine = make_engine(other, tmp_path, other_root)
    other.create_folder("/EmptyRemote")
    other_engine.run_once()
    assert (other_root / "EmptyLocal").is_dir()

    engine.run_once()
    assert (sync_root / "EmptyRemote").is_dir()


def test_hidden_files_ignored(engine: SyncEngine, sync_root: Path) -> None:
    (sync_root / ".hidden").write_bytes(b"secret")
    (sync_root / ".hiddendir").mkdir()
    (sync_root / ".hiddendir" / "inner.txt").write_bytes(b"nested")
    result = engine.run_once()
    assert result.changed == 0
