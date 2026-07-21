# File & Sync API (Core)

Reverse-engineered specification for the **core file, sync, search and dropbox-style
(V2/local/NAS)** endpoints of the Supernote cloud-sync server (Spring Boot,
package root `com.ratta`). Derived entirely from controller annotations, DTO/VO
fields, domain `*DO` classes, MyBatis mapper SQL, and `FileErrorCodeEnum` — the
compiled JAR is **ClassFinal-protected**, so all method bodies are stripped and
behaviour below is *inferred* from those artefacts. Uncertainty is flagged inline.

- **Server port:** `19071`
- **Controllers covered:**
  - `F_FileController` (base `/api/file`) — one deprecated folder/file endpoint.
  - `F_FileLocalController` (base `/api/file`) — the "private-deployment / NAS /
    dropbox" file operations (~18 endpoints): sync, folders, upload, download,
    query, move, copy, delete, capacity, note conversion, connectivity ping.
  - `F_FileV2Controller` (base `/api/file`) — dropbox-style file query by id/path.
  - `F_FileSearchController` (base `/api/file`) — label/name search.

## File model (inferred)

The persistent file tree lives in table **`f_user_file`** (`UserFileDO`). Key
columns:

- `id` — file/folder id (bigint, DB-generated on insert).
- `user_id` — owner. Every query is scoped `and user_id = #{userId}` (multi-tenant).
- `directory_id` — parent folder id. The **root directory is `directory_id = 0`**
  (search explicitly excludes `directory_id != 0`; error `E0313` = "Cannot be
  operated from the root directory").
- `file_name` — display name.
- `inner_name` — the object-storage key (S3/bucket). On rename `inner_name` is set
  equal to `file_name` (`updateName`), but on content replacement it tracks the
  uploaded object (`updateInnerName`). So `inner_name` is the physical blob handle.
- `size` — bytes (nullable for folders).
- `md5` — content hash (used for dedup, see below).
- `is_folder` — `"Y"` folder / `"N"` file.
- `is_active` — `"Y"` live / `"N"` logically deleted (soft-delete; the row survives
  until hard-purged). Almost every read filters `is_active = "Y"`.
- `create_time`, `update_time`.
- `terminal_file_edit_time` — last edit timestamp reported by the device (epoch ms,
  Long); lets the device win/merge on modification time during sync.

Related tables:

- **`f_file_action`** (`FileActionDO`) — the *change journal* / incremental-sync
  feed. One row per operation with an `action` code (`R`=rename, and others — see
  notes), `path`/`new_path`, `file_name`/`new_file_name`, `md5`, `inner_name`,
  `size`. `selectByTime` returns rows `where update_time > #{updateTime} order by
  update_time asc` — this is the delta a client pulls with a sync token
  (`update_time` acts as the cursor / `nextSyncToken`).
- **`f_recycle_file`** (`RecycleFileDO`) — recycle-bin index (`file_id`,
  `file_name`, `size`, `is_folder`). Recycle path constant is `/recycle_bin/`.
- **`f_deleted_user_file`** (`DeleteUserFileDO`) — tombstone of permanently deleted
  files (keeps `file_server`, `inner_name`, `md5`, `delete_time = sysdate()`) so a
  re-upload of identical content can be reconciled.
- **`f_capacity`** (`CapacityDO`) — per-user quota: `used_capacity`,
  `total_capacity`. Mutated by `plusUsedCapacity`/`minusUsedCapacity` (clamped at 0)
  on upload/delete.
- **`f_sync_record`** (`SyncRecordDO`) — global sync counters (`success_count`,
  `fail_count`, `total_time`); a metrics/telemetry table, not per-user.
- **`f_file_his_sync`** (`FileHisSyncDO`) — per-device snapshot of the file set at
  last successful sync (`equipment_number`, plus a full copy of file metadata). Used
  to diff the device's previous view against the current server state.
- **`f_file_server_change`** (`FileServerChangeDO`) — records storage-server /
  region migrations (`old_file_server` → `new_file_server`).
- **`f_file_convert`** (`FileConvertDO`) — cache of `.note` → PDF/PNG conversions
  keyed by `origin_inner_name` + `file_type` + `page_no`.

### Sync model (inferred)

The API is a **bidirectional incremental sync** built around a *sync session* and a
*change journal*:

1. The device calls **`/2/files/synchronous/start`** to open a session. The
   response `synType` decides the strategy: `true` = normal incremental sync
   (compare against `f_file_his_sync`); `false` = the device must re-upload its
   entire file set with no comparison (full re-seed). While a session is open,
   concurrent mutating calls fail with `E0301`/`E0078` ("Sync in progress…").
2. The device pulls deltas from `f_file_action` (rows newer than its cursor,
   ordered by `update_time asc`). The cursor is the last-seen `update_time`; the
   config **`next.sync.tiamout=5`** (`application.properties`, sic — "timeout")
   bounds sync-token validity, and an expired token yields `E0330`
   ("NextSyncToken timeout") / `E0342` ("Data has expired…").
3. Mutations (create/rename/move/copy/delete/upload) each append a row to
   `f_file_action` and update `f_user_file`, so the counterpart device sees them on
   its next pull.
4. The device calls **`/2/files/synchronous/end`** with a success `flag` to close
   the session; the server snapshots current state into `f_file_his_sync` and
   updates `f_sync_record` counters.

### Dedup & capacity

- Upload is a two-phase flow: **apply** (server returns a presigned S3 PUT via the
  `supernote` bucket) then **finish** (client posts back `content_hash` + `size` +
  `innerName`; server commits the `f_user_file` row and bumps `f_capacity`).
- MD5 dedup: if a file with the same md5 already exists the server may short-circuit
  with `E0310` ("There are identical md5 files already. No need to upload!").
- Quota is enforced on upload (`E0309`) and copy (`E0307`); global storage
  exhaustion is `E0333`.

### Envelope

All responses extend `BaseVO { boolean success=true; String errorCode; String
errorMsg }`. `CommonVO<T>` adds `voT`; `CommonListVO<T>` adds `total`(long),
`size`(int), `pages`(int), `voList`. **Logical failures return HTTP 200 with
`success=false`** and a `FileErrorCodeEnum` code — not a 4xx/5xx. Transport-level
exceptions map: invalid/expired token → 401; resubmit (idempotency) → "409";
bean-validation failure → "400"; uncaught → "500".

### Auth & headers

- `x-access-token` — JWT (resolved via `JwtTokenUserUtil`); yields `userId`.
- `equipmentNo` — device id, sent both as a header and duplicated in most DTO
  bodies. `equipment` type: 1=web cloud disk, 2=mobile app, 3=device/terminal,
  4=user platform.
- No global auth interceptor is present; token requirement is per-endpoint. Every
  file operation reads `userId` from the token, so all are **Required (inferred)**
  except the connectivity ping `/query/server`.

---

## Endpoint summary

| Method | Path | Auth | Summary |
|--------|------|------|---------|
| POST | `/api/file/add/folder/file/deleteApi` | Required (inferred) | **Deprecated.** Create folder/file (desktop app only) |
| POST | `/api/file/2/files/synchronous/start` | Required (inferred) | Begin sync session (NAS) |
| POST | `/api/file/2/files/synchronous/end` | Required (inferred) | End sync session (NAS) |
| POST | `/api/file/2/files/create_folder_v2` | Required (inferred) | Create folder (NAS) |
| POST | `/api/file/2/files/list_folder` | Required (inferred) | List folder by path (dropbox) |
| POST | `/api/file/3/files/list_folder_v3` | Required (inferred) | List folder by id (NAS) |
| POST | `/api/file/3/files/delete_folder_v3` | Required (inferred) | Delete file/folder → recycle bin (NAS) |
| POST | `/api/file/3/files/upload/apply` | Required (inferred) | Request presigned upload URL (NAS) |
| POST | `/api/file/2/files/upload/finish` | Required (inferred) | Commit uploaded file (NAS) |
| POST | `/api/file/3/files/download_v3` | Required (inferred) | Get download URL by id (NAS) |
| POST | `/api/file/3/files/query_v3` | Required (inferred) | Query file by id (NAS) |
| POST | `/api/file/3/files/query/by/path_v3` | Required (inferred) | Query file by path (NAS) |
| POST | `/api/file/3/files/move_v3` | Required (inferred) | Move / rename (NAS) |
| POST | `/api/file/3/files/copy_v3` | Required (inferred) | Copy file/folder (NAS) |
| POST | `/api/file/2/users/get_space_usage` | Required (inferred) | Query capacity/quota (NAS) |
| POST | `/api/file/note/to/pdf` | Required (inferred) | Convert `.note` → PDF |
| POST | `/api/file/note/to/png` | Required (inferred) | Convert `.note` → PNG (per page) |
| POST | `/api/file/pdfwithmark/to/pdf` | Required (inferred) | Convert annotated-PDF note → PDF |
| GET  | `/api/file/query/server` | Not required | Connectivity ping (no business logic) |
| POST | `/api/file/2/files/query/deleteApi` | Required (inferred) | **Deprecated.** Query file by id (dropbox) |
| POST | `/api/file/2/files` | Required (inferred) | Query file by path (dropbox) |
| POST | `/api/file/label/list/search` | Required (inferred) | Search files/folders by name in a folder |

22 endpoints total (2 deprecated).

---

## POST `/api/file/add/folder/file/deleteApi`  — Create folder or file (DEPRECATED)

**Summary:** "New-folder interface (desktop app only)". Marked `@Deprecated`; the
`deleteApi` suffix signals it is scheduled for removal. Returns a
`FolderFileAddVO`.

**Auth:** Required (inferred).

**Request body — `FolderFileAddDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `fileName` | String | Yes | `@NotBlank` "File name cannot be empty." | File name |
| `fileId` | Long | Yes | `@NotNull` "File ID cannot be empty." | File id |
| `directoryId` | Long | Yes | `@NotNull` "The ID of the file root directory cannot be empty." | Parent directory id |
| `goDirectoryId` | Long | Yes | `@NotNull` "The ID of the target directory the file is being moved to cannot be empty." | Target parent directory id |
| `isFolder` | String | Yes | `@NotNull` "The value of the folder attribute cannot be empty." | `Y`=folder, `N`=file |

**Response — `FolderFileAddVO`** (extends `BaseVO`): see [FolderFileAddVO](#folderfileaddvo--userfilevo). Note the presence of both `directoryId` and `goDirectoryId` suggests this endpoint doubled as a create-and-move.

**Errors:** `E0304` (duplicate name), `E0313` (root-dir op), `E0322` (dup name), validation "400".

---

## POST `/api/file/2/files/synchronous/start`  — Begin sync session (NAS)

**Summary:** "Sync-start interface (NAS edition)". Opens a sync session for a device
and tells it whether to do an incremental or full re-seed sync.

**Auth:** Required (inferred).

**Request body — `SynchronousStartLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No (validated at logic level) | — | Device number |

**Response — `SynchronousStartLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `synType` | Boolean | `true` = normal incremental sync; `false` = client must re-upload **all** file data to the server, no comparison performed |

**Notes/inferences:** `synType=false` maps to the full re-seed path — likely
returned when no `f_file_his_sync` snapshot exists for this `equipmentNo`
(first sync, or after a server migration recorded in `f_file_server_change`).
Concurrent mutation while a session is open → `E0301`/`E0078`.

---

## POST `/api/file/2/files/synchronous/end`  — End sync session (NAS)

**Summary:** "Sync-end interface (NAS edition)". Closes the session opened above.

**Auth:** Required (inferred).

**Request body — `SynchronousEndLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `flag` | String | No | — | Synchronization success flag (client reports whether the sync succeeded) |

**Response — `SynchronousEndLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |

**Notes/inferences:** On success the server snapshots the current file set into
`f_file_his_sync` for this `equipmentNo` (`FileHisSyncMapper.insert`, batch) and
increments `f_sync_record` (`updateSuccess`/`updateFail` with `total_time`).

---

## POST `/api/file/2/files/create_folder_v2`  — Create folder (NAS)

**Summary:** "New-folder interface (NAS edition)".

**Auth:** Required (inferred). Guarded by `@ResubmitCheck` on
(`path`, `equipmentNo`, `path`) when `path != null` — duplicate submits → "409".

**Request body — `CreateFolderLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `path` | String | No* | *empty path → `E0334* | Full path of the folder to create |
| `autorename` | boolean | No | — | If `true`, auto-rename on name collision instead of failing |

**Response — `CreateFolderLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `metadata` | [MetadataVO](#metadatavo) | Created folder metadata (`tag`, `id`, `name`, `path_display`) |

**Errors:** `E0304`/`E0322` (name exists, when `autorename=false`), `E0334`
(empty path), `E0306` (target directory deleted). Creates an `f_user_file` row
(`is_folder="Y"`, `directory_id` from parent path) and a `f_file_action` row.

---

## POST `/api/file/2/files/list_folder`  — List folder by path (dropbox)

**Summary:** "Folder-list interface (dropbox edition)". Lists the contents of a
folder addressed by path.

**Auth:** Required (inferred).

**Request body — `ListFolderV2DTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `path` | String | No | — | Path; pass empty/blank to list the **root** directory |
| `recursive` | boolean | No | — | Recursion flag — include the entire subtree |

**Response — `ListFolderLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `entries` | List<[EntriesVO](#entriesvo)> | Folder contents (folders + files) |

**Notes:** SQL `querybyDirectoryId` (filter `is_active`); ordering in the paged
variant is `order by is_folder desc, ${order} ${sequence}` (folders first).

---

## POST `/api/file/3/files/list_folder_v3`  — List folder by id (NAS)

**Summary:** "Folder-list interface (NAS edition)". Same as above but addressed by
directory **id** rather than path.

**Auth:** Required (inferred).

**Request body — `ListFolderLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `id` | Long | No | — | Directory id (root = `0`) |
| `recursive` | boolean | No | — | Recursion flag |

**Response — `ListFolderLocalVO`** (extends `BaseVO`): same shape as the path variant
(`equipmentNo`, `entries` = List<[EntriesVO](#entriesvo)>).

**Errors:** `E0302` (root directory deleted), `E0303` (folder deleted).

---

## POST `/api/file/3/files/delete_folder_v3`  — Delete file/folder (NAS)

**Summary:** "Delete-file-or-folder interface (NAS edition)". Soft-deletes the target
and moves it to the recycle bin.

**Auth:** Required (inferred).

**Request body — `DeleteFolderLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `id` | Long | Yes | `@NotNull` "File ID cannot be empty." | File/folder id to delete |

**Response — `DeleteFolderLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `metadata` | [MetadataVO](#metadatavo) | Deleted item metadata |

**Notes/inferences:** Soft delete — `UserFileMapper.updateN` sets `is_active="N"`
(folders cascade via `updateListN` over child ids); a row is inserted into
`f_recycle_file`; `f_capacity.minusUsedCapacity` reduces used space; a delete row is
appended to `f_file_action`. Errors: `E0313` (cannot delete root), `E0317`/`E0318`
(target dir/file gone), `E0341` (exception while deleting).

---

## POST `/api/file/3/files/upload/apply`  — Request upload URL (NAS)

**Summary:** "Upload-file apply interface (NAS edition)". Phase 1 of upload: reserves
an inner object name and returns presigned S3 credentials/URLs. No `f_user_file` row
is committed yet.

**Auth:** Required (inferred). `@ResubmitCheck` on (`fileName`, `equipmentNo`,
`path`) when `fileName != null`.

**Request body — `FileUploadApplyLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `path` | String | No | — | Destination path |
| `fileName` | String | No | — | File name |
| `size` | String | No | (str or int accepted) | File size in bytes |

**Response — `FileUploadApplyLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `bucketName` | String | Object-storage bucket — constant `"supernote"` |
| `innerName` | String | Reserved internal object key to upload under |
| `xAmzDate` | String | `X-Amz-Date` value for the signed request |
| `authorization` | String | AWS SigV4 `Authorization` signature |
| `fullUploadUrl` | String | Presigned PUT URL for a single (whole-file) upload |
| `partUploadUrl` | String | Presigned URL for multipart/part upload |

**Errors:** `E0309` (would exceed quota), `E0325` (file-name byte length too long),
`E0324` (file cannot be uploaded), `E0333` (storage full).

---

## POST `/api/file/2/files/upload/finish`  — Commit uploaded file (NAS)

**Summary:** "Upload-file finish interface (NAS edition)". Phase 2: client confirms
the object was PUT to storage; server commits the `f_user_file` row and updates
capacity.

**Auth:** Required (inferred).

**Request body — `FileUploadFinishLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `path` | String | No | — | File path |
| `size` | String | No | (str or int) | File size |
| `fileName` | String | Yes | `@NotBlank` "File name cannot be empty." | File name |
| `content_hash` | String | Yes | `@NotBlank` "File MD5 cannot be empty." | File MD5 |
| `innerName` | String | Yes | `@NotBlank` "Internal file name cannot be empty." | The internal object key returned by /upload/apply |

**Response — `FileUploadFinishLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `path_display` | String | Full display path of committed file |
| `id` | String | New file id |
| `size` | Long | Committed size |
| `name` | String | File name |
| `content_hash` | String | Committed MD5 |

**Notes/inferences:** Inserts/updates `f_user_file` (`insert` or `updateInnerName`
when replacing existing content — the latter updates `inner_name`, `md5`, `size`);
`f_capacity.plusUsedCapacity`; appends to `f_file_action`. Dedup: identical md5 may
return `E0310`. Errors: `E0304`/`E0322` (name exists), `E0306` (target dir deleted),
`E0309`/`E0333` (quota).

---

## POST `/api/file/3/files/download_v3`  — Get download URL (NAS)

**Summary:** "Download-file interface (NAS edition)". Returns a presigned/download URL
for a file addressed by id.

**Auth:** Required (inferred).

**Request body — `FileDownloadLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `id` | Long | Yes | `@NotNull` "File ID cannot be empty." | File id |

**Response — `FileDownloadLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `id` | String | File id |
| `url` | String | Download URL |
| `name` | String | File name |
| `path_display` | String | Full display path |
| `content_hash` | String | MD5 hash |
| `size` | Long | File size |
| `is_downloadable` | boolean | Whether download is supported |

**Errors:** `E0308`/`E0321` (file does not exist), `E0303` (deleted),
`E0311` (in recycle bin — restored or permanently deleted).

---

## POST `/api/file/3/files/query_v3`  — Query file by id (NAS)

**Summary:** "Query-file interface (NAS edition)". Fetches metadata for one file/folder
by id.

**Auth:** Required (inferred).

**Request body — `FileQueryLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `id` | String | No | — | File id |

**Response — `FileQueryLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `entriesVO` | [EntriesVO](#entriesvo) | File/folder metadata |

**Errors:** `E0308`/`E0321` (does not exist), `E0303` (deleted). SQL `queryByIdY`
(only `is_active="Y"` rows).

---

## POST `/api/file/3/files/query/by/path_v3`  — Query file by path (NAS)

**Summary:** "Query-file interface (by path) (NAS edition)".

**Auth:** Required (inferred).

**Request body — `FileQueryByPathLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `path` | String | No | — | Full file path |

**Response — `FileQueryByPathLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `entriesVO` | [EntriesVO](#entriesvo) | File/folder metadata |

**Errors:** `E0308`/`E0321` (does not exist), `E0303` (deleted), `E0334` (empty path).

---

## POST `/api/file/3/files/move_v3`  — Move / rename (NAS)

**Summary:** "Move-or-rename-file interface (NAS edition)". A single endpoint for both
move (change `directory_id`) and rename (change `file_name`) — driven by `to_path`.

**Auth:** Required (inferred).

**Request body — `FileMoveLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `autorename` | boolean | No | — | Auto-rename on collision at destination |
| `id` | Long | Yes | `@NotNull` "File ID cannot be empty." | File/folder id to move |
| `to_path` | String | No | — | Target parent directory (full path) |

**Response — `FileMoveLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `entriesVO` | [EntriesVO](#entriesvo) | Metadata of moved/renamed item |

**Notes/inferences:** SQL `updateNameAndDirectoryId` (rename+move) /
`updateName` (rename only); appends `f_file_action` with `action` distinguishing
move vs rename (`R` = rename per `selectByParam`). Errors: `E0319`/`E0320`
(target file/dir to move or rename does not exist), `E0304`/`E0322`
(dup name at target), `E0305` (already moved), `E0343` (rename failed),
`E0344` (move failed), `E0358` ("Cannot move a folder into itself or its
subdirectory"), `E0313` (root op).

---

## POST `/api/file/3/files/copy_v3`  — Copy file/folder (NAS)

**Summary:** "Copy-file-or-folder interface V3 (NAS edition)". Note: the controller's
`@ApiImplicitParam` names the type `FileCopyV3DTO`, but the actual bound parameter is
`FileCopyLocalDTO` (the V3 name is a stale annotation).

**Auth:** Required (inferred).

**Request body — `FileCopyLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `id` | Long | Yes | `@NotNull` "File ID list cannot be empty." | File/folder id to copy |
| `autorename` | boolean | No | — | Auto-rename on collision |
| `to_path` | String | Yes | `@NotEmpty` "The target directory path cannot be empty." | Target parent directory |

**Response — `FileCopyLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `entriesVO` | [EntriesVO](#entriesvo) | Metadata of the new copy |

**Notes/inferences:** Copy duplicates the `f_user_file` row (and children for
folders) and charges quota — `E0307` ("Copying the files will exceed the total
capacity!"). Errors: `E0306` (target dir deleted), `E0304`/`E0322` (dup name),
`E0345` (copy failed), `E0308` (source missing). The `@NotNull` message
("File ID **list** cannot be empty") hints an earlier multi-id copy design, though
the field is a single `Long`.

---

## POST `/api/file/2/users/get_space_usage`  — Query capacity (NAS)

**Summary:** "Query-capacity interface (NAS edition)". Returns used vs. total quota
for the user (Dropbox `get_space_usage`-shaped).

**Auth:** Required (inferred).

**Request body — `CapacityLocalDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |

**Response — `CapacityLocalVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `used` | Long | Used capacity (bytes) |
| `allocationVO` | [AllocationVO](#allocationvo) | Total allocation (`tag`, `allocated`) |
| `equipmentNo` | String | Echoed device number |

**Notes:** Backed by `f_capacity` (`query`). `used` = `used_capacity`,
`allocationVO.allocated` = `total_capacity`. `E0109` if the user does not exist.

---

## POST `/api/file/note/to/pdf`  — Convert `.note` → PDF

**Summary:** "note-to-pdf interface". Converts a Supernote `.note` file (or selected
pages) to a PDF and returns a download URL.

**Auth:** Required (inferred).

**Request body — `PdfDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `id` | Long | Yes | `@NotNull` "id不能为空" (id cannot be empty) | Source file id |
| `host` | String | No | — | Host (base URL for building the returned download link) |
| `pageNoList` | List<Integer> | No | — | Page numbers to include (omit for all pages) |

**Response — `PdfVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `url` | String | Download URL of the generated PDF |

**Errors:** `E0314` (QT program failed to parse the file), `E0315` (incorrect file
type), `E0316` (file is being converted). Conversions cached in `f_file_convert`.

---

## POST `/api/file/note/to/png`  — Convert `.note` → PNG

**Summary:** "note-to-png interface". Converts a `.note` file to per-page PNG images.

**Auth:** Required (inferred).

**Request body — `PngDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `id` | Long | Yes | `@NotNull` "id不能为空" (id cannot be empty) | Source file id |
| `host` | String | No | — | Host (base URL for returned links) |

**Response — `PngVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `pngPageVOList` | List<[PngPageVO](#pngpagevo)> | Per-page download URLs (`pageNo`, `url`) |

**Errors:** `E0314`, `E0315`, `E0316`. `f_file_convert.selectPngList` uses
`file_type="2"` for PNG conversions keyed by `origin_inner_name`.

---

## POST `/api/file/pdfwithmark/to/pdf`  — Convert annotated-PDF note → PDF

**Summary:** "pdf-with-annotations note-to-pdf interface". Same request/response as
`/note/to/pdf` (reuses `PdfDTO` → `PdfVO`); flattens a PDF that was annotated on the
device into a standalone PDF.

**Auth:** Required (inferred).

**Request body — `PdfDTO`:** identical to [note/to/pdf](#post-apifilenotetopdf--convert-note--pdf).

**Response — `PdfVO`** (extends `BaseVO`): `url` (download URL).

**Errors:** `E0314`, `E0315`, `E0316`.

---

## GET `/api/file/query/server`  — Connectivity ping

**Summary:** "Used by the client to check connectivity with the server; performs no
business processing."

**Auth:** Not required.

**Params:** none.

**Response — `BaseVO`:** bare envelope (`success`, `errorCode`, `errorMsg`).

---

## POST `/api/file/2/files/query/deleteApi`  — Query file by id (dropbox, DEPRECATED)

**Summary:** "Query-file interface (dropbox edition)". `@Deprecated`; superseded by the
NAS `query_v3` / the V2 by-path endpoint.

**Auth:** Required (inferred).

**Request body — `FileQueryV2DTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `id` | String | No | — | File id |

**Response — `FileQueryV2VO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `entriesVO` | [EntriesVO](#entriesvo) | File metadata |

**Errors:** `E0308`/`E0321` (not found), `E0303` (deleted).

---

## POST `/api/file/2/files`  — Query file by path (dropbox)

**Summary:** "Query-file interface (by path)". Dropbox-style lookup by file name +
path.

**Auth:** Required (inferred).

**Request body — `FileQueryByPathV2DTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `equipmentNo` | String | No | — | Device number |
| `fileName` | String | No | — | File name |
| `path` | String | No | — | File path |

**Response — `FileQueryByPathV2VO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `equipmentNo` | String | Echoed device number |
| `entriesVO` | [EntriesVO](#entriesvo) | File metadata |

**Errors:** `E0308`/`E0321` (not found), `E0303` (deleted), `E0334` (empty path).

---

## POST `/api/file/label/list/search`  — Search files/folders by name

**Summary:** "Label-search interface: list of files or folders whose name contains the
term." Substring search within (and, per SQL, across the whole tree of) the user's
files.

**Auth:** Required (inferred).

**Request body — `FileLabelSearchDTO`:**

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `fileName` | String | Yes | `@NotBlank` "文件名不能为空" (file name cannot be empty) | Search term (matched `LIKE '%term%'`) |
| `directoryId` | Long | Yes | `@NotNull` "父目录id不能为空" (parent directory id cannot be empty) | Parent directory id (search scope) |

**Response — `FileLabelSearchVO`** (extends `BaseVO`):

| Field | Type | Description |
|-------|------|-------------|
| `userFileSearchVOList` | List<[UserFileSearchVO](#userfilesearchvo)> | Matching files/folders |

**Notes/inferences:** SQL `searchList` matches `file_name LIKE CONCAT('%',#{fileName},'%')`
with `user_id`, `is_active="Y"`, and `directory_id != 0` (root excluded); it also
resolves each result's `directory_name` via a correlated subquery. Ordering
`is_folder desc, ${order} ${sequence}`, paged with `limit #{pageNo},#{pageSize}`.
Despite the DTO carrying `directoryId`, the mapper searches the entire user tree —
`directoryId` is likely the caller's current-folder context rather than a hard scope
(**uncertain**, bodies stripped).

---

## Data models

### BaseVO (envelope)

| Field | Type | Description |
|-------|------|-------------|
| `success` | boolean | `true` on success (default); `false` on logical failure (still HTTP 200) |
| `errorCode` | String | `FileErrorCodeEnum` key (e.g. `E0304`) when `success=false` |
| `errorMsg` | String | Human-readable message |

`CommonVO<T>` adds `voT` (single payload); `CommonListVO<T>` adds `total` (long),
`size` (int), `pages` (int), `voList` (List<T>) for paged results. (No file-core
endpoint in this scope returns `CommonListVO` directly; paging is done in the mappers
for recycle/search and wrapped in bespoke VOs.)

### EntriesVO

The canonical file/folder representation returned by NAS/V2 query, list, move, copy,
download, and upload-finish endpoints. Field names are Dropbox-flavoured
(`content_hash`, `path_display`, `is_downloadable`) and **must be preserved verbatim**.

| Field | Type | Description |
|-------|------|-------------|
| `tag` | String | Folder-or-file flag (e.g. `"folder"` / `"file"`) |
| `id` | String | File/folder id |
| `name` | String | File name |
| `path_display` | String | Full display path (incl. file name) |
| `content_hash` | String | File MD5 |
| `is_downloadable` | boolean | Whether the item is downloadable (getter `is_downloadable()`, setter `set_downloadable`) |
| `size` | Long | Size in bytes |
| `lastUpdateTime` | Long | Last update time (epoch ms) |
| `parent_path` | String | Parent path (excludes the file name) |

### MetadataVO

Lighter metadata returned by create-folder and delete endpoints.

| Field | Type | Description |
|-------|------|-------------|
| `tag` | String | Folder-or-file flag |
| `id` | String | File id |
| `name` | String | File name |
| `path_display` | String | Full file path |

### UserFileVO

| Field | Type | Description |
|-------|------|-------------|
| `id` | String | File id |
| `directoryId` | String | Parent directory id |
| `fileName` | String | File name |
| `size` | Long | File size |
| `md5` | String | File MD5 |
| `isFolder` | String | `Y`=folder, `N`=file |
| `createTime` | Date | Created |
| `updateTime` | Date | Updated |

### FolderFileAddVO

Extends `BaseVO`; body fields identical to [UserFileVO](#userfilevo)
(`id`, `directoryId`, `fileName`, `size`, `md5`, `isFolder`, `createTime`, `updateTime`).

### UserFileSearchVO

Search-result row (adds `directoryName`, drops `createTime`).

| Field | Type | Description |
|-------|------|-------------|
| `id` | String | File id |
| `directoryId` | String | Parent directory id |
| `fileName` | String | File name |
| `directoryName` | String | Parent directory name (resolved via correlated subquery) |
| `size` | Long | File size |
| `md5` | String | File MD5 |
| `isFolder` | String | `Y`=folder, `N`=file |
| `updateTime` | Date | Updated |

### AllocationVO

| Field | Type | Description |
|-------|------|-------------|
| `tag` | String | Allocation type (e.g. "individual"/personal) |
| `allocated` | Long | Total allocated capacity (bytes) |

### PngPageVO

| Field | Type | Description |
|-------|------|-------------|
| `pageNo` | Integer | Page number |
| `url` | String | Download URL for that page's PNG |

### Domain objects (DB, not wire) — for behavioural reference

**UserFileDO → `f_user_file`:** `id`, `userId`, `directoryId`, `fileName`,
`innerName` (object key), `size`, `md5`, `isActive` (Y/N soft-delete), `isFolder`
(Y/N), `createTime`, `updateTime`, `terminalFileEditTime` (device edit epoch ms).

**FileActionDO → `f_file_action`** (change journal / sync delta): `id`, `userId`,
`fileId`, `fileName`, `newFileName`, `path`, `newPath`, `md5`, `innerName`,
`isFolder`, `size`, `action`, `createTime`, `updateTime`. `action` codes seen:
`R` = rename (`selectByParam` filters `action='R'`); other codes (create / move /
delete / upload) exist but are not enumerated in the stripped source
(**uncertain**). `selectByTime` (`update_time > cursor`, asc) is the incremental
delta feed.

**RecycleFileDO → `f_recycle_file`:** `fileId`, `userId`, `fileName`, `size`,
`isFolder`, `createTime`, `updateTime`. Recycle path constant `/recycle_bin/`.

**DeleteUserFileDO → `f_deleted_user_file`** (permanent-delete tombstone): `id`,
`fileId`, `userId`, `directoryId`, `fileName`, `innerName`, `size`, `fileServer`,
`md5`, `isFolder`, `deleteTime` (`sysdate()`).

**CapacityDO → `f_capacity`:** `id`, `userId`, `usedCapacity`, `totalCapacity`,
`createTime`, `updateTime`. `minusUsedCapacity` clamps at 0.

**SyncRecordDO → `f_sync_record`** (global telemetry): `id`, `successCount`,
`failCount`, `totalTime` (BigDecimal), `createTime`, `updateTime`.

**FileHisSyncDO → `f_file_his_sync`** (per-device last-sync snapshot): `id`,
`fileId`, `userId`, `equipmentNumber`, `directoryId`, `fileName`, `innerName`,
`size`, `md5`, `isFolder`, `createTime`, `syncTime`, `terminalFileEditTime`.

**FileServerChangeDO → `f_file_server_change`** (storage/region migration log): `id`,
`equipmentNumber`, `userId`, `oldFileServer`, `newFileServer`, `changeTime`,
`createTime`.

**FileConvertDO → `f_file_convert`** (note→PDF/PNG cache): `id`, `userId`,
`fileType` (`"2"` = PNG), `convertType`, `fileId`, `originInnerName`, `innerName`,
`pageNo`, `createTime`, `updateTime`.

### FileErrorCodeEnum (E-codes relevant to core file ops)

| Code | Message |
|------|---------|
| `E0078` | Sync in progress, please wait until it's completed before proceeding! |
| `E0109` | User does not exist! |
| `E0301` | Sync in progress, please wait until it's completed before proceeding! |
| `E0302` | The root directory has been deleted |
| `E0303` | The file or folder has been deleted |
| `E0304` | A file or folder with the same name already exists |
| `E0305` | The file or folder has been moved |
| `E0306` | The target directory has been deleted |
| `E0307` | Copying the files will exceed the total capacity! |
| `E0308` | File does not exist |
| `E0309` | Uploading the files will exceed the total capacity! |
| `E0310` | There are identical md5 files already. No need to upload! |
| `E0311` | The files in the recycle bin have been restored or permanently deleted! |
| `E0312` | The device isn't linked to an account! |
| `E0313` | Cannot be operated from the root directory! |
| `E0314` | The QT program failed to parse the file! |
| `E0315` | Incorrect file type |
| `E0316` | This file is being converted |
| `E0317` | The folder or the file directory you want to delete does not exist |
| `E0318` | The folder or file you want to delete does not exist |
| `E0319` | The folder or file directory you want to move or rename does not exist |
| `E0320` | The folder or file you want to move or rename does not exist |
| `E0321` | This file does not exist |
| `E0322` | A file with the same name already exists |
| `E0323` | Unable to migrate from a server outside of China to a server within China |
| `E0324` | This file cannot be uploaded |
| `E0325` | The byte length of the file name is too long |
| `E0326` | The server has reached the migration limit |
| `E0327` | Server migration failed |
| `E0330` | NextSyncToken timeout |
| `E0332` | Unable to sync. Please upgrade to the latest version. |
| `E0333` | Not enough Supernote Cloud storage. Please try deleting files from the recycle bin. |
| `E0334` | The path cannot be empty. |
| `E0341` | An exception occurred while deleting the file |
| `E0342` | Data has expired, please refresh the page or re-sync and try again. |
| `E0343` | Rename failed |
| `E0344` | Move failed |
| `E0345` | Copy failed |
| `E0346` | Unknown error |
| `E0347` | The file or folder already exists in the target directory on the private storage disk |
| `E0348` | The file access is denied on the private storage disk |
| `E0349` | The file IO error on the private storage disk |
| `E0350` | The file is too large on the private storage disk |
| `E0351` | The directory access is denied on the private storage disk |
| `E0352` | The directory IO error on the private storage disk |
| `E0353` | The permission is denied on the private storage disk |
| `E0354` | The invalid path on the private storage disk |
| `E0355` | The chunk upload failed on the private storage disk |
| `E0356` | The chunk merge failed on the private storage disk |
| `E0357` | The chunk verify failed on the private storage disk |
| `E0358` | Cannot move a folder into itself or its subdirectory |

(`E0328`/`E0329`/`E0331`/`E0335`–`E0340` concern schedule tasks and summaries —
out of scope for core file ops. The `E03xx` private-storage-disk codes `E0347`–`E0357`
are surfaced by the NAS/local upload/move/copy/delete endpoints when the underlying
filesystem I/O fails.)
