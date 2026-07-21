# User & Account Management API

This document specifies the user-information, account, registration, and
email-server-configuration endpoints of the Supernote cloud-sync server
(reverse-engineered from the ClassFinal-protected Spring Boot JAR under
`com.ratta`). It covers four controllers:

- `U_UserController` — base path `/api` — user existence checks, profile
  query/update, nickname change, admin listing, freeze/unfreeze, and
  registration-restriction management.
- `U_AccountController` — base path `/api/user` — email change.
- `U_UserRegisterController` — base path `/api/user` — registration and account
  cancellation (account clear).
- `U_EmailServerController` — base path `/api` — SMTP email-server config,
  test-send, and public-key retrieval for encrypting sensitive fields.

## Conventions (recap)

- Server listens on port **19071**.
- All responses extend `BaseVO { boolean success=true; String errorCode; String errorMsg }`.
  `CommonListVO<T>` additionally carries `total` (long), `size` (int),
  `pages` (int), and `voList` (List<T>).
- **Logical failures return HTTP 200 with `success:false`** and an `errorCode`
  drawn from the enums below; they are not represented as 4xx/5xx. The
  documented HTTP mappings (401 invalid token, "400" validation, "409"
  resubmit, "422" unreadable, "500" generic) come from framework-level
  exception handlers, not from these handlers' own logic.
- Authentication uses the **`x-access-token`** request header (JWT, HS256) and
  the **`equipmentNo`** header (device identifier). There is no global auth
  interceptor; every handler validates the token itself, so auth is stated
  per-endpoint and marked "(inferred)" where it is deduced from the handler
  reading `HttpServletRequest` / the JWT util rather than proven.
- The `equipment` field elsewhere in the protocol encodes the caller type:
  1 = web cloud disk, 2 = mobile APP, 3 = device/terminal, 4 = user platform.
- Bodies decompile to empty method stubs; request/response shapes below are
  derived entirely from controller annotations, DTO/VO classes, domain `*DO`
  classes, and MyBatis mapper SQL. Behavioural notes are inferences and are
  marked as such.

## Endpoint summary

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| POST | `/api/user/check/exists` | Not required (inferred) | Check whether a user exists (by phone/email/nickname) |
| POST | `/api/official/user/check/exists/server` | Not required (inferred) | Check whether a user exists, disambiguating cross-server accounts |
| POST | `/api/user/query` | Required (inferred) | Query the current user's profile by JWT identity |
| POST | `/api/user/query/info` | Required (inferred) | Query user profile (terminal-compatible variant) |
| POST | `/api/user/update` | Required (inferred) | Update the current user's basic profile |
| POST | `/api/user/update/name` | Required (inferred) | Update the current user's nickname |
| POST | `/api/user/query/all` | Required — admin (inferred) | List users by filter (paged) |
| PUT | `/api/user/freeze` | Required — admin (inferred) | Freeze or unfreeze a user |
| POST | `/api/user/query/one` | Required (inferred) | Query a single user by phone/email |
| GET | `/api/user/query/user/{userId}` | Internal call (inferred) | Query user profile by id (internal) |
| PUT | `/api/user/register/restriction` | Required — admin | Enable/disable global registration restriction |
| GET | `/api/user/register/restriction` | Required — admin | Query global registration-restriction state |
| PUT | `/api/user/email` | Required (inferred) | Change the current user's email |
| POST | `/api/user/register` | Not required | Register a new account |
| POST | `/api/user/account/clear` | Required (inferred) | Cancel (close) the current account |
| POST | `/api/save/email/config` | Required — admin (inferred) | Save SMTP email-server configuration |
| POST | `/api/send/email/test` | Required — admin (inferred) | Send a test email with the given config |
| GET | `/api/query/email/config` | Required — admin (inferred) | Retrieve the active email-server configuration |
| GET | `/api/query/email/publickey` | Not required (inferred) | Retrieve the RSA public key used to encrypt sensitive input |

---

## POST `/api/user/check/exists`

**Summary:** Check whether a user exists. (`@ApiOperation` "检查用户是否存在接口".)

**Auth:** Not required (inferred). Handler receives `HttpServletRequest` but the
operation is an existence probe typically used pre-login.

**Request body:** `UserCheckDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| countryCode | String | No | — | Country code |
| telephone | String | No | — | Phone number |
| email | String | No | — | Email address |
| userName | String | No | — | Nickname |
| domain | String | No | — | Domain (server/region hint) |

**Response:** `BaseVO` (envelope only — `success` / `errorCode` / `errorMsg`).
Existence is conveyed via `success` plus an error code (e.g. E0109 when the user
does not exist); no payload VO is returned by this variant.

**Error codes:** E0109 (user does not exist), E0110 (country code empty),
E0106 (email format), E0104 (phone format), E9999/E0706 (generic).

**Notes:** At least one of `telephone` (with `countryCode`), `email`, or
`userName` is expected in practice, mirroring E0117 ("phone, email and WeChat
cannot all be empty"), though no bean-validation annotation enforces this.

---

## POST `/api/official/user/check/exists/server`

**Summary:** Check whether a user exists, explicitly reporting when the account
lives on another (e.g. US vs. Chinese) server. (`@ApiOperation`
"检查用户是否存在(如果存在于其他服务器，需明确返回)".)

**Auth:** Not required (inferred).

**Request body:** `UserCheckDTO` (see above).

**Response:** `UserCheckVO` (extends `BaseVO`)

| Field | Type | Description |
| --- | --- | --- |
| success | boolean | Envelope — request logically succeeded |
| errorCode | String | Envelope — error code on failure |
| errorMsg | String | Envelope — human-readable error |
| dms | String | Server/region marker for the account (DMS routing hint; identifies which server holds the user) |
| userId | Long | Matched user id, when found |
| uniqueMachineId | String | Bound device's unique machine id, when applicable |

**Error codes:** E0114 (account is on the US server), E0116 (account is on the
Chinese server), E0109 (user does not exist), E0110 (country code empty),
E9999 (network error).

**Notes:** The `dms` / server-routing fields let a client redirect to the
correct regional cloud. Exact semantics of `dms` are inferred from the
"official … server" naming and the E0114/E0116 codes.

---

## POST `/api/user/query`

**Summary:** Query user information by user id. (`@ApiOperation`
"根据用户id查询用户信息".) The user id is taken from the authenticated JWT
identity, not from the body — the handler takes only `HttpServletRequest` /
`HttpServletResponse`.

**Auth:** Required (inferred) — identity resolved from `x-access-token`.

**Request body:** None.

**Response:** `UserQueryByIdVO` (extends `BaseVO`)

| Field | Type | Description |
| --- | --- | --- |
| success | boolean | Envelope |
| errorCode | String | Envelope |
| errorMsg | String | Envelope |
| address | String | Address |
| avatarsUrl | String | Avatar image URL |
| birthday | String | Birthday |
| education | String | Education |
| email | String | Email |
| hobby | String | Hobbies / interests |
| job | String | Occupation |
| personalSign | String | Personal signature / bio |
| telephone | String | Phone number |
| countryCode | String | Country code |
| sex | String | Gender |
| totalCapacity | String | Total storage capacity |
| userName | String | Nickname |
| fileServer | String | File-server flag: `0` = ufile, `1` = aws |
| userId | Long | User id |
| isNormal | String | Account status (`Y` normal, `N` migrated, `F` frozen, `A` admin — per mapper CASE logic) |

**Error codes:** E0712/E0085 (not logged in / token invalid → 401), E0109
(user does not exist), E9999.

---

## POST `/api/user/query/info`

**Summary:** Query user information — terminal-compatible variant.
(`@ApiOperation` "查询用户信息（终端兼容版本）".)

**Auth:** Required (inferred). The DTO additionally carries `token` and
`equipmentNo` in the body, so terminals may authenticate via the body rather
than headers for this compatibility endpoint.

**Request body:** `UserQueryDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| countryCode | String | No | — | Country code |
| value | String | No | — | Lookup value (phone number or email) |
| token | String | No | — | Access token (body-carried, terminal compatibility) |
| equipmentNo | String | No | — | Device identifier (body-carried) |

**Response:** `UserQueryVO` (extends `BaseVO`)

| Field | Type | Description |
| --- | --- | --- |
| success | boolean | Envelope |
| errorCode | String | Envelope |
| errorMsg | String | Envelope |
| user | `UserInfo` | Nested profile object (see Data models) |
| isUser | boolean | Whether the looked-up value corresponds to an existing user (serialized as `isUser`) |
| equipmentNo | String | Echoed device identifier |

**Error codes:** E0109, E0110, E0712/E0085, E9999.

**Notes:** `@Valid` is present but `UserQueryDTO` declares no constraint
annotations, so validation is effectively a no-op. The `value` field is a
generic identifier (phone or email) resolved together with `countryCode`.

---

## POST `/api/user/update`

**Summary:** Update the current user's basic profile information.
(`@ApiOperation` "更新用户基本信息接口".)

**Auth:** Required (inferred) — target user is the JWT identity.

**Request body:** `UserUpdateDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| sex | String | No | — | Gender |
| birthday | String | No | — | Birthday |
| personalSign | String | No | — | Personal signature / bio |
| hobby | String | No | — | Hobbies / interests |
| address | String | No | — | Address |
| job | String | No | — | Occupation |
| education | String | No | — | Education |

**Response:** `BaseVO`.

**Error codes:** E0070 (no need to update), E0705 (modification failed),
E0712/E0085, E9999.

**Notes:** Email, nickname, and password are intentionally not updatable here
(separate endpoints); the `u_user.update` SQL only touches the columns present
in the DTO plus `update_time`.

---

## POST `/api/user/update/name`

**Summary:** Update (rename) the current user's nickname. (`@ApiOperation`
"修改昵称接口".)

**Auth:** Required (inferred).

**Request body:** `UpdateUserNameDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| userName | String | Yes | `@NotBlank` — message "昵称不能为空" ("Nickname cannot be empty") | New nickname |

**Response:** `BaseVO`.

**Error codes:** E0073 (nickname cannot be empty), E0074/E0107/E0111 (nickname
already exists — choose a new one), E0705, E9999.

---

## POST `/api/user/query/all`

**Summary:** Query all users matching filter criteria, paged. (`@ApiOperation`
"按条件查询所有用户".)

**Auth:** Required — admin (inferred). Bulk user listing is an administrative
operation; no `HttpServletRequest` parameter is present, so any auth is enforced
inside the service.

**Request body:** `UserDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| userName | String | No | — | Nickname filter (SQL `LIKE %…%`) |
| telephone | String | No | — | Phone-number filter |
| email | String | No | — | Email filter (SQL `LIKE %…%`) |
| isNormal | String | No | — | Status filter (`Y` normal / `N` migrated / `F` frozen / `A` admin) |
| createTimeStart | String | No | — | Registration start date (`yyyy-MM-dd`) |
| createTimeEnd | String | No | — | Registration end date (`yyyy-MM-dd`) |
| fileServer | String | No | — | File-server filter |
| pageNo | String | Yes | `@NotBlank` — "页码不能为空" ("Page number cannot be empty") | Page number (1-based) |
| pageSize | String | Yes | `@NotBlank` — "每页显示的个数不能为空" ("Page size cannot be empty") | Items per page |

**Response:** `CommonListVO<UserVO>`

| Field | Type | Description |
| --- | --- | --- |
| success | boolean | Envelope |
| errorCode | String | Envelope |
| errorMsg | String | Envelope |
| total | long | Total matching rows |
| size | int | Page size actually used |
| pages | int | Total page count |
| voList | List<`UserVO`> | Page of users (see Data models) |

**Error codes:** E0400 (validation failure on `pageNo`/`pageSize` → "400"),
E0709 (disabled), E0712, E9999.

**Notes:** The mapper's `queryUserAll` maps gender to Chinese labels (男/女) and
`is_normal` to Chinese status labels (正常/已迁移/冻结) in SQL; a client should
treat `UserVO.sex` / `UserVO.isNormal` as possibly-localized display strings for
this endpoint.

---

## PUT `/api/user/freeze`

**Summary:** Freeze or unfreeze a user account. (`@ApiOperation` "冻结或解冻用户".)

**Auth:** Required — admin (inferred).

**Request body:** `FreezeOrUnfreezeUserDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| userId | String | Yes | `@NotBlank` — "用户Id不能为空" ("User id cannot be empty") | Target user id |
| flag | String | Yes | `@NotBlank` — "状态不能为空" ("Status cannot be empty") | `Y` = freeze, `N` = unfreeze |

**Response:** `BaseVO`.

**Error codes:** E0108 (account has been frozen), E0109 (user does not exist),
E0715 (enable/unfreeze failed), E0718 (disable/freeze failed), E9999.

**Notes:** The underlying `updateUserNormal` SQL sets `is_normal` directly; the
`flag` values map to the `u_user.is_normal` column. Freeze likely writes `F`
(frozen) and unfreeze `Y` (normal), inferred from the status-label mapping.

---

## POST `/api/user/query/one`

**Summary:** Query a single user by criteria. (`@ApiOperation` "按条件查询单个用户".)

**Auth:** Required (inferred).

**Request body:** `UserInfoDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| countryCode | String | No | — | Country code |
| telephone | String | No | — | Phone number |
| email | String | No | — | Email address |

**Response:** `UserInfoVo` (extends `BaseVO`)

| Field | Type | Description |
| --- | --- | --- |
| success | boolean | Envelope |
| errorCode | String | Envelope |
| errorMsg | String | Envelope |
| userId | Long | Matched user id (only the id is returned) |

**Error codes:** E0109 (user does not exist), E0110, E9999.

---

## GET `/api/user/query/user/{userId}`

**Summary:** Query user information by id — internal service-to-service call.
(`@ApiOperation` "根据用户id查询用户信息(内部调用)".)

**Auth:** Internal call (inferred) — no `HttpServletRequest` parameter; intended
for trusted intra-service use rather than public clients.

**Path parameters:**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| userId | Long | Yes | Target user id (`@PathVariable`) |

**Request body:** None.

**Response:** `UserQueryByIdVO` (same shape as `POST /api/user/query`; see that
endpoint's table).

**Error codes:** E0109, E9999.

---

## PUT `/api/user/register/restriction`

**Summary:** Enable or disable the global registration restriction (admin only).
(`@ApiOperation` "设置注册限制（仅管理员）".)

**Auth:** Required — admin. Enforced in-handler; failure returns E0122.

**Request body:** `RegistrationRestrictionDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| enabled | Boolean | Yes | `@NotNull` — "enabled不能为空" ("enabled cannot be empty") | `true` restricts new registrations, `false` allows them |

**Response:** `BaseVO`.

**Error codes:** E0122 (only admin can set registration restriction), E0712,
E9999.

**Notes:** The restriction flag is inferred to live in Redis (the register
controller autowires `RedisTemplate`), not a mapped table.

---

## GET `/api/user/register/restriction`

**Summary:** Query the current global registration-restriction state (admin
only). (`@ApiOperation` "查询注册限制状态（仅管理员）".)

**Auth:** Required — admin. Failure returns E0123.

**Request body:** None.

**Response:** `RegistrationRestrictionVO` (extends `BaseVO`)

| Field | Type | Description |
| --- | --- | --- |
| success | boolean | Envelope |
| errorCode | String | Envelope |
| errorMsg | String | Envelope |
| enabled | Boolean | Whether registration is currently restricted |

**Error codes:** E0123 (only admin can query registration restriction), E0712,
E9999.

---

## PUT `/api/user/email`

**Summary:** Change the current user's email address. (`@ApiOperation` "修改邮箱".
Controller `U_AccountController`, base `/api/user`.)

**Auth:** Required (inferred) — target user is the JWT identity
(`HttpServletRequest` parameter present).

**Request body:** `UpdateEmailDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| email | String | Yes | `@NotBlank` — "邮箱不能为空" ("Email cannot be empty") | New email address |

**Response:** `BaseVO`.

**Error codes:** E0105 (email already registered), E0106 (email format
incorrect), E0109 (user does not exist), E0705, E9999.

**Notes:** Because `u_user.update` also sets `user_name = email` when email
changes (see mapper), changing the email may also reset the derived username on
the backend.

---

## POST `/api/user/register`

**Summary:** Register a new account. (`@ApiOperation` "注册". Controller
`U_UserRegisterController`, base `/api/user`.)

**Auth:** Not required.

**Idempotency:** Annotated `@ResubmitCheck(argExpressions={"#userRegisterDTO.email"},
conditionExpressions={"#userRegisterDTO.email != null"})` — duplicate concurrent
submissions keyed on `email` are rejected as a resubmit (mapped to "409").

**Request body:** `UserRegisterDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| email | String | Yes | `@NotBlank` — "邮箱不能为空" ("Email cannot be empty") | Registration email (also used as initial username) |
| password | String | Yes | `@NotBlank` — "密码不能为空" ("Password cannot be empty") | Account password |
| userName | String | No | — | Optional display nickname |

**Response:** `BaseVO`.

**Error codes:** E0105 (email already registered), E0106 (email format
incorrect), E0115 (account frozen — cannot register), E0120 (registration is
currently restricted), E0121 (user already exists — registration not allowed),
"409" (resubmit), E9999.

**Notes:** The `insert` SQL writes `email` into both the `email` and
`user_name` columns; the optional `userName` is applied separately. Registration
honours the global restriction flag set via `/api/user/register/restriction`
(E0120).

---

## POST `/api/user/account/clear`

**Summary:** Cancel / close the current account ("销户"). (`@ApiOperation` "销户".)

**Auth:** Required (inferred) — operates on the JWT identity; takes
`HttpServletRequest` / `HttpServletResponse`.

**Request body:** None.

**Response:** `BaseVO`.

**Error codes:** E0061 (account already cancelled), E0109 (user does not exist),
E0712/E0085, E9999.

**Notes:** On cancellation the account is inferred to be archived into the
`u_user_sold_out` table (`UserSoldOutDO` / `U-UserSoldOutMapper`), capturing
`userId`, `countryCode`, `telephone`, `email`, `wechatNo`, and `createTime`
before the `u_user` row is removed. This is a tombstone so a cancelled
identifier can be recognized on re-registration.

---

## POST `/api/save/email/config`

**Summary:** Save the SMTP email-server configuration. (`@ApiOperation`
"保存邮箱服务器配置". Controller `U_EmailServerController`, base `/api`.)

**Auth:** Required — admin (inferred). Server-wide mail configuration.

**Request body:** `EmailServerDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| smtpServer | String | Yes | `@NotNull` — "服务器地址不能为空" ("Server address cannot be empty") | SMTP host |
| port | String | Yes | `@NotNull` — "服务器地址端口不能为空" ("Server port cannot be empty") | SMTP port |
| username | String | Yes | `@NotNull` — "服务器邮箱地址不能为空" ("Server email address cannot be empty") | SMTP account / from-address |
| password | String | Yes | `@NotNull` — "服务器邮箱密码不能为空" ("Server email password cannot be empty") | SMTP password (see public-key note) |
| encryption | String | Yes | `@NotNull` — "加密方式不能为空" ("Encryption method cannot be empty") | Transport encryption, e.g. `SSL` / `TLS` |
| testEmail | String | No | — | Test-recipient email address |
| language | String | No | — | Language for test/template email content |

**Response:** `BaseVO`.

**Error codes:** E0400 (validation), E1201 (email sending exception), E9999.

**Notes:** Persisted to `u_email_config` with `flag='Y'` (active) on insert; a
prior active config is inferred to be flipped to `flag='N'` (the mapper exposes
an `update` that sets `flag='N'` by id). `password` is expected to arrive
encrypted with the RSA public key from `/api/query/email/publickey`.

---

## POST `/api/send/email/test`

**Summary:** Send a test email using the supplied configuration. (`@ApiOperation`
"测试邮箱发送".)

**Auth:** Required — admin (inferred).

**Request body:** `EmailServerDTO` (same fields as `/api/save/email/config`;
`testEmail` is the recipient and `language` selects the message language).

**Response:** `BaseVO`.

**Error codes:** E1201 (email sending exception), E0400 (validation), E9999.

**Notes:** Does not persist config; only exercises an SMTP send to `testEmail`.

---

## GET `/api/query/email/config`

**Summary:** Retrieve the active email-server configuration. (`@ApiOperation`
"获取邮箱服务器配置".)

**Auth:** Required — admin (inferred).

**Request body:** None.

**Response:** `EmailServerVO` (extends `BaseVO`)

| Field | Type | Description |
| --- | --- | --- |
| success | boolean | Envelope |
| errorCode | String | Envelope |
| errorMsg | String | Envelope |
| smtpServer | String | SMTP host |
| port | String | SMTP port |
| username | String | SMTP account / from-address |
| password | String | SMTP password (likely encrypted / masked) |
| encryption | String | Encryption method: SSL/TLS |
| flag | String | Status: `N` disabled, `Y` enabled |
| testEmail | String | Test-recipient email address |
| adminEmail | String | Administrator email address |

**Error codes:** E0712, E9999. Returns envelope-only with empty fields when no
active (`flag='Y'`) config exists.

**Notes:** Selects the row where `flag='Y'` (`u_email_config`). `adminEmail` has
no matching `EmailServerDO`/table column in the mapper and is inferred to be
populated from separate configuration (e.g. the admin account's email).

---

## GET `/api/query/email/publickey`

**Summary:** Retrieve the RSA public key used to encrypt sensitive request data
(e.g. the SMTP password). (`@ApiOperation` "获取公钥用户数据加密".)

**Auth:** Not required (inferred) — the public key must be fetchable before an
authenticated write.

**Request body:** None.

**Response:** `EmailPublicKeyVO` (extends `BaseVO`)

| Field | Type | Description |
| --- | --- | --- |
| success | boolean | Envelope |
| errorCode | String | Envelope |
| errorMsg | String | Envelope |
| publicKey | String | RSA public key (clients encrypt sensitive fields with it) |

**Error codes:** E9999.

**Notes:** The controller autowires `RedisTemplate`, so the key pair is inferred
to be generated/cached in Redis; the private half stays server-side to decrypt
submitted `EmailServerDTO.password`.

---

## Data models

### Envelope types

**`BaseVO`** — base of every response.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| success | boolean | `true` | `false` on logical failure |
| errorCode | String | — | Error code (see enums) when `success=false` |
| errorMsg | String | — | Human-readable error message |

**`CommonListVO<T>`** — paged-list envelope (extends `BaseVO`).

| Field | Type | Description |
| --- | --- | --- |
| total | long | Total matching records |
| size | int | Page size |
| pages | int | Total number of pages |
| voList | List<T> | Records for the current page |

### Request DTOs

**`UserCheckDTO`** — `countryCode` (String), `telephone` (String), `email`
(String), `userName` (String, nickname), `domain` (String). No validation
annotations.

**`UserQueryDTO`** (Serializable) — `countryCode` (String), `value` (String,
phone or email), `token` (String), `equipmentNo` (String). No validation
annotations.

**`UserUpdateDTO`** (Serializable) — `sex`, `birthday`, `personalSign`, `hobby`,
`address`, `job`, `education` (all String, all optional).

**`UpdateUserNameDTO`** (Serializable) — `userName` (String, `@NotBlank`
"昵称不能为空").

**`UserDTO`** (Serializable) — `userName`, `telephone`, `email`, `isNormal`,
`createTimeStart`, `createTimeEnd`, `fileServer` (all String, optional);
`pageNo` (String, `@NotBlank` "页码不能为空"); `pageSize` (String, `@NotBlank`
"每页显示的个数不能为空").

**`FreezeOrUnfreezeUserDTO`** (Serializable) — `userId` (String, `@NotBlank`
"用户Id不能为空"); `flag` (String, `@NotBlank` "状态不能为空"; `Y` freeze / `N`
unfreeze).

**`UserInfoDTO`** (Serializable) — `countryCode`, `telephone`, `email` (all
String, optional).

**`RegistrationRestrictionDTO`** — `enabled` (Boolean, `@NotNull`
"enabled不能为空").

**`UpdateEmailDTO`** (Serializable) — `email` (String, `@NotBlank`
"邮箱不能为空").

**`UserRegisterDTO`** — `email` (String, `@NotBlank` "邮箱不能为空"); `password`
(String, `@NotBlank` "密码不能为空"); `userName` (String, optional).

**`EmailServerDTO`** (Serializable) — `smtpServer`, `port`, `username`,
`password`, `encryption` (all String, all `@NotNull` with the messages listed in
the save-config endpoint); `testEmail` (String, optional); `language` (String,
optional).

### Response VOs

**`UserCheckVO`** (extends `BaseVO`) — `dms` (String, server/region marker),
`userId` (Long), `uniqueMachineId` (String).

**`UserQueryByIdVO`** (extends `BaseVO`) — `address`, `avatarsUrl`, `birthday`,
`education`, `email`, `hobby`, `job`, `personalSign`, `telephone`, `countryCode`,
`sex`, `totalCapacity`, `userName`, `fileServer` (String, `0` ufile / `1` aws),
`userId` (Long), `isNormal` (String). Full descriptions in the
`POST /api/user/query` table.

**`UserQueryVO`** (extends `BaseVO`) — `user` (`UserInfo`), `isUser` (boolean;
getter `getIsUser`), `equipmentNo` (String).

**`UserInfoVo`** (extends `BaseVO`) — `userId` (Long) only.

**`UserVO`** (Serializable, not a `BaseVO`; appears as list element) —
`userId` (String), `userName` (String), `countryCode` (String), `telephone`
(String), `email` (String), `wechatNo` (String, WeChat credential), `sex`
(String), `birthday` (String), `personalSign` (String), `hobby` (String),
`education` (String), `job` (String), `address` (String), `createTime` (Date,
registration time), `isNormal` (String; `Y` normal / `N` frozen per the field's
own doc, though the `query/all` mapper localizes it), `fileServer` (String).

**`RegistrationRestrictionVO`** (extends `BaseVO`) — `enabled` (Boolean). Has a
convenience constructor `RegistrationRestrictionVO(Boolean enabled)`.

**`EmailServerVO`** (extends `BaseVO`) — `smtpServer`, `port`, `username`,
`password`, `encryption` (SSL/TLS), `flag` (`N` disabled / `Y` enabled),
`testEmail`, `adminEmail` (all String).

**`EmailPublicKeyVO`** (extends `BaseVO`) — `publicKey` (String).

**`UserInfo`** (Serializable; nested inside `UserQueryVO`) —

| Field | Type | Description |
| --- | --- | --- |
| address | String | Address |
| avatarsUrl | String | Avatar image URL |
| birthday | String | Birthday |
| education | String | Education |
| email | String | Email |
| hobby | String | Hobbies / interests |
| job | String | Occupation |
| personalSign | String | Personal signature / bio |
| phone | String | Phone number (note: field is `phone`, not `telephone`) |
| countryCode | String | Country code |
| sex | String | Gender |
| totalCapacity | String | Total storage capacity |
| userName | String | Nickname |
| fileServer | String | File-server flag: `0` ufile, `1` aws |
| userId | Long | User id |

### Domain objects (persistence, for reference)

**`UserDO`** → table `u_user` — `userId` (Long, PK), `userName`, `email`, `sex`,
`birthday`, `personalSign`, `hobby`, `education`, `job`, `avatarsUrl`,
`avatarsPosition`, `address`, `password`, `createTime` (Date), `updateTime`
(Date), `isNormal` (String — `Y` normal / `N` migrated / `F` frozen / `A` admin,
per mapper CASE), `fileServer` (String — `0` ufile / `1` aws), `counts` (Long,
used by the per-file-server count query).

**`UserInfoDO`** → view over `u_user` (used by `queryUserAll`) — same columns as
`UserDO` plus `countryCode`, `telephone`, `wechatNo`; `userId` typed as String
here. No `counts`/`avatarsPosition` in the projection.

**`EmailServerDO`** → table `u_email_config` — `id` (String, PK), `smtpServer`,
`port`, `username` (column `user_name`), `password`, `encryption`, `flag`
(`Y`/`N`), `testEmail`, `updateTime` (Date). Active row selected by `flag='Y'`.

**`UserSoldOutDO`** → table `u_user_sold_out` (cancelled-account tombstones) —
`userId` (Long), `countryCode`, `telephone`, `email`, `wechatNo`, `createTime`
(Date). Queryable by telephone+countryCode, email, or wechatNo.

**`CommonlyArea`** → table `u_commonly_area` (frequently-used regions per user;
referenced by the user domain but not directly exposed by these endpoints) —
`id` (Long, PK), `userId` (Long), `countryCode` (String), `areaCode` (String),
`count` (Integer, incremented on reuse), `createTime` (Date), `updateTime`
(Date).

### Error codes

Drawn from `UserErrorCodeEnum` and `BaseErrorCodeEnum`. Codes most relevant to
these endpoints:

| Code | Meaning |
| --- | --- |
| E0018 | Account does not exist |
| E0061 | The account has been cancelled |
| E0070 | No need to update |
| E0073 | The nickname cannot be empty |
| E0074 | The nickname already exists; choose a new one |
| E0085 | The token is invalid |
| E0086 | The country code is empty |
| E0104 | Phone number format is incorrect |
| E0105 | Email has been registered |
| E0106 | Email format is incorrect |
| E0107 | Nickname already exists; please rename |
| E0108 | Account has been frozen |
| E0109 | User does not exist |
| E0110 | Country code is empty |
| E0111 | Nickname already exists; please rename |
| E0112 | Network error; please try again |
| E0113 | File server already selected |
| E0114 | Account is on the US server |
| E0115 | Account is frozen and cannot be registered temporarily |
| E0116 | Account is on the Chinese server |
| E0117 | Phone number, email, and WeChat cannot all be empty |
| E0118 | Request to Ali(baba) failed |
| E0119 | Request to Alibaba succeeded, but data processing failed |
| E0120 | Registration is currently restricted |
| E0121 | User already exists; registration is not allowed |
| E0122 | Only admin can set registration restriction |
| E0123 | Only admin can query registration restriction |
| E0705 | Modification failed |
| E0706 | System error |
| E0712 | Not logged in or login expired (→ HTTP 401) |
| E0715 | Enablement/unfreeze failed |
| E0718 | Disabling/freeze failed |
| E1201 | Email sending exception |
| E1752 | Identity verification failed |
| E9999 | Network error (generic) |

Framework HTTP mappings: invalid token → 401; validation failure → "400";
resubmit (`@ResubmitCheck`) → "409"; unreadable body → "422"; uncaught → "500".
</content>
</invoke>
