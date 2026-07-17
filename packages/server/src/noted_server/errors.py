"""Application errors mapped to the BaseVO failure envelope.

Logical failures return HTTP 200 with {success: false, errorCode, errorMsg} — the
envelope-over-status convention this API family follows. If real-device captures
show firmware branching on HTTP status instead, flip it in `app_error_handler`.

Error codes are our own invention (the spec only documents '0000' = success).
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from noted_protocol.models.common import fail


class AppError(Exception):
    def __init__(self, code: str, msg: str) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg


class AuthFailed(AppError):
    def __init__(self, msg: str = "authentication failed") -> None:
        super().__init__("1001", msg)


class TokenInvalid(AppError):
    def __init__(self, msg: str = "invalid or expired token") -> None:
        super().__init__("1002", msg)


class RateLimited(AppError):
    def __init__(self, msg: str = "too many attempts") -> None:
        super().__init__("1003", msg)


class NotFound(AppError):
    def __init__(self, msg: str = "not found") -> None:
        super().__init__("2001", msg)


class NameConflict(AppError):
    def __init__(self, msg: str = "name already exists") -> None:
        super().__init__("2002", msg)


class InvalidName(AppError):
    def __init__(self, msg: str = "invalid file or folder name") -> None:
        super().__init__("2003", msg)


class InvalidPath(AppError):
    def __init__(self, msg: str = "invalid path") -> None:
        super().__init__("2004", msg)


class UploadError(AppError):
    def __init__(self, msg: str) -> None:
        super().__init__("3001", msg)


class QuotaExceeded(AppError):
    def __init__(self, msg: str = "storage quota exceeded") -> None:
        super().__init__("3002", msg)


class SignatureInvalid(AppError):
    def __init__(self, msg: str = "invalid or expired signature") -> None:
        super().__init__("3003", msg)


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content=fail(exc.code, exc.msg).model_dump(),
    )
