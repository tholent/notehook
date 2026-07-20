"""Change table appends (workflow-spec.md §7, Phase 1b): one row per tree
mutation, committed atomically with the mutation it describes.
"""

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from notehook_server.files import change_service
from notehook_server.models import Change
from tests.conftest import TEST_ACCOUNT, TEST_PASSWORD
from tests.helpers.fake_device import FakeDevice


@pytest.fixture
def device(client: TestClient) -> FakeDevice:
    d = FakeDevice(client, TEST_ACCOUNT, TEST_PASSWORD, "SN12345678")
    assert d.login()["success"]
    return d


def _all_changes(app: FastAPI) -> list[Change]:
    with Session(app.state.engine) as session:
        return change_service.since(session, 0, 1000)


def test_create_folder_appends_one_row_for_leaf_only(app: FastAPI, device: FakeDevice) -> None:
    device.create_folder("/Note")  # leaf "Note" -> one row
    device.create_folder("/Note/Work")  # "Note" already exists; leaf "Work" -> one row

    rows = _all_changes(app)
    assert [(r.op, r.path_display, r.is_folder) for r in rows] == [
        ("create", "/Note", True),
        ("create", "/Note/Work", True),
    ]
    assert all(r.equipment_no == device.equipment_no for r in rows)


def test_create_folder_idempotent_recreate_appends_nothing(
    app: FastAPI, device: FakeDevice
) -> None:
    device.create_folder("/Note")
    before = len(_all_changes(app))
    again = device.create_folder("/Note")
    assert again["success"]
    assert len(_all_changes(app)) == before


def test_upload_new_file_appends_create_change(app: FastAPI, device: FakeDevice) -> None:
    device.create_folder("/Note")
    before = len(_all_changes(app))
    data = b"hello world"
    finish = device.upload("/Note", "a.note", data)
    assert finish["success"]

    rows = _all_changes(app)
    assert len(rows) == before + 1
    row = rows[-1]
    assert row.op == "create"
    assert row.path_display == "/Note/a.note"
    assert row.is_folder is False
    assert row.content_hash == hashlib.md5(data).hexdigest()
    assert row.equipment_no == device.equipment_no
    assert row.node_id == int(finish["id"])


def test_upload_replace_appends_update_change(app: FastAPI, device: FakeDevice) -> None:
    device.create_folder("/Note")
    device.upload("/Note", "a.note", b"version one")
    before = len(_all_changes(app))
    finish = device.upload("/Note", "a.note", b"version two")
    assert finish["success"]

    rows = _all_changes(app)
    assert len(rows) == before + 1
    row = rows[-1]
    assert row.op == "update"
    assert row.path_display == "/Note/a.note"
    assert row.content_hash == hashlib.md5(b"version two").hexdigest()
    assert row.equipment_no == device.equipment_no


def test_move_appends_change_with_new_path(app: FastAPI, device: FakeDevice) -> None:
    device.create_folder("/Note")
    device.create_folder("/Archive")
    up = device.upload("/Note", "old.note", b"content")
    before = len(_all_changes(app))

    vo = device._post(
        "/api/file/3/files/move_v3",
        {
            "equipmentNo": device.equipment_no,
            "id": int(up["id"]),
            "to_path": "/Archive/new.note",
        },
    )
    assert vo["success"], vo

    rows = _all_changes(app)
    assert len(rows) == before + 1
    row = rows[-1]
    assert row.op == "move"
    assert row.path_display == "/Archive/new.note"
    assert row.node_id == int(up["id"])
    assert row.equipment_no == device.equipment_no


def test_copy_folder_appends_single_row_for_root_only(
    app: FastAPI, device: FakeDevice
) -> None:
    device.create_folder("/Note/Sub")
    device.upload("/Note/Sub", "deep.note", b"deep")
    note_id = device._post(
        "/api/file/3/files/query/by/path_v3",
        {"equipmentNo": device.equipment_no, "path": "/Note"},
    )["entriesVO"]["id"]
    before = len(_all_changes(app))

    vo = device._post(
        "/api/file/3/files/copy_v3",
        {"equipmentNo": device.equipment_no, "id": int(note_id), "to_path": "/Backup"},
    )
    assert vo["success"], vo

    rows = _all_changes(app)
    # One row for the copy root only — children (Sub/, deep.note) are implied.
    assert len(rows) == before + 1
    row = rows[-1]
    assert row.op == "copy"
    assert row.path_display == "/Backup"
    assert row.is_folder is True
    assert row.node_id == int(vo["entriesVO"]["id"])
    assert row.equipment_no == device.equipment_no


def test_delete_appends_single_row_for_deleted_root(app: FastAPI, device: FakeDevice) -> None:
    device.create_folder("/Note/Sub")
    device.upload("/Note/Sub", "x.note", b"data")
    note_id = device._post(
        "/api/file/3/files/query/by/path_v3",
        {"equipmentNo": device.equipment_no, "path": "/Note"},
    )["entriesVO"]["id"]
    before = len(_all_changes(app))

    vo = device._post(
        "/api/file/3/files/delete_folder_v3",
        {"equipmentNo": device.equipment_no, "id": int(note_id)},
    )
    assert vo["success"], vo

    rows = _all_changes(app)
    # One row for the deleted root only — children (Sub/, x.note) are implied.
    assert len(rows) == before + 1
    row = rows[-1]
    assert row.op == "delete"
    assert row.path_display == "/Note"  # snapshot taken before deletion
    assert row.node_id == int(note_id)
    assert row.equipment_no == device.equipment_no


def test_failed_upload_finish_appends_no_change(app: FastAPI, device: FakeDevice) -> None:
    # Atomicity: a failed mutation must not leave a Change row behind.
    device.create_folder("/Note")
    apply_vo = device._post(
        "/api/file/3/files/upload/apply",
        {
            "equipmentNo": device.equipment_no,
            "path": "/Note",
            "fileName": "bad.note",
            "size": "4",
        },
    )
    assert apply_vo["success"]
    upload_resp = device.client.post(
        apply_vo["fullUploadUrl"], files={"file": ("bad.note", b"data")}
    )
    assert upload_resp.json()["success"]

    before = len(_all_changes(app))
    finish = device._post(
        "/api/file/2/files/upload/finish",
        {
            "equipmentNo": device.equipment_no,
            "path": "/Note",
            "fileName": "bad.note",
            "content_hash": "0" * 32,  # wrong on purpose
            "innerName": apply_vo["innerName"],
        },
    )
    assert finish["success"] is False
    assert len(_all_changes(app)) == before
