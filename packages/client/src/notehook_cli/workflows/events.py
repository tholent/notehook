"""Event log: durable, batch-settled record of sync actions.

Spec: docs/workflow-spec.md §1. stdlib `sqlite3`, not SQLModel (decision D1,
docs/workflow-implementation-plan.md) — the spec freezes literal SQL
(AUTOINCREMENT, indexes, explicit control over the settle/orphan
transactions), which raw sqlite3 expresses more faithfully than an ORM. This
module owns all SQL against `events.db`; nothing else touches the file.

Every connection is short-lived (opened, used, closed) and sets
`journal_mode=WAL` + `busy_timeout=5000` so that concurrent writers (a sync
pass and a `backfill`/`manual` command, or two threads in tests) retry
instead of raising `SQLITE_BUSY`.

This phase only implements the producer side (append/settle). Consumer-side
methods (claiming runs, cursor advancement) land in Phase 3 — the schema is
complete now so no migration is needed later.
"""

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

# Schema per spec §1, plus decision D8: `run` gets a denormalized `rel_path`
# column and `idx_run_install_path_status (install, rel_path, status)`
# replaces the spec's `idx_run_install_path (install, event_id)` — coalescing
# and per-(install, rel_path) serialization (Phase 3) query on rel_path
# constantly, and joining through `event` for it would make both the queries
# and the indexes worse.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    type         TEXT NOT NULL,              -- 'created' | 'updated' | 'deleted'
    rel_path     TEXT NOT NULL,              -- posix path relative to sync root
    content_hash TEXT NOT NULL DEFAULT '',   -- md5; '' for deleted
    size         INTEGER NOT NULL DEFAULT 0, -- bytes; 0 for deleted
    source       TEXT NOT NULL,              -- 'sync-download' | 'sync-upload'
                                             --   | 'backfill' | 'manual'
    origin_equipment TEXT NOT NULL DEFAULT '',
                                             -- equipment_no that made the change
                                             -- (this client's own for sync-upload)
    sync_pass    TEXT NOT NULL,              -- uuid; groups one engine pass
    settled      INTEGER NOT NULL DEFAULT 0, -- 0 until the pass ends; intake
                                             --   only consumes settled = 1
    target_install TEXT NOT NULL DEFAULT '', -- '' = fan out to all matching
                                             --   installs; set = this install
                                             --   only (`run` / `backfill`)
    created_at   INTEGER NOT NULL            -- epoch ms
);
CREATE INDEX IF NOT EXISTS idx_event_intake ON event(settled, id);

CREATE TABLE IF NOT EXISTS run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    install       TEXT NOT NULL,             -- install alias (see spec §5)
    workflow_name TEXT NOT NULL,             -- manifest name at time of run
    workflow_version TEXT,
    event_id      INTEGER NOT NULL REFERENCES event(id),
    rel_path      TEXT NOT NULL,             -- denormalized from event (D8)
    attempt       INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL,             -- 'queued' | 'running' | 'success'
                                             --   | 'failed' | 'retry'
                                             --   | 'superseded'
    next_attempt_at INTEGER,                 -- epoch ms, for 'retry'
    started_at    INTEGER,
    finished_at   INTEGER,
    exit_code     INTEGER,
    stdout        TEXT,                      -- truncated to 256 KiB
    stderr        TEXT                       -- truncated to 256 KiB
);
CREATE INDEX IF NOT EXISTS idx_run_claim ON run(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_run_install_path_status ON run(install, rel_path, status);

CREATE TABLE IF NOT EXISTS runner_meta (   -- single row
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cursor_event_id INTEGER NOT NULL DEFAULT 0
);
"""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True)
class EventRow:
    """One row of the `event` table (read-side representation)."""

    id: int
    type: str
    rel_path: str
    content_hash: str
    size: int
    source: str
    origin_equipment: str
    sync_pass: str
    settled: bool
    target_install: str
    created_at: int


class EventLog:
    """Owns all SQL against `events.db` (decision D1).

    Producer API only in this phase: `append`, `append_settled`,
    `settle_pass`, `settle_orphans`, plus `events_since`/`all_events` read
    helpers for tests. Consumer/claiming methods are Phase 3.
    """

    def __init__(self, db_file: Path) -> None:
        db_file.parent.mkdir(parents=True, exist_ok=True)
        self._db_file = db_file
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_file, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # --- producer API ---

    def append(
        self,
        event_type: str,
        rel_path: str,
        content_hash: str,
        size: int,
        source: str,
        origin_equipment: str,
        sync_pass: str,
        *,
        settled: bool = False,
        target_install: str = "",
    ) -> int:
        """Insert one event row, in the same moment as the caller's state
        upsert (spec §1). `settled` defaults to False: sync-pass events are
        only made visible to consumers once `settle_pass` runs at pass end.
        """
        return self._insert(
            event_type,
            rel_path,
            content_hash,
            size,
            source,
            origin_equipment,
            sync_pass,
            settled,
            target_install,
        )

    def append_settled(
        self,
        event_type: str,
        rel_path: str,
        content_hash: str,
        size: int,
        source: str,
        origin_equipment: str,
        sync_pass: str,
        *,
        target_install: str = "",
    ) -> int:
        """Insert an already-settled event (spec §1: `backfill`/`manual`
        events don't belong to a sync pass — the action they describe, the
        file existing, has already happened)."""
        return self._insert(
            event_type,
            rel_path,
            content_hash,
            size,
            source,
            origin_equipment,
            sync_pass,
            True,
            target_install,
        )

    def _insert(
        self,
        event_type: str,
        rel_path: str,
        content_hash: str,
        size: int,
        source: str,
        origin_equipment: str,
        sync_pass: str,
        settled: bool,
        target_install: str,
    ) -> int:
        with closing(self._connect()) as conn:
            cur = conn.execute(
                """
                INSERT INTO event (
                    type, rel_path, content_hash, size, source,
                    origin_equipment, sync_pass, settled, target_install, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    rel_path,
                    content_hash,
                    size,
                    source,
                    origin_equipment,
                    sync_pass,
                    int(settled),
                    target_install,
                    _now_ms(),
                ),
            )
            conn.commit()
            row_id = cur.lastrowid
            assert row_id is not None
            return row_id

    def settle_pass(self, sync_pass: str) -> None:
        """Mark every row of one sync pass settled (spec §1: run at pass
        end, in a `finally`, so a partial pass still settles what it did
        complete)."""
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE event SET settled = 1 WHERE sync_pass = ? AND settled = 0",
                (sync_pass,),
            )
            conn.commit()

    def settle_orphans(self) -> None:
        """Settle every unsettled row regardless of pass (spec §1 orphan
        recovery). Called at the start of `run_once()`, before that pass's
        own `sync_pass` exists: an unsettled row at that point can only
        belong to a dead pass (exception or hard crash) — its action *did*
        execute, so it must eventually become visible."""
        with closing(self._connect()) as conn:
            conn.execute("UPDATE event SET settled = 1 WHERE settled = 0")
            conn.commit()

    # --- read helpers (tests) ---

    def events_since(self, id_: int) -> list[EventRow]:
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM event WHERE id > ? ORDER BY id", (id_,)).fetchall()
            return [_row_to_event(row) for row in rows]

    def all_events(self) -> list[EventRow]:
        return self.events_since(0)


def _row_to_event(row: sqlite3.Row) -> EventRow:
    return EventRow(
        id=row["id"],
        type=row["type"],
        rel_path=row["rel_path"],
        content_hash=row["content_hash"],
        size=row["size"],
        source=row["source"],
        origin_equipment=row["origin_equipment"],
        sync_pass=row["sync_pass"],
        settled=bool(row["settled"]),
        target_install=row["target_install"],
        created_at=row["created_at"],
    )
