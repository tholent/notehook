# Equipment, Reference Data & System API

This document specifies the device/equipment management, data-dictionary,
system-reference (parameter) and system-log surfaces of the Supernote cloud-sync
server (Spring Boot, decompiled from a ClassFinal-protected JAR — method bodies
are stripped, so behaviour below is inferred from controller/DTO/VO annotations,
domain objects, MyBatis mapper SQL and error-code enums; uncertainty is flagged).

Server listens on port **19071**. Every response is the `BaseVO` envelope
(`success`/`errorCode`/`errorMsg`); logical failures return **HTTP 200 with
`success=false`**, never 4xx/5xx. An invalid/expired token yields HTTP 401; an
unhandled server error yields `errorCode="500"`.

## Domain overview

- **Device lifecycle.** A physical Supernote device is identified by its serial
  number (`equipmentNo` / `equipment_number`). The manufacturing/inventory table
  (`e_equipment`) holds one row per built device with its firmware version and
  update status. A device is *activated* (registered against inventory), then
  *bound* to exactly one user account (`e_user_equipment` links `user_id` ↔
  `equipment_number`). A device already bound to another account cannot be
  re-bound (error `E0503`/`E0083`). Unbinding removes the link row. `bind/status`
  lets a device ask the cloud whether it is currently bound.
- **Manuals.** Per-device user manuals are versioned by language and logic
  version in `e_equipment_manual`; the device downloads the matching PDF/URL.
- **Dictionaries & references.** `b_dictionary` (business-code → coded value with
  Chinese/English/Japanese meanings) and `b_reference` (business-code + serial →
  value) are lookup/config tables. `b_reference` also backs the public
  `official/system/base/param` endpoint that the login flow reads (it returns a
  `random` nonce used for secure-login hashing).
- **System logs.** Runtime logback level management (query/adjust log levels
  without a restart). Note: device-uploaded *diagnostic* logs (the `E05xx`
  "device log" error family) are handled by a separate feedback/log controller
  outside this document's scope; the codes are listed here because they live in
  `EquipmentErrorCodeEnum`.

## Endpoint summary

### `E_EquipmentController` — base path `/api`

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| POST | `/api/terminal/user/activateEquipment` | Required (inferred) | Activate device |
| POST | `/api/terminal/user/bindEquipment` | Required (inferred) | Bind device to account |
| POST | `/api/terminal/equipment/unlink` | Required (inferred) | Unbind device |
| POST | `/api/equipment/bind/status` | Required (inferred) | Query binding status of caller device |
| POST | `/api/equipment/query/user/equipment/deleteApi` | Required (inferred) | **Deprecated** — paged query of user devices by criteria |
| POST | `/api/equipment/query/by/equipmentno` | Required (inferred) | Query bound user-device by device number |
| POST | `/api/equipment/manual/deleteApi` | Required (inferred) | **Deprecated** — query device manual |
| GET | `/api/equipment/query/{equipmentNo}/deleteApi` | Not required (internal) | **Deprecated** — query device by number (internal call) |
| GET | `/api/equipment/query/by/{userId}` | Required (inferred) | List a user's devices by user id |
| DELETE | `/api/equipment/delete/{equipmentNumber}/deleteApi` | Required (inferred) | **Deprecated** — delete device by number |

### `B_DictionaryController` — base path `/api/system/base`

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| GET | `/api/system/base/dictionary/deleteApi` | Required (inferred) | **Deprecated** — list dictionary values by name/language |
| POST | `/api/system/base/dictionary/deleteApi` | Required (inferred) | **Deprecated** — fuzzy paged dictionary query |
| GET | `/api/system/base/dictionary/{id}/deleteApi` | Required (inferred) | **Deprecated** — get dictionary entry by id |
| POST | `/api/system/base/dictionary/param/deleteApi` | Required (inferred) | **Deprecated** — query dictionary by business code + value |
| GET | `/api/system/base/dictionary/by/{name}/deleteApi` | Required (inferred) | **Deprecated** — query dictionary by business code |

### `B_ReferenceController` — base path `/api`

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| POST | `/api/system/base/reference/deleteApi` | Required (inferred) | **Deprecated** — fuzzy paged reference query |
| GET | `/api/system/base/reference/{id}/deleteApi` | Required (inferred) | **Deprecated** — get reference by id |
| POST | `/api/system/base/reference/param` | Required (inferred) | Query reference by business code + serial |
| POST | `/api/official/system/base/param` | Not required (public) | Fetch public system parameters + login random nonce |

### `S_SystemLogController` — base path `/api/system/log`

| Method | Path | Auth | Summary |
| --- | --- | --- | --- |
| GET | `/api/system/log/level` | Required (inferred) | Query current logback log-level config |
| PUT | `/api/system/log/level` | Required (inferred) | Dynamically change a logger's level (no restart) |

---

## Equipment endpoints

### POST `/api/terminal/user/activateEquipment`

Activate a device. Marks the device (looked up in the `e_equipment` inventory
table) as activated. `equipment` channel is **3 = terminal** (path segment
`/terminal/`).

- **Auth:** Required (inferred). Headers `x-access-token` (JWT), `equipmentNo`
  (device serial).
- **Request body:** `ActivateEquipmentDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| `equipmentNo` | string | Yes | `@NotBlank` — "设备号不能为空" (Device number cannot be empty) | Device serial number |

- **Response:** `BaseVO` (envelope only).
- **Errors:** `E0501` invalid device; `E0506` device not found in inventory;
  `E0505` device version number cannot be empty; `E0502` account does not exist.
- **Notes:** Body carries only the serial; the acting account is taken from the
  JWT. Inventory lookup corresponds to `EquipmentMapper.queryEquipment`
  (`select … from e_equipment where equipment_number = #{equipmentNo}`).

### POST `/api/terminal/user/bindEquipment`

Bind a device to a user account. Creates a link row in `e_user_equipment`.

- **Auth:** Required (inferred). Headers `x-access-token`, `equipmentNo`.
- **Request body:** `BindEquipmentDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| `equipmentNo` | string | Yes | `@NotBlank` — "设备号不能为空" (Device number cannot be empty) | Device serial number |
| `account` | string | Yes | `@NotNull` — "账号不能为空" (Account cannot be empty) | Account (email/phone) to bind to |
| `countryCode` | string | No | — | Country code |
| `name` | string | Yes | `@NotNull` — "设备名称不能为空" (Device name cannot be empty) | Device display name |
| `totalCapacity` | string | Yes | `@NotNull` — "设备总容量不能为空" (Total capacity cannot be empty) | Total device storage capacity |
| `flag` | string | No | — | Flag (fixed value: `1`) |
| `label` | array&lt;string&gt; | Yes | `@NotNull` — "标签页不能为空" (Tab page cannot be empty) | Tab-page labels |

- **Response:** `BaseVO`.
- **Errors:** `E0502` account does not exist; `E0503` device already bound to
  another account and cannot be re-bound; `E0083` (same, `BaseErrorCodeEnum`
  variant); `E0075` device already bound to *this* account — no need to bind
  again; `E0077` logged-in account differs from the account bound to the device;
  `E0501` invalid device; `E0506` device not found in inventory.
- **Notes:** Insert corresponds to `UserEquipmentMapper.insert`
  (`insert into e_user_equipment (equipment_number, user_id, name, create_time)
  values (…, sysdate())`). One device → one account is enforced at bind time.

### POST `/api/terminal/equipment/unlink`

Unbind (unlink) a device from the caller's account.

- **Auth:** Required (inferred). Headers `x-access-token`, `equipmentNo`.
- **Request body:** `UnbindEquipmentDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| `equipmentNo` | string | Yes | `@NotNull` — "设备号不能为空" (Device number cannot be empty) | Device serial number |

- **Response:** `BaseVO`.
- **Errors:** `E0560` device is not bound to an account; `E0077` logged-in
  account differs from account bound to the device; `E0501` invalid device.
- **Notes:** Delete corresponds to `UserEquipmentMapper.delete`
  (`delete from e_user_equipment where user_id=#{userId} and
  equipment_number=#{equipmentNumber}`). The `user_id` is taken from the JWT, so
  a caller can only unbind devices bound to their own account.

### POST `/api/equipment/bind/status`

Return whether the calling device is currently bound to an account. Takes no
body — the device serial is read from the `equipmentNo` header (via
`HttpServletRequest`).

- **Auth:** Required (inferred). Headers `x-access-token`, `equipmentNo`.
- **Request body:** none (parameters read from request/headers).
- **Response:** `BindStatusVO`

| Field | Type | Description |
| --- | --- | --- |
| `bindStatus` | boolean | Binding status — `true` = bound, `false` = not bound (defaults to `false`) |
| `success` | boolean | Envelope success flag |
| `errorCode` | string | Envelope error code |
| `errorMsg` | string | Envelope error message |

- **Errors:** `E0501` invalid device; `E0712`/`E0085` token invalid/expired.
- **Notes:** Bound state is inferred from the existence of an `e_user_equipment`
  row for the device serial (`UserEquipmentMapper.queryUserEquipment`).

### POST `/api/equipment/query/user/equipment/deleteApi` — Deprecated

Paged query of user devices by admin-style criteria. Marked `@Deprecated`; path
carries the `/deleteApi` retirement suffix.

- **Auth:** Required (inferred).
- **Request body:** `QueryEquipmentDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| `pageNo` | string | Yes | `@NotBlank` — "页码不能为空" (Page number cannot be empty) | Page number |
| `pageSize` | string | Yes | `@NotBlank` — "每页显示的个数不能为空" (Page size cannot be empty) | Items per page |
| `equipmentNumber` | string | No | — | Device number |
| `firmwareVersion` | string | No | — | Firmware version |
| `countryCode` | string | No | — | Country code |
| `telephone` | string | No | — | Phone number |
| `email` | string | No | — | Email |

- **Response:** `CommonListVO<QueryEquipmentVO>` — list envelope with `total`
  (long), `size` (int), `pages` (int) and `voList` of `QueryEquipmentVO` (see
  Data models).
- **Notes:** Corresponds to `UserEquipmentMapper.queryUserEquipmentByParam`
  (LEFT JOIN of `e_user_equipment` to `e_equipment` on `equipment_number`, fuzzy
  `LIKE` on device number). Deprecated — do not implement unless required for
  device compatibility.

### POST `/api/equipment/query/by/equipmentno`

Query the bound user-device record for a given device serial.

- **Auth:** Required (inferred).
- **Request body:** `UserEquipmentDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| `equipmentNo` | string | Yes | `@NotNull` — "设备号不能为空" (Device number cannot be empty) | Device serial number |

- **Response:** `UserEquipmentVO`

| Field | Type | Description |
| --- | --- | --- |
| `equipmentNumber` | string | Device number |
| `userId` | long | Owning user id |
| `name` | string | Device name |
| `status` | string | Device status: `Y` = normal, `N` = locked |
| `success`/`errorCode`/`errorMsg` | — | Envelope fields |

- **Errors:** `E0556` device information does not exist; `E0560` device not bound
  to an account.
- **Notes:** Maps to `UserEquipmentMapper.queryUserEquipment`
  (`select … from e_user_equipment where equipment_number = #{equipmentNumber}`).

### POST `/api/equipment/manual/deleteApi` — Deprecated

Query the user manual for a device/language/logic-version. Marked `@Deprecated`.

- **Auth:** Required (inferred).
- **Request body:** `EquipmentManualDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| `equipmentNo` | string | Yes | `@NotBlank` — "设备号不能为空" (Device number cannot be empty) | Device serial number |
| `language` | string | Yes | `@NotBlank` — "语言不能为空" (Language cannot be empty) | Language — one of `JP`, `CN`, `HK`, `EN` |
| `logicVersion` | string | Yes | `@NotBlank` — "逻辑版本号不能为空" (Logic version cannot be empty) | Logic version number |

- **Response:** `EquipmentManualVO`

| Field | Type | Description |
| --- | --- | --- |
| `equipmentNo` | string | Device number |
| `url` | string | Download URL |
| `md5` | string | MD5 checksum of the manual file |
| `fileName` | string | File name |
| `version` | string | Manual version number |
| `success`/`errorCode`/`errorMsg` | — | Envelope fields |

- **Notes:** Corresponds to `EquipmentManualMapper.queryEquipmentManual`
  (`select id,logic_version,language,file_name,url,version,md5,create_time from
  e_equipment_manual where language=#{language}`). Note the SQL filters only on
  `language`; the device/logic-version filtering (if any) happens in the
  stripped service body. Deprecated.

### GET `/api/equipment/query/{equipmentNo}/deleteApi` — Deprecated, internal

Query raw device (inventory) info by serial. Marked `@Deprecated` and documented
as an internal call.

- **Auth:** Not required (internal service-to-service call).
- **Path params:** `equipmentNo` (string) — device serial number.
- **Response:** `EquipmentVO`

| Field | Type | Description |
| --- | --- | --- |
| `equipmentNumber` | string | Device number |
| `firmwareVersion` | string | Firmware version number |
| `updateStatus` | string | Update status — `0` initial version, `1` not updated, `2` updated |
| `remark` | string | Remark |

- **Notes:** `EquipmentVO` is a bare `Serializable` (no `BaseVO` envelope) —
  consistent with an internal DTO. Maps to `EquipmentMapper.queryEquipment` on
  `e_equipment`.

### GET `/api/equipment/query/by/{userId}`

List all devices bound to a given user id.

- **Auth:** Required (inferred).
- **Path params:** `userId` (long) — user id.
- **Response:** `UserEquipmentListVO`

| Field | Type | Description |
| --- | --- | --- |
| `equipmentVOList` | array&lt;`UserEquipmentVO`&gt; | Device records (see `UserEquipmentVO` above) |
| `success`/`errorCode`/`errorMsg` | — | Envelope fields |

- **Notes:** Maps to `UserEquipmentMapper.queryUserEquipmentByUserid`
  (`select … from e_user_equipment where user_id = #{userId}`).

### DELETE `/api/equipment/delete/{equipmentNumber}/deleteApi` — Deprecated

Delete a device record by serial. Marked `@Deprecated`.

- **Auth:** Required (inferred).
- **Path params:** `equipmentNumber` (string) — device serial number.
- **Response:** `BaseVO`.
- **Notes:** Corresponds to `EquipmentMapper.delete`. Deprecated.

---

## Dictionary endpoints

All `B_DictionaryController` endpoints are `@Deprecated` (each path ends in
`/deleteApi`). Base path `/api/system/base`. They read the `b_dictionary` table
(business-code → coded value with localized meanings).

### GET `/api/system/base/dictionary/deleteApi` — Deprecated

List dictionary values, optionally by name-type and language.

- **Auth:** Required (inferred).
- **Query params:**

| Param | Type | Required | Description |
| --- | --- | --- | --- |
| `name` | string | No | Business-code / name type. If empty, defaults to querying by `TRESOURCETYPE_ID`; if present, queries by that name type |
| `language` | string | No | Language. Empty defaults to Chinese; value `US` returns the English version |

- **Response:** `List<DictionarysVO>` (raw JSON array, not wrapped in an
  envelope).

| `DictionarysVO` field | Type | Description |
| --- | --- | --- |
| `id` | long | Data id |
| `valueMeaning` | string | Data meaning (Chinese or English per `language`) |

- **Notes:** Maps to `DictionaryMapper.findDictionaryByName` — selects
  `value as id`, and `value_cn`/`value_en as valueMeaning` chosen by
  `language == 'US'`, `where name=#{name}`.

### POST `/api/system/base/dictionary/deleteApi` — Deprecated

Fuzzy, paged dictionary query.

- **Auth:** Required (inferred).
- **Request body:** `DictionaryVagueDTO` (extends `PageDTO`)

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| `pageNo` | integer | Yes | `@NotNull` — "起始页不能为空" (Start page cannot be empty) | Page number (from `PageDTO`) |
| `pageSize` | integer | Yes | `@NotNull` — "页大小不能为空" (Page size cannot be empty) | Page size (from `PageDTO`) |
| `sortField` | string | No | — | Sort field (from `PageDTO`) |
| `sortRules` | string | No | — | Sort rule (from `PageDTO`) |
| `name` | string | No | — | Dictionary name |
| `valueMeaning` | string | No | — | Data meaning (fuzzy) |

- **Response:** `CommonListVO<DictionaryVagueVO>` — `total`/`size`/`pages` plus
  `voList` of `DictionaryVagueVO` (see Data models).
- **Notes:** Maps to `DictionaryMapper.queryList` (fuzzy `LIKE` on `name` and
  `value_cn`, joined to `b_user` for `opUser`, ordered by `op_time DESC`).

### GET `/api/system/base/dictionary/{id}/deleteApi` — Deprecated

Get a dictionary entry by id.

- **Auth:** Required (inferred).
- **Path params:** `id` (long) — dictionary id.
- **Response:** `DictionaryVO` (see Data models).
- **Notes:** Maps to `DictionaryMapper.queryDictionaryById`.

### POST `/api/system/base/dictionary/param/deleteApi` — Deprecated

Query dictionary entries by business code and coded value.

- **Auth:** Required (inferred).
- **Request body:** `DictionaryQueryDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| `name` | string | No | — | Business code |
| `value` | string | No | — | Code / value |

- **Response:** `DictionaryListVO`

| Field | Type | Description |
| --- | --- | --- |
| `dictionaryVOList` | array&lt;`DictionarysVO`&gt; | Data collection |
| `success`/`errorCode`/`errorMsg` | — | Envelope fields |

- **Notes:** Maps to `DictionaryMapper.queryByPamram` / `checkPamram`
  (optional `name` and `value` equality filters).

### GET `/api/system/base/dictionary/by/{name}/deleteApi` — Deprecated

Query dictionary entries by business code.

- **Auth:** Required (inferred).
- **Path params:** `name` (string) — business code.
- **Response:** `DictionaryByNameVO`

| Field | Type | Description |
| --- | --- | --- |
| `dictionaryVOList` | array&lt;`DictionaryVO`&gt; | Data collection |
| `success`/`errorCode`/`errorMsg` | — | Envelope fields |

- **Notes:** Uses `DictionaryMapper.findDictionaryByName` / `query`. Note the two
  list VOs differ in element type: `DictionaryByNameVO` holds `DictionaryVO`,
  `DictionaryListVO` holds `DictionarysVO` — preserve exactly.

---

## Reference endpoints

Base path `/api`. Read the `b_reference` table (business-code + serial → value).

### POST `/api/system/base/reference/deleteApi` — Deprecated

Fuzzy, paged query of system parameter details.

- **Auth:** Required (inferred).
- **Request body:** `ReferenceVaguaDTO` (extends `PageDTO`)

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| `pageNo` | integer | Yes | `@NotNull` — "起始页不能为空" (Start page cannot be empty) | Page number (from `PageDTO`) |
| `pageSize` | integer | Yes | `@NotNull` — "页大小不能为空" (Page size cannot be empty) | Page size (from `PageDTO`) |
| `sortField` | string | No | — | Sort field (from `PageDTO`) |
| `sortRules` | string | No | — | Sort rule (from `PageDTO`) |
| `name` | string | No | — | Business code |

- **Response:** `CommonListVO` (raw generic; element type `ReferenceVO` inferred
  from `ReferenceMapper.query`).
- **Notes:** Maps to `ReferenceMapper.query` (fuzzy `LIKE` on `name`, ordered by
  `op_time DESC`).

### GET `/api/system/base/reference/{id}/deleteApi` — Deprecated

Get a system-parameter record by id.

- **Auth:** Required (inferred).
- **Path params:** `id` (long) — parameter id.
- **Response:** `ReferenceVO` (see Data models).
- **Notes:** Maps to `ReferenceMapper.queryById`.

### POST `/api/system/base/reference/param`

Query reference/parameter details by coded value (serial) and business code.

- **Auth:** Required (inferred).
- **Request body:** `ReferenceQueryDTO`

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| `serial` | string | No | — | Code / serial |
| `name` | string | Yes | `@NotBlank` — "业务码不能为空" (Business code cannot be empty) | Business code |

- **Response:** `ReferenceListVO`

| Field | Type | Description |
| --- | --- | --- |
| `referenceVOList` | array&lt;`ReferenceVO`&gt; | Data collection |
| `success`/`errorCode`/`errorMsg` | — | Envelope fields |

- **Notes:** Maps to `ReferenceMapper.queryByParamCode` (optional `name` and
  `serial` equality filters, joined to `b_user`).

### POST `/api/official/system/base/param`

Fetch the public system-parameter set plus a login random nonce. This is an
`official`/public endpoint (no device/user context required) — the device or web
client calls it before login to obtain configuration and the `random` value used
for secure-login hash effacement.

- **Auth:** Not required (public). Parameters read from
  `HttpServletRequest`/`HttpServletResponse` (e.g. client IP for the nonce).
- **Request body:** none.
- **Response:** `ReferenceRespVO`

| Field | Type | Description |
| --- | --- | --- |
| `paramList` | array&lt;`ReferenceInfoVO`&gt; | Parameter collection |
| `random` | string | Random number, used for secure-login verification |
| `success`/`errorCode`/`errorMsg` | — | Envelope fields |

`ReferenceInfoVO`:

| Field | Type | Description |
| --- | --- | --- |
| `serial` | string | Serial number |
| `name` | string | Parameter code |
| `value` | string | Parameter value |

- **Notes:** Maps to `ReferenceMapper.queryParam`
  (`SELECT serial, name, group_concat(value) value FROM b_reference GROUP BY
  name`) — multiple values per business code are comma-joined into one `value`.
  The `random` nonce is generated server-side and (per `E0561`/`E0562`) is
  short-lived and single-use for the login-hash flow.

---

## System-log endpoints

Base path `/api/system/log`. Manage the running application's logback log levels
at runtime. Allowed levels: `ERROR`, `WARN`, `INFO`. A curated set of framework
loggers is recognized (`org.springframework`, `org.springframework.boot`,
`org.springframework.web`, `org.springframework.data.redis`, `org.apache`,
`org.mybatis`, `com.alibaba.druid`, `io.netty`, `com.corundumstudio.socketio`,
`springfox`).

### GET `/api/system/log/level`

Query the current log-level configuration.

- **Auth:** Required (inferred) — administrative operation.
- **Request body:** none.
- **Response:** `CommonVO<Map<String, Object>>` — the envelope plus a `voT` map
  whose keys are logger names and values are their current levels/metadata
  (exact shape not recoverable from the stripped body; inferred to include the
  framework logger set above and the application root/`com.ratta` loggers).

### PUT `/api/system/log/level`

Dynamically change a logger's level without restarting.

- **Auth:** Required (inferred) — administrative operation.
- **Request body:** `Map<String, String>` (raw JSON object). Inferred keys: a
  logger name (e.g. `com.ratta`) → target level, and/or a `level`/`logger` pair.
  Only `ERROR`, `WARN`, `INFO` are accepted (validated against `ALLOWED_LEVELS`);
  other values are rejected.
- **Response:** `BaseVO`.
- **Notes:** `parseLevel(String)` (private) maps the string to a logback
  `ch.qos.logback.classic.Level`. An unrecognized level is inferred to return
  `success=false`. Exact request-key names are uncertain (body stripped).

---

## Data models

### Envelope types

- **`BaseVO`** — base envelope: `success` (boolean, default `true`), `errorCode`
  (string), `errorMsg` (string). All VOs below that "extend `BaseVO`" include
  these three fields.
- **`CommonVO<T>`** — `BaseVO` + `voT` (T): single-object payload.
- **`CommonListVO<T>`** — `BaseVO` + `total` (long), `size` (int), `pages` (int),
  `voList` (List&lt;T&gt;): paged list payload.

### `PageDTO` (base paging DTO — `com.ratta.base.dto.PageDTO`)

| Field | Type | Required | Constraints | Description |
| --- | --- | --- | --- | --- |
| `pageNo` | integer | Yes | `@NotNull` — "起始页不能为空" (Start page cannot be empty) | Page number |
| `pageSize` | integer | Yes | `@NotNull` — "页大小不能为空" (Page size cannot be empty) | Page size |
| `sortField` | string | No | — | Sort field |
| `sortRules` | string | No | — | Sort rule |

> Note: there is a **separate** `com.ratta.equipment.dto.PageDTO`; the equipment
> `QueryEquipmentDTO` uses its own string `pageNo`/`pageSize` fields (documented
> above) rather than this base `PageDTO`.

### Equipment DTOs

- **`ActivateEquipmentDTO`** — `equipmentNo` (string, `@NotBlank`).
- **`BindEquipmentDTO`** — see bind endpoint table.
- **`UnbindEquipmentDTO`** — `equipmentNo` (string, `@NotNull`).
- **`UserEquipmentDTO`** — `equipmentNo` (string, `@NotNull`).
- **`QueryEquipmentDTO`** — see deprecated query endpoint table.
- **`EquipmentManualDTO`** — see manual endpoint table.

### Equipment VOs

**`BindStatusVO`** (extends `BaseVO`)

| Field | Type | Description |
| --- | --- | --- |
| `bindStatus` | boolean | `true` = bound, `false` = not bound (default `false`) |

**`UserEquipmentVO`** (extends `BaseVO`)

| Field | Type | Description |
| --- | --- | --- |
| `equipmentNumber` | string | Device number |
| `userId` | long | User id |
| `name` | string | Device name |
| `status` | string | Device status: `Y` normal, `N` locked |

**`UserEquipmentListVO`** (extends `BaseVO`)

| Field | Type | Description |
| --- | --- | --- |
| `equipmentVOList` | array&lt;`UserEquipmentVO`&gt; | Device records |

**`EquipmentVO`** (plain `Serializable`, no envelope)

| Field | Type | Description |
| --- | --- | --- |
| `equipmentNumber` | string | Device number |
| `firmwareVersion` | string | Firmware version |
| `updateStatus` | string | `0` initial, `1` not updated, `2` updated |
| `remark` | string | Remark |

**`QueryEquipmentVO`** (plain `Serializable`)

| Field | Type | Description |
| --- | --- | --- |
| `userId` | string | User id |
| `equipmentNumber` | string | Device number |
| `name` | string | Device name |
| `firmwareVersion` | string | Device firmware version |
| `createTime` | date | Device binding time |
| `activateTime` | date | Device activation time |
| `countryCode` | string | Country code |
| `telephone` | string | Phone number |
| `email` | string | Email |
| `status` | string | Device status: `Y` normal, `N` locked |
| `updateStatus` | string | `0` initial, `1` not updated, `2` updated |
| `remark` | string | Remark |
| `fileServer` | string | File-server flag: `0` ufile, `1` aws |

**`EquipmentManualVO`** (extends `BaseVO`)

| Field | Type | Description |
| --- | --- | --- |
| `equipmentNo` | string | Device number |
| `url` | string | Download URL |
| `md5` | string | MD5 checksum |
| `fileName` | string | File name |
| `version` | string | Version number |

### Dictionary DTOs

- **`DictionaryQueryDTO`** — `name` (string, business code), `value` (string,
  code). No bean-validation constraints.
- **`DictionaryVagueDTO`** (extends `PageDTO`) — `name` (string, dictionary
  name), `valueMeaning` (string, data meaning) + inherited paging fields.

### Dictionary VOs

**`DictionarysVO`** (plain `Serializable`)

| Field | Type | Description |
| --- | --- | --- |
| `id` | long | Data id |
| `valueMeaning` | string | Data meaning |

**`DictionaryVO`** (plain `Serializable`)

| Field | Type | Description |
| --- | --- | --- |
| `id` | long | Id |
| `name` | string | Business code / name |
| `value` | string | Coded value |
| `valueCn` | string | Chinese meaning |
| `valueEn` | string | English meaning |
| `valueJa` | string | Japanese meaning |
| `opUser` | string | Operator (created-by user) |
| `opTime` | date | Operation time |
| `remark` | string | Remark |

**`DictionaryVagueVO`** (plain `Serializable`)

| Field | Type | Description |
| --- | --- | --- |
| `id` | long | Data id |
| `name` | string | Data name |
| `value` | string | Data value |
| `valueCn` | string | Chinese data meaning |
| `valueEn` | string | English data meaning |
| `opUser` | string | Created-by user |
| `opTime` | date | Created time |
| `remark` | string | Remark |

**`DictionaryByNameVO`** (extends `BaseVO`) — `dictionaryVOList`
(array&lt;`DictionaryVO`&gt;).

**`DictionaryListVO`** (extends `BaseVO`) — `dictionaryVOList`
(array&lt;`DictionarysVO`&gt;).

### Reference DTOs

- **`ReferenceQueryDTO`** — `serial` (string, code), `name` (string, business
  code, `@NotBlank` — "业务码不能为空").
- **`ReferenceVaguaDTO`** (extends `PageDTO`) — `name` (string, business code) +
  inherited paging fields.

### Reference VOs

**`ReferenceVO`** (plain `Serializable`)

| Field | Type | Description |
| --- | --- | --- |
| `id` | long | Id |
| `serial` | string | Serial / code |
| `name` | string | Business code |
| `value` | string | Value |
| `valueCn` | string | Chinese value |
| `opUser` | string | Created-by user |
| `opTime` | date | Operation time |
| `remark` | string | Remark |

**`ReferenceInfoVO`** (plain `Serializable`)

| Field | Type | Description |
| --- | --- | --- |
| `serial` | string | Serial number |
| `name` | string | Parameter code |
| `value` | string | Parameter value |

**`ReferenceListVO`** (extends `BaseVO`) — `referenceVOList`
(array&lt;`ReferenceVO`&gt;).

**`ReferenceRespVO`** (extends `BaseVO`) — `paramList`
(array&lt;`ReferenceInfoVO`&gt;), `random` (string, secure-login nonce).

### Domain / table mapping (for reference)

| Table | Purpose | Key columns |
| --- | --- | --- |
| `e_equipment` | Device inventory | `equipment_number`, `firmware_version`, `update_status`, `remark` |
| `e_user_equipment` | Account ↔ device binding | `equipment_number`, `user_id`, `name`, `status`, `create_time` |
| `e_user_equipment_record` | Bind/unbind history | (see `UserEquipmentRecordMapper`) |
| `e_equipment_manual` | Device manuals | `logic_version`, `language`, `file_name`, `url`, `version`, `md5` |
| `t_language` / `t_language_dictionary` | Language packs (by `country_code`) | `id`, `file_name`, `country_code`, `md5`, `size` |
| `b_dictionary` | Data dictionary | `name`, `value`, `value_cn`, `value_en`, `op_user`, `op_time`, `remark` |
| `b_reference` | System parameters | `name`, `serial`, `value`, `value_cn`, `op_user`, `op_time`, `remark` |

---

## Error codes

### `EquipmentErrorCodeEnum` (device/equipment)

| Code | Message |
| --- | --- |
| `E0501` | Invalid device |
| `E0502` | Account does not exist! |
| `E0503` | The device is already bound to another account and cannot be bound to a new account again! |
| `E0504` | This task does not exist on the device! |
| `E0505` | The device version number cannot be empty! |
| `E0506` | This device was not found in inventory! |
| `E0560` | The device is not bound to an account |
| `E0561` | Random number does not exist |
| `E0562` | Random number has expired |
| `E0018` / `E0019` | Incorrect account or password |
| `E0045` | The user has been locked. Please try again later! |
| `E0070` | No need to update! |
| `E0077` | The logged-in account is not the same as the account bound to the device! |
| `E0078` | A device is currently synchronizing. Please wait until it's finished before synchronizing again! |
| `E1202` | Failed to send SMS! |
| `E1203` | Network error. Please try again |
| `E1204` | Warranty period not found. Please contact the purchase channel to inquire about the warranty period |

Device-diagnostic-log family (in `EquipmentErrorCodeEnum`, used by the separate
device-log/feedback surface):

| Code | Message |
| --- | --- |
| `E0550` | Only ordinary logs or reviewed error logs are allowed to be deleted! |
| `E0551` | Adding remarks to un-downloaded records is not allowed! |
| `E0552` | The remark exceeds the maximum number of characters! |
| `E0553` | Failed to add a remark! |
| `E0554` | Only viewed records are allowed to be reviewed! |
| `E0555` | Operation failed! |
| `E0556` | Device information does not exist! |
| `E0557` | Download failed! |
| `E0558` | Failed to add device logs! |

### `BaseErrorCodeEnum` (selected — dictionary/reference/binding/auth)

| Code | Message |
| --- | --- |
| `E0069` | The device is invalid! |
| `E0075` | The device is already bound to this account. No need to bind again! |
| `E0077` | The logged-in account is not the same as the one bound to the device! |
| `E0083` | The device is already bound to another account. It cannot be bound to a new account! |
| `E0085` | The token is invalid! |
| `E0086` | The country code is empty! |
| `E0088` | This resource is already in use by a role and cannot be deleted |
| `E0704` | ID cannot be empty! |
| `E0706` | System error! |
| `E0712` | You are not logged in or your login has expired. Please log in again! |
| `E0730` | Identical codes are not allowed under the same business code! |
| `E0731` | The parameter already exists! |
| `E0739` | The request data is empty! |
| `E0844` | The time zone information for this area was not obtained |
| `500` | Generic unhandled server error (envelope `errorCode`) |

(Full `BaseErrorCodeEnum` E0701–E0742 covers admin CRUD/role/user/scheduled-task
operations outside this document's scope; the subset above is what the
dictionary, reference, and binding flows here can surface.)
