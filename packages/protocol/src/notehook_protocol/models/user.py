"""User DTO/VO models (docs/openapi/components/schemas/users-accounts.yaml)."""

from notehook_protocol.models.common import BaseVO, ProtocolModel


class UserCheckDTO(ProtocolModel):
    """Body of check/exists probes. The device sends only `email` (plus a
    `version` field the spec omits — ignored via the extras rule)."""

    email: str | None = None
    countryCode: str | None = None
    telephone: str | None = None
    userName: str | None = None
    domain: str | None = None


class UserCheckVO(BaseVO):
    dms: str | None = None
    userId: int | None = None
    uniqueMachineId: str | None = None
