from fastapi.testclient import TestClient

from noted_protocol.crypto import login_hash_md5, login_hash_sha256, password_md5
from tests.conftest import TEST_ACCOUNT, TEST_PASSWORD, do_login


def _get_random_code(client: TestClient) -> str:
    resp = client.post("/api/official/user/query/random/code", json={"account": TEST_ACCOUNT})
    code: str = resp.json()["randomCode"]
    return code


def test_ping_no_auth(client: TestClient) -> None:
    resp = client.get("/api/file/query/server")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_login_sha256_scheme(client: TestClient) -> None:
    token = do_login(client)
    assert token


def test_login_md5_scheme(client: TestClient) -> None:
    rc = _get_random_code(client)
    password = login_hash_md5(password_md5(TEST_PASSWORD), rc)
    resp = client.post(
        "/api/official/user/account/login/new",
        json={
            "password": password,
            "account": TEST_ACCOUNT,
            "equipment": 2,
            "loginMethod": "2",
            "equipmentNo": "CLI-abc",
        },
    )
    assert resp.json()["success"] is True
    assert resp.json()["token"]


def test_login_wrong_password(client: TestClient) -> None:
    rc = _get_random_code(client)
    password = login_hash_sha256(password_md5("wrong password"), rc)
    resp = client.post(
        "/api/official/user/account/login/equipment",
        json={
            "password": password,
            "account": TEST_ACCOUNT,
            "equipment": 3,
            "loginMethod": "2",
            "equipmentNo": "SN1",
        },
    )
    assert resp.status_code == 200  # envelope convention: logical failure, HTTP 200
    assert resp.json()["success"] is False
    assert resp.json()["errorCode"] == "1001"


def test_login_unknown_account(client: TestClient) -> None:
    _get_random_code(client)
    resp = client.post(
        "/api/official/user/account/login/equipment",
        json={
            "password": "x",
            "account": "nobody@example.com",
            "equipment": 3,
            "loginMethod": "2",
        },
    )
    assert resp.json()["success"] is False


def test_login_without_random_code_fails(client: TestClient) -> None:
    resp = client.post(
        "/api/official/user/account/login/equipment",
        json={
            "password": login_hash_sha256(password_md5(TEST_PASSWORD), "stale"),
            "account": TEST_ACCOUNT,
            "equipment": 3,
            "loginMethod": "2",
            "equipmentNo": "SN1",
        },
    )
    assert resp.json()["success"] is False


def test_random_code_single_use(client: TestClient) -> None:
    rc = _get_random_code(client)
    password = login_hash_sha256(password_md5(TEST_PASSWORD), rc)
    payload = {
        "password": password,
        "account": TEST_ACCOUNT,
        "equipment": 3,
        "loginMethod": "2",
        "equipmentNo": "SN1",
    }
    assert client.post("/api/official/user/account/login/equipment", json=payload).json()[
        "success"
    ]
    # Same code again must be rejected (nonce is consumed).
    resp = client.post("/api/official/user/account/login/equipment", json=payload)
    assert resp.json()["success"] is False


def test_query_token(client: TestClient, token: str) -> None:
    resp = client.post("/api/user/query/token", headers={"x-access-token": token})
    assert resp.json()["success"] is True
    assert resp.json()["token"] == token


def test_query_token_invalid(client: TestClient) -> None:
    resp = client.post("/api/user/query/token", headers={"x-access-token": "bogus"})
    assert resp.json()["success"] is False
    assert resp.json()["errorCode"] == "1002"


def test_logout_revokes_token(client: TestClient, token: str) -> None:
    assert client.post("/api/user/logout", headers={"x-access-token": token}).json()["success"]
    resp = client.post("/api/user/query/token", headers={"x-access-token": token})
    assert resp.json()["success"] is False


def test_protected_route_requires_token(client: TestClient) -> None:
    resp = client.post("/api/file/2/files/synchronous/start", json={})
    assert resp.json()["success"] is False
    assert resp.json()["errorCode"] == "1002"


def test_catch_all_unimplemented(client: TestClient) -> None:
    resp = client.post("/api/file/note/to/pdf", json={"id": 1})
    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert resp.json()["errorCode"] == "9999"
