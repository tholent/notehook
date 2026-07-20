# notehook workflows — implementation plan

Companion to [workflow-spec.md](workflow-spec.md) (v1 draft, gaps 1–2 resolved:
settled flag + `target_install`). This plan turns the spec's seven coarse
phases into PR-sized work packages with concrete files, wiring points, tests,
and acceptance criteria, grounded in the code as it exists today.

Ground rules (from repo conventions):

- Every phase lands green through `make check` (ruff, strict mypy, per-package
  coverage gates: protocol 90 / server 85 / client 80).
- No new packages: the SDK + runner live in `notehook-cli`, server pieces in
  `notehook-server`, the one additive VO field in `notehook-protocol`. No
  Makefile/CI changes needed.
- Server tests extend `FakeDevice`; client tests run against the real server
  app in-process (existing `conftest.py` fixtures).

---

## 0. Cross-cutting decisions (made here, not in the spec)

These are implementation choices the spec leaves open; recorded once so the
phases below can reference them.

**D1 — `events.db` access layer: stdlib `sqlite3`, not SQLModel.**
The spec freezes literal SQL (AUTOINCREMENT, CHECK, partial control over
transactions for claim/cursor updates). Raw `sqlite3` with explicit
`PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` on every connection
is simpler and more faithful than mapping through the ORM. One module owns
all SQL (`EventLog` class); nothing else touches the file. `state.db` stays
SQLModel — no migration.

**D2 — `events.db` path derives from `ClientConfig.config_dir`.**
New properties on `ClientConfig` ([config.py](../packages/client/src/notehook_cli/config.py)):
`events_db_file`, `workflows_dir`, `workflow_config_dir`, `runner_lock_file`.
Never hardcode `~/.config/notehook/` — the test fixtures point `--config-dir`
at tmp dirs and everything must follow.

**D3 — module layout (client package).**

```
packages/client/src/notehook_cli/workflows/
  __init__.py
  events.py        # EventLog: schema, append/settle/claim/cursor, sweeps (D1)
  manifest.py      # PEP 723 + pyproject [tool.notehook] parsing/validation
  installs.py      # install dir + <alias>.toml config: load/validate/write
  harness.py       # generated harness script + uv invocation building
  executor.py      # one job: spawn, timeout, capture, exit-code protocol
  runner.py        # serve loop: intake, coalescing, claiming, retries, sweep
  cli.py           # typer sub-app `notehook workflows …`
  _sdk/
    notehook_workflow.py   # THE SDK: single pure-stdlib module (ships as data)
packages/client/src/notehook_cli/lock.py   # flock helper (engine + runner)
```

`workflows/cli.py` exposes `workflows_app = typer.Typer()`; registered in
[cli.py](../packages/client/src/notehook_cli/cli.py) via
`app.add_typer(workflows_app, name="workflows")`.

**D4 — SDK delivery.** `_sdk/notehook_workflow.py` is included in the wheel
(hatch packages `src/notehook_cli`, so it ships automatically). The runner
sets `PYTHONPATH` to the `_sdk` directory when spawning; the harness does
`import notehook_workflow`. The module is pure stdlib and must not import
anything from `notehook_cli` — enforce with a test that imports it in a
subprocess with an empty `sys.path` + stdlib.

**D5 — long-poll implementation: async endpoint with short internal poll.**
`/api/notehook/changes` is `async def` and, when `wait_seconds > 0`, loops
`await asyncio.sleep(0.5)` re-checking `change` for rows past `since` until
the deadline. No cross-thread condition plumbing (the mutation endpoints are
sync/threadpool — notifying an asyncio primitive from them is fiddly); 0.5 s
granularity is well inside the ~1–2 s freshness target and costs one indexed
query per tick per waiting client (exactly one client in practice). The
server's single-worker constraint is unaffected: the endpoint yields while
waiting.

**D6 — subprocess seam for testability.** `executor.py` takes an `invoke`
callable (build argv + env → `subprocess.Popen`). Unit tests inject an
invoker that runs plain `python` scripts directly — no uv in unit tests.
Exactly one integration test (marked `@pytest.mark.uv`, skipped when `uv`
is not on PATH) goes through the real harness + `uv run` path.

**D7 — capture middleware exclusion.** `RequestCaptureMiddleware` gets a
prefix skip-list including `/api/notehook/` — otherwise the daemon's 25 s
long-poll writes a capture line every 25 s forever.

**D8 — run-table shape deviation from the spec (record in spec at the end).**
`run` gains a denormalized `rel_path` column: coalescing and the
per-`(install, rel_path)` serialization query it constantly, and joining
through `event` for it makes both the queries and the indexes worse. Replace
the spec's `idx_run_install_path (install, event_id)` with
`(install, rel_path, status)`. Update the spec when Phase 3 lands.

---

## Phase 1 — protocol + server groundwork

*Spec §7 (server half) + per-device attribution. Independently shippable;
nothing client-side depends on it until Phase 6.*

### 1a. `last_modified_by` on `EntriesVO`

- [protocol/models/file.py](../packages/protocol/src/notehook_protocol/models/file.py):
  add `last_modified_by: str | None = None` to `EntriesVO`. Additive; extras
  are already ignored by `ProtocolModel`, and this is our own extension field
  (document with a comment: *not* in `specs/` — notehook extension, snake_case
  to match neighboring extension-style fields).
- [server tree_service.py](../packages/server/src/notehook_server/files/tree_service.py):
  populate it in `to_entry()` and the `EntriesVO` constructed inside
  `list_entries().walk()` from `node.last_modified_by or ""`.
- Tests (server): `FakeDevice` uploads as equipment A, lists as equipment B →
  entry carries A's `equipment_no`. Also: legacy node with
  `last_modified_by=None` serializes as `""`/absent without error.

### 1b. `change` table + appends

- [server models.py](../packages/server/src/notehook_server/models.py): new
  `Change` SQLModel table per spec §7 (op, node_id, path_display snapshot,
  is_folder, content_hash, equipment_no, created_at). Index on `id` is the
  PK; no other index needed (reads are `WHERE id > :since ORDER BY id`).
- New `files/change_service.py`: `record(session, op, node, path_display,
  equipment_no)` — adds a `Change` row **without committing** (caller's
  commit makes it atomic with the mutation), plus `latest_cursor(session)`
  and `since(session, cursor, limit)`.
- Wire into the five mutation sites, inside the existing sessions **before**
  their final commit:
  - `upload_service.finish()` (both branches: replace-in-place = `update`,
    new node = `create`) — [upload_service.py:167](../packages/server/src/notehook_server/files/upload_service.py#L167)
  - `tree_service.delete_node()` — snapshot `node_path()` *before* deleting
    (op `delete`, one row for the deleted root only; children are implied,
    matching how the client diff sees it)
  - `tree_service.move_node()` (op `move`, new path snapshot)
  - `tree_service.copy_node()` (op `copy`, one row for the copy root)
  - `tree_service.create_folder()` / `_new_node` folder leaf (op `create`,
    `is_folder=True`)
- Tests: each FakeDevice mutation appends exactly one row with correct
  op/path/equipment; a failed mutation (e.g. hash-mismatch upload finish)
  appends nothing (atomicity).

### 1c. `/api/notehook/changes` endpoint

- New `routers/notehook.py`, mounted in
  [main.py](../packages/server/src/notehook_server/main.py) before the
  catch-all. Auth via existing `CurrentDep` (`x-access-token`).
- Request/response models live in the **server** package (`notehook_server`),
  not `notehook_protocol` — this is not part of the reverse-engineered spec
  and must not pollute the protocol package's "mirror of `specs/`" rule.
  Envelope still follows `BaseVO` semantics (HTTP 200, `success` flag) via a
  local `ChangesVO(BaseVO)`.
- Semantics per spec: `since=0` → return current cursor, no rows;
  `wait_seconds` clamped to [0, 30], long-poll per **D5**; `limit` clamped to
  [1, 500].
- **D7**: add the `/api/notehook/` prefix skip to
  [capture_middleware.py](../packages/server/src/notehook_server/debug/capture_middleware.py).
- Tests: bootstrap (`since=0`); incremental fetch returns rows past cursor in
  order; `wait_seconds` returns early when a change lands mid-wait (drive the
  mutation from a second thread against `TestClient`, or pre-insert with a
  short wait); auth required; catch-all still answers unknown
  `/api/notehook/*` subpaths with 9999.

**Acceptance**: `make check` green; server coverage ≥ 85 including the new
modules; a FakeDevice sequence upload→move→delete replays correctly through
the endpoint.

---

## Phase 2 — event log + engine emission (client)

*Spec §1 exactly, including the settled-flag durability contract. The heart
of the system; land it with the durability tests it exists for.*

### 2a. `EventLog` (`workflows/events.py`, per D1)

- Schema from spec §1 verbatim (`event`, `run`, `runner_meta` + indexes,
  plus D8's `rel_path` on `run`), created idempotently on open
  (`CREATE TABLE IF NOT EXISTS` — migration-free, like `StateDB`).
- API (producer side, used this phase):
  `append(event_row, settled=False)`, `settle_pass(sync_pass)`,
  `settle_orphans()`, `append_settled(...)` (for `backfill`/`manual` later).
  Consumer-side methods (`unconsumed()`, claiming, cursor) are added in
  Phase 3 but the schema is complete now.
- Every connection: WAL + `busy_timeout` pragmas.

### 2b. `lock.py`

`flock`-based context manager (`fcntl.flock`, exclusive, non-blocking with a
short retry loop). Used around each engine pass; later reused by the runner's
single-instance guard.

### 2c. Engine emission

- [engine.py](../packages/client/src/notehook_cli/engine.py): `SyncEngine`
  gains an optional `event_log: EventLog | None = None` (None = feature off —
  keeps every existing test untouched).
- `run_once()`: take the pass lock; `settle_orphans()` first (spec §1 orphan
  recovery); generate `sync_pass` UUID; in a `finally`, `settle_pass()` (the
  `finally` closes the common exception path immediately; orphan recovery
  covers kill-9).
- Emission points, per the spec §1 table:
  - `_do_download` → `created`/`updated` (by `item.known` presence), source
    `sync-download`, `origin_equipment` from
    `item.remote.last_modified_by or ""` (Phase 1a field; absent on old
    servers → `""`)
  - `_do_upload` → `created`/`updated`, source `sync-upload`,
    `origin_equipment = api.equipment_no`
  - `_do_delete_local` / `_do_delete_remote` → `deleted` (files only — skip
    the `is_dir()` branch), hash `""`, size 0
  - `_do_conflict` keep-both → `created` for the conflicted copy (its
    `_do_upload` call emits it naturally — verify, don't double-emit; the
    subsequent `_do_download` emits the `updated` for the original name)
  - `RECORD` / `MKDIR_*` / `FORGET` → nothing
- Each emit happens in the handler right after the state upsert (spec:
  "in the same moment").
- [cli.py](../packages/client/src/notehook_cli/cli.py) `_make_engine`: pass
  `EventLog(config.events_db_file)`.

### 2d. Tests (the reason this phase exists)

- Full-pass matrix against the in-process server: download-new / download-
  changed / upload-new / upload-changed / delete both directions / conflict
  keep-both → exact expected event rows (type, source, hash, origin,
  settled=1, shared `sync_pass`).
- Hash-equal touch → no event. Folders → no events.
- **Durability**: monkeypatch `_do_download` to fail on file N of M → events
  for files 1..N-1 exist and are settled (finally path); simulate hard crash
  (emit with settle suppressed) → next `run_once()` settles orphans before
  emitting its own events.
- Two-equipment attribution via the second `make_api(...)` fixture: device A
  uploads, client B syncs → B's event has `origin_equipment == A`.
- Concurrent-writer smoke: two threads appending (daemon pass + backfill
  shape) with busy_timeout → no `SQLITE_BUSY` escapes.

**Acceptance**: `make check` green; existing sync tests unmodified and
passing; the failed-pass-partway case from spec §1 is a named test.

---

## Phase 3 — SDK, harness, executor, runner core

*Spec §2 (frozen author API), §6 lifecycle. Largest phase; no CLI surface
yet — everything drivable from tests.*

### 3a. SDK (`_sdk/notehook_workflow.py`, per D4)

- `@workflow(on=[...])` decorator (registers into a module-level registry;
  default `["created", "updated"]`), `RetryLater(Exception)`,
  `secret(name)` (reads `NOTEHOOK_SECRET_<NAME_UPPER>`; `None` when unset),
  `Event` dataclass with the eleven frozen fields (spec §2 table),
  constructed from the JSON payload with unknown keys ignored.
- Dispatch entry `notehook_workflow._main()`: read `NOTEHOOK_PAYLOAD_FILE`,
  import the target workflow module (path via `NOTEHOOK_WORKFLOW_FILE` env),
  call every registered handler whose `on` includes the event type, in
  definition order; map outcomes to the exit-code protocol (0 / 1 with
  traceback on stderr / **75** on `RetryLater`).
- Purity test per D4 (subprocess import with stdlib-only path).

### 3b. Manifest parsing (`workflows/manifest.py`)

- PEP 723 block extraction (regex per the PEP's reference implementation) +
  `tomllib`; `pyproject.toml` `[tool.notehook]` for packages; package form
  wins. Validation into a frozen `Manifest` dataclass: name/version/
  description/entry/timeout/retry/suggested_paths/inputs/secrets, defaults
  per spec §3. Unknown keys inside `[tool.notehook]` → warning, not error.
- Tests: golden good/bad examples incl. the spec §3 and §9 blocks verbatim;
  minimal file with no `[tool.notehook]` at all.

### 3c. Install config (`workflows/installs.py`)

- `InstallConfig` from `workflow-config/<alias>.toml`: workflow/source/
  enabled/paths/on/skip_own_changes + `[inputs]`/`[secrets]` (spec §5).
  Loader pairs it with the manifest from `workflows/<alias>/`, cross-checks
  `workflow` name, validates inputs (missing required → install marked
  broken; unknown → warn; defaults filled), and compiles `paths` globs
  (`fnmatch`-style against `rel_path`, `**` semantics via
  `pathlib.PurePosixPath.full_match`).
- `discover(config)` → `dict[alias, Install | BrokenInstall]`; broken
  installs carry their error for `list`/logs and are skipped at intake
  (spec §6 hot-reload rule).

### 3d. Harness + executor (`workflows/harness.py`, `executor.py`)

- Harness generator: temp dir per job; single-file → copy of the PEP 723
  block + `import notehook_workflow; notehook_workflow._main()`; package →
  argv `uv run --project <dir> <harness>`. Env: `PYTHONPATH=<_sdk dir>`,
  `NOTEHOOK_PAYLOAD_FILE`, `NOTEHOOK_WORKFLOW_FILE`,
  `NOTEHOOK_SECRET_*` from install config. Payload JSON: the event row +
  `attempt` + resolved config dict.
- Executor: spawn via the **D6** invoker; wait with manifest timeout;
  on expiry SIGTERM → 10 s → SIGKILL, classify as retry (spec §6); capture
  stdout/stderr, truncate at 256 KiB; return
  `(outcome, exit_code, stdout, stderr)` where outcome ∈
  success/failed/retry.

### 3e. Runner core (`workflows/runner.py`)

- **Intake**: `EventLog` consumer methods — settled rows past cursor;
  fan-out per install filters (globs + effective `on` = decorator∩config);
  `target_install` honored (manual bypasses filters; backfill keeps the type
  filter) — spec §6 as amended. Cursor advance + run inserts in one
  transaction. Coalescing: pending (`queued`/`retry`) run for same
  `(install, rel_path)` → mark `superseded`; never touch `running`.
- **Claim/execute loop**: claim oldest eligible run (`queued`, or `retry`
  with `next_attempt_at <= now`) whose `(install, rel_path)` has nothing
  `running`; mark `running` + `started_at`; execute; finalize status.
  `max_parallel` (default 2) via `ThreadPoolExecutor`; claims serialized
  through `EventLog`.
- **Retry**: exit 75 / timeout → backoff `min(base·4^(attempt−1), cap)`,
  manifest overrides, exhausted → `failed` (spec §6 numbers).
- **Startup recovery**: `running` rows → treated as timed out, rescheduled
  under retry policy. Runner takes its own flock (one runner per config
  dir).
- **Housekeeping**: daily sweep — old `run` rows first, then `event` rows
  older than retention with no remaining runs (FK-safe order), per
  `[workflows]` settings (Phase 5 wires config; constants until then).
- Tests (all through stub workflows + D6 invoker, driving `run_pending()` /
  `poll_step()` directly — no threads/sleeps in unit tests; backoff clock
  injected):
  fan-out incl. targeting and skip_own_changes; coalescing incl. the
  running-not-superseded case; per-path serialization vs cross-path
  parallelism; exit-code protocol incl. 75; timeout kill; retry schedule
  arithmetic; crash recovery; broken-install skip; sweep FK order.
  Plus the one real-`uv` integration test (D6) running the spec §9 example
  against a fake X4 HTTP server (`http.server` on localhost).

**Acceptance**: `make check` green (client gate 80 will demand most of this
be covered — the D6 seam exists precisely for that); frozen-contract tests
named after spec sections so contract drift is loud.

---

## Phase 4 — install/configure CLI verbs

*Spec §5 + §8 (management half).*

- `workflows/cli.py` sub-app (registered per D3): `install`, `configure`,
  `enable`, `disable`, `remove`, `update`, `list`.
- `install`: git URL → `git clone` (subprocess, depth 1) / local dir → copy,
  into `config.workflows_dir/<alias>`; alias collision → error naming
  `--as` (spec §5); manifest parse + **disclosure block** (inputs, secrets,
  deps, unsandboxed-execution notice — spec §3); interactive prompts via
  typer (`--input k=v`, `--secret k=v`, `--paths`, `--yes` flags for
  non-interactive use and for tests); write `<alias>.toml` chmod 0600.
- `update`: `git pull` for git-sourced installs (copied dirs: error with
  hint); revalidate; prompt only for newly-required inputs/secrets.
- `remove`: delete clone + config; prompt about run history (`--keep-runs`).
- `list`: rich table — alias, name, version, enabled, paths, broken-reason,
  last run status (query via `EventLog`).
- Tests: pattern from existing `test_cli.py` (typer `CliRunner`,
  `--config-dir` at tmp): install from a local fixture dir end-to-end incl.
  disclosure output and 0600 perms; collision; double-install under two
  aliases with distinct configs; update re-prompt; broken manifest → clear
  error, exit ≠ 0. Git paths tested against local `file://` repos created
  in-test (no network).

**Acceptance**: `make check` green; a user can go from fixture workflow dir
to a valid, listed install entirely offline.

---

## Phase 5 — serve daemon + run/backfill/logs

*Spec §6 serve loop + §8 (operational half).*

- `serve`: poll loop (default 2 s; `[workflows]` config) around Phase 3's
  intake/execute; `watchfiles` on `workflows_dir` + `workflow_config_dir`
  re-discovers only affected installs (bad reload → broken-marked, daemon
  survives — spec §6); SIGINT/SIGTERM clean shutdown (finish running jobs,
  no new claims); housekeeping timer.
- `[workflows]` section in `config.toml` (`poll_interval`, `max_parallel`,
  `retention_days`) — extend `ClientConfig.load/save` (nested table;
  loader currently flat — small refactor, keep old keys top-level).
- `run <alias> --path <file>`: resolve path against sync root; type from
  `StateDB` row presence (spec §8); `append_settled` with
  `target_install=alias`, source `manual`. Prints the created event id; with
  `--wait`, tails the run to completion and exits nonzero on failure
  (CI-friendly).
- `backfill <alias> [--glob G]`: walk sync root through the install's globs
  (∩ `--glob`); `created`/`backfill` targeted events, `settled=1`; report
  count.
- `logs [--alias] [--failed] [--follow] [-n]`: reads `run` joined to
  `event`; `--follow` polls. Truncated-output marker when 256 KiB cap hit.
- Tests: serve loop driven with a tiny poll interval against stub workflows
  (real threads, bounded waits on observable DB state — no bare sleeps);
  hot-reload by rewriting an install config mid-run; targeting proof:
  backfill of A does not queue runs for overlapping install B (the Gap-2
  regression test); `run --wait` exit codes; logs filters.

**Acceptance**: `make check` green; demo script (documented in the phase PR):
`serve` + `daemon` in two terminals, touch a file in a watched folder, watch
the stub workflow fire and `logs` show it.

---

## Phase 6 — change-feed trigger in the sync daemon

*Spec §7 client half. Depends on Phase 1; independent of Phases 3–5.*

- [api_client.py](../packages/client/src/notehook_cli/api_client.py): add
  `changes(since, limit, wait_seconds)` → `(cursor, rows)`; raise the
  endpoint's 9999 (catch-all) as a typed `Unsupported` signal.
- [daemon.py](../packages/client/src/notehook_cli/daemon.py): third thread
  alongside `_watch_loop`: bootstrap cursor (`since=0`), then long-poll
  (`wait_seconds=25`) on a **dedicated** `httpx.Client` (timeout must exceed
  the wait; the shared 120 s client is fine but a separate connection avoids
  head-of-line blocking with transfers). Rows with foreign `equipment_no` →
  `self._wake.set()` (echo suppression per spec §7). `Unsupported` or
  repeated transport errors → log once, back off to the plain poll timer
  (never crash the daemon — "feed absence degrades to today's behavior").
- Tests: client-vs-in-process-server — device fixture uploads → daemon wakes
  well before `poll_interval` (drive with a large poll interval so the test
  proves the feed, not the timer); own-echo suppression (client's own upload
  does not re-wake); endpoint-absent fallback (server app without the
  router).

**Acceptance**: `make check` green; end-to-end freshness demo: device-push →
event row, without waiting out the poll interval.

---

## Phase 7 — docs + spec reconciliation

- README section + `docs/workflows.md` user guide (install → configure →
  serve; the §9 X4 example as the worked example; systemd/launchd snippets
  for `notehook daemon` + `notehook workflows serve`).
- Update `packages/client/CLAUDE.md` (new subsystem map, EventLog ownership
  rules, "SDK is pure stdlib — never import notehook_cli from it") and
  `packages/server/CLAUDE.md` (change feed, capture skip-list).
- Reconcile the spec with as-built reality: D8 (`run.rel_path` +
  index change) and anything else that drifted; mark spec status
  "implemented (v1)".
- Fold the remaining review notes into the spec where they were accepted
  (async long-poll shape, retention order, config-dir-derived paths).

---

## Sequencing and sizing

```
Phase 1 (server)  ──────────────┐
Phase 2 (events)  ──► Phase 3 ──► Phase 4 ──► Phase 5 ──► Phase 7
                                └────────────► Phase 6 (needs 1 only) ─┘
```

- 1 ∥ 2 are independent starting points. 6 needs only 1. 3–5 are strictly
  ordered. 7 closes.
- Suggested PR granularity: one PR per phase, except Phase 3 which splits
  naturally into 3a+3b (SDK + manifest: pure, no I/O), 3c+3d (installs +
  executor), 3e (runner core) — three PRs, each independently green.
- Rough effort weights: P1 ~1, P2 ~1.5, P3 ~3 (the bulk), P4 ~1.5, P5 ~1.5,
  P6 ~0.5, P7 ~0.5 — Phase 3 is the schedule risk; its D6 seam and
  contract-named tests are what keep it tractable.

## Risks / watch items

- **Device-compat of `EntriesVO.last_modified_by`** (accepted deviation,
  spec §1): unverifiable until a real-device capture. Mitigation: it's one
  field; if a capture ever shows firmware choking, gate it on the requesting
  `equipmentNo` having the `CLI-` prefix.
- **uv presence at runtime**: the runner shells out to `uv`. Fail with a
  clear message at `serve` startup if `uv` is missing (checked once), not
  per-job.
- **Windows**: `fcntl.flock` and SIGTERM semantics are POSIX. v1 targets
  Linux/macOS (matching the daemon story); guard imports so the rest of the
  CLI still works, and document the limitation.
- **Threaded runner + SQLite**: all claim/finalize writes funnel through
  `EventLog` with busy_timeout; keep worker threads out of direct DB access
  (executor returns results; the loop writes). The Phase 3 concurrency tests
  pin this.
- **Coverage gates**: client is the gate most exposed (lots of new surface at
  80%). The D6 invoker seam, clock injection in retry logic, and driving
  `poll_step()` directly are the levers that keep serve-loop code testable
  without flaky sleeps.
