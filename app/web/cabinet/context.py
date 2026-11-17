"""Shared authentication and dependencies for cabinet route factories.

No router is built here. Authentication retains its own short session and the
existing five-minute customer activity update; domain transactions stay in the
calling handler. This context never substitutes trusted-browser recognition for
an authenticated account session.
"""

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import BigInteger, cast, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config.settings import Settings
from app.database.models import AppSetting, User
from app.database.repositories import SQLRepository
from app.database.session import Database

STATIC = Path(__file__).resolve().parents[1] / "static"
SUPPORT_STORAGE = STATIC.parents[2] / "data" / "web-support"
COOKIE = "freeman_session"
SESSION_AGE = 30 * 86400
SESSION_DECRYPT_TTL = 31 * 86400
REVOKED_SESSION_PREFIX = "web_session_revoked:"


@dataclass(frozen=True, slots=True)
class WebContext:
    database: Database
    settings: Settings

    def cipher(self) -> Fernet:
        if not self.settings.encryption_key:
            raise RuntimeError("ENCRYPTION_KEY is required for web sessions")
        return Fernet(self.settings.encryption_key.encode())

    def session_token(self, user_id: int, version: int = 0) -> str:
        payload = json.dumps(
            {
                "user_id": user_id,
                "version": version,
                "expires": int(time.time()) + SESSION_AGE,
            }
        )
        return self.cipher().encrypt(payload.encode()).decode()

    def session_payload(self, token: str | None) -> dict:
        if not token:
            raise HTTPException(401, "Требуется вход")
        try:
            payload = json.loads(
                self.cipher().decrypt(token.encode(), ttl=SESSION_DECRYPT_TTL)
            )
            if (
                not isinstance(payload, dict)
                or type(payload.get("user_id")) is not int
                or type(payload.get("version", 0)) is not int
                or type(payload.get("expires")) not in (int, float)
                or not payload["expires"] >= time.time()
            ):
                raise ValueError("Invalid session claims")
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
            raise HTTPException(401, "Сессия истекла")
        return payload

    @staticmethod
    def revocation_key(token: str) -> str:
        # Store an irreversible fingerprint, never the bearer cookie itself.
        return REVOKED_SESSION_PREFIX + hashlib.sha256(token.encode()).hexdigest()

    async def revoke_session(self, token: str | None) -> None:
        try:
            payload = self.session_payload(token)
        except HTTPException:
            return  # Missing, malformed and expired cookies are already unusable.
        async with self.database.sessions.begin() as session:
            await SQLRepository(session).set_setting(
                self.revocation_key(token), f"{int(payload['expires']):020d}"
            )
            # Bounded cleanup of our expired markers; no user/settings scans.
            expired = select(AppSetting.key).where(
                AppSetting.key.startswith(REVOKED_SESSION_PREFIX, autoescape=True),
                AppSetting.value < f"{int(time.time()):020d}",
            ).limit(100)
            await session.execute(delete(AppSetting).where(AppSetting.key.in_(expired)))

    async def current_user(self, token: str | None) -> User:
        payload = self.session_payload(token)
        async with self.database.sessions() as session:
            if await session.get(AppSetting, self.revocation_key(token)):
                raise HTTPException(401, "Сеанс завершён")
            user = await session.get(User, payload["user_id"])
            if (
                not user
                or user.status == "blocked"
                or payload.get("version", 0) != user.auth_version
            ):
                raise HTTPException(401, "Профиль недоступен")
            if not self.settings.is_admin(user.telegram_id):
                stamp = str(int(time.time()))
                if session.bind.dialect.name == "postgresql":
                    statement = pg_insert(AppSetting).values(
                        key=f"client_seen:{user.id}", value=stamp
                    )
                    await session.execute(
                        statement.on_conflict_do_update(
                            index_elements=[AppSetting.key],
                            set_={"value": stamp},
                            where=cast(AppSetting.value, BigInteger) < int(stamp) - 300,
                        )
                    )
                else:
                    repo = SQLRepository(session)
                    old = await repo.setting(f"client_seen:{user.id}", "0")
                    if int(old) < int(stamp) - 300:
                        await repo.set_setting(f"client_seen:{user.id}", stamp)
                await session.commit()
            return user

    async def owner(self, token: str | None) -> User:
        user = await self.current_user(token)
        if not self.settings.is_owner(user.telegram_id):
            raise HTTPException(403, "Доступно только владельцу")
        return user
