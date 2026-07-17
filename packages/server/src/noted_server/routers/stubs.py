"""Static stubs for endpoints the device may call around login/sync.

These are outside the file-sync core scope but the device likely probes them
during its handshake; static success responses keep it from stalling. Real
payloads (if firmware turns out to need them) get shaped from debug captures.
"""

from typing import Any

from fastapi import APIRouter, Request

from noted_protocol.models.common import BaseVO, ok
from noted_server.auth.deps import SettingsDep

router = APIRouter()


@router.get("/api/file/query/server")
def query_server() -> BaseVO:
    """Connectivity ping — the first thing to verify from a real device."""
    return ok()


@router.post("/api/terminal/user/activateEquipment")
def activate_equipment(_request: Request) -> BaseVO:
    return ok()


@router.post("/api/terminal/user/bindEquipment")
def bind_equipment(_request: Request) -> BaseVO:
    return ok()


@router.post("/api/equipment/bind/status")
def bind_status() -> dict[str, Any]:
    return {"success": True, "errorCode": "0000", "isBind": "Y"}


@router.post("/api/terminal/equipment/unlink")
def unbind_equipment() -> BaseVO:
    return ok()


@router.post("/api/user/query/info")
def query_user_info(settings: SettingsDep) -> dict[str, Any]:
    return {
        "success": True,
        "errorCode": "0000",
        "userName": settings.user_name,
        "email": settings.account,
        # '0'/'1' select a public storage provider; unknown whether firmware
        # reads this on private servers — a capture-and-adjust candidate.
        "fileServer": settings.base_url,
    }


@router.post("/api/user/query")
def query_user(settings: SettingsDep) -> dict[str, Any]:
    return {
        "success": True,
        "errorCode": "0000",
        "userName": settings.user_name,
        "email": settings.account,
    }
