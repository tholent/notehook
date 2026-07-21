# Schedule & Summary API

Two controllers on the Supernote cloud-sync server, both mounted under the base
path `/api/file` and served on port **19071**. They cover two independent
feature areas that happen to share a base path:

- `F_ScheduleController` (Swagger tag *日程相关接口* — "Schedule-related APIs") —
  the device's To-Do / task planner sync surface: task lists (groups), tasks,
  recurring task instances, and per-list custom sort ordering. **16 endpoints.**
- `F_SummaryController` (Swagger tag *摘要相关接口* — "Summary-related APIs") —
  the "digest" / knowledge-clipping feature: summary libraries, summaries
  (clipped knowledge points), free-form tags, and file upload/download apply
  helpers. **16 endpoints.**

All handlers take the standard authenticated headers. Every response extends
`BaseVO` and follows the project envelope convention: logical failures are
returned as **HTTP 200 with `success: false`** plus an `errorCode`/`errorMsg`
drawn from `FileErrorCodeEnum`; an invalid/absent token yields `401`, a generic
unhandled error `"500"`, a duplicate submission caught by `@ResubmitCheck`
`"409"`, and a bean-validation failure `"400"`.

## (a) The schedule / task / recurrence data model

Inferred from the DTO/VO fields and the four schedule mapper XMLs. The model is
a close clone of the Google Tasks API surface (the DTO field docs literally
reference `nextPageToken`, `nextSyncToken`, `maxResults`, `needsAction`,
`completed`, and RFC 5545 recurrence), scoped per user via `user_id` on every
table.

Three tables hold the core data plus a fourth for sort state:

- **`t_schedule_task_group`** (task *lists*, called "groups") — PK `task_list_id`
  (client-supplied string/UUID), `user_id`, `title`, `last_modified`,
  `is_deleted`, `create_time`. This is a To-Do *list* container.
- **`t_schedule_task`** (root tasks) — PK `task_id` (client-supplied), FK
  `task_list_id`, `user_id`, plus the full task payload: `title`, `detail`,
  `last_modified`, `recurrence` (RFC 5545 rule string), `is_reminder_on`,
  `status` (`needsAction`|`completed`), `importance`, `due_time`,
  `completed_time`, `links`, `is_deleted`, and six sort columns (see below).
  Note: although the domain class is named `ScheduleTaskFileDO`, the mapper
  namespace and result map map it to table `t_schedule_task`.
- **`t_schedule_recur_task`** (recurring-task *instances*) — a root task whose
  `recurrence` rule expands into individual occurrences; each occurrence is a
  row keyed by `recurrence_id` (with the parent `task_id`, `task_list_id`,
  `user_id`). It carries only the per-occurrence mutable fields: `last_modified`,
  `due_time`, `completed_time`, `status`, `is_deleted`, and the same six sort
  columns. When a task is fetched (`ScheduleTaskVO` / `ScheduleTaskInfo`), its
  child occurrences are attached as a `scheduleRecurTask` list of
  `ScheduleRecurTaskDO`. The `AddScheduleTaskDTO.recurrenceId` field ("跟任务Id"
  = follow/parent-task id) links an added task to its recurrence series.
- **`t_schedule_sort`** — per-list custom ordering blob. PK `id`
  (auto), `user_id`, `task_list_id`, `title`, `last_modify`, and a `content`
  column holding the serialized sort order. Note the result map / `Base_Column_List`
  omits `content` from SELECTs even though `insert`/`update` write it (see the
  Sort endpoints' notes).

**The six sort columns** appear on both `t_schedule_task` and
`t_schedule_recur_task` and encode the device's multiple independent ordered
views of the same task:

| Column | DTO field | Meaning (translated) |
|---|---|---|
| `sort` | `sort` | position among *incomplete* tasks in a custom group / inbox |
| `sort_completed` | `sortCompleted` | position among *completed* tasks in a custom group / inbox |
| `planer_sort` | `planerSort` | position among incomplete tasks in the "Planner" view |
| `all_sort` | `allSort` | position among incomplete tasks in the "All" view |
| `all_sort_completed` | `allSortCompleted` | position among completed tasks in the "All" view |
| `sort_time` / `planer_sort_time` / `all_sort_time` | `sortTime` / `planerSortTime` / `allSortTime` | timestamp of the last reorder for each respective view |

**Sync model.** "Get all" is delta + page based. `t_schedule_task.selectAll`
filters `last_modified >= :since OR sort_time >= :since OR planer_sort_time >= :since`,
orders by `last_modified ASC`, and pages with `LIMIT :pageSize OFFSET :offset`.
The client sends `nextSyncToken` (a timestamp; the DTO doc says it is valid for
5 days and the server returns 403 / error `E0330` "NextSyncToken timeout" if
older) and `maxResults`/`nextPageTokens`; the response returns a fresh
`nextSyncToken` and `nextPageToken`. Deletes are soft (`is_deleted` flag via
`updateIsDeleted`) with a hard-delete path also present in the mappers.

## (b) The summary / digest + tagging data model

Inferred from the summary DTO/VO fields and the two summary mappers. A "summary"
(摘要 / digest) is a clipped knowledge point extracted from a note; summaries are
grouped into "summary libraries" (摘要库). Interestingly both the library and its
member summaries live in the **same table `t_summary`**, distinguished by the
`is_summary_group` flag:

- **`t_summary`** — PK `id` (auto Long), `user_id`, `file_id`, `name`,
  `unique_identifier`, `parent_unique_identifier` (a summary's parent library's
  unique id), `content` (the knowledge-point text), `source_path`,
  `data_source`, `source_type` (Integer), **`is_summary_group`** ('Y' = this row
  is a library/group, 'N'/absent = a leaf summary), `description`, `tags`
  (comma-separated tag names, denormalized), `md5_hash`, `metadata` (JSON),
  `comment_str`, `comment_handwrite_name`, `handwrite_inner_name`,
  `handwrite_md5`, `creation_time`, `last_modified_time`, `is_deleted`
  ('Y'/'N'), `create_time`, `update_time` (server DB `Date`s), `author`.
- **`t_summary_tag`** — PK `id` (auto), `user_id`, `name`, `created_at`. A flat
  per-user tag dictionary. Tags are also attached to summaries denormalized as
  the comma-joined `t_summary.tags` string; there is **no** join table, so the
  tag dictionary and the per-summary `tags` string are maintained separately.

**Handwriting attachments.** A summary can carry an associated handwritten
annotation file, referenced by `handwriteInnerName` (server object key),
`handwriteMD5`, and a display `commentHandwriteName`. Binary content is not
stored in the DB; it is uploaded/downloaded via S3-style pre-signed URLs
(`/upload/apply/summary` returns `fullUploadUrl`/`partUploadUrl`/`innerName`;
`/download/summary` returns a `url`).

**Query/paging.** Summary and summary-group queries are page/size based and
respond with `totalRecords`/`totalPages`/`currentPage`/`pageSize` plus the row
list (`SummaryDO` list, or a slim `SummaryInfoVO` list for the hash query). All
selects filter `is_deleted = 'N'`; deletes are soft (`is_deleted='Y'` +
`update_time`), with a scheduled hard-purge query
(`selectSoftDeletedSummariesBeforeDate`) present in the mapper.

---

## Endpoint summary

### Schedule (base `/api/file`)

| Method | Path | Auth | Summary |
|---|---|---|---|
| POST | `/schedule/group` | Required (inferred) | Add a task list (schedule group) |
| PUT | `/schedule/group` | Required (inferred) | Update a task list |
| DELETE | `/schedule/group/{taskListId}` | Required (inferred) | Delete a task list |
| POST | `/schedule/group/clear` | Required (inferred) | Clear all tasks in a task list |
| GET | `/schedule/group/{taskListId}` | Required (inferred) | Get one task list |
| POST | `/schedule/group/all` | Required (inferred) | Get all task lists (paged) |
| POST | `/schedule/task` | Required (inferred) | Add a task |
| PUT | `/schedule/task` | Required (inferred) | Update a task |
| PUT | `/schedule/task/list` | Required (inferred) | Batch update tasks |
| DELETE | `/schedule/task/{taskId}` | Required (inferred) | Delete a task |
| GET | `/schedule/task/{taskId}` | Required (inferred) | Get one task (with recurrences) |
| POST | `/schedule/task/all` | Required (inferred) | Get all tasks (delta sync, paged) |
| POST | `/schedule/sort` | Required (inferred) | Add a sort record for a list |
| PUT | `/schedule/sort` | Required (inferred) | Update a sort record |
| DELETE | `/schedule/sort/{taskListId}` | Required (inferred) | Delete a list's sort record |
| POST | `/query/schedule/sort` | Required (inferred) | Get sort records (cursor paged) |

### Summary (base `/api/file`)

| Method | Path | Auth | Summary |
|---|---|---|---|
| POST | `/add/summary/tag` | Required (inferred) | Add a summary tag |
| PUT | `/update/summary/tag` | Required (inferred) | Update a summary tag |
| DELETE | `/delete/summary/tag` | Required (inferred) | Delete a summary tag |
| GET | `/query/summary/tag` | Required (inferred) | Query all tags |
| POST | `/add/summary/group` | Required (inferred) | Add a summary library (group) |
| PUT | `/update/summary/group` | Required (inferred) | Update a summary library |
| DELETE | `/delete/summary/group` | Required (inferred) | Delete a summary library |
| POST | `/query/summary/group` | Required (inferred) | Query all summary libraries (paged) |
| POST | `/add/summary` | Required (inferred) | Add a summary |
| PUT | `/update/summary` | Required (inferred) | Update a summary |
| DELETE | `/delete/summary` | Required (inferred) | Delete a summary |
| POST | `/query/summary/id` | Required (inferred) | Query summaries by id set |
| POST | `/query/summary/hash` | Required (inferred) | Query all summary MD5 hashes (paged) |
| POST | `/query/summary` | Required (inferred) | Query all summaries (paged) |
| POST | `/download/summary` | Required (inferred) | Get a download URL for a summary file |
| POST | `/upload/apply/summary` | Required (inferred) | Apply for a summary-file upload URL |

Common headers for **every** endpoint below: `x-access-token` (JWT, required),
`equipmentNo` (device identifier; `equipment` type 1=web, 2=APP, 3=terminal,
4=user platform). The authenticated `userId` is derived from the JWT
(`JwtTokenUserUtil`), never from the request body, and scopes all queries.

---

## Schedule endpoints

### POST `/api/file/schedule/group`

**Summary:** Add a new task list (schedule group).
**Auth:** Required (inferred).
**Guard:** `@ResubmitCheck` keyed on `taskListId`, `title`, `createTime`,
`lastModified`, active only when `title != null` — duplicate rapid submits
return `"409"`.

**Request body — `AddScheduleTaskGroupDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `taskListId` | String | No | — | Task-list id (client-supplied; UUID) |
| `title` | String | Yes | `@NotBlank` "The name of the To-Do list cannot be empty." | Task-list name |
| `lastModified` | Long | No | — | Last-modified timestamp (ms) |
| `createTime` | Long | No | — | List creation timestamp (ms) |

**Response — `AddScheduleTaskGroupVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `taskListId` | String | Id of the created task list |
| *(inherited)* `success`/`errorCode`/`errorMsg` | boolean/String/String | Envelope |

**Errors:** `400` (validation), `409` (resubmit), `500`.
**Notes:** Insert maps to `t_schedule_task_group` (`insert`). The mapper declares
`useGeneratedKeys` with `keyProperty="task_list_id"`, but the id is normally
client-supplied; the server likely generates one when `taskListId` is null.

### PUT `/api/file/schedule/group`

**Summary:** Update a task list.
**Auth:** Required (inferred).

**Request body — `UpdateScheduleTaskGroupDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `taskListId` | String | Yes | `@NotBlank` "The ID of the To-Do list cannot be empty." | Task-list id |
| `title` | String | Yes | `@NotBlank` "The name of the To-Do list cannot be empty." | New name |
| `lastModified` | Long | No | — | Last-modified timestamp (ms) |

**Response — `BaseVO`** (`success`/`errorCode`/`errorMsg`).
**Errors:** `400`, `E0328` "The task group does not exist", `500`.
**Notes:** Maps to `updateByPrimaryKeySelective` (`title`, `last_modified`,
`is_deleted` are the selectively-updated columns) scoped by `task_list_id` +
`user_id`.

### DELETE `/api/file/schedule/group/{taskListId}`

**Summary:** Delete a task list.
**Auth:** Required (inferred).

**Path params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `taskListId` | String | Yes | Task-list id to delete |

**Response — `BaseVO`.**
**Errors:** `E0328` "The task group does not exist", `500`.
**Notes:** `deleteScheduleTaskGroup` hard-deletes the group row by
`task_list_id` + `user_id`. Member tasks are removed separately (see clear /
`deleteScheduleTask` with `taskListId`).

### POST `/api/file/schedule/group/clear`

**Summary:** Clear (empty) a task list — remove all its tasks while keeping the
list.
**Auth:** Required (inferred).

**Request body — `ClearScheduleTaskGroupDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `taskListId` | String | Yes | `@NotBlank` "The ID of the To-Do list cannot be empty." | Task-list id to clear |
| `lastModified` | Long | Yes | `@NotNull` "The last modification time of the To-Do list cannot be empty." | Last-modified timestamp (ms) |

**Response — `BaseVO`.**
**Errors:** `400`, `E0328`, `500`.
**Notes:** Uses `t_schedule_task.updateIsDeleted` (soft delete) / `deleteScheduleTask`
filtered by `task_list_id` + `user_id` to remove member tasks.

### GET `/api/file/schedule/group/{taskListId}`

**Summary:** Get a single task list.
**Auth:** Required (inferred).

**Path params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `taskListId` | String | Yes | Task-list id |

**Response — `GetScheduleTaskGroupVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `taskListId` | String | Task-list id |
| `userId` | Long | Owner user id |
| `title` | String | Task-list name |
| `lastModified` | Long | Last-modified timestamp (ms) |
| `isDeleted` | String | Soft-delete flag ('Y'/'N') |
| `createTime` | Long | List creation timestamp (ms) |

**Errors:** `E0328` "The task group does not exist", `500`.
**Notes:** Maps to `selectByPrimaryKey`/`selectById` on `t_schedule_task_group`.

### POST `/api/file/schedule/group/all`

**Summary:** Get all task lists (paged).
**Auth:** Required (inferred).

**Request body — `ScheduleTaskGroupDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `maxResults` | String | No | default 20 | Max task-lists per page |
| `pageToken` | String | No | — | Page cursor (result page to return) |

**Response — `ScheduleTaskGroupVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `pageToken` | String | Cursor for the next page |
| `scheduleTaskGroup` | List<`ScheduleTaskGroupDO`> | Task lists (see Data models) |

**Errors:** `500`.
**Notes:** `selectGroup` orders `last_modified ASC` with `LIMIT :pageSize OFFSET
:offset`; `selectCount` supplies the total for cursor math.

### POST `/api/file/schedule/task`

**Summary:** Add a new task.
**Auth:** Required (inferred).
**Guard:** `@ResubmitCheck` keyed on `taskId`, `taskListId`, `recurrenceId`,
`title`, `detail`, `lastModified`, active when `title != null` → `"409"`.

**Request body — `AddScheduleTaskDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `taskId` | String | No | — | Task id (client-supplied) |
| `taskListId` | String | No | — | Parent task-list id |
| `recurrenceId` | String | No | — | Follow/parent-task id linking this to a recurrence series |
| `title` | String | Yes | `@NotBlank` "The name of the To-Do task cannot be empty." | Task name |
| `detail` | String | No | — | Task details/description |
| `lastModified` | Long | No | — | Task last-modified timestamp |
| `recurrence` | String | No | — | Recurrence rule (RFC 5545 RRULE standard) |
| `isReminderOn` | String | No | default false | Whether a reminder is enabled |
| `status` | String | No | — | Task status: `needsAction` or `completed` |
| `importance` | String | No | — | Task importance/priority |
| `dueTime` | Long | No | — | Due time (UTC ms timestamp) |
| `completedTime` | Long | No | — | Completion time (UTC ms timestamp) |
| `links` | String | No | — | Task link attribute(s) |
| `isDeleted` | String | No | default false | Soft-delete flag |
| `sort` | Integer | No | — | Incomplete order in custom group / inbox |
| `sortCompleted` | Integer | No | — | Completed order in custom group / inbox |
| `planerSort` | Integer | No | — | Incomplete order in Planner view |
| `allSort` | Integer | No | — | Incomplete order in All view |
| `allSortCompleted` | Integer | No | — | Completed order in All view |
| `sortTime` | Long | No | — | Timestamp of last `sort`/inbox reorder |
| `planerSortTime` | Long | No | — | Timestamp of last Planner reorder |
| `allSortTime` | Long | No | — | Timestamp of last All-view reorder |

**Response — `AddScheduleTaskVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `taskId` | String | Id of the created task |

**Errors:** `400`, `409`, `E0328` (parent list missing, inferred), `500`.
**Notes:** Insert into `t_schedule_task` (all 22 columns). If `recurrenceId`/
`recurrence` are present the server also seeds `t_schedule_recur_task`
occurrence rows.

### PUT `/api/file/schedule/task`

**Summary:** Update a task.
**Auth:** Required (inferred).

**Request body — `UpdateScheduleTaskDTO`** (same shape as `AddScheduleTaskDTO`
with tightened validation):

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `taskId` | String | Yes | `@NotNull` "The ID of the To-Do task cannot be empty." | Task id |
| `taskListId` | String | No | — | Parent task-list id |
| `recurrenceId` | String | No | — | Follow/parent-task id |
| `title` | String | Yes | `@NotBlank` "The name of the To-Do task cannot be empty." | Task name |
| `detail` | String | No | — | Task details |
| `lastModified` | Long | Yes | `@NotNull` "The last modification time of the To-Do list cannot be empty." | Last-modified timestamp |
| `recurrence` | String | No | — | Recurrence rule (RFC 5545) |
| `isReminderOn` | String | No | default false | Reminder enabled |
| `status` | String | No | — | `needsAction` / `completed` |
| `importance` | String | No | — | Importance |
| `dueTime` | Long | No | — | Due time (UTC ms) |
| `completedTime` | Long | No | — | Completion time (UTC ms) |
| `links` | String | No | — | Link attribute(s) |
| `isDeleted` | String | No | default false | Soft-delete flag |
| `sort` … `allSortTime` | Integer / Long | No | — | Same six-view sort columns as Add (see the sort-column table) |

**Response — `UpdateScheduleTaskVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `taskId` | String | Id of the updated task |

**Errors:** `400`, `E0329` "The root task does not exist", `500`.
**Notes:** Maps to `t_schedule_task.update`; `recurrence`, `isReminderOn`,
`isDeleted`, and each sort column update only when non-null (`last_modified`
only when non-null and non-zero). Row scoped by `task_id` + `user_id`.

### PUT `/api/file/schedule/task/list`

**Summary:** Batch update tasks.
**Auth:** Required (inferred).

**Request body — `UpdateScheduleTaskListDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `taskListId` | String | No | — | Parent task-list id (context for the batch) |
| `updateScheduleTaskList` | List<`UpdateScheduleTaskDTO`> | No | each element validated as above | Tasks to update |

**Response — `BaseVO`.**
**Errors:** `400`, `E0329`, `500`.
**Notes:** Iterates `t_schedule_task.update` per element; primary use is bulk
reordering (writing the sort columns) after a device-side drag reorder.

### DELETE `/api/file/schedule/task/{taskId}`

**Summary:** Delete a task.
**Auth:** Required (inferred).

**Path params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `taskId` | String | Yes | Task id to delete |

**Response — `BaseVO`.**
**Errors:** `E0329` "The root task does not exist", `500`.
**Notes:** `updateIsDeleted` (soft) or `deleteScheduleTask` by `task_id` +
`user_id`. Associated `t_schedule_recur_task` rows are cleared via
`updateIsDeleted`/`deleteScheduleRecurTask`.

### GET `/api/file/schedule/task/{taskId}`

**Summary:** Get a single task, including its recurring occurrences.
**Auth:** Required (inferred).

**Path params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `taskId` | String | Yes | Task id |

**Response — `ScheduleTaskVO`** (extends `BaseVO`). Full task payload plus its
recurrence children:

| Field | Type | Description |
|---|---|---|
| `taskId` | Long | Task id (note: Long here, String in DTOs/`ScheduleTaskInfo`) |
| `taskListId` | Long | Parent task-list id (Long here) |
| `title` | String | Task name |
| `detail` | String | Details |
| `lastModified` | Long | Last-modified timestamp |
| `recurrence` | String | RFC 5545 rule |
| `isReminderOn` | String | Reminder enabled |
| `status` | String | `needsAction`/`completed` |
| `importance` | String | Importance |
| `dueTime` | Long | Due time (UTC ms) |
| `completedTime` | Long | Completion time (UTC ms) |
| `links` | String | Link attribute(s) |
| `isDeleted` | String | Soft-delete flag |
| `sort`, `sortCompleted`, `planerSort`, `allSort`, `allSortCompleted` | Integer | Six-view sort positions |
| `sortTime`, `planerSortTime`, `allSortTime` | Long | Per-view reorder timestamps |
| `scheduleRecurTask` | List<`ScheduleRecurTaskDO`> | Recurrence occurrences (see Data models) |

**Errors:** `E0329` "The root task does not exist", `500`.
**Notes:** `t_schedule_task.selectById` + `t_schedule_recur_task.selectByPrimaryKey`
joined into the VO. The Long typing of `taskId`/`taskListId` here diverges from
the String typing everywhere else — treat as ids and be lenient parsing.

### POST `/api/file/schedule/task/all`

**Summary:** Get all tasks (delta sync, paged).
**Auth:** Required (inferred).

**Request body — `ScheduleTaskDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `maxResults` | String | No | default 20 | Max tasks per page |
| `nextPageTokens` | String | No | — | Page cursor (result page to return) |
| `nextSyncToken` | Long | No | valid 5 days | Timestamp of the client's last sync; expired → error (see notes) |

**Response — `ScheduleTaskAllVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `nextPageToken` | String | Cursor for the next page |
| `nextSyncToken` | Long | New sync token returned on the last page |
| `scheduleTask` | List<`ScheduleTaskInfo`> | Tasks (each with its `scheduleRecurTask` children) |

**Errors:** `E0330` "NextSyncToken timeout" (the DTO doc says the server returns
**403** when `nextSyncToken` is older than 5 days), `500`.
**Notes:** `selectAll` returns rows where
`last_modified >= :since OR sort_time >= :since OR planer_sort_time >= :since`,
ordered `last_modified ASC`, `LIMIT :pageSize OFFSET :offset`. This is the
primary device pull-sync endpoint; the delta window spans content edits *and*
reorders.

### POST `/api/file/schedule/sort`

**Summary:** Add a sort record for a task list.
**Auth:** Required (inferred).

**Request body — `ScheduleSortDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `taskListId` | String | No | — | Task-list id the sort belongs to |
| `title` | String | No | — | Task-list name (denormalized copy) |
| `lastModify` | Long | No | — | Last-modified timestamp |
| `content` | String | No | — | Serialized sort order payload |

**Response — `BaseVO`.**
**Errors:** `E0331` "This task group sorting already exists" (duplicate), `500`.
**Notes:** Insert into `t_schedule_sort` (writes `content`). One sort record per
`(user_id, task_list_id)`; a second insert triggers `E0331`.

### PUT `/api/file/schedule/sort`

**Summary:** Update a sort record.
**Auth:** Required (inferred).

**Request body — `ScheduleSortDTO`** (same as Add):

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `taskListId` | String | No | — | Task-list id |
| `title` | String | No | — | Task-list name |
| `lastModify` | Long | No | — | Last-modified timestamp |
| `content` | String | No | — | Serialized sort order |

**Response — `BaseVO`.**
**Errors:** `E0331` (inferred, if absent), `500`.
**Notes:** `t_schedule_sort.update` selectively sets `title` (when non-null) and
`last_modify` (when non-null and non-zero) by `(user_id, task_list_id)`. NB: the
`update` statement does **not** write `content` (only `insert` does) — updating
the order string alone may not persist; flagged as an inconsistency in the
decompiled mapper.

### DELETE `/api/file/schedule/sort/{taskListId}`

**Summary:** Delete a task list's sort record.
**Auth:** Required (inferred).

**Path params:**

| Param | Type | Required | Description |
|---|---|---|---|
| `taskListId` | String | Yes | Task-list id whose sort record to delete (bound to method param `taskId`) |

**Response — `BaseVO`.**
**Errors:** `500`.
**Notes:** `t_schedule_sort.delete` by `(user_id, task_list_id)`.

### POST `/api/file/query/schedule/sort`

**Summary:** Get sort records (cursor paged).
**Auth:** Required (inferred).

**Request body — `GetScheduleSortDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `nextIndexNumber` | Integer | No | null on first call | Cursor for the next record; null means "first query" |

**Response — `GetScheduleSortVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `taskListId` | String | Task-list id |
| `title` | String | Task-list name |
| `lastModify` | Long | Last-modified timestamp |
| `content` | String | Serialized sort order |
| `nextIndexNumber` | Integer | Next cursor; null means no more data |

**Errors:** `500`.
**Notes:** `t_schedule_sort.select`/`selectSort` by `user_id`. Iterated via
`nextIndexNumber` (request null → server walks from the first record; response
null → end of data). Returns one record per response; `content` is read here
even though the shared `Base_Column_List` omits it, implying a dedicated select.

---

## Summary endpoints

### POST `/api/file/add/summary/tag`

**Summary:** Add a summary tag.
**Auth:** Required (inferred).

**Request body — `AddSummaryTagDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `name` | String | Yes | `@NotNull` "tag name cannot be empty." | Tag name |

**Response — `AddSummaryTagVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `id` | Long | Id of the created tag |

**Errors:** `400`, `E0335`/`E0337` "The tag name already exists.", `500`.
**Notes:** `t_summary_tag.insertTag` (`useGeneratedKeys`). Existence checked via
`selectTagByUserIdAndName` before insert. Tags are per-user.

### PUT `/api/file/update/summary/tag`

**Summary:** Update a summary tag.
**Auth:** Required (inferred).

**Request body — `UpdateSummaryTagDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `id` | Long | Yes | `@NotNull` "tag id cannot be empty." | Tag id |
| `name` | String | Yes | `@NotNull` "tag name cannot be empty." | New tag name |

**Response — `BaseVO`.**
**Errors:** `400`, `E0336` "The tag does not exist.", `E0335`/`E0337` "The tag
name already exists.", `500`.
**Notes:** `t_summary_tag.updateTag` by `id`.

### DELETE `/api/file/delete/summary/tag`

**Summary:** Delete a summary tag.
**Auth:** Required (inferred).

**Request body — `DeleteSummaryTagDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `id` | Long | Yes | `@NotNull` "tag id cannot be empty." | Tag id |

**Response — `BaseVO`.**
**Errors:** `400`, `E0336` "The tag does not exist.", `500`.
**Notes:** `t_summary_tag.deleteTag` (hard delete) by `id`. Does not rewrite the
denormalized `t_summary.tags` strings.

### GET `/api/file/query/summary/tag`

**Summary:** Query all tags for the current user.
**Auth:** Required (inferred).
**Request:** none (no body, no params — user from JWT).

**Response — `QuerySummaryTagVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `summaryTagDOList` | List<`SummaryTagDO`> | All of the user's tags (see Data models) |

**Errors:** `500`.
**Notes:** `t_summary_tag.selectTagsByUserId`.

### POST `/api/file/add/summary/group`

**Summary:** Add a summary library (group).
**Auth:** Required (inferred).

**Request body — `AddSummaryGroupDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `uniqueIdentifier` | String | Yes | `@NotNull` "The unique identifier cannot be empty." | Library unique identifier |
| `name` | String | Yes | `@NotNull` "Summary library name cannot be empty." | Library name |
| `description` | String | No | — | Library description |
| `md5Hash` | String | Yes | `@NotNull` "The data MD5 checksum value cannot be empty." | Data MD5 checksum |
| `creationTime` | Long | No | — | Creation timestamp (ms) |
| `lastModifiedTime` | Long | No | — | Last-modified timestamp (ms) |

**Response — `AddSummaryGroupVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `id` | Long | Id of the created library row |

**Errors:** `400`, `E0338` "The unique identifier already exists.", `500`.
**Notes:** Inserts a `t_summary` row with `is_summary_group='Y'`. Uniqueness
checked via `selectSummaryByUniqueIdentifier`.

### PUT `/api/file/update/summary/group`

**Summary:** Update a summary library.
**Auth:** Required (inferred).

**Request body — `UpdateSummaryGroupDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `id` | Long | Yes | `@NotNull` "Id cannot be empty." | Library row id |
| `uniqueIdentifier` | String | No | — | Unique identifier |
| `name` | String | No | — | Library name |
| `description` | String | No | — | Description |
| `metadata` | String | No | — | Metadata (JSON) |
| `commentStr` | String | No | — | Comment text |
| `commentHandwriteName` | String | No | — | Handwritten comment file display name |
| `handwriteInnerName` | String | No | — | Handwriting file inner (object) name |
| `md5Hash` | String | Yes | `@NotNull` "The data MD5 checksum value cannot be empty." | Data MD5 checksum |
| `lastModifiedTime` | Long | No | — | Last-modified timestamp (ms) |

**Response — `BaseVO`.**
**Errors:** `400`, `E0339` "The summary library does not exist.", `500`.
**Notes:** `t_summary.updateSummary` (selective) by `id` + `user_id`.

### DELETE `/api/file/delete/summary/group`

**Summary:** Delete a summary library (and, by inference, cascade its members).
**Auth:** Required (inferred).

**Request body — `DeleteSummaryGroupDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `id` | Long | Yes | `@NotNull` "The summary group ID cannot be empty." | Library row id |

**Response — `BaseVO`.**
**Errors:** `400`, `E0339` "The summary library does not exist.", `500`.
**Notes:** Soft delete via `softDeletionSummaryById`; member summaries cleared by
`softDeletionSummaryByParentUniqueIdentifier` (matching the library's
`unique_identifier` against members' `parent_unique_identifier`). Hard-delete
variants (`deleteSummary`, `deleteSummaryByParentUniqueIdentifier`) exist in the
mapper for the purge job.

### POST `/api/file/query/summary/group`

**Summary:** Query all summary libraries (paged).
**Auth:** Required (inferred).

**Request body — `QuerySummaryGroupDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `page` | Integer | No | — | Page number |
| `size` | Integer | No | — | Page size |

**Response — `QuerySummaryGroupVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `totalRecords` | Long | Total library rows |
| `totalPages` | Integer | Total pages |
| `currentPage` | Integer | Current page |
| `pageSize` | Integer | Page size |
| `summaryDOList` | List<`SummaryDO`> | Library rows (see Data models) |

**Errors:** `500`.
**Notes:** `selectSummaryByUserIdAndUniqueIdentifier` with
`is_summary_group='Y'`, filtered `is_deleted='N'`.

### POST `/api/file/add/summary`

**Summary:** Add a summary (knowledge point).
**Auth:** Required (inferred).

**Request body — `AddSummaryDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `uniqueIdentifier` | String | No | — | Unique id (libraries always have one; leaf summaries may not — terminal design) |
| `fileId` | Long | No | — | Associated file id |
| `parentUniqueIdentifier` | String | No | — | Parent library's unique identifier |
| `content` | String | No | — | Knowledge-point content |
| `dataSource` | String | No | — | Data source |
| `sourcePath` | String | No | — | Source path |
| `sourceType` | Integer | No | — | Source type |
| `tags` | String | No | — | Tags, comma-separated |
| `md5Hash` | String | No | — | Data MD5 checksum |
| `metadata` | String | No | — | Metadata (JSON) |
| `commentStr` | String | No | — | Comment text |
| `commentHandwriteName` | String | No | — | Handwritten comment display name |
| `handwriteInnerName` | String | No | — | Handwriting file inner name |
| `handwriteMD5` | String | No | — | Handwriting file MD5 |
| `creationTime` | Long | No | — | Creation timestamp (ms) |
| `lastModifiedTime` | Long | No | — | Last-modified timestamp (ms) |
| `author` | String | No | — | Author |

**Response — `AddSummaryVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `id` | Long | Id of the created summary |

**Errors:** `E0339` "The summary library does not exist." (bad
`parentUniqueIdentifier`, inferred), `500`.
**Notes:** `t_summary.insertSummary` with `is_summary_group` unset/'N'. The
`tags` string is stored denormalized (no join table).

### PUT `/api/file/update/summary`

**Summary:** Update a summary.
**Auth:** Required (inferred).

**Request body — `UpdateSummaryDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `id` | Long | Yes | `@NotNull` "Id cannot be empty." | Summary id |
| `parentUniqueIdentifier` | String | No | — | Parent library unique id |
| `content` | String | No | — | Knowledge-point content |
| `sourcePath` | String | No | — | Source path |
| `dataSource` | String | No | — | Data source |
| `sourceType` | Integer | No | — | Source type |
| `tags` | String | No | — | Tags, comma-separated |
| `md5Hash` | String | No | — | Data MD5 checksum |
| `metadata` | String | No | — | Metadata (JSON) |
| `commentStr` | String | No | — | Comment text |
| `commentHandwriteName` | String | No | — | Handwritten comment display name |
| `handwriteInnerName` | String | No | — | Handwriting file inner name |
| `handwriteMD5` | String | No | — | Handwriting file MD5 |
| `lastModifiedTime` | Long | No | — | Last-modified timestamp (ms) |
| `author` | String | No | — | Author |

**Response — `BaseVO`.**
**Errors:** `400`, `E0340` "The summary does not exist.", `500`.
**Notes:** `t_summary.updateSummary` (selective) by `id` + `user_id`; always
touches `update_time`.

### DELETE `/api/file/delete/summary`

**Summary:** Delete a summary.
**Auth:** Required (inferred).

**Request body — `DeleteSummaryDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `id` | Long | Yes | `@NotNull` "The summary ID cannot be empty." | Summary id |

**Response — `BaseVO`.**
**Errors:** `400`, `E0340` "The summary does not exist.", `500`.
**Notes:** `softDeletionSummaryById` (sets `is_deleted='Y'`, `update_time`).

### POST `/api/file/query/summary/id`

**Summary:** Query summaries by a set of ids.
**Auth:** Required (inferred).

**Request body — `QuerySummaryDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `page` | Integer | No | — | Page number |
| `size` | Integer | No | — | Page size |
| `parentUniqueIdentifier` | String | No | — | Parent library unique id filter |
| `ids` | List<Long> | No | — | Explicit id set to fetch |

**Response — `QuerySummaryByIdVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `summaryDOList` | List<`SummaryDO`> | Matching summaries |

**Errors:** `500`.
**Notes:** `selectSummaryByIds` — `WHERE user_id = ? AND is_deleted='N' AND id IN
(…)`. Not actually paged despite the shared DTO carrying `page`/`size`.

### POST `/api/file/query/summary/hash`

**Summary:** Query all summary MD5 hashes (paged) — a slim sync-diff endpoint.
**Auth:** Required (inferred).

**Request body — `QuerySummaryDTO`** (same as above: `page`, `size`,
`parentUniqueIdentifier`, `ids`).

**Response — `QuerySummaryMD5HashVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `totalRecords` | Long | Total rows |
| `totalPages` | Integer | Total pages |
| `currentPage` | Integer | Current page |
| `pageSize` | Integer | Page size |
| `summaryInfoVOList` | List<`SummaryInfoVO`> | Slim per-summary info (see Data models) |

**Errors:** `500`.
**Notes:** Returns only id/user/hashes/handwrite/lastModified + a parsed
`metadataMap`; used by the device to diff which summaries changed before pulling
full bodies.

### POST `/api/file/query/summary`

**Summary:** Query all summaries (paged, full rows).
**Auth:** Required (inferred).

**Request body — `QuerySummaryDTO`** (same fields as `/query/summary/id`).

**Response — `QuerySummaryVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `totalRecords` | Long | Total rows |
| `totalPages` | Integer | Total pages |
| `currentPage` | Integer | Current page |
| `pageSize` | Integer | Page size |
| `summaryDOList` | List<`SummaryDO`> | Full summary rows |

**Errors:** `500`.
**Notes:** `selectSummaryByParentUniqueIdentifier` / `selectSummaryByUserIdAndUniqueIdentifier`,
`is_deleted='N'`, paged by `page`/`size`.

### POST `/api/file/download/summary`

**Summary:** Get a download URL for a summary's associated (handwriting) file.
**Auth:** Required (inferred).

**Request body — `DownloadSummaryDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `id` | Long | Yes | `@NotNull` "Id cannot be empty." | Summary id |

**Response — `DownloadSummaryVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `url` | String | Pre-signed download URL |

**Errors:** `400`, `E0340` "The summary does not exist.", `500`.
**Notes:** Resolves the summary's `handwrite_inner_name` object key to a
pre-signed S3-style URL.

### POST `/api/file/upload/apply/summary`

**Summary:** Apply for a summary-file upload URL (pre-signed upload handshake).
**Auth:** Required (inferred).

**Request body — `UploadSummaryApplyDTO`:**

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `fileName` | String | Yes | `@NotBlank` "文件名 不能为空" ("File name cannot be empty") | File name to upload |
| `equipmentNo` | String | No | — | Device number |

**Response — `UploadSummaryApplyVO`** (extends `BaseVO`):

| Field | Type | Description |
|---|---|---|
| `fullUploadUrl` | String | Pre-signed URL for a single/full upload |
| `partUploadUrl` | String | Pre-signed URL for a multipart/part upload |
| `innerName` | String | Server-assigned object key to record on the summary |

**Errors:** `400`, `E0334` "The path cannot be empty." (inferred), `500`.
**Notes:** Mirrors the main file-upload apply flow; the returned `innerName` is
later written back as the summary's `handwriteInnerName`. This is the only DTO in
scope whose validation message is still in Chinese (untranslated by the vendor).

---

## Data models

### Envelope (shared)

- **`BaseVO`** — `success` (boolean, default true), `errorCode` (String),
  `errorMsg` (String). All VOs below extend it.

### Schedule domain objects

**`ScheduleTaskGroupDO`** (table `t_schedule_task_group`) — returned inside
`ScheduleTaskGroupVO.scheduleTaskGroup`:

| Field | Column | Type | Description |
|---|---|---|---|
| `taskListId` | `task_list_id` (PK) | String | Task-list id |
| `userId` | `user_id` | Long | Owner |
| `title` | `title` | String | List name |
| `lastModified` | `last_modified` | Long | Last-modified ms |
| `isDeleted` | `is_deleted` | String | Soft-delete flag |
| `createTime` | `create_time` | Long | Creation ms |

**`ScheduleTaskFileDO`** (table `t_schedule_task`) — the root-task domain object
(the "get one" VO `ScheduleTaskVO` and list element `ScheduleTaskInfo` mirror it;
see field list under GET `/schedule/task/{taskId}` and `ScheduleTaskInfo` below).
Columns: `task_id` (PK, String), `task_list_id`, `user_id` (Long), `title`,
`detail`, `last_modified`, `recurrence`, `is_reminder_on`, `status`,
`importance`, `due_time`, `completed_time`, `links`, `is_deleted`, `sort`,
`sort_completed`, `planer_sort`, `all_sort`, `all_sort_completed`, `sort_time`,
`planer_sort_time`, `all_sort_time`.

**`ScheduleTaskInfo`** (list element in `ScheduleTaskAllVO.scheduleTask`) — same
fields as `ScheduleTaskFileDO` but `taskId`/`taskListId` typed as **String**,
plus a `scheduleRecurTask` list of `ScheduleRecurTaskDO`. Field-by-field: `taskId`,
`taskListId`, `title`, `detail`, `lastModified`, `recurrence`, `isReminderOn`,
`status`, `importance`, `dueTime`, `completedTime`, `links`, `isDeleted`, `sort`,
`sortCompleted`, `planerSort`, `allSort`, `allSortCompleted`, `sortTime`,
`planerSortTime`, `allSortTime`, `scheduleRecurTask`.

**`ScheduleTaskGroupInfo`** (declared VO helper; `taskListId` Long, `title`,
`lastModified` Long, `isDeleted`) — a slim task-list projection; present in the
codebase but not referenced by any in-scope endpoint response.

**`ScheduleRecurTaskDO`** (table `t_schedule_recur_task`) — one recurrence
occurrence; attached to `ScheduleTaskVO`/`ScheduleTaskInfo`:

| Field | Column | Type | Description |
|---|---|---|---|
| `taskId` | `task_id` | String | Parent root-task id |
| `recurrenceId` | `recurrence_id` | String | Occurrence id (query key) |
| `taskListId` | `task_list_id` | String | Parent list id |
| `userId` | `user_id` | Long | Owner |
| `lastModified` | `last_modified` | Long | Last-modified ms |
| `dueTime` | `due_time` | Long | Occurrence due (UTC ms) |
| `completedTime` | `completed_time` | Long | Occurrence completion (UTC ms) |
| `status` | `status` | String | `needsAction`/`completed` |
| `isDeleted` | `is_deleted` | String | Soft-delete flag |
| `sort`, `sortCompleted`, `planerSort`, `allSort`, `allSortCompleted` | resp. | Integer | Per-view sort positions |
| `sortTime`, `planerSortTime`, `allSortTime` | resp. | Long | Per-view reorder timestamps |

*(`ScheduleRecurTaskVO` is the `BaseVO`-wrapped standalone form of the same
object — `taskId`/`recurrenceId`/`taskListId` as Long — but no in-scope endpoint
returns it directly; occurrences are always delivered nested in a task.)*

**`ScheduleSortDO`** (table `t_schedule_sort`) — `id` (PK Long), `userId`,
`taskListId`, `title`, `lastModify` (Long), `content` (String, the serialized
order; note it is excluded from the mapper's shared column list and update
statement). Surfaced to clients via `GetScheduleSortVO`.

### Summary domain objects

**`SummaryDO`** (table `t_summary`) — the unified library+summary row; returned
in `QuerySummaryVO`, `QuerySummaryByIdVO`, `QuerySummaryGroupVO`:

| Field | Column | Type | Description |
|---|---|---|---|
| `id` | `id` (PK) | Long | Row id (auto) |
| `fileId` | `file_id` | Long | Associated file id |
| `name` | `name` | String | Library/summary name |
| `userId` | `user_id` | Long | Owner |
| `uniqueIdentifier` | `unique_identifier` | String | Row unique id (always set for libraries) |
| `parentUniqueIdentifier` | `parent_unique_identifier` | String | Parent library's unique id |
| `content` | `content` | String | Knowledge-point text |
| `sourcePath` | `source_path` | String | Source path |
| `dataSource` | `data_source` | String | Data source |
| `sourceType` | `source_type` | Integer | Source type |
| `isSummaryGroup` | `is_summary_group` | String | 'Y' = library/group, else leaf summary |
| `description` | `description` | String | Library description |
| `tags` | `tags` | String | Comma-separated tag names (denormalized) |
| `md5Hash` | `md5_hash` | String | Data MD5 checksum |
| `metadata` | `metadata` | String | Metadata (JSON) |
| `commentStr` | `comment_str` | String | Comment text |
| `commentHandwriteName` | `comment_handwrite_name` | String | Handwritten comment display name |
| `handwriteInnerName` | `handwrite_inner_name` | String | Handwriting file object key |
| `handwriteMD5` | `handwrite_md5` | String | Handwriting file MD5 |
| `creationTime` | `creation_time` | Long | Creation ms |
| `lastModifiedTime` | `last_modified_time` | Long | Last-modified ms |
| `isDeleted` | `is_deleted` | String | 'Y'/'N' soft-delete flag |
| `createTime` | `create_time` | Date | Server row-create time |
| `updateTime` | `update_time` | Date | Server row-update time |
| `author` | `author` | String | Author |

**`SummaryInfoVO`** (list element in `QuerySummaryMD5HashVO`) — slim diff
projection: `id` (Long), `userId` (Long), `md5Hash` (String), `handwriteMd5`
(String), `commentHandwriteName` (String), `lastModifiedTime` (Long),
`metadataMap` (Map<String,String> — the parsed `metadata` JSON).

**`SummaryTagDO`** (table `t_summary_tag`) — returned in `QuerySummaryTagVO`:

| Field | Column | Type | Description |
|---|---|---|---|
| `id` | `id` (PK) | Long | Tag id (auto) |
| `name` | `name` | String | Tag name |
| `userId` | `user_id` | Long | Owner |
| `createdAt` | `created_at` | Date | Creation time |

### `FileErrorCodeEnum` — codes applicable to this surface

| Code | Message |
|---|---|
| `E0328` | The task group does not exist |
| `E0329` | The root task does not exist |
| `E0330` | NextSyncToken timeout |
| `E0331` | This task group sorting already exists |
| `E0332` | Unable to sync. Please upgrade to the latest version. |
| `E0333` | Not enough Supernote Cloud storage. Please try deleting files from the recycle bin. |
| `E0334` | The path cannot be empty. |
| `E0335` | The tag name already exists. |
| `E0336` | The tag does not exist. |
| `E0337` | The tag name already exists. (duplicate of E0335) |
| `E0338` | The unique identifier already exists. |
| `E0339` | The summary library does not exist. |
| `E0340` | The summary does not exist. |

Codes `E0328`–`E0331` are schedule-specific; `E0335`–`E0340` are summary/tag
specific; `E0332`–`E0334` are shared sync/storage/path errors that any of these
handlers may surface. Non-enumerated failures use the numeric envelope codes:
`"400"` (validation), `"401"` (invalid/absent token), `"409"` (`@ResubmitCheck`
duplicate), `"500"` (generic). Error codes attached to individual endpoints above
marked "(inferred)" are not proven by a decompiled method body (stripped by
ClassFinal) — they are deduced from the DTO validation, mapper existence checks,
and the enum's evident intent.
