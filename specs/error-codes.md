# Error-Code Catalogue

Consolidated list of every error code the server can return in the
`errorCode` field of the [response envelope](README.md#the-response-envelope).

Codes fall into two groups:

1. **Transport/framework codes** emitted by `GlobalExceptionHandler` — numeric
   HTTP-like strings (`"400"`, `"401"`, `"409"`, `"422"`, `"500"`,
   `"403 Forbidden"`, `"FILE_UPLOAD_FAILED"`).
2. **Domain codes** (`E####`) defined in the enums under `com/ratta/enums`. The
   same `E####` value can appear in more than one enum with a
   context-appropriate message; where they differ, both messages are shown.

All messages are reproduced verbatim from the source (they are already in
English in the enums).

## Transport / framework codes

| Code | HTTP status | Message | Cause |
|------|-------------|---------|-------|
| `400` | 200 | *(joined field-validation messages)* | Bean `@Valid` failure |
| `401` | 401 | `Unauthorized` | `InvalidTokenException` (missing/expired/invalid `x-access-token`) |
| `403 Forbidden` | 200 | `Content type not supported` | `HttpMediaTypeNotSupportedException` |
| `409` | 200 | `Please do not resubmit the data` | Duplicate submission caught by `@ResubmitCheck` |
| `422` | 200 | `Request Parameter Serialisation Exception` | `HttpMessageNotReadableException` (bad/unparseable body) |
| `500` | 200 | `The system is temporarily unable to process your request. Please try again later…` | Any uncaught exception |
| `FILE_UPLOAD_FAILED` | 500 | *(exception message)* | `FileUploadException` during multipart upload |
| *(plain text)* | 500 | *(exception message)* | `FileDownloadException` during download |

## Base / system codes — `BaseErrorCodeEnum`

| Code | Message |
|------|---------|
| E0701 | Operation failed! |
| E0702 | Deletion failed! |
| E0703 | Please delete the child nodes first before deleting the current node! |
| E0704 | ID cannot be empty! |
| E0705 | Modification failed! |
| E0706 | System error! |
| E0707 | There are still users under this role, deletion is not allowed! |
| E0708 | Incorrect username or password! |
| E0709 | The current user is in a disabled state. Please contact the administrator! |
| E0710 | The user is locked. Please contact the administrator or try logging in later! |
| E0711 | Incorrect username or password. Remaining login attempts |
| E0712 | You are not logged in or your login has expired. Please log in again! |
| E0713 | The password cannot be the same as the recent ones! |
| E0714 | The original password entered is incorrect! |
| E0715 | Enablement failed! |
| E0716 | A user cannot enable themselves! |
| E0717 | A user cannot disable themselves! |
| E0718 | Disabling failed! |
| E0719 | This user already has operation records and cannot be deleted! |
| E0720 | A user cannot delete themselves! |
| E0721 | Only locked users can be unlocked! |
| E0722 | No information found for this user! |
| E0723 | A user cannot authorize themselves! |
| E0724 | Authorization failed! |
| E0725 | Scheduled task disabling failed! |
| E0726 | Scheduled task enabling failed! |
| E0727 | The task is running! |
| E0728 | Data cleanup exception! |
| E0729 | Scheduled task execution exception. Please stop the task and restart it! |
| E0730 | Identical codes are not allowed under the same business code! |
| E0731 | The parameter already exists! |
| E0732 | Normal users are not allowed to be enabled again! |
| E0733 | The user already exists! |
| E0734 | Disabled users are not allowed to be disabled again! |
| E0735 | A user cannot unlock themselves! |
| E0736 | All data is in the enabled state. Please select a task that is not enabled! |
| E0737 | All data is in the disabled state. Please select a task that is not disabled! |
| E0738 | Please enable the task first! |
| E0739 | The request data is empty! |
| E0740 | Please delete all child roles under this role first! |
| E0741 | Please delete all child users under this user first! |
| E0742 | The system does not match the superior resources! |
| E0061 | The account has been cancelled! |
| E0062 | The phone number is empty! |
| E0064 | Too many SMS messages have been sent! |
| E0065 | Failed to send SMS! |
| E0066 | The phone number format is incorrect! |
| E0067 | Failed to upload the avatar! |
| E0068 | The number of copied files exceeds the limit! |
| E0069 | The device is invalid! |
| E0070 | No need to update! |
| E0071 | There is no compressed package! |
| E0072 | No operations are allowed under the supernote directory! |
| E0073 | The nickname cannot be empty! |
| E0074 | The nickname already exists. Please choose a new one! |
| E0075 | The device is already bound to this account. No need to bind again! |
| E0077 | The logged-in account is not the same as the one bound to the device! |
| E0078 | A device is currently synchronizing. Please wait until it's finished before synchronizing again! |
| E0079 | Synchronization is in progress. Please wait until it's finished before performing other operations! |
| E0081 | The path does not exist! |
| E0082 | There is a file with the same MD5 value. No need to upload! |
| E0083 | The device is already bound to another account. It cannot be bound to a new account! |
| E0084 | The published version number is incorrect! |
| E0085 | The token is invalid! |
| E0086 | The country code is empty! |
| E0087 | There is no latest version. No need to update. |
| E0088 | This resource is already in use by a role and cannot be deleted |
| E0844 | The time zone information for this area was not obtained |

## User codes — `UserErrorCodeEnum`

| Code | Message |
|------|---------|
| E0018 | Account does not exist |
| E0019 | Password error |
| E0045 | User has been locked. Please try again later! |
| E0070 | No need to update! |
| E0077 | The logged-in account is not the same as the account bound to the device! |
| E0078 | A device is currently synchronizing. Please wait until it finishes before synchronizing again! |
| E0101 | Verification code has expired |
| E0102 | Verification code error |
| E0103 | Phone number has been registered |
| E0104 | Phone number format is incorrect! |
| E0105 | Email has been registered |
| E0106 | Email format is incorrect! |
| E0107 | Nickname already exists. Please rename it! |
| E0108 | Account has been frozen! |
| E0109 | User does not exist! |
| E0110 | Country code is empty! |
| E0111 | Nickname already exists. Please rename it! |
| E0112 | Network error. Please try again |
| E0113 | File server has been selected. No need to select again |
| E0114 | Account is on the US server |
| E0115 | Account is currently in a frozen state and cannot be registered temporarily |
| E0116 | Account is on the Chinese server |
| E0117 | Phone number, email, and WeChat cannot all be empty |
| E0118 | Request to Ali failed |
| E0119 | Request to Alibaba succeeded, but data processing failed |
| E0120 | Registration is currently restricted |
| E0121 | User already exists, registration is not allowed |
| E0122 | Only admin can set registration restriction |
| E0123 | Only admin can query registration restriction |
| E0124 | MFA already enabled |
| E0125 | MFA not enabled |
| E0126 | MFA setup session expired |
| E0127 | MFA verification code invalid |
| E0128 | MFA login token expired |
| E0129 | Recovery code invalid or used |
| E1752 | Identity verification failed |
| E1201 | Email sending exception! |
| E1202 | SMS sending failed! |
| E0844 | Time zone information for this region was not obtained |
| E0845 | Failed to rename the directory using the email account name as the folder name. |
| E9999 | Network error |

## File / sync codes — `FileErrorCodeEnum`

| Code | Message |
|------|---------|
| E0078 | Sync in progress, please wait until it's completed before proceeding! |
| E0109 | User does not exist! |
| E0301 | Sync in progress, please wait until it's completed before proceeding! |
| E0302 | The root directory has been deleted |
| E0303 | The file or folder has been deleted |
| E0304 | A file or folder with the same name already exists |
| E0305 | The file or folder has been moved |
| E0306 | The target directory has been deleted |
| E0307 | Copying the files will exceed the total capacity! |
| E0308 | File does not exist |
| E0309 | Uploading the files will exceed the total capacity! |
| E0310 | There are identical md5 files already. No need to upload! |
| E0311 | The files in the recycle bin have been restored or permanently deleted! |
| E0312 | The device isn't linked to an account! |
| E0313 | Cannot be operated from the root directory! |
| E0314 | The QT program failed to parse the file! |
| E0315 | Incorrect file type |
| E0316 | This file is being converted |
| E0317 | The folder or the file directory you want to delete does not exist |
| E0318 | The folder or file you want to delete does not exist |
| E0319 | The folder or file directory you want to move or rename does not exist |
| E0320 | The folder or file you want to move or rename does not exist |
| E0321 | This file does not exist |
| E0322 | A file with the same name already exists |
| E0323 | Unable to migrate from a server outside of China to a server within China |
| E0324 | This file cannot be uploaded |
| E0325 | The byte length of the file name is too long |
| E0326 | The server has reached the migration limit |
| E0327 | Server migration failed |
| E0328 | The task group does not exist |
| E0329 | The root task does not exist |
| E0330 | NextSyncToken timeout |
| E0331 | This task group sorting already exists |
| E0332 | Unable to sync. Please upgrade to the latest version. |
| E0333 | Not enough Supernote Cloud storage. Please try deleting files from the recycle bin. |
| E0334 | The path cannot be empty. |
| E0335 | The tag name already exists. |
| E0336 | The tag does not exist. |
| E0337 | The tag name already exists. |
| E0338 | The unique identifier already exists. |
| E0339 | The summary library does not exist. |
| E0340 | The summary does not exist. |
| E0341 | An exception occurred while deleting the file |
| E0342 | Data has expired, please refresh the page or re-sync and try again. |
| E0343 | Rename failed |
| E0344 | Move failed |
| E0345 | Copy failed |
| E0346 | Unknown error |
| E0347 | The file or folder already exists in the target directory on the private storage disk |
| E0348 | The file access is denied on the private storage disk |
| E0349 | The file IO error on the private storage disk |
| E0350 | The file is too large on the private storage disk |
| E0351 | The directory access is denied on the private storage disk |
| E0352 | The directory IO error on the private storage disk |
| E0353 | The permission is denied on the private storage disk |
| E0354 | The invalid path on the private storage disk |
| E0355 | The chunk upload failed on the private storage disk |
| E0356 | The chunk merge failed on the private storage disk |
| E0357 | The chunk verify failed on the private storage disk |
| E0358 | Cannot move a folder into itself or its subdirectory |
| E9999 | Network error |

## Equipment / device codes — `EquipmentErrorCodeEnum`

| Code | Message |
|------|---------|
| E0018 | Incorrect account or password |
| E0019 | Incorrect account or password |
| E0045 | The user has been locked. Please try again later! |
| E0070 | No need to update! |
| E0077 | The logged-in account is not the same as the account bound to the device! |
| E0078 | A device is currently synchronizing. Please wait until it's finished before synchronizing again! |
| E0501 | Invalid device |
| E0502 | Account does not exist! |
| E0503 | The device is already bound to another account and cannot be bound to a new account again! |
| E0504 | This task does not exist on the device! |
| E0505 | The device version number cannot be empty! |
| E0506 | This device was not found in inventory! |
| E0550 | Only ordinary logs or reviewed error logs are allowed to be deleted! |
| E0551 | Adding remarks to un-downloaded records is not allowed! |
| E0552 | The remark exceeds the maximum number of characters! |
| E0553 | Failed to add a remark! |
| E0554 | Only viewed records are allowed to be reviewed! |
| E0555 | Operation failed! |
| E0556 | Device information does not exist! |
| E0557 | Download failed! |
| E0558 | Failed to add device logs! |
| E0560 | The device is not bound to an account |
| E0561 | Random number does not exist |
| E0562 | Random number has expired |
| E1202 | Failed to send SMS! |
| E1203 | Network error. Please try again |
| E1204 | Warranty period not found. Please contact the purchase channel to inquire about the warranty period |

## Object-storage codes — `OssErrorCodeEnum`

| Code | Message |
|------|---------|
| E1301 | Delete file failed. |
| E1302 | URL construction error. |
| E1303 | File does not exist. |
| E1304 | File is empty. |
| E1305 | File upload failed. |
| E1306 | Signature verification failed. |
| E1307 | File download failed. |
