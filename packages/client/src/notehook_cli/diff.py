"""Three-way diff: local scan vs remote listing vs last-synced state.

The state db acts as the merge base, letting us distinguish "changed locally
only" / "changed remotely only" / true conflicts, and deletes from never-seen.
"""

from dataclasses import dataclass
from enum import Enum

from notehook_cli.scan import LocalFile
from notehook_cli.state_db import SyncedFile
from notehook_protocol.models.file import EntriesVO


class Action(Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    DELETE_LOCAL = "delete_local"
    DELETE_REMOTE = "delete_remote"
    MKDIR_LOCAL = "mkdir_local"
    MKDIR_REMOTE = "mkdir_remote"
    CONFLICT = "conflict"
    FORGET = "forget"  # drop stale state entry (gone on both sides)
    RECORD = "record"  # sides already agree; just update state


@dataclass
class SyncItem:
    action: Action
    rel_path: str
    local: LocalFile | None = None
    remote: EntriesVO | None = None
    known: SyncedFile | None = None


def remote_by_rel_path(entries: list[EntriesVO]) -> dict[str, EntriesVO]:
    return {(e.path_display or "").lstrip("/"): e for e in entries if e.path_display}


def classify(
    local: dict[str, LocalFile],
    remote: dict[str, EntriesVO],
    known: dict[str, SyncedFile],
) -> list[SyncItem]:
    items: list[SyncItem] = []
    for rel in sorted(local.keys() | remote.keys() | known.keys()):
        loc = local.get(rel)
        rem = remote.get(rel)
        base = known.get(rel)
        items.append(_classify_one(rel, loc, rem, base))
    # Order matters: folder creations before file transfers into them,
    # deletions last (children before parents is handled by the engine).
    priority = {
        Action.MKDIR_LOCAL: 0,
        Action.MKDIR_REMOTE: 0,
        Action.UPLOAD: 1,
        Action.DOWNLOAD: 1,
        Action.RECORD: 1,
        Action.CONFLICT: 1,
        Action.FORGET: 2,
        Action.DELETE_LOCAL: 3,
        Action.DELETE_REMOTE: 3,
    }
    items.sort(key=lambda i: (priority[i.action], i.rel_path))
    return items


def _classify_one(
    rel: str, loc: LocalFile | None, rem: EntriesVO | None, base: SyncedFile | None
) -> SyncItem:
    if loc is not None and loc.is_folder or rem is not None and rem.tag == "folder":
        return _classify_folder(rel, loc, rem, base)

    if base is None:
        if loc is not None and rem is None:
            return SyncItem(Action.UPLOAD, rel, local=loc)
        if loc is None and rem is not None:
            return SyncItem(Action.DOWNLOAD, rel, remote=rem)
        assert loc is not None and rem is not None
        if loc.content_hash() == rem.content_hash:
            return SyncItem(Action.RECORD, rel, local=loc, remote=rem)
        return SyncItem(Action.CONFLICT, rel, local=loc, remote=rem)

    local_changed = loc is not None and loc.content_hash(base) != base.local_hash
    remote_changed = rem is not None and rem.content_hash != base.server_hash

    if loc is None and rem is None:
        return SyncItem(Action.FORGET, rel, known=base)
    if loc is None:
        # Locally deleted. Remote edit wins over a local delete (data safety).
        if remote_changed:
            return SyncItem(Action.DOWNLOAD, rel, remote=rem, known=base)
        return SyncItem(Action.DELETE_REMOTE, rel, remote=rem, known=base)
    if rem is None:
        # Remotely deleted. A local edit wins over a remote delete.
        if local_changed:
            return SyncItem(Action.UPLOAD, rel, local=loc, known=base)
        return SyncItem(Action.DELETE_LOCAL, rel, local=loc, known=base)

    if not local_changed and not remote_changed:
        return SyncItem(Action.RECORD, rel, local=loc, remote=rem, known=base)
    if local_changed and not remote_changed:
        return SyncItem(Action.UPLOAD, rel, local=loc, remote=rem, known=base)
    if remote_changed and not local_changed:
        return SyncItem(Action.DOWNLOAD, rel, local=loc, remote=rem, known=base)
    if loc.content_hash(base) == rem.content_hash:
        return SyncItem(Action.RECORD, rel, local=loc, remote=rem, known=base)
    return SyncItem(Action.CONFLICT, rel, local=loc, remote=rem, known=base)


def _classify_folder(
    rel: str, loc: LocalFile | None, rem: EntriesVO | None, base: SyncedFile | None
) -> SyncItem:
    if loc is not None and rem is not None:
        return SyncItem(Action.RECORD, rel, local=loc, remote=rem, known=base)
    if loc is None and rem is None:
        return SyncItem(Action.FORGET, rel, known=base)
    if rem is None:
        if base is not None:  # was synced before and removed remotely
            return SyncItem(Action.DELETE_LOCAL, rel, local=loc, known=base)
        return SyncItem(Action.MKDIR_REMOTE, rel, local=loc)
    if base is not None:  # was synced before and removed locally
        return SyncItem(Action.DELETE_REMOTE, rel, remote=rem, known=base)
    return SyncItem(Action.MKDIR_LOCAL, rel, remote=rem)
