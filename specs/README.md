# Supernote Private-Cloud Server — API Specification

Reconstructed specification of the HTTP + real-time API exposed by the
**`supernote-service`** server (the self-hosted "Supernote Private Cloud"
backend that a Supernote device, the mobile app, and the web cloud-disk all
talk to).

This spec was reverse-engineered from a decompiled Spring Boot artifact located
at `research/supernote-decompile/`. It describes the **complete API surface as
declared by the code**, not only the parts any particular client happens to use.

> **Provenance & confidence.** The artifact is a **ClassFinal-protected JAR**:
> every Java *method body* has been stripped (each decompiles to `return null` /
> `return false` / empty). Therefore **no business logic is recoverable**. What
> *is* fully intact and authoritative:
> - Controller routing: base paths, HTTP verbs, method paths, path/query params.
> - Swagger annotations: `@ApiOperation` summaries and `@ApiModelProperty` field
>   descriptions (mostly Chinese in the source; translated here).
> - Request/response object *shapes*: every DTO and VO field, its Java type, and
>   its bean-validation constraints (`@NotNull`, `@NotBlank`, `@Size`, …).
> - MyBatis mapper XML (`BOOT-INF/classes/mapper/*.xml`): the actual SQL, which
>   reveals table structure and much behavioral intent.
> - Enums (error codes), constants (headers, keys, TTLs), and configuration.
>
> Everything describing **behavior** (as opposed to shape) is *inferred* from the
> above and is marked as such in the per-domain documents. Treat behavioral notes
> as well-supported hypotheses, not verified contract.

## Documents

The REST surface is **140 endpoints across 26 controllers**, split into six
domain documents below (endpoint counts in the last column).

| File | Domain | Endpoints |
|------|--------|:---------:|
| [`README.md`](README.md) | This overview: transport, envelope, auth, cross-cutting conventions, non-REST surfaces | — |
| [`error-codes.md`](error-codes.md) | Consolidated error-code catalogue (all enums) | — |
| [`authentication.md`](authentication.md) | Login, MFA/TOTP, SMS/email verification codes, image captcha, password + password-reset, sensitive-operation verification | 20 |
| [`users-accounts.md`](users-accounts.md) | User profile query/update, freeze/unfreeze, nickname, registration + registration restriction, account, email-server config | 19 |
| [`files-core.md`](files-core.md) | File/folder model, listing, create/rename/move/copy/delete, recycle bin, capacity, bidirectional sync tokens, search | 22 |
| [`files-upload-share-oss.md`](files-upload-share-oss.md) | Chunked & terminal upload, MD5 dedup, web cloud-disk browse/download, sharing, local object storage (signed URLs) | 26 |
| [`schedule-summary.md`](schedule-summary.md) | Calendar/schedule tasks, task groups, recurrence, sort order; summaries/digests and summary tags | 32 |
| [`equipment-base-system.md`](equipment-base-system.md) | Device (equipment) binding & lifecycle, device manuals & logs, dictionaries, reference/lookup data, system logs | 21 |
| | **Total** | **140** |

## Application at a glance

- **Framework:** Spring Boot (Spring MVC REST controllers), MyBatis, Druid pool.
- **Datastore:** MariaDB (`supernotedb`), Redis (sessions, throttles, caches).
- **Application name:** `supernote-service`. Swagger doc title *"Supernote-Service /
  私有化部署API接口文档"* (private-deployment API docs), version `1.0`.
- **HTTP port:** `19071`.
- **Socket.IO ports:** `18072` (file/digest real-time channel) and `18073`
  (task/to-do channel). See [Real-time channel](#real-time-channel-socketio).
- **Note-library sidecar:** a separate "note file library" service (`notelib`,
  port `6000`) is used server-side for `.note` parsing / format conversion.
- **Upload limits:** single file and single request both capped at **1024 MB**.

## Transport & base URL

All REST endpoints are served under `http://<host>:19071`. Controllers declare
these base paths (combined with per-method paths in each domain doc):

| Base path | Controllers |
|-----------|-------------|
| `/api` | login, MFA, password, user, register-adjacent, reference, email-server, equipment |
| `/api/user` | account, register, valid-code, sensitive-operation |
| `/api/base` | image captcha |
| `/api/file` | all file, upload, share, schedule, summary controllers |
| `/api/oss` | local object storage |
| `/api/system/base` | dictionaries |
| `/api/system/log` | system logs |
| `/download/local/`, `/download/ftp/` | file download endpoints (non-`/api`) |

### CORS

A high-priority servlet filter (`CorsFilter`, order `Integer.MIN_VALUE`) sets on
**every** response:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: POST, PUT, GET, OPTIONS, DELETE
Access-Control-Allow-Headers: Authorization, Content-Type
Access-Control-Max-Age: 3600
```

`OPTIONS` preflight requests short-circuit with `200` and no body.

## The response envelope

Almost every JSON response is (a subclass of) **`BaseVO`**:

```jsonc
{
  "success": true,          // boolean; false on any logical failure
  "errorCode": null,        // string error code when success=false (see error-codes.md)
  "errorMsg": null          // human-readable message when success=false
}
```

Two generic envelopes carry payloads:

- **`CommonVO<T>`** — single object:
  ```jsonc
  { "success": true, "errorCode": null, "errorMsg": null, "voT": { /* T */ } }
  ```
- **`CommonListVO<T>`** — paged list:
  ```jsonc
  {
    "success": true, "errorCode": null, "errorMsg": null,
    "total": 0,      // total row count (long)
    "size": 0,       // items in this page (int)
    "pages": 0,      // total page count (int)
    "voList": [ /* T[] */ ]
  }
  ```

Many endpoints return a *bespoke* VO that also extends `BaseVO` (so it always
carries `success`/`errorCode`/`errorMsg` plus its own fields). Each domain doc
documents those field-by-field.

### HTTP status vs. logical status

The dominant convention is **logical failures return HTTP `200` with
`success:false`** and an `errorCode`/`errorMsg` — *not* a 4xx/5xx. The only
exceptions are produced by the global exception handler (`GlobalExceptionHandler`,
`@ControllerAdvice`):

| Situation | HTTP status | Body |
|-----------|-------------|------|
| Business/validation failure (normal path) | `200` | `success:false`, domain `errorCode` |
| Invalid/expired token (`InvalidTokenException`) | **`401`** | `BaseVO{success:false, errorCode:"401", errorMsg:"Unauthorized"}` |
| Bean-validation failure (`@Valid`) | `200` | `success:false, errorCode:"400", errorMsg:"<joined messages>"` |
| Unreadable/mis-serialized body | `200` | `success:false, errorCode:"422", errorMsg:"Request Parameter Serialisation Exception"` |
| Unsupported `Content-Type` | `200` | `success:false, errorCode:"403 Forbidden", errorMsg:"Content type not supported"` |
| Duplicate submission (idempotency guard) | `200` | `success:false, errorCode:"409", errorMsg:"Please do not resubmit the data"` |
| Multipart upload failure (`FileUploadException`) | **`500`** | `{success:false, errorCode:"FILE_UPLOAD_FAILED", errorMsg:<detail>}` |
| File download failure (`FileDownloadException`) | **`500`** | plain-text message |
| Any other uncaught exception | `200` | `{success:false, errorCode:"500", errorMsg:"The system is temporarily unable to process your request…"}` |

> **Idempotency guard.** A `@ResubmitCheck` aspect (Redis-backed) rejects rapid
> duplicate submissions of guarded endpoints with the `409` above.

## Authentication & authorization

### Session token — `x-access-token`

The primary auth credential is a **JWT** passed in the request header
**`x-access-token`** (constant `AUTHORIZE_TOKEN`). Properties:

- Algorithm HS256; signing secret is a hard-coded constant (`JWT_SECRET =
  7786df7fc3a34e26a61c034d5ec8245d`). *(A hard-coded shared secret — a
  self-hosted-deployment characteristic; note for security review.)*
- TTLs defined in `Constant`: 1 h (`JWT_TTL`), 8 h (`JWT_TTL_8`), 30 days
  (`JWT_DAY_TTL`, the "30-day remember-me" per `TOKEN_TIME=30`), plus refresh
  windows (`JWT_REFRESH_INTERVAL` 55 min, `JWT_REFRESH_TTL` 12 h).
- Tokens are also tracked in Redis (`token_…` / `…_token` / `…_sso_key` keys),
  enabling server-side invalidation on logout and single-sign-on enforcement.

There is **no global authentication interceptor** — each handler validates the
token itself. A logging aspect (`HttpAspect`) reads `x-access-token` and
`equipmentNo` off every controller call for logging only. Consequently, whether a
given endpoint requires a token is *inferred per endpoint* in the domain docs
(login, registration, verification-code issuance, image captcha, and random-code
endpoints are the unauthenticated entry points; everything acting on a
logged-in user's data requires the token).

### Device identity — `equipmentNo`

A second header **`equipmentNo`** carries the device serial. It participates in
device-binding checks (a device binds to exactly one account) and in sync
concurrency control (only one device may sync at a time — see file error codes
E0078/E0079/E0301).

### The `equipment` client-type discriminator

Many request bodies carry an `equipment` integer identifying the calling client:

| value | client |
|-------|--------|
| `1` | Web cloud disk |
| `2` | Mobile app |
| `3` | Device / terminal (the Supernote itself) |
| `4` | User platform (admin/console) |

Some flows are gated on this value (e.g. MFA second-factor is applied for
`equipment` 1 and 4).

### Request signing — `Authorization` + `x-amz-date` (object storage)

The object-storage / download surface uses **AWS-SigV4-style signed requests**.
Swagger advertises these global headers:

| Header | Purpose |
|--------|---------|
| `User-ID` | User id (long) |
| `x-amz-date` | Request timestamp for URL/time validation |
| `Authorization` | Request signature |

Signature verification failure yields OSS error **`E1306` / "Signature
verification failed."** Helper classes present: `SignVerifier`, `RSAUtil`,
`AES256GCMUtil`. See [`files-upload-share-oss.md`](files-upload-share-oss.md).

### Passwords

Login/registration transmit the password as a client-side hash (the ecosystem
uses a fixed login-hash scheme; the server stores/compares hashes). Account
lockout is enforced: `MAX_ERR_COUNTS=6` failures lock the user for
`LOCK_TIME=5` minutes (Redis `…_password_error_count` / `…_account_lock`,
12-hour count window `PASSWORD_ERROR_COUNT_TIMEOUT`).

## Multi-factor authentication (TOTP)

RFC-6238 TOTP MFA is available (Microsoft/Google Authenticator compatible),
configured via `mfa.*` properties:

| Property | Default | Meaning |
|----------|---------|---------|
| `mfa.issuer` | `Supernote Private Cloud` | Issuer label in the authenticator |
| `mfa.recovery.count` | `10` | Recovery codes generated on enable |
| `mfa.setup.cache.seconds` | `600` | Setup session TTL (Redis `mfa:setup:…`) |
| `mfa.login.token.seconds` | `300` | MFA login-token validity |
| `mfa.login.fail.max` | `5` | Max MFA attempts before lockout |
| `mfa.login.fail.window.seconds` | `300` | MFA failure window |

Full flow (enable / verify / disable / status / recovery-code regeneration and
the login second-factor exchange) is in [`authentication.md`](authentication.md).

## Real-time channel (Socket.IO)

Beyond REST, the server pushes change notifications to connected clients over
**Socket.IO** (Netty-based `com.corundumstudio.socketio`), so a device syncs
promptly when another client changes data.

- **Two servers:** file/digest channel on port **`18072`**, task/to-do channel
  on port **`18073`**. Ping interval `5000 ms`, ping timeout `25000 ms`.
- **Socket.IO events:** `ServerMessage` (server→client), `ClientMessage`
  (client→server ack), `to-do` (schedule/task channel), `digest` (summary
  channel), `ratta_ping` (heartbeat). Ack argument `"Received"`.
- **Message types:** `FILE-SYN`, `TASK-SYN`, `DIGEST-SYN`.
- **Change opcodes** carried in messages: `DOWNLOADFILE`, `ADDFOLDER`,
  `MODIFYFILE`, `MODIFYFOLDER`, `DELETEFILE`, `DELETEFOLDER`, `COPYFILE`,
  `COPYFOLDER`, `MOVEFILE`, `MOVEFOLDER`, `WAITING`, `STARTSYNC`, `QUERY`,
  `SORT`, `ADD_DIGEST`, `UPDATE_DIGEST`, `DELETE_DIGEST`.
- **Auth:** the connection is authenticated with the same JWT
  (`JwtTokenUtil` in the socket package) and messages are signature-verified
  (`SignVerifierSocketIO`). Delivery state is buffered in Redis
  (`…_fileSocket_…`, `…_todoSocket_…`, `…_digestSocket_…`).

*(The Socket.IO wire protocol is documented at the level the decompiled shapes
allow; message payload classes are `Socket*MessageData` / `*MessageTemplate` in
`com/ratta/socket/io`.)*

## Other operational surfaces (not application API)

These are framework/ops endpoints exposed by configuration, listed for
completeness:

- **Swagger UI / OpenAPI 2** (SpringFox `@EnableSwagger2`): `/swagger-ui.html`,
  `/v2/api-docs` — enabled for all profiles.
- **Druid monitoring servlet:** `/druid/*` (basic-auth `druid`/`druid`,
  `allow=*`). *(Exposed with weak creds — security-review note.)*
- **Spring Boot Actuator:** only the `loggers` endpoint is exposed
  (`/actuator/loggers`) for runtime log-level changes.

## Conventions used in the domain docs

- **Paths** are shown fully (base + method mapping).
- **"Auth"** column: *Required (inferred)* when a valid `x-access-token` is
  needed; *Not required* for public entry points. This is inferred (no global
  interceptor exists to confirm).
- **Chinese** `@ApiOperation` / `@ApiModelProperty` text is translated to English;
  the original intent is preserved.
- **Field tables** list every DTO/VO field with Java type, whether required
  (from validation annotations), constraints, and description.
- **Inference is labeled.** Anything derived from SQL, field names, Redis keys,
  or naming convention (rather than an explicit annotation) is called out.
- **Protocol strings are verbatim** and must not be renamed (`bucketName`
  `"supernote"`, the `x-access-token` header, endpoint paths, camelCase field
  names, recycle path `/recycle_bin/`, digest path `/digest/`).
