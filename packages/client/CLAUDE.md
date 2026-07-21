# notehook-cli

The `notehook` CLI: keeps a local directory bidirectionally synced with the
server, plus a `notehook workflows` sub-app that runs Python automations
against a log of sync events. Entry point `cli.py` (typer): `init` / `login`
/ `sync` / `daemon` / `status`, and `app.add_typer(workflows_app)`.

## How sync works

- `state_db.py` (`SyncedFile`) is the **three-way merge base**: last-known
  local+remote state per relative path. It's what lets `diff.py` distinguish
  local-only change / remote-only change / true conflict / delete vs never-seen.
- `scan.py` uses mtime+size as the fast path and only re-hashes files whose
  stat changed — don't add unconditional hashing.
- `diff.classify` returns an ordered plan (mkdirs → transfers → deletes,
  deletes deepest-first); `engine.py` executes it. Data-safety bias is
  deliberate: an edit on one side always beats a delete on the other.
- Conflict policies (`engine.py`): `keep-both` (default — losing side becomes
  a `(conflicted copy …)` file, both survive), `newest-wins`, `local-wins`,
  `remote-wins`. `sync` exits 3 on keep-both conflicts.
- `daemon.py`: watchfiles thread + poll timer both funnel into one
  debounced loop — sync passes never run concurrently.
- Credentials: password is transient at `login`; only the token is cached
  (0600 file under `~/.config/notehook/`). Never persist the password.

## Workflow automation (`workflows/`, docs/workflow-spec.md)

- `events.py` (`EventLog`) is the **only** thing that touches `events.db`
  (stdlib `sqlite3`, not SQLModel — the schema is frozen SQL, see the spec).
  Everything else — `engine.py`'s emission, `runner.py`'s intake/claim/
  finalize — goes through it; never open the file directly elsewhere.
  Producer methods (`append`/`append_settled`/`settle_pass`/`settle_orphans`)
  are called from `engine.py` and `workflows/cli.py`'s `run`/`backfill`;
  consumer methods (`unconsumed_settled`/`intake`/`claim_next`/`finalize_*`)
  are called only from `runner.py`, under the runner's own lock file
  (`config.runner_lock_file`) — `claim_next` is thread-safe but **not**
  cross-process-safe, so any new code path that drives a `Runner` (like
  `run --wait`) must take that lock first, exactly like `serve` does.
- `_sdk/notehook_workflow.py` is shipped to end-user workflow venvs via
  `PYTHONPATH`, not imported by anything in this codebase — it must stay
  **pure stdlib** (no `notehook_cli` imports, no third-party deps) and
  Python-3.11-syntax-compatible even though the rest of this package targets
  3.12+. If you touch it, the purity test (a subprocess import with a
  stdlib-only `sys.path`) is the thing that would catch a regression.
- `manifest.py` → `installs.py` → `harness.py`/`executor.py` → `runner.py` is
  the dependency chain: manifest parsing is pure/no I/O beyond reading the
  one file; `installs.discover()` pairs config with manifest and never
  raises for one bad install (`BrokenInstall` instead — a broken workflow
  must never take `serve` down); `harness.py` builds the `uv run` invocation
  and env (secret scrubbing happens here); `executor.py` owns the
  timeout/SIGTERM/SIGKILL/exit-code-to-outcome mapping behind an injectable
  `invoke` callable so tests never shell out to real `uv`; `runner.py` is
  the only thing that calls `installs.discover()` at intake/execute time —
  it's called fresh every step, which is also how hot reload works (no
  separate cache-invalidation machinery).
- `workflows/cli.py`'s alias-derived paths (`workflows/<alias>`,
  `workflow-config/<alias>.toml`) always go through `_validate_alias` first
  — a git-sourced install's default alias comes from the cloned repo's own
  manifest `name`, so it's attacker-controlled content, not just user input.

## Tests

Sync scenarios run against the **real server app in-process** (fixtures in
`tests/conftest.py` build a `TestClient`-backed API client) — prefer that over
mocking HTTP. A second `make_api(...)` client simulates "the device" for
two-equipment scenarios. CLI tests monkeypatch `cli.httpx.Client` to route at
the in-process app. Coverage gate: 80%.
