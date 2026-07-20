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

Phase 3e adds the consumer side: cursor read/advance, atomic intake (run
insertion + coalescing + cursor advance in one transaction), claiming,
finalization, crash recovery, and the housekeeping sweep. See
`workflows/runner.py` for the fan-out/execution logic built on top of this.
"""

import sqlite3
import threading
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


@dataclass(frozen=True)
class PendingRun:
    """One (event, install) match the intake fan-out decided to queue a run
    for (runner.py builds these; `EventLog.intake` inserts them atomically
    with the cursor advance)."""

    install: str
    workflow_name: str
    workflow_version: str | None
    event_id: int
    rel_path: str


@dataclass(frozen=True)
class RunRow:
    """One row of the `run` table (read-side representation, for tests —
    mirrors `EventRow`/`all_events`)."""

    id: int
    install: str
    workflow_name: str
    workflow_version: str | None
    event_id: int
    rel_path: str
    attempt: int
    status: str
    next_attempt_at: int | None
    started_at: int | None
    finished_at: int | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None


@dataclass(frozen=True)
class ClaimedRun:
    """One `run` row plus its underlying event, as returned by `claim_next`
    and `running_runs` — everything the runner needs to build the job
    payload and, later, finalize the row without a second query."""

    id: int
    install: str
    workflow_name: str
    workflow_version: str | None
    rel_path: str
    attempt: int
    event: EventRow


class EventLog:
    """Owns all SQL against `events.db` (decision D1).

    Producer API: `append`, `append_settled`, `settle_pass`,
    `settle_orphans`, plus `events_since`/`all_events` read helpers (Phase 2).

    Consumer API (Phase 3e): `read_cursor`/`unconsumed_settled` (intake
    input), `intake` (atomic insert-with-coalescing + cursor advance),
    `claim_next`/`running_runs` (claiming, incl. crash recovery's read side),
    `finalize_success`/`finalize_failed`/`finalize_retry`, and `sweep`
    (housekeeping). `runner.py` owns all policy (fan-out filters, retry
    backoff math); this class only guarantees the storage operations are
    atomic and race-free.
    """

    def __init__(self, db_file: Path) -> None:
        db_file.parent.mkdir(parents=True, exist_ok=True)
        self._db_file = db_file
        # Claiming is a read-modify-write ("find one eligible row, mark it
        # running") that must never let two threads claim the same row.
        # SQLite's own locking is coarse (whole-database, not row-level) and
        # only one runner *process* ever touches this table (guarded by the
        # runner's own flock — spec §6 crash-recovery assumes a single
        # runner), so a plain Python lock around the claim operation is
        # simplest and sufficient; no need for SQL-level `RETURNING` tricks.
        self._claim_lock = threading.Lock()
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

    def all_runs(self) -> list[RunRow]:
        """Every `run` row, for tests (mirrors `all_events`) — the consumer
        API otherwise only ever reads runs to act on them (`claim_next`,
        `running_runs`), never to enumerate the whole table."""
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM run ORDER BY id").fetchall()
            return [_row_to_run(row) for row in rows]

    # --- consumer API: intake (Phase 3e) ---

    def read_cursor(self) -> int:
        """The runner's current position (`runner_meta.cursor_event_id`); 0
        before the first `intake`."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT cursor_event_id FROM runner_meta WHERE id = 1").fetchone()
            return int(row[0]) if row is not None else 0

    def unconsumed_settled(self) -> list[EventRow]:
        """Settled rows past the cursor, as a **contiguous prefix**: stops at
        the first unsettled row rather than skipping it.

        `id > cursor AND settled = 1` alone (the spec §6 literal query) would
        let the cursor jump past a still-unsettled row with a lower id than a
        later settled one — a real possibility since several CLI processes
        write `event` concurrently (spec §1/D1: a daemon pass mid-flight,
        unsettled, racing a `backfill` command whose rows are settled at
        insert). If intake advanced the cursor to the max id of what it saw,
        that lower unsettled row would fall behind the new cursor and, once
        it *did* settle, would never satisfy `id > cursor` again — silently
        dropped forever. Stopping at the first gap means the cursor only ever
        advances over a run of rows already known-settled; the stalled row
        and everything after it are picked up together once it settles.
        """
        cursor = self.read_cursor()
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM event WHERE id > ? ORDER BY id", (cursor,)
            ).fetchall()
        result: list[EventRow] = []
        for row in rows:
            if not row["settled"]:
                break
            result.append(_row_to_event(row))
        return result

    def intake(
        self, pending: list[PendingRun], new_cursor: int, *, now_ms: int | None = None
    ) -> None:
        """Insert one `queued` run per `pending` entry and advance the cursor
        to `new_cursor`, atomically (one connection, one commit — spec §6
        "Advance the cursor in the same transaction").

        Coalescing (spec §6): before inserting a run for `(install,
        rel_path)`, any existing `queued`/`retry` run for that same pair is
        marked `superseded` first (with `finished_at` set, so it becomes
        eligible for the housekeeping sweep like any other terminal run). A
        `running` row is never touched — the new run simply queues behind it
        and becomes claimable once it finishes (`claim_next`'s
        not-already-running check).

        Called even when `pending` is empty: the cursor must still advance
        past events that matched no install (spec §6 amendment).
        """
        moment = now_ms if now_ms is not None else _now_ms()
        with closing(self._connect()) as conn:
            for run in pending:
                conn.execute(
                    """
                    UPDATE run SET status = 'superseded', finished_at = ?
                    WHERE install = ? AND rel_path = ? AND status IN ('queued', 'retry')
                    """,
                    (moment, run.install, run.rel_path),
                )
                conn.execute(
                    """
                    INSERT INTO run (
                        install, workflow_name, workflow_version, event_id, rel_path,
                        attempt, status
                    ) VALUES (?, ?, ?, ?, ?, 1, 'queued')
                    """,
                    (
                        run.install,
                        run.workflow_name,
                        run.workflow_version,
                        run.event_id,
                        run.rel_path,
                    ),
                )
            conn.execute(
                """
                INSERT INTO runner_meta (id, cursor_event_id) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET cursor_event_id = excluded.cursor_event_id
                """,
                (new_cursor,),
            )
            conn.commit()

    # --- consumer API: claiming (Phase 3e) ---

    _CLAIM_COLUMNS = """
        r.id AS run_id, r.install, r.workflow_name, r.workflow_version,
        r.rel_path, r.attempt, r.event_id,
        e.type AS event_type, e.content_hash, e.size, e.source,
        e.origin_equipment, e.sync_pass, e.settled, e.target_install, e.created_at
    """

    def claim_next(self, now_ms: int) -> ClaimedRun | None:
        """Atomically pick one eligible run and mark it `running`.

        Eligible: `status = 'queued'`, or `status = 'retry'` with
        `next_attempt_at <= now_ms`, for an `(install, rel_path)` pair with
        nothing currently `running` (per-path serialization, spec §6). Picks
        the oldest eligible row (`ORDER BY r.id`). Serialized through
        `self._claim_lock` so two threads never claim the same row.
        """
        with self._claim_lock, closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"""
                SELECT {self._CLAIM_COLUMNS}
                FROM run r JOIN event e ON e.id = r.event_id
                WHERE (
                    r.status = 'queued'
                    OR (r.status = 'retry' AND r.next_attempt_at <= ?)
                )
                AND NOT EXISTS (
                    SELECT 1 FROM run r2
                    WHERE r2.install = r.install AND r2.rel_path = r.rel_path
                      AND r2.status = 'running'
                )
                ORDER BY r.id
                LIMIT 1
                """,
                (now_ms,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE run SET status = 'running', started_at = ? WHERE id = ?",
                (now_ms, row["run_id"]),
            )
            conn.commit()
            return _row_to_claimed_run(row)

    def running_runs(self) -> list[ClaimedRun]:
        """Every row currently `status = 'running'` — the crash-recovery read
        side (spec §6): at startup, any such row belongs to a dead process
        (this process just started), and `runner.py` reschedules it under
        the normal retry policy."""
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT {self._CLAIM_COLUMNS}
                FROM run r JOIN event e ON e.id = r.event_id
                WHERE r.status = 'running'
                ORDER BY r.id
                """
            ).fetchall()
            return [_row_to_claimed_run(row) for row in rows]

    # --- consumer API: finalize (Phase 3e) ---

    def finalize_success(
        self, run_id: int, exit_code: int | None, stdout: str, stderr: str, finished_at_ms: int
    ) -> None:
        self._set_terminal(run_id, "success", exit_code, stdout, stderr, finished_at_ms)

    def finalize_failed(
        self, run_id: int, exit_code: int | None, stdout: str, stderr: str, finished_at_ms: int
    ) -> None:
        self._set_terminal(run_id, "failed", exit_code, stdout, stderr, finished_at_ms)

    def finalize_retry(
        self,
        run_id: int,
        next_attempt: int,
        next_attempt_at_ms: int,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        finished_at_ms: int,
    ) -> None:
        """Reschedule the same row for another attempt: increments `attempt`
        to `next_attempt`, sets `next_attempt_at`, and records this attempt's
        exit_code/stdout/stderr/finished_at (overwriting the previous
        attempt's, matching the run log's one-row-per-(install,rel_path)-
        chain shape rather than one row per attempt)."""
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE run
                SET status = 'retry', attempt = ?, next_attempt_at = ?,
                    exit_code = ?, stdout = ?, stderr = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    next_attempt,
                    next_attempt_at_ms,
                    exit_code,
                    stdout,
                    stderr,
                    finished_at_ms,
                    run_id,
                ),
            )
            conn.commit()

    def _set_terminal(
        self,
        run_id: int,
        status: str,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        finished_at_ms: int,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE run
                SET status = ?, exit_code = ?, stdout = ?, stderr = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, exit_code, stdout, stderr, finished_at_ms, run_id),
            )
            conn.commit()

    # --- housekeeping (Phase 3e) ---

    def sweep(self, retention_days: int, *, now_ms: int | None = None) -> None:
        """Delete `run` rows older than `retention_days` (by `finished_at`,
        terminal statuses only: `success`/`failed`/`superseded` — all three
        set `finished_at`), then `event` rows older than `retention_days`
        with zero referencing `run` rows.

        Run rows first, then event rows, deliberately (spec §8 "Housekeeping"
        + plan Phase 3e): a `run` references its `event` via `event_id`, so
        sweeping events first could delete a row a not-yet-swept run still
        points at.
        """
        moment = now_ms if now_ms is not None else _now_ms()
        cutoff = moment - retention_days * 86_400_000
        with closing(self._connect()) as conn:
            conn.execute(
                """
                DELETE FROM run
                WHERE finished_at IS NOT NULL AND finished_at < ?
                  AND status IN ('success', 'failed', 'superseded')
                """,
                (cutoff,),
            )
            conn.execute(
                """
                DELETE FROM event
                WHERE created_at < ?
                  AND id NOT IN (SELECT event_id FROM run)
                """,
                (cutoff,),
            )
            conn.commit()


def _row_to_claimed_run(row: sqlite3.Row) -> ClaimedRun:
    return ClaimedRun(
        id=row["run_id"],
        install=row["install"],
        workflow_name=row["workflow_name"],
        workflow_version=row["workflow_version"],
        rel_path=row["rel_path"],
        attempt=row["attempt"],
        event=EventRow(
            id=row["event_id"],
            type=row["event_type"],
            rel_path=row["rel_path"],
            content_hash=row["content_hash"],
            size=row["size"],
            source=row["source"],
            origin_equipment=row["origin_equipment"],
            sync_pass=row["sync_pass"],
            settled=bool(row["settled"]),
            target_install=row["target_install"],
            created_at=row["created_at"],
        ),
    )


def _row_to_run(row: sqlite3.Row) -> RunRow:
    return RunRow(
        id=row["id"],
        install=row["install"],
        workflow_name=row["workflow_name"],
        workflow_version=row["workflow_version"],
        event_id=row["event_id"],
        rel_path=row["rel_path"],
        attempt=row["attempt"],
        status=row["status"],
        next_attempt_at=row["next_attempt_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        exit_code=row["exit_code"],
        stdout=row["stdout"],
        stderr=row["stderr"],
    )


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
