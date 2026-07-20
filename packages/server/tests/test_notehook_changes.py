"""POST /api/notehook/changes (workflow-spec.md §7, Phase 1c)."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_ACCOUNT, TEST_PASSWORD
from tests.helpers.fake_device import FakeDevice


@pytest.fixture
def device(client: TestClient) -> FakeDevice:
    d = FakeDevice(client, TEST_ACCOUNT, TEST_PASSWORD, "SN12345678")
    assert d.login()["success"]
    return d


def test_bootstrap_since_zero_returns_cursor_without_replay(device: FakeDevice) -> None:
    device.create_folder("/A")
    device.create_folder("/B")

    boot = device.changes(since=0)
    assert boot["success"], boot
    assert boot["changes"] == []
    assert boot["cursor"] > 0


def test_incremental_fetch_returns_rows_past_cursor_in_order(device: FakeDevice) -> None:
    device.create_folder("/A")
    cursor0 = device.changes(since=0)["cursor"]

    device.create_folder("/B")
    device.create_folder("/C")

    incr = device.changes(since=cursor0)
    assert incr["success"], incr
    assert [c["path_display"] for c in incr["changes"]] == ["/B", "/C"]
    assert [c["op"] for c in incr["changes"]] == ["create", "create"]
    assert incr["cursor"] == incr["changes"][-1]["id"]

    # Calling again with the new cursor sees nothing further.
    tail = device.changes(since=incr["cursor"])
    assert tail["changes"] == []
    assert tail["cursor"] == incr["cursor"]


def test_limit_is_clamped(device: FakeDevice) -> None:
    device.create_folder("/seed")  # ensure cursor0 != 0 (0 is the bootstrap sentinel)
    cursor0 = device.changes(since=0)["cursor"]
    device.create_folder("/D")
    device.create_folder("/E")

    small = device.changes(since=cursor0, limit=1)
    assert len(small["changes"]) == 1

    # limit=0 clamps up to the minimum of 1, not down to "no rows".
    zero = device.changes(since=cursor0, limit=0)
    assert len(zero["changes"]) == 1

    huge = device.changes(since=cursor0, limit=999999)
    assert len(huge["changes"]) == 2


def test_missing_token_fails(client: TestClient) -> None:
    resp = client.post("/api/notehook/changes", json={"since": 0})
    body = resp.json()
    assert body["success"] is False


def test_bad_token_fails(client: TestClient) -> None:
    resp = client.post(
        "/api/notehook/changes",
        json={"since": 0},
        headers={"x-access-token": "not-a-real-token"},
    )
    body = resp.json()
    assert body["success"] is False


def test_unknown_notehook_subpath_hits_catch_all(client: TestClient) -> None:
    resp = client.post("/api/notehook/frobnicate", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["errorCode"] == "9999"


def test_wait_seconds_returns_early_when_change_lands_mid_wait(device: FakeDevice) -> None:
    device.create_folder("/Watched")
    cursor0 = device.changes(since=0)["cursor"]

    def _mutate_soon() -> None:
        time.sleep(0.3)
        device.create_folder("/Watched/Late")

    thread = threading.Thread(target=_mutate_soon)
    started = time.perf_counter()
    thread.start()
    result = device.changes(since=cursor0, wait_seconds=5)
    elapsed = time.perf_counter() - started
    thread.join()

    assert result["success"], result
    assert [c["path_display"] for c in result["changes"]] == ["/Watched/Late"]
    # Woke up on the change, not after waiting out the full 5s deadline.
    assert elapsed < 4.0
