"""Auth DTO/VO models (docs/openapi/components/schemas/authentication.yaml)."""

from notehook_protocol.models.common import BaseVO, ProtocolModel


class LoginDTO(ProtocolModel):
    password: str
    account: str
    equipment: int  # 1=Web, 2=App, 3=Terminal/Device, 4=Platform
    loginMethod: str  # "1"=Phone/Account, "2"=Email, "3"=WeChat
    countryCode: str | None = None
    browser: str | None = None
    language: str | None = None
    equipmentNo: str | None = None
    timestamp: int | None = None


class LoginVO(BaseVO):
    token: str | None = None
    counts: str | None = None
    userName: str | None = None
    avatarsUrl: str | None = None
    lastUpdateTime: str | None = None
    isBind: str | None = None
    isBindEquipment: str | None = None
    soldOutCount: int | None = None
    # MFA second-factor exchange (equipment 1/4 only; a device login, equipment
    # 3, never triggers it). When mfaRequired is true, token is empty and the
    # client calls login/mfa/verify with mfaToken (valid ~5 min).
    mfaRequired: bool | None = None
    mfaToken: str | None = None


class RandomCodeDTO(ProtocolModel):
    account: str
    countryCode: str | None = None


class RandomCodeVO(BaseVO):
    randomCode: str | None = None
    timestamp: int | None = None


class QueryTokenVO(BaseVO):
    token: str | None = None
