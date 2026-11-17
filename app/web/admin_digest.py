"""Owner daily summaries and customer publication Web Push with durable leases.

No billing mutations. Moscow calendar days; external HTTP never holds a DB session.
"""

import asyncio
import base64
import hashlib
import importlib.util
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, case, cast, func, select, text
from app.database.models import AppSetting, User, Publication, Withdrawal, now
from app.database.repositories import SQLRepository
from app.database.schema_readiness import inspect_schema
from app.web.access import require_business_admin

LOG = logging.getLogger(__name__)
LOCK = 186001
PREFIX = "admin_digest:"


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(value):
    if not isinstance(value, str) or len(value) > 1024:
        raise ValueError("Invalid push key")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except Exception as exc:
        raise ValueError("Invalid push key") from exc


def valid_subscription(data):
    endpoint = data.get("endpoint", "")
    if (
        not isinstance(endpoint, str)
        or len(endpoint) > 2048
        or any(c.isspace() for c in endpoint)
    ):
        raise ValueError("Некорректный адрес push")
    u = urlsplit(endpoint)
    host = u.hostname or ""
    allowed = host in {
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
    } or (host.endswith(".notify.windows.com") and host != "notify.windows.com")
    if (
        u.scheme != "https"
        or not allowed
        or u.username
        or u.password
        or u.fragment
        or u.port not in (None, 443)
    ):
        raise ValueError("Неизвестный сервис push")
    keys = data.get("keys") or {}
    try:
        public = unb64(keys.get("p256dh"))
        auth = unb64(keys.get("auth"))
        if len(public) != 65 or len(auth) != 16:
            raise ValueError("Invalid lengths")
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    except Exception as exc:
        raise ValueError("Некорректные ключи push") from exc
    return dict(endpoint=endpoint, keys=dict(p256dh=b64(public), auth=b64(auth)))


async def locked(s):
    if s.bind.dialect.name == "postgresql":
        return bool(
            await s.scalar(
                text("SELECT pg_try_advisory_xact_lock(186001, :key)"), dict(key=LOCK)
            )
        )
    return True


def digest_summary(report, day):
    # SQL snapshots already exclude test/deleted sales and count actual paid totals.
    from app.web.admin_reports import TZ

    plans = report.get("tariffs", [])
    popular = max(plans, key=lambda p: (p["sales"], p["revenue"]), default=None)
    stats = report["stats"]
    money = lambda n: (
        format(int(n) / 100, ",.2f").replace(",", " ").rstrip("0").rstrip(".") + " ₽"
    )
    data = {
        k: int(stats.get(k, 0))
        for k in ("sales", "revenue", "new_customers", "buyers", "average", "renewals")
    }
    data.update(
        day=day.isoformat(),
        timezone=str(TZ),
        popular=popular["name"] if popular else None,
        sales_growth=stats.get("sales_growth"),
        revenue_growth=stats.get("revenue_growth"),
    )
    title = "Итоги " + day.strftime("%d.%m.%Y")
    body = f"Выручка {money(data['revenue'])} · продаж {data['sales']} · новых клиентов {data['new_customers']}"
    if data["popular"]:
        body += " · популярный тариф: " + data["popular"]
    return dict(
        id=day.isoformat(),
        title=title,
        text=body,
        target="admin-reports",
        summary=data,
        created_at=now().isoformat(),
    )


async def generate_due(database, settings, clock=None):
    from app.web.admin_reports import TZ, build_report

    today = (clock or now()).astimezone(TZ).date()
    due = today - timedelta(days=1)
    async with database.sessions.begin() as s:
        if not await locked(s):
            return None
        repo = SQLRepository(s)
        cursor = await repo.setting(PREFIX + "cursor")
        if not cursor:
            await repo.set_setting(PREFIX + "cursor", due.isoformat())
            await repo.set_setting(PREFIX + "activated", today.isoformat())
            return None  # Start tonight; do not flood the inbox with old reports.
        day = date.fromisoformat(cursor) + timedelta(days=1)
        if day > due:
            return None
        key = PREFIX + "report:" + day.isoformat()
        existing = await s.get(AppSetting, key)
        if not existing:
            lo = datetime.combine(day, datetime.min.time(), TZ)
            hi = lo + timedelta(days=1)
            report = await build_report(s, lo, hi, limit=10)
            notice = digest_summary(report, day)
            await repo.set_setting(key, json.dumps(notice, ensure_ascii=False))
            subscriptions = list(
                await s.scalars(
                    select(AppSetting)
                    .where(AppSetting.key.startswith(PREFIX + "subscription:"))
                    .limit(500)
                )
            )
            for row in subscriptions:
                meta = json.loads(row.value)
                user = await s.get(User, meta["user_id"])
                if (
                    not user
                    or user.status != "active"
                    or not settings.is_owner(user.telegram_id)
                    or user.auth_version != meta["auth_version"]
                ):
                    continue
                job = (
                    PREFIX
                    + "delivery:"
                    + day.isoformat()
                    + ":"
                    + row.key.split(":")[-1]
                )
                if not await s.get(AppSetting, job):
                    await repo.set_setting(
                        job,
                        json.dumps(
                            dict(
                                subscription=row.key,
                                report=key,
                                state="pending",
                                attempts=0,
                                available=0,
                            )
                        ),
                    )
        await repo.set_setting(PREFIX + "cursor", day.isoformat())
        return day.isoformat()


async def push_keys(s, settings):
    cipher = Fernet(settings.encryption_key.encode())
    row = await s.get(AppSetting, PREFIX + "vapid")
    if row:
        return json.loads(cipher.decrypt(row.value.encode()))
    private = ec.generate_private_key(ec.SECP256R1())
    data = dict(
        private=b64(
            private.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        ),
        public=b64(
            private.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        ),
    )
    await SQLRepository(s).set_setting(
        PREFIX + "vapid", cipher.encrypt(json.dumps(data).encode()).decode()
    )
    return data


async def send_webpush(subscription, payload, keys, settings):
    from pywebpush import webpush_async
    import aiohttp

    # Standard encrypted Web Push. Strict vendor endpoints; never follow redirects.
    class NoRedirectSession:
        def __init__(self, client):
            self.client = client

        def post(self, url, **kwargs):
            kwargs["allow_redirects"] = False
            return self.client.post(url, **kwargs)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as client:
        return await webpush_async(
            subscription_info=subscription,
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=keys["private"],
            vapid_claims={"sub": settings.web_base_url.rstrip("/")},
            ttl=86400,
            timeout=10,
            headers={
                "Topic": b64(hashlib.sha256(payload["id"].encode()).digest()[:16]),
                "Urgency": "normal",
            },
            aiohttp_session=NoRedirectSession(client),
        )


async def deliver_one(database, settings, sender=send_webpush, clock=None):
    stamp = (clock or now()).timestamp()
    claimed = None
    async with database.sessions.begin() as s:
        if not await locked(s):
            return False
        data = cast(
            case(
                (AppSetting.key.startswith(PREFIX + "delivery:"), AppSetting.value),
                else_="{}",
            ),
            JSON,
        )
        rows = await s.scalars(
            select(AppSetting)
            .where(
                AppSetting.key.startswith(PREFIX + "delivery:"),
                data["state"].as_string().in_(["pending", "sending"]),
                func.coalesce(data["available"].as_float(), 0) <= stamp,
            )
            .order_by(AppSetting.key)
            .limit(50)
        )
        for row in rows:
            job = json.loads(row.value)
            if (
                job["state"] not in ("pending", "sending")
                or job.get("available", 0) > stamp
            ):
                continue
            subrow = await s.get(AppSetting, job["subscription"])
            report = await s.get(AppSetting, job["report"])
            meta = json.loads(subrow.value) if subrow else {}
            user = await s.get(User, meta.get("user_id", 0))
            if (
                not user
                or user.status != "active"
                or (
                    not job.get("publication_id")
                    and not settings.is_owner(user.telegram_id)
                )
                or user.auth_version != meta.get("auth_version")
                or meta.get("expires", 0) <= stamp
                or not report
            ):
                job["state"] = "cancelled"
                row.value = json.dumps(job)
                continue
            if job.get("withdrawal_id"):
                withdrawal = await s.get(Withdrawal,job["withdrawal_id"])
                if not withdrawal or withdrawal.status!="pending":
                    job["state"]="cancelled"
                    row.value=json.dumps(job)
                    continue
            if job.get("publication_id"):
                publication = await s.get(Publication, job["publication_id"])
                if (
                    not publication
                    or publication.status != "published"
                    or not publication.notify_clients
                ):
                    job["state"] = "cancelled"
                    row.value = json.dumps(job)
                    continue
            token = secrets.token_hex(12)
            job.update(
                state="sending",
                lease=token,
                available=stamp + 60,
                attempts=job["attempts"] + 1,
            )
            row.value = json.dumps(job)
            subscription = json.loads(
                Fernet(settings.encryption_key.encode()).decrypt(meta["data"].encode())
            )
            valid_subscription(subscription)
            keys = await push_keys(s, settings)
            payload = json.loads(report.value)
            claimed = (row.key, token, job, subscription, payload, keys)
            break
    if not claimed:
        return False
    key, token, job, subscription, payload, keys = claimed
    status = 0
    try:
        response = await sender(
            subscription,
            dict(
                id=payload["id"],
                title=payload["title"],
                text=payload["text"],
                url=payload.get("url") or "/?daily_report=" + payload["id"],
                kind="publication" if job.get("publication_id") else payload.get("kind", "daily"),
            ),
            keys,
            settings,
        )
        status = getattr(response, "status", getattr(response, "status_code", 0))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        status = (
            getattr(exc, "status_code", 0)
            or getattr(getattr(exc, "response", None), "status", 0)
            or getattr(getattr(exc, "response", None), "status_code", 0)
        )
        LOG.warning(
            "Daily Web Push deferred (status %s)", status
        )  # Never log endpoints/keys/payloads.
    async with database.sessions.begin() as s:
        row = await s.scalar(
            select(AppSetting).where(AppSetting.key == key).with_for_update()
        )
        current = json.loads(row.value) if row else {}
        if current.get("lease") != token:
            return True
        if 200 <= status < 300:
            current["state"] = "sent"
        elif status in (404, 410):
            current["state"] = "expired"
            sub = await s.get(AppSetting, job["subscription"])
            if sub:
                await s.delete(sub)
        else:
            current.update(
                state="failed" if current["attempts"] >= 6 else "pending",
                available=stamp + min(3600, 30 * 2 ** current["attempts"]),
            )
        row.value = json.dumps(current)
    return True


async def worker(database, settings):
    from app.web.admin_reports import TZ

    ready = False
    while True:
        try:
            if not ready:
                ready = (await inspect_schema(database.engine)).ready
            if ready:
                await asyncio.wait_for(generate_due(database, settings), 30)
                from app.web.admin_referrals import generate_payout_notices
                await asyncio.wait_for(generate_payout_notices(database, settings), 15)
                for _ in range(10):
                    if not await deliver_one(database, settings):
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("Daily report worker deferred; will retry")
        current = now()
        local = current.astimezone(TZ)
        midnight = datetime.combine(
            local.date() + timedelta(days=1), datetime.min.time(), TZ
        )
        await asyncio.sleep(min(30, max(0.1, (midnight - current).total_seconds())))


class PushBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(max_length=2048)
    keys: dict[str, str]
    expirationTime: float | None = Field(
        default=None, ge=0, strict=True, allow_inf_nan=False
    )


class EndpointBody(BaseModel):
    endpoint: str = Field(max_length=2048)


def digest_router(database, settings, current_user):
    @asynccontextmanager
    async def lifespan(_):
        task = asyncio.create_task(
            worker(database, settings), name="website-daily-digest"
        )
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    router = APIRouter(lifespan=lifespan)

    async def actor(token):
        return require_business_admin(await current_user(token), settings)

    def origin(request):
        if (
            request.headers.get("origin") != settings.web_base_url.rstrip("/")
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            raise HTTPException(403, "Откройте страницу на сайте Freeman ONE.")

    @router.get("/admin-notifications-sw.js")
    async def service_worker():
        return FileResponse(
            Path(__file__).with_name("static") / "admin-notifications-sw.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @router.get("/admin-manifest.webmanifest")
    async def manifest():
        return JSONResponse(
            dict(
                id="/freeman-admin",
                name="Freeman ONE",
                short_name="Freeman",
                start_url="/",
                scope="/",
                display="standalone",
                background_color="#f6f7f9",
                theme_color="#f6f7f9",
                icons=[
                    dict(src="/assets/freeman-icon-20261029.png", sizes="256x256", type="image/png")
                ],
            )
        )

    @router.get("/api/web/admin/digests")
    async def listing(freeman_session: str | None = Cookie(default=None)):
        user = await actor(freeman_session)
        async with database.sessions() as s:
            rows = await s.scalars(
                select(AppSetting)
                .where(AppSetting.key.startswith(PREFIX + "report:"))
                .order_by(func.coalesce(cast(case((AppSetting.key.startswith(PREFIX+"report:"),AppSetting.value),else_="{}"),JSON)["created_at"].as_string(),cast(case((AppSetting.key.startswith(PREFIX+"report:"),AppSetting.value),else_="{}"),JSON)["day"].as_string()).desc(),AppSetting.key.desc())
                .limit(30)
            )
            items = []
            for row in rows:
                value = json.loads(row.value)
                value["read"] = bool(
                    await s.get(AppSetting, PREFIX + f"read:{user.id}:" + value["id"])
                )
                items.append(value)
        return JSONResponse(
            dict(items=items, timezone="Europe/Moscow"),
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/web/admin/digests/{day}/read")
    async def read(
        day: str, request: Request, freeman_session: str | None = Cookie(default=None)
    ):
        origin(request)
        user = await (
            actor(freeman_session)
            if request.url.path.startswith("/api/web/admin/")
            else current_user(freeman_session)
        )
        try:
            if not re.fullmatch(r"(?:payout-[0-9]+|test-[a-f0-9]{32})",day):day = date.fromisoformat(day).isoformat()
        except ValueError as exc:
            raise HTTPException(422, "Некорректная дата") from exc
        async with database.sessions.begin() as s:
            if not await s.get(AppSetting, PREFIX + "report:" + day):
                raise HTTPException(404, "Отчёт не найден")
            await SQLRepository(s).set_setting(
                PREFIX + f"read:{user.id}:" + day, now().isoformat()
            )
        return {"ok": True}

    @router.get("/api/web/notifications/push/config")
    @router.get("/api/web/admin/push/config")
    async def config(
        request: Request, freeman_session: str | None = Cookie(default=None)
    ):
        await (
            actor(freeman_session)
            if request.url.path.startswith("/api/web/admin/")
            else current_user(freeman_session)
        )
        if importlib.util.find_spec("pywebpush") is None:
            return dict(available=False, reason="Push service is not installed")
        async with database.sessions.begin() as s:
            if not await locked(s):
                raise HTTPException(503, "Повторите запрос")
            keys = await push_keys(s, settings)
        return JSONResponse(
            dict(available=True, public_key=keys["public"]),
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/web/notifications/push/subscription")
    @router.post("/api/web/admin/push/subscription")
    async def subscribe(
        payload: PushBody,
        request: Request,
        freeman_session: str | None = Cookie(default=None),
    ):
        origin(request)
        user = await (
            actor(freeman_session)
            if request.url.path.startswith("/api/web/admin/")
            else current_user(freeman_session)
        )
        try:
            data = valid_subscription(payload.model_dump())
            claims = json.loads(
                Fernet(settings.encryption_key.encode()).decrypt(
                    freeman_session.encode()
                )
            )
            if int(claims["user_id"]) != user.id:
                raise ValueError("Wrong owner")
        except Exception as exc:
            raise HTTPException(422, "Не удалось проверить push-подписку") from exc
        endpoint_hash = hashlib.sha256(data["endpoint"].encode()).hexdigest()
        key = PREFIX + f"subscription:{user.id}:" + endpoint_hash
        async with database.sessions.begin() as s:
            if not await locked(s):
                raise HTTPException(503, "Повторите запрос")
            rows = list(
                await s.scalars(
                    select(AppSetting).where(
                        AppSetting.key.startswith(PREFIX + f"subscription:{user.id}:")
                    )
                )
            )
            if len(rows) >= 10 and not any(r.key == key for r in rows):
                raise HTTPException(422, "Не более 10 устройств")
            await SQLRepository(s).set_setting(
                key,
                json.dumps(
                    dict(
                        user_id=user.id,
                        auth_version=user.auth_version,
                        session_hash=hashlib.sha256(
                            freeman_session.encode()
                        ).hexdigest(),
                        expires=min(claims["expires"], payload.expirationTime / 1000)
                        if payload.expirationTime is not None
                        else claims["expires"],
                        data=Fernet(settings.encryption_key.encode())
                        .encrypt(json.dumps(data).encode())
                        .decode(),
                    )
                ),
            )
        return {"ok": True}

    @router.delete("/api/web/notifications/push/subscription")
    @router.delete("/api/web/admin/push/subscription")
    async def unsubscribe(
        payload: EndpointBody,
        request: Request,
        freeman_session: str | None = Cookie(default=None),
    ):
        origin(request)
        user = await (
            actor(freeman_session)
            if request.url.path.startswith("/api/web/admin/")
            else current_user(freeman_session)
        )
        key = (
            PREFIX
            + f"subscription:{user.id}:"
            + hashlib.sha256(payload.endpoint.encode()).hexdigest()
        )
        async with database.sessions.begin() as s:
            row = await s.get(AppSetting, key)
            if row:
                await s.delete(row)
        return {"ok": True}

    @router.post("/api/web/notifications/push/status")
    async def push_status(
        payload: EndpointBody,
        request: Request,
        freeman_session: str | None = Cookie(default=None),
    ):
        origin(request)
        user = await current_user(freeman_session)
        key = (
            PREFIX
            + f"subscription:{user.id}:"
            + hashlib.sha256(payload.endpoint.encode()).hexdigest()
        )
        async with database.sessions() as session:
            row = await session.get(AppSetting, key)
            meta = json.loads(row.value) if row else {}
        return JSONResponse(
            dict(
                enabled=bool(
                    meta
                    and meta.get("auth_version") == user.auth_version
                    and meta.get("expires", 0) > now().timestamp()
                )
            ),
            headers={"Cache-Control": "no-store"},
        )

    return router


async def revoke_session_push(database, settings, token):
    if not token:
        return
    try:
        claims = json.loads(
            Fernet(settings.encryption_key.encode()).decrypt(token.encode())
        )
        uid = int(claims["user_id"])
    except Exception:
        return
    digest = hashlib.sha256(token.encode()).hexdigest()
    async with database.sessions.begin() as s:
        for row in await s.scalars(
            select(AppSetting).where(
                AppSetting.key.startswith(PREFIX + f"subscription:{uid}:")
            )
        ):
            if json.loads(row.value).get("session_hash") == digest:
                await s.delete(row)


async def enqueue_publication_push(session, publication, title):
    """Same transaction as publication; one durable event and per-device job.

    No historical catch-up and no remote calls. Publishing/editing cannot enqueue twice.
    """
    event = PREFIX + "publication:" + str(publication.id)
    if await session.get(AppSetting, event):
        return
    await SQLRepository(session).set_setting(
        event,
        json.dumps(
            dict(
                id="post-" + str(publication.id),
                title=title,
                text=publication.plain[:180],
                url="/?post=" + str(publication.id),
            ),
            ensure_ascii=False,
        ),
    )
    if session.bind.dialect.name == "postgresql":
        await session.execute(
            text("""
        INSERT INTO app_settings (key,value)
        SELECT :job_prefix || key,
          json_build_object('subscription',key,'report',cast(:event as text),'publication_id',cast(:id as integer),
                            'state','pending','attempts',0,'available',0)::text
        FROM app_settings WHERE starts_with(key,:prefix)
        ON CONFLICT (key) DO NOTHING
        """),
            dict(
                job_prefix=PREFIX + "delivery:post-" + str(publication.id) + ":",
                event=event,
                id=publication.id,
                prefix=PREFIX + "subscription:",
            ),
        )
    else:
        # Isolated SQLite fixtures; production uses atomic INSERT SELECT above.
        for key in await session.scalars(
            select(AppSetting.key).where(
                AppSetting.key.startswith(PREFIX + "subscription:")
            )
        ):
            await SQLRepository(session).set_setting(
                PREFIX + "delivery:post-" + str(publication.id) + ":" + key,
                json.dumps(
                    dict(
                        subscription=key,
                        report=event,
                        publication_id=publication.id,
                        state="pending",
                        attempts=0,
                        available=0,
                    )
                ),
            )


async def publication_push_status(session, publication_id):
    prefix = PREFIX + "delivery:post-" + str(publication_id) + ":"
    if session.bind.dialect.name == "postgresql":
        rows = await session.execute(
            text("""
        SELECT (CASE WHEN starts_with(key,:prefix) THEN value ELSE '{}' END)::jsonb->>'state',count(*)
        FROM app_settings WHERE starts_with(key,:prefix) GROUP BY 1
        """),
            dict(prefix=prefix),
        )
        return dict(rows.all())
    counts = {}
    for value in await session.scalars(
        select(AppSetting.value).where(AppSetting.key.startswith(prefix))
    ):
        state = json.loads(value)["state"]
        counts[state] = counts.get(state, 0) + 1
    return counts
