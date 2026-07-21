"""Auth endpoints: random code, login, token validation, logout."""

import hashlib
import time
from typing import Annotated

from fastapi import APIRouter, Header

from notehook_protocol.models.auth import (
    LoginDTO,
    LoginVO,
    QueryTokenVO,
    RandomCodeDTO,
    RandomCodeVO,
)
from notehook_protocol.models.common import BaseVO, ok
from notehook_protocol.models.user import UserCheckDTO, UserCheckVO
from notehook_server.auth.deps import AuthServiceDep, DbDep, SettingsDep

router = APIRouter()


@router.post("/api/official/user/check/exists/server")
def check_exists_server(
    dto: UserCheckDTO, db: DbDep, auth: AuthServiceDep, settings: SettingsDep
) -> UserCheckVO:
    """Pre-login account probe the device fires before random/code. It sends
    `{"email": ...}` and expects a UserCheckVO; the catch-all's 9999 stalls the
    handshake here. Single-user server: only the configured account exists."""
    email = (dto.email or "").strip().lower()
    if not email or email != settings.account.lower():
        # Mirror the login envelope for an unknown account (UserErrorCodeEnum
        # E0018) rather than fabricating a user for an email we don't serve.
        return UserCheckVO(success=False, errorCode="E0018", errorMsg="Account does not exist")
    user = auth.get_or_create_user(db)
    return UserCheckVO(
        success=True,
        errorCode="0000",
        userId=user.id,
        # `dms` is the real cloud's regional data-center id ("ALL"/"CN"/"US").
        # "ALL" = no regional redirect. This is the #1 capture-and-adjust
        # candidate: if the device's *next* request goes to a different host,
        # firmware is deriving that host from `dms` and this value is wrong.
        dms="ALL",
        # Stable per-server id, derived from (without leaking) the persisted
        # secret_key so it survives restarts but reveals nothing about the key.
        uniqueMachineId=hashlib.sha256(settings.secret_key.encode()).hexdigest()[:32],
    )


@router.post("/api/official/user/query/random/code")
def random_code(dto: RandomCodeDTO, auth: AuthServiceDep) -> RandomCodeVO:
    code = auth.issue_random_code(dto.account)
    return RandomCodeVO(
        success=True, errorCode="0000", randomCode=code, timestamp=int(time.time() * 1000)
    )


def _login(dto: LoginDTO, db: DbDep, auth: AuthServiceDep) -> LoginVO:
    token, user = auth.login(
        db,
        account=dto.account,
        password_hash=dto.password,
        equipment_no=dto.equipmentNo or f"UNKNOWN-{dto.equipment}",
        equipment_type=dto.equipment,
    )
    return LoginVO(
        success=True,
        errorCode="0000",
        token=token,
        userName=user.user_name,
        isBind="Y",
        isBindEquipment="Y",
    )


@router.post("/api/official/user/account/login/equipment")
def login_equipment(dto: LoginDTO, db: DbDep, auth: AuthServiceDep) -> LoginVO:
    return _login(dto, db, auth)


@router.post("/api/official/user/account/login/new")
def login_new(dto: LoginDTO, db: DbDep, auth: AuthServiceDep) -> LoginVO:
    return _login(dto, db, auth)


@router.post("/api/user/query/token")
def query_token(
    db: DbDep,
    auth: AuthServiceDep,
    x_access_token: Annotated[str | None, Header()] = None,
) -> QueryTokenVO:
    # Spec omits a security block here (likely a reverse-engineering gap) —
    # validated manually so an unauthenticated call fails cleanly.
    record = auth.validate_token(db, x_access_token or "")
    return QueryTokenVO(success=True, errorCode="0000", token=record.token)


@router.post("/api/user/logout")
def logout(
    db: DbDep,
    auth: AuthServiceDep,
    x_access_token: Annotated[str | None, Header()] = None,
) -> BaseVO:
    if x_access_token:
        auth.revoke_token(db, x_access_token)
    return ok()
