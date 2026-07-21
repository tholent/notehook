# File Upload, Sharing & Object Storage API

This document specifies the file-upload (web/app + terminal/device), web
cloud-disk browsing, sharing, and local object-storage (OSS) endpoints of the
Supernote cloud-sync server (Spring Boot, decompiled). It covers five
controllers, all mounted on the base paths `/api/file` and `/api/oss`:

- `F_FileUploadController` (`/api/file`) — legacy FTP multipart upload endpoint.
- `F_TerminalFileUploadController` (`/api/file`) — device (terminal) upload apply/finish.
- `F_FileLocalWebController` (`/api/file`) — ~18 web cloud-disk operations (list, folder ops, delete, move/copy, rename, search, recycle bin, download URL, upload apply/finish).
- `F_ShareController` (`/api/file`) — create a share record.
- `O_OssLocalController` (`/api/oss`) — local object storage with signed upload/download URLs and chunked upload.

> **ClassFinal note:** the JAR is ClassFinal-protected, so all method bodies are
> stripped (`return null;` / "Couldn't be decompiled"). Everything below is
> derived from controller annotations, DTO/VO field declarations + Bean
> Validation constraints, MyBatis mapper SQL, the `SignVerifier` utility, the
> `LocalFileUtil` method signatures, and the error-code enums. Behavioural
> statements are inferences and are flagged as such.

## Envelope & conventions

- All JSON responses (except the raw download stream and the internal
  `FileDownloadApplyVO`) extend `BaseVO { boolean success; String errorCode;
  String errorMsg; }`. Logical failures return **HTTP 200** with
  `success:false` and an `errorCode` from `FileErrorCodeEnum` /
  `OssErrorCodeEnum`.
- Auth headers (per the server-wide contract): **`x-access-token`** (JWT) and
  **`equipmentNo`**. Handlers here take `HttpServletRequest` and resolve the
  user via `JwtTokenUserUtil`, so token auth is **Required (inferred)** unless
  noted. Invalid token → HTTP 401.
- `equipment` codes: 1=web, 2=APP, 3=terminal, 4=user platform.
- Multipart limits: max file size 1024 MB, max request size 1024 MB. Server
  port **19071**.
- Protocol strings preserved verbatim: `bucketName="supernote"`,
  `x-access-token`, and all paths below.
- **File-upload exceptions** (`FileUploadException` from the multipart
  endpoints) surface as HTTP 500 with `errorCode:"FILE_UPLOAD_FAILED"`.
  **Download exceptions** surface as HTTP 500 plain text.

## Upload / MD5-dedup flow (inferred)

The web/app upload is a two-phase, MD5-deduplicated flow:

1. **Apply** — client posts `directoryId`, `size`, `fileName`, `md5` to
   `POST /api/file/upload/apply`. The server returns a `FileUploadApplyLocalVO`
   containing an `innerName` (server-generated storage key) plus a signed upload
   target (`fullUploadUrl` for whole-file `PUT`, `partUploadUrl` for chunked
   upload) with `bucketName="supernote"` and `authorization`/`xAmzDate`
   signature fields. If a file with the same `md5` already exists the server can
   short-circuit dedup (`E0310` "identical md5 files already… No need to
   upload!").
2. **Transfer** — client uploads the bytes to the returned URL. For the local
   OSS implementation this is `POST /api/oss/upload` (whole file) or repeated
   `POST /api/oss/upload/part` (chunks), each carrying an HMAC signature.
3. **Finish** — client posts `POST /api/file/upload/finish` with the
   `innerName`, final `fileName`, `directoryId`, `fileSize`, `md5` to commit the
   file record into the user's cloud disk.

Terminal (device) uploads follow the same apply → finish shape via
`/api/file/terminal/upload/apply` and `/api/file/terminal/upload/finish`, but
carry `equipmentNo`, string-typed sizes, an explicit `filePath`, and device
`modifyTime`/`uploadTime` timestamps.

## Local OSS signed-URL flow (inferred)

`O_OssLocalController` is a self-hosted S3-substitute. `SignVerifier` (HMAC of
`path + timestamp + nonce [+ fileSize] + secret`, SHA-256 hex) protects each
transfer:

- `POST /api/oss/generate/upload/url` mints a signed upload URL
  (`FileUploadApplyLocalVO`) valid **30 minutes** (`validate()` rejects
  `now - timestamp > 1_800_000 ms`).
- `POST /api/oss/upload` / `POST /api/oss/upload/part` accept the multipart file
  plus `signature`, `timestamp`, `nonce`, encrypted `path`. Signature mismatch →
  `E1306`.
- `POST /api/oss/generate/download/url` mints a signed download URL
  (`FileDownloadApplyVO`) valid **24 hours** (`validateDownload()` rejects
  `now - timestamp > 86_400_000 ms`).
- `GET /api/oss/download` streams the file (supports HTTP `Range` → 206 partial
  content via `LocalFileUtil.downloadFileRange`).

Chunked upload merges via `LocalFileUtil.mergeLocalFiles` with per-chunk status
tracking and a post-merge `verifyFileIntegrity`; chunk-stage failures map to
`E0355` (upload), `E0356` (merge), `E0357` (verify).

## Sharing flow (inferred)

`POST /api/file/share/record/add` records a share intent
(`f_share_record`: `user_id`, `file_id`, `share_way`, `create_time`) where
`shareWay` selects the exported format (0 = PDF, 1 = PNG). The device-side share
pipeline additionally persists rendered artifacts in `f_terminal_share_file`,
`f_composite_image`, `f_file_convert`, and `f_terminal_file_convert` (converted
PDF/PNG pages keyed by `share_id`), per the mapper SQL. The add endpoint returns
a bare `BaseVO`; the generated share URL/token is produced by the conversion
pipeline, not returned inline here.

## Endpoint summary

| Method | Path | Auth | Summary |
|---|---|---|---|
| PUT  | `/api/file/upload/ftp/{innerName}/deleteApi` | Not required (inferred) | Legacy FTP multipart file upload |
| POST | `/api/file/terminal/upload/apply` | Required (inferred) | Terminal (device) upload apply |
| POST | `/api/file/terminal/upload/finish` | Required (inferred) | Terminal (device) upload finish/commit |
| POST | `/api/file/list/query` | Required (inferred) | List files + folders in a directory (paged) |
| POST | `/api/file/folder/list/query` | Required (inferred) | List folders (for move/copy picker) |
| POST | `/api/file/capacity/query` | Required (inferred) | Query cloud-disk capacity |
| POST | `/api/file/delete` | Required (inferred) | Delete files/folders (to recycle bin) |
| POST | `/api/file/folder/add` | Required (inferred) | Create folder |
| POST | `/api/file/move` | Required (inferred) | Move files/folders |
| POST | `/api/file/copy` | Required (inferred) | Copy files/folders |
| POST | `/api/file/rename` | Required (inferred) | Rename file/folder |
| POST | `/api/file/list/search` | Required (inferred) | Search files/folders by name (paged) |
| POST | `/api/file/recycle/list/query` | Required (inferred) | List recycle-bin contents (paged) |
| POST | `/api/file/recycle/clear` | Required (inferred) | Empty recycle bin |
| POST | `/api/file/recycle/delete` | Required (inferred) | Permanently delete recycle-bin items |
| POST | `/api/file/recycle/revert` | Required (inferred) | Restore recycle-bin items |
| POST | `/api/file/download/url` | Required (inferred) | Get a file download URL |
| POST | `/api/file/path/query` | Required (inferred) | Query a file's directory path |
| POST | `/api/file/upload/apply` | Required (inferred) | Web/app upload apply (phase 1) |
| POST | `/api/file/upload/finish` | Required (inferred) | Web/app upload finish/commit (phase 3) |
| POST | `/api/file/share/record/add` | Required (inferred) | Create a share record |
| POST | `/api/oss/generate/upload/url` | Not required (inferred) | Mint signed upload URL |
| POST | `/api/oss/upload` | Signature (not JWT) | Upload whole file via signed URL |
| POST | `/api/oss/upload/part` | Signature (not JWT) | Upload one chunk via signed URL |
| POST | `/api/oss/generate/download/url` | Not required (inferred) | Mint signed download URL |
| GET  | `/api/oss/download` | Signature (not JWT) | Stream file via signed URL (Range-capable) |

---

## PUT `/api/file/upload/ftp/{innerName}/deleteApi`

**Summary (zh→en):** "Upload file." Legacy FTP-backed multipart upload endpoint
(`F_FileUploadController`).

- **Auth:** Not required (inferred) — no `JwtTokenUserUtil` field on this
  controller; the handler takes only `MultipartFile` + `HttpServletRequest`.
- **Content-Type:** `multipart/form-data`.
- **Path params:** `innerName` (String) — server-side internal storage name/key
  of the target object.
- **Form/file params:** `file` (`MultipartFile`) — the file bytes.
- **Response:** `BaseVO` (envelope only).
- **Errors:** `FILE_UPLOAD_FAILED` (HTTP 500) on multipart failure; OSS codes
  `E1305` (upload failed) possible from the storage layer.
- **Notes:** The `.../deleteApi` suffix and `PUT` verb are protocol-literal and
  must not be renamed. Path segment `/upload/ftp/` matches the configured FTP
  upload path. Method body stripped; exact semantics (write vs delete-then-write)
  cannot be confirmed.

---

## POST `/api/file/terminal/upload/apply`

**Summary (zh→en):** "Terminal file upload apply interface." Phase 1 of a device
upload; reserves storage and returns a signed upload target.

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `TerminalFileUploadApplyDTO` (see Data models).
- **Response:** `FileUploadApplyLocalVO` (see Data models) — `innerName` +
  signed `fullUploadUrl`/`partUploadUrl`, `bucketName`, `authorization`,
  `xAmzDate`.
- **Errors:** `E0309`/`E0333` (capacity exceeded), `E0310` (md5 dedup — already
  present), `E0312` (device not linked to an account), `E0324`/`E0325` (file
  cannot be uploaded / name too long), `E1302` (URL construction error),
  `E9999`.
- **Notes:** `fileSize` is a **String** here (device sends string-typed sizes);
  be lenient. `filePath` and `equipmentNo` are optional.

---

## POST `/api/file/terminal/upload/finish`

**Summary (zh→en):** "Terminal file upload finish interface." Commits an
uploaded device file into the cloud disk.

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `TerminalFileUploadFinishDTO` (see Data models).
- **Response:** `BaseVO`.
- **Errors:** `E0308`/`E0321` (file does not exist), `E0304`/`E0322` (duplicate
  name), `E0309`/`E0333` (capacity), `E0314`/`E0315` (parse/type), `E9999`.
- **Notes:** Carries device `modifyTime` + `uploadTime` (both required strings)
  in addition to the apply fields, so the committed record preserves the
  device's original timestamps.

---

## POST `/api/file/list/query`

**Summary (zh→en):** "Query the list of files and folders under a directory."

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `FileListQueryDTO` — `directoryId`, `order`, `sequence`,
  `pageNo`, `pageSize`.
- **Response:** `FileListQueryVO` — `total` + `userFileVOList` (`List<UserFileVO>`).
- **Errors:** `E0302` (root directory deleted), `E0303` (file/folder deleted),
  `E0342` (data expired — refresh/re-sync).
- **Notes:** `order` ∈ {`filename`,`time`,`size`}, `sequence` ∈ {`asc`,`desc`}.

---

## POST `/api/file/folder/list/query`

**Summary (zh→en):** "Query folder list." Used to render a destination-folder
tree (e.g. for move/copy), excluding the folders being operated on.

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `FolderListQueryDTO` — `directoryId`, `idList` (folders to
  exclude / expand under).
- **Response:** `FolderListQueryVO` — `folderVOList` (`List<FolderVO>`; each has
  `empty` = Y/N).
- **Errors:** `E0302`, `E0317`/`E0318`.

---

## POST `/api/file/capacity/query`

**Summary (zh→en):** "Query cloud-disk capacity."

- **Auth:** Required (inferred).
- **Content-Type:** none (empty body; only `HttpServletRequest`).
- **Request body:** none.
- **Response:** `CapacityVO` — `usedCapacity`, `totalCapacity` (bytes).
- **Errors:** `E0109` (user does not exist).

---

## POST `/api/file/delete`

**Summary (zh→en):** "Delete file or folder." Soft delete → recycle bin.

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `FileDeleteDTO` — `equipmentNo`, `idList` (required, non-empty),
  `directoryId` (required).
- **Response:** `BaseVO`.
- **Errors:** `E0303`, `E0313` (cannot operate on root), `E0317`/`E0318` (target
  missing), `E0341` (exception while deleting).
- **Notes:** Validation messages: idList "File ID list cannot be empty.";
  directoryId "The ID of the file root directory cannot be empty."

---

## POST `/api/file/folder/add`

**Summary (zh→en):** "Create a new folder."

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `FolderAddDTO` — `fileName` (required), `directoryId` (required).
- **Response:** `BaseVO`.
- **Errors:** `E0304`/`E0322` (duplicate name), `E0302` (parent deleted),
  `E0325` (name too long).

---

## POST `/api/file/move`

**Summary (zh→en):** "Move file or folder."

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `FileMoveAndCopyDTO` — `idList`, `directoryId` (source
  parent), `goDirectoryId` (target parent).
- **Response:** `BaseVO`.
- **Errors:** `E0305` (already moved), `E0306` (target dir deleted), `E0319`/`E0320`
  (target missing), `E0344` (move failed), `E0358` (cannot move a folder into
  itself/subdirectory).

---

## POST `/api/file/copy`

**Summary (zh→en):** "Copy file or folder."

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `FileMoveAndCopyDTO` — same shape as move.
- **Response:** `BaseVO`.
- **Errors:** `E0306` (target dir deleted), `E0307` (copy would exceed total
  capacity), `E0345` (copy failed).

---

## POST `/api/file/rename`

**Summary (zh→en):** "Rename file or folder."

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `FileReNameDTO` — `id` (required), `newName` (required).
- **Response:** `BaseVO`.
- **Errors:** `E0304`/`E0322` (duplicate name), `E0319`/`E0320` (target missing),
  `E0343` (rename failed), `E0325` (name too long).

---

## POST `/api/file/list/search`

**Summary (zh→en):** "Search the list of files or folders containing a name."

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `FileListSearchDTO` — `fileName`, `order`, `sequence`,
  `pageNo`, `pageSize`.
- **Response:** `FileListSearchVO` — `total` + `userFileSearchVOList`
  (`List<UserFileSearchVO>`; each includes `directoryName` to show location).
- **Errors:** `E0342`.

---

## POST `/api/file/recycle/list/query`

**Summary (zh→en):** "Query recycle-bin file list."

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `RecycleFileListDTO` — `order`, `sequence`, `pageNo`, `pageSize`.
- **Response:** `RecycleFileListVO` — `total` + `recycleFileVOList`
  (`List<RecycleFileVO>`).
- **Errors:** `E0311` (items already restored/permanently deleted).

---

## POST `/api/file/recycle/clear`

**Summary (zh→en):** "Empty the recycle bin."

- **Auth:** Required (inferred).
- **Content-Type:** none (empty body).
- **Request body:** none.
- **Response:** `BaseVO`.
- **Errors:** `E0311`.

---

## POST `/api/file/recycle/delete`

**Summary (zh→en):** "Permanently delete recycle-bin items."

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `RecycleFileDTO` — `idList` (required, non-empty).
- **Response:** `BaseVO`.
- **Errors:** `E0311`, `E0341`.

---

## POST `/api/file/recycle/revert`

**Summary (zh→en):** "Restore items from the recycle bin."

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `RecycleFileDTO` — `idList` (required, non-empty).
- **Response:** `BaseVO`.
- **Errors:** `E0311` (already restored/deleted), `E0304`/`E0322` (name clash on
  restore target), `E0306` (original parent gone).

---

## POST `/api/file/download/url`

**Summary (zh→en):** "Get a file download URL."

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `FileDownloadDTO` — `id` (required), `type` (required;
  `0`=download, `1`=share).
- **Response:** `FileDownloadUrlVO` — `url` (download URL), `md5`.
- **Errors:** `E0308`/`E0321` (file does not exist), `E1302` (URL construction
  error), `E1307` (download failed).
- **Notes:** For the local OSS backend the returned `url` is a signed
  `/api/oss/download?...` link (see `generate/download/url`).

---

## POST `/api/file/path/query`

**Summary (zh→en):** "Query the directory a file belongs to."

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `FilePathQueryDTO` — `id` (required).
- **Response:** `FilePathQueryVO` — `path` (human-readable path),
  `idPath` (slash-joined ancestor id chain).
- **Errors:** `E0321` (file does not exist), `E0334` (path cannot be empty).

---

## POST `/api/file/upload/apply`

**Summary (zh→en):** "Upload file apply interface." Phase 1 of the web/app
two-phase upload. Guarded by `@ResubmitCheck` on `fileName`+`directoryId` to
dedup rapid resubmits.

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `FileUploadApplyDTO` — `directoryId`, `size`, `fileName`, `md5`.
- **Response:** `FileUploadApplyLocalVO` — `innerName` + signed
  `fullUploadUrl`/`partUploadUrl`, `bucketName` (`"supernote"`),
  `authorization`, `xAmzDate`, `equipmentNo`.
- **Errors:** `E0309`/`E0333` (capacity), `E0310` (md5 dedup — no upload
  needed), `E0324`/`E0325` (cannot upload / name too long), `E1302` (URL
  construction), `E9999`.
- **Notes:** If `md5` already exists server-side, the response signals dedup
  (`E0310`) so the client can skip straight to `finish`.

---

## POST `/api/file/upload/finish`

**Summary (zh→en):** "Upload file finish interface." Phase 3 — commits the
uploaded object into the user's cloud disk. Guarded by `@ResubmitCheck`.

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `FileUploadFinishDTO` — `type` (`1`=app,`2`=cloud disk),
  `directoryId`, `fileSize`, `fileName`, `md5`, `innerName`.
- **Response:** `BaseVO`.
- **Errors:** `E0308`/`E0321` (uploaded object missing), `E0304`/`E0322`
  (duplicate name), `E0309`/`E0333` (capacity), `E0310` (md5 already present),
  `E0324` (cannot be uploaded).

---

## POST `/api/file/share/record/add`

**Summary (zh→en):** "Add share interface." Records a user's intent to share a
file in a given export format.

- **Auth:** Required (inferred).
- **Content-Type:** `application/json`.
- **Request body:** `ShareRecordDTO` — `fileId` (required), `shareWay`
  (required; `0`=PDF, `1`=PNG).
- **Response:** `BaseVO`.
- **Errors:** `E0308`/`E0321` (file missing), `E0315` (incorrect file type),
  `E0316` (file is being converted).
- **Notes:** Inserts into `f_share_record (user_id, file_id, share_way,
  create_time)` (see `F-ShareRecordMapper.xml`). The device rendering pipeline
  writes converted artifacts to `f_composite_image`, `f_file_convert`,
  `f_terminal_file_convert` (PDF/PNG pages keyed by `share_id`) and
  `f_terminal_share_file`. The final share link is produced downstream and is
  not part of this response body.

---

## POST `/api/oss/generate/upload/url`

**Summary (zh→en):** "1. Generate a signed upload URL."

- **Auth:** Not required (inferred) — no token required to mint the URL; the URL
  itself is HMAC-signed. `JwtTokenUserUtil` is present but the params are plain
  query params.
- **Content-Type:** `application/x-www-form-urlencoded` (query/form params).
- **Query/form params:**
  | Name | Type | Required | Meaning |
  |---|---|---|---|
  | `filePath` | String | yes | Logical storage path/key |
  | `fileName` | String | yes | File name |
  | `fileSize` | Long | no | File size in bytes (folded into the signature when present) |
  | `ip` | String | no | Client IP (unannotated param) |
- **Response:** `FileUploadApplyLocalVO` — signed `fullUploadUrl` +
  `partUploadUrl`, `authorization`, `xAmzDate`, `innerName`, `bucketName`.
- **Errors:** `E1302` (URL construction error).
- **Notes:** Signature = `SHA-256hex(path + timestamp + nonce + [fileSize] +
  secret)`; valid **30 min** (`SignVerifier.validate`).

---

## POST `/api/oss/upload`

**Summary (zh→en):** "2. Upload a file via the dynamic URL." Whole-file upload
to local OSS.

- **Auth:** Signature-based (not JWT). Signature mismatch → `E1306`.
- **Content-Type:** `multipart/form-data`.
- **Form/file params:**
  | Name | Type | Required | Meaning |
  |---|---|---|---|
  | `file` | `MultipartFile` | yes | File bytes |
  | `signature` | String | yes | HMAC signature from the signed URL |
  | `timestamp` | Long | yes | Signed timestamp (ms) |
  | `nonce` | String | yes | Signed nonce |
  | `path` | String | yes | Encrypted target path (`encryptedPath`) |
- **Response:** `UploadFileVO` — `innerName`, `md5` (server-computed).
- **Errors:** `E1304` (file empty), `E1305` (upload failed), `E1306` (signature
  failed); throws `FileUploadException` → HTTP 500 `FILE_UPLOAD_FAILED`.
- **Notes:** `path` is decrypted server-side (`decryptPath`) then validated by
  `isValidPath` (path-traversal guard). Backed by `LocalFileUtil.uploadFile`.

---

## POST `/api/oss/upload/part`

**Summary (zh→en):** "Chunked upload." Uploads a single chunk of a large file.

- **Auth:** Signature-based (not JWT). Signature mismatch → `E1306`.
- **Content-Type:** `multipart/form-data`.
- **Form/file params:**
  | Name | Type | Required | Meaning |
  |---|---|---|---|
  | `file` | `MultipartFile` | yes | Chunk bytes |
  | `signature` | String | yes | HMAC signature |
  | `timestamp` | Long | yes | Signed timestamp (ms) |
  | `nonce` | String | yes | Signed nonce |
  | `path` | String | yes | Encrypted target path |
  | `partNumber` | int | yes | 1-based chunk index |
  | `totalChunks` | int | yes | Total number of chunks |
  | `uploadId` | String | yes | Upload session id |
- **Response:** `FileChunkVO` — `uploadId`, `partNumber`, `totalChunks`,
  `chunkMd5`, `status` (e.g. `"SUCCESS"`).
- **Errors:** `E0355` (chunk upload failed), `E0356` (chunk merge failed),
  `E0357` (chunk verify failed), `E1306` (signature), `E1304` (empty); throws
  `FileUploadException` → HTTP 500 `FILE_UPLOAD_FAILED`.
- **Notes:** Chunks written under `local.part.upload.targetDirPath`; per-chunk
  status tracked (`isChunkUploaded`/`markChunkAsUploaded`), merged by
  `LocalFileUtil.mergeLocalFiles`, then integrity-checked
  (`verifyFileIntegrity`). Merge/verify presumably triggered on the final chunk.

---

## POST `/api/oss/generate/download/url`

**Summary (zh→en):** "Generate a signed download URL."

- **Auth:** Not required (inferred) — the minted URL is HMAC-signed.
- **Content-Type:** `application/x-www-form-urlencoded` (query/form params).
- **Query/form params:**
  | Name | Type | Required | Meaning |
  |---|---|---|---|
  | `filePath` | String | yes | Logical storage path/key |
  | `fileName` | String | yes | File name |
  | `pathId` | String | yes | Path/object identifier echoed on download |
  | `ip` | String | no | Client IP (unannotated param) |
- **Response:** `FileDownloadApplyVO` — **plain POJO (not a `BaseVO`)**:
  `url`, `signature`, `timestamp`, `nonce`, `pathId`.
- **Errors:** `E1303` (file does not exist), `E1302` (URL construction).
- **Notes:** Download signature = `SHA-256hex(path + timestamp + nonce +
  secret)` (no fileSize); valid **24 h** (`SignVerifier.validateDownload`).

---

## GET `/api/oss/download`  — RAW STREAM / NON-ENVELOPE

**Summary (zh→en):** "Download a file via the dynamic URL."

- **Auth:** Signature-based (not JWT).
- **Content-Type (response):** `application/octet-stream` (or the file's type);
  **not** a JSON envelope.
- **Query params:**
  | Name | Type | Required | Meaning |
  |---|---|---|---|
  | `path` | String | yes | Encrypted file path |
  | `signature` | String | yes | HMAC download signature |
  | `timestamp` | Long | yes | Signed timestamp (ms) |
  | `nonce` | String | yes | Signed nonce |
  | `pathId` | String | yes | Path/object identifier |
- **Response:** `ResponseEntity<Resource>` — streams the file bytes.
  **Supports HTTP `Range`** requests (→ `206 Partial Content` via
  `LocalFileUtil.downloadFileRange`); full body otherwise (`200`). Sets
  `Content-Disposition`/`Content-Length` headers (inferred).
- **Errors:** signature/expiry failure → `E1306` / rejected; `E1303` (file does
  not exist); `E1307` (download failed). **Download exceptions surface as HTTP
  500 plain text**, not the envelope.
- **Notes:** Method body "Couldn't be decompiled". This is a streaming/download
  endpoint — clients must not JSON-parse the response. `isValidPath` guards
  against traversal after `decryptPath`.

---

## Data models

### Envelope base — `BaseVO`
| Field | Type | Meaning |
|---|---|---|
| `success` | boolean | true on success |
| `errorCode` | String | error code (from an enum) when `success:false` |
| `errorMsg` | String | human-readable error message |

### `TerminalFileUploadApplyDTO`
| Field | Type | Required | Validation msg | Meaning (zh→en) |
|---|---|---|---|---|
| `equipmentNo` | String | no | — | Device number |
| `filePath` | String | no | — | File path |
| `fileSize` | String | yes | "文件大小 不能为空" (size cannot be empty) | File size (string-typed) |
| `fileName` | String | yes | "文件名 不能为空" (name cannot be empty) | File name |
| `md5` | String | yes | "文件Md5不能为空" (md5 cannot be empty) | File MD5 |

### `TerminalFileUploadFinishDTO`
| Field | Type | Required | Meaning (zh→en) |
|---|---|---|---|
| `equipmentNo` | String | no | Device number |
| `filePath` | String | no | File path |
| `fileSize` | String | yes | File size (string-typed) |
| `fileName` | String | yes | File name |
| `md5` | String | yes | File MD5 |
| `innerName` | String | yes | Internal storage name/key |
| `modifyTime` | String | yes | File modification time (device) |
| `uploadTime` | String | yes | File upload time (device) |

### `FileListQueryDTO`
| Field | Type | Required | Meaning (zh→en) |
|---|---|---|---|
| `directoryId` | Long | yes | Parent directory id |
| `order` | String | yes | Sort key: `filename`/`time`/`size` |
| `sequence` | String | yes | Direction: `asc`/`desc` |
| `pageNo` | Integer | yes | Page number |
| `pageSize` | Integer | yes | Page size |

### `FolderListQueryDTO`
| Field | Type | Required | Meaning |
|---|---|---|---|
| `directoryId` | Long | yes | Parent directory id |
| `idList` | List&lt;Long&gt; | yes (non-empty) | Folder ids (to expand/exclude) |

### `FileDeleteDTO`
| Field | Type | Required | Validation msg | Meaning |
|---|---|---|---|---|
| `equipmentNo` | String | no | — | Device number |
| `idList` | List&lt;Long&gt; | yes (non-empty) | "File ID list cannot be empty." | Ids to delete |
| `directoryId` | Long | yes | "The ID of the file root directory cannot be empty." | Parent directory id |

### `FolderAddDTO`
| Field | Type | Required | Meaning |
|---|---|---|---|
| `fileName` | String | yes | Folder name |
| `directoryId` | Long | yes | Parent directory id |

### `FileMoveAndCopyDTO` (used by `/move` and `/copy`)
| Field | Type | Required | Meaning |
|---|---|---|---|
| `idList` | List&lt;Long&gt; | yes (non-empty) | Ids to move/copy |
| `directoryId` | Long | yes | Source parent directory id |
| `goDirectoryId` | Long | yes | Target parent directory id |

### `FileReNameDTO`
| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | Long | yes | File/folder id |
| `newName` | String | yes | New name |

### `FileListSearchDTO`
| Field | Type | Required | Meaning |
|---|---|---|---|
| `fileName` | String | yes | Name substring to search |
| `order` | String | yes | Sort key: `filename`/`time`/`size` |
| `sequence` | String | yes | Direction: `asc`/`desc` |
| `pageNo` | Integer | yes | Page number |
| `pageSize` | Integer | yes | Page size |

### `RecycleFileListDTO`
| Field | Type | Required | Meaning |
|---|---|---|---|
| `order` | String | yes | Sort key |
| `sequence` | String | yes | Direction |
| `pageNo` | Integer | yes | Page number |
| `pageSize` | Integer | yes | Page size |

### `RecycleFileDTO` (used by `/recycle/delete` and `/recycle/revert`)
| Field | Type | Required | Meaning |
|---|---|---|---|
| `idList` | List&lt;Long&gt; | yes (non-empty) | Recycle-bin item ids |

### `FileDownloadDTO`
| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | Long | yes | File id |
| `type` | String | yes | Purpose: `0`=download, `1`=share |

### `FilePathQueryDTO`
| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | Long | yes | File id |

### `FileUploadApplyDTO`
| Field | Type | Required | Meaning |
|---|---|---|---|
| `directoryId` | Long | yes | Parent directory id |
| `size` | Long | yes | File size (bytes) |
| `fileName` | String | yes | File name |
| `md5` | String | yes | File MD5 (dedup key) |

### `FileUploadFinishDTO`
| Field | Type | Required | Meaning |
|---|---|---|---|
| `type` | String | (required per doc, no constraint) | Source: `1`=app, `2`=cloud disk |
| `directoryId` | Long | yes | Parent directory id |
| `fileSize` | Long | yes | File size (bytes) |
| `fileName` | String | yes | File name |
| `md5` | String | yes | File MD5 |
| `innerName` | String | yes | Internal storage name/key from apply |

### `ShareRecordDTO`
| Field | Type | Required | Meaning |
|---|---|---|---|
| `fileId` | String | yes | Id of the file to share |
| `shareWay` | String | yes | Export format: `0`=PDF, `1`=PNG |

### `OssLocalDTO` (local-OSS helper DTO; not directly bound by these endpoints)
| Field | Type | Required | Meaning |
|---|---|---|---|
| `fileName` | String | yes | File name |
| `innerName` | String | yes | Internal storage name/key |

### `OssDTO` (generic OSS object reference; not bound by these endpoints)
| Field | Type | Required | Meaning |
|---|---|---|---|
| `bucketName` | String | yes | Bucket name (e.g. `supernote`) |
| `key` | String | yes | Unique object key |

---

### `FileUploadApplyLocalVO` (extends `BaseVO`)
Returned by apply endpoints and `generate/upload/url`.
| Field | Type | Meaning |
|---|---|---|
| `equipmentNo` | String | Device number |
| `bucketName` | String | Bucket name (`"supernote"`) |
| `innerName` | String | Server-generated internal storage key |
| `xAmzDate` | String | Signature timestamp (AWS-style) |
| `authorization` | String | Signature / Authorization header value |
| `fullUploadUrl` | String | Signed whole-file upload URL |
| `partUploadUrl` | String | Signed chunked-upload URL |

### `UploadFileVO` (extends `BaseVO`)
| Field | Type | Meaning |
|---|---|---|
| `innerName` | String | Stored internal name/key |
| `md5` | String | Server-computed MD5 of the stored file |

### `FileChunkVO` (extends `BaseVO`)
| Field | Type | Meaning |
|---|---|---|
| `uploadId` | String | Chunk upload session id |
| `partNumber` | int | Current chunk index |
| `totalChunks` | int | Total chunk count |
| `chunkMd5` | String | MD5 of this chunk |
| `status` | String | Chunk status (e.g. `SUCCESS`) |

### `FileDownloadApplyVO` (plain POJO — NOT a `BaseVO`)
Returned by `generate/download/url`. No envelope fields.
| Field | Type | Meaning |
|---|---|---|
| `url` | String | Signed download URL |
| `signature` | String | HMAC download signature |
| `timestamp` | Long | Signature timestamp (ms) |
| `nonce` | String | Signature nonce |
| `pathId` | String | Path/object identifier |

### `CapacityVO` (extends `BaseVO`)
| Field | Type | Meaning |
|---|---|---|
| `usedCapacity` | Long | Used bytes |
| `totalCapacity` | Long | Total bytes |

### `FileDownloadUrlVO` (extends `BaseVO`)
| Field | Type | Meaning |
|---|---|---|
| `url` | String | Download URL |
| `md5` | String | File MD5 |

### `FilePathQueryVO` (extends `BaseVO`)
| Field | Type | Meaning |
|---|---|---|
| `path` | String | Human-readable folder path |
| `idPath` | String | Ancestor id chain |

### `FileListQueryVO` (extends `BaseVO`)
| Field | Type | Meaning |
|---|---|---|
| `total` | Long | Total item count |
| `userFileVOList` | List&lt;`UserFileVO`&gt; | Files + folders on this page |

### `UserFileVO`
| Field | Type | Meaning |
|---|---|---|
| `id` | String | File id |
| `directoryId` | String | Parent directory id |
| `fileName` | String | File name |
| `size` | Long | File size (bytes) |
| `md5` | String | File MD5 |
| `isFolder` | String | `Y`=folder, `N`=file |
| `createTime` | Date | Created |
| `updateTime` | Date | Updated |

### `FolderListQueryVO` (extends `BaseVO`)
| Field | Type | Meaning |
|---|---|---|
| `folderVOList` | List&lt;`FolderVO`&gt; | Folders |

### `FolderVO`
| Field | Type | Meaning |
|---|---|---|
| `id` | String | Folder id |
| `directoryId` | String | Parent directory id |
| `fileName` | String | Folder name |
| `empty` | String | `Y`=empty, `N`=not empty |

### `FileListSearchVO` (extends `BaseVO`)
| Field | Type | Meaning |
|---|---|---|
| `total` | Long | Total match count |
| `userFileSearchVOList` | List&lt;`UserFileSearchVO`&gt; | Matches on this page |

### `UserFileSearchVO`
| Field | Type | Meaning |
|---|---|---|
| `id` | String | File id |
| `directoryId` | String | Parent directory id |
| `fileName` | String | File name |
| `directoryName` | String | Parent directory name (for display) |
| `size` | Long | File size |
| `md5` | String | File MD5 |
| `isFolder` | String | `Y`=folder, `N`=file |
| `updateTime` | Date | Updated |

### `RecycleFileListVO` (extends `BaseVO`)
| Field | Type | Meaning |
|---|---|---|
| `total` | Long | Total item count |
| `recycleFileVOList` | List&lt;`RecycleFileVO`&gt; | Recycle-bin items |

### `RecycleFileVO`
| Field | Type | Meaning |
|---|---|---|
| `fileId` | String | File id |
| `isFolder` | String | Folder flag (`Y`/`N`) |
| `fileName` | String | File name |
| `size` | Long | File size |
| `updateTime` | Date | Last updated (deletion time) |

---

### Enum — `OssErrorCodeEnum`
| Code | Message |
|---|---|
| `E1301` | Delete file failed. |
| `E1302` | URL construction error. |
| `E1303` | File does not exist. |
| `E1304` | File is empty. |
| `E1305` | File upload failed. |
| `E1306` | Signature verification failed. |
| `E1307` | File download failed. |

### Enum — `FileErrorCodeEnum` (subset most relevant to this scope)
| Code | Message |
|---|---|
| `E0078` / `E0301` | Sync in progress, please wait until it's completed before proceeding! |
| `E0109` | User does not exist! |
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
| `E0324` | This file cannot be uploaded |
| `E0325` | The byte length of the file name is too long |
| `E0333` | Not enough Supernote Cloud storage. Please try deleting files from the recycle bin. |
| `E0334` | The path cannot be empty. |
| `E0341` | An exception occurred while deleting the file |
| `E0342` | Data has expired, please refresh the page or re-sync and try again. |
| `E0343` | Rename failed |
| `E0344` | Move failed |
| `E0345` | Copy failed |
| `E0346` | Unknown error |
| `E0347`–`E0354` | Private-storage-disk errors (name clash / access denied / IO / too large / invalid path) |
| `E0355` | The chunk upload failed on the private storage disk |
| `E0356` | The chunk merge failed on the private storage disk |
| `E0357` | The chunk verify failed on the private storage disk |
| `E0358` | Cannot move a folder into itself or its subdirectory |
| `E9999` | Network error |

### `SignVerifier` (signature scheme)
- Secret key (hard-coded): `K+5xFzxbnB1iSZWqmu3Etw==`.
- **Upload signature:** `SHA-256hex( path + timestamp + nonce + fileSize?("") + secret )`; window **30 min** (`1_800_000 ms`) via `validate()`.
- **Download signature:** `SHA-256hex( path + timestamp + nonce + secret )` (no fileSize); window **24 h** (`86_400_000 ms`) via `validateDownload()`.
- A second HMAC-SHA256 scheme (`signData`/`verifySignature`, Base64 then
  stripped of non-alphanumerics) also exists and is used for inner-name-style
  tokens (`cloud_<uuid>.note_...`). Signature failures surface as `E1306`.

### Share-pipeline persistence (mapper SQL, for reference)
- `f_share_record` — `id, user_id, file_id, share_way, create_time` (`F-ShareRecordMapper.xml`, `insert`).
- `f_terminal_share_file` — `id, equipment_number, file_name, inner_name, create_time, update_time` (`insert`, `queryBeforeDate`, `delete`).
- `f_composite_image` — `id, user_id, file_id, file_name, inner_name, create_time, update_time` (`insert`).
- `f_file_convert` — `id, user_id, file_type, convert_type, file_id, origin_inner_name, inner_name, page_no, create_time, update_time`; queries `selectByFileId`, `queryByUserId`, `selectPngList` (`file_type="2"` = PNG pages), plus `insert`/`update`/`delete`.
- `f_terminal_file_convert` — `id, equipment_number, file_type, file_name, convert_type, share_id, url, page_no, create_time, update_time`; `queryByShareId` fetches all converted pages for a share.
