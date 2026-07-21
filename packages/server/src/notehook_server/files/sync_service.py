"""Single-device sync lock (spec: FileErrorCodeEnum E0078 / E0079 / E0301).

The real cloud lets only one device sync at a time: a second device's syncStart
is rejected with E0078, and any mutating file operation while another device
holds an active session is rejected with E0079. The lock lives in the
``SyncSession`` table. A session older than the TTL is treated as abandoned (the
device crashed mid-sync) so the lock self-heals instead of wedging forever.
"""

from sqlmodel import Session, select

from notehook_server.errors import SyncInProgress
from notehook_server.models import SyncSession, now_ms


def _active_other(session: Session, equipment_no: str, ttl_ms: int) -> SyncSession | None:
    """The newest non-expired active session held by a *different* device."""
    cutoff = now_ms() - ttl_ms
    return session.exec(
        select(SyncSession)
        .where(
            SyncSession.status == "active",
            SyncSession.equipment_no != equipment_no,
            SyncSession.started_at >= cutoff,
        )
        .order_by(SyncSession.started_at.desc())  # type: ignore[attr-defined]
    ).first()


def begin_sync(session: Session, equipment_no: str, ttl_ms: int) -> SyncSession:
    """Open a sync session, rejecting with E0078 if another device holds one."""
    if _active_other(session, equipment_no, ttl_ms) is not None:
        raise SyncInProgress(
            code="E0078",
            msg="A device is currently synchronizing. Please wait until it's "
            "finished before synchronizing again!",
        )
    # Close any prior/stale active session for this same device before opening a
    # fresh one, so a device that never sent syncEnd can't leak active rows.
    for prior in session.exec(
        select(SyncSession).where(
            SyncSession.status == "active",
            SyncSession.equipment_no == equipment_no,
        )
    ).all():
        prior.status = "completed"
        prior.ended_at = now_ms()
        session.add(prior)
    row = SyncSession(equipment_no=equipment_no)
    session.add(row)
    session.commit()
    return row


def end_sync(session: Session, equipment_no: str, flag: str | None) -> SyncSession | None:
    row = session.exec(
        select(SyncSession)
        .where(SyncSession.equipment_no == equipment_no, SyncSession.status == "active")
        .order_by(SyncSession.started_at.desc())  # type: ignore[attr-defined]
    ).first()
    if row is not None:
        row.status = "completed"
        row.ended_at = now_ms()
        row.flag = flag
        session.add(row)
        session.commit()
    return row


def guard_not_syncing(session: Session, equipment_no: str, ttl_ms: int) -> None:
    """Raise E0079 if a *different* device is mid-sync (mutations are blocked)."""
    if _active_other(session, equipment_no, ttl_ms) is not None:
        raise SyncInProgress()
