"""Customer Telegram Proxy access and checkout endpoints."""

import hashlib
import re
from datetime import timezone

from fastapi import APIRouter, Cookie, HTTPException
from sqlalchemy import select, update

from app.database.models import (
    ProxyCredential,
    ProxyServer,
    User,
)
from app.database.repositories import SQLRepository
from app.services.proxy_control import meta
from app.web.proxy_renewal import can_renew_proxy
from app.web.cabinet.context import WebContext
from app.web.cabinet.schemas import ProxyCheckoutPayload


def proxy_router(context: WebContext) -> APIRouter:
    database = context.database
    settings = context.settings
    current_user = context.current_user
    router = APIRouter()

    @router.get("/api/web/proxy")
    async def proxy_details(freeman_session: str | None = Cookie(default=None)):
        user = await current_user(freeman_session)
        async with database.sessions() as session:
            repo = SQLRepository(session)
            from app.services.proxies import ProxyService

            service = ProxyService(repo, settings)
            kind = await service.access_kind(user)
            credentials = list(
                await session.scalars(
                    select(ProxyCredential)
                    .where(
                        ProxyCredential.user_id == user.id,
                        ProxyCredential.status.not_in(("deleted", "migrated")),
                    )
                    .order_by(ProxyCredential.id.desc())
                )
            )
            servers = (
                {
                    item.id: item
                    for item in await session.scalars(
                        select(ProxyServer).where(
                            ProxyServer.id.in_([x.server_id for x in credentials])
                        )
                    )
                }
                if credentials
                else {}
            )
            rows = []
            for item in credentials:
                link = ""
                if item.link:
                    try:
                        link = service.public_link(service.decrypt(item.link))
                    except (ValueError, TypeError):
                        pass
                server = servers.get(item.server_id)
                issued_kind = await repo.setting(f"proxy_credential_kind:{item.id}")
                stored_limit = await repo.setting(
                    f"proxy_credential_ip_limit:{item.id}"
                )
                stats_seen = await repo.setting(f"proxy_stats_seen:{item.id}") == "1"
                rows.append(
                    {
                        "id": item.id,
                        "server_id": item.server_id,
                        "server": server.name
                        if server
                        else f"Сервер #{item.server_id}",
                        "link": link,
                        "access_kind": issued_kind
                        if issued_kind in ("vpn", "paid")
                        else None,
                        "source": (await meta(repo, item)).source,
                        "subscription_id": (await meta(repo, item)).subscription_id,
                        "status": item.status,
                        "can_renew": user.status != "blocked" and bool(server and server.status == "active") and await can_renew_proxy(repo, item),
                        "expires_at": item.expires_at.replace(
                            tzinfo=timezone.utc
                        ).isoformat(),
                        "created_at": item.created_at.replace(
                            tzinfo=timezone.utc
                        ).isoformat(),
                        "connections": item.current_connections if stats_seen else None,
                        "unique_ips": item.active_unique_ips if stats_seen else None,
                        "ip_limit": int(stored_limit)
                        if stored_limit and stored_limit.isdigit()
                        else None,
                        "last_stats_at": item.last_sync.replace(
                            tzinfo=timezone.utc
                        ).isoformat()
                        if stats_seen and item.last_sync
                        else None,
                    }
                )
            return {
                "eligible": kind == "vpn",
                "kind": kind,
                "price": int(await repo.setting("proxy_monthly_price", "3000")),
                "items": rows,
                "credentials": rows,
                "payments": {
                    "yookassa": bool(
                        settings.yookassa_shop_id and settings.yookassa_secret_key
                    ),
                    "balance": True,
                },
            }

    @router.post("/api/web/proxy/access")
    async def proxy_access(
        payload: ProxyCheckoutPayload,
        freeman_session: str | None = Cookie(default=None),
    ):
        user = await current_user(freeman_session)
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            from app.services.proxies import ProxyService

            service = ProxyService(repo, settings)
            await session.execute(
                update(User).where(User.id == user.id).values(status=User.status)
            )
            kind = await service.access_kind(user)
            if kind != "vpn":
                raise HTTPException(
                    409, "Бесплатное подключение доступно с активной VPN-подпиской"
                )
            if not re.fullmatch(r"[A-Za-z0-9_-]{12,80}", payload.idempotency_key):
                raise HTTPException(422, "Некорректный идентификатор запроса")
            token = hashlib.sha256(payload.idempotency_key.encode()).hexdigest()[:20]
            servers = await service.matching_servers(kind)
            items = []
            for server in servers:
                try:
                    credential, link = await service.provision(
                        server, user, force_kind=kind, connection_token=token
                    )
                    items.append(
                        {
                            "id": credential.id,
                            "server": server.name,
                            "link": link,
                            "status": credential.status,
                            "expires_at": credential.expires_at.replace(
                                tzinfo=timezone.utc
                            ).isoformat(),
                        }
                    )
                    break
                except ValueError:
                    continue
            if not items:
                raise HTTPException(
                    409, "Нет доступных прокси-серверов. Обратитесь в поддержку."
                )
            return {"kind": kind, "items": items}

    @router.post("/api/web/proxy/checkout")
    async def proxy_checkout(
        payload: ProxyCheckoutPayload,
        freeman_session: str | None = Cookie(default=None),
    ):
        user = await current_user(freeman_session)
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            await session.execute(
                update(User).where(User.id == user.id).values(status=User.status)
            )
            from app.services.proxy_control import plans
            from app.web.proxy_control import Purchase, purchase

            plan = next((p for p in await plans(repo) if p.enabled), None)
            if not plan:
                raise HTTPException(409, "Тарифы Proxy недоступны")
            try:
                return await purchase(
                    repo,
                    settings,
                    user,
                    Purchase(
                        method=payload.method,
                        plan_id=plan.id,
                        request_id=payload.idempotency_key,
                    ),
                )
            except ValueError as e:
                raise HTTPException(409, str(e))

    return router
