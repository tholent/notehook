"""FastAPI dependencies: db session, services, current-equipment auth."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlmodel import Session

from notehook_server.auth.service import AuthService
from notehook_server.config import Settings
from notehook_server.errors import TokenInvalid
from notehook_server.models import AccessToken, Equipment, User


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_auth_service(request: Request) -> AuthService:
    service: AuthService = request.app.state.auth_service
    return service


def get_db(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session


@dataclass
class AuthContext:
    token: AccessToken
    user: User
    equipment: Equipment


def get_current(
    db: Annotated[Session, Depends(get_db)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    x_access_token: Annotated[str | None, Header()] = None,
) -> AuthContext:
    if not x_access_token:
        raise TokenInvalid("missing x-access-token header")
    record = auth.validate_token(db, x_access_token)
    user = db.get(User, record.user_id)
    equipment = db.get(Equipment, record.equipment_id)
    if user is None or equipment is None:
        raise TokenInvalid()
    return AuthContext(token=record, user=user, equipment=equipment)


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
DbDep = Annotated[Session, Depends(get_db)]
CurrentDep = Annotated[AuthContext, Depends(get_current)]
