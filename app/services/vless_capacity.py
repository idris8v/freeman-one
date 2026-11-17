"""Short PostgreSQL capacity transactions; no external HTTP under these locks."""

import json
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm.attributes import set_committed_value

from app.database.models import RemnawaveServer, VlessCreation, VlessReservation, now
from app.services.vless_creation import durable_session, utc

LEASE_SECONDS = 120  # Three provider requests have a 15-second timeout each.


class CapacityUnavailable(ValueError):
    pass


async def pending(session, server_id):
    return await session.scalar(
        select(func.count())
        .select_from(VlessReservation)
        .where(
            VlessReservation.server_id == server_id,
            VlessReservation.state == "reserved",
        )
    )


def available(panel):
    return (
        panel
        and panel.status == "active"
        and panel.health != "offline"
        and bool(panel.internal_squad_uuid)
    )


@dataclass
class Claim:
    token: str
    panel: RemnawaveServer
    request: dict
    result: str | None
    pristine: bool
    can_create: bool


async def claim(
    repo, identity, customer, user_id, server_id, server_url, body, *, automatic=False
):
    async with durable_session(repo) as session, session.begin():
        # Admission takes the panel lock and only *tries* the operation lock.
        panel = await session.get(RemnawaveServer, server_id, with_for_update=True)
        if not panel or panel.api_url != server_url:
            raise ValueError("Панель исходной выдачи VLESS изменилась.")
        operation = await session.scalar(
            select(VlessCreation)
            .where(VlessCreation.id == identity)
            .with_for_update(skip_locked=True)
        )
        if operation is None and await session.get(VlessCreation, identity):
            # The caller may be committing the local association. Never wait
            # for that transaction while monopolizing this panel's admission.
            return None
        if operation and (
            operation.server_id != panel.id or operation.server_url != panel.api_url
        ):
            from app.services.vless_fallback import PlacementChanged

            raise PlacementChanged("Панель операции выдачи VLESS изменилась.")
        if operation:
            original = json.loads(operation.request)
            comparable = lambda value: {
                k: v for k, v in value.items() if k != "expireAt"
            }
            if (
                operation.customer_ref != customer
                or (
                    operation.credential_id is not None and operation.user_id != user_id
                )
                or operation.server_id != panel.id
                or operation.server_url != panel.api_url
                or comparable(original) != comparable(body)
            ):
                raise ValueError(
                    "Параметры операции выдачи изменились. Нужна проверка заказа."
                )
        else:
            original = body
        reservation = await session.get(VlessReservation, identity)
        if reservation and reservation.server_id != panel.id:
            raise ValueError("Резерв выдачи закреплён за другой панелью.")
        if reservation and reservation.lease_token and reservation.lease_until:
            if utc(reservation.lease_until) > now():
                return None  # Wait without holding a PostgreSQL lock.
        occupied = await pending(session, panel.id)
        if not reservation or reservation.state == "released":
            # Associated legacy operations have already consumed a slot.
            associated = operation and operation.credential_id is not None
            if not associated and (
                not available(panel) or panel.users_total + occupied >= panel.max_users
            ):
                raise CapacityUnavailable(
                    "В панели VLESS нет свободных мест для выдачи."
                )
            if not reservation:
                reservation = VlessReservation(
                    operation_id=identity,
                    server_id=panel.id,
                    state="consumed" if associated else "reserved",
                    attempts=0,
                )
                session.add(reservation)
            else:
                reservation.state = "reserved"
            if not associated:
                occupied += 1
        if not operation:
            operation = VlessCreation(
                id=identity,
                user_id=user_id,
                customer_ref=customer,
                server_id=panel.id,
                server_url=panel.api_url,
                request=json.dumps(body),
                state="outcome_unknown",
                dispatch_state="not_started",
                placement=json.dumps(
                    {"mode": "automatic" if automatic else "pinned", "visited": []}
                ),
            )
            session.add(operation)
        pristine = (
            operation.dispatch_state in {"not_started", "not_created"}
            and operation.result is None
        )
        token = secrets.token_hex(16)
        reservation.lease_token = token
        reservation.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
        reservation.attempts += 1
        reservation.updated_at = now()
        operation.state = "outcome_unknown"
        panel.capacity_revision += 1
        return Claim(
            token,
            panel,
            original,
            operation.result,
            pristine,
            available(panel)
            and reservation.state == "reserved"
            and panel.users_total + occupied <= panel.max_users,
        )


async def finish(repo, identity, claim, *, result=None, failure=None, rejected=False):
    async with durable_session(repo) as session, session.begin():
        # A caller may still hold the local-association lock. Wait for it BEFORE
        # taking the panel lock, so unrelated admissions/batch grants can proceed.
        # Admission's SKIP LOCKED above prevents the opposite lock-order cycle.
        operation = await session.get(VlessCreation, identity, with_for_update=True)
        panel = await session.get(RemnawaveServer, claim.panel.id, with_for_update=True)
        reservation = await session.get(VlessReservation, identity)
        if (
            not panel
            or not operation
            or not reservation
            or reservation.lease_token != claim.token
        ):
            raise ValueError(
                "Выдача VLESS уже проверяется другим запросом. Повторите исходный заказ."
            )
        if result is not None:
            if reservation.state == "reserved":
                # Remote usage survives the caller's later rollback. This replaces
                # the old increment in the local subscription transaction.
                panel.users_total += 1
                reservation.state = "consumed"
            operation.dispatch_state = "confirmed"
            operation.result = json.dumps(result)
            operation.state = (
                "associated" if operation.credential_id else "remote_confirmed"
            )
            operation.last_error = ""
        else:
            operation.state = "failed" if rejected else "outcome_unknown"
            if rejected:
                operation.dispatch_state = "not_created"
            operation.last_error = (
                f"{type(failure).__name__}: HTTP {getattr(failure, 'status', 0)}"
            )
            if rejected and reservation.state == "reserved":
                reservation.state = "released"
        reservation.lease_token = ""
        reservation.lease_until = None
        reservation.updated_at = operation.updated_at = now()
        panel.capacity_revision += 1


async def clear_failed_commit_lease(repo, identity, claim):
    # Best effort after a failed confirmation commit; never release capacity.
    # On a total DB outage/crash, the durable lease expires instead.
    async with durable_session(repo) as session, session.begin():
        panel = await session.get(RemnawaveServer, claim.panel.id, with_for_update=True)
        reservation = await session.get(VlessReservation, identity)
        if panel and reservation and reservation.lease_token == claim.token:
            reservation.lease_token = ""
            reservation.lease_until = None
            panel.capacity_revision += 1


async def refresh_version(repo, server_id):
    async with durable_session(repo) as session:
        return await session.scalar(
            select(RemnawaveServer.capacity_revision).where(
                RemnawaveServer.id == server_id
            )
        )


async def refresh_total(repo, item, version, total, *, session=None):
    if session is None:
        async with durable_session(repo) as independent, independent.begin():
            await refresh_total(repo, item, version, total, session=independent)
        return
    panel = await session.get(RemnawaveServer, item.id, with_for_update=True)
    if panel is None:
        return  # A newly added panel is not yet committed by its caller.
    if (
        version is not None
        and version == panel.capacity_revision
        and not await pending(session, panel.id)
        and total is not None
    ):
        # A stats snapshot taken during/preceding provisioning cannot erase
        # a durable reservation or double-count its remotely created user.
        panel.users_total = max(0, total)
        panel.capacity_revision += 1
    # Do not make the caller's stale ORM copy dirty: its later commit must
    # never overwrite another operation's capacity counter.
    set_committed_value(item, "users_total", panel.users_total)
    set_committed_value(item, "capacity_revision", panel.capacity_revision)
