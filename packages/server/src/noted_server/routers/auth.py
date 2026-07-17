"""Auth endpoints: random code, login, token validation, logout."""

import time
from typing import Annotated

from fastapi import APIRouter, Header

from noted_protocol.models.auth import (
    LoginDTO,
    LoginVO,
    QueryTokenVO,
    RandomCodeDTO,
    RandomCodeVO,
)
from noted_protocol.models.common import BaseVO, ok
from noted_server.auth.deps import AuthServiceDep, DbDep

router = APIRouter()


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
