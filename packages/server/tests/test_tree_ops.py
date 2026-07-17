"""Folder/file mutations: create, delete, move, copy, autorename, safety checks."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_ACCOUNT, TEST_PASSWORD
from tests.helpers.fake_device import FakeDevice


@pytest.fixture
def device(client: TestClient) -> FakeDevice:
    d = FakeDevice(client, TEST_ACCOUNT, TEST_PASSWORD, "SN12345678")
    assert d.login()["success"]
    return d


def test_create_nested_folder(device: FakeDevice) -> None:
    vo = device.create_folder("/Note/Work/Projects")
    assert vo["success"]
    assert vo["metadata"]["path_display"] == "/Note/Work/Projects"
    assert vo["metadata"]["tag"] == "folder"


def test_create_folder_idempotent_without_autorename(device: FakeDevice) -> None:
    first = device.create_folder("/Note")
    second = device.create_folder("/Note")
    assert second["metadata"]["id"] == first["metadata"]["id"]


def test_create_folder_autorename(device: FakeDevice) -> None:
    first = device.create_folder("/Note")
    renamed = device.create_folder("/Note", autorename=True)
    assert renamed["metadata"]["id"] != first["metadata"]["id"]
    assert renamed["metadata"]["name"] == "Note (1)"


def test_case_insensitive_paths(device: FakeDevice) -> None:
    device.create_folder("/Note")
    vo = device._post(
        "/api/file/3/files/query/by/path_v3",
        {"equipmentNo": device.equipment_no, "path": "/note"},
    )
    assert vo["success"]
    assert vo["entriesVO"]["name"] == "Note"


def test_query_by_id_and_path(device: FakeDevice) -> None:
    created = device.create_folder("/Note/Sub")
    node_id = created["metadata"]["id"]
    by_id = device._post(
        "/api/file/3/files/query_v3", {"equipmentNo": device.equipment_no, "id": node_id}
    )
    assert by_id["entriesVO"]["path_display"] == "/Note/Sub"
    by_path = device._post(
        "/api/file/3/files/query/by/path_v3",
        {"equipmentNo": device.equipment_no, "path": "/Note/Sub"},
    )
    assert by_path["entriesVO"]["id"] == node_id


def test_delete_subtree(device: FakeDevice) -> None:
    device.create_folder("/Note/Sub")
    device.upload("/Note/Sub", "x.note", b"data")
    note_id = device._post(
        "/api/file/3/files/query/by/path_v3",
        {"equipmentNo": device.equipment_no, "path": "/Note"},
    )["entriesVO"]["id"]
    vo = device._post(
        "/api/file/3/files/delete_folder_v3",
        {"equipmentNo": device.equipment_no, "id": int(note_id)},
    )
    assert vo["success"]
    assert device.list_folder("", recursive=True)["entries"] == []


def test_delete_missing_id(device: FakeDevice) -> None:
    vo = device._post(
        "/api/file/3/files/delete_folder_v3", {"equipmentNo": device.equipment_no, "id": 424242}
    )
    assert vo["success"] is False
    assert vo["errorCode"] == "2001"


def test_move_renames_and_relocates(device: FakeDevice) -> None:
    device.create_folder("/Note")
    device.create_folder("/Archive")
    up = device.upload("/Note", "old.note", b"content")
    vo = device._post(
        "/api/file/3/files/move_v3",
        {
            "equipmentNo": device.equipment_no,
            "id": int(up["id"]),
            "to_path": "/Archive/new.note",
        },
    )
    assert vo["success"]
    assert vo["entriesVO"]["path_display"] == "/Archive/new.note"
    listing = device.list_folder("/Note")
    assert listing["entries"] == []


def test_move_conflict_without_autorename(device: FakeDevice) -> None:
    device.create_folder("/Note")
    a = device.upload("/Note", "a.note", b"a")
    device.upload("/Note", "b.note", b"b")
    vo = device._post(
        "/api/file/3/files/move_v3",
        {"equipmentNo": device.equipment_no, "id": int(a["id"]), "to_path": "/Note/b.note"},
    )
    assert vo["success"] is False
    assert vo["errorCode"] == "2002"


def test_move_folder_into_itself_rejected(device: FakeDevice) -> None:
    created = device.create_folder("/Note")
    vo = device._post(
        "/api/file/3/files/move_v3",
        {
            "equipmentNo": device.equipment_no,
            "id": int(created["metadata"]["id"]),
            "to_path": "/Note/Inner/Note",
        },
    )
    assert vo["success"] is False


def test_copy_file(device: FakeDevice) -> None:
    device.create_folder("/Note")
    up = device.upload("/Note", "orig.note", b"copy me")
    vo = device._post(
        "/api/file/3/files/copy_v3",
        {
            "equipmentNo": device.equipment_no,
            "id": int(up["id"]),
            "to_path": "/Note/copy.note",
        },
    )
    assert vo["success"]
    assert vo["entriesVO"]["content_hash"] == up["content_hash"]
    # Both copies download independently with identical content.
    assert device.download(int(vo["entriesVO"]["id"])) == b"copy me"
    assert device.download(int(up["id"])) == b"copy me"


def test_copy_folder_recursive(device: FakeDevice) -> None:
    device.create_folder("/Note/Sub")
    device.upload("/Note/Sub", "deep.note", b"deep")
    note_id = device._post(
        "/api/file/3/files/query/by/path_v3",
        {"equipmentNo": device.equipment_no, "path": "/Note"},
    )["entriesVO"]["id"]
    vo = device._post(
        "/api/file/3/files/copy_v3",
        {"equipmentNo": device.equipment_no, "id": int(note_id), "to_path": "/Backup"},
    )
    assert vo["success"]
    paths = {e["path_display"] for e in device.list_folder("", recursive=True)["entries"]}
    assert "/Backup/Sub/deep.note" in paths
    assert "/Note/Sub/deep.note" in paths


def test_copy_autorename(device: FakeDevice) -> None:
    device.create_folder("/Note")
    up = device.upload("/Note", "doc.note", b"1")
    vo = device._post(
        "/api/file/3/files/copy_v3",
        {
            "equipmentNo": device.equipment_no,
            "id": int(up["id"]),
            "to_path": "/Note/doc.note",
            "autorename": True,
        },
    )
    assert vo["success"]
    assert vo["entriesVO"]["name"] == "doc (1).note"


def test_evil_names_rejected(device: FakeDevice) -> None:
    for bad in ["..", "a/../../b", "nul\x00byte"]:
        vo = device.create_folder(bad)
        assert vo["success"] is False, bad


def test_deleted_blob_not_downloadable_after_delete(device: FakeDevice) -> None:
    device.create_folder("/Note")
    up = device.upload("/Note", "gone.note", b"bye")
    device._post(
        "/api/file/3/files/delete_folder_v3",
        {"equipmentNo": device.equipment_no, "id": int(up["id"])},
    )
    vo = device._post(
        "/api/file/3/files/download_v3",
        {"equipmentNo": device.equipment_no, "id": int(up["id"])},
    )
    assert vo["success"] is False
