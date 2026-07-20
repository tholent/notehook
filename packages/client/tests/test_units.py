"""Narrow unit tests: conflict naming, scanning, config round-trip."""

from pathlib import Path

from notehook_cli.config import ClientConfig
from notehook_cli.engine import conflict_copy_name
from notehook_cli.scan import file_md5, scan_local


def test_conflict_copy_name_with_extension() -> None:
    name = conflict_copy_name("Note/doc.note", "CLI-abc123")
    assert name.startswith("Note/doc (conflicted copy CLI-abc123 ")
    assert name.endswith(".note")


def test_conflict_copy_name_no_extension() -> None:
    name = conflict_copy_name("README", "CLI-x")
    assert name.startswith("README (conflicted copy CLI-x ")
    assert "." not in name.rsplit(")", 1)[1]


def test_conflict_copy_name_top_level() -> None:
    assert "/" not in conflict_copy_name("file.txt", "E")


def test_file_md5(tmp_path: Path) -> None:
    f = tmp_path / "x"
    f.write_bytes(b"")
    assert file_md5(f) == "d41d8cd98f00b204e9800998ecf8427e"


def test_scan_skips_hidden_and_tmp(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_bytes(b"k")
    (tmp_path / ".skip").write_bytes(b"s")
    (tmp_path / "part.notehook-tmp").write_bytes(b"p")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_bytes(b"n")
    result = scan_local(tmp_path)
    assert set(result) == {"keep.txt", "sub", "sub/nested.txt"}
    assert result["sub"].is_folder is True


def test_scan_missing_root(tmp_path: Path) -> None:
    assert scan_local(tmp_path / "nope") == {}


def test_config_round_trip(tmp_path: Path) -> None:
    cfg = ClientConfig.load(tmp_path / "cfg")
    generated_eq = cfg.equipment_no
    assert generated_eq.startswith("CLI-")
    cfg.server_url = "https://sync.example.com"
    cfg.account = "me@example.com"
    cfg.sync_root = tmp_path / "notes"
    cfg.conflict_policy = "newest-wins"
    cfg.save()

    again = ClientConfig.load(tmp_path / "cfg")
    assert again.server_url == "https://sync.example.com"
    assert again.account == "me@example.com"
    assert again.sync_root == tmp_path / "notes"
    assert again.conflict_policy == "newest-wins"
    assert again.equipment_no == generated_eq  # stable across loads


def test_token_storage(tmp_path: Path) -> None:
    cfg = ClientConfig.load(tmp_path / "cfg")
    assert cfg.load_token() is None
    cfg.save_token("tok-123")
    assert cfg.load_token() == "tok-123"
    assert (cfg.token_file.stat().st_mode & 0o777) == 0o600
