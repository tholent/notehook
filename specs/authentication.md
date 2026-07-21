# Authentication & Account Security API

This document specifies the authentication, login, MFA (multi-factor
authentication), captcha, password, password-reset, and sensitive-operation
audit endpoints of the decompiled Supernote cloud-sync server (Spring Boot,
package root `com.ratta`, listening on port **19071**).

## Domain overview

The server authenticates a caller with a **JWT** (HS256) carried in the
`x-access-token` request header. The device that a call originates from is
identified by the `equipmentNo` header and by the `equipment` body field
(`1` = web cloud disk; `2` = mobile APP; `3` = device/terminal; `4` = user
platform). There is **no global auth interceptor** — each handler enforces the
token itself, so the token requirement below is inferred per endpoint from
context (login/register/captcha/random-code/reset-code endpoints are
unauthenticated; endpoints operating on a logged-in user require a token).

Passwords are transmitted **pre-hashed by the client** (login-hash helpers live
in the client ecosystem); the `password` fields below carry that
already-hashed value, not a plaintext secret. The `/official/user/query/random/code`
endpoint returns a per-account `randomCode` + `timestamp`, which the client
folds into the password hash to defeat replay — see that endpoint's notes.

### Inferred login / MFA flow

1. Client optionally calls **`POST /api/official/user/query/random/code`** to
   obtain a `randomCode` + `timestamp` for the account, and salts the password
   hash with it.
2. Client calls **`POST /api/official/user/account/login/new`** (web/APP/user
   platform) or **`POST /api/official/user/account/login/equipment`** (device)
   with the account, hashed password, and `equipment`.
3. If the account has MFA enabled **and** the login channel is `equipment=1`
   (web) or `4` (user platform), the login response returns
   `mfaRequired=true` + a short-lived `mfaToken` (5 min, `mfa.login.token.seconds=300`)
   and **no** session `token`.
4. Client prompts for a TOTP 6-digit code (or a recovery code) and calls
   **`POST /api/official/user/account/login/mfa/verify`** with the `mfaToken` +
   `code`; on success it returns the real session `token`.
5. Device/mobile logins (`equipment=2,3`) and SMS/email-code login skip the MFA
   second factor.

MFA enrolment is a separate authenticated flow: `setup` (generate secret + QR)
→ `enable` (confirm with an authenticator code, receive one-time recovery
codes) → optional `disable` / `recovery/regenerate`, with `status` to query.
TOTP follows **RFC 6238**; the issuer label is `Supernote Private Cloud`
(`mfa.issuer`).

## Endpoint summary

| Method | Path | Auth | Summary |
|--------|------|------|---------|
| POST | `/api/official/user/account/login/new` | Not required | Account login (new) — web / APP / user platform |
| POST | `/api/official/user/account/login/mfa/verify` | Not required (uses `mfaToken`) | MFA second-factor verification (equipment 1 & 4 only) |
| POST | `/api/official/user/account/login/equipment` | Not required | Device/terminal login |
| POST | `/api/user/sms/login` | Not required | SMS/email verification-code login |
| POST | `/api/user/logout` | Required (inferred) | Log out |
| POST | `/api/user/query/loginRecord` | Required (inferred) | Query all login records by criteria |
| POST | `/api/user/query/token` | Required (inferred) | Query / re-issue the current token |
| POST | `/api/official/user/query/random/code` | Not required | Get a per-account random code (password-hash salt) |
| POST | `/api/user/mfa/setup` | Required (inferred) | MFA enrol: generate secret + QR code |
| POST | `/api/user/mfa/enable` | Required (inferred) | MFA enrol: confirm/enable with an authenticator code |
| POST | `/api/user/mfa/disable` | Required (inferred) | Disable MFA (logged-in + TOTP/recovery second factor) |
| POST | `/api/user/mfa/recovery/regenerate` | Required (inferred) | Regenerate recovery codes (needs TOTP) |
| GET | `/api/user/mfa/status` | Required (inferred) | Query current MFA status |
| POST | `/api/user/mail/validcode` | Not required | Send an email verification code |
| POST | `/api/user/check/validcode` | Not required | Verify an SMS/email verification code only |
| GET | `/api/base/pic/code` | Not required | Get an image (graphic) captcha (PNG stream) |
| POST | `/api/terminal/send/password/unlock/code` | Not required | Send a device password-unlock code |
| POST | `/api/official/user/retrieve/password` | Not required | Retrieve / reset password |
| PUT | `/api/user/password` | Required (inferred) | Change password |
| POST | `/api/user/query/sensitive/record` | Required (inferred) | Query sensitive-operation audit records |

---

## POST `/api/official/user/account/login/new`

Account login (new). Summary (translated): "Account login (new)". The primary
login entry for web cloud disk (`equipment=1`), mobile APP (`equipment=2`), and
user platform (`equipment=4`).

- **Auth:** Not required. `equipmentNo` header only relevant for terminal, which
  uses the dedicated endpoint below.
- **Path/query params:** none.
- **Request body:** `LoginDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `password` | String | Yes | `@NotBlank` "密码不能为空" (password cannot be empty) | Password (pre-hashed by client) |
| `countryCode` | String | No | — | Country calling code |
| `account` | String | Yes | `@NotBlank` "账号不能为空" (account cannot be empty) | Account (phone / email / WeChat, per `loginMethod`) |
| `browser` | String | No | — | Browser name |
| `equipment` | Integer | Yes | `@NotNull` "使用设备不能为空" (device cannot be empty) | Client device: 1 web cloud disk; 2 APP; 3 terminal; 4 user platform |
| `loginMethod` | String | Yes | `@NotBlank` "登录方式不能为空" (login method cannot be empty) | Login method: 1 phone; 2 email; 3 WeChat |
| `language` | String | No | — | Language |
| `equipmentNo` | String | No | — | Device serial (required for terminal login) |
| `timestamp` | Long | No | — | Timestamp (pairs with the random code for hash salting) |

- **Response body:** `LoginVO` (extends `BaseVO`)

| Field | Type | Description |
|-------|------|-------------|
| `token` | String | Session login token (JWT). Empty when `mfaRequired=true` |
| `counts` | String | Number of failed login attempts (login error count) |
| `userName` | String | User nickname (terminal) |
| `avatarsUrl` | String | Avatar URL (terminal) |
| `lastUpdateTime` | Date (`yyyy-MM-dd HH:mm:ss`) | Modification time (terminal) |
| `isBind` | String | Whether a device is bound: `Y` bound / `N` not bound (terminal) |
| `isBindEquipment` | String | Terminal-only; when account de-registration did not re-initialise, this field allows re-binding: `Y`/`N` |
| `soldOutCount` | int | Deregistration/cancellation count (terminal only) |
| `mfaRequired` | Boolean | `true` = MFA second factor required; caller must then call `/api/official/user/account/login/mfa/verify` with `mfaToken` |
| `mfaToken` | String | MFA temporary token, returned when `mfaRequired=true`, valid 5 minutes |

- **Error codes (plausible):** `E0708` incorrect username or password; `E0711`
  incorrect username or password, remaining attempts; `E0710`/`E0045` user
  locked; `E0709` user disabled; `E0108` account frozen; `E0018` account does
  not exist; `E0019` password error; `E0061` account cancelled; `E0128` MFA
  login token expired (on the follow-up verify); `E0110`/`E0086` country code
  empty.
- **Notes / inferences:** `counts` (login error count) plus the `mfa.login.fail.*`
  properties (`max=5`, `window.seconds=300`) suggest a windowed lock-out. MFA
  is only injected for `equipment` 1 and 4 (see the verify endpoint's summary),
  so APP (2) and terminal (3) never receive `mfaRequired=true`.

---

## POST `/api/official/user/account/login/mfa/verify`

MFA second-factor verification. Summary (translated): "Account login — MFA
second-factor verification (equipment=1,4 only)".

- **Auth:** Not required in the usual token sense; authorised by the short-lived
  `mfaToken` from step 1 of login.
- **Path/query params:** none.
- **Request body:** `MfaLoginVerifyDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `mfaToken` | String | Yes | `@NotBlank` "mfaToken cannot be empty" | The `mfaToken` returned by the first login step |
| `code` | String | Yes | `@NotBlank` "Verification code cannot be empty" | TOTP 6-digit code or a recovery code |
| `type` | String | No | — | Code type: `TOTP` / `RECOVERY`, default `TOTP` |
| `equipment` | Integer | Yes | `@NotNull` "Equipment cannot be empty" | Client device: 1 web cloud disk; 4 user platform |

- **Response body:** `LoginVO` (same shape as login/new; on success `token` is
  populated and `mfaRequired` is false/absent).
- **Error codes (plausible):** `E0128` MFA login token expired; `E0127` MFA
  verification code invalid; `E0129` recovery code invalid or used; `E0125` MFA
  not enabled.
- **Notes / inferences:** `type` maps to `MfaService.MfaCodeType` (`TOTP` /
  `RECOVERY`). The `mfaToken` lifetime is 5 min (`mfa.login.token.seconds=300`).
  Used TOTP codes are tracked under Redis prefix `mfa:used:` (replay
  prevention). Recovery codes are single-use (marked used once consumed).

---

## POST `/api/official/user/account/login/equipment`

Device/terminal login. Summary (translated): "Device-side login". Same DTO/VO
as `login/new` but scoped to `equipment=3` terminals, which never require the
MFA second factor.

- **Auth:** Not required. `equipmentNo` in the body identifies the terminal (the
  `LoginDTO.equipmentNo` doc notes it is required for terminal login).
- **Request body:** `LoginDTO` (see table under `login/new`). For terminals,
  `equipmentNo` should be supplied.
- **Response body:** `LoginVO` (see `login/new`). Terminal-specific fields
  (`userName`, `avatarsUrl`, `lastUpdateTime`, `isBind`, `isBindEquipment`,
  `soldOutCount`) are the primary payload here.
- **Error codes (plausible):** `E0708`/`E0711` bad credentials; `E0077` logged-in
  account differs from the account bound to the device; `E0083` device bound to
  another account; `E0075` device already bound; `E0069` device invalid.
- **Notes / inferences:** `isBindEquipment` + `soldOutCount` support the
  "cancelled but not re-initialised" re-bind path (see `LoginVO`).

---

## POST `/api/user/sms/login`

SMS/email verification-code login. Summary (translated): "SMS login (requires
SMS verification code)". Despite the name the DTO carries an `email`; the flow
logs a user in by a previously sent verification code rather than a password.

- **Auth:** Not required.
- **Request body:** `SmsLoginDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `email` | String | Yes | `@NotBlank` "邮箱不能为空" (email cannot be empty) | Email address |
| `browser` | String | No | — | Browser name |
| `equipment` | Integer | Yes | `@NotNull` "使用设备不能为空" (device cannot be empty) | 1 web cloud disk; 2 APP; 3 terminal; 4 user platform |
| `validCode` | String | Yes | `@NotBlank` "验证码不能为空" (verification code cannot be empty) | The verification code |
| `validCodeKey` | String | Yes | `@NotBlank` "验证码key不能为空" (code key cannot be empty) | Key returned by the send-code endpoint identifying the stored code |
| `devices` | String | No | — | Login device for desktop/mobile apps (required for those, empty for others), uppercase: `WINDOWS`, `MACOS`, `LINUX`, `ANDROID`, `IOS`, etc. |

- **Response body:** `SmsLoginVO` (extends `BaseVO`)

| Field | Type | Description |
|-------|------|-------------|
| `token` | String | Session login token |

- **Error codes (plausible):** `E0101` verification code expired; `E0102`
  verification code error; `E0109`/`E0018` user does not exist; `E0108` account
  frozen.
- **Notes / inferences:** `validCodeKey` pairs with the `EmailVO.validCodeKey`
  returned by `/api/user/mail/validcode`.

---

## POST `/api/user/logout`

Log out. Summary (translated): "Log out".

- **Auth:** Required (inferred). Reads the token from the request to invalidate
  the session.
- **Request body:** none. Uses `HttpServletRequest`/`HttpServletResponse` only.
- **Response body:** `BaseVO` (`success` / `errorCode` / `errorMsg`).
- **Error codes (plausible):** `E0712`/`E0085` not logged in / token invalid or
  expired (or HTTP 401 `InvalidTokenException`).
- **Notes:** Token likely cleared from Redis server-side.

---

## POST `/api/user/query/loginRecord`

Query all login records by criteria. Summary (translated): "Query all user
login records by conditions".

- **Auth:** Required (inferred) — administrative/account audit query.
- **Request body:** `LoginRecordDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `telephone` | String | No | — | Phone number filter |
| `email` | String | No | — | Email filter |
| `loginMethod` | String | No | — | Login method filter: 1 phone; 2 email; 3 WeChat |
| `equipment` | String | No | — | Device filter: 1 web cloud disk; 2 APP; 3 terminal; 4 user platform |
| `createTimeStart` | String | No | — | Login start date (filter `>= yyyy-MM-dd`) |
| `createTimeEnd` | String | No | — | Login end date (filter `<= yyyy-MM-dd`) |
| `pageNo` | String | Yes | `@NotBlank` "页码不能为空" (page number cannot be empty) | Page number |
| `pageSize` | String | Yes | `@NotBlank` "每页显示的个数不能为空" (page size cannot be empty) | Items per page |

- **Response body:** `CommonListVO<LoginRecordVO>` — paginated list envelope
  (`total`, `size`, `pages`, `voList`) over `LoginRecordVO`:

| Field | Type | Description |
|-------|------|-------------|
| `userId` | String | User ID |
| `userName` | String | User name |
| `createTime` | Date | Login time |
| `telephone` | String | Phone number |
| `email` | String | Email |
| `wechatNo` | String | WeChat |
| `browser` | String | Browser |
| `equipment` | String | Device (1 web cloud disk; 2 APP; 3 main account; 4 terminal — note the label wording differs from the numeric convention in the source enum) |
| `ip` | String | IP address |
| `loginMethod` | String | Login method: 1 phone; 2 email; 3 WeChat |

- **Notes / inferences:** Mapper `U-LoginRecordMapper.xml` joins `u_login_record t`
  with `u_user s`, and **localises `login_method` and `equipment` to Chinese
  strings** in the SQL (`手机/邮箱/微信`, `网页云盘/手机APP/终端设备/用户平台`), so the
  emitted `loginMethod`/`equipment` values may be these Chinese labels, not the
  numeric codes. Records older than one month are pruned by a scheduled
  `delete` (interval `#{days}` days).

---

## POST `/api/user/query/token`

Query / re-issue the current token. Summary (translated): "Query token
interface".

- **Auth:** Required (inferred) — reads the existing token from the request.
- **Request body:** none (`HttpServletRequest`/`HttpServletResponse`).
- **Response body:** `QueryTokenVO` (extends `BaseVO`)

| Field | Type | Description |
|-------|------|-------------|
| `token` | String | Login token (current or refreshed) |

- **Error codes (plausible):** `E0712`/`E0085` not logged in / token invalid.
- **Notes:** Likely used by clients to validate or refresh a stored session
  token.

---

## POST `/api/official/user/query/random/code`

Get a per-account random code. Summary (translated): "Get random code".

- **Auth:** Not required (part of the pre-login handshake).
- **Request body:** `RandomCodeDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `countryCode` | String | No | — | Country calling code |
| `account` | String | Yes | `@NotBlank` "账号不能为空" (account cannot be empty) | Account |

- **Response body:** `RandomCodeVO` (extends `BaseVO`)

| Field | Type | Description |
|-------|------|-------------|
| `randomCode` | String | Random code (salt for the password hash) |
| `timestamp` | Long | Timestamp bound to the random code |

- **Error codes (plausible):** `E0018`/`E0109` account/user does not exist.
- **Notes / inferences:** The returned `randomCode` + `timestamp` correspond to
  `LoginDTO.timestamp`; the client mixes them into the transmitted password hash
  to prevent replay of a captured hash. Almost certainly cached in Redis with a
  short TTL keyed by account.

---

## POST `/api/user/mfa/setup`

MFA enrol — generate secret + QR code. Summary (translated): "MFA binding:
generate secret and QR code".

- **Auth:** Required (inferred). User identity resolved from the token
  (`currentUserId(request)`).
- **Request body:** none.
- **Response body:** `MfaSetupVO` (extends `BaseVO`)

| Field | Type | Description |
|-------|------|-------------|
| `secret` | String | Base32 secret for manual entry into an authenticator app |
| `otpauthUrl` | String | Standard `otpauth://` provisioning URL |
| `qrCodeBase64` | String | QR-code PNG as `data:image/png;base64,...` |

- **Error codes (plausible):** `E0124` MFA already enabled; `E0712` not logged in.
- **Notes / inferences:** The pending secret is cached in Redis under prefix
  `mfa:setup:` for `mfa.setup.cache.seconds=600` (10 min), throttled via
  `mfa:setup:lock:` for `mfa.setup.throttle.seconds=60` (a 60-second cool-down
  between setup calls, per `MfaServiceImpl` constants). `otpauthUrl` embeds the
  issuer `Supernote Private Cloud`. RFC 6238 TOTP.

---

## POST `/api/user/mfa/enable`

MFA enrol — confirm and enable. Summary (translated): "MFA binding: confirm
enabling with an Authenticator code".

- **Auth:** Required (inferred).
- **Request body:** `MfaEnableDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `code` | String | Yes | `@NotBlank` "Verification code cannot be empty" | The 6-digit code from the authenticator app |

- **Response body:** `MfaEnableVO` (extends `BaseVO`)

| Field | Type | Description |
|-------|------|-------------|
| `recoveryCodes` | List<String> | One-time recovery codes (returned only on this call) |

- **Error codes (plausible):** `E0126` MFA setup session expired; `E0127` MFA
  verification code invalid; `E0124` MFA already enabled.
- **Notes / inferences:** Consumes the pending `mfa:setup:` secret; on success
  persists to `u_user_mfa` (`UserMfaDO`: `secret`, `enabled`, `recoveryCodes`,
  `lastUsedAt`). `mfa.recovery.count=10` recovery codes are generated. Recovery
  codes are stored **hashed** (`toHashedJson`/`parseHashedRecoveryCodes`), so the
  plaintext list is only ever visible in this one response.

---

## POST `/api/user/mfa/disable`

Disable MFA. Summary (translated): "MFA disable: relies on logged-in state +
TOTP/recovery-code second factor".

- **Auth:** Required (inferred) — logged-in state **plus** a valid second factor.
- **Request body:** `MfaDisableDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `code` | String | Yes | `@NotBlank` "Verification code cannot be empty" | TOTP 6-digit code or a recovery code |
| `type` | String | No | — | Code type: `TOTP` / `RECOVERY`, default `TOTP` |

- **Response body:** `BaseVO`.
- **Error codes (plausible):** `E0125` MFA not enabled; `E0127` MFA verification
  code invalid; `E0129` recovery code invalid or used.
- **Notes / inferences:** `type` maps to `MfaService.MfaCodeType`. On success
  the `u_user_mfa` row is disabled/cleared.

---

## POST `/api/user/mfa/recovery/regenerate`

Regenerate recovery codes. Summary (translated): "MFA regenerate recovery codes
(requires TOTP verification)".

- **Auth:** Required (inferred).
- **Request body:** `MfaEnableDTO` (reused)

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `code` | String | Yes | `@NotBlank` "Verification code cannot be empty" | TOTP 6-digit code (per the summary, TOTP is required here) |

- **Response body:** `MfaEnableVO`

| Field | Type | Description |
|-------|------|-------------|
| `recoveryCodes` | List<String> | Freshly generated one-time recovery codes |

- **Error codes (plausible):** `E0125` MFA not enabled; `E0127` MFA verification
  code invalid.
- **Notes / inferences:** Reuses `MfaEnableDTO`/`MfaEnableVO`. Replaces the
  stored hashed recovery-code set; old codes are invalidated.

---

## GET `/api/user/mfa/status`

Query MFA status. Summary (translated): "Query current MFA status".

- **Auth:** Required (inferred).
- **Path/query params:** none.
- **Request body:** none (GET).
- **Response body:** `MfaStatusVO` (extends `BaseVO`)

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | boolean | Whether MFA is enabled for the user |
| `recoveryCodesRemaining` | int | Count of unused recovery codes remaining |

- **Notes / inferences:** `recoveryCodesRemaining` derives from the unused
  entries of the hashed recovery-code set in `u_user_mfa`.

---

## POST `/api/user/mail/validcode`

Send an email verification code. Summary (translated): "Email verification-code
send interface".

- **Auth:** Not required (used in registration / SMS-login / password-recovery
  flows).
- **Request body:** `EmailDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `email` | String | Yes | `@NotNull` "邮箱不能为空" (email cannot be empty) | Target email address |
| `language` | String | No | — | Language (selects the email template locale) |

- **Response body:** `EmailVO` (extends `BaseVO`)

| Field | Type | Description |
|-------|------|-------------|
| `validCodeKey` | String | Key under which the sent code is stored; echoed back on verification/login |

- **Error codes (plausible):** `E1201` email sending exception; `E0106` email
  format incorrect; `E0064` too many codes sent (throttle).
- **Notes / inferences:** `validCodeKey` is consumed by
  `/api/user/check/validcode` and by `SmsLoginDTO.validCodeKey`. The code itself
  is almost certainly cached in Redis with a TTL (expiry surfaces as `E0101`).

---

## POST `/api/user/check/validcode`

Verify an SMS/email verification code only. Summary (translated): "Verification
code check (only verifies an SMS or email code)".

- **Auth:** Not required.
- **Request body:** `ValidCodeDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `validCodeKey` | String | Yes | `@NotBlank` "验证码key不能为空" (code key cannot be empty) | Key returned by the send-code endpoint |
| `validCode` | String | Yes | `@NotBlank` "验证码不能为空" (verification code cannot be empty) | The code the user entered |

- **Response body:** `BaseVO` (`success=true` on match).
- **Error codes (plausible):** `E0101` verification code expired; `E0102`
  verification code error.
- **Notes / inferences:** Pure verification — does not log the user in or mutate
  state. `getReferenceValue(List<ReferenceVO>)` in the controller is a helper
  (likely dictionary/reference lookup), not an endpoint.

---

## GET `/api/base/pic/code`

Get an image (graphic) captcha. Summary (translated): "Get graphic captcha".

- **Auth:** Not required.
- **Path/query params:** none declared on the method. (A captcha-key query
  parameter is plausible but not present in the decompiled signature; the
  handler works directly off `HttpServletRequest`/`HttpServletResponse`.)
- **Request body:** none.
- **Response body:** **Not a VO** — the method return type is `void` and it
  writes a **PNG image stream** directly to the `HttpServletResponse` body
  (`Content-Type: image/png`). No `BaseVO` envelope.
- **Notes / inferences:** The generated captcha text is presumably stored
  server-side (Redis/session) keyed to the caller so a later step can validate
  it. Because the body is a raw image, the standard `success/errorCode` envelope
  does not apply to this endpoint.

---

## POST `/api/terminal/send/password/unlock/code`

Send a device password-unlock code. Summary (translated): "Send unlock
verification code interface". Note: this controller has **no class-level
`@RequestMapping`**, so the path is exactly `/api/terminal/send/password/unlock/code`.

- **Auth:** Not required (a device-recovery flow keyed on the device serial).
- **Request body:** `PasswordResetCodeDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | Yes | `@NotBlank` "设备号不能为空" (device number cannot be empty) | Device serial number |
| `language` | String | Yes | `@NotBlank` "语言不能为空" (language cannot be empty) | Language: `1`=CN; `2`=EN |
| `code` | String | Yes | `@NotBlank` "加密后的验证码不能为空" (encrypted code cannot be empty) | Encrypted verification code |
| `type` | String | No | — | Password type: `1`=lock-screen password; `2`=file protection |

- **Response body:** `PasswordResetCodeVO` (extends `BaseVO`)

| Field | Type | Description |
|-------|------|-------------|
| `telephone` | String | Phone number the code was sent to |
| `email` | String | Email the code was sent to |
| `equipmentNo` | String | Device serial number |
| `time` | Integer | Timeout / validity window, in **seconds** |

- **Error codes (plausible):** `E0069` device invalid; `E1201` email sending
  exception; `E1202` SMS sending failed; `E0064` too many messages sent.
- **Notes / inferences:** Resolves the account bound to `equipmentNo` and sends
  an unlock code to that account's phone/email; `time` tells the device how long
  the code stays valid. `type` distinguishes unlocking the screen-lock PIN vs.
  the file-protection password.

---

## POST `/api/official/user/retrieve/password`

Retrieve / reset password. Summary (translated): "Retrieve password".

- **Auth:** Not required (forgot-password flow; the caller has proven ownership
  via a verification code beforehand).
- **Request body:** `RetrievePasswordDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `telephone` | String | No | — | Phone number (phone and email must not both be empty) |
| `email` | String | No | — | Email |
| `countryCode` | String | No | — | Country code (must be present iff phone is present) |
| `password` | String | Yes | `@NotBlank` "密码不能为空" (password cannot be empty) | New password (pre-hashed) |
| `version` | String | No | — | Version, named by date, e.g. `202305` (May 2023) |

- **Response body:** `BaseVO`.
- **Error codes (plausible):** `E0117` phone, email and WeChat cannot all be
  empty; `E0110`/`E0086` country code empty; `E0018`/`E0109` account/user does
  not exist; `E0713` password cannot be the same as recent ones.
- **Notes / inferences:** At least one of `telephone`/`email` is required
  (enforced in handler, not by annotations — surfaces as `E0117`). Ownership is
  established by a prior `/api/user/mail/validcode` + `/api/user/check/validcode`
  (or SMS equivalent) rather than a token.

---

## PUT `/api/user/password`

Change password. Summary (translated): "Change password".

- **Auth:** Required (inferred) — operates on the logged-in user resolved from
  the token.
- **Request body:** `UpdatePasswordDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `password` | String | Yes | `@NotBlank` "密码不能为空" (password cannot be empty) | New password (pre-hashed) |
| `version` | String | No | — | Version, named by date, e.g. `202305` |

- **Response body:** `BaseVO`.
- **Error codes (plausible):** `E0714` the original password entered is
  incorrect; `E0713` password cannot be the same as recent ones; `E0712` not
  logged in / login expired.
- **Notes / inferences:** No "old password" field on the DTO despite `E0714`
  existing — the original-password check may run against a separate step or the
  handler may compare using another source; documented as uncertain. A
  password-history check (`E0713`) is implied.

---

## POST `/api/user/query/sensitive/record`

Query sensitive-operation audit records. Summary (translated): "Query sensitive
operation records".

- **Auth:** Required (inferred) — scoped to the logged-in user (`request`
  supplies the user id; the mapper filters `where user_id=#{userId}`).
- **Request body:** `SensitiveOperDTO`

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `pageNo` | String | Yes | `@NotBlank` "页码不能为空" (page number cannot be empty) | Page number |
| `pageSize` | String | Yes | `@NotBlank` "每页显示的个数不能为空" (page size cannot be empty) | Items per page |
| `Language` | String | Yes | `@NotBlank` "语言不能为空" (language cannot be empty) | Language (note: field is capitalised `Language`; JSON key is `Language`) |

- **Response body:** `CommonListVO<SensitiveOperVO>` — paginated list envelope
  over `SensitiveOperVO`:

| Field | Type | Description |
|-------|------|-------------|
| `ip` | String | IP address of the operation |
| `operateRecord` | String | Operation description/record |
| `createTime` | Date | Operation time |

- **Notes / inferences:** Backed by `u_sensitive_record`
  (`U-SensitiveRecordMapper.xml`), filtered by the current `userId`. The
  `Language` field likely selects the locale used to render `operateRecord`
  labels. The getter is `getLanguage()` even though the field is `Language`, so
  Jackson serialises/deserialises the JSON property as `Language` (capital L) —
  documented as a quirk to preserve.

---

## Data models

### Envelopes

**`BaseVO`** — base of every response:

| Field | Type | Description |
|-------|------|-------------|
| `success` | boolean | Defaults `true`; logical failures set `false` |
| `errorCode` | String | Error code (e.g. `E0708`, or `400`/`401`/`409`/`422`/`500`) |
| `errorMsg` | String | Human-readable error message |

**`CommonVO<T>`** — `BaseVO` + `voT` (single data object of type `T`).

**`CommonListVO<T>`** — `BaseVO` + pagination:

| Field | Type | Description |
|-------|------|-------------|
| `total` | long | Total record count |
| `size` | int | Page size |
| `pages` | int | Total page count |
| `voList` | List<T> | Page of items |

### Request DTOs

- **`LoginDTO`** — see `/api/official/user/account/login/new`. Fields:
  `password` (req), `countryCode`, `account` (req), `browser`, `equipment`
  (req, Integer), `loginMethod` (req), `language`, `equipmentNo`, `timestamp`
  (Long).
- **`MfaLoginVerifyDTO`** — `mfaToken` (req), `code` (req), `type`
  (`TOTP`/`RECOVERY`, default `TOTP`), `equipment` (req, Integer; 1 or 4).
- **`SmsLoginDTO`** — `email` (req), `browser`, `equipment` (req, Integer),
  `validCode` (req), `validCodeKey` (req), `devices` (uppercase OS name).
- **`LoginRecordDTO`** — `telephone`, `email`, `loginMethod`, `equipment`
  (String), `createTimeStart`, `createTimeEnd`, `pageNo` (req), `pageSize`
  (req).
- **`RandomCodeDTO`** — `countryCode`, `account` (req).
- **`MfaEnableDTO`** — `code` (req). Reused by `mfa/enable` and
  `mfa/recovery/regenerate`.
- **`MfaDisableDTO`** — `code` (req), `type` (`TOTP`/`RECOVERY`, default
  `TOTP`).
- **`EmailDTO`** — `email` (req, `@NotNull`), `language`.
- **`ValidCodeDTO`** — `validCodeKey` (req), `validCode` (req).
- **`PasswordResetCodeDTO`** — `equipmentNo` (req), `language` (req, `1`=CN/`2`=EN),
  `code` (req, encrypted), `type` (`1`=lock-screen/`2`=file-protection).
- **`RetrievePasswordDTO`** — `telephone`, `email`, `countryCode`, `password`
  (req), `version`. At least one of phone/email required (handler-enforced).
- **`UpdatePasswordDTO`** — `password` (req), `version`.
- **`SensitiveOperDTO`** — `pageNo` (req), `pageSize` (req), `Language` (req,
  capital-L JSON key).

### Response VOs

- **`LoginVO`** (extends `BaseVO`) — `token`, `counts`, `userName`,
  `avatarsUrl`, `lastUpdateTime` (Date, `yyyy-MM-dd HH:mm:ss`), `isBind`,
  `isBindEquipment`, `soldOutCount` (int), `mfaRequired` (Boolean), `mfaToken`.
- **`SmsLoginVO`** (extends `BaseVO`) — `token`.
- **`QueryTokenVO`** (extends `BaseVO`) — `token`.
- **`RandomCodeVO`** (extends `BaseVO`) — `randomCode`, `timestamp` (Long).
- **`LoginRecordVO`** — `userId`, `userName`, `createTime` (Date), `telephone`,
  `email`, `wechatNo`, `browser`, `equipment`, `ip`, `loginMethod`. (Plain
  serializable, wrapped in `CommonListVO`.)
- **`MfaSetupVO`** (extends `BaseVO`) — `secret` (Base32), `otpauthUrl`,
  `qrCodeBase64` (`data:image/png;base64,...`).
- **`MfaEnableVO`** (extends `BaseVO`) — `recoveryCodes` (List<String>).
- **`MfaStatusVO`** (extends `BaseVO`) — `enabled` (boolean),
  `recoveryCodesRemaining` (int).
- **`EmailVO`** (extends `BaseVO`) — `validCodeKey`.
- **`PasswordResetCodeVO`** (extends `BaseVO`) — `telephone`, `email`,
  `equipmentNo`, `time` (Integer, seconds).
- **`SensitiveOperVO`** — `ip`, `operateRecord`, `createTime` (Date). (Plain
  serializable, wrapped in `CommonListVO`.)

### Domain / persistence

- **`UserMfaDO`** (table `u_user_mfa`) — `userId` (Long), `secret` (String,
  Base32), `enabled` (Integer, 0/1), `recoveryCodes` (String, hashed JSON),
  `lastUsedAt` (Date), `createTime`, `updateTime`.
- **`u_login_record`** — login audit table (join with `u_user`); pruned monthly.
- **`u_sensitive_record`** — sensitive-operation audit table (`user_id`,
  `operate_record`, `ip`, `create_time`, `update_time`).

### MFA configuration (`application.properties`, `mfa.*`)

| Key | Value | Meaning |
|-----|-------|---------|
| `mfa.issuer` | `Supernote Private Cloud` | Issuer label in `otpauth://` URL |
| `mfa.recovery.count` | `10` | Recovery codes generated per enable/regenerate |
| `mfa.setup.cache.seconds` | `600` | Pending-setup secret TTL (Redis `mfa:setup:`) |
| `mfa.setup.throttle.seconds` | `60` | Cool-down between setup calls (Redis `mfa:setup:lock:`) |
| `mfa.login.token.seconds` | `300` | `mfaToken` lifetime (5 min) |
| `mfa.login.fail.max` | `5` | Max MFA failures in the window |
| `mfa.login.fail.window.seconds` | `300` | MFA-failure counting window |

Redis key prefixes (from `MfaServiceImpl`): `mfa:setup:` (pending secret),
`mfa:setup:lock:` (setup throttle), `mfa:used:` (consumed TOTP codes, replay
prevention). TOTP is RFC 6238; code type enum is `MfaService.MfaCodeType`
(`TOTP`, `RECOVERY`).

### `MfaService.MfaCodeType`

`TOTP` — time-based one-time code from an authenticator app.
`RECOVERY` — a one-time recovery code issued at enable/regenerate.
Parsed from the DTO `type` string via `MfaCodeType.fromString(...)`; default
`TOTP` when omitted.

### Error codes referenced

**`UserErrorCodeEnum`** (subset relevant here): `E0018` Account does not exist;
`E0019` Password error; `E0045` User has been locked, try again later; `E0101`
Verification code has expired; `E0102` Verification code error; `E0104`/`E0106`
phone/email format incorrect; `E0108` Account has been frozen; `E0109` User does
not exist; `E0110` Country code is empty; `E0124` MFA already enabled; `E0125`
MFA not enabled; `E0126` MFA setup session expired; `E0127` MFA verification
code invalid; `E0128` MFA login token expired; `E0129` Recovery code invalid or
used; `E1201` Email sending exception; `E1202` SMS sending failed; `E9999`
Network error.

**`BaseErrorCodeEnum`** (subset relevant here): `E0061` Account cancelled;
`E0062` Phone number empty; `E0064` Too many SMS messages sent; `E0065` Failed
to send SMS; `E0066` Phone format incorrect; `E0069` Device invalid; `E0085`
Token invalid; `E0086` Country code empty; `E0708` Incorrect username or
password; `E0709` User disabled; `E0710` User locked; `E0711` Incorrect
username or password, remaining login attempts; `E0712` Not logged in / login
expired; `E0713` Password cannot be the same as recent ones; `E0714` Original
password incorrect.

**Transport/framework codes** (envelope-level, not enum): `401` Unauthorized
(HTTP 401, `InvalidTokenException`); `500` generic exception (HTTP 200 body);
`409` resubmit/duplicate; `400` bean-validation failure (joined messages);
`422` unreadable request body.
