"""File-tree operations over FileNode: path resolution, listing, mutations.

Paths are slash-separated, rooted at the virtual root (id=0). Resolution and
sibling uniqueness are case-insensitive (NOCASE collation on FileNode.name).
"""

import re

from sqlmodel import Session, func, select

from notehook_protocol.models.file import EntriesVO, MetadataVO
from notehook_server.errors import InvalidName, InvalidPath, NameConflict, NotFound
from notehook_server.files import change_service
from notehook_server.models import ROOT_ID, FileNode, now_ms

_FORBIDDEN_NAME = re.compile(r"[/\\\x00-\x1f]")


def validate_name(name: str) -> str:
    name = name.strip()
    if not name or name in {".", ".."} or _FORBIDDEN_NAME.search(name):
        raise InvalidName(f"invalid name: {name!r}")
    return name


def split_path(path: str) -> list[str]:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    return [validate_name(p) for p in parts]


def find_child(session: Session, user_id: int, parent_id: int, name: str) -> FileNode | None:
    return session.exec(
        select(FileNode).where(
            FileNode.owner_user_id == user_id,
            FileNode.parent_id == parent_id,
            func.lower(FileNode.name) == name.lower(),
        )
    ).one_or_none()


def resolve_path(session: Session, user_id: int, path: str) -> FileNode | None:
    """Resolve a path to its node. Returns None for the virtual root itself."""
    node: FileNode | None = None
    parent_id = ROOT_ID
    for part in split_path(path):
        node = find_child(session, user_id, parent_id, part)
        if node is None:
            raise NotFound(f"path not found: {path}")
        parent_id = node.id or 0
    return node


def get_node(session: Session, user_id: int, node_id: int) -> FileNode:
    node = session.get(FileNode, node_id)
    if node is None or node.owner_user_id != user_id:
        raise NotFound(f"no such file id: {node_id}")
    return node


def node_path(session: Session, node: FileNode) -> str:
    parts = [node.name]
    current = node
    while current.parent_id != ROOT_ID:
        parent = session.get(FileNode, current.parent_id)
        if parent is None:  # orphaned — shouldn't happen, but don't loop forever
            break
        parts.append(parent.name)
        current = parent
    return "/" + "/".join(reversed(parts))


def to_entry(session: Session, node: FileNode) -> EntriesVO:
    path = node_path(session, node)
    parent_path = path.rsplit("/", 1)[0] or "/"
    return EntriesVO(
        tag="folder" if node.is_folder else "file",
        id=str(node.id),
        name=node.name,
        path_display=path,
        content_hash=node.content_hash,
        is_downloadable=not node.is_folder,
        size=node.size,
        lastUpdateTime=node.last_update_time,
        parent_path=parent_path,
        last_modified_by=node.last_modified_by or "",
    )


def to_metadata(session: Session, node: FileNode) -> MetadataVO:
    return MetadataVO(
        tag="folder" if node.is_folder else "file",
        id=str(node.id),
        name=node.name,
        path_display=node_path(session, node),
    )


def list_children(session: Session, user_id: int, parent_id: int) -> list[FileNode]:
    return list(
        session.exec(
            select(FileNode)
            .where(FileNode.owner_user_id == user_id, FileNode.parent_id == parent_id)
            .order_by(FileNode.name)
        ).all()
    )


def list_entries(
    session: Session, user_id: int, parent_id: int, recursive: bool
) -> list[EntriesVO]:
    entries: list[EntriesVO] = []

    def walk(pid: int, prefix: str) -> None:
        for child in list_children(session, user_id, pid):
            path = f"{prefix}/{child.name}"
            entries.append(
                EntriesVO(
                    tag="folder" if child.is_folder else "file",
                    id=str(child.id),
                    name=child.name,
                    path_display=path,
                    content_hash=child.content_hash,
                    is_downloadable=not child.is_folder,
                    size=child.size,
                    lastUpdateTime=child.last_update_time,
                    parent_path=prefix or "/",
                    last_modified_by=child.last_modified_by or "",
                )
            )
            if recursive and child.is_folder:
                walk(child.id or 0, path)

    if parent_id == ROOT_ID:
        prefix = ""
    else:
        prefix = node_path(session, get_node(session, user_id, parent_id))
    walk(parent_id, prefix)
    return entries


def autorenamed(session: Session, user_id: int, parent_id: int, name: str) -> str:
    """Dropbox-style 'name (1)' suffixing until the name is free."""
    if find_child(session, user_id, parent_id, name) is None:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    suffix = f".{ext}" if ext and dot else ""
    n = 1
    while True:
        candidate = f"{stem} ({n}){suffix}"
        if find_child(session, user_id, parent_id, candidate) is None:
            return candidate
        n += 1


def _resolve_target_name(
    session: Session, user_id: int, parent_id: int, name: str, autorename: bool
) -> str:
    existing = find_child(session, user_id, parent_id, name)
    if existing is None:
        return name
    if autorename:
        return autorenamed(session, user_id, parent_id, name)
    raise NameConflict(f"name already exists: {name}")


def create_folder(
    session: Session,
    user_id: int,
    path: str,
    autorename: bool,
    equipment_no: str | None,
) -> FileNode:
    parts = split_path(path)
    if not parts:
        raise InvalidPath(code="E0334", msg="The path cannot be empty.")
    parent_id = ROOT_ID
    # Intermediate segments are created (or reused) without renaming; only the
    # leaf folder participates in autorename/conflict handling.
    for part in parts[:-1]:
        existing = find_child(session, user_id, parent_id, part)
        if existing is None:
            existing = _new_node(session, user_id, parent_id, part, True, equipment_no)
        elif not existing.is_folder:
            raise InvalidPath(f"path segment is a file: {part}")
        parent_id = existing.id or 0

    leaf = parts[-1]
    existing = find_child(session, user_id, parent_id, leaf)
    if existing is not None and existing.is_folder and not autorename:
        return existing  # idempotent create, matching Dropbox-ish semantics
    name = _resolve_target_name(session, user_id, parent_id, leaf, autorename)
    return _new_node(session, user_id, parent_id, name, True, equipment_no, record_as="create")


def ensure_folder(
    session: Session, user_id: int, path: str, equipment_no: str | None
) -> int:
    """Return the folder id for path, creating intermediate folders as needed."""
    parent_id = ROOT_ID
    for part in split_path(path):
        existing = find_child(session, user_id, parent_id, part)
        if existing is None:
            existing = _new_node(session, user_id, parent_id, part, True, equipment_no)
        elif not existing.is_folder:
            raise InvalidPath(f"path segment is a file: {part}")
        parent_id = existing.id or 0
    return parent_id


def _new_node(
    session: Session,
    user_id: int,
    parent_id: int,
    name: str,
    is_folder: bool,
    equipment_no: str | None,
    *,
    record_as: str | None = None,
) -> FileNode:
    """Create a node. When `record_as` is set, a Change row for it is appended
    atomically (same commit) — used only for mutations that are themselves the
    unit of change (e.g. the leaf folder in `create_folder`), not for folders
    implicitly created along the way (`ensure_folder`, intermediate segments).
    """
    node = FileNode(
        parent_id=parent_id,
        name=name,
        is_folder=is_folder,
        owner_user_id=user_id,
        last_modified_by=equipment_no,
    )
    session.add(node)
    session.flush()
    if record_as is not None:
        change_service.record(session, record_as, node, node_path(session, node), equipment_no)
    session.commit()
    session.refresh(node)
    return node


def delete_node(
    session: Session, user_id: int, node_id: int, equipment_no: str | None
) -> tuple[FileNode, list[str]]:
    """Hard-delete a node (and subtree). Returns (node, orphaned inner_names)."""
    node = get_node(session, user_id, node_id)
    path = node_path(session, node)  # snapshot before deletion
    inner_names: list[str] = []

    def collect(n: FileNode) -> None:
        if n.inner_name:
            inner_names.append(n.inner_name)
        for child in list_children(session, user_id, n.id or 0):
            collect(child)
            session.delete(child)

    collect(node)
    session.delete(node)
    change_service.record(session, "delete", node, path, equipment_no)
    session.commit()
    return node, inner_names


def _is_descendant(session: Session, user_id: int, node_id: int, candidate_parent: int) -> bool:
    current = candidate_parent
    while current != ROOT_ID:
        if current == node_id:
            return True
        parent = session.get(FileNode, current)
        if parent is None:
            return False
        current = parent.parent_id
    return False


def move_node(
    session: Session,
    user_id: int,
    node_id: int,
    to_path: str,
    autorename: bool,
    equipment_no: str | None,
) -> FileNode:
    """Move (and/or rename) a node. to_path is the full destination path."""
    node = get_node(session, user_id, node_id)
    parts = split_path(to_path)
    if not parts:
        raise InvalidPath("empty destination path")
    new_name = parts[-1]
    dest_parent = ensure_folder(session, user_id, "/".join(parts[:-1]), equipment_no)
    if node.is_folder and _is_descendant(session, user_id, node_id, dest_parent):
        raise InvalidPath("cannot move a folder into itself")
    existing = find_child(session, user_id, dest_parent, new_name)
    if existing is not None and existing.id != node.id:
        if not autorename:
            raise NameConflict(f"name already exists: {new_name}")
        new_name = autorenamed(session, user_id, dest_parent, new_name)
    node.parent_id = dest_parent
    node.name = new_name
    node.last_update_time = now_ms()
    node.version += 1
    node.last_modified_by = equipment_no
    session.add(node)
    session.flush()
    change_service.record(session, "move", node, node_path(session, node), equipment_no)
    session.commit()
    session.refresh(node)
    return node


def copy_node(
    session: Session,
    user_id: int,
    node_id: int,
    to_path: str,
    autorename: bool,
    equipment_no: str | None,
) -> FileNode:
    """Copy a node (recursively for folders) to the full destination path.

    Blob content is shared by inner_name (copy-on-write at the metadata level):
    blobs are immutable once written — modified files always arrive as new
    uploads with fresh inner_names — so sharing is safe as long as deletion
    only trashes a blob when no other node references it (upload_service checks).
    """
    node = get_node(session, user_id, node_id)
    parts = split_path(to_path)
    if not parts:
        raise InvalidPath("empty destination path")
    dest_parent = ensure_folder(session, user_id, "/".join(parts[:-1]), equipment_no)
    if node.is_folder and _is_descendant(session, user_id, node_id, dest_parent):
        raise InvalidPath("cannot copy a folder into itself")
    name = _resolve_target_name(session, user_id, dest_parent, parts[-1], autorename)

    def duplicate(src: FileNode, parent_id: int, name: str, is_root: bool) -> FileNode:
        clone = FileNode(
            parent_id=parent_id,
            name=name,
            is_folder=src.is_folder,
            size=src.size,
            content_hash=src.content_hash,
            inner_name=src.inner_name,
            owner_user_id=user_id,
            last_modified_by=equipment_no,
        )
        session.add(clone)
        session.flush()
        if is_root:
            # Only the copy root gets a Change row — children are implied,
            # matching how delete_node records only the deleted root.
            change_service.record(
                session, "copy", clone, node_path(session, clone), equipment_no
            )
        session.commit()
        session.refresh(clone)
        for child in list_children(session, user_id, src.id or 0):
            duplicate(child, clone.id or 0, child.name, False)
        return clone

    return duplicate(node, dest_parent, name, True)


def used_bytes(session: Session, user_id: int) -> int:
    total = session.exec(
        select(func.coalesce(func.sum(FileNode.size), 0)).where(
            FileNode.owner_user_id == user_id,
            FileNode.is_folder == False,  # noqa: E712
        )
    ).one()
    return int(total)


def has_any_files(session: Session, user_id: int) -> bool:
    return (
        session.exec(
            select(FileNode.id).where(FileNode.owner_user_id == user_id).limit(1)
        ).first()
        is not None
    )


def blob_referenced(session: Session, inner_name: str) -> bool:
    return (
        session.exec(
            select(FileNode.id).where(FileNode.inner_name == inner_name).limit(1)
        ).first()
        is not None
    )
