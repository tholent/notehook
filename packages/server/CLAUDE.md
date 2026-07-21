# notehook-server

FastAPI server implementing the file-sync core of the Supernote device API.
App factory: `main.py:create_app` (services live on `app.state`); settings via
`NOTEHOOK_*` env vars (`config.py`).

## Architecture

- `models.py` — SQLModel tables. `FileNode` is one tree for files+folders;
  `parent_id=0` means root (no root row exists). Sibling names are unique
  case-insensitively (NOCASE collation). `path_display` is computed on read,
  never stored.
- `auth/` — random-code nonce → login → opaque token in `x-access-token`.
  Nonce cache and rate limiter are **in-process: the server must run
  single-worker**. Login accepts both hash schemes and logs which matched.
- `files/` — `tree_service` (path↔id, mutations, autorename), `blob_store`
  (sharded on-disk blobs), `upload_service` (apply → OSS receive → finish),
  `download_service` (HMAC-signed single-use URLs).
- `routers/` — `files_device` (NAS endpoints), `oss` (byte transfer, signed
  query-param auth, not token), `auth`, `stubs` (static handshake responses),
  `notehook` (our own extension, outside the reverse-engineered API: the
  workflow change feed, docs/workflow-spec.md §7).
- `files/change_service.py` appends one `Change` row per tree mutation
  (`upload_service.finish`, `tree_service.delete_node`/`move_node`/
  `copy_node`/`create_folder`), in the **same transaction** as the mutation
  it describes (flush-then-record-then-commit, not a second commit) — a
  failed mutation must append nothing. `POST /api/notehook/changes` is the
  read side: an `async def` endpoint that long-polls by re-querying on a
  short internal interval rather than any cross-thread wakeup, since the
  mutation endpoints it's watching run in the sync threadpool.
- `debug/capture_middleware.py` + the catch-all route in `main.py` log
  everything (redacted) to `data/captures/*.jsonl` — the primary tool for
  real-device debugging. New endpoints firmware needs show up there as
  errorCode 9999 first.

## Invariants (do not weaken)

- Client-supplied values never touch filesystem paths directly: `inner_name`
  is pattern-validated (`blob_store.validate_inner_name`) and blob paths
  derive from the validated DB record; tree names reject `/`, `..`, control chars.
- Upload md5 is computed server-side from the actual bytes; the client's
  claimed `content_hash` is verified against it, never trusted.
- Token/signature comparisons are constant-time; download URLs are single-use.
- `synType` on syncStart must reflect whether the account has data —
  reporting false-empty risks the device interpreting it as mass deletion.
- Capture logs must stay safe to share: redact any new secret-bearing field
  in `_REDACT_KEYS` / `_REDACT_QUERY` when adding one.
- `debug/capture_middleware.py` skips `/api/notehook/*` entirely — the
  workflow change feed's daemon-side long-poll re-requests every ~25s
  forever, and capturing it would fill the log with noise no device capture
  needs. Extend the same skip-list for any future extension endpoint that
  polls on a tight loop.

## Tests

`tests/helpers/fake_device.py` scripts real device call sequences
(randomCode→login→syncStart→…→syncEnd); prefer extending it over raw
endpoint pokes. Fixtures in `conftest.py` give a fully-wired app on tmp dirs.
Coverage gate: 85%.
