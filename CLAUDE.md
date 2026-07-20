# notehook

Self-hosted Supernote cloud-sync replacement: a FastAPI server a real Supernote
device can be pointed at, plus a `notehook` CLI that keeps a local directory in
bidirectional sync. Built against the reverse-engineered OpenAPI spec in
`specs/` — **the spec is the contract**; check it before changing any
request/response shape.

## Layout

uv workspace, three packages (each has its own CLAUDE.md with specifics):

- `packages/protocol/` — `notehook_protocol`: shared pydantic DTO/VO models + login hash helpers
- `packages/server/` — `notehook_server`: the FastAPI server
- `packages/client/` — `notehook_cli`: the `notehook` CLI (sync engine + daemon)

## Commands

```bash
make check        # lint + typecheck + all tests (what CI runs)
make lint         # ruff          (make lint-fix to auto-fix)
make typecheck    # mypy --strict over packages/*/src
make test         # per-package pytest with coverage gates: protocol 90 / server 85 / client 80
make test-server  # (or test-protocol / test-client) one suite
```

CI: three independent workflows in `.github/workflows/` (lint, typecheck, test)
that call these same make targets — keep Makefile and workflows in lockstep.

## Repo-wide conventions

- Python 3.12+, strict mypy, ruff (line length 100). New code must pass `make check`.
- All API responses use the `BaseVO` envelope (`success`/`errorCode`/`errorMsg`);
  logical failures return **HTTP 200 with `success: false`**, never 4xx/5xx —
  this mimics the real cloud until device captures prove otherwise.
- Real-device compatibility is unverified: be lenient parsing device input
  (extra fields ignored, sizes accept str or int), strict on what we emit.
- Some strings look renameable but are protocol-facing — do not touch:
  `bucketName="supernote"`, the `x-access-token` header, all endpoint paths,
  and DTO/VO field names (camelCase/snake_case exactly as in `specs/`).
- Env config uses the `NOTEHOOK_` prefix. Runtime state lives in `data/`
  (gitignored — captures may contain user data, never commit them).
- Tests favor end-to-end realism: server tests drive real auth+sync sequences
  via `FakeDevice`; client tests run against the real server app in-process.
