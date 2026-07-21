"""End-to-end device scenarios driven through the FakeDevice helper."""

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from notehook_server.models import FileNode, SyncSession, now_ms
from tests.conftest import TEST_ACCOUNT, TEST_PASSWORD
from tests.helpers.fake_device import FakeDevice


@pytest.fixture
def device(client: TestClient) -> FakeDevice:
    d = FakeDevice(client, TEST_ACCOUNT, TEST_PASSWORD, "SN12345678")
    assert d.login()["success"]
    return d


def test_first_sync_empty_server(device: FakeDevice) -> None:
    start = device.sync_start()
    assert start["success"] is True
    assert start["synType"] is False  # init mode: server is empty
    assert device.list_folder()["entries"] == []
    assert device.sync_end()["success"] is True


def test_syn_type_true_after_data_exists(device: FakeDevice) -> None:
    assert device.sync_start()["synType"] is False
    device.create_folder("/Note")
    device.sync_end()
    # Second session: server now has data -> differential sync.
    assert device.sync_start()["synType"] is True


def test_full_upload_download_round_trip(device: FakeDevice) -> None:
    device.create_folder("/Note")
    data = b"supernote note binary content" * 100
    finish = device.upload("/Note", "test.note", data)
    assert finish["success"], finish
    assert finish["content_hash"] == hashlib.md5(data).hexdigest()
    assert finish["size"] == len(data)
    assert finish["path_display"] == "/Note/test.note"

    downloaded = device.download(int(finish["id"]))
    assert downloaded == data


def test_reupload_replaces_content(device: FakeDevice) -> None:
    device.create_folder("/Note")
    first = device.upload("/Note", "doc.note", b"version one")
    second = device.upload("/Note", "doc.note", b"version two, longer")
    assert second["id"] == first["id"]  # same node, updated content
    assert device.download(int(second["id"])) == b"version two, longer"


def test_listing_shapes(device: FakeDevice) -> None:
    device.create_folder("/Note/Work")
    device.upload("/Note", "a.note", b"aaa")
    listing = device.list_folder("", recursive=True)
    by_path = {e["path_display"]: e for e in listing["entries"]}
    assert by_path["/Note"]["tag"] == "folder"
    assert by_path["/Note/Work"]["tag"] == "folder"
    assert by_path["/Note/Work"]["parent_path"] == "/Note"
    entry = by_path["/Note/a.note"]
    assert entry["tag"] == "file"
    assert entry["size"] == 3
    assert entry["content_hash"] == hashlib.md5(b"aaa").hexdigest()
    assert entry["is_downloadable"] is True
    assert entry["lastUpdateTime"] > 0

    non_recursive = device.list_folder("/Note", recursive=False)
    names = {e["name"] for e in non_recursive["entries"]}
    assert names == {"Work", "a.note"}


def test_two_equipment_share_one_tree(client: TestClient, device: FakeDevice) -> None:
    device.create_folder("/Note")
    device.upload("/Note", "shared.note", b"from the device")

    cli = FakeDevice(client, TEST_ACCOUNT, TEST_PASSWORD, "CLI-000000000001")
    assert cli.login()["success"]
    assert cli.sync_start()["synType"] is True  # device's data is visible
    listing = cli.list_folder("", recursive=True)
    paths = {e["path_display"] for e in listing["entries"]}
    assert "/Note/shared.note" in paths


def test_last_modified_by_reflects_uploading_equipment(
    client: TestClient, device: FakeDevice
) -> None:
    # 1a: equipment A uploads, equipment B lists — B sees A's equipment_no.
    device.create_folder("/Note")
    device.upload("/Note", "shared.note", b"from A")

    other = FakeDevice(client, TEST_ACCOUNT, TEST_PASSWORD, "SN99999999")
    assert other.login()["success"]
    listing = other.list_folder("/Note", recursive=True)
    entry = next(e for e in listing["entries"] if e["name"] == "shared.note")
    assert entry["last_modified_by"] == device.equipment_no


def test_legacy_node_last_modified_by_none_serializes_empty(
    app: FastAPI, device: FakeDevice
) -> None:
    # 1a: nodes created before the field existed have last_modified_by=None
    # in the DB; they must still serialize cleanly as "".
    device.create_folder("/Legacy")
    with Session(app.state.engine) as session:
        node = session.exec(select(FileNode).where(FileNode.name == "Legacy")).one()
        node.last_modified_by = None
        session.add(node)
        session.commit()

    vo = device._post(
        "/api/file/3/files/query/by/path_v3",
        {"equipmentNo": device.equipment_no, "path": "/Legacy"},
    )
    assert vo["success"], vo
    assert vo["entriesVO"]["last_modified_by"] == ""


def test_chunked_upload(device: FakeDevice) -> None:
    device.create_folder("/Document")
    data = b"0123456789abcdef" * 1000
    chunks = [data[i : i + 4096] for i in range(0, len(data), 4096)]

    apply_vo = device._post(
        "/api/file/3/files/upload/apply",
        {
            "equipmentNo": device.equipment_no,
            "path": "/Document",
            "fileName": "big.pdf",
            "size": str(len(data)),
        },
    )
    assert apply_vo["success"]
    part_url = apply_vo["partUploadUrl"]
    for idx, chunk in enumerate(chunks, start=1):
        resp = device.client.post(
            f"{part_url}&uploadId=upl-1&partNumber={idx}&totalChunks={len(chunks)}",
            files={"file": ("big.pdf", chunk)},
        )
        body = resp.json()
        assert body["success"], body
        expected_status = "completed" if idx == len(chunks) else "uploading"
        assert body["status"] == expected_status

    finish = device._post(
        "/api/file/2/files/upload/finish",
        {
            "equipmentNo": device.equipment_no,
            "path": "/Document",
            "fileName": "big.pdf",
            "size": str(len(data)),
            "content_hash": hashlib.md5(data).hexdigest(),
            "innerName": apply_vo["innerName"],
        },
    )
    assert finish["success"], finish
    assert device.download(int(finish["id"])) == data


def _other_device(client: TestClient) -> FakeDevice:
    other = FakeDevice(client, TEST_ACCOUNT, TEST_PASSWORD, "SN-OTHER-0001")
    assert other.login()["success"]
    return other


def test_second_device_sync_blocked_while_one_active(
    client: TestClient, device: FakeDevice
) -> None:
    assert device.sync_start()["success"] is True
    other = _other_device(client)
    blocked = other.sync_start()
    assert blocked["success"] is False
    assert blocked["errorCode"] == "E0078"


def test_mutation_blocked_while_other_device_syncing(
    client: TestClient, device: FakeDevice
) -> None:
    assert device.sync_start()["success"] is True
    other = _other_device(client)
    rejected = other.create_folder("/Intruder")
    assert rejected["success"] is False
    assert rejected["errorCode"] == "E0079"


def test_owning_device_may_mutate_during_its_own_sync(device: FakeDevice) -> None:
    assert device.sync_start()["success"] is True
    assert device.create_folder("/Note")["success"] is True  # same device, allowed


def test_sync_lock_released_by_sync_end(client: TestClient, device: FakeDevice) -> None:
    assert device.sync_start()["success"] is True
    assert device.sync_end()["success"] is True
    other = _other_device(client)
    assert other.sync_start()["success"] is True  # lock released, other may proceed


def test_stale_active_session_self_heals(app: FastAPI, client: TestClient) -> None:
    # A session left "active" long ago (device crashed mid-sync) must not wedge
    # the lock forever: it falls outside the TTL and is ignored.
    ttl_ms = app.state.settings.sync_session_ttl_seconds * 1000
    with Session(app.state.engine) as session:
        session.add(
            SyncSession(equipment_no="SN-GHOST", started_at=now_ms() - ttl_ms - 1000)
        )
        session.commit()
    device = FakeDevice(client, TEST_ACCOUNT, TEST_PASSWORD, "SN-FRESH")
    assert device.login()["success"]
    assert device.sync_start()["success"] is True  # ghost session is expired


def test_space_usage(device: FakeDevice) -> None:
    device.create_folder("/Note")
    device.upload("/Note", "n.note", b"x" * 500)
    vo = device._post("/api/file/2/users/get_space_usage", {"equipmentNo": device.equipment_no})
    assert vo["success"]
    assert vo["used"] == 500
    assert vo["allocationVO"]["allocated"] > 0
