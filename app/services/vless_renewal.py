"""Absolute, durable VLESS renewal with verified PATCH/enable recovery."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.database.models import (
    AccessKey,
    RemnawaveServer,
    Subscription,
    VlessCredential,
    VlessRenewal,
    now,
)
from app.services.remnawave import RemnawaveApiError, RemnawaveService
from app.services.vless_creation import durable_session, utc

PENDING = ("outcome_unknown", "patch_confirmed", "remote_confirmed")
REJECTED = (400, 401, 403, 404, 422)
MESSAGE = (
    "Продление VLESS пока не подтверждено. Повторите исходный заказ; "
    "повторно оплачивать или создавать другое продление не нужно."
)


@dataclass(frozen=True)
class RenewalResult:
    expires_at: datetime
    replayed: bool = False


def operation_id(user_id, key):
    return hashlib.sha256(f"vless-renew:{user_id}:{key}".encode()).hexdigest()


async def unresolved(repo, user, keys):
    return (
        await repo.session.scalar(
            select(VlessRenewal.id).where(
                VlessRenewal.id.in_([operation_id(user.id, key) for key in keys]),
                VlessRenewal.state.in_(PENDING),
            )
        )
        is not None
    )


async def locked_context(repo, credential):
    # Every renewal entry point uses the same lock order and fresh local base.
    sub = await repo.session.scalar(
        select(Subscription)
        .where(Subscription.id == credential.subscription_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    credential = await repo.session.get(
        VlessCredential, credential.id, populate_existing=True
    )
    if not credential:
        raise ValueError("Не найдена действующая связь подписки с VLESS.")
    key = await repo.session.get(
        AccessKey, credential.access_key_id, populate_existing=True
    )
    if (
        not sub
        or not key
        or sub.user_id != credential.user_id
        or key.user_id != sub.user_id
        or key.mode != "vless"
        or key.status == "deleted"
        or credential.status == "deleted"
    ):
        raise ValueError("Не найдена действующая связь подписки с VLESS.")
    return sub, key, credential


def same_time(a, b):
    return abs((utc(a) - utc(b)).total_seconds()) < 1


async def guard_expiry_sync(repo, credential, expiry):
    # Status-only reactivation still syncs the local expiry through control().
    # It must not overwrite an unresolved paid PATCH, or a newer committed term.
    sub, _, _ = await locked_context(repo, credential)
    pending = await repo.session.scalar(
        select(VlessRenewal.id).where(
            VlessRenewal.subscription_id == sub.id,
            VlessRenewal.state.in_(PENDING),
        )
    )
    if pending or not same_time(expiry, sub.expires_at):
        raise ValueError(MESSAGE)


def traffic_value(value):
    # Missing/invalid provider data is never interpreted as unlimited (zero).
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("Панель не подтвердила лимит трафика VLESS.")
    return value


def traffic_terms(operation):
    terms = json.loads(operation.request)["traffic"]
    traffic_value(terms["base"])
    traffic_value(terms["limit"])
    if type(terms["purchased"]) is not bool:
        raise ValueError("Некорректные условия трафика продления VLESS.")
    return terms


def squad_terms(operation,base=False):
    intent=json.loads(operation.request).get('intent',{})
    if intent.get('kind') not in ('bot_antig_upgrade','bot_antig_renewal','bot_antig_expire'):return None
    values=intent.get('active_squads',{}).get('base' if base else 'target')
    from uuid import UUID
    if not isinstance(values,list) or not values or len(values)>50 or any(not isinstance(v,str) or str(UUID(v))!=v for v in values):
        raise ValueError('Не подтверждены сквады изменения подписки.')
    return frozenset(values)


def snapshot(payload, operation):
    if not isinstance(payload, dict):
        raise ValueError("Некорректный ответ VLESS.")
    data = payload.get("response", payload)
    try:
        if (
            not isinstance(data, dict)
            or str(
                data.get("id")
                if operation.remote_identity.isdigit()
                else data.get("uuid")
            )
            != operation.remote_identity
        ):
            raise ValueError("Панель вернула другого пользователя.")
        expiry = utc(datetime.fromisoformat(data["expireAt"].replace("Z", "+00:00")))
        devices = data["hwidDeviceLimit"]
        if (
            not isinstance(devices, int)
            or isinstance(devices, bool)
            or devices < 0
            or data["status"] not in ("ACTIVE", "DISABLED", "EXPIRED", "LIMITED")
        ):
            raise ValueError("Панель не подтвердила параметры VLESS.")
        groups=None
        if squad_terms(operation) is not None:
            raw=data.get('activeInternalSquads')
            if not isinstance(raw,list):raise ValueError('Панель не подтвердила сквады.')
            groups=frozenset(v.get('uuid') if isinstance(v,dict) else v for v in raw)
        return (expiry, devices, data["status"], traffic_value(data["trafficLimitBytes"]),groups) if groups is not None else (expiry, devices, data["status"], traffic_value(data["trafficLimitBytes"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Панель не подтвердила состояние исходного ключа.") from error


def matches(remote, expiry, devices, traffic_limit, squads=None):
    return (
        same_time(remote[0], expiry)
        and remote[1] == devices
        and remote[3] == traffic_limit
        and (squads is None or (len(remote)>4 and remote[4]==squads))
    )


def failed(operation, error, *, state="outcome_unknown"):
    operation.state = state
    operation.last_error = f"{type(error).__name__}: HTTP {getattr(error, 'status', 0)}"
    operation.updated_at = now()


def valid_term(request, expiry, base, devices, old_devices):
    # This explicit bot-only intent changes allowance without selling extra days.
    # All existing renewal requests retain their strictly-increasing expiry rule.
    if request.get("kind") in ("bot_antig_upgrade","bot_antig_expire"):
        return utc(expiry)==utc(base) and type(devices) is int and devices==old_devices
    if request.get("kind") == "bot_device_upgrade":
        return (utc(expiry) == utc(base) and type(devices) is int
                and type(old_devices) is int and 0 < old_devices < devices <= 100)
    return utc(expiry) > utc(base)


async def prepare(
    repo,
    credential,
    sub,
    key,
    server,
    identity,
    request,
    expiry,
    devices,
    activate,
    traffic_limit,
):
    async with durable_session(repo) as session, session.begin():
        operation = await session.get(VlessRenewal, identity, with_for_update=True)
        values = dict(
            user_id=sub.user_id,
            subscription_id=sub.id,
            credential_id=credential.id,
            key_id=key.id,
            server_id=server.id,
            server_url=server.api_url,
            remote_identity=credential.remote_user_uuid,
            request=json.dumps(
                dict(intent=request, devices=devices, activate=activate), sort_keys=True
            ),
        )
        original = json.loads(operation.request) if operation else None
        if operation:
            # The original I02 intent remains comparable for old journal rows.
            # Quota is a frozen server-derived purchase term, not a new client
            # parameter. A catalog edit must not replace it during recovery.
            comparable = {k: v for k, v in original.items() if k != "traffic"}
            if any(
                getattr(operation, k) != v for k, v in values.items() if k != "request"
            ) or comparable != json.loads(values["request"]):
                raise ValueError("Параметры исходного продления изменились.")
            if operation.state == "applied":
                if utc(sub.expires_at) < utc(operation.expires_at):
                    raise ValueError(
                        "Срок ранее выполненного продления изменён. Нужна проверка."
                    )
                return operation
            if operation.state != "failed":
                if not same_time(sub.expires_at, operation.base_expires_at):
                    raise ValueError(
                        "Срок подписки изменён вне исходного продления. Нужна проверка."
                    )
                if "traffic" not in original:
                    # Old in-flight I02 writes never owned a traffic change.
                    # Don't guess a different historical paid allowance.
                    baseline = traffic_value(credential.traffic_limit)
                    if traffic_limit is not None and traffic_limit != baseline:
                        raise ValueError(
                            "В старом продлении не сохранён лимит трафика. Нужна проверка исходного заказа."
                        )
                    original["traffic"] = dict(
                        base=baseline,
                        limit=baseline,
                        purchased=traffic_limit is not None,
                    )
                    operation.request = json.dumps(original, sort_keys=True)
                    operation.updated_at = now()
                traffic_terms(operation)
                return operation
            # A confirmed rejection with verified unchanged remote state owns no
            # paid period. It can safely rebase after another completed renewal.
        pending = await session.scalar(
            select(VlessRenewal.id).where(
                VlessRenewal.subscription_id == sub.id,
                VlessRenewal.id != identity,
                VlessRenewal.state.in_(PENDING),
            )
        )
        if pending:
            raise ValueError("Сначала завершите проверку предыдущего продления VLESS.")
        if (
            not isinstance(devices, int)
            or isinstance(devices, bool)
            or devices < 0
            or not valid_term(request, expiry, sub.expires_at, devices, credential.device_limit)
        ):
            raise ValueError("Некорректные срок или лимит продления VLESS.")
        if operation is None:
            operation = VlessRenewal(id=identity, **values)
            session.add(operation)
        baseline = traffic_value(credential.traffic_limit)
        quota = original.get("traffic") if original else None
        if quota:
            # Only a confirmed rejection can rebase. Duration-only extensions
            # preserve any newer paid allowance; purchases retain their target.
            traffic_terms(operation)
            purchased = quota["purchased"]
            target = quota["limit"] if purchased else baseline
        else:
            purchased = traffic_limit is not None
            target = traffic_value(traffic_limit) if purchased else baseline
        journal_request = json.loads(values["request"])
        journal_request["traffic"] = dict(
            base=baseline, limit=target, purchased=purchased
        )
        operation.request = json.dumps(journal_request, sort_keys=True)
        operation.base_expires_at = utc(sub.expires_at)
        operation.base_device_limit = credential.device_limit
        operation.expires_at = utc(expiry)
        operation.device_limit = devices
        operation.target_status = "ACTIVE" if activate else "DISABLED"
        operation.state = "outcome_unknown"
        operation.patch_state = "not_started"
        operation.enable_state = "not_started" if activate else "not_required"
        operation.last_error = ""
        operation.updated_at = now()
        return operation


async def patch_expiry(repo, api, identity):
    error, dispatch = None, False
    async with durable_session(repo) as session, session.begin():
        op = await session.get(VlessRenewal, identity, with_for_update=True)
        try:
            limits = traffic_terms(op)
            remote = snapshot(await api.user(op.remote_identity), op)
            if utc(op.expires_at) <= now() and json.loads(op.request).get('intent',{}).get('kind')!='bot_antig_expire':
                raise ValueError("Исходный оплаченный срок уже истёк. Нужна проверка.")
            if matches(remote, op.expires_at, op.device_limit, limits["limit"], squad_terms(op)):
                op.patch_state, op.state = "confirmed", "patch_confirmed"
            elif not matches(
                remote, op.base_expires_at, op.base_device_limit, limits["base"], squad_terms(op,True)
            ):
                raise ValueError("Удалённый срок или лимит изменён вне операции.")
            elif op.patch_state not in ("not_started", "failed"):
                # A timed-out PATCH may still execute. Do not enqueue another
                # write that could arrive after a subsequent paid renewal.
                raise ValueError("Результат предыдущего PATCH ещё неизвестен.")
            else:
                op.patch_state = "outcome_unknown"
                dispatch = True
            op.updated_at = now()
        except (RemnawaveApiError, ValueError) as failure:
            failed(op, failure)
            error = ValueError(MESSAGE)
    if error:
        raise error
    if not dispatch:
        return

    # The uncertain-write marker is committed before dispatch, so a crash or
    # failed result commit cannot accidentally permit another blind PATCH.
    async with durable_session(repo) as session, session.begin():
        op = await session.get(VlessRenewal, identity, with_for_update=True)
        payload = {
            ("id" if op.remote_identity.isdigit() else "uuid"): int(op.remote_identity)
            if op.remote_identity.isdigit()
            else op.remote_identity,
            "expireAt": utc(op.expires_at).isoformat(),
            "hwidDeviceLimit": op.device_limit,
        }
        limits = traffic_terms(op)
        groups=squad_terms(op)
        if groups is not None:payload["activeInternalSquads"]=sorted(groups)
        if limits["purchased"]:
            payload["trafficLimitBytes"] = limits["limit"]
        if op.target_status == "DISABLED":
            payload["status"] = "DISABLED"
        rejection = None
        try:
            try:
                await api.update_user(payload)
            except RemnawaveApiError as failure:
                rejection = failure
            remote = snapshot(await api.user(op.remote_identity), op)
            if not matches(remote, op.expires_at, op.device_limit, limits["limit"], squad_terms(op)):
                if (
                    rejection
                    and rejection.status in REJECTED
                    and matches(
                        remote, op.base_expires_at, op.base_device_limit, limits["base"], squad_terms(op,True)
                    )
                ):
                    op.patch_state = "failed"
                    failed(op, rejection, state="failed")
                else:
                    failed(op, rejection or ValueError("Unconfirmed PATCH"))
                error = ValueError(MESSAGE)
            else:
                op.patch_state, op.state = "confirmed", "patch_confirmed"
                op.last_error = ""
        except (RemnawaveApiError, ValueError) as failure:
            failed(op, failure)
            error = ValueError(MESSAGE)
        op.updated_at = now()
    if error:
        raise error


async def enable(repo, api, identity):
    error, dispatch = None, False
    async with durable_session(repo) as session, session.begin():
        op = await session.get(VlessRenewal, identity, with_for_update=True)
        try:
            limits = traffic_terms(op)
            remote = snapshot(await api.user(op.remote_identity), op)
            if not matches(remote, op.expires_at, op.device_limit, limits["limit"], squad_terms(op)):
                raise ValueError("PATCH не подтверждён перед активацией.")
            if remote[2] == op.target_status:
                op.enable_state = (
                    "confirmed" if op.target_status == "ACTIVE" else "not_required"
                )
                op.state, op.last_error = "remote_confirmed", ""
            elif op.target_status == "ACTIVE":
                op.enable_state = "outcome_unknown"
                dispatch = True
            else:
                raise ValueError("Панель не сохранила блокировку ключа.")
            op.updated_at = now()
        except (RemnawaveApiError, ValueError) as failure:
            failed(op, failure, state="patch_confirmed")
            error = ValueError(MESSAGE)
    if error:
        raise error
    if not dispatch:
        return
    async with durable_session(repo) as session, session.begin():
        op = await session.get(VlessRenewal, identity, with_for_update=True)
        rejection = None
        limits = traffic_terms(op)
        try:
            try:
                await api.enable_user(op.remote_identity)
            except RemnawaveApiError as failure:
                rejection = failure
            remote = snapshot(await api.user(op.remote_identity), op)
            if (
                not matches(remote, op.expires_at, op.device_limit, limits["limit"], squad_terms(op))
                or remote[2] != "ACTIVE"
            ):
                op.enable_state = (
                    "failed"
                    if rejection and rejection.status in REJECTED
                    else "outcome_unknown"
                )
                failed(
                    op,
                    rejection or ValueError("Unconfirmed enable"),
                    state="patch_confirmed",
                )
                error = ValueError(MESSAGE)
            else:
                op.enable_state, op.state, op.last_error = (
                    "confirmed",
                    "remote_confirmed",
                    "",
                )
        except (RemnawaveApiError, ValueError) as failure:
            failed(op, failure, state="patch_confirmed")
            error = ValueError(MESSAGE)
        op.updated_at = now()
    if error:
        raise error


async def renew(
    repo,
    settings,
    credential,
    expiry,
    *,
    operation_key,
    request,
    devices,
    activate=True,
    traffic_limit=None,
):
    if request.get("kind") in ("bot_antig_upgrade","bot_antig_renewal","bot_antig_expire") and not str(operation_key).startswith("bot-antig:"):
        raise ValueError("Не подтверждена операция дополнения.")
    if not operation_key:
        raise ValueError("Для продления VLESS нужен устойчивый идентификатор оплаты.")
    if traffic_limit is not None:
        traffic_value(traffic_limit)
    sub, key, credential = await locked_context(repo, credential)
    server = await repo.session.get(RemnawaveServer, credential.server_id)
    if not server:
        raise ValueError("Панель исходного VLESS недоступна.")
    identity = operation_id(sub.user_id, operation_key)
    try:
        operation = await prepare(
            repo,
            credential,
            sub,
            key,
            server,
            identity,
            request,
            expiry,
            devices,
            activate,
            traffic_limit,
        )
        if operation.state == "applied":
            return RenewalResult(sub.expires_at, replayed=True)
        api = RemnawaveService(repo, settings).api(server)
        await patch_expiry(repo, api, identity)
        await enable(repo, api, identity)
        # Payment/debit, local expiry/limits/status and applied journal marker
        # share the caller's commit. A rollback leaves durable remote progress.
        operation = await repo.session.scalar(
            select(VlessRenewal)
            .where(VlessRenewal.id == identity)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if operation.state != "remote_confirmed":
            raise ValueError(MESSAGE)
        sub.expires_at = key.expires_at = credential.expires_at = operation.expires_at
        sub.device_limit = credential.device_limit = operation.device_limit
        credential.traffic_limit = traffic_terms(operation)["limit"]
        await repo.set_setting(
            f"subscription_traffic_limit:{sub.id}", str(credential.traffic_limit)
        )
        credential.status = "active" if activate else "disabled"
        credential.last_sync = now()
        if activate:
            key.status = "active"
            await repo.set_setting(f"billing_blocked:{key.id}", "0")
            await repo.set_setting(f"subscription_status:{sub.id}", "active")
        operation.state, operation.updated_at = "applied", now()
        return RenewalResult(operation.expires_at)
    except SQLAlchemyError as error:
        raise ValueError(MESSAGE) from error
