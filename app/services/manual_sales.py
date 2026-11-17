"""Durable administrator issuance accounting. Never calls a payment provider.

Prepare in a short transaction before provisioning. Finish alongside the confirmed
key/renewal in its transaction; a rollback retains the original price and claim.
"""

import hashlib
import json
from datetime import timedelta

from sqlalchemy import select, update

from app.database.models import (
    AccessKey,
    Payment,
    Subscription,
    User,
    VlessCredential,
    VpnCredential,
    AdminAudit,
    now,
)
from app.database.repositories import SQLRepository
from app.services.billing import extend, purchase_limits
from app.services.offers import consume, lock_customer, reserve
from app.services.pricing import quote
from app.services.vless_creation import utc

CATEGORIES = ("manual_sale", "complimentary", "test")


class Conflict(ValueError):
    pass


def operation_key(actor_id, request_id):
    return f"manual_sale:{actor_id}:{request_id}"


async def read(repo, actor_id, request_id):
    raw = await repo.setting(operation_key(actor_id, request_id))
    if not raw:
        return None
    try:
        op = json.loads(raw)
        if not isinstance(op, dict) or op.get("version") != 1:
            raise ValueError()
        if op.get("result") and op.get("payment_id"):
            payment = await repo.session.get(
                Payment, op["payment_id"], populate_existing=True
            )
            if payment:
                op["result"]["payment_received"] = payment.status == "manual_paid"
        return op
    except (TypeError, ValueError) as error:
        raise Conflict(
            "Не удалось прочитать ручную выдачу. Обратитесь в поддержку; повторная выдача не выполнена."
        ) from error


async def prepare(repo, actor, user, request, *, server=None, subscription=None):
    """Caller owns the actor lock; request contains IDs/terms, never credentials."""
    key = operation_key(actor.id, request["request_id"])
    previous = await read(repo, actor.id, request["request_id"])
    canonical = dict(request, user_id=user.id)
    if previous and not canonical.get("tariff_id"):
        canonical["tariff_id"] = previous["request"].get("tariff_id")
    category = canonical["issuance_kind"]
    if category not in CATEGORIES:
        raise ValueError("Выберите продажу, подарок или тестовую выдачу.")
    if category != "manual_sale" and canonical["payment_received"]:
        raise ValueError("Для подарка или теста отметка оплаты не применяется.")
    tariff = None
    if not previous:
        if category == "manual_sale" and not canonical.get("tariff_id"):
            candidates = []
            for item in await repo.tariffs():
                if (
                    max(1, item.months * 30) == canonical["days"]
                    and item.device_limit == canonical["devices"]
                    and await repo.tariff_setting(item, "tariff_enabled", "1") == "1"
                ):
                    candidates.append(item)
            if len(candidates) != 1:
                raise ValueError(
                    "Выберите конкретный тариф для продажи. Произвольный срок доступен для подарка или теста."
                )
            canonical["tariff_id"] = candidates[0].id
        if canonical.get("tariff_id"):
            tariff = await repo.lock_tariff(
                await repo.tariff_by_id(canonical["tariff_id"])
            )
            if (
                not tariff
                or await repo.tariff_setting(tariff, "tariff_enabled", "1") != "1"
            ):
                raise ValueError("Тариф недоступен. Выберите действующий тариф.")
            if (
                max(1, tariff.months * 30) != canonical["days"]
                or tariff.device_limit != canonical["devices"]
            ):
                raise ValueError(
                    "Срок и количество устройств должны соответствовать выбранному тарифу."
                )
    fingerprint = hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode()
    ).hexdigest()
    if previous:
        if previous["fingerprint"] != fingerprint:
            raise Conflict(
                "Этот запрос уже используется с другими параметрами. Сначала проверьте результат предыдущей выдачи."
            )
        return previous
    await lock_customer(repo, user.id)
    devices, traffic = (
        await purchase_limits(repo, tariff) if tariff else (canonical["devices"], 0)
    )
    if subscription and not tariff:
        traffic = int(
            await repo.setting(f"subscription_traffic_limit:{subscription.id}", "0")
        )
    amount = (
        (await quote(repo, user.id, tariff, subscription.id if subscription else 0))[0]
        if category == "manual_sale"
        else 0
    )
    if type(amount) is not int or not 0 <= amount <= 100_000_000:
        raise ValueError("Проверьте цену и скидки тарифа. Учёт продажи не создан.")
    if (
        canonical.get("expected_amount") is not None
        and canonical["expected_amount"] != amount
    ):
        raise Conflict(
            "Цена изменилась. Обновите расчёт и подтвердите условия; выдача не выполнена."
        )
    stamp = now().isoformat()
    snapshot = dict(
        kind="vpn",
        source="manual",
        issuance_kind=category,
        tariff_id=tariff.id if tariff else None,
        tariff_name=await repo.tariff_setting(
            tariff, "tariff_name", f"{tariff.months} мес."
        )
        if tariff
        else "",
        months=tariff.months if tariff else 0,
        days=canonical["days"],
        devices=devices,
        traffic_bytes=traffic,
        total=amount,
        base_price=tariff.price if tariff else 0,
        renewal=bool(subscription),
        recorded_by=actor.id,
        issued_at=None,
        received_at=stamp if canonical["payment_received"] else None,
    )
    payment = None
    if category == "manual_sale":
        payment = await repo.add(
            Payment(
                user_id=user.id,
                subscription_id=subscription.id if subscription else None,
                amount=amount,
                status="manual_received_pending"
                if canonical["payment_received"]
                else "manual_preparing",
                provider_id=f"manual:{actor.id}:{canonical['request_id']}",
            )
        )
        await reserve(
            repo, payment, tariff, subscription.id if subscription else 0, amount
        )
        await repo.set_setting(f"sale_snapshot:{payment.id}", json.dumps(snapshot))
    op = dict(
        version=1,
        fingerprint=fingerprint,
        request=canonical,
        freeman_id=user.freeman_id,
        snapshot=snapshot,
        payment_id=payment.id if payment else None,
        created_at=stamp,
        result=None,
        server=server,
        # Renewal targets are calculated from fresh, locked subscription
        # state by I02. Only new grants freeze their starting expiration.
        expires_at=None
        if subscription
        else (
            extend(now(), tariff.months)
            if category == "manual_sale" and tariff.months > 0
            else now() + timedelta(days=canonical["days"])
        ).isoformat(),
    )
    await repo.set_setting(key, json.dumps(op))
    return op


async def finish(repo, actor, op, subscription, key):
    """Only call after the provider and local association are confirmed."""
    marker = operation_key(actor.id, op["request"]["request_id"])
    current = await read(repo, actor.id, op["request"]["request_id"])
    if current["result"]:
        return current["result"]
    snapshot = dict(
        op["snapshot"],
        issued_at=now().isoformat(),
        key_id=key.id,
        subscription_id=subscription.id,
    )
    if op["payment_id"]:
        payment = await repo.session.scalar(
            select(Payment).where(Payment.id == op["payment_id"]).with_for_update()
        )
        payment.subscription_id = subscription.id
        paid = bool(snapshot["received_at"])
        payment.status = "manual_paid" if paid else "manual_unpaid"
        if paid:
            snapshot["paid_at"] = snapshot["received_at"]
            await consume(repo, payment, snapshot["total"])
        await repo.set_setting(f"sale_snapshot:{payment.id}", json.dumps(snapshot))
    result = dict(
        user_id=subscription.user_id,
        key_id=key.id,
        freeman_id=op.get("freeman_id"),
        subscription_id=subscription.id,
        payment_id=op["payment_id"],
        issuance_kind=snapshot["issuance_kind"],
        amount=snapshot["total"],
        payment_received=bool(snapshot["received_at"]),
        done=True,
    )
    label = {
        "manual_sale": "Ручная продажа",
        "complimentary": "Подарок",
        "test": "Тест",
    }[snapshot["issuance_kind"]]
    await repo.add(
        AdminAudit(
            actor_telegram_id=actor.telegram_id,
            action="manual_issuance",
            target=str(subscription.user_id),
            details=f"{label} · ключ #{key.id} · {snapshot['days']} дней · {snapshot['devices']} устр. · {snapshot['total'] / 100:.2f} ₽",
        )
    )
    current.update(snapshot=snapshot, result=result)
    await repo.set_setting(marker, json.dumps(current))
    return result


async def confirm_received(repo, payment_id, actor=None):
    payment = await repo.session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if not payment or payment.status not in ("manual_unpaid", "manual_paid"):
        raise ValueError(
            "Подтверждать оплату можно только для выданной ручной продажи."
        )
    if payment.status == "manual_paid":
        return payment
    snapshot = json.loads(await repo.setting(f"sale_snapshot:{payment.id}", "{}"))
    if snapshot.get("issuance_kind") != "manual_sale" or not snapshot.get("issued_at"):
        raise ValueError("Выдача ключа ещё не подтверждена.")
    snapshot["received_at"] = snapshot["paid_at"] = now().isoformat()
    payment.status = "manual_paid"
    await consume(repo, payment, snapshot["total"])
    await repo.set_setting(f"sale_snapshot:{payment.id}", json.dumps(snapshot))
    if actor:
        await repo.add(
            AdminAudit(
                actor_telegram_id=actor.telegram_id,
                action="manual_payment_received",
                target=str(payment.id),
                details="External receipt confirmed; no provider charge or wallet debit",
            )
        )
    # Result recovery reads the current Payment; immutable intent/price is not
    # rewritten just because external receipt was confirmed later.
    return payment


async def renew(database, settings, actor, key_id, payload):
    """Separate new commercial intent from legacy administrative extensions.

    Price is frozen before dispatch; the expiry is calculated only while owning
    the existing customer/subscription renewal locks. I02 owns remote recovery.
    """
    if (
        payload.unit != "days"
        or not payload.value.isdigit()
        or not 1 <= int(payload.value) <= 3650
    ):
        raise ValueError("Выберите тариф или срок от 1 до 3650 дней.")
    if payload.amount or payload.payment_mode != "free" or payload.expires_at:
        raise ValueError(
            "Для ручной продажи используется расчёт тарифа, без списания баланса."
        )
    request = dict(
        request_id=payload.request_id,
        issuance_kind=payload.issuance_kind,
        tariff_id=payload.tariff_id,
        days=int(payload.value),
        key_id=key_id,
        payment_received=payload.payment_confirmed,
        expected_amount=payload.expected_amount,
    )
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        await session.execute(
            update(User).where(User.id == actor.id).values(status=User.status)
        )
        key = await session.get(AccessKey, key_id)
        credential = await session.scalar(
            select(VlessCredential).where(VlessCredential.access_key_id == key_id)
        )
        vpn = await session.scalar(
            select(VpnCredential).where(VpnCredential.access_key_id == key_id)
        )
        if not key or key.status == "deleted" or not (credential or vpn):
            raise ValueError("Не найдена действующая VPN-подписка.")
        await lock_customer(repo, key.user_id)
        sub = await session.get(Subscription, (credential or vpn).subscription_id)
        user = await session.get(User, key.user_id)
        if not sub or sub.user_id != key.user_id or user.status == "blocked":
            raise ValueError("Подписка или клиент недоступны.")
        previous = await read(repo, actor.id, payload.request_id)
        request["devices"] = (
            previous["request"]["devices"] if previous else sub.device_limit
        )
        if not previous and payload.tariff_id:
            tariff = await repo.tariff_by_id(payload.tariff_id)
            if not tariff:
                raise ValueError("Тариф недоступен.")
            request["devices"] = tariff.device_limit
        op = await prepare(repo, actor, user, request, subscription=sub)
        if op["result"]:
            return op["result"]
    async with database.sessions.begin() as session:
        repo = SQLRepository(session)
        await session.execute(
            update(User).where(User.id == actor.id).values(status=User.status)
        )
        op = await read(repo, actor.id, payload.request_id)
        if op["result"]:
            return op["result"]
        await lock_customer(repo, op["request"]["user_id"])
        sub = await session.scalar(
            select(Subscription)
            .where(Subscription.id == sub.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        key = await session.get(AccessKey, key_id, populate_existing=True)
        user = await session.get(User, op["request"]["user_id"])
        if (
            not sub
            or not key
            or sub.user_id != user.id
            or key.user_id != user.id
            or key.status == "deleted"
            or user.status == "blocked"
        ):
            raise Conflict(
                "Владелец или состояние подписки изменились; требуется проверка."
            )
        terms = op["snapshot"]
        base = max(now(), utc(sub.expires_at))
        expiry = (
            extend(base, terms["months"])
            if terms["issuance_kind"] == "manual_sale" and terms["months"] > 0
            else base + timedelta(days=terms["days"])
        )
        stamp = f"web_key_action:{actor.id}:{key_id}:{payload.request_id}"
        credential = await session.scalar(
            select(VlessCredential).where(VlessCredential.access_key_id == key_id)
        )
        if credential:
            from app.services.vless_provision import control

            activate = key.status == "active" or (
                key.status == "billing_blocked"
                and await repo.setting(f"billing_blocked:{key.id}") == "1"
            )
            result = await control(
                repo,
                settings,
                credential,
                "extend",
                expiry,
                renewal=dict(
                    operation_key=stamp,
                    request=dict(manual=op["fingerprint"]),
                    devices=terms["devices"]
                    if terms["tariff_id"]
                    else credential.device_limit,
                    traffic_limit=terms["traffic_bytes"]
                    if terms["tariff_id"]
                    else None,
                    activate=activate,
                ),
            )
            expiry = result.expires_at
        else:
            from app.services.vpn import VpnService

            vpn = await session.scalar(
                select(VpnCredential).where(VpnCredential.access_key_id == key_id)
            )
            if not vpn:
                raise Conflict("Связь с VPN-сервером изменилась; требуется проверка.")
            vpn.expires_at = sub.expires_at = expiry
            if terms["tariff_id"]:
                sub.device_limit = terms["devices"]
            await VpnService(repo, settings).set_subscription_status(
                sub.id, "active" if key.status == "active" else "disabled"
            )
        key.expires_at = sub.expires_at = expiry
        if terms["tariff_id"]:
            sub.tariff_id = terms["tariff_id"]
        result = await finish(repo, actor, op, sub, key)
        await repo.set_setting(stamp, op["fingerprint"])
        await repo.add(
            AdminAudit(
                actor_telegram_id=actor.telegram_id,
                action="web_key_extend",
                target=str(key.id),
                details=json.dumps(
                    dict(
                        issuance_kind=terms["issuance_kind"],
                        payment_id=op["payment_id"],
                        amount=terms["total"],
                    )
                ),
            )
        )
        return result
