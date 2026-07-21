# notehook

notehook is a self-hosted replacement for the Supernote cloud sync service, built against the
reverse-engineered OpenAPI spec in [specs/](specs/), plus a CLI tool that keeps a
local directory bidirectionally in sync with the server.

## Layout

uv workspace with three packages:

| Package | Path | What it is |
|---|---|---|
| `notehook-protocol` | [packages/protocol/](packages/protocol/) | Shared pydantic DTO/VO models mirroring the spec, plus the login password-hash helpers |
| `notehook-server` | [packages/server/](packages/server/) | FastAPI server implementing the file-sync core: auth, device NAS endpoints, OSS-style upload/download |
| `notehook-cli` | [packages/client/](packages/client/) | CLI client: one-shot `sync` and watch+poll `daemon`, three-way diff with conflict policies, and `notehook workflows` — run Python automations when notes change |

## Quick start

```bash
uv sync --all-packages

# 1. Generate the password digest (plaintext is never stored server-side)
uv run scripts/hash_password.py

# 2. Run the server (single worker only — nonce cache and rate limiter are in-process)
NOTEHOOK_ACCOUNT=you@example.com \
NOTEHOOK_PASSWORD_MD5=<from step 1> \
NOTEHOOK_BASE_URL=http://your-host:8080 \
uv run notehook-server

# 3. Configure and run the client
uv run notehook init --server http://your-host:8080 \
    --account you@example.com --dir ~/Supernote
uv run notehook login
uv run notehook sync      # one-shot pass
uv run notehook daemon    # watch + poll continuously
```

### Server configuration (env vars, `NOTEHOOK_` prefix)

| Variable | Default | Notes |
|---|---|---|
| `NOTEHOOK_ACCOUNT` | `user@example.com` | The single account's email |
| `NOTEHOOK_PASSWORD_MD5` | *(empty)* | `md5(plaintext)` hex — see `scripts/hash_password.py` |
| `NOTEHOOK_BASE_URL` | `http://localhost:8080` | Must be reachable *from the device/client* — it's embedded in upload/download URLs |
| `NOTEHOOK_DATA_DIR` | `data` | SQLite db, blobs, trash, captures |
| `NOTEHOOK_DEBUG_CAPTURE` | `false` | Log redacted request/response JSONL to `data/captures/` |
| `NOTEHOOK_MAX_UPLOAD_BYTES` | 2 GiB | Per-file limit, enforced while streaming |
| `NOTEHOOK_TOTAL_CAPACITY_BYTES` | 32 GiB | Account quota (also reported to `get_space_usage`) |

### Conflict policy (client)

`--conflict-policy` on `init`: `keep-both` (default — the losing side is renamed
to a `(conflicted copy …)` file and both survive), `newest-wins`, `local-wins`,
`remote-wins`. `sync` exits with code 3 when keep-both hit a conflict.

## Workflow automation

`notehook workflows` runs Python scripts automatically when notes are
created, updated, or deleted in watched folders — note→PDF conversion,
pushing files to another device, calling an external API. See
[docs/workflows.md](docs/workflows.md) for the full guide (install →
configure → serve) and [docs/workflow-spec.md](docs/workflow-spec.md) for
the design. Quick taste:

```bash
uv run notehook workflows install ./my-workflow --paths "Note/ToReader/**"
uv run notehook workflows serve
```

## Pointing a real device at this server

Untested against real hardware so far — this is the highest-risk unknown:

1. Redirect `cloud.supernote.com` to your server's IP (DNS override / hosts file
   on your router or a local DNS server).
2. The device speaks HTTPS: you need a TLS cert for `cloud.supernote.com` that
   the firmware will accept (try mkcert/self-signed first; if firmware pins or
   validates the CA chain strictly, this approach may need a firmware-level CA
   install). **Verify this first** — serve just `GET /api/file/query/server`
   and watch whether the TLS handshake completes before investing more time.
3. Run with `NOTEHOOK_DEBUG_CAPTURE=true` and watch `data/captures/*.jsonl`:
   every unimplemented endpoint the device calls is logged by the catch-all
   route (`errorCode 9999`), which tells you exactly what to build/stub next.
   Captures redact passwords, tokens, and signatures, so they're safe to share.

Known open questions to resolve from captures:
- Which login hash scheme firmware uses (`sha256(md5(pw)+rc)` vs `md5(md5(pw)+rc)`)
  — the server accepts both and logs which matched.
- Whether firmware uses the v3 NAS upload path or the `/api/file/terminal/upload/*`
  variant (not yet implemented; shares the same session machinery if needed).
- Whether firmware branches on HTTP status codes or only the `success` envelope
  (all logical errors currently return HTTP 200 + `{success: false}`).

## Development

```bash
make lint         # ruff over the whole workspace
make typecheck    # strict mypy over all package sources
make test         # all suites (coverage gates: protocol 90%, server 85%, client 80%)
make check        # all of the above (default target)
```

`make test-protocol` / `test-server` / `test-client` run one suite. CI runs each
track as an independent GitHub Actions workflow ([.github/workflows/](.github/workflows/)):
`lint`, `typecheck`, and `test` (the latter matrixed per package).

Server tests drive the real auth + sync flows through a `FakeDevice` helper;
client tests run the sync engine against the real server app in-process.
