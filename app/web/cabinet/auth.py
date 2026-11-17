from app.services.accounts import freeman_id_aliases, display_freeman_id
"""Telegram and bot login, logout and the deprecated email-binding endpoint."""

import hashlib
import hmac
import re
import time

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.database.models import Referral, User
from app.database.repositories import SQLRepository
from app.services.web_login import consume as consume_web_login
from app.web.cabinet.context import COOKIE, WebContext
from app.web.cabinet.schemas import BotLoginPayload, ProfileEmailPayload


def auth_router(context: WebContext) -> APIRouter:
    database = context.database
    settings = context.settings
    current_user = context.current_user
    session_token = context.session_token
    router = APIRouter()

    @router.get("/auth/telegram/callback", include_in_schema=False)
    async def telegram_callback(request: Request):
        values = dict(request.query_params)

        state = values.pop("state", "")
        from app.services import telegram_auth

        browser_secret = request.cookies.get(telegram_auth.COOKIE, "")
        if not state or not browser_secret:
            return RedirectResponse("/?auth=invalid", status_code=303)
        received = values.pop("hash", "")

        if (
            not received
            or not values.get("id", "").isdigit()
            or not values.get("auth_date", "").isdigit()
        ):
            return RedirectResponse("/?auth=invalid", status_code=303)

        check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))

        secret = hashlib.sha256(settings.bot_token.encode()).digest()

        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()

        if (
            not hmac.compare_digest(received, expected)
            or abs(time.time() - int(values["auth_date"])) > 600
        ):
            return RedirectResponse("/?auth=expired", status_code=303)

        telegram_id = int(values["id"])

        async with database.sessions.begin() as session:
            row = await telegram_auth.challenge(session, state)
            if (
                not row
                or row.user_id is not None
                or not hmac.compare_digest(
                    row.code_hash, telegram_auth.digest(browser_secret)
                )
            ):
                return RedirectResponse("/?auth=expired", status_code=303)
            repo = SQLRepository(session)

            user = await session.scalar(
                select(User).where(User.telegram_id == telegram_id)
            )

            if not user:
                referrer_name = request.cookies.get("freeman_referrer", "")

                inviter = (
                    await session.scalar(
                        select(User).where(
                            User.freeman_id.in_(freeman_id_aliases(referrer_name)),
                            User.status == "active",
                        )
                    )
                    if referrer_name
                    else None
                )

                user = User(
                    telegram_id=telegram_id,
                    username=values.get("username") or None,
                    first_name=(values.get("first_name") or "Пользователь")[:255],
                    referrer_id=inviter.id if inviter else None,
                )

                await repo.add(user)

                if inviter:
                    await repo.add(Referral(inviter_id=inviter.id, invitee_id=user.id))

            else:
                user.username = values.get("username") or user.username

                user.first_name = (values.get("first_name") or user.first_name)[:255]

            from app.services.accounts import ensure_account

            await ensure_account(repo, user)

            if values.get("photo_url"):
                await repo.set_setting(
                    f"user_avatar:{user.id}", values["photo_url"][:1000]
                )

            if not await repo.setting(f"freeman_id:{user.id}"):
                await repo.set_setting(f"freeman_id:{user.id}", display_freeman_id(user.freeman_id) or f"FO-{user.id}")

            if user.status != "active":
                return RedirectResponse("/?auth=invalid", status_code=303)
            if not await telegram_auth.approve(session, state, user):
                return RedirectResponse("/?auth=expired", status_code=303)
            await telegram_auth.consume(session, state, browser_secret)
            user_id = user.id

            version = user.auth_version

        response = RedirectResponse("/", status_code=303)

        response.set_cookie(
            COOKIE,
            session_token(user_id, version),
            max_age=86400 * 30,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )

        response.delete_cookie(telegram_auth.COOKIE, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    async def authenticate_bot_code(login: str, code: str):
        match = re.fullmatch(r"(?:FO-|FRM-)(\d{1,12})", login.strip().upper())
        if not match:
            return None
        async with database.sessions.begin() as session:
            from app.services.accounts import find_freeman_id

            user = await find_freeman_id(session, login.strip())
            if not user or user.status == "blocked":
                return None
            if not await consume_web_login(session, user.id, code.strip()):
                return None
            return user.id, user.auth_version

    def bot_login_response(user_id: int, version=0):
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE,
            session_token(user_id, version),
            max_age=86400 * 30,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @router.get("/auth/bot/support", include_in_schema=False)
    async def bot_support_link(
        mode: str, token: str = "", freeman_session: str | None = Cookie(default=None)
    ):
        from fastapi.responses import HTMLResponse

        from app.services.web_login import consume_direct

        if mode not in ("new", "list", "staff"):
            raise HTTPException(422, "Раздел недоступен")
        existing = None
        if freeman_session:
            try:
                existing = await current_user(freeman_session)
            except HTTPException:
                pass
        async with database.sessions.begin() as session:
            identity = await consume_direct(
                session, settings, token, purpose="direct_support:" + mode
            )
        if not identity and existing:
            identity = (existing.id, existing.auth_version)
        if not identity:
            return HTMLResponse(
                "Ссылка истекла. Откройте поддержку заново в боте.",
                status_code=403,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        if mode == "staff":
            async with database.sessions() as session:
                staff = await session.get(User, identity[0])
                if not staff or not settings.is_admin(staff.telegram_id):
                    raise HTTPException(403, "Недостаточно прав")
        response = bot_login_response(*identity)
        response.headers["location"] = f"/?support_mode={mode}&miniapp=1"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @router.get("/auth/bot/chat", include_in_schema=False)
    async def bot_chat_link(
        ticket: int, token: str = "", freeman_session: str | None = Cookie(default=None)
    ):
        from fastapi.responses import HTMLResponse

        from app.database.models import Ticket
        from app.services.web_login import consume_direct

        existing = None
        if freeman_session:
            try:
                existing = await current_user(freeman_session)
            except HTTPException:
                pass
        async with database.sessions.begin() as session:
            identity = await consume_direct(
                session, settings, token, purpose=f"direct_chat:{ticket}"
            )
            if not identity and existing:
                identity = (existing.id, existing.auth_version)
            item = await session.get(Ticket, ticket)
            if not identity or not item or item.user_id != identity[0]:
                return HTMLResponse(
                    "Ссылка истекла или обращение недоступно. Откройте его заново в боте.",
                    status_code=403,
                    headers={
                        "Cache-Control": "no-store",
                        "Referrer-Policy": "no-referrer",
                    },
                )
        response = bot_login_response(*identity)
        response.headers["location"] = f"/?support_ticket={ticket}&miniapp=1"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @router.get("/auth/bot", include_in_schema=False)
    async def bot_login_link(login: str = "", code: str = "", token: str = ""):
        if token:
            from app.services.web_login import consume_direct

            async with database.sessions.begin() as session:
                user_id = await consume_direct(session, settings, token)
        else:
            user_id = await authenticate_bot_code(login, code)
        if user_id is None:
            return RedirectResponse(
                "/?auth=expired",
                status_code=303,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        return bot_login_response(*user_id)

    @router.post("/auth/bot/code", include_in_schema=False)
    async def bot_login_form(payload: BotLoginPayload):
        user_id = await authenticate_bot_code(payload.login, payload.code)
        if user_id is None:
            raise HTTPException(401, "Freeman ID или код неверен либо истёк")
        return bot_login_response(*user_id)

    @router.post("/api/web/logout")
    async def logout(request: Request):
        freeman_session = request.cookies.get(COOKIE)
        try:
            await context.revoke_session(freeman_session)
        except Exception:
            # Do not claim a successful logout if server-side revocation failed.
            raise HTTPException(503, "Не удалось завершить сеанс. Повторите попытку.")
        from app.web.admin_digest import revoke_session_push
        import asyncio
        try:
            await asyncio.wait_for(revoke_session_push(database, settings, freeman_session), 2)
        except Exception:
            pass  # Push cleanup cannot prevent an already revoked session from exiting.
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.put("/api/web/profile/email")
    async def update_profile_email(
        payload: ProfileEmailPayload, freeman_session: str | None = Cookie(default=None)
    ):
        await current_user(freeman_session)
        raise HTTPException(409, "Для привязки email требуется код подтверждения.")

    return router
