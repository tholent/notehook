"""Client tests run against the real FastAPI server app in-process."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from notehook_cli.api_client import SupernoteApiClient
from notehook_cli.engine import SyncEngine
from notehook_cli.state_db import StateDB
from notehook_cli.workflows.events import EventLog
from notehook_protocol.crypto import password_md5
from notehook_server.config import Settings
from notehook_server.main import create_app

TEST_PASSWORD = "hunter2 hunter2"
TEST_ACCOUNT = "chris@example.com"


@pytest.fixture
def server_settings(tmp_path: Path) -> Settings:
    return Settings(
        account=TEST_ACCOUNT,
        password_md5=password_md5(TEST_PASSWORD),
        base_url="http://testserver",
        data_dir=tmp_path / "server-data",
        database_url=f"sqlite:///{tmp_path / 'server.db'}",
    )


@pytest.fixture
def http(server_settings: Settings) -> httpx.Client:
    # TestClient is an httpx.Client subclass, so the api client can use it directly.
    return TestClient(create_app(server_settings))


def make_api(http: httpx.Client, equipment_no: str = "CLI-test0001") -> SupernoteApiClient:
    api = SupernoteApiClient(http, equipment_no)
    api.login(TEST_ACCOUNT, TEST_PASSWORD)
    return api


@pytest.fixture
def api(http: httpx.Client) -> SupernoteApiClient:
    return make_api(http)


@pytest.fixture
def sync_root(tmp_path: Path) -> Path:
    root = tmp_path / "local"
    root.mkdir()
    return root


def make_engine(
    api: SupernoteApiClient, tmp_path: Path, sync_root: Path, policy: str = "keep-both"
) -> SyncEngine:
    state = StateDB(tmp_path / f"state-{api.equipment_no}.db")
    return SyncEngine(api, state, sync_root, conflict_policy=policy)


@pytest.fixture
def engine(api: SupernoteApiClient, tmp_path: Path, sync_root: Path) -> SyncEngine:
    return make_engine(api, tmp_path, sync_root)


def make_engine_with_events(
    api: SupernoteApiClient, tmp_path: Path, sync_root: Path, policy: str = "keep-both"
) -> tuple[SyncEngine, EventLog]:
    """Like make_engine, but wires an EventLog + lock file (Phase 2 emission)."""
    state = StateDB(tmp_path / f"state-{api.equipment_no}.db")
    event_log = EventLog(tmp_path / f"events-{api.equipment_no}.db")
    lock_file = tmp_path / f"events-{api.equipment_no}.db.lock"
    engine = SyncEngine(
        api,
        state,
        sync_root,
        conflict_policy=policy,
        event_log=event_log,
        lock_file=lock_file,
    )
    return engine, event_log
