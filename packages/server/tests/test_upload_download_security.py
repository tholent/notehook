"""Security-focused tests: signatures, traversal, hash verification, limits."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from notehook_server.config import Settings
from notehook_server.main import create_app
from tests.conftest import TEST_ACCOUNT, TEST_PASSWORD, do_login
from tests.helpers.fake_device import FakeDevice


@pytest.fixture
def device(client: TestClient) -> FakeDevice:
    d = FakeDevice(client, TEST_ACCOUNT, TEST_PASSWORD, "SN12345678")
    assert d.login()["success"]
    return d


def _apply(device: FakeDevice, name: str = "f.note", size: int = 10) -> dict[str, str]:
    vo = device._post(
        "/api/file/3/files/upload/apply",
        {
            "equipmentNo": device.equipment_no,
            "path": "/Note",
            "fileName": name,
            "size": str(size),
        },
    )
    assert vo["success"]
    return vo


def test_upload_bad_signature_rejected(device: FakeDevice) -> None:
    vo = _apply(device)
    resp = device.client.post(
        f"/api/oss/upload?signature=WRONG&path={vo['innerName']}",
        files={"file": ("f.note", b"data")},
    )
    assert resp.json()["success"] is False
    assert resp.json()["errorCode"] == "3003"


def test_upload_path_signature_mismatch_rejected(device: FakeDevice) -> None:
    first = _apply(device, "a.note")
    second = _apply(device, "b.note")
    # Valid signature from one session with the inner_name of another.
    resp = device.client.post(
        f"/api/oss/upload?signature={first['authorization']}&path={second['innerName']}",
        files={"file": ("x", b"data")},
    )
    assert resp.json()["success"] is False


def test_upload_traversal_path_rejected(device: FakeDevice) -> None:
    vo = _apply(device)
    resp = device.client.post(
        f"/api/oss/upload?signature={vo['authorization']}&path=../../etc/passwd",
        files={"file": ("x", b"data")},
    )
    assert resp.json()["success"] is False


def test_finish_wrong_hash_rejected(device: FakeDevice) -> None:
    vo = _apply(device)
    device.client.post(vo["fullUploadUrl"], files={"file": ("f.note", b"real content")})
    finish = device._post(
        "/api/file/2/files/upload/finish",
        {
            "fileName": "f.note",
            "content_hash": hashlib.md5(b"claimed different content").hexdigest(),
            "innerName": vo["innerName"],
            "path": "/Note",
        },
    )
    assert finish["success"] is False
    assert finish["errorCode"] == "3001"


def test_finish_without_upload_rejected(device: FakeDevice) -> None:
    vo = _apply(device)  # applied but never uploaded
    finish = device._post(
        "/api/file/2/files/upload/finish",
        {
            "fileName": "f.note",
            "content_hash": "d41d8cd98f00b204e9800998ecf8427e",
            "innerName": vo["innerName"],
            "path": "/Note",
        },
    )
    assert finish["success"] is False


def test_download_url_single_use(device: FakeDevice) -> None:
    device.create_folder("/Note")
    up = device.upload("/Note", "f.note", b"content")
    vo = device._post(
        "/api/file/3/files/download_v3",
        {"equipmentNo": device.equipment_no, "id": int(up["id"])},
    )
    assert device.client.get(vo["url"]).status_code == 200
    # Replay of the same signed URL is rejected.
    replay = device.client.get(vo["url"])
    assert replay.json()["success"] is False


def test_download_tampered_signature(device: FakeDevice) -> None:
    device.create_folder("/Note")
    up = device.upload("/Note", "f.note", b"content")
    vo = device._post(
        "/api/file/3/files/download_v3",
        {"equipmentNo": device.equipment_no, "id": int(up["id"])},
    )
    url: str = vo["url"]
    sig_start = url.index("signature=") + len("signature=")
    tampered = url[:sig_start] + "0" * 64 + url[url.index("&", sig_start) :]
    resp = device.client.get(tampered)
    assert resp.json()["success"] is False


def test_download_traversal_rejected(device: FakeDevice) -> None:
    resp = device.client.get(
        "/api/oss/download",
        params={
            "path": "../../../etc/passwd",
            "signature": "x",
            "timestamp": 0,
            "nonce": "n",
            "pathId": 1,
        },
    )
    assert resp.json()["success"] is False
    assert resp.json()["errorCode"] == "2004"


def test_max_upload_size_enforced(settings: Settings) -> None:
    settings.max_upload_bytes = 100
    app = create_app(settings)
    with TestClient(app) as client:
        device = FakeDevice(client, TEST_ACCOUNT, TEST_PASSWORD, "SN1")
        assert device.login()["success"]
        vo = _apply(device, size=50)  # claimed small, actually large
        resp = client.post(vo["fullUploadUrl"], files={"file": ("f.note", b"x" * 500)})
        assert resp.json()["success"] is False


def test_apply_oversized_rejected(settings: Settings) -> None:
    settings.max_upload_bytes = 100
    app = create_app(settings)
    with TestClient(app) as client:
        device = FakeDevice(client, TEST_ACCOUNT, TEST_PASSWORD, "SN1")
        assert device.login()["success"]
        vo = device._post(
            "/api/file/3/files/upload/apply",
            {"path": "/Note", "fileName": "big.pdf", "size": "5000"},
        )
        assert vo["success"] is False


def test_quota_enforced(settings: Settings) -> None:
    settings.total_capacity_bytes = 100
    app = create_app(settings)
    with TestClient(app) as client:
        device = FakeDevice(client, TEST_ACCOUNT, TEST_PASSWORD, "SN1")
        assert device.login()["success"]
        device.create_folder("/Note")
        assert device.upload("/Note", "ok.note", b"x" * 80)["success"]
        vo = device._post(
            "/api/file/3/files/upload/apply",
            {"path": "/Note", "fileName": "over.note", "size": "80"},
        )
        assert vo["success"]
        resp = client.post(vo["fullUploadUrl"], files={"file": ("over.note", b"y" * 80)})
        assert resp.json()["success"] is False
        assert resp.json()["errorCode"] == "3002"


def test_login_rate_limit(settings: Settings) -> None:
    settings.login_attempts_per_minute = 3
    app = create_app(settings)
    with TestClient(app) as client:
        results = []
        for _ in range(5):
            resp = client.post(
                "/api/official/user/query/random/code", json={"account": TEST_ACCOUNT}
            )
            results.append(resp.json())
        assert results[2]["success"] is True
        assert results[3]["success"] is False
        assert results[3]["errorCode"] == "1003"


def test_capture_redacts_secrets(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        do_login(client)
    capture_files = list(settings.captures_dir.glob("*.jsonl"))
    assert capture_files
    content = capture_files[0].read_text()
    assert "password" in content  # field is present...
    import json

    for line in content.splitlines():
        record = json.loads(line)
        body = record.get("request_body")
        if isinstance(body, dict) and "password" in body:
            assert body["password"] == "***"  # ...but its value is redacted
