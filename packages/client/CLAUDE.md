# noted-cli

The `noted` CLI: keeps a local directory bidirectionally synced with the
server. Entry point `cli.py` (typer): `init` / `login` / `sync` / `daemon` / `status`.

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
  (0600 file under `~/.config/noted/`). Never persist the password.

## Tests

Sync scenarios run against the **real server app in-process** (fixtures in
`tests/conftest.py` build a `TestClient`-backed API client) — prefer that over
mocking HTTP. A second `make_api(...)` client simulates "the device" for
two-equipment scenarios. CLI tests monkeypatch `cli.httpx.Client` to route at
the in-process app. Coverage gate: 80%.
