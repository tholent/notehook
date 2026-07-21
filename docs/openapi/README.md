# Supernote Private-Cloud Server — OpenAPI 3.0

A machine-readable **OpenAPI 3.0** description of the `supernote-service` API,
generated **solely from the Markdown specification in [`../specs/`](../specs/)**
(no content was taken from `specs-old/`).

- **140 operations** across **132 URL paths**, matching the 140 endpoints in `specs/`.
- Validates clean under `redocly lint` (0 errors).

## Layout

```
openapi.yaml                     # root: info, servers, security, tags, all 132 path $refs
paths/<domain>.yaml              # operations, keyed by operationId / path-item
components/schemas/common.yaml   # BaseVO / CommonVO / CommonListVO envelopes
components/schemas/<domain>.yaml # request DTOs + response VOs per domain
```

Domains mirror the `specs/` split: `authentication`, `users-accounts`,
`files-core`, `files-upload-share-oss`, `schedule-summary`, `equipment-base-system`.

## Conventions baked into the spec

- **Envelope.** Almost every response `allOf`-composes `common.yaml#/BaseVO`
  (`success`/`errorCode`/`errorMsg`). Paged responses use `CommonListVO`.
- **Status codes.** The server returns **HTTP 200 with `success:false`** for
  logical failures, so operations document `200` as the outcome channel.
  Transport exceptions that do use HTTP status (401 invalid token; 500
  `FILE_UPLOAD_FAILED` / download errors) are noted where they occur. This is why
  `redocly lint` emits `operation-4xx-response` warnings — they are expected and
  faithful to the API, not defects.
- **Security.** Global default security is the `accessToken` API-key header
  (`x-access-token`). Public entry points (login, register, captcha, random-code,
  reset-code, OSS signed-URL transfers, connectivity ping) override with
  `security: []`. Object-storage transfer endpoints authenticate by HMAC URL
  signature rather than the JWT.
- **`equipmentNo`** device header is available as the reusable
  `components/parameters/equipmentNoHeader`.
- **Provenance.** Derived from a ClassFinal-stripped JAR: shapes/paths/verbs/
  validation/error-codes are authoritative; behaviour is inferred. See
  `../specs/README.md` and `../specs/error-codes.md`.

## Tooling

```bash
# validate (multi-file, resolves $refs)
npx @redocly/cli lint openapi.yaml

# bundle into a single self-contained document
npx @redocly/cli bundle openapi.yaml -o supernote.openapi.json
```

The root file uses relative `$ref`s across `paths/` and `components/`, so open or
bundle `openapi.yaml` (not the individual fragment files).
