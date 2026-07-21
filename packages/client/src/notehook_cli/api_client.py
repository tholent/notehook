"""HTTP client implementing the device-side Supernote sync protocol."""

import hashlib
from pathlib import Path
from typing import Any

import httpx

from notehook_protocol.crypto import login_hash_sha256, password_md5
from notehook_protocol.models.file import EntriesVO


class ApiError(RuntimeError):
    def __init__(self, code: str | None, msg: str | None) -> None:
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg


class EndpointUnsupported(ApiError):
    """The server answered via its catch-all route (errorCode '9999'): it
    predates /api/notehook/changes. Distinct from ApiError so callers (the
    daemon's change-feed thread) can permanently disable the feature instead
    of retrying forever (workflow-spec.md §7: "feed absence degrades to
    today's behavior, never breaks sync")."""


class SupernoteApiClient:
    """Thin wrapper over the server API. Accepts any httpx.Client-compatible
    object (the test suite injects FastAPI's TestClient)."""

    def __init__(self, http: httpx.Client, equipment_no: str) -> None:
        self._http = http
        self.equipment_no = equipment_no
        self.token: str | None = None

    def _check(self, resp: httpx.Response) -> dict[str, Any]:
        # The server signals logical failures through the BaseVO envelope, and
        # an invalid/expired token through HTTP 401 *with* that envelope body.
        # Parse the envelope first so token expiry surfaces as a clean ApiError
        # ("401"/"Unauthorized") rather than a bare httpx.HTTPStatusError.
        try:
            body: dict[str, Any] = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise
        if not body.get("success"):
            raise ApiError(body.get("errorCode"), body.get("errorMsg"))
        resp.raise_for_status()
        return body

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"x-access-token": self.token} if self.token else {}
        return self._check(self._http.post(path, json=payload, headers=headers))

    def ping(self) -> bool:
        try:
            self._check(self._http.get("/api/file/query/server"))
            return True
        except (httpx.HTTPError, ApiError):
            return False

    def login(self, account: str, password: str) -> str:
        rc = self._post("/api/official/user/query/random/code", {"account": account})
        hashed = login_hash_sha256(password_md5(password), rc["randomCode"])
        body = self._post(
            "/api/official/user/account/login/new",
            {
                "password": hashed,
                "account": account,
                "equipment": 2,
                "loginMethod": "2",
                "equipmentNo": self.equipment_no,
            },
        )
        self.token = str(body["token"])
        return self.token

    def validate_token(self) -> bool:
        try:
            self._post("/api/user/query/token", {})
            return True
        except (httpx.HTTPError, ApiError):
            return False

    def sync_start(self) -> bool:
        body = self._post(
            "/api/file/2/files/synchronous/start", {"equipmentNo": self.equipment_no}
        )
        return bool(body.get("synType"))

    def sync_end(self, flag: str = "success") -> None:
        self._post(
            "/api/file/2/files/synchronous/end",
            {"equipmentNo": self.equipment_no, "flag": flag},
        )

    def list_all(self) -> list[EntriesVO]:
        body = self._post(
            "/api/file/2/files/list_folder",
            {"equipmentNo": self.equipment_no, "path": "", "recursive": True},
        )
        return [EntriesVO.model_validate(e) for e in body.get("entries", [])]

    def create_folder(self, path: str) -> str:
        body = self._post(
            "/api/file/2/files/create_folder_v2",
            {"equipmentNo": self.equipment_no, "path": path, "autorename": False},
        )
        return str(body["metadata"]["id"])

    def delete(self, node_id: int) -> None:
        self._post(
            "/api/file/3/files/delete_folder_v3",
            {"equipmentNo": self.equipment_no, "id": node_id},
        )

    def upload_file(self, local_path: Path, remote_folder: str, name: str) -> EntriesVO:
        data = local_path.read_bytes()
        apply_vo = self._post(
            "/api/file/3/files/upload/apply",
            {
                "equipmentNo": self.equipment_no,
                "path": remote_folder,
                "fileName": name,
                "size": str(len(data)),
            },
        )
        upload_resp = self._http.post(
            apply_vo["fullUploadUrl"], files={"file": (name, data)}
        )
        self._check(upload_resp)
        finish = self._post(
            "/api/file/2/files/upload/finish",
            {
                "equipmentNo": self.equipment_no,
                "path": remote_folder,
                "size": str(len(data)),
                "fileName": name,
                "content_hash": hashlib.md5(data).hexdigest(),
                "innerName": apply_vo["innerName"],
            },
        )
        return EntriesVO(
            tag="file",
            id=finish["id"],
            name=finish["name"],
            path_display=finish["path_display"],
            content_hash=finish["content_hash"],
            size=finish["size"],
        )

    def changes(
        self, since: int, limit: int = 500, wait_seconds: int = 0
    ) -> tuple[int, list[dict[str, Any]]]:
        """Server-side change feed (workflow-spec.md §7): notehook's own
        extension, outside the reverse-engineered device API. Returns
        (new_cursor, raw change rows). Servers without the endpoint answer
        via the catch-all (success=false, errorCode '9999'), surfaced here
        as EndpointUnsupported rather than the generic ApiError.
        """
        try:
            body = self._post(
                "/api/notehook/changes",
                {"since": since, "limit": limit, "wait_seconds": wait_seconds},
            )
        except ApiError as exc:
            if exc.code == "9999":
                raise EndpointUnsupported(exc.code, exc.msg) from exc
            raise
        return int(body.get("cursor", 0)), list(body.get("changes", []))

    def download_file(self, node_id: int, dest: Path) -> str:
        vo = self._post(
            "/api/file/3/files/download_v3",
            {"equipmentNo": self.equipment_no, "id": node_id},
        )
        resp = self._http.get(vo["url"])
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".notehook-tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(dest)
        return str(vo.get("content_hash") or hashlib.md5(resp.content).hexdigest())
