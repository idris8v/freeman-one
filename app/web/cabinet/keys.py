"""Customer key connection, renewal, gifts and connected devices."""

import hashlib
from datetime import timezone
from io import BytesIO

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import Response
from sqlalchemy import select

from app.database.models import (
    AccessKey,
    AdminAudit,
    Device,
    Subscription,
    VlessCredential,
    VpnCredential,
)
from app.database.repositories import SQLRepository
from app.web.cabinet.context import WebContext
from app.web.cabinet.schemas import AutoRenewPayload, GiftPayload, KeyRenamePayload


async def owned_key(session, user_id: int, key_id: int) -> AccessKey:
    """Resolve only the authenticated customer's non-deleted key."""
    key = await session.scalar(
        select(AccessKey).where(
            AccessKey.id == key_id,
            AccessKey.user_id == user_id,
            AccessKey.status != "deleted",
        )
    )
    if not key:
        raise HTTPException(404, "Ключ не найден")
    return key


async def connection_data(repo, settings, user_id: int, key_id: int) -> dict:
    """Use one ownership check and provider precedence for links and QR codes."""
    key = await owned_key(repo.session, user_id, key_id)
    vpn = await repo.session.scalar(
        select(VpnCredential).where(
            VpnCredential.access_key_id == key.id,
            VpnCredential.user_id == user_id,
        )
    )
    if vpn:
        from app.services.vpn import VpnService

        return {"value": VpnService(repo, settings).decrypt(vpn.config), "kind": "vpn"}
    vless = await repo.session.scalar(
        select(VlessCredential).where(
            VlessCredential.access_key_id == key.id,
            VlessCredential.user_id == user_id,
        )
    )
    if vless and vless.subscription_url:
        return {"value": vless.subscription_url, "kind": "vless"}
    demo = await repo.setting(f"demo_file:{key.id}")
    from app.services.demo_keys import demo_enabled

    if demo and demo_enabled(repo):
        digest = hashlib.sha256(demo.encode()).hexdigest()[:32]
        return {
            "value": f"https://example.invalid/freeman-demo/{key.id}/{digest}",
            "kind": "demo",
        }
    raise HTTPException(404, "Конфигурация ещё не выдана")


def keys_router(context: WebContext) -> APIRouter:
    database = context.database
    settings = context.settings
    current_user = context.current_user
    router = APIRouter()

    @router.get("/api/web/keys/{key_id}/qr")
    async def key_qr(key_id: int, freeman_session: str | None = Cookie(default=None)):
        user = await current_user(freeman_session)
        async with database.sessions() as session:
            connection = await connection_data(
                SQLRepository(session), settings, user.id, key_id
            )
            import qrcode

            image = qrcode.make(connection["value"])
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            return Response(
                buffer.getvalue(),
                media_type="image/png",
                headers={"Cache-Control": "private, no-store"},
            )

    @router.post("/api/web/keys/{key_id}/autorenew")
    async def key_autorenew(
        key_id: int,
        payload: AutoRenewPayload,
        freeman_session: str | None = Cookie(default=None),
    ):
        user = await current_user(freeman_session)
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            key = await owned_key(session, user.id, key_id)
            linked = None
            for sub in await repo.subscriptions(user.id):
                if await repo.setting(f"subscription_key:{sub.id}") == str(key.id):
                    linked = sub
                    break
            if not linked:
                raise HTTPException(409, "Подписка ключа не найдена")
            if payload.enabled:
                tariff = await repo.select_tariff(payload.months, payload.tariff_id)
                if (
                    not tariff
                    or tariff.device_limit != linked.device_limit
                    or await repo.tariff_setting(tariff, "tariff_enabled", "1") != "1"
                ):
                    raise HTTPException(409, "Тариф недоступен")
                await repo.set_setting(f"auto_renew_tariff:{linked.id}", str(tariff.id))
                await repo.set_setting(f"auto_source:{linked.id}", "balance")
                await repo.set_setting(f"auto_renew:{linked.id}", "1")
                await repo.set_setting(
                    f"auto_renew_months:{linked.id}", str(tariff.months)
                )
                await repo.set_setting(f"auto_renew_cycle:{linked.id}", "monthly")
            else:
                await repo.set_setting(f"auto_renew:{linked.id}", "0")
            return {
                "enabled": payload.enabled,
                "months": payload.months,
                "next_renewal_at": linked.expires_at.replace(
                    tzinfo=timezone.utc
                ).isoformat()
                if payload.enabled
                else None,
            }

    @router.post("/api/web/keys/{key_id}/gift", deprecated=True)
    async def gift_key(
        key_id: int,
        payload: GiftPayload,
        freeman_session: str | None = Cookie(default=None),
    ):
        await current_user(freeman_session)
        raise HTTPException(410, "Передача подписок в подарок на сайте больше недоступна")

    @router.put("/api/web/keys/{key_id}/name")
    async def rename_key(
        key_id: int,
        payload: KeyRenamePayload,
        freeman_session: str | None = Cookie(default=None),
    ):
        user = await current_user(freeman_session)
        name = payload.name.strip()
        if not name or any(ord(char) < 32 for char in name):
            raise HTTPException(422, "Введите название от 1 до 60 символов")
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            key = await owned_key(session, user.id, key_id)
            key.name = name
            for sub in await repo.subscriptions(user.id):
                if await repo.setting(f"subscription_key:{sub.id}") == str(key.id):
                    await repo.set_setting(f"subscription_name:{sub.id}", name)
            await repo.add(
                AdminAudit(
                    # Zero denotes a non-Telegram actor; retain its real account
                    # identity in details without inventing a Telegram account.
                    actor_telegram_id=user.telegram_id or 0,
                    action="client_key_rename",
                    target=str(key.id),
                    details=f"Новое название: {name}; user_id={user.id}",
                )
            )
            return {"id": key.id, "name": key.name}

    @router.get("/api/web/keys/{key_id}/connection")
    async def key_connection(
        key_id: int, freeman_session: str | None = Cookie(default=None)
    ):
        user = await current_user(freeman_session)
        async with database.sessions() as session:
            return await connection_data(
                SQLRepository(session), settings, user.id, key_id
            )

    @router.delete("/api/web/keys/{key_id}/devices/{device_id}")
    async def disconnect_device(
        key_id: int, device_id: int, freeman_session: str | None = Cookie(default=None)
    ):
        user = await current_user(freeman_session)
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            key = await session.scalar(
                select(AccessKey).where(
                    AccessKey.id == key_id,
                    AccessKey.status != "deleted",
                    *(
                        []
                        if settings.is_owner(user.telegram_id)
                        else [AccessKey.user_id == user.id]
                    ),
                )
            )
            if not key:
                raise HTTPException(404, "Ключ не найден")
            linked = None
            for sub in await repo.subscriptions(key.user_id):
                if await repo.setting(f"subscription_key:{sub.id}") == str(key.id):
                    linked = sub
                    break
            if not linked:
                credential = await session.scalar(
                    select(VlessCredential).where(
                        VlessCredential.access_key_id == key.id
                    )
                ) or await session.scalar(
                    select(VpnCredential).where(VpnCredential.access_key_id == key.id)
                )
                if credential:
                    linked = await session.get(Subscription, credential.subscription_id)
            if not linked:
                raise HTTPException(404, "Подписка устройства не найдена")
            device = await session.scalar(
                select(Device).where(
                    Device.id == device_id,
                    Device.subscription_id == linked.id,
                    Device.status.not_in(("disconnected", "deleted")),
                )
            )
            if not device:
                raise HTTPException(404, "Устройство не найдено")
            vless = await session.scalar(
                select(VlessCredential).where(
                    VlessCredential.subscription_id == linked.id,
                    VlessCredential.user_id == key.user_id,
                    VlessCredential.status != "deleted",
                )
            )
            vpn = await session.scalar(
                select(VpnCredential).where(
                    VpnCredential.subscription_id == linked.id,
                    VpnCredential.user_id == key.user_id,
                    VpnCredential.status == "active",
                )
            )
            try:
                if vless:
                    if not device.provisioning_id:
                        raise ValueError(
                            "Панель VLESS не передала идентификатор этого устройства"
                        )
                    from app.services.remnawave import RemnawaveService

                    service = RemnawaveService(repo, settings)
                    server = await service.server(vless.server_id)
                    await service.api(server).delete_device(
                        device.provisioning_id, vless.remote_user_uuid
                    )
                    from app.services.vless_hwid import confirmed_removal

                    await confirmed_removal(repo, vless, device.provisioning_id)
                elif vpn:
                    raise ValueError(
                        "Amnezia не поддерживает отключение одного устройства. Используйте сброс всех устройств."
                    )
                elif await repo.setting(f"demo_file:{key.id}"):
                    from app.services.demo_keys import create_demo_file

                    await create_demo_file(repo, key)
                else:
                    raise ValueError(
                        "Провайдер этого ключа не поддерживает отключение устройства"
                    )
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            except Exception as error:
                raise HTTPException(
                    502, "Сервер не подтвердил отключение устройства"
                ) from error
            device.status = "disconnected"
            await repo.add(
                AdminAudit(
                    actor_telegram_id=user.telegram_id or -user.id,
                    action="client_device_disconnect",
                    target=str(device.id),
                    details=f"Ключ #{key.id}; подписка #{linked.id}",
                )
            )
            return {
                "id": device.id,
                "status": device.status,
                "key_rotated": bool(vpn or not vless),
            }

    from app.web.cabinet.key_upgrade import key_upgrade_router
    router.include_router(key_upgrade_router(context))
    return router
