"""Sync engine: executes the plan produced by diff.classify."""

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from notehook_cli.api_client import SupernoteApiClient
from notehook_cli.diff import Action, SyncItem, classify, remote_by_rel_path
from notehook_cli.lock import file_lock
from notehook_cli.scan import LocalFile, file_md5, scan_local
from notehook_cli.state_db import StateDB, SyncedFile
from notehook_cli.workflows.events import EventLog

logger = logging.getLogger(__name__)

POLICIES = ("keep-both", "newest-wins", "local-wins", "remote-wins")


@dataclass
class SyncResult:
    uploaded: list[str] = field(default_factory=list)
    downloaded: list[str] = field(default_factory=list)
    deleted_local: list[str] = field(default_factory=list)
    deleted_remote: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return (
            len(self.uploaded)
            + len(self.downloaded)
            + len(self.deleted_local)
            + len(self.deleted_remote)
        )


def conflict_copy_name(rel_path: str, equipment_no: str) -> str:
    stamp = time.strftime("%Y-%m-%d %H%M%S")
    parent, _, name = rel_path.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    suffix = f".{ext}" if dot and ext else ""
    new_name = f"{stem} (conflicted copy {equipment_no} {stamp}){suffix}"
    return f"{parent}/{new_name}" if parent else new_name


class SyncEngine:
    def __init__(
        self,
        api: SupernoteApiClient,
        state: StateDB,
        sync_root: Path,
        conflict_policy: str = "keep-both",
        event_log: EventLog | None = None,
        lock_file: Path | None = None,
    ) -> None:
        if conflict_policy not in POLICIES:
            raise ValueError(f"unknown conflict policy: {conflict_policy}")
        if event_log is not None and lock_file is None:
            raise ValueError("lock_file is required when event_log is set")
        self._api = api
        self._state = state
        self._root = sync_root
        self._policy = conflict_policy
        self._event_log = event_log
        self._lock_file = lock_file
        self._sync_pass: str | None = None

    @property
    def root(self) -> Path:
        return self._root

    def run_once(self) -> SyncResult:
        self._root.mkdir(parents=True, exist_ok=True)
        if self._event_log is None:
            return self._run_pass()
        assert self._lock_file is not None
        with file_lock(self._lock_file):
            # Orphan recovery (spec §1): an unsettled row at this point can
            # only belong to a dead pass (exception or hard crash) — its
            # action *did* execute, so settle it before this pass's own
            # events might be mistaken for it.
            self._event_log.settle_orphans()
            self._sync_pass = str(uuid.uuid4())
            try:
                return self._run_pass()
            finally:
                # Batch-settled semantics (spec §1): make this pass's events
                # visible to consumers only once the whole pass has finished
                # (or failed) — a partial pass still settles what it did
                # complete, via this `finally`.
                self._event_log.settle_pass(self._sync_pass)
                self._sync_pass = None

    def _run_pass(self) -> SyncResult:
        self._api.sync_start()
        result = SyncResult()
        try:
            local = scan_local(self._root)
            remote = remote_by_rel_path(self._api.list_all())
            known = self._state.all()
            plan = classify(local, remote, known)
            # Delete children before parents (deepest paths first).
            deletes_last = [i for i in plan if i.action.name.startswith("DELETE")]
            deletes_last.sort(key=lambda i: i.rel_path.count("/"), reverse=True)
            ordered = [i for i in plan if not i.action.name.startswith("DELETE")] + deletes_last
            for item in ordered:
                self._execute(item, result)
            self._api.sync_end("success")
        except Exception:
            self._api.sync_end("failed")
            raise
        return result

    def _emit(
        self,
        event_type: str,
        rel_path: str,
        content_hash: str,
        size: int,
        source: str,
        origin_equipment: str,
    ) -> None:
        if self._event_log is None:
            return
        assert self._sync_pass is not None
        self._event_log.append(
            event_type,
            rel_path,
            content_hash,
            size,
            source,
            origin_equipment,
            self._sync_pass,
        )

    def _execute(self, item: SyncItem, result: SyncResult) -> None:
        handler = {
            Action.UPLOAD: self._do_upload,
            Action.DOWNLOAD: self._do_download,
            Action.DELETE_LOCAL: self._do_delete_local,
            Action.DELETE_REMOTE: self._do_delete_remote,
            Action.MKDIR_LOCAL: self._do_mkdir_local,
            Action.MKDIR_REMOTE: self._do_mkdir_remote,
            Action.CONFLICT: self._do_conflict,
            Action.FORGET: self._do_forget,
            Action.RECORD: self._do_record,
        }[item.action]
        handler(item, result)

    # --- handlers ---

    def _do_upload(self, item: SyncItem, result: SyncResult) -> None:
        assert item.local is not None
        folder, _, name = item.rel_path.rpartition("/")
        entry = self._api.upload_file(item.local.abs_path, f"/{folder}", name)
        self._record_synced(item.rel_path, int(entry.id or 0), entry.content_hash or "")
        self._emit(
            "created" if item.known is None else "updated",
            item.rel_path,
            entry.content_hash or "",
            entry.size or 0,
            "sync-upload",
            self._api.equipment_no,
        )
        result.uploaded.append(item.rel_path)

    def _do_download(self, item: SyncItem, result: SyncResult) -> None:
        assert item.remote is not None
        dest = self._root / item.rel_path
        server_hash = self._api.download_file(int(item.remote.id or 0), dest)
        self._record_synced(item.rel_path, int(item.remote.id or 0), server_hash)
        self._emit(
            "created" if item.known is None else "updated",
            item.rel_path,
            server_hash,
            item.remote.size or 0,
            "sync-download",
            item.remote.last_modified_by or "",
        )
        result.downloaded.append(item.rel_path)

    def _do_delete_local(self, item: SyncItem, result: SyncResult) -> None:
        target = self._root / item.rel_path
        if target.is_dir():
            # Only remove if empty — a non-empty dir means files still syncing.
            try:
                target.rmdir()
            except OSError:
                return
        elif target.exists():
            target.unlink()
            # Files only — folder deletes (above) never emit.
            self._emit("deleted", item.rel_path, "", 0, "sync-download", "")
        self._state.remove(item.rel_path)
        result.deleted_local.append(item.rel_path)

    def _do_delete_remote(self, item: SyncItem, result: SyncResult) -> None:
        assert item.remote is not None
        self._api.delete(int(item.remote.id or 0))
        self._state.remove(item.rel_path)
        if item.remote.tag != "folder":
            self._emit("deleted", item.rel_path, "", 0, "sync-upload", "")
        result.deleted_remote.append(item.rel_path)

    def _do_mkdir_local(self, item: SyncItem, result: SyncResult) -> None:
        assert item.remote is not None
        (self._root / item.rel_path).mkdir(parents=True, exist_ok=True)
        self._record_folder(item.rel_path, int(item.remote.id or 0))

    def _do_mkdir_remote(self, item: SyncItem, result: SyncResult) -> None:
        node_id = self._api.create_folder(f"/{item.rel_path}")
        self._record_folder(item.rel_path, int(node_id))

    def _do_forget(self, item: SyncItem, result: SyncResult) -> None:
        self._state.remove(item.rel_path)

    def _do_record(self, item: SyncItem, result: SyncResult) -> None:
        if item.local is not None and item.local.is_folder:
            self._record_folder(item.rel_path, int((item.remote and item.remote.id) or 0))
            return
        assert item.local is not None and item.remote is not None
        self._record_synced(
            item.rel_path, int(item.remote.id or 0), item.remote.content_hash or ""
        )

    def _do_conflict(self, item: SyncItem, result: SyncResult) -> None:
        assert item.local is not None and item.remote is not None
        logger.warning("conflict on %s (policy: %s)", item.rel_path, self._policy)
        result.conflicts.append(item.rel_path)

        if self._policy == "local-wins":
            self._do_upload(item, result)
            return
        if self._policy == "remote-wins":
            self._do_download(item, result)
            return
        if self._policy == "newest-wins":
            remote_ms = item.remote.lastUpdateTime or 0
            local_ms = item.local.mtime_ns // 1_000_000
            if local_ms >= remote_ms:
                self._do_upload(item, result)
            else:
                self._do_download(item, result)
            return

        # keep-both: move the local file aside as a conflicted copy, upload it
        # (through _do_upload, so it emits its own 'created' event naturally —
        # no separate emission here, avoiding double-emission), then download
        # the remote version to the original name (via _do_download, which
        # emits 'updated' for it since item.known is already set).
        copy_rel = conflict_copy_name(item.rel_path, self._api.equipment_no)
        copy_abs = self._root / copy_rel
        item.local.abs_path.rename(copy_abs)
        stat = copy_abs.stat()
        copy_item = SyncItem(
            Action.UPLOAD,
            copy_rel,
            local=LocalFile(
                rel_path=copy_rel,
                abs_path=copy_abs,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                is_folder=False,
            ),
        )
        self._do_upload(copy_item, result)
        self._do_download(item, result)

    # --- state helpers ---

    def _record_synced(self, rel_path: str, server_id: int, server_hash: str) -> None:
        abs_path = self._root / rel_path
        stat = abs_path.stat()
        self._state.upsert(
            SyncedFile(
                rel_path=rel_path,
                server_id=server_id,
                server_hash=server_hash,
                local_hash=file_md5(abs_path),
                local_mtime_ns=stat.st_mtime_ns,
                local_size=stat.st_size,
                is_folder=False,
            )
        )

    def _record_folder(self, rel_path: str, server_id: int) -> None:
        self._state.upsert(
            SyncedFile(
                rel_path=rel_path,
                server_id=server_id,
                server_hash="",
                local_hash="",
                local_mtime_ns=0,
                local_size=0,
                is_folder=True,
            )
        )
