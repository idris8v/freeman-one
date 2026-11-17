"""Owner issuance in Telegram: confirmed access, offline sale and wallet income."""

import json
import secrets
from types import SimpleNamespace

from sqlalchemy import select, update

from app.database.models import (
    AccessKey,
    AdminAudit,
    BalanceTransaction,
    Payment,
    User,
    now,
)
from app.services.billing import BillingService, purchase_limits


async def sale_terms(repo, tariff, *, renewal=False):
    if not tariff or await repo.tariff_setting(tariff, "tariff_enabled", "1") != "1":
        raise ValueError("Тариф недоступен.")
    if type(tariff.price) is not int or not 0 <= tariff.price <= 100_000_000:
        raise ValueError("Некорректная цена тарифа. Продажа не создана.")
    devices, traffic = await purchase_limits(repo, tariff)
    # Owner issuance is a manual sale at the public offer price, never at the
    # owner's private personal discount. Freeze the amount before provisioning.
    from app.services.catalog import public_price

    total = await public_price(repo, "vpn", tariff.id, tariff.price, renewal)
    return dict(
        tariff_id=tariff.id,
        months=tariff.months,
        devices=devices,
        traffic_bytes=traffic,
        total=total,
        base_price=tariff.price,
        discount=tariff.price - total,
        tariff_name=await repo.tariff_setting(
            tariff, "tariff_name", f"{tariff.months} мес."
        ),
    )


async def preview(repo, user, months, sid=0):
    # This quote is committed by the bot dispatcher before confirmation. It
    # survives an uncertain API result or rollback of the later issuance.
    tariff = await repo.lock_tariff(await repo.tariff(months))
    terms = await sale_terms(repo, tariff, renewal=bool(sid))
    token = secrets.token_hex(6)
    await repo.set_setting(
        "admin_grant:" + token,
        json.dumps(
            dict(
                actor=user.telegram_id,
                months=months,
                sid=sid,
                expires=now().timestamp() + 300,
                sale_terms=terms,
            )
        ),
    )
    return f"admin_free:{months}:{sid}:{token}"


async def grant(repo, settings, user, months, sid, token, *, complimentary=False):
    if await repo.setting(f'staff_role:{user.telegram_id}') == 'partner':
        raise PermissionError('Выдача доступна в партнёрском разделе.')
    if not getattr(settings, "can_free_subscription", settings.is_owner)(
        user.telegram_id
    ):
        raise PermissionError("Admin access denied")
    if complimentary and not settings.is_owner(user.telegram_id):
        raise PermissionError('Продление без продажи доступно только администратору.')
    await repo.session.execute(
        update(User).where(User.id == user.id).values(status=User.status)
    )
    data = json.loads(await repo.setting("admin_grant:" + token, "{}"))
    if (
        data.get("actor") != user.telegram_id
        or str(data.get("months")) != str(months)
        or data.get("sid") != sid
    ):
        raise ValueError("Подтверждение недоступно.")
    if data.get("result"):
        # Historical grants are not silently repriced or credited on replay.
        return data["result"]
    if data.get("expires", 0) < now().timestamp():
        from app.services.vless_creation import unresolved as creation_pending
        from app.services.vless_renewal import unresolved as renewal_pending

        operation_keys = ["admin-grant:" + token]
        if not (
            await creation_pending(repo, user, operation_keys)
            or await renewal_pending(repo, user, operation_keys)
        ):
            raise ValueError("Подтверждение устарело. Откройте тариф заново.")
    terms = data.get("sale_terms")
    if terms is None:
        # Compatibility for a five-minute confirmation opened before upgrade.
        terms = await sale_terms(repo, await repo.tariff(months), renewal=bool(sid))
    widget_admin=data.get('widget_admin')
    if widget_admin:
        from app.services.bot_widget_admin import require
        root=json.loads(await repo.setting(widget_admin.get('root',''),'{}'))
        require(settings,user,root)
        binding=root.get('admin_issue',{})
        if binding.get('token')!=token or binding.get('selection')!=widget_admin.get('selection') or data.get('widget_accounting')!=('free' if complimentary else 'sale'):
            raise PermissionError('Режим выдачи не подтверждён.')
    if len((terms.get('bot_test_access') or {}).get('squads',[]))==2 and not widget_admin:
        from app.services.bot_antig_price import require_enabled
        await require_enabled(repo)
    tariff = SimpleNamespace(
        id=terms["tariff_id"],
        months=terms["months"],
        device_limit=terms["devices"],
        price=terms["total"],
    )
    await purchase_limits(repo, tariff, terms)
    if type(tariff.price) is not int or not 0 <= tariff.price <= 100_000_000:
        raise ValueError("Некорректная цена тарифа. Продажа не создана.")
    sub = await repo.subscription(user.id, sid) if sid else None
    if sid and not sub:
        raise ValueError("Подписка недоступна.")
    key_id = await repo.setting(f"subscription_key:{sub.id}") if sub else None
    key = await repo.session.get(AccessKey, int(key_id)) if key_id else None
    reference = f"admin-sale:{user.id}:{token}"
    previous = await repo.session.scalar(
        select(Payment).where(Payment.provider_id == reference)
    )
    if previous:
        # Payment, credit and result are one transaction; an inconsistent legacy
        # state must be reconciled rather than issuing/crediting again.
        raise ValueError(
            "Продажа уже существует. Проверьте историю; повторная выдача не выполнена."
        )
    if sid:
        accounting = "free" if complimentary else "sale"
        if data.get("renewal_accounting") not in (None, accounting):
            raise ValueError("Продление уже запущено в другом режиме учёта. Завершите исходное подтверждение или откройте продление заново.")
        data["renewal_accounting"] = accounting
        await repo.set_setting("admin_grant:" + token, json.dumps(data))
    access = terms.get("bot_test_access")
    if access is not None:
        from app.services.bot_antig_test import validate_profile
        validate_profile(access)
        if not data.get("antig_test") or not settings.is_owner(user.telegram_id) or sid:
            raise ValueError("Выбор сквадов доступен только при новой выдаче администратору.")
    sub, key = await BillingService(repo, settings).fulfill(
        user,
        tariff,
        sub,
        key,
        terms=terms,
        operation_key="admin-grant:" + token,
    )
    demo = key.mode in ("mock", "demo")
    if not demo and settings.is_owner(user.telegram_id) and not complimentary:
        sub.kind = "paid"
        payment = await repo.add(
            Payment(
                user_id=user.id,
                subscription_id=sub.id,
                amount=tariff.price,
                status="manual_paid",
                provider_id=reference,
            )
        )
        stamp = now().isoformat()
        snapshot = dict(
            terms,
            kind="vpn",
            source="manual",
            issuance_kind="manual_sale",
            base_price=terms.get("base_price", tariff.price),
            renewal=bool(sid),
            recorded_by=user.id,
            key_id=key.id,
            subscription_id=sub.id,
            paid_at=stamp,
            issued_at=stamp,
            received_at=stamp,
        )
        await repo.set_setting(f"sale_snapshot:{payment.id}", json.dumps(snapshot))
        await repo.add(
            BalanceTransaction(
                user_id=user.id,
                type="manual_sale_income",
                amount=tariff.price,
                description=f"Ручная продажа #{payment.id} · подписка #{sub.id}",
            )
        )
        data["payment_id"] = payment.id
        details = f"Ручная продажа #{payment.id}; ключ #{key.id}; {tariff.price} коп.; срок {tariff.months} мес.; без онлайн-списания"
    else:
        if not sid:sub.kind = "admin"
        label = "Бесплатная замена" if complimentary else "Тестовая выдача" if demo else "Бесплатная выдача сотруднику"
        if complimentary and sid:label = "Продление без продажи"
        if complimentary and not sid:
            await repo.set_setting(f"subscription_issuance_kind:{sub.id}","replacement")
            data["issuance_kind"]="replacement"
        details = (
            f"{label}; ключ #{key.id}; срок {tariff.months} мес.; без продажи и дохода"
        )
    data["result"] = sub.id
    await repo.set_setting("admin_grant:" + token, json.dumps(data))
    await repo.add(
        AdminAudit(
            actor_telegram_id=user.telegram_id,
            action="admin_free",
            target=str(sub.id),
            details=details,
        )
    )
    return sub.id
