# noted-server

FastAPI server implementing the file-sync core of the Supernote device API.
App factory: `main.py:create_app` (services live on `app.state`); settings via
`NOTED_*` env vars (`config.py`).

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
  query-param auth, not token), `auth`, `stubs` (static handshake responses).
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

## Tests

`tests/helpers/fake_device.py` scripts real device call sequences
(randomCode→login→syncStart→…→syncEnd); prefer extending it over raw
endpoint pokes. Fixtures in `conftest.py` give a fully-wired app on tmp dirs.
Coverage gate: 85%.
