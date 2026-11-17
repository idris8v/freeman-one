"""Safe new-key placement changes; never move a confirmed or uncertain key."""

import json

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.database.models import RemnawaveServer, VlessCreation, VlessReservation, now
from app.services.vless_creation import durable_session, utc


class PlacementChanged(ValueError):
    pass


class NotCreated(ValueError):
    """No create was dispatched, or a pristine rejection and absence were verified."""


def confirmed_absence(error):
    return error.status == 404 and error.error_code in {"A025", "A063"}


def safe_rejection(claim, creation, lookup):
    return (
        claim.pristine
        and creation.status in {400, 401, 403, 422}
        and confirmed_absence(lookup)
    )


def placement(operation):
    try:
        value = json.loads(operation.placement)
        if (
            not isinstance(value, dict)
            or value.get("mode") not in {"automatic", "pinned"}
            or not isinstance(value.get("visited"), list)
            or any(type(pid) is not int for pid in value["visited"])
        ):
            return {"mode": "pinned", "visited": []}
        return value
    except (ValueError, TypeError):
        return {"mode": "pinned", "visited": []}


async def dispatch(repo, identity, claim):
    """Commit uncertainty BEFORE POST; fence workers paused beyond their lease.

    Even after lease expiry, an uncertain POST remains on the same panel. A
    successor's absent GET or rejected retry cannot erase the earlier uncertainty.
    """
    async with durable_session(repo) as session, session.begin():
        op = await session.get(VlessCreation, identity, with_for_update=True)
        reservation = await session.get(VlessReservation, identity)
        if (
            not op
            or not reservation
            or op.server_id != claim.panel.id
            or op.server_url != claim.panel.api_url
            or op.result is not None
            or op.credential_id is not None
            or reservation.server_id != op.server_id
            or reservation.state != "reserved"
            or reservation.lease_token != claim.token
            or not reservation.lease_until
            or utc(reservation.lease_until) <= now()
        ):
            raise PlacementChanged("Выдача VLESS уже проверяется другим запросом.")
        op.dispatch_state = "outcome_unknown"
        op.updated_at = now()


async def move(repo, identity, source_id, *, excluded=()):
    try:
        return await _move(repo, identity, source_id, excluded=excluded)
    except SQLAlchemyError as error:
        raise ValueError(
            "Не удалось сохранить резерв VLESS. Повторите исходный заказ; повторно платить не нужно."
        ) from error


async def _move(repo, identity, source_id, *, excluded=()):
    """Atomically move only a positively resolved, unassociated operation.

    Keep the same operation ID, username, customer, expiry and purchased limits.
    Reuse I03 admission accounting; no database lock spans external HTTP.
    """
    from app.services.vless_capacity import available, pending

    async with durable_session(repo) as session, session.begin():
        op = await session.get(VlessCreation, identity, with_for_update=True)
        reservation = await session.get(VlessReservation, identity)
        if not op or op.server_id != source_id:
            raise PlacementChanged("Панель операции выдачи VLESS изменилась.")
        policy = placement(op)
        if (
            policy["mode"] != "automatic"
            or op.state != "failed"
            or op.dispatch_state != "not_created"
            or op.result is not None
            or op.credential_id is not None
            or op.subscription_id is not None
            or not reservation
            or reservation.server_id != source_id
            or reservation.state != "released"
            or reservation.lease_token
        ):
            return None
        visited = set(policy["visited"]) | {source_id}
        panels = await session.scalars(
            select(RemnawaveServer)
            .where(
                RemnawaveServer.id.not_in(set(excluded) | {source_id}),
                RemnawaveServer.status == "active",
                RemnawaveServer.health != "offline",
                RemnawaveServer.internal_squad_uuid != "",
            )
            .order_by(RemnawaveServer.priority, RemnawaveServer.id)
            .with_for_update(skip_locked=True)
        )
        for panel in panels:
            if (
                not available(panel)
                or panel.users_total + await pending(session, panel.id)
                >= panel.max_users
            ):
                continue
            body = json.loads(op.request)
            body["activeInternalSquads"] = [panel.internal_squad_uuid]
            op.server_id, op.server_url = panel.id, panel.api_url
            op.request = json.dumps(body)
            op.placement = json.dumps({"mode": "automatic", "visited": sorted(visited)})
            op.dispatch_state, op.state = "not_started", "outcome_unknown"
            op.updated_at = now()
            reservation.server_id = panel.id
            reservation.state = "reserved"
            reservation.attempts = 0
            reservation.lease_until = None
            reservation.updated_at = now()
            panel.capacity_revision += 1
            return panel
        return None
