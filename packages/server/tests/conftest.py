from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notehook_protocol.crypto import login_hash_sha256, password_md5
from notehook_server.config import Settings
from notehook_server.main import create_app

TEST_PASSWORD = "correct horse battery staple"
TEST_ACCOUNT = "chris@example.com"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        account=TEST_ACCOUNT,
        password_md5=password_md5(TEST_PASSWORD),
        user_name="Chris",
        base_url="http://testserver",
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        debug_capture=True,
        login_attempts_per_minute=100,  # rate-limit tests set their own budget
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def do_login(client: TestClient, equipment_no: str = "SN12345678") -> str:
    """Run the real random-code + login flow; returns the access token."""
    rc_resp = client.post(
        "/api/official/user/query/random/code", json={"account": TEST_ACCOUNT}
    )
    assert rc_resp.json()["success"], rc_resp.text
    random_code = rc_resp.json()["randomCode"]
    password = login_hash_sha256(password_md5(TEST_PASSWORD), random_code)
    login_resp = client.post(
        "/api/official/user/account/login/equipment",
        json={
            "password": password,
            "account": TEST_ACCOUNT,
            "equipment": 3,
            "loginMethod": "2",
            "equipmentNo": equipment_no,
        },
    )
    body = login_resp.json()
    assert body["success"], login_resp.text
    token: str = body["token"]
    return token


@pytest.fixture
def token(client: TestClient) -> str:
    return do_login(client)


@pytest.fixture
def authed(client: TestClient, token: str) -> TestClient:
    client.headers["x-access-token"] = token
    return client
