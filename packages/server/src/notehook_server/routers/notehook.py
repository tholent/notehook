"""notehook's own extension endpoints — deliberately namespaced outside the
reverse-engineered Supernote API (workflow-spec.md §7), so there are no
device-compatibility constraints and the request/response models live here
rather than in notehook-protocol (which mirrors the reverse-engineered spec only).

POST /api/notehook/changes: the server-side half of the change feed that
wakes the client sync engine soon after a device push, instead of waiting out
the client's periodic poll.
"""

import asyncio

from fastapi import APIRouter, Request
from pydantic import Field
from sqlalchemy import Engine
from sqlmodel import Session

from notehook_protocol.models.common import BaseVO, ProtocolModel
from notehook_server.auth.deps import CurrentDep
from notehook_server.files import change_service
from notehook_server.models import Change

router = APIRouter()

# D5: no cross-thread condition plumbing between the sync/threadpool mutation
# endpoints and this async endpoint — just re-poll on a short interval, well
# inside the ~1-2s freshness target.
_POLL_INTERVAL_SECONDS = 0.5


class ChangesDTO(ProtocolModel):
    since: int = 0
    limit: int = 500
    wait_seconds: int = 0


class ChangeRowVO(ProtocolModel):
    id: int
    op: str
    node_id: int
    path_display: str
    is_folder: bool
    content_hash: str | None = None
    equipment_no: str
    created_at: int


class ChangesVO(BaseVO):
    cursor: int = 0
    changes: list[ChangeRowVO] = Field(default_factory=list)


def _to_row(change: Change) -> ChangeRowVO:
    return ChangeRowVO(
        id=change.id or 0,
        op=change.op,
        node_id=change.node_id,
        path_display=change.path_display,
        is_folder=change.is_folder,
        content_hash=change.content_hash,
        equipment_no=change.equipment_no,
        created_at=change.created_at,
    )


def _fetch(engine: Engine, since: int, limit: int) -> tuple[int, list[Change]]:
    """One indexed query against a short-lived session — safe to call from
    inside the async long-poll loop without blocking the event loop for long.
    """
    with Session(engine) as session:
        cursor = change_service.latest_cursor(session)
        rows = change_service.since(session, since, limit)
        return cursor, rows


@router.post("/api/notehook/changes")
async def changes(dto: ChangesDTO, request: Request, _current: CurrentDep) -> ChangesVO:
    limit = min(max(dto.limit, 1), 500)
    wait_seconds = min(max(dto.wait_seconds, 0), 30)
    engine: Engine = request.app.state.engine

    if dto.since == 0:
        # Bootstrap: hand back the current cursor, never replay history.
        cursor, _rows = _fetch(engine, dto.since, limit)
        return ChangesVO(success=True, errorCode="0000", cursor=cursor, changes=[])

    cursor, rows = _fetch(engine, dto.since, limit)
    if not rows and wait_seconds > 0:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while not rows and loop.time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            cursor, rows = _fetch(engine, dto.since, limit)

    if rows:
        cursor = rows[-1].id or cursor
    return ChangesVO(
        success=True,
        errorCode="0000",
        cursor=cursor,
        changes=[_to_row(row) for row in rows],
    )
