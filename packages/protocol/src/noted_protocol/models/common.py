"""Common response envelope (specs/components/schemas/common.yaml)."""

from pydantic import BaseModel, ConfigDict


class ProtocolModel(BaseModel):
    """Base for all DTO/VO models.

    Real firmware may send fields the reverse-engineered spec doesn't know about;
    ignoring extras (while the debug-capture middleware logs the raw body) keeps
    the server lenient without losing visibility.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class BaseVO(ProtocolModel):
    success: bool = True
    errorCode: str | None = None
    errorMsg: str | None = None


def ok() -> BaseVO:
    return BaseVO(success=True, errorCode="0000")


def fail(code: str, msg: str) -> BaseVO:
    return BaseVO(success=False, errorCode=code, errorMsg=msg)
