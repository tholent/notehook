"""Authentication: random-code nonces, login verification, token lifecycle.

Nonce cache and rate limiter are in-process: the server must run single-worker
(uvicorn default). Fine for a single-user personal server.
"""

import logging
import secrets
import time

from sqlmodel import Session, select

from notehook_protocol.crypto import login_hash_md5, login_hash_sha256
from notehook_server.config import Settings
from notehook_server.errors import AuthFailed, RateLimited, TokenInvalid
from notehook_server.models import AccessToken, Equipment, User, now_ms

logger = logging.getLogger(__name__)


class TTLStore:
    """Tiny single-use TTL map (nonce storage, replay guards)."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, tuple[float, str]] = {}

    def put(self, key: str, value: str) -> None:
        self._prune()
        self._items[key] = (time.monotonic() + self._ttl, value)

    def pop(self, key: str) -> str | None:
        self._prune()
        item = self._items.pop(key, None)
        return item[1] if item else None

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._items.items() if exp < now]
        for k in expired:
            del self._items[k]


class _RateLimiter:
    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < 60]
        if len(hits) >= self._max:
            raise RateLimited()
        hits.append(now)
        self._hits[key] = hits


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._nonces = TTLStore(settings.random_code_ttl_seconds)
        self._limiter = _RateLimiter(settings.login_attempts_per_minute)

    def issue_random_code(self, account: str) -> str:
        self._limiter.check(f"rc:{account}")
        code = secrets.token_hex(16)
        self._nonces.put(account.lower(), code)
        return code

    def get_or_create_user(self, session: Session) -> User:
        user = session.exec(
            select(User).where(User.account == self._settings.account)
        ).one_or_none()
        if user is None:
            user = User(account=self._settings.account, user_name=self._settings.user_name)
            session.add(user)
            session.commit()
            session.refresh(user)
        return user

    def login(
        self,
        session: Session,
        account: str,
        password_hash: str,
        equipment_no: str,
        equipment_type: int,
    ) -> tuple[str, User]:
        self._limiter.check(f"login:{account}")
        if account.lower() != self._settings.account.lower():
            raise AuthFailed(code="E0018", msg="Account does not exist")

        random_code = self._nonces.pop(account.lower())
        if random_code is None:
            raise AuthFailed(code="E0561", msg="Random number does not exist")

        pw_md5 = self._settings.password_md5
        candidates = {
            "sha256": login_hash_sha256(pw_md5, random_code),
            "md5": login_hash_md5(pw_md5, random_code),
        }
        matched = next(
            (
                scheme
                for scheme, expected in candidates.items()
                if secrets.compare_digest(expected, password_hash.lower())
            ),
            None,
        )
        if matched is None:
            raise AuthFailed()  # canonical E0019 "Password error"
        # Which scheme real firmware uses is unknown until observed — record it.
        logger.info(
            "login ok for %s using %s scheme (equipment=%s)", account, matched, equipment_no
        )

        user = self.get_or_create_user(session)
        equipment = session.exec(
            select(Equipment).where(Equipment.equipment_no == equipment_no)
        ).one_or_none()
        if equipment is None:
            equipment = Equipment(
                equipment_no=equipment_no,
                user_id=user.id or 0,
                equipment_type=equipment_type,
            )
            session.add(equipment)
        equipment.last_seen_at = now_ms()
        equipment.equipment_type = equipment_type
        session.add(equipment)
        session.commit()
        session.refresh(equipment)

        token = secrets.token_urlsafe(32)
        session.add(
            AccessToken(
                token=token,
                user_id=user.id or 0,
                equipment_id=equipment.id or 0,
                expires_at=now_ms() + self._settings.token_ttl_seconds * 1000,
            )
        )
        session.commit()
        return token, user

    def validate_token(self, session: Session, token: str) -> AccessToken:
        record = session.exec(
            select(AccessToken).where(AccessToken.token == token)
        ).one_or_none()
        if record is None or record.revoked or record.expires_at < now_ms():
            raise TokenInvalid()
        return record

    def revoke_token(self, session: Session, token: str) -> None:
        record = session.exec(
            select(AccessToken).where(AccessToken.token == token)
        ).one_or_none()
        if record is not None:
            record.revoked = True
            session.add(record)
            session.commit()
