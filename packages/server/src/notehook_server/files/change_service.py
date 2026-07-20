"""Change feed (workflow-spec.md §7): append-only log of tree mutations.

`record()` adds a row without committing — the caller's own commit (of the
mutation the row describes) is what makes the append atomic with the change
it records. Every call site is responsible for calling this *before* its
final commit, inside the same session.
"""

from sqlmodel import Session, col, func, select

from notehook_server.models import Change, FileNode


def record(
    session: Session,
    op: str,
    node: FileNode,
    path_display: str,
    equipment_no: str | None,
) -> Change:
    change = Change(
        op=op,
        node_id=node.id or 0,
        path_display=path_display,
        is_folder=node.is_folder,
        content_hash=node.content_hash,
        equipment_no=equipment_no or "",
    )
    session.add(change)
    return change


def latest_cursor(session: Session) -> int:
    cursor = session.exec(select(func.max(Change.id))).one()
    return int(cursor) if cursor is not None else 0


def since(session: Session, cursor: int, limit: int) -> list[Change]:
    return list(
        session.exec(
            select(Change).where(col(Change.id) > cursor).order_by(col(Change.id)).limit(limit)
        ).all()
    )
