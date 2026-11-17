"""Cabinet bootstrap/profile response and the current customer avatar."""

from datetime import timezone
from time import perf_counter

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute
from sqlalchemy import String, cast, func, literal, or_, select

from app.web.device_limit import LIMIT_REJECTION_STATUS, REGISTERED_DEVICE_STATUSES, device_limit_exceeded
from app.database.models import (
    AccessKey,
    AppSetting,
    Device,
    ProxyCredential,
    Subscription,
    User,
    VlessCredential,
    VpnCredential,
    WebNews,
    WebUsefulLink,
    now,
)
from app.database.repositories import SQLRepository
from app.services.accounts import display_freeman_id, freeman_id_aliases
from app.services.avatar import TelegramAvatars
from app.services.pricing import quote
from app.services.publication_urls import publication_url
from app.web.cabinet.content_views import link_json, news_json
from app.web.cabinet.context import WebContext
from app.web.device_names import device_display_name
from app.web.request_performance import checkpoint, measured_route


class ProfileRepository(SQLRepository):
    """Request-local, explicitly scoped settings snapshot for profile rendering."""

    async def prefetch_profile(self, user_id, subscriptions):
        prefixes = (
            "bot_test_access",
            "bot_antig_until",
            "subscription_key",
            "subscription_name",
            "subscription_status",
            "subscription_removed",
            "subscription_transferred",
            "auto_renew",
            "auto_renew_months",
            "auto_source",
            "auto_card",
        )
        self.profile_keys = {
            f"{prefix}:{sub.id}" for sub in subscriptions for prefix in prefixes
        }
        self.profile_keys.update({
            f"auto_master:{user_id}", f"proxy_paid_until:{user_id}",
            f"freeman_id:{user_id}", f"user_phone:{user_id}", f"user_email:{user_id}",
            "trial_enabled", "trial_hours", "bot_antig_enabled",
            "payment_provider:yookassa",
        })
        self.profile_values = dict(
            (
                await self.session.execute(
                    select(AppSetting.key, AppSetting.value).where(
                        AppSetting.key.in_(self.profile_keys)
                    )
                )
            ).all()
        )

    async def setting(self, key, default=""):
        if key in getattr(self, "profile_keys", ()):
            return self.profile_values.get(key, default)
        return await super().setting(key, default)

    async def prefetch_catalog(self, user_id):
        # This repository lives for one profile request. Reuse the shared quote
        # implementation, with one snapshot of its setting inputs, never prices
        # cached across users or requests. Missing keys retain their defaults.
        self._screen_cache = {}
        tariffs = await self.tariffs()
        keys = {f"client_discount:{user_id}", "promos_enabled"}
        for tariff in tariffs:
            keys.add(f"legacy_tariff:{tariff.months}")
            for name in ("tariff_enabled", "tariff_name", f"client_price:{user_id}"):
                keys.update((f"{name}:id{tariff.id}", f"{name}:{tariff.months}"))
        await self.prefetch_settings(keys=keys, prefixes=("public_promotion:",))

    async def active_key_ids(self, user_id, keys):
        """Batch equivalent of client_registry.active_key for this owned key list.

        A key is stopped by any linked subscription, not only the subscriptions
        owned by this user. Preserve credential and exact string-setting links,
        including the rule that an orphan setting does not establish a link.
        Regression tests compare these cases with the unchanged shared predicate.
        """
        instant = now()
        candidates = {
            key.id
            for key in keys
            if key.user_id == user_id
            and key.status == "active"
            and key.expires_at.replace(tzinfo=timezone.utc) > instant
        }
        if not candidates:
            return set()
        links = []
        for model in (VlessCredential, VpnCredential):
            links.extend(
                await self.session.execute(
                    select(model.access_key_id, model.subscription_id).where(
                        model.access_key_id.in_(candidates)
                    )
                )
            )
        links.extend(
            (int(key_id), subscription_id)
            for key_id, subscription_id in await self.session.execute(
                select(AppSetting.value, Subscription.id)
                .join(
                    AppSetting,
                    AppSetting.key
                    == literal("subscription_key:") + cast(Subscription.id, String),
                )
                .where(AppSetting.value.in_([str(key_id) for key_id in candidates]))
            )
        )
        subscription_ids = {subscription_id for _, subscription_id in links}
        if not subscription_ids:
            return candidates
        flags = dict(
            (
                await self.session.execute(
                    select(AppSetting.key, AppSetting.value).where(
                        AppSetting.key.in_(
                            [
                                f"{prefix}:{subscription_id}"
                                for subscription_id in subscription_ids
                                for prefix in (
                                    "subscription_status",
                                    "subscription_removed",
                                    "subscription_transferred",
                                )
                            ]
                        )
                    )
                )
            ).all()
        )
        stopped = {
            sid
            for sid in subscription_ids
            if flags.get(f"subscription_status:{sid}", "active") != "active"
            or flags.get(f"subscription_removed:{sid}") == "1"
            or flags.get(f"subscription_transferred:{sid}") == "1"
        }
        return candidates - {kid for kid, sid in links if sid in stopped}


class ProfileTimingRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        if self.path != "/api/web/me":
            return handler

        async def timed(request):
            started = perf_counter()
            response = await handler(request)
            response.headers["Server-Timing"] = (
                f"profile;dur={(perf_counter() - started) * 1000:.1f}"
            )
            return response

        return timed


def profile_router(context: WebContext) -> APIRouter:
    database = context.database
    settings = context.settings
    current_user = context.current_user
    router = APIRouter(route_class=measured_route(database.engine, ProfileTimingRoute))
    avatars = TelegramAvatars()

    @router.get("/api/web/config")
    async def config(request: Request):
        async with database.sessions() as session:
            referrer_name = request.cookies.get("freeman_referrer", "")
            inviter = (
                await session.scalar(
                    select(User.id).where(
                        User.freeman_id.in_(freeman_id_aliases(referrer_name)),
                        User.status == "active",
                    )
                )
                if referrer_name
                else None
            )
        return {
            "brand": "Freeman ONE",
            "bot_username": settings.bot_username,
            "bot_start": f"site_ref_{inviter}" if inviter else "site_login",
            "email_registration_available": bool(
                settings.smtp_host
                and settings.smtp_from
                and settings.smtp_username
                and (settings.smtp_password or settings.smtp_password_b64)
            ),
            "telegram_auth_url": f"{settings.web_base_url}/auth/telegram/callback",
        }

    @router.get("/api/web/avatar")
    async def telegram_avatar(freeman_session: str | None = Cookie(default=None)):
        user = await current_user(freeman_session)
        image = await avatars.get(user.telegram_id, settings.bot_token)
        headers = {
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        }
        if not image:
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        return Response(image, media_type="image/jpeg", headers=headers)

    @router.get("/api/web/profile/details")
    async def details(freeman_session: str | None = Cookie(default=None)):
        actor = await current_user(freeman_session)
        async with database.sessions() as session:
            user = await session.get(User, actor.id)
            from app.services.recurring_cards import methods

            cards = await methods(SQLRepository(session), user.id, include_expired=True)
            return dict(
                name=user.display_name,
                freeman_id=display_freeman_id(user.freeman_id),
                email=user.email,
                email_verified=user.email_verified,
                telegram_id=user.telegram_id,
                username=user.username,
                registered_at=user.created_at.isoformat(),
                can_unlink_telegram=bool(
                    user.telegram_id
                    and user.email
                    and user.email_verified
                    and user.password_hash
                    and not settings.is_admin(user.telegram_id)
                ),
                payment_methods=[
                    dict(
                        brand=m.get("brand", "Карта"),
                        last4=m.get("last4", ""),
                        month=m.get("month"),
                        year=m.get("year"),
                    )
                    for m in cards
                ],
            )

    @router.get("/api/web/me")
    async def me(freeman_session: str | None = Cookie(default=None)):
        user = await current_user(freeman_session)
        checkpoint("auth")
        async with database.sessions.begin() as session:
            repo = ProfileRepository(session)
            subscriptions = list(
                await session.scalars(
                    select(Subscription)
                    .where(Subscription.user_id == user.id)
                    .order_by(Subscription.expires_at.desc())
                )
            )
            await repo.prefetch_profile(user.id, subscriptions)
            keys = list(
                await session.scalars(
                    select(AccessKey)
                    .where(AccessKey.user_id == user.id, AccessKey.status != "deleted")
                    .order_by(AccessKey.created_at.desc(), AccessKey.id.desc())
                )
            )
            links = list(
                await session.scalars(
                    select(WebUsefulLink)
                    .where(WebUsefulLink.enabled == True)
                    .order_by(WebUsefulLink.position, WebUsefulLink.id)
                )
            )
            from app.database.models import Publication

            news = list(
                await session.scalars(
                    select(WebNews)
                    .outerjoin(Publication, Publication.id == WebNews.id)
                    .where(
                        WebNews.published == True,
                        or_(Publication.id.is_(None), Publication.section == "news"),
                    )
                    .order_by(Publication.pinned.desc().nullslast(), WebNews.id.desc())
                    .limit(12)
                )
            )
            publication_rows = (
                await session.execute(
                    select(
                        Publication.id, Publication.link_id, Publication.notified_at
                    ).where(
                        or_(
                            Publication.id.in_([n.id for n in news]),
                            Publication.link_id.in_([link.id for link in links]),
                        )
                    )
                )
            ).all()
            publication_flags = {
                p.id: p.notified_at is not None for p in publication_rows
            }
            publication_links = {p.link_id: p.id for p in publication_rows if p.link_id}
            checkpoint("profile_queries")
            await repo.prefetch_catalog(user.id)
            tariffs = []
            for tariff in await repo.tariffs():
                if await repo.tariff_setting(tariff, "tariff_enabled", "1") == "1":
                    price, discount, special = await quote(repo, user.id, tariff)
                    tariffs.append(
                        {
                            "id": tariff.id,
                            "months": tariff.months,
                            "price": price,
                            "base_price": tariff.price,
                            "discount": discount,
                            "special_price": special,
                            "device_limit": tariff.device_limit,
                            "name": await repo.tariff_setting(
                                tariff, "tariff_name", f"{tariff.months} мес."
                            ),
                        }
                    )
            checkpoint("catalog")
            active_ids = await repo.active_key_ids(user.id, keys)
            fallback = {}
            missing = [
                sub.id
                for sub in subscriptions
                if not await repo.setting(f"subscription_key:{sub.id}")
            ]
            if missing:
                for model in (VlessCredential, VpnCredential):
                    for sid, kid in await session.execute(
                        select(model.subscription_id, model.access_key_id).where(
                            model.subscription_id.in_(missing)
                        )
                    ):
                        if kid:
                            fallback.setdefault(sid, kid)
            active = []
            for sub in subscriptions:
                kid = await repo.setting(f"subscription_key:{sub.id}")
                if (
                    sub.expires_at.replace(tzinfo=timezone.utc) > now()
                    and await repo.setting(f"subscription_status:{sub.id}", "active")
                    == "active"
                    and await repo.setting(f"subscription_removed:{sub.id}") != "1"
                    and await repo.setting(f"subscription_transferred:{sub.id}") != "1"
                    and (int(kid) if kid else fallback.get(sub.id)) in active_ids
                ):
                    active.append(sub)
            paid_proxy = await repo.setting(f"proxy_paid_until:{user.id}")
            key_rows = []
            # The shared metadata/link rules expect id-descending subscriptions.
            # Reuse the owned rows already fetched above, preserving that order.
            user_subscriptions = sorted(subscriptions, key=lambda sub: sub.id, reverse=True)
            subscription_names = {
                key: value for key, value in repo.profile_values.items()
                if key.startswith("subscription_name:")
            }
            from app.services.web_keys import key_metadata

            metadata = await key_metadata(repo, user.id, keys, user_subscriptions)
            # Fetch owned links/devices once. The former nested key/subscription
            # loop issued O(keys * subscriptions) SQL queries during startup.
            links_by_subscription = {
                key: value for key, value in repo.profile_values.items()
                if key.startswith("subscription_key:")
            }
            subscriptions_by_key = {}
            for sub in user_subscriptions:
                key_id = links_by_subscription.get(f"subscription_key:{sub.id}")
                if key_id:
                    subscriptions_by_key.setdefault(key_id, sub)
            devices_by_subscription = {}
            if user_subscriptions:
                for device in await session.scalars(
                    select(Device)
                    .where(
                        Device.subscription_id.in_(
                            [sub.id for sub in user_subscriptions]
                        ),
                        Device.status.not_in(("disconnected", "deleted")),
                    )
                    .order_by(Device.id.desc())
                ):
                    devices_by_subscription.setdefault(
                        device.subscription_id, []
                    ).append(device)
            from app.web.key_antig import addon_active
            for item in keys:
                linked = subscriptions_by_key.get(str(item.id))
                devices = devices_by_subscription.get(linked.id, []) if linked else []
                registered = [d for d in devices if d.status in REGISTERED_DEVICE_STATUSES]
                rejected_count = sum(d.status == LIMIT_REJECTION_STATUS for d in devices)

                auto_renew = bool(
                    linked and await repo.setting(f"auto_renew:{linked.id}", "0") == "1"
                )
                renewal = dict(effective=False, reason="")
                if auto_renew:
                    from app.services.recurring_cards import renewal_status

                    renewal = await renewal_status(repo, settings, user, linked)
                auto_months = (
                    int(await repo.setting(f"auto_renew_months:{linked.id}", "1"))
                    if linked
                    else 1
                )
                custom_name = (
                    subscription_names.get(f"subscription_name:{linked.id}", "").strip()
                    if linked
                    else ""
                )
                key_rows.append(
                    {
                        "id": item.id,
                        "name": custom_name or item.name,
                        **({"has_custom_name": True} if custom_name else {}),
                        "status": item.status,
                        "mode": item.mode,
                        **metadata[item.id],
                        "expires_at": item.expires_at.replace(
                            tzinfo=timezone.utc
                        ).isoformat(),
                        "subscription_id": linked.id if linked else None,
                        "antig_active": await addon_active(repo, linked),
                        "device_limit": linked.device_limit
                        if linked
                        else settings.device_limit,
                        "device_over_limit": device_limit_exceeded(
                            linked.device_limit if linked else None, len(registered), rejected_count
                        ),
                        "auto_renew": auto_renew,
                        "auto_renew_months": auto_months,
                        "auto_source": await repo.setting(
                            f"auto_source:{linked.id}", "balance"
                        )
                        if linked
                        else "balance",
                        "auto_effective": renewal["effective"],
                        "auto_reason": renewal["reason"],
                        "next_renewal_at": item.expires_at.replace(
                            tzinfo=timezone.utc
                        ).isoformat()
                        if auto_renew
                        else None,
                        "display_key": f"FRM-{item.id:04d}-••••-••••",
                        "devices": [
                            {
                                "id": device.id,
                                "name": device_display_name(device.name),
                                "type": device.type,
                                "status": device.status,
                                "connected_at": device.created_at.replace(
                                    tzinfo=timezone.utc
                                ).isoformat(),
                            }
                            for device in registered
                        ],
                    }
                )
            key_rows.sort(
                key=lambda row: (row["purchased_at"] or row["created_at"], row["id"]),
                reverse=True,
            )
            checkpoint("key_rows")
            freeman_id = await repo.setting(f"freeman_id:{user.id}")
            if not freeman_id:
                freeman_id = display_freeman_id(user.freeman_id) or f"FO-{user.id}"
                await repo.set_setting(f"freeman_id:{user.id}", freeman_id)
            return {
                "profile": {
                    "name": user.display_name,
                    "username": user.username,
                    "telegram_id": user.telegram_id,
                    "is_admin": settings.is_admin(user.telegram_id),
                    "freeman_id": display_freeman_id(user.freeman_id or freeman_id),
                    "freeman_id_display": display_freeman_id(
                        user.freeman_id or freeman_id
                    ),
                    "account_username": user.account_username,
                    "email_verified": user.email_verified,
                    "password_login_available": bool(user.password_hash),
                    "registration_source": user.registration_source,
                    "authentication": "otp",
                    "avatar": "/api/web/avatar" if user.telegram_id else None,
                    "phone": await repo.setting(f"user_phone:{user.id}") or None,
                    "email": user.email
                    or await repo.setting(f"user_email:{user.id}")
                    or None,
                },
                "trial": {
                    "available": not user.trial_used
                    and await repo.setting("trial_enabled", "1") == "1",
                    "hours": int(await repo.setting("trial_hours", "24")),
                },
                "balance": await repo.balance(user.id),
                "summary": {
                    "active_subscriptions": len(active),
                    "keys": len(keys),
                    "keys_active": len(active_ids),
                    "proxy_count": await session.scalar(
                        select(func.count(ProxyCredential.id)).where(
                            ProxyCredential.user_id == user.id
                        )
                    ),
                    "referral_count": await repo.referral_count(user.id),
                    "proxy_included": bool(active),
                    "proxy_paid_until": paid_proxy or None,
                },
                "subscriptions": [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "device_limit": item.device_limit,
                        "expires_at": item.expires_at.replace(
                            tzinfo=timezone.utc
                        ).isoformat(),
                        "active": item in active,
                        "name": await repo.setting(
                            f"subscription_name:{item.id}",
                            f"F R E E M A N · #{item.id}",
                        ),
                    }
                    for item in subscriptions[:8]
                ],
                "keys": key_rows,
                "tariffs": tariffs,
                "antig_addon_available": await repo.setting("bot_antig_enabled", "0") == "1",
                "payments": {
                    "yookassa": bool(
                        settings.yookassa_shop_id and settings.yookassa_secret_key
                    )
                    and await repo.setting("payment_provider:yookassa", "1") == "1",
                    "balance": True,
                    "owner_free": settings.is_owner(user.telegram_id),
                },
                "useful_links": [
                    link_json(item)
                    | (
                        {
                            "target": publication_url(
                                settings, publication_links[item.id]
                            )
                        }
                        if item.id in publication_links
                        else {}
                    )
                    for item in links
                ],
                "news": [
                    news_json(item)
                    | {"notify": publication_flags.get(item.id, True)}
                    | (
                        {"target": publication_url(settings, item.id)}
                        if item.id in publication_flags
                        else {}
                    )
                    for item in news
                ],
            }

    return router
