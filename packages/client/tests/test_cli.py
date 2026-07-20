"""CLI command tests, routing httpx at the in-process server app."""

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from notehook_cli import cli
from notehook_cli.config import ClientConfig
from notehook_server.config import Settings
from notehook_server.main import create_app
from tests.conftest import TEST_ACCOUNT, TEST_PASSWORD

runner = CliRunner()


@pytest.fixture
def patched_http(server_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = create_app(server_settings)
    client = TestClient(app)

    def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:
        return client

    monkeypatch.setattr(cli.httpx, "Client", fake_client)
    return client


def _init(tmp_path: Path) -> Path:
    config_dir = tmp_path / "cfg"
    result = runner.invoke(
        cli.app,
        [
            "init",
            "--server",
            "http://testserver",
            "--account",
            TEST_ACCOUNT,
            "--dir",
            str(tmp_path / "notes"),
            "--config-dir",
            str(config_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    return config_dir


def test_init_writes_config(tmp_path: Path) -> None:
    config_dir = _init(tmp_path)
    cfg = ClientConfig.load(config_dir)
    assert cfg.account == TEST_ACCOUNT
    assert cfg.equipment_no.startswith("CLI-")


def test_init_rejects_bad_policy(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        [
            "init",
            "--server",
            "http://x",
            "--account",
            "a@b.c",
            "--dir",
            str(tmp_path),
            "--conflict-policy",
            "coin-flip",
            "--config-dir",
            str(tmp_path / "cfg"),
        ],
    )
    assert result.exit_code == 2


def test_login_and_sync_and_status(tmp_path: Path, patched_http: TestClient) -> None:
    config_dir = _init(tmp_path)

    result = runner.invoke(
        cli.app,
        ["login", "--config-dir", str(config_dir), "--password-stdin"],
        input=TEST_PASSWORD + "\n",
    )
    assert result.exit_code == 0, result.output
    assert "Logged in" in result.output

    notes = tmp_path / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "first.note").write_bytes(b"hello")
    result = runner.invoke(cli.app, ["sync", "--config-dir", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert "first.note" in result.output

    result = runner.invoke(cli.app, ["sync", "--config-dir", str(config_dir)])
    assert "up to date" in result.output

    result = runner.invoke(cli.app, ["status", "--config-dir", str(config_dir)])
    assert result.exit_code == 0
    assert "yes" in result.output


def test_login_wrong_password(tmp_path: Path, patched_http: TestClient) -> None:
    config_dir = _init(tmp_path)
    result = runner.invoke(
        cli.app,
        ["login", "--config-dir", str(config_dir), "--password-stdin"],
        input="wrong password\n",
    )
    assert result.exit_code == 1
    assert "Login failed" in result.output


def test_sync_without_login(tmp_path: Path, patched_http: TestClient) -> None:
    config_dir = _init(tmp_path)
    result = runner.invoke(cli.app, ["sync", "--config-dir", str(config_dir)])
    assert result.exit_code == 2
    assert "Not logged in" in result.output


def test_login_without_init(tmp_path: Path, patched_http: TestClient) -> None:
    result = runner.invoke(
        cli.app,
        ["login", "--config-dir", str(tmp_path / "empty"), "--password-stdin"],
        input="x\n",
    )
    assert result.exit_code == 2
    assert "No account configured" in result.output
