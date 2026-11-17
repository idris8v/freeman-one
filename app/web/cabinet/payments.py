"""Customer checkout, payment status, balance and payout endpoints."""

import json
import hashlib
import re
from datetime import timezone

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Cookie, HTTPException, Query, Response
from sqlalchemy import select, update, text

from app.database.models import (
    AccessKey,
    BalanceTransaction,
    ChatNotice,
    Payment,
    User,
    Withdrawal,
)
from app.database.repositories import SQLRepository
from app.services.pricing import quote
from app.web.cabinet.context import WebContext
from app.web.cabinet.schemas import CheckoutPayload, TopupPayload, WithdrawalPayload
from app.web.payout_details import (
    PayoutDetailsPayload,
    masked_details,
    normalize_details,
)


async def _checkout_operation(repo: SQLRepository, idem: str, request=None):
    raw = await repo.setting(idem)
    if not raw:
        return None
    try:
        operation = json.loads(raw)
        if not isinstance(operation, dict):
            raise ValueError
    except (ValueError, TypeError) as error:
        raise HTTPException(
            503, "Не удалось прочитать заказ. Обратитесь в поддержку."
        ) from error
    if "request" not in operation:
        # Historical results have no request fingerprint. Keep them readable,
        # but never guess their parameters or execute another purchase.
        if request is not None:
            raise HTTPException(
                409, "Заказ уже существует. Проверьте его статус или историю платежей."
            )
        return {"request": None, "result": operation}
    if request is not None and operation["request"] != request:
        raise HTTPException(
            409, "Этот ключ запроса уже использован для другой покупки."
        )
    return operation


async def _save_checkout(repo, idem, operation, result=None):
    if result is not None:
        operation["result"] = result
    await repo.set_setting(idem, json.dumps(operation))


def payments_router(context: WebContext) -> APIRouter:
    database = context.database
    settings = context.settings
    current_user = context.current_user
    cipher = context.cipher
    router = APIRouter()

    @router.get("/api/web/checkout")
    async def checkout_status(
        idempotency_key: str = Query(min_length=12, max_length=80),
        freeman_session: str | None = Cookie(default=None),
    ):
        user = await current_user(freeman_session)
        async with database.sessions() as session:
            operation = await _checkout_operation(
                SQLRepository(session), f"web_checkout:{user.id}:{idempotency_key}"
            )
            if operation is None:
                raise HTTPException(404, "Заказ пока не зарегистрирован")
            return operation

    async def finish_card_checkout(user, idem, request):
        from app.services.offers import lock_customer
        from app.services.topups import SubscriptionCheckoutService

        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            await lock_customer(repo, user.id)
            operation = await _checkout_operation(repo, idem, request)
            if operation.get("result") is not None:
                return operation["result"]
            payment = await session.get(Payment, operation["payment_id"])
            if not payment or payment.user_id != user.id:
                raise HTTPException(409, "Заказ недоступен. Обратитесь в поддержку.")
            if payment.status in {"order_paid", "order_canceled"}:
                # The bot/webhook can finish a staged invoice before the
                # website recovers its missing response.
                result = {
                    "status": "paid" if payment.status == "order_paid" else "canceled",
                    "payment_id": payment.id,
                    "amount": payment.amount,
                }
                if payment.status == "order_paid":
                    result["subscription_id"] = payment.subscription_id
                    key_id = await repo.setting(
                        f"subscription_key:{payment.subscription_id}"
                    )
                    result["key_id"] = int(key_id) if key_id.isdecimal() else None
                await _save_checkout(repo, idem, operation, result)
                return result
            service = SubscriptionCheckoutService(repo, settings)
            # The local order and its provider key were committed before this
            # network call. A timeout/retry must reuse both, never prepare anew.
            try:
                screen = await service.render(f"order_pay:{payment.id}", user)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            redirect = next(
                (
                    action[4:]
                    for row in screen.rows
                    for _, action in row
                    if action.startswith("url:https://")
                ),
                "",
            )
            if not redirect:
                raise HTTPException(502, "Платёжная система не вернула ссылку оплаты")
            order = await service.details(payment)
            result = {
                "status": "pending",
                "payment_id": payment.id,
                "redirect_url": redirect,
                "amount": payment.amount,
                "total": order["total"],
                "balance_applied": order["balance_applied"],
            }
            await _save_checkout(repo, idem, operation, result)
            return result

    @router.post("/api/web/checkout/cancel")
    async def cancel_unstarted_checkout(
        payload: CheckoutPayload, freeman_session: str | None = Cookie(default=None)
    ):
        from app.services.offers import lock_customer

        user = await current_user(freeman_session)
        idem = f"web_checkout:{user.id}:{payload.idempotency_key}"
        request = payload.model_dump(
            exclude={"idempotency_key"}
            | ({"tariff_id"} if payload.tariff_id is None else set())
            | ({"antig"} if not payload.antig else set())
            | ({"expected_amount"} if payload.expected_amount is None else set())
        )
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            await lock_customer(repo, user.id)
            operation = await _checkout_operation(repo, idem, request)
            if operation is None:
                operation = {"request": request, "result": None}
            # Never abandon a committed purchase or an ambiguous bank invoice.
            if not operation.get("payment_id") and operation.get("result") is None:
                from app.services.vless_creation import unresolved
                from app.services.vless_renewal import unresolved as renewal_unresolved

                keys = (idem, f"balance:web:{user.id}:{payload.idempotency_key}")
                if await unresolved(repo, user, keys) or await renewal_unresolved(
                    repo, user, keys
                ):
                    raise HTTPException(
                        409,
                        "Выдача ключа ещё проверяется. Повторите исходный заказ; менять покупку пока нельзя.",
                    )
                await _save_checkout(repo, idem, operation, {"status": "canceled"})
            return operation

    @router.post("/api/web/checkout")
    async def checkout(
        payload: CheckoutPayload, freeman_session: str | None = Cookie(default=None)
    ):
        from app.services.billing import BillingService
        from app.services.offers import lock_customer

        user = await current_user(freeman_session)
        idem = f"web_checkout:{user.id}:{payload.idempotency_key}"
        request = payload.model_dump(
            exclude={"idempotency_key"}
            | ({"tariff_id"} if payload.tariff_id is None else set())
            | ({"antig"} if not payload.antig else set())
            | ({"expected_amount"} if payload.expected_amount is None else set())
        )
        # Bind the key durably even if the next transaction/network call fails.
        # Reuse C01's customer lock; no independent/advisory locking scheme.
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            operation = await _checkout_operation(repo, idem, request)
            if operation and operation.get("result") is not None:
                return operation["result"]
            if not operation or not operation.get("payment_id"):
                tariff = await repo.lock_tariff(
                    await repo.select_tariff(payload.months, payload.tariff_id)
                )
                if tariff:
                    from app.services.billing import purchase_limits

                    try:
                        await purchase_limits(repo, tariff)
                    except ValueError as error:
                        raise HTTPException(422, str(error)) from error
            await lock_customer(repo, user.id)
            operation = await _checkout_operation(repo, idem, request)
            if operation and operation.get("result") is not None:
                return operation["result"]
            if operation is None:
                operation = {"request": request, "result": None}
                await _save_checkout(repo, idem, operation)

        if operation.get("payment_id"):
            return await finish_card_checkout(user, idem, request)

        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            validation_error = None
            sub, key = (None, None)
            try:
                tariff = await repo.select_tariff(payload.months, payload.tariff_id)
                if (
                    not tariff
                    or await repo.tariff_setting(tariff, "tariff_enabled", "1") != "1"
                ):
                    raise HTTPException(404, "Тариф недоступен")
                if payload.subscription_id:
                    from app.services.recurring_cards import unsettled_card

                    if await unsettled_card(repo, payload.subscription_id):
                        raise HTTPException(
                            409,
                            "Платёж автопродления ещё проверяется. Не оплачивайте повторно; обратитесь в поддержку.",
                        )
                    try:
                        sub, key = await BillingService(repo, settings).valid(
                            user, payload.subscription_id
                        )
                    except ValueError as error:
                        raise HTTPException(409, str(error)) from error
            except HTTPException as error:
                validation_error = error
            await lock_customer(repo, user.id)
            # A concurrent executor may have finished while we waited.
            operation = await _checkout_operation(repo, idem, request)
            if operation.get("result") is not None:
                return operation["result"]
            if not operation.get("payment_id"):
                # Replay wins even if catalogue/access changed while waiting.
                if validation_error is not None:
                    raise validation_error
                if payload.antig and settings.is_owner(user.telegram_id):
                    raise HTTPException(422, "Для административной выдачи используйте раздел подписок.")
                if settings.is_owner(user.telegram_id):
                    try:
                        sub, key = await BillingService(repo, settings).fulfill(
                            user, tariff, sub, key, operation_key=idem
                        )
                    except ValueError as error:
                        raise HTTPException(409, str(error)) from error
                    sub.kind = "admin"
                    result = {
                        "status": "paid",
                        "subscription_id": sub.id,
                        "key_id": key.id,
                        "amount": 0,
                    }
                    await _save_checkout(repo, idem, operation, result)
                    return result
                amount = (
                    await quote(repo, user.id, tariff, payload.subscription_id or 0)
                )[0]
                balance = await repo.balance(user.id)
                if payload.antig:
                    from app.web.purchase_antig import prepare, create_order, settle_balance
                    if payload.subscription_id or payload.use_balance:
                        raise HTTPException(422, "Антиглушилка доступна для новой подписки. Выберите один способ оплаты.")
                    from app.services.topups import SubscriptionCheckoutService
                    try:
                        prepared = await prepare(repo, settings, tariff, amount, True, payload.expected_amount)
                        payment = await create_order(repo, settings, user, tariff, prepared,
                            method=payload.method, return_url=f"{settings.web_base_url.rstrip('/')}/?payment=return")
                        if payload.method == "balance":
                            sub, key = await settle_balance(repo, settings, payment, user)
                            result = {"status": "paid", "subscription_id": sub.id,
                                      "key_id": key.id, "amount": payment.amount}
                            await _save_checkout(repo, idem, operation, result)
                            return result
                    except ValueError as error:
                        raise HTTPException(409, str(error)) from error
                    operation["payment_id"] = payment.id
                    await _save_checkout(repo, idem, operation)
                elif payload.method == "balance" or (
                    payload.use_balance and balance >= amount
                ):
                    try:
                        sub = await BillingService(repo, settings).charge(
                            user,
                            tariff,
                            sub,
                            key,
                            amount,
                            f"web:{user.id}:{payload.idempotency_key}",
                        )
                    except ValueError as error:
                        raise HTTPException(409, str(error)) from error
                    result = {
                        "status": "paid",
                        "subscription_id": sub.id,
                        "key_id": int(await repo.setting(f"subscription_key:{sub.id}")),
                        "amount": amount,
                    }
                    await _save_checkout(repo, idem, operation, result)
                    return result
                if not payload.antig:
                    if (
                        not (settings.yookassa_shop_id and settings.yookassa_secret_key)
                        or await repo.setting("payment_provider:yookassa", "1") != "1"
                    ):
                        raise HTTPException(409, "Оплата через ЮKassa сейчас недоступна")
                    from app.services.topups import SubscriptionCheckoutService
    
                    try:
                        payment = await SubscriptionCheckoutService(
                            repo, settings
                        ).create_order(
                            user,
                            f"id{tariff.id}",
                            sid=payload.subscription_id or 0,
                            balance_applied=min(balance, amount)
                            if payload.use_balance
                            else 0,
                            return_url=f"{settings.web_base_url.rstrip('/')}/?payment=return",
                        )
                    except ValueError as error:
                        raise HTTPException(409, str(error)) from error
                    operation["payment_id"] = payment.id
                    await repo.set_setting(f"sale_source:{payment.id}", "website")
                    await _save_checkout(repo, idem, operation)
        return await finish_card_checkout(user, idem, request)

    async def read_payout_details(repo, user_id):
        token = await repo.setting(f"web:payout-details:{user_id}")
        if not token:
            return None
        try:
            return json.loads(cipher().decrypt(token.encode()).decode())
        except (InvalidToken, ValueError):
            raise HTTPException(503, "Не удалось загрузить реквизиты. Попробуйте позже")

    @router.get("/api/web/payout-details")
    async def payout_details(freeman_session: str | None = Cookie(default=None)):
        user = await current_user(freeman_session)
        async with database.sessions() as session:
            return {
                "details": await read_payout_details(SQLRepository(session), user.id)
            }

    @router.put("/api/web/payout-details")
    async def save_payout_details(
        payload: PayoutDetailsPayload,
        freeman_session: str | None = Cookie(default=None),
    ):
        user = await current_user(freeman_session)
        data = normalize_details(payload)
        encrypted = (
            cipher().encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()
        )
        async with database.sessions.begin() as session:
            await SQLRepository(session).set_setting(
                f"web:payout-details:{user.id}", encrypted
            )
        return {"details": masked_details(data)}

    @router.get("/api/web/balance")
    async def balance_details(response: Response, freeman_session: str | None = Cookie(default=None)):
        user = await current_user(freeman_session)
        response.headers["Cache-Control"] = "no-store"
        async with database.sessions() as session:
            # Balance, ledger and withdrawal states must describe one committed snapshot.
            if session.bind.dialect.name == "postgresql":
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            repo = SQLRepository(session)
            transactions = list(
                await session.scalars(
                    select(BalanceTransaction)
                    .where(BalanceTransaction.user_id == user.id)
                    .order_by(BalanceTransaction.id.desc())
                    .limit(100)
                )
            )
            payments = list(
                await session.scalars(
                    select(Payment)
                    .where(Payment.user_id == user.id)
                    .order_by(Payment.id.desc())
                    .limit(100)
                )
            )
            withdrawals = list(
                await session.scalars(
                    select(Withdrawal)
                    .where(Withdrawal.user_id == user.id, Withdrawal.status != "draft")
                    .order_by(Withdrawal.id.desc())
                    .limit(50)
                )
            )

            def masked(item):
                try:
                    raw = cipher().decrypt(item.details.encode()).decode()
                    data = json.loads(raw) if raw.startswith("{") else {"account": raw}
                    value = "".join(
                        ch for ch in str(data.get("account", "")) if ch.isdigit()
                    )
                    shown = (
                        ("*" * (max(0, len(value) - 4)) + value[-4:])
                        if value
                        else "Реквизиты скрыты"
                    )
                    return {
                        "method": data.get("method", ""),
                        "account": shown,
                        "owner_name": data.get("owner_name", ""),
                    }
                except Exception:
                    return {
                        "method": "",
                        "account": "Реквизиты скрыты",
                        "owner_name": "",
                    }

            operations = [
                {
                    "id": f"t{x.id}",
                    "type": x.type,
                    "amount": x.amount,
                    "description": x.description,
                    "status": "completed",
                    "created_at": x.created_at.replace(tzinfo=timezone.utc).isoformat(),
                }
                for x in transactions
            ]
            transaction_payment_ids = set()
            for item in transactions:
                found = (
                    re.search(r"#(\d+)", item.description or "")
                    if item.type == "yookassa_topup"
                    else None
                )
                if found:
                    transaction_payment_ids.add(int(found.group(1)))
            for item in payments:
                if item.id in transaction_payment_ids or item.status == "balance_paid":
                    continue
                if item.status in ("cancelled", "refunded", "failed"):
                    continue
                if item.status.startswith("manual_"):
                    title = "Ручная продажа · оплата вне сайта"
                    amount = -item.amount
                elif item.status.startswith("topup"):
                    title = "Пополнение через ЮKassa"
                    amount = item.amount
                elif item.status.startswith("proxy"):
                    title = "Покупка Telegram-прокси"
                    amount = -item.amount
                else:
                    title = "Покупка подписки"
                    amount = -item.amount
                operations.append(
                    {
                        "id": f"p{item.id}",
                        "type": "payment",
                        "amount": amount,
                        "description": title,
                        "status": item.status,
                        "created_at": item.created_at.replace(
                            tzinfo=timezone.utc
                        ).isoformat(),
                    }
                )
            operations.sort(key=lambda x: x["created_at"], reverse=True)
            return {
                "payout_details": masked_details(
                    await read_payout_details(repo, user.id)
                ),
                "balance": await repo.balance(user.id),
                "withdraw_min": max(1, int(await repo.setting("withdraw_min", "1000")))
                * 100,
                "withdraw_enabled": await repo.setting("withdraw_enabled", "1") == "1",
                "operations": operations[:150],
                "transactions": [
                    {
                        "id": x.id,
                        "type": x.type,
                        "amount": x.amount,
                        "description": x.description,
                        "created_at": x.created_at.replace(
                            tzinfo=timezone.utc
                        ).isoformat(),
                    }
                    for x in transactions
                ],
                "payments": [
                    {
                        "id": x.id,
                        "amount": x.amount,
                        "status": x.status,
                        "created_at": x.created_at.replace(
                            tzinfo=timezone.utc
                        ).isoformat(),
                    }
                    for x in payments
                ],
                "withdrawal_notifications": [
                    {
                        "id": f"payout-{x.id}-{x.status}",
                        "title": "Выплата выполнена" if x.status == "paid" else "Заявка на вывод отклонена",
                        "text": f"Заявка #{x.id} · {x.amount // 100},{x.amount % 100:02d} ₽. " + (
                            "Администратор подтвердил выплату." if x.status == "paid" else "Зарезервированная сумма возвращена на баланс."
                        ),
                        "target": "balance",
                        "created_at": (x.processed_at or x.created_at).replace(tzinfo=timezone.utc).isoformat(),
                    }
                    for x in withdrawals if x.status in ("paid", "rejected")
                ],
                "withdrawals": [
                    {
                        "id": x.id,
                        "amount": x.amount,
                        "status": x.status,
                        "created_at": x.created_at.replace(
                            tzinfo=timezone.utc
                        ).isoformat(),
                        **masked(x),
                    }
                    for x in withdrawals
                ],
            }

    @router.post("/api/web/topups")
    async def create_topup(
        payload: TopupPayload, freeman_session: str | None = Cookie(default=None)
    ):
        user = await current_user(freeman_session)
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            from app.services.topups import TopupService

            service = TopupService(repo, settings)
            try:
                preview = await service.prepare(user, payload.amount)
                action = preview.rows[0][0][1]
                await repo.set_setting(
                    f"topup_return_url:{action.split(':')[-1]}",
                    f"{settings.web_base_url.rstrip('/')}/?payment=return",
                )
                screen = await service.render(action, user)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            redirect = next(
                (
                    target[4:]
                    for row in screen.rows
                    for _, target in row
                    if target.startswith("url:https://")
                ),
                "",
            )
            payment = await session.scalar(
                select(Payment)
                .where(Payment.user_id == user.id, Payment.status == "topup_pending")
                .order_by(Payment.id.desc())
                .limit(1)
            )
            if not redirect or not payment:
                raise HTTPException(502, "Платёжная система не вернула ссылку")
            return {
                "payment_id": payment.id,
                "status": payment.status,
                "redirect_url": redirect,
                "amount": payment.amount,
            }

    @router.get("/api/web/payments/{payment_id}")
    async def payment_status(
        payment_id: int, freeman_session: str | None = Cookie(default=None)
    ):
        user = await current_user(freeman_session)
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            payment = await session.get(Payment, payment_id)
            if not payment or payment.user_id != user.id:
                raise HTTPException(404, "Платёж не найден")
            confirmed = False
            if payment.provider_id and payment.status in (
                "topup_pending",
                "order_pending",
                "proxy_order_pending",
            ):
                from app.services.topups import (
                    SubscriptionCheckoutService,
                    TopupService,
                    YooKassa,
                )

                original_status = payment.status
                data = await YooKassa(settings).get(payment.provider_id)
                try:
                    async with session.begin_nested():
                        if original_status == "topup_pending":
                            await TopupService(repo, settings).credit(payment, data)
                        elif original_status == "order_pending":
                            await SubscriptionCheckoutService(repo, settings).credit(
                                payment, data
                            )
                        else:
                            from app.services.proxies import ProxyCheckoutService

                            await ProxyCheckoutService(repo, settings).credit(
                                payment, data
                            )
                except (ValueError, RuntimeError, OSError):
                    await session.refresh(payment)
                    if original_status == "order_pending":
                        from app.web.post_payment import subscription_confirmed

                        confirmed = await subscription_confirmed(
                            payment,
                            data,
                            SubscriptionCheckoutService(repo, settings),
                            settings,
                        )
            key_id = None
            if payment.subscription_id and payment.status in (
                "order_paid",
                "balance_paid",
                "stars_paid",
                "manual_paid",
                "succeeded",
            ):
                linked_id = await repo.setting(
                    f"subscription_key:{payment.subscription_id}"
                )
                key = (
                    await session.get(AccessKey, int(linked_id)) if linked_id else None
                )
                if key and key.user_id == user.id and key.status != "deleted":
                    key_id = key.id
            try:
                order = json.loads(await repo.setting(f"order:{payment.id}", "{}"))
            except json.JSONDecodeError:
                order = {}
            return {
                "id": payment.id,
                "status": payment.status,
                "confirmed": confirmed,
                "amount": payment.amount,
                "subscription_id": payment.subscription_id,
                "key_id": key_id,
                "months": order.get("months"),
                "created_at": payment.created_at.replace(
                    tzinfo=timezone.utc
                ).isoformat(),
            }

    @router.post("/api/web/withdrawals")
    async def create_withdrawal(
        payload: WithdrawalPayload, freeman_session: str | None = Cookie(default=None)
    ):
        user = await current_user(freeman_session)
        if payload.request_id is None:
            raise HTTPException(409, "Обновите страницу перед созданием заявки на вывод")
        data = normalize_details(
            PayoutDetailsPayload(
                owner_name=payload.owner_name,
                bank=payload.bank,
                method=payload.method,
                account=payload.account,
            )
        )
        account = data["account"]
        async with database.sessions.begin() as session:
            repo = SQLRepository(session)
            # Same short user lock as bot withdrawals and checkout. The durable
            # request journal, withdrawal, reserve and notice commit atomically.
            await session.execute(update(User).where(User.id == user.id).values(status=User.status))
            journal = f"withdraw_request:{user.id}:{payload.request_id}" if payload.request_id else None
            fingerprint = hashlib.sha256(json.dumps(dict(data, amount=payload.amount), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            existing = await repo.setting(journal) if journal else None
            if existing:
                record = json.loads(existing)
                if record.get("fingerprint") != fingerprint:
                    raise HTTPException(409, "Этот запрос уже использован с другими реквизитами или суммой")
                item = await session.get(Withdrawal, record["id"])
                if not item or item.user_id != user.id:
                    raise HTTPException(409, "Сохранённая заявка требует проверки")
                return {"id": item.id, "status": item.status, "amount": item.amount}
            minimum = max(1, int(await repo.setting("withdraw_min", "1000"))) * 100
            if await repo.setting("withdraw_enabled", "1") != "1":
                raise HTTPException(409, "Заявки на вывод временно отключены")
            balance = await repo.balance(user.id)
            if payload.amount < minimum:
                raise HTTPException(
                    422, f"Минимальная сумма вывода — {minimum // 100} ₽"
                )
            if payload.amount > balance:
                raise HTTPException(409, "На балансе недостаточно средств")
            details = json.dumps(
                {
                    "owner_name": payload.owner_name.strip(),
                    "method": payload.method,
                    "account": account,
                    "bank": payload.bank.strip(),
                },
                ensure_ascii=False,
            )
            item = await repo.add(
                Withdrawal(
                    user_id=user.id,
                    amount=payload.amount,
                    details=cipher().encrypt(details.encode()).decode(),
                    status="pending",
                )
            )
            await repo.add(
                BalanceTransaction(
                    user_id=user.id,
                    type="withdrawal_hold",
                    amount=-payload.amount,
                    description=f"Резерв на вывод #{item.id}",
                )
            )
            for admin_id in settings.owner_ids():
                await repo.add(
                    ChatNotice(
                        dedup=f"web:withdrawal:{item.id}:{admin_id}",
                        target_id=admin_id,
                        kind="client",
                        text=f"💸 Новая заявка на вывод #{item.id}: {payload.amount / 100:.2f} ₽. Клиент {user.telegram_id}.",
                        buttons="[]",
                    )
                )
            if journal:
                await repo.set_setting(journal, json.dumps({"fingerprint": fingerprint, "id": item.id}))
            return {"id": item.id, "status": item.status, "amount": item.amount}

    @router.get("/api/web/withdrawals/requests/{request_id}")
    async def withdrawal_request_status(request_id: str, response: Response, freeman_session: str | None = Cookie(default=None)):
        user = await current_user(freeman_session)
        response.headers["Cache-Control"] = "no-store"
        if not re.fullmatch(r"[a-zA-Z0-9-]{10,64}", request_id):
            raise HTTPException(422, "Некорректный идентификатор запроса")
        async with database.sessions() as session:
            raw = await SQLRepository(session).setting(f"withdraw_request:{user.id}:{request_id}")
            item = await session.get(Withdrawal, json.loads(raw)["id"]) if raw else None
            if not item or item.user_id != user.id:
                raise HTTPException(404, "Заявка пока не найдена")
            return {"id": item.id, "status": item.status, "amount": item.amount}

    return router
