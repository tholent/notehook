"""Scripted client that mimics the real device call sequence against the API."""

import hashlib
from typing import Any

from fastapi.testclient import TestClient

from noted_protocol.crypto import login_hash_sha256, password_md5


class FakeDevice:
    def __init__(
        self, client: TestClient, account: str, password: str, equipment_no: str
    ) -> None:
        self.client = client
        self.account = account
        self.password = password
        self.equipment_no = equipment_no
        self.token: str | None = None

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"x-access-token": self.token} if self.token else {}
        resp = self.client.post(path, json=payload, headers=headers)
        body: dict[str, Any] = resp.json()
        return body

    def login(self) -> dict[str, Any]:
        rc = self._post("/api/official/user/query/random/code", {"account": self.account})
        assert rc["success"], rc
        body = self._post(
            "/api/official/user/account/login/equipment",
            {
                "password": login_hash_sha256(password_md5(self.password), rc["randomCode"]),
                "account": self.account,
                "equipment": 3,
                "loginMethod": "2",
                "equipmentNo": self.equipment_no,
            },
        )
        if body.get("success"):
            self.token = body["token"]
        return body

    def sync_start(self) -> dict[str, Any]:
        return self._post(
            "/api/file/2/files/synchronous/start", {"equipmentNo": self.equipment_no}
        )

    def sync_end(self, flag: str = "success") -> dict[str, Any]:
        return self._post(
            "/api/file/2/files/synchronous/end",
            {"equipmentNo": self.equipment_no, "flag": flag},
        )

    def list_folder(self, path: str = "", recursive: bool = False) -> dict[str, Any]:
        return self._post(
            "/api/file/2/files/list_folder",
            {"equipmentNo": self.equipment_no, "path": path, "recursive": recursive},
        )

    def create_folder(self, path: str, autorename: bool = False) -> dict[str, Any]:
        return self._post(
            "/api/file/2/files/create_folder_v2",
            {"equipmentNo": self.equipment_no, "path": path, "autorename": autorename},
        )

    def upload(self, folder: str, name: str, data: bytes) -> dict[str, Any]:
        """Full apply -> PUT bytes -> finish flow, like the device does."""
        apply_vo = self._post(
            "/api/file/3/files/upload/apply",
            {
                "equipmentNo": self.equipment_no,
                "path": folder,
                "fileName": name,
                "size": str(len(data)),
            },
        )
        assert apply_vo["success"], apply_vo
        upload_resp = self.client.post(
            apply_vo["fullUploadUrl"], files={"file": (name, data)}
        )
        assert upload_resp.json()["success"], upload_resp.text
        return self._post(
            "/api/file/2/files/upload/finish",
            {
                "equipmentNo": self.equipment_no,
                "path": folder,
                "size": str(len(data)),
                "fileName": name,
                "content_hash": hashlib.md5(data).hexdigest(),
                "innerName": apply_vo["innerName"],
            },
        )

    def download(self, node_id: int) -> bytes:
        vo = self._post(
            "/api/file/3/files/download_v3",
            {"equipmentNo": self.equipment_no, "id": node_id},
        )
        assert vo["success"], vo
        resp = self.client.get(vo["url"])
        assert resp.status_code == 200, resp.text
        return resp.content
