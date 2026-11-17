"""Durable, purchase-scoped RemnaWave creation and uncertain-outcome recovery."""

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.database.models import Subscription, User, VlessCreation, now
from app.services.remnawave import RemnawaveApiError, RemnawaveService


@asynccontextmanager
async def durable_session(repo):
    bind = repo.session.bind
    if bind.dialect.name != "postgresql":
        raise ValueError("Надёжная выдача VLESS требует PostgreSQL.")
    path = repo.session.info.get("vless_journal_search_path")
    if path is None:
        path = await repo.session.scalar(text("SHOW search_path"))
        repo.session.info["vless_journal_search_path"] = path
    # A dedicated short-lived connection avoids exhausting the request pool
    # while all its connections hold/wait on customer transaction locks.
    engine = create_async_engine(
        getattr(bind, "engine", bind).url,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "server_settings": {
                "search_path": path,
                "timezone": "UTC",
                "application_name": "freemanone-vless-journal",
            }
        },
    )
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


async def customer_ref(repo, settings, user):
    if user.registration_source == "external":
        from cryptography.fernet import Fernet

        from app.services.external_contacts import index

        token = await repo.setting(f"external_contact:{user.id}")
        if token:
            contact = (
                Fernet(settings.encryption_key.encode())
                .decrypt(token.encode())
                .decode()
            )
            reference = index(settings, contact)
            if not reference:
                raise ValueError("Не удалось подтвердить клиента исходной выдачи.")
            return reference
    async with durable_session(repo) as session:
        if not await session.get(User, user.id):
            raise ValueError("Сохраните клиента перед выдачей VLESS.")
    return f"user:{user.id}"


async def operation_id(repo, settings, sub, user, operation_key):
    async with durable_session(repo) as session:
        stored = await session.get(Subscription, sub.id)
    if stored:
        if stored.user_id != user.id:
            raise ValueError("Подписка недоступна для выдачи VLESS.")
        # One committed subscription always means one creation, regardless of
        # how many entry points/request keys try to provision it concurrently.
        operation_key = f"subscription:{sub.id}"
    elif not operation_key:
        raise ValueError(
            "Для новой выдачи VLESS нужен устойчивый идентификатор заказа."
        )
    customer = await customer_ref(repo, settings, user)
    return creation_id(customer, operation_key)


def creation_id(customer, operation_key):
    return hashlib.sha256(
        f"vless-create:{customer}:{operation_key}".encode()
    ).hexdigest()


async def unresolved(repo, user, operation_keys):
    # Checkout cancellation holds the same customer lock as execution. An
    # uncertain remote creation is already started, even if its local purchase
    # rolled back and therefore has no committed Payment/Subscription yet.
    rows = await repo.session.execute(
        select(VlessCreation.id, VlessCreation.customer_ref).where(
            VlessCreation.user_id == user.id,
            VlessCreation.state.in_(("outcome_unknown", "remote_confirmed")),
        )
    )
    return any(
        identity == creation_id(customer, key)
        for identity, customer in rows
        for key in operation_keys
    )


async def recorded(repo, identity):
    async with durable_session(repo) as session:
        return await session.get(VlessCreation, identity)


def utc(value):
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def validate_limits(devices, traffic):
    # Zero is the supported unlimited value. bool is an int subclass in Python,
    # but is never a numeric provider allowance or a purchased limit.
    for value, maximum, label in (
        (devices, 2**31 - 1, "устройств"),
        (traffic, 2**63 - 1, "трафика"),
    ):
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(
                f"Некорректный лимит {label} VLESS: требуется целое число от 0."
            )


def validate_subscription_url(value):
    if (
        not isinstance(value, str)
        or "\\" in value
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Панель вернула некорректную ссылку подписки VLESS.")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        )
        parsed.port  # Reject malformed/out-of-range ports, without normalizing URL.
    except ValueError as error:
        raise ValueError(
            "Панель вернула некорректную ссылку подписки VLESS."
        ) from error
    if not valid:
        raise ValueError("Панель вернула некорректную ссылку подписки VLESS.")


def confirmed(data, body):
    validate_limits(body.get("hwidDeviceLimit"), body.get("trafficLimitBytes"))
    if not isinstance(data, dict):
        raise ValueError("Панель вернула некорректный ответ выдачи.")
    data = data.get("response", data)
    if not isinstance(data, dict):
        raise ValueError("Панель вернула некорректный ответ выдачи.")
    identity = data.get("id") or data.get("uuid")
    url = data.get("subscriptionUrl", "")
    try:
        validate_limits(data.get("hwidDeviceLimit"), data.get("trafficLimitBytes"))
        validate_subscription_url(url)
        identity_valid = (
            isinstance(identity, int)
            and not isinstance(identity, bool)
            and identity > 0
        ) or (
            isinstance(identity, str)
            and (identity.isdecimal() and int(identity) > 0 or bool(UUID(identity)))
        )
        squads = [
            s.get("uuid") if isinstance(s, dict) else s
            for s in data.get("activeInternalSquads", [])
        ]
        expiry = utc(datetime.fromisoformat(data["expireAt"].replace("Z", "+00:00")))
        expected = utc(datetime.fromisoformat(body["expireAt"]))
        valid = (
            identity_valid
            and data.get("username") == body["username"]
            and data.get("description") == body["description"]
            and data.get("status") == "ACTIVE"
            and expiry > now()
            and abs((expiry - expected).total_seconds()) < 1
            and data.get("hwidDeviceLimit") == body["hwidDeviceLimit"]
            and data.get("trafficLimitBytes") == body["trafficLimitBytes"]
            and data.get("trafficLimitStrategy") == body["trafficLimitStrategy"]
            and set(squads) == set(body["activeInternalSquads"])
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(
            "Панель не подтвердила параметры и принадлежность создаваемого ключа."
        )
    return {
        "identity": str(identity),
        "subscription_url": url,
        "short_uuid": data.get("shortUuid") or "",
    }


async def recover(
    repo, settings, identity, sub, user, server, traffic_limit, *, automatic=False
):
    validate_limits(sub.device_limit, traffic_limit)
    customer = await customer_ref(repo, settings, user)
    from app.services.bot_antig_test import creation_squads
    body = dict(
        username="fo_" + identity[:32],
        status="ACTIVE",
        expireAt=utc(sub.expires_at).isoformat(),
        hwidDeviceLimit=sub.device_limit,
        activeInternalSquads=await creation_squads(repo, sub, server),
        trafficLimitBytes=traffic_limit,
        trafficLimitStrategy="NO_RESET",
        description=f"FO provisioning:{identity}",
    )
    from app.services import vless_capacity as capacity

    # Persist admission and dispatch ownership before HTTP. A bounded durable
    # lease replaces I01's long-lived journal row lock during remote calls.
    deadline = time.monotonic() + 55
    while True:
        claim = await capacity.claim(
            repo,
            identity,
            customer,
            user.id,
            server.id,
            server.api_url,
            body,
            automatic=automatic,
        )
        if claim:
            break
        if time.monotonic() >= deadline:
            raise ValueError("Выдача VLESS ещё проверяется. Повторите исходный заказ.")
        await asyncio.sleep(0.1)
    from app.services.vless_fallback import (
        NotCreated,
        confirmed_absence,
        dispatch,
        safe_rejection,
    )

    original = claim.request
    rejected = False
    try:
        api = RemnawaveService(repo, settings).api(claim.panel)
        try:
            data = await api.user_by_username(original["username"])
        except RemnawaveApiError as lookup:
            if not confirmed_absence(lookup):
                # The durable pre-POST fence proves a pristine operation has
                # never dispatched a create, even if this read timed out. A
                # legacy or previously uncertain POST can never use this path.
                rejected = claim.pristine
                raise
            if claim.result is not None:
                raise ValueError(
                    "Ранее подтверждённый ключ отсутствует в панели. Нужна проверка."
                )
            if not claim.can_create:
                rejected = claim.pristine
                raise ValueError("Панель VLESS сейчас недоступна для новой выдачи.")
            await dispatch(repo, identity, claim)
            try:
                data = await api.create_user(original)
            except RemnawaveApiError as creation:
                # A conflicting/still-running POST is recovered by stable username.
                try:
                    data = await api.user_by_username(original["username"])
                except RemnawaveApiError as check:
                    rejected = safe_rejection(claim, creation, check)
                    raise creation
        result = confirmed(data, original)
    except (RemnawaveApiError, ValueError) as failure:
        try:
            await capacity.finish(
                repo, identity, claim, failure=failure, rejected=rejected
            )
        except SQLAlchemyError:
            try:
                await capacity.clear_failed_commit_lease(repo, identity, claim)
            except SQLAlchemyError:
                pass
            raise
        raise (NotCreated if rejected else ValueError)(
            "Панель отклонила выдачу ключа. Исправьте причину и повторите тот же заказ."
            if rejected
            else "Результат выдачи VLESS пока не подтверждён. Повтор проверит тот же ключ; новую покупку создавать не нужно."
        ) from failure
    try:
        await capacity.finish(repo, identity, claim, result=result)
    except SQLAlchemyError:
        try:
            await capacity.clear_failed_commit_lease(repo, identity, claim)
        except SQLAlchemyError:
            pass
        raise
    return original, result


async def confirm_remote(*args, **kwargs):
    try:
        return await recover(*args, **kwargs)
    except SQLAlchemyError as error:
        raise ValueError(
            "Не удалось сохранить результат выдачи VLESS. Повторите тот же заказ для проверки ключа."
        ) from error
