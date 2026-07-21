"""Application errors mapped to the BaseVO failure envelope.

Error codes and messages are the authoritative ones from the reverse-engineered
spec catalogue (docs/server-spec/error-codes.md). Each ``AppError`` subclass
carries the canonical ``E####`` code and message for its situation; callers may
override the message (to add safe debugging detail) or pass ``code=`` to select
a context-specific variant from the same family.

Logical failures return HTTP 200 with {success: false, errorCode, errorMsg} —
the envelope-over-status convention this API family follows. The one documented
exception is an invalid/expired token, which the spec's GlobalExceptionHandler
returns as **HTTP 401** with errorCode "401" / errorMsg "Unauthorized"
(``TokenInvalid`` below sets ``http_status = 401``).
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from notehook_protocol.models.common import fail


class AppError(Exception):
    default_code: str = "E9999"
    default_msg: str = "Network error"
    http_status: int = 200

    def __init__(self, msg: str | None = None, *, code: str | None = None) -> None:
        self.code = code or self.default_code
        self.msg = msg or self.default_msg
        super().__init__(self.msg)


class AuthFailed(AppError):
    # UserErrorCodeEnum E0019 "Password error"; pass code="E0018" (account does
    # not exist) or code="E0561" (random number does not exist) for the variants.
    default_code = "E0019"
    default_msg = "Password error"


class TokenInvalid(AppError):
    # GlobalExceptionHandler InvalidTokenException -> HTTP 401, "401"/"Unauthorized".
    default_code = "401"
    default_msg = "Unauthorized"
    http_status = 401


class RateLimited(AppError):
    # E0045 "The user has been locked. Please try again later!"
    default_code = "E0045"
    default_msg = "The user has been locked. Please try again later!"


class SyncInProgress(AppError):
    # FileErrorCodeEnum E0079 (blocks other operations mid-sync); pass
    # code="E0078" for the "another device is already synchronizing" variant.
    default_code = "E0079"
    default_msg = (
        "Synchronization is in progress. Please wait until it's finished "
        "before performing other operations!"
    )


class NotFound(AppError):
    # FileErrorCodeEnum E0308 "File does not exist"
    default_code = "E0308"
    default_msg = "File does not exist"


class NameConflict(AppError):
    # E0304 "A file or folder with the same name already exists"
    default_code = "E0304"
    default_msg = "A file or folder with the same name already exists"


class InvalidName(AppError):
    # E0325 is the only name-specific code ("byte length of the file name is too
    # long"); it doubles as the bucket for otherwise-invalid names (slashes,
    # control chars), which the device should never send — this is a guard.
    default_code = "E0325"
    default_msg = "The byte length of the file name is too long"


class InvalidPath(AppError):
    # E0081 "The path does not exist!"; pass code="E0334" (path cannot be empty)
    # or code="E0354" (invalid path on the private storage disk) for variants.
    default_code = "E0081"
    default_msg = "The path does not exist!"


class UploadError(AppError):
    # OssErrorCodeEnum E1305 "File upload failed."
    default_code = "E1305"
    default_msg = "File upload failed."


class QuotaExceeded(AppError):
    # E0309 "Uploading the files will exceed the total capacity!"
    default_code = "E0309"
    default_msg = "Uploading the files will exceed the total capacity!"


class SignatureInvalid(AppError):
    # OssErrorCodeEnum E1306 "Signature verification failed."
    default_code = "E1306"
    default_msg = "Signature verification failed."


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content=fail(exc.code, exc.msg).model_dump(),
    )
