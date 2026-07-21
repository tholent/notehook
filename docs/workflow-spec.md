# notehook workflows — specification (v1)

Status: **implemented (v1)**. All seven phases in §10 have shipped and are
covered by `make check`; §11 records where the as-built system resolved
ambiguity the original draft left open, or corrected something the draft
got wrong once real code exercised it.

A GitHub-Actions-like automation system for notehook: when notes are created or
updated in watched folders, Python workflows run automatically (note→PDF
conversion, external API calls, pushing files to an Xteink X4 over its local
HTTP API, …).

This spec resolves open questions 1–3 from the design brief, makes the calls
on 4–9 that the runner lifecycle forces, and freezes the four contracts:
event schema, workflow author API, manifest schema, runner lifecycle.

---

## 0. Components

```
┌────────────────────┐  append events   ┌──────────────────┐
│ notehook CLI       │  (per action;    │ events.db        │
│ (sync engine:      │   settled at     │  event log       │
│  daemon or         │   pass end)      │  run log         │
│  one-shot sync)    ├─────────────────►│  runner cursor   │
└────────────────────┘                  └───────┬──────────┘
                                                │ poll (2s)
                                        ┌───────▼──────────┐
                                        │ notehook          │
                                        │ workflows serve   │
                                        │ (runner daemon)   │
                                        └───────┬──────────┘
                                                │ spawn per job
                                        ┌───────▼──────────┐
                                        │ uv run <workflow> │
                                        │ (subprocess,      │
                                        │  timeout, retry)  │
                                        └──────────────────┘
```

- **Producer**: the notehook CLI sync engine appends events. Works identically
  from `notehook daemon` and one-shot `notehook sync` — events queue durably either
  way.
- **Consumer**: `notehook workflows serve`, a separate daemon (open question 3:
  resolved as separate process). Crash isolation both directions; either side
  can restart freely.
- **Transport**: the SQLite file itself (`~/.config/notehook/events.db`, WAL
  mode). No sockets, no IPC. The runner polls a cheap indexed query every 2s
  (configurable); at personal scale this is indistinguishable from push and
  removes an entire class of notify-mechanism bugs.
- **Latency**: end-to-end freshness is bounded by how quickly the client
  notices server-side changes, not by the runner's 2s poll — the server
  change feed (§7) closes that gap by waking the sync engine within ~1–2s of
  a device push.

---

## 1. Event schema (open question 1 — resolved)

### Where events come from

Events are emitted by the **client sync engine** (`notehook_cli/engine.py`), not
the server. Rationale: workflows want real local file paths
(`note_to_pdf(event.path)`); the client is the component that both knows when
a file has *finished* syncing and owns the local copy. The server's view is
blobs with obfuscated names — wrong altitude.

Every sync action handler already knows what the event needs:

| Sync action | `item.known` | Event emitted |
|---|---|---|
| download (remote change pulled) | absent | `created`, source `sync-download` |
| download | present | `updated`, source `sync-download` |
| upload (local change pushed) | absent | `created`, source `sync-upload` |
| upload | present | `updated`, source `sync-upload` |
| delete_local (remote deletion pulled) | — | `deleted`, source `sync-download` |
| delete_remote (local deletion pushed) | — | `deleted`, source `sync-upload` |
| conflict keep-both copy | — | `created` for the conflicted-copy file |
| record / mkdir / forget | — | no event |

Notes:

- **created vs updated**: yes, notehook distinguishes them — derived from the
  three-way merge base (`SyncedFile` row exists = updated). Free and reliable.
- **deleted**: included. **renamed**: not a first-class event — the diff
  engine has no rename detection, so renames surface as `deleted` + `created`.
  Documented; a future rename detector could add a `renamed` type without
  breaking consumers (workflows opt into types explicitly).
- **Local files that never sync** (files appearing outside sync): the daemon
  syncs them on its next pass, which emits a `created`/`updated` event with
  source `sync-upload`. This covers the "filesystem watcher fallback" case
  without a watcher — **v1 ships no watchdog** (deviation flagged: the brief
  allowed it as "at most a fallback"; the sync pass makes it redundant, and
  `backfill` covers pre-existing files).
- **Touch with identical content**: hash-equal syncs are no-ops (`RECORD`)
  and emit nothing. Events fire on content change, not mtime change.
- **Folders**: no events. File events only.
- **Per-device attribution** ("which equipment made this change"): included.
  The server already records `last_modified_by` (equipment number) per node;
  it is exposed as an additive `last_modified_by` field on `EntriesVO`
  (safe under the extras-ignored rule — old clients simply don't see it) and
  carried onto the event as `origin_equipment`. For `sync-upload` events the
  originator is this client itself. Empty string when unknown (e.g. nodes
  created before the field existed).

### Batch-settled semantics (settled flag)

Each `run_once()` pass gets a `sync_pass` UUID. Events must be exactly as
durable as the files themselves: each sync handler already commits its
`SyncedFile` state as it executes, so a pass that fails partway (network drop
on file 6 of 10) leaves files 1–5 synced — if their events were only written
"after the pass completes", they would be lost forever (the next pass sees
hash-equal `RECORD` no-ops and emits nothing).

Therefore events are inserted **per action, in the same moment as the state
upsert, with `settled = 0`**, and one `UPDATE event SET settled = 1 WHERE
sync_pass = ?` runs at pass end. The runner's intake only consumes
`settled = 1` rows, which preserves the batch guarantee: by the time any
event is visible, the whole pass has settled — a multi-file sync never
exposes a half-synced state to workflows.

**Orphan recovery**: at the start of the next `run_once()`, settle any
leftover `settled = 0` rows — an unsettled row can only belong to a dead pass
(exception or hard crash), and its action *did* execute. This assumes one
sync engine per config dir at a time; the engine takes a lock file
(`events.db.lock`, flock) around each pass to guarantee it (the daemon and a
concurrent one-shot `notehook sync` would otherwise race). Orphaned events
settle late, so a workflow may see an event for a file that changed again
since — coalescing (§6) and the hash-keyed idempotency rule make this
harmless.

`backfill` and `manual` events don't belong to a sync pass and are inserted
with `settled = 1` directly (their action — the file existing — has already
happened).

Workflows are per-file in v1; a batch-level trigger (`on=["sync-completed"]`)
is noted as deferred and fits the schema (events grouped by `sync_pass`).

### Tables

One SQLite file `~/.config/notehook/events.db`, WAL mode:

```sql
CREATE TABLE event (
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
CREATE INDEX idx_event_intake ON event(settled, id);

CREATE TABLE run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    install       TEXT NOT NULL,             -- install alias (see §5)
    workflow_name TEXT NOT NULL,             -- manifest name at time of run
    workflow_version TEXT,
    event_id      INTEGER NOT NULL REFERENCES event(id),
    rel_path      TEXT NOT NULL,             -- denormalized from event (§11 D8)
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
CREATE INDEX idx_run_claim ON run(status, next_attempt_at);
CREATE INDEX idx_run_install_path_status ON run(install, rel_path, status);

CREATE TABLE runner_meta (                   -- single row
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cursor_event_id INTEGER NOT NULL DEFAULT 0
);
```

Writers: notehook CLI writes `event`; the runner writes `run` and `runner_meta`.
No table is written by both components, so WAL needs no cross-component
coordination — but several CLI *processes* can write `event` concurrently
(daemon pass + a `backfill` command), so every connection sets a
`busy_timeout` rather than treating `SQLITE_BUSY` as an error.

---

## 2. Workflow author API (open question 2 — resolved; freeze this)

### The contract

```python
from notehook_workflow import workflow, secret, RetryLater

@workflow(on=["created", "updated"])
def run(event, config):
    pdf = note_to_pdf(event.path)
    push_to_x4(pdf, ip=config["device_ip"], key=secret("x4_api_key"))
```

- A workflow file may define multiple decorated functions; every function
  whose `on` includes the event's type is called, in definition order.
  `on` defaults to `["created", "updated"]`.
- The glob is **not** in the decorator (settled design #4) — binding lives in
  install config (§5).

### `event` — frozen fields

| Field | Type | Notes |
|---|---|---|
| `event.id` | `int` | event-log row id; stable across retries |
| `event.type` | `str` | `"created"` / `"updated"` / `"deleted"` |
| `event.path` | `pathlib.Path` | absolute path in the sync root; for `deleted`, points where the file was |
| `event.rel_path` | `str` | posix path relative to sync root |
| `event.content_hash` | `str` | md5 hex; `""` for deleted. **This is the idempotency key** — skip work if output for this hash already exists |
| `event.size` | `int` | bytes |
| `event.timestamp` | `datetime` | UTC, when the event was recorded |
| `event.source` | `str` | `"sync-download"` / `"sync-upload"` / `"backfill"` / `"manual"` |
| `event.origin_equipment` | `str` | equipment number that made the change (e.g. the device's serial, or this client's own `CLI-…` id for local edits); `""` when unknown |
| `event.sync_pass` | `str` | uuid grouping one sync pass |
| `event.attempt` | `int` | 1-based attempt number for this run |

New fields may be added; existing fields never change meaning (workflows must
tolerate unknown fields — the SDK dataclass ignores extras).

### `config` — plain `dict[str, Any]`

Values from the install's `config.toml` `[inputs]` section, validated against
the manifest: unknown keys warn, missing `required` inputs fail the run
*before* the subprocess spawns (misconfiguration is an install problem, not a
retry-able runtime problem), declared defaults are filled in.

### Secrets — env delivery + accessor

Secrets are **not** in `config` and not in the payload file, so logging or
dumping config can't leak them. The runner injects each configured secret as
an environment variable `NOTEHOOK_SECRET_<NAME_UPPERCASED>`;
`notehook_workflow.secret("name")` is sugar for reading it (returns `None` if
unset and the manifest marks it `required = false`; a missing required secret
fails at configure/validate time). Env delivery also means child processes a
workflow spawns (curl, etc.) can use them directly.
*(Deviation flagged: the brief left delivery open between env/config/object;
env + accessor chosen for the leak-resistance property.)*

### Outcomes — exit-code protocol

| Outcome | How | Runner behavior |
|---|---|---|
| success | return normally (exit 0) | run `success` |
| permanent failure | raise any exception (exit 1) | run `failed`; no retry |
| retry later | `raise RetryLater("X4 offline")` (exit **75**, `EX_TEMPFAIL`) | reschedule with backoff (§6) |

X4-unreachable is the canonical `RetryLater` case. No structured return
values in v1 — stdout/stderr are captured to the run log and that's the whole
output channel (open question 6, resolved: structured returns deferred; the
run log makes them addable later without breaking anyone).

### Invocation mechanics (informative, not part of the frozen contract)

The SDK (`notehook_workflow`) is a **single pure-stdlib module shipped inside
notehook-cli** and supplied by the runner at invocation time — workflows do
*not* declare it as a dependency, keeping single-file workflows portable and
free of any "where does the SDK come from" pinning.

- Single-file workflow: the runner parses the script's PEP 723 block, writes
  a tiny generated harness script carrying the *same* block, and `uv run`s
  the harness; the harness imports the SDK (via `PYTHONPATH`) and the
  workflow file, then dispatches matching handlers.
- Package workflow: `uv run --project <workflow-dir>` (honoring the committed
  `uv.lock`) with the same harness/`PYTHONPATH` arrangement.

Event + config are passed as a JSON payload file (`NOTEHOOK_PAYLOAD_FILE` env
var); secrets as env vars per above. Authors only ever see `(event, config)`.

---

## 3. Manifest schema (`[tool.notehook]`)

*(Deviation flagged, invited by the brief: namespace is `[tool.notehook]`, not
`[tool.notesync]`, matching the project rename.)*

Inline in the PEP 723 block for single files; in `pyproject.toml` for
packages; **package form wins if both exist**. Deps and Python version live in
standard metadata (PEP 723 / `[project]`) where uv reads them — the manifest
declares only what packaging doesn't cover.

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "pypdf"]
#
# [tool.notehook]
# name = "supernote-to-x4"
# version = "0.2.0"
# description = "Convert synced notes to PDF and push to an Xteink X4"
# timeout = 300                      # seconds; optional, default 300
# suggested_paths = ["Note/ToReader/**"]   # default offered at configure time
#
# [tool.notehook.inputs]
# device_ip = { required = true, description = "X4 IP on the local network" }
# target_folder = { default = "Books" }
#
# [tool.notehook.secrets]
# x4_api_key = { required = false }
# ///
```

| Key | Required | Meaning |
|---|---|---|
| `name` | no (defaults to filename / directory name) | workflow id; also the default install alias |
| `version` | no | informational; shown in `list`, recorded per run |
| `description` | no | shown at install time |
| `entry` | no (packages only; default `workflow.py` at package root) | file containing the decorated functions |
| `timeout` | no (default 300) | per-run subprocess timeout, seconds |
| `retry` | no | `{ max_attempts = 20, backoff_base = 60, backoff_cap = 3600 }` overrides |
| `suggested_paths` | no | glob list offered as the default binding at configure time — *suggestion only*; the user's install config is authoritative (open question 4) |
| `inputs.<name>` | no | `required` (default false), `default`, `description` |
| `secrets.<name>` | no | `required` (default false), `description` |

A minimal one-file workflow needs no `[tool.notehook]` at all.

**Install-time disclosure** (security posture, settled design #11): `install`
prints the manifest's declared inputs and secrets, its dependency list, and a
fixed notice that workflows run unsandboxed with network access and full
user-level file access. Personal-scale trust; no sandboxing in v1.

---

## 4. notehook↔runner boundary (open question 3 — resolved)

**Separate daemon**, `notehook workflows serve`, consuming `events.db`.

- Isolation: a wedged runner can't stall sync; a crashed sync can't take the
  runner down. Each restarts independently (both are systemd/launchd-friendly).
- Discovery: polling (2s default) on an indexed query against the cursor in
  `runner_meta`. Chosen over notify mechanisms for zero moving parts; the
  event log is already the source of truth, so polling is just reading it.
- The runner works whether events came from `notehook daemon`, one-shot
  `notehook sync`, `backfill`, or `manual` — producers never know or care
  whether a runner is attached. No runner running = events queue durably.

---

## 5. Install & configuration model (open question 4 — resolved)

Layout (all under the existing notehook config root — deviation flagged:
`~/.config/notehook/…`, not `~/notesync/…`, matching the project rename and
keeping one backupable state root):

```
~/.config/notehook/
  events.db                       # event + run log
  workflows/<alias>/              # git clone (or copied local dir) — code only
  workflow-config/<alias>.toml    # local config, 0600 — never committed
```

`notehook workflows install <git-url-or-local-dir> [--as <alias>]`:

1. Clone/copy into `workflows/<alias>/` (alias defaults to manifest name,
   falling back to repo/dir name).
2. Parse + validate the manifest; show the disclosure summary.
3. Prompt for required inputs/secrets and trigger paths (pre-filled from
   `suggested_paths`) → write `workflow-config/<alias>.toml`.

**Name collisions / multiple installs**: an existing alias is an error with a
hint to pass `--as`. The same workflow **can** be installed twice under
different aliases with different configs (same workflow, two X4s or two
folders) — the alias, not the workflow name, is the unit of installation,
which is why `run.install` keys on it.

**Alias validation**: an alias (`--as`, or defaulted from the manifest's
`name`) is rejected if it's empty, `.`, `..`, or contains `/`, `\`, or a
control character — it becomes a path component under `workflows/` and
`workflow-config/`, and for a git-sourced install the default comes from
`manifest.name`, i.e. **content the cloned repository's own author
controls**. Without this check a crafted manifest name is a path-traversal
write (`install`) or, worse, an arbitrary-directory delete (`remove`, which
`rmtree`s whatever the alias resolves to).

Install config format:

```toml
workflow = "supernote-to-x4"          # manifest name (sanity-checked on load)
source = "https://github.com/…"       # provenance, used by `update`
enabled = true
paths = ["Note/ToReader/**"]          # authoritative trigger binding (globs on rel_path)
on = ["created", "updated"]           # optional narrowing of the decorator's `on`
                                      # (pre-spawn filter — see §11 for why
                                      # an unset `on` queues on glob match
                                      # alone rather than reading the code)
skip_own_changes = false              # true: drop events this client itself
                                      # originated (origin_equipment == own id) —
                                      # the clean loop guard for workflows that
                                      # write output into the sync root

[inputs]
device_ip = "192.168.1.50"

[secrets]
x4_api_key = "…"
```

`update` = `git pull` + revalidate manifest + re-prompt for any newly
required inputs/secrets. Code is shared; config is local.

**Loop guard**: a workflow that writes output into a watched folder will
trigger events on its own output (source `sync-upload` after the daemon syncs
it). Defenses, in order: set `skip_own_changes = true` (drops events whose
`origin_equipment` is this client — i.e. react only to changes made
elsewhere, typically the device); bind narrow globs; write outputs outside
the sync root; and the idempotency rule — hash-keyed skip — makes any
remaining loop converge instead of running away. No magical self-output
detection beyond that.

---

## 6. Runner lifecycle (open question 6 — resolved)

### Intake (fan-out)

Poll **settled** `event` rows past the cursor, as a **contiguous prefix**:
stop at the first unsettled row rather than skipping it (a naive `settled = 1
AND id > cursor` would let the cursor jump past a still-unsettled lower-id
row — a real possibility since several CLI processes write `event`
concurrently, §1 — and once that row *did* settle it would never satisfy
`id > cursor` again, silently dropping it forever; see §11). For each row
considered: for each enabled, valid install whose `paths` globs match
`rel_path` and whose effective `on` includes the type, insert a `queued` run.
Advance the cursor to the highest id considered, in the same transaction —
even when nothing matched, so the cursor still moves past events no install
cared about.

**Targeted events**: when `target_install` is set, only that install is
considered. For `manual` events the glob and `on` filters are bypassed
entirely — a manual trigger means "run this workflow on this file", full
stop. `backfill` events are targeted but respect the type filter as normal
(they carry type `created`, so a workflow subscribed only to `updated`
ignores them — backfill simulates first sight).

**Coalescing**: when queuing a run for `(install, rel_path)` that already has
a `queued` or `retry` run pending, the older run is marked `superseded` —
only the newest event per path runs. (A `running` job is never superseded
mid-flight; the newer event queues behind it.)

### Execution

- **Concurrency**: global default 2 parallel jobs (configurable). Per
  `(install, rel_path)`: strictly serial, guaranteed by coalescing plus
  never claiming a run while another for the same pair is `running`.
  Same workflow on *different* files may run in parallel.
- **Spawn**: `uv run` per §2 invocation mechanics; job temp dir as cwd;
  stdout/stderr captured (256 KiB cap each).
- **Timeout**: manifest `timeout` (default 300s). On expiry: SIGTERM, 10s
  grace, SIGKILL → treated as `RetryLater` (a hung X4 push should retry, not
  hard-fail).
- **Retry**: exit 75 (or timeout) → `retry` with exponential backoff:
  `min(backoff_base * 4^(attempt-1), backoff_cap)` — defaults 60s base,
  1h cap, `max_attempts` 20 (≈ a day of an offline X4). Exhausted → `failed`.
  Any other nonzero exit → `failed` immediately, no retry. There is no
  runner-level `retryable` flag — retry is the workflow's explicit signal
  (`RetryLater`), keeping the policy in exactly one place.

### Crash recovery

On startup the runner marks any `running` rows (from a previous crash) as
timed out and reschedules them under the normal retry policy. Events are
never lost — anything past the cursor is simply processed on the next poll.

### Hot reload

`watchfiles` on `workflows/` and `workflow-config/`: a change re-parses only
the affected install. Invalid manifest/config → install marked broken, error
logged, skipped at intake; the daemon never crashes over a bad workflow.

### Housekeeping (open question 8 — resolved)

Runner-side sweep, roughly once per day (tracked by elapsed wall time, not
tick count, so it's independent of the poll interval): delete `run` rows
older than `retention_days` first, then `event` rows older than
`retention_days` that have no remaining referencing `run` rows (that order
matters — `run.event_id` references `event`). Runner settings live in
`~/.config/notehook/config.toml` under `[workflows]`: `poll_interval_seconds`
(default 2), `max_parallel` (default 2), `retention_days` (default 90).

---

## 7. Server change feed (latency + attribution)

The server is the authoritative observer of every mutation and learns about
device changes immediately — but workflows must not consume server events
directly: the server has obfuscated blobs, not a real file tree, and a
server-side event would be visible *before* the local file exists, breaking
the settled-file invariant. Instead the server feeds the **client's sync
trigger**; `events.db` stays the single source of truth for workflows.

### Server side

A change-log table appended to in the same transaction as every tree
mutation (`uploadFinishV2`, `deleteFolderV3`, `moveV3`, `copyV3`,
`createFolderV2`):

```sql
CREATE TABLE change (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    op           TEXT NOT NULL,          -- 'create' | 'update' | 'delete'
                                         --   | 'move' | 'copy'
    node_id      INTEGER NOT NULL,
    path_display TEXT NOT NULL,          -- snapshot at mutation time
    is_folder    BOOLEAN NOT NULL,
    content_hash TEXT,
    equipment_no TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL        -- epoch ms
);
```

One endpoint, deliberately namespaced **outside** the reverse-engineered
Supernote API (it's our extension; no device-compatibility constraints):

```
POST /api/notehook/changes            (auth: x-access-token, like device endpoints)
  {"since": <cursor>, "limit": 500, "wait_seconds": 25}
→ {"success": true, "cursor": <new>, "changes": [ … rows … ]}
```

`wait_seconds > 0` long-polls: the server holds the request until a change
arrives past `since` or the timeout elapses (bounded at 30s). `since = 0`
returns the current cursor without history (a client bootstraps its cursor,
it does not replay — replay is what `backfill` is for).

### Client side

The `notehook daemon` gains a third trigger alongside the FS watcher and the
poll timer: a long-poll loop on `/api/notehook/changes`. When changes arrive
whose `equipment_no` is not this client's own, it wakes the sync engine
immediately (own changes are echoes of uploads it just performed — skipped
as triggers). Effect: device saves a note → server → client syncs within
~1–2s → workflow event fires. The periodic poll remains as a fallback for
servers without the endpoint (feed absence degrades to today's behavior,
never breaks sync).

### What this also buys later (not in scope now)

The same cursor is the foundation for **delta sync**: today every pass lists
the whole tree (`list_folder recursive=true`, O(tree) per poll); a `since`
cursor makes polling O(changes). Explicitly deferred — v1 uses the feed only
as a wake-up signal.

---

## 8. CLI surface (open question 7 — resolved)

All under `notehook workflows …`:

| Command | Purpose |
|---|---|
| `install <src> [--as ALIAS]` | clone + disclose + configure |
| `configure <alias>` | re-prompt inputs/secrets/paths |
| `enable` / `disable <alias>` | toggle without uninstalling |
| `remove <alias>` | delete clone + config (prompts about run history) |
| `update <alias>` | git pull + revalidate + re-prompt new requirements |
| `list` | installs with name/version/enabled/paths/last-run status |
| `run <alias> --path <file> [--wait]` | manual trigger; appends a `manual` event targeted at `<alias>` (`target_install`), bypassing its glob/`on` filters — type is derived like sync does (`created` if no state row for the path, else `updated`). Without `--wait`: fire-and-forget, returns as soon as the event is appended — a live `serve` (or a later poll) picks it up. With `--wait`: acquires the runner's own lock file and drives one intake+execute pass itself, then reports the outcome and exits nonzero on failure (CI-friendly) — see §11 for why this needs the same cross-process lock `serve` holds |
| `backfill <alias> [--glob G]` | append `created`/`backfill` events targeted at `<alias>` for every existing file matching the install's `paths` (narrowed, never widened, by `--glob`) — the replay/backfill story, built on the same log. Append-only: never executes anything itself and never triggers other installs |
| `logs [--alias A] [--failed] [--follow]` | run log viewer |
| `serve` | the runner daemon — holds the runner lock file for its whole lifetime; a second `serve` (or a `run --wait`) fails clearly instead of racing it |

### Deferred (explicitly out of v1)

- **Step-packages** (open question 5): resolved as *no first-class support* —
  reusable steps like `note_to_pdf` are ordinary packages pulled via uv
  (git/PyPI/path deps). Nothing in this spec needs to know about them.
- **Notifications** (open question 9): not built in. The run log is the v1
  surface (`logs --failed`). A notification *workflow* triggered on run
  failure is the natural plugin shape later (needs a runner-emitted event
  type — schema-compatible addition).
- Batch-level trigger (`on=["sync-completed"]`), rename events, delta sync
  built on the change-feed cursor (§7) — all noted above as compatible
  extensions.

---

## 9. Example: Supernote → X4 push

The X4 (CrossPoint firmware) exposes HTTP REST on port 80 and WebSocket
chunked upload on port 81 on the LAN (reference: `Wferr/xteink-sync`,
`web/api.html`). "Send to X4" is just a workflow — no first-class support:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "websockets"]
#
# [tool.notehook]
# name = "push-to-x4"
# suggested_paths = ["Note/ToReader/**"]
# [tool.notehook.inputs]
# device_ip = { required = true }
# ///
from notehook_workflow import workflow, RetryLater
import requests

@workflow(on=["created", "updated"])
def run(event, config):
    marker = event.path.with_suffix(event.path.suffix + f".{event.content_hash[:8]}.sent")
    if marker.exists():          # idempotency: this exact content already pushed
        return
    try:
        upload_to_x4(event.path, config["device_ip"])   # REST/WS per api.html
    except (requests.ConnectionError, requests.Timeout) as e:
        raise RetryLater(f"X4 unreachable: {e}") from e
    marker.write_text("")
```

(A real implementation would keep markers out of the sync root — see §5 loop
guard — this sketch just shows the API shape.)

---

## 10. Implementation phases (post-review)

1. Protocol/server groundwork: `last_modified_by` on `EntriesVO`, server
   `change` table appended in each mutation, `/api/notehook/changes` endpoint
   (server package; independently testable via FakeDevice).
2. Event emission in the sync engine (incl. `origin_equipment`, per-action
   insert + settle-at-pass-end, orphan settlement, engine lock file) +
   `events.db` schema + migration-free table creation (client package;
   fully unit-testable — incl. the failed-pass-partway durability case).
3. SDK module (`notehook_workflow`) + invocation harness + subprocess runner
   core with timeout/retry/coalescing (testable with stub workflows, no uv
   needed in unit tests; one integration test through real `uv run`).
4. Install/configure/manifest parsing + CLI verbs, incl. `skip_own_changes`.
5. `serve` daemon (poll loop + hot reload) + `backfill`/`run`/`logs`.
6. Change-feed long-poll trigger in `notehook daemon` (graceful fallback to
   the periodic poll when the endpoint is absent).
7. Docs + example workflow; coverage/lint/type gates as everywhere else.

---

## 11. As-built deviations

Everything below surfaced while implementing §1–§8 — either the draft left
real ambiguity that only became concrete once code had to make a choice, or
building the thing exposed a correctness gap the draft didn't anticipate.
None of these change the four frozen contracts (event schema, workflow
author API, manifest schema, runner lifecycle); they resolve gaps between
them.

**Settled-cursor durability (§1, §6)**: the original intake query,
`settled = 1 AND id > cursor`, has a bug once you account for concurrent
writers (§1 already establishes that a daemon pass and a `backfill`/`manual`
command can write `event` at the same time). A daemon pass's rows sit at
`settled = 0` until the pass finishes; a `backfill` running concurrently
inserts already-settled rows with higher ids. If intake advanced the cursor
to the max id it saw, it would jump past the still-unsettled daemon-pass
rows — and once *those* rows did settle, `id > cursor` would already be
false for them, dropping them silently and permanently. Fixed by treating
"settled rows past the cursor" as a **contiguous prefix**: stop at the first
unsettled row instead of skipping it, so the cursor only ever advances over
a run of already-known-settled rows, and a stalled row (plus everything
after it) is picked up together once it finally settles.

**`on`-narrowing is a pre-spawn filter, not a decorator read (§2, §5)**: the
draft's "install config `on` optionally narrows the decorator's `on`" implies
the runner can see both and intersect them. It can't, safely: the
decorator's `on` set only exists inside the workflow's own Python source,
knowable only by importing it — and the runner must never import untrusted
workflow code in its own process (that's exactly the isolation the
subprocess/`uv run` model exists to provide). So the pre-spawn filter is
`install.config.on` alone: if set, it's authoritative; if unset, the runner
queues on a glob/target match for all three event types and lets the SDK's
own per-handler `on` dispatch inside the spawned subprocess no-op harmlessly
when nothing matches. A wasted spawn is an acceptable cost; a configured
install that silently never runs is not.

**`run.rel_path` denormalization (§1)**: coalescing and per-`(install,
rel_path)` serialization (§6) query on `rel_path` on every intake and every
claim — joining through `event` for it on every one of those queries, and
indexing accordingly, is strictly worse than carrying the column directly.
`run` gained a `rel_path` column (copied from its `event` at insert time) and
`idx_run_install_path_status (install, rel_path, status)` replaces the
originally-specified `idx_run_install_path (install, event_id)`.

**`run --wait` needs the same cross-process lock as `serve` (§6, §8)**:
`claim_next`'s claim-one-eligible-row operation is guarded by an in-process
lock, sufficient only because the runner lifecycle assumes a single runner
process ever touches `run` (enforced by the runner's own flock, §6). `run
--wait` is a second code path that drives a `Runner` synchronously — without
also taking that same lock, a `run --wait` invocation racing a live `serve`
could both claim and execute the same row. `run --wait` therefore acquires
the runner's lock file itself before driving its local pass, and fails
clearly (not silently, not by racing) if `serve` already holds it — the
manual event is still appended first either way, so a failed `--wait` still
leaves the trigger queued for whichever runner picks it up next.

**Alias path-traversal validation (§5)**: `install`'s default alias is the
manifest's `name` field, which for a git-sourced install is content the
cloned repository's own author controls. Every command that turns an alias
into a filesystem path (`install`, `configure`, `enable`/`disable`,
`remove`, `update`) validates it first — reject empty, `.`, `..`, or any
`/`, `\`, or control character — matching the same invariant the server
already applies to client-supplied tree names (`tree_service.validate_name`).
Without it, a malicious workflow source's manifest name could write outside
`workflows/`/`workflow-config/` on install, or — since `remove` deletes
whatever the alias resolves to — delete an arbitrary directory.
